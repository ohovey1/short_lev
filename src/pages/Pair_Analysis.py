"""Cross-pair leaderboard: one row per pair in config.PAIRS.

Reached via the sidebar (default multipage nav is hidden). Runs the backtest
twice per pair -- once at zero borrow (gross) and once at the pair's config
borrow rate (net) -- and tables the comparison. Presentation only: every
number comes from backtest.run_backtest or engine.breakeven_borrow_rate, no
P&L math here.
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
    "Every pair in the registry, run once gross (zero borrow) and once net (the "
    "pair's configured borrow rate). Sortable -- click a column header."
)
st.warning(
    "NOTE: Borrow cost uses indicative annual rates (see config.py) -- hand-refresh "
    "from IBKR/iBorrowDesk for accuracy. Expense ratio, spread, and dividends are "
    "still omitted -- results are optimistic and not a verdict."
)

# --- Sidebar controls (same widgets as app.py) ---
st.sidebar.header("Settings")

lookback_days = st.sidebar.number_input(
    "Lookback (trading days)", min_value=10, value=240, step=10
)
hold_days = st.sidebar.slider(
    "Hold days", min_value=1, max_value=max(2, lookback_days // 2),
    value=min(5, max(2, lookback_days // 2)),
)
base_capital = st.sidebar.number_input(
    "Base capital ($)", min_value=100, value=10000, step=1000
)


@st.cache_data
def leaderboard(hold_days, base_capital, lookback_days):
    """Gross + net backtest for every pair, tabled into one leaderboard row each."""
    rows = []
    for pair_key, pair in config.PAIRS.items():
        gross = backtest.run_backtest(
            pair_key, hold_days, base_capital,
            lookback_days=lookback_days, borrow_rate_annual=0.0,
        )
        net = backtest.run_backtest(
            pair_key, hold_days, base_capital,
            lookback_days=lookback_days, borrow_rate_annual=pair["borrow_rate_annual"],
        )
        rows.append({
            "Pair": f"{pair_key} / {pair['underlying_ticker']}",
            "Leverage": pair["leverage"],
            "Gross return %": gross["pct_return"],
            "Net return %": net["pct_return"],
            "Borrow paid ($)": net["borrow_paid"],
            "Max drawdown ($)": net["max_drawdown"],
            "Worst day ($)": net["worst_day"],
            "Breakeven borrow rate %": engine.breakeven_borrow_rate(
                gross["total_return"], gross["notional_days"]
            ) * 100,
        })
    return pd.DataFrame(rows)


df = leaderboard(hold_days, base_capital, lookback_days)
df = df.sort_values("Net return %", ascending=False)

st.dataframe(
    df,
    hide_index=True,
    column_config={
        "Gross return %": st.column_config.NumberColumn(format="%.2f%%"),
        "Net return %": st.column_config.NumberColumn(format="%.2f%%"),
        "Borrow paid ($)": st.column_config.NumberColumn(format="$%.2f"),
        "Max drawdown ($)": st.column_config.NumberColumn(format="$%.2f"),
        "Worst day ($)": st.column_config.NumberColumn(format="$%.2f"),
        "Breakeven borrow rate %": st.column_config.NumberColumn(format="%.2f%%"),
    },
)
