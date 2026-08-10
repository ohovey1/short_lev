"""The monitor loop. Reads one pair, decides, prints. Never trades.

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

  - Submit orders. Out of scope in spec 004, not behind a flag. Gateway should
    also be running in Read-Only API mode.

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
import logging
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

import alert_state
import broker
import config
import decision
import events
import monitor_state
import notify

load_dotenv()

log = logging.getLogger("monitor")

PAIR_KEY = "TSLL"

# One tolerance for all three startup comparisons. These WARN and never refuse:
# an operator who knows the position is mid-adjustment still needs the monitor
# running.
SANITY_TOLERANCE = 0.10

RECONNECT_BACKOFF_START = 10
RECONNECT_BACKOFF_MAX = 300

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

    Interpreted in the machine's local timezone. The deploy target runs on ET;
    a box on another zone should set the clock or the hour accordingly, which
    is noted in .env.example rather than solved with a tz dependency.
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


# Severity per event, straight from the spec's table. Trips are WARNING; the
# two that mean the position is already in trouble are CRITICAL.
# The dedup slot holds one value. While a trip is suppressed as unactionable
# this key occupies it, so the sanity warning repeats on the normal timer
# instead of the two keys alternating and re-sending every cycle.
SANITY_TRIGGER_KEY = "sanity: base_capital exceeds NLV"

TRIGGER_SEVERITY = {
    "drawdown stop": "CRITICAL",
    "margin de-risk": "CRITICAL",
    "foil decay band": "WARNING",
    "long-short band": "WARNING",
}


def trip_message(state, d, prices):
    """The body of a trip alert: current state, then both trade lines.

    Actionable without opening TWS -- that is the whole requirement.
    """
    lines = [
        f"short {state.short_notional:,.2f} / target {state.target:,.2f}",
        f"long {state.long_notional:,.2f} | net delta {d.net_delta:+,.2f}",
        f"cushion {d.margin_cushion:+,.2f} (equity {state.account_equity:,.2f})",
        "",
    ]
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
        f"monitoring {PAIR_KEY}, {checks_since} checks since last heartbeat",
        f"short {state.short_notional:,.2f} / target {state.target:,.2f}",
        f"long {state.long_notional:,.2f} | net delta {d.net_delta:+,.2f}",
        f"cushion {d.margin_cushion:+,.2f} (equity {state.account_equity:,.2f})",
        f"state: {d.trigger or 'no trigger'}",
    ])


def check_record(state, d, unactionable):
    """The checks.jsonl row. One line per check, tripped or not."""
    pair = config.PAIRS[PAIR_KEY]
    return {
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
    }


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

    log.info(
        "alerts: telegram=%s repeat=%.0fmin heartbeat=%02d:%02d events=%s "
        "alert_state=%s",
        "configured" if (token and chat_id) else "NOT configured (no-op)",
        repeat_minutes, heartbeat_hour, heartbeat_minute,
        events.event_log_dir(), alert_path,
    )

    def send(severity, kind, title, body):
        """Hand a decision to the sink. Never raises."""
        try:
            notify.notify(token, chat_id, severity, kind, title, body)
        except Exception as exc:
            # notify() already catches its own failures; this is the belt to
            # that braces. A broken sink must never stop the monitor.
            log.warning("alert dispatch failed (non-fatal): %s: %s",
                        type(exc).__name__, exc)

    peak_equity = monitor_state.load(path)
    ib = broker.connect()
    checked_sanity = False
    backoff = RECONNECT_BACKOFF_START
    was_connected = True
    checks_since_heartbeat = 0
    legs_missing = False

    while True:
        try:
            if not ib.isConnected():
                log.warning("not connected; reconnecting in %ds", backoff)
                was_connected = False
                ib.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
                ib = broker.connect()
                backoff = RECONNECT_BACKOFF_START
                continue

            if not was_connected:
                # One INFO on recovery. Without it a nightly restart looks
                # identical to a monitor that died and never came back.
                log.info("reconnected")
                send("INFO", "reconnected", f"{PAIR_KEY} -- reconnected",
                     "Gateway connection re-established; monitoring resumed.")
                was_connected = True

            # Re-derived every cycle, deliberately. Never read from state.
            target = derive_target(base_capital, params, pair)

            # Resolve peak_equity BEFORE building the state, so the state that
            # reaches evaluate() carries the correct peak on the very first
            # cycle. Reading NLV first costs one extra account lookup and
            # avoids rebuilding a frozen dataclass.
            nlv = broker.net_liquidation(ib)
            if nlv is None:
                log.info("NLV unreadable; polling again in %.0fs", poll_seconds)
                ib.sleep(poll_seconds)
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
                ib.sleep(poll_seconds)
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

            prices = broker.leg_prices(ib, PAIR_KEY)
            log_check(state, d, prices, unactionable=unactionable)
            events.log_check(check_record(state, d, unactionable))
            checks_since_heartbeat += 1

            now = datetime.datetime.now()

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
                    send(TRIGGER_SEVERITY.get(d.trigger, "WARNING"), "trip",
                         f"{PAIR_KEY} -- {d.trigger}",
                         trip_message(state, d, prices))
                    alerts = alert_state.record_sent(alerts, d.trigger, now)
                elif send_it and kind == "resolved":
                    send("INFO", "resolved", f"{PAIR_KEY} -- resolved",
                         "No band is tripped. The position is back inside both "
                         "bands and no action is required.")
                    alerts = alert_state.record_sent(alerts, None, now)

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

            ib.sleep(poll_seconds)

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
            ib.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
        except Exception:
            # Not a disconnect: a real bug keeps its traceback. Never exit.
            log.exception("cycle failed; retrying in %ds", backoff)
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
