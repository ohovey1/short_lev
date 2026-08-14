"""The monitor loop. Reads one pair, decides, alerts; tripped decisions go to
the execution layer's railed dispatch (spec 009).

Long-running process: connect once, loop until killed, reconnect on drop. NOT a
cron job that connects and exits each cycle -- reconnecting every 15 minutes
burns client ids and loses the account-data warmup.

It calls the same decision.evaluate() the backtest calls. That is the hard
constraint from CLAUDE.md: one definition of "is a band breached", two callers.
Nothing here re-derives a trip condition.

Two things this deliberately does NOT do:

  - Persist target. It is re-derived every cycle from base_capital. In
    particular, band.py's loop ends with an unconditional `target = d.new_target`
    and this loop must NOT mirror that line. It is correct for a backtest that
    executes the de-risk and wrong for a monitor that does not: moving the
    reference for a trade that never happened corrupts every subsequent band
    reading. The de-risk target is reported and thrown away.

  - Touch the wire itself. Spec 009 gave trips an executor, but every
    submission decision is execution.dispatch's -- four default-off rails, one
    line to audit -- and every wire-touching line lives in execution.py, never
    here. Until armed (DRY_RUN=0, EXECUTION_ENABLED=1, no HALT, no orphan
    flag) a trip produces a logged dry-run ticket and an alert, nothing else;
    terminal triggers never dispatch at all.

ib.sleep, never time.sleep
--------------------------
ib_async runs an asyncio event loop underneath. ib.sleep() keeps pumping it;
time.sleep(900) blocks it for fifteen minutes. The socket stays open so nothing
looks broken, but keepalives are missed and portfolio data goes stale -- the
monitor then reports quarter-hour-old numbers with complete confidence. This is
the single most likely bug in this file, it passes every gate except gate 10,
and it applies to the poll interval AND to every wait in the reconnect backoff.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from ib_async import IB

import alert_state
import approval
import broker
import config
import decision
import events
import execution
import execution_state
import monitor_state
import notify
import orders
import runtime_state

load_dotenv()

log = logging.getLogger("monitor")

PAIR_KEY = "TSLL"

# All alert timing runs on ET, not on whatever zone the box is set to.
# HEARTBEAT_HOUR defaults to 09:45 because that is just after the market open --
# an ET fact. On a UTC box the naive-local version fired at an arbitrary hour
# with no error and no clue why. zoneinfo is stdlib; there is no dependency cost.
ET = ZoneInfo("America/New_York")

# One tolerance for all three startup comparisons. These WARN and never refuse:
# an operator who knows the position is mid-adjustment still needs the monitor
# running.
SANITY_TOLERANCE = 0.10

RECONNECT_BACKOFF_START = 10
RECONNECT_BACKOFF_MAX = 300

# The approval loop's clocks. The poll sleep is sliced so a button press gets
# a reply in ~10 seconds instead of up to fifteen minutes; the confirm expiry
# is short enough that the marks have not moved under a posted proposal; the
# stale-refusal age is where a tapped alert's context is judged gone entirely.
# The last two are flagged as guesses in spec 008's design-decisions section.
INTENT_CHECK_SECONDS = 10
PROPOSAL_EXPIRY_SECONDS = 60
STALE_REFUSE_SECONDS = 4 * 3600

# Routine, expected, and not a bug: these get one WARNING line and a DEBUG
# traceback. Anything else keeps its full traceback -- a genuine logic error in
# the loop must not be disguised as a nightly Gateway restart. ConnectionError
# is an OSError subclass; both are named for clarity about what is covered.
DISCONNECT_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError)


def _env_float(name, default):
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _heartbeat_time():
    """(hour, minute) from HEARTBEAT_HOUR. Accepts "09:45" or "9".

    Interpreted in ET, regardless of the box's timezone -- see the ET constant
    above. The heartbeat fires only inside a window that starts at this time
    (alert_state.HEARTBEAT_WINDOW_MINUTES), not on the first poll after it.
    """
    raw = (os.environ.get("HEARTBEAT_HOUR") or "").strip()
    if not raw:
        return 9, 45
    try:
        if ":" in raw:
            hour, minute = raw.split(":", 1)
            return int(hour), int(minute)
        return int(raw), 0
    except ValueError:
        log.warning("HEARTBEAT_HOUR=%r is not readable; using 09:45", raw)
        return 9, 45


def band_params():
    """BandParams from config defaults, each overridable by env.

    Same values as the backtest -- no separate monitor tuning. A monitor that
    alerts on different thresholds than the backtest measured is reporting on a
    strategy nobody tested.
    """
    return decision.BandParams(
        long_short_band=_env_float("LONG_SHORT_BAND", config.DEFAULT_LONG_SHORT_BAND),
        foil_decay_band=_env_float("FOIL_DECAY_BAND", config.DEFAULT_FOIL_DECAY_BAND),
        capital_utilization=_env_float("CAPITAL_UTILIZATION", config.DEFAULT_CAPITAL_UTILIZATION),
        drawdown_stop=_env_float("DRAWDOWN_STOP", config.DEFAULT_DRAWDOWN_STOP),
        margin_derisk=_env_bool("MARGIN_DERISK", config.DEFAULT_MARGIN_DERISK),
    )


def derive_target(base_capital, params, pair):
    """target = (base_capital * capital_utilization) / margin_multiplier

    The one formula, identical to band.py, with the denominator derived from
    the pair's two margin rates rather than stored (config.margin_multiplier).

    base_capital is an allocation decision, never NLV -- deriving target from
    account value makes the reference drift with P&L, so drift never
    accumulates against it and the foil decay band silently never fires.
    STRATEGY_SPEC section 1 has the full argument; do not re-derive it here.
    """
    return (base_capital * params.capital_utilization) / config.margin_multiplier(pair)


@dataclass(frozen=True)
class Sanity:
    """Whether the configuration describes this account. Three advisory checks.

    capital_exceeds_nlv is the only one that gates anything: a target derived
    from more capital than the account holds is unachievable, so a trip
    recommending the resize contradicts the warning printed above it. The other
    two produce entirely actionable trips -- an undeployed deposit and a
    position opened at a different size both want a real trade.
    """
    capital_exceeds_nlv: bool
    nlv_exceeds_capital: bool
    notional_off_target: bool
    messages: tuple


def sanity_flags(base_capital, target, state):
    """Pure: evaluate the three sanity conditions. No logging, no I/O.

    Called every cycle so suppression tracks the current account rather than
    whatever was true at startup -- if NLV recovers above base_capital, the
    suppression must lift without a restart.
    """
    nlv = state.account_equity
    messages = []

    capital_exceeds_nlv = base_capital > nlv * (1 + SANITY_TOLERANCE)
    if capital_exceeds_nlv:
        messages.append(
            f"base_capital {base_capital:,.2f} exceeds NLV {nlv:,.2f} by more "
            f"than {SANITY_TOLERANCE * 100:.0f}%. The derived target "
            f"{target:,.2f} is unachievable -- the position will be oversized, "
            "cushion thin, and margin de-risk will fire repeatedly against a "
            "reference that can never be met. Band trips are suppressed while "
            "this holds: acting on them would add exposure the account cannot "
            "carry."
        )

    nlv_exceeds_capital = nlv > base_capital * (1 + SANITY_TOLERANCE)
    if nlv_exceeds_capital:
        messages.append(
            f"NLV {nlv:,.2f} exceeds base_capital {base_capital:,.2f} by more "
            f"than {SANITY_TOLERANCE * 100:.0f}% -- likely an undeployed "
            "deposit. Taking NO sizing action: raising base_capital is a human "
            f"decision (STRATEGY_SPEC section 1). Derived target remains "
            f"{target:,.2f}."
        )

    # Arithmetically identical to the foil decay band, but a different
    # diagnosis. The band says "time to trade"; this says "your config may not
    # describe this account". Reported separately so the first check of a
    # session does not read as a spurious trip.
    notional_off_target = abs(state.short_notional - target) > SANITY_TOLERANCE * target
    if notional_off_target:
        messages.append(
            f"short notional {state.short_notional:,.2f} is more than "
            f"{SANITY_TOLERANCE * 100:.0f}% from derived target {target:,.2f}. "
            "Either base_capital is wrong or the position was opened at a "
            "different size. This is a config warning, not a band trip."
        )

    return Sanity(
        capital_exceeds_nlv=capital_exceeds_nlv,
        nlv_exceeds_capital=nlv_exceeds_capital,
        notional_off_target=notional_off_target,
        messages=tuple(messages),
    )


def log_startup_sanity(s):
    """Console output for the sanity checks -- once per process, at startup.

    sanity_flags() runs every cycle to keep suppression current; this prints
    once so the terminal does not gain a repeating wall of warnings.
    """
    for message in s.messages:
        log.warning("SANITY: %s", message)

    log.info(
        "note: the startup foil-decay verdict is vacuous -- drift from a "
        "just-derived target is zero by construction. Net delta and margin "
        "cushion are meaningful immediately; foil decay becomes meaningful "
        "once the bot has been running."
    )


def log_check(state, d, prices, unactionable=False):
    """Log every check, tripped or not.

    Non-trips matter as much as trips: this log is the raw material for
    calibrating the intraday-cadence question deferred in the ROADMAP, which
    needs the checks that did nothing as much as the ones that fired.
    """
    log.info(
        "check: short=%.2f long=%.2f target=%.2f net_delta=%+.2f "
        "cushion=%+.2f equity=%.2f peak=%.2f trigger=%s",
        state.short_notional, state.long_notional, state.target,
        d.net_delta, d.margin_cushion, state.account_equity,
        state.peak_equity, d.trigger or "none",
    )

    if d.trigger is None:
        return

    log.warning(
        "TRIP: %s%s%s", d.trigger, " (TERMINAL)" if d.terminal else "",
        " (UNACTIONABLE -- target unachievable, not alerted)" if unactionable else "",
    )
    for line in trade_lines(state, d, prices):
        for part in line.split("\n"):
            log.warning("  %s", part)

    if d.trigger == "margin de-risk":
        log.warning(
            "  de-risk would ratchet target to %.2f. REPORTED ONLY -- not "
            "persisted. The monitor does not trade, and moving the reference "
            "for a trade that never happened would corrupt every subsequent "
            "band reading. Next cycle re-derives target from base_capital.",
            d.new_target,
        )


def format_trade_line(ticker, current_notional, new_notional, price, short_leg):
    """Return the trade to place, in shares -- 'buy 43 TSLA', not 'resize long leg'.

    Returns a string (possibly two lines) rather than logging, because the same
    text goes to the console and to Telegram. One definition, two consumers --
    the same seam reasoning as decision.evaluate().

    Every dollar figure derives from the ROUNDED share count. The prior version
    printed three numbers describing three different trades: the exact dollar
    requirement, the share count rounded from it, and a landing notional that
    assumed the exact figure was traded. You cannot reconcile a fill against
    that. The residual against the requested size is shown instead of being
    papered over -- it is expected and small, but it must be visible.
    """
    delta = new_notional - current_notional
    if abs(delta) < 0.005:
        return f"{ticker}: no change ({current_notional:,.2f})"

    # For the short leg, a notional INCREASE means shorting more, i.e. selling.
    if short_leg:
        action = "SELL (short more)" if delta > 0 else "BUY TO COVER"
    else:
        action = "BUY" if delta > 0 else "SELL"

    if not price:
        return (
            f"{ticker}: {action} ${abs(delta):,.2f} notional "
            f"({current_notional:,.2f} -> {new_notional:,.2f}). "
            "No price available, so no share count."
        )

    shares = round(abs(delta) / price)
    if shares == 0:
        # Sub-one-share requirement. Saying "0 shares @ ~329.38 = $0.00" reads
        # as an actionable trade; it is not one.
        return (
            f"{ticker}: no trade -- ${abs(delta):,.2f} at ~{price:,.2f} rounds "
            f"to 0 shares ({current_notional:,.2f}, target {new_notional:,.2f})"
        )

    amount = shares * price
    landing = current_notional + (amount if delta > 0 else -amount)
    residual = landing - new_notional

    return (
        f"{ticker}: {action} {shares:,d} shares @ ~{price:,.2f} = ${amount:,.2f}\n"
        f"      {current_notional:,.2f} -> {landing:,.2f}  "
        f"(target {new_notional:,.2f}, residual {residual:+,.2f})"
    )


def trade_lines(state, d, prices):
    """Both legs' trade lines for a tripped decision, short leg first."""
    pair = config.PAIRS[PAIR_KEY]
    lev, und = pair["leveraged_ticker"], pair["underlying_ticker"]
    return [
        format_trade_line(lev, state.short_notional, d.new_short_notional,
                          prices.get(lev), short_leg=True),
        format_trade_line(und, state.long_notional, d.new_long_notional,
                          prices.get(und), short_leg=False),
    ]


# The dedup slot holds one value. While a trip is suppressed as unactionable
# this key occupies it, so the sanity warning repeats on the normal timer
# instead of the two keys alternating and re-sending every cycle.
SANITY_TRIGGER_KEY = "sanity: base_capital exceeds NLV"

# Severity per trip now lives in notify.TRIGGER_SEVERITY: the bot's /help is
# generated from the same table and must not import this module (it would
# drag broker in). Aliased so existing call sites read the same.
TRIGGER_SEVERITY = notify.TRIGGER_SEVERITY


def _fmt_et(ts):
    """ISO timestamp -> '14:32 ET', never a bare ISO string in a message."""
    parsed = _parse_ts(ts)
    return parsed.astimezone(ET).strftime("%H:%M ET") if parsed else "unknown"


def trip_message(state, d, prices, ts=None):
    """The body of a trip alert: current state, then both trade lines.

    Actionable without opening TWS -- that is the whole requirement. The marks
    and their time are stated because the alert may be tapped an hour later:
    the staleness protocol needs the reader to see what the numbers WERE.
    """
    lines = [
        f"short {state.short_notional:,.2f} / target {state.target:,.2f}",
        f"long {state.long_notional:,.2f} | net delta {d.net_delta:+,.2f}",
        f"cushion {d.margin_cushion:+,.2f} (equity {state.account_equity:,.2f})",
    ]
    if ts and prices:
        marks = " / ".join(f"{t} {p:,.2f}" for t, p in sorted(prices.items()))
        lines.append(f"marks {marks} at {_fmt_et(ts)}")
    lines.append("")
    lines.extend(trade_lines(state, d, prices))

    if d.trigger == "margin de-risk":
        lines.append(
            f"\nde-risk would ratchet target to {d.new_target:,.2f}. REPORTED "
            "ONLY -- the monitor does not trade and does not persist this."
        )
    if d.terminal:
        lines.append(
            "\nTERMINAL: nothing is executed here. The position stays open "
            "until a human acts."
        )
    return "\n".join(lines)


def heartbeat_message(state, d, checks_since):
    return "\n".join([
        f"monitoring {PAIR_KEY}, {checks_since} "
        f"check{'' if checks_since == 1 else 's'} since last heartbeat",
        f"short {state.short_notional:,.2f} / target {state.target:,.2f}",
        f"long {state.long_notional:,.2f} | net delta {d.net_delta:+,.2f}",
        f"cushion {d.margin_cushion:+,.2f} (equity {state.account_equity:,.2f})",
        f"state: {d.trigger or 'no trigger'}",
    ])


def decision_id(ts):
    """Eight hex chars derived from the check timestamp.

    Short because Telegram caps callback_data at 64 bytes; deterministic so
    the id in a button and the id in the checks.jsonl row cannot disagree.
    """
    return hashlib.sha1(ts.encode("utf-8")).hexdigest()[:8]


def proposal_id(ts):
    """Eight hex chars for a posted proposal. Prefixed before hashing so a
    proposal created in the same second as a check cannot collide with that
    check's decision_id inside the consumed-intent set."""
    return hashlib.sha1(("proposal:" + ts).encode("utf-8")).hexdigest()[:8]


def _parse_ts(ts):
    """ISO timestamp -> aware datetime, or None. Naive values are read as ET,
    same reasoning as alert_state._match_awareness."""
    try:
        parsed = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed


def proposal_expired(active, now):
    """True when an active proposal can no longer be confirmed.

    Validated at PROCESSING time, not tap time: the expiry exists so the
    marks cannot have moved under the ticket, and they keep moving while an
    intent waits in the file. An unreadable timestamp counts as expired --
    refusing is the safe direction.
    """
    posted = _parse_ts((active or {}).get("ts"))
    if posted is None:
        return True
    return (now - posted).total_seconds() > PROPOSAL_EXPIRY_SECONDS


def find_check(check_id):
    """The checks.jsonl row a button's decision_id points at, or None.

    A rescan of the file, newest first. The row is the alert's frozen context
    -- its marks, shares, and time -- which is exactly what the staleness
    protocol needs to show the reader what moved."""
    path = os.path.join(events.event_log_dir(), events.CHECKS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("decision_id") == check_id:
            return row
    return None


def state_from_row(row, pair):
    """Rebuild the PositionState a logged check was evaluated from.

    Exists so the drift disclosure can re-derive the OLD ticket with the same
    machinery as the fresh one -- decision.evaluate plus orders.build_proposal
    -- instead of reverse-engineering share counts out of message text."""
    return decision.PositionState(
        short_notional=row["short_notional"],
        long_notional=row["long_notional"],
        leverage=pair["leverage"],
        target=row["target"],
        account_equity=row["account_equity"],
        peak_equity=row["peak_equity"],
        margin_required=row["ibkr_maint"],
        margin_multiplier=config.margin_multiplier(pair),
    )


def drift_block(old_row, old_proposal, fresh_proposal, prices, pair):
    """Old price, new price, and the change in shares, per traded leg.

    Rendered ABOVE the fresh ticket, not as a footnote: the point is that the
    reader notices the numbers are no longer the ones they tapped."""
    price_keys = {
        pair["leveraged_ticker"]: "short_price",
        pair["underlying_ticker"]: "long_price",
    }
    old_legs = {leg["ticker"]: leg
                for leg in (old_proposal or {}).get("legs", [])}
    lines = []
    for leg in fresh_proposal.get("legs", []):
        ticker = leg["ticker"]
        old_price = old_row.get(price_keys.get(ticker, ""))
        new_price = prices.get(ticker)
        old_leg = old_legs.get(ticker)
        old_part = f"{old_price:,.2f}" if old_price else "?"
        new_part = f"{new_price:,.2f}" if new_price else "?"
        old_shares = old_leg["shares"] if old_leg else 0
        lines.append(f"{ticker:<5} {old_part} -> {new_part}   "
                     f"trade {old_shares:,d} sh -> {leg['shares']:,d} sh")
    return "\n".join(lines)


def _round(value, digits=2):
    return round(value, digits) if value is not None else None


def check_record(state, d, unactionable, params, details, ts):
    """The checks.jsonl row. One line per check, tripped or not.

    Widened in spec 008 (additive -- consumers read by key) so the poller can
    answer /positions from this file alone: share counts, marks, and average
    cost per leg, the two band fractions so distance can be stated as a
    percentage of its band without the poller re-deriving params, and the
    decision_id a trip alert's button will carry. That widening, not a second
    IBKR connection, is how the bot sees the position.
    """
    pair = config.PAIRS[PAIR_KEY]
    details = details or {}
    lev = details.get(pair["leveraged_ticker"], {})
    und = details.get(pair["underlying_ticker"], {})
    return {
        "ts": ts,
        "decision_id": decision_id(ts),
        "pair": PAIR_KEY,
        "short_notional": round(state.short_notional, 2),
        "long_notional": round(state.long_notional, 2),
        "target": round(state.target, 2),
        "net_delta": round(d.net_delta, 2),
        "margin_cushion": round(d.margin_cushion, 2),
        "account_equity": round(state.account_equity, 2),
        "peak_equity": round(state.peak_equity, 2),
        "ibkr_maint": round(state.margin_required, 2),
        # The same two-term model broker.read_position logs each cycle. Not a
        # strategy formula -- the sizing denominator stays in config.py.
        "modeled_maint": round(
            pair["long_rate"] * state.long_notional
            + pair["short_rate"] * state.short_notional, 2),
        "trigger": d.trigger,
        "unactionable": unactionable,
        "short_shares": lev.get("shares"),
        "long_shares": und.get("shares"),
        "short_price": _round(lev.get("price"), 4),
        "long_price": _round(und.get("price"), 4),
        "short_avg_cost": _round(lev.get("avg_cost"), 4),
        "long_avg_cost": _round(und.get("avg_cost"), 4),
        "long_short_band": params.long_short_band,
        "foil_decay_band": params.foil_decay_band,
    }


def _orphan_line(o):
    """One human line per unaccountable order or fill. The two shapes share
    the flag file, so the formatter reads both."""
    if "exec_id" in o:
        return (f"FILL {o.get('symbol')} {o.get('side')} "
                f"{o.get('shares') or 0:g} @ {o.get('price')} "
                f"at {o.get('time')} execId={o.get('exec_id')} "
                f"ref={o.get('order_ref') or '-'}")
    return (f"{o.get('symbol')} {o.get('action')} {o.get('quantity') or 0:g} "
            f"{o.get('order_type')} status={o.get('status')} "
            f"clientId={o.get('client_id')} ref={o.get('order_ref') or '-'}")


def orphan_alert(found, send, filled=False):
    """The orphan rail: flag file, CRITICAL log, CRITICAL alert.

    Spec 009 section 4 rows five and six share it. A working order the ledger
    cannot account for is the classic executor bug; an EXECUTION the ledger
    cannot account for is worse -- something traded with no record, which is
    precisely what an event-driven design would never see. Either way
    submission stays blocked until a human reconciles and deletes the flag.
    Nothing is cancelled here.
    """
    flag = execution.flag_orphans(found)
    what = ("execution(s) with no ledger record -- something traded that "
            "this process did not record" if filled else
            "working order(s) the ledger cannot account for")
    log.critical("ORPHAN CHECK: %d %s. Submission blocked until a human "
                 "reconciles and deletes %s.", len(found), what, flag)
    lines = []
    for o in found:
        line = _orphan_line(o)
        log.critical("  %s", line)
        lines.append(line)
    send("CRITICAL", "orphan orders",
         f"{PAIR_KEY} -- unaccounted {'fills' if filled else 'orders'} found",
         f"Found {what}. Submission is blocked until a human reconciles "
         "and deletes the flag file.\n\n" + "\n".join(lines))


def _record_fill(order_ref, f):
    """One ledger row per execution, keyed by exec_id for dedup."""
    events.log_order({"status": "fill", "order_ref": order_ref,
                      "exec_id": f.get("exec_id"), "shares": f.get("shares"),
                      "price": f.get("price"), "perm_id": f.get("perm_id"),
                      "fill_time": f.get("time")})


def _record_foreign_fill(f):
    """A foreign execution gets its own ledger row so the SAME event is not
    re-flagged on every reconnect -- that would train reflexive
    flag-clearing, the exact habit section 9 exists to avoid."""
    events.log_order({"status": "foreign_fill", "exec_id": f.get("exec_id"),
                      "symbol": f.get("symbol"), "side": f.get("side"),
                      "shares": f.get("shares"), "price": f.get("price"),
                      "perm_id": f.get("perm_id"), "fill_time": f.get("time")})


def _leg_complete(exec_state, leg, fill_time):
    """Mark a leg's ticket entry complete; anchor the unhedged timer on the
    FIRST completed leg of a multi-leg ticket; drop the ticket once every leg
    is done -- both legs filled means hedged again, nothing left to time."""
    pid = leg.get("proposal_id")
    ticket = (exec_state.get("tickets") or {}).get(pid)
    if not ticket:
        return
    entry = (ticket.get("legs") or {}).get(leg["order_ref"])
    if entry is None:
        return
    entry["complete"] = True
    if all(e.get("complete") for e in ticket["legs"].values()):
        exec_state["tickets"].pop(pid, None)
    elif not ticket.get("first_complete_ts"):
        # The fill's own time when parseable -- a restart mid-window must not
        # restart the clock -- else now.
        ticket["first_complete_ts"] = (fill_time if _parse_ts(fill_time)
                                       else events.now_iso())


def _ticket_dead(exec_state, leg):
    """Drop a ticket whose leg stopped trading unfilled (expiry, cancel,
    refusal): its unhedged story is over and the band re-trip takes over."""
    (exec_state.get("tickets") or {}).pop(leg.get("proposal_id"), None)


def predispatch_orders(ib, send):
    """The fresh open-order read immediately before a dispatch: (ok, reason).

    The connect-time picture ages, and re-issuing into a book that already
    holds our resting DAY order is the double-submission bug. Three ways to
    say no, each with a different consequence:

      - the read failed: refuse THIS cycle, write no flag (section 9 -- a
        missed cycle costs one poll interval, submitting into an unknown
        book is unbounded);
      - our own order is still working: not a re-issue situation, wait;
      - a foreign order is working: the orphan rail fires (flag + CRITICAL)
        and the gate blocks until a human clears it.
    """
    open_rows = broker.open_orders(ib)
    if open_rows is None:
        return False, ("open-order query failed; dispatch refused this "
                       "cycle, no flag written")
    ours = [o for o in open_rows if execution.is_ours(o.get("order_ref"))]
    if ours:
        return False, (f"{len(ours)} working order(s) of ours -- not "
                       "re-issuing while a DAY order rests")
    foreign = [o for o in open_rows
               if not execution.is_ours(o.get("order_ref"))]
    if foreign:
        orphan_alert(foreign, send)
        return False, "foreign working orders found -- flagged and blocked"
    return True, "order book clean"


def reconcile_on_connect(ib, send, exec_state):
    """Spec 009 section 4: the spine, not a recovery path.

    Fills that happen while disconnected are replayed as data with no events,
    and our reconnect discards every subscription anyway (execution-notes
    section 3) -- so on every connect the picture is rebuilt from
    reqAllOpenOrders + the fills replay + the orders.jsonl ledger, and every
    non-terminal ledger row is classified into the six-case table.

    Returns True when both reads answered and the findings were applied.
    False means the order book or fill state is UNKNOWN: dispatch stays
    refused until a later attempt succeeds, and NO flag is written -- fail
    closed on the read, no sticky state. Section 9 splits exactly this from
    the confirmed-orphan case: a missed cycle costs one poll interval;
    submitting into an unknown order book is unbounded.
    """
    open_rows = broker.open_orders(ib)
    if open_rows is None:
        log.error("reconcile: open-order query failed; order book UNKNOWN. "
                  "Dispatch refused until a read succeeds; not flagging on "
                  "a failed read.")
        return False
    fill_rows = broker.fills(ib)
    if fill_rows is None:
        log.error("reconcile: fills read failed; fill state UNKNOWN. "
                  "Dispatch refused until a read succeeds; not flagging on "
                  "a failed read.")
        return False

    findings = execution.classify(execution.read_ledger(), open_rows,
                                  fill_rows)
    orphan_orders, orphan_fills = [], []
    for finding in findings:
        case = finding["case"]
        leg = finding.get("leg")
        ref = finding.get("order_ref")
        if case == "working":
            log.info("reconcile: %s still working (%s)", ref,
                     finding["order"].get("status"))
        elif case == "filled_while_away":
            for f in finding["new_fills"]:
                _record_fill(ref, f)
            shares, notional = execution.fill_totals(
                list(leg["fills"].values()) + finding["new_fills"])
            events.log_order({"status": "filled", "order_ref": ref,
                              "filled_shares": shares,
                              "filled_notional": notional})
            log.info("reconcile: %s filled while away: %g sh = %.2f",
                     ref, shares, notional)
            send("INFO", "reconcile", f"{PAIR_KEY} -- order filled while away",
                 f"{ref}: {shares:g} shares for {notional:,.2f} filled while "
                 "the monitor was not watching. Recorded from execution "
                 "replay.")
            fill_time = (finding["new_fills"] or [{}])[-1].get("time")
            _leg_complete(exec_state, leg, fill_time)
            if leg.get("trigger"):
                execution.clear_issues(exec_state, leg["trigger"])
        elif case == "partial":
            for f in finding["new_fills"]:
                _record_fill(ref, f)
            if finding["working"]:
                send("WARNING", "reconcile", f"{PAIR_KEY} -- partial fill",
                     f"{ref}: {finding['filled_shares']:g} of "
                     f"{finding['total_shares']:g} shares filled; remainder "
                     "still working.")
            else:
                events.log_order({
                    "status": "cancelled", "order_ref": ref,
                    "note": "remainder gone unfilled (cancelled or expired "
                            "at the close); fills already received stay ours"})
                send("WARNING", "reconcile",
                     f"{PAIR_KEY} -- partial fill, remainder gone",
                     f"{ref}: {finding['filled_shares']:g} of "
                     f"{finding['total_shares']:g} shares filled; the "
                     "remainder is no longer working. Recorded; the band "
                     "re-check decides what happens next.")
                _ticket_dead(exec_state, leg)
        elif case == "expired":
            events.log_order({"status": "expired", "order_ref": ref})
            log.info("reconcile: %s expired unfilled (DAY at the close, or "
                     "a submit that never reached the wire)", ref)
            _ticket_dead(exec_state, leg)
        elif case == "orphan_working":
            orphan_orders.append(finding["order"])
        elif case == "orphan_filled":
            _record_foreign_fill(finding["fill"])
            orphan_fills.append(finding["fill"])

    if orphan_orders:
        orphan_alert(orphan_orders, send)
    if orphan_fills:
        orphan_alert(orphan_fills, send, filled=True)
    if not (orphan_orders or orphan_fills):
        log.info("reconcile: clean -- %d open order(s), %d fill(s), all "
                 "accounted for", len(open_rows), len(fill_rows))
    return True


def connect_with_backoff(backoff=RECONNECT_BACKOFF_START):
    """Connect, retrying forever with exponential backoff. Never raises.

    Used for the FIRST connect as well as every reconnect -- one backoff path,
    not two. Under systemd the monitor starts as soon as Gateway's *process*
    exists, which is well before Gateway is logged in and serving the API:
    After= is ordering, not readiness. A connect that raises there is a crash
    loop on every boot, silent for the first minutes and filling the journal
    with noise that masks real failures.

    Catches Exception, not DISCONNECT_ERRORS. The two failures actually seen --
    the paper-disclaimer gate (Error 10141) and a stale clientId -- come back as
    ib_async errors, which are not OSError subclasses. Catching only the
    disconnect tuple is exactly why they killed the process.

    IB.sleep, never time.sleep: it is a staticmethod (the same function object
    as util.sleep) and pumps the event loop without a live handle, which is what
    the first connect and a failed reconnect both need.
    """
    while True:
        try:
            return broker.connect()
        except KeyboardInterrupt:
            # Ctrl-C during startup must still exit. BaseException, so it would
            # slip past `except Exception` anyway -- named to say that is meant.
            raise
        except Exception as exc:
            log.warning(
                "connect failed: %s: %s -- retrying in %ds",
                type(exc).__name__, exc, backoff,
            )
            log.debug("connect traceback", exc_info=True)
            IB.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)


def run():
    raw_capital = os.environ.get("MONITOR_BASE_CAPITAL")
    if raw_capital in (None, ""):
        raise SystemExit(
            "MONITOR_BASE_CAPITAL is required. It is an allocation decision -- "
            "the capital deliberately committed to this pair -- not NLV and not "
            "a market quantity. See STRATEGY_SPEC section 1."
        )
    base_capital = float(raw_capital)

    poll_seconds = _env_float("POLL_INTERVAL_SECONDS", 900)
    params = band_params()
    pair = config.PAIRS[PAIR_KEY]
    path = monitor_state.state_path()
    exec_state_path = execution_state.state_path()
    exec_state = execution_state.load(exec_state_path)

    target = derive_target(base_capital, params, pair)
    log.info(
        "target=%.2f derived from base_capital=%.2f x capital_utilization=%.2f "
        "/ margin_multiplier=%.2f (= long %.2f x %s + short %.2f)  "
        "[pair=%s leverage=%s]",
        target, base_capital, params.capital_utilization,
        config.margin_multiplier(pair),
        pair["long_rate"], pair["leverage"], pair["short_rate"],
        PAIR_KEY, pair["leverage"],
    )
    log.info(
        "bands: long_short=%.2f foil_decay=%.2f drawdown_stop=%s derisk=%s "
        "poll=%.0fs state=%s",
        params.long_short_band, params.foil_decay_band, params.drawdown_stop,
        params.margin_derisk, poll_seconds, path,
    )

    repeat_minutes = _env_float("ALERT_REPEAT_MINUTES", 60)
    heartbeat_hour, heartbeat_minute = _heartbeat_time()
    token, chat_id = notify.credentials()
    alert_path = alert_state.state_path()
    alerts = alert_state.load(alert_path)

    marketable_offset = _env_float("MARKETABLE_OFFSET_BP", 25) / 1e4
    intents_p = approval.intents_path()
    seen_p = approval.intents_seen_path()
    approval_p = approval.approval_state_path()
    seen = approval.load_seen(seen_p)
    approval_st = approval.load_state(approval_p)

    log.info(
        "alerts: telegram=%s repeat=%.0fmin heartbeat=%02d:%02d events=%s "
        "alert_state=%s",
        "configured" if (token and chat_id) else "NOT configured (no-op)",
        repeat_minutes, heartbeat_hour, heartbeat_minute,
        events.event_log_dir(), alert_path,
    )
    log.info(
        "approval: intents=%s seen=%d consumed marketable_offset=%.0fbp",
        intents_p, len(seen), marketable_offset * 1e4,
    )

    # Rail b: the execution state is logged on every startup, and refreshed
    # into runtime.json each cycle so /status reports it. Nothing consults
    # the gate to act yet -- the executor path is spec 009's.
    gate = execution.submission_gate()
    log.info(
        "execution: mode=%s -- %s (halt=%s orphan_flag=%s "
        "max_order_multiple=%g state=%s unhedged=%gmin reissue_warn=%d)",
        gate.mode, gate.reason, execution.halt_path(),
        execution.orphan_flag_path(), execution.max_order_multiple(),
        exec_state_path, execution.unhedged_minutes(),
        execution.reissue_warn_count(),
    )

    def send(severity, kind, title, body, reply_markup=None):
        """Hand a decision to the sink. Never raises. Returns the Telegram
        message id when delivered, else None -- trip alerts store it so a
        superseding alert can edit the stale keyboard off."""
        try:
            _, message_id = notify.notify(token, chat_id, severity, kind,
                                          title, body,
                                          reply_markup=reply_markup)
            return message_id
        except Exception as exc:
            # notify() already catches its own failures; this is the belt to
            # that braces. A broken sink must never stop the monitor.
            log.warning("alert dispatch failed (non-fatal): %s: %s",
                        type(exc).__name__, exc)
            return None

    def send_reply(text, label, user=None):
        """A command-flow reply (proposal, refusal, already-handled). Sent
        raw, logged to commands.jsonl -- these are answers to a button press,
        not alerts, and must not pollute alerts.jsonl."""
        delivered, error, message_id = notify.send_text(token, chat_id, text)
        if not delivered:
            log.warning("reply %s failed: %s", label, error)
        events.log_command({"from_user_id": user, "command": label,
                            "authorized": True, "delivered": delivered})
        return message_id

    def send_trip(trip_state, trip_d, trip_prices, trip_ts):
        """One trip alert, with its Rebalance button, with the dedup ledger
        and the superseded-keyboard bookkeeping updated. The button carries
        the check's decision_id -- an intent, never a ticket."""
        nonlocal alerts
        keyboard = {"inline_keyboard": [[{
            "text": "Rebalance",
            "callback_data": f"req:{decision_id(trip_ts)}",
        }]]}
        message_id = send(
            TRIGGER_SEVERITY.get(trip_d.trigger, "WARNING"), "trip",
            f"{PAIR_KEY} -- {trip_d.trigger}",
            trip_message(trip_state, trip_d, trip_prices, ts=trip_ts),
            reply_markup=keyboard,
        )
        # A stale button that cannot be pressed is better than one that can:
        # strip the keyboard off the alert this one supersedes.
        old_message_id = approval_st["trip_messages"].get(trip_d.trigger)
        if old_message_id:
            notify.remove_keyboard(token, chat_id, old_message_id)
        if message_id:
            approval_st["trip_messages"][trip_d.trigger] = message_id
        else:
            approval_st["trip_messages"].pop(trip_d.trigger, None)
        approval.save_state(approval_p, approval_st)
        alerts = alert_state.record_sent(alerts, trip_d.trigger,
                                         datetime.datetime.now(ET))
        alert_state.save(alert_path, alerts)

    def fresh_read():
        """A full fresh evaluation at press time, logged to checks.jsonl like
        any other check (it IS one). Returns None when the position or the
        account is unreadable."""
        target_now = derive_target(base_capital, params, pair)
        fresh_state = broker.read_position(ib, PAIR_KEY, target_now, peak_equity)
        if fresh_state is None:
            return None
        fresh_d = decision.evaluate(fresh_state, params)
        fresh_details = broker.leg_details(ib, PAIR_KEY)
        fresh_prices = {t: leg["price"] for t, leg in fresh_details.items()
                        if leg["price"]}
        fresh_ts = events.now_iso()
        fresh_sanity = sanity_flags(base_capital, target_now, fresh_state)
        fresh_unactionable = (fresh_d.trigger is not None
                              and fresh_sanity.capital_exceeds_nlv)
        events.log_check(check_record(fresh_state, fresh_d, fresh_unactionable,
                                      params, fresh_details, fresh_ts))
        return (fresh_state, fresh_d, fresh_details, fresh_prices, fresh_ts,
                fresh_unactionable)

    def dispatch_on_trip(trip_state, trip_d, trip_details, trip_prices,
                         trip_ts):
        """The executor path, coupled 1:1 to trip alerts: the orders.jsonl
        ticket and the Telegram alert describe the same moment, which is
        exactly what gate 13 audits. Terminal triggers never dispatch --
        automating the largest action in the system is a separate argument,
        deliberately not inherited from spec 009."""
        if trip_d.trigger is None:
            return
        if trip_d.terminal:
            log.info("executor: %s is terminal -- alert-only by design",
                     trip_d.trigger)
            return
        if not reconciled:
            log.error("executor: dispatch refused this cycle -- reconcile "
                      "has not succeeded since connect, so the order book "
                      "is not known to be clean")
            return
        ok, reason = predispatch_orders(ib, send)
        if not ok:
            log.log(logging.ERROR if "failed" in reason else logging.INFO,
                    "executor: %s", reason)
            return
        held = {t: leg["shares"] for t, leg in trip_details.items()}
        proposal = orders.build_proposal(trip_state, trip_d, params,
                                         trip_prices, held, pair,
                                         marketable_offset)
        if proposal is None or not any(leg["tradeable"]
                                       for leg in proposal["legs"]):
            log.info("executor: nothing tradeable on this proposal; "
                     "not dispatching")
            return
        pid = proposal_id(trip_ts)
        status = execution.dispatch(
            proposal, target, send=send,
            extra={"proposal_id": pid, "decision_id": decision_id(trip_ts),
                   "source": "executor"},
            ib=ib)
        log.info("executor: dispatch -> %s (proposal %s)", status, pid)
        if status not in ("dry_run", "submitted"):
            return
        # Issues count in dry run too -- a nudge that is never rehearsed is
        # a nudge that fires wrong the first time it matters.
        issues = execution.note_issue(exec_state, trip_d.trigger)
        if issues - 1 >= execution.reissue_warn_count():
            send("WARNING", "reissue",
                 f"{PAIR_KEY} -- {issues - 1} re-issues without a fill",
                 f"The {trip_d.trigger} proposal has been issued {issues} "
                 "times without a fill. The limit style may be wrong for "
                 "this pair (spec 009 section 7). A nudge, not an action -- "
                 "nothing is escalated automatically.")
        if status == "submitted":
            exec_state["tickets"][pid] = {
                "trigger": trip_d.trigger, "ts": trip_ts,
                "legs": {execution.order_ref(pid, leg["ticker"]): {
                             "ticker": leg["ticker"],
                             "shares": leg["shares"],
                             "complete": False}
                         for leg in proposal["legs"] if leg["tradeable"]},
                "first_complete_ts": None,
                "unhedged_alerted": False,
            }
        execution_state.save(exec_state_path, exec_state)

    def track_fills():
        """Intra-session fill tracking, once per sleep slice. ib.fills() is
        a cached list -- no wire call -- so polling it is free, and the
        ledger is only re-read when the count moves. Events would be lower
        latency, but our reconnect discards subscriptions and replays fire
        no events; this poll is the latency layer that survives both
        (execution-notes section 3). Accounting stays fills-derived."""
        nonlocal last_fill_count
        fill_rows = broker.fills(ib)
        if fill_rows is None or len(fill_rows) == last_fill_count:
            return
        last_fill_count = len(fill_rows)
        ledger_rows = execution.read_ledger()
        legs = execution.fold_ledger(ledger_rows)
        recorded = {r["exec_id"] for r in ledger_rows if r.get("exec_id")}
        new_by_ref, foreign = execution.match_fills(legs, recorded, fill_rows)
        dirty = False
        for ref, fs in new_by_ref.items():
            leg = legs[ref]
            for f in fs:
                _record_fill(ref, f)
            filled = leg["filled_shares"] + execution.fill_totals(fs)[0]
            total = leg["shares"] or 0
            log.info("fill: %s now %g of %g shares", ref, filled, total)
            if total and filled >= total:
                shares, notional = execution.fill_totals(
                    list(leg["fills"].values()) + fs)
                events.log_order({"status": "filled", "order_ref": ref,
                                  "filled_shares": shares,
                                  "filled_notional": notional})
                _leg_complete(exec_state, leg, fs[-1].get("time"))
                if leg.get("trigger"):
                    execution.clear_issues(exec_state, leg["trigger"])
                dirty = True
        for f in foreign:
            _record_foreign_fill(f)
        if foreign:
            orphan_alert(foreign, send, filled=True)
        if dirty:
            execution_state.save(exec_state_path, exec_state)

    def check_unhedged_tickets():
        """Spec 009 section 5: detection bounds the fill gap, sequencing
        would not. Checked every sleep slice (~10s) -- a 10-minute bound
        checked once per 15-minute poll is not a 10-minute bound."""
        now = datetime.datetime.now(ET)
        minutes = execution.unhedged_minutes()
        fired = False
        for pid, ticket in list((exec_state.get("tickets") or {}).items()):
            if not execution.check_unhedged(ticket, now, minutes):
                continue
            ticket["unhedged_alerted"] = True
            fired = True
            legs_desc = "\n".join(
                f"{entry['ticker']}: "
                f"{'complete' if entry.get('complete') else 'NOT filled'} "
                f"({entry.get('shares')} sh)"
                for entry in ticket["legs"].values())
            net_line = ""
            if peak_equity is not None:
                state_now = broker.read_position(
                    ib, PAIR_KEY, derive_target(base_capital, params, pair),
                    peak_equity)
                if state_now is not None:
                    d_now = decision.evaluate(state_now, params)
                    net_line = f"\nnet delta now: {d_now.net_delta:+,.2f}"
            send("CRITICAL", "unhedged",
                 f"{PAIR_KEY} -- one leg unhedged for {minutes:g} min",
                 f"Ticket {pid} ({ticket.get('trigger')}): one leg complete, "
                 f"the other not, for {minutes:g} minutes.\n" + legs_desc
                 + net_line +
                 "\nNothing is escalated automatically; an unfilled DAY leg "
                 "expires at the close (spec 009 sections 5 and 7).")
        if fired:
            execution_state.save(exec_state_path, exec_state)

    def handle_request(intent):
        """A Rebalance tap. Everything is re-derived fresh -- the button
        carried an intent, never a ticket -- and the staleness protocol from
        spec 008 section 7 decides how the answer is framed."""
        check_id = intent.get("decision_id")
        user = intent.get("from_user_id")
        label = f"reply:req:{check_id}"
        now = datetime.datetime.now(ET)

        orig = find_check(check_id)
        orig_ts = _parse_ts((orig or {}).get("ts"))
        orig_age = (now - orig_ts).total_seconds() if orig_ts else None
        started = _parse_ts(runtime.get("started_at"))

        refusal = None
        if orig is None or orig_age is None:
            refusal = "the originating check cannot be found"
        elif orig_age > STALE_REFUSE_SECONDS:
            refusal = "that alert is more than four hours old"
        elif started and orig_ts < started:
            refusal = "that alert is from a previous monitor session"

        fresh = fresh_read()
        if fresh is None:
            send_reply(notify.escape_md_v2(
                "Cannot read the position right now -- a leg or account "
                "value is unreadable. Try again in a few minutes."),
                label, user)
            return
        (fresh_state, fresh_d, fresh_details, fresh_prices, fresh_ts,
         fresh_unactionable) = fresh

        if refusal:
            # Refuse outright and send a fresh alert instead. Do not silently
            # re-propose against an alert whose context the reader no longer
            # has.
            send_reply(notify.escape_md_v2(
                f"Refusing to act on that button: {refusal}. "
                "Fresh state follows."), label, user)
            if fresh_d.trigger is not None and not fresh_unactionable:
                send_trip(fresh_state, fresh_d, fresh_prices, fresh_ts)
            else:
                send_reply(notify.escape_md_v2(
                    "Nothing is tripped right now. No action required."),
                    label, user)
            return

        if fresh_d.trigger is None:
            send_reply(notify.escape_md_v2(
                "No longer tripped, nothing to do -- the position is back "
                "inside both bands."), label, user)
            return
        if fresh_unactionable:
            send_reply(notify.escape_md_v2(
                "The trip is real but unactionable: base_capital exceeds "
                "NLV, so acting would add exposure the account cannot "
                "carry. Fix the sizing input first."), label, user)
            return

        held = {t: leg["shares"] for t, leg in fresh_details.items()}
        fresh_proposal = orders.build_proposal(
            fresh_state, fresh_d, params, fresh_prices, held, pair,
            marketable_offset)
        new_pid = proposal_id(fresh_ts)

        parts = []
        if orig_age > poll_seconds:
            # The tapper is looking at old numbers. Show the drift explicitly
            # ABOVE the fresh ticket -- old price, new price, share delta.
            old_state = state_from_row(orig, pair)
            old_d = decision.evaluate(old_state, params)
            old_marks = {
                pair["leveraged_ticker"]: orig.get("short_price"),
                pair["underlying_ticker"]: orig.get("long_price"),
            }
            old_held = {
                pair["leveraged_ticker"]: orig.get("short_shares"),
                pair["underlying_ticker"]: orig.get("long_shares"),
            }
            old_proposal = (orders.build_proposal(
                old_state, old_d, params, old_marks, old_held, pair,
                marketable_offset) if old_d.trigger else None)
            parts.append(notify.escape_md_v2(
                f"Marks moved since the alert you tapped "
                f"({orig_age / 60:.0f} min ago):"))
            parts.append(notify.fenced_block(
                drift_block(orig, old_proposal, fresh_proposal,
                            fresh_prices, pair)))

        parts.append(notify.escape_md_v2(
            f"Fresh proposal for {fresh_d.trigger}. Informational: the "
            "executor dispatches on trip alerts itself (spec 009); there is "
            "nothing to confirm here."))
        parts.append(notify.fenced_block(orders.format_proposal(fresh_proposal)))

        # No Confirm/Cancel keyboard: a second human-triggered path into
        # dispatch is exactly the interleaving spec 009 declines to have.
        delivered, error, message_id = notify.send_text(
            token, chat_id, "\n\n".join(parts))
        if not delivered:
            log.warning("proposal %s failed to send: %s", new_pid, error)
        events.log_command({"from_user_id": user,
                            "command": f"proposal:{new_pid}",
                            "authorized": True, "delivered": delivered})

        # Strip any lingering pre-009 proposal's buttons so an old Confirm
        # cannot sit half-alive under a retired flow.
        previous = approval_st.get("active_proposal")
        if previous and previous.get("message_id"):
            notify.remove_keyboard(token, chat_id, previous["message_id"])
        approval_st["active_proposal"] = None
        approval.save_state(approval_p, approval_st)

    def handle_confirm(intent):
        """Retired. Spec 009 made the executor autonomous -- dispatch is
        coupled to trip alerts -- so a Confirm tap must not open a second
        path into the ledger or the broker. Old messages keep their buttons;
        this answers them honestly and cleans up any pre-009 active
        proposal."""
        pid = intent.get("proposal_id")
        user = intent.get("from_user_id")
        label = f"reply:confirm:{pid}"
        active = approval_st.get("active_proposal")
        if active and active.get("proposal_id") == pid:
            if active.get("message_id"):
                notify.remove_keyboard(token, chat_id, active["message_id"])
            approval_st["active_proposal"] = None
            approval.save_state(approval_p, approval_st)
        send_reply(notify.escape_md_v2(
            "Confirm is retired: the executor dispatches on trip alerts "
            "itself (spec 009), behind the dry-run and enable rails. This "
            "tap recorded nothing and submitted nothing."), label, user)

    def handle_cancel(intent):
        pid = intent.get("proposal_id")
        user = intent.get("from_user_id")
        label = f"reply:cancel:{pid}"
        active = approval_st.get("active_proposal")
        if active and active.get("proposal_id") == pid:
            if active.get("message_id"):
                notify.remove_keyboard(token, chat_id, active["message_id"])
            approval_st["active_proposal"] = None
            approval.save_state(approval_p, approval_st)
            send_reply(notify.escape_md_v2("Cancelled -- nothing done."),
                       label, user)
        else:
            send_reply(notify.escape_md_v2(
                "Nothing to cancel -- that proposal is not active."),
                label, user)

    def process_intents():
        """Consume new intents.jsonl lines. Ids are marked consumed BEFORE
        handling (double taps, two devices, and Telegram retries all happen,
        and Restart=always makes a restart between tap and confirm ordinary);
        a consumed id gets an 'already handled' reply, not silence."""
        if not (token and chat_id):
            return
        records = approval.read_intents(intents_p)
        cursor = min(approval_st.get("intents_processed", 0), len(records))
        for record in records[cursor:]:
            cursor += 1
            approval_st["intents_processed"] = cursor
            approval.save_state(approval_p, approval_st)

            intent_ref = approval.intent_id(record)
            if intent_ref in seen:
                send_reply(notify.escape_md_v2(
                    "Already handled -- that button press was processed once "
                    "and will not run twice."),
                    f"reply:duplicate:{intent_ref}",
                    record.get("from_user_id"))
                continue
            seen.add(intent_ref)
            approval.save_seen(seen_p, seen)

            try:
                kind = record.get("kind")
                if kind == "request":
                    handle_request(record)
                elif kind == "confirm":
                    handle_confirm(record)
                elif kind == "cancel":
                    handle_cancel(record)
                else:
                    log.warning("unknown intent kind %r -- skipping", kind)
            except Exception:
                # One bad intent must not take the monitor down or wedge the
                # cursor on itself forever.
                log.exception("intent %s failed", intent_ref)

    def sleep_with_intent_checks(seconds):
        """The poll wait, sliced so a button press is answered in ~10 seconds
        instead of at the end of a fifteen-minute sleep. ib.sleep for EVERY
        slice: time.sleep blocks the asyncio loop (module note at the top),
        and slicing the wait is exactly the kind of change where it creeps
        back in."""
        process_intents()
        track_fills()
        check_unhedged_tickets()
        remaining = seconds
        while remaining > 0:
            step = min(INTENT_CHECK_SECONDS, remaining)
            ib.sleep(step)
            remaining -= step
            process_intents()
            track_fills()
            check_unhedged_tickets()

    peak_equity = monitor_state.load(path)

    # Health telemetry for /status, one file, rewritten every cycle. Kept as a
    # plain dict here; runtime_state owns the atomic write and never raises.
    rt_path = runtime_state.runtime_path()
    runtime = {
        "started_at": events.now_iso(),
        "last_check_ts": None,
        "last_connect_ts": None,
        "cycles_since_start": 0,
        "connected": False,
        "last_error": None,
        "execution": {"mode": gate.mode, "reason": gate.reason},
    }

    backoff = RECONNECT_BACKOFF_START
    ib = connect_with_backoff()
    runtime["last_connect_ts"] = events.now_iso()
    runtime["connected"] = True
    runtime_state.write(rt_path, runtime)
    # The reconcile is the spine (spec 009 section 4): on every connect,
    # before any decision, rebuild the executor's picture. False means the
    # order book is UNKNOWN -- dispatch stays refused until a retry succeeds.
    reconciled = reconcile_on_connect(ib, send, exec_state)
    execution_state.save(exec_state_path, exec_state)
    last_fill_count = -1

    # One INFO per start: pair, target, connection, gate verdict -- the four
    # facts a reader needs to trust (or distrust) everything that follows.
    # Suppressed inside the window because Restart=always turns a crash loop
    # into a message per crash; a suppressed start logs instead of sending.
    startup_now = datetime.datetime.now(ET)
    if alert_state.startup_due(alerts, startup_now):
        port = int(os.environ.get("IB_PORT", 4002))
        reconcile_note = ("clean" if reconciled else
                          "NOT confirmed -- dispatch refused until a read "
                          "succeeds")
        send("INFO", "startup", f"{PAIR_KEY} -- monitor started",
             f"target {target:,.2f} (from base_capital {base_capital:,.2f})\n"
             f"connected: port {port} "
             f"({'paper' if port == 4002 else 'LIVE'}), "
             f"reconcile {reconcile_note}\n"
             f"execution: {gate.mode} -- {gate.reason}")
        alerts = alert_state.record_startup(alerts, startup_now)
        alert_state.save(alert_path, alerts)
    else:
        log.info("startup alert suppressed -- one was sent within the last "
                 "%g minutes (crash-loop guard); state unchanged",
                 alert_state.STARTUP_SUPPRESS_MINUTES)
    checked_sanity = False
    was_connected = True
    checks_since_heartbeat = 0
    legs_missing = False

    while True:
        try:
            if not ib.isConnected():
                # No sleep before the first attempt: the DISCONNECT_ERRORS
                # handler below has already waited `backoff` by the time the
                # loop re-enters here, and the old code waited a second time.
                # The helper owns every wait after a FAILED attempt, and does
                # not give up. Reset to the floor once it returns a live handle.
                log.warning("not connected; reconnecting")
                was_connected = False
                runtime["connected"] = False
                runtime_state.write(rt_path, runtime)
                ib = connect_with_backoff(backoff)
                backoff = RECONNECT_BACKOFF_START
                runtime["last_connect_ts"] = events.now_iso()
                runtime["connected"] = True
                runtime_state.write(rt_path, runtime)
                # Every connect gets the full reconcile, not just the first:
                # the nightly session reset is precisely when a fill that
                # fired no events, a DAY expiry, or a hand-entered order
                # would be discovered.
                reconciled = reconcile_on_connect(ib, send, exec_state)
                execution_state.save(exec_state_path, exec_state)
                last_fill_count = -1
                continue

            if not was_connected:
                # One INFO on recovery. Without it a nightly restart looks
                # identical to a monitor that died and never came back.
                log.info("reconnected")
                send("INFO", "reconnected", f"{PAIR_KEY} -- reconnected",
                     "Gateway connection re-established; monitoring resumed.")
                was_connected = True

            if not reconciled:
                # The spine must not stay broken silently: retry the reads
                # each cycle until they answer. Decisions and alerts continue
                # meanwhile -- only dispatch is refused (section 9's split:
                # fail closed on the read, no sticky flag).
                reconciled = reconcile_on_connect(ib, send, exec_state)
                if reconciled:
                    execution_state.save(exec_state_path, exec_state)

            # Re-derived every cycle, deliberately. Never read from state.
            target = derive_target(base_capital, params, pair)

            # Resolve peak_equity BEFORE building the state, so the state that
            # reaches evaluate() carries the correct peak on the very first
            # cycle. Reading NLV first costs one extra account lookup and
            # avoids rebuilding a frozen dataclass.
            nlv = broker.net_liquidation(ib)
            if nlv is None:
                log.info("NLV unreadable; polling again in %.0fs", poll_seconds)
                sleep_with_intent_checks(poll_seconds)
                continue

            if peak_equity is None:
                peak_equity = nlv
                log.info("initializing peak_equity=%.2f", peak_equity)

            # peak_equity is an observation, always safe to write. Updated
            # before evaluate() so a fresh high cannot trip the stop on its own
            # cycle -- same ordering as band.py.
            peak_equity = max(peak_equity, nlv)
            monitor_state.save(path, peak_equity)

            state = broker.read_position(ib, PAIR_KEY, target, peak_equity)
            if state is None:
                # Missing or inverted legs. Keep polling -- the position may be
                # opened later, and a monitor that exits here needs a human to
                # notice and restart it.
                log.info("no valid position this cycle; polling again in %.0fs", poll_seconds)
                if not legs_missing:
                    # Transition only: a position closed overnight must not
                    # alert every cycle until someone reopens it.
                    legs_missing = True
                    send("WARNING", "leg_check", f"{PAIR_KEY} -- leg check failed",
                         "Could not read a valid two-leg position: a leg is "
                         "missing, flat, or wrongly signed. Not deciding until "
                         "both legs read correctly.")
                sleep_with_intent_checks(poll_seconds)
                continue

            if legs_missing:
                log.info("position readable again")
                send("INFO", "leg_check", f"{PAIR_KEY} -- position readable",
                     "Both legs read correctly again; band checks resumed.")
                legs_missing = False

            # Evaluated EVERY cycle, so suppression tracks the current account.
            # Console output stays once-only -- see log_startup_sanity.
            s = sanity_flags(base_capital, target, state)
            if not checked_sanity:
                log_startup_sanity(s)
                checked_sanity = True

            d = decision.evaluate(state, params)

            # A trip recommending ~$56k of new exposure directly below a warning
            # that the target is unachievable is two correct statements that
            # contradict each other. The trip is real, so it is still evaluated
            # and logged -- but it is not something you can act on, so it is not
            # sent. The sanity warning is what gets sent instead.
            unactionable = d.trigger is not None and s.capital_exceeds_nlv

            details = broker.leg_details(ib, PAIR_KEY)
            prices = {t: leg["price"] for t, leg in details.items() if leg["price"]}
            log_check(state, d, prices, unactionable=unactionable)
            check_ts = events.now_iso()
            events.log_check(check_record(state, d, unactionable,
                                          params, details, check_ts))
            checks_since_heartbeat += 1

            runtime["last_check_ts"] = check_ts
            runtime["cycles_since_start"] += 1
            runtime["connected"] = True
            # Re-evaluated each cycle, not frozen at startup: a HALT file
            # touched mid-incident must show up in /status within one poll.
            gate = execution.submission_gate()
            runtime["execution"] = {"mode": gate.mode, "reason": gate.reason}
            runtime_state.write(rt_path, runtime)

            now = datetime.datetime.now(ET)

            if s.capital_exceeds_nlv:
                # While suppressed the sanity warning OWNS the dedup slot. The
                # band trigger must not be written into it: last_trigger holds
                # one value, so alternating the two keys would make every cycle
                # look like a transition and re-send both forever. Lifting the
                # suppression then reads as a fresh trip, which is correct --
                # it is the first one that was ever actionable.
                send_it, _ = alert_state.should_send(
                    alerts, SANITY_TRIGGER_KEY, now, repeat_minutes)
                if send_it:
                    send("WARNING", "sanity",
                         f"{PAIR_KEY} -- base_capital exceeds NLV",
                         "\n".join(s.messages))
                    alerts = alert_state.record_sent(alerts, SANITY_TRIGGER_KEY, now)
            else:
                send_it, kind = alert_state.should_send(
                    alerts, d.trigger, now, repeat_minutes)
                if send_it and kind == "trip":
                    # send_trip owns the keyboard, the superseded-message
                    # bookkeeping, and the dedup recording. The executor is
                    # coupled to exactly this branch so tickets match alerts
                    # 1:1 (gate 13).
                    send_trip(state, d, prices, check_ts)
                    dispatch_on_trip(state, d, details, prices, check_ts)
                elif send_it and kind == "resolved":
                    send("INFO", "resolved", f"{PAIR_KEY} -- resolved",
                         "No band is tripped. The position is back inside both "
                         "bands and no action is required.")
                    alerts = alert_state.record_sent(alerts, None, now)
                    # Back inside both bands: every re-issue lineage ended.
                    execution.clear_issues(exec_state)
                    execution_state.save(exec_state_path, exec_state)

            if alert_state.heartbeat_due(alerts, now, heartbeat_hour, heartbeat_minute):
                send("INFO", "heartbeat", f"{PAIR_KEY} -- daily heartbeat",
                     heartbeat_message(state, d, checks_since_heartbeat))
                alerts = alert_state.record_heartbeat(alerts, now)
                checks_since_heartbeat = 0

            alert_state.save(alert_path, alerts)

            if d.terminal:
                # A drawdown stop is terminal in the backtest: the run ends. Here
                # nothing is executed, so the position is still open and will
                # re-trip every cycle. Keep monitoring -- going quiet after the
                # single most important alert would be the wrong failure.
                log.error(
                    "TERMINAL trigger with no executor: the position is still "
                    "open and this needs a human. Alerts repeat every %.0f min "
                    "until it clears or the process is stopped.", repeat_minutes,
                )

            sleep_with_intent_checks(poll_seconds)

        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
            broker.disconnect(ib)
            return
        except DISCONNECT_ERRORS as exc:
            # Disconnects are a primary code path, not an error path: IBKR
            # resets sessions nightly around 23:45 ET, and opening TWS on the
            # same credentials will kick this process off. Twenty-five lines of
            # asyncio traceback for an expected nightly event trains you to
            # ignore the log. One line, with the traceback available at DEBUG.
            log.warning(
                "disconnected: %s: %s -- reconnecting in %ds",
                type(exc).__name__, exc, backoff,
            )
            log.debug("disconnect traceback", exc_info=True)
            was_connected = False
            runtime["connected"] = False
            runtime["last_error"] = {
                "message": f"{type(exc).__name__}: {exc}",
                "ts": events.now_iso(),
            }
            runtime_state.write(rt_path, runtime)
            ib.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
        except Exception as exc:
            # Not a disconnect: a real bug keeps its traceback. Never exit.
            log.exception("cycle failed; retrying in %ds", backoff)
            runtime["last_error"] = {
                "message": f"{type(exc).__name__}: {exc}",
                "ts": events.now_iso(),
            }
            runtime_state.write(rt_path, runtime)
            ib.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run()


if __name__ == "__main__":
    main()
