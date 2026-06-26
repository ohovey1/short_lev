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
- Fill in the borrow-fee stub (daily borrow charge on the short leg).
- Add expense ratio, spread, dividends.
- More pairs in the registry (SPY/SPXU, IWM/TZA, etc.).
- Longer history (swap data source to a paid/keyed tier with multi-year coverage).
- Sharpe / Sortino, rolling stats.
- Parameterize rebalance frequency (daily is hardcoded in v1).

## Open decisions
- (none currently — log new ones in SCRATCHPAD.md)