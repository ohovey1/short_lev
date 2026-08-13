# Execution reconnaissance: ib_async order handling

Spec 009 prep, 2026-08-13. Findings for the executor design; no code in this
document. Everything below was checked against the installed ib_async 2.1.0
source (`ib_async/ib.py`, `ib_async/wrapper.py`, `ib_async/order.py` --
version confirmed at research time), the TWS API reference at
interactivebrokers.github.io, and the issue threads cited inline. Where a
claim rests on community reports rather than source or docs, it says so; where
the sources are ambiguous, the answer is "ambiguous", not a guess.

Summary of what changes the executor's shape, argued in the sections below:

1. **The executor must be reconciliation-first, not event-driven.** Fills that
   happen while disconnected are recovered as *data* on reconnect but their
   *events* never fire, and our reconnect discards the old `IB` handle anyway.
   Events are a latency optimization; `trade.fills`, `ib.fills()`, and
   positions are the truth.
2. **A Read-Only rejection is a trap for naive tracking.** It arrives as
   error 321, which ib_async classifies as a *warning*: the Trade is parked in
   `ValidationError`, which is an ACTIVE state -- `trade.isDone()` stays False
   forever and `cancelledEvent` never fires. An executor that awaits
   done/filled hangs silently. It must watch `errorEvent`/`statusEvent` and
   treat `ValidationError` as refuse-and-alert.
3. **Tag every order with `orderRef` and persist `permId` at submission.**
   orderRef is the discriminator, permId the durable per-order identity;
   clientId alone cannot distinguish our orders from manual tickets on a
   clientId-0 tool, and orderId is meaningless across clients.

## 1. Placing a limit order and tracking it to fill

**Objects.** `ib.placeOrder(contract, order)` returns a `Trade` immediately
("Place a new order or modify an existing order. Returns a Trade that is kept
live updated with status changes, fills, etc." -- `ib.py`). The order for us
is `LimitOrder(action, totalQuantity, lmtPrice)` from `ib_async.order`, with
`tif="DAY"` and `orderRef` set (section 2). The Trade carries `contract`,
`order`, `orderStatus`, `fills` (list of `Fill(contract, execution,
commissionReport, time)`), and `log` (a `TradeLogEntry` per transition,
including error text). Calling `placeOrder` again with the same `orderId`
modifies the working order rather than placing a new one.

**Events on the Trade** (from the `Trade` docstring): `statusEvent`,
`modifyEvent`, `fillEvent(trade, fill)`, `commissionReportEvent`,
`filledEvent` (complete fill only), `cancelEvent` (cancel requested),
`cancelledEvent` (cancel confirmed). The `IB` instance mirrors these
globally: `orderStatusEvent`, `execDetailsEvent`, `newOrderEvent`,
`orderModifyEvent`, `errorEvent`.

**Lifecycle states** (`OrderStatus` in `order.py`, meanings from the TWS API
order-submission page): `PendingSubmit` (sent, no confirmation yet -- this is
the state placeOrder assigns locally), `ApiPending`, `PreSubmitted` (accepted
by IB, not yet elected -- simulated/held orders), `Submitted` (working at the
destination), `PendingCancel`, `ApiCancelled`, `Cancelled`, `Filled`,
`Inactive` (invalid, or exchange closed, or blocked). ib_async adds
`ValidationError` and `ApiUpdate` and groups them: `DoneStates = {Filled,
Cancelled, ApiCancelled, Inactive}`; everything else, `ValidationError`
included, is in `ActiveStates`. `trade.isDone()` is membership in DoneStates.

**Tracking.** For our resting DAY limits the natural pattern is: keep the
Trade, react to `fillEvent` for accounting and `statusEvent` for transitions,
and treat `filledEvent`/`cancelledEvent` as terminal. Two caveats from the
TWS docs worth building around: "Typically there are duplicate orderStatus
messages with the same information" and "there are not guaranteed to be
orderStatus callbacks for every change in order status". Drive accounting off
executions (`fillEvent`, `trade.fills`), never off counting status callbacks.

## 2. Open orders on connect, and telling ours from foreign

**Query.** Three requests, different populations (TWS API open-orders page,
ib_async docstrings):

- `reqOpenOrders()` -- orders "submitted by the client application connected
  with the exact same client Id with which the order was sent".
- `reqAllOpenOrders()` -- one-shot snapshot of open orders from ALL API
  client ids AND tickets entered manually in TWS/Gateway. Manual tickets
  arrive with API orderId 0 (ib_async keys them by `permId` --
  `wrapper.orderKey`). The ib_async docstring adds: "the orders of other
  clients will not be kept in sync" -- it is a snapshot, which is exactly
  what the connect-time orphan check wants.
- `reqAutoOpenOrders(True)` -- clientId 0 only; binds future manual TWS
  orders. Not for us: the monitor deliberately does not own manual orders.

**A finding that matters for us today:** ib_async's `connectAsync` skips the
automatic open-orders sync entirely when `readonly=True` (source:
`if not readonly: reqs["open orders"] = ...`). Our `broker.connect()` passes
`readonly=True`, so `ib.openTrades()` is EMPTY on our connections even when
working orders exist. Any orphan check must issue an explicit
`reqAllOpenOrders()`; the cached accessors cannot be trusted under our
connect settings. (Whether Gateway's Read-Only mode answers
`reqAllOpenOrders` at all is a read and should be fine, but it costs nothing
to confirm on paper -- listed in open questions.)

**Ours vs not ours: orderRef is the discriminator, permId the identity,
clientId only a cross-check.** Reasons:

- `orderId` is a per-client counter -- meaningless across clients and
  sessions, and 0 for unbound manual tickets.
- `clientId` does say which API client placed an order, and manual TWS
  tickets carry clientId 0. But it is weak as an ownership tag: it is one
  .env edit away from changing, any other tool could reuse the number, and a
  clientId-0 tool becomes indistinguishable from hand-entered tickets.
- `orderRef` is a free-text tag set at submission, echoed back on open-order
  rows (`wrapper.openOrder` copies it), and empty on manual tickets unless a
  human deliberately fills the TWS "Order ref" field. Tag every order
  `shortlev:<proposal_id>`: anything bearing the prefix is ours and traceable
  to its orders.jsonl row; anything without it is foreign.
- `permId` is IB's stable server-side id for the order across sessions
  (ib_async falls back to `permId2Trade` to match executions). Persist it
  into orders.jsonl at submission time; it is how a row in our log is matched
  to what IBKR reports tomorrow.

One ambiguity, cheap to resolve: the TWS docs do not explicitly promise that
`orderRef` is populated on rows returned to a *different* clientId than the
submitter (the handler copies whatever arrives). Verify once on paper before
relying on it across clients; within one clientId it is documented behavior.

## 3. Disconnects and the nightly reset

**Working orders survive an API disconnect.** Once `Submitted`, native-type
orders rest at IB's servers, not in the API client. `disconnect()` does not
cancel them (ib_async discussion #126), and IBKR's own reset guidance says
"existing orders (native types) will operate normally although execution
reports and simulated orders will be delayed until the reset is complete".
Our orders are plain limits -- native. A DAY limit dies at the end of the
trading day by its own TIF, which bounds how stale an orphan can be, but the
nightly ~23:45 reset happens after the close, so "the process restarted
overnight" and "the order expired at the close" are different events and the
morning reconcile must distinguish them.

**Fills during a disconnect are recovered as data, and their events are
missed.** This is from source, not inference: `wrapper.execDetails` handles
"both live fills and responses to reqExecutions", and only the live path
(`isLive`) emits `execDetailsEvent` and `trade.fillEvent`. Replayed
executions -- which is what a reconnect sync delivers -- are appended to
`trade.fills` and `ib.fills()` silently. `connectAsync` requests executions
on every connect (not gated by readonly), so the data arrives; nothing fires.
On top of that, our `broker.connect()` constructs a fresh `IB()` per connect,
so the old Trade objects and every event subscription on them are dead after
a reconnect regardless.

Consequence, stated plainly: **the executor cannot be event-driven across its
own restarts or Gateway's.** After every connect it must rebuild its picture
from `reqAllOpenOrders()` + `ib.fills()` + positions and reconcile against
orders.jsonl. Events are for intra-session latency only.

**Scope of the execution replay is the one genuinely ambiguous point.**
`fills()` is documented as "from this session"; `reqExecutions()` returns
execution reports which IBKR documents as available for the current day.
Whether a fill from 15:59 is still returned by a `reqExecutions` issued after
the nightly reset (a new IBKR "day"), and exactly when that boundary rolls,
is not documented anywhere I can cite. Ambiguous -- do not design the morning
reconcile around exec replay alone; positions plus the orders.jsonl ledger
close the gap.

## 4. Read-Only API mode: loud or silent?

Layered answer, and the layers disagree:

- **ib_async does not guard locally.** `placeOrder` sends unconditionally;
  the `readonly` flag only suppresses connect-time syncs (source, section 2).
  `readonly=True` in our broker does not itself stop a submission.
- **Gateway rejects loudly at the wire**: error 321, "Error validating
  request ... The API interface is currently in Read-Only mode" (ib_insync
  issue #1; community reports agree on the code). `ib.errorEvent` fires and a
  log line is written.
- **But ib_async classifies 321 as a warning** (`warningCodes` in
  `wrapper.error`): the Trade's status is set to `ValidationError`, a log
  entry is appended, `statusEvent` fires -- and `cancelledEvent` does NOT,
  because as far as ib_async knows the order might still be live (the code
  comments say exactly this for the modify case, with a TODO for the
  new-order case). `ValidationError` is in `ActiveStates` and `WorkingStates`
  and not in `DoneStates`.

So: **loud in the log and on `errorEvent`; silent to an executor that waits
on `isDone()`/`filledEvent`, which will wait forever.** The submission path
must subscribe to `errorEvent`/`statusEvent` and treat `ValidationError` as a
terminal refusal with an alert. Also note our own belt: `broker.connect()`
hardcodes `readonly=True`, so coming out of Read-Only is two deliberate
changes (Gateway setting AND the connect flag), which matches spec 008
section 8's intent that it be its own step.

## 5. Partial fills

**What we observe.** One `fillEvent(trade, fill)` per execution; each `Fill`
carries the `Execution` (shares, price, execId, permId, time) and later its
`CommissionReport` via `commissionReportEvent`. `trade.orderStatus` carries
`filled`, `remaining`, `avgFillPrice`, `lastFillPrice` from the orderStatus
callback. `trade.filled()`/`trade.remaining()` are computed from the fills
list, which given the duplicate/missing-statusEvent caveat in section 1 makes
the fills list the number to trust. Status stays `Submitted` while partially
filled; `Filled` and `filledEvent` mean complete.

**What we are left holding.** A partial position: some shares traded at the
limit, the remainder still working. For a one-leg drift correction that is
just a smaller correction -- the band re-check next cycle sees the improved
position and re-trips or not, which the re-derivation protocol already
handles. For a two-leg foil decay ticket it is the one-leg-filled-first
problem spec 008 section 6 flagged: unhedged delta until the other leg
catches up, and leg ordering remains a section 8 gate. If the remainder of a
DAY order is still unfilled at the close, IB cancels it server-side; that
arrives as error 202 / status `Cancelled` (ib_async then does fire
`cancelledEvent`), and the fills already received stay ours. Reconciliation
to the cent is `sum(execution.shares x execution.price)` against the ticket
row, which is why orders.jsonl should store per-leg share counts and limits
exactly as proposed (it does).

## Sources

- ib_async 2.1.0 installed source: `ib.py` (`placeOrder`, `connectAsync`,
  request docstrings), `wrapper.py` (`error`, `openOrder`, `execDetails`,
  `orderKey`), `order.py` (`OrderStatus` state sets, `Trade` events).
- https://ib-api-reloaded.github.io/ib_async/api.html
- https://interactivebrokers.github.io/tws-api/order_submission.html
  (status meanings, orderStatus fields, duplicate/missing callback caveats)
- https://interactivebrokers.github.io/tws-api/open_orders.html
  (reqOpenOrders/reqAllOpenOrders/reqAutoOpenOrders scopes, clientId 0
  binding, manual orders with API orderId 0)
- https://interactivebrokers.github.io/tws-api/message_codes.html
  (201 order rejected, 202 order cancelled, 321 validation)
- https://github.com/erdewit/ib_insync/issues/1 (error 321 text in
  Read-Only mode)
- https://github.com/ib-api-reloaded/ib_async/discussions/126 (disconnect
  does not cancel working orders)
- https://www.interactivebrokers.com/docs/tws-api/doc/architecture/the-trader-workstation/the-ib-gateway
  and IBKR reset guidance (nightly restart; native orders operate normally
  through resets)

## Open questions for the paper account (cheap, single-session)

1. Does `reqAllOpenOrders()` answer normally on a `readonly=True` connection
   with Gateway's Read-Only API enabled? (Expected yes -- it is a read.)
2. Is `orderRef` echoed on open-order rows queried from a different clientId
   than the submitter?
3. The exec-replay window across the nightly reset (section 3) -- observe
   once rather than trust a forum post.
