"""Layer 3: overlapping-tranche, multi-day-hold backtest.

Strategy: open one tranche per day at that day's prices and hold each tranche
for hold_days days. On any mid-window day, hold_days tranches are open at
staggered ages; the window edges (warmup/winddown) have fewer. This ladder of
overlapping holds is what captures the leveraged fund's multi-day decay (the
old daily-reset version only scraped one interval's cost drag).

This layer holds all state (the tranche ladder + realized P&L). It calls the
engine for every P&L number and contains NO P&L math of its own.

Fair-comparison normalization: longer hold_days means more open tranches and
thus more gross exposure, so raw dollar P&L would not be comparable across
hold_days. We size each tranche so total deployed capital is constant:
per-tranche notional = base_capital / hold_days.
"""

import pandas as pd

import config
import data
import engine


def run_backtest(pair_key, hold_days, base_capital, price_field="close",
                 lookback_days=None):
    """Run the overlapping-tranche backtest for one pair.

    pair_key indexes config.PAIRS (the leveraged ETF ticker).

    A tranche opened on day d is realized at the close of day d + hold_days, so
    on any mid-window day exactly hold_days tranches are open.

    lookback_days restricts the run to the last N trading days of the cached
    window (None = full window). Slicing happens before the loop, so the curve
    and metrics are computed on the same window.

    Returns a dict with the equity curve (a pandas Series indexed by date), the
    daily P&L series, and metrics (total return, max drawdown, worst day).
    """
    pair = config.PAIRS[pair_key]
    leverage = pair["leverage"]

    lev = data.get_prices(pair["leveraged_ticker"])
    und = data.get_prices(pair["underlying_ticker"])

    # Align on the dates both legs have, in order, then optionally trim to the
    # most recent lookback_days.
    dates = lev.index.intersection(und.index).sort_values()
    if lookback_days is not None:
        dates = dates[-lookback_days:]
    lev_prices = lev.loc[dates, price_field]
    und_prices = und.loc[dates, price_field]

    # Each tranche is its own delta-neutral two-leg position. Normalize so total
    # deployed capital is constant across hold_days.
    notional = base_capital / hold_days
    short_size = notional
    long_size = leverage * notional

    open_tranches = []   # each: {entry_date, entry_lev, entry_und, age}
    realized_pnl = 0.0
    realized_short = 0.0
    realized_long = 0.0
    borrow_paid = 0.0

    equity = []
    open_counts = []
    long_pts = []   # per-day cumulative long-leg P/L (realized + open marks)
    short_pts = []  # per-day cumulative short-leg P/L
    trades = []     # one row per fully-realized tranche (closed trade)

    n = len(dates)
    for i, date in enumerate(dates):
        lev_now = lev_prices.loc[date]
        und_now = und_prices.loc[date]

        # 1. Age yesterday's open tranches by one day. Any that have now been held
        #    hold_days days (opened on day d, today is d + hold_days) realize at
        #    today's prices and drop out of the ladder.
        still_open = []
        for t in open_tranches:
            t["age"] += 1
            if t["age"] >= hold_days:
                r = engine.position_pnl(
                    t["entry_lev"], lev_now, t["entry_und"], und_now, short_size, long_size
                )
                realized_pnl += r["net"]
                realized_short += r["short_pnl"]
                realized_long += r["long_pnl"]
                trades.append({
                    "open_date": t["entry_date"],
                    "close_date": date,
                    "lev_entry": t["entry_lev"],
                    "lev_exit": lev_now,
                    "und_entry": t["entry_und"],
                    "und_exit": und_now,
                    "short_pnl": r["short_pnl"],
                    "long_pnl": r["long_pnl"],
                    "total_pnl": r["net"],
                })
            else:
                still_open.append(t)
        open_tranches = still_open

        # 2. Open a new tranche at today's prices (age 0) -- but only if it can
        #    complete a full hold_days hold before the window ends. This tapers the
        #    tail (winddown) so every opened tranche realizes, symmetric with the
        #    warmup ramp at the start.
        if n - i > hold_days:
            open_tranches.append(
                {"entry_date": date, "entry_lev": lev_now, "entry_und": und_now, "age": 0}
            )

        # 3. Mark every open tranche via the engine; sum their current P&L, split by
        #    leg. Charge borrow per open tranche per day (0 in v1, kept in equity).
        open_pnl = 0.0
        open_short = 0.0
        open_long = 0.0
        for t in open_tranches:
            r = engine.position_pnl(
                t["entry_lev"], lev_now, t["entry_und"], und_now, short_size, long_size
            )
            open_pnl += r["net"]
            open_short += r["short_pnl"]
            open_long += r["long_pnl"]
            borrow_paid += engine.borrow_cost(notional)

        # 4. Equity = realized + open marks - borrow paid to date. The per-leg curves
        #    sum back to this (borrow = 0 in v1).
        equity.append(realized_pnl + open_pnl - borrow_paid)
        short_pts.append(realized_short + open_short)
        long_pts.append(realized_long + open_long)
        open_counts.append(len(open_tranches))

    equity_curve = pd.Series(equity, index=dates, name="equity")
    open_curve = pd.Series(open_counts, index=dates, name="open_tranches")
    long_curve = pd.Series(long_pts, index=dates, name="long")
    short_curve = pd.Series(short_pts, index=dates, name="short")
    daily_pnl = equity_curve.diff()
    trades_df = pd.DataFrame(trades)

    ohlc_cols = ["open", "high", "low", "close"]
    lev_ohlc = lev.loc[dates, ohlc_cols]
    und_ohlc = und.loc[dates, ohlc_cols]

    total_return = equity_curve.iloc[-1]
    return {
        "equity_curve": equity_curve,   # cumulative P/L ($), 0-based; UI adds base_capital
        "daily_pnl": daily_pnl,
        "open_tranches": open_curve,
        "long_curve": long_curve,       # cumulative long-leg P/L ($), 0-based
        "short_curve": short_curve,     # cumulative short-leg P/L ($), 0-based
        "trades": trades_df,
        "lev_ohlc": lev_ohlc,
        "und_ohlc": und_ohlc,
        "starting_capital": base_capital,
        "ending_capital": base_capital + total_return,
        "total_return": total_return,
        "pct_return": total_return / base_capital,
        "max_drawdown": (equity_curve - equity_curve.cummax()).min(),
        "worst_day": daily_pnl.min(),
    }
