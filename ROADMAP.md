# ROADMAP

Scope gate: if a task isn't here, ask before building.

## v1 — MVP (build in this order)

### 1. Data layer
- [ ] `config.py`: pair registry dict (start with `QQQ: {leveraged: SQQQ, underlying: QQQ, leverage: 3}`).
- [ ] `data.py`: `get_prices(ticker)` → DataFrame of daily OHLC.
- [ ] `_fetch_polygon(ticker)`: pull daily aggregates from Polygon using `POLYGON_API_KEY`.
- [ ] Cache to `./cache/{ticker}.csv`; read cache if present, fetch only on miss.

**Done when:** get_prices("SQQQ") returns a clean OHLC DataFrame, and a second call reads from ./cache/SQQQ.csv instead of hitting Polygon.

### 2. Engine
- [ ] `engine.py`: pure function — given one day's prices for both legs + position sizes, return net P&L.
- [ ] Wire in `borrow_fee_stub(...)` returning 0. Keep it in the P&L path.

**Done when:** a flat day (no price change in either leg) returns net P&L = 0, and a hand-picked day with known price moves matches a by-hand calculation. The borrow term appears in the net formula and currently evaluates to 0.

### 3. Validate (one pair, from a script — no UI yet)
- [ ] Run the engine on QQQ/SQQQ for a handful of days by hand-checking numbers.
- [ ] Confirm the sign and magnitude make sense (decay = positive on choppy days).

**Done when:** for QQQ/SQQQ, a known choppy day yields positive net P&L and a known calm/trending day behaves as expected, with both matching a by-hand calculation of the real cached prices.

### 4. Backtest wrapper
- [ ] `backtest.py`: loop the engine over a date window. **Must call the engine.**
- [ ] Build the equity curve (cumulative net daily P&L).
- [ ] Metrics: total return, max drawdown, worst day.

**Done when:** running it over a date window for QQQ/SQQQ produces an equity curve whose final value equals the sum of the per-day engine outputs, and the three metrics (total return, max drawdown, worst day) read correctly off that curve. The backtest contains no P&L math of its own — every daily number comes from an engine call.

### 5. UI (last)
- [ ] `app.py`: pair dropdown (from `config.py`).
- [ ] Lookback slider (not hardcoded).
- [ ] Equity-curve chart.
- [ ] Metrics table.
- [ ] Visible disclaimer: "Fees (incl. borrow) omitted — results are optimistic and not a verdict."

## v2 — backlog (do NOT build yet)
- Fill in the borrow-fee stub (daily borrow charge on the short leg).
- Add expense ratio, spread, dividends.
- More pairs in the registry (SPY/SPXU, IWM/TZA, etc.).
- Longer history (swap data source to a paid/keyed tier with multi-year coverage).
- Sharpe / Sortino, rolling stats.
- Parameterize rebalance frequency (daily is hardcoded in v1).

## Open decisions
- (none currently — log new ones in SCRATCHPAD.md)