# CLAUDE.md

Guidance for Claude Code working in this repo. Read this first, every session.

## What this is
A barebones backtest for a delta-neutral leveraged-ETF decay strategy. One-day MVP. Ship simple, add later.

## Coding philosophy (non-negotiable)
- **Simple > complex > complicated.** If a solution feels clever, it's probably wrong for this project.
- Write the least code that works. No abstractions until there are two concrete uses.
- Keep the layer boundaries (get_prices, the engine's P&L function) as clean single-responsibility functions. That seam is the one form of extensibility we invest in up front — everything behind it is YAGNI.
- No premature config, no plugin systems, no classes where a function does.
- Prefer plain functions and plain data (dicts, DataFrames). Avoid frameworks.
- If you're tempted to add a dependency, stop and ask first.
- Match the existing style. Standard library + pandas/requests/streamlit only.

## Development Workflow
- No emojis — stated as a hard rule covering code, comments, commits, and UI. (Caveat from the docs: CLAUDE.md is advisory, so this is a strong request, not a guarantee. If Claude Code ever slips an emoji in, that's the signal to add the one-line PreToolUse hook that blocks it deterministically. Not worth setting up preemptively.)
- Commits — one commit per layer/checkbox once its "Done when" gate passes, imperative lowercase messages with a scope prefix, no broken commits, never commit .env/cache/. I also explicitly banned AI-attribution trailer lines since those often carry emojis and you clearly want clean history.
- Best practices — build one layer at a time in ROADMAP order, use plan mode before non-trivial edits, validate against gates before moving on, ask before adding deps, don't touch ignored/read-only files.

## The strategy (Structure B)
- Short $X of a 3x leveraged ETF (e.g. SQQQ).
- Long $3X of its underlying (e.g. QQQ) to offset the leverage.
- Rebalance to delta-neutral **daily**: each day reset short to $X, long to $3X.
- **Stateless**: each day's P&L is independent. Apply the day's price moves to both fixed legs, record the net, reset. Sum into an equity curve.
- The edge is the leveraged fund's volatility decay; the daily divergence between legs is the P&L.

## Architecture — 4 layers, kept separate
1. **Data** (`data.py`) — daily OHLC per ticker, cached to `./cache/{ticker}.csv`. Source: Polygon (one fetch per ticker, then cache). Fetch is abstracted: `get_prices(ticker)` calls an internal `_fetch_polygon(ticker)`. Swapping sources = write a new `_fetch_*` and point one line at it.
2. **Engine** (`engine.py`) — pure/stateless. Given one day's prices, return the two-leg P&L. Includes a **borrow-fee stub** (returns 0 for v1) so v2 is a fill-in, not a refactor.
3. **Backtest** (`backtest.py`) — loops the engine over a date window, builds the equity curve + metrics.
4. **UI** (`app.py`) — Streamlit. Pair dropdown, lookback slider, equity-curve chart, metrics table.

## Hard constraints
- **The backtest MUST call the engine. Never reimplement the trading logic in `backtest.py`.** (A prior project's backtest diverged from its live engine. Don't repeat it.)
- The engine stays pure: no I/O, no global state, no date awareness beyond the prices passed in.
- Keep the borrow-fee stub present and wired in even though it returns 0.

## Pairing
Static registry in `config.py`: a dict mapping underlying → `{leveraged_ticker, underlying_ticker, leverage}`. Adding a pair = editing the dict. No discovery algorithm.

## Scope cuts for v1 (be explicit, don't sneak these in)
- **No fees.** Omit borrow fee, expense ratio, spread, dividends.
- Borrow-fee **stub only** in the engine (returns 0).
- UI must show: *"Fees (incl. borrow) omitted — results are optimistic and not a verdict."*

## File map
```
config.py     pair registry dict
data.py       layer 1: get_prices() + cache + _fetch_polygon()
engine.py     layer 2: pure two-leg daily P&L + borrow stub
backtest.py   layer 3: loops engine over a window -> equity curve + metrics
app.py        layer 4: streamlit UI
.env          POLYGON_API_KEY=... (gitignored)
cache/        cached CSVs (gitignored)
```

## Build order (follow this)
data layer → engine → validate on one pair (QQQ/SQQQ) → backtest wrapper → UI last.
Validate the engine on one pair before building the backtest. Don't build the UI until the backtest works from a script.

## Secrets
`POLYGON_API_KEY` lives in `.env`, read via env var. Never hardcode it. `.env` and `cache/` are gitignored.

## Session hygiene
- Update `SCRATCHPAD.md` at the end of each session (what changed, what's next, open questions).
- Check `ROADMAP.md` for what's in scope. If a task isn't on it, ask before building.

## Keep this file lean

CLAUDE.md loads into context every session. Keep it short and specific; long files dilute attention and lower adherence. Prune anything stale rather than letting it grow.