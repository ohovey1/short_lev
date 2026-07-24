# ROADMAP

Scope gate: if a task isn't here, ask before building.

## v1 — MVP (build in this order)

### 1. Data layer
- [x] `config.py`: pair registry dict (start with `QQQ: {leveraged: SQQQ, underlying: QQQ, leverage: 3}`).
- [x] `data.py`: `get_prices(ticker)` → DataFrame of daily OHLC.
- [x] `_fetch_polygon(ticker)`: pull daily aggregates from Polygon using `POLYGON_API_KEY`.
- [x] Cache to `./cache/{ticker}.csv`; read cache if present, fetch only on miss.

**Done when:** get_prices("SQQQ") returns a clean OHLC DataFrame, and a second call reads from ./cache/SQQQ.csv instead of hitting Polygon.

### 2. Engine
- [x] `engine.py`: pure `position_pnl(...)` — interval P&L (entry -> current prices) for one
  two-leg position. Price legs only; no borrow in `net`.
- [x] `borrow_cost(notional, days=1)` returning 0.0 — a separate pure stub, applied per open
  tranche per day by the backtest (kept wired in so v2 is a fill-in).

**Done when:** a flat interval returns net = 0, and a known-move interval matches a by-hand
calculation. (Engine renamed from the old `daily_pnl` during the multi-day pivot; the math
is unchanged — same formula, entry/current naming.)

### 3. Validate (one pair, from a script — no UI yet)
- [x] Run the engine on QQQ/TQQQ for a handful of intervals, hand-checking numbers
  (`scripts/verify_engine.py`).
- [x] Confirm the signs/magnitudes make sense. NOTE: a single hedged interval's residual is
  embedded cost drag + tracking error, NOT decay — real decay is the multi-day ladder effect
  measured by the backtest (step 4).

**Done when:** for QQQ/TQQQ the synthetic sign/arithmetic checks pass and a real cached
interval shows the two legs roughly cancelling (hedged), matching the by-hand prices.

### 4. Backtest wrapper — overlapping multi-day tranches
The strategy is a multi-day hold, not a daily reset: open one tranche per day at that day's
prices and hold each for `hold_days` days. The ladder of overlapping holds is what captures
leveraged-fund decay (daily-reset = the degenerate `hold_days=1` case, which only scrapes one
interval's cost drag).
- [x] `backtest.py`: maintain a ladder of open tranches (entry prices + age) and a
  `realized_pnl` total. **Must call the engine** — no P&L math of its own.
- [x] Each day: open a tranche (taper at the tail so each completes a full hold); mark every
  open tranche via `position_pnl`; realize + drop a tranche at the close of day d+hold_days;
  apply `borrow_cost` per open tranche per day (0 now).
- [x] Equity(t) = realized_pnl + sum(open tranche marks) - borrow_paid. Equity curve = this
  series; daily P&L = equity.diff().
- [x] Metrics off the curve: total return, max drawdown, worst day.

**Decisions:**
- **Normalization:** per-tranche `notional = base_capital / hold_days`, so total deployed
  capital is constant across `hold_days` and dollar P&L is comparable between windows.
- **Borrow** is charged per open tranche per day (0 in v1), kept visibly in equity.
- **Edges:** warmup ramps up over the first `hold_days-1` days; the tail tapers (we stop
  opening new tranches in the last `hold_days-1` days) so every tranche realizes — symmetric.

**Done when:** for a pair + window + `hold_days` it produces an equity curve; on any
mid-window day exactly `hold_days` tranches are open (fewer at the edges); every P&L number
comes from an engine call (no P&L math in `backtest.py`); and changing `hold_days` changes
the curve.

### 5. UI (last) -- DONE; v1 MVP complete
- [x] `app.py`: pair dropdown (from `config.py`).
- [x] Lookback slider (not hardcoded; backed by `lookback_days` in `run_backtest`).
- [x] Equity-curve chart.
- [x] Metrics table (total return, max drawdown, worst day).
- [x] Visible disclaimer: "Fees (incl. borrow) omitted — results are optimistic and not a verdict."
- [x] Extras: hold_days slider, base_capital input, open-tranche-count chart. Run with
  `streamlit run src/app.py`.

## v2 — backlog (do NOT build yet)
- Support inverse funds (e.g. SQQQ) via signed leverage in config: store leverage with a
  sign and size/direct the hedge from it, so inverse and long funds are both delta-neutral.
  (v1 assumes positive-leverage funds only; shorting them hedges the long underlying.)
  - **Motivating experiment (delta-neutrality control):** the clean inverse-QQQ test is
    `short SQQQ + SHORT QQQ` (NOT long QQQ -- that's +6x double-long). One decay source,
    true mirror of QQQ/TQQQ. If the strategy is delta-neutral, it should profit too; if the
    bull-pair returns were just beta, it should not. Needs the signed-leverage logic above.
  - Separate idea (different strategy, log don't conflate): `short SQQQ + long $3X PSQ`
    avoids shorting the underlying but adds a 2nd decay source (PSQ); and a both-leveraged
    `short TQQQ + long SQQQ` double-decay short-vol variant.
- [x] Fill in the borrow-fee stub (daily borrow charge on the short leg). NOTE: the LETF's
  expense ratio is already in its historical price -- do NOT add it (would double-count).
  Borrow fee is the cost WE pay to short, and is the missing real cost. DONE
  2026-07-03: `engine.borrow_cost` charges `notional * rate_annual * days / 360`;
  `config.PAIRS` carries an indicative `borrow_rate_annual` per pair (hand-refresh from
  IBKR/iBorrowDesk); `backtest.run_backtest` takes `borrow_rate_annual` (defaults to the
  pair's config value) and returns `borrow_paid` + `notional_days`; app.py has a rate
  slider and a borrow-paid metric.
- Add expense ratio as reference data only (to sort/test the high-fee hypothesis), spread,
  dividends.
- More pairs: inverse pairs once signed leverage exists (SPXU, TZA, SQQQ).
  - [x] Single-stock 2x pairs added 2026-07-03 (NVDL/NVDA, TSLL/TSLA, CONL/COIN,
    borrow_rate_annual 0.10) -- no code changes needed, architecture is ticker-agnostic.
- Longer history (swap data source to a paid/keyed tier with multi-year coverage).
- Sharpe / Sortino, rolling stats.
- Parameterize rebalance frequency (daily is hardcoded in v1).
- [x] Data layer: add rate-limit handling (Polygon free tier ~5 req/min; pre-warming many
  tickers at once trips HTTP 429). DONE 2026-07-05: `_fetch_polygon` retries up to 5x on
  429 with a 15s wait, and the price CSVs are now committed seed data (deploys no longer
  cold-fetch 26 tickers -- that was crashing the Streamlit Cloud leaderboard with 429s).
- [x] Cross-pair leaderboard page added 2026-07-03 (`src/pages/Pair_Analysis.py`): gross
  vs net return, borrow paid, max drawdown, worst day, and an analytic breakeven borrow
  rate (`engine.breakeven_borrow_rate`) for every pair in one sortable table.
- [x] Live IBKR indicative borrow rates added 2026-07-03: `data.get_borrow_rates()`
  fetches IBKR's public shortstock list (12h cache, None on failure); the leaderboard
  uses live rates per leveraged ticker with config fallback and shows rate source +
  shares available; every successful fetch also appends per-pair rows to
  `cache/borrow_history.csv` (passive accrual, no reader yet -- future backfill work).
  The config rates are now the fallback tier only. Engine/backtest untouched.
- Borrow-history reader: once `cache/borrow_history.csv` has accrued a few weeks of
  rows, backtest with time-varying borrow instead of one flat rate per run.
- [x] Band-rebalanced single-position backtest DONE 2026-07-05 (`src/band.py`): the
  live-bot strategy as a sibling to the tranche ladder (which stays the reference
  implementation). One continuous position, hedge frozen between trades; a trade fires
  only when the short-notional or net-delta band trips (short-band reset re-neutralizes
  both legs; delta trip re-neutralizes via the long leg only). All P&L through
  engine.py. UI strategy selector on the main page (ladder / band); gates in
  `scripts/verify_band.py` (hand-computed two-segment check, degenerate zero-trade
  check, ladder sanity cross-check).

## v3 — automation (do NOT build yet)
Phases and gates mirror `docs/AUTOMATION.md` -- that doc is the fuller spec; this section is
the trackable checklist version, with an explicit "success looks like" added per phase since
"Done when" alone tells you a phase technically works, not that it's actually safe to move
past.

### Phase 1 — signal-only bot
- [ ] Poll live IBKR quotes for pairs flagged `live: true` in `config.py` (flag TBD, see open
  decisions).
- [ ] Run through existing `band.py` logic unchanged -- no new P&L math.
- [ ] On a band trip: push a notification (channel TBD) with the pair and the recommended
  rebalancing trade. No order submission.
- [ ] Log every check (tripped or not) and every notification sent.

**Done when:** the bot runs on a schedule against IBKR quotes, correctly identifies a band
trip against a known test case, and a notification arrives with the right trade.
**Success looks like:** a full week of scheduled checks with zero missed runs and zero false
trips against hand-verified band math.

### Phase 2 — semi-manual execution
- [ ] Execute notified trades by hand.
- [ ] Reconcile actual fills against backtest assumptions (slippage, fill timing, borrow cost
  realized vs. modeled).
- [ ] Run for several weeks minimum before automating execution.

**Done when:** a handful of live-notified trades have been manually executed and reconciled,
with no unexplained gap versus backtest assumptions.
**Success looks like:** a run of reconciled trades landing within a small, pre-agreed slippage
tolerance of modeled fill price, and realized borrow within a few points of the rate assumed.

### Phase 3 — broker integration (paper)
- [ ] `ib_async` + IB Gateway on Hetzner (or an always-on local machine first).
- [ ] IBC for headless login and daily-restart handling (IBKR resets sessions nightly).
- [ ] Connect to the paper account first. Same Phase 1 signal logic, now auto-submitting
  orders instead of just notifying.
- [ ] **Blocking open decision (see below):** confirm IBKR's current 2FA policy for
  unattended headless logins before committing to Hetzner as the always-on host.

**Done when:** the bot runs unattended against paper for at least a few weeks with no manual
intervention needed to recover from a disconnect or restart.
**Success looks like:** several consecutive weeks unattended, zero missed band checks, zero
manual recoveries needed at the nightly IBKR reset.

### Phase 4 — live capital
- [ ] Switch config (host/port/credentials) from paper to live -- no logic changes.
- [ ] Start at small size, scale up gradually.
- [ ] Run in parallel with, not instead of, the regime stress-testing already planned for the
  strategy itself (deep-history runs, borrow-rate stress multiples) -- automating an
  unstress-tested strategy compounds risk rather than reducing it.

**Done when:** to be defined once Phase 3 is complete -- don't backfill this now.
**Success looks like:** live turnover/trade count tracking backtest-predicted levels for the
same window, no unexplained slippage, and only reached once the strategy's own regime
stress-testing is done -- "it automated cleanly" is not success if the underlying strategy is
still unvalidated against adverse regimes.

## Open decisions
- **`target` derivation** -- fixed cash figure vs. equity-scaling over time. No decision yet;
  do not silently add reinvestment/scaling logic (see CLAUDE.md strategy section).
- **Per-pair margin multiplier** -- needs to land in `config.py` once real position sizing is
  implemented; margin requirements differ meaningfully by pair (single-stock leveraged ETFs
  vs. broad-index leveraged ETFs get different house margin treatment).
- **Live-vs-backtest pair flag** -- which of the 13 pairs actually trade live vs. stay
  backtest/research-only; needs a field in `config.py`.
- **Notification channel** -- Slack vs. email, not yet chosen.
- **VPS vs. always-on local machine** for Phase 3, not yet chosen.
- **IBKR 2FA / unattended login policy** -- blocking for Phase 3. IBC can automate credential
  entry, but mobile-push 2FA may not have a clean unattended bypass on standard retail
  accounts. Needs direct confirmation (security code card exemption vs. accepting a manual
  intervention window at the nightly reset) before Hetzner is treated as viable for Phase 3.
- **Portfolio Margin upgrade timing** relative to the $100k threshold -- depends on capital
  available at automation time, not blocking now. Also worth noting when it comes up:
  Portfolio Margin's correlation offsets are for broad-based index products -- single-stock
  pairs (TSLA/TSLL etc.) may see little or no benefit from it even above $100k.