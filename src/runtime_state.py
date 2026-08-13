"""Monitor runtime health: written once per cycle, read by the Telegram bot.

This file is what lets /status answer "should I trust anything else this bot
tells me" without the poller opening an IBKR connection of its own -- the
one-connection invariant from spec 008. It is deliberately NOT part of
monitor.json: peak_equity is load-bearing safety state whose loss silently
disables the drawdown stop, while this is disposable telemetry whose loss
costs one stale /status reply. Different consequences, different files.

Writes are atomic (temp file, then rename) so a poller reading mid-write can
never parse a truncated file, and they never raise -- same discipline as
events._append, and for the same reason: telemetry is not worth the loop.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_RUNTIME_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state", "runtime.json"
)

FIELDS = ("started_at", "last_check_ts", "last_connect_ts",
          "cycles_since_start", "connected", "last_error", "execution")


def runtime_path():
    return os.environ.get("RUNTIME_STATE_PATH") or DEFAULT_RUNTIME_PATH


def write(path, record):
    """Persist the runtime record atomically. Never raises."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("could not write runtime state to %s: %s: %s",
                    path, type(exc).__name__, exc)


def read(path):
    """Return the runtime record, or None when missing or malformed.

    None is not an error here: the reader's correct response to an absent or
    unreadable runtime file is "the monitor's health is unknown", which the
    bot renders as its Stale verdict rather than a crash.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("runtime state at %s is unreadable: %s: %s",
                    path, type(exc).__name__, exc)
        return None
