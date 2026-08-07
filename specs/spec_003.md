# Spec 003 -- Extract the point-in-time decision rule

**Phase:** 1a
**Depends on:** spec 002 (all four triggers must exist before extraction)
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
    account_equity: float       # base_capital + cumulative P&L
    peak_equity: float
    margin_required: float      # multiplier * short_notional, or broker truth
    margin_multiplier: float

@dataclass(frozen=True)
class BandParams:
    long_short_band: float
    foil_decay_band: float
    capital_utilization: float
    drawdown_stop: float | None

@dataclass(frozen=True)
class Decision:
    trigger: str | None         # None | "drawdown stop" | "margin de-risk"
                                # | "foil decay band" | "long-short band"
    new_short_notional: float   # target sizes if acted on
    new_long_notional: float
    new_target: float           # ratcheted target after the action
    net_delta: float            # reported every check, tripped or not
    margin_cushion: float       # reported every check, tripped or not
```

`margin_required` is a field rather than a computed value precisely so the
monitor can pass IBKR's actual `MaintMarginReq` while the backtest passes
`margin_multiplier * short_notional`. That difference is the whole point.

## 2. Extract `evaluate()`

```python
def evaluate(state: PositionState, params: BandParams) -> Decision:
```

Pure. No dates, no bars, no accumulation, no I/O. It applies the priority order
already established in spec 002 item 3:

1. Drawdown stop -- `account_equity < peak_equity * (1 - drawdown_stop)`
2. Margin de-risk -- `margin_cushion < 0`
3. Foil decay band -- `abs(short_notional - target) > foil_decay_band * target`
4. Long-short band -- `abs(net_delta) > long_short_band * target`

At most one fires. If none fire, `trigger` is `None` and the `new_*` fields
mirror current state. `net_delta` and `margin_cushion` are always populated --
the monitor logs them on every check regardless of whether anything tripped.

Copy the conditions from `band.py` verbatim. Do not simplify, tidy, or
"improve" them while moving them. Any cleanup is a separate commit after the
regression gate is green.

## 3. Rewrite the backtest loop to call it

`run_band_backtest` keeps everything `evaluate()` does not do: iterating bars,
realizing segments through `engine.position_pnl`, accruing borrow, tracking
`peak_equity` and the ratchet, appending to the equity and cushion curves,
building the trades DataFrame.

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

1. **Regression, the whole point.** All 13 pairs at defaults produce output
   numerically identical to the pre-spec-003 baseline. Capture it first:
   `uv run python scripts/verify_band.py > baseline_003.txt`. Not "close" --
   identical. A changed number means a condition was altered in the move.
2. `scripts/verify_band.py` and `scripts/verify_engine.py` pass.
3. Direct unit tests on `evaluate()` in `scripts/verify_band.py`: one case per
   trigger plus one no-trip case, each with hand-derived expected output.
4. **Priority test.** A state satisfying more than one trigger at once returns
   the higher-priority one. At minimum: drawdown + cushion breached together
   returns `"drawdown stop"`.
5. `grep -n "long_short_band\|foil_decay_band" src/band.py` shows them only being
   passed into `BandParams`, never compared against anything.

Separate commits per numbered item, imperative lowercase with a scope prefix. Do
not commit until I have reviewed the diff.

---

## Result

*(Fill in after the session.)*