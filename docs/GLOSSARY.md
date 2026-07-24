# Glossary

| Term | Meaning |
|---|---|
| **target** | Fixed dollar size the strategy holds the short leg at. Set once, everything else is measured against it. `target = (base_capital × capital_utilization) ÷ margin_multiplier`. |
| **short notional** | Current mark-to-market value of the short leg (`short_size × price_now/price_entry`). Moves with price in either direction. |
| **net delta** | `long_notional − leverage × short_notional`. The imbalance between the two legs — how far off-hedge the position currently is. |
| **short_band** | % threshold on short notional vs. `target`. Trip it → reset both legs back to `target`. |
| **delta_band** | % threshold on net delta vs. `target`. Trip it → resize the long leg only; short stays put. |
| **leverage (L)** | The LETF's stated multiple (e.g. 2 for TSLL). Long leg = `L × target` when hedged. |
| **base_capital** | Cash figure fed into the backtest. Denominates `pct_return`; divided by `margin_multiplier` to get `target`. |
| **margin multiplier** | How much cash a given short notional actually requires (≈1.6x for TSLA/TSLL, pair-dependent). Lives in `config.PAIRS[...]["margin_multiplier"]`. |
| **borrow cost** | Daily charge on short notional at the pair's borrow rate — the real cost that eats into decay P&L. |
| **breakeven borrow rate** | The annualized borrow rate at which gross decay P&L would be fully consumed by borrow. Used to rank pairs. |
| **margin required** | `margin_multiplier × short_notional` — the cash the short leg's *current* size actually requires, day by day (not just at entry). |
| **margin cushion** | `equity − margin_required`. Negative means a real account backing this position would be under a margin call at that size — observation only, does not change when trades fire. |
| **capital_utilization** | Fraction of `base_capital` committed as margin (default 0.75). `1 − capital_utilization` is deliberate slack kept as headroom against margin cushion going negative — reduces breach risk, does not guarantee zero breaches. |