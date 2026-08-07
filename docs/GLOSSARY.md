# Glossary

| Term | Meaning |
|---|---|
| **target** | Fixed dollar size the strategy holds the short leg at. Set once, everything else is measured against it. `target = (base_capital × capital_utilization) ÷ margin_multiplier`. |
| **short notional** | Current mark-to-market value of the short leg (`short_size × price_now/price_entry`). Moves with price in either direction. |
| **net delta** | `long_notional − leverage × short_notional`. The imbalance between the two legs — how far off-hedge the position currently is. |
| **foil_decay_band** | % threshold on short notional vs. `target`. Trip it → reset both legs back to `target`. |
| **long_short_band** | % threshold on net delta vs. `target`. Trip it → resize the long leg only; short stays put. |
| **leverage (L)** | The LETF's stated multiple (e.g. 2 for TSLL). Long leg = `L × target` when hedged. |
| **base_capital** | Cash figure fed into the backtest. Denominates `pct_return`; divided by `margin_multiplier` to get `target`. |
| **margin multiplier** | How much cash a given short notional actually requires (≈1.6x for TSLA/TSLL, pair-dependent). Lives in `config.PAIRS[...]["margin_multiplier"]`. |
| **borrow cost** | Daily charge on short notional at the pair's borrow rate — the real cost that eats into decay P&L. |
| **breakeven borrow rate** | The annualized borrow rate at which gross decay P&L would be fully consumed by borrow. Used to rank pairs. |
| **margin required** | `margin_multiplier × short_notional` — the cash the short leg's *current* size actually requires, day by day (not just at entry). |
| **margin cushion** | `equity − margin_required`. Negative means a real account backing this position would be under a margin call at that size. A live trigger, not an observation: a negative cushion fires the margin de-risk rule, which resets both legs to a target recomputed from current equity. |
| **drawdown stop** | Terminal exit: when account equity falls `drawdown_stop` (default 0.10) below its running peak, both legs close and the run stays flat for the rest of the window. No re-entry. |
| **margin de-risk** | Partial reset fired by a negative margin cushion. `target` is recomputed from current equity and ratchets **down only** — it never grows back as equity recovers. |
| **capital_utilization** | Fraction of `base_capital` committed as margin (default 0.75). `1 − capital_utilization` is deliberate slack kept as headroom against margin cushion going negative — reduces breach risk, does not guarantee zero breaches. |