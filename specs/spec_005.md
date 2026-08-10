# Spec 005 -- Margin model: fix the rate and the shape

**Phase:** 1b.5
**Depends on:** spec 004 (shipped -- live margin observations exist)
**Estimated:** one session

## Why

Spec 004 measured IBKR's actual maintenance margin against our model and found
two separate errors.

**Rate.** `margin_multiplier` for a 2x single-stock pair was built as
`0.50 x leverage + 0.30 x leverage = 1.60`. The 0.50 is the *initial* requirement
on the single-stock long leg; IBKR maintains long equity at 25%. Correcting it
gives `0.25 x 2 + 0.60 = 1.10`, against an observed ~1.11.

**Shape.** `margin_multiplier x short_notional` is that formula evaluated at zero
net delta -- it assumes `long = leverage x short`. True at entry, false as soon
as delta drifts. Observed ratio moved 0.691 -> 0.719 with the short leg unchanged
and only the long leg altered.

Sizing depends on this: `target = (base_capital x capital_utilization) /
margin_multiplier`. A multiplier 45% too high undersizes every position by ~31%,
across all 13 pairs, and the leaderboard ranking may move since multipliers
differ per pair.

## Out of scope (do not build)

- **Changing `capital_utilization`.** 0.75 was calibrated against the *old*
  margin numbers. Whether it still holds is a question for after this lands, with
  fresh breach counts. Do not retune it in the same session that changes the
  thing it was calibrated against.
- **Portfolio Margin.** Reg T only. PM is a separate model above $110k NLV.
- **Live confirmation.** The observations are from paper. This spec corrects the
  model to match the regulatory formula and validates against paper data; it does
  not claim live equivalence.
- Telegram, systemd, executor, grid search.
- Any change to `decision.py`'s trigger conditions.

---

## 1. Correct the maintenance rates

Rebuild `margin_multiplier` from **maintenance** requirements throughout. Current
values and their corrections:

| Pair type | Current | Correct | Reason |
|---|---|---|---|
| 3x broad/sector ETF | `0.25 x 3 + 0.90 = 1.65` | unchanged | long already at 25% maintenance |
| 2x broad/sector ETF | `0.25 x 2 + 0.60 = 1.10` | unchanged | same |
| 2x single stock | `0.50 x 2 + 0.60 = 1.60` | `0.25 x 2 + 0.60 = 1.10` | 0.50 was initial, not maintenance |

Only the single-stock pairs change. Document the derivation as a comment in
`config.py` so the initial-vs-maintenance distinction cannot be lost again.

**Note:** IBKR applies house requirements above the regulatory minimum on
volatile names, and these can be raised without notice. Treat the corrected
figures as estimates too -- better ones, but still estimates.

## 2. Fix the shape

Margin is a function of both legs:

```
margin_required = long_rate * long_notional + short_rate * short_notional
```

Where per-pair `long_rate` and `short_rate` replace the single
`margin_multiplier`. For a 2x single-stock pair: `long_rate = 0.25`,
`short_rate = 0.60`.

`decision.PositionState` already carries `margin_required` as a field precisely
so the monitor can pass broker truth. This spec changes what the **backtest**
puts there -- currently `margin_multiplier * short_notional`, now the two-term
form.

**Target derivation must stay single-valued.** At entry `long = leverage x
short`, so:

```
target = (base_capital * capital_utilization) / (long_rate * leverage + short_rate)
```

The denominator is exactly the old `margin_multiplier`, which is why the old
model looked right at entry. Keep `margin_multiplier` as a **derived** property
for sizing, computed from the two rates rather than stored independently -- one
source of truth, no drift between them.

## 3. Config migration

`config.PAIRS` currently stores `margin_multiplier` per pair. Replace with
`long_rate` and `short_rate`. Derive `margin_multiplier` where sizing needs it.

Every pair must be revisited, not just the single-stock ones -- the stored value
must not survive anywhere as an independent number.

## 4. Re-run and report

- All 13 pairs at defaults, before and after.
- Report per pair: target, return, max drawdown, margin breach-days, stopped,
  n_derisk, breakeven borrow.
- Report the leaderboard ranking before and after.
- Report breach-days at `capital_utilization` 0.75 and 1.00. The 0.75 figure was
  chosen because 1.00 breached on 38-203 days per pair under the old model; that
  calibration is now suspect and the new numbers are the input to revisiting it.

---

## Session gate

1. **Regression on the shape, at zero delta.** With a hand-constructed state
   where `long = leverage x short` exactly, the two-term formula must return the
   same `margin_required` as `margin_multiplier x short_notional` did. This is
   the algebraic identity that makes the old model a special case; if it fails,
   the rates are wrong.
2. **Divergence off-neutral.** With `long` perturbed +10% and the short leg
   unchanged, `margin_required` must move. Under the old model it would not. Sign
   and rough magnitude checked by hand.
3. **Validation against observed data.** Spec 004 recorded four
   (long, short, ibkr_maint) triples. The corrected formula must land within a
   few percent of each. **Report the residuals; do not fit to them** -- the rates
   come from the regulatory formula, and the observations are a check on that
   derivation, not a training set. Two noisy points cannot support fitted
   constants.
4. **`margin_multiplier` is nowhere stored.** `grep -rn "margin_multiplier" src/`
   shows only derivation from the two rates, never a literal per-pair value.
5. `scripts/verify_band.py`, `verify_engine.py`, `verify_monitor.py` pass.
6. **Backtest output changes, and the digest proves it changed deliberately.**
   `hash_band.py` will differ -- that is expected here, unlike specs 003 and 004.
   Record the new digest and confirm the *only* reason it moved is target sizing,
   by re-running with the old rates restored and matching the old digest exactly.

Gate 6 is the one that matters. It separates "sizing changed" from "something
else broke."

Separate commits per numbered item, imperative lowercase with a scope prefix. Do
not commit until I have reviewed the diff.

---

## Result

*(Fill in after the session. Include the before/after leaderboard ranking, the
residuals against spec 004's observed triples, and the new breach-day counts at
0.75 and 1.00 utilisation.)*