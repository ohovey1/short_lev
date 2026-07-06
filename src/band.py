"""Layer 3 (sibling): band-rebalanced single-position backtest (the live-bot strategy).

One continuous position: short ~target notional of the LETF, long ~L*target of
the underlying. Between trades the hedge is frozen (a "segment"); the engine
marks each segment from its entry prices. Trades happen only when a band trips:

  - |short notional - target| > short_band * target
                                           -> reset short to target, re-neutralize
  - else |net delta| > delta_band * target -> re-neutralize via the LONG leg only

All P&L math goes through engine.position_pnl / engine.borrow_cost, same as
backtest.py. This file holds only state (the current segment) and the band
policy.
"""

import pandas as pd

import config
import data
import engine


def run_band_backtest(pair_key, base_capital, delta_band=0.10, short_band=0.10,
                      price_field="close", lookback_days=None,
                      borrow_rate_annual=None):
    """Run the band-rebalanced backtest for one pair.

    pair_key indexes config.PAIRS. target (the steady-state short notional) is
    base_capital, matching the ladder's total deployed capital. Returns a dict
    shaped like run_backtest where fields overlap, plus the trade stats
    (n_trades, turnover_lev, turnover_und, breakeven_borrow).
    """
    pair = config.PAIRS[pair_key]
    L = pair["leverage"]
    if borrow_rate_annual is None:
        borrow_rate_annual = pair["borrow_rate_annual"]

    lev = data.get_prices(pair["leveraged_ticker"])
    und = data.get_prices(pair["underlying_ticker"])
    dates = lev.index.intersection(und.index).sort_values()
    if lookback_days is not None:
        dates = dates[-lookback_days:]
    lev_p = lev.loc[dates, price_field]
    und_p = und.loc[dates, price_field]

    target = base_capital  # steady-state short notional, matches the ladder's total

    # Segment state: dollar sizes fixed at segment entry, marked by the engine.
    lev_e = lev_p.iloc[0]
    und_e = und_p.iloc[0]
    short_size = target
    long_size = L * target

    realized = 0.0
    borrow_paid = 0.0
    notional_days = 0.0
    turnover_lev = 0.0   # gross $ traded on the LETF leg
    turnover_und = 0.0   # gross $ traded on the underlying leg
    n_trades = 0

    seg_date = dates[0]  # entry date of the current segment
    trades = []          # one row per band-triggered rebalance (closed segment)
    equity = []

    for date in dates:
        lev_now = lev_p.loc[date]
        und_now = und_p.loc[date]

        short_notional = short_size * (lev_now / lev_e)
        long_notional = long_size * (und_now / und_e)
        net_delta = long_notional - L * short_notional

        # Band checks (short band first: its reset also re-neutralizes delta).
        if abs(short_notional - target) > short_band * target:
            r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
            realized += r["net"]
            traded_lev = abs(target - short_notional)
            traded_und = abs(L * target - long_notional)
            turnover_lev += traded_lev
            turnover_und += traded_und
            trades.append({
                "open_date": seg_date, "close_date": date, "trigger": "short band",
                "lev_entry": lev_e, "lev_exit": lev_now,
                "und_entry": und_e, "und_exit": und_now,
                "short_pnl": r["short_pnl"], "long_pnl": r["long_pnl"],
                "total_pnl": r["net"],
                "traded_lev": traded_lev, "traded_und": traded_und,
            })
            lev_e, und_e = lev_now, und_now
            short_size, long_size = target, L * target
            short_notional, long_notional = target, L * target
            seg_date = date
            n_trades += 1
        elif abs(net_delta) > delta_band * target:
            r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
            realized += r["net"]
            traded_und = abs(net_delta)             # long leg only
            turnover_und += traded_und
            trades.append({
                "open_date": seg_date, "close_date": date, "trigger": "delta band",
                "lev_entry": lev_e, "lev_exit": lev_now,
                "und_entry": und_e, "und_exit": und_now,
                "short_pnl": r["short_pnl"], "long_pnl": r["long_pnl"],
                "total_pnl": r["net"],
                "traded_lev": 0.0, "traded_und": traded_und,
            })
            lev_e, und_e = lev_now, und_now
            short_size = short_notional             # carry the short at its current value
            long_size = L * short_notional
            long_notional = long_size
            seg_date = date
            n_trades += 1

        borrow_paid += engine.borrow_cost(short_notional, borrow_rate_annual)
        notional_days += short_notional

        r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
        equity.append(realized + r["net"] - borrow_paid)

    equity_curve = pd.Series(equity, index=dates, name="equity")
    daily_pnl = equity_curve.diff()
    total_return = equity_curve.iloc[-1]

    ohlc_cols = ["open", "high", "low", "close"]
    lev_ohlc = lev.loc[dates, ohlc_cols]
    und_ohlc = und.loc[dates, ohlc_cols]

    return {
        "equity_curve": equity_curve,   # cumulative P/L ($), 0-based; UI adds base_capital
        "pct_return": total_return / base_capital,
        "borrow_paid": borrow_paid,
        "notional_days": notional_days,  # sum of current short notional per day
        "max_drawdown": (equity_curve - equity_curve.cummax()).min(),
        "worst_day": daily_pnl.min(),
        "n_trades": n_trades,
        "trades": pd.DataFrame(trades),  # final still-open segment not included
        "turnover_lev": turnover_lev,
        "turnover_und": turnover_und,
        "breakeven_borrow": engine.breakeven_borrow_rate(total_return + borrow_paid, notional_days),
        "lev_ohlc": lev_ohlc,
        "und_ohlc": und_ohlc,
    }
