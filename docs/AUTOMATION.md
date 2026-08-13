# Automation — spec

Scope gate: if a task isn't here, ask before building. This spec covers getting from
the current backtest-only repo to a live, broker-connected bot. Do NOT build broker
integration until the signal-only phase is validated.

## Current state (as of this doc)

- **A read-only monitor exists** (spec 004): `src/broker.py`, `src/monitor_state.py`,
  `src/monitor.py`, with `ib_async` as a dependency. It reads one manually-opened
  pair, calls the same `decision.evaluate()` the backtest calls, and logs the
  decision to the console.
- **No order submission logic exists anywhere in the repo**, and none is in scope
  until Phase 3. The monitor only reads. Gateway runs with Read-Only API enabled so
  that is enforced by the broker rather than by convention.
- `data.get_borrow_rates()` is the other touchpoint with IBKR — pulls the
  public shortstock FTP list, 12h cache, appends to `cache/borrow_history.csv`.
- `engine.py` and `band.py` are pure and data-source-agnostic. This is the reusable
  core — live automation should not change this logic, only what feeds it and what it
  outputs to.

## Signal source: IBKR, not Polygon

Polygon stays a backtesting/historical-validation tool only — free tier is rate-limited
(~5 req/min) and not real-time, per existing `ROADMAP.md` notes.

Live band checks should run against **IBKR quotes**, once connected via `ib_async` for
execution anyway. Reasoning: using the same venue for the trigger check and the fill
eliminates basis risk between "the price that told us to trade" and "the price we
actually get."

Two different 15-minute numbers are in play here and should not be conflated:
**quote delay** (IBKR's free market-data feed lags real-time by up to 15 minutes) and
**poll interval** (`docs/STRATEGY_SPEC.md`'s target cadence for checking the bands,
also 15 minutes). Delayed quotes are acceptable for now because the band strategy's
thresholds are wide (10% moves), not because the poll interval happens to share the
same number — a stale quote inside a 15-minute-old snapshot is a small fraction of
the band width. Revisit quote delay if execution timing tightens; the poll interval
is a separate knob, tracked as its own modeling assumption in the strategy spec.

Borrow-rate data is unaffected — already IBKR-sourced.

## Account requirements

- **Margin account** is required to short at all — cash accounts can't do it.
- **Portfolio Margin** ($110,000 NLV plus options approval) is the capital-efficient
  target given the hedged long/short structure — risk-based margining recognizes the
  offset between legs, versus Reg T pricing legs closer to independently. Below that
  threshold, Reg T works as a starting point, just less capital-efficient.
- **Reg SHO locates** — need actual borrow availability before shorting. Already
  tracked via `get_borrow_rates()`'s `available` field; live bot needs to treat
  zero/unavailable as a hard block, not just a rate input.
- Paper trading account mirrors the live account (same API, same permissions, same
  base currency) — free, auto-created alongside a live account. Different login
  credentials and port (4002 paper vs 4001 live) but identical `ib_async` code path.
  Fills are optimistic (always at displayed bid/ask) — won't validate real slippage or
  borrow/buy-in risk. Useful for plumbing validation only, not a substitute for live
  risk-testing.

## Running the monitor (spec 004)

Read-only. Reads one manually-opened TSLA/TSLL position, decides, logs. Never
trades.

### Gateway prerequisites (operator, not code)

IB Gateway must be running and logged in before the monitor starts. In
Gateway -> Configure -> Settings -> API -> Settings:

- **Read-Only API: ON.** This makes "no order submission" a property of the broker
  rather than a promise in a spec. Stays on through Phase 1c.
- Enable ActiveX and Socket Clients
- Socket port 4002 (paper)
- Trusted IPs includes `127.0.0.1`

**There are no IBKR credentials anywhere in this codebase.** Gateway holds the
authenticated session; the API socket on localhost requires no authentication.
`.env` carries connection coordinates only — see `.env.example`.

Gateway and TWS cannot hold the same credentials at once: opening TWS to check the
position will disconnect the monitor. Expected, not a bug — the reconnect path
handles it. IBKR also resets sessions nightly around 23:45 ET, so disconnects are a
routine code path rather than an error condition.

### Run

```
PYTHONPATH=src .venv/Scripts/python.exe src/monitor.py
```

### `base_capital` is an allocation decision, not NLV

`MONITOR_BASE_CAPITAL` is the capital deliberately committed to this pair. It is
configuration. It changes only when a human decides to run more or less size, and
never in response to price, P&L, or account value.

`target` is derived from it every cycle and **never persisted**:

```
target = (base_capital * capital_utilization) / margin_multiplier
```

where `margin_multiplier` is derived per cycle via `config.margin_multiplier(pair)`
(= `long_rate * leverage + short_rate`), never stored. The startup log prints the
multiplier and the two rates it came from.

Deriving `target` from live account value instead would make the reference drift
with P&L, so `abs(short_notional - target)` never accumulates and the foil decay
band silently never fires — while looking like it works. Full argument in
`docs/STRATEGY_SPEC.md` section 1; do not re-litigate it here.

A deposit is therefore **detected and alerted, never acted on**. The monitor warns
when NLV diverges from `base_capital` by more than 10% and takes no sizing action.

### Persisted state

`peak_equity` only, at `MONITOR_STATE_PATH` (default `<repo>/data/state/monitor.json`,
gitignored). It is the one value that cannot be rebuilt from configuration, and the
drawdown stop is wrong without it.

**In deployment, point `MONITOR_STATE_PATH` outside the repo tree.** A `git pull` or
re-clone that wipes the file resets the peak and silently disables the drawdown stop.
A malformed state file makes the monitor exit loudly rather than reinitialize, for
the same reason.

On the VPS all runtime paths live in `/var/lib/short-lev`, created by the
monitor unit's `StateDirectory=`: `MONITOR_STATE_PATH`, `ALERT_STATE_PATH`,
`EVENT_LOG_DIR`, and (spec 009) `EXECUTION_STATE_PATH=/var/lib/short-lev/execution_state.json`.
See `docs/VPS.md` section 8.

### What the monitor deliberately does not do

- **Touch the wire itself.** Spec 009 routes tripped decisions through
  `execution.dispatch` — four default-off rails, one auditable line — and every
  wire-touching line lives in `src/execution.py`, never in the monitor.
  Terminal triggers (drawdown stop) never dispatch at all; they alert until a
  human acts.
- **Persist the de-risk target.** `evaluate()` returns a ratcheted `new_target` on a
  margin de-risk; the monitor reports it and throws it away. It does not trade, so
  moving the reference for a trade that never happened would corrupt every
  subsequent band reading.
- **Size on deposit.** Detect and alert only.

## Telegram alerts and the event log (spec 006)

The monitor produces decisions; `src/notify.py` consumes them and sends. The
monitor never calls Telegram directly. That seam is what lets the executor later
consume the same decisions, and it keeps the logic worth testing from being
welded to a network call.

### Creating the bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`.
   Answer the name and username prompts.
2. BotFather replies with the token, of the form `123456789:AAH...`. That is
   `TELEGRAM_BOT_TOKEN`.
3. **Send your new bot a message** — any message. A bot cannot start a
   conversation with you, so until you write to it first there is no chat to
   reply into and `getUpdates` returns nothing.
4. Fetch the chat ID:

   ```
   curl https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

   Read `result[0].message.chat.id` out of the JSON. That is
   `TELEGRAM_CHAT_ID`. A personal chat ID is a positive integer; a group is
   negative, and the leading `-` is part of the value.

**The bot token is a full credential.** Anyone holding it controls the bot. It
lives in `.env`, is never committed, and is never logged, not even at DEBUG. If
it leaks, `/revoke` in BotFather and reissue.

Leaving either variable blank disables Telegram: the monitor runs normally, logs
to console and JSONL, sends nothing, and raises nothing. Delivery failures are
recorded and never fatal — a monitor that dies because an alert failed to send
is worse than one that stays quiet.

### Alert cadence

A tripped band stays tripped until a human acts, so alerting every poll would
train you to mute it. Instead: send on the transition into a trigger, re-send
every `ALERT_REPEAT_MINUTES` (default 60) while it stands, and send once at INFO
on the return to normal. A *change* of trigger sends immediately — it is new
information.

Dedup state lives in `data/state/alert_state.json`, **separate from
`monitor.json`**. Losing it costs one duplicate message; losing `peak_equity`
silently disables the drawdown stop. Different consequences, different files.

### The event log

Two append-only JSONL files under `EVENT_LOG_DIR` (default `data/events`):

- `checks.jsonl` — one line per check, **tripped or not**.
- `alerts.jsonl` — one line per send attempt, **delivered or not**.

Both halves of those matter. Non-trips are the raw material for the intraday
cadence question deferred in the ROADMAP: you cannot calibrate a poll interval
from the trips alone. Failed sends are how you learn the bot went quiet for a
reason other than nothing happening. Read either with
`pd.read_json(path, lines=True)`.

Writes never crash the monitor. A full disk is a reason to lose telemetry, not
a reason to stop watching a live position.

### Heartbeat, and its limitation

Once per day at `HEARTBEAT_HOUR` (default 09:45, just after the open) the monitor
sends an INFO summary: pair, state, target, cushion, checks completed since the
last heartbeat, and whether anything is tripped.

**Always ET**, whatever timezone the box runs. The 09:45 default is an ET fact —
on a UTC box the old local-time version fired at an arbitrary hour with no error
and no clue why.

It fires once inside a **30-minute window** starting at that time, not on the
first poll after it. Open-ended meant any restart later in the day sent one
immediately (observed at 15:24 and again at 15:57 with the hour set to 09:45),
and a heartbeat arriving at an arbitrary time cannot do its job. Past the window
it skips the day rather than firing late: a missing heartbeat is the signal, and
a late one only adds noise to it.

**This is a heartbeat you must notice missing.** If the monitor dies at 02:00 on
Tuesday you find out at 09:45 — the process cannot tell you it died, so silence
has to be the signal, and silence is only a signal if someone is watching for
it. That is acceptable for a monitor that executes nothing. It stops being
acceptable once the executor exists, at which point the right answer is an
external dead-man: a service that alerts when a ping *stops* arriving. Not built
now, deliberately.

### Trips that are deliberately not sent

When `base_capital` exceeds NLV by more than 10%, the derived target is
unachievable, and a trip recommending the resize contradicts the sanity warning
printed above it. Both statements are correct in isolation; together they are
noise. The trip is still evaluated and written to `checks.jsonl` with
`unactionable: true` — it is real — but it is not sent. The sanity warning is
sent instead, at WARNING.

The other two sanity conditions do **not** suppress. An undeployed deposit and a
position opened at a different size both produce entirely actionable trips.

## Phased rollout

### Phase 1 — signal-only bot
- Poll live IBKR quotes for open pairs.
- Run through existing `band.py` logic unchanged.
- On a band trip: push a notification (Slack or email — pick one) with the pair and
  the recommended rebalancing trade. No order submission.
- Log every check (tripped or not) and every notification sent.

**Done when:** the bot runs on a schedule against IBKR quotes, correctly identifies a
band trip against a known test case, and a notification arrives with the right trade.

### Phase 2 — semi-manual execution
- Execute notified trades by hand.
- Reconcile actual fills against backtest assumptions (slippage, fill timing, borrow
  cost realized vs. modeled).
- Run for several weeks minimum before automating execution.

**Done when:** a handful of live-notified trades have been manually executed and
reconciled, with no unexplained gap versus backtest assumptions.

### Phase 3 — broker integration (paper)
- Add `ib_async` + IB Gateway on a VPS (or always-on local machine first).
- IBC for headless login and daily-restart handling (IBKR resets all sessions
  ~11:45 PM EST nightly — expected behavior, not a bug).
- Connect to the **paper account** first. Same signal logic from Phase 1, now
  auto-submitting orders instead of just notifying.

**Done when:** the bot runs unattended against paper for at least a few weeks with no
manual intervention needed to recover from a disconnect or restart.

### Phase 4 — live capital
- Switch config (host/port/credentials) from paper to live — no logic changes.
- Start at small size, scale up gradually.
- Should run in parallel with, not instead of, the regime stress-testing already
  planned for the strategy itself — automating an unstress-tested strategy compounds
  risk rather than reducing it.

## Open decisions

- ~~Notification channel: Slack vs. email~~ — resolved: **Telegram**, built in spec
  006 (`src/notify.py`). The monitor never calls the sink directly; it produces a
  decision and the sink consumes it.
- ~~Inbound commands (`/status`, `/positions`)~~ — deferred deliberately, its own
  spec. Receiving needs either a webhook (a public HTTPS endpoint this box does
  not expose) or long-polling `getUpdates`, which is a second concurrent loop
  alongside `ib_async`'s asyncio loop. That is a real design question, not a
  feature bolted onto an outbound sink. It also needs a chat-ID allowlist, since
  bots are discoverable by username.
- An external dead-man for the heartbeat — required before the executor exists,
  not before. See the heartbeat limitation above.
- VPS vs. always-on local machine for Phase 3 — not yet chosen.
- Timing of Portfolio Margin upgrade relative to the $110,000 NLV plus options
  approval threshold — depends on capital available at automation time, not a
  blocking decision now.