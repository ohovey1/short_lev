# Spec 002 -- Closing logic: drawdown stop and margin de-risk

**Phase:** 0.5a
**Depends on:** spec 001 (band renames must be settled)
**Estimated:** one session, largest item first

## Why

`docs/STRATEGY_SPEC.md` section 1 defines three exit paths. `band.py` implements
none of them, and its docstring says the opposite -- margin cushion is tracked as
"observation only" that "does not feed back into the trigger logic."

Consequence: every number currently produced describes a strategy that never
de-risks and never stops out. That is not the strategy in the spec, and the
leaderboard ranking derived from it is not trustworthy. This spec closes that gap.

Expect the numbers to get worse. A stop that fires converts an unrealized dip
into a realized loss and forfeits the recovery. Some pairs will rank differently
afterward. That is the point.

## Out of scope (do not build)

- **Re-entry after a stop.** Once flat, the run ends. Any re-entry rule is a new
  strategy parameter needing its own justification.
- **Target ratchet-up.** Target shrinks under stress and never grows back.
- **Kill switch.** It is a human decision with no automated trigger
  (STRATEGY_SPEC section 1). There is nothing to backtest. Do not invent one.
- Intraday check cadence (spec 003), grid search (spec 004), notifications,
  broker code.
- Any change to `engine.py`. All new P&L still routes through it.

---

## 1. Make `target` mutable

Currently `target` is computed once and never changes. The de-risk rule requires
it to ratchet downward, so it becomes loop state.

- Initialize as today: `target = (base_capital * capital_utilization) / margin_multiplier`.
- After a de-risk event: `target = min(target, target_new)`. Never increases.
- The foil decay band measures against the **current** target, so a ratcheted-down
  target also tightens the band in absolute dollars. This is intended.

## 2. Define account equity explicitly

`equity_curve` is cumulative P&L, 0-based. Account equity is
`base_capital + cumulative P&L`, which is already what the margin cushion line
uses. Both new rules operate on **account equity**, not the 0-based curve --
a 10% drawdown on a near-zero cumulative series would be meaningless.

Add a local `account_equity = base_capital + realized + mark - borrow_paid`,
computed at the top of each day **before** any action, and use it for both checks.
Peeking does not mutate state, so this reordering must not change any existing
number (see gate 3).

## 3. Per-day ordering

Restructure the loop body to this exact sequence. Ordering is load-bearing:

1. Mark the position at today's prices; compute `short_notional`,
   `long_notional`, `net_delta`, `account_equity`.
2. **Drawdown stop** (terminal -- checked first).
3. **Margin de-risk** (urgency: under-margined beats off-hedge; its reset also
   re-neutralizes delta, so the band checks below are moot on a de-risk day).
4. **Foil decay band**, else **long-short band** (unchanged from today).
5. Accrue borrow on the post-action short notional; append equity and cushion.

At most one of steps 2-4 fires on a given day.

## 4. Drawdown stop

- Track `peak_equity`, initialized to `base_capital` (not zero).
- Update: `peak_equity = max(peak_equity, account_equity)`.
- Trip when `account_equity < peak_equity * (1 - drawdown_stop)`.
- On trip: realize the open segment via `engine.position_pnl`, set both leg sizes
  to zero, record a trade row with trigger `"drawdown stop"`, and mark the run
  stopped.
- After a stop: no marks, no trades, no borrow accrual (there is no short leg).
  The equity curve continues as a **flat line** at its stopped value through the
  end of the window, so the chart and all downstream metrics still compute.
- Cushion after a stop is `account_equity - 0`, i.e. just equity. Never negative.

`drawdown_stop` defaults to `0.10`. Passing `None` disables it.

## 5. Margin de-risk

- Trip when `margin_cushion < 0`, equivalently
  `account_equity < margin_multiplier * short_notional`.
- On trip:
  ```
  target_new = (account_equity * capital_utilization) / margin_multiplier
  ```
  Realize the open segment, reset short to `target_new` and long to
  `L * target_new`, open a new segment at today's prices, record a trade row with
  trigger `"margin de-risk"`, and ratchet: `target = min(target, target_new)`.
- **Guard:** if `account_equity <= 0`, close fully instead of resizing. In
  practice the drawdown stop fires long before this, but do not leave it
  undefined.

**Property worth knowing (and gated below):** after the reset, cushion equals
`account_equity * (1 - capital_utilization)`, which is positive by construction.
A single de-risk always restores the cushion -- it cannot thrash across
consecutive days.

`margin_derisk` defaults to `True`. Passing `False` disables it. Both this and
`drawdown_stop=None` exist so gate 3 can reproduce pre-spec-002 output; they are
test knobs, not strategy parameters, and should say so in the docstring.

## 6. Result fields and UI

New keys in the `run_band_backtest` return dict:

- `stopped` (bool), `stop_date` (date or None)
- `n_derisk` (int), `final_target` (float)

The two new trigger strings flow through the existing trades DataFrame, so the
trades table and per-trade P&L chart need no changes.

UI, kept minimal:

- Main page metrics table: stopped status and stop date if stopped; de-risk count.
- Leaderboard (`Pair_Analysis.py`): a `stopped` column and a `n_derisk` column.
  A stopped pair's return is its return at the stop -- do not annotate or
  asterisk it, the column says enough.

## 7. Fix the stale docstring

`band.py`'s module docstring currently says the margin cushion "does not feed
back into the trigger logic." After this spec that is false. Rewrite it, and
update `docs/GLOSSARY.md` (`margin cushion` entry says "observation only") and
`docs/strategy.md` to match.

---

## Session gate

1. **Drawdown stop, hand-checked.** Fabricate a price path that drives account
   equity 10% below peak on a known day. Assert the stop fires on that exact day,
   equity matches a hand-derived value, both leg sizes are zero afterward, and
   the curve is flat to the end of the window.
2. **De-risk, hand-checked.** Fabricate a path driving cushion negative. Assert
   `target_new` matches the hand-derived figure, both legs reset to it, and
   cushion on that day is exactly `account_equity * (1 - capital_utilization)`.
3. **Regression.** With `drawdown_stop=None` and `margin_derisk=False`, output
   must be numerically identical to the pre-spec-002 baseline for at least three
   pairs. This proves the reordering in item 3 is behavior-neutral.
4. **Property.** Across all 13 pairs at defaults, no day has negative cushion
   following a de-risk event on that day.
5. **Sanity.** With the stop active, max drawdown on account equity never
   materially exceeds `drawdown_stop`. A large overshoot means the check is
   running after the action instead of before it.
6. All 13 pairs re-run at defaults; report which stopped, when, and how the
   leaderboard ranking changed versus the pre-spec-002 ordering.

Gates 1-2 go in `scripts/verify_band.py` alongside the existing checks. Gate 6 is
a reported result, not an assertion -- put it in the Result section below.

Separate commits per numbered item, imperative lowercase with a scope prefix. Do
not commit until I have reviewed the diff.

---

## Result

*(Fill in after the session: gate passed or not, what deviated, what deferred.
Gate 6's ranking comparison goes here.)*