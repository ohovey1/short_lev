"""Alert dedup state: what was last alerted, when, and the last heartbeat date.

A tripped band stays tripped until a human acts. Without dedup that is an alert
every poll interval, and an alert every poll interval is an alert you mute.

The rules:
  - Send on TRANSITION into a trigger state.
  - Re-send every ALERT_REPEAT_MINUTES while it persists. A standing trip should
    nag, not vanish -- silence must never mean "still broken".
  - Send once on the transition back to none, at INFO ("resolved").
  - A CHANGE of trigger (foil decay -> margin de-risk) is a new transition and
    sends immediately, regardless of the repeat timer. It is new information.

Deliberately a SEPARATE file from monitor.json. Losing this file costs one
duplicate message; losing peak_equity silently disables the drawdown stop.
Different consequences, different files -- do not merge them.

For the same reason a malformed file here WARNS and resets, where
monitor_state.load raises SystemExit. Refusing to start because a dedup ledger
is corrupt would trade a live position's monitoring for one duplicate alert.
"""

import datetime
import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_ALERT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state", "alert_state.json"
)

EMPTY = {"last_trigger": None, "last_sent_ts": None, "last_heartbeat_date": None}


def state_path():
    return os.environ.get("ALERT_STATE_PATH") or DEFAULT_ALERT_STATE_PATH


def load(path):
    """Read the dedup state. A missing or malformed file resets, never raises."""
    if not os.path.exists(path):
        return dict(EMPTY)

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected an object, got {type(data).__name__}")
    except Exception as exc:
        log.warning(
            "alert state at %s is unreadable (%s: %s) -- resetting. Cost is at "
            "most one duplicate alert.",
            path, type(exc).__name__, exc,
        )
        return dict(EMPTY)

    merged = dict(EMPTY)
    merged.update({k: data.get(k) for k in EMPTY})
    return merged


def save(path, state):
    """Persist the dedup state. Never raises -- dedup is not worth the loop."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
    except Exception as exc:
        log.warning("could not save alert state to %s: %s: %s",
                    path, type(exc).__name__, exc)


def _parse(ts):
    try:
        return datetime.datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def should_send(state, trigger, now, repeat_minutes):
    """Decide whether this trigger warrants a message right now.

    Pure. Returns (send: bool, kind: str|None) where kind is "trip" for a
    trigger state and "resolved" for the return to none.

    `trigger` is decision.Decision.trigger: None, or one of the four strings.
    """
    last = state.get("last_trigger")

    if trigger is None:
        # Only the transition sends. Repeated non-trips are the normal case and
        # must stay silent, or the whole exercise is pointless.
        return (last is not None, "resolved" if last is not None else None)

    if last != trigger:
        # Transition in, or a change of trigger. New information either way.
        return True, "trip"

    last_sent = _parse(state.get("last_sent_ts"))
    if last_sent is None:
        return True, "trip"

    due = last_sent + datetime.timedelta(minutes=repeat_minutes)
    return (now >= due, "trip" if now >= due else None)


def record_sent(state, trigger, now):
    """State after a send. Pure: returns a new dict."""
    updated = dict(state)
    updated["last_trigger"] = trigger
    updated["last_sent_ts"] = now.isoformat(timespec="seconds")
    return updated


def heartbeat_due(state, now, hour, minute):
    """True when today's heartbeat has not been sent and the time has passed."""
    if now.hour < hour or (now.hour == hour and now.minute < minute):
        return False
    return state.get("last_heartbeat_date") != now.date().isoformat()


def record_heartbeat(state, now):
    updated = dict(state)
    updated["last_heartbeat_date"] = now.date().isoformat()
    return updated
