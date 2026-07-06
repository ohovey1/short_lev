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
import band

DISCLAIMER = (
    "NOTE: Borrow cost uses the live IBKR indicative rate where available, else the "
    "config fallback rate. Expense ratio, spread, and dividends are still omitted "
    "-- results are optimistic and not a verdict."
)


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


def trade_pnl_bar(trades):
    """Green/red bar chart of per-trade total P/L by close date."""
    colors = ["green" if v >= 0 else "red" for v in trades["total_pnl"]]
    bar = go.Figure(go.Bar(
        x=trades["close_date"], y=trades["total_pnl"], marker_color=colors,
    ))
    bar.update_layout(
        height=300, margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="Total P/L ($)", xaxis_title="Close date",
    )
    return bar


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
def run(pair_key, hold_days, base_capital, lookback_days, borrow_rate_annual):
    """Cached wrapper so slider nudges don't recompute the loop needlessly."""
    return backtest.run_backtest(
        pair_key, hold_days, base_capital, lookback_days=lookback_days,
        borrow_rate_annual=borrow_rate_annual,
    )


@st.cache_data
def run_band(pair_key, base_capital, delta_band, short_band, lookback_days,
             borrow_rate_annual):
    """Cached wrapper for the band strategy, mirroring run()."""
    return band.run_band_backtest(
        pair_key, base_capital, delta_band=delta_band, short_band=short_band,
        lookback_days=lookback_days, borrow_rate_annual=borrow_rate_annual,
    )


@st.cache_data
def window_length(pair_key):
    """Number of aligned trading days available for a pair (for slider bounds)."""
    pair = config.PAIRS[pair_key]
    lev = data.get_prices(pair["leveraged_ticker"])
    und = data.get_prices(pair["underlying_ticker"])
    return len(lev.index.intersection(und.index))


@st.cache_data(ttl="1h")
def borrow_rates():
    """Session cache over data.get_borrow_rates so a failed fetch (None) is not
    retried -- with its FTP timeout -- on every Streamlit rerun."""
    return data.get_borrow_rates()


st.title("Leveraged-ETF decay backtest")

# --- Sidebar controls ---
st.sidebar.header("Settings")

pair_key = st.sidebar.selectbox(
    "Pair",
    list(config.PAIRS.keys()),
    format_func=lambda k: f"{k} / {config.PAIRS[k]['underlying_ticker']}",
)

n_days = window_length(pair_key)

# Preset lookback windows (trading days). "Max" uses the full cached window.
LOOKBACK_PRESETS = [30, 60, 120, 240, 360]
preset = st.sidebar.radio(
    "Lookback",
    [str(d) for d in LOOKBACK_PRESETS] + ["Max"],
    index=len(LOOKBACK_PRESETS),  # default Max
    horizontal=True,
)
lookback = n_days if preset == "Max" else min(int(preset), n_days)

strategy = st.sidebar.radio("Strategy", ["Band", "Ladder"], horizontal=True)

if strategy == "Ladder":
    # Cap hold_days so the ladder fits inside the chosen window.
    hold_days = st.sidebar.slider(
        "Hold days",
        min_value=1,
        max_value=max(2, lookback // 2),
        value=min(5, max(2, lookback // 2)),
    )
else:
    delta_band = st.sidebar.slider(
        "Delta band", min_value=0.05, max_value=0.30, value=0.10, step=0.01,
        help="Re-neutralize via the long leg when |net delta| exceeds this fraction of target.",
    )
    short_band = st.sidebar.slider(
        "Short band", min_value=0.05, max_value=0.30, value=0.10, step=0.01,
        help="Reset the short to target when its notional drifts this fraction from target.",
    )

base_capital = st.sidebar.number_input(
    "Base capital ($)", min_value=100, value=10000, step=1000
)

# Borrow rate: live IBKR rate for the leveraged ticker where available, else
# the pair's config fallback. No manual control -- the rate is data now.
rates = borrow_rates()
if rates is not None and pair_key in rates.index:
    borrow_rate_annual = float(rates.loc[pair_key, "fee_rate"])
    borrow_source = "live"
else:
    borrow_rate_annual = config.PAIRS[pair_key]["borrow_rate_annual"]
    borrow_source = "config fallback"

chart_style = st.sidebar.radio("Price chart", ["Line", "Candlestick"], horizontal=True)
candles = chart_style == "Candlestick"

# Docs section: link to the strategy explainer (its own page; default nav hidden).
st.sidebar.header("Docs")
st.sidebar.page_link("pages/Trade_Strategy.py", label="Trade Strategy")
st.sidebar.page_link("pages/Pair_Analysis.py", label="Pair Leaderboard")

# --- Run + render ---
if strategy == "Ladder":
    st.write(
        "Short the leveraged ETF, long the underlying, open one tranche per day and "
        "hold each for a set number of days. The ladder of overlapping holds harvests "
        "the leveraged fund's daily-reset decay."
    )
    result = run(pair_key, hold_days, base_capital, lookback, borrow_rate_annual)
else:
    st.write(
        "Short the leveraged ETF, long the underlying, as one continuous position. "
        "Between trades the hedge is frozen; a trade fires only when the net delta "
        "or the short notional drifts past its band."
    )
    result = run_band(pair_key, base_capital, delta_band, short_band, lookback,
                      borrow_rate_annual)
st.warning(DISCLAIMER)

pair = config.PAIRS[pair_key]

price_header(f"{pair['underlying_ticker']} price (underlying)", result["und_ohlc"])
st.plotly_chart(
    price_chart(result["und_ohlc"], pair["underlying_ticker"], candles),
    use_container_width=True,
)

price_header(f"{pair['leveraged_ticker']} price ({pair['leverage']}x leveraged)", result["lev_ohlc"])
st.plotly_chart(
    price_chart(result["lev_ohlc"], pair["leveraged_ticker"], candles),
    use_container_width=True,
)

if strategy == "Ladder":
    st.subheader("Long vs short P/L ($)")
    st.caption(
        "Each leg's cumulative P/L. The legs are hedged -- short trends down, long up -- and "
        "the gap between them is the decay edge that becomes total return."
    )
    st.line_chart(
        pd.DataFrame({"Long P/L": result["long_curve"], "Short P/L": result["short_curve"]})
    )

st.subheader("Metrics")
if strategy == "Ladder":
    c1, c2, c3 = st.columns(3)
    c1.metric("Starting capital", f"${result['starting_capital']:,.2f}")
    c2.metric("Ending capital", f"${result['ending_capital']:,.2f}")
    c3.metric("Total return", f"${result['total_return']:,.2f}")
    c4, c5, c6 = st.columns(3)
    c4.metric("Max drawdown", f"${result['max_drawdown']:,.2f}")
    c5.metric("Worst day", f"${result['worst_day']:,.2f}")
    c6.metric("Return %", f"{result['pct_return']:.2%}")
    c7, c8, _ = st.columns(3)
    c7.metric("Borrow paid", f"${result['borrow_paid']:,.2f}")
    c8.metric("Borrow rate (annual)", f"{borrow_rate_annual:.2%} ({borrow_source})")
else:
    # Display offset only: capital totals = base capital + cumulative P/L.
    total_return = result["equity_curve"].iloc[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric("Starting capital", f"${base_capital:,.2f}")
    c2.metric("Ending capital", f"${base_capital + total_return:,.2f}")
    c3.metric("Total return", f"${total_return:,.2f}")
    c4, c5, c6 = st.columns(3)
    c4.metric("Max drawdown", f"${result['max_drawdown']:,.2f}")
    c5.metric("Worst day", f"${result['worst_day']:,.2f}")
    c6.metric("Return %", f"{result['pct_return']:.2%}")
    c7, c8, c9 = st.columns(3)
    c7.metric("Borrow paid", f"${result['borrow_paid']:,.2f}")
    c8.metric("Borrow rate (annual)", f"{borrow_rate_annual:.2%} ({borrow_source})")
    c9.metric("Breakeven borrow", f"{result['breakeven_borrow']:.2%}")
    c10, c11, _ = st.columns(3)
    c10.metric("Trades", f"{result['n_trades']}")
    c11.metric(
        "Total turnover",
        f"${result['turnover_lev'] + result['turnover_und']:,.2f}",
    )

st.subheader("Equity curve ($)")
# Display offset only: equity = starting capital + cumulative P/L (not P&L math).
st.plotly_chart(
    equity_chart(result["equity_curve"] + base_capital),
    use_container_width=True,
)

trades = result["trades"]
st.subheader("Trades")
if strategy == "Ladder":
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
else:
    st.caption(
        "Each row is one band-triggered rebalance: it closes the segment opened at "
        "the prior trade (P/L is that segment's, via the engine) and shows which "
        "band fired and the dollars traded to re-neutralize. The final still-open "
        "segment is not listed."
    )
    st.dataframe(
        trades,
        hide_index=True,
        column_config={
            "open_date": st.column_config.DateColumn("Open"),
            "close_date": st.column_config.DateColumn("Close"),
            "trigger": st.column_config.TextColumn("Trigger"),
            "lev_entry": st.column_config.NumberColumn("Lev in", format="%.2f"),
            "lev_exit": st.column_config.NumberColumn("Lev out", format="%.2f"),
            "und_entry": st.column_config.NumberColumn("Und in", format="%.2f"),
            "und_exit": st.column_config.NumberColumn("Und out", format="%.2f"),
            "short_pnl": st.column_config.NumberColumn("Short P/L", format="$%.2f"),
            "long_pnl": st.column_config.NumberColumn("Long P/L", format="$%.2f"),
            "total_pnl": st.column_config.NumberColumn("Total P/L", format="$%.2f"),
            "traded_lev": st.column_config.NumberColumn("Traded lev", format="$%.2f"),
            "traded_und": st.column_config.NumberColumn("Traded und", format="$%.2f"),
        },
    )

st.subheader("Trade P/L")
if not trades.empty:
    st.plotly_chart(trade_pnl_bar(trades), use_container_width=True)
