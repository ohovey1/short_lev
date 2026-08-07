"""Persisted monitor state. Exactly one field: peak_equity.

peak_equity is the only thing the monitor cannot rebuild from configuration. It
is path-dependent -- the highest equity actually observed over the run -- and
the drawdown stop is wrong without it. So it is persisted.

target is NOT persisted, and this module has no field for it. target is derived
every cycle from base_capital via the one formula in STRATEGY_SPEC section 1:

    target = (base_capital * capital_utilization) / margin_multiplier

Writing it to a file would create a second source of truth that could silently
drift from config, which is exactly the failure the strategy spec's "there is
one formula" line guards against. A manual rebalance in TWS changes nothing
about target, and that is the point. If you find yourself wanting to add a
target field here, the answer is no -- re-derive it instead.

peak_equity is an observation of reality rather than a decision, so it is always
safe to write: max(peak_equity, account_equity) on every check.
"""

import datetime
import json
import logging
import os

log = logging.getLogger(__name__)

# Local-dev default. Deployment MUST override MONITOR_STATE_PATH to a path
# OUTSIDE the repo tree: a git pull or re-clone that wipes this file resets the
# peak and silently disables the drawdown stop, with nothing in the logs to say
# the safety net is gone. See docs/AUTOMATION.md.
DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state", "monitor.json"
)


def state_path():
    """Resolve the state file path from env, falling back to the local default."""
    return os.environ.get("MONITOR_STATE_PATH") or DEFAULT_STATE_PATH


def load(path):
    """Return persisted peak_equity, or None if no state file exists yet.

    A malformed file raises. It does NOT silently reinitialize: doing so would
    reset the peak to current equity and quietly suppress the drawdown stop,
    which is the one thing this file exists to prevent. Loud failure at startup
    is recoverable; a suppressed stop is not.
    """
    if not os.path.exists(path):
        log.info("no state file at %s -- first run", path)
        return None

    with open(path) as f:
        raw = f.read()

    try:
        data = json.loads(raw)
        peak = float(data["peak_equity"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit(
            f"state file {path} is malformed ({exc}). Refusing to start: "
            f"silently reinitializing would reset peak_equity and disable the "
            f"drawdown stop. Inspect the file, fix or delete it deliberately, "
            f"then restart. Contents: {raw[:200]!r}"
        )

    if peak <= 0:
        raise SystemExit(
            f"state file {path} has peak_equity={peak}, which cannot be right. "
            f"Refusing to start rather than running with a broken drawdown stop."
        )

    log.info("restored peak_equity=%.2f from %s", peak, path)
    return peak


def save(path, peak_equity):
    """Write peak_equity. Creates the parent directory on first write."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    payload = {
        "peak_equity": round(float(peak_equity), 2),
        "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
