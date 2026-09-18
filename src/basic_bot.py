#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Aug 25 11:40:44 2026

@author: fionastrasser

Telegram bot to size on demand.

Input: two tickers, leverage multiplier, current share counts, and base
capital.

Fetches IBKR prices, checks foil-decay and long-short conditions same as
decision.evaluate(), with imported thresholds from config

Anything requiring actual peak equity and maintenance margin not included.

Uses same gateway, with own clientId

In Telegram:
    /calc SHORT_TICKER LONG_TICKER LEVERAGE SHARES_SHORT SHARES_LONG BASE_CAPITAL
    /calc TSLT TSLA 2 100 250 10000
"""

import logging
import os
import time
 
from dotenv import load_dotenv
from ib_async import IB, Stock
 
# import config
import config_detailed as config
import notify
import asyncio

import datetime
from zoneinfo import ZoneInfo

import watch_state
 
load_dotenv()
log = logging.getLogger("basic_bot")
 
CALC_CLIENT_ID = int(os.environ.get("CALC_CLIENT_ID", "21"))
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", 4002))
 
LONG_POLL_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = LONG_POLL_SECONDS + 10
BACKOFF_START = 5
BACKOFF_MAX = 300
 
# in case we check a pair not in config, which shouldn't happen now
REG_T_LONG_RATE = 0.25
REG_T_SHORT_RATE_PER_LEVERAGE = 0.30

RECONNECT_BACKOFF_START = 10
RECONNECT_BACKOFF_MAX = 300

DISCONNECT_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError)
 
ET = ZoneInfo("America/New_York")

WATCH_POLL_SECONDS = float(os.environ.get("WATCH_POLL_SECONDS", 180)) # todo
NEARING_BAND_FRACTION = 0.5 # 0.7

FOIL_DECAY_BAND = 0.075 # config.DEFAULT_FOIL_DECAY_BAND
LONG_SHORT_BAND = 0.075 # config.DEFAULT_LONG_SHORT_BAND

def _time_env(name, default_hour, default_minute):
    """
    Same format as heartbeat_time
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default_hour, default_minute
    try:
        if ":" in raw:
            hour, minute = raw.split(":", 1)
            return int(hour), int(minute)
        return int(raw), 0
    except ValueError:
        log.warning("%s=%r is not readable; using %02d:%02d",
                    name, raw, default_hour, default_minute)
        return default_hour, default_minute

MORNING_HOUR, MORNING_MINUTE = _time_env("WATCHING_MORNING_HOUR", 9, 30)
EOD_HOUR, EOD_MINUTE = _time_env("WATCH_EOD_HOUR", 15, 55)

_BY_LEVERAGED = {pair["leveraged_ticker"].upper(): pair for pair in config.PAIRS.values()}
_KEY_BY_LEVERAGED = {pair["leveraged_ticker"].upper(): k for k, pair in config.PAIRS.items()}

def _bands_for(ticker):
    pair = _BY_LEVERAGED.get(ticker.upper())
    if pair:
        ls_band, foil_band, source = pair["ideal_ls_band"], pair["ideal_foil_decay_band"], "config.PAIRS backtested"
        if ls_band is not None and foil_band is not None:
            return ls_band, foil_band, source
    return (FOIL_DECAY_BAND, LONG_SHORT_BAND, "generic fallback")  

# todo edited 
def _rates_for(short_ticker, leverage):
    pair = _BY_LEVERAGED.get(short_ticker.upper())
    if pair:
        return pair["long_rate"], pair["short_rate"], pair["leverage"], "config.PAIRS (IBKR-observed)"
    return (REG_T_LONG_RATE, REG_T_SHORT_RATE_PER_LEVERAGE * leverage, leverage,
            "generic Reg-T fallback -- NOT IBKR-confirmed for this ticker")    

_SNAPSHOT_WAIT_SECONDS = 8.0
_SNAPSHOT_POLL_SECONDS = 0.25
 
def _price(ib, ticker):
    """Attempts to fetch a live price, or returns None."""
    contract = Stock(ticker, "SMART", "USD")
    [qualified] = ib.qualifyContracts(contract)
    if qualified.conId == 0:
        # qualifyContracts leaves conId at 0, rather than raising when IBKR
        # doesn't recognize the symbol (like typo). Treat it the same as
        # "no price" so the existing missing-symbol message covers it.
        return None
    
    ticker_obj = ib.reqMktData(qualified, "", False, False)
    try:
        waited = 0.0
        price = ticker_obj.marketPrice()
    
        while (price is None or price != price or price <= 0) and waited < _SNAPSHOT_WAIT_SECONDS:  # NaN check
            ib.sleep(_SNAPSHOT_POLL_SECONDS)
            waited += _SNAPSHOT_POLL_SECONDS
            price = ticker_obj.marketPrice()
    finally: 
        ib.cancelMktData(qualified)
            
    if price is None or price != price or price <= 0:
        return None
    return price

def _resolve_pair_key(raw):
    """Accept both underlying and leveraged ticker and return config.PAIRS key.
    Or None if neither matches."""
    key = raw.upper()
    if key in config.PAIRS:
        return key
    return _KEY_BY_LEVERAGED.get(key)

def build_calcfull_reply(ib, args, state):
    """Args in, reply text out. Pure given the ib price lookups."""
    e = notify.escape_md_v2
    
    # todo
    # branch for existing tracked positions
    if len(args) == 1:
        pair_key_raw = args[0].upper()
        pair_key = _resolve_pair_key(pair_key_raw)
        # if pair_key not in config.PAIRS:
        if pair_key is None:
            return e(f"Unknown pair {pair_key}.upper(). Configured pairs (by underlying): {', '.join(config.PAIRS)}")
        entry = state["pairs"].get(pair_key)
        ss = entry.get("shares_short") if entry else None
        sl = entry.get("shares_long") if entry else None
        bc = entry.get("base_capital") if entry else None
        if ss is None or sl is None or bc is None:
            return e(f"{pair_key} isn't being tracked yet -- use /setshares "
                     f"first, or give full /calc args.")
        if ss == 0 and sl == 0:
            return e(f"{pair_key} is paused (0/0) -- use /resize to set numbers "
                     f"first, or give full /calc args.")
        pair = config.PAIRS[pair_key]
        args = [pair["leveraged_ticker"], pair["underlying_ticker"], str(pair["leverage"]),
                str(ss), str(sl), str(bc)]
    
    if len(args) not in (5, 6):
        return e(USAGE)
 
    short_ticker, long_ticker, leverage_s, shares_short_s, shares_long_s = args[:5]
    base_capital_s = args[5] if len(args) == 6 else None
 
    try:
        leverage_in = float(leverage_s)
        shares_short = float(shares_short_s)
        shares_long = float(shares_long_s)
        base_capital = float(base_capital_s) if base_capital_s is not None else None
    except ValueError:
        return e("Leverage, shares, and base_capital must all be numbers.\n\n") + e(USAGE)
    
    if base_capital is None and shares_long == 0 and shares_short == 0:
        return (e("BASE_CAPITAL is required when both share coutns are 0 --"
                "there's no held position to derive it from.\n\n") + e(USAGE))
 
    price_short = _price(ib, short_ticker)
    price_long = _price(ib, long_ticker)
    if price_short is None or price_long is None:
        missing = []
        if price_short is None:
            missing.append(short_ticker.upper())
        if price_long is None:
            missing.append(long_ticker.upper())
        return (e(f"Could not get a live price for: {', '.join(missing)}. ") + 
                e("Check the symbol(s) and try again."))
 
    long_rate, short_rate, leverage, rate_source = _rates_for(short_ticker, leverage_in)
    margin_mult = long_rate * leverage + short_rate
 
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    
    derived_from = None # None, long, or short
    
    if base_capital is None:
        if shares_long != 0:
            derived_from = "long"
            target_for_derivation = long_notional / leverage
        else:
            derived_from = "short"
            target_for_derivation = short_notional        
        base_capital = target_for_derivation * margin_mult / config.DEFAULT_CAPITAL_UTILIZATION
        
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    twice_base = leverage * short_notional
    net_delta = long_notional - leverage * short_notional
    long_or_short = "long 🟢" if net_delta > 0 else "short 🔴"
 
    lines = [
        "*" + e(f"CURRENT PRICES FOR {short_ticker.upper()} & {long_ticker.upper()}") + "*",
        e(f"{long_ticker.upper()} (long)  @ ${price_long:,.2f} x {shares_long:,.0f} sh "
          f"= ${long_notional:,.2f}"),
        e(f"{short_ticker.upper()} (short) @ ${price_short:,.2f} x {shares_short:,.0f} sh "
          f"= ${short_notional:,.2f}"),
        # "",
        # e(f"Leverage: {leverage:g}"),
        # e(f"Margin multiplier: {margin_mult:.3f} (long rate={long_rate:.2f}, short rate={short_rate:.2f})"),
        # e(f"Rates source: {rate_source}"),
        "",
        "*" + e("TARGET PARAMETERS") + "*",
        e(f"Leverage {leverage:g} * {short_ticker.upper()} ${short_notional:,.2f} = "),
        "*" + e(f"${twice_base:,.2f}\n") + "*",
        e("Net distance limit = "),
        e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
          f"short ${short_notional:,.2f}"),
        "*" + e(f"= ${net_delta:,.2f}") + "*",
        e(f"Position is {long_or_short}")
        ]
    if derived_from == "long":
        lines.append(
            e(f"base_capital not given -- derived as ${base_capital:,.2f} from "
              f"the long leg (${long_notional:,.2f} invested, assuming the book "
              "is balanced at target."),
        )
        lines.append("")
    elif derived_from == "short":
        lines.append(
            e(f"base_capital not given -- derived as ${base_capital:,.2f} from "
              f"the short leg (${short_notional:,.2f} held, treated as sitting "
              "exactly on target."),
        )
        lines.append("")
        
    long_short_band, foil_decay_band, source = _bands_for(short_ticker)    
    signed_foil = (short_notional - target) / target / foil_decay_band
    signed_ls = net_delta / (pair["leverage"] * target) / long_short_band
    
    ls_direction = "long ➡️🟢" if signed_ls > 0 else "short ⬅️🔴"
    foil_direction = "long ➡️🟢" if signed_foil > 0 else "short ⬅️🔴"
     
    lines += [
        # e(f"target (short) = base_capital ${base_capital:,.2f} x "),
        # e(f"capital_utilization {config.DEFAULT_CAPITAL_UTILIZATION:.0%} / "),
        # e(f"margin_multiplier {margin_mult:.3f}"),
        # e(f"= {target:,.2f}"),
        # "",
        # e("Net distance limit = "),
        #  e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
        #   f"short ${short_notional:,.2f}"),
        # e(f"= ${net_delta:,.2f}"),
        "",
        "*" + e("BANDS") + "*",
        e(f"Trip limits: long_short = {long_short_band:.2%}  "
          f"FOIL_decay = {foil_decay_band:.2%}\n"),
        # e(f"Long-short: {_band_bar(signed_ls)}"),
        e(f"Long-short band: {abs(net_delta) / (pair['leverage'] * target):.1%} off target."),
        e(f"Direction is {ls_direction}."),
        # e(f"{signed_ls:.1%} of a {config.DEFAULT_LONG_SHORT_BAND:.0%} band."),
        # e(f"FOIL decay: {_band_bar(signed_foil)}"),
        e(f"FOIL decay band: {abs(short_notional - target) / target:.1%} off target."),
        e(f"Direction is {foil_direction}."),
        # e(f"{signed_foil:.1%} of a {config.DEFAULT_FOIL_DECAY_BAND:.0%} band."),
        "",
        "*" + e("ACTION TO TAKE") + "*",
    ]
 
    if abs(short_notional - target) > foil_decay_band * target:
        new_short_shares = round(target / price_short)
        new_long_shares = round((leverage * target) / price_long)
        
        new_target_short = short_notional
        new_long_shares_alt = round((leverage * new_target_short) / price_long)
        
        new_target_long = long_notional / leverage
        new_short_shares_alt = round(new_target_long / price_short)
    
        lines.append(e(
            f"TRIP: FOIL decay band -- short notional is "
            f"{abs(short_notional - target) / target:.1%} off target.\n"
            f"  Option A: Reset both legs to target:\n"
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares:,d} sh\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh\n"
            f" Option B: Short leg unchanged. Reset target to match and resize long leg only:\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares_alt:,d} sh\n"
            f"    Target resized to match short leg: ${target:,.0f} -> ${new_target_short:,.0f}\n"
            f" Option C: Long leg unchanged. Reset target to match and resize short leg only:\n" 
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares_alt:,d} sh\n"
            f"    Target resized to match long leg: ${target:,.0f} -> ${new_target_long:,.0f}"
        ))
    elif abs(net_delta) > long_short_band * leverage * target:
        new_long_shares = round((leverage * short_notional) / price_long)
        new_short_shares_alt = round(long_notional / (leverage * price_short))
        lines.append(e(
            f"TRIP: long-short band -- net delta is "
            f"{abs(net_delta) / (pair['leverage'] * target):.1%} off target.\n"
            f"  Option A: Short leg unchanged. Resize long leg only:\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh\n"
            f"  Option B: Long leg unchanged. Resize short leg only:\n"
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares_alt:,d} sh"
        ))
    else:
        lines.append(e("No trip -- current shares are both within bands."))
    
    """
    lines.append(e(
        "\n(Only foil-decay and long-short are checked here. Drawdown stop and "
        "margin de-risk both need a live position's persisted peak_equity / "
        "actual maintenance margin, which this what-if calculator has no reason to hold.)"
    ))"""
 
    return "\n".join(lines)

USAGE = (
    "Usage: /calc SHORT_TICKER LONG_TICKER LEVERAGE SHARES_SHORT SHARES_LONG [BASE_CAPITAL]\n"
    "Shorthand for existing position: /calc PAIR_KEY -- uses stored shares & target if already"
    "tracked (e.g. /calc TSLA\n"
    "Example (from existing long position): /calc TSLT TSLA 2 100 250\n"
    "Example (from existing short position): /calc TSLT TSLA 2 100 0\n"
    "Example (if no long position yet): /calc TSLT TSLA 2 0 0 10000\n"
    "BASE_CAPITAL is required when both share counts are 0 - -- otherwise derived from"
    "whichever leg is currently held."
    "Does not return 'Action to Take' or foil-decay band. Use /calcfull to get these."
)

def build_calc_reply(ib, args, state):
    """Args in, reply text out. Pure given the ib price lookups."""
    e = notify.escape_md_v2
    
    # todo
    # branch for existing tracked positions
    if len(args) == 1:
        pair_key_raw = args[0].upper()
        pair_key = _resolve_pair_key(pair_key_raw)
        if pair_key is None:
            return e(f"Unknown pair {pair_key}. Configured pairs (by underlying): {', '.join(config.PAIRS)}")
        entry = state["pairs"].get(pair_key)
        ss = entry.get("shares_short") if entry else None
        sl = entry.get("shares_long") if entry else None
        bc = entry.get("base_capital") if entry else None
        if ss is None or sl is None or bc is None:
            return e(f"{pair_key} isn't being tracked yet -- use /setshares "
                     f"first, or give full /calc args.")
        if ss == 0 and sl == 0:
            return e(f"{pair_key} is paused (0/0) -- use /resize to set numbers "
                     f"first, or give full /calc args.")
        pair = config.PAIRS[pair_key]
        args = [pair["leveraged_ticker"], pair["underlying_ticker"], str(pair["leverage"]),
                str(ss), str(sl), str(bc)]
    
    if len(args) not in (5, 6):
        return e(USAGE)
 
    short_ticker, long_ticker, leverage_s, shares_short_s, shares_long_s = args[:5]
    base_capital_s = args[5] if len(args) == 6 else None
 
    try:
        leverage_in = float(leverage_s)
        shares_short = float(shares_short_s)
        shares_long = float(shares_long_s)
        base_capital = float(base_capital_s) if base_capital_s is not None else None
    except ValueError:
        return e("Leverage, shares, and base_capital must all be numbers.\n\n") + e(USAGE)
    
    if base_capital is None and shares_long == 0 and shares_short == 0:
        return (e("BASE_CAPITAL is required when both share coutns are 0 --"
                "there's no held position to derive it from.\n\n") + e(USAGE))
 
    price_short = _price(ib, short_ticker)
    price_long = _price(ib, long_ticker)
    if price_short is None or price_long is None:
        missing = []
        if price_short is None:
            missing.append(short_ticker.upper())
        if price_long is None:
            missing.append(long_ticker.upper())
        return (e(f"Could not get a live price for: {', '.join(missing)}. ") + 
                e("Check the symbol(s) and try again."))
 
    long_rate, short_rate, leverage, rate_source = _rates_for(short_ticker, leverage_in)
    margin_mult = long_rate * leverage + short_rate
 
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    
    derived_from = None # None, long, or short
    
    if base_capital is None:
        if shares_long != 0:
            derived_from = "long"
            target_for_derivation = long_notional / leverage
        else:
            derived_from = "short"
            target_for_derivation = short_notional        
        base_capital = target_for_derivation * margin_mult / config.DEFAULT_CAPITAL_UTILIZATION
        
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    twice_base = leverage * short_notional
    net_delta = long_notional - leverage * short_notional
    long_or_short = "long 🟢" if net_delta > 0 else "short 🔴"
 
    lines = [
        "*" + e(f"CURRENT PRICES FOR {short_ticker.upper()} & {long_ticker.upper()}") + "*",
        e(f"{long_ticker.upper()} (long)  @ ${price_long:,.2f} x {shares_long:,.0f} sh "
          f"= ${long_notional:,.2f}"),
        e(f"{short_ticker.upper()} (short) @ ${price_short:,.2f} x {shares_short:,.0f} sh "
          f"= ${short_notional:,.2f}"),
        # "",
        # e(f"Leverage: {leverage:g}"),
        # e(f"Margin multiplier: {margin_mult:.3f} (long rate={long_rate:.2f}, short rate={short_rate:.2f})"),
        # e(f"Rates source: {rate_source}"),
        "",
        "*" + e("TARGET PARAMETERS") + "*",
        e(f"Leverage {leverage:g} * {short_ticker.upper()} ${short_notional:,.2f} = "),
        "*" + e(f"${twice_base:,.2f}\n") + "*",
        e("Net distance limit = "),
        e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
          f"short ${short_notional:,.2f}"),
        "*" + e(f"= ${net_delta:,.2f}") + "*",
        e(f"Position is {long_or_short}")
        ]
    if derived_from == "long":
        lines.append(
            e(f"base_capital not given -- derived as ${base_capital:,.2f} from "
              f"the long leg (${long_notional:,.2f} invested, assuming the book "
              "is balanced at target."),
        )
        lines.append("")
    elif derived_from == "short":
        lines.append(
            e(f"base_capital not given -- derived as ${base_capital:,.2f} from "
              f"the short leg (${short_notional:,.2f} held, treated as sitting "
              "exactly on target."),
        )
        lines.append("")
        
    long_short_band, foil_decay_band, source = _bands_for(short_ticker)    
    
    # signed_foil = (short_notional - target) / target / FOIL_DECAY_BAND
    signed_ls = net_delta / (leverage_in * target) / long_short_band
    
    ls_direction = "long ➡️🟢" if signed_ls > 0 else "short ⬅️🔴"
     
    lines += [
        # e(f"target (short) = base_capital ${base_capital:,.2f} x "),
        # e(f"capital_utilization {config.DEFAULT_CAPITAL_UTILIZATION:.0%} / "),
        # e(f"margin_multiplier {margin_mult:.3f}"),
        # e(f"= {target:,.2f}"),
        # "",
        # e("Net distance limit = "),
        #  e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
        #   f"short ${short_notional:,.2f}"),
        # e(f"= ${net_delta:,.2f}"),
        "",
        # e(f"bands: long_short={LONG_SHORT_BAND:.2%}  "
        #  f"FOIL_decay={FOIL_DECAY_BAND:.2%}"),
        # e(f"FOIL decay: {_band_bar(signed_foil)}"),
        # e(f"{abs(short_notional - target) / target:.1%} off target."),
        # e(f"{signed_foil:.1%} of a {config.DEFAULT_FOIL_DECAY_BAND:.0%} band."),
        # e(f"Long-short: {_band_bar(signed_ls)}"),
        e(f"Long-short band: {abs(net_delta) / (leverage_in * target):.1%} off target."),
        e(f"Direction is {ls_direction}"),
        # e(f"{signed_ls:.1%} of a {config.DEFAULT_LONG_SHORT_BAND:.0%} band."),
        "",
        "*" + e("TRIPS") + "*",
        e(f"Trip limits: long_short = {long_short_band:.2%}  "
          f"FOIL_decay = {foil_decay_band:.2%}"),
        "",
    ]
 
    if abs(short_notional - target) > foil_decay_band * target:
        lines.append(e(
            f"TRIP: FOIL decay band -- short notional is "
            f"{abs(short_notional - target) / target:.1%} off target.\n"
            "To get specific options for action to take: run /calcaction or /calcfull\n"
        ))
    elif abs(net_delta) > long_short_band * leverage * target:
        lines.append(e(
            f"TRIP: long-short band -- net delta is "
            f"{abs(net_delta) / (leverage_in * target):.1%} off target.\n"
            "To get specific options for action to take: run /calcaction or /calcfull\n"
        ))
    else:
        lines.append(e("No trip -- current shares are both within bands."))
    
    """
    lines.append(e(
        "\n(Only foil-decay and long-short are checked here. Drawdown stop and "
        "margin de-risk both need a live position's persisted peak_equity / "
        "actual maintenance margin, which this what-if calculator has no reason to hold.)"
    ))"""
 
    return "\n".join(lines)

USAGE = (
    "Usage: /calc SHORT_TICKER LONG_TICKER LEVERAGE SHARES_SHORT SHARES_LONG [BASE_CAPITAL]\n"
    "Shorthand for existing position: /calc PAIR_KEY -- uses stored shares & target if already"
    "tracked (e.g. /calc TSLA\n"
    "Example (from existing long position): /calc TSLT TSLA 2 100 250\n"
    "Example (from existing short position): /calc TSLT TSLA 2 100 0\n"
    "Example (if no long position yet): /calc TSLT TSLA 2 0 0 10000\n"
    "BASE_CAPITAL is required when both share counts are 0 - -- otherwise derived from"
    "whichever leg is currently held."
    "Does not return 'Action to Take' or foil-decay band. Use /calcfull to get these."
)

def build_calcaction_reply(ib, args, state):
    """Args in, reply text out. Pure given the ib price lookups."""
    e = notify.escape_md_v2
    
    # todo
    # branch for existing tracked positions
    if len(args) == 1:
        pair_key_raw = args[0].upper()
        pair_key = _resolve_pair_key(pair_key_raw)
        # if pair_key not in config.PAIRS:
        if pair_key is None:
            return e(f"Unknown pair {pair_key}.upper(). Configured pairs (by underlying): {', '.join(config.PAIRS)}")
        entry = state["pairs"].get(pair_key)
        ss = entry.get("shares_short") if entry else None
        sl = entry.get("shares_long") if entry else None
        bc = entry.get("base_capital") if entry else None
        if ss is None or sl is None or bc is None:
            return e(f"{pair_key} isn't being tracked yet -- use /setshares "
                     f"first, or give full /calc args.")
        if ss == 0 and sl == 0:
            return e(f"{pair_key} is paused (0/0) -- use /resize to set numbers "
                     f"first, or give full /calc args.")
        pair = config.PAIRS[pair_key]
        args = [pair["leveraged_ticker"], pair["underlying_ticker"], str(pair["leverage"]),
                str(ss), str(sl), str(bc)]
    
    if len(args) not in (5, 6):
        return e(USAGE)
 
    short_ticker, long_ticker, leverage_s, shares_short_s, shares_long_s = args[:5]
    base_capital_s = args[5] if len(args) == 6 else None
 
    try:
        leverage_in = float(leverage_s)
        shares_short = float(shares_short_s)
        shares_long = float(shares_long_s)
        base_capital = float(base_capital_s) if base_capital_s is not None else None
    except ValueError:
        return e("Leverage, shares, and base_capital must all be numbers.\n\n") + e(USAGE)
    
    if base_capital is None and shares_long == 0 and shares_short == 0:
        return (e("BASE_CAPITAL is required when both share coutns are 0 --"
                "there's no held position to derive it from.\n\n") + e(USAGE))
 
    price_short = _price(ib, short_ticker)
    price_long = _price(ib, long_ticker)
    if price_short is None or price_long is None:
        missing = []
        if price_short is None:
            missing.append(short_ticker.upper())
        if price_long is None:
            missing.append(long_ticker.upper())
        return (e(f"Could not get a live price for: {', '.join(missing)}. ") + 
                e("Check the symbol(s) and try again."))
 
    long_rate, short_rate, leverage, rate_source = _rates_for(short_ticker, leverage_in)
    margin_mult = long_rate * leverage + short_rate
 
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    
    derived_from = None # None, long, or short
    
    if base_capital is None:
        if shares_long != 0:
            derived_from = "long"
            target_for_derivation = long_notional / leverage
        else:
            derived_from = "short"
            target_for_derivation = short_notional        
        base_capital = target_for_derivation * margin_mult / config.DEFAULT_CAPITAL_UTILIZATION
        
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    twice_base = leverage * short_notional
    net_delta = long_notional - leverage * short_notional
    long_or_short = "long 🟢" if net_delta > 0 else "short 🔴"
 
    lines = [
        "*" + e(f"CURRENT PRICES FOR {short_ticker.upper()} & {long_ticker.upper()}") + "*",
        e(f"{long_ticker.upper()} (long)  @ ${price_long:,.2f} x {shares_long:,.0f} sh "
          f"= ${long_notional:,.2f}"),
        e(f"{short_ticker.upper()} (short) @ ${price_short:,.2f} x {shares_short:,.0f} sh "
          f"= ${short_notional:,.2f}"),
        # "",
        # e(f"Leverage: {leverage:g}"),
        # e(f"Margin multiplier: {margin_mult:.3f} (long rate={long_rate:.2f}, short rate={short_rate:.2f})"),
        # e(f"Rates source: {rate_source}"),
        "",
        "*" + e("TARGET PARAMETERS") + "*",
        e(f"Leverage {leverage:g} * {short_ticker.upper()} ${short_notional:,.2f} = "),
        "*" + e(f"${twice_base:,.2f}\n") + "*",
        e("Net distance limit = "),
        e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
          f"short ${short_notional:,.2f}"),
        "*" + e(f"= ${net_delta:,.2f}") + "*",
        e(f"Position is {long_or_short}")
        ]
    if derived_from == "long":
        lines.append(
            e(f"base_capital not given -- derived as ${base_capital:,.2f} from "
              f"the long leg (${long_notional:,.2f} invested, assuming the book "
              "is balanced at target."),
        )
        lines.append("")
    elif derived_from == "short":
        lines.append(
            e(f"base_capital not given -- derived as ${base_capital:,.2f} from "
              f"the short leg (${short_notional:,.2f} held, treated as sitting "
              "exactly on target."),
        )
        lines.append("")
        
    long_short_band, foil_decay_band, source = _bands_for(short_ticker)    
        
    # signed_foil = (short_notional - target) / target / FOIL_DECAY_BAND
    signed_ls = net_delta / (leverage_in * target) / long_short_band
    
    ls_direction = "long ➡️🟢" if signed_ls > 0 else "short ⬅️🔴"
     
    lines += [
        # e(f"target (short) = base_capital ${base_capital:,.2f} x "),
        # e(f"capital_utilization {config.DEFAULT_CAPITAL_UTILIZATION:.0%} / "),
        # e(f"margin_multiplier {margin_mult:.3f}"),
        # e(f"= {target:,.2f}"),
        # "",
        # e("Net distance limit = "),
        #  e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
        #   f"short ${short_notional:,.2f}"),
        # e(f"= ${net_delta:,.2f}"),
        "",
        # e(f"bands: long_short={LONG_SHORT_BAND:.2%}  "
        #  f"FOIL_decay={FOIL_DECAY_BAND:.2%}"),
        # e(f"FOIL decay: {_band_bar(signed_foil)}"),
        # e(f"{abs(short_notional - target) / target:.1%} off target."),
        # e(f"{signed_foil:.1%} of a {config.DEFAULT_FOIL_DECAY_BAND:.0%} band."),
        # e(f"Long-short: {_band_bar(signed_ls)}"),
        e(f"Long-short band: {abs(net_delta) / (leverage_in * target):.1%} off target."),
        e(f"Direction is {ls_direction}."),
        # e(f"{signed_ls:.1%} of a {config.DEFAULT_LONG_SHORT_BAND:.0%} band."),
        "",
        "*" + e("ACTION TO TAKE") + "*",
        e(f"Trip limits: long_short = {long_short_band:.2%}  "
          f"FOIL_decay = {foil_decay_band:.2%}"),
        "",
    ]
 
    if abs(short_notional - target) > foil_decay_band * target:
        new_short_shares = round(target / price_short)
        new_long_shares = round((leverage * target) / price_long)
        
        new_target_short = short_notional
        new_long_shares_alt = round((leverage * new_target_short) / price_long)
        
        new_target_long = long_notional / leverage
        new_short_shares_alt = round(new_target_long / price_short)
    
        lines.append(e(
            f"TRIP: FOIL decay band -- short notional is "
            f"{abs(short_notional - target) / target:.1%} off target.\n"
            f"  Option A: Reset both legs to target:\n"
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares:,d} sh\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh\n"
            f" Option B: Short leg unchanged. Reset target to match and resize long leg only:\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares_alt:,d} sh\n"
            f"    Target resized to match short leg: ${target:,.0f} -> ${new_target_short:,.0f}\n"
            f" Option C: Long leg unchanged. Reset target to match and resize short leg only:\n" 
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares_alt:,d} sh\n"
            f"    Target resized to match long leg: ${target:,.0f} -> ${new_target_long:,.0f}"
        ))
    elif abs(net_delta) > long_short_band * leverage * target:
        new_long_shares = round((leverage * short_notional) / price_long)
        new_short_shares_alt = round(long_notional / (leverage * price_short))
        lines.append(e(
            f"TRIP: long-short band -- net delta is "
            f"{abs(net_delta) / (pair['leverage'] * target):.1%} off target.\n"
            f"  Option A: Short leg unchanged. Resize long leg only:\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh\n"
            f"  Option B: Long leg unchanged. Resize short leg only:\n"
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares_alt:,d} sh"
        ))
    else:
        lines.append(e("No trip -- current shares are both within bands."))
    
    """
    lines.append(e(
        "\n(Only foil-decay and long-short are checked here. Drawdown stop and "
        "margin de-risk both need a live position's persisted peak_equity / "
        "actual maintenance margin, which this what-if calculator has no reason to hold.)"
    ))"""
 
    return "\n".join(lines)

def _pair_reading(ib, pair_key, entry):
    """
    Price on both legs and computes current fractions. Or None if no prices or
    nothing set for pair, or if pair paused (both sides set to 0).
    
    Target rederived every time.
    """
    shares_short = entry.get("shares_short")
    shares_long = entry.get("shares_long")
    base_capital = entry.get("base_capital")
    if shares_short is None or shares_long is None or base_capital is None:
        return None
    if shares_short == 0 or shares_long == 0:
        return None # deliberately "paused" at 0 shares
    
    pair = config.PAIRS[pair_key]
    price_short = _price(ib, pair["leveraged_ticker"])
    price_long = _price(ib, pair["underlying_ticker"])
    if price_short is None or price_long is None:
        log.warning("%s: no live price for %s -- skipping this cycle",
                    pair_key, pair["leveraged_ticker"] if price_short is None
                    else pair["underlying_ticker"])
        return None
    
    margin_mult = config.margin_multiplier(pair)
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    net_delta = long_notional - pair["leverage"] * short_notional
    
    return {
        "target": target,
        "foil_frac": abs(short_notional - target) / target,
        "long_short_frac": abs(net_delta) / (pair["leverage"] * target)
    }

def _alert_level(frac, band):
    """
    None, near, or trip for fractions against band width
    """
    if band <= 0:
        return None
    if frac >= band:
        return "trip"
    if frac >= NEARING_BAND_FRACTION * band:
        return "near"
    return None

def _maybe_alert(pair_key, label, entry, state_key, new_level, frac, band, send):
    """
    Parameters
    ----------
    pair_key : the leveraged ticker for pair
    label : the type of trip
    entry : entry in watch_state cache
    state_key : the last entry for given pair
    new_level : the last kind of alert
    frac : the actual current frac for this pair
    band : the parameter for bands where trip happens
    send : function to send

    Returns
    -------
    Send only transition. Level is uncahgned since last cycle means chat already
    notified. Trip just cleared gets 'resolved' line and not just quiet.

    """
    old_level = entry.get(state_key)
    if new_level == old_level:
        return
    
    e = notify.escape_md_v2
    
    if new_level == "trip":
        send(e(f"TRIP ({label}) -- {pair_key}: {frac:.1%} of a {band:.2%} band."))
    elif new_level == "near":
        send(e(f"Nearing ({label}) -- {pair_key}: {frac:.1%} of a {band:.2%} band."))
    elif old_level is not None:
        send(e(f"Resolved ({label}) -- {pair_key}: back inside band ({frac:.2%})."))
        
    entry[state_key] = new_level
    entry["last_alert_ts"] = datetime.datetime.now(ET).isoformat()
    
def _check_pair(ib, pair_key, state, send):
    entry = watch_state.pair_entry(state, pair_key)
    reading = _pair_reading(ib, pair_key, entry)
    if reading is None:
        return
    
    pair = config.PAIRS[pair_key]
    long_short_band, foil_decay_band, source = _bands_for(pair["leveraged_ticker"])    
    
    foil_level = _alert_level(reading["foil_frac"], foil_decay_band)
    ls_level = _alert_level(reading["long_short_frac"], long_short_band)
    
    _maybe_alert(pair_key, "FOIL", entry, "last_alert_foil", foil_level, 
                 reading["foil_frac"], foil_decay_band, send)
    _maybe_alert(pair_key, "long-short", entry, "last_alert_long_short", ls_level, 
                 reading["long_short_frac"], long_short_band, send)
        
def _heartbeat_due(state, key, now_et, hour, minute):
    today = now_et.date().isoformat()
    if state.get(key) == today:
        return False
    if (now_et.hour, now_et.minute) < (hour, minute):
        return False
    return True

def _tracked_summary(state):
    lines = []
    for k, e in state["pairs"].items():
        ss, sl =  e.get("shares_short"), e.get("shares_long")
        if ss is None or sl is None:
            continue
        if ss == 0 and sl == 0:
            lines.append(f" {k}: paused")
        else:
            lines.append(f" {k}: short {ss:,.0f} / long {sl:,.0f}")
    return "\n".join(lines) if lines else  " (no pairs have shares set)"

def _run_heartbeat_if_due(state, send):
    now = datetime.datetime.now(ET)
    summary = _tracked_summary(state)
    
    e = notify.escape_md_v2
    
    if _heartbeat_due(state, "last_morning_date", now, MORNING_HOUR, MORNING_MINUTE):
        send(e(f"Morning check-in: alive.\nTracking:\n{summary}"))
        state["last_morning_date"] = now.date().isoformat()
    if _heartbeat_due(state, "last_eod_date", now, EOD_HOUR, EOD_MINUTE):
        send(e(f"End-of-day check-in: alive.\nTracking:\n{summary}"))
        state["last_eod_date"] = now.date().isoformat()
        
def _handle_setshares(ib, args, state):
    if len(args) != 3:
        return ("Usage: /setshares PAIR_KEY SHARES_SHORT SHARES_LONG\n"
                "Example: /setshares TSLT 100 250\n"
                "Base capital is back-solved from current price, assuming short"
                "leg is where you want it now.")
    
    pair_key_raw, shares_short_s, shares_long_s = args
    pair_key_raw = pair_key_raw.upper()
    pair_key = _resolve_pair_key(pair_key_raw)
    # if pair_key not in config.PAIRS:
    if pair_key is None:
        return f"Unknown pair {pair_key}.upper(). Configured pairs (by underlying): {', '.join(config.PAIRS)}"
    try:
        shares_short = float(shares_short_s)
        shares_long = float(shares_long_s)
    except ValueError:
        return "Shares must be numbers."
    if shares_short <= 0 or shares_long <= 0:
        return "Both share counts should be entered as positive values."
    
    pair = config.PAIRS[pair_key]
    price_short = _price(ib, pair["leveraged_ticker"])
    price_long = _price(ib, pair["underlying_ticker"])
    if price_short is None or price_long is None:
        missing = pair["leveraged_ticker"] if price_short is None else pair["underlying_ticker"]
        return f"No live price for {missing} right now -- try again in a moment."
    
    
    margin_mult = config.margin_multiplier(pair)
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    base_capital = short_notional * margin_mult / config.DEFAULT_CAPITAL_UTILIZATION
    target = short_notional
    net_delta = long_notional - pair["leverage"] * short_notional
    long_short_frac = abs(net_delta) / (pair["leverage"] * target)
    
    entry = watch_state.pair_entry(state, pair_key)
    entry["shares_short"] = shares_short
    entry["shares_long"] = shares_long
    entry["base_capital"] = base_capital
    
    long_short_band, foil_decay_band, source = _bands_for(pair["leveraged_ticker"])    
    
    entry["last_alert_foil"] = None
    entry["last_alert_long_short"] = _alert_level(long_short_frac, long_short_band)
    
    lines = [
        f"{pair_key}: short {shares_short:,.0f} @ ${price_short:,.2f}, "
        f"long {shares_long:,.0f} @ ${price_long:,.2f}",
        f"Back-solved base_capital = ${base_capital:,.2f} (target = ${target:,.2f})",
    ]
    if entry["last_alert_long_short"]:
        lines.append(
            f"Note: long-short is already at {long_short_frac:.1%} of its "
            f"{long_short_band:.2%} band with these numbers -- "
            f"not flagged as new since you just set it."
        )
        
    signed_foil = (short_notional - target) / target / foil_decay_band
    signed_ls = net_delta / (pair["leverage"] * target) / long_short_band
    
    ls_direction = "long ➡️🟢" if signed_ls > 0 else "short ⬅️🔴"
    foil_direction = "long ➡️🟢" if signed_foil > 0 else "short ⬅️🔴"
    
    # lines.append(f"Long-short: {_band_bar(signed_ls)}")
    lines.append(f"Long-short band: {abs(net_delta) / (pair['leverage'] * target):.1%} off target.")
    lines.append(f"Direction is {ls_direction}.")
    # lines.append(f"{signed_ls:.1%} of a {LONG_SHORT_BAND:.2%} band.")
    # lines.append(f"FOIL decay: {_band_bar(signed_foil)}")
    lines.append(f"FOIL decay band: {abs(short_notional - target) / target:.1%} off target.")
    lines.append(f"Direction is {foil_direction}.")
    # lines.append(f"{signed_foil:.1%} of a {FOIL_DECAY_BAND:20%} band.")
    
    return "\n".join(lines)

def _handle_resize(ib, args, state):
    if len(args) != 3:
        return ("Usage: /resize PAIR_KEY SHARES_SHORT SHARES_LONG\n"
                "Changes share counts without moving target. Enter total new "
                "share count, not just the number of added/subtracted shares. "
                "0 0 pauses the pair but keeps the target."
                "Use /setshares instead if you want to restart position, or"
                "/untrack to forget it.")
    
    pair_key_raw, shares_short_s, shares_long_s = args
    pair_key_raw = pair_key_raw.upper()
    pair_key = _resolve_pair_key(pair_key_raw)
    # if pair_key not in config.PAIRS:
    if pair_key is None:
        return f"Unknown pair {pair_key}.upper(). Configured pairs (by underlying): {', '.join(config.PAIRS)}"
    
    entry = watch_state.pair_entry(state, pair_key)
    base_capital = entry.get("base_capital")
    if not base_capital:
        return f"{pair_key} has no target set yet -- use /setshares first."
    
    try:
        shares_short = float(shares_short_s)
        shares_long = float(shares_long_s)
    except ValueError:
        return "Shares must be numbers."
    if shares_short < 0 or shares_long < 0:
        return "Both share counts should be entered as non-negative values."
    
    pair = config.PAIRS[pair_key]
    margin_mult = config.margin_multiplier(pair)
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    
    if shares_short == 0 and shares_long == 0:
        entry["shares_short"] = 0
        entry["shares_long"] = 0
        entry["last_alert_foil"] = None
        entry["last_alert_long_short"] = None
        return (f"{pair_key}: paused. Target kept at ${target:,.2f} -- "
                f"/resize back to real numbers to resume, or /untrack to "
                f"forget this pair entirely.")
    
    price_short = _price(ib, pair["leveraged_ticker"])
    price_long = _price(ib, pair["underlying_ticker"])
    if price_short is None or price_long is None:
        missing = pair["leveraged_ticker"] if price_short is None else pair["underlying_ticker"]
        return f"No live price for {missing} right now -- try again in a moment."
    
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    net_delta = long_notional - pair["leverage"] * short_notional
    foil_frac = abs(short_notional - target) / target
    long_short_frac = abs(net_delta) / (pair["leverage"] * target)
    
    entry["shares_short"] = shares_short
    entry["shares_long"] = shares_long
    
    long_short_band, foil_decay_band, source = _bands_for(pair["leveraged_ticker"])    
    
    entry["last_alert_foil"] = _alert_level(foil_frac, foil_decay_band)
    entry["last_alert_long_short"] = _alert_level(long_short_frac, long_short_band)
    
    lines = [
        f"{pair_key}: short {shares_short:,.0f} @ ${price_short:,.2f}, "
        f"long {shares_long:,.0f} @ ${price_long:,.2f}",
        f"Target unchanged: target = ${target:,.2f} (= ${base_capital:,.2f})",
        f"FOIL={foil_frac:.1%} of {foil_decay_band:.2%} band, "
        f"long-short={long_short_frac:.1%} of {long_short_band:.2%} band, "
    ]
    if entry["last_alert_foil"] or entry["last_alert_long_short"]:
        lines.append(
            "Note: Already inside a warn/trip range with these numbers -- "
            "not flagged as new new since you just set it."
        )
        
    signed_foil = (short_notional - target) / target / foil_decay_band
    signed_ls = net_delta / (pair["leverage"] * target) / long_short_band
    
    ls_direction = "long ➡️🟢" if signed_ls > 0 else "short ⬅️🔴"
    foil_direction = "long ➡️🟢" if signed_foil > 0 else "short ⬅️🔴"
    
    # lines.append(f"Long-short: {_band_bar(signed_ls)}")
    lines.append(f"Long-short band: {abs(net_delta) / (pair['leverage'] * target):.1%} off target.")
    lines.append(f"Direction is {ls_direction}.")
    # lines.append(f"{signed_ls:.1%} of a {LONG_SHORT_BAND:.2%} band.")
    # lines.append(f"FOIL decay: {_band_bar(signed_foil)}")
    lines.append(f"FOIL decay band: {abs(short_notional - target) / target:.1%} off target.")
    lines.append(f"Direction is {foil_direction}.")
    # lines.append(f"{signed_foil:.1%} of a {FOIL_DECAY_BAND:.2%} band.")
    
    return "\n".join(lines)

def _handle_rescale(ib, args, state):
    if len(args) != 3:
        return ("Usage: /rescale PAIR_KEY NEW_SHARES_SHORT NEW_SHARES_LONG\n"
                "For growing/shrinking the whole position on purpose -- scales "
                "base by same ratio as short leg change, so % off target carries "
                "through instead of exploding or resetting to 0."
                "Use /resize to correct toward existing target or /setshares to "
                "recenter the target at a new position.")
    
    pair_key_raw, shares_short_s, shares_long_s = args
    pair_key_raw = pair_key_raw.upper()
    pair_key = _resolve_pair_key(pair_key_raw)
    # if pair_key not in config.PAIRS:
    if pair_key is None:
        return f"Unknown pair {pair_key}.upper(). Configured pairs (by underlying): {', '.join(config.PAIRS)}"
    
    entry = watch_state.pair_entry(state, pair_key)
    old_short = entry.get("shares_short")
    old_long = entry.get("shares_long")
    base_capital = entry.get("base_capital")
    if old_short is None or base_capital is None:
        return f"{pair_key} has no position set yet -- use /setshares first."
    if old_short == 0:
        return (f"{pair_key} is paused at 0 shares -- use /resize to add real "
                "numbers first, or /setshares to start fresh.")
    
    try:
        new_short = float(shares_short_s)
        new_long = float(shares_long_s)
    except ValueError:
        return "Shares must be numbers."
    if new_short <= 0 or new_long <= 0:
        return ("Both share counts should be entered as positive values. " 
                "Use /resize 0 0 to pause instead.")
    
    pair = config.PAIRS[pair_key]
    price_short = _price(ib, pair["leveraged_ticker"])
    price_long = _price(ib, pair["underlying_ticker"])
    if price_short is None or price_long is None:
        missing = []
        if price_short is None:
            missing.append(pair["leveraged_ticker"].upper())
        if price_long is None:
            missing.append(pair["underlying_ticker"].upper())
        return (f"Could not get a live price for: {', '.join(missing)}. " 
                "Check the symbol(s) and try again.")
    scale_factor = new_short / old_short
    new_base_capital = base_capital * scale_factor
    
    margin_mult = config.margin_multiplier(pair)
    old_target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    new_target = (new_base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    
    short_notional = new_short * price_short
    long_notional = new_long * price_long
    net_delta = long_notional - pair["leverage"] * short_notional
    
    foil_frac = abs(short_notional - new_target) / new_target
    long_short_frac = abs(net_delta) / (pair["leverage"] * new_target)
    
    entry["shares_short"] = new_short
    entry["shares_long"] = new_long
    entry["base_capital"] = new_base_capital
    
    long_short_band, foil_decay_band, source = _bands_for(pair["leveraged_ticker"])    
    
    entry["last_alert_foil"] = _alert_level(foil_frac, foil_decay_band)
    entry["last_alert_long_short"] = _alert_level(long_short_frac, long_short_band)
    
    lines = [f"{pair_key}: scaled {scale_factor:.3f}x (short leg {old_short:,.0f} --> {new_short:,.0f})",
             f"Target scaled to match: ${old_target:,.2f} --> ${new_target:,.2f} "
             f"Base capital: ${base_capital:,.2f} --> ${new_base_capital:,.2f}",
             f"Long-short = {abs(net_delta) / (pair['leverage'] * new_target):.1%} off target band\n"
             f"FOIL decay = {abs(short_notional - new_target) / new_target:.1%} off target."]
    
    if old_long:
        long_scale_factor = new_long / old_long
        if abs(long_scale_factor - scale_factor) > 0.02:
            lines.append(
                f"Note: long leg scaled {long_scale_factor:.3f}x vs. short leg "
                f"{scale_factor:.3f}x -- these differ, so this also shifted the "
                f"long-short balance, not just overall size."
            )
    
    return "\n".join(lines)
    

def _handle_untrack(args, state):
    if len(args) != 1:
        return "Usage: /untrack PAIR_KEY\nExample: /untrack QQQ"
    pair_key_raw = args[0].upper()
    
    pair_key = _resolve_pair_key(pair_key_raw)
    # if pair_key not in config.PAIRS:
    if pair_key is None:
        return f"Unknown pair {pair_key}.upper(). Configured pairs (by underlying): {', '.join(config.PAIRS)}"
    entry = state["pairs"].pop(pair_key, None)
    if entry is None:
        return f"{pair_key} wasn't being tracked."
    was = (f"was short {entry.get('shared_short', 0):,.0f} / "
         f"long {entry.get('shares_long', 0):,.0f}")
    return f"{pair_key}: stopped tracking({was}). "
        
def _handle_listshares(state):
    if not state["pairs"]:
        return "No pairs have shares set. Use /setshares to add one."\
            
    lines = []
    orphaned = []
    for k, e in state["pairs"].items():
        ss, sl, bc =  e.get("shares_short"), e.get("shares_long"),  e.get("base_capital")
        if ss is None or sl is None or bc is None:
            continue
        
        pair = config.PAIRS[k]
        if pair is None:
            orphaned.append(k)
            continue
        target = (bc * config.DEFAULT_CAPITAL_UTILIZATION / config.margin_multiplier(pair))
        if ss == 0 and sl == 0:
            lines.append(f"{k} / {pair['leveraged_ticker']}: paused (target ${target:,.2f})")
        else:
            lines.append(f"{k} / {pair['leveraged_ticker']}: short {ss:,.0f} / long {sl:,.0f} (target ${target:,.2f})")
            
    if orphaned:
        log.warning("listshares: %s match no config.PAIRS entry -- stale key? "
                    "consider /untrack or a stale migration.", ", ".join(orphaned))
        lines.append(f"\n {', '.join(orphaned)} tracked in state but not in config.PAIRS "
                     f"(stale key -- use /untrack or fix state file).")
    return "\n".join(lines) if lines else  "No pairs have shares set."
    
def _handle_shares_report(ib, args, state):
    if not state["pairs"]:
        return "No pairs have shares set. Use /setshares to add one."
    lines = []
    orphaned = []
    for k, e in state["pairs"].items():
        if e.get("shares_short") and e.get("shares_long") and e.get("base_capital"):
            pair = config.PAIRS[k]
            if pair is None:
                orphaned.append(k)
                continue
            target = (e["base_capital"] * config.DEFAULT_CAPITAL_UTILIZATION
                      / config.margin_multiplier(pair))
            
            price_short = _price(ib, pair["leveraged_ticker"])
            price_long = _price(ib, pair["underlying_ticker"])
            if price_short is None or price_long is None:
                lines.append(f"{k}: target ${target:,.2f} (no live price right now)")
                continue
            
            short_notional = e["shares_short"] * price_short
            long_notional = e["shares_long"] * price_long
            net_delta = long_notional - pair["leverage"] * short_notional
            
            long_short_band, foil_decay_band, source = _bands_for(pair["leveraged_ticker"])    
            
            signed_foil = (short_notional - target) / target / foil_decay_band
            signed_ls = net_delta / (pair["leverage"] * target) / long_short_band
            
            lines.append(
                f"{k} / {pair['leveraged_ticker']}:\n"
                f"short {e['shares_short']:,.0f} / long {e['shares_long']:,.0f} "
                f"(target ${target:,.2f})"
            )
            
            ls_direction = "long ➡️🟢" if signed_ls > 0 else "short ⬅️🔴"
            foil_direction = "long ➡️🟢" if signed_foil > 0 else "short ⬅️🔴"
            
            # lines.append(f"Long-short: {_band_bar(signed_ls)}")
            lines.append(f"\nLong-short band: {abs(net_delta) / (pair['leverage'] * target):.1%} off target "
                         f"(trip at {long_short_band:.2%}).")
            lines.append(f"Direction is {ls_direction}.")
            # lines.append(f"{signed_ls:.1%} of a {LONG_SHORT_BAND:.2%} band.")
            # lines.append(f"FOIL decay: {_band_bar(signed_foil)}")
            lines.append(f"FOIL decay band: {abs(short_notional - target) / target:.1%} off target "
                         f"(trip at {foil_decay_band:.2%}).")
            lines.append(f"Direction is {foil_direction}.\n")
            # lines.append(f"{signed_foil:.1%} of a {FOIL_DECAY_BAND:.2%} band.")
    
    if orphaned:
        log.warning("shares report: %s match no config.PAIRS entry -- stale key? "
                    "consider /untrack or a stale migration.", ", ".join(orphaned))
        lines.append(f"\n {', '.join(orphaned)} tracked in state but not in config.PAIRS "
                     f"(stale key -- use /untrack or fix state file).")
    
    return "\n".join(lines) if lines else "No pairs have shares set."

def _band_bar(signed_frac, n_cells=6, defining_band=0.75):
    """
    2*n_cells emoji moji gauge of a signed value/band ratio.
    signed_frac: value / band_threshold, signed. +-1 = trip line.
    Colors from alert_level, with band normalized to 1.
    trip -> orange, near --> yellow, safe --> geen or blue
    Negative means drifted short/under, while positive is long/over.
    """
    step = defining_band / 3.0
    
    half = []
    
    for c in range(n_cells):
        cell_val = (c + 0.5) * step
        
        if cell_val <= step:
            half.append("🟩")
        elif cell_val <= step * 2.0:
            half.append("🟦")
        elif cell_val <= step * 3.0:
            half.append("🟨")
        else:
            half.append("🟧")
            
    bar = half[::-1] + half
    
    separator = "┃"  
    #bar.insert(3, separator)
    #bar.insert(5, separator)
    #bar.insert(10, separator)
    #bar.insert(12, separator)
    
    bar.insert(9, separator)
    bar.insert(8, separator)
    bar.insert(4, separator)
    bar.insert(3, separator)
    
    max_val = n_cells * step
    clamped = max(-max_val, min(max_val, signed_frac))
    
    idx_mapped = round((clamped + max_val) / (max_val * 2) * (len(bar) - 1))
    
    if bar[idx_mapped] == separator:
        idx_mapped = idx_mapped + 1 if signed_frac >= 0 else idx_mapped - 1
        
    bar[idx_mapped] = "✴️" if abs(signed_frac) >= 1.0 else "✳️"
    
    return "".join(bar)
    
    
def connect_with_backoff(backoff=RECONNECT_BACKOFF_START):
    """
    Connect or reconnect to IB Gateway, and continuously retry.
    """
    while True:
        try:
            ib = IB()
            ib.connect(IB_HOST, IB_PORT, clientId=CALC_CLIENT_ID, readonly=True)
            # other bot runs off assumption of held position
            # if not holding position, can only get 15 min delayed data
            # mismatch and worth noting for both: to stay accurate here, and if any
            # new positions there
            ib.reqMarketDataType(3)
            log.info(
                "connected to IB Gateway for price lookups: host=%s port=%s clientId=%s",
                IB_HOST, IB_PORT, CALC_CLIENT_ID,
            )
            return ib
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.warning("IB connect failed: %s: %s -- retrying in %ds",
                  type(e).__name__, e, backoff)      
            log.debug("connect traceback", exc_info=True)
            IB.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
 
# ---------------------------------------------------------------------------
# Telegram  -- same shape as bot.py's get_updates/run for comparison
 
def get_updates(token, offset):
    """One long poll. Raises on transport or API failure; run() owns backoff."""
    import requests
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"timeout": LONG_POLL_SECONDS, "offset": offset + 1},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    body = resp.json()
    if resp.status_code != 200 or not body.get("ok"):
        raise RuntimeError(f"getUpdates HTTP {resp.status_code}: {resp.text[:200]}")
    return body.get("result", [])

_NOT_REPEATABLE = {"/previous"}

def _dispatch_command(ib, command, args, state):
    if command == "/calc":
        return  build_calc_reply(ib, args, state)
    elif command == "/setshares":
        reply = _handle_setshares(ib, args, state)
        return notify.escape_md_v2(reply)
    elif command == "/resize":
        reply = _handle_resize(ib, args, state)
        return notify.escape_md_v2(reply)
    elif command == "/shares":
        reply = _handle_shares_report(ib, args, state)
        return notify.escape_md_v2(reply)
    elif command == "/listshares":
        reply = _handle_listshares(state)
        return notify.escape_md_v2(reply)
    elif command == "/untrack":
        reply = _handle_untrack(args, state)
        return notify.escape_md_v2(reply)    
    elif command == "/calcfull":
        return build_calcfull_reply(ib, args, state)
    elif command == "/calcaction":
        return build_calcaction_reply(ib, args, state)
    elif command == "/rescale":
        reply = _handle_rescale(ib, args, state)
        return notify.escape_md_v2(reply)    
    # else:
        # log.info("Unknown command %s from chat %s", command, chat_id)
        # return # unknown command
    return None
 
def handle_message(ib, message, token, configured_chat_id, state): # todo
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return
 
    chat_id = message.get("chat", {}).get("id")
    if str(chat_id) != str(configured_chat_id):
        # Same silence-on-mismatch policy as bot.py: an error reply would
        # confirm to a stranger that the bot is live and their message
        # arrived. Log it -- a stream of these is the signal worth watching.
        # configured_chat_id here is TELEGRAM_BASIC_CHAT_ID -- deliberately
        # its own chat, separate from the monitor's TELEGRAM_CHAT_ID alert
        # channel, so /calc traffic never mixes with live-position alerts.
        log.info("unauthorized command from chat %s -- ignoring", chat_id)
        return
    
    command = text.split()[0].split("@")[0].lower()
    args = text.split()[1:]
    
    if command == "/previous":
        previous = state.get("last_command")
        if not previous:
            reply = notify.escape_md_v2("No previous command to repeat yet.")
            delivered, error, _ = notify.send_text(token, configured_chat_id, reply)    
            if not delivered:
                log.warning("reply to %s failed: %s", command, error)
            return
        command = previous["command"]
        args = previous["args"]
        log.info("/previous replaying: %s %s", command, " ".join(args))
    
    reply = _dispatch_command(ib, command, args, state)
    if reply is None:
        log.info("Unknown command %s from chat %s", command, chat_id)
        return # unknown command
    
    if command not in _NOT_REPEATABLE:
        state["last_command"] = {"command": command, "args": args}
            
    delivered, error, _ = notify.send_text(token, configured_chat_id, reply)    
    if not delivered:
        log.warning("reply to %s failed: %s", command, error)
 
    
def run():
    token = os.environ["TELEGRAM_BASIC_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_BASIC_CHAT_ID"]
 
    # No persisted offset: this bot is stateless by design (no
    # StateDirectory=, see the unit file). Starting at 0 means a restart can
    # re-answer at most one in-flight /calc
    offset = 0
    backoff = BACKOFF_START
    ib_backoff = RECONNECT_BACKOFF_START
    state = watch_state.load(watch_state.state_path())
    last_watch_check = 0.0
    
    ib = connect_with_backoff()
 
    log.info("basic_bot: chat_id=%s (TELEGRAM_BASIC_CHAT_ID) clientId=%s",
              chat_id, CALC_CLIENT_ID)
    
    def send(text):
        delivered, error, _ = notify.send_text(token, chat_id, text)
        if not delivered:
            log.warning("watched alert send failed: %s", error)
    
    try:
        while True:
            if not ib.isConnected():
                log.warning("IB not connected; reconnecting")
                ib = connect_with_backoff(ib_backoff)
                ib_backoff = RECONNECT_BACKOFF_START
                
            
            try:
                updates = get_updates(token, offset)
            except KeyboardInterrupt:
                log.info("interrupted; shutting down")
                return
            except Exception as exc:
                log.warning("getUpdates failed: %s: %s -- retrying in %ds",
                            type(exc).__name__, exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
 
            backoff = BACKOFF_START
            for update in updates:
                try:
                    if "message" in update:
                        handle_message(ib, update["message"], token, chat_id, state)
                except DISCONNECT_ERRORS as e:
                    log.warning("IB disconnnected mid-update: %s: %s",
                                type(e).__name__, e)
                except Exception:
                    log.exception("update %s failed; skipping", update.get("update_id"))
                offset = update.get("update_id", offset)
                
            now_monotonic = time.monotonic()
            if now_monotonic - last_watch_check >= WATCH_POLL_SECONDS:
                for pair_key in config.PAIRS:
                    try:
                        _check_pair(ib, pair_key, state, send)
                    except DISCONNECT_ERRORS as e:
                        log.warning("Watch check for %s hit a disconect: %s: %s",
                                    pair_key, type(e).__name__, e)
                    except Exception:
                        log.exception("Watch check for %s failed; skipping this cycle",
                                      pair_key)
                try:
                    _run_heartbeat_if_due(state, send)
                except Exception:
                    log.exception("Heartbeat check failed; skipping this cycle.")
                last_watch_check = now_monotonic
                
            watch_state.save(watch_state.state_path(), state)
    finally:
        if ib.isConnected():
            ib.disconnect()
 
 
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    run()
 
 
if __name__ == "__main__":
    main()
