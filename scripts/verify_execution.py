"""Verify the execution rails and the submit path offline. Run from the
project root:

    .venv/Scripts/python.exe scripts/verify_execution.py

Spec 009: the rails must refuse for the right reasons in the right order, and
the live branch must write the ledger before it touches the wire. No market,
no Gateway, no Telegram -- placeOrder lands on a stub that returns real
ib_async Trade objects, so the submit path runs for real up to the socket.

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
  l. A fully-unlocked dispatch submits (gates 5 and 6): one "submitted" row
     per tradeable leg, each written BEFORE its placeOrder call (asserted on
     the code path -- the stub reads the ledger at call time), orderRef
     carries the proposal id on the row and on the wire order, tif DAY,
     outsideRth False, sides mapped to BUY/SELL, and a SUBMITTED alert.
  m. placeOrder appears in execution.py and NOWHERE else in src/ -- the
     submit path has exactly one home.
  n. /status renders the execution line from runtime.json, and says
     "not reported" when a pre-rails monitor wrote the file.
  o. reconcile_on_connect flags and alerts on a confirmed orphan order, and
     treats a failed open-order or fills read as UNKNOWN -- returns False,
     logs, writes NO flag (spec 009 section 9: fail closed for the cycle,
     no sticky state).
  p. ValidationError and Inactive are terminal refusals, and nothing in src/
     waits on trade.isDone() or trade.filledEvent (gate 2).
  q. classify() names all six section-4 cases from fixtures, matches by
     orderRef prefix with a permId fallback, and dedups recorded execs
     (gate 3).
  r. A filled-not-in-ledger execution blocks submission (flag file -> gate
     blocked), alerts CRITICAL, is recorded as foreign_fill, and does NOT
     re-flag on the next reconcile (gate 4).
  s. The unhedged timer fires exactly at the bound, once, and only when a
     multi-leg ticket is one-legged (gate 7).
  t. Partial-fill accounting derives from folded fill rows -- duplicate
     exec_ids count once, totals reconcile to the cent (gate 8).
  u. predispatch_orders refuses the cycle on a failed query with NO flag,
     waits on our own working order, flags a foreign one, and passes a
     clean book (gate 9).
  v. ValidationError on a submitted trade is terminal ON THE SUBMIT PATH
     (gate 2): one broker_refused row carrying the broker's error text, one
     CRITICAL alert, exactly once even when the status fires twice; a permId
     arriving only on the callback is recorded and logged as late.
  w. The live branch's own guards refuse loudly: no proposal_id in extra,
     and a missing or disconnected ib handle, each write a "refused" row.
"""

import datetime
import json
import logging
import os
import sys
import tempfile
from zoneinfo import ZoneInfo

from eventkit import Event
from ib_async import Trade
from ib_async.order import TradeLogEntry

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bot
import config
import decision
import execution
import execution_state
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


class _StubIB:
    """Offline stand-in for the broker handle. placeOrder returns a REAL
    ib_async Trade -- so the submit path's event subscriptions run against
    the genuine objects -- and records whether the "submitted" ledger row
    already existed at call time. That is the write-before-wire assertion
    made on the code path itself, not inferred from end state (gate 5)."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.trades = []
        self.row_preceded = []
        self.errorEvent = Event("errorEvent")

    def isConnected(self):
        return True

    def placeOrder(self, contract, order):
        rows = read_orders(self.tmp)
        self.row_preceded.append(any(
            r.get("status") == "submitted"
            and r.get("order_ref") == order.orderRef for r in rows))
        order.orderId = len(self.trades) + 1
        trade = Trade(contract=contract, order=order)
        self.trades.append(trade)
        return trade


class _DeadIB:
    def isConnected(self):
        return False


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
    """Gates 5 and 6, asserted on the code path: the stub records at
    placeOrder time whether that leg's submitted row was already in the
    ledger, so a reordering of write and wire fails here even if the end
    state looks identical."""
    sent = []
    with tempfile.TemporaryDirectory() as tmp, \
            _rails_env(tmp, DRY_RUN="0", EXECUTION_ENABLED="1"):
        stub = _StubIB(tmp)
        status = execution.dispatch(
            drift_proposal(), TARGET,
            send=lambda *a: sent.append(a),
            extra={"proposal_id": "p-live", "source": "check_l"},
            ib=stub)
        rows = read_orders(tmp)
    tradeable = [leg for leg in drift_proposal()["legs"] if leg["tradeable"]]
    submitted = [r for r in rows if r.get("status") == "submitted"]
    refs_ok = all(r["order_ref"] == f"shortlev:p-live:{r['ticker']}"
                  and r["proposal_id"] == "p-live" for r in submitted)
    wire_ok = all(
        t.order.orderRef == execution.order_ref("p-live", t.contract.symbol)
        and t.order.tif == "DAY" and t.order.outsideRth is False
        and t.order.action in ("BUY", "SELL")
        for t in stub.trades)
    alerts = [a for a in sent if "SUBMITTED" in a[2]]
    ok = (
        status == "submitted"
        and len(submitted) == len(tradeable) == len(stub.trades) >= 1
        and stub.row_preceded and all(stub.row_preceded)
        and refs_ok and wire_ok
        and len(alerts) == 1 and alerts[0][0] == "WARNING"
    )
    return check("l. a fully-unlocked dispatch submits: ledger row precedes "
                 "each placeOrder, orderRef carries the proposal id",
                 ok, f"status={status}, submitted rows={len(submitted)} for "
                     f"{len(tradeable)} tradeable leg(s), row preceded wire="
                     f"{stub.row_preceded}, refs ok={refs_ok}, wire ok="
                     f"{wire_ok}, SUBMITTED alerts={len(alerts)}")


def check_m():
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    hits = []
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(src_dir, name), encoding="utf-8") as f:
            if "placeOrder" in f.read():
                hits.append(name)
    ok = hits == ["execution.py"]
    return check("m. placeOrder lives in execution.py and nowhere else "
                 "in src/",
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
    real_open, real_fills = monitor.broker.open_orders, monitor.broker.fills
    capture = _Capture()
    monitor.log.addHandler(capture)
    sent = []
    try:
        with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp) as env:
            state = execution_state.fresh()
            monitor.broker.open_orders = lambda ib: found
            monitor.broker.fills = lambda ib: []
            flagged = monitor.reconcile_on_connect(
                None, lambda *a: sent.append(a), state)
            flag_written = os.path.exists(env.values["ORPHAN_FLAG_PATH"])

            os.remove(env.values["ORPHAN_FLAG_PATH"])
            monitor.broker.open_orders = lambda ib: None
            unknown = monitor.reconcile_on_connect(
                None, lambda *a: sent.append(a), state)
            flag_after_unknown = os.path.exists(env.values["ORPHAN_FLAG_PATH"])

            monitor.broker.open_orders = lambda ib: []
            monitor.broker.fills = lambda ib: None
            fills_unknown = monitor.reconcile_on_connect(
                None, lambda *a: sent.append(a), state)
            flag_after_fills = os.path.exists(env.values["ORPHAN_FLAG_PATH"])
    finally:
        monitor.broker.open_orders = real_open
        monitor.broker.fills = real_fills
        monitor.log.removeHandler(capture)
    criticals = [r for r in capture.records if r.levelno == logging.CRITICAL]
    errors = [r for r in capture.records if r.levelno == logging.ERROR]
    ok = (
        flagged is True and flag_written
        and len(sent) == 1 and sent[0][0] == "CRITICAL"
        and sent[0][1] == "orphan orders"
        and len(criticals) >= 1
        and unknown is False and not flag_after_unknown
        and fills_unknown is False and not flag_after_fills
        and len(errors) == 2 and len(sent) == 1
    )
    return check("o. reconcile flags a confirmed orphan; a failed open-order "
                 "or fills read is UNKNOWN -- False, logged, not flagged",
                 ok, f"orphan run={flagged} flag={flag_written} alerts="
                     f"{len(sent)}; failed open read -> {unknown}, flag="
                     f"{flag_after_unknown}; failed fills read -> "
                     f"{fills_unknown}, flag={flag_after_fills}, "
                     f"ERROR lines={len(errors)}")


def check_p():
    """Gate 2. The classifier says terminal for the states an executor would
    otherwise wait on forever, and no src module waits on trade.isDone() or
    trade.filledEvent -- the dotted forms, so prose ABOUT the trap does not
    trip the check that enforces it."""
    verdicts = [execution.is_terminal_refusal(s)
                for s in ("ValidationError", "Inactive", "Submitted",
                          "PendingSubmit", "PreSubmitted", "Filled")]
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    waiters = []
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(src_dir, name), encoding="utf-8") as f:
            text = f.read()
        if ".isDone(" in text or ".filledEvent" in text:
            waiters.append(name)
    ok = verdicts == [True, True, False, False, False, False] and not waiters
    return check("p. ValidationError/Inactive are terminal refusals; nothing "
                 "waits on isDone or filledEvent",
                 ok, f"verdicts={verdicts}, waiting modules: "
                     f"{waiters or 'none'}")


def _reconcile_fixture():
    """Six-case fixture: one ledger, one order book, one fills replay."""
    ledger = [
        {"status": "submitted", "order_ref": "shortlev:p1:TSLL",
         "proposal_id": "p1", "ticker": "TSLL", "side": "BUY TO COVER",
         "shares": 50, "limit": 9.40, "trigger": "foil decay band"},
        {"status": "submitted", "order_ref": "shortlev:p1:TSLA",
         "proposal_id": "p1", "ticker": "TSLA", "side": "BUY",
         "shares": 40, "limit": 340.00, "trigger": "foil decay band"},
        {"status": "submitted", "order_ref": "shortlev:p2:TSLL",
         "proposal_id": "p2", "ticker": "TSLL", "side": "SELL (short more)",
         "shares": 30, "limit": 9.80, "trigger": "foil decay band"},
        {"status": "submitted", "order_ref": "shortlev:p3:TSLL",
         "proposal_id": "p3", "ticker": "TSLL", "side": "BUY TO COVER",
         "shares": 100, "limit": 9.40, "trigger": "foil decay band"},
        {"status": "submitted", "order_ref": "shortlev:p4:TSLA",
         "proposal_id": "p4", "ticker": "TSLA", "side": "BUY",
         "shares": 10, "limit": 339.00, "trigger": "long-short band",
         "perm_id": 777},
        {"status": "fill", "order_ref": "shortlev:p3:TSLL",
         "exec_id": "e-known", "shares": 10, "price": 9.40},
    ]
    open_rows = [
        {"symbol": "TSLL", "order_ref": "shortlev:p1:TSLL", "perm_id": 11,
         "status": "Submitted"},
        {"symbol": "TSLL", "order_ref": "shortlev:p3:TSLL", "perm_id": 33,
         "status": "Submitted"},
        # orderRef missing (question 2's pessimistic outcome) -- must still
        # match leg p4 through the permId fallback.
        {"symbol": "TSLA", "order_ref": None, "perm_id": 777,
         "status": "Submitted"},
        {"symbol": "MSFT", "order_ref": None, "perm_id": 999,
         "status": "Submitted"},
    ]
    fill_rows = [
        {"exec_id": "e1", "order_ref": "shortlev:p1:TSLA", "shares": 25,
         "price": 340.00},
        {"exec_id": "e2", "order_ref": "shortlev:p1:TSLA", "shares": 15,
         "price": 340.10},
        {"exec_id": "e3", "order_ref": "shortlev:p3:TSLL", "shares": 20,
         "price": 9.40},
        # Replay duplicate of the exec the ledger already carries.
        {"exec_id": "e-known", "order_ref": "shortlev:p3:TSLL", "shares": 10,
         "price": 9.40},
        {"exec_id": "e4", "order_ref": None, "perm_id": 555, "shares": 5,
         "price": 100.00, "symbol": "MSFT", "side": "BOT"},
    ]
    return ledger, open_rows, fill_rows


def check_q():
    ledger, open_rows, fill_rows = _reconcile_fixture()
    findings = execution.classify(ledger, open_rows, fill_rows)
    by_ref = {f.get("order_ref"): f["case"] for f in findings
              if f.get("order_ref")}
    partial = next((f for f in findings if f["case"] == "partial"), {})
    orphan_orders = [f for f in findings if f["case"] == "orphan_working"]
    orphan_fills = [f for f in findings if f["case"] == "orphan_filled"]
    ok = (
        by_ref.get("shortlev:p1:TSLL") == "working"
        and by_ref.get("shortlev:p1:TSLA") == "filled_while_away"
        and by_ref.get("shortlev:p2:TSLL") == "expired"
        and by_ref.get("shortlev:p3:TSLL") == "partial"
        and by_ref.get("shortlev:p4:TSLA") == "working"  # permId fallback
        and partial.get("filled_shares") == 30  # 10 known + 20 new, dup dropped
        and partial.get("working") is True
        and len(orphan_orders) == 1
        and orphan_orders[0]["order"]["symbol"] == "MSFT"
        and len(orphan_fills) == 1
        and orphan_fills[0]["fill"]["exec_id"] == "e4"
        and len(findings) == 7
    )
    return check("q. classify names all six cases, permId fallback matches, "
                 "recorded execs dedup",
                 ok, f"cases={by_ref}, partial filled="
                     f"{partial.get('filled_shares')}, orphans="
                     f"{len(orphan_orders)} order(s) + "
                     f"{len(orphan_fills)} fill(s), findings={len(findings)}")


def check_r():
    foreign_fill = {"exec_id": "zz1", "symbol": "TSLL", "side": "SLD",
                    "shares": 5, "price": 9.50, "perm_id": 42,
                    "time": "2026-08-13 15:59:59-04:00"}
    real_open, real_fills = monitor.broker.open_orders, monitor.broker.fills
    sent = []
    try:
        with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp) as env:
            monitor.broker.open_orders = lambda ib: []
            monitor.broker.fills = lambda ib: [foreign_fill]
            state = execution_state.fresh()
            first = monitor.reconcile_on_connect(
                None, lambda *a: sent.append(a), state)
            flag_written = os.path.exists(env.values["ORPHAN_FLAG_PATH"])
            gate = execution.submission_gate()
            rows = read_orders(tmp)

            os.remove(env.values["ORPHAN_FLAG_PATH"])
            second = monitor.reconcile_on_connect(
                None, lambda *a: sent.append(a), state)
            reflagged = os.path.exists(env.values["ORPHAN_FLAG_PATH"])
    finally:
        monitor.broker.open_orders = real_open
        monitor.broker.fills = real_fills
    recorded = [r for r in rows if r.get("status") == "foreign_fill"
                and r.get("exec_id") == "zz1"]
    ok = (
        first is True and flag_written
        and gate.allow is False and gate.mode == "blocked"
        and "orphan" in gate.reason
        and len(recorded) == 1
        and len(sent) == 1 and sent[0][0] == "CRITICAL"
        and second is True and not reflagged
    )
    return check("r. a filled-not-in-ledger exec blocks the gate, alerts "
                 "CRITICAL, records foreign_fill, does not re-flag",
                 ok, f"flag={flag_written}, gate={gate.mode} "
                     f"({gate.reason!r}), foreign_fill rows={len(recorded)}, "
                     f"alerts={len(sent)}, re-flagged after clear={reflagged}")


def check_s():
    base = datetime.datetime(2026, 8, 13, 14, 0, tzinfo=ET)
    ticket = {
        "trigger": "foil decay band",
        "legs": {"shortlev:p1:TSLL": {"ticker": "TSLL", "shares": 50,
                                      "complete": True},
                 "shortlev:p1:TSLA": {"ticker": "TSLA", "shares": 40,
                                      "complete": False}},
        "first_complete_ts": base.isoformat(timespec="seconds"),
        "unhedged_alerted": False,
    }
    early = execution.check_unhedged(
        ticket, base + datetime.timedelta(minutes=9, seconds=59), 10)
    due = execution.check_unhedged(
        ticket, base + datetime.timedelta(minutes=10), 10)
    fired = dict(ticket, unhedged_alerted=True)
    silent_after = execution.check_unhedged(
        fired, base + datetime.timedelta(hours=2), 10)
    both = {**ticket, "legs": {
        ref: dict(leg, complete=True) for ref, leg in ticket["legs"].items()}}
    hedged = execution.check_unhedged(
        both, base + datetime.timedelta(hours=2), 10)
    none_yet = {**ticket, "legs": {
        ref: dict(leg, complete=False) for ref, leg in ticket["legs"].items()}}
    unstarted = execution.check_unhedged(
        none_yet, base + datetime.timedelta(hours=2), 10)
    single = {**ticket, "legs": {"shortlev:p1:TSLL":
                                 {"ticker": "TSLL", "shares": 50,
                                  "complete": True}}}
    one_leg = execution.check_unhedged(
        single, base + datetime.timedelta(hours=2), 10)
    ok = (early is False and due is True and silent_after is False
          and hedged is False and unstarted is False and one_leg is False)
    return check("s. unhedged timer: fires at the bound, once, only for a "
                 "one-legged multi-leg ticket",
                 ok, f"9m59s={early}, 10m={due}, after alert={silent_after}, "
                     f"both done={hedged}, none done={unstarted}, "
                     f"single leg={one_leg}")


def check_t():
    rows = [
        {"status": "submitted", "order_ref": "shortlev:p9:TSLA",
         "proposal_id": "p9", "ticker": "TSLA", "side": "BUY", "shares": 40,
         "limit": 340.50, "trigger": "long-short band"},
        {"status": "fill", "order_ref": "shortlev:p9:TSLA", "exec_id": "x1",
         "shares": 25, "price": 340.10},
        {"status": "fill", "order_ref": "shortlev:p9:TSLA", "exec_id": "x2",
         "shares": 15, "price": 340.25},
        # The same execution replayed -- documented duplication, counted once.
        {"status": "fill", "order_ref": "shortlev:p9:TSLA", "exec_id": "x1",
         "shares": 25, "price": 340.10},
    ]
    leg = execution.fold_ledger(rows)["shortlev:p9:TSLA"]
    expected = round(25 * 340.10 + 15 * 340.25, 2)
    ok = (leg["filled_shares"] == 40
          and leg["filled_notional"] == expected
          and execution.fill_totals(leg["fills"].values()) == (40, expected))
    return check("t. accounting folds fill rows: duplicate execs count once, "
                 "notional reconciles to the cent",
                 ok, f"filled={leg['filled_shares']} sh, notional="
                     f"{leg['filled_notional']} (expected {expected})")


def check_u():
    ours = [{"symbol": "TSLL", "order_ref": "shortlev:p1:TSLL",
             "perm_id": 11, "status": "Submitted"}]
    foreign = [{"symbol": "MSFT", "action": "BUY", "quantity": 10,
                "order_type": "LMT", "status": "Submitted", "perm_id": 99,
                "client_id": 0, "order_ref": None}]
    real = monitor.broker.open_orders
    sent = []
    results = {}
    try:
        with tempfile.TemporaryDirectory() as tmp, _rails_env(tmp) as env:
            flag = env.values["ORPHAN_FLAG_PATH"]
            monitor.broker.open_orders = lambda ib: None
            results["failed"] = (monitor.predispatch_orders(
                None, lambda *a: sent.append(a)), os.path.exists(flag))
            monitor.broker.open_orders = lambda ib: ours
            results["ours"] = (monitor.predispatch_orders(
                None, lambda *a: sent.append(a)), os.path.exists(flag))
            monitor.broker.open_orders = lambda ib: foreign
            results["foreign"] = (monitor.predispatch_orders(
                None, lambda *a: sent.append(a)), os.path.exists(flag))
            os.remove(flag)
            monitor.broker.open_orders = lambda ib: []
            results["clean"] = (monitor.predispatch_orders(
                None, lambda *a: sent.append(a)), os.path.exists(flag))
    finally:
        monitor.broker.open_orders = real
    (f_ok, f_reason), f_flag = results["failed"]
    (o_ok, o_reason), o_flag = results["ours"]
    (x_ok, _), x_flag = results["foreign"]
    (c_ok, _), c_flag = results["clean"]
    ok = (
        f_ok is False and "no flag" in f_reason and not f_flag
        and o_ok is False and "not re-issuing" in o_reason and not o_flag
        and x_ok is False and x_flag and len(sent) == 1
        and sent[0][0] == "CRITICAL"
        and c_ok is True and not c_flag
    )
    return check("u. predispatch: failed read refuses with no flag, ours "
                 "waits, foreign flags, clean passes",
                 ok, f"failed=({f_ok}, flag={f_flag}), ours=({o_ok}, flag="
                     f"{o_flag}), foreign=({x_ok}, flag={x_flag}, alerts="
                     f"{len(sent)}), clean=({c_ok}, flag={c_flag})")


def check_v():
    """Gate 2 on the submit path itself: a synthetic ValidationError (the
    Read-Only rejection ib_async classifies as a warning and parks in an
    ACTIVE state) must terminate with one broker_refused row and one
    CRITICAL alert -- even fired twice, because status callbacks are
    documented as duplicable -- and a permId arriving only on the callback
    must be recorded and logged as late."""
    sent = []
    capture = _Capture()
    execution.log.addHandler(capture)
    # The arrived-late line is INFO; the root logger's WARNING default would
    # drop it before the capture handler ever sees it.
    prior_level = execution.log.level
    execution.log.setLevel(logging.INFO)
    try:
        with tempfile.TemporaryDirectory() as tmp, \
                _rails_env(tmp, DRY_RUN="0", EXECUTION_ENABLED="1"):
            stub = _StubIB(tmp)
            status = execution.dispatch(
                drift_proposal(), TARGET,
                send=lambda *a: sent.append(a),
                extra={"proposal_id": "p-val"}, ib=stub)
            trade = stub.trades[0]
            # The broker's sequence for error 321: errorEvent with the text,
            # a log entry, the status parked in ValidationError, statusEvent.
            stub.errorEvent.emit(
                trade.order.orderId, 321,
                "Error validating request. The API interface is currently "
                "in Read-Only mode.", None)
            trade.log.append(TradeLogEntry(
                time=datetime.datetime.now(datetime.timezone.utc),
                status="ValidationError",
                message="Error 321: The API interface is currently in "
                        "Read-Only mode.", errorCode=321))
            trade.orderStatus.status = "ValidationError"
            trade.order.permId = 654321
            trade.statusEvent.emit(trade)
            trade.statusEvent.emit(trade)  # documented duplicate callback
            rows = read_orders(tmp)
    finally:
        execution.log.removeHandler(capture)
        execution.log.setLevel(prior_level)
    refused = [r for r in rows if r.get("status") == "broker_refused"]
    perm = [r for r in rows if r.get("status") == "perm_id"]
    late = [r for r in capture.records
            if "arrived after submission" in r.getMessage()]
    criticals = [r for r in capture.records if r.levelno == logging.CRITICAL]
    crit_alerts = [a for a in sent if a[0] == "CRITICAL"]
    ok = (
        status == "submitted"
        and len(refused) == 1 and "321" in refused[0]["reason"]
        and refused[0]["broker_status"] == "ValidationError"
        and len(criticals) == 1 and len(crit_alerts) == 1
        and len(perm) == 1 and perm[0]["perm_id"] == 654321
        and len(late) == 1
    )
    return check("v. ValidationError on the submit path is terminal: one "
                 "broker_refused row, one CRITICAL, late permId recorded",
                 ok, f"refused rows={len(refused)} (reason="
                     f"{refused[0].get('reason') if refused else 'none'!r}), "
                     f"CRITICAL log/alert={len(criticals)}/{len(crit_alerts)}, "
                     f"perm rows={len(perm)}, late lines={len(late)}")


def check_w():
    """The live branch's own guards: without a proposal_id the orderRef
    ownership tag cannot exist, and without a connected handle nothing can
    be sent -- both refuse loudly, neither writes a submitted row."""
    with tempfile.TemporaryDirectory() as tmp, \
            _rails_env(tmp, DRY_RUN="0", EXECUTION_ENABLED="1"):
        no_pid = execution.dispatch(drift_proposal(), TARGET,
                                    extra={"source": "check_w"},
                                    ib=_StubIB(tmp))
        no_ib = execution.dispatch(drift_proposal(), TARGET,
                                   extra={"proposal_id": "p-w1"}, ib=None)
        dead = execution.dispatch(drift_proposal(), TARGET,
                                  extra={"proposal_id": "p-w2"}, ib=_DeadIB())
        rows = read_orders(tmp)
    refused = [r for r in rows if r.get("status") == "refused"]
    ok = (
        no_pid == "refused" and no_ib == "refused" and dead == "refused"
        and len(refused) == 3
        and sum("proposal_id" in r["reason"] for r in refused) == 1
        and sum("broker handle" in r["reason"] for r in refused) == 2
        and not any(r.get("status") == "submitted" for r in rows)
    )
    return check("w. live-branch guards: no proposal_id and no connected "
                 "handle both refuse, nothing submitted",
                 ok, f"verdicts={no_pid}/{no_ib}/{dead}, refused rows="
                     f"{len(refused)}, reasons="
                     f"{[r['reason'][:40] for r in refused]}")


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(),
        check_f(), check_g(), check_h(), check_i(), check_j(),
        check_k(), check_l(), check_m(), check_n(), check_o(),
        check_p(), check_q(), check_r(), check_s(), check_t(),
        check_u(), check_v(), check_w(),
    ]
    print("=" * 60)
    if all(results):
        print("All execution offline checks PASSED.")
        print("The submit path lives in execution._submit, reached only "
              "through dispatch with every rail clear; a clean environment "
              "still dry-runs and submits nothing.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
