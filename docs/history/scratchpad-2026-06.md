# SCRATCHPAD archive -- 2026-06

Entries moved verbatim from SCRATCHPAD.md during the spec_001 session
(2026-08-06). Text is unedited; see SCRATCHPAD.md for the retention policy.

---

### 2026-07-03 (live IBKR borrow rates on the leaderboard)
**Did:**
- `data.get_borrow_rates()`: IBKR indicative borrow rates, DataFrame indexed by ticker
  with fee_rate, available, fetched_at. `_fetch_ibkr_borrow()` downloads usa.txt from
  IBKR's public shortstock FTP (user "shortstock", empty password, pipe-delimited;
  line 1 = #BOF stamp, line 2 = column header, last line = #EOF footer). Cache
  `cache/borrow_rates.csv` reused under 12 hours; any fetch/parse failure returns None
  and callers fall back to config rates.
- **CONVENTION -- FEERATE is in PERCENT in the raw file** (e.g. TQQQ "0.8175" means
  0.8175%/yr). `_fetch_ibkr_borrow` divides by 100 once, so `fee_rate` everywhere
  downstream (cache, history, page, backtest calls) is an annualized FRACTION
  (0.008175), matching config's `borrow_rate_annual` convention. Do not divide again.
  AVAILABLE can be ">10000000" -- the ">" is stripped before int conversion.
- Host note: the spec named ftp3.interactivebrokers.com but it times out from this
  network (port-21 probe confirmed); ftp2.interactivebrokers.com serves the identical
  file. `IBKR_BORROW_HOSTS` tries ftp2 first, ftp3 as fallback -- flip the list if that
  ever inverts.
- Pair_Analysis.py: net-rate precedence per pair = sidebar override > live rate for the
  leveraged ticker > config fallback. New "Rate source" and "Shares available" columns
  (placed before "Borrow rate (used) %" so the used rate stays beside breakeven);
  caption shows the fetch timestamp, warning shown when falling back to config. A
  st.cache_data(ttl="1h") wrapper around get_borrow_rates keeps a failed fetch from
  re-paying the FTP timeout on every rerun.
- Passive history: every successful fetch appends one row per PAIRS ticker (date,
  ticker, fee_rate, available) to `cache/borrow_history.csv`, skipping tickers already
  logged today. No reader yet -- accrues for future time-varying-borrow backfill.
- Engine/backtest untouched, per scope.

**Verified:**
- Gate 1: TQQQ 0.82% / UPRO 1.28% (index funds well under 5%); second call within 12h
  read the cache (0.03s with the host list deliberately broken); no cache + no host ->
  None.
- Gate 2: all 13 rows source=live with FTP reachable; TQQQ hand-check gross - net =
  53.364583 = notional_days * 0.008175 / 360 = borrow_paid exactly; with cache moved
  aside + hosts broken the page renders all rows on config rates with the warning.
- Gate 3: fresh fetch -> exactly 13 history rows for today; same-day refetch (rates
  cache deleted, history kept) -> still 13, no duplicates.
- verify_engine.py + verify_backtest.py pass; app boots headless, both pages 200.
- Live rates vs config placeholders diverge notably: TNA 6.98% live vs 1% config,
  UDOW 4.84%, SOXL 5.70%, ERX 5.71% -- net returns on the leaderboard shifted down
  accordingly for those pairs.

**Next:**
- Let borrow_history.csv accrue; then a reader for time-varying borrow in the backtest
  (logged in ROADMAP).
- Consider surfacing the live rate on the main page's borrow slider default too.

**Open questions / blockers:**
- None.

---

### 2026-07-03 (leaderboard fixes: percent formatting, config-rate column, drawdown as %)
**Did:**
- Fixed a percent-formatting bug in `src/pages/Pair_Analysis.py`: `Gross return %` and
  `Net return %` stored raw fractions (e.g. 0.1544) but the column_config used a printf
  format (`"%.2f%%"`), which does NOT auto-multiply by 100 the way Python's `.2%`
  string-format does -- so 0.1544 rendered as "0.15%" instead of "15.44%". Fix: store
  every percent column pre-multiplied by 100 (matching the convention the breakeven
  column already used), keep the same `"%.2f%%"` column_config format. **Applies
  generally: any NumberColumn with a printf-style format string needs the value already
  scaled -- st's `format="percent"` auto-scales, printf formats do not.** Watch for this
  again if a new percent column is added here or elsewhere.
- Added `Borrow rate (config) %` column (from `pair["borrow_rate_annual"]`), placed
  immediately left of the renamed `Breakeven borrow rate % (annualized)` column, so the
  two sit side by side for comparison.
- Changed `Max drawdown` and `Worst day` from dollars to percent of `base_capital`
  (`net["max_drawdown"] / base_capital * 100`, same for worst_day), formatted like the
  other percent columns. `Borrow paid` stays in dollars (the one column that should
  scale with base_capital rather than stay fixed).
- Added one sentence to the page's warning note explaining the breakeven column: "the
  annualized borrow rate at which the pair's net P&L is zero; the pair is only viable if
  real borrow is below this."
- Made the sidebar uniform with the main page: lookback is now the same preset radio
  (30/60/120/240/360/Max; no cross-pair data cap -- shorter-history pairs just truncate),
  hold_days slider bounds derive from the preset like app.py, and a new "Override borrow
  rate for all pairs" checkbox reveals the same 0-30% slider (default off = each pair's
  config rate). Column renamed "Borrow rate (config) %" -> "Borrow rate (used) %" since
  it can now show the override. Invariant re-verified in both modes: any pair whose used
  rate exceeds its breakeven shows net return <= 0 (checked at a forced 25% override).

**Verified:**
- Gate 1: leaderboard's gross run (`borrow_rate_annual=0.0`) produces the exact same
  `pct_return` as the main page's `run_backtest` called with `borrow_rate_annual=0.0` for
  the same pair/settings (TQQQ, hold_days=5, lookback=240, base=10000) -- exact float
  match.
- Gate 2: spot-checked all 13 pairs -- ERX is the only pair where config rate (3.00%)
  exceeds its breakeven rate (1.79%), and its net return is indeed negative (-0.79%);
  every other pair has config rate below breakeven and positive net return.
- Gate 3: doubling `base_capital` (10000 -> 20000) leaves `Max drawdown %` and
  `Worst day %` unchanged while `Borrow paid ($)` exactly doubles.
- `verify_engine.py` and `verify_backtest.py` both still pass; app boots headless on
  both pages (main + leaderboard), HTTP 200, no tracebacks.

**Next:** v2 backlog -- signed leverage (inverse funds), expense ratio as reference data,
longer history, Sharpe/Sortino.

**Open questions / blockers:**
- None.

---

### 2026-07-03 (borrow fee, single-stock pairs, leaderboard)
**Did:**
- Filled in the borrow-fee stub (v2 item). `engine.borrow_cost(notional, rate_annual,
  days=1)` now returns `notional * rate_annual * days / 360` (was a 0.0 stub).
  `config.PAIRS` gained `borrow_rate_annual` per pair -- indicative placeholders (0.01
  index/2x pairs, 0.02 TMF, 0.03 sectors, 0.10 single-stocks), flagged for hand-refresh
  from IBKR/iBorrowDesk. `backtest.run_backtest` gained `borrow_rate_annual=None`
  (defaults to the pair's config value), charges borrow per open tranche per day, and
  returns `borrow_paid` + `notional_days`. `app.py` got a borrow-rate slider (0-30%,
  default = pair's config rate) and a "Borrow paid" metric; updated the disclaimer since
  borrow is no longer omitted (expense ratio/spread/dividends still are).
- Added three single-stock 2x pairs to `config.PAIRS`: NVDL/NVDA, TSLL/TSLA, CONL/COIN,
  borrow_rate_annual 0.10. No code changes needed -- get_prices/position_pnl/backtest are
  ticker-agnostic to stock vs ETF underlyings. All six new tickers (three leveraged +
  three underlying) fetched from Polygon and cached (501 rows each), paced 15s apart to
  stay under the free-tier ~5 req/min limit.
- New page `src/pages/Pair_Analysis.py`: cross-pair leaderboard. Same controls as
  app.py (hold_days, lookback_days, base_capital). Runs every pair in config.PAIRS twice
  (borrow_rate_annual=0.0 for gross, the pair's config rate for net), tables pair,
  leverage, gross %, net %, borrow paid, max drawdown, worst day, and a breakeven borrow
  rate. Breakeven is analytic (new `engine.breakeven_borrow_rate(gross_pnl,
  notional_days)` = `gross_pnl / (notional_days / 360)`), not searched -- borrow scales
  linearly in rate so the closed form is exact. Cached via `st.cache_data` keyed on the
  controls. Sorted net % descending by default; no charts (table is the v1 deliverable).
  Linked from app.py's sidebar.

**Verified:**
- Borrow gate: rate=0 -> borrow_paid=0, total_return identical to pre-change output.
  Raising the slider (0/5/10/20%) degrades total_return monotonically; borrow_paid scales
  exactly linearly with rate (76.39 -> 152.78 -> 305.56 as rate doubles 5%->10%->20%).
- `scripts/verify_backtest.py` updated to pass `borrow_rate_annual=0.0` explicitly (its
  independent re-derivation doesn't model borrow) -- passes again after the update.
  `scripts/verify_engine.py` unaffected, still passes.
- Single-stock pairs: backtest runs cleanly for all three (NVDL/TSLL/CONL), sane
  metrics, no errors.
- Leaderboard gate: all 13 pairs render (10 original + 3 new); gross-minus-net in dollars
  equals borrow_paid exactly for every pair spot-checked; TQQQ's computed breakeven rate
  (14.93%) plugged into the main page's borrow slider drives TQQQ's total_return to
  ~0 (1e-13, float noise).
- App boots headless on both pages (main + leaderboard), HTTP 200, no tracebacks.

**Decisions:**
- Borrow accrual convention: simple daily interest on a 360-day basis (money-market
  convention), matching the task's stated formula. No compounding.
- Kept the leverage-label UI fix from the prior session (`SSO price (2x leveraged)`)
  bundled into the borrow-stub commit since it was a small uncommitted leftover in the
  same file.

**Next:** v2 backlog -- signed leverage (inverse funds / the delta-neutrality control
experiment), expense ratio as reference data, longer history, Sharpe/Sortino.

**Open questions / blockers:**
- None.

---

### 2026-06-26
**Did:**
- Built layer 1 (data). `config.py`: `PAIRS` registry seeded with QQQ -> SQQQ/QQQ/3.
- `data.py`: `get_prices(ticker)` (cache-first) + `_fetch_polygon(ticker)`. Pulls ~2yr
  (730d) of Polygon daily aggregates, returns date-indexed OHLCV DataFrame, caches to
  `./cache/{ticker}.csv`.
- Gate passed: `get_prices("SQQQ")` returned 501 rows; second call reads cache (verified
  with the API key stripped, so no network hit). `config.PAIRS["QQQ"]` resolves correctly.

**Next:**
- Layer 2 (engine): pure stateless two-leg daily P&L + `borrow_fee_stub` returning 0.

**Open questions / blockers:**
- None. Polygon free tier returned a full ~2yr window for SQQQ (better than the ~1yr
  fallback we planned for).

---

### 2026-06-26 (cont.)
**Did:**
- Moved source into `src/` (config.py, data.py); anchored CACHE_DIR to project root so
  `./cache/` resolves regardless of CWD. Added verify-before-commit rule to CLAUDE.md.
- Built layer 2 (engine) `src/engine.py`: `daily_pnl(lev_start, lev_end, und_start,
  und_end, short_size, long_size)` -> dict {short_pnl, long_pnl, borrow_fee, net}, plus
  `borrow_fee_stub` returning 0.0 wired into net. Engine is pure: no I/O, no dates, no
  OHLC awareness.
- Gate passed: flat day -> net 0; ETF -10% / underlying +3% (sizes 1000/3000) -> net +190
  matching by-hand; flipped -> net -190. Borrow term present at 0 in the net formula.

**Decisions:**
- Engine stays price-agnostic. The open-vs-close price-selection knob lives in the backtest
  layer (layer 3), not the engine. v1 default mode there: prior-close -> close.

**Next:**
- Layer 3 validate (ROADMAP step 3): run engine on QQQ/SQQQ real cached prices for a few
  days by hand, confirm decay = positive on choppy days. Then layer 4 backtest wrapper,
  where the price-mode knob gets built.

**Open questions / blockers:**
- None.

---

### 2026-06-26 (pivot: multi-day hold)
**Did:**
- PIVOT: strategy is now a multi-day overlapping-tranche hold, not a daily delta-neutral
  reset. Open one tranche/day at that day's prices, hold each `hold_days` days. The ladder
  of overlapping holds is what captures leveraged-fund decay. Daily-reset only scraped one
  interval's embedded cost drag + tracking error (the ~$1.81 real-day residual), which is
  NOT decay -- that realization drove the pivot.
- Engine (math unchanged, renamed/relocated):
  - `daily_pnl` -> `position_pnl(lev_entry, lev_now, und_entry, und_now, short_size,
    long_size)`; interval P&L, returns {short_pnl, long_pnl, net}. Removed borrow from net.
  - `borrow_fee_stub` -> `borrow_cost(notional, days=1) -> 0.0`, separate pure stub, applied
    per open tranche per day in the backtest.
- New `src/backtest.py` (layer 3): holds the tranche ladder + realized_pnl; calls the engine
  for every P&L number (no P&L math of its own). Equity = realized + open marks - borrow.
  Metrics: total return, max drawdown, worst day.
- Cleaned `scripts/verify_engine.py`: applied rename, dropped the borrow line, reworded the
  "decay" language (single interval = cost drag + tracking error, not decay).

**Decisions:**
- Tranche opened day d realizes at close of day d+hold_days -> exactly hold_days open
  mid-window.
- Normalization: per-tranche notional = base_capital / hold_days (constant deployed capital,
  comparable across hold_days). Equity curve in dollars.
- Tail taper: stop opening new tranches in the last hold_days-1 days so every tranche
  completes its hold (winddown symmetric with warmup). Equity stays mark-to-market.

**Verified:**
- verify_engine.py still passes (numbers identical; formula untouched).
- Backtest gate: hold_days 1/5/20 give distinct curves; warmup ramp + mid-window ==
  hold_days + tail taper to 0 open; hold_days=1 is a real degenerate daily-reset curve
  (+$2206), not zero. No P&L math in backtest.py (grep-confirmed).

**Next:**
- Layer 5 UI (app.py): pair dropdown, lookback slider, hold_days control, equity chart,
  metrics table, fees-omitted disclaimer.

**Open questions / blockers:**
- None.

**Verified (lifecycle):**
- `scripts/verify_backtest.py`: independent re-derivation of the tranche ladder (separate
  entry-set + realize-at-d+hold_days price lookup, direct engine calls) matches the
  backtest curve exactly (max equity error 0.0 over 501 days, 0 open-count mismatches).
  Confirms: equity = realized + open marks with no double-count, and tranches realize at
  the correct d+hold_days prices. Backtest P&L bookkeeping is sound -> clear for UI.

---

### 2026-06-26 (UI -- v1 complete)
**Did:**
- Added `lookback_days=None` to `backtest.run_backtest`: trims to the last N aligned
  trading days before the loop (curve + metrics on the same window). Backward-compatible.
- New `src/app.py` (Streamlit UI): pair dropdown (from config.PAIRS), lookback slider,
  hold_days slider, base_capital input; equity-curve chart, metrics (total return / max
  drawdown / worst day), open-tranche-count chart, and the verbatim fees-omitted
  disclaimer. Presentation only -- every number from run_backtest, no P&L math.

**Verified:**
- lookback_days=60 -> 60-day curve (ends 2026-06-25); full-window call unchanged
  (+$2442.27). verify_backtest.py still PASS.
- App boots headless (HTTP 200, no tracebacks); app's exact calls exercised standalone
  produce sane metrics; no bare P&L arithmetic in app.py.

**Status:** v1 MVP complete (data -> engine -> backtest -> UI). Run with
`streamlit run src/app.py`.

**Next (v2 backlog):** real borrow fee, inverse-fund support via signed leverage, more
pairs, Sharpe/Sortino, longer history.

**Open questions / blockers:**
- None.

---

### 2026-06-26 (viz upgrades)
**Did:**
- Added `plotly` dep (approved; pyproject + uv sync -> plotly 6.8.0).
- backtest.run_backtest now also returns: `long_curve`/`short_curve` (per-leg cumulative
  P/L, 0-based; sum to equity_curve since borrow=0), `lev_ohlc`/`und_ohlc` (OHLC slices for
  candlesticks). Dropped the old close-only `lev_prices`/`und_prices`. New series are just
  retained engine outputs -- no new P&L math.
- app.py: candlestick charts (both assets) with trade entry/exit markers; FIXED the equity
  curve mislabel -- now renders equity_curve + starting_capital (true $10k-based equity);
  added long-vs-short P/L chart; added per-trade P/L bar chart (green/red) below the table.

**Verified:**
- Invariant long_curve + short_curve == equity_curve (max diff ~3.6e-12). Legs: long
  +$8201.50, short -$7226.82, sum +$974.68 = total_return.
- Equity offset: starts exactly $10,000, ends $10,974.68.
- Figures build from real result (candlestick 3 traces, 235 bars 197g/38r). App boots
  headless, no errors. verify_engine + verify_backtest both PASS.

**Next:** v2 backlog -- real borrow fee (would shrink the curve), inverse-fund support,
more pairs, Sharpe/Sortino.

**Open questions / blockers:**
- None.

---

### 2026-06-27 (expand pair universe to 10)
**Did:**
- Expanded PAIRS from 1 to 10 bull (positive-leverage) pairs. Rekeyed the dict by the
  LEVERAGED ticker (was the underlying) because two pairs share an underlying (TQQQ & QLD
  on QQQ; UPRO & SSO on SPY). Updated callers: backtest run_backtest param underlying ->
  pair_key; app dropdown format_func and var; both verify scripts (key "QQQ" -> "TQQQ").
- No engine/logging/borrow changes -- pure universe expansion, per scope.
- Pairs: TQQQ/QQQ, UPRO/SPY, UDOW/DIA, TNA/IWM, TMF/TLT (3x indices+bonds);
  SOXL/SOXX, FAS/XLF, ERX/XLE (sectors); QLD/QQQ, SSO/SPY (2x contrasts). All 16 new
  tickers data-validated on Polygon (501 rows each). Fetching all at once trips the free
  tier's ~5 req/min (HTTP 429) -- paced with sleeps; cache makes it one-time.

**Early findings (240d, hold_days=5, no fees, bull market):**
- Leverage: 3x harvests ~2x the decay of 2x on the same underlying (TQQQ +9.75% vs QLD
  +4.58%; UPRO +7.95% vs SSO +3.87%). Supports the leverage hypothesis.
- Volatility: high-vol sectors dominate (FAS +15.44%, SOXL +14.28%) vs calm bonds (TMF
  +1.71%). Supports vol -> decay.
- CAVEAT: all bull funds in an up market, no fees -> can't yet separate decay from beta.
  The inverse-QQQ delta-neutrality control (short SQQQ + SHORT QQQ) is the experiment that
  would, and it needs v2 signed leverage. Logged in ROADMAP.

**Decisions:**
- PAIRS keyed by leveraged ticker. Inverse delta-neutrality test = short SQQQ + short QQQ
  (clean), not the PSQ long-inverse version (2nd decay source). Both logged for v2.
- Expense ratio is already in the LETF price; do NOT add it. Borrow fee (what we pay to
  short) is the real missing cost -- still a stub, still deferred.

**Next:** v2 -- signed leverage (unlocks the inverse delta-neutrality test), then borrow fee.

**Open questions / blockers:**
- None.

---

