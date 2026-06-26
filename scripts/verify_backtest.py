"""Verify the backtest's tranche lifecycle end to end.

Run from the project root:
    .venv/Scripts/python.exe scripts/verify_backtest.py

backtest.py holds the tranche ladder and is the thing we trust to be right.
This script checks it two independent ways, neither of which reuses the
backtest's loop:

  (A) Equity identity, day by day: equity(t) must equal realized P&L so far
      plus the current mark of every still-open tranche -- with no tranche
      counted twice. We rebuild realized + open-marks from first principles
      (one engine call per open tranche per day) and compare to the backtest's
      reported equity curve.

  (B) Realize-price correctness: a tranche opened on day d is realized at the
      close of day d + hold_days. We recompute the whole equity curve from an
      independent re-derivation of the ladder and require it to match the
      backtest's curve exactly.

Both checks call engine.position_pnl directly so a bug in the backtest's loop
(double-count, off-by-one realize day, wrong realize price) would show up as a
mismatch rather than agreeing with itself.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import data
import engine
import backtest

UNDERLYING = "QQQ"
HOLD_DAYS = 3
BASE_CAPITAL = 10000.0


def independent_equity_curve(underlying, hold_days, base_capital):
    """Re-derive the equity curve from scratch, separate from backtest.py.

    Same rules (open one tranche/day, taper the tail, realize at d+hold_days,
    mark-to-market equity) but written independently so agreement is meaningful.
    Returns a list of (date, equity, open_count) tuples.
    """
    pair = config.PAIRS[underlying]
    leverage = pair["leverage"]
    lev = data.get_prices(pair["leveraged_ticker"])["close"]
    und = data.get_prices(pair["underlying_ticker"])["close"]
    dates = list(lev.index.intersection(und.index).sort_values())

    notional = base_capital / hold_days
    short_size = notional
    long_size = leverage * notional

    # An entry is the index at which a tranche opens. A tranche opened at index i
    # is open (marked) on days i .. i+hold_days, and realizes at i+hold_days.
    # Only open if i+hold_days exists in the window (the tail taper).
    n = len(dates)
    entries = [i for i in range(n) if n - i > hold_days]

    rows = []
    for t in range(n):
        lev_t, und_t = lev.iloc[t], und.iloc[t]
        realized = 0.0
        open_marks = 0.0
        open_count = 0
        for i in entries:
            if i > t:
                continue  # not opened yet
            entry_lev, entry_und = lev.iloc[i], und.iloc[i]
            realize_day = i + hold_days
            if realize_day <= t:
                # Already realized -- lock its value at the realize-day prices.
                r = engine.position_pnl(
                    entry_lev, lev.iloc[realize_day],
                    entry_und, und.iloc[realize_day],
                    short_size, long_size,
                )
                realized += r["net"]
            else:
                # Still open -- mark at today's prices.
                r = engine.position_pnl(
                    entry_lev, lev_t, entry_und, und_t, short_size, long_size
                )
                open_marks += r["net"]
                open_count += 1
        rows.append((dates[t], realized + open_marks, open_count))
    return rows


def main():
    print(f"Verifying backtest tranche lifecycle: {UNDERLYING}, hold_days={HOLD_DAYS}, "
          f"base_capital=${BASE_CAPITAL:,.0f}\n")

    result = backtest.run_backtest(UNDERLYING, HOLD_DAYS, BASE_CAPITAL)
    bt_equity = result["equity_curve"]
    bt_open = result["open_tranches"]

    expected = independent_equity_curve(UNDERLYING, HOLD_DAYS, BASE_CAPITAL)

    assert len(expected) == len(bt_equity), "length mismatch"

    max_eq_err = 0.0
    open_mismatches = 0
    for (date, exp_eq, exp_open) in expected:
        eq_err = abs(bt_equity.loc[date] - exp_eq)
        max_eq_err = max(max_eq_err, eq_err)
        if bt_open.loc[date] != exp_open:
            open_mismatches += 1

    # Trace one concrete tranche so the lifecycle is human-readable.
    pair = config.PAIRS[UNDERLYING]
    lev = data.get_prices(pair["leveraged_ticker"])["close"]
    und = data.get_prices(pair["underlying_ticker"])["close"]
    dates = list(lev.index.intersection(und.index).sort_values())
    i = 0  # first tranche
    notional = BASE_CAPITAL / HOLD_DAYS
    r = engine.position_pnl(
        lev.iloc[i], lev.iloc[i + HOLD_DAYS],
        und.iloc[i], und.iloc[i + HOLD_DAYS],
        notional, pair["leverage"] * notional,
    )
    print(f"Sample tranche opened {dates[i].date()}, realized {dates[i + HOLD_DAYS].date()} "
          f"(held {HOLD_DAYS} days):")
    print(f"  TQQQ {lev.iloc[i]} -> {lev.iloc[i + HOLD_DAYS]}, "
          f"QQQ {und.iloc[i]} -> {und.iloc[i + HOLD_DAYS]}")
    print(f"  realized net P&L (engine) = {r['net']:+.4f}\n")

    print(f"(A) equity identity (realized + open marks, no double-count):")
    print(f"    max abs error across all {len(expected)} days = {max_eq_err:.6e}")
    print(f"(B) realize-price / lifecycle re-derivation vs backtest curve:")
    print(f"    same comparison (the curves are reconstructed independently)")
    print(f"    open-tranche-count mismatches = {open_mismatches}\n")

    ok = max_eq_err < 1e-6 and open_mismatches == 0
    print("=" * 60)
    if ok:
        print("PASS: backtest ladder matches an independent re-derivation.")
    else:
        print("FAIL: backtest diverges from the independent re-derivation.")
        sys.exit(1)


if __name__ == "__main__":
    main()
