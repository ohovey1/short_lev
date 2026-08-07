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

import logging
import os
import sys

from dotenv import load_dotenv

import broker
import config
import decision
import monitor_state

load_dotenv()

log = logging.getLogger("monitor")

PAIR_KEY = "TSLL"

# One tolerance for all three startup comparisons. These WARN and never refuse:
# an operator who knows the position is mid-adjustment still needs the monitor
# running.
SANITY_TOLERANCE = 0.10

RECONNECT_BACKOFF_START = 10
RECONNECT_BACKOFF_MAX = 300


def _env_float(name, default):
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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

    The one formula, identical to band.py. base_capital is an allocation
    decision, never NLV -- deriving target from account value makes the
    reference drift with P&L, so drift never accumulates against it and the
    foil decay band silently never fires. STRATEGY_SPEC section 1 has the full
    argument; do not re-derive it here.
    """
    return (base_capital * params.capital_utilization) / pair["margin_multiplier"]


def startup_sanity_check(base_capital, target, state):
    """Warn loudly on configuration that does not describe this account.

    Three checks, all advisory. None refuses to run.
    """
    nlv = state.account_equity

    if base_capital > nlv * (1 + SANITY_TOLERANCE):
        log.warning(
            "SANITY: base_capital %.2f exceeds NLV %.2f by more than %.0f%%. The "
            "derived target %.2f is unachievable -- the position will be "
            "oversized, cushion thin, and margin de-risk will fire repeatedly "
            "against a reference that can never be met.",
            base_capital, nlv, SANITY_TOLERANCE * 100, target,
        )

    if nlv > base_capital * (1 + SANITY_TOLERANCE):
        log.warning(
            "SANITY: NLV %.2f exceeds base_capital %.2f by more than %.0f%% -- "
            "likely an undeployed deposit. Taking NO sizing action: raising "
            "base_capital is a human decision (STRATEGY_SPEC section 1). "
            "Derived target remains %.2f.",
            nlv, base_capital, SANITY_TOLERANCE * 100, target,
        )

    # Arithmetically identical to the foil decay band, but a different
    # diagnosis. The band says "time to trade"; this says "your config may not
    # describe this account". Logged separately so the first check of a session
    # does not read as a spurious trip.
    if abs(state.short_notional - target) > SANITY_TOLERANCE * target:
        log.warning(
            "SANITY: short notional %.2f is more than %.0f%% from derived target "
            "%.2f. Either base_capital is wrong or the position was opened at a "
            "different size. This is a config warning, not a band trip.",
            state.short_notional, SANITY_TOLERANCE * 100, target,
        )

    log.info(
        "note: the startup foil-decay verdict is vacuous -- drift from a "
        "just-derived target is zero by construction. Net delta and margin "
        "cushion are meaningful immediately; foil decay becomes meaningful "
        "once the bot has been running."
    )


def log_check(state, d, prices):
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

    pair = config.PAIRS[PAIR_KEY]
    log.warning("TRIP: %s%s", d.trigger, " (TERMINAL)" if d.terminal else "")
    _log_trade(pair["leveraged_ticker"], state.short_notional,
               d.new_short_notional, prices, short_leg=True)
    _log_trade(pair["underlying_ticker"], state.long_notional,
               d.new_long_notional, prices, short_leg=False)

    if d.trigger == "margin de-risk":
        log.warning(
            "  de-risk would ratchet target to %.2f. REPORTED ONLY -- not "
            "persisted. The monitor does not trade, and moving the reference "
            "for a trade that never happened would corrupt every subsequent "
            "band reading. Next cycle re-derives target from base_capital.",
            d.new_target,
        )


def _log_trade(ticker, current_notional, new_notional, prices, short_leg):
    """Log the specific trade, in shares -- 'buy 43 TSLA', not 'resize long leg'.

    The point is being able to act without opening TWS.
    """
    delta = new_notional - current_notional
    if abs(delta) < 0.005:
        log.warning("  %s: no change (%.2f)", ticker, current_notional)
        return

    # For the short leg, a notional INCREASE means shorting more, i.e. selling.
    if short_leg:
        action = "SELL (short more)" if delta > 0 else "BUY TO COVER"
    else:
        action = "BUY" if delta > 0 else "SELL"

    price = prices.get(ticker)
    if not price:
        log.warning(
            "  %s: %s $%.2f notional (%.2f -> %.2f). No price available, so no "
            "share count.",
            ticker, action, abs(delta), current_notional, new_notional,
        )
        return

    shares = abs(delta) / price
    log.warning(
        "  %s: %s %.0f shares @ ~%.2f = $%.2f (%.2f -> %.2f)",
        ticker, action, shares, price, abs(delta), current_notional, new_notional,
    )


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
        "/ margin_multiplier=%.2f  [pair=%s leverage=%s]",
        target, base_capital, params.capital_utilization,
        pair["margin_multiplier"], PAIR_KEY, pair["leverage"],
    )
    log.info(
        "bands: long_short=%.2f foil_decay=%.2f drawdown_stop=%s derisk=%s "
        "poll=%.0fs state=%s",
        params.long_short_band, params.foil_decay_band, params.drawdown_stop,
        params.margin_derisk, poll_seconds, path,
    )

    peak_equity = monitor_state.load(path)
    ib = broker.connect()
    checked_sanity = False
    backoff = RECONNECT_BACKOFF_START

    while True:
        try:
            if not ib.isConnected():
                log.warning("not connected; reconnecting in %ds", backoff)
                ib.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
                ib = broker.connect()
                backoff = RECONNECT_BACKOFF_START
                continue

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
                ib.sleep(poll_seconds)
                continue

            if not checked_sanity:
                startup_sanity_check(base_capital, target, state)
                checked_sanity = True

            d = decision.evaluate(state, params)
            log_check(state, d, broker.leg_prices(ib, PAIR_KEY))

            if d.terminal:
                # A drawdown stop is terminal in the backtest: the run ends. Here
                # nothing is executed, so the position is still open and will
                # re-trip every cycle. Keep monitoring -- going quiet after the
                # single most important alert would be the wrong failure -- but
                # say plainly that this needs a human, since dedup is spec 005.
                log.error(
                    "TERMINAL trigger with no executor: the position is still "
                    "open and this will re-alert every cycle until a human acts "
                    "or the process is stopped."
                )

            ib.sleep(poll_seconds)

        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
            broker.disconnect(ib)
            return
        except Exception:
            # Disconnects are a primary code path, not error handling: IBKR
            # resets sessions nightly around 23:45 ET, and opening TWS on the
            # same credentials will kick this process off. Never exit.
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
