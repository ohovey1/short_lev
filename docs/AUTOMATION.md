# Automation — spec

Scope gate: if a task isn't here, ask before building. This spec covers getting from
the current backtest-only repo to a live, broker-connected bot. Do NOT build broker
integration until the signal-only phase is validated.

## Current state (as of this doc)

- No broker/execution code exists anywhere in the repo. No `ib_async` dependency, no
  live-runner, no order submission logic.
- `data.get_borrow_rates()` is the only existing touchpoint with IBKR — pulls the
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

- Notification channel: Slack vs. email — not yet chosen.
- VPS vs. always-on local machine for Phase 3 — not yet chosen.
- Timing of Portfolio Margin upgrade relative to the $110,000 NLV plus options
  approval threshold — depends on capital available at automation time, not a
  blocking decision now.