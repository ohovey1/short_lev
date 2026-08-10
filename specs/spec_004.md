# Spec 004 -- Monitor core

**Phase:** 1b
**Depends on:** spec 003 (shipped -- `decision.evaluate()` exists)
**Estimated:** one session, largest so far

## Why

First code that touches a real account. It reads one manually-opened pair,
builds a `PositionState`, calls the same `evaluate()` the backtest calls, and
prints the decision. Nothing is executed. Nothing is sent anywhere yet.

The point is operational reality: does the connection hold, do the leg
identifications work, does our modeled `margin_multiplier` match IBKR's actual
`MaintMarginReq`.

## Out of scope (do not build)

- **Order submission.** Not now, not behind a flag, not "just the code path."
  Gateway will additionally be running in Read-Only API mode -- see
  Prerequisites -- so an order would be rejected at the broker anyway. Do not
  treat that as license to write one.
- **Telegram.** Console only. Spec 005.
- **Multi-pair.** TSLA/TSLL only.
- **Heartbeat / dead-man's switch.** Spec 005, with the sink.
- **Automatic sizing on deposit.** Detect and alert only -- `STRATEGY_SPEC.md`
  section 1 is explicit about this and about why.
- **Applying the de-risk ratchet.** `evaluate()` returns a `new_target` on a
  margin de-risk. The monitor **reports** it and never persists it. It does not
  trade, so moving the reference for a trade that never happened would corrupt
  every subsequent band reading.
- **VPS, systemd, IBC.** Phase 1d.
- Any change to `decision.py`, `engine.py`, `band.py`, or the backtest.
- Market-hours gating, holiday calendars, dedup logic.

---

## Prerequisites (operator, not code)

IB Gateway must be running and logged in before the monitor starts. In
Gateway -> Configure -> Settings -> API -> Settings:

- **Read-Only API: ON.** This makes "no order submission" a property of the
  broker rather than a promise in a spec. Stays on through Phase 1c.
- Enable ActiveX and Socket Clients
- Socket port 4002 (paper)
- Trusted IPs includes `127.0.0.1`

**There are no IBKR credentials anywhere in this codebase.** Gateway holds the
authenticated session; the API socket on localhost requires no authentication.
`.env` carries connection coordinates only.

---

## 1. Target derivation

`target` is derived, never observed:

```
target = (base_capital * capital_utilization) / margin_multiplier
```

Identical to the backtest -- same formula, same inputs. `base_capital` comes from
`MONITOR_BASE_CAPITAL` in `.env`; `capital_utilization` and `margin_multiplier`
from the same defaults and `config.PAIRS` the backtest uses.

There is no adoption step, no observed reference, and no state for `target`.
A manual rebalance in TWS changes nothing about it, which is the point.

Single-pair phase only. When multi-pair arrives in Phase 3, `base_capital`
becomes a per-pair allocation map. Do not build that now.

## 2. `src/broker.py` -- IBKR reads

One job: connect, read, hand back a `PositionState`. No decisions, no loop, no
files.

```python
def connect() -> IB
def read_position(ib, pair_key, target, peak_equity) -> PositionState | None
def disconnect(ib)
```

Config from `.env`: `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`, `IB_ACCOUNT`.

**On connect, log the port and account number.** `IB_PORT` is the only thing
separating paper from live. A misconfigured deploy must announce itself on line
one, not after a week of reading paper numbers believing they were live.

Building the `PositionState`:

- **Leg values come from `ib.portfolio()`**, not `reqMktData`. IBKR has already
  computed `marketValue` per position, which sidesteps market-data subscriptions
  and delayed-quote handling entirely.
- **`short_notional` must be positive.** IBKR reports a short as a negative
  position and negative `marketValue`; `evaluate()` expects a positive magnitude.
  Assert the sign was negative, *then* take `abs()`. Silently `abs()`-ing a
  long LETF position would produce a plausible-looking and completely wrong
  decision -- the worst failure mode available here.
- **Validate leg identity, loudly.** The leveraged ticker must be held short and
  the underlying long, per `config.PAIRS[pair_key]`. If either leg is missing or
  the signs are inverted, return `None` with a clear log line. Do not guess.
- `account_equity` = IBKR's `NetLiquidation`. Live, the broker's number is the
  truth and already nets accrued borrow.
- `margin_required` = IBKR's `MaintMarginReq`.
- `leverage`, `margin_multiplier` from `config.PAIRS`.
- `target`, `peak_equity` passed in by the caller.

**Log the margin comparison every check**: IBKR's `MaintMarginReq`, our modeled
`margin_multiplier * short_notional`, and the ratio. This closes the open
decision that has been in the ROADMAP since June.

## 3. `src/monitor_state.py` -- persisted state

One field. JSON, path from `MONITOR_STATE_PATH`, default
`data/state/monitor.json`.

```json
{ "peak_equity": 10000.00, "updated_at": "2026-08-06T14:31:00-04:00" }
```

`peak_equity` is an observation of reality, so it is always safe to write:
`peak_equity = max(peak_equity, account_equity)` every check. On first run,
initialize to current `account_equity`.

**The state path must resolve outside the repo tree in deployment.** A `git pull`
or re-clone that wipes `peak_equity` silently disables the drawdown stop.

Malformed state file: exit with a clear error. Do not silently reinitialize --
that would reset the peak and quietly suppress the drawdown stop.

## 4. `src/monitor.py` -- the loop

Long-running process. Connect once at startup, loop until killed, reconnect on
drop. **Not** a cron job that connects and exits each cycle.

```
connect (log port + account)
derive target from base_capital; log it and the inputs
load or initialize peak_equity
startup sanity check
loop:
    read position -> PositionState
    peak_equity = max(peak_equity, account_equity); persist
    decision = evaluate(state, params)
    log the check
    ib.sleep(POLL_INTERVAL_SECONDS)      # NOT time.sleep -- see notes
```

`BandParams` from `config` defaults, overridable by env. Same values as the
backtest -- no separate monitor tuning.

**Startup sanity check.** Compare `base_capital` against `NetLiquidation` and
against the observed short notional. Warn loudly (do not refuse) if:

- `base_capital` materially exceeds NLV -- derived target is unachievable, the
  position will be oversized and margin de-risk will fire repeatedly against a
  reference that can never be met.
- NLV materially exceeds `base_capital` -- likely an undeployed deposit. Report
  both figures and the derived target. Take no sizing action; see
  `STRATEGY_SPEC.md` section 1.
- Observed short notional is far from derived target -- either `base_capital` is
  wrong or the position was opened at a different size.

"Materially" is a configurable tolerance; 10% is a reasonable default.

**Log every check, not just trips**: timestamp, both leg notionals, net delta,
margin cushion, IBKR vs modeled margin, and the trigger (or `none`). This log is
the raw material for calibrating the intraday-cadence question deferred in the
ROADMAP, so non-trips matter as much as trips.

On a trip, log the trigger and the **specific trade** -- "buy 43 TSLA", not
"resize long leg". Derive share counts from `Decision.new_*` and the position's
current price. The point is being able to act without opening TWS.

Failure handling, deliberately minimal:

- **Connection lost:** log, reconnect with backoff (10s, capped at 5 min), keep
  looping. Never exit.
- **No position / bad legs:** log, keep polling. The position may be opened later.

## 5. `.env.example` and docs

Add to a committed `.env.example` with blank values: `IB_HOST`, `IB_PORT`,
`IB_CLIENT_ID`, `IB_ACCOUNT`, `MONITOR_BASE_CAPITAL`, `MONITOR_STATE_PATH`,
`POLL_INTERVAL_SECONDS`.

Add a section to `docs/AUTOMATION.md` on running the monitor: the Gateway
prerequisites above, that no credentials live in the repo, the `base_capital`
policy, and a pointer to `STRATEGY_SPEC.md` section 1 for why it is not NLV.

No credential in any committed file.

---

## Implementation notes -- `ib_async`

Known traps. Several of these pass every gate and then fail quietly in
deployment, which is why they are specified rather than left to discovery.

**Use `ib.sleep()`, never `time.sleep()`.** `ib_async` runs an asyncio event loop
underneath. `ib.sleep()` keeps pumping it; `time.sleep(900)` blocks it for
fifteen minutes. The socket stays open so nothing looks broken, but keepalives
are missed and portfolio data goes stale -- the monitor confidently reports
quarter-hour-old numbers. This is the single most likely bug in this spec.
Applies to the poll interval and to every wait in the reconnect backoff.

**Portfolio data is not ready at connect.** `ib.portfolio()` called immediately
after `connect()` frequently returns an empty list. Wait for account data to
populate before the first read, or the first check reports "no position found"
on a perfectly healthy account. Do not treat an empty portfolio at startup as
the no-position case.

**Account values are strings, tagged by currency.** `NetLiquidation` and
`MaintMarginReq` come back as text and need explicit float conversion. Filter on
the base currency rather than taking the first match.

**Client ID must be distinct.** Every example script uses 0 or 1. Reconnecting
with an ID still held by a half-dead session fails. Use `IB_CLIENT_ID` from env
(11 by default) and never hardcode.

**Competing sessions.** Gateway and TWS cannot hold the same credentials at
once. Opening TWS to check the position will disconnect the monitor. Expected
behavior, not a bug -- but the reconnect path must handle it gracefully rather
than spinning.

**Disconnects are normal, not exceptional.** IBKR resets sessions nightly around
23:45 ET. The monitor will be disconnected on a schedule for the rest of its
life. Reconnect logic is a primary code path, not error handling.

---

## Session gate

Manual, against the paper account, position opened by hand. Record actual
numbers in the Result -- not "matched", the figures.

1. **Connects.** Startup log shows port and account. `NetLiquidation` matches TWS.
2. **Derives target correctly.** Startup log shows `base_capital`,
   `capital_utilization`, `margin_multiplier`, and the resulting target. Verify
   the arithmetic by hand.
3. **Reads both legs correctly.** Logged notionals match TWS. `short_notional`
   is positive.
4. **Rejects a bad position.** Close one leg manually; monitor logs a clear
   failure and keeps polling rather than crashing or guessing.
5. **Sanity check fires.** Set `MONITOR_BASE_CAPITAL` well above NLV; startup
   warns. Set it well below; startup warns about the divergence. Neither refuses
   to run.
6. **Trips correctly.** Hand-buy enough underlying to breach the long-short band.
   Next check reports `long-short band` and a share count that would
   re-neutralize. Verify by hand.
7. **Target is unchanged by everything.** After gates 4-6, restart the monitor:
   derived target is identical. No state file holds it.
8. **Peak equity persists.** Restart; `peak_equity` is restored, not reset to
   current NLV.
9. **Survives disconnection.** Kill Gateway mid-run. Monitor logs, backs off,
   reconnects when Gateway returns, without restarting.
10. **Data stays fresh across a full interval.** Let one complete poll interval
    elapse, then confirm the reported notionals reflect current prices rather
    than the values from the previous cycle. This is the `time.sleep()` trap;
    gates 1-9 all pass with it present.
11. **Margin comparison logged** every check: IBKR's figure, ours, the ratio.

Separate commits per numbered item, imperative lowercase with a scope prefix. Do
not commit until I have reviewed the diff.

---

## Note on the borrow-timing seam

The backtest's `Decision.margin_cushion` is pre-borrow; live, `NetLiquidation`
already nets accrued borrow, so the monitor's is post-borrow. Same seam as spec
002, opposite side. Not a defect in this spec -- record it in the Result so the
eventual seam spec has both cases written down.

---

## Result

## Result

All eleven gates passed, manually, against paper account DUQ985373 over
2026-08-07 and 2026-08-10. Position: short 575 TSLL, long 28 TSLA, NLV ~$10,500.

### Gates

| Gate | Outcome |
|---|---|
| 1 connects | port 4002, `accounts=DUQ985373 (PAPER)` |
| 2 target derived | `10000 x 0.75 / 1.60 = 4687.50`, verified by hand |
| 3 legs read | `position=-575.0, marketValue=-4659.80` -> `short=4659.80` positive |
| 4 bad position | closed TSLA leg -> `TSLL=held TSLA=MISSING`, kept polling, no crash |
| 5 sanity check | `base_capital=50000` -> both warnings fired with correct diagnosis |
| 6 trip | net delta +586.15 vs 468.75 threshold -> `long-short band`, acted on, resolved to -101.54 |
| 7 target stable | identical across restarts, absent from state file |
| 8 peak persists | restored, not reinitialised |
| 9 disconnect | `pkill ibgateway` -> caught, backoff 10s->80s, reconnected unattended |
| 10 freshness | values step on IBKR's ~3min push, not frozen |
| 11 margin logged | every check |

### The margin finding

**This is the session's main output.** Observed ratio
`ibkr_maint / (1.60 x short_notional)` across the session: **0.691 to 0.719**.

Sample points:

| long | short | ibkr_maint | modeled | ratio |
|---|---|---|---|---|
| 9871.25 | 4642.55 | 5342.81 | 7428.08 | 0.719 |
| 9254.86 | 4678.20 | 5188.72 | 7485.12 | 0.693 |
| 9222.36 | 4683.95 | 5180.59 | 7494.32 | 0.691 |
| 9212.61 | 4672.45 | 5178.15 | 7475.92 | 0.693 |

**Two distinct defects, not one.**

**(a) Wrong rate.** The 1.60 multiplier was built as
`0.50 x leverage + 0.30 x leverage` -- 50% on the single-stock long leg. That is
the *initial* requirement. IBKR maintains long equity at 25%. Substituting:
`0.25 x 2 + 0.60 = 1.10`, against an observed ~1.11. The discrepancy is
explained, not mysterious.

**(b) Wrong shape.** Row 1 above has the same short notional as the others but a
ratio of 0.719 instead of ~0.692 -- because only the *long* leg had changed.
`margin_multiplier x short_notional` is the correct formula evaluated **at zero
net delta**; it assumes `long = leverage x short`, true at entry and false as
soon as delta drifts. Fitting `maint = a*long + b*short` across the observations
gives roughly `a ~ 0.285, b ~ 0.545` -- close to the 25%/60% the regulatory
formula predicts, but derived from two noisy points and not to be trusted as
constants.

Consequence: `target = base_capital x 0.75 / 1.60` undersizes by ~31%, and by a
*drifting* amount. Affects all 13 pairs, and the leaderboard ranking may move
since multipliers differ per pair. Spec 005.

**Caveat: this is a paper reading.** IBKR's paper margin engine may be more
permissive than live. Confirm against a funded account before resizing anything.

### Other findings

- **Commissions are real and unmodelled.** ~$1.00-1.20 per leg regardless of
  size. Establishing the position cost ~$5.09. On a $4,687 position that is
  ~4bp per rebalance, against a strategy whose edge is basis points of drag. The
  backtest models zero commission.
- **Trade line arithmetic is inconsistent.**
  `TSLA: SELL 2 shares @ ~327.84 = $586.15` -- 2 x 327.84 is $655.68. The dollar
  figure is the exact requirement; the share count is that rounded. Three numbers
  describing three different trades. Harmless here, costly during Phase 2
  reconciliation.
- **Trips fire even when the sanity check has declared the target
  unachievable.** With `base_capital=50000`, the monitor warned the target was
  unreachable and then recommended ~$56k of new exposure on a $10.5k account.
  Both correct in isolation; contradictory together, and the trip is the last
  thing on screen.
- **Disconnect handling is right but loud.** A routine reconnect prints ~25 lines
  of asyncio traceback. This path runs every night on the Gateway restart.
- **Portfolio updates arrive on IBKR's ~3-minute push**, not on our poll.
  Consecutive 60s cycles legitimately report identical values.
- **Weekly re-auth took username and password only -- no 2FA.** Friday's session
  expired Sunday 01:00 ET; Monday's login was a genuine fresh weekly auth. Paper
  only; live will likely differ. If it holds, IBC could automate the weekly login
  entirely.
- **Gateway and Client Portal cannot share a login.** Manual trades require
  stopping Gateway. Permanent operational constraint, matters in Phase 2.
- **Paper trading disclaimer** must be accepted once in Gateway before the API
  will serve position data (`Error 10141`).

### Deviations

- `readonly=True` added to `ib.connect()` after the first run triggered
  Gateway's write-access popup. `ib_async` defaults to `readonly=False` and calls
  `reqAutoOpenOrders` during setup. Added to Implementation notes.
- `executions request timed out` still appears on every connect -- harmless under
  read-only mode, adds ~4s to startup. Not suppressed.