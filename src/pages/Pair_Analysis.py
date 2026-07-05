"""Cross-pair leaderboard: one row per pair in config.PAIRS.

Reached via the sidebar (default multipage nav is hidden). Runs the backtest
twice per pair -- once at zero borrow (gross) and once at a net borrow rate --
and tables the comparison. The net rate per pair is the live IBKR indicative
rate for the leveraged ticker (data.get_borrow_rates) where available, else
the config fallback rate. Presentation only: every number comes from
backtest.run_backtest or engine.breakeven_borrow_rate, no P&L math here.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import streamlit as st

import config
import backtest
import data
import engine

st.set_page_config(layout="wide")

st.page_link("app.py", label="< Back to backtest")
st.title("Pair leaderboard")
st.write(
    "Every pair in the registry, run once gross (zero borrow) and once net (the "
    "live IBKR borrow rate where available, else the config rate). Sortable -- "
    "click a column header."
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

@st.cache_data(ttl="1h")
def borrow_rates():
    """Session cache over data.get_borrow_rates so a failed fetch (None) is not
    retried -- with its FTP timeout -- on every Streamlit rerun."""
    return data.get_borrow_rates()


rates = borrow_rates()

# Live rate per pair, keyed by the leveraged ticker (the leg we short and pay
# borrow on). Pairs missing from the IBKR list fall back to config.
live = {}
if rates is not None:
    for _pair_key in config.PAIRS:
        if _pair_key in rates.index:
            live[_pair_key] = {
                "rate": float(rates.loc[_pair_key, "fee_rate"]),
                "available": int(rates.loc[_pair_key, "available"]),
            }


@st.cache_data
def leaderboard(hold_days, base_capital, lookback_days, live):
    """Gross + net backtest for every pair, tabled into one leaderboard row each.

    Net rate per pair: the live rate from the live dict where present, else
    the pair's config rate.
    """
    rows = []
    for pair_key, pair in config.PAIRS.items():
        gross = backtest.run_backtest(
            pair_key, hold_days, base_capital,
            lookback_days=lookback_days, borrow_rate_annual=0.0,
        )
        if pair_key in live:
            net_rate, source = live[pair_key]["rate"], "live"
        else:
            net_rate, source = pair["borrow_rate_annual"], "config fallback"
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
            "Rate source": source,
            "Shares available": live[pair_key]["available"] if pair_key in live else None,
            "Borrow rate (used) %": net_rate * 100,
            "Breakeven borrow rate % (annualized)": engine.breakeven_borrow_rate(
                gross["total_return"], gross["notional_days"]
            ) * 100,
        })
    return pd.DataFrame(rows)


if rates is not None:
    st.caption(f"Live IBKR indicative borrow rates fetched {rates['fetched_at'].iloc[0]}.")
else:
    st.warning(
        "Live IBKR borrow rates unavailable -- all rows use config fallback rates."
    )

df = leaderboard(hold_days, base_capital, lookback_days, live)
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
        "Shares available": st.column_config.NumberColumn(format="%.0f"),
        "Borrow rate (used) %": st.column_config.NumberColumn(format="%.2f%%"),
        "Breakeven borrow rate % (annualized)": st.column_config.NumberColumn(format="%.2f%%"),
    },
)
