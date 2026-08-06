# Spec 001 -- Reconcile the repo with SPEC.md

**Phase:** 0
**Depends on:** `docs/SPEC.md` being in the repo
**Estimated:** one session

## Why

`docs/SPEC.md` is now the source of truth. Several files contradict it, and
several more make claims that were true when written and are no longer. This
session fixes only that. No behavior changes, no new features.

## Out of scope (do not build)

- Closing logic (drawdown stop, margin de-risk) -- that is spec 002.
- Grid search, intraday check modeling, notifications, broker code.
- Any change to `engine.py`. The engine is correct and stays untouched.
- Any change to P&L math anywhere. If a number moves, something is wrong.

---

## 1. Rename the bands to spec terminology

`delta_band` -> `long_short_band`
`short_band` -> `foil_decay_band`

Files with occurrences: `src/band.py`, `src/app.py`, `src/pages/Pair_Analysis.py`,
`scripts/verify_band.py`, `docs/GLOSSARY.md`, `docs/strategy.md`.

**Careful:** do not blanket-replace on the substring `short`. `short_size`,
`short_notional`, `short_pnl`, `short_leg`, and `turnover_lev` are unrelated and
must not change. Only the two parameter names above, plus:

- The `trigger` strings in the trades DataFrame: `"delta band"` ->
  `"long-short band"`, `"short band"` -> `"foil decay band"`.
- Streamlit slider labels: `"Delta band"` -> `"Long-Short Band"`,
  `"Short band"` -> `"Foil Decay Band"` (both `app.py` and `Pair_Analysis.py`).
- Slider help text, which currently describes the bands by the old names.

Keyword arguments change, so every call site of `run_band_backtest` must be
updated in the same commit.

**Done when:** `grep -rn "delta_band\|short_band" src/ scripts/ docs/` returns
nothing, and `scripts/verify_band.py` passes with identical numeric output to
before the rename.

## 2. Fix stale claims in CLAUDE.md

- File map calls `engine.py` a "borrow stub". It is a real accrual. The Scope
  Cuts section in the same file already says so -- make the file map agree.
- The Pairing section says the margin-multiplier and live-flag rework is "in
  progress". It shipped (`dee4c5e`, `180aed8`, `25b65d6`). Update the field list
  to include `live` and `margin_multiplier` and drop the "rework in progress" line.
- It links to `GLOSSARY.md` at the repo root. The file is `docs/GLOSSARY.md`.
- The file map omits `scripts/`, `docs/`, and `src/pages/`. Add them.
- Add a line pointing at `docs/SPEC.md` as the source of truth, and at `specs/`
  for per-session specs.
- The strategy section's three-sentence definition stays, but the band names in
  it change per item 1.

**Done when:** every factual claim in `CLAUDE.md` is true of the current repo.

## 3. Fix stale claims in docs/strategy.md

This file is the in-app explainer (rendered by `src/pages/Trade_Strategy.py`).
Keep it plain-English -- it is not a spec and should not become one.

- It says the position shorts "about `base capital`" of the leveraged ETF. Wrong
  since `margin_multiplier` and `capital_utilization` landed. Correct it to the
  actual formula and explain both terms in one sentence each.
- No mention of margin cushion or capital utilization despite both being sidebar
  controls. Add a short paragraph.
- Band names per item 1.
- Add a link to `docs/SPEC.md` for the full mechanics.

**Done when:** a user reading the explainer page and then moving the sidebar
sliders finds nothing surprising.

## 4. Fix the app.py docstring

It reads "Layer 4: Streamlit UI for the overlapping-tranche backtest" and
"every number comes from `backtest.run_backtest`". Band is the default strategy
and most numbers come from `band.run_band_backtest`. Correct both.

## 5. Record the check-cadence assumption

The backtest evaluates bands once per day on the close. Live polling is every 15
minutes, which will trip bands more often. Add a one-line note stating that
backtest trade count and turnover are a lower bound:

- in the `run_band_backtest` docstring, and
- next to the trades/turnover metrics in the UI (help text is fine).

Do not change any logic. This is a disclosure, not a fix -- the fix is spec 003.

## 6. Standardize the Portfolio Margin threshold

`docs/AUTOMATION.md` and `ROADMAP.md` say $100k. Standardize on **$110,000 NLV
plus options approval** everywhere it appears.

Also in `docs/AUTOMATION.md`: the "Signal source" section argues delayed 15-minute
quotes are acceptable "because the band operates on daily-ish thresholds." That
now sits awkwardly next to a 15-minute poll interval. Keep the conclusion but
distinguish the two clearly -- quote *delay* and poll *interval* are different
things and the current wording blurs them.

---

## Session gate

All of the following must hold before committing:

1. `scripts/verify_band.py` and `scripts/verify_engine.py` pass.
2. Running any pair through the UI produces numerically identical output to
   before this session. This is a rename-and-document session; if a number
   moved, find out why before proceeding.
3. `grep -rn "delta_band\|short_band" src/ scripts/ docs/` is empty.
4. No file claims something contradicted by `docs/SPEC.md`.

Commit as separate commits per numbered item, imperative lowercase with a scope
prefix. Do not commit until I have reviewed the diff.