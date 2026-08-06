# ROADMAP

Scope gate: if a task isn't here, ask before building.

`docs/SPEC.md` is the source of truth for the strategy itself. This file tracks
*what gets built and in what order*. Per-session detail lives in `specs/`.

## Shipped

- **v1 (2026-06)** -- data layer (Polygon + cache), pure engine, tranche-ladder
  backtest, Streamlit UI. Verify scripts for engine and backtest.
- **v2 (2026-07)** -- borrow cost as a real daily accrual, live IBKR indicative
  borrow rates, single-stock 2x pairs, cross-pair leaderboard, band-rebalanced
  strategy (`band.py`) as the live design, margin multiplier + cushion tracking,
  capital utilization.
- Detail is in git history and `SCRATCHPAD.md`. Do not re-litigate it here.

---

## Phase 0 -- reconcile with the spec

Spec: `specs/spec_001.md`. No new features. The repo currently contradicts
`docs/SPEC.md` in several places, and shipping on top of that just moves the
contradiction downstream.

- [ ] Rename bands to spec terminology (`delta_band` -> `long_short_band`,
      `short_band` -> `foil_decay_band`).
- [ ] Fix stale claims in `CLAUDE.md`, `docs/strategy.md`, `app.py` docstring.
- [ ] Record the daily-close check-cadence assumption where the numbers are read.
- [ ] Standardize the Portfolio Margin threshold on $110,000 NLV plus options approval.

**Done when:** no file in the repo contradicts `docs/SPEC.md`, and
`scripts/verify_band.py` passes unchanged in behavior.
**Success looks like:** a fresh reader can go from `CLAUDE.md` to `SPEC.md` to
the code without hitting a single statement that turns out to be false.

---

## Phase 0.5 -- make the backtest model the actual strategy

The spec defines closing logic. The code has none. Until this lands, every
reported return and drawdown describes a strategy that never de-risks and never
stops out -- which is not the strategy in the spec, and not what the numbers
going to a stakeholder should describe.

### 0.5a -- closing logic
- [ ] Drawdown stop: 10% peak-to-trough on equity, closes both legs, stays flat.
- [ ] Margin de-risk: on negative cushion, recompute target from current equity
      and reset both legs. Ratchets down only.
- [ ] Surface both in the metrics table and the leaderboard (stopped? when? how
      many de-risk events?).

**Done when:** a hand-constructed price path that breaches each rule produces the
exact expected position sizes, verified in `scripts/verify_band.py`.
**Success looks like:** re-running all 13 pairs shows which ones would have
stopped out, and the leaderboard ranking is re-derived from post-stop numbers.

### 0.5b -- intraday check cadence
- [ ] Model band checks more frequently than daily close, or quantify the gap.

**Done when:** the difference in trade count and turnover between daily-close and
intraday checking is measured for at least three pairs.
**Success looks like:** we can state a defensible multiplier on modeled
transaction costs instead of a hand-wave.

### 0.5c -- band grid search
- [ ] `scripts/` tool sweeping `long_short_band` x `foil_decay_band`, reporting
      net return, drawdown, trade count, and breakeven borrow per cell.
- [ ] Not a UI feature.

**Done when:** the sweep runs across all pairs and outputs a sortable table.
**Success looks like:** parameter choice is defended by a surface, not a default,
and flat regions are visibly preferred over sharp peaks.

---

## Phase 1 -- signal-only bot

- [ ] Poll live IBKR quotes for pairs flagged `live: true` in `config.py`.
- [ ] Run through `band.py` logic unchanged -- no new P&L math.
- [ ] On a band trip or closing trigger: push a notification with the pair and
      the recommended trade. No order submission.
- [ ] Log every check (tripped or not) and every notification sent.

**Done when:** the bot runs on a schedule against IBKR quotes, correctly
identifies a band trip against a known test case, and a notification arrives with
the right trade.
**Success looks like:** a full week of scheduled checks with zero missed runs and
zero false trips against hand-verified band math.

## Phase 2 -- semi-manual execution

- [ ] Execute notified trades by hand.
- [ ] Reconcile actual fills against backtest assumptions (slippage, fill timing,
      realized vs. modeled borrow).
- [ ] Run for several weeks minimum before automating execution.

**Done when:** a handful of live-notified trades have been executed and
reconciled with no unexplained gap versus backtest assumptions.
**Success looks like:** reconciled trades land within a pre-agreed slippage
tolerance, and realized borrow is within a few points of the modeled rate.

## Phase 3 -- broker integration (paper)

- [ ] `ib_async` + IB Gateway on a VPS (or an always-on local machine first).
- [ ] IBC for headless login and the nightly IBKR session reset.
- [ ] Paper account first. Same Phase 1 signal logic, now auto-submitting.
- [ ] **Blocking open decision:** confirm IBKR's current 2FA policy for
      unattended headless logins before committing to a VPS host.

**Done when:** the bot runs unattended against paper for at least a few weeks
with no manual intervention to recover from a disconnect or restart.
**Success looks like:** several consecutive weeks unattended, zero missed checks,
zero manual recoveries at the nightly reset.

## Phase 4 -- live capital

- [ ] Switch host/port/credentials from paper to live -- no logic changes.
- [ ] Start small, scale gradually.
- [ ] Runs in parallel with, not instead of, regime stress-testing.

**Done when:** to be defined once Phase 3 is complete.
**Success looks like:** live turnover and trade count track backtest-predicted
levels, no unexplained slippage, and the strategy's own regime stress-testing is
already done. "It automated cleanly" is not success if the strategy is still
unvalidated against adverse regimes.

---

## Open decisions

- **Notification channel** -- Slack vs. email. Phase 1 decision.
- **VPS vs. always-on local machine** for Phase 3.
- **IBKR 2FA / unattended login policy** -- blocking for Phase 3. IBC automates
  credential entry, but mobile-push 2FA may have no clean unattended bypass on
  retail accounts. Needs direct confirmation before a VPS host is viable.
- **Portfolio Margin timing** relative to the $110,000 NLV plus options approval
  threshold. Note that PM's correlation offsets are for broad-based index products
  -- single-stock pairs may see little benefit even above the threshold.
- **Auxiliary collateral sleeve** (BRK, GLD) to support the margin cushion.
  Scope-cut for now; changes the margin model materially.
- **SCRATCHPAD retention** -- currently unbounded at 27KB and read every session.
  Trim to recent sessions and archive the rest, or leave it.

---

## Backlog (not scheduled)

- Inverse funds via signed leverage in config. Motivating control experiment:
  `short SQQQ + short QQQ` is the clean inverse-QQQ test (long QQQ would be
  double-long). If the strategy is truly delta-neutral it should profit there
  too; if the bull-pair returns were beta, it should not.
- Deep-history backtests (TQQQ/UPRO back to 2009-2010) -- needs a paid data tier.
- Synthetic LETF simulation from underlying returns, validated against real fund
  overlap periods.
- Borrow-rate stress testing at 1x / 2x / 3x current rates.
- Borrow-history reader: time-varying borrow from `cache/borrow_history.csv`
  instead of one flat rate per run.
- Expand the pair universe beyond the current tech/crypto-beta concentration.
- Sharpe / Sortino, rolling stats.
- Next-open fills instead of same-close execution.