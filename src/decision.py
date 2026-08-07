"""Layer 2 (sibling): the point-in-time rebalance decision.

Given one snapshot of a position -- current marks, target, equity, margin --
decide whether to act and what the new leg sizes should be. Pure: no I/O, no
global state, no dates, no bars, no accumulation. The caller holds the history
and does the accounting.

One definition, two callers: band.py's backtest loop today, the live monitor
next. Nothing may reimplement these conditions -- that is the failure mode
CLAUDE.md hard-constrains against. If a threshold moves, it moves here.

Priority is load-bearing and is the order band.py shipped: drawdown stop
(terminal) beats margin de-risk (whose reset also re-neutralizes delta, making
the bands moot that day), which beats the foil decay band, which beats the
long-short band. At most one fires.

margin_required is a field on PositionState rather than something computed
here, precisely so the monitor can pass IBKR's actual MaintMarginReq while the
backtest passes its margin_multiplier * short_notional estimate. That
difference is the whole point of the seam.
"""

from dataclasses import dataclass


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


def evaluate(state, params):
    """Decide whether this position should be rebalanced right now.

    Returns a Decision. At most one trigger fires; if none does, trigger is
    None, terminal is False, and the new_* fields mirror current state.

    net_delta and margin_cushion are populated on every call whether or not
    anything tripped -- the monitor logs them each check.

    margin_cushion here is state.account_equity - state.margin_required, both
    exactly as passed in. In the backtest that is a PRE-borrow, PRE-action
    figure and is deliberately NOT the same quantity as band.py's recorded
    margin_cushion series, which is post-borrow and post-action. The two differ
    by one day's accrual and by the size actually carried out of the day. See
    spec 002's Result: that seam is a known open issue with its own spec
    coming. Do not reconcile them here.
    """
    L = state.leverage
    net_delta = state.long_notional - L * state.short_notional
    margin_cushion = state.account_equity - state.margin_required

    # The four conditions below are moved verbatim from band.py's loop
    # (spec 003). Do not simplify, reorder, or hoist -- bit-exact output is
    # the gate.
    if params.drawdown_stop is not None and state.account_equity < state.peak_equity * (1 - params.drawdown_stop):
        # Terminal: close both legs, stay flat. A stop does not touch target --
        # band.py leaves it alone, and final_target is a reported field.
        return Decision(
            trigger="drawdown stop", terminal=True,
            new_short_notional=0.0, new_long_notional=0.0,
            new_target=state.target,
            net_delta=net_delta, margin_cushion=margin_cushion,
        )

    if params.margin_derisk and state.account_equity < state.margin_required:
        # Cushion is negative (equity < margin required). Recompute target from
        # CURRENT equity -- a partial reset, not a close.
        if state.account_equity <= 0:
            # Nothing left to margin. Close rather than resize; a target
            # computed from non-positive equity is meaningless. In practice
            # the drawdown stop fires long before this.
            target_new = 0.0
        else:
            target_new = (state.account_equity * params.capital_utilization) / state.margin_multiplier
        return Decision(
            trigger="margin de-risk", terminal=False,
            new_short_notional=target_new, new_long_notional=L * target_new,
            new_target=min(state.target, target_new),  # ratchets DOWN only, never back up
            net_delta=net_delta, margin_cushion=margin_cushion,
        )

    # Foil decay band first: its reset also re-neutralizes delta.
    if abs(state.short_notional - state.target) > params.foil_decay_band * state.target:
        return Decision(
            trigger="foil decay band", terminal=False,
            new_short_notional=state.target, new_long_notional=L * state.target,
            new_target=state.target,
            net_delta=net_delta, margin_cushion=margin_cushion,
        )

    if abs(net_delta) > params.long_short_band * state.target:
        # Long leg only: the short carries at its current, drifted value.
        return Decision(
            trigger="long-short band", terminal=False,
            new_short_notional=state.short_notional,
            new_long_notional=L * state.short_notional,
            new_target=state.target,
            net_delta=net_delta, margin_cushion=margin_cushion,
        )

    return Decision(
        trigger=None, terminal=False,
        new_short_notional=state.short_notional,
        new_long_notional=state.long_notional,
        new_target=state.target,
        net_delta=net_delta, margin_cushion=margin_cushion,
    )
