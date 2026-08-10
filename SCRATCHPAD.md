# SCRATCHPAD

Rolling session log. Newest entry on top. Retention: keep the last three sessions here;
archive the rest to `docs/history/` (see `docs/history/scratchpad-2026-06.md`).

---

### 2026-08-10 (spec 005: margin model -- fixed the rate and the shape)

**Spec:** `specs/spec_005.md`

**Shipped:**
- `config.PAIRS` now carries `long_rate`/`short_rate`; `margin_multiplier` is
  derived via `config.margin_multiplier(pair)` and stored nowhere.
- `margin_required` is two-term (`long_rate*long + short_rate*short`) at both
  `band.py` sites, in `broker.py`'s comparison log, and in the docs.
- Single-stock multiplier 1.60 -> 1.10; their targets grew 45.5%.
- All 6 gates passed. New GRAND `08baa20a...`, recorded in
  `baseline_005_hash.txt` (003's file kept -- spec 003 references it by name).

**Surprised us:**
- **All 13 pair digests moved, not just the 3 single-stock ones.** The plan
  expected only NVDL/TSLL/CONL. Attributed, not waved through: the *shape* fix
  alone moves all 13 (it changes `margin_required` -> `margin_cushion` for
  every pair), and the *rate* fix additionally moves the 3. Proof: TQQQ's
  `cu1.00-noclose` arm changed digest with trade count fixed at 125, and that
  arm has de-risk off, so only the recorded series could have moved.
- The corrected model measures *less* margin off-neutral, so de-risk counts
  fell everywhere (TQQQ 108 -> 90 at cu=1.0) and breach-days at cu=1.00 went
  38-203 -> 20-200. Positions were being de-risked partly on a modelling error.
- `verify_band` check_a hit a genuine one-ulp knife edge: at cu=1.0 entry
  cushion is zero by construction, and the two-term sum lands on
  1000.0000000000001, tipping `equity < margin_required`. Fixed in the fixture
  (it was never meant to test the margin rule). Confirmed fixture-specific --
  at base_capital=10000 all 13 pairs land on exactly 10000.0.
- Leaderboard moved: NVDL 10th -> 5th, TSLL 3rd -> 2nd. CONL holds 1st.

**Next:**
- **Revisit `capital_utilization`.** Deliberately untouched this session (0.75
  was calibrated against the old numbers). Headroom now looks larger than it
  did: zero breach-days at 0.75 across all 13 pairs even with the bigger
  single-stock targets. Fresh breach counts are in spec 005's Result.
- Then Telegram (heartbeat + dedup), then systemd.

**Open questions / blockers:**
- Still paper-only. The 1.10 estimate carries a consistent +1.25-1.67% residual
  against IBKR's actual, which reads as a house add-on. Unconfirmed on a funded
  account -- do not resize anything live on this.
- Any open paper position was opened at the old target; the monitor's sanity
  check will warn until it is resized. Expected, not a regression.

### 2026-08-10 (spec 004: monitor core, gates passed on paper)

**Spec:** `specs/spec_004.md`

**Shipped:**
- All 11 spec 004 gates passed against paper DUQ985373.
- VPS provisioned and documented (`docs/VPS.md`). Gateway 10.45 headless under
  Xvfb, VNC over SSH tunnel, monitor running from `/opt/short_lev`.

**Surprised us:**
- IBKR's actual maintenance margin is ~1.11x short notional, not our modeled
  1.60. Cause identified: we used the 50% *initial* rate on the single-stock long
  leg where 25% *maintenance* applies.
- The model also has the wrong shape -- it assumes `long = leverage x short`, so
  the ratio drifts with net delta. Observed 0.719 at +$586 delta vs 0.691 at
  -$146, same short leg.
- Commissions ~$1/leg. Backtest models none.
- Weekly Gateway re-auth needed no 2FA on paper.

**Next:**
- Spec 005: margin model. Fix both the rate and the shape, re-run all 13 pairs.
- Then Telegram (heartbeat + dedup), then systemd.

**Open questions / blockers:**
- Paper vs live margin: the 1.11 reading is unconfirmed against a funded account.

### 2026-08-06 (spec 001: reconcile repo with docs/STRATEGY_SPEC.md)
**Spec:** `specs/spec_001.md`

**Shipped:**
- Renamed `delta_band` -> `long_short_band`, `short_band` -> `foil_decay_band`
  everywhere (`band.py`, `app.py`, `Pair_Analysis.py`, `verify_band.py`,
  `GLOSSARY.md`, `strategy.md`), including trigger strings and slider labels.
- Fixed stale claims in `CLAUDE.md` (borrow "stub" language, Pairing section's
  "rework in progress", `GLOSSARY.md` path, file map), replaced the scattered
  workflow bullets with one Working agreement section pointing at
  `docs/STRATEGY_SPEC.md` / `ROADMAP.md` / `specs/` / `SCRATCHPAD.md`.
- `docs/strategy.md`: corrected the sizing formula (was "about base capital",
  now the real `target` formula), added a margin cushion / capital utilization
  paragraph, linked `docs/STRATEGY_SPEC.md`.
- `app.py` docstring corrected: band is the default strategy, not the ladder.
- Added the daily-close check-cadence disclosure to `run_band_backtest`'s
  docstring and the Trades/Total turnover metric help text.
- Standardized the Portfolio Margin threshold on "$110,000 NLV plus options
  approval" everywhere (`docs/AUTOMATION.md`, `ROADMAP.md`); reworded
  `AUTOMATION.md`'s Signal Source section to distinguish quote delay from
  poll interval instead of conflating two unrelated 15-minute numbers.
- Restructured this file per spec item 7 (see below).

**Surprised us:**
- `ROADMAP.md` had already been updated to "$110k NLV" before this session,
  just missing the "plus options approval" clause -- less drift than the spec
  assumed, but still worth standardizing the full phrase.
- The gate's literal grep (`delta_band\|short_band`) still matches after the
  rename, because `long_short_band` contains `short_band` as a substring.
  Confirmed by exclusion-diff that zero real old-name occurrences remain.

**Next:** spec 002 (closing logic: drawdown stop, margin de-risk) per
`specs/spec_001.md`'s own out-of-scope list.

---

### 2026-07-05 (band-rebalanced backtest as sibling strategy)
**Did:**
- New `src/band.py`: `run_band_backtest(pair_key, base_capital, delta_band=0.10,
  short_band=0.10, price_field, lookback_days, borrow_rate_annual)` -- adapted from the
  user-provided `band_prototype.py` (repo root, untracked; policy kept verbatim). One
  continuous position, hedge frozen between trades (engine.position_pnl marks each
  segment); short-band trip resets short to target and re-neutralizes both legs,
  else delta-band trip re-neutralizes via the LONG leg only (short carries at current
  value). Borrow accrues daily on current (post-trade) short notional. Adaptations vs
  prototype: max_drawdown/worst_day in dollars (run_backtest convention, prototype
  normalized by capital), notional_days added to the return dict, gross_return dropped,
  lev_ohlc/und_ohlc added so the UI price charts work for both strategies.
- `src/app.py`: sidebar "Strategy" radio (Ladder / Band) after the pair + lookback
  widgets (unconditional widgets keep their state across the switch). Ladder shows the
  hold_days slider; band shows Delta band / Short band sliders (0.05-0.30). Shared:
  pair, lookback, capital, borrow-rate resolution, price charts, equity chart. Band
  metrics add Breakeven borrow, Trades, Total turnover (turnover_lev + turnover_und,
  display aggregation). Intro blurb is strategy-aware; ladder view unchanged.
- New `scripts/verify_band.py` (mirrors verify_engine.py style): (a) hand-computed
  two-segment check on 4 fabricated days (monkeypatches data.get_prices; one delta trip;
  final equity 150 - 10 - 200/7 = 111.428571 exact), (b) degenerate bands=10.0 on real
  TQQQ -> 0 trades and equity == single position_pnl interval minus independently
  re-derived borrow, (c) band vs ladder(hold_days=5) on TQQQ full window, same config
  rate -> both positive, ratio within [0.5, 2].

**Verified:**
- verify_band.py exit 0: (a) exact match; (b) 4961.042928 both sides; (c) band 22.53%
  (125 trades) vs ladder 23.26%, ratio 0.97. verify_engine.py + verify_backtest.py
  still pass (engine/data/config untouched -- confirmed via git diff, only app.py
  modified plus two new files).
- Item-2 gate via streamlit AppTest: TSLL @ 240 lookback renders both strategies with
  no exceptions; pair + lookback preserved switching Ladder -> Band -> Ladder; band
  sliders and Trades / Total turnover metrics present. Headless boot: all three pages
  HTTP 200, no tracebacks.

**Follow-up (same day, committed separately):** band is now the DEFAULT strategy on
the main page (radio order Band/Ladder). Pair_Analysis got the same strategy radio:
band default, delta/short band sliders replace hold_days when selected; leaderboard()
reparameterized (strategy + both strategies' params in the cache key), band rows use
gross["breakeven_borrow"] (same formula as the ladder's engine call at zero borrow)
and add a Trades column. docs/strategy.md restructured: "two ways to trade it" intro,
band section (default) + ladder section (reference), band metrics explained,
ladder-only charts labeled. Verified via AppTest: main page defaults Band; leaderboard
renders 13 rows both strategies, Trades column band-only, lookback survives the
switch. NOTE: AppTest cannot run a pages/ file standalone (st.page_link needs the
multipage runtime; from_string temp dir breaks the relative sys.path insert) -- test
stubs the link + pins src/ absolutely; real multipage covered by headless boot (all
three pages 200, no tracebacks).

**Follow-up 2 (same day): band per-trade log.** band.py now records one row per
band-triggered rebalance (open/close date of the closed segment, trigger name,
entry/exit prices, per-leg P/L from the realization engine call, traded_lev/
traded_und = that trade's turnover increments) and returns it as "trades"
(DataFrame, matching run_backtest's key; the final still-open segment is not
listed). app.py shows a band trades table (adds Trigger + Traded columns) and the
per-trade P/L bar for both strategies -- bar chart factored into trade_pnl_bar()
(second concrete use). verify_band extended: (a) asserts 1 row / 'delta band' /
P&L 150 / traded 150-und 0-lev; (b) asserts empty trades log. All pass with
IDENTICAL equity numbers to before (tracking is bookkeeping only); TSLL invariant
check: len(trades) == n_trades (105), traded columns sum exactly to both turnover
totals (100 short-band / 5 delta-band trips).

**Next:**
- Maybe: band params on the leaderboard rows.

**Open questions / blockers:**
- band_prototype.py: user confirmed they deleted it after adaptation (2026-07-05).

---

### 2026-07-05 (fix deployed 429 crash: seed price data + polygon retry)
**Did:**
- Diagnosed the deployed Streamlit Cloud leaderboard crash: cache/ was gitignored, so
  the deploy had no price CSVs; the leaderboard's 26 cold get_prices calls hit
  Polygon's free-tier ~5 req/min limit -> HTTP 429 -> raise_for_status crash. Local
  never saw it because the cache was warmed once, slowly.
- Fix 1: price CSVs are now COMMITTED seed data. .gitignore narrowed from cache/ to
  just cache/borrow_rates.csv + cache/borrow_history.csv (transient/locally-accruing).
  Re-warmed all 24 tickers paced (13s apart) so every committed window ends today.
  Deleted stale cache/SQQQ.csv (SQQQ left PAIRS in the 10-pair rekey; inverse = v2).
  Tradeoff: deployed price data is frozen at the last committed fetch -- refresh by
  deleting cache/*.csv, rerunning the warm loop, committing.
- Fix 2: _fetch_polygon retries up to 5x on HTTP 429 with a 15s sleep (safety net for
  any remaining cold fetch). Other HTTP errors still raise immediately.
- CLAUDE.md commit rules / file map / secrets notes updated for the new cache split.

**Verified:**
- Warm loop: 24/24 tickers fetched clean (500 rows each, no 429s at 13s pacing).
- verify_engine.py + verify_backtest.py pass on the fresh data; all 13 pairs run
  (240-day windows ending 2026-07-02). App boots headless, both pages 200.
- git status confirmed borrow_rates.csv / borrow_history.csv stay untracked under the
  narrowed .gitignore; only price CSVs staged.

**Incident (owned + fixed):** the warm loop's `rm -f cache/*.csv` also deleted
borrow_rates.csv and borrow_history.csv (the accruing history!). Recovered:
borrow_rates refetches itself; the 2026-07-03 history rows were reconstructed from the
exact values logged in that session's gate output (this file, entry below), and
2026-07-05 rows re-logged on refetch. Verified 13 rows per date afterward. Lesson: the
borrow CSVs live alongside the price CSVs, so never glob-delete cache/*.csv -- delete
price files by ticker list, or move the borrow files out of cache/ someday.

**Next:**
- Redeploy on Streamlit Cloud and confirm the leaderboard renders.

**Open questions / blockers:**
- Committed price data goes stale; fine for the demo, revisit if this becomes a
  daily-use tool (ROADMAP has the longer-history/paid-tier item).

---

### 2026-07-05 (remove borrow sliders; live rate on the main page)
**Did:**
- Removed both manual borrow-rate controls now that rates are live data: the main
  page's "Borrow rate (annual %)" slider and the leaderboard's "Override borrow rate
  for all pairs" checkbox+slider. Net-rate resolution everywhere is now live IBKR rate
  for the leveraged ticker where available, else the pair's config fallback -- no
  manual tier.
- app.py: same st.cache_data(ttl="1h") borrow_rates() wrapper as the leaderboard;
  resolved rate feeds run_backtest unchanged (run() still keys its cache on the rate).
  New metric "Borrow rate (annual)" next to "Borrow paid" showing the rate and its
  source, e.g. "0.82% (live)" or "1.00% (config fallback)". Disclaimer updated (no
  longer says "adjustable below").
- Leaderboard: leaderboard() lost its borrow_rate_annual param and the "override"
  source value; docstrings/intro updated.

**Verified:**
- TQQQ resolves 0.8175% (live); borrow_paid $53.36 matches the live-rate run from the
  2026-07-03 session exactly. Two-day-old cache refetched cleanly (new fetched_at,
  history file gained day two of rows automatically). Both pages boot headless, 200,
  no tracebacks. No leftover override/slider references (grep-checked).

**Next:**
- Let borrow_history.csv accrue for the time-varying-borrow reader (ROADMAP).

**Open questions / blockers:**
- None.

---

## Session template (copy this)

### YYYY-MM-DD (spec NNN: one-line description)
**Spec:** `specs/spec_NNN.md`

**Shipped:**
-

**Surprised us:**
-

**Next:**
-

---
