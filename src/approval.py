"""Approval-loop state shared between the bot and the monitor.

The bot writes intents; the monitor consumes them. That file seam IS the
design: the poller holds no IBKR connection, so everything it wants to cause
is written as an intent and everything real happens in the one process that
can see the account. The button carries an intent, never a ticket -- the
ticket is re-derived fresh at press time by the monitor.

Three files, all under the state directory:

  intents.jsonl        appended by the bot, read by the monitor. One line per
                       button press: {kind, decision_id|proposal_id,
                       from_user_id, ts}.
  intents_seen.json    consumed intent ids. Persisted, not in-memory, because
                       Restart=always makes a monitor restart between tap and
                       confirm an ordinary event -- and a double tap must get
                       an "already handled" reply, never a second proposal.
  approval_state.json  the active proposal and the last trip alert message id
                       per trigger, so a superseding alert can edit the stale
                       keyboard off its predecessor.

None of this merges into alert_state.json or monitor.json. Same reasoning as
spec 006: losing these files costs one duplicate reply or one stale button;
losing peak_equity silently disables the drawdown stop. Different
consequences, different files.

Reads are tolerant (malformed lines skipped, malformed state reset) and writes
never raise beyond returning False -- a broken ledger here must degrade to a
missed button press, never take down the monitor.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state"
)

DEFAULT_INTENTS_PATH = os.path.join(_STATE_DIR, "intents.jsonl")
DEFAULT_INTENTS_SEEN_PATH = os.path.join(_STATE_DIR, "intents_seen.json")
DEFAULT_APPROVAL_STATE_PATH = os.path.join(_STATE_DIR, "approval_state.json")

# intents_processed is a line cursor into intents.jsonl: lines before it have
# been responded to. It exists so a double tap (two lines, one id) can get an
# "already handled" reply for the second line -- the seen set alone would
# filter the duplicate into silence.
EMPTY_STATE = {"active_proposal": None, "trip_messages": {},
               "intents_processed": 0}


def intents_path():
    return os.environ.get("INTENTS_PATH") or DEFAULT_INTENTS_PATH


def intents_seen_path():
    return os.environ.get("INTENTS_SEEN_PATH") or DEFAULT_INTENTS_SEEN_PATH


def approval_state_path():
    return os.environ.get("APPROVAL_STATE_PATH") or DEFAULT_APPROVAL_STATE_PATH


def intent_id(record):
    """The id an intent is deduplicated on: decision_id for a Rebalance
    request, proposal_id for a Confirm or Cancel."""
    return record.get("decision_id") or record.get("proposal_id")


def append_intent(path, record):
    """Append one intent line. Returns False rather than raising, so the bot
    can tell the presser their tap was lost instead of pretending otherwise."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return True
    except Exception as exc:
        log.warning("could not append intent to %s: %s: %s",
                    path, type(exc).__name__, exc)
        return False


def read_intents(path):
    """All intents on disk, malformed lines skipped. The file stays small --
    one line per button press on one pair -- so the monitor just rescans it
    and filters against the seen set rather than tracking offsets."""
    if not os.path.exists(path):
        return []
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and intent_id(record):
                    records.append(record)
    except Exception as exc:
        log.warning("could not read intents from %s: %s: %s",
                    path, type(exc).__name__, exc)
    return records


def load_seen(path):
    """The consumed-id set. Malformed resets: the cost is one duplicate
    'already handled' exchange, not a duplicate order row -- the terminal step
    re-checks the active proposal before writing anything."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("consumed", []))
    except Exception as exc:
        log.warning("intents_seen at %s is unreadable (%s: %s) -- resetting",
                    path, type(exc).__name__, exc)
        return set()


def save_seen(path, seen):
    """Persist the consumed-id set. Never raises."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"consumed": sorted(seen)}, f, indent=2)
            f.write("\n")
    except Exception as exc:
        log.warning("could not save intents_seen to %s: %s: %s",
                    path, type(exc).__name__, exc)


def load_state(path):
    """The approval state (active proposal, trip message ids). Malformed
    resets -- a stale keyboard that cannot be stripped is annoying, not
    dangerous, and refusing to start over it would be the wrong trade."""
    if not os.path.exists(path):
        return json.loads(json.dumps(EMPTY_STATE))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected an object, got {type(data).__name__}")
    except Exception as exc:
        log.warning("approval state at %s is unreadable (%s: %s) -- resetting",
                    path, type(exc).__name__, exc)
        return json.loads(json.dumps(EMPTY_STATE))

    merged = json.loads(json.dumps(EMPTY_STATE))
    for key in merged:
        if key in data:
            merged[key] = data[key]
    return merged


def save_state(path, state):
    """Persist the approval state. Never raises."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
    except Exception as exc:
        log.warning("could not save approval state to %s: %s: %s",
                    path, type(exc).__name__, exc)
