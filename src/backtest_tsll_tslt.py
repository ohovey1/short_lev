#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Aug 24 14:58:15 2026

@author: fionastrasser
"""

# -*- coding: utf-8 -*-
import pandas as pd

import band
import config
import data

BASE_CAPITAL = 10000
CAPITAL_UTILIZATION = 0.75
LOOKBACK_DAYS = None

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
            
def run(pair_key, base_capital, long_short_band, foil_decay_band, capital_utilization,
        lookback_days, rate): # pair_key, rate):
    return band.run_band_backtest(
        pair_key, base_capital, long_short_band=long_short_band, foil_decay_band=foil_decay_band,
        capital_utilization=capital_utilization, lookback_days=lookback_days,
        borrow_rate_annual=rate,
    )

def run_backtest(pair_key, pair, capital_utilization, base_capital, 
                    lookback_days, live): 
    rows = []
    #  for pair_key, pair in config.PAIRS.items():
    gross = run(pair_key, base_capital, 0.1, 0.1,
                capital_utilization, lookback_days,
                rate=0.0)
    if pair_key in live:
        net_rate, source = live[pair_key]["rate"], "live"
    else:
        net_rate, source = pair["borrow_rate_annual"], "config fallback"
    net = run(pair_key, base_capital, 0.1, 0.1,
                capital_utilization, lookback_days,
                net_rate) # pair_key, net_rate)

    # Same formula: at zero borrow, breakeven_borrow = gross edge / basis.
    breakeven = gross["breakeven_borrow"]
    
    row = {
        "Pair": f"{pair_key} / {pair['underlying_ticker']}",
        "Long Short Band": 0.1,
        "Foil Decay Band": 0.1,
        "Leverage": pair["leverage"],
        "Gross return %": gross["pct_return"] * 100,
        "Net return %": net["pct_return"] * 100,
        "Borrow paid ($)": net["borrow_paid"],
        "Max drawdown %": net["max_drawdown"] / base_capital * 100,
        "Worst day %": net["worst_day"] / base_capital * 100,
        "Trades": net["n_trades"]
    }

    row.update({
        "Rate source": source,
        "Shares available": live[pair_key]["available"] if pair_key in live else None,
        "Borrow rate (used) %": net_rate * 100,
        "Breakeven borrow rate % (annualized)": breakeven * 100,
    })
    
    row["Min margin cushion ($)"] = net["min_margin_cushion"]
    # A stopped pair's return is its return at the stop -- the column
    # says enough, so the figure is not annotated.
    row["Stopped"] = net["stopped"]
    row["De-risks"] = net["n_derisk"]
    rows.append(row)
    
    return pd.DataFrame(rows)

def run_all_pairs():
    list_of_dfs = []
    
    for pair_key, pair in config.PAIRS.items():
        df = run_backtest(pair_key, pair, CAPITAL_UTILIZATION,
                        BASE_CAPITAL, LOOKBACK_DAYS, live)
        
        list_of_dfs.append(df)

    final_df = pd.concat(list_of_dfs, ignore_index=True)
    
    return final_df

result = run_all_pairs()
result.to_csv('backtest_results.csv', header=True, index=True)
print(result)
            