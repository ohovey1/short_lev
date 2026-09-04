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
    /calc TSLL TSLA 2 100 250 10000
"""

import logging
import os
import time
 
from dotenv import load_dotenv
from ib_async import IB, Stock
 
import config
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

WATCH_POLL_SECONDS = float(os.environ.get("WATCH_POLL_SECONDS", 1800)) # todo
NEARING_BAND_FRACTION = 0.8

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

 
def _rates_for(short_ticker, leverage):
    pair = config.PAIRS.get(short_ticker.upper())
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
    
        while (price is None or price != price or price == 0) and waited < _SNAPSHOT_WAIT_SECONDS:  # NaN check
            ib.sleep(_SNAPSHOT_POLL_SECONDS)
            waited += _SNAPSHOT_POLL_SECONDS
            price = ticker_obj.marketPrice()
    finally: 
        ib.cancelMktData(qualified)
            
    if price is None or price != price or price == 0:
        return None
    return price

USAGE = (
    "Usage: /calc SHORT_TICKER LONG_TICKER LEVERAGE SHARES_SHORT SHARES_LONG [BASE_CAPITAL]\n"
    "Example (from existing long position): /calc TSLL TSLA 2 100 250\n"
    "Example (from existing short position): /calc TSLL TSLA 2 100 0\n"
    "Example (if no long position yet): /calc TSLL TSLA 2 0 0 10000\n"
    "BASE_CAPITAL is required when both share counts are 0 - -- otherwise derived from"
    "whichever leg is currently held."
)

def build_calc_reply(ib, args):
    """Args in, reply text out. Pure given the ib price lookups."""
    e = notify.escape_md_v2
    
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
    net_delta = long_notional - leverage * short_notional
 
    lines = [
        "*" + e(f"CURRENT PRICES FOR {short_ticker.upper()} & {long_ticker.upper()}") + "*",
        e(f"{short_ticker.upper()} (short) @ ${price_short:,.2f} x {shares_short:,.0f} sh "
          f"= ${short_notional:,.2f}"),
        e(f"{long_ticker.upper()} (long)  @ ${price_long:,.2f} x {shares_long:,.0f} sh "
          f"= ${long_notional:,.2f}"),
        "",
        e(f"Leverage: {leverage:g}"),
        e(f"Margin multiplier: {margin_mult:.3f} (long rate={long_rate:.2f}, short rate={short_rate:.2f})"),
        e(f"Rates source: {rate_source}"),
        "",
        "*" + e("BAND TRIP PARAMETERS") + "*",
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
        
    signed_foil = (short_notional - target) / target / config.DEFAULT_FOIL_DECAY_BAND
    signed_ls = net_delta / target / config.DEFAULT_LONG_SHORT_BAND
     
    lines += [
        e(f"target (short) = base_capital ${base_capital:,.2f} x "),
        e(f"capital_utilization {config.DEFAULT_CAPITAL_UTILIZATION:.0%} / "),
        e(f"margin_multiplier {margin_mult:.3f}"),
        e(f"= {target:,.2f}"),
        "",
        e("Net distance limit = "),
        e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
          f"short ${short_notional:,.2f}"),
        e(f"= ${net_delta:,.2f}"),
        "",
        e(f"bands (config.py defaults): long_short={config.DEFAULT_LONG_SHORT_BAND:.0%}  "
          f"foil_decay={config.DEFAULT_FOIL_DECAY_BAND:.0%}"),
        e(f"Foil decay: {_band_bar(signed_foil)}"),
        e(f"{signed_foil:.1%} of a {config.DEFAULT_FOIL_DECAY_BAND:.0%} band."),
        e(f"Long-short: {_band_bar(signed_ls)}"),
        e(f"{signed_ls:.1%} of a {config.DEFAULT_LONG_SHORT_BAND:.0%} band."),
        "",
        "*" + e("ACTION TO TAKE") + "*",
    ]
 
    if abs(short_notional - target) > config.DEFAULT_FOIL_DECAY_BAND * target:
        new_short_shares = round(target / price_short)
        new_long_shares = round((leverage * target) / price_long)
        lines.append(e(
            f"TRIP: foil decay band -- short notional is "
            f"{abs(short_notional - target) / target:.1%} off target.\n"
            f"  Reset both legs to target:\n"
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares:,d} sh\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh"
        ))
    elif abs(net_delta) > config.DEFAULT_LONG_SHORT_BAND * target:
        new_long_shares = round((leverage * short_notional) / price_long)
        new_short_shares_alt = round(long_notional / (leverage * price_short))
        lines.append(e(
            f"TRIP: long-short band -- net delta is "
            f"{abs(net_delta) / target:.1%} of target.\n"
            f"  Option A: Short leg unchanged. Resize long leg only:\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh\n"
            f"  Option B: Long leg unchanged. Resize short leg only:\n"
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares_alt:,d} sh"
        ))
    else:
        lines.append(e("No trip -- current shares are both within bands."))
 
    lines.append(e(
        "\n(Only foil-decay and long-short are checked here. Drawdown stop and "
        "margin de-risk both need a live position's persisted peak_equity / "
        "actual maintenance margin, which this what-if calculator has no reason to hold.)"
    ))
 
    return "\n".join(lines)

def _pair_reading(ib, pair_key, entry):
    """
    Price on both legs and computes current fractions. Or None if no prices or
    nothing set for pair.
    
    Target rederived every time.
    """
    shares_short = entry.get("shares_short")
    shares_long = entry.get("shares_long")
    base_capital = entry.get("base_capital")
    if not shares_short or not shares_long or not base_capital:
        return None
    
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
        "long_short_frac": abs(net_delta) / target
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
    
    if new_level == "trip":
        send(f"TRIP ({label}) -- {pair_key}: {frac:.1%} of a {band:.0%} band.")
    elif new_level == "near":
        send(f"Nearing ({label}) -- {pair_key}: {frac:.1%} of a {band:.0%} band.")
    elif old_level is not None:
        send(f"Resolved ({label}) -- {pair_key}: back inside band ({frac:.1%}).")
        
    entry[state_key] = new_level
    entry["last_alert_ts"] = datetime.datetime.now(ET).isoformat()
    
def _check_pair(ib, pair_key, state, send):
    entry = watch_state.pair_entry(state, pair_key)
    reading = _pair_reading(ib, pair_key, entry)
    if reading is None:
        return
    
    foil_level = _alert_level(reading["foil_frac"], config.DEFAULT_FOIL_DECAY_BAND)
    ls_level = _alert_level(reading["long_short_frac"], config.DEFAULT_LONG_SHORT_BAND)
    
    _maybe_alert(pair_key, "foil", entry, "last_alert_foil", foil_level, 
                 reading["foil_frac"], config.DEFAULT_FOIL_DECAY_BAND, send)
    _maybe_alert(pair_key, "long-short", entry, "last_alert_long_short", ls_level, 
                 reading["long_short_frac"], config.DEFAULT_LONG_SHORT_BAND, send)
        
def _heartbeat_due(state, key, now_et, hour, minute):
    today = now_et.date().isoformat()
    if state.get(key) == today:
        return False
    if (now_et.hour, now_et.minute) < (hour, minute):
        return False
    return True

def _tracked_summary(state):
    tracked = [(k, e) for k, e in state["pairs"].items()
               if e.get("shares_short") and e.get("shares_long")]
    if not tracked:
        return " (no pairs have shares set)"
    return "\n".join(f" {k}: short {e['shares_short']:,.0f} / "
                     f" long {e['shares_long']:,.0f}" for k, e in tracked)

def _run_heartbeat_if_due(state, send):
    now = datetime.datetime.now(ET)
    summary = _tracked_summary(state)
    
    if _heartbeat_due(state, "last_morning_date", now, MORNING_HOUR, MORNING_MINUTE):
        send(f"Morning check-in: alive.\Tracking:\n{summary}")
        state["last_morning_date"] = now.date().isoformat()
    if _heartbeat_due(state, "last_eod_date", now, EOD_HOUR, EOD_MINUTE):
        send(f"End-od-day check-in: alive.\Tracking:\n{summary}")
        state["last_eod_date"] = now.date().isoformat()
        
def _handle_setshares(ib, args, state):
    if len(args) != 3:
        return ("Usage: /setshares PAIR_KEY SHARES_SHORT SHARES_LONG\n"
                "Example: /setshares TSLL 100 250\n"
                "Base capital is back-solved from current price, assuming short"
                "leg is where you want it now.")
    
    pair_key, shares_short_s, shares_long_s = args
    pair_key = pair_key.upper()
    if pair_key not in config.PAIRS:
        return f"Unknown pair {pair_key}. Configured pairs: {', '.join(config.PAIRS)}"
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
    long_short_frac = abs(net_delta) / target
    
    entry = watch_state.pair_entry(state, pair_key)
    entry["shares_short"] = shares_short
    entry["shares_long"] = shares_long
    entry["base_capital"] = base_capital
    
    entry["last_alert_foil"] = None
    entry["last_alert_long_short"] = _alert_level(long_short_frac, config.DEFAULT_LONG_SHORT_BAND)
    
    lines = [
        f"{pair_key}: short {shares_short:,.0f} @ ${price_short:,.2f}, "
        f"long {shares_long:,.0f} @ ${price_long:,.2f}",
        f"Back-solved base_capital = ${base_capital:,.2f} (target = ${target:,.2f})",
    ]
    if entry["last_alert_long_short"]:
        lines.append(
            f"Note: long-short is already at {long_short_frac:.1%} of its "
            f"{config.DEFAULT_LONG_SHORT_BAND:.0%} band with these numbers -- "
            f"not flagged as new since you just set it."
        )
        
    signed_foil = (short_notional - target) / target / config.DEFAULT_FOIL_DECAY_BAND
    signed_ls = net_delta / target / config.DEFAULT_LONG_SHORT_BAND
    
    lines.append(f"Foil decay: {_band_bar(signed_foil)}")
    lines.append(f"Foil decay: {_band_bar(signed_foil)}")
    lines.append(f"Long-short: {_band_bar(signed_ls)}")
    lines.append(f"Long-short: {_band_bar(signed_ls)}")
    
    return "\n".join(lines)

def _handle_resize(ib, args, state):
    if len(args) != 3:
        return ("Usage: /resize PAIR_KEY SHARES_SHORT SHARES_LONG\n"
                "Changes share counts without moving target. Enter total new"
                "share count, not just the number of added/subtracted shares."
                "Use /setshares instead if you want to restart position.")
    
    pair_key, shares_short_s, shares_long_s = args
    pair_key = pair_key.upper()
    if pair_key not in config.PAIRS:
        return f"Unknown pair {pair_key}. Configured pairs: {', '.join(config.PAIRS)}"
    
    entry = watch_state.pair_entry(state, pair_key)
    base_capital = entry.get("base_capital")
    if not base_capital:
        return f"{pair_key} has no target set yet -- use /setshares first."
    
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
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    net_delta = long_notional - pair["leverage"] * short_notional
    foil_frac = abs(short_notional - target) / target
    long_short_frac = abs(net_delta) / target
    
    entry["shares_short"] = shares_short
    entry["shares_long"] = shares_long
    
    entry["last_alert_foil"] = _alert_level(foil_frac, config.DEFAULT_FOIL_DECAY_BAND)
    entry["last_alert_long_short"] = _alert_level(long_short_frac, config.DEFAULT_LONG_SHORT_BAND)
    
    lines = [
        f"{pair_key}: short {shares_short:,.0f} @ ${price_short:,.2f}, "
        f"long {shares_long:,.0f} @ ${price_long:,.2f}",
        f"Target unchanged: target = ${target:,.2f} (= ${base_capital:,.2f})",
        f"foil={foil_frac:.1%} of {config.DEFAULT_FOIL_DECAY_BAND:.0%} band, "
        f"foil={long_short_frac:.1%} of {config.DEFAULT_LONG_SHORT_BAND:.0%} band, "
    ]
    if entry["last_alert_foil"] or entry["last_alert_long_short"]:
        lines.append(
            "Note: Already inside a warn/trip range with these numbers -- "
            "not flagged as new new since you just set it."
        )
        
    signed_foil = (short_notional - target) / target / config.DEFAULT_FOIL_DECAY_BAND
    signed_ls = net_delta / target / config.DEFAULT_LONG_SHORT_BAND
    
    lines.append(f"Foil decay: {_band_bar(signed_foil)}")
    lines.append(f"Foil decay: {_band_bar(signed_foil)}")
    lines.append(f"Long-short: {_band_bar(signed_ls)}")
    lines.append(f"Long-short: {_band_bar(signed_ls)}")
    
    return "\n".join(lines)

#todo
def _handle_listshares(state):
    if not state["pairs"]:
        return "No pairs have shares set. Use /setshares to add one."
    lines = []
    for k, e in state["pairs"].items():
        if e.get("shares_short") and e.get("shares_long") and e.get("base_capital"):
            pair = config.PAIRS[k]
            target = (e["base_capital"] * config.DEFAULT_CAPITAL_UTILIZATION
                      / config.margin_multiplier(pair))
            lines.append(
                f"{k}: short {e['shares_short']:,.0f} / long {e['shares_long']:,.0f} "
                f"(target ${target:,.2f})"
            )
    return "\n".join(lines) if lines else "No pairs have shares set."
    
def _handle_shares_report(ib, args, state):
    if not state["pairs"]:
        return "No pairs have shares set. Use /setshares to add one."
    lines = []
    for k, e in state["pairs"].items():
        if e.get("shares_short") and e.get("shares_long") and e.get("base_capital"):
            pair = config.PAIRS[k]
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
            
            signed_foil = (short_notional - target) / target / config.DEFAULT_FOIL_DECAY_BAND
            signed_ls = net_delta / target / config.DEFAULT_LONG_SHORT_BAND
            
            lines.append(
                f"{k}: short {e['shares_short']:,.0f} / long {e['shares_long']:,.0f} "
                f"(target ${target:,.2f})"
            )
            
            lines.append(f"Foil decay: {_band_bar(signed_foil)}")
            lines.append(f"Foil decay: {_band_bar(signed_foil)}")
            lines.append(f"Long-short: {_band_bar(signed_ls)}")
            lines.append(f"Long-short: {_band_bar(signed_ls)}")
            
    return "\n".join(lines) if lines else "No pairs have shares set."

def _band_bar(signed_frac, n_cells=7, overshoot=1.15):
    """
    2*n_cells emoji moji gauge of a signed value/band ratio.
    signed_frac: value / band_threshold, signed. +-1 = trip line.
    Colors from alert_level, with band normalized to 1.
    trip -> orange, near --> yellow, safe --> geen or blue
    Negative means drifted short/under, while positive is long/over.
    """
    SAFE_COLORS = ["🟩"] # 0 -> NEARING_BAND_FRACTION
    NEAR_COLORS = ["🟦","🟨"] # NEARING_BAND_FRACTION -> 1.0 (the line)
    TRIP_COLORS = ["🟧", "🟪"]
    
    def cell_color(ratio):
        level = _alert_level(ratio, 1.0)
        if level is None:
            zone_pos = ratio / NEARING_BAND_FRACTION if NEARING_BAND_FRACTION else 0
            i = min(len(SAFE_COLORS) - 1, int(zone_pos * len(SAFE_COLORS)))
            return SAFE_COLORS[i]
        if level == "near":
            zone_pos = (ratio - NEARING_BAND_FRACTION) / (1.0 - NEARING_BAND_FRACTION)
            i = min(len(NEAR_COLORS) - 1, int(zone_pos * len(NEAR_COLORS)))
            return NEAR_COLORS[i]
        zone_pos = ratio - 1.0
        i = min(len(TRIP_COLORS) - 1, int(zone_pos * len(TRIP_COLORS)))
        return TRIP_COLORS[i]
              
    half = [cell_color(i / (n_cells - 1) * 2.0) for i in range(n_cells)]     
    bar = half[::-1]  + half # bar and in reverse
    
    clamped = max(-2.0, min(2.0, signed_frac))
    idx = round((clamped + 2.0) / (4.0) * len(bar) - 1)
    bar[idx] = "✴️" if abs(signed_frac) > 1.0 else "✳️"
    
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
       
    if command == "/calc":
        reply = build_calc_reply(ib, args)
    elif command == "/setshares":
        reply = _handle_setshares(ib, args, state)
        reply = notify.escape_md_v2(reply)
    elif command == "/resize":
        reply = _handle_resize(ib, args, state)
        reply = notify.escape_md_v2(reply)
    elif command == "/shares":
        reply = _handle_shares_report(ib, args, state)
        reply = notify.escape_md_v2(reply)
    elif command == "/listshares":
        reply = _handle_listshares(state)
        reply = notify.escape_md_v2(reply)
    else:
        log.info("Unknown command %s from chat %s", command, chat_id)
        return # unknown command

            
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
                    _check_pair(ib, pair_key, state, send)
                _run_heartbeat_if_due(state, send)
                last_watch_check = now_monotonic
                
            watch_state.save(watch_state.state_path(), state)
    finally:
        if ib.isConnected:
            ib.disconnect()
 
 
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    run()
 
 
if __name__ == "__main__":
    main()
