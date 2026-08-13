# Spec 008 -- inbound Telegram commands and the approval loop

**Phase:** 1c (extension)
**Depends on:** spec 006 (Telegram sink), spec 007 (systemd units)
**Estimated:** one session, code-heavy
**Revision:** 2 (2026-08-13, after review)

## Why

The bot talks and cannot listen. That was correct for 006 -- outbound alerts are
the load-bearing half -- but it makes the bot unusable for anyone who is not
already SSH'd into the box. The stakeholder demo hinges on live interaction with
a running system rather than a document, and "read the alerts and hope" is not
an interactive system.

Three people are about to share one Telegram group: the account holder, a second
developer, and a non-technical funding stakeholder. Two consequences follow, and
both are design constraints rather than nice-to-haves.

First, **the bot becomes discoverable**. Bots are findable by username and
strangers will message it. Every command here reports positions, equity, or
margin. An unauthenticated reply is an account-disclosure bug.

Second, **group membership becomes the authorization model**. There is
deliberately no role system (decided 2026-08-13: roles are complexity we have
not earned with three trusted people). That is fine while every command is
read-only. It stops being obviously fine the moment a message carries a button
that can move the account, which section 7 introduces -- so section 7 keeps the
terminal step inert and section 8 says exactly what has to be true before it
stops being inert.

The approval loop is worth building now even though it cannot trade, because the
plumbing -- re-derivation at press time, limit pricing, idempotency, expiry, the
one-connection invariant -- is the entire difficulty. The `placeOrder` call is
one line and is the *only* part being deferred. Building the loop against a
placeholder also delivers Phase 2 as a side effect: a proposal timestamped at T,
acknowledged at T+n, filled by hand at T+m, all three recorded, is precisely the
semi-manual reconciliation the ROADMAP asks for.

## Out of scope (do not build)

- **`placeOrder`, or any order submission.** There is no order-submission code
  in this repo, not behind a flag, and this spec does not add any. Gateway stays
  in Read-Only API mode. See section 8 for what unblocks it.
- **Roles, per-user permissions, separate channels.** One group, one chat id.
- **A webhook.** The box exposes SSH and nothing else. Long-polling only.
- **`/pause`, `/log`, `/pairs`, `/backtest`.** Deferred until wanted twice.
- **Multi-pair.** `PAIR_KEY = "TSLL"` stays hardcoded.
- **Any change to the rebalance rule.** `decision.py` resets to target. Section 6
  computes a limit *price* at the band boundary; it does not change what the
  position is rebalanced *to*. If those two ever get conflated, stop.
- Any change to `decision.py`, `band.py`, `engine.py`, or `config.py`.

---

## 1. Group migration (DONE -- read for the constraints it sets)

The group id is `-5220003871`.

**Done (2026-08-13).** The group exists, the bot is in it, `TELEGRAM_CHAT_ID` on
the VPS is set to the group id, the monitor has been restarted, and a test send
was confirmed delivered. Nothing in this section is outstanding.

Two decisions were taken along the way and are recorded because later sections
depend on them.

**No group permissions were configured**, deliberately. Restricting who can add
members was considered and dropped as unnecessary ceremony for three trusted
people. The consequence is the `migrate_to_chat_id` handler below, which stops being
optional as a result.

**The 1:1 chat is retained via per-machine `.env`, not fan-out.** The VPS holds
the group id; the laptop keeps the 1:1 id. Local test runs, forced trips, and
half-finished features go only to Owen, and the stakeholder never sees a test
alert from a monitor started to check something. Two ids in one env var would
mean duplicate delivery and a fan-out loop for no strategy reason, so
`TELEGRAM_CHAT_ID` is **replaced on the VPS, not extended**, and any future
"send to both" request should be met with this paragraph.

### Still open

**Membership is now the access-control action, and it does not feel like one.**
The section 3 gate matches on chat id, not sender, so anyone added to the group
is authorized the instant they join -- no allowlist edit, no restart. That is
correct and intended while every command is read-only. Once section 7's button
exists there is no per-person step between joining and being able to press
`Rebalance`. Adding someone to this group is granting trading authority.

**Add the stakeholder only after this spec ships.** His first contact with the
bot should be one where `/help` exists and `/status` answers. A bot that ignores
every command he sends is a bad first impression that costs more to undo than to
avoid. Fiona can join earlier; she can read a journal.

**Confirm privacy mode is Enabled** in BotFather (`/setprivacy`) before anyone
else joins. It is the default and almost certainly already on, but with a
single-member group nobody would ever notice it being off -- the symptom only
appears once several people are talking and the bot starts receiving every
message rather than only `/`-prefixed ones.

**Run BotFather `/setcommands`** once the four commands exist. It puts a
tap-to-run menu in the message bar, which is realistically how a non-technical
user discovers `/help` at all. Also `/setdescription`, which is what the
stakeholder reads before his first message.

### Required: handle `migrate_to_chat_id` in `notify._send_http`

The recorded id has ten digits and no `-100` prefix, so this is a **basic**
group. Basic groups convert to supergroups automatically on various ordinary
events, and **conversion changes the chat id**. Because no permissions were set,
that conversion is now unscheduled rather than something done deliberately up
front -- it will happen, if it happens, as a surprise.

Today the failure is silent by construction. The sink is deliberately non-fatal,
so a rejected send produces no crash; the truncated error body lands in
`alerts.jsonl`, which nobody reads until they notice the bot has been quiet for
a day. **A silent alerting channel on a live position is the worst failure mode
in this system**, and it is the one this project has already been bitten by in
other forms.

So: parse the Telegram error payload for `parameters.migrate_to_chat_id`, and on
finding it log at **ERROR** with both the dead id and the replacement, naming
`.env` as the fix. Three lines, and it converts a silent outage into a log line
that states its own remedy. Add the same note to `VPS.md`.

Do not attempt to auto-update `.env` or retry against the new id. The correct
response is a human changing a config value, and a process that rewrites its own
credentials file to reach a chat it was not configured for is a worse thing to
own than an outage.

## 2. The poller is a separate process, and it does not touch IBKR

New module `src/bot.py`, new unit `short-lev-bot.service`, modelled on the
monitor unit (same `User`, `StateDirectory`, hardening block, `Restart=always`).

**The invariant, and it is the one that matters in this spec: exactly one
process holds an IBKR connection.** The poller opens no `IB()` handle, imports
no `broker`, and holds no client id. Everything it reports comes from files the
monitor writes; everything it wants to *cause* is written as an intent that the
monitor consumes.

Two reasons. A second client id is a second thing to collide on the nightly
reset, and we have already lost an evening to `clientId already in use`. More
importantly, a poller that can read the account is one small change away from a
poller that can act on it, at which point two processes can decide to trade the
same position and neither knows about the other.

Long-poll `getUpdates` with `timeout=30`. **Persist the update offset** to
`/var/lib/short-lev/bot_offset.json` and pass `offset=last+1`. Without this, a
poller restart either re-delivers commands already answered -- including, once
section 7 exists, a re-delivered button press -- or silently skips the ones that
arrived while it was down.

Network failures get the same treatment as everywhere else in this codebase:
caught, one WARNING line, backoff, never exit.

## 3. The authorization gate

One equality check, before any command is dispatched:

```python
if str(update_chat_id) != str(configured_chat_id):
    # log at INFO with the chat id and the command, reply nothing
    continue
```

Reply nothing rather than "unauthorized" -- an error message confirms the bot is
live and tells a stranger their message was received. Log it, because a stream
of these is the signal that the username has been found.

## 4. The four commands

All four read from files. None connects to anything.

**Every reply carries the age of the data it is reporting.** The monitor polls
every 900 seconds, so a reply can be nearly fifteen minutes stale, and a monitor
that died forty minutes ago produces a reply that looks identical to a healthy
one. Render it as `as of 14:32 ET (4 min ago)`, and if the last check is older
than three poll intervals, prefix the reply with a STALE warning.

### `/status` -- is the bot alive and well

**This is a process-health command, not a strategy command.** It answers "should
I trust anything else this bot tells me," and it is the 3 AM command. Lead with
a single plain-English verdict on its own line -- `Healthy`, `Degraded`,
`Stale`, or `Disconnected` -- so it is readable at a glance by someone who will
not parse a table.

Underneath: connection state and how long since the last successful connect,
monitor uptime, checks completed since start, time of the last check, last error
and when it occurred, and whether Telegram alerting is configured.

The data does not exist yet. Have the monitor write
`/var/lib/short-lev/runtime.json` once per cycle -- `started_at`,
`last_check_ts`, `last_connect_ts`, `cycles_since_start`, `connected`, and
`last_error` as `{message, ts}` -- using the same never-raises discipline as
`events._append`. Write it atomically (temp file plus rename) so a poller
reading mid-write does not parse a truncated file.

`/health` from revision 1 is deleted; this replaces it.

### `/positions` -- the position and where it sits against the bands

Two sections in one reply, because they answer one question together.

**The legs.** Per leg: shares, average cost, current mark, notional, unrealized.

**Band distance.** Short notional against target and the foil decay threshold;
net delta against the long-short threshold; current trigger or an explicit "no
band tripped." Express each as a percentage of its band -- `12.3% of a 15.0%
band` is readable; `$4,684 vs $4,910` is not.

The strategy frame lived in `/status` in revision 1 and moves here. It belongs
next to the legs: the position and its distance from action are one thought, and
splitting them means two commands to answer "are we about to trade."

**`check_record` does not currently carry share counts or marks, so widen it.**
Add `short_shares`, `long_shares`, `short_price`, `long_price`, and per-leg
average cost if `broker.read_position` already has it to hand. This is additive
-- existing consumers read by key -- and it keeps the one-connection invariant
intact, which is the whole reason for doing it this way rather than letting the
poller ask IBKR.

### `/account`

NLV, `MaintMarginReq` as reported, cushion, peak equity, drawdown from peak.

Then the line that earns this command: **modeled margin and IBKR's reported
figure side by side, with the ratio.** `check_record` already logs both
`ibkr_maint` and `modeled_maint` every cycle, so the samples are already
accruing -- this only surfaces them. The 1.013 figure came from two hand-taken
readings; the open question from spec 006 is whether `MaintMarginReq` is even
the binding constraint, and the reading we most want is one taken during a
stress moment. Putting it one command away makes that reading obtainable from a
phone.

### `/help`

Written for the stakeholder, not for us. Three parts.

1. One sentence on what the bot is for.
2. The four commands, one line each.
3. **An alerts section: every message the bot can send unprompted**, so an
   unexpected alert is recognisable rather than alarming. Nine kinds exist today
   and all nine belong in the list, grouped by severity:

   | Severity | Kind | Means |
   |---|---|---|
   | CRITICAL | `drawdown stop` | Equity fell past the drawdown threshold. Terminal in the backtest; here it needs a human. |
   | CRITICAL | `margin de-risk` | Margin cushion went negative. Position must shrink. |
   | WARNING | `foil decay band` | Short leg drifted past the foil decay band. Rebalance both legs. |
   | WARNING | `long-short band` | Net delta drifted past the long-short band. Resize the long leg only. |
   | WARNING | `sanity` | `base_capital` exceeds NLV -- a sizing input is wrong, trips are suppressed as unactionable. |
   | WARNING | `leg_check` | A leg could not be read from IBKR. |
   | INFO | `resolved` | A previously tripped band is back inside. |
   | INFO | `reconnected` | Gateway connection re-established after a drop. |
   | INFO | `leg_check` | Position readable again after a failed read. |
   | INFO | `heartbeat` | Daily 09:45 ET proof-of-life. Silence is the signal, not the message. |

   Add a closing line stating the two that always warrant attention (both
   CRITICALs) and that a missing heartbeat matters more than any single alert.

   **Keep this table generated from the same source as `TRIGGER_SEVERITY`
   wherever the two overlap.** A `/help` that drifts out of date is worse than
   no `/help`, and this is exactly the kind of hand-maintained list that drifts.

## 5. Formatting

Every reply must be readable on a phone by someone who did not write it.

**Numbers go in a fenced code block.** Telegram renders normal message text in a
proportional font, so any column alignment done with spaces collapses. A
MarkdownV2 fenced block renders monospaced and columns hold. Prose framing
outside the block, aligned figures inside it.

**Escaping differs inside and outside code blocks**, and getting this wrong is
the most likely way a reply fails to send at all. Outside, every character in
`notify._MD_V2_RESERVED` needs a backslash. Inside a fenced block, only
`` ` `` and `\` do -- escaping `.` and `-` inside a block puts literal
backslashes on the screen. Reuse `notify.escape_md_v2` for the outside and add a
separate helper for block content rather than reaching for the same function.

Other rules: one blank line between sections; label then value, never a bare
number; currency with a thousands separator and two decimals; percentages to one
decimal; times as `14:32 ET`, never a bare ISO string; and the leading verdict
line of `/status` in bold so it survives being skimmed.

Promote `notify._send_http` to a public `notify.send_text(token, chat_id, text)`
so the poller does not reach into a private. Command replies are **not** alerts
and must not be written to `alerts.jsonl`; that file answers "did an alert get
delivered", and filling it with `/status` responses destroys its value. Log
command traffic to a new `commands.jsonl` alongside the other event logs:
timestamp, from user id, command, whether it was authorized, whether the reply
was delivered.

## 6. Limit pricing

**This section computes prices. It submits nothing.** The output is a ticket
rendered into a Telegram message and appended to `orders.jsonl`.

New module `src/orders.py`: pure functions, no I/O, no broker import. It takes a
`Decision`, the current marks, and the current share counts, and returns a
ticket per leg -- side, ticker, shares, limit price, order type, and the
resulting notional. Pure because the arithmetic is the part that has to be
right, and pure functions are the part of this codebase that can actually be
tested offline.

### Two order styles, chosen by trigger

The distinction is **whether the trade is reducing risk or correcting drift**,
and it matters more than it looks.

**Drift corrections -- `foil decay band` and `long-short band` -- price at the
band boundary.** These trips say the position wandered outside a tolerance we
chose. There is no urgency in them; the position was acceptable at the boundary
by construction. Resting a limit at the boundary means we only trade if price
comes back to us, and we capture the spread instead of paying it.

**Risk reductions -- `margin de-risk` and `drawdown stop` -- price marketably.**
A passive limit on a de-risk order is an order that may not fill, and the
scenario where it fails to fill is exactly the scenario it exists for: price
moving hard against the position. Use a marketable limit -- through the touch by
a configurable offset, default 25 basis points -- which fills like a market
order but caps the damage from a stale quote or a gapped book. **A resting limit
must never be the mechanism by which we de-risk.**

### Deriving the boundary price

For a drift correction, invert the band condition for the price at which it
sits, holding everything else at its current value.

**Foil decay band.** The condition is
`|short_notional - target| > foil_decay_band x target`, and `short_notional` is
`short_shares x short_price`. So the boundary prices are

```
short_price_boundary = target x (1 +/- foil_decay_band) / short_shares
```

Take the sign on the side the position actually breached. Note what this
implies: if the short leg grew too large, the fix is to buy back shares and the
boundary price is *below* the current mark -- a genuinely passive buy. If it
shrank too small, we sell more shares at a price *above* the mark. Passive in
both directions, which is the intent.

**Long-short band.** The condition is on
`net_delta = long_notional - leverage x short_notional`, which contains two
prices. It is only invertible if one is held fixed -- and here that is exactly
right, because this trip resizes the long leg only:

```
long_price_boundary = (leverage x short_notional +/- long_short_band x target) / long_shares
```

with `short_notional` frozen at its current mark. **Document the freeze in the
code.** A future reader will wonder why one price is treated as a constant, and
the answer is that the rule itself only moves one leg.

### The three things that make this real

**Non-fill has to have a protocol.** A resting limit at the boundary may sit
unfilled while the position drifts further out of band. That is tolerable for a
drift correction and unacceptable if it persists. For this spec: attach a
time-in-force of DAY, state it on the ticket, and have the monitor re-derive on
its normal cycle -- an unfilled proposal simply re-trips and re-alerts. Do not
build automatic escalation now; do write down that a fill-or-escalate policy is
required before orders are ever live, because an unfilled de-risk is the
liquidation scenario.

**Both legs, or the ordering matters.** A foil decay trip moves both legs. If
one fills and the other does not, the position is left with unhedged delta --
and a one-leg-filled-first transition is a live hypothesis for the auto
liquidation in spec 006. This spec does not solve it. It must **render both legs
on one ticket as a single proposal**, so the problem is visible in the artifact
rather than discovered later, and section 8 records leg ordering as a gate on
going live.

**Share rounding is already solved -- do not re-solve it.**
`format_trade_line` derives every dollar figure from the rounded share count and
shows the residual. `orders.py` must do the same, and the ticket and the alert
must agree to the cent. If they disagree, one of them is lying about what will
be sent.

### The placeholder output

The terminal step of the approval loop appends the ticket to `orders.jsonl` with
`status: "placeholder"` and replies with it. Wording must be unambiguous that
nothing was sent -- a stakeholder reading `placed limit order` will believe an
order exists. Use the past-conditional framing:

```
PROPOSED -- not submitted. Enter manually or ignore.

  BUY   TSLL   12 sh  @ limit $18.36   =  $220.32
  SELL  TSLA    1 sh  @ limit $331.05  =  $331.05

  Limit basis: foil decay band boundary (15.0% of target)
  TIF: DAY
  Residual after rounding: -$4.18
```

## 7. The approval loop

An alert that proposes a trade gets an inline keyboard with one button.

1. Trip alert is sent with an inline keyboard: one `Rebalance` button carrying
   `callback_data="req:<decision_id>"`. `decision_id` is short -- Telegram caps
   `callback_data` at 64 bytes -- so use eight hex characters derived from the
   check timestamp, and write it into the `checks.jsonl` row.
2. Someone taps. The poller calls `answerCallbackQuery` **immediately** (Telegram
   shows a spinner and then an error if this takes more than a few seconds) with
   a short "re-deriving" acknowledgement, then appends
   `{kind: "request", decision_id, from_user_id, ts}` to
   `/var/lib/short-lev/intents.jsonl`.
3. The monitor picks the intent up and calls `decision.evaluate()` **fresh**,
   then `orders.py` on the fresh result. If nothing is tripped now, it replies
   "no longer tripped, nothing to do" and stops. **The button carries an intent,
   never a ticket.**
4. If still tripped, the monitor posts the freshly derived ticket with `Confirm`
   and `Cancel` buttons and a stated 60-second expiry.
5. `Confirm` writes a second intent. The monitor validates that the confirm id
   is unexpired and unconsumed, then executes the terminal step from section 6.

**Staleness protocol.** Re-derivation handles correctness; the remaining problem
is that a person tapping an hour-old alert does not know the world moved. So:

- The trip alert states the marks it was computed from and the time.
- On tap, if the originating check is **older than one poll interval**, the
  re-derived proposal must **show the drift explicitly** -- old price, new price,
  and the change in shares -- above the fresh ticket. Not a footnote. The point
  is that the reader notices the numbers are not the ones they tapped.
- If the originating check is **older than four hours or from a previous
  session**, refuse outright and send a fresh alert instead. Do not silently
  re-propose against an alert whose context the reader no longer has.
- When a new trip alert supersedes an earlier one for the same trigger, the
  monitor **edits the keyboard off the old message** (store `message_id` when
  sending). A stale button that cannot be pressed is better than one that can.

**Latency.** The monitor sits in `ib.sleep(900)`, so a naive implementation
leaves someone staring at a tapped button for up to fifteen minutes. Split the
sleep into 10-second slices with an intent check between them. Use `ib.sleep`
for every slice -- `time.sleep` blocks the asyncio loop, which is the failure
mode called out at the top of `monitor.py`, and slicing the wait is exactly the
kind of change where it creeps back in.

**Idempotency.** Double taps, two devices, and Telegram retries all happen.
Persist consumed ids to `/var/lib/short-lev/intents_seen.json` -- in-memory is
not enough, because `Restart=always` means a monitor restart between tap and
confirm is an ordinary event. A consumed id gets an "already handled" reply, not
silence.

**Who may press.** Alerts go to the whole group, so by default anyone in it can
press. Add `TELEGRAM_ACTION_USER_IDS` as an optional comma-separated allowlist
of Telegram user ids, defaulting to empty, which means everyone in the group.
One `in` check, not a role system. Flagged in section 9.

## 8. What must be true before the placeholder becomes an order

Written here so the change is a decision rather than a one-line diff someone
makes on a Friday.

- **The margin investigation resolves.** IBKR force-closed part of the position
  (`liquidation=1`) while the monitor read a positive cushion. Until we know
  whether `MaintMarginReq` is the binding constraint, an executor sizes orders
  against a number that may not be the one that matters.
- **Leg ordering is specified and justified**, per section 6. One live
  hypothesis for that liquidation is the resize itself.
- **A fill-or-escalate policy exists** for resting limits, per section 6.
- **Several tickets have been filled by hand and reconciled** against backtest
  assumptions for slippage and timing -- including how often a boundary limit
  filled at all, which is unknown and is the main empirical question section 6
  raises.
- Gateway comes out of Read-Only API mode, which is its own deliberate step.

---

## Session gate

Do not close this spec until each of these has actually been run. Mark anything
not run as not run.

1. **Unauthorized silence.** Message the bot from an account outside the group.
   No reply. One INFO line in the journal with the chat id.
2. **`migrate_to_chat_id` is handled.** Feed `notify._send_http` a synthetic
   Telegram error payload carrying `parameters.migrate_to_chat_id`; confirm one
   ERROR line naming both the dead and the replacement id. No network needed.
   Confirm nothing auto-rewrites `.env`.
3. **All four commands** return correctly formatted replies in the group, each
   carrying a data-age line, each rendering monospaced columns correctly on a
   phone rather than only in the desktop client.
4. **`/help` covers all nine alert kinds** and its severities match
   `TRIGGER_SEVERITY`.
5. **Arithmetic check on `/positions`.** `shares x price` equals the stated
   notional for both legs. Compare against TWS on the same account.
6. **Staleness.** Stop the monitor, wait past three poll intervals, run
   `/status`. The verdict line reads Stale or Disconnected, not Healthy.
7. **`/status` after a restart.** Restart `short-lev-monitor`; uptime resets and
   `last_error` reflects reality rather than a stale value.
8. **Offset persistence.** Send a command with the bot unit stopped, start it,
   confirm the command is answered exactly once. Restart again, confirm it is
   not answered a second time.
9. **Limit price arithmetic, offline.** For a known synthetic state, hand-check
   both boundary formulas: recompute the notional at the derived price and
   confirm it lands exactly on the band edge. This is the gate that matters most
   in this spec and it needs no market.
10. **Ticket and alert agree.** For one real trip, `format_trade_line` and the
   `orders.py` ticket state the same share counts and the same dollar figures.
11. **De-risk uses a marketable limit.** Force a `margin de-risk` state in a
    synthetic test; confirm the ticket is priced through the touch and not at a
    band boundary.
12. **Approval, happy path.** Force a trip. Tap `Rebalance`, confirm the spinner
    clears within about a second, confirm a freshly derived ticket arrives with
    Confirm/Cancel. Tap `Confirm`. Verify the placeholder reply says nothing was
    submitted, and the `orders.jsonl` row.
13. **Approval, stale path.** Force a trip, wait for the alert, close the band by
    hand, then tap `Rebalance`. The reply says the band is no longer tripped and
    nothing is written to `orders.jsonl`.
14. **Approval, drift disclosure.** Tap a trip alert older than one poll
    interval. The reply shows old price, new price, and the share delta above the
    fresh ticket.
15. **Approval, expiry.** Tap `Rebalance`, wait past 60 seconds, tap `Confirm`.
    Refused. Nothing written.
16. **Idempotency across restart.** Tap `Rebalance`, restart the monitor before
    confirming, then tap `Confirm`. Either refused or handled once -- never
    twice. Then double-tap `Confirm` on a fresh proposal and confirm exactly one
    `orders.jsonl` row.
17. **The invariant.** `grep -n "import broker\|IB(" src/bot.py` returns nothing.
    `grep -rn "placeOrder" src/` returns nothing.
18. **Both units survive a reboot** and the nightly Gateway restart.

## Design decisions for review

- **`TELEGRAM_ACTION_USER_IDS` default.** Empty means anyone in the group can
  press a button that will eventually place orders. Restricting it to one id
  costs nothing now and is awkward to add after people are used to the button.
- **The 25bp marketable offset** on risk-reducing orders is a guess and should be
  sanity-checked against observed TSLL/TSLA spreads.
- **The four-hour hard refusal** on stale alerts is a guess.
- **Whether `/positions` should report average cost at all.** It requires
  `broker.read_position` to carry it, and it may not today.
- **Whether the confirm expiry should be 60 seconds.** Long enough to read a
  ticket on a phone, short enough that the marks have not moved.

---

## Result

**Offline gates pass (2, 9, 10, 11, 17, and the offline halves of 4, 5, 6, 8,
15). Every gate needing the group, the paper account, or the VPS is not run
and is the operator's.** Nothing is committed; the diff is in the working
tree for review.

### What shipped

All of sections 2-7 plus the section 1 `migrate_to_chat_id` handler. New:
`src/orders.py`, `src/bot.py`, `src/approval.py`, `src/runtime_state.py`,
`deploy/short-lev-bot.service`, `scripts/verify_orders.py`,
`scripts/verify_bot.py`. Changed: `src/monitor.py` (widened check_record,
runtime.json, trip keyboards, intent processing, sliced sleep),
`src/notify.py` (public `send_text` with keyboards and message ids, block
escaping, alert catalog, migrate handler), `src/broker.py` (`leg_prices` ->
`leg_details`), `src/events.py` (commands.jsonl, orders.jsonl),
`deploy/install-units.sh`, `.env.example`, `docs/VPS.md`,
`scripts/verify_monitor.py` (checks s, t, u).

`decision.py`, `band.py`, `engine.py`, `config.py` untouched;
`hash_band.py`'s GRAND digest is
`08baa20ac842502aedbb5f647f6ea24cf3128429f17f4a5a1d7d1d0cb85de942`, identical
to `baseline_005_hash.txt`, captured before the first edit and re-checked
after the last. `grep -rn "placeOrder" src/` and
`grep -n "import broker\|IB(" src/bot.py` both return nothing (gate 17), and
verify_bot check i re-runs both mechanically on every offline run.

### Gate status

| # | Gate | Status |
|---|---|---|
| 1 | Unauthorized silence | Offline half PASS (verify_bot b); live send **not run** |
| 2 | migrate_to_chat_id handled | **PASS** (verify_bot a, synthetic payload; no .env write path exists) |
| 3 | Four commands render in the group | **Not run** -- needs the group and a phone |
| 4 | /help covers all kinds, severities match | Offline PASS (verify_bot d, generated from the one table); live render **not run** |
| 5 | /positions arithmetic | Offline PASS (verify_bot g); TWS comparison **not run** |
| 6 | Staleness verdict | Offline PASS (verify_bot e); live stop-the-monitor test **not run** |
| 7 | /status after restart | **Not run** |
| 8 | Offset persistence | Offline round-trip PASS (verify_bot c); live restart sequence **not run** |
| 9 | Limit price arithmetic | **PASS** (verify_orders a-d: both formulas, both directions, exact to 1e-9) |
| 10 | Ticket and alert agree | **PASS** (verify_orders e, under the interpretation below) |
| 11 | De-risk is marketable | **PASS** (verify_orders f, g) |
| 12-16 | Approval round trips | Offline pieces PASS (expiry: verify_monitor u; idempotency files: verify_bot h); live taps **not run** |
| 17 | The invariant greps | **PASS** (by hand and verify_bot i) |
| 18 | Both units survive reboot | **Not run** |

### Deviations from the spec

1. **Gate 10 cannot hold literally, and was resolved by review.** The ticket
   prices at the boundary, the alert at the mark, so the two dollar amounts
   cannot both match "to the cent". Decided 2026-08-13: share counts must be
   identical (both derived from the dollar delta at the current mark,
   rounded), and every dollar figure on each artifact derives from that
   rounded count at its own price. Two further review decisions: the foil
   decay trip's second leg (the underlying, which has no boundary formula)
   rests at the current mark with its basis stated; `TELEGRAM_ACTION_USER_IDS`
   unset means everyone in the group, as section 7 wrote it.
2. **"Nine alert kinds" is ten table rows.** The spec's own /help table lists
   leg_check at both WARNING and INFO. The catalog (`notify.ALERT_KINDS`)
   carries all ten rows; the four trip severities are generated from
   `TRIGGER_SEVERITY`, which moved to `notify.py` -- the bot must render the
   table but must not import the monitor, which drags in the broker.
3. **check_record gained more than the spec's list**: the two band fractions
   (so /positions can state "x% of a y% band" without the bot re-deriving
   params -- the row self-describes), `ts`, `decision_id`, and per-leg
   average cost (review decision: include). `broker.leg_prices` became
   `leg_details` to carry shares and average cost; same read, wider return.
4. **Two modules the spec did not name.** `runtime_state.py`: runtime.json is
   written by the monitor and read by the bot, and neither may import the
   other, so the path resolver and atomic writer needed a shared home.
   `approval.py`: same argument for the intent files. Also one state file the
   spec did not name, `approval_state.json` (active proposal, trip message
   ids, intent cursor) -- kept out of `alert_state.json` for spec 006's
   losing-it-costs-what reasoning.
5. **Confirm expiry is validated at processing time**, not tap time: the
   expiry exists so the marks cannot have moved under the ticket, and they
   keep moving while an intent waits in the file. Worst case a tap near the
   deadline is refused up to one 10-second slice late -- refusal is the safe
   direction.
6. **Request ids are consumed permanently.** After a proposal expires,
   re-tapping the SAME alert's button replies "already handled"; a fresh
   proposal needs a fresh alert, which the standing-trip repeat (60 min)
   provides. Simplest idempotency that satisfies gates 15/16; if the wait
   feels wrong in use, revisit deliberately.
7. **Button-triggered evaluations are logged to checks.jsonl** as ordinary
   rows -- they are real checks, and the fresh proposal needs a decision_id
   that resolves. The intraday-cadence dataset now contains intent-triggered
   rows as well as timer rows.
8. **/positions derives displayed notionals from the displayed two-decimal
   price** (marketValue still drives the band math). Gate 5 says a reader
   multiplies the two numbers shown; with the raw marketValue printed beside
   a rounded price, that multiplication fails by cents.
9. **bot.py uses time.sleep, deliberately.** There is no asyncio loop in that
   process and ib.sleep would require importing ib_async, which it must not.
   The ib.sleep rule is the monitor's, where the sliced sleep uses it for
   every slice; verify_bot i mechanically confirms monitor.py never imports
   time.
10. **install-units.sh installs and enables the fifth unit** -- not in this
    spec's text, but spec 007's reconciliation lesson says a unit the install
    script skips is a hand-fix waiting to be forgotten.

### Left for the operator

- The live gate runs above (1, 3-8, 12-16, 18), including the phone-rendering
  check that fenced columns hold in the mobile client.
- BotFather: confirm privacy mode Enabled, `/setcommands` for the four
  commands, `/setdescription` -- all before the stakeholder joins.
- The 25bp marketable offset and the 60-second expiry remain guesses to
  sanity-check against observed TSLL/TSLA behavior once tickets flow.
- Section 8 stands unchanged: nothing here submits an order, and the list of
  what must be true before the placeholder becomes one is unshrunk.