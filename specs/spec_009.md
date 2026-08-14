# Spec 009 -- The execution layer

**Phase:** 3
**Depends on:** spec 008 (shipped); the execution rails and
`docs/execution-notes.md` (shipped 2026-08-13)
**Estimated:** one session of code, then a dry-run day, then arming

## Why

The approval loop proposes trades and a human retypes them into TWS. This spec
closes that gap on the **paper account**, so the wrinkles surface against
DUQ985373 rather than against a document.

Spec 008 section 8 named four gates before the placeholder became an order.
Three are being **dropped or deferred rather than met**, and recording that
plainly is the point of this section:

- **The margin investigation is cancelled** (2026-08-13, parked at
  `specs/parked/margin-investigation.md`). The pre-migration `checks.jsonl` was
  lost in the spec 007 state move; the event is un-forensicable, and blocking
  indefinitely on an unanswerable question was worse than proceeding on paper.
- **Phase 2 hand-execution is skipped.** The reconciliation data it would have
  produced comes from paper fills instead.
- **Fill-or-escalate still does not exist.** Section 7 gives it a bounded
  stand-in, not a policy.
- **Leg ordering is decided here rather than tested** (section 5).

### The one thing this spec is protecting

Paper money is not the risk. **Spec 008 wrote "paper to live is credentials
only, no logic change" -- so whatever ships here is the live executor**, and the
paper account's job is to produce the evidence for that decision.

The failure worth guarding against is therefore not a bad fill. It is a sloppy
execution path producing confident-looking numbers that a go-live decision then
rests on. That is why the rails below stay and the ceremony goes: **the rails
protect the evidence, not the balance.**

### What the reconnaissance changed

`docs/execution-notes.md`, sourced against ib_async 2.1.0, produced three
findings that shape the design. They are not details to be simplified away.

1. **The executor cannot be event-driven across restarts.** Fills occurring
   while disconnected are replayed as *data* on reconnect but their *events
   never fire* (`wrapper.execDetails` emits only on the `isLive` path), and
   `broker.connect()` builds a fresh `IB()` per connect, killing every prior
   subscription regardless. **Reconciliation on connect is the spine; events are
   a latency optimization layered on top.**
2. **A Read-Only rejection is invisible to naive tracking.** Error 321 is
   classified by ib_async as a *warning*: the Trade parks in `ValidationError`,
   which is in `ActiveStates`, so `isDone()` never returns True and
   `cancelledEvent` never fires. An executor awaiting completion hangs silently.
3. **`orderRef` is the ownership tag, `permId` the durable identity.**
   `clientId` is one `.env` edit from changing; `orderId` is meaningless across
   clients and sessions and is 0 for manual tickets.

A fourth finding is already load-bearing in shipped code: `connectAsync` skips
the open-orders sync when `readonly=True`, so `ib.openTrades()` is **empty on
our connections even when working orders exist**. `broker.open_orders` issues an
explicit `reqAllOpenOrders()` for exactly this reason. Do not "simplify" it back.

## Out of scope (do not build)

- **Live account.** Paper only, `IB_PORT=4002` throughout.
- **Multi-pair.** `PAIR_KEY = "TSLL"`.
- **Cancel/replace, order chasing, any TIF but DAY.** An unfilled DAY limit
  expires at the close and the band re-trips. That is the policy.
- **Automatic escalation of an unfilled order.** Section 7 alerts; it does not act.
- **Removing or loosening any rail.** The four-way default-off gate stays as
  shipped.
- Any change to `decision.py`, `band.py`, `engine.py`, or `config.py`.

---

## 1. Answer the three open questions first

`docs/execution-notes.md` ends with three questions needing a paper account and
one session. **Answer them before writing the submit path**; two can invalidate
parts of it.

1. Does `reqAllOpenOrders()` answer normally on a `readonly=True` connection
   with Gateway's Read-Only API enabled? If not, the orphan check is blind and
   the rail needs rethinking.
2. **Is `orderRef` echoed on open-order rows queried from a different clientId
   than the submitter?** The whole ownership model rests on this. If it is not,
   ownership falls back to `permId` matched against `orders.jsonl`, which is a
   materially different reconcile.
3. The execution-replay window across the nightly reset.

Record answers in `docs/execution-notes.md` under a dated "Verified on paper"
heading. An answer from observation replaces an answer from a forum post.

## 2. Coming out of Read-Only

Two deliberate changes, kept separate as spec 008 intended:

- Gateway's API setting.
- `broker.connect()` hardcodes `readonly=True`. It becomes
  `readonly=not execution_enabled`, derived from the same env the gate reads --
  never a bare literal `False`.

**Keep `readonly=True` until section 8 step 3.** Everything through the dry-run
day runs Read-Only, so an accidental submit is rejected at the wire rather than
filled. That is a free rail; keep it as long as possible.

When it comes off, finding 2 applies: **subscribe to `errorEvent` and
`statusEvent`, and treat `ValidationError` as a terminal refusal with a CRITICAL
alert.** Never wait on `isDone()` alone.

## 3. Submission

One function, called only from the `gate.mode == "live"` branch of
`execution.dispatch`. The CRITICAL refusal at the bottom of that function is the
line this spec replaces, and **it is the only line that changes there.**

Per leg: `LimitOrder(action, shares, limit)` with `tif="DAY"`,
`orderRef=f"shortlev:{proposal_id}:{ticker}"`, `outsideRth=False`.

**Write the `orders.jsonl` row at submission, before anything can interleave** --
status `submitted`, plus `permId`, `orderRef`, proposal id, and the full ticket.
`permId` may not be populated on the immediate return; if so, record it on the
first status callback and log that it arrived late. **A submitted order absent
from the ledger is unreconcilable**, which is the worst state this system can
reach -- worse than a bad fill, because it is invisible.

Both legs of a two-leg ticket go in the same cycle, back to back (section 5).

## 4. Reconciliation on connect

Per finding 1 this is the spine, not a recovery path.

On every successful connect, before any decision is evaluated:

1. `reqAllOpenOrders()` -- what is working now.
2. `ib.fills()` -- what filled, including replays that fired no events.
3. Positions, already read.
4. `orders.jsonl` -- what we believe we did.

Classify every non-terminal row:

| Case | Meaning | Action |
|---|---|---|
| In ledger, still working | Normal | Track it |
| In ledger, filled while away | Common after a restart | Record fill, INFO, alert |
| In ledger, gone and unfilled | DAY expiry at the close | Mark expired |
| In ledger, partially filled | Section 6 | Record, alert |
| **Working, not in ledger** | Orphan | Existing rail: flag, block, alert |
| **Filled, not in ledger** | Something traded with no record | CRITICAL, block, human |

The last row matters most and is precisely what an event-driven design would
never see. Positions plus the ledger close the gap that ambiguous exec replay
leaves open.

State persists to `/var/lib/short-lev/execution_state.json` -- outside the repo,
per spec 007.

## 5. Leg ordering: do not sequence

**Decided, not tested.** Both legs go in the same cycle, back to back.

Sequencing does not solve the problem it appears to. The risk in a two-leg
rebalance is the *fill* gap, not the *send* gap: sending the short first and the
long two seconds later still leaves the position unhedged for however long the
second leg takes to fill, which is unbounded for a resting limit. Sequencing
adds a knob, a justification, and a test matrix, and removes nothing.

What bounds the exposure is detection. **If one leg of a two-leg ticket is
complete and the other is not after `UNHEDGED_ALERT_MINUTES` (default 10), alert
CRITICAL** with both legs' state and the resulting net delta. That control works
regardless of send order.

Record in the Result whether the gap ever materially opened. That observation is
what a future sequencing decision should rest on, and generating it is exactly
what paper running is for.

## 6. Partial fills

Status stays `Submitted` while partially filled; one `fillEvent` per execution;
`trade.fills` is the number to trust, because status callbacks may be duplicated
or dropped (both documented).

- **Drive all accounting off executions**, never off counting status callbacks.
- A partial on a one-leg drift correction is just a smaller correction. Next
  cycle re-evaluates and re-trips or does not. No special handling.
- A partial on a two-leg ticket is the unhedged case; section 5's timer applies.
- At the close IB cancels the remainder server-side (error 202 / `Cancelled`);
  fills already received stay ours. Reconcile to the cent as
  `sum(execution.shares x execution.price)` against the ticket row.

## 7. Unfilled orders

No escalation. A DAY limit that does not fill expires at the close, the band
re-trips next cycle, a fresh proposal is issued.

**One bounded stand-in:** if a proposal has been re-issued `N` times (default 3)
without filling, alert WARNING that the limit style may be wrong for this pair.
A nudge, not an action -- the cheapest possible substitute for the
fill-or-escalate policy this spec is skipping.

**The most valuable observation from the paper period is the fill rate of
passive boundary limits.** Only the foil-decay short leg is still passive;
long-short legs now price marketably at 25bp. If passive limits essentially
never fill, boundary pricing is wrong and belongs in the bin -- a finding worth
more than the executor itself.

## 8. Arming, in order

Each step is separately reversible. None may be skipped.

1. **Dry run, Read-Only, one full session.** `DRY_RUN=1`, Gateway Read-Only,
   `readonly=True`. Every trip produces a logged ticket and an alert; nothing is
   sent. Compare each against what you would have done by hand.
2. **Review `orders.jsonl` against the alerts.** Share counts, limits, and sides
   must agree, and the reconcile must run clean across at least one nightly
   Gateway restart.
3. **Read-Only off**, both places. `DRY_RUN` stays 1. Confirm the reconcile still
   runs and the orphan check still answers.
4. **Arm.** `DRY_RUN=0`, `EXECUTION_ENABLED=1`. Watch the first submission on VNC.
5. **Reduce `MAX_ORDER_MULTIPLE`** for the first armed day so the first real
   order is small.

At every step `touch /var/lib/short-lev/HALT` stops submission without stopping
the monitor.

## 9. Fail closed on an unknown orphan state

`monitor.orphan_scan` currently fails **open**: a failed `reqAllOpenOrders()`
logs ERROR and permits submission. The stated reasoning -- that flagging on
transient errors trains reflexive flag-clearing -- is a real concern but
conflates two things.

**Split them.** A failed query refuses submission *for that cycle* without
writing the persistent flag file. Fail-closed on the read, no sticky state, no
reflexive-clearing habit. A missed cycle costs one poll interval; submitting
into an unknown order book is unbounded.

---

## Session gate

Offline unless marked live.

1. **The three open questions are answered** and recorded, dated, in
   `docs/execution-notes.md`. (live)
2. **`ValidationError` is terminal.** Synthetic status; confirm refuse-and-alert
   and that nothing waits on `isDone()`.
3. **Reconcile classifies all six cases** in section 4, from synthetic ledger
   and query fixtures.
4. **Filled-not-in-ledger blocks submission** and alerts CRITICAL.
5. **The ledger write precedes anything interleaving.** Inspect the path; assert
   the write happens before the return.
6. **`orderRef` is set on every order** and carries the proposal id.
7. **The unhedged timer fires** at `UNHEDGED_ALERT_MINUTES` with one leg
   complete. Synthetic.
8. **Partial-fill accounting derives from `trade.fills`**, not status callbacks.
9. **A failed open-order query refuses the cycle** and writes no flag file.
10. **All four rails still default off.** `submission_gate()` in a clean env
    returns `dry_run`; each rail independently blocks.
11. **The clamp refuses in dry run too.**
12. **HALT stops submission mid-session** without stopping the monitor. (live)
13. **A full dry-run session** produces tickets matching alerts, across a
    nightly Gateway restart. (live)
14. **First armed submission** fills or rests as intended, appears in
    `orders.jsonl` with its `permId`, and reconciles on the next connect. (live)

## Design decisions for review

- **`MAX_ORDER_MULTIPLE = 0.5` of target per leg.** A foil decay reset from a
  wide drift could legitimately exceed this and be refused. Correct, or too tight?
- **`UNHEDGED_ALERT_MINUTES = 10`.** A guess.
- **Re-issue count of 3** before the limit-style nudge. A guess.
- **Whether the executor should act on `drawdown stop` at all.** It is terminal
  and closes both legs. Automating the largest action in the system is
  defensible on paper; live it is a separate decision and should be argued
  explicitly rather than inherited from this spec.

---

## Result

**Session of 2026-08-14 (the submit path, section 3).** Prior sessions shipped
the rails, the reconcile spine, fill accounting, and the unhedged timer; this
session replaced the CRITICAL refusal in dispatch's live branch with
`execution._submit` -- per tradeable leg: the orders.jsonl "submitted" row
written BEFORE the wire call, then `placeOrder` with a DAY limit,
`outsideRth=False`, and `orderRef shortlev:<proposal_id>:<ticker>`. Both legs
go in one pass, back to back, no sleep (section 5 as decided). `statusEvent`
and `errorEvent` are subscribed at submission; ValidationError/Inactive are
terminal refusals (broker_refused row + CRITICAL alert, never a wait on a
done state); a permId absent at placeOrder-return is recorded from the first
status callback and logged as late. The live branch grew two guards of its
own: no proposal_id and no connected handle both refuse. Also added: a deduped
startup alert (one INFO per start -- pair, target, connection, gate verdict --
suppressed 15 minutes as a crash-loop guard).

**Section 1 was skipped by deliberate decision, not met.** The probe was not
run; all three open questions in `docs/execution-notes.md` remain UNANSWERED
and are recorded there as deferred. Question 2 (is orderRef echoed across
clientIds?) will be answered by the first real fill instead -- the classifier's
permId fallback already tolerates the pessimistic outcome. Gate 1 is therefore
not passed and will not be; treat the reconnaissance answers as provisional
until paper observation contradicts or confirms them.

**Gate status (offline items, all passing in `scripts/verify_execution.py`
and `scripts/verify_monitor.py`):**

- Gate 2 (ValidationError terminal): checks p and v. v drives the actual
  submit path with a synthetic 321 sequence; nothing in src waits on a done
  state or filledEvent (source-scanned).
- Gate 3 (six-case classify): check q. Gate 4 (filled-not-in-ledger blocks):
  check r. Gate 7 (unhedged timer): check s. Gate 8 (fills-derived
  accounting): check t. Gate 9 (failed query refuses cycle, no flag):
  checks o and u.
- Gate 5 (ledger write precedes the wire): check l, asserted on the code path
  -- the stub broker reads the ledger at placeOrder time.
- Gate 6 (orderRef on every order, carrying the proposal id): check l, on
  both the ledger row and the wire order.
- Gate 10 (four rails default off): checks e-i. Gate 11 (clamp refuses in dry
  run): check k.

**Still open, live-only:** gates 12 (HALT mid-session), 13 (full dry-run day
across a Gateway restart), 14 (first armed submission). The arming ladder in
section 8 is untouched: Read-Only stays on both places until step 3, DRY_RUN
stays 1 until step 4.

**Deviations from the spec text:** none in behavior; two interpretations worth
recording. The "submitted" ledger record is one row per tradeable leg (the
shape `fold_ledger` folds), not an additional proposal-level row. And the live
path sends a WARNING "SUBMITTED" alert mirroring the dry-run's as-if alert,
so the message flow stays rehearsed and step 4's "watch the first submission"
has something to watch. Contracts are `Stock(ticker, "SMART", "USD")` with no
qualifyContracts round-trip -- a bad symbol surfaces through the
ValidationError path, which is handled and tested.