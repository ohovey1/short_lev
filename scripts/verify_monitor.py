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
"""

import inspect
import json
import os
import sys
import tempfile

# Make the src/ modules importable when run from the project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

import band
import broker
import config
import decision
import monitor
import monitor_state

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
    margin_multiplier 1.60, capital_utilization 0.75.

        target = (10,000 * 0.75) / 1.60 = 4,687.50
    """
    params = monitor.band_params()
    pair = config.PAIRS["TSLL"]
    target = monitor.derive_target(10_000.0, params, pair)
    ok = abs(target - 4687.50) < 1e-9
    return check("a. target derivation matches the spec's worked example",
                 ok, f"derived {target:.2f}, spec says 4687.50")


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


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(),
        check_f(), check_g(), check_h(), check_i(), check_j(),
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
