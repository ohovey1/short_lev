"""Cross-pair leaderboard: one row per pair in config.PAIRS.

Reached via the sidebar (default multipage nav is hidden). Runs the backtest
twice per pair -- once at zero borrow (gross) and once at each pair's config
borrow rate, or one overridden rate for every pair if the sidebar override is
on (net) -- and tables the comparison. Presentation only: every number comes
from backtest.run_backtest or engine.breakeven_borrow_rate, no P&L math here.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import streamlit as st

import config
import backtest
import engine

st.set_page_config(layout="wide")

st.page_link("app.py", label="< Back to backtest")
st.title("Pair leaderboard")
st.write(
    "Every pair in the registry, run once gross (zero borrow) and once net (each "
    "pair's configured borrow rate, or one overridden rate for all pairs if set below). "
    "Sortable -- click a column header."
)
st.warning(
    "NOTE: Borrow cost uses indicative annual rates (see config.py) -- hand-refresh "
    "from IBKR/iBorrowDesk for accuracy. Expense ratio, spread, and dividends are "
    "still omitted -- results are optimistic and not a verdict. The breakeven borrow "
    "rate is the annualized borrow rate at which the pair's net P&L is zero; the pair "
    "is only viable if real borrow is below this."
)

# --- Sidebar controls (same widgets as app.py) ---
st.sidebar.header("Settings")

# Same preset radio as app.py. Unlike app.py there's no single pair to cap "Max"
# against (this page runs every pair) -- a pair with less history than the chosen
# preset just gets truncated to what it has (run_backtest already handles that).
LOOKBACK_PRESETS = [30, 60, 120, 240, 360]
preset = st.sidebar.radio(
    "Lookback",
    [str(d) for d in LOOKBACK_PRESETS] + ["Max"],
    index=3,  # default 240 days
    horizontal=True,
)
lookback_days = None if preset == "Max" else int(preset)

# Cap hold_days the same way app.py does, off the chosen preset (Max uses the
# largest preset as a stand-in bound since there's no single window_length here).
lookback_for_bounds = lookback_days or LOOKBACK_PRESETS[-1]
hold_days = st.sidebar.slider(
    "Hold days", min_value=1, max_value=max(2, lookback_for_bounds // 2),
    value=min(5, max(2, lookback_for_bounds // 2)),
)

base_capital = st.sidebar.number_input(
    "Base capital ($)", min_value=100, value=10000, step=1000
)

override_borrow = st.sidebar.checkbox("Override borrow rate for all pairs")
borrow_rate_annual = None
if override_borrow:
    borrow_rate_annual = st.sidebar.slider(
        "Borrow rate (annual %)", min_value=0.0, max_value=30.0, value=1.0, step=0.5,
    ) / 100


@st.cache_data
def leaderboard(hold_days, base_capital, lookback_days, borrow_rate_annual):
    """Gross + net backtest for every pair, tabled into one leaderboard row each.

    borrow_rate_annual=None means "use each pair's own config rate" for the net
    run; a float overrides every pair's net run to that one rate.
    """
    rows = []
    for pair_key, pair in config.PAIRS.items():
        gross = backtest.run_backtest(
            pair_key, hold_days, base_capital,
            lookback_days=lookback_days, borrow_rate_annual=0.0,
        )
        net_rate = pair["borrow_rate_annual"] if borrow_rate_annual is None else borrow_rate_annual
        net = backtest.run_backtest(
            pair_key, hold_days, base_capital,
            lookback_days=lookback_days, borrow_rate_annual=net_rate,
        )
        rows.append({
            "Pair": f"{pair_key} / {pair['underlying_ticker']}",
            "Leverage": pair["leverage"],
            "Gross return %": gross["pct_return"] * 100,
            "Net return %": net["pct_return"] * 100,
            "Borrow paid ($)": net["borrow_paid"],
            "Max drawdown %": net["max_drawdown"] / base_capital * 100,
            "Worst day %": net["worst_day"] / base_capital * 100,
            "Borrow rate (used) %": net_rate * 100,
            "Breakeven borrow rate % (annualized)": engine.breakeven_borrow_rate(
                gross["total_return"], gross["notional_days"]
            ) * 100,
        })
    return pd.DataFrame(rows)


df = leaderboard(hold_days, base_capital, lookback_days, borrow_rate_annual)
df = df.sort_values("Net return %", ascending=False)

st.dataframe(
    df,
    hide_index=True,
    column_config={
        "Gross return %": st.column_config.NumberColumn(format="%.2f%%"),
        "Net return %": st.column_config.NumberColumn(format="%.2f%%"),
        "Borrow paid ($)": st.column_config.NumberColumn(format="$%.2f"),
        "Max drawdown %": st.column_config.NumberColumn(format="%.2f%%"),
        "Worst day %": st.column_config.NumberColumn(format="%.2f%%"),
        "Borrow rate (used) %": st.column_config.NumberColumn(format="%.2f%%"),
        "Breakeven borrow rate % (annualized)": st.column_config.NumberColumn(format="%.2f%%"),
    },
)
