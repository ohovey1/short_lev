"""Verify the execution rails offline. Run from the project root:

    .venv/Scripts/python.exe scripts/verify_execution.py

Spec 009 prep: everything here refuses, and these checks prove it refuses for
the right reasons in the right order. No market, no Gateway, no Telegram --
the rails are exactly the part of an executor that must be testable offline.

Checks:
  a. The clamp allows a normal drift-correction ticket.
  b. The clamp refuses an oversized leg, naming the leg and both numbers.
  c. The clamp is strictly-above: a leg exactly at the ceiling passes.
  d. The clamp refuses a missing or nonpositive target outright.
  e. The gate's default state -- nothing set, no files -- is dry_run, never
     allow. A box with no configuration at all cannot submit.
  f. DRY_RUN=0 alone is blocked: EXECUTION_ENABLED must be exactly "1".
  g. DRY_RUN=0 plus EXECUTION_ENABLED=1 with no files is live.
  h. The HALT sentinel blocks even a fully enabled box.
  i. The orphan flag outranks everything, round-trips through flag_orphans,
     and deleting the file clears it.
  j. Dry-run dispatch writes an orders.jsonl row with status "dry_run" and
     alerts as if it had submitted.
  k. A clamp refusal writes status "refused" with the reason in the row and
     one WARNING log line -- the reason is logged on every refusal.
  l. A fully-unlocked dispatch still refuses: the submit call is spec 009's,
     and until it lands the live branch logs CRITICAL and refuses.
  m. No placeOrder anywhere in src/, execution.py and broker.py included.
  n. /status renders the execution line from runtime.json, and says
     "not reported" when a pre-rails monitor wrote the file.
  o. orphan_scan flags and alerts on found orders, and treats a failed query
     as UNKNOWN -- logged, not flagged.
"""

import datetime
import json
import logging
import os
import sys
import tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bot
import config
import decision
import execution
import monitor
import orders

ET = ZoneInfo("America/New_York")
NOW = datetime.datetime(2026, 8, 13, 14, 36, tzinfo=ET)

PAIR = config.PAIRS["TSLL"]
PARAMS = decision.BandParams(
    long_short_band=0.10, foil_decay_band=0.10,
    capital_utilization=0.75, drawdown_stop=0.10, margin_derisk=True,
)
TARGET = 75000 / 11

# Every env var the rails read, controlled explicitly in each check: the
# imports above ran load_dotenv, so the developer's real .env must not be able
# to change a verdict here.
RAIL_ENV = ("DRY_RUN", "EXECUTION_ENABLED", "MAX_ORDER_MULTIPLE",
            "EXECUTION_HALT_PATH", "ORPHAN_FLAG_PATH", "EVENT_LOG_DIR")


def check(label, ok, detail):
    print(f"{label}")
    print(f"  {detail}")
    print(f"  => {'PASS' if ok else 'FAIL'}\n")
    return ok


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _rails_env:
    """Pin every rail-relevant env var inside a temp dir, restore on exit.
    Paths default into the temp dir so no check can see a real HALT file or
    write a real flag."""

    def __init__(self, tmp, **overrides):
        self.values = {
            "DRY_RUN": "", "EXECUTION_ENABLED": "", "MAX_ORDER_MULTIPLE": "",
            "EXECUTION_HALT_PATH": os.path.join(tmp, "HALT"),
            "ORPHAN_FLAG_PATH": os.path.join(tmp, "orphan_orders.json"),
            "EVENT_LOG_DIR": os.path.join(tmp, "events"),
        }
        self.values.update(overrides)

    def __enter__(self):
        self.prior = {name: os.environ.get(name) for name in RAIL_ENV}
        for name, value in self.values.items():
            os.environ[name] = value
        return self

    def __exit__(self, *exc):
        for name, value in self.prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def make_state(short, long_, equity=10000.0, peak=10000.0, maint=7000.0):
    return decision.PositionState(
        short_notional=short, long_notional=long_, leverage=PAIR["leverage"],
        target=TARGET, account_equity=equity, peak_equity=peak,
        margin_required=maint,
        margin_multiplier=config.margin_multiplier(PAIR),
    )


def drift_proposal():
    """A routine foil decay correction: one small tradeable leg."""
    state = make_state(short=7600.0, long_=13600.0)
    d = decision.evaluate(state, PARAMS)
    return orders.build_proposal(
        state, d, PARAMS, {"TSLL": 9.50, "TSLA": 340.0},
        {"TSLL": 800, "TSLA": 40}, PAIR)


def close_proposal():
    """A drawdown-stop close: both legs, target-sized and larger."""
    state = make_state(short=6800.0, long_=13600.0, equity=8900.0,
                       peak=10000.0)
    d = decision.evaluate(state, PARAMS)
    return orders.build_proposal(
        state, d, PARAMS, {"TSLL": 8.50, "TSLA": 340.0},
        {"TSLL": 800, "TSLA": 40}, PAIR)


def read_orders(tmp):
    path = os.path.join(tmp, "events", "orders.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def check_a():
    allow, reason = execution.size_clamp(drift_proposal(), TARGET)
    ok = allow is True and "within" in reason
    return check("a. clamp allows a routine drift-correction ticket",
                 ok, f"allow={allow}, reason={reason!r}")


def check_b():
    """A drawdown-stop close is the honest oversized example: the short leg
    alone is ~1x target against a 0.5x ceiling. This refusal is DELIBERATE
    and flagged for spec 009 -- see the MAX_ORDER_MULTIPLE comment."""
    proposal = close_proposal()
    allow, reason = execution.size_clamp(proposal, TARGET)
    ok = (allow is False and "TSLL" in reason
          and "0.5" in reason and f"{0.5 * TARGET:,.2f}" in reason)
    return check("b. clamp refuses an oversized leg, naming leg and numbers",
                 ok, f"allow={allow}, reason={reason!r}")


def check_c():
    exactly = {"legs": [{"ticker": "TSLL", "side": "BUY TO COVER",
                         "tradeable": True, "notional": 0.5 * TARGET}]}
    just_over = {"legs": [{"ticker": "TSLL", "side": "BUY TO COVER",
                           "tradeable": True,
                           "notional": 0.5 * TARGET + 0.01}]}
    at_allow, _ = execution.size_clamp(exactly, TARGET)
    over_allow, _ = execution.size_clamp(just_over, TARGET)
    ok = at_allow is True and over_allow is False
    return check("c. clamp refuses strictly ABOVE the ceiling; at it passes",
                 ok, f"at ceiling allow={at_allow}, one cent over "
                     f"allow={over_allow}")


def check_d():
    verdicts = [execution.size_clamp(drift_proposal(), t)[0]
                for t in (None, 0, -5.0)]
    ok = verdicts == [False, False, False]
    return check("d. clamp refuses a missing or nonpositive target",
                 ok, f"allow for target None/0/-5 = {verdicts}")


def check_e():
    with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp):
        gate = execution.submission_gate()
    ok = (gate.allow is False and gate.mode == "dry_run"
          and "DRY_RUN" in gate.reason)
    return check("e. default state (nothing set, no files) is dry_run, "
                 "never allow",
                 ok, f"allow={gate.allow}, mode={gate.mode}, "
                     f"reason={gate.reason!r}")


def check_f():
    with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp, DRY_RUN="0"):
        gate = execution.submission_gate()
    ok = (gate.allow is False and gate.mode == "blocked"
          and "EXECUTION_ENABLED" in gate.reason)
    return check("f. DRY_RUN=0 alone is blocked: EXECUTION_ENABLED must be '1'",
                 ok, f"allow={gate.allow}, mode={gate.mode}, "
                     f"reason={gate.reason!r}")


def check_g():
    with tempfile.TemporaryDirectory() as tmp, \
            _rails_env(tmp, DRY_RUN="0", EXECUTION_ENABLED="1"):
        gate = execution.submission_gate()
    ok = gate.allow is True and gate.mode == "live"
    return check("g. DRY_RUN=0 + EXECUTION_ENABLED=1 with no files is live",
                 ok, f"allow={gate.allow}, mode={gate.mode}, "
                     f"reason={gate.reason!r}")


def check_h():
    with tempfile.TemporaryDirectory() as tmp, \
            _rails_env(tmp, DRY_RUN="0", EXECUTION_ENABLED="1") as env:
        with open(env.values["EXECUTION_HALT_PATH"], "w") as f:
            f.write("stop\n")
        gate = execution.submission_gate()
    ok = (gate.allow is False and gate.mode == "blocked"
          and "HALT" in gate.reason)
    return check("h. the HALT sentinel blocks even a fully enabled box",
                 ok, f"allow={gate.allow}, mode={gate.mode}, "
                     f"reason={gate.reason!r}")


def check_i():
    with tempfile.TemporaryDirectory() as tmp, \
            _rails_env(tmp, DRY_RUN="0", EXECUTION_ENABLED="1") as env:
        with open(env.values["EXECUTION_HALT_PATH"], "w") as f:
            f.write("stop\n")
        flag = execution.flag_orphans(
            [{"symbol": "TSLL", "action": "BUY", "quantity": 10}])
        flagged = execution.submission_gate()
        with open(flag, encoding="utf-8") as f:
            stored = json.load(f)
        os.remove(flag)
        cleared = execution.submission_gate()
    ok = (
        flagged.mode == "blocked" and "orphan" in flagged.reason
        and stored["orders"][0]["symbol"] == "TSLL" and "ts" in stored
        and "HALT" in cleared.reason  # HALT still present: next rail in line
    )
    return check("i. orphan flag outranks everything, round-trips, clears "
                 "on delete",
                 ok, f"flagged reason={flagged.reason!r}; stored orders="
                     f"{len(stored['orders'])}; after delete falls through "
                     f"to {cleared.reason!r}")


def check_j():
    sent = []
    with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp):
        status = execution.dispatch(
            drift_proposal(), TARGET,
            send=lambda sev, kind, title, body: sent.append((sev, kind,
                                                             title, body)))
        rows = read_orders(tmp)
    ok = (
        status == "dry_run" and len(rows) == 1
        and rows[0]["status"] == "dry_run" and rows[0]["legs"]
        and len(sent) == 1 and "DRY RUN" in sent[0][2]
        and "not submitted" in sent[0][3]
    )
    return check("j. dry-run dispatch logs the full ticket and alerts as if "
                 "submitted",
                 ok, f"status={status}, rows={len(rows)}, alerts={len(sent)}, "
                     f"title={sent[0][2] if sent else 'none'!r}")


def check_k():
    sent = []
    capture = _Capture()
    execution.log.addHandler(capture)
    try:
        with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp):
            status = execution.dispatch(
                close_proposal(), TARGET,
                send=lambda *a: sent.append(a))
            rows = read_orders(tmp)
    finally:
        execution.log.removeHandler(capture)
    warnings = [r for r in capture.records
                if r.levelno == logging.WARNING and "refused" in r.getMessage()]
    ok = (
        status == "refused" and len(rows) == 1
        and rows[0]["status"] == "refused" and "clamp" in rows[0]["reason"]
        and len(warnings) == 1 and not sent
    )
    return check("k. a clamp refusal is logged with its reason and recorded, "
                 "no as-if-submitted alert",
                 ok, f"status={status}, row reason="
                     f"{rows[0].get('reason') if rows else 'none'!r}, "
                     f"WARNING lines={len(warnings)}, alerts={len(sent)}")


def check_l():
    capture = _Capture()
    execution.log.addHandler(capture)
    try:
        with tempfile.TemporaryDirectory() as tmp, \
                _rails_env(tmp, DRY_RUN="0", EXECUTION_ENABLED="1"):
            status = execution.dispatch(drift_proposal(), TARGET)
            rows = read_orders(tmp)
    finally:
        execution.log.removeHandler(capture)
    criticals = [r for r in capture.records if r.levelno == logging.CRITICAL]
    ok = (
        status == "refused" and len(rows) == 1
        and rows[0]["status"] == "refused"
        and "spec 009" in rows[0]["reason"]
        and len(criticals) == 1
    )
    return check("l. a fully-unlocked dispatch still refuses: the submit "
                 "call is spec 009's",
                 ok, f"status={status}, reason="
                     f"{rows[0].get('reason') if rows else 'none'!r}, "
                     f"CRITICAL lines={len(criticals)}")


def check_m():
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    hits = []
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(src_dir, name), encoding="utf-8") as f:
            if "placeOrder" in f.read():
                hits.append(name)
    ok = not hits
    return check("m. no placeOrder anywhere in src/",
                 ok, f"hits: {hits or 'none'}")


def check_n():
    runtime = {
        "started_at": (NOW - datetime.timedelta(hours=3)).isoformat(
            timespec="seconds"),
        "last_check_ts": (NOW - datetime.timedelta(minutes=4)).isoformat(
            timespec="seconds"),
        "last_connect_ts": None, "cycles_since_start": 12,
        "connected": True, "last_error": None,
        "execution": {"mode": "dry_run",
                      "reason": "DRY_RUN is not '0' (the default)"},
    }
    with_field = bot.build_status(runtime, NOW, 900.0).replace("\\", "")
    runtime.pop("execution")
    without = bot.build_status(runtime, NOW, 900.0).replace("\\", "")
    ok = ("execution    dry run" in with_field
          and "DRY_RUN" in with_field
          and "execution    not reported" in without)
    return check("n. /status renders the gate verdict; absent field reads "
                 "'not reported'",
                 ok, f"with field renders dry run="
                     f"{'execution    dry run' in with_field}, absent renders "
                     f"not reported={'execution    not reported' in without}")


def check_o():
    found = [{"symbol": "TSLL", "action": "BUY", "quantity": 10,
              "order_type": "LMT", "limit_price": 9.5, "status": "Submitted",
              "perm_id": 1, "client_id": 0, "order_ref": None}]
    real = monitor.broker.open_orders
    capture = _Capture()
    monitor.log.addHandler(capture)
    sent = []
    try:
        with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp) as env:
            monitor.broker.open_orders = lambda ib: found
            flagged = monitor.orphan_scan(None, lambda *a: sent.append(a))
            flag_written = os.path.exists(env.values["ORPHAN_FLAG_PATH"])

            monitor.broker.open_orders = lambda ib: None
            os.remove(env.values["ORPHAN_FLAG_PATH"])
            unknown = monitor.orphan_scan(None, lambda *a: sent.append(a))
            flag_after_unknown = os.path.exists(env.values["ORPHAN_FLAG_PATH"])
    finally:
        monitor.broker.open_orders = real
        monitor.log.removeHandler(capture)
    criticals = [r for r in capture.records if r.levelno == logging.CRITICAL]
    errors = [r for r in capture.records if r.levelno == logging.ERROR]
    ok = (
        flagged is True and flag_written
        and len(sent) == 1 and sent[0][0] == "CRITICAL"
        and sent[0][1] == "orphan orders"
        and len(criticals) >= 1
        and unknown is False and not flag_after_unknown
        and len(errors) == 1 and len(sent) == 1
    )
    return check("o. orphan_scan flags and alerts on found orders; a failed "
                 "query is UNKNOWN, logged, not flagged",
                 ok, f"flagged={flagged}, flag file={flag_written}, alerts="
                     f"{len(sent)}, CRITICAL={len(criticals)}, failed query "
                     f"-> flagged={flag_after_unknown}, ERROR={len(errors)}")


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(),
        check_f(), check_g(), check_h(), check_i(), check_j(),
        check_k(), check_l(), check_m(), check_n(), check_o(),
    ]
    print("=" * 60)
    if all(results):
        print("All execution-rail offline checks PASSED.")
        print("Nothing here submits. The live branch of dispatch() refuses "
              "until spec 009 deliberately adds the submit call.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
