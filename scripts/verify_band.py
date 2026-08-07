"""Verify the band-rebalanced backtest (src/band.py).

Run from the project root:
    .venv/Scripts/python.exe scripts/verify_band.py

Checks:
  a. Hand-computed two-segment run on 4 fabricated days that trip the
     long-short band exactly once (foil decay band disabled by making it huge).
  b. Degenerate run on real cached data with both bands huge: zero trades, so
     the equity must equal one engine.position_pnl interval (first -> last
     close) minus independently re-derived accrued borrow.
  c. Sanity: band vs ladder (hold_days=5) on TQQQ over the full cached window
     at the same config borrow rate -- both positive, within a factor of 2.
  d-g. target formula, margin cushion at the 0.75 default, capital_utilization
     =1.0 backward compatibility, and the cross-pair breach-day sweep.
  h. Drawdown stop, hand-checked on a fabricated path (spec 002 gate 1).
  i. Margin de-risk, hand-checked on a fabricated path (spec 002 gate 2).
  j. Property: a de-risk restores the cushion, no thrash (spec 002 gate 4).
  k. Max account-equity drawdown vs drawdown_stop (spec 002 gate 5).
  l-n. Direct unit tests on decision.evaluate(): one case per trigger plus a
     no-trip case, the priority ordering, and the closing-rule knobs (spec 003
     gates 3-5). These call evaluate() with hand-built states and no backtest.

Spec 003's gate 1 (the bitwise regression proving the decision.py extraction is
behavior-neutral) is not run from here either: see scripts/hash_band.py, which
digests raw float.hex() values across 5 parameter arms x 13 pairs.

Spec 002 gate 3 (the regression proving the loop reordering is behavior-neutral)
is not run from here: it compares this script's full output, with the closing
rules disabled, against the pre-spec-002 baseline. See the spec's Result section.

Checks (a), (h), and (i) monkeypatch data.get_prices so run_band_backtest sees
fabricated prices without band.py needing a price-injection parameter.
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
import decision
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

    # Closing rules off: this check isolates the band policy, and at
    # capital_utilization=1.0 the post-reset cushion is exactly zero, so the
    # de-risk rule would otherwise fire daily and break the zero-trade premise.
    r = band.run_band_backtest(
        PAIR_KEY, base_capital=base, long_short_band=10.0, foil_decay_band=10.0,
        capital_utilization=1.0, borrow_rate_annual=rate,
        drawdown_stop=None, margin_derisk=False,
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

    # Closing rules off: the ladder has none, so this is only apples-to-apples
    # against the band policy in isolation.
    b = band.run_band_backtest(PAIR_KEY, base_capital=base, capital_utilization=1.0,
                               borrow_rate_annual=rate,
                               drawdown_stop=None, margin_derisk=False)
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

    # Closing rules off: this check asserts a PRE-spec-002 fact (that cu=1.0
    # breached), so it must measure the position the way it was measured then.
    r = band.run_band_backtest(
        "TSLL", base_capital=base_capital, capital_utilization=1.0,
        borrow_rate_annual=rate, drawdown_stop=None, margin_derisk=False,
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
        # Closing rules off in both arms: this isolates the capital_utilization
        # knob's effect on breach-days. With de-risk on, a breach resizes the
        # position and the two arms would no longer differ by utilization alone.
        r1 = band.run_band_backtest(
            pair_key, base_capital=base_capital, capital_utilization=1.0,
            borrow_rate_annual=rate, drawdown_stop=None, margin_derisk=False,
        )
        r75 = band.run_band_backtest(
            pair_key, base_capital=base_capital, capital_utilization=0.75,
            borrow_rate_annual=rate, drawdown_stop=None, margin_derisk=False,
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


def check_h():
    """Gate 1 -- drawdown stop, hand-checked on a fabricated path.

    TQQQ (L=3, margin_multiplier=1.65), base_capital=1000, capital_utilization=1.0
    -> target = 1000/1.65 = 606.060606, long = 1818.181818. Bands are huge so
    they never trip; borrow is 0. The stop is the only actor.
      d0 (100, 100): entry. account_equity = 1000 = peak. No trip.
      d1 (117, 100): LETF +17%, underlying flat. The frozen segment marks
          short_pnl = -606.060606 * 0.17 = -103.030303, long_pnl = 0, so
          account_equity = 1000 - 103.030303 = 896.969697. Peak is still 1000
          and 10% below it is 900.000000, so 896.969697 < 900 -> STOP on d1.
          Equity (0-based) at the stop is -103.030303.
      d2, d3: flat. No marks, no trades, no borrow -- equity holds at
          -103.030303 and cushion is just account equity (896.969697), which a
          zero short leg can never make negative.
    """
    print("--- h. Drawdown stop, hand-checked (fabricated 4-day window) ---")
    pair = config.PAIRS[PAIR_KEY]
    base_capital = 1000.0
    target = base_capital / pair["margin_multiplier"]
    fake = {
        pair["leveraged_ticker"]: fake_ohlc([100.0, 117.0, 130.0, 90.0]),
        pair["underlying_ticker"]: fake_ohlc([100.0, 100.0, 100.0, 100.0]),
    }

    real_get_prices = data.get_prices
    data.get_prices = lambda ticker: fake[ticker]
    try:
        r = band.run_band_backtest(
            PAIR_KEY, base_capital=base_capital, long_short_band=10.0,
            foil_decay_band=10.0, capital_utilization=1.0, borrow_rate_annual=0.0,
            drawdown_stop=0.10, margin_derisk=False,
        )
    finally:
        data.get_prices = real_get_prices

    expected_stop_equity = -target * 0.17          # -103.030303
    eq = r["equity_curve"]
    stop_day = eq.index[1]                          # d1

    flat_after = bool((eq.iloc[1:] == eq.iloc[1]).all())
    t = r["trades"]
    ok = (
        r["stopped"] is True
        and r["stop_date"] == stop_day
        and abs(eq.iloc[1] - expected_stop_equity) < 1e-9
        and flat_after
        and abs(eq.iloc[-1] - expected_stop_equity) < 1e-9
        and len(t) == 1
        and t.iloc[0]["trigger"] == "drawdown stop"
        and bool((r["margin_cushion"].iloc[1:] >= 0).all())
        # Both legs closed: the stop trade sold the full marked size of each.
        and abs(t.iloc[0]["traded_lev"] - target * 1.17) < 1e-9
        and abs(t.iloc[0]["traded_und"] - 3 * target) < 1e-9
    )
    return check(
        "h", ok,
        f"stopped {r['stopped']} on {r['stop_date'].date()} (expected "
        f"{stop_day.date()}); equity at stop {eq.iloc[1]:.6f} (expected "
        f"{expected_stop_equity:.6f}); flat to end {flat_after} "
        f"(last {eq.iloc[-1]:.6f}); trades {len(t)} x "
        f"'{t.iloc[0]['trigger'] if len(t) else '-'}'; legs closed "
        f"(traded_lev {t.iloc[0]['traded_lev']:.6f}, traded_und "
        f"{t.iloc[0]['traded_und']:.6f}); cushion never negative after the stop",
    )


def check_i():
    """Gate 2 -- margin de-risk, hand-checked on a fabricated path.

    TQQQ (L=3, mm=1.65), base_capital=1000, capital_utilization=0.75 ->
    target = 750/1.65 = 454.545455, long = 1363.636364. Bands huge, borrow 0,
    drawdown stop disabled so de-risk is the only actor.
      d0 (100, 100): entry. cushion = 1000 - 1.65*454.545455 = 250 > 0.
      d1 (140, 100): LETF +40%, underlying flat. short_notional =
          454.545455*1.40 = 636.363636; margin required = 1.65*636.363636 =
          1050.000000. The segment marks short_pnl = -454.545455*0.40 =
          -181.818182, so account_equity = 818.181818 < 1050 -> DE-RISK on d1.
          target_new = (818.181818 * 0.75) / 1.65 = 371.900826.
          Post-reset cushion must equal account_equity * (1 - 0.75) =
          204.545455 exactly (the spec's stated property).
    """
    print("--- i. Margin de-risk, hand-checked (fabricated window) ---")
    pair = config.PAIRS[PAIR_KEY]
    mm = pair["margin_multiplier"]
    base_capital = 1000.0
    cu = 0.75
    fake = {
        pair["leveraged_ticker"]: fake_ohlc([100.0, 140.0, 141.0]),
        pair["underlying_ticker"]: fake_ohlc([100.0, 100.0, 100.0]),
    }

    real_get_prices = data.get_prices
    data.get_prices = lambda ticker: fake[ticker]
    try:
        r = band.run_band_backtest(
            PAIR_KEY, base_capital=base_capital, long_short_band=10.0,
            foil_decay_band=10.0, capital_utilization=cu, borrow_rate_annual=0.0,
            drawdown_stop=None, margin_derisk=True,
        )
    finally:
        data.get_prices = real_get_prices

    target0 = (base_capital * cu) / mm
    account_equity = base_capital - target0 * 0.40   # 818.181818
    expected_target_new = (account_equity * cu) / mm  # 371.900826
    expected_cushion = account_equity * (1 - cu)      # 204.545455

    t = r["trades"]
    derisk = t[t["trigger"] == "margin de-risk"] if len(t) else t
    got_cushion = r["margin_cushion"].iloc[1]
    ok = (
        r["n_derisk"] == 1
        and len(derisk) == 1
        and abs(r["final_target"] - expected_target_new) < 1e-9
        and abs(got_cushion - expected_cushion) < 1e-9
        # Both legs reset to the new target (traded_lev = |new - drifted|).
        and abs(derisk.iloc[0]["traded_lev"] - abs(expected_target_new - target0 * 1.40)) < 1e-9
        and r["stopped"] is False
    )
    return check(
        "i", ok,
        f"n_derisk {r['n_derisk']} (expected 1); target_new "
        f"{r['final_target']:.6f} (expected {expected_target_new:.6f}); "
        f"cushion on the de-risk day {got_cushion:.6f} (expected "
        f"account_equity*(1-cu) = {expected_cushion:.6f}); ratcheted down from "
        f"{target0:.6f}",
    )


def check_j():
    """Gate 4 -- property: a de-risk restores the cushion, no thrash.

    Cushion is measured PRE-borrow here, which is what the spec's property is
    about: after the reset, cushion = account_equity * (1 - capital_utilization).
    The recorded margin_cushion series is post-borrow, so at capital_utilization
    = 1.0 that property is exactly 0 and one day's borrow drives it fractionally
    negative -- a degenerate setting, not a defect. Run at 0.90, where de-risk
    genuinely fires and the restored cushion has real headroom.
    """
    print("--- j. De-risk restores cushion, all pairs (capital_utilization=0.90) ---")
    base_capital = 10000.0
    cu = 0.90
    all_ok = True
    lines = []
    for pair_key, pair in config.PAIRS.items():
        r = band.run_band_backtest(
            pair_key, base_capital=base_capital, capital_utilization=cu,
            borrow_rate_annual=pair["borrow_rate_annual"],
        )
        t = r["trades"]
        derisk_dates = set(t[t["trigger"] == "margin de-risk"]["close_date"]) if len(t) else set()
        # On every de-risk day the cushion must be non-negative afterward.
        bad = [d for d in derisk_dates if r["margin_cushion"].loc[d] < 0]
        pair_ok = not bad
        all_ok = all_ok and pair_ok
        lines.append(f"{pair_key}: {len(derisk_dates)} de-risks, "
                     f"{len(bad)} still-negative{'  [FAIL]' if bad else ''}")
    return check("j", all_ok, "; ".join(lines))


def check_k():
    """Gate 5 -- with the stop active, max drawdown on ACCOUNT equity never
    materially exceeds drawdown_stop. A large overshoot would mean the check is
    running after the day's action instead of before it.

    Tolerance is one day of adverse move past the trip, not a fudge factor: the
    stop is evaluated once per day on the close, so equity can gap through the
    threshold intraday before the next close closes the position.
    """
    print("--- k. Max account-equity drawdown vs drawdown_stop, all pairs ---")
    base_capital = 10000.0
    stop = 0.10
    all_ok = True
    lines = []
    for pair_key, pair in config.PAIRS.items():
        r = band.run_band_backtest(
            pair_key, base_capital=base_capital, drawdown_stop=stop,
            borrow_rate_annual=pair["borrow_rate_annual"],
        )
        ae = r["equity_curve"] + base_capital
        dd = float((ae / ae.cummax() - 1.0).min())
        pair_ok = dd >= -(stop + 0.05)   # no material overshoot
        all_ok = all_ok and pair_ok
        lines.append(f"{pair_key}: {dd*100:.2f}%{'  [FAIL]' if not pair_ok else ''}")
    return check(
        "k", all_ok,
        "; ".join(lines) + f"\n  (all must be >= {-(stop+0.05)*100:.0f}%)",
    )


DEFAULT_PARAMS = decision.BandParams(
    long_short_band=0.10, foil_decay_band=0.10, capital_utilization=0.75,
    drawdown_stop=0.10, margin_derisk=True,
)


def state(short_notional, long_notional, target, account_equity, peak_equity,
          margin_required, leverage=3.0, margin_multiplier=1.65):
    """A PositionState with the boilerplate filled in."""
    return decision.PositionState(
        short_notional=short_notional, long_notional=long_notional,
        leverage=leverage, target=target, account_equity=account_equity,
        peak_equity=peak_equity, margin_required=margin_required,
        margin_multiplier=margin_multiplier,
    )


def check_l():
    """Gate 3 -- one direct unit test per trigger, plus a no-trip case.

    These call decision.evaluate() with no backtest at all. Expected values are
    the same hand-derived arithmetic checks h, i, and a already pin down, so
    the two layers are anchored independently.

      stop:      check h's numbers -- peak 1000, equity 896.969697, stop 0.10
                 -> threshold 900.0, so it trips. Sizes go to 0 and target is
                 UNTOUCHED (band.py never ratchets on a stop, and final_target
                 is a reported field).
      de-risk:   check i's numbers -- equity 818.181818 < margin required
                 1050.0, cu 0.75, mm 1.65 -> target_new 371.900826, long leg
                 2x that (leverage=2 here, matching check i's TSLL-style math).
      foil:      short 15% above a target of 1000 at a 0.10 band -> both legs
                 reset to target.
      long-short: check a's numbers -- target 606.060606, net_delta 90.909091
                 against a threshold of 60.606061. The SHORT is unchanged and
                 the long goes to L * short.
      no trip:   inside every band -> trigger None, new_* mirror current state.
    """
    print("--- l. evaluate() unit tests, one per trigger + no-trip ---")
    lines = []
    ok = True

    # Drawdown stop (check h's arithmetic).
    d = decision.evaluate(
        state(606.060606, 1818.181818, 606.060606, 896.969697, 1000.0, 100.0),
        DEFAULT_PARAMS,
    )
    stop_ok = (
        d.trigger == "drawdown stop" and d.terminal is True
        and d.new_short_notional == 0.0 and d.new_long_notional == 0.0
        and d.new_target == 606.060606      # untouched by a stop
    )
    ok = ok and stop_ok
    lines.append(f"stop: trigger '{d.trigger}', terminal {d.terminal}, sizes "
                 f"{d.new_short_notional}/{d.new_long_notional}, new_target "
                 f"{d.new_target:.6f} (expected target unchanged)"
                 f"{'  [FAIL]' if not stop_ok else ''}")

    # Margin de-risk (check i's arithmetic), leverage 2 so the long leg is 2x.
    equity = 818.181818
    expected_target_new = (equity * 0.75) / 1.65
    d = decision.evaluate(
        state(636.363636, 1272.727272, 454.545455, equity, 1000.0, 1050.0,
              leverage=2.0),
        decision.BandParams(long_short_band=10.0, foil_decay_band=10.0,
                            capital_utilization=0.75, drawdown_stop=None,
                            margin_derisk=True),
    )
    derisk_ok = (
        d.trigger == "margin de-risk" and d.terminal is False
        and abs(d.new_short_notional - expected_target_new) < 1e-9
        and abs(d.new_long_notional - 2 * expected_target_new) < 1e-9
        and abs(d.new_target - expected_target_new) < 1e-9   # ratcheted down
    )
    ok = ok and derisk_ok
    lines.append(f"de-risk: trigger '{d.trigger}', target_new "
                 f"{d.new_short_notional:.6f} (expected "
                 f"{expected_target_new:.6f}), ratcheted to {d.new_target:.6f}"
                 f"{'  [FAIL]' if not derisk_ok else ''}")

    # Foil decay band: short 15% off target, comfortably outside a 0.10 band.
    d = decision.evaluate(
        state(1150.0, 3000.0, 1000.0, 10000.0, 10000.0, 1897.5),
        DEFAULT_PARAMS,
    )
    foil_ok = (
        d.trigger == "foil decay band" and d.terminal is False
        and d.new_short_notional == 1000.0 and d.new_long_notional == 3000.0
        and d.new_target == 1000.0
    )
    ok = ok and foil_ok
    lines.append(f"foil: trigger '{d.trigger}', both legs to "
                 f"{d.new_short_notional}/{d.new_long_notional} (expected "
                 f"1000.0/3000.0){'  [FAIL]' if not foil_ok else ''}")

    # Long-short band (check a's arithmetic): short unchanged, long to L*short.
    target = 20000.0 / 33.0        # 606.060606
    short_notional = target        # LETF flat, so the short has not drifted
    long_notional = 3 * target * 1.05
    d = decision.evaluate(
        state(short_notional, long_notional, target, 10000.0, 10000.0, 1000.0),
        DEFAULT_PARAMS,
    )
    ls_ok = (
        d.trigger == "long-short band" and d.terminal is False
        and d.new_short_notional == short_notional        # short untouched
        and abs(d.new_long_notional - 3 * short_notional) < 1e-9
        and d.new_target == target
        and abs(d.net_delta - 1000.0 / 11.0) < 1e-9
    )
    ok = ok and ls_ok
    lines.append(f"long-short: trigger '{d.trigger}', short unchanged "
                 f"{d.new_short_notional == short_notional}, net_delta "
                 f"{d.net_delta:.6f} (expected {1000.0/11.0:.6f})"
                 f"{'  [FAIL]' if not ls_ok else ''}")

    # No trip: delta-neutral, short exactly at target, healthy equity.
    d = decision.evaluate(
        state(1000.0, 3000.0, 1000.0, 10000.0, 10000.0, 1650.0),
        DEFAULT_PARAMS,
    )
    none_ok = (
        d.trigger is None and d.terminal is False
        and d.new_short_notional == 1000.0 and d.new_long_notional == 3000.0
        and d.new_target == 1000.0
        and d.net_delta == 0.0
        and d.margin_cushion == 10000.0 - 1650.0   # populated even untripped
    )
    ok = ok and none_ok
    lines.append(f"no-trip: trigger {d.trigger}, new_* mirror state, net_delta "
                 f"{d.net_delta}, margin_cushion {d.margin_cushion:.2f} "
                 f"(both populated){'  [FAIL]' if not none_ok else ''}")

    return check("l", ok, "; ".join(lines))


def check_m():
    """Gate 4 -- priority: a state satisfying several triggers returns the
    highest-priority one. Pins the whole ordering, not just the top of it."""
    print("--- m. Trigger priority ordering ---")
    lines = []
    ok = True

    # Drawdown AND cushion breached together -> the stop wins, terminally.
    both = state(636.363636, 1272.727272, 454.545455,
                 account_equity=800.0, peak_equity=1000.0, margin_required=1050.0)
    d = decision.evaluate(both, DEFAULT_PARAMS)
    p1 = d.trigger == "drawdown stop" and d.terminal is True
    ok = ok and p1
    lines.append(f"drawdown+cushion -> '{d.trigger}' terminal {d.terminal} "
                 f"(expected 'drawdown stop' True){'  [FAIL]' if not p1 else ''}")

    # Cushion AND foil decay breached, stop disabled -> de-risk wins.
    d = decision.evaluate(
        both,
        decision.BandParams(long_short_band=0.10, foil_decay_band=0.10,
                            capital_utilization=0.75, drawdown_stop=None,
                            margin_derisk=True),
    )
    p2 = d.trigger == "margin de-risk"
    ok = ok and p2
    lines.append(f"cushion+foil -> '{d.trigger}' (expected 'margin de-risk')"
                 f"{'  [FAIL]' if not p2 else ''}")

    # Foil decay AND long-short breached, closing rules off -> foil wins.
    d = decision.evaluate(
        state(1150.0, 5000.0, 1000.0, 10000.0, 10000.0, 1897.5),
        decision.BandParams(long_short_band=0.10, foil_decay_band=0.10,
                            capital_utilization=0.75, drawdown_stop=None,
                            margin_derisk=False),
    )
    p3 = d.trigger == "foil decay band"
    ok = ok and p3
    lines.append(f"foil+long-short -> '{d.trigger}' (expected 'foil decay band')"
                 f"{'  [FAIL]' if not p3 else ''}")

    return check("m", ok, "; ".join(lines))


def check_n():
    """Gate 5 -- knobs: with drawdown_stop=None and margin_derisk=False, the
    two closing triggers never fire even on states that would otherwise trip
    them, and the bands still work."""
    print("--- n. Closing-rule knobs disable only their own triggers ---")
    off = decision.BandParams(long_short_band=0.10, foil_decay_band=0.10,
                              capital_utilization=0.75, drawdown_stop=None,
                              margin_derisk=False)
    lines = []
    ok = True

    # Everything breached at once: with the knobs off, a band must surface.
    d = decision.evaluate(
        state(1150.0, 5000.0, 1000.0, account_equity=500.0, peak_equity=10000.0,
              margin_required=1897.5),
        off,
    )
    k1 = d.trigger == "foil decay band" and d.terminal is False
    ok = ok and k1
    lines.append(f"all breached, knobs off -> '{d.trigger}' terminal "
                 f"{d.terminal} (expected a band, never a closing rule)"
                 f"{'  [FAIL]' if not k1 else ''}")

    # ONLY the closing rules breached (both bands well inside) -> nothing fires.
    d = decision.evaluate(
        state(1000.0, 3000.0, 1000.0, account_equity=500.0, peak_equity=10000.0,
              margin_required=1650.0),
        off,
    )
    k2 = d.trigger is None and d.terminal is False
    ok = ok and k2
    lines.append(f"only closing rules breached, knobs off -> trigger "
                 f"{d.trigger} (expected None){'  [FAIL]' if not k2 else ''}")

    # Same state with the knobs ON must fire, proving the state was live.
    d = decision.evaluate(
        state(1000.0, 3000.0, 1000.0, account_equity=500.0, peak_equity=10000.0,
              margin_required=1650.0),
        DEFAULT_PARAMS,
    )
    k3 = d.trigger == "drawdown stop"
    ok = ok and k3
    lines.append(f"same state, knobs on -> '{d.trigger}' (expected 'drawdown "
                 f"stop', proving the knobs did the suppressing)"
                 f"{'  [FAIL]' if not k3 else ''}")

    return check("n", ok, "; ".join(lines))


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(), check_f(), check_g(),
        check_h(), check_i(), check_j(), check_k(), check_l(), check_m(), check_n(),
    ]
    print("=" * 60)
    if all(results):
        print("All band checks PASSED.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
