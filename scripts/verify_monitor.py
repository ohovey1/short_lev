"""Verify the monitor's offline logic. Run from the project root:

    .venv/Scripts/python.exe scripts/verify_monitor.py

These are the checks that do NOT need IB Gateway. The real gate for spec 004 is
manual, against the paper account -- see specs/spec_004.md. This script exists so
that gate starts from a known-good base rather than debugging sign handling live.

Checks:
  a. Target derivation matches the strategy spec's worked example.
  b. A long leveraged leg is REJECTED, not abs()'d into a plausible wrong answer.
  c. A flat (zero) leg is rejected too.
  d. A short underlying leg is rejected.
  e. A correctly-signed pair reads back with a POSITIVE short_notional.
  f. A missing leg is rejected.
  g. Account values are filtered by currency, not taken first-match.
  h. peak_equity round-trips; a malformed state file refuses to load.
  i. Band params come from config and match band.py's signature defaults.
  j. The monitor never persists target -- only peak_equity is written.

Spec 006 adds:
  k. Trade line arithmetic is self-consistent: shares x price equals the stated
     amount, and current + amount equals the stated landing.
  l. MarkdownV2 escaping covers every reserved character in a real trade line.
  m. An unconfigured Telegram is a silent no-op that writes no alerts row.
  n. Dedup: transition sends, repeats stay quiet until the window, a trigger
     change sends immediately, resolution sends exactly once.
  n2. A suppressed (unactionable) trip does not steal the dedup slot.
  o. A JSONL write to an unwritable path logs and continues rather than raising.
  p. Malformed alert state resets rather than refusing to start.
"""

import datetime
import inspect
import json
import os
import sys
import tempfile

# Make the src/ modules importable when run from the project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

import alert_state
import band
import broker
import config
import decision
import events
import monitor
import monitor_state
import notify

# broker/monitor call this at import too; calling it here makes the ACCOUNT
# lookup below independent of import order.
load_dotenv()


def check(label, ok, detail):
    print(f"{label}")
    print(f"  {detail}")
    print(f"  => {'PASS' if ok else 'FAIL'}\n")
    return ok


# broker filters portfolio items and account values by IB_ACCOUNT when it is
# set. The fakes must carry whatever this environment configures, or every leg
# reads as MISSING and the rejection checks below pass for the wrong reason.
ACCOUNT = os.environ.get("IB_ACCOUNT") or "DU1"


class FakeContract:
    def __init__(self, symbol):
        self.symbol = symbol


class FakeItem:
    def __init__(self, symbol, position, market_value, market_price=0.0, account=None):
        self.contract = FakeContract(symbol)
        self.position = position
        self.marketValue = market_value
        self.marketPrice = market_price
        self.account = account or ACCOUNT


class FakeValue:
    def __init__(self, tag, value, currency="USD", account=None):
        self.tag = tag
        self.value = value
        self.currency = currency
        self.account = account or ACCOUNT


class FakeIB:
    """Minimal stand-in for the parts of IB that broker.read_position touches."""

    def __init__(self, items, values):
        self._items = items
        self._values = values

    def portfolio(self):
        return self._items

    def accountValues(self):
        return self._values


def _values(nlv="100000", maint="7500"):
    return [
        FakeValue("NetLiquidation", nlv),
        FakeValue("MaintMarginReq", maint),
    ]


def check_a():
    """Worked example, STRATEGY_SPEC section 4: TSLA/TSLL, base_capital 10,000,
    capital_utilization 0.75, margin_multiplier DERIVED as
    long_rate 0.25 x leverage 2 + short_rate 0.60 = 1.10 (spec 005; it was 1.60
    on the 50% initial single-stock long rate, where 25% maintenance applies).

        target = (10,000 * 0.75) / 1.10 = 7500 / 1.1 = 75000/11 = 6,818.1818...

    75000/11 has no exact binary float representation, so the expectation is
    the rational rather than a rounded decimal. The monitor and the backtest
    must agree here -- same formula, same inputs, one definition.
    """
    params = monitor.band_params()
    pair = config.PAIRS["TSLL"]
    target = monitor.derive_target(10_000.0, params, pair)
    expected = 75000 / 11
    ok = abs(target - expected) < 1e-9
    return check("a. target derivation matches the spec's worked example",
                 ok, f"derived {target:.4f}, spec says {expected:.4f} (75000/11)")


def check_b():
    """The failure this whole module is written against: TSLL held LONG.

    An unchecked abs() would produce short_notional=5000 -- a state that passes
    every downstream check and is completely wrong.
    """
    ib = FakeIB(
        [FakeItem("TSLL", +100, +5000.0), FakeItem("TSLA", +200, 10000.0)],
        _values(),
    )
    state = broker.read_position(ib, "TSLL", 4687.50, 100000.0)
    ok = state is None
    return check("b. long leveraged leg is rejected (not abs()'d)",
                 ok, f"TSLL position +100 shares -> read_position returned {state!r}")


def check_c():
    """A flat leg must fail too: >= 0, not > 0. Otherwise a closed short slides
    through as a $0 notional and we silently monitor half a position."""
    ib = FakeIB(
        [FakeItem("TSLL", 0, 0.0), FakeItem("TSLA", +200, 10000.0)],
        _values(),
    )
    state = broker.read_position(ib, "TSLL", 4687.50, 100000.0)
    ok = state is None
    return check("c. flat leveraged leg is rejected",
                 ok, f"TSLL position 0 shares -> read_position returned {state!r}")


def check_d():
    """Underlying held short -- the book would be double-short, not hedged."""
    ib = FakeIB(
        [FakeItem("TSLL", -100, -5000.0), FakeItem("TSLA", -200, -10000.0)],
        _values(),
    )
    state = broker.read_position(ib, "TSLL", 4687.50, 100000.0)
    ok = state is None
    return check("d. short underlying leg is rejected",
                 ok, f"TSLA position -200 shares -> read_position returned {state!r}")


def check_e():
    """Correctly signed: TSLL short, TSLA long. IBKR reports the short's
    marketValue as negative; evaluate() needs a positive magnitude."""
    ib = FakeIB(
        [FakeItem("TSLL", -500, -4687.50, 9.375),
         FakeItem("TSLA", +40, 9375.00, 234.375)],
        _values(),
    )
    state = broker.read_position(ib, "TSLL", 4687.50, 100000.0)
    ok = (
        state is not None
        and state.short_notional == 4687.50
        and state.long_notional == 9375.00
        and state.account_equity == 100000.0
        and state.margin_required == 7500.0
    )
    detail = "read_position returned None" if state is None else (
        f"short={state.short_notional:.2f} (positive), long={state.long_notional:.2f}, "
        f"equity={state.account_equity:.2f}, maint={state.margin_required:.2f}"
    )
    return check("e. correctly-signed pair reads back with positive short_notional",
                 ok, detail)


def check_f():
    """Only one leg held."""
    ib = FakeIB([FakeItem("TSLL", -500, -4687.50)], _values())
    state = broker.read_position(ib, "TSLL", 4687.50, 100000.0)
    ok = state is None
    return check("f. missing underlying leg is rejected",
                 ok, f"TSLA absent -> read_position returned {state!r}")


def check_g():
    """A CAD-denominated NetLiquidation must not be picked up by a USD account.
    First-match would grab 999999 here."""
    ib = FakeIB(
        [FakeItem("TSLL", -500, -4687.50), FakeItem("TSLA", +40, 9375.00)],
        [
            FakeValue("NetLiquidation", "999999", currency="CAD"),
            FakeValue("NetLiquidation", "100000", currency="USD"),
            FakeValue("MaintMarginReq", "7500", currency="USD"),
        ],
    )
    state = broker.read_position(ib, "TSLL", 4687.50, 100000.0)
    ok = state is not None and state.account_equity == 100000.0
    got = "None" if state is None else f"{state.account_equity:.2f}"
    return check("g. account values filter on base currency, not first match",
                 ok, f"CAD 999999 listed before USD 100000; read {got}")


def check_h():
    """peak_equity round-trips; a malformed file must NOT silently reinitialize
    -- that would reset the peak and disable the drawdown stop."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "monitor.json")
        monitor_state.save(path, 12345.67)
        restored = monitor_state.load(path)
        round_trips = restored == 12345.67

        with open(path, "w") as f:
            f.write("{not json")
        try:
            monitor_state.load(path)
            refused = False
        except SystemExit:
            refused = True

    ok = round_trips and refused
    return check("h. peak_equity round-trips; malformed state refuses to load",
                 ok, f"restored={restored}, malformed raised SystemExit={refused}")


def check_i():
    """The monitor must not tune separately from the backtest. Compare against
    band.run_band_backtest's actual signature defaults."""
    sig = inspect.signature(band.run_band_backtest).parameters
    params = monitor.band_params()
    pairs = [
        ("long_short_band", params.long_short_band),
        ("foil_decay_band", params.foil_decay_band),
        ("capital_utilization", params.capital_utilization),
        ("drawdown_stop", params.drawdown_stop),
        ("margin_derisk", params.margin_derisk),
    ]
    mismatches = [
        f"{name}: monitor={value} band.py={sig[name].default}"
        for name, value in pairs if sig[name].default != value
    ]
    ok = not mismatches
    return check("i. band params match band.py's signature defaults",
                 ok, "all five match" if ok else "; ".join(mismatches))


def check_j():
    """target must never be persisted. Save state, then confirm the file holds
    peak_equity and nothing resembling a target."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "monitor.json")
        monitor_state.save(path, 10000.0)
        with open(path) as f:
            data = json.load(f)

    keys = set(data)
    ok = keys == {"peak_equity", "updated_at"} and "target" not in keys
    return check("j. state file holds peak_equity only -- never target",
                 ok, f"keys written: {sorted(keys)}")


def _parse_trade_line(line):
    """Pull (shares, price, amount, current, landing) back out of a trade line.

    Parsing the rendered text rather than recomputing is the point: the gate is
    that the numbers a human reads are mutually consistent, so the check must
    read the same characters they do.
    """
    head, tail = line.split("\n")
    shares = int(head.split(" shares ")[0].split()[-1].replace(",", ""))
    price = float(head.split("@ ~")[1].split(" =")[0].replace(",", ""))
    amount = float(head.split("= $")[1].replace(",", ""))
    current = float(tail.split("->")[0].strip().replace(",", ""))
    landing = float(tail.split("->")[1].split("(")[0].strip().replace(",", ""))
    return shares, price, amount, current, landing


def check_k():
    """Spec 006 item 1. The old line printed three numbers describing three
    different trades: 13 x 329.38 is 4,281.94, not the 4,413.60 it reported.

    Checks both legs and both directions, reading the numbers back out of the
    rendered string.
    """
    cases = [
        ("TSLA", 9222.76, 13636.36, 329.38, False),   # long leg, buying
        ("TSLA", 13636.36, 9222.76, 329.38, False),   # long leg, selling
        ("TSLL", 4683.95, 6818.18, 8.15, True),       # short leg, shorting more
        ("TSLL", 6818.18, 4683.95, 8.15, True),       # short leg, covering
    ]
    failures = []
    for ticker, current, new, price, short_leg in cases:
        line = monitor.format_trade_line(ticker, current, new, price, short_leg)
        shares, got_price, amount, got_current, landing = _parse_trade_line(line)

        if abs(shares * got_price - amount) > 0.005:
            failures.append(f"{ticker}: {shares} x {got_price} != {amount}")
        direction = 1 if new > current else -1
        if abs((got_current + direction * amount) - landing) > 0.005:
            failures.append(
                f"{ticker}: {got_current} + {direction * amount} != {landing}")
        if abs(got_current - current) > 0.005:
            failures.append(f"{ticker}: current {got_current} != {current}")

    ok = not failures
    sample = monitor.format_trade_line("TSLA", 9222.76, 13636.36, 329.38, False)
    detail = (sample.replace("\n", " | ") if ok else "; ".join(failures))
    return check("k. trade line arithmetic is self-consistent", ok, detail)


def check_l():
    """Spec 006 gate 8. Telegram REJECTS a message containing an unescaped
    reserved character, so a trade line full of . - ( ) would simply never
    arrive. escape_md_v2 is copied verbatim from hype_arb; this proves it
    covers what these specific messages contain.

    Note $ is not in Telegram's reserved set and correctly passes through.
    """
    line = monitor.format_trade_line("TSLA", 9222.76, 13636.36, 329.38, False)
    escaped = notify.escape_md_v2(line)

    missing = [ch for ch in ".-()" if ch in line and f"\\{ch}" not in escaped]
    # Every reserved character present must be preceded by a backslash.
    unescaped = []
    for i, ch in enumerate(escaped):
        if ch in notify._MD_V2_RESERVED and (i == 0 or escaped[i - 1] != "\\"):
            unescaped.append(ch)

    round_trips = escaped.replace("\\", "") == line
    ok = not missing and not unescaped and round_trips and "$" in escaped
    detail = (
        f"escaped {sum(1 for c in line if c in notify._MD_V2_RESERVED)} reserved "
        f"chars, $ passes through unescaped (not reserved), round-trips={round_trips}"
        if ok else
        f"missing={missing} unescaped={unescaped} round_trips={round_trips}"
    )
    return check("l. MarkdownV2 escaping covers the trade lines", ok, detail)


def check_m():
    """Spec 006 gate 1. Unconfigured must be a CLEAN no-op: no send, no raise,
    and no alerts.jsonl row -- running without a bot is a supported mode, not a
    failed delivery to be logged every cycle.
    """
    with tempfile.TemporaryDirectory() as tmp:
        prior = os.environ.get("EVENT_LOG_DIR")
        os.environ["EVENT_LOG_DIR"] = tmp
        try:
            sent = notify.notify(None, None, "WARNING", "trip", "t", "b")
            sent_blank = notify.notify("", "", "WARNING", "trip", "t", "b")
            wrote = os.path.exists(os.path.join(tmp, events.ALERTS_FILE))
        finally:
            if prior is None:
                del os.environ["EVENT_LOG_DIR"]
            else:
                os.environ["EVENT_LOG_DIR"] = prior

    ok = sent is False and sent_blank is False and not wrote
    return check("m. unconfigured Telegram is a silent no-op",
                 ok, f"returned {sent}/{sent_blank}, wrote alerts.jsonl={wrote}")


def check_n():
    """Spec 006 gates 4 and 5. A standing trip must nag, not spam: one message
    per ALERT_REPEAT_MINUTES, not one per cycle. A CHANGE of trigger is new
    information and jumps the timer.
    """
    t0 = datetime.datetime(2026, 8, 10, 10, 0, 0)
    s = dict(alert_state.EMPTY)
    seen = []

    def step(trigger, minutes):
        nonlocal s
        now = t0 + datetime.timedelta(minutes=minutes)
        send, kind = alert_state.should_send(s, trigger, now, 60)
        if send:
            s = alert_state.record_sent(s, trigger, now)
        seen.append((send, kind))
        return send, kind

    step(None, 0)                       # quiet: nothing tripped
    step("long-short band", 0)          # transition in -> send
    step("long-short band", 1)          # still tripped -> quiet
    step("long-short band", 59)         # still inside window -> quiet
    step("long-short band", 60)         # window elapsed -> re-send
    step("margin de-risk", 61)          # trigger CHANGED -> immediate
    step("long-short band", 62)         # changed back -> immediate
    step(None, 63)                      # resolved -> send once
    step(None, 64)                      # still clear -> quiet

    expected = [
        (False, None), (True, "trip"), (False, None), (False, None),
        (True, "trip"), (True, "trip"), (True, "trip"),
        (True, "resolved"), (False, None),
    ]
    ok = seen == expected
    detail = (f"{sum(1 for s_, _ in seen if s_)} sends across 9 cycles, "
              "resolution exactly once" if ok else f"got {seen}")
    return check("n. dedup: transition, repeat window, change, resolution",
                 ok, detail)


def check_n2():
    """Spec 006 item 2 + item 6, together. The dedup slot holds ONE value, so a
    suppressed trip must not write its trigger into it.

    Caught in testing: alternating the sanity key and the band trigger made
    every cycle look like a transition, and the sanity warning re-sent four
    times in seven cycles instead of once per window.
    """
    t0 = datetime.datetime(2026, 8, 10, 10, 0, 0)
    s = dict(alert_state.EMPTY)
    sends = 0

    # Seven cycles, all with the target unachievable and a band tripped.
    for minute in range(7):
        now = t0 + datetime.timedelta(minutes=minute)
        send, _ = alert_state.should_send(
            s, monitor.SANITY_TRIGGER_KEY, now, 60)
        if send:
            s = alert_state.record_sent(s, monitor.SANITY_TRIGGER_KEY, now)
            sends += 1

    ok = sends == 1 and s["last_trigger"] == monitor.SANITY_TRIGGER_KEY
    return check("n2. a suppressed trip does not steal the dedup slot",
                 ok, f"{sends} sanity alert(s) across 7 suppressed cycles "
                     f"(expected 1), slot holds {s['last_trigger']!r}")


def check_o():
    """Spec 006 item 4. Telemetry is never worth a live position: a full disk or
    a bad path must log and continue, not take the monitor down mid-cycle.
    """
    prior = os.environ.get("EVENT_LOG_DIR")
    # A path under a FILE, so makedirs cannot succeed.
    with tempfile.NamedTemporaryFile(suffix=".notadir", delete=False) as f:
        blocker = f.name
    os.environ["EVENT_LOG_DIR"] = os.path.join(blocker, "events")
    try:
        events.log_check({"pair": "TSLL", "trigger": None})
        events.log_alert({"severity": "INFO", "delivered": False})
        raised = False
    except Exception:
        raised = True
    finally:
        if prior is None:
            del os.environ["EVENT_LOG_DIR"]
        else:
            os.environ["EVENT_LOG_DIR"] = prior
        os.unlink(blocker)

    ok = not raised
    return check("o. an unwritable event log never raises",
                 ok, f"log_check/log_alert on an invalid path raised={raised}")


def check_p():
    """Spec 006 item 6. Unlike monitor_state, malformed alert state RESETS.
    Refusing to start because a dedup ledger is corrupt would trade a live
    position's monitoring for at most one duplicate message.

    Also confirms a round-trip, and that peak_equity is not in this file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "alert_state.json")
        now = datetime.datetime(2026, 8, 10, 9, 46)
        saved = alert_state.record_heartbeat(
            alert_state.record_sent(dict(alert_state.EMPTY), "foil decay band", now),
            now)
        alert_state.save(path, saved)
        restored = alert_state.load(path)
        round_trips = (restored["last_trigger"] == "foil decay band"
                       and restored["last_heartbeat_date"] == "2026-08-10")
        with open(path) as f:
            keys = set(json.load(f))

        with open(path, "w") as f:
            f.write("{not json")
        reset = alert_state.load(path)
        recovered = reset == alert_state.EMPTY

    ok = round_trips and recovered and "peak_equity" not in keys
    return check("p. alert state round-trips; malformed resets, never exits",
                 ok, f"round_trips={round_trips}, malformed reset to empty="
                     f"{recovered}, keys={sorted(keys)}")


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(),
        check_f(), check_g(), check_h(), check_i(), check_j(),
        check_k(), check_l(), check_m(), check_n(), check_n2(),
        check_o(), check_p(),
    ]
    print("=" * 60)
    if all(results):
        print("All monitor offline checks PASSED.")
        print("The real gate is manual against paper -- see specs/spec_004.md.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
