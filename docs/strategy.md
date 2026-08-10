# Trade Strategy

## The idea
Leveraged ETFs (like TQQQ, a 3x fund on QQQ) reset their leverage every day.
Over time that daily reset causes **volatility decay**: on choppy, back-and-forth
days the leveraged fund loses a little more than 3x the underlying's net move. This
backtest tries to harvest that decay while staying hedged to market direction.

## The position
Each tranche is two legs, sized so they offset each other's market exposure:

- **Short** a fixed dollar amount of the leveraged ETF (TQQQ).
- **Long** 3x that amount of the underlying (QQQ).

Shorting a +3x fund gives -3x exposure to QQQ; the long QQQ leg adds +3x back. The
two cancel, so the position is roughly **delta-neutral** -- it doesn't make or lose
much from QQQ simply going up or down. What's left over is the decay.

The backtest page offers **two ways to trade this position**, selectable in the
sidebar. **Band rebalancing** (the default) holds one continuous position and only
trades when it drifts past a threshold -- this is how a live bot would trade.
The **tranche ladder** opens and closes positions on a fixed calendar -- it is the
reference implementation the band strategy is checked against.

## Band rebalancing (default)

Hold **one continuous position**: short a fixed `target` notional of the leveraged
ETF, long `leverage` times that of the underlying. `target` is not simply base
capital -- it's `(base_capital x capital_utilization) / margin_multiplier`. Between
trades nothing is touched -- the hedge is frozen and the position simply marks to
market. A trade fires only when the position drifts past a band:

- **Foil Decay Band:** if the short leg's notional drifts more than `foil_decay_band`
  (e.g. 10%) of target away from target, reset the short back to target and
  re-neutralize both legs.
- **Long-Short Band:** otherwise, if the net delta (long notional minus leverage
  times short notional) exceeds `long_short_band` of target, re-neutralize using
  the **long leg only** -- the short keeps running at its current value.

No fixed schedule, few trades, each one only when the hedge has genuinely drifted.
Borrow accrues daily on whatever the short notional currently is. The band widths
are the two sidebar sliders: wider bands mean fewer trades (less turnover) but a
sloppier hedge.

**Two closing rules run ahead of the bands**, both measured on account equity
(base capital plus cumulative P&L), checked before any action that day:

- **Drawdown stop:** if account equity falls 10% below its running peak, both legs
  close and the run stays flat to the end of the window. Terminal -- there is no
  re-entry.
- **Margin de-risk:** if the margin cushion goes negative, both legs reset to a
  target recomputed from current equity. A partial reset, not a close, and `target`
  ratchets **down only** -- it never grows back as equity recovers.

At most one of these -- or one band -- fires on any given day.

**Margin multiplier and capital utilization.** Shorting a leveraged ETF and holding
the offsetting long both require margin collateral, and the two legs aren't netted
below Portfolio Margin thresholds. Margin required is a function of both legs,
`long_rate * long_notional + short_rate * short_notional`, with per-leg maintenance
rates set in the pair registry (not a sidebar control). `margin_multiplier` is that
formula collapsed at zero net delta -- how much cash a dollar of short notional ties
up while the position is neutral -- and is derived from the two rates rather than
stored, which is what the target formula above divides by.
`capital_utilization` (a sidebar slider, default 0.75) is
the fraction of base capital actually deployed as margin; the rest is deliberate
slack, kept as headroom against a margin call as the position drifts. Lower
utilization means a smaller position and more cushion; 1.0 uses all of base capital
with no cushion -- the "Min margin cushion" metric below shows how close the run came
to breaching. At 1.0 the de-risk rule resets to a cushion of exactly zero, so the
day's borrow tips it straight back under and it re-fires daily; that setting is a
backward-compatibility knob, not a sensible live configuration.

Full mechanics, the closing rules, and a worked numeric example:
[`docs/STRATEGY_SPEC.md`](./STRATEGY_SPEC.md).

## The tranche ladder (reference)

**When trades open and close:** a new tranche opens every trading day at
that day's closing prices, and closes `hold_days` days later at that day's close --
fixed timing, not signal-driven. The point is to study the decay over a steady
schedule, so there are no entry/exit rules beyond the calendar.

Instead of resetting every day, we **open one tranche per day and hold it for a set
number of days** (`hold_days`). On a normal day, `hold_days` tranches are open at
once, at staggered ages. This ladder of overlapping holds is what actually captures
multi-day decay -- a single-day version only scrapes one day's cost drag.

To keep results comparable across different `hold_days`, total deployed capital is
held constant: each tranche gets `base_capital / hold_days`.

## Reading the charts and numbers

- **Price charts (QQQ, TQQQ):** the raw price action over your selected window.
  Toggle line/candlestick in the sidebar. TQQQ should move the same direction as
  QQQ but about 3x as much -- that extra volatility is the source of the decay.
- **Long vs short P/L** (ladder only): each leg's running P/L. They move in
  opposite directions (the hedge working). The **gap between them is the edge** --
  that gap is what becomes total return.
- **Equity curve:** starting capital plus cumulative P/L over time. A gentle,
  mostly-upward line is the decay being collected day by day.
- **Metrics:**
  - *Total return* -- dollars made over the window.
  - *Return %* -- that return as a fraction of starting capital.
  - *Max drawdown* -- the worst peak-to-trough dip in the equity curve.
  - *Worst day* -- the single largest one-day loss.
  - *Borrow paid* -- total borrow cost charged on the short leg over the window.
  - *Borrow rate (annual)* -- the rate used for that charge: IBKR's live
    indicative rate for the shorted fund when available, else a config fallback.
  - *Trades / Total turnover* (band) -- how many band-triggered rebalances fired,
    and the gross dollars traded across both legs doing so. Fewer trades and less
    turnover mean lower real-world costs (spreads, commissions) for the same edge.
  - *Breakeven borrow* (band) -- the annualized borrow rate at which the gross
    edge would be fully consumed by borrow cost.
- **Trades table / Trade P/L:** on the ladder, each closed tranche with both legs'
  P/L and the net. On the band strategy, each band-triggered rebalance: which band
  fired, the closed segment's per-leg P/L, and the dollars traded to re-neutralize
  (the final still-open segment is not listed). Most trades are small wins; the
  green/red bars show the spread.

## Important caveat
**Borrow cost is included; other fees are not.** The short leg is charged daily
borrow at IBKR's live indicative rate (or a config fallback when the fetch fails)
-- indicative, not a firm quote. Expense ratios, spreads, and dividends are still
omitted, so these results remain **optimistic**. Treat this as a study of the decay
effect, not a verdict on whether the strategy is profitable after costs.
