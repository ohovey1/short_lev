"""Layer 4: Streamlit UI for the overlapping-tranche backtest.

Run from the repo root:
    streamlit run src/app.py

Presentation only -- every number comes from backtest.run_backtest. No P&L
math here.
"""

import os
import sys

# Make sibling modules importable however the script is launched.
sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

import config
import data
import backtest

DISCLAIMER = "Fees (incl. borrow) omitted -- results are optimistic and not a verdict."


@st.cache_data
def run(underlying, hold_days, base_capital, lookback_days):
    """Cached wrapper so slider nudges don't recompute the loop needlessly."""
    return backtest.run_backtest(
        underlying, hold_days, base_capital, lookback_days=lookback_days
    )


@st.cache_data
def window_length(underlying):
    """Number of aligned trading days available for a pair (for slider bounds)."""
    pair = config.PAIRS[underlying]
    lev = data.get_prices(pair["leveraged_ticker"])
    und = data.get_prices(pair["underlying_ticker"])
    return len(lev.index.intersection(und.index))


st.title("Leveraged-ETF decay backtest")
st.write(
    "Short the leveraged ETF, long the underlying, open one tranche per day and "
    "hold each for a set number of days. The ladder of overlapping holds harvests "
    "the leveraged fund's daily-reset decay."
)
st.warning(DISCLAIMER)

# --- Sidebar controls ---
st.sidebar.header("Settings")

underlying = st.sidebar.selectbox(
    "Pair",
    list(config.PAIRS.keys()),
    format_func=lambda u: f"{config.PAIRS[u]['leveraged_ticker']} / {u}",
)

n_days = window_length(underlying)

# Preset lookback windows (trading days). "Max" uses the full cached window.
LOOKBACK_PRESETS = [30, 60, 120, 240, 360]
preset = st.sidebar.radio(
    "Lookback",
    [str(d) for d in LOOKBACK_PRESETS] + ["Max"],
    index=3,  # default 240 days
    horizontal=True,
)
lookback = n_days if preset == "Max" else min(int(preset), n_days)

# Cap hold_days so the ladder fits inside the chosen window.
hold_days = st.sidebar.slider(
    "Hold days",
    min_value=1,
    max_value=max(2, lookback // 2),
    value=min(5, max(2, lookback // 2)),
)

base_capital = st.sidebar.number_input(
    "Base capital ($)", min_value=100, value=10000, step=1000
)

# --- Run + render ---
result = run(underlying, hold_days, base_capital, lookback)

pair = config.PAIRS[underlying]

st.subheader(f"{pair['underlying_ticker']} price (underlying)")
st.line_chart(result["und_prices"].rename(pair["underlying_ticker"]))

st.subheader(f"{pair['leveraged_ticker']} price (leveraged)")
st.line_chart(result["lev_prices"].rename(pair["leveraged_ticker"]))

st.subheader("Equity curve ($)")
st.line_chart(result["equity_curve"])

st.subheader("Metrics")
c1, c2, c3 = st.columns(3)
c1.metric("Starting capital", f"${result['starting_capital']:,.2f}")
c2.metric("Ending capital", f"${result['ending_capital']:,.2f}")
c3.metric("Total return", f"${result['total_return']:,.2f}")
c4, c5, c6 = st.columns(3)
c4.metric("Max drawdown", f"${result['max_drawdown']:,.2f}")
c5.metric("Worst day", f"${result['worst_day']:,.2f}")
c6.metric("Return %", f"{result['pct_return']:.2%}")

st.subheader("Trades")
st.caption("Each row is one tranche held to its full hold_days, closed at that day's prices.")
trades = result["trades"]
st.dataframe(
    trades,
    hide_index=True,
    column_config={
        "open_date": st.column_config.DateColumn("Open"),
        "close_date": st.column_config.DateColumn("Close"),
        "lev_entry": st.column_config.NumberColumn("Lev in", format="%.2f"),
        "lev_exit": st.column_config.NumberColumn("Lev out", format="%.2f"),
        "und_entry": st.column_config.NumberColumn("Und in", format="%.2f"),
        "und_exit": st.column_config.NumberColumn("Und out", format="%.2f"),
        "short_pnl": st.column_config.NumberColumn("Short P/L", format="$%.2f"),
        "long_pnl": st.column_config.NumberColumn("Long P/L", format="$%.2f"),
        "total_pnl": st.column_config.NumberColumn("Total P/L", format="$%.2f"),
    },
)
