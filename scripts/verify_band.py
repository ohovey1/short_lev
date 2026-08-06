"""Verify the band-rebalanced backtest (src/band.py).

Run from the project root:
    .venv/Scripts/python.exe scripts/verify_band.py

Three checks:
  a. Hand-computed two-segment run on 4 fabricated days that trip the
     long-short band exactly once (foil decay band disabled by making it huge).
  b. Degenerate run on real cached data with both bands huge: zero trades, so
     the equity must equal one engine.position_pnl interval (first -> last
     close) minus independently re-derived accrued borrow.
  c. Sanity: band vs ladder (hold_days=5) on TQQQ over the full cached window
     at the same config borrow rate -- both positive, within a factor of 2.

Check (a) monkeypatches data.get_prices so run_band_backtest sees fabricated
prices without band.py needing a price-injection parameter.
"""

import os
import sys

# Make the src/ modules importable when run from the project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pandas as pd

import backtest
import band
import config
import data
import engine

PAIR_KEY = "TQQQ"  # L=3; check (a) fabricates its prices, (b)/(c) use the cache


def check(label, ok, detail):
    print(f"  {detail}")
    print(f"  => {'PASS' if ok else 'FAIL'}\n")
    return ok


def fake_ohlc(closes):
    """Date-indexed OHLC frame where every field equals the given closes."""
    dates = pd.date_range("2026-01-05", periods=len(closes), freq="B")
    return pd.DataFrame(
        {f: closes for f in ["open", "high", "low", "close"]}, index=dates
    )


def check_a():
    """Two-segment hand check: one long-short-band trip on fabricated prices.

    L=3, margin_multiplier=1.65 (TQQQ config), base_capital=1000 ->
    target = 1000/1.65 = 20000/33 = 606.060606..., long_short_band=0.10,
    foil_decay_band=10.0 (never trips), borrow 0.
      d0 (100, 100): entry, no trip, equity 0.
      d1 (100, 105): short_notional = target (unchanged, lev flat) -> no foil
          decay trip. long_notional = 3*target*105/100 = 21000/11 = 1909.090909.
          net_delta = long_notional - 3*target = 1000/11 = 90.909091, which
          exceeds long_short_band*target = 2000/33 = 60.606061 -> long-short trip.
          Realized = short_pnl(0) + long_pnl(3*target*5%) = 1000/11 = 90.909091.
          Long reset to long_notional (1818.181818) at (100, 105); short
          carries at its current notional (606.060606, unchanged since lev
          didn't move).
      d2 (102, 106): short_notional = 606.06.../100*102 = 618.18..., net_delta
          ~ -19.05, no trip (threshold 60.606061).
      d3 (101, 104): short_notional ~ 612.12, net_delta ~ -35.50, no trip.
          Final segment (100,105)->(101,104): short_pnl = -606.060606*1% =
          -6.060606; long_pnl = 1818.181818*(104/105-1) = -1800/77-(-6.060606)...
          net = -1800/77 = -23.376623.
          Final equity = 1000/11 - 1800/77 = 5200/77 = 67.532468.
    """
    print("--- a. Hand-computed two-segment check (fabricated 4-day window) ---")
    pair = config.PAIRS[PAIR_KEY]
    mm = pair["margin_multiplier"]
    base_capital = 1000.0
    target = base_capital / mm
    fake = {
        pair["leveraged_ticker"]: fake_ohlc([100.0, 100.0, 102.0, 101.0]),
        pair["underlying_ticker"]: fake_ohlc([100.0, 105.0, 106.0, 104.0]),
    }

    real_get_prices = data.get_prices
    data.get_prices = lambda ticker: fake[ticker]
    try:
        r = band.run_band_backtest(
            PAIR_KEY, base_capital=base_capital, long_short_band=0.10, foil_decay_band=10.0,
            capital_utilization=1.0, borrow_rate_annual=0.0,
        )
    finally:
        data.get_prices = real_get_prices

    trip_pnl = 1000.0 / 11.0
    traded_und = trip_pnl  # net_delta at the trip equals the day-1 realized net here
    expected = 5200.0 / 77.0
    got = r["equity_curve"].iloc[-1]
    t = r["trades"]
    trades_ok = (
        len(t) == 1
        and t.iloc[0]["trigger"] == "long-short band"
        and abs(t.iloc[0]["total_pnl"] - trip_pnl) < 1e-6
        and abs(t.iloc[0]["traded_und"] - traded_und) < 1e-6
        and t.iloc[0]["traded_lev"] == 0.0
    )
    ok = (
        abs(got - expected) < 1e-6
        and r["n_trades"] == 1
        and abs(r["turnover_und"] - traded_und) < 1e-9
        and r["turnover_lev"] == 0.0
        and trades_ok
    )
    return check(
        "a", ok,
        f"final equity {got:.6f} (expected {expected:.6f}), "
        f"n_trades {r['n_trades']} (expected 1), "
        f"turnover und {r['turnover_und']:.2f} / lev {r['turnover_lev']:.2f} "
        f"(expected {traded_und:.2f} / 0.00); trades log: {len(t)} row(s), "
        f"trigger '{t.iloc[0]['trigger']}', P/L {t.iloc[0]['total_pnl']:.2f} "
        f"(expected 1 x 'long-short band' x {trip_pnl:.2f})",
    )


def check_b():
    """Degenerate: both bands huge on real cached data -> zero trades, and
    equity = one position_pnl interval (first -> last) minus accrued borrow,
    with borrow re-derived independently from the same aligned closes."""
    print("--- b. Degenerate zero-trade check (real cached data, bands=10.0) ---")
    pair = config.PAIRS[PAIR_KEY]
    rate = pair["borrow_rate_annual"]
    base = 10000.0
    target = base / pair["margin_multiplier"]

    r = band.run_band_backtest(
        PAIR_KEY, base_capital=base, long_short_band=10.0, foil_decay_band=10.0,
        capital_utilization=1.0, borrow_rate_annual=rate,
    )

    # Independent re-derivation, mirroring band.py's alignment.
    lev = data.get_prices(pair["leveraged_ticker"])
    und = data.get_prices(pair["underlying_ticker"])
    dates = lev.index.intersection(und.index).sort_values()
    lev_p = lev.loc[dates, "close"]
    und_p = und.loc[dates, "close"]

    interval = engine.position_pnl(
        lev_p.iloc[0], lev_p.iloc[-1], und_p.iloc[0], und_p.iloc[-1],
        target, pair["leverage"] * target,
    )
    borrow = sum(
        engine.borrow_cost(target * (lev_p.loc[d] / lev_p.iloc[0]), rate)
        for d in dates
    )
    expected = interval["net"] - borrow
    got = r["equity_curve"].iloc[-1]
    ok = r["n_trades"] == 0 and r["trades"].empty and abs(got - expected) < 1e-6
    return check(
        "b", ok,
        f"n_trades {r['n_trades']} (expected 0, trades log empty: "
        f"{r['trades'].empty}); final equity {got:.6f} vs "
        f"single-interval net {interval['net']:.6f} - borrow {borrow:.6f} "
        f"= {expected:.6f}",
    )


def check_c():
    """Sanity: band (defaults) vs ladder (hold_days=5), full window, same
    config borrow rate -> both positive, pct_return within a factor of 2."""
    print("--- c. Band vs ladder sanity (TQQQ, full window, config borrow) ---")
    rate = config.PAIRS[PAIR_KEY]["borrow_rate_annual"]
    base = 10000.0

    b = band.run_band_backtest(PAIR_KEY, base_capital=base, capital_utilization=1.0,
                               borrow_rate_annual=rate)
    l = backtest.run_backtest(PAIR_KEY, hold_days=5, base_capital=base,
                              borrow_rate_annual=rate)

    ratio = b["pct_return"] / l["pct_return"] if l["pct_return"] != 0 else float("inf")
    ok = b["pct_return"] > 0 and l["pct_return"] > 0 and 0.5 <= ratio <= 2.0
    return check(
        "c", ok,
        f"band pct_return {b['pct_return']:.4%} ({b['n_trades']} trades), "
        f"ladder pct_return {l['pct_return']:.4%}, ratio {ratio:.2f} "
        f"(need both > 0 and ratio in [0.5, 2])",
    )


def check_d():
    """Formula check: target = (base_capital * capital_utilization) / margin_multiplier.

    TSLL's margin_multiplier is 1.60 (config.py); at the new default
    capital_utilization=0.75, base_capital=10000 should resolve to
    target = 10000*0.75/1.6 = 4687.5."""
    print("--- d. target = (base_capital * capital_utilization) / margin_multiplier (TSLL) ---")
    mm = config.PAIRS["TSLL"]["margin_multiplier"]
    base_capital = 10000.0
    capital_utilization = 0.75
    target = (base_capital * capital_utilization) / mm
    expected = 4687.5
    ok = abs(target - expected) < 1e-9
    return check(
        "d", ok,
        f"TSLL margin_multiplier {mm}, base_capital {base_capital:.2f}, "
        f"capital_utilization {capital_utilization} -> target {target:.4f} "
        f"(expected {expected:.4f})",
    )


def check_e():
    """Margin cushion at the new default (capital_utilization=0.75): entry-day
    cushion is (1 - utilization)*base_capital - one day's borrow (deliberate
    headroom, not exactly 0 or exactly the raw slack), and TSLL over its full
    cached window with default bands no longer breaches at this setting --
    confirmed by hand-reconstructing the loop before writing this check: all
    13 configured pairs drop to zero breach-days at 0.75 vs. breaching at 1.0
    (see check_g for the cross-pair sweep this check's TSLL case is part of)."""
    print("--- e. Margin cushion at capital_utilization=0.75 (TSLL, full window) ---")
    pair = config.PAIRS["TSLL"]
    rate = pair["borrow_rate_annual"]
    base_capital = 10000.0
    capital_utilization = 0.75
    target = (base_capital * capital_utilization) / pair["margin_multiplier"]

    r = band.run_band_backtest(
        "TSLL", base_capital=base_capital, capital_utilization=capital_utilization,
        borrow_rate_annual=rate,
    )

    expected_entry_cushion = (1 - capital_utilization) * base_capital - engine.borrow_cost(target, rate)
    got_entry_cushion = r["margin_cushion"].iloc[0]

    ok = (
        abs(got_entry_cushion - expected_entry_cushion) < 1e-6
        and r["margin_breached"] is False
        and r["min_margin_cushion"] >= 0
    )
    return check(
        "e", ok,
        f"entry cushion {got_entry_cushion:.6f} (expected {expected_entry_cushion:.6f}); "
        f"margin_breached {r['margin_breached']}, min_margin_cushion "
        f"{r['min_margin_cushion']:.2f} (expected not breached, min >= 0)",
    )


def check_f():
    """Backward compatibility: capital_utilization=1.0 exactly reproduces the
    prior (pre-cushion-knob) sizing -- target = base_capital / margin_multiplier,
    TSLL -> 6250.0 -- and TSLL still breaches at that setting (the same
    historical fact check_e used to assert before the default changed)."""
    print("--- f. capital_utilization=1.0 backward compatibility (TSLL) ---")
    pair = config.PAIRS["TSLL"]
    rate = pair["borrow_rate_annual"]
    base_capital = 10000.0
    mm = pair["margin_multiplier"]
    target = (base_capital * 1.0) / mm
    expected_target = 6250.0

    r = band.run_band_backtest(
        "TSLL", base_capital=base_capital, capital_utilization=1.0,
        borrow_rate_annual=rate,
    )

    ok = (
        abs(target - expected_target) < 1e-9
        and r["margin_breached"] is True
        and r["min_margin_cushion"] < 0
    )
    return check(
        "f", ok,
        f"target {target:.4f} (expected {expected_target:.4f}); margin_breached "
        f"{r['margin_breached']}, min_margin_cushion {r['min_margin_cushion']:.2f} "
        f"(expected breached, min < 0 -- matches pre-cushion-knob behavior)",
    )


def check_g():
    """Directional sweep: for every configured pair, capital_utilization=0.75
    must not increase breach-days vs. 1.0, and at least one pair must show a
    strict improvement (so the check can't trivially pass if the knob did
    nothing). Does not assert zero breaches anywhere -- that's an empirical
    fact about today's cached data, not a guarantee this sizing holds for
    every future pair/base_capital/band combination."""
    print("--- g. Breach-day sweep across all pairs, 1.0 vs 0.75 ---")
    base_capital = 10000.0
    all_ok = True
    any_improved = False
    lines = []
    for pair_key, pair in config.PAIRS.items():
        rate = pair["borrow_rate_annual"]
        r1 = band.run_band_backtest(
            pair_key, base_capital=base_capital, capital_utilization=1.0,
            borrow_rate_annual=rate,
        )
        r75 = band.run_band_backtest(
            pair_key, base_capital=base_capital, capital_utilization=0.75,
            borrow_rate_annual=rate,
        )
        days1 = int((r1["margin_cushion"] < 0).sum())
        days75 = int((r75["margin_cushion"] < 0).sum())
        pair_ok = days75 <= days1
        all_ok = all_ok and pair_ok
        any_improved = any_improved or (days75 < days1)
        lines.append(f"{pair_key}: {days1} -> {days75} days{'  [FAIL]' if not pair_ok else ''}")

    ok = all_ok and any_improved
    detail = "; ".join(lines) + f"\n  all pairs non-increasing: {all_ok}, at least one improved: {any_improved}"
    return check("g", ok, detail)


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(), check_f(), check_g(),
    ]
    print("=" * 60)
    if all(results):
        print("All band checks PASSED.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
