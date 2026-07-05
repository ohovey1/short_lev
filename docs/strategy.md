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

**When trades open and close (this demo):** a new tranche opens every trading day at
that day's closing prices, and closes `hold_days` days later at that day's close --
fixed timing, not signal-driven. The point is to study the decay over a steady
schedule, so there are no entry/exit rules beyond the calendar.

## Multi-day holds
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
- **Long vs short P/L:** each leg's running P/L. They move in opposite directions
  (the hedge working). The **gap between them is the edge** -- that gap is what
  becomes total return.
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
- **Trades table / Trade P/L:** each closed tranche, with both legs' P/L and the
  net. Most trades are small wins; the green/red bars show the spread.

## Important caveat
**Borrow cost is included; other fees are not.** The short leg is charged daily
borrow at IBKR's live indicative rate (or a config fallback when the fetch fails)
-- indicative, not a firm quote. Expense ratios, spreads, and dividends are still
omitted, so these results remain **optimistic**. Treat this as a study of the decay
effect, not a verdict on whether the strategy is profitable after costs.
