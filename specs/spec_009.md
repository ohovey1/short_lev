# Spec 009 -- What actually binds: the margin investigation

**Phase:** 1b (unblocking)
**Depends on:** spec 005 (margin model), spec 008 (shipped)
**Estimated:** one session of code, then a waiting period. This spec does not
close in one sitting and should not pretend to.

## Why

IBKR force-closed part of a live paper position while the monitor reported a
positive margin cushion. Everything downstream is blocked on that: the executor
sizes orders against a margin number, the stakeholder presentation describes a
risk control, and both are unsafe to build on a model that has already been
wrong once in the direction that costs money.

Spec 005 left a residual it explained as a small house add-on above the
regulatory minimum -- four observations, all the same sign, 1.26% to 1.70%. A
reading taken 2026-08-13 kills that explanation:

| long | short | ibkr_maint | modeled | residual | residual % |
|---|---|---|---|---|---|
| 9871.25 | 4642.55 | 5342.81 | 5253.34 | +89.47 | 1.70% |
| 9254.86 | 4678.20 | 5188.72 | 5120.64 | +68.09 | 1.33% |
| 9222.36 | 4683.95 | 5180.59 | 5115.96 | +64.63 | 1.26% |
| 9212.61 | 4672.45 | 5178.15 | 5106.62 | +71.53 | 1.40% |
| **14249.31** | **7211.67** | **7889.33** | **7889.33** | **+0.00** | **0.00%** |

A fixed dollar add-on would have stayed near $70 at a position half again as
large. A proportional one would have grown. It went to **exactly zero, to the
cent** -- IBKR is now charging precisely `0.25 x long + 0.60 x short`.

So the requirement is not a stable function of our positions. It moved. And a
maintenance requirement that moves on its own is a much better explanation for
a liquidation at positive modeled cushion than any error in the formula's shape,
which spec 005 already fixed and validated.

That reframes the question. It is not "is our formula right" -- at this instant
it is exactly right. It is **"what quantity does IBKR actually liquidate
against, and how does it move when we are not looking."**

### The honest constraint on this spec

The liquidation has already happened and we did not log the fields that would
explain it. Some of it is recoverable from Client Portal; some is gone. So this
spec has two halves and they should not be confused:

- **Instrumentation** is fully achievable this session. The next such event must
  be diagnosable from our own logs alone.
- **Forensics** is best-effort against records IBKR retains.

**Do not let a plausible story from half the evidence close this out.** The
failure mode here is concluding "it was probably X" and unblocking the executor
on a guess. If the forensics are inconclusive, say so and leave the gate shut.

## Out of scope (do not build)

- **The executor, or `placeOrder`.** Unchanged from spec 008 section 8.
- **Portfolio Margin.** A different model above $110k NLV; not our regime.
- **Refitting rates to observations.** Spec 005 declined to fit constants to
  noisy paper data and was right. The new data point makes fitting worse, not
  better -- you would now be fitting a time-varying quantity with five samples.
- **Changing `decision.py`.** Section 4 changes what the *caller* passes into
  `margin_required`. That seam exists precisely so this is not a `decision.py`
  change, and it must stay that way.
- Backtest re-runs. Section 6 records the implication; acting on it is its own
  spec.

---

## 1. Instrument first

The monitor reads exactly two account fields today: `NetLiquidation` and
`MaintMarginReq`. That is why the last event was undiagnosable.

**Step one, before any code: dump everything once.** Connect and print every
`ib.accountValues()` tag for the account, with values, to a file committed to
`docs/`. Field availability varies by account type and region, and the rest of
this section should be written against what actually comes back rather than
against what the documentation promises.

Then widen `broker.read_position` to carry the account fields below, log them
every cycle, and add them to `check_record`. Expected tags, to be confirmed
against the dump:

| Field | Why it might be the binding one |
|---|---|
| `ExcessLiquidity` | IBKR's own cushion figure. If this ever disagrees with `NetLiquidation - MaintMarginReq`, our cushion is not IBKR's cushion, and that alone would explain everything. |
| `SMA` | Special Memorandum Account. **A Reg T account is liquidated when SMA goes negative at end of day, independent of maintenance margin.** This is the strongest single candidate for "liquidated with positive cushion." |
| `LookAheadMaintMarginReq`, `LookAheadExcessLiquidity` | The requirement as of the next calculation. A house rate change lands here before it lands in the current figure. |
| `FullMaintMarginReq`, `FullExcessLiquidity` | The "full" variants can differ from the plain ones; if they do, which one is enforced is exactly our question. |
| `InitMarginReq` | Not the liquidation trigger, but its movement dates a rate change. |
| `GrossPositionValue`, `Cushion` | Cheap, and `Cushion` is IBKR's own ratio. |

**Log the disagreements, not just the values.** The useful derived series is
`ExcessLiquidity - (NetLiquidation - MaintMarginReq)` and the per-field
implied rate. A number that is always zero is a settled question; a number that
moves is the answer.

**Alert on rate movement.** Add a `margin regime change` alert kind: when the
implied short or long rate shifts by more than a threshold between consecutive
checks, send a WARNING. Right now a house rate could double overnight and the
first we would know is a liquidation. This is the single highest-value line in
the spec, because it converts an invisible risk into a notification.

## 2. Forensics

Best-effort, in Client Portal, on the account and date of the event.

- **The activity statement for that day.** Liquidation trades carry an IBKR
  notation distinguishing them from ours. Get the exact timestamp, the symbol,
  and the quantity closed. **Which leg** IBKR chose is itself evidence: closing
  the short says margin or borrow, closing the long says something else.
- **The margin risk notification.** Whatever it named as the breached quantity
  is the most direct evidence available, and it may name the constraint outright.
- **The current house requirement for TSLA and TSLL** on the margin
  requirements page, against the 25% / 60% we assume. If today's page reads 25
  and 60, and the requirement was different at the time of the event, the
  time-varying hypothesis is confirmed rather than inferred.
- **Our own `checks.jsonl`** around the event. It has `ibkr_maint` per cycle
  going back, so the residual series over time can be reconstructed for every
  cycle we logged -- which is the same table as the Why section, extended. If
  the residual jumped before the event, that is the smoking gun and it is
  already in our data.

## 3. Hypotheses, and what discriminates them

Enumerated so the investigation cannot quietly settle on the first plausible
one. Each needs a discriminating observation, not a story that fits.

1. **SMA went negative.** Discriminator: SMA in the logs, and whether the
   liquidation timestamp is end-of-day rather than intraday.
2. **A house rate rose, then fell back.** Discriminator: the residual series in
   `checks.jsonl` around the event; the current requirements page. Consistent
   with the 1.4% -> 0.00% shift already observed.
3. **`MaintMarginReq` is not what is enforced** -- a Look-Ahead or Full variant
   is. Discriminator: field disagreement in the logs, once collected.
4. **Not a margin event at all: a short-sale buy-in or borrow recall.** TSLL is
   a leveraged ETF and can go hard-to-borrow; a recall closes the short
   regardless of cushion. Discriminator: which leg closed, the trade notation on
   the statement, and the borrow rate at the time. **This hypothesis is easy to
   overlook because we went looking for a margin bug**, and the margin risk
   notification makes it feel settled when it is not.
5. **A transient breach during a two-leg resize.** One leg filled, the other did
   not, and the position was briefly unhedged and margin-hungry. Discriminator:
   the event timestamp against our own rebalance timestamps. This is the
   hypothesis that most directly constrains the executor's leg ordering.
6. **Concentration or volatility add-on** applied at a size threshold.
   Discriminator: the residual as a function of position size across the whole
   `checks.jsonl` history.

These are not mutually exclusive. 2 and 5 together would explain an event that
neither explains alone.

## 4. Once the binding constraint is known

`decision.PositionState.margin_required` is a field filled by the caller. That
seam was built for exactly this, and it is why nothing here touches
`decision.py`.

When the evidence names the binding quantity, the monitor passes **that**, or
conservatively `max()` across the candidates. Do not make this change on
inference. Instrument, watch for a defined observation period, then change it
once with the evidence written into the Result.

If the max is taken, `margin_multiplier` -- the sizing denominator -- has to be
reconsidered alongside it. Sizing against one quantity and de-risking against a
stricter one produces a position that is margin-safe and systematically
undersized, which is a real cost, not a free safety margin.

## 5. Folded in: `orders.py` long-leg limits are marketable, not passive

Found reviewing spec 008. The formula is right -- it reproduces the band edge
exactly -- but the interpretation is wrong on the long leg:

```
foil: short too big    BUY TO COVER   mark  10.62  limit   9.80   rests
foil: short too small  SELL           mark   6.25  limit   7.24   rests
l-s:  net delta +      SELL           mark 360.00  limit 357.05   IMMEDIATE
l-s:  net delta -      BUY            mark 320.00  limit 322.95   IMMEDIATE
```

All four are labelled `passive boundary`. The cause is structural rather than a
coding error: reducing a **short** leg's notional means buying, and the boundary
sits at a lower price, so the order rests. Reducing a **long** leg's notional
means selling, and the boundary still sits at a lower price -- so it crosses.
The sign of the position flips the relationship. Spec 008 asserted the passive
framing for both bands without checking the long case; that error is the spec's,
not the implementation's.

The consequence is a slippage cap looser than the one deliberately chosen: at
trip time the boundary sits near the mark, but it widens as drift accumulates
between polls, reaching 82bp in the case above against the 25bp used for orders
meant to be aggressive.

**Fix:** route the long leg of a long-short trip through the marketable path at
`MARKETABLE_OFFSET_BP`. Tighter, honest, and it fills either way.

**Add a permanent direction assertion to `verify_orders.py`:** anything labelled
passive must satisfy `limit < mark` for sells and `limit > mark` for buys. That
assertion is what would have caught this.

This matters now rather than at go-live because **Phase 2 is hand-executing
these tickets.** A limit annotated passive that fills instantly is a small real
money event, and it corrupts the fill-rate data -- the main empirical question
Phase 2 exists to answer is how often a boundary limit fills at all, and a limit
that always fills answers it falsely.

## 6. Folded in: state paths must fail loudly

Third occurrence of the same shape. `peak_equity` inside the repo tree (spec
007, silently disabled the drawdown stop after a re-clone); `runtime.json` and
the three approval files defaulting to `/opt/short_lev/data/` on the 2026-08-13
deploy (read-only under `ProtectSystem=strict`, so the approval loop would have
silently swallowed every button press).

The recurring cause: an unset variable produces a **plausible** path instead of
an error, and the resulting failure is non-fatal by design.

For files that only make sense in the state directory -- `MONITOR_STATE_PATH`,
`ALERT_STATE_PATH`, `RUNTIME_STATE_PATH`, `BOT_OFFSET_PATH`, `INTENTS_PATH`,
`INTENTS_SEEN_PATH`, `APPROVAL_STATE_PATH`, `EVENT_LOG_DIR` -- **fail at startup
with a clear message when unset**, the way `MONITOR_BASE_CAPITAL` already does.
Keep the repo-relative default available only under an explicit opt-in
(`SHORT_LEV_DEV=1`) so local runs still work.

The write path stays never-raises. Losing telemetry mid-run must not stop a live
position; that is a different question from starting up misconfigured.

## 7. What this does not unblock

Even a clean answer leaves spec 008 section 8 mostly intact: leg ordering still
needs specifying, the fill-or-escalate policy still does not exist, and no
ticket has been filled by hand. **Resolving the margin question removes one gate
of four.** Written here because the temptation on closing this spec will be to
treat the executor as unblocked.

---

## Session gate

1. **The full `accountValues()` dump exists** in `docs/`, dated, with the
   account redacted.
2. **Every field in section 1 is logged every cycle** and appears in
   `check_record`.
3. **The disagreement series is logged**: `ExcessLiquidity` against our derived
   cushion, per cycle.
4. **A regime-change alert fires** on a synthetic rate shift. Offline test.
5. **`/account` surfaces the new fields**, including IBKR's own `ExcessLiquidity`
   beside our derived cushion.
6. **The residual series is reconstructed** from the full `checks.jsonl`
   history and plotted or tabulated against date and position size. This is
   already-collected data and needs no market.
7. **The activity statement for the event date has been retrieved**, or its
   unavailability recorded.
8. **Each of the six hypotheses is marked** confirmed, excluded, or
   undetermined, with the discriminating observation named. Undetermined is an
   acceptable outcome; unexamined is not.
9. **Long-leg direction fix** shipped, with the permanent assertion in
   `verify_orders.py`. All prior order checks still pass.
10. **Unset state paths fail at startup** with a message naming the variable;
    `SHORT_LEV_DEV=1` still permits repo-relative defaults.
11. **The monitor still runs unattended** across a nightly Gateway restart with
    all the new fields.

## Design decisions for review

- **The observation period before changing `margin_required`.** Long enough to
  catch a rate move, short enough not to block the presentation indefinitely.
- **The regime-change alert threshold.** Too tight and it fires on rounding; too
  loose and it misses the move that matters.
- **Whether to take `max()` across candidate constraints** or wait for a single
  identified one. Conservative versus correctly sized, per section 4.
- **Whether a time-varying house rate invalidates the backtest's fixed
  `long_rate`/`short_rate`.** If the requirement moves, the backtest understates
  breach-days, and the "zero breach-days at 75% utilization" result -- which the
  stakeholder presentation leans on -- is weaker than stated. Do not put that
  claim in front of the stakeholder until this is settled.

---

## Result

<!-- Filled in after the session. -->