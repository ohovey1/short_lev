"""
Persisted watch loop state: shares counts & base capital per pair, plus the last
alert sent per pair.

base_capital can't be rebuilt or recomputed. computed once with /setshares for
pair. snap of where wanted target.

target not persisted in case config changes.

last_alert_foil, last_alert_long_short, last_alert_ts are state, not decisions.
"""
import datetime
import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state", "watch.json"
)

def state_path():
    return os.environ.get("WATCH_STATE_PATH") or DEFAULT_STATE_PATH

def load(path):
    if not os.path.exists(path):
        log.info("no state file at %s -- first run", path)
        return {"pairs": {}, "last_morning_date": None, "last_eod_date":  None}
    
    with open(path) as f:
        raw = f.read()
        
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("pairs", {}), dict):
            raise ValueError("Expected a JSON object with a 'pairs' object,")
    except (ValueError, TypeError) as e:
        raise SystemExit(
            f"state file {path} is malformed ({e}). Refusing to start. Silently"
            " reinitializing would drop every tracked pai'rs info with nothing"
            " logged. Inspect file, then restart. Contents: {raw[:200]!r}"
        )
    
    data.setdefault("pairs", {})
    data.setdefault("last_morning_date", None)
    data.setdefault("last_eod_date", None)
    log.info("restored watch state from %s: %d pair(s)", path, len(data["pairs"]))
    return data

def save(path, state):
    """Writes state. Creates dir on first write."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
        
    payload = dict(state)
    payload["updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
        
def pair_entry(state, pair_key):
    return state["pairs"].setdefault(pair_key, {
                "shares_short": None,
                "shares_long": None,
                "base_capital": None,
                "last_alert_foil": None,
                "last_alert_long_short": None,
                "last_alert_ts": None,
        })
                                     