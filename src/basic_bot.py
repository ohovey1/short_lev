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

from dotenv import load_dotenv
from ib_async import IB, Stock
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config

load_dotenv()
log = logging.getLogger(__name__)

CALC_CLIENT_ID = int(os.environ.get("CALC_CLIENT_ID"), 21) #TODO
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", 4002))

# in case we check pair not in config, which shouldn't happen now
REG_T_LONG_RATE = 0.25
REG_T_SHORT_RATE_PER_LEVERAGE = 0.30

USAGE = (
    "Usage: /calc SHORT_TICKER LONG_TICKER LEVERAGE SHARES_SHORT SHARES_LONG BASE_CAPITAL\n"
    "Example: /calc TSLL TSLA 2 100 250 10000"
)

def _rates_for(short_ticker, leverage):
    """
    """
    pair = config.PAIRS.get(short_ticker.upper())
    
    if pair:
        return pair["long_rate"], pair["short_rate"], pair["leverage"], "config.PAIRS (IBKR-observed)"
    
    return (REG_T_LONG_RATE, REG_T_SHORT_RATE_PER_LEVERAGE * leverage, leverage,
            "generic Reg-T fallback -- NOT IBKR-confirmed for this ticker")

def _price(ib, ticker):
    """
    Attempts to fet live price, or returns None
    """
    contract = Stock(ticker, "SMART", "USD")
    [snap] = ib.reqTickers(contract)
    price = snap.marketPrice()
    if price is None or price != price:
        return None
    return price

async def calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 6:
        await update.message.reply_text(USAGE)
        return
    
    short_ticker, long_ticker, leverage_s, shares_short_s, shares_long_s, base_capital_s = args
    
    try:
        leverage_in = float(leverage_s)
        shares_short = float(shares_short_s)
        shares_long = float(shares_long_s)
        base_capital = float(base_capital_s)
    except ValueError:
        await update.message.reply_text("Leverage, shares, and base_capital must all be numbers.\n\n" + USAGE)
        return
    
    ib = context.bot_data["ib"]
    price_short = _price(ib, short_ticker)
    price_long = _price(ib, long_ticker)
    if price_short is None or price_long is None:
        missing = []
        if price_short is None:
            missing.append(short_ticker.upper())
        if price_long is None:
            missing.append(long_ticker.upper())  
        await update.message.reply_text(
            f"Could not get a live price for: {', '.join(missing)}. Check the symbol(s) and try again."
        )
        return
    
    long_rate, short_rate, leverage, rate_source = _rates_for(short_ticker, leverage_in)
    margin_mult = long_rate * leverage + short_rate
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
    
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    net_delta = long_notional - leverage * short_notional
    
    lines = [
        f"{short_ticker.upper()} (short) @ ${price_short:,.2f} x {shares_short:,.0f} sh "
        f"= ${short_notional:,.2f}",
        f"{long_ticker.upper()} (long)  @ ${price_long:,.2f} x {shares_long:,.0f} sh "
        f"= ${long_notional:,.2f}",
        "",
        f"leverage={leverage:g}  margin_multiplier={margin_mult:.3f}  "
        f"(long_rate={long_rate:.2f}, short_rate={short_rate:.2f})",
        f"rates source: {rate_source}",
        "",
        f"target (short) = base_capital ${base_capital:,.2f} x "
        f"capital_utilization {config.DEFAULT_CAPITAL_UTILIZATION:.0%} / "
        f"margin_multiplier {margin_mult:.3f} = ${target:,.2f}",
        f"net_delta = long ${long_notional:,.2f} - leverage {leverage:g} x "
        f"short ${short_notional:,.2f} = ${net_delta:,.2f}",
        "",
        f"bands (config.py defaults): long_short={config.DEFAULT_LONG_SHORT_BAND:.0%}  "
        f"foil_decay={config.DEFAULT_FOIL_DECAY_BAND:.0%}",
        "",
    ]
    
    if abs(short_notional - target) > config.DEFAULT_FOIL_DECAY_BAND * target:
        new_short_shares = round(target / price_short)
        new_long_shares = round((leverage * target) / price_long)
        
        lines.append(
            f"TRIP: foil decay band -- short notional is "
            f"{abs(short_notional - target) / target:.1%} off target.\n"
            f"  Reset both legs to target:\n"
            f"    {short_ticker.upper()}: {shares_short:,.0f} -> {new_short_shares:,d} sh\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh"
        )
        
    elif abs(net_delta) > config.DEFAULT_LONG_SHORT_BAND * target:
        new_long_shares = round((leverage * short_notional) / price_long)
        
        lines.append(
            f"TRIP: long-short band -- net delta is "
            f"{abs(net_delta) / target:.1%} of target.\n"
            f"  Short leg unchanged. Resize long leg only:\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh"   
        )
        
    else:
        lines.append("No trip -- current shares are both within bands.")
        
    lines.append(
        "\n(Only foil-decay and long-short are checked here. Drawdown stop and "
        "margin de-risk both need a live position's persisted peak_equity / "
        "actual maintenance margin, which this what-if calculator has no reason to hold.)"
    )
 
    await update.message.reply_text("\n".join(lines))
    
async def post_init(app: Application):
    """
    Connect to same Gatway as monitor on diff client id
    
    Read-only: never touches actual positions
    """
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=CALC_CLIENT_ID, readonly=True)
    app.bot_data["ib"] = ib
    log.info(
        "connected to IB Gateway for price lookups: host=%s port=%s clientId=%s",
        IB_HOST, IB_PORT, CALC_CLIENT_ID,
    )
    
async def post_shutdown(app: Application):
    ib = app.bot_data.get("ib")
    if ib is not None and ib.isConnected():
        ib.disconnect()
        
def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ["TELEGRAM_BASIC_BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("calc", calc))
    app.run_polling()
    
if __name__ == "__main__":
    main()
