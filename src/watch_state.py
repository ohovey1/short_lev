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

def _has_data(entry):
    return entry is not None and entry.get("shares_short") is not None

def _migrate_keys(data, pairs, key_by_leveraged):
    """
    Rekey state["pairs"] from leveraged to underlying.
    
    A key already under new scheme left alone. 
    
    A key matching neither kept as-is and logged rather than dropped. These
    are pairs who were removed or commented out (like TSLL, because double on
    the Tesla underlying). Silently discarding data is what I am trying to avoid.
    """
    old_pairs = data.get("pairs") or {}
    new_pairs = {}
    migrated, orphaned, collisions = [], [], []
    
    for key, entry in old_pairs.items():
        upper = key.upper()
        if upper in pairs:
            target = upper
        else:
            mapped = key_by_leveraged.get(upper)
            if mapped:
                target = mapped
                migrated.append(f"{upper} --> {mapped}")
            else:
                target = upper
                if upper not in orphaned:
                    orphaned.append(upper)
        if target not in new_pairs:
            new_pairs[target] = entry
        elif _has_data(entry) and not _has_data(new_pairs[target]):
            new_pairs[target] = entry
        elif _has_data(entry) and _has_data(new_pairs[target]):
            collisions.append(target)
    
    if collisions:
        raise SystemExit(
            f"Watch state migration: multiple keys with real data map to "
            f"the same target: {', '.join(sorted(set(collisions)))}. "
            "Refusing to guess which entry to keep -- "
            f"inspect the state file and remove one by hand."
        )
        
    if migrated:
        log.info("Watch state: rekeyed %d pair(s) to underlying tickers: %s",
                 len(migrated), ", ".join(migrated))
    if orphaned:
        log.warning(
            "Watch state: %s match no configured pair under either the old "
            "(leveraged) or new (underlying) key scheme -- kept as-is but "
            "they will never be checked. /untrack them, or restore their "
            "config.PAIRS entry.", ", ".join(orphaned)
        )
    data["pairs"] = new_pairs
    return data


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
    
    import config_detailed as config
    data = _migrate_keys(
        data, 
        config.PAIRS,
        {p["leveraged_ticker"].upper(): k for k, p in config.PAIRS.items()}
    )
    
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
                                     