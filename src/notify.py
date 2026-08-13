"""Outbound Telegram alerts.

Ported from hype_arb's src/tg/notifier.py, with the SQLite audit row replaced
by an events.log_alert() JSONL line. escape_md_v2() and the reserved-character
set are copied VERBATIM: Telegram rejects any message containing an unescaped
MarkdownV2 reserved character, these alerts are full of `.`, `-`, `(`, `)`, and
that is a solved problem not worth re-solving.

Design constraints, unchanged from that module:
- Must never crash the monitor. All network/HTTP failures are caught and logged.
- Must be a no-op when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is unset (dev
  convenience; it must stay possible to run the monitor with no bot at all).
- Every attempt (success or failure) is logged to alerts.jsonl.

The monitor never calls this module's transport directly on its own behalf: it
produces a decision, and the caller hands that decision here. That seam is what
lets the executor later consume the same decisions, and it keeps the logic
worth testing from being welded to a network call.

The bot token is a full credential -- anyone holding it controls the bot. It
lives in .env, is never committed, and is never logged, not even at DEBUG.
"""

import logging
import os

import requests

import events

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {
    "INFO": "\U0001f7e2",
    "WARNING": "\U0001f7e0",
    "CRITICAL": "\U0001f534",
}

_TIMEOUT_SECONDS = 5.0

# MarkdownV2 reserves these characters; any literal occurrence in message
# text/values must be backslash-escaped.
_MD_V2_RESERVED = r"_*[]()~`>#+-=|{}.!"


def escape_md_v2(text):
    """Escape MarkdownV2 reserved characters in free-form text."""
    out = []
    for ch in text:
        if ch in _MD_V2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def escape_md_v2_block(text):
    """Escape for the INSIDE of a fenced code block, where only backslash and
    backtick are special. A separate helper on purpose: running block content
    through escape_md_v2 puts literal backslashes on the reader's screen in
    front of every `.` and `-`, which the aligned number columns are full of.
    """
    return text.replace("\\", "\\\\").replace("`", "\\`")


def fenced_block(text):
    """Wrap plain text in a MarkdownV2 fenced code block, escaped for the
    inside of one. The block renders monospaced on every client, which is the
    only way space-aligned number columns survive a phone screen."""
    return f"```\n{escape_md_v2_block(text)}\n```"


# Severity per trip, straight from the spec's table: trips are WARNING; the
# two that mean the position is already in trouble are CRITICAL. Lives here
# rather than in monitor.py because the bot's /help is generated from the same
# table and the bot must not import the monitor (which drags in the broker --
# exactly one process holds an IBKR connection).
TRIGGER_SEVERITY = {
    "drawdown stop": "CRITICAL",
    "margin de-risk": "CRITICAL",
    "foil decay band": "WARNING",
    "long-short band": "WARNING",
}

_TRIP_MEANINGS = {
    "drawdown stop": "Equity fell past the drawdown threshold. Terminal in "
                     "the backtest; here it needs a human.",
    "margin de-risk": "The margin cushion went negative. The position must "
                      "shrink.",
    "foil decay band": "The short leg drifted past the foil decay band. "
                       "Rebalance both legs.",
    "long-short band": "Net delta drifted past the long-short band. Resize "
                       "the long leg only.",
}

# Every message the bot can send unprompted. The spec counts nine kinds; the
# table has ten rows because leg_check appears twice (WARNING when a read
# fails, INFO when it recovers) -- both rows belong here. /help renders this
# list so an unexpected alert is recognisable rather than alarming, and
# the four trip entries take their severity from TRIGGER_SEVERITY above so the
# two can never drift apart: this is exactly the kind of hand-maintained list
# that drifts, so it is not hand-maintained twice.
ALERT_KINDS = tuple(
    [{"name": name, "severity": TRIGGER_SEVERITY[name],
      "meaning": _TRIP_MEANINGS[name]} for name in TRIGGER_SEVERITY]
    + [
        {"name": "sanity", "severity": "WARNING",
         "meaning": "base_capital exceeds NLV -- a sizing input is wrong, and "
                    "band trips are suppressed as unactionable while it holds."},
        {"name": "leg check failed", "severity": "WARNING",
         "meaning": "A leg could not be read from IBKR: missing, flat, or "
                    "wrongly signed. No decisions until both legs read."},
        {"name": "resolved", "severity": "INFO",
         "meaning": "A previously tripped band is back inside. No action "
                    "required."},
        {"name": "reconnected", "severity": "INFO",
         "meaning": "Gateway connection re-established after a drop."},
        {"name": "position readable", "severity": "INFO",
         "meaning": "Both legs read correctly again after a failed read."},
        {"name": "heartbeat", "severity": "INFO",
         "meaning": "Daily 09:45 ET proof-of-life. Silence is the signal, "
                    "not the message."},
    ]
)


def credentials():
    """(token, chat_id) from the environment. Either may be None."""
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        os.environ.get("TELEGRAM_CHAT_ID") or None,
    )


def format_message(severity, title, body, ts):
    """Build the MarkdownV2-formatted payload for a Telegram message.

    title and body are caller-provided strings that may contain MarkdownV2
    reserved characters; both are escaped. The severity emoji and bold
    wrappers around the title are literal MarkdownV2 syntax and are not
    escaped.
    """
    emoji = _SEVERITY_EMOJI.get(severity, "")
    title_esc = escape_md_v2(title)
    body_esc = escape_md_v2(body)
    ts_esc = escape_md_v2(ts)
    return f"{emoji} *{title_esc}*\n{body_esc}\n_{ts_esc}_"


def send_text(token, chat_id, text, reply_markup=None):
    """Send one MarkdownV2 message. Returns (delivered, error, message_id).

    Public since spec 008: the command poller replies through this too, and a
    sibling process reaching into a private was the alternative. Callers own
    their own audit trail -- notify() logs to alerts.jsonl, the poller to
    commands.jsonl -- because a /status reply is not an alert and must not
    pollute the file that answers "did an alert get delivered".

    message_id is returned so the monitor can later edit the inline keyboard
    off a superseded trip alert. reply_markup is passed through verbatim
    (Telegram's InlineKeyboardMarkup shape).
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}", None

    try:
        body = resp.json()
    except ValueError:
        body = {}

    if resp.status_code == 200 and body.get("ok"):
        return True, None, body.get("result", {}).get("message_id")

    # A basic group converting to a supergroup CHANGES THE CHAT ID, and the
    # sink is deliberately non-fatal -- without this, every later send fails
    # quietly into alerts.jsonl and the bot just goes silent. A silent
    # alerting channel on a live position is the worst failure mode in this
    # system, so this one error states its own remedy at ERROR level.
    migrated = (body.get("parameters") or {}).get("migrate_to_chat_id")
    if migrated:
        logger.error(
            "Telegram chat %s has migrated to %s (basic group became a "
            "supergroup). Every send will fail until TELEGRAM_CHAT_ID in "
            ".env is updated to the new id and the process restarted. Not "
            "auto-updating: a process that rewrites its own credentials file "
            "to reach a chat it was not configured for is worse to own than "
            "an outage.",
            chat_id, migrated,
        )

    # Truncate TG error payloads -- they are verbose and the tail is rarely the
    # informative part.
    detail = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}: {detail}", None


def answer_callback(token, callback_query_id, text=None):
    """Acknowledge a button press. Telegram shows a spinner until this is
    called and an error if it takes more than a few seconds, so the poller
    calls it before doing anything else. Returns (delivered, error)."""
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if resp.status_code == 200 and resp.json().get("ok"):
        return True, None
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def remove_keyboard(token, chat_id, message_id):
    """Edit the inline keyboard off a sent message. Used when a fresh trip
    alert supersedes an older one: a stale button that cannot be pressed is
    better than one that can. Returns (edited, error)."""
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "message_id": message_id,
                  "reply_markup": {"inline_keyboard": []}},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if resp.status_code == 200 and resp.json().get("ok"):
        return True, None
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def notify(token, chat_id, severity, kind, title, body, ts=None,
           reply_markup=None):
    """Send a Telegram alert and record the attempt to alerts.jsonl.

    Returns (delivered, message_id). delivered is False (without raising) when:
      - TG is unconfigured (no token or no chat_id) -- silent no-op
      - the HTTP request failed for any reason
      - Telegram returned a non-ok response

    message_id is None unless delivered; the monitor stores it for trip alerts
    so a superseded alert's keyboard can be edited off. reply_markup rides
    through to send_text untouched.

    The JSONL row is written on both the success and failure paths, so a silent
    bot can be told apart from a quiet market. It is NOT written on the
    unconfigured path: running without a bot is a supported mode, not an
    incident, and logging a failed "send" every cycle would be noise.
    """
    if not token or not chat_id:
        logger.debug("Telegram not configured -- skipping alert: %s", title)
        return False, None

    ts = ts or events.now_iso()
    text = format_message(severity, title, body, ts)
    delivered, error, message_id = send_text(token, chat_id, text,
                                             reply_markup=reply_markup)

    if delivered:
        logger.info("Telegram alert delivered: [%s] %s", severity, title)
    else:
        logger.warning("Telegram alert failed: [%s] %s -- %s", severity, title, error)

    events.log_alert({
        "ts": ts,
        "severity": severity,
        "kind": kind,
        "title": title,
        "body": body,
        "delivered": delivered,
        "error": error,
    })

    return delivered, message_id
