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

## Working agreement
- No emojis — code, comments, commits, UI. Advisory, not enforced; if it ever slips through, that's the signal to add a PreToolUse hook.
- Never commit until I've verified the changes myself. Stage nothing, run no `git commit`, until I explicitly confirm. One commit per spec item once its gate passes, imperative lowercase with a scope prefix, no AI-attribution trailers, never commit `.env` or the borrow CSVs (`cache/borrow_*.csv`) -- price CSVs under `cache/` ARE committed as seed data.
- Four documents, each with one job:
  - `docs/STRATEGY_SPEC.md` — what the strategy is. Source of truth. If code disagrees with it, the code is the bug.
  - `ROADMAP.md` — what gets built and in what order.
  - `specs/spec_NNN.md` — the active session's scope and gate.
  - `SCRATCHPAD.md` — what actually happened, appended after each session.
- Scope gate: **if it isn't in the active spec, ask before building.** (ROADMAP says what is eventually in scope; the spec says what is in scope now.)
- Specs are written before the work and not rewritten during it. If scope needs to change mid-session, stop and amend deliberately.
- Each spec gets a **Result** section appended after the session: gate passed or not, what deviated, what got deferred. The spec is then a self-contained record and needs no scratchpad cross-reference.
- Use plan mode before non-trivial edits. Don't touch ignored/read-only files.

## The strategy (band-rebalanced — this is the canonical definition, live and backtest both)
Short a fixed target notional of the leveraged ETF and hold `leverage`x that long in the
underlying, delta-neutral at entry, then leave both legs' dollar sizes untouched between
trades so they simply mark to market. A trade fires only when the position drifts past one
of two bands: if the short leg's notional strays too far from target, reset both legs back
to target; otherwise, if net delta alone strays too far, re-neutralize using only the long
leg, leaving the short running at its drifted size. The entire P&L objective is the
volatility decay collected while the hedge sits frozen between these infrequent trades, net
of borrow cost on the short leg -- nothing else is being harvested.

Implemented in `band.py`. Full mechanics, worked numeric example, and term definitions:
`docs/strategy.md` and `docs/GLOSSARY.md`.

Scope notes (not in the 3-sentence definition on purpose -- keep that one pure mechanics):
- **Positive-leverage funds only.** Short = short exposure, long underlying = offsetting
  long exposure. Inverse funds are out of scope (v2, see ROADMAP) -- do not add them without
  the signed-leverage rework.
- **`target` is fixed for a run, not equity-scaled.** No reinvestment of P&L, no growth with
  account equity. Whether it should scale is an open question, logged in ROADMAP -- don't
  silently add scaling logic.

`backtest.py` (the tranche ladder) is a **reference/validation implementation only** -- it is
not traded and not the live design. It exists to sanity-check the band strategy's decay
capture against a known-good overlapping-hold baseline. Do not describe it as "the strategy"
or treat its output as a live-trading projection.

## Architecture — 4 layers, kept separate
1. **Data** (`data.py`) — daily OHLC per ticker, cached to `./cache/{ticker}.csv`. Source: Polygon (one fetch per ticker, then cache). Fetch is abstracted: `get_prices(ticker)` calls an internal `_fetch_polygon(ticker)`. Swapping sources = write a new `_fetch_*` and point one line at it.
2. **Engine** (`engine.py`) — pure/stateless. Given one day's prices, return the two-leg P&L. Includes `borrow_cost`, a real daily accrual charge on short notional (not a stub).
3. **Backtest** — two siblings, both call the engine, neither reimplements its math:
   - `band.py` — the live-bot design. One continuous position, trades only on a band trip.
   - `backtest.py` — the tranche ladder, kept as a reference/validation baseline only.
4. **UI** (`app.py`) — Streamlit. Pair dropdown, lookback slider, equity-curve chart, metrics table.

## Hard constraints
- **Both `band.py` and `backtest.py` MUST call the engine. Never reimplement P&L math in either.** (A prior project's backtest diverged from its live engine. Don't repeat it.)
- The engine stays pure: no I/O, no global state, no date awareness beyond the prices passed in.
- Borrow cost is real, not a stub -- both `band.py` and `backtest.py` must keep charging it.

## Pairing
Static registry in `config.py`: a dict keyed by the **leveraged ticker** (not the underlying —
two pairs can share an underlying, e.g. TQQQ and QLD both on QQQ) → `{leveraged_ticker,
underlying_ticker, leverage, borrow_rate_annual, live, margin_multiplier}`. Adding a pair =
editing the dict. No discovery algorithm.

## Scope cuts (be explicit, don't sneak these in)
- **Borrow fee is implemented** (`engine.borrow_cost`, daily accrual on short notional) and
  charged in both `band.py` and `backtest.py`. Do not describe it as a stub.
- **Still omitted:** expense ratio (already embedded in the LETF's price history -- do NOT
  add it, would double-count), spread, dividends.
- UI must show: *"Expense ratio, spread, and dividends are still omitted -- results are
  optimistic and not a verdict."*

## File map
```
src/config.py           pair registry dict
src/data.py             layer 1: get_prices() + cache + _fetch_polygon()
src/engine.py           layer 2: pure two-leg daily P&L + real borrow accrual
src/backtest.py         layer 3: loops engine over a window -> equity curve + metrics
src/band.py             layer 3 sibling: band-rebalanced single-position backtest
src/app.py              layer 4: streamlit UI
src/pages/              additional Streamlit pages (strategy explainer, pair leaderboard)
.env                    POLYGON_API_KEY=... (gitignored)
cache/                  price CSVs (committed seed data); borrow_*.csv (gitignored)
scripts/                verify_*.py: gate checks for the engine and the two backtests
docs/                   STRATEGY_SPEC.md, GLOSSARY.md, strategy.md, AUTOMATION.md
specs/                  spec_NNN.md: the active session's scope and gate
```
Source lives in `src/`. Run with `src` on the path (e.g. `PYTHONPATH=src`); modules
import each other flat (`import data`, `import config`).

## Build order (follow this)
data layer → engine → validate on one pair (QQQ/SQQQ) → backtest wrapper → UI last.
Validate the engine on one pair before building the backtest. Don't build the UI until the backtest works from a script.

## Secrets
`POLYGON_API_KEY` lives in `.env`, read via env var. Never hardcode it. `.env` and the borrow CSVs are gitignored; price CSVs are committed.

## Keep this file lean

CLAUDE.md loads into context every session. Keep it short and specific; long files dilute attention and lower adherence. Prune anything stale rather than letting it grow.