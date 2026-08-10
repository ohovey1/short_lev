# ROADMAP

Scope gate: if a task isn't here, ask before building.

`docs/STRATEGY_SPEC.md` is the source of truth for the strategy itself. This file
tracks *what gets built and in what order*. Per-session detail lives in `specs/`.

## Shipped

- **v1 (2026-06)** -- data layer (Polygon + cache), pure engine, tranche-ladder
  backtest, Streamlit UI. Verify scripts for engine and backtest.
- **v2 (2026-07)** -- borrow cost as a real daily accrual, live IBKR indicative
  borrow rates, single-stock 2x pairs, cross-pair leaderboard, band-rebalanced
  strategy (`band.py`), margin multiplier + cushion tracking, capital utilization.
- **spec 001 (2026-08)** -- repo reconciled with `docs/STRATEGY_SPEC.md`. Bands
  renamed to spec terminology, scratchpad restructured, specs workflow adopted.
- Detail is in git history and `docs/history/`. Do not re-litigate it here.

---

## Scope note (2026-08)

The end goal is still a fully autonomous trading bot. The near-term goal is
narrower and deliberately so: a **monitoring bot** that watches a manually-opened
position and alerts. No order submission. One pair (TSLA/TSLL). This buys real
operational experience -- connection reliability, live margin numbers, actual
trip frequency -- before execution complexity enters the engine.

Two components eventually: **monitor** (reads state, decides, emits) and
**executor** (acts on decisions). Only the monitor gets built now. The seam
between them is that the monitor returns a structured decision and never sends
or trades directly.

---

## Phase 0.5a -- closing logic

Spec: `specs/spec_002.md`. Drawdown stop and margin de-risk as real backtest
actions. **Do this first** -- the monitor needs to alert on cushion and drawdown,
and those triggers must be defined in one place, not two.

**Done when:** gates in spec 002 pass.
**Success looks like:** the leaderboard is re-derived from numbers that include
stop-outs, and we know which pairs would have stopped.

## Phase 1a -- extract the decision rule

Spec: `specs/spec_003.md`. Pull the point-in-time trip decision out of
`band.py`'s daily loop into a pure function taking current state and returning a
decision. The backtest loop calls it; the monitor will too.

**Done when:** backtest output is numerically identical to before the extraction.
**Success looks like:** exactly one definition of "is a band breached" exists in
the codebase, and adding the monitor cannot create a second.

## Phase 1b -- monitor core (local)

- [ ] Connect to IBKR paper via `ib_async`. Confirm the 2FA situation on the way
      through -- see open decisions.
- [ ] Read TSLA/TSLL positions, `NetLiquidation`, `MaintMarginReq`.
- [ ] On startup: derive target from MONITOR_BASE_CAPITAL; persist peak_equity only.
- [ ] Run the phase 1a decision function on a timer. Log every check.
- [ ] Console output only. No Telegram yet.

Note: the foil decay verdict at startup is vacuous -- drift from an
adopted-at-startup target is zero by construction. It becomes meaningful once the
bot has been running. Net delta and margin cushion are meaningful immediately.

**Done when:** the bot runs a full session against paper, its numbers agree with
what TWS shows, and a hand-forced delta drift produces the expected trip.
**Success looks like:** modeled `margin_multiplier` and IBKR's actual
`MaintMarginReq` logged side by side, so we finally know how far off the
estimates are.

## Phase 1b.5 -- margin model  [SHIPPED 2026-08-10]

Spec: `specs/spec_005.md`. Live observation showed the modeled multiplier was
both the wrong value and the wrong shape. Sizing depends on it, so it preceded
alerting. Both fixed: rates are now per-leg and maintenance-based,
`margin_required` is two-term, and `margin_multiplier` is derived rather than
stored. Single-stock targets grew 45.5%; the leaderboard moved.

**Open, deferred out of spec 005 on purpose:** `capital_utilization` = 0.75 was
calibrated against the *old* margin numbers and has not been revisited. Fresh
breach-day counts at 0.75 and 1.00 are in spec 005's Result and are the input
to that decision.

## Phase 1c -- Telegram sink

- [ ] Decisions from 1b routed to Telegram. The monitor still never calls
      Telegram directly -- something else consumes its output.
- [ ] Alert on band trip, negative cushion, drawdown threshold, connection loss.
- [ ] Deduplicate. A tripped band that stays tripped must not alert every cycle.

**Done when:** a forced trip produces one Telegram message carrying the pair,
trigger, current state, and recommended trade.

## Phase 1d -- VPS deploy

- [ ] **Blocked on the 2FA answer.** Do not provision until 1b has confirmed
      unattended login is possible.
- [ ] Hetzner + IB Gateway + IBC. Handle the nightly ~11:45 PM EST reset.
- [ ] Restart recovery: the state file must survive, and the bot must report what
      it restored rather than silently re-adopting.

**Done when:** several consecutive weeks unattended, zero missed checks, zero
manual recoveries at the nightly reset.

## Phase 2 -- semi-manual execution

- [ ] Execute alerted trades by hand; update the state file by hand.
- [ ] Reconcile fills against backtest assumptions (slippage, timing, realized
      vs. modeled borrow).
- [ ] Reconcile `target` after manual rebalances -- the deferred problem from 1b.

**Done when:** several live-alerted trades executed and reconciled with no
unexplained gap versus backtest assumptions.

## Phase 3 -- executor

- [ ] Order submission against paper, consuming the same decisions the monitor
      already emits.
- [ ] Multi-pair.

## Phase 4 -- live capital

- [ ] Paper to live is host/port/credentials only, no logic change.
- [ ] Start small. Runs in parallel with, not instead of, regime stress-testing.

---

## Deferred backtest work

Slid back behind the monitor. Phase 1b produces real trip-frequency data that
makes the first of these much easier to calibrate.

- **Intraday check cadence** -- model band checks more often than daily close, or
  quantify the gap. Backtest trade counts are currently a lower bound.
- **Band grid search** -- sweep `long_short_band` x `foil_decay_band`, reporting
  net return, drawdown, trade count, and breakeven borrow per cell. A `scripts/`
  tool, not a UI feature.

---

## Open decisions

- **IBKR 2FA / unattended login** -- blocking for 1d, answerable during 1b. IBC
  automates credential entry, but mobile-push 2FA may have no clean unattended
  bypass on retail accounts. Check before provisioning a VPS.
- **Portfolio Margin timing** relative to the $110k NLV threshold. PM's
  correlation offsets are for broad-based index products -- single-stock pairs
  may see little benefit even above the threshold.
- **Auxiliary collateral sleeve** (BRK, GLD) to support the margin cushion.
  Scope-cut; changes the margin model materially.
- automatic sizing on deposit — detect and alert only, see STRATEGY_SPEC section 1.
- **Paper vs live margin.** The ~1.11 observed multiplier is a paper reading.
  IBKR's paper engine may be more permissive. Confirm on a funded account before
  any live sizing.
- **Commission modelling.** ~$1/leg observed, ~4bp per rebalance at current
  size. The backtest models zero. Decide whether to add it before or after the
  band grid search -- it changes the optimal band width.

---

## Backlog (not scheduled)

- Inverse funds via signed leverage. Motivating control experiment:
  `short SQQQ + short QQQ` is the clean inverse-QQQ test. If the strategy is
  truly delta-neutral it should profit there too; if the bull-pair returns were
  beta, it should not.
- Deep-history backtests (TQQQ/UPRO back to 2009-2010) -- needs a paid data tier.
- Synthetic LETF simulation from underlying returns, validated against real fund
  overlap periods.
- Borrow-rate stress testing at 1x / 2x / 3x current rates.
- Borrow-history reader: time-varying borrow from `cache/borrow_history.csv`.
- Expand the pair universe beyond the current tech/crypto-beta concentration.
- Sharpe / Sortino, rolling stats.
- Next-open fills instead of same-close execution.