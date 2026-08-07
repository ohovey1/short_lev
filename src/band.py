"""Layer 3 (sibling): band-rebalanced single-position backtest (the live-bot strategy).

One continuous position: short ~target notional of the LETF, long ~L*target of
the underlying. Between trades the hedge is frozen (a "segment"); the engine
marks each segment from its entry prices. Trades happen only when a band trips:

  - |short notional - target| > foil_decay_band * target
                                           -> reset short to target, re-neutralize
  - else |net delta| > long_short_band * target -> re-neutralize via the LONG leg only

All P&L math goes through engine.position_pnl / engine.borrow_cost, same as
backtest.py. This file holds only state (the current segment) and the band
policy.

Two closing rules run ahead of the bands, checked in this order:

  - account equity below drawdown_stop of its running peak -> close both legs,
    terminally. The run stays flat for the remainder of the window.
  - margin cushion (equity - margin_required) negative -> reset both legs to a
    target recomputed from current equity. A partial reset, not a close.

Margin cushion is therefore a live trigger, not an observation: it feeds back
into the trigger logic and can resize the position. capital_utilization
(default 0.75) leaves a deliberate slice of base_capital undeployed as headroom
against the cushion going negative -- it reduces breach risk, it does not
eliminate it, which is why the de-risk rule exists.
"""

import pandas as pd

import config
import data
import engine


def run_band_backtest(pair_key, base_capital, long_short_band=0.10, foil_decay_band=0.10,
                      capital_utilization=0.75, price_field="close",
                      lookback_days=None, borrow_rate_annual=None,
                      drawdown_stop=0.10, margin_derisk=True):
    """Run the band-rebalanced backtest for one pair.

    pair_key indexes config.PAIRS. target (the steady-state short notional) is
    derived from base_capital via the pair's margin_multiplier and
    capital_utilization: target = (base_capital * capital_utilization) /
    margin_multiplier, i.e. the short notional that the utilized fraction of
    base_capital (cash) supports at that pair's margin rate. The rest
    (1 - capital_utilization) is deliberate slack, kept as headroom against
    margin cushion going negative. base_capital itself keeps meaning "cash
    deployed" -- it's still the pct_return denominator regardless of
    utilization. Returns a dict shaped like run_backtest where fields
    overlap, plus the trade stats (n_trades, turnover_lev, turnover_und,
    breakeven_borrow) and margin cushion stats (margin_cushion,
    min_margin_cushion, margin_breached, margin_breach_date) -- observation
    only, computed from the same short_notional the band checks already use.

    Bands are evaluated once per day on the close; live polling (target: every
    15 minutes) will trip bands strictly more often. n_trades and turnover
    here are therefore a lower bound on live behavior.

    Two closing rules run ahead of the bands, both measured on account equity
    (base_capital + cumulative P&L), not on the 0-based equity_curve:

      drawdown_stop  -- fraction below the running equity peak that closes both
                        legs terminally. Default 0.10. Once stopped the run
                        stays flat: no marks, no trades, no borrow, and the
                        curve flat-lines to the end of the window.
      margin_derisk  -- when True (default), a negative margin cushion resets
                        both legs to a target recomputed from current equity.
                        target ratchets DOWN only and never grows back.

    Passing drawdown_stop=None and margin_derisk=False disables both and
    reproduces pre-spec-002 output exactly. They are TEST KNOBS for that
    regression check, not strategy parameters -- live behavior is the defaults.
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

    # Steady-state short notional. Loop state, not a constant: a margin de-risk
    # recomputes it from current equity and ratchets it DOWN (never up), so the
    # foil decay band below always measures against the *current* target.
    target = (base_capital * capital_utilization) / pair["margin_multiplier"]

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
    margin_cushion = []  # equity - margin_required, per day

    # Closing-rule state. peak_equity starts at base_capital, not zero: the
    # drawdown stop measures account equity against its running peak, and the
    # run starts at base_capital by definition.
    peak_equity = base_capital
    stopped = False
    stop_date = None
    n_derisk = 0

    for date in dates:
        # Flat after a stop: no legs, so nothing to mark, trade, or borrow. The
        # curve carries its stopped value to the end of the window so the chart
        # and every downstream metric still compute. Cushion is just equity
        # (margin required on a zero short is zero), hence never negative.
        if stopped:
            equity.append(equity[-1])
            margin_cushion.append(equity[-1] + base_capital)
            continue

        lev_now = lev_p.loc[date]
        und_now = und_p.loc[date]

        # --- 1. Mark the position, before any action ------------------------
        short_notional = short_size * (lev_now / lev_e)
        long_notional = long_size * (und_now / und_e)
        net_delta = long_notional - L * short_notional

        # Account equity BEFORE any action today: base capital plus realized P&L
        # plus the open segment's mark, less borrow accrued through YESTERDAY
        # (today's accrual is charged after the action, below). The closing rules
        # measure against this, never against equity_curve -- that series is
        # 0-based cumulative P&L, and a 10% drawdown on a near-zero series would
        # be meaningless.
        #
        # This is a pure read: realizing a segment moves value from the mark into
        # `realized` without changing the total, so peeking here cannot perturb
        # any existing number.
        mark = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
        account_equity = base_capital + realized + mark["net"] - borrow_paid

        # --- 2-4. Triggers, highest urgency first ---------------------------
        # One chain, so at most one of these fires on a given day. Order is
        # load-bearing: drawdown stop (terminal) beats margin de-risk (its reset
        # also re-neutralizes delta, making the bands moot that day), which beats
        # the bands. Steps 2 and 3 land in the next two spec items; the band
        # checks below are step 4 and are unchanged.
        #
        peak_equity = max(peak_equity, account_equity)

        if drawdown_stop is not None and account_equity < peak_equity * (1 - drawdown_stop):
            # Terminal: realize the open segment, close both legs, stay flat.
            r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
            realized += r["net"]
            traded_lev = short_notional
            traded_und = long_notional
            turnover_lev += traded_lev
            turnover_und += traded_und
            trades.append({
                "open_date": seg_date, "close_date": date, "trigger": "drawdown stop",
                "lev_entry": lev_e, "lev_exit": lev_now,
                "und_entry": und_e, "und_exit": und_now,
                "short_pnl": r["short_pnl"], "long_pnl": r["long_pnl"],
                "total_pnl": r["net"],
                "traded_lev": traded_lev, "traded_und": traded_und,
            })
            short_size, long_size = 0.0, 0.0
            short_notional, long_notional = 0.0, 0.0
            seg_date = date
            n_trades += 1
            stopped = True
            stop_date = date
        elif margin_derisk and account_equity < pair["margin_multiplier"] * short_notional:
            # Cushion is negative (equity < margin required). Recompute target
            # from CURRENT equity and reset both legs to it -- a partial reset,
            # not a close. Ranks above the bands: being under-margined is more
            # urgent than being off-hedge, and this reset re-neutralizes delta
            # anyway, so the band checks would be moot today.
            r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
            realized += r["net"]

            if account_equity <= 0:
                # Nothing left to margin. Close rather than resize; a target
                # computed from non-positive equity is meaningless. In practice
                # the drawdown stop fires long before this.
                target_new = 0.0
            else:
                target_new = (account_equity * capital_utilization) / pair["margin_multiplier"]

            traded_lev = abs(target_new - short_notional)
            traded_und = abs(L * target_new - long_notional)
            turnover_lev += traded_lev
            turnover_und += traded_und
            trades.append({
                "open_date": seg_date, "close_date": date, "trigger": "margin de-risk",
                "lev_entry": lev_e, "lev_exit": lev_now,
                "und_entry": und_e, "und_exit": und_now,
                "short_pnl": r["short_pnl"], "long_pnl": r["long_pnl"],
                "total_pnl": r["net"],
                "traded_lev": traded_lev, "traded_und": traded_und,
            })
            lev_e, und_e = lev_now, und_now
            short_size, long_size = target_new, L * target_new
            short_notional, long_notional = target_new, L * target_new
            seg_date = date
            n_trades += 1
            n_derisk += 1
            target = min(target, target_new)  # ratchets DOWN only, never back up
        # Foil decay band first: its reset also re-neutralizes delta.
        elif abs(short_notional - target) > foil_decay_band * target:
            r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
            realized += r["net"]
            traded_lev = abs(target - short_notional)
            traded_und = abs(L * target - long_notional)
            turnover_lev += traded_lev
            turnover_und += traded_und
            trades.append({
                "open_date": seg_date, "close_date": date, "trigger": "foil decay band",
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
        elif abs(net_delta) > long_short_band * target:
            r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
            realized += r["net"]
            traded_und = abs(net_delta)             # long leg only
            turnover_und += traded_und
            trades.append({
                "open_date": seg_date, "close_date": date, "trigger": "long-short band",
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

        # --- 5. Accrue borrow on the POST-action short notional, then record --
        # Post-action is deliberate: a reset above already rewrote short_notional,
        # so the day is charged on the size actually carried out of it.
        borrow_paid += engine.borrow_cost(short_notional, borrow_rate_annual)
        notional_days += short_notional

        r = engine.position_pnl(lev_e, lev_now, und_e, und_now, short_size, long_size)
        equity.append(realized + r["net"] - borrow_paid)

        margin_required = pair["margin_multiplier"] * short_notional
        margin_cushion.append(equity[-1] + base_capital - margin_required)

    equity_curve = pd.Series(equity, index=dates, name="equity")
    margin_cushion_series = pd.Series(margin_cushion, index=dates, name="margin_cushion")
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
        "margin_cushion": margin_cushion_series,
        "min_margin_cushion": margin_cushion_series.min(),
        "margin_breached": bool(margin_cushion_series.min() < 0),
        "margin_breach_date": (
            margin_cushion_series[margin_cushion_series < 0].index[0]
            if margin_cushion_series.min() < 0 else None
        ),
        "stopped": stopped,
        "stop_date": stop_date,
        "n_derisk": n_derisk,
        "final_target": target,
        "lev_ohlc": lev_ohlc,
        "und_ohlc": und_ohlc,
    }
