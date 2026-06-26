"""Verify the engine and explain what its numbers mean.

Run from the project root:
    .venv/Scripts/python.exe scripts/verify_engine.py

This checks the engine in isolation. One position is SHORT $1,000 of the 3x
leveraged ETF (TQQQ) and LONG $3,000 of the underlying (QQQ). We apply a price
move (entry -> current) to both fixed legs and record the net. This script
feeds known moves to engine.position_pnl and checks the result against a
by-hand answer.

Because TQQQ is +3x, shorting it gives ~-3x QQQ delta and the long $3X QQQ leg
cancels it: a single marked position is roughly delta-neutral, so its residual
net is one interval's embedded cost drag + tracking error -- NOT decay. Real
decay is the multi-day, overlapping-tranche effect measured by the backtest
layer, not by any single interval here.
"""

import os
import sys

# Make the src/ modules importable when run from the project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import data
import engine

PAIR = config.PAIRS["QQQ"]
LEV_TICKER = PAIR["leveraged_ticker"]       # e.g. TQQQ; sourced from the registry
UND_TICKER = PAIR["underlying_ticker"]      # e.g. QQQ

SHORT_SIZE = 1000  # dollars short on the leveraged ETF
LONG_SIZE = 3000   # dollars long on the underlying


def show(label, lev_start, lev_end, und_start, und_end, expected_net, note):
    """Run one interval through the engine and print a readable breakdown."""
    r = engine.position_pnl(lev_start, lev_end, und_start, und_end, SHORT_SIZE, LONG_SIZE)
    lev_move = (lev_end / lev_start - 1) * 100
    und_move = (und_end / und_start - 1) * 100

    print(f"--- {label} ---")
    print(f"  leveraged ETF (we are SHORT ${SHORT_SIZE}): {lev_start} -> {lev_end}  ({lev_move:+.2f}%)")
    print(f"  underlying    (we are LONG  ${LONG_SIZE}): {und_start} -> {und_end}  ({und_move:+.2f}%)")
    print(f"  short leg P&L: {r['short_pnl']:+.2f}   (we gain when the leveraged ETF falls)")
    print(f"  long leg P&L : {r['long_pnl']:+.2f}   (we gain when the underlying rises)")
    print(f"  NET P&L      : {r['net']:+.2f}   (price legs only; borrow handled in backtest)")
    print(f"  -> {note}")

    ok = abs(r["net"] - expected_net) < 1e-6
    print(f"  expected net {expected_net:+.2f}  =>  {'PASS' if ok else 'FAIL'}\n")
    return ok


def main():
    print(f"Position: short ${SHORT_SIZE:,} {LEV_TICKER}, long ${LONG_SIZE:,} {UND_TICKER}. P&L is in dollars.\n")
    results = []

    # 1. Flat day: nothing moves, so we make and lose nothing. The gate's zero check.
    results.append(show(
        "Flat day (no move in either leg)",
        lev_start=100, lev_end=100, und_start=50, und_end=50,
        expected_net=0,
        note="No price change anywhere, so net P&L is exactly 0.",
    ))

    # 2. Known-move day: leveraged ETF -10%, underlying +3%. By hand:
    #    short leg = -1000 * -0.10 = +100 ; long leg = 3000 * 0.03 = +90 ; net = +190.
    # NOTE: these two moves are NOT 3x-consistent (a real +3% QQQ day implies ~-9%
    # for a +3x fund). They are deliberately round so each leg's sign and arithmetic
    # are trivial to hand-check in isolation. This is a sign/arithmetic check, not a
    # realistic day -- see the real cached day below for that.
    results.append(show(
        "Known-move day (ETF -10%, underlying +3%) -- sign/arithmetic check",
        lev_start=100, lev_end=90, und_start=50, und_end=51.5,
        expected_net=190,
        note="Both legs happen to move our way here, so the gains add to +190.",
    ))

    # 3. Flipped: same magnitudes the other direction -> net should mirror to -190.
    results.append(show(
        "Flipped day (ETF +10%, underlying -3%)",
        lev_start=100, lev_end=110, und_start=50, und_end=48.5,
        expected_net=-190,
        note="Both legs move against us, so net is the mirror image: -190.",
    ))

    # 4. Real data: two consecutive cached closes for the leveraged fund vs the
    #    underlying (prior close -> close). Tickers come from config.PAIRS so this
    #    can't drift from the registry.
    lev = data.get_prices(LEV_TICKER)
    und = data.get_prices(UND_TICKER)
    lev_start, lev_end = lev["close"].iloc[-2], lev["close"].iloc[-1]
    und_start, und_end = und["close"].iloc[-2], und["close"].iloc[-1]
    real = engine.position_pnl(lev_start, lev_end, und_start, und_end, SHORT_SIZE, LONG_SIZE)
    show(
        f"Real cached interval ({und.index[-1].date()}, {LEV_TICKER} vs {UND_TICKER} closes)",
        lev_start, lev_end, und_start, und_end,
        expected_net=real["net"],  # no by-hand target; just display + interpret
        note="One real interval with the legs hedged: short and long P&L roughly cancel, "
             "leaving a small residual = this interval's embedded cost drag + tracking "
             "error, NOT decay. Decay is the multi-day ladder effect, measured by the "
             "backtest -- not by a single interval like this one.",
    )

    print("=" * 60)
    if all(results):
        print("All synthetic gate checks PASSED.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
