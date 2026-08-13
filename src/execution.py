"""The execution rails: everything that must refuse before anything is sent.

Spec 009 prep. There is NO order submission in this module or anywhere else in
the repo. dispatch() is the executor path spec 009 will finish, and today even
its fully-unlocked branch refuses -- the submit call gets added deliberately,
in its own spec, never as a side effect of these rails existing. What exists
now is every rail that is independent of submission:

  - size_clamp: a pure function refusing any leg above a configurable
    multiple of target notional. A band rebalance moves a fraction of a
    band-width; an order anywhere near target-sized means a corrupted input
    (a bad mark, a zeroed share count, a target derived from the wrong
    capital), and the clamp exists to stop exactly the ticket that looks
    plausible to a tired reviewer.
  - submission_gate: THE gate. Kill switch, enable flag, orphan flag, and
    dry run are all decided in this one function with one return, so there
    is exactly one line to audit before anything is ever sent.
  - the orphan flag: working orders found at connect that this process cannot
    account for. Restarts are routine under Restart=always, and orphaned
    working orders are the classic executor bug -- so their presence blocks
    submission until a human reconciles and deletes the flag file.
  - dry run: the executor path computes everything, logs the full ticket to
    orders.jsonl with status "dry_run", alerts as if it had submitted, and
    submits nothing. The point is that the only untested line left is the
    submit call itself.

Every default points at refusal: no env, no files, no configuration at all
means nothing can ever be sent.
"""

import json
import logging
import os
from collections import namedtuple

import events
import orders

log = logging.getLogger(__name__)

# Per-leg ceiling as a multiple of target notional. 0.5 is deliberate and
# strict: it refuses not only corrupted tickets but also a full drawdown-stop
# close (the short leg alone is ~1x target, the long ~2x). Flagged for spec
# 009 -- either risk reductions get their own bound or closes are chunked --
# but until that decision is made, refusing a close beats trusting a clamp
# that never fires.
MAX_ORDER_MULTIPLE = 0.5

# The kill switch. A file, not an env var, so a human mid-incident can stop
# submission with one `touch` and no restart.
DEFAULT_HALT_PATH = "/var/lib/short-lev/HALT"

DEFAULT_ORPHAN_FLAG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state",
    "orphan_orders.json"
)

# allow is True only when a live submission may proceed. mode is "live",
# "dry_run", or "blocked"; reason is human-readable and is logged on every
# refusal.
Gate = namedtuple("Gate", "allow mode reason")


def halt_path():
    return os.environ.get("EXECUTION_HALT_PATH") or DEFAULT_HALT_PATH


def orphan_flag_path():
    return os.environ.get("ORPHAN_FLAG_PATH") or DEFAULT_ORPHAN_FLAG_PATH


def max_order_multiple():
    raw = os.environ.get("MAX_ORDER_MULTIPLE")
    return float(raw) if raw not in (None, "") else MAX_ORDER_MULTIPLE


def size_clamp(proposal, target, max_multiple=MAX_ORDER_MULTIPLE):
    """Pure: (allow, reason) for a proposal against the per-leg ceiling.

    Refuses any tradeable leg whose order notional exceeds
    max_multiple x target -- strictly above; exactly at the ceiling passes.
    Pure and offline-testable on purpose: this is the rail that must be right,
    so it takes everything as arguments and reads nothing.
    """
    if not target or target <= 0:
        return False, f"clamp: target {target!r} is not a positive number"
    ceiling = max_multiple * target
    for leg in (proposal or {}).get("legs", []):
        if not leg.get("tradeable"):
            continue
        notional = leg.get("notional") or 0.0
        if notional > ceiling:
            return False, (
                f"clamp: {leg['ticker']} {leg['side']} notional "
                f"{notional:,.2f} exceeds {max_multiple:g} x target = "
                f"{ceiling:,.2f}")
    return True, f"clamp: all legs within {max_multiple:g} x target"


def submission_gate(env=None):
    """THE gate: (allow, mode, reason), decided in one place.

    Rails b (kill switch), c (orphan flag), and d (dry run) all live here so
    there is exactly one line to audit before anything is ever sent. Checked
    in severity order:

      1. The orphan flag file. Unaccounted-for working orders mean the world
         is not what the executor believes; nothing moves until a human
         reconciles and deletes the file.
      2. The HALT sentinel. A human said stop; even the dry-run pantomime
         stays quiet.
      3. DRY_RUN, default "1": anything but an explicit "0" is a dry run.
         Deliberately checked BEFORE the enable flag, so a box with nothing
         configured still exercises the full path safely.
      4. EXECUTION_ENABLED must be exactly "1".

    Going live therefore requires all four at once: no orphan flag, no HALT
    file, DRY_RUN=0, and EXECUTION_ENABLED=1.
    """
    env = os.environ if env is None else env
    orphan = orphan_flag_path()
    if os.path.exists(orphan):
        return Gate(False, "blocked",
                    f"orphan orders flagged -- reconcile, then delete "
                    f"{orphan} to clear")
    halt = halt_path()
    if os.path.exists(halt):
        return Gate(False, "blocked", f"kill switch: HALT file at {halt}")
    if (env.get("DRY_RUN") or "1").strip() != "0":
        return Gate(False, "dry_run",
                    "DRY_RUN is not '0' (the default): computing and logging "
                    "everything, submitting nothing")
    if (env.get("EXECUTION_ENABLED") or "").strip() != "1":
        return Gate(False, "blocked", "EXECUTION_ENABLED is not '1'")
    return Gate(True, "live", "all rails clear")


def flag_orphans(found):
    """Write the orphan flag file. Cleared only by a human deleting it.

    Persisted as a file rather than held in memory because Restart=always
    makes restarts routine -- and if the orphans fill or cancel while the
    monitor is down, the unknown fill has already happened and still needs a
    human before anything is submitted. Logs at ERROR if the write fails: a
    safety flag that silently failed to arm is worth more noise than lost
    telemetry elsewhere.
    """
    path = orphan_flag_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": events.now_iso(), "orders": found}, f, indent=2)
            f.write("\n")
    except Exception as exc:
        log.error("could not write orphan flag to %s: %s: %s -- submission "
                  "is NOT blocked by file, only by this process's logs",
                  path, type(exc).__name__, exc)
    return path


def _ticket_fields(proposal):
    proposal = proposal or {}
    return {
        "trigger": proposal.get("trigger"),
        "style": proposal.get("style"),
        "tif": proposal.get("tif"),
        "marks": proposal.get("marks"),
        "legs": proposal.get("legs"),
        "residual_total": proposal.get("residual_total"),
    }


def _refuse(proposal, reason, extra):
    # The reason is logged on EVERY refusal -- that is the rail's contract.
    log.warning("submission refused: %s", reason)
    events.log_order({**(extra or {}), "status": "refused", "reason": reason,
                      **_ticket_fields(proposal)})
    return "refused"


def dispatch(proposal, target, send=None, extra=None):
    """The executor path. Today it can refuse or dry-run; it cannot submit.

    Clamp first, then the gate: an oversized ticket is refused in every mode,
    dry run included, because a dry run that pantomimes a ticket the clamp
    would reject live is rehearsing the wrong play.

    send is the monitor's alert closure (severity, kind, title, body) -- this
    module owns no Telegram credentials. extra rides into the orders.jsonl
    row (proposal ids, who confirmed). Returns the status string written to
    orders.jsonl: "refused" or "dry_run".
    """
    allow, reason = size_clamp(proposal, target, max_order_multiple())
    if not allow:
        return _refuse(proposal, reason, extra)

    gate = submission_gate()
    if gate.mode == "blocked":
        return _refuse(proposal, gate.reason, extra)

    if gate.mode == "dry_run":
        events.log_order({**(extra or {}), "status": "dry_run",
                          "gate": gate.reason, **_ticket_fields(proposal)})
        log.info("dry run: ticket logged, nothing submitted (%s)", gate.reason)
        if send:
            # Alert exactly as a submission would, so the message flow is
            # rehearsed end to end -- with the one word that keeps a reader
            # from believing an order exists.
            send("WARNING", "execution",
                 "DRY RUN -- would have submitted",
                 orders.format_proposal(proposal))
        return "dry_run"

    # gate.mode == "live": every rail is clear, and this is the line where
    # spec 009's submit call goes. Until that spec lands, the honest behavior
    # is a loud refusal -- a quiet success here would mean an operator armed
    # a build with no executor and believed an order went out.
    log.critical("live submission requested but not implemented -- the "
                 "submit call is spec 009; refusing")
    return _refuse(proposal, "live submission not implemented (spec 009)",
                   extra)
