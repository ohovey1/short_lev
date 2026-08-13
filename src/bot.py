"""The Telegram command poller. Reads files, answers commands, records intents.

THE invariant, and it is the one that matters in spec 008: exactly one process
holds an IBKR connection, and it is not this one. This module imports no
broker, no ib_async, no monitor (which would drag the broker in transitively),
and holds no client id. Everything it reports comes from files the monitor
writes -- runtime.json for /status, the widened checks.jsonl row for
/positions and /account -- and everything it wants to CAUSE is appended to
intents.jsonl for the monitor to consume. A poller that can read the account
is one small change away from a poller that can act on it, at which point two
processes can decide to trade the same position and neither knows about the
other.

Because there is no ib_async event loop in this process, time.sleep here is
correct and ib.sleep is impossible -- ib.sleep is the rule for the monitor,
whose asyncio loop a blocking sleep starves. Do not copy sleeps between the
two files in either direction.

Authorization is one equality check on the chat id: group membership is the
access model (spec 008 section 3), decided deliberately over roles. Strangers
who find the bot by username get silence -- an "unauthorized" reply would
confirm the bot is live and that their message arrived -- plus one INFO line,
because a stream of those is the signal the username has been found.

Long-polling only. The box exposes SSH and nothing else, so a webhook is out
by construction.
"""

import datetime
import json
import logging
import os
import time
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import approval
import config
import events
import notify
import runtime_state

load_dotenv()

log = logging.getLogger("bot")

ET = ZoneInfo("America/New_York")

LONG_POLL_SECONDS = 30
# Comfortably past the long-poll window, so a healthy empty poll is not a
# timeout error.
REQUEST_TIMEOUT_SECONDS = LONG_POLL_SECONDS + 10

BACKOFF_START = 5
BACKOFF_MAX = 300

# A reply is prefixed STALE once the newest check is older than this many poll
# intervals -- a monitor that died forty minutes ago otherwise produces a
# reply that looks identical to a healthy one.
STALE_POLL_INTERVALS = 3

DEFAULT_BOT_OFFSET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state",
    "bot_offset.json"
)


def _env_float(name, default):
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def bot_offset_path():
    return os.environ.get("BOT_OFFSET_PATH") or DEFAULT_BOT_OFFSET_PATH


def load_offset(path):
    """The last processed update id, 0 if none. Malformed resets with a
    warning: the cost is re-answering at most one batch of commands, and a
    poller that refuses to start over a corrupt cursor is a poller that is
    down."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            return int(json.load(f)["offset"])
    except Exception as exc:
        log.warning("bot offset at %s is unreadable (%s: %s) -- resetting to 0",
                    path, type(exc).__name__, exc)
        return 0


def save_offset(path, offset):
    """Persist after each handled update. Without this a restart either
    re-answers commands -- including, now that buttons exist, re-recording a
    button press -- or silently skips the ones that arrived while down."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"offset": offset}, f)
            f.write("\n")
    except Exception as exc:
        log.warning("could not save bot offset to %s: %s: %s",
                    path, type(exc).__name__, exc)


def action_user_ids():
    """The optional allowlist for button presses. Empty means everyone in the
    group -- membership is the authorization model, and a role system is
    complexity three trusted people have not earned (spec 008 section 7,
    flagged for review there)."""
    raw = os.environ.get("TELEGRAM_ACTION_USER_IDS") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def get_updates(token, offset):
    """One long poll. Raises on transport or API failure; run() owns backoff."""
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"timeout": LONG_POLL_SECONDS, "offset": offset + 1},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    body = resp.json()
    if resp.status_code != 200 or not body.get("ok"):
        raise RuntimeError(f"getUpdates HTTP {resp.status_code}: {resp.text[:200]}")
    return body.get("result", [])


# ---------------------------------------------------------------------------
# Time and formatting helpers. Everything user-facing goes through these so
# the rules from spec 008 section 5 live in one place: times as 14:32 ET,
# never a bare ISO string; label then value; numbers inside a fenced monospace
# block because Telegram's proportional font collapses space-aligned columns.

def _parse_ts(ts):
    if not ts:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Same reasoning as alert_state._match_awareness: the box that wrote a
        # naive timestamp ran ET.
        parsed = parsed.replace(tzinfo=ET)
    return parsed


def _age_seconds(ts, now):
    parsed = _parse_ts(ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds()


def _fmt_et(ts):
    parsed = _parse_ts(ts)
    return parsed.astimezone(ET).strftime("%H:%M ET") if parsed else "unknown"


def _fmt_age(seconds):
    if seconds is None:
        return "age unknown"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "under a minute ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"
    return f"{seconds // 86400} d ago"


def _as_of(ts, now):
    if _parse_ts(ts) is None:
        return "no data recorded yet"
    return f"as of {_fmt_et(ts)} ({_fmt_age(_age_seconds(ts, now))})"


def _fmt_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


def _stale_banner(ts, now, poll_seconds):
    """The STALE prefix, or None while the data is fresh enough to trust."""
    age = _age_seconds(ts, now)
    if age is not None and age <= STALE_POLL_INTERVALS * poll_seconds:
        return None
    return ("STALE: last data is "
            f"{_fmt_age(age).replace(' ago', '') if age is not None else 'missing'}"
            " old -- the monitor may be down.")


def _assemble(now, poll_seconds, data_ts, header, block_text, footer=None):
    """Prose framing outside the fenced block, aligned figures inside it."""
    parts = []
    banner = _stale_banner(data_ts, now, poll_seconds)
    if banner:
        parts.append(notify.escape_md_v2(banner))
    parts.append(header)
    parts.append(notify.escape_md_v2(_as_of(data_ts, now)))
    if block_text:
        parts.append(notify.fenced_block(block_text))
    if footer:
        parts.append(notify.escape_md_v2(footer))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# The four commands. Each build_* takes its data and `now` as arguments so
# scripts/verify_bot.py can drive them offline against synthetic files.

def status_verdict(runtime, now, poll_seconds, alerting_configured=True):
    """(verdict, reason). The one-line answer to "should I trust anything
    else this bot tells me" -- readable at 3 AM by someone who will not parse
    a table."""
    if runtime is None:
        return "Stale", "no runtime data on disk -- the monitor may never have started"

    if runtime.get("connected") is False:
        return "Disconnected", "the monitor reports no Gateway connection"

    check_age = _age_seconds(runtime.get("last_check_ts"), now)
    if check_age is None or check_age > STALE_POLL_INTERVALS * poll_seconds:
        return "Stale", "the last check is older than three poll intervals"

    error = runtime.get("last_error") or None
    if error:
        error_ts = _parse_ts(error.get("ts"))
        check_ts = _parse_ts(runtime.get("last_check_ts"))
        if error_ts and check_ts and error_ts > check_ts:
            return "Degraded", "the most recent cycle recorded an error"

    if not alerting_configured:
        return "Degraded", "Telegram alerting is not configured"

    return "Healthy", None


def build_status(runtime, now, poll_seconds, alerting_configured=True):
    verdict, reason = status_verdict(runtime, now, poll_seconds,
                                     alerting_configured)
    header = f"*{notify.escape_md_v2(verdict)}*"
    if reason:
        header += "\n" + notify.escape_md_v2(reason)

    if runtime is None:
        return _assemble(now, poll_seconds, None, header, None)

    error = runtime.get("last_error") or None
    if error:
        error_line = (f"{error.get('message', 'unknown')} "
                      f"(at {_fmt_et(error.get('ts'))})")
    else:
        error_line = "none"

    uptime = _fmt_duration(_age_seconds(runtime.get("started_at"), now))
    connect_ts = runtime.get("last_connect_ts")
    connected_line = (
        f"yes, since {_fmt_et(connect_ts)}" if runtime.get("connected")
        else "NO -- reconnect in progress"
    )
    block = "\n".join([
        f"connected    {connected_line}",
        f"uptime       {uptime} (started {_fmt_et(runtime.get('started_at'))})",
        f"checks       {runtime.get('cycles_since_start', 0)} since start",
        f"last check   {_fmt_et(runtime.get('last_check_ts'))} "
        f"({_fmt_age(_age_seconds(runtime.get('last_check_ts'), now))})",
        f"last error   {error_line}",
        f"alerting     {'configured' if alerting_configured else 'NOT configured'}",
    ])
    return _assemble(now, poll_seconds, runtime.get("last_check_ts"),
                     header, block)


def _leg_lines(label, ticker, shares, price, avg_cost, notional, short_leg):
    if shares is None or price is None:
        return [f"{ticker:<5} {label}  detail not recorded this cycle",
                f"      notional {notional:,.2f}"]
    # Every displayed dollar figure derives from the displayed price: a phone
    # reader WILL multiply shares by the two-decimal price shown, and the
    # result must be the stated notional to the cent (same discipline as
    # format_trade_line). The row's marketValue still drives the band math.
    shown_price = round(price, 2)
    shown_notional = shares * shown_price
    lines = [f"{ticker:<5} {label:<5} {shares:,.0f} sh @ {shown_price:,.2f}"]
    if avg_cost is not None:
        # For a short, price falling below the average open is the winning
        # direction -- hence the sign flip.
        unrealized = (round(avg_cost, 2) - shown_price if short_leg
                      else shown_price - round(avg_cost, 2)) * shares
        lines[0] += f"  (avg {avg_cost:,.2f})"
        lines.append(f"      notional {shown_notional:,.2f}   "
                     f"unrealized {unrealized:+,.2f}")
    else:
        lines.append(f"      notional {shown_notional:,.2f}")
    return lines


def build_positions(row, now, poll_seconds):
    if row is None:
        return notify.escape_md_v2(
            "No checks recorded yet -- the monitor has not completed a cycle.")

    pair = config.PAIRS.get(row.get("pair"), {})
    lev = pair.get("leveraged_ticker", "short leg")
    und = pair.get("underlying_ticker", "long leg")
    header = notify.escape_md_v2(f"{lev}/{und} -- position and bands")

    leg_block = _leg_lines("short", lev, row.get("short_shares"),
                           row.get("short_price"), row.get("short_avg_cost"),
                           row.get("short_notional", 0.0), short_leg=True)
    leg_block += _leg_lines("long", und, row.get("long_shares"),
                            row.get("long_price"), row.get("long_avg_cost"),
                            row.get("long_notional", 0.0), short_leg=False)

    # Band distance as a percentage OF THE BAND: "12.3% of a 15.0% band" is
    # readable on a phone; "$4,684 vs $4,910" is not.
    target = row.get("target") or 0.0
    band_block = []
    if target:
        drift = (row.get("short_notional", 0.0) - target) / target
        foil = row.get("foil_decay_band")
        if foil:
            band_block.append(
                f"short vs target  {drift * 100:+.1f}%  "
                f"= {abs(drift) / foil * 100:.1f}% of a {foil * 100:.1f}% band")
        net_delta = row.get("net_delta", 0.0)
        long_short = row.get("long_short_band")
        if long_short:
            band_block.append(
                f"net delta        {net_delta:+,.2f}  "
                f"= {abs(net_delta) / target / long_short * 100:.1f}% "
                f"of a {long_short * 100:.1f}% band")
    trigger = row.get("trigger")
    if trigger:
        suffix = " (suppressed as unactionable)" if row.get("unactionable") else ""
        band_block.append(f"trigger          {trigger}{suffix}")
    else:
        band_block.append("trigger          no band tripped")

    block = "\n".join(leg_block) + "\n\n" + "\n".join(band_block)
    return _assemble(now, poll_seconds, row.get("ts"), header, block)


def build_account(row, now, poll_seconds):
    if row is None:
        return notify.escape_md_v2(
            "No checks recorded yet -- the monitor has not completed a cycle.")

    header = notify.escape_md_v2("account")
    equity = row.get("account_equity", 0.0)
    peak = row.get("peak_equity") or 0.0
    ibkr_maint = row.get("ibkr_maint")
    modeled = row.get("modeled_maint")

    lines = [
        f"NLV            {equity:,.2f}",
        f"maint (IBKR)   {ibkr_maint:,.2f}" if ibkr_maint is not None
        else "maint (IBKR)   not recorded",
        f"cushion        {row.get('margin_cushion', 0.0):+,.2f}",
        f"peak equity    {peak:,.2f}",
    ]
    if peak:
        lines.append(f"drawdown       {(equity - peak) / peak * 100:+.1f}% from peak")
    # The line that earns this command: modeled vs reported, side by side.
    # The 1.013 ratio came from two hand readings; the open question from spec
    # 006 is whether MaintMarginReq is even the binding constraint, and the
    # reading we most want is one taken during a stress moment, from a phone.
    if ibkr_maint is not None and modeled:
        lines.append(f"margin model   {modeled:,.2f} vs IBKR {ibkr_maint:,.2f}"
                     f"  (ratio {ibkr_maint / modeled:.3f})")

    return _assemble(now, poll_seconds, row.get("ts"), header, "\n".join(lines))


def build_help():
    """Written for the stakeholder, not for us. The alerts section is
    generated from notify.ALERT_KINDS -- the same table the monitor's
    severities come from -- because a /help that drifts out of date is worse
    than no /help."""
    e = notify.escape_md_v2
    lines = [
        e("Band monitor for the TSLL/TSLA delta-neutral position. It reads "
          "and alerts; it never places orders."),
        "",
        "*" + e("Commands") + "*",
        e("/status -- is the monitor alive, and should you trust it"),
        e("/positions -- the legs and where they sit against the bands"),
        e("/account -- equity, margin, and drawdown"),
        e("/help -- this message"),
        "",
        "*" + e("Alerts you may see unprompted") + "*",
    ]
    for severity in ("CRITICAL", "WARNING", "INFO"):
        lines.append("_" + e(severity) + "_")
        for kind in notify.ALERT_KINDS:
            if kind["severity"] == severity:
                lines.append(e(f"{kind['name']} -- {kind['meaning']}"))
        lines.append("")
    lines.append(e(
        "Both CRITICAL alerts always warrant attention. A missing heartbeat "
        "matters more than any single alert -- one arrives every trading day "
        "at 09:45 ET, and silence past it means the monitor needs looking at."))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading the monitor's files.

def _tail_line(path):
    """The last non-empty line of a file, without reading all of it -- the
    checks log grows by ~100 lines a day forever."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in chunk.splitlines() if line.strip()]
    return lines[-1] if lines else None


def latest_check():
    """The newest checks.jsonl row, or None."""
    path = os.path.join(events.event_log_dir(), events.CHECKS_FILE)
    line = _tail_line(path)
    if line is None:
        return None
    try:
        row = json.loads(line)
        return row if isinstance(row, dict) else None
    except ValueError:
        return None


def dispatch(command, poll_seconds, now=None, alerting_configured=True):
    """Command name in, MarkdownV2 reply out. Pure given its file reads."""
    now = now or datetime.datetime.now(ET)
    if command == "/status":
        runtime = runtime_state.read(runtime_state.runtime_path())
        return build_status(runtime, now, poll_seconds, alerting_configured)
    if command == "/positions":
        return build_positions(latest_check(), now, poll_seconds)
    if command == "/account":
        return build_account(latest_check(), now, poll_seconds)
    if command == "/help":
        return build_help()
    return notify.escape_md_v2(
        "Unknown command. /help lists what I can answer.")


# ---------------------------------------------------------------------------
# Update handling.

def handle_message(message, token, configured_chat_id, poll_seconds):
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    chat_id = message.get("chat", {}).get("id")
    from_user = message.get("from", {}).get("id")
    command = text.split()[0].split("@")[0].lower()

    # The authorization gate: one equality check on the chat id, before any
    # dispatch. Reply NOTHING on failure -- an error message confirms the bot
    # is live and tells a stranger their message was received. Log it, because
    # a stream of these is the signal the username has been found.
    if str(chat_id) != str(configured_chat_id):
        log.info("unauthorized command %s from chat %s -- ignoring",
                 command, chat_id)
        events.log_command({"from_user_id": from_user, "command": command,
                            "chat_id": chat_id, "authorized": False,
                            "delivered": False})
        return

    reply = dispatch(command, poll_seconds)
    delivered, error, _ = notify.send_text(token, configured_chat_id, reply)
    if not delivered:
        log.warning("reply to %s failed: %s", command, error)
    events.log_command({"from_user_id": from_user, "command": command,
                        "authorized": True, "delivered": delivered})


def handle_callback(callback, token, configured_chat_id, allowed_user_ids):
    """A button press. Answer the spinner immediately, then write the intent
    -- the monitor does everything real, freshly, on its side of the file."""
    data = callback.get("data") or ""
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    from_user = callback.get("from", {}).get("id")
    label = f"callback:{data}"

    if str(chat_id) != str(configured_chat_id):
        # Same silence as commands; the stranger's spinner times out on its own.
        log.info("unauthorized callback %r from chat %s -- ignoring",
                 data, chat_id)
        events.log_command({"from_user_id": from_user, "command": label,
                            "chat_id": chat_id, "authorized": False,
                            "delivered": False})
        return

    prefix, _, intent_ref = data.partition(":")
    kind = {"req": "request", "confirm": "confirm", "cancel": "cancel"}.get(prefix)
    if kind is None or not intent_ref:
        notify.answer_callback(token, callback.get("id"), "Unrecognized button.")
        return

    if allowed_user_ids and str(from_user) not in allowed_user_ids:
        notify.answer_callback(
            token, callback.get("id"),
            "Not authorized to act -- ask to be added to TELEGRAM_ACTION_USER_IDS.")
        log.info("callback %r from user %s refused by action allowlist",
                 data, from_user)
        events.log_command({"from_user_id": from_user, "command": label,
                            "authorized": False, "delivered": True})
        return

    # Telegram shows a spinner until this call and an error after a few
    # seconds, so it comes before anything else.
    ack = ("Re-deriving against live marks -- reply follows within about "
           "ten seconds." if kind == "request"
           else "Recorded -- processing within about ten seconds.")
    notify.answer_callback(token, callback.get("id"), ack)

    record = {"kind": kind, "from_user_id": from_user, "ts": events.now_iso()}
    record["decision_id" if kind == "request" else "proposal_id"] = intent_ref
    written = approval.append_intent(approval.intents_path(), record)
    if not written:
        notify.send_text(token, configured_chat_id, notify.escape_md_v2(
            "Could not record that button press -- check the bot logs and "
            "press again."))
    events.log_command({"from_user_id": from_user, "command": label,
                        "authorized": True, "delivered": written})


def handle_update(update, token, configured_chat_id, allowed_user_ids,
                  poll_seconds):
    if "callback_query" in update:
        handle_callback(update["callback_query"], token, configured_chat_id,
                        allowed_user_ids)
    elif "message" in update:
        handle_message(update["message"], token, configured_chat_id,
                       poll_seconds)


def run():
    token, chat_id = notify.credentials()
    if not token or not chat_id:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required. The "
            "monitor degrades to a no-op without them; a command poller "
            "without them has nothing to do at all."
        )

    allowed = action_user_ids()
    poll_seconds = _env_float("POLL_INTERVAL_SECONDS", 900)
    offset_path = bot_offset_path()
    offset = load_offset(offset_path)
    backoff = BACKOFF_START

    log.info(
        "bot: chat_id=%s action_allowlist=%s offset=%d (%s) intents=%s",
        chat_id, ",".join(sorted(allowed)) or "everyone in the group",
        offset, offset_path, approval.intents_path(),
    )

    while True:
        try:
            updates = get_updates(token, offset)
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
            return
        except Exception as exc:
            # Same treatment as network failure everywhere else in this
            # codebase: one WARNING, backoff, never exit.
            log.warning("getUpdates failed: %s: %s -- retrying in %ds",
                        type(exc).__name__, exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        backoff = BACKOFF_START
        for update in updates:
            try:
                handle_update(update, token, chat_id, allowed, poll_seconds)
            except Exception:
                # One bad update must not wedge the poller on it forever.
                log.exception("update %s failed; skipping",
                              update.get("update_id"))
            # Persisted after EVERY handled update, so a restart neither
            # re-answers nor skips.
            offset = update.get("update_id", offset)
            save_offset(offset_path, offset)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
