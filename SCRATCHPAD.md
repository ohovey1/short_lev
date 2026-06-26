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

## Session template (copy this)

### YYYY-MM-DD 
**Did:**
-

**Next:**
-

**Open questions / blockers:**
-

---
