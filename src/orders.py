"""Limit pricing for a tripped decision. Computes prices; submits NOTHING.

The output is a proposal: a plain dict of per-leg tickets that gets rendered
into a Telegram message and appended to orders.jsonl with status "placeholder".
There is no order-submission code in this module or anywhere else in the repo,
and Gateway stays in Read-Only API mode. Spec 008 section 8 lists what must be
true before that changes; until then this file is arithmetic only.

Pure on purpose: no I/O, no broker import, no dates, no Decision mutation. The
arithmetic is the part that has to be right, and pure functions are the part of
this codebase that can actually be tested offline (scripts/verify_orders.py).

Two order styles, chosen by what the trigger means:

  Drift corrections (foil decay band, long-short band) price at the band
  boundary -- the position was acceptable there by construction, so there is no
  urgency, and a limit resting at the boundary trades only if price comes back
  to us. Risk reductions (margin de-risk, drawdown stop) price MARKETABLY:
  through the mark by a configurable offset, because the scenario where a
  passive limit fails to fill is exactly the scenario a de-risk exists for. A
  resting limit must never be the mechanism by which we de-risk.

This module changes a limit PRICE only. What the position is rebalanced TO is
decision.py's output and is taken as given -- if pricing and sizing ever get
conflated, stop.

Share rounding is format_trade_line's, not re-solved: the dollar delta at the
CURRENT mark, rounded to whole shares, with every dollar figure derived from
the rounded count and the residual shown. The ticket's notional is those same
shares at the limit price, so ticket and alert agree on the share count while
each stays internally consistent at its own price.

Known gap, recorded here deliberately: TIF is DAY and there is NO fill-or-
escalate policy. An unfilled drift correction simply re-trips on the monitor's
next cycle and re-alerts, which is tolerable; an unfilled de-risk is the
liquidation scenario. A fill-or-escalate policy is REQUIRED before any of these
tickets become live orders (spec 008 section 8).
"""

# Basis points through the mark for risk-reducing orders. A guess pending
# observed TSLL/TSLA spreads (spec 008, design decisions for review); the
# monitor overrides it from MARKETABLE_OFFSET_BP.
DEFAULT_MARKETABLE_OFFSET = 0.0025

PASSIVE = "passive boundary"
MARKETABLE = "marketable"

_RISK_TRIGGERS = ("margin de-risk", "drawdown stop")


def _side(delta, short_leg):
    """Same verbs as monitor.format_trade_line -- the ticket and the alert must
    read as the same trade."""
    if short_leg:
        return "SELL (short more)" if delta > 0 else "BUY TO COVER"
    return "BUY" if delta > 0 else "SELL"


def _is_buy(side):
    return side.startswith("BUY")


def _ticket(ticker, side, current, new, mark, limit_exact, basis):
    """One leg's ticket, or a no-trade entry when it cannot be priced or rounds
    to zero shares -- both legs stay visible on the proposal either way, so the
    one-leg-fills-first problem is visible in the artifact."""
    delta = new - current

    base = {
        "ticker": ticker,
        "side": side,
        "order_type": "LMT",
        "tif": "DAY",
        "basis": basis,
        "current_notional": round(current, 2),
        "new_notional": round(new, 2),
    }

    if not mark:
        return {**base, "shares": 0, "limit": None, "notional": 0.0,
                "residual": 0.0, "tradeable": False,
                "note": "no price available, so no share count"}

    shares = round(abs(delta) / mark)
    if shares == 0:
        return {**base, "shares": 0, "limit": None, "notional": 0.0,
                "residual": 0.0, "tradeable": False,
                "note": f"{abs(delta):,.2f} at ~{mark:,.2f} rounds to 0 shares"}

    if limit_exact is None:
        return {**base, "shares": shares, "limit": None, "notional": 0.0,
                "residual": 0.0, "tradeable": False,
                "note": f"cannot derive a limit -- {basis}"}

    # Limit rounded to the cent BEFORE the notional is computed, so the two
    # figures a phone reader multiplies are the two figures shown.
    limit = round(limit_exact, 2)
    notional = shares * limit
    landing = current + (notional if delta > 0 else -notional)

    return {**base, "shares": shares, "limit": limit,
            "limit_exact": limit_exact, "notional": round(notional, 2),
            "residual": round(landing - new, 2), "tradeable": True}


def build_proposal(state, d, params, marks, held_shares, pair,
                   marketable_offset=DEFAULT_MARKETABLE_OFFSET):
    """The proposal for a tripped decision: one ticket per leg that trades.

    state is the decision.PositionState the decision was evaluated from, d the
    resulting Decision, params the BandParams, marks {ticker: price},
    held_shares {ticker: currently held share count, positive magnitudes} --
    the boundary formulas divide by what is actually held, not by a count
    re-derived from notional/mark -- and pair the config.PAIRS entry. Returns
    None when nothing is tripped.

    Legs whose size the decision leaves unchanged (the short leg on a
    long-short trip) get no ticket at all; legs that trade but cannot be priced
    or round to zero shares get an explicit no-trade entry rather than silence.
    """
    if d.trigger is None:
        return None

    lev, und = pair["leveraged_ticker"], pair["underlying_ticker"]
    lev_mark, und_mark = marks.get(lev), marks.get(und)
    style = MARKETABLE if d.trigger in _RISK_TRIGGERS else PASSIVE

    legs = []

    short_delta = d.new_short_notional - state.short_notional
    if abs(short_delta) >= 0.005:
        side = _side(short_delta, short_leg=True)
        limit_exact, basis = _short_leg_limit(
            d.trigger, state, params, lev_mark, held_shares.get(lev),
            side, marketable_offset)
        legs.append(_ticket(lev, side, state.short_notional,
                            d.new_short_notional, lev_mark, limit_exact, basis))

    long_delta = d.new_long_notional - state.long_notional
    if abs(long_delta) >= 0.005:
        side = _side(long_delta, short_leg=False)
        limit_exact, basis = _long_leg_limit(
            d.trigger, state, params, und_mark, held_shares.get(und),
            side, marketable_offset)
        legs.append(_ticket(und, side, state.long_notional,
                            d.new_long_notional, und_mark, limit_exact, basis))

    return {
        "trigger": d.trigger,
        "style": style,
        "tif": "DAY",
        "marks": {t: round(p, 4) for t, p in marks.items() if p},
        "legs": legs,
        "residual_total": round(sum(leg["residual"] for leg in legs), 2),
    }


def _marketable(mark, side, offset):
    """Through the touch by the offset: fills like a market order, caps the
    damage from a stale quote or a gapped book."""
    if not mark:
        return None
    return mark * (1 + offset) if _is_buy(side) else mark * (1 - offset)


def _short_leg_limit(trigger, state, params, mark, held, side, offset):
    """(exact limit, basis string) for the leveraged leg."""
    if trigger in _RISK_TRIGGERS:
        return (_marketable(mark, side, offset),
                f"marketable, {offset * 1e4:.0f}bp through the mark "
                "(a resting limit must never be the de-risk mechanism)")

    if trigger == "foil decay band":
        # Invert the band condition |short_notional - target| > band * target
        # for the price where the leg sits exactly on the edge, holding the
        # share count at what is actually held. Sign on the side breached:
        # grew too large -> lower boundary, a passive buy below the mark;
        # shrank too small -> upper boundary, a passive sell above it.
        if not held:
            return None, "no share count available"
        sign = 1 if state.short_notional > state.target else -1
        boundary = state.target * (1 + sign * params.foil_decay_band) / held
        return (boundary,
                f"foil decay band boundary "
                f"({params.foil_decay_band * 100:.1f}% of target)")

    # A long-short trip resizes the long leg only; the short leg never trades
    # here, so there is nothing to price.
    return None, "short leg does not trade on this trigger"


def _long_leg_limit(trigger, state, params, mark, held, side, offset):
    """(exact limit, basis string) for the underlying leg."""
    if trigger in _RISK_TRIGGERS:
        return (_marketable(mark, side, offset),
                f"marketable, {offset * 1e4:.0f}bp through the mark "
                "(a resting limit must never be the de-risk mechanism)")

    if trigger == "long-short band":
        # The band condition is on net_delta = long - leverage * short, which
        # contains two prices. It is invertible only with one held fixed --
        # and freezing the short at its current mark is exactly right, because
        # this trip resizes the long leg only: the short is a constant of the
        # rule itself, not an approximation.
        if not held:
            return None, "no share count available"
        net_delta = state.long_notional - state.leverage * state.short_notional
        sign = 1 if net_delta > 0 else -1
        boundary = (state.leverage * state.short_notional
                    + sign * params.long_short_band * state.target) / held
        return (boundary,
                f"long-short band boundary "
                f"({params.long_short_band * 100:.1f}% of target, "
                "short leg frozen at current mark)")

    # Foil decay resets both legs, but the band condition carries no price for
    # the underlying, so there is no boundary to invert. Rest at the current
    # mark and say so -- do not pretend a boundary exists.
    return mark, "current mark"


def format_proposal(proposal):
    """The placeholder ticket text. Plain text; the caller owns any Telegram
    escaping and code-fencing.

    The framing must be unambiguous that nothing was sent -- a stakeholder
    reading "placed limit order" will believe an order exists.
    """
    lines = ["PROPOSED -- not submitted. Enter manually or ignore.", ""]

    legs = proposal["legs"]
    side_w = max((len(leg["side"]) for leg in legs), default=4)
    for leg in legs:
        if not leg["tradeable"]:
            lines.append(f"  {leg['side']:<{side_w}}  {leg['ticker']:<5} "
                         f"no trade -- {leg['note']}")
            continue
        lines.append(
            f"  {leg['side']:<{side_w}}  {leg['ticker']:<5} "
            f"{leg['shares']:>5,d} sh  @ limit ${leg['limit']:,.2f}"
            f"  = ${leg['notional']:,.2f}"
        )

    lines.append("")
    bases = []
    for leg in legs:
        entry = f"{leg['ticker']} at {leg['basis']}"
        if entry not in bases:
            bases.append(entry)
    lines.append("  Limit basis: " + "; ".join(bases))
    lines.append(f"  TIF: {proposal['tif']}")
    r = proposal["residual_total"]
    lines.append(f"  Residual after rounding: {'-' if r < 0 else '+'}${abs(r):,.2f}")
    return "\n".join(lines)
