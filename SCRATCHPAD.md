# SCRATCHPAD

Rolling session log. Newest entry on top. Update at the end of every session. Latest entries go directly underneath session template, with most recent updates at the top.

Each entry: what changed, what's next, open questions/blockers.

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

---

## Session template (copy this)

### YYYY-MM-DD 
**Did:**
-

**Next:**
-

**Open questions / blockers:**
-

---
