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

import datetime
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

# The ownership tag. Every order this executor submits carries
# orderRef "shortlev:<proposal_id>:<ticker>"; anything bearing the prefix is
# ours and traceable to its orders.jsonl rows, anything without it is foreign
# (execution-notes section 2). clientId is deliberately NOT the discriminator
# -- it is one .env edit from changing.
ORDER_REF_PREFIX = "shortlev:"

# Ledger statuses that end a leg's lifecycle. "refused", "dry_run", and
# "placeholder" are proposal-level rows that never carry an order_ref, so they
# never enter the per-leg fold at all.
TERMINAL_STATUSES = ("filled", "expired", "cancelled", "broker_refused")

# ib_async parks a Read-Only or validation rejection (e.g. error 321) in
# ValidationError, which it keeps in ActiveStates: isDone() never returns True
# and cancelledEvent never fires, so an executor awaiting completion hangs
# forever. It is terminal HERE, whatever ib_async thinks. Inactive is IB's own
# "invalid or blocked" verdict and gets the same treatment.
TERMINAL_REFUSAL_STATUSES = ("ValidationError", "Inactive")

# Spec 009 section 5: if one leg of a two-leg ticket is complete and the other
# is not after this many minutes, alert CRITICAL. Detection bounds the fill
# gap; sequencing the sends would not. The default is a flagged guess.
UNHEDGED_ALERT_MINUTES = 10

# Spec 009 section 7: after this many consecutive re-issues of the same
# trigger without a fill, nudge WARNING that the limit style may be wrong for
# the pair. A nudge, not an action. The default is a flagged guess.
REISSUE_WARN_COUNT = 3

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


def unhedged_minutes():
    raw = os.environ.get("UNHEDGED_ALERT_MINUTES")
    return float(raw) if raw not in (None, "") else UNHEDGED_ALERT_MINUTES


def reissue_warn_count():
    raw = os.environ.get("REISSUE_WARN_COUNT")
    return int(raw) if raw not in (None, "") else REISSUE_WARN_COUNT


def execution_enabled(env=None):
    """Exactly one definition of "is execution enabled".

    Two callers: submission_gate (rail 4) and broker.connect, which derives
    its readonly flag as `not execution_enabled()` -- never a bare literal.
    One definition means the connection can never be writable while the gate
    still reads disabled, or vice versa.
    """
    env = os.environ if env is None else env
    return (env.get("EXECUTION_ENABLED") or "").strip() == "1"


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
    if not execution_enabled(env):
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


def order_ref(proposal_id, ticker):
    """The orderRef tag for one leg: shortlev:<proposal_id>:<ticker>."""
    return f"{ORDER_REF_PREFIX}{proposal_id}:{ticker}"


def is_ours(ref):
    """Ownership by orderRef prefix. Foreign until proven ours: a manual
    ticket's empty ref, None, and any other tool's tag all say foreign."""
    return bool(ref) and ref.startswith(ORDER_REF_PREFIX)


def is_terminal_refusal(status):
    """True when a status string means the broker refused and will never
    progress the order -- despite ib_async keeping it in ActiveStates. The
    submit path must treat these as done-and-failed; waiting on isDone()
    hangs forever (execution-notes section 4)."""
    return status in TERMINAL_REFUSAL_STATUSES


def fill_totals(fills):
    """(shares, notional) summed over plain fill dicts, notional to the cent.

    The one accounting primitive. Everything that counts shares or dollars
    derives from execution rows like these -- never from counting status
    callbacks, which IB documents as both duplicable and droppable.
    """
    shares = sum(f.get("shares") or 0 for f in fills)
    notional = sum((f.get("shares") or 0) * (f.get("price") or 0.0)
                   for f in fills)
    return shares, round(notional, 2)


def read_ledger(path=None):
    """All orders.jsonl rows, oldest first. Missing file is an empty ledger.

    Same defensive line-by-line read as monitor.find_check: one corrupt line
    (a crash mid-append) must not take the whole ledger with it.
    """
    if path is None:
        path = os.path.join(events.event_log_dir(), events.ORDERS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def fold_ledger(rows):
    """Fold append-only rows into one state per orderRef: {ref: leg dict}.

    The ledger never rewrites a line, so a leg's current state is the fold of
    every row bearing its order_ref: the "submitted" row carries the ticket,
    "fill" rows accumulate executions keyed by exec_id (the dedup key --
    replays and event/reconcile overlap both hand us the same execution
    twice), lifecycle rows move `status`, and perm_id sticks from whichever
    row first carried it. Rows without an order_ref (placeholder, dry_run,
    refused, foreign_fill) are proposal-level or foreign and never fold.
    """
    legs = {}
    for row in rows:
        ref = row.get("order_ref")
        if not ref:
            continue
        leg = legs.setdefault(ref, {
            "order_ref": ref, "perm_id": None, "proposal_id": None,
            "ticker": None, "side": None, "shares": None, "limit": None,
            "trigger": None, "status": None, "fills": {},
        })
        if row.get("perm_id"):
            leg["perm_id"] = row["perm_id"]
        status = row.get("status")
        if status == "submitted":
            for key in ("proposal_id", "ticker", "side", "shares", "limit",
                        "trigger"):
                if row.get(key) is not None:
                    leg[key] = row[key]
            leg["status"] = "submitted"
        elif status == "fill":
            exec_id = row.get("exec_id") or f"row-{len(leg['fills'])}"
            leg["fills"][exec_id] = {"shares": row.get("shares"),
                                     "price": row.get("price")}
        elif status == "partial" or status in TERMINAL_STATUSES:
            leg["status"] = status
    for leg in legs.values():
        shares, notional = fill_totals(leg["fills"].values())
        leg["filled_shares"] = shares
        leg["filled_notional"] = notional
    return legs


def classify(ledger_rows, open_rows, fill_rows):
    """Spec 009 section 4's table as a pure function. Returns findings.

    Inputs are plain data -- raw ledger rows, broker.open_orders dicts,
    broker.fills dicts -- so every case is testable from fixtures with no
    Gateway. The caller applies the actions; this only names what it sees.

    Ownership matching is orderRef-prefix first, perm_id-against-ledger as
    the fallback, so the answer to open question 2 (is orderRef echoed
    across clientIds?) adjusts which branch matches, not the shape.

    Each finding is {"case": <one of six>, ...context}:
      working            in ledger, still working -- track it
      filled_while_away  in ledger, gone, executions cover the size
      expired            in ledger, gone, nothing filled (DAY expiry -- or a
                         submit that died between ledger write and the wire,
                         which reconciles to the same "no order, no fill")
      partial            in ledger with fills short of the size; "working"
                         says whether the remainder is still live or was
                         cancelled at the close
      orphan_working     working order not in the ledger -- the existing rail
      orphan_filled      an execution not in the ledger: something traded
                         with no record. The worst state; CRITICAL.

    Fills already recorded in the ledger are excluded by exec_id, foreign
    ones via their "foreign_fill" rows -- otherwise every reconnect would
    re-flag the same event forever and train reflexive flag-clearing.
    """
    legs = fold_ledger(ledger_rows)
    recorded = {row["exec_id"] for row in ledger_rows if row.get("exec_id")}
    perm_to_ref = {leg["perm_id"]: ref for ref, leg in legs.items()
                   if leg["perm_id"]}

    open_by_ref, orphan_orders = {}, []
    for row in open_rows:
        ref = _match(row, legs, perm_to_ref)
        if ref:
            open_by_ref[ref] = row
        else:
            orphan_orders.append(row)

    new_fills, orphan_fills = match_fills(legs, recorded, fill_rows)

    findings = []
    for ref, leg in legs.items():
        unseen = new_fills.get(ref, [])
        if leg["status"] in TERMINAL_STATUSES and not unseen:
            continue
        working = ref in open_by_ref
        filled = leg["filled_shares"] + fill_totals(unseen)[0]
        total = leg["shares"] or 0
        if working:
            if filled > 0:
                findings.append({"case": "partial", "order_ref": ref,
                                 "leg": leg, "new_fills": unseen,
                                 "working": True, "filled_shares": filled,
                                 "total_shares": total})
            else:
                findings.append({"case": "working", "order_ref": ref,
                                 "leg": leg, "order": open_by_ref[ref]})
        elif total and filled >= total:
            findings.append({"case": "filled_while_away", "order_ref": ref,
                             "leg": leg, "new_fills": unseen})
        elif filled > 0:
            findings.append({"case": "partial", "order_ref": ref,
                             "leg": leg, "new_fills": unseen,
                             "working": False, "filled_shares": filled,
                             "total_shares": total})
        else:
            findings.append({"case": "expired", "order_ref": ref,
                             "leg": leg})

    findings.extend({"case": "orphan_working", "order": o}
                    for o in orphan_orders)
    findings.extend({"case": "orphan_filled", "fill": f}
                    for f in orphan_fills)
    return findings


def _match(row, legs, perm_to_ref):
    """One ownership rule for open-order rows and fill rows both: orderRef
    prefix first, perm_id against the ledger as the fallback. Returns the
    ledger orderRef, or None for foreign."""
    ref = row.get("order_ref")
    if is_ours(ref) and ref in legs:
        return ref
    return perm_to_ref.get(row.get("perm_id"))


def match_fills(legs, recorded_exec_ids, fill_rows):
    """Route fill rows to their ledger legs: ({ref: [fills]}, [foreign]).

    Shared by the connect-time classify and the monitor's intra-session fill
    tracker, so both apply the identical ownership rule. Rows whose exec_id
    the ledger already carries (foreign_fill rows included) are dropped --
    replays and event/reconcile overlap both deliver duplicates, and the
    dedup is what makes reprocessing harmless.
    """
    perm_to_ref = {leg["perm_id"]: ref for ref, leg in legs.items()
                   if leg["perm_id"]}
    new_by_ref, foreign = {}, []
    for row in fill_rows:
        if row.get("exec_id") in recorded_exec_ids:
            continue
        ref = _match(row, legs, perm_to_ref)
        if ref:
            new_by_ref.setdefault(ref, []).append(row)
        else:
            foreign.append(row)
    return new_by_ref, foreign


def _parse_ts(ts):
    """ISO timestamp -> aware datetime, or None. Naive values are read as
    local time -- events.now_iso always writes an offset, so naive only
    appears in hand-built fixtures."""
    try:
        parsed = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def check_unhedged(ticket, now, minutes):
    """True when a multi-leg ticket has been one-legged past the alert bound.

    Pure over the persisted ticket state: legs with their "complete" flags,
    the timestamp the first leg completed, and whether the alert already
    fired (it fires once per ticket -- the caller sets unhedged_alerted).
    Sequencing the sends would not bound this exposure; detection does
    (spec 009 section 5).
    """
    if ticket.get("unhedged_alerted"):
        return False
    legs = ticket.get("legs") or {}
    if len(legs) < 2:
        return False
    complete = sum(1 for leg in legs.values() if leg.get("complete"))
    if complete == 0 or complete == len(legs):
        return False
    first = _parse_ts(ticket.get("first_complete_ts"))
    if first is None:
        return False
    return (now - first).total_seconds() >= minutes * 60


def note_issue(state, trigger):
    """Count a proposal issue for a trigger; returns consecutive issues.

    The re-issue nudge (section 7) is `issues - 1 >= reissue_warn_count()`:
    the first issue is not a re-issue. Cleared by clear_issues on a fill or
    on the trip resolving.
    """
    counts = state.setdefault("reissue", {})
    counts[trigger] = counts.get(trigger, 0) + 1
    return counts[trigger]


def clear_issues(state, trigger=None):
    """Reset the re-issue count for one trigger, or all when trigger is None
    (the position came back inside both bands -- every lineage ended)."""
    if trigger is None:
        state["reissue"] = {}
    else:
        state.setdefault("reissue", {}).pop(trigger, None)


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


def dispatch(proposal, target, send=None, extra=None, ib=None):
    """The executor path. Today it can refuse or dry-run; it cannot submit.

    Clamp first, then the gate: an oversized ticket is refused in every mode,
    dry run included, because a dry run that pantomimes a ticket the clamp
    would reject live is rehearsing the wrong play.

    send is the monitor's alert closure (severity, kind, title, body) -- this
    module owns no Telegram credentials. extra rides into the orders.jsonl
    row (proposal ids, who confirmed). ib is the broker handle the live
    branch will need once spec 009's submit call lands there; every other
    branch ignores it. Returns the status string written to orders.jsonl:
    "refused" or "dry_run".
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
