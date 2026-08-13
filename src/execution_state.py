"""Persisted executor state: in-flight tickets and re-issue counts.

Derived convenience, not truth -- orders.jsonl is the ledger, and the
reconcile-on-connect rebuilds the working picture from broker queries plus
that ledger every time. What lives here is only the part a restart would
otherwise lose entirely: the timestamp a two-leg ticket's first leg completed
(the unhedged timer's anchor) and how many consecutive times a trigger has
issued without filling (the limit-style nudge). Losing this file costs a late
CRITICAL and a reset counter, not safety state.

That difference in consequence is why, unlike monitor_state, a malformed file
does NOT refuse to start: it is moved aside to <path>.bad with an ERROR line
-- loud, evidence preserved, monitor still running. monitor_state refuses
because a silently reset peak disables the drawdown stop; a silently reset
ticket table is rebuilt by the next reconcile.

Shape, all plain JSON:

    {
      "tickets": {
        "<proposal_id>": {
          "trigger": ..., "ts": ...,
          "legs": {"<order_ref>": {"ticker": ..., "shares": ...,
                                    "complete": false}},
          "first_complete_ts": null,
          "unhedged_alerted": false
        }
      },
      "reissue": {"<trigger>": <consecutive issue count>}
    }
"""

import json
import logging
import os

log = logging.getLogger(__name__)

# Local-dev default. Deployment overrides EXECUTION_STATE_PATH to
# /var/lib/short-lev/execution_state.json (spec 009 section 4, per spec 007's
# outside-the-repo rule).
DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state",
    "execution_state.json"
)


def state_path():
    return os.environ.get("EXECUTION_STATE_PATH") or DEFAULT_STATE_PATH


def fresh():
    return {"tickets": {}, "reissue": {}}


def load(path):
    """Return the persisted state, or a fresh one when missing or malformed.

    Malformed is loud but not fatal: the bad file is renamed to <path>.bad so
    a human can inspect what happened, and the monitor keeps running on a
    fresh table the next reconcile will repopulate.
    """
    if not os.path.exists(path):
        log.info("no execution state at %s -- starting fresh", path)
        return fresh()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected an object, got {type(data).__name__}")
    except Exception as exc:
        bad = path + ".bad"
        try:
            os.replace(path, bad)
            moved = f"moved aside to {bad}"
        except OSError:
            moved = "could not be moved aside"
        log.error(
            "execution state at %s is malformed (%s: %s); %s. Starting fresh "
            "-- the unhedged anchor and re-issue counts are lost, the ledger "
            "and the next reconcile are not.",
            path, type(exc).__name__, exc, moved)
        return fresh()
    data.setdefault("tickets", {})
    data.setdefault("reissue", {})
    return data


def save(path, state):
    """Persist atomically (temp then rename). Never raises: a failed state
    write costs one timer anchor, not the monitoring loop -- but it is logged
    at ERROR, unlike disposable telemetry, because a lost anchor can delay a
    CRITICAL alert."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception as exc:
        log.error("could not write execution state to %s: %s: %s",
                  path, type(exc).__name__, exc)
