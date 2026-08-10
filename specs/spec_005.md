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

All six gates passed. `config.PAIRS` now stores `long_rate`/`short_rate` per
pair; `margin_multiplier` is derived by `config.margin_multiplier(pair)` and
appears as a stored value nowhere in `src/`.

### Gates

| Gate | Outcome |
|---|---|
| 1 zero-delta identity | `verify_band.py` check o. TSLL short 1000 / long 2000: two-term `0.25x2000 + 0.60x1000 = 1100.00`, collapsed `1.10x1000 = 1100.00`. Identical. |
| 2 off-neutral divergence | Same check. Long +10% -> 2200: `0.25x2200 + 0.60x1000 = 1150.00`, a **+50.00** move (= `0.25 x 200`, the long leg's rate times its increase). Was +0.00 under the short-only model. |
| 3 validation vs observed | Residual table below. Within 1.25%-1.67% on all four triples, **not fitted**. |
| 4 nowhere stored | `grep -rn "margin_multiplier" src/` returns only the helper definition, calls to it, the `PositionState` field (filled by callers), and prose. No per-pair literal. |
| 5 verify scripts | `verify_engine.py`, `verify_band.py`, `verify_monitor.py` all pass. |
| 6 digest attribution | New GRAND `08baa20a...b85de942`. Restoring the old rates **and** the old short-only shape reproduces `88db6fc1...77bf88ba` byte-identically across all 65 pair-arm digests. |

### Gate 3 -- residuals against spec 004's observed triples

Predicted with `0.25 x long + 0.60 x short`, straight from the regulatory
formula. **No constant was fitted to these points.**

| long | short | ibkr_maint | predicted | residual | residual % |
|---|---|---|---|---|---|
| 9871.25 | 4642.55 | 5342.81 | 5253.34 | +89.47 | 1.67% |
| 9254.86 | 4678.20 | 5188.72 | 5120.64 | +68.09 | 1.31% |
| 9222.36 | 4683.95 | 5180.59 | 5115.96 | +64.63 | 1.25% |
| 9212.61 | 4672.45 | 5178.15 | 5106.62 | +71.53 | 1.38% |

All four residuals are the same sign and of similar magnitude -- the signature
of a small unmodelled house add-on above the regulatory minimum, not of a wrong
rate. Spec 004's fitted `a ~ 0.285, b ~ 0.545` would shrink them and was
deliberately not used: two noisy paper observations cannot support fitted
constants, and the fit's own Result flags it as untrustworthy.

### Gate 6 -- what moved, and why

**All 13 pair digests moved, not just the three single-stock pairs.** The plan
predicted only NVDL/TSLL/CONL would move and that expectation was wrong; the
difference is fully attributed rather than assumed benign:

- **The shape fix alone moves all 13.** Run with the corrected shape but the
  *old* rates, GRAND is `97c54a70...` and every pair's digest differs from
  baseline. `margin_required` feeds the recorded `margin_cushion` series for
  every pair regardless of its rates, so the shape change touches all of them.
- **The rate fix additionally moves the three single-stock pairs' sizing**
  (target 4687.50 -> 6818.18, +45.5%).
- Confirming evidence: TQQQ's `cu1.00-noclose` arm changed digest while its
  trade count stayed at exactly 125. That arm has `margin_derisk=False`, so no
  trigger could have differed -- only the recorded cushion series moved.
- De-risk counts fell across the board (TQQQ 108 -> 90, TSLL 133 -> 93 at
  cu=1.00) because the two-term form measures margin correctly off-neutral
  instead of overstating it.

Restoring both changes together reproduces the old digest exactly, so nothing
outside the margin model moved.

### Per-pair, before -> after (defaults, base_capital 10,000, cu 0.75)

`*` marks a changed multiplier.

| pair | mm | target | return % | max DD $ | n_derisk | breach-days @1.0 |
|---|---|---|---|---|---|---|
| CONL * | 1.60 -> 1.10 | 4687.50 -> 6818.18 | 13.1987 -> 19.0346 | -65.92 -> -95.89 | 3 -> 1 | 38 -> 20 |
| ERX | 1.10 | 6818.18 | 1.2198 | -179.38 | 0 | 196 -> 185 |
| FAS | 1.65 | 4545.45 | 8.7003 | -79.66 | 0 | 111 -> 102 |
| NVDL * | 1.60 -> 1.10 | 4687.50 -> 6818.18 | 5.5637 -> 8.5132 | -66.63 -> -96.92 | 1 -> 0 | 106 -> 56 |
| QLD | 1.10 | 6818.18 | 6.1109 | -53.53 | 0 | 145 -> 111 |
| SOXL | 1.65 | 4545.45 | 8.3296 -> 8.3282 | -112.90 | 1 -> 0 | 59 -> 49 |
| SSO | 1.10 | 6818.18 | 4.8557 | -120.19 | 0 | 175 -> 143 |
| TMF | 1.65 | 4545.45 | 0.4424 | -113.27 | 0 | 203 -> 200 |
| TNA | 1.65 | 4545.45 | 8.1574 | -96.18 | 0 | 105 -> 97 |
| TQQQ | 1.65 | 4545.45 | 10.2388 | -31.68 | 0 | 66 -> 63 |
| TSLL * | 1.60 -> 1.10 | 4687.50 -> 6818.18 | 10.1210 -> 14.7338 | -95.85 -> -138.88 | 3 -> 0 | 97 -> 52 |
| UDOW | 1.65 | 4545.45 | 6.4884 | -38.79 | 0 | 139 -> 140 |
| UPRO | 1.65 | 4545.45 | 7.7656 | -122.14 | 0 | 84 -> 73 |

Note SOXL: its return changed (8.3296 -> 8.3282) despite an unchanged
multiplier. It previously fired one de-risk, which shrinks the position and
costs return; under the corrected margin that de-risk no longer triggers, and
the defaults run now equals the closing-rules-off run exactly (8.328228%). The
other nine unchanged-multiplier pairs have bit-identical returns.

### Leaderboard, before -> after

| rank | before | after |
|---|---|---|
| 1 | CONL | CONL |
| 2 | TQQQ | **TSLL** |
| 3 | TSLL | **TQQQ** |
| 4 | FAS | FAS |
| 5 | SOXL | **NVDL** |
| 6 | TNA | **SOXL** |
| 7 | UPRO | **TNA** |
| 8 | UDOW | **UPRO** |
| 9 | QLD | **UDOW** |
| 10 | NVDL | **QLD** |
| 11 | SSO | SSO |
| 12 | ERX | ERX |
| 13 | TMF | TMF |

NVDL is the significant move: 10th -> 5th (+5). TSLL 3rd -> 2nd. Everything
between them shifts down one place. CONL holds 1st and the bottom three are
unchanged. The ranking did move, as spec 004 predicted it might.

### Breach-days

- **At cu = 0.75: zero for all 13 pairs, before and after.** The 45% larger
  single-stock targets did not introduce a breach.
- **At cu = 1.00: 38-203 before, 20-200 after.** Every pair breaches on fewer
  days, because the corrected model measures less margin off-neutral.

This is the input to revisiting `capital_utilization`, which was **not** touched
this session -- 0.75 was calibrated against the old numbers and retuning it in
the same session that changed them would confound the two. The headroom at 0.75
now looks larger than it did; whether 0.75 is still the right figure is the next
question.

### Deviations

- **`verify_band.py` check_a needed `margin_derisk=False`.** The fixture runs at
  `capital_utilization=1.0`, where entry cushion is zero by construction
  (STRATEGY_SPEC section 1 notes this), so equity and margin required are
  exactly equal on d0. The two-term form evaluates
  `0.25 x 1818.181818 + 0.90 x 606.060606` to `1000.0000000000001` -- one ulp
  above 1000.0 -- which tips the strict `equity < margin_required` comparison
  and fired a spurious de-risk. Fixed in the fixture, not the engine: check_a
  isolates the long-short band (it already sets `foil_decay_band=10.0` for the
  same reason) and never intended to exercise the margin rule. Verified this is
  fixture-specific: at `base_capital=10000` all 13 pairs land on exactly
  10000.0 with zero ulp gap, so `hash_band.py`'s cu=1.0 arms are unaffected.
- **New baseline written to `baseline_005_hash.txt`** rather than overwriting
  `baseline_003_hash.txt`, which spec 003's Result references by name and which
  remains the record that spec passed against.
- `check_d`/`check_f`/`verify_monitor` check_a now assert against exact
  rationals (`75000/11`, `100000/11`) rather than decimal literals -- neither
  target is representable in binary floating point.

### Note for the live monitor

Any position currently open on paper was opened at the old target (4687.50).
The startup sanity check will warn that observed short notional is far from the
derived target (6818.18) until the position is resized. That warning is correct
behaviour, not a regression.

Spec 004's caveat still stands: these are **paper** observations. IBKR's paper
margin engine may be more permissive than live. Confirm against a funded
account before resizing anything real.