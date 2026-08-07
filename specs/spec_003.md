# Spec 003 -- Extract the point-in-time decision rule

**Phase:** 1a
**Depends on:** spec 002 (shipped)
**Estimated:** half a session

## Why

The monitor needs to answer "should this position be rebalanced right now."
`band.py` already answers that -- but the answer is tangled inside a loop over
daily bars, so the monitor cannot call it.

If the monitor reimplements the trip conditions, there are two definitions of a
band breach that will drift apart. That is the exact failure mode `CLAUDE.md`
hard-constrains against after `backtest.py` diverged from `engine.py`. One
definition, called from two places.

This is a **behavior-neutral refactor**. No new logic, no new triggers, no
change to any number.

## Out of scope (do not build)

- IBKR connection, monitor loop, state file, Telegram. That is spec 004.
- Any change to the trigger conditions themselves. If a threshold moves, that is
  a bug in this session.
- **The borrow-timing seam.** `account_equity` excludes the current day's borrow
  while the recorded `margin_cushion` is post-borrow (see spec 002's Result).
  Preserve this exactly. It is a known open issue with its own spec coming; do
  not fix it here.
- Any change to `engine.py`.
- A monitor/executor class hierarchy. There is one consumer today. A function is
  enough.

---

## 1. Define the decision types

In `src/decision.py` (new, pure, no I/O, no imports from `band.py`):

```python
@dataclass(frozen=True)
class PositionState:
    short_notional: float       # current mark value of the short leg
    long_notional: float        # current mark value of the long leg
    leverage: float
    target: float               # current (possibly ratcheted) target
    account_equity: float       # base_capital + realized + mark - borrow_paid
    peak_equity: float
    margin_required: float      # backtest passes margin_multiplier * short_notional;
                                # the monitor will pass IBKR's MaintMarginReq
    margin_multiplier: float    # still needed for target_new

@dataclass(frozen=True)
class BandParams:
    long_short_band: float
    foil_decay_band: float
    capital_utilization: float
    drawdown_stop: float | None
    margin_derisk: bool

@dataclass(frozen=True)
class Decision:
    trigger: str | None         # None | "drawdown stop" | "margin de-risk"
                                # | "foil decay band" | "long-short band"
    terminal: bool              # True only for the drawdown stop -- the run ends
    new_short_notional: float   # target sizes if acted on
    new_long_notional: float
    new_target: float           # ratcheted target after the action
    net_delta: float            # reported every check, tripped or not
    margin_cushion: float       # reported every check, tripped or not
```

`margin_required` is a field rather than a computed value precisely so the
monitor can pass IBKR's actual `MaintMarginReq` while the backtest passes its
`margin_multiplier * short_notional` estimate. That difference is the whole point.

`terminal` exists so the caller does not have to infer "the run is over" from a
trigger string plus zero sizes.

## 2. Extract `evaluate()`

```python
def evaluate(state: PositionState, params: BandParams) -> Decision:
```

Pure. No dates, no bars, no accumulation, no I/O. It applies the priority order
shipped in spec 002:

1. Drawdown stop -- `drawdown_stop is not None` and
   `account_equity < peak_equity * (1 - drawdown_stop)`. Sets `terminal=True`.
2. Margin de-risk -- `margin_derisk` and `account_equity < margin_required`.
   Includes the `account_equity <= 0` full-close guard.
3. Foil decay band -- `abs(short_notional - target) > foil_decay_band * target`
4. Long-short band -- `abs(net_delta) > long_short_band * target`

At most one fires. If none fire, `trigger` is `None`, `terminal` is `False`, and
the `new_*` fields mirror current state. `net_delta` and `margin_cushion` are
always populated -- the monitor logs them every check regardless of whether
anything tripped.

Copy the conditions from `band.py` verbatim. Do not simplify, tidy, or
"improve" them while moving them. Any cleanup is a separate commit after the
regression gate is green.

## 3. Rewrite the backtest loop to call it

`run_band_backtest` keeps everything `evaluate()` does not do: iterating bars,
realizing segments through `engine.position_pnl`, accruing borrow, tracking
`peak_equity` and the ratchet, carrying the flat post-stop curve, appending to
the equity and cushion series, building the trades DataFrame.

Per bar: build a `PositionState`, call `evaluate()`, act on the returned
`Decision`. The loop no longer contains a single `if ... > ... * target`
comparison.

## 4. Docstrings

`band.py`'s module docstring should say the trip conditions now live in
`decision.py` and that it owns iteration and accounting only. Add the same
layering note to `CLAUDE.md`'s file map: `decision.py` is pure and sits beside
`engine.py`, and nothing may reimplement its conditions.

---

## Session gate

1. **Regression, the whole point.** All 13 pairs produce output identical to the
   pre-spec-003 baseline. Capture it first:
   `uv run python scripts/verify_band.py > baseline_003.txt`. Use the same
   bitwise-digest approach as spec 002's gate 3 -- hash the raw float values, not
   the rounded printed output. Text comparison alone is not sufficient.
2. `scripts/verify_band.py` and `scripts/verify_engine.py` pass.
3. Direct unit tests on `evaluate()` in `scripts/verify_band.py`: one case per
   trigger plus one no-trip case, each with hand-derived expected output.
4. **Priority test.** A state satisfying more than one trigger returns the
   higher-priority one. At minimum: drawdown and cushion breached together
   returns `"drawdown stop"` with `terminal=True`.
5. **Knobs.** With `drawdown_stop=None` and `margin_derisk=False`, `evaluate()`
   never returns those two triggers even on states that would otherwise fire
   them, and still returns band triggers correctly.
6. `grep -n "long_short_band\|foil_decay_band" src/band.py` shows them only being
   passed into `BandParams`, never compared against anything.

Separate commits per numbered item, imperative lowercase with a scope prefix. Do
not commit until I have reviewed the diff.

---

## Result

**All six gates pass.** Implemented as specced across items 1-4; no scope added,
no trigger condition altered.

### Gates

1. **Regression, bitwise** -- PASS. `scripts/hash_band.py` (new) digests
   `float.hex()` of every raw float the backtest produces -- every equity-curve
   and margin-cushion value, ten scalar metrics, and **every trade row**
   individually (trigger, both traded amounts, per-leg P&L, entry/exit prices) --
   across 5 parameter arms x 13 pairs. The grand digest is identical pre- and
   post-refactor: `88db6fc1...77bf88ba`. All 65 pair-arm digests match; the
   `diff` is empty. Trade rows are hashed per-row on purpose: turnover is a sum
   and could net an error out, but a wrong `traded_lev` on a single long-short
   row moves the digest immediately.
2. **`verify_band.py` and `verify_engine.py`** -- PASS, and both reproduce their
   pre-refactor output as an exact text diff. `engine.py` was not touched.
3. **Unit tests on `evaluate()`** -- PASS (check l). One case per trigger plus a
   no-trip case, called directly with hand-built states and no backtest.
   Expected values reuse the arithmetic already hand-derived in checks h, i, and
   a, so the two layers are anchored independently: de-risk lands on
   `target_new = 371.900826`, long-short on `net_delta = 90.909091`. The no-trip
   case asserts `net_delta` and `margin_cushion` are populated anyway.
4. **Priority** -- PASS (check m). Drawdown + cushion breached together returns
   `"drawdown stop"` with `terminal=True`. The two lower boundaries are pinned
   too: cushion + foil returns de-risk, foil + long-short returns foil decay.
5. **Knobs** -- PASS (check n). With `drawdown_stop=None, margin_derisk=False`, a
   state breaching everything surfaces a band and never a closing rule, and a
   state breaching *only* the closing rules returns `None`. The same state with
   the knobs on returns `"drawdown stop"`, proving the knobs did the suppressing
   rather than the state being inert.
6. **grep** -- PASS. `grep -n "long_short_band\|foil_decay_band" src/band.py`:

   ```
   7:  - |short notional - target| > foil_decay_band * target
   9:  - else |net delta| > long_short_band * target -> re-neutralize via the LONG leg only
   40:def run_band_backtest(pair_key, base_capital, long_short_band=0.10, foil_decay_band=0.10,
   171:                long_short_band=long_short_band,
   172:                foil_decay_band=foil_decay_band,
   ```

   Docstring, signature defaults, and the two `BandParams` keyword lines. Zero
   comparisons.

### How the loop reconstructs each branch

`Decision` carries seven fields, but the four branches did very different side-effect
work. Everything proved derivable with no extra field:

- `traded_lev`/`traded_und` are `abs(new - old)` on each leg, uniformly for all
  four triggers. This is bit-exact, not approximate: `abs(SN - SN)` is exactly
  `+0.0` (recovering the long-short literal), `abs(0.0 - SN) == SN` for the stop
  (both notionals are provably non-negative), and `abs(L*SN - LN)` vs band.py's
  `abs(net_delta) = abs(LN - L*SN)` are exact IEEE-754 negations. Verified
  empirically over 600k random trials before relying on it, then confirmed by the
  digest.
- Entry prices advance on `if not d.terminal` -- a stop opens no new segment.
- The ratchet is an unconditional `target = d.new_target`; `evaluate()` does the
  `min` internally and returns `state.target` unchanged on every other path.

The loop's only remaining non-numeric reads are `d.trigger is not None`,
`d.terminal`, and `d.trigger == "margin de-risk"` (for `n_derisk`). No trigger
condition is re-derived.

### Deviations

- **`hash_band.py` runs 5 arms, not just the defaults.** At every default-ish
  setting **no pair stops** on this cached window (confirmed by running them), so
  a digest built only from default arms cannot see the terminal branch at all.
  The added `cu=0.75, drawdown_stop=0.005` arm stops 11 of 13 pairs and makes two
  real traps digest-visible: returning `0.0` for `new_target` on a stop (
  `final_target` is nonzero on stopped pairs -- 4545.454545 on UPRO/TMF/SOXL,
  4687.50 on CONL) and wrongly advancing the segment entry prices. This is
  strictly more coverage than the gate asked for; it can only catch a regression,
  never manufacture a false failure, since it is compared pre- vs post-refactor
  against a baseline captured on clean HEAD.
- **`scripts/hash_band.py` is committed**, unlike spec 002's ad-hoc digest which
  was never checked in and had to be rewritten from scratch here. Spec 004 and any
  later refactor of this loop can now re-run the same gate.

### Preserved, not fixed

- **The borrow-timing seam.** `account_equity` still excludes the current day's
  borrow while the recorded `margin_cushion` series is post-borrow.
  `Decision.margin_cushion` is `account_equity - margin_required` -- a pre-borrow,
  pre-action report field for the monitor -- and deliberately does **not** feed
  `band.py`'s recorded series, which still computes from the loop's own
  post-action `short_notional`. Wiring one into the other would have changed
  output every single day. Documented in `decision.evaluate()`'s docstring, and
  still awaiting its own spec.

### Deferred

Nothing from this spec. Out-of-scope items (IBKR connection, monitor loop, state
file, Telegram, any trigger-condition change, any `engine.py` change, a
monitor/executor class hierarchy) were not built, as specced.