"""Append-only structured event log. Two JSONL files, one line per event.

Not a database. hype_arb earns SQLite because it queries across trades; this
produces one append-only stream from one pair, read by pandas when someone
wants to look at it. `pd.read_json(path, lines=True)` is the whole reader.

  checks.jsonl  one line per check, tripped or NOT
  alerts.jsonl  one line per send attempt, delivered or NOT

Both halves of those "or not"s are the point. Non-trips are the raw material
for the intraday-cadence question deferred in the ROADMAP -- you cannot
calibrate a poll interval from the trips alone. Failed sends are how you find
out the bot went silent for a reason other than nothing happening.

Writes must never crash the monitor. A full disk, a permissions error, or a
read-only mount is a reason to lose telemetry, not a reason to stop watching a
live position. Every failure is caught and logged.
"""

import datetime
import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_EVENT_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "events"
)

CHECKS_FILE = "checks.jsonl"
ALERTS_FILE = "alerts.jsonl"


def event_log_dir():
    return os.environ.get("EVENT_LOG_DIR") or DEFAULT_EVENT_LOG_DIR


def now_iso():
    """Local ISO-8601 with offset, seconds precision.

    Same convention monitor_state.save already writes. The offset is carried so
    a log written across a DST change is still unambiguous.
    """
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _append(filename, record):
    """Append one JSON object as a line. Never raises."""
    try:
        directory = event_log_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, filename), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        # Telemetry is not worth a live position. Log and continue.
        log.warning("could not write %s: %s: %s", filename, type(exc).__name__, exc)


def log_check(record):
    """One line per check. `ts` is filled in if the caller did not."""
    record.setdefault("ts", now_iso())
    _append(CHECKS_FILE, record)


def log_alert(record):
    """One line per send attempt, delivered or not."""
    record.setdefault("ts", now_iso())
    _append(ALERTS_FILE, record)
