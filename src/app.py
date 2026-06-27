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

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import data
import backtest

DISCLAIMER = "NOTE: Fees (including borrowing fees) are omitted -- results are optimistic and not a verdict."


def price_chart(ohlc, title, candles):
    """Price figure for an OHLC frame: candlestick if candles else a close line."""
    if candles:
        trace = go.Candlestick(
            x=ohlc.index,
            open=ohlc["open"], high=ohlc["high"],
            low=ohlc["low"], close=ohlc["close"],
            name=title,
        )
    else:
        trace = go.Scatter(x=ohlc.index, y=ohlc["close"], mode="lines", name=title)
    fig = go.Figure(trace)
    fig.update_layout(
        title=title, xaxis_rangeslider_visible=False, height=350,
        margin=dict(l=0, r=0, t=30, b=0),
    )
    return fig


def equity_chart(equity_dollars):
    """Equity curve with the y-axis fit to the data (~5% padding) so the curve
    fills the chart instead of being dwarfed by a 0-anchored axis."""
    lo, hi = equity_dollars.min(), equity_dollars.max()
    pad = (hi - lo) * 0.05 or 1.0  # avoid a zero-width range on a flat curve
    fig = go.Figure(go.Scatter(x=equity_dollars.index, y=equity_dollars, mode="lines"))
    fig.update_layout(
        height=350, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="Equity ($)",
    )
    fig.update_yaxes(range=[lo - pad, hi + pad])
    return fig


def price_header(label, ohlc):
    """Header row: chart label on the left, the asset's window return (close-to-
    close) on the right, green for positive and red for negative."""
    pct = ohlc["close"].iloc[-1] / ohlc["close"].iloc[0] - 1
    color = "green" if pct >= 0 else "red"
    left, right = st.columns([3, 1])
    left.subheader(label)
    right.markdown(
        f"<div style='text-align:right; font-size:1.3rem; font-weight:600; "
        f"color:{color}'>{pct:+.2%}</div>",
        unsafe_allow_html=True,
    )


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

chart_style = st.sidebar.radio("Price chart", ["Line", "Candlestick"], horizontal=True)
candles = chart_style == "Candlestick"

# Docs section: link to the strategy explainer (its own page; default nav hidden).
st.sidebar.header("Docs")
st.sidebar.page_link("pages/Trade_Strategy.py", label="Trade Strategy")

# --- Run + render ---
result = run(underlying, hold_days, base_capital, lookback)

pair = config.PAIRS[underlying]
trades = result["trades"]

price_header(f"{pair['underlying_ticker']} price (underlying)", result["und_ohlc"])
st.plotly_chart(
    price_chart(result["und_ohlc"], pair["underlying_ticker"], candles),
    use_container_width=True,
)

price_header(f"{pair['leveraged_ticker']} price (leveraged)", result["lev_ohlc"])
st.plotly_chart(
    price_chart(result["lev_ohlc"], pair["leveraged_ticker"], candles),
    use_container_width=True,
)

st.subheader("Long vs short P/L ($)")
st.caption(
    "Each leg's cumulative P/L. The legs are hedged -- short trends down, long up -- and "
    "the gap between them is the decay edge that becomes total return."
)
st.line_chart(
    pd.DataFrame({"Long P/L": result["long_curve"], "Short P/L": result["short_curve"]})
)

st.subheader("Metrics")
c1, c2, c3 = st.columns(3)
c1.metric("Starting capital", f"${result['starting_capital']:,.2f}")
c2.metric("Ending capital", f"${result['ending_capital']:,.2f}")
c3.metric("Total return", f"${result['total_return']:,.2f}")
c4, c5, c6 = st.columns(3)
c4.metric("Max drawdown", f"${result['max_drawdown']:,.2f}")
c5.metric("Worst day", f"${result['worst_day']:,.2f}")
c6.metric("Return %", f"{result['pct_return']:.2%}")

st.subheader("Equity curve ($)")
# Display offset only: equity = starting capital + cumulative P/L (not P&L math).
st.plotly_chart(
    equity_chart(result["equity_curve"] + result["starting_capital"]),
    use_container_width=True,
)

st.subheader("Trades")
st.caption("Each row is one tranche held to its full hold_days, closed at that day's prices.")
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

st.subheader("Trade P/L")
if not trades.empty:
    colors = ["green" if v >= 0 else "red" for v in trades["total_pnl"]]
    bar = go.Figure(go.Bar(
        x=trades["close_date"], y=trades["total_pnl"], marker_color=colors,
    ))
    bar.update_layout(
        height=300, margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="Total P/L ($)", xaxis_title="Close date",
    )
    st.plotly_chart(bar, use_container_width=True)
