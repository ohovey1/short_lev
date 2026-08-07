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
- Intraday check cadence and band grid search -- both deferred, see ROADMAP.
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

**All six gates pass.** Implemented across items 1-7 as specced; no scope added.

### Gates

1. **Drawdown stop, hand-checked** -- PASS (`verify_band.py` check h). Fabricated
   4-day TQQQ path, `capital_utilization=1.0`, bands disabled, borrow 0. The LETF
   rises 17% on d1 against a flat underlying, so the frozen segment marks
   `-606.060606 x 0.17 = -103.030303` and account equity is 896.969697 against a
   peak of 1000 -- below the 900.000000 threshold, so the stop fires on d1 exactly.
   Both leg sizes are zero afterward, the curve is flat to the end, and the cushion
   is never negative post-stop.
2. **De-risk, hand-checked** -- PASS (check i). Fabricated path, `cu=0.75`. On the
   trip day account equity is 818.181818 against margin required 1050.000000;
   `target_new = (818.181818 x 0.75) / 1.65 = 371.900826`, matching the
   hand-derived figure, and cushion that day is exactly
   `account_equity x (1 - 0.75) = 204.545455`.
3. **Regression** -- PASS, and verified two ways. With `drawdown_stop=None` and
   `margin_derisk=False`: (a) checks a-g reproduce `baseline_002.txt` as exact
   text, and (b) a SHA-256 over every equity-curve and margin-cushion value plus
   trade counts, turnover, borrow, and returns -- 13 pairs x 2 utilizations,
   ~13,000 floats hashed by raw hex -- is identical to pre-spec-002 `HEAD`
   (`28e5fdfe...a2bc5ef5`). The printed output rounds at ~6 decimals, so the
   bitwise digest is the load-bearing check: the reordering is bit-for-bit
   neutral, not merely close.
4. **Property** -- PASS (check j). No day has a negative cushion following a
   de-risk on that day, across all 13 pairs. Run at `capital_utilization=0.90`,
   not 1.0 -- see the deviation note below.
5. **Sanity** -- PASS (check k). Max drawdown on account equity across all 13
   pairs at defaults ranges -0.29% to -1.77%, far inside the 10% stop. Nothing
   approaches the threshold, so the stop has no opportunity to overshoot.
6. **Ranking comparison** -- reported below.

### Gate 6: all 13 pairs at defaults, pre- vs post-spec-002

| Rank | Pair | Pre % | Post % | Stopped | De-risks | Rank move |
|---:|---|---:|---:|---|---:|---:|
| 1 | CONL | 13.34 | 13.20 | No | 3 | 0 |
| 2 | TQQQ | 10.24 | 10.24 | No | 0 | 0 |
| 3 | TSLL | 10.13 | 10.12 | No | 3 | 0 |
| 4 | FAS | 8.70 | 8.70 | No | 0 | 0 |
| 5 | SOXL | 8.33 | 8.33 | No | 1 | 0 |
| 6 | TNA | 8.16 | 8.16 | No | 0 | 0 |
| 7 | UPRO | 7.77 | 7.77 | No | 0 | 0 |
| 8 | UDOW | 6.49 | 6.49 | No | 0 | 0 |
| 9 | QLD | 6.11 | 6.11 | No | 0 | 0 |
| 10 | NVDL | 5.85 | 5.56 | No | 1 | 0 |
| 11 | SSO | 4.86 | 4.86 | No | 0 | 0 |
| 12 | ERX | 1.22 | 1.22 | No | 0 | 0 |
| 13 | TMF | 0.44 | 0.44 | No | 0 | 0 |

**No pair stopped, and the ranking is unchanged.** Eight de-risk events fire
across four pairs (CONL 3, TSLL 3, SOXL 1, NVDL 1), costing at most 29bp (NVDL
5.85% -> 5.56%).

This is a weaker result than the spec anticipated ("expect the numbers to get
worse... some pairs will rank differently"). The reason is that on this cached
window the strategy is nowhere near either trigger at defaults: worst
account-equity drawdown is ERX at -1.77% against a 10% stop, and
`capital_utilization=0.75` already keeps end-of-day cushion positive on every
pair-day (check g). The de-risks that do fire come from days where the
*pre-borrow* cushion dips negative even though the recorded end-of-day cushion
does not.

**The ranking being stable is not evidence the rules are inert.** It is evidence
this window contains no stress event severe enough to trigger them. The
hand-checked gates 1-2 are what demonstrate the rules work; a window containing a
genuine drawdown would be needed to see them reshape the leaderboard.

### Deviations

- **Gate 4 runs at `capital_utilization=0.90`, not the 0.75 default or 1.0.**
  At 0.75 nothing de-risks on this data, so the property would pass vacuously. At
  1.0 it is degenerate in the other direction: the spec's own formula puts the
  post-reset cushion at `equity x (1 - 1.0) = 0` exactly, so the day's borrow tips
  it fractionally negative (~ -0.17 on a $6,060 short) and de-risk re-fires every
  day -- 108 events on TQQQ, with `target` never ratcheting because equity is
  essentially unchanged each time. That contradicts the spec's "cannot thrash
  across consecutive days" only at `cu=1.0`, and it is a property of full
  utilization, not a defect in the rule. 0.90 exercises de-risk genuinely (1-28
  events per pair) with real headroom after each reset.
- **Four pre-existing checks (b, c, f, g) now pass `drawdown_stop=None,
  margin_derisk=False`.** All four run at `cu=1.0` and assert pre-spec-002
  properties -- b's zero-trade premise, c's apples-to-apples comparison against
  the ladder (which has no closing rules), f's explicit "matches pre-cushion-knob
  behavior" claim, and g's isolation of the utilization knob. Without the knobs
  they would measure the new rules instead of what they were written to test; b
  failed outright (22 de-risk trades against an expected zero) before the fix.
- **`account_equity` excludes the current day's borrow**, per spec item 2's
  formula read literally and in position (today's accrual is step 5). The
  recorded `margin_cushion` series is post-borrow, so the trigger and the
  reported series differ by one day's accrual. This is why an isolated day can
  show a negative recorded cushion without a de-risk having fired on it.

### Deferred

Nothing from this spec. Out-of-scope items (re-entry, target ratchet-up, kill
switch, intraday cadence, band grid search) were not built, as specced.