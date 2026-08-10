# Spec 006 -- Telegram sink, heartbeat, and actionable output

**Phase:** 1c
**Depends on:** spec 005 (shipped)
**Estimated:** one session

## Why

The monitor currently exists only while a terminal is open, and a band trip is
something you find out about by reading a screen. This closes that: decisions go
to Telegram, every check is persisted, and the output becomes something you can
act on from a phone without opening TWS.

Three defects surfaced during spec 004/005 live testing ride along, because all
three are tolerable on a terminal and dangerous as a notification.

## Out of scope (do not build)

- **Inbound commands** (`/status`, `/positions`). Deferred deliberately, not
  forgotten -- see the note at the end. Outbound only.
- **Order submission.** Read-Only API stays on.
- **Multi-pair**, systemd, dashboard.
- **SQLite.** `hype_arb` earns a database; this produces one append-only event
  stream from one pair. JSONL, read directly by pandas later.
- Any change to `decision.py`'s trigger conditions or `config.py`'s rates.

---

## 1. Fix the trade line arithmetic

Currently:

```
TSLA: BUY 13 shares @ ~329.38 = $4413.60 (9222.76 -> 13636.36)
```

13 x 329.38 is $4,282, not $4,413.60. The dollar figure is the *exact*
requirement; the share count is that figure rounded; the resulting notional
assumes the exact figure was traded. Three numbers, three different trades.

Report the trade that will actually happen:

```
TSLA: BUY 13 shares @ ~329.38 = $4,281.94
      9,222.76 -> 13,504.70  (target 13,636.36, residual -131.66)
```

- Round shares to whole numbers, nearest.
- Every dollar figure derives from the **rounded** share count.
- Show the residual against target so the landing point is explicit.

The residual is expected and harmless -- $132 against a $681.82 band -- but it
must be visible rather than papered over. In Phase 2 you will reconcile fills
against these lines, and three inconsistent numbers make that impossible.

## 2. Suppress trips when the target is unachievable

Observed with `MONITOR_BASE_CAPITAL=50000` against a $10.5k account: the sanity
check correctly warned the derived target was unreachable, then the trip line
recommended ~$56k of new exposure. Both correct in isolation, contradictory
together, and the trip is the last thing on screen.

When the `base_capital` exceeds NLV sanity check has fired, the trip is still
**evaluated and logged** -- it is real -- but marked unactionable and **not
sent to Telegram**. The Telegram message in that case is the sanity warning
itself, at WARNING severity.

Do not suppress on the other two sanity conditions. An undeployed deposit or a
position opened at different sizing both produce trips that are entirely
actionable.

## 3. Quieten routine disconnects

A nightly Gateway restart currently prints ~25 lines of asyncio traceback for a
completely expected event. Log one WARNING line with the exception type and
message; put the traceback at DEBUG.

Disconnects are a primary code path here, not an error path -- IBKR resets
sessions every night for the life of this system.

---

## 4. `src/events.py` -- structured event log

Append-only JSONL, two files, path from `EVENT_LOG_DIR` (default `data/events`).

**`checks.jsonl`** -- one line per check, tripped or not:

```json
{"ts": "2026-08-10T14:21:55-04:00", "pair": "TSLL", "short_notional": 6808.16,
 "long_notional": 13821.84, "target": 6818.18, "net_delta": 205.52,
 "margin_cushion": 2853.67, "account_equity": 10494.13, "peak_equity": 10507.95,
 "ibkr_maint": 7640.46, "modeled_maint": 7540.36, "trigger": null}
```

**`alerts.jsonl`** -- one line per send attempt, delivered or not:

```json
{"ts": "...", "severity": "WARNING", "kind": "trip", "title": "...",
 "delivered": false, "error": "HTTP 429: ..."}
```

Non-trips matter as much as trips: this is the raw material for the deferred
intraday-cadence question, and trip frequency just changed materially now that
bands scale off a 45% larger target.

Writes must never crash the monitor. Catch, log, continue.

## 5. `src/notify.py` -- the Telegram sink

Port the `hype_arb` design (`src/tg/notifier.py`), adapted to JSONL.

Non-negotiable constraints, straight from that module's docstring:

- **Never crash the monitor.** All network and HTTP failures caught and logged.
- **No-op when `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is unset.** Dev
  convenience, and it must stay possible to run the monitor with no bot.
- **Every attempt logged**, success or failure, to `alerts.jsonl`.

Copy `escape_md_v2()` **verbatim**. Telegram rejects messages containing
unescaped MarkdownV2 reserved characters, and these alerts are full of `$`, `.`,
`-`, `(`, `)`. This is a solved problem; do not re-solve it.

Severity map: INFO / WARNING / CRITICAL, with the same emoji.

**The monitor never calls Telegram directly.** It produces a decision; the sink
consumes it. That seam is what lets the executor later consume the same
decisions, and it keeps the thing you want to test from being welded to a
network call.

### Message content

A trip message must be actionable without opening TWS:

```
🟠 TSLL -- Foil Decay Band

short 6,808.16 / target 6,818.18
long 13,821.84 | net delta +205.52
cushion 2,853.67 (equity 10,494.13)

TSLL: SELL (short more) 262 shares @ ~8.15 = $2,135.30
      4,683.95 -> 6,819.25 (target 6,818.18, residual +1.07)
TSLA: BUY 13 shares @ ~329.38 = $4,281.94
      9,222.76 -> 13,504.70 (target 13,636.36, residual -131.66)
```

### What gets sent

| Event | Severity |
|---|---|
| Foil decay / long-short trip | WARNING |
| Margin de-risk | CRITICAL |
| Drawdown stop | CRITICAL |
| `base_capital` exceeds NLV | WARNING (and suppresses the trip, item 2) |
| Leg check failed | WARNING |
| Reconnected after disconnect | INFO |
| Trip resolved (trigger -> none) | INFO |
| Daily heartbeat | INFO |

## 6. Dedup

A tripped band stays tripped until a human acts. Without dedup that is an alert
every 60 seconds.

- Send on **transition** into a trigger state.
- Re-send at `ALERT_REPEAT_MINUTES` (default 60) while it persists -- a standing
  trip should nag, not vanish.
- Send once on transition back to `none`, at INFO ("resolved").
- A *change* of trigger (foil decay -> margin de-risk) is a new transition and
  sends immediately regardless of the repeat timer.

State in `data/state/alert_state.json`, **separate from `monitor.json`**. Losing
alert state costs one duplicate message; losing `peak_equity` silently disables
the drawdown stop. Different consequences, different files -- do not merge them.

## 7. Heartbeat

The monitor cannot tell you it died. Silence has to be the signal.

Once per trading day at `HEARTBEAT_HOUR` (default 09:45 ET, just after open),
send an INFO summary: pair, current state, target, cushion, checks completed
since the last heartbeat, and whether anything is currently tripped.

**Known limitation, state it in the docs:** this is a heartbeat you must notice
*missing*. If the monitor dies at 02:00 Tuesday you find out at 09:45. That is
acceptable for a monitor that executes nothing, and it is not acceptable once
the executor exists -- at which point an external dead-man (a service that
alerts when a ping *stops* arriving) is the right answer. Do not build that now.

## 8. Config and docs

Add to `.env.example`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`EVENT_LOG_DIR`, `ALERT_REPEAT_MINUTES`, `HEARTBEAT_HOUR`.

**The bot token is a full credential** -- anyone holding it controls the bot.
`.env` only, never committed, and never logged even at DEBUG.

Add a section to `docs/AUTOMATION.md`: creating the bot via BotFather, obtaining
the chat ID via `getUpdates`, and the heartbeat's limitation above.

---

## Session gate

1. **Unconfigured is a clean no-op.** With `TELEGRAM_BOT_TOKEN` unset the
   monitor runs normally, logs to console and JSONL, sends nothing, and raises
   nothing.
2. **Trip delivers.** Force a long-short breach. One message arrives with the
   pair, trigger, current state, and both trade lines.
3. **Trade line arithmetic is self-consistent.** In the delivered message,
   `shares x price` equals the stated dollar amount, and the resulting notional
   equals current plus that amount. Check by hand.
4. **Dedup holds.** With the trip standing, exactly one message arrives per
   `ALERT_REPEAT_MINUTES` -- not one per cycle. Set it to 2 minutes for the test.
5. **Resolution sends once.** Act on the trade; one INFO "resolved" arrives and
   nothing further.
6. **Delivery failure is recorded, not fatal.** Point `TELEGRAM_BOT_TOKEN` at a
   bad value. The monitor keeps running; `alerts.jsonl` shows
   `delivered: false` with the error.
7. **Suppression works.** Set `MONITOR_BASE_CAPITAL=50000`. The sanity warning
   is sent; the trip is logged and marked unactionable but **not** sent.
8. **Escaping holds.** A message containing `$`, `.`, `-`, `(`, `)` renders
   correctly rather than being rejected by Telegram. The trade lines already
   contain all five.
9. **Disconnect is quiet.** `pkill -f ibgateway` produces one WARNING line, not
   a traceback. Reconnection sends one INFO.
10. **Checks are logged.** `checks.jsonl` has one line per cycle including
    non-trips, parseable as JSON.
11. `verify_band.py`, `verify_engine.py`, `verify_monitor.py` pass, and
    `hash_band.py` is unchanged -- this spec must not touch the backtest.

Gate 11 matters: this is a monitor-side spec. If the digest moves, something
reached into shared code that should not have.

Separate commits per numbered item, imperative lowercase with a scope prefix. Do
not commit until I have reviewed the diff.

---

## Note: why inbound commands are deferred

Asking the bot for balance or positions on demand is genuinely wanted, but it is
not a small addition. Receiving requires either a webhook -- which needs a
public HTTPS endpoint, and this box deliberately exposes nothing -- or
long-polling `getUpdates`, which is a **second concurrent loop alongside
`ib_async`'s asyncio loop**. That is a real design question about how the two
cooperate, not a feature bolted onto an outbound sink. It also needs a chat-ID
allowlist, since bots are discoverable by username and strangers will message it.

Its own spec, once outbound is proven in daily use.

---

## Result

**Offline gates pass. Live gates (2, 5, 6, 7, 9) not yet run -- they need IB
Gateway and a real bot token, and are the operator's to execute.**

### What shipped

All eight items. Three new modules (`src/events.py`, `src/notify.py`,
`src/alert_state.py`), `src/monitor.py` rewired, `.env.example` and
`docs/AUTOMATION.md` updated, six new offline checks in `verify_monitor.py`.

### Gate status

| # | Gate | Status |
|---|---|---|
| 1 | Unconfigured is a clean no-op | PASS (check m) |
| 2 | Trip delivers | **Not run** -- needs Gateway + bot |
| 3 | Trade line arithmetic self-consistent | PASS (check k) |
| 4 | Dedup holds | PASS offline (checks n, n2); not run live |
| 5 | Resolution sends once | PASS offline (check n); not run live |
| 6 | Delivery failure recorded, not fatal | **Not run** -- needs a bad token live |
| 7 | Suppression works | PASS offline (smoke + check n2); not run live |
| 8 | Escaping holds | PASS (check l) |
| 9 | Disconnect is quiet | **Not run** -- needs `pkill -f ibgateway` |
| 10 | Checks are logged | PASS (smoke: 7/7 rows including all non-trips) |
| 11 | verify_* pass, `hash_band.py` unchanged | **PASS** |

Gate 11 in full: `verify_band.py`, `verify_engine.py`, `verify_monitor.py` all
pass, and the `hash_band.py` GRAND digest is
`08baa20ac842502aedbb5f647f6ea24cf3128429f17f4a5a1d7d1d0cb85de942`, identical to
`baseline_005_hash.txt`. Captured before the first edit and re-checked after the
last. Nothing reached into shared code -- `band.py`, `decision.py`, `engine.py`,
`config.py`, `broker.py`, and `monitor_state.py` are untouched.

### Deviations from the spec

- **`ALERT_STATE_PATH` added**, not named in item 8. The spec fixes the dedup
  file at `data/state/alert_state.json`; that is the default, but the path is
  overridable for the same reason `MONITOR_STATE_PATH` is -- and the offline
  checks need to write to a temp directory.
- **`HEARTBEAT_HOUR` accepts `HH:MM`**, not just an hour. The spec's default is
  09:45, which a bare hour cannot express.
- **Heartbeat is local time, not ET.** No timezone dependency was added for a
  purely informational message; noted in `.env.example` and AUTOMATION.md. The
  deploy box runs ET.
- **`record_trigger()` written, then removed.** See the bug below.

### Bug found during the session

The first wiring of item 2 let the suppressed band trigger and the sanity key
alternate in the single `last_trigger` slot. Every cycle then looked like a
transition, and the sanity warning sent **four times in seven cycles** instead of
once per repeat window -- the exact alert fatigue item 6 exists to prevent, and
invisible to a unit test of `should_send()` alone. Found by driving `run()`
against a fake IB.

Fix: while suppressed, the sanity key owns the dedup slot outright and the band
trigger is never recorded. Lifting the suppression then reads as a fresh trip,
which is correct -- it is the first one that was ever actionable. Regression
check `n2` covers it.

### Deferred, deliberately

- **Inbound commands** (`/status`, `/positions`) -- unchanged from the spec's
  closing note; needs its own spec once outbound is proven in daily use.
- **External dead-man** for the heartbeat. The known limitation is documented in
  AUTOMATION.md rather than solved: this is a heartbeat you must notice
  *missing*. Required before an executor exists, not before.
- **Multi-pair.** `PAIR_KEY` is still the hardcoded `"TSLL"`.
- The live gates above.