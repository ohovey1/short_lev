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


def _send_http(token, chat_id, text):
    """Execute the HTTPS POST. Returns (delivered, error_summary)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if resp.status_code == 200 and resp.json().get("ok"):
        return True, None

    # Truncate TG error payloads -- they are verbose and the tail is rarely the
    # informative part.
    detail = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}: {detail}"


def notify(token, chat_id, severity, kind, title, body, ts=None):
    """Send a Telegram alert and record the attempt to alerts.jsonl.

    Returns True if delivered. Returns False (without raising) when:
      - TG is unconfigured (no token or no chat_id) -- silent no-op
      - the HTTP request failed for any reason
      - Telegram returned a non-ok response

    The JSONL row is written on both the success and failure paths, so a silent
    bot can be told apart from a quiet market. It is NOT written on the
    unconfigured path: running without a bot is a supported mode, not an
    incident, and logging a failed "send" every cycle would be noise.
    """
    if not token or not chat_id:
        logger.debug("Telegram not configured -- skipping alert: %s", title)
        return False

    ts = ts or events.now_iso()
    text = format_message(severity, title, body, ts)
    delivered, error = _send_http(token, chat_id, text)

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

    return delivered
