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
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# The monitor's clock. HEARTBEAT_HOUR's default is 09:45 because that is just
# after the market open, which is an ET fact -- not a fact about whatever zone
# the box happens to be set to.
ET = ZoneInfo("America/New_York")

# How long after HH:MM a heartbeat may still be sent. Comfortably more than the
# 15-minute poll interval, so a normal cycle always lands inside it.
HEARTBEAT_WINDOW_MINUTES = 30

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


def _match_awareness(parsed, now):
    """Make a stored timestamp comparable to `now`, in whichever direction.

    The monitor's `now` is ET-aware, but state files written before that switch
    hold naive timestamps, and comparing the two raises TypeError -- which the
    monitor's catch-all turns into a backoff loop that never clears until
    someone deletes the file. A dedup ledger must never be able to do that.

    Coerced against `now` rather than unconditionally to ET, so this is correct
    in both directions: an aware `now` pulls a naive timestamp up to ET, and a
    naive `now` (tests, or any caller that has not switched) drops an aware one
    back down. Assuming ET for a naive value is safe -- the box that wrote it ran
    ET, and an hour's error in a repeat timer costs one duplicate message.
    """
    if parsed is None:
        return None
    if now.tzinfo is not None and parsed.tzinfo is None:
        return parsed.replace(tzinfo=ET)
    if now.tzinfo is None and parsed.tzinfo is not None:
        return parsed.astimezone(ET).replace(tzinfo=None)
    return parsed


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

    last_sent = _match_awareness(_parse(state.get("last_sent_ts")), now)
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


def heartbeat_due(state, now, hour, minute, window_minutes=HEARTBEAT_WINDOW_MINUTES):
    """True only inside the window starting at HH:MM, at most once per day.

    The window is the point. "Past the hour and none sent today" is open-ended,
    so ANY restart later in the day sends one immediately -- observed at 15:24
    and again at 15:57 with the hour set to 09:45. A heartbeat arriving at an
    arbitrary time cannot do its job: you can only notice silence if you know
    when to expect noise.

    Past the window, skip today rather than fire late. A missing heartbeat is
    the signal; a late one just adds noise to it.
    """
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < due:
        return False
    if (now - due).total_seconds() > window_minutes * 60:
        return False
    return state.get("last_heartbeat_date") != now.date().isoformat()


def record_heartbeat(state, now):
    updated = dict(state)
    updated["last_heartbeat_date"] = now.date().isoformat()
    return updated
