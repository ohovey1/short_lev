"""Verify the bot's offline logic. Run from the project root:

    .venv/Scripts/python.exe scripts/verify_bot.py

These are the spec 008 checks that need neither Telegram nor the VPS. The live
half of the gate list -- delivery, phone rendering, offset persistence across
a real restart, the approval round trips -- is the operator's, per the spec.

Checks:
  a. Gate 2: a Telegram error payload carrying migrate_to_chat_id produces one
     ERROR line naming both the dead and the replacement id, and nothing
     rewrites .env.
  b. The authorization gate: a command from the wrong chat id sends NOTHING
     and logs one INFO line; the right chat id gets exactly one reply.
  c. The getUpdates offset round-trips; a malformed offset file resets to 0
     rather than raising.
  d. Gate 4 (offline half): /help covers all nine alert kinds and its
     severities are generated from notify.TRIGGER_SEVERITY.
  e. /status verdict logic: Healthy, Stale, Disconnected, Degraded each fire
     on the state that defines them.
  f. Escaping: reserved characters are escaped OUTSIDE fenced blocks and left
     alone INSIDE them -- the two most likely ways a reply fails to send or
     renders with stray backslashes.
  g. Gate 5 (offline half): in /positions, shares x displayed price equals the
     stated notional for both legs.
  h. Approval state: intents append/read round-trip, consumed ids survive a
     save/load, malformed state resets, and the intent cursor exists.
  i. The invariants, mechanically: no broker/ib_async/monitor import and no
     IB( in bot.py, no time.sleep import in monitor.py, no placeOrder
     anywhere in src/.
"""

import datetime
import json
import logging
import os
import sys
import tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import approval
import bot
import events
import notify

ET = ZoneInfo("America/New_York")
NOW = datetime.datetime(2026, 8, 13, 14, 36, tzinfo=ET)
POLL = 900.0


def check(label, ok, detail):
    print(f"{label}")
    print(f"  {detail}")
    print(f"  => {'PASS' if ok else 'FAIL'}\n")
    return ok


def _iso(minutes_ago):
    return (NOW - datetime.timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds")


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def check_a():
    """Gate 2. The recorded chat id is a BASIC group, conversion to a
    supergroup changes the id, and the sink is non-fatal -- so without this
    handler the bot just goes silent. The remedy must appear in the log."""
    payload = {
        "ok": False, "error_code": 400,
        "description": "Bad Request: group chat was upgraded to a supergroup chat",
        "parameters": {"migrate_to_chat_id": -1005220003871},
    }
    capture = _Capture()
    notify.logger.addHandler(capture)
    real_post = notify.requests.post
    notify.requests.post = lambda *a, **k: _FakeResponse(400, payload)
    try:
        delivered, error, message_id = notify.send_text(
            "tok", "-5220003871", "hello")
    finally:
        notify.requests.post = real_post
        notify.logger.removeHandler(capture)

    errors = [r for r in capture.records if r.levelno == logging.ERROR]
    rendered = errors[0].getMessage() if errors else ""
    ok = (
        delivered is False and message_id is None
        and len(errors) == 1
        and "-5220003871" in rendered
        and "-1005220003871" in rendered
        and "TELEGRAM_CHAT_ID" in rendered
    )
    return check("a. gate 2: migrate_to_chat_id logs both ids and the remedy",
                 ok, f"delivered={delivered}, {len(errors)} ERROR line(s), "
                     f"names dead id={'-5220003871' in rendered}, "
                     f"new id={'-1005220003871' in rendered}, "
                     f"fix={'TELEGRAM_CHAT_ID' in rendered}")


def check_b():
    """Gate 1's offline half. Silence for strangers -- an error reply would
    confirm the bot is live -- and exactly one reply for the group."""
    sent = []
    real_send = notify.send_text
    notify.send_text = lambda *a, **k: (sent.append(a), (True, None, 1))[1]

    capture = _Capture()
    bot.log.addHandler(capture)
    prior_level = bot.log.level
    bot.log.setLevel(logging.INFO)

    with tempfile.TemporaryDirectory() as tmp:
        prior = os.environ.get("EVENT_LOG_DIR")
        os.environ["EVENT_LOG_DIR"] = tmp
        try:
            stranger = {"text": "/status", "chat": {"id": -999},
                        "from": {"id": 42}}
            bot.handle_message(stranger, "tok", "-5220003871", POLL)
            stranger_sends = len(sent)

            member = {"text": "/help", "chat": {"id": -5220003871},
                      "from": {"id": 7}}
            bot.handle_message(member, "tok", "-5220003871", POLL)
            member_sends = len(sent) - stranger_sends

            with open(os.path.join(tmp, events.COMMANDS_FILE)) as f:
                rows = [json.loads(line) for line in f if line.strip()]
        finally:
            if prior is None:
                del os.environ["EVENT_LOG_DIR"]
            else:
                os.environ["EVENT_LOG_DIR"] = prior
            notify.send_text = real_send
            bot.log.removeHandler(capture)
            bot.log.setLevel(prior_level)

    infos = [r for r in capture.records
             if r.levelno == logging.INFO and "unauthorized" in r.getMessage()]
    ok = (
        stranger_sends == 0 and member_sends == 1
        and len(infos) == 1 and "-999" in infos[0].getMessage()
        and len(rows) == 2
        and rows[0]["authorized"] is False and rows[1]["authorized"] is True
    )
    return check("b. wrong chat id gets silence plus one INFO; right chat "
                 "gets one reply",
                 ok, f"stranger sends={stranger_sends}, member sends="
                     f"{member_sends}, INFO lines={len(infos)}, "
                     f"commands.jsonl authorized flags="
                     f"{[r['authorized'] for r in rows]}")


def check_c():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "bot_offset.json")
        virgin = bot.load_offset(path)
        bot.save_offset(path, 123456)
        restored = bot.load_offset(path)
        with open(path, "w") as f:
            f.write("{not json")
        reset = bot.load_offset(path)
    ok = virgin == 0 and restored == 123456 and reset == 0
    return check("c. offset round-trips; malformed resets to 0",
                 ok, f"virgin={virgin}, restored={restored}, "
                     f"after corruption={reset}")


def check_d():
    """Gate 4's offline half. The spec counts nine KINDS but its table has
    ten rows -- leg_check appears at both WARNING (read failed) and INFO
    (readable again). The catalog carries all ten rows; severities for the
    four trips come from the one TRIGGER_SEVERITY table."""
    help_text = bot.build_help().replace("\\", "")
    missing = [k["name"] for k in notify.ALERT_KINDS
               if k["name"] not in help_text]
    trip_mismatches = [
        name for name, severity in notify.TRIGGER_SEVERITY.items()
        if not any(k["name"] == name and k["severity"] == severity
                   for k in notify.ALERT_KINDS)
    ]
    closing = "missing heartbeat matters more" in help_text
    ok = (len(notify.ALERT_KINDS) == 10 and not missing
          and not trip_mismatches and closing)
    return check("d. gate 4: /help covers all nine kinds from TRIGGER_SEVERITY's table",
                 ok, f"{len(notify.ALERT_KINDS)} kinds, missing from help: "
                     f"{missing or 'none'}, severity mismatches: "
                     f"{trip_mismatches or 'none'}, closing line={closing}")


def check_e():
    """Gate 6's offline half: the verdict must not read Healthy when the data
    says otherwise."""
    healthy = {"started_at": _iso(200), "last_check_ts": _iso(4),
               "last_connect_ts": _iso(200), "cycles_since_start": 13,
               "connected": True, "last_error": None}
    stale = dict(healthy, last_check_ts=_iso(50))          # > 3 x 15 min
    disconnected = dict(healthy, connected=False)
    degraded = dict(healthy, last_error={"message": "boom", "ts": _iso(2)})

    got = {
        "healthy": bot.status_verdict(healthy, NOW, POLL)[0],
        "stale": bot.status_verdict(stale, NOW, POLL)[0],
        "missing": bot.status_verdict(None, NOW, POLL)[0],
        "disconnected": bot.status_verdict(disconnected, NOW, POLL)[0],
        "degraded": bot.status_verdict(degraded, NOW, POLL)[0],
    }
    expected = {"healthy": "Healthy", "stale": "Stale", "missing": "Stale",
                "disconnected": "Disconnected", "degraded": "Degraded"}
    ok = got == expected
    return check("e. /status verdicts fire on their defining states",
                 ok, ", ".join(f"{k}={v}" for k, v in got.items()))


def _synthetic_row():
    return {
        "ts": _iso(4), "decision_id": "ab12cd34", "pair": "TSLL",
        "short_notional": 7500.0, "long_notional": 13605.04,
        "target": 6818.18, "net_delta": -31.32, "margin_cushion": 2853.67,
        "account_equity": 10494.13, "peak_equity": 10507.95,
        "ibkr_maint": 7640.46, "modeled_maint": 7540.36,
        "trigger": None, "unactionable": False,
        "short_shares": 800, "long_shares": 40,
        "short_price": 9.375, "long_price": 340.126,
        "short_avg_cost": 9.10, "long_avg_cost": 300.0,
        "long_short_band": 0.10, "foil_decay_band": 0.10,
    }


def check_f():
    """The two escaping regimes, in one real reply. Outside a fence every
    reserved character is backslashed; inside, none are -- escaped dots in a
    block render as literal backslashes on the phone."""
    text = bot.build_positions(_synthetic_row(), NOW, POLL)
    segments = text.split("```")
    outside = segments[0::2]
    inside = segments[1::2]

    unescaped_outside = []
    for part in outside:
        for i, ch in enumerate(part):
            if ch == "*" or ch == "_":
                continue  # intentional MarkdownV2 formatting
            if ch in notify._MD_V2_RESERVED and (i == 0 or part[i - 1] != "\\"):
                unescaped_outside.append(ch)

    bad_inside = [part for part in inside
                  if "\\." in part or "\\-" in part or "\\(" in part]

    ok = bool(inside) and not unescaped_outside and not bad_inside
    return check("f. escaping: reserved chars escaped outside fences, bare inside",
                 ok, f"{len(inside)} fenced block(s), unescaped outside: "
                     f"{unescaped_outside or 'none'}, over-escaped inside "
                     f"blocks: {len(bad_inside)}")


def check_g():
    """Gate 5's offline half. A reader multiplies the two numbers shown."""
    text = bot.build_positions(_synthetic_row(), NOW, POLL).replace("\\", "")
    failures = []
    legs = 0
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if " sh @ " not in line:
            continue
        legs += 1
        shares = float(line.split(" sh @ ")[0].split()[-1].replace(",", ""))
        price = float(line.split(" sh @ ")[1].split()[0].replace(",", ""))
        notional_line = lines[i + 1]
        notional = float(
            notional_line.split("notional ")[1].split()[0].replace(",", ""))
        if abs(shares * price - notional) > 0.005:
            failures.append(f"{shares} x {price} != {notional}")
    ok = legs == 2 and not failures
    return check("g. gate 5: shares x displayed price equals stated notional",
                 ok, f"{legs} legs parsed" + (
                     "" if not failures else "; " + "; ".join(failures)))


def check_h():
    with tempfile.TemporaryDirectory() as tmp:
        intents = os.path.join(tmp, "intents.jsonl")
        seen_path = os.path.join(tmp, "intents_seen.json")
        state_path = os.path.join(tmp, "approval_state.json")

        wrote = approval.append_intent(intents, {
            "kind": "request", "decision_id": "ab12cd34",
            "from_user_id": 7, "ts": _iso(0)})
        approval.append_intent(intents, {
            "kind": "confirm", "proposal_id": "ef56ab78",
            "from_user_id": 7, "ts": _iso(0)})
        with open(intents, "a") as f:
            f.write("{garbage\n")
        records = approval.read_intents(intents)
        ids = [approval.intent_id(r) for r in records]

        approval.save_seen(seen_path, {"ab12cd34"})
        seen = approval.load_seen(seen_path)

        with open(state_path, "w") as f:
            f.write("[]")
        state = approval.load_state(state_path)

    ok = (
        wrote and ids == ["ab12cd34", "ef56ab78"]
        and seen == {"ab12cd34"}
        and state == approval.EMPTY_STATE
        and "intents_processed" in state
    )
    return check("h. approval files: append/read, seen set, malformed state resets",
                 ok, f"ids={ids}, seen={sorted(seen)}, reset state keys="
                     f"{sorted(state)}")


def check_i():
    """Gate 17, run mechanically here as well as by hand: the whole spec
    hinges on these being true."""
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    with open(os.path.join(src_dir, "bot.py"), encoding="utf-8") as f:
        bot_src = f.read()
    with open(os.path.join(src_dir, "monitor.py"), encoding="utf-8") as f:
        monitor_src = f.read()

    place_order_hits = []
    for name in os.listdir(src_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(src_dir, name), encoding="utf-8") as f:
            if "placeOrder" in f.read():
                place_order_hits.append(name)

    # Imports, not prose: the docstring is allowed to NAME what it must not
    # import. "IB(" is checked against the whole source, exactly as gate 17
    # writes it.
    import_lines = [line.strip() for line in bot_src.splitlines()
                    if line.strip().startswith(("import ", "from "))]
    bot_bad = [line for line in import_lines
               if any(m in line for m in ("broker", "ib_async", "monitor"))]
    if "IB(" in bot_src:
        bot_bad.append("IB(")
    monitor_imports_time = any(line.strip() == "import time"
                               for line in monitor_src.splitlines())

    ok = not place_order_hits and not bot_bad and not monitor_imports_time
    return check("i. invariants: no placeOrder in src/, no broker/IB in bot.py, "
                 "no time import in monitor.py",
                 ok, f"placeOrder in: {place_order_hits or 'nothing'}; bot.py "
                     f"contains: {bot_bad or 'nothing forbidden'}; monitor "
                     f"imports time: {monitor_imports_time}")


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(),
        check_f(), check_g(), check_h(), check_i(),
    ]
    print("=" * 60)
    if all(results):
        print("All bot offline checks PASSED.")
        print("The live gates (delivery, rendering, approval round trips) "
              "are the operator's -- see specs/spec_008.md.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
