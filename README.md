# LETF Decay Backtest — MVP

A barebones backtesting tool for a delta-neutral leveraged-ETF decay strategy.

> **Fees (incl. borrow) omitted — results are optimistic and not a verdict.**

## The idea
Short $X of a 3x leveraged ETF (e.g. SQQQ), long $3X of its underlying (e.g. QQQ) to offset the leverage, rebalance to delta-neutral daily. The strategy harvests the leveraged fund's volatility decay. Each day's P&L is computed independently and summed into an equity curve.

## Setup (Windows)

Install `uv` (package manager):
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Clone/enter the project, then install deps:
```powershell
uv sync
```

Add your Polygon API key. Create a `.env` file in the project root:
```
POLYGON_API_KEY=your_key_here
```
Get a free key at https://polygon.io (free tier: daily bars, ~1 year history, 5 calls/min — fine since data is cached per ticker).

## Run

The Streamlit UI:
```powershell
uv run streamlit run app.py
```

## Architecture
Four separate layers:
1. **`data.py`** — daily OHLC per ticker, cached to `./cache/`.
2. **`engine.py`** — pure, stateless two-leg daily P&L. Borrow-fee stub for v2.
3. **`backtest.py`** — loops the engine over a date window. Calls the engine; does not reimplement it.
4. **`app.py`** — Streamlit UI: pair dropdown, lookback slider, equity curve, metrics.

Pairs live in `config.py` as a dict. Add a pair = edit the dict.

## Scope (v1)
No fees, no dividends, no spread. Single data source (Polygon). Borrow fee is a stub returning 0. See `ROADMAP.md` for what's next.

## Disclaimer
For research only. Not investment advice. Results omit all costs and are optimistic.