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
 
USAGE = (
    "Usage: /calc SHORT_TICKER LONG_TICKER LEVERAGE SHARES_SHORT SHARES_LONG BASE_CAPITAL\n"
    "Example: /calc TSLL TSLA 2 100 250 10000"
)
 
 
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
 
 
def build_calc_reply(ib, args):
    """Args in, reply text out. Pure given the ib price lookups."""
    if len(args) != 6:
        return USAGE
 
    short_ticker, long_ticker, leverage_s, shares_short_s, shares_long_s, base_capital_s = args
 
    try:
        leverage_in = float(leverage_s)
        shares_short = float(shares_short_s)
        shares_long = float(shares_long_s)
        base_capital = float(base_capital_s)
    except ValueError:
        return "Leverage, shares, and base_capital must all be numbers.\n\n" + USAGE
 
    price_short = _price(ib, short_ticker)
    price_long = _price(ib, long_ticker)
    if price_short is None or price_long is None:
        missing = []
        if price_short is None:
            missing.append(short_ticker.upper())
        if price_long is None:
            missing.append(long_ticker.upper())
        return (f"Could not get a live price for: {', '.join(missing)}. "
                f"Check the symbol(s) and try again.")
 
    long_rate, short_rate, leverage, rate_source = _rates_for(short_ticker, leverage_in)
    margin_mult = long_rate * leverage + short_rate
    target = (base_capital * config.DEFAULT_CAPITAL_UTILIZATION) / margin_mult
 
    short_notional = shares_short * price_short
    long_notional = shares_long * price_long
    net_delta = long_notional - leverage * short_notional
    
    e = notify.escape_md_v2
 
    lines = [
        "*" + e(f"*CURRENT PRICES FOR {short_ticker.upper()} & {long_ticker.upper()}") + "*",
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
        e("Target (short) = "),
        e(f"base_capital ${base_capital:,.2f} x capital_utilization {config.DEFAULT_CAPITAL_UTILIZATION:.0%} / "),
        e(f"margin_multiplier {margin_mult:.3f}"),
        e(f"= ${target:,.2f}"),
        "",
        e("Net distance limit = "),
        e(f"long ${long_notional:,.2f} - leverage {leverage:g} x "
          f"short ${short_notional:,.2f}"),
        e(f"= ${net_delta:,.2f}"),
        "",
        e(f"bands (config.py defaults): long_short={config.DEFAULT_LONG_SHORT_BAND:.0%}  "
          f"foil_decay={config.DEFAULT_FOIL_DECAY_BAND:.0%}"),
        ""
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
        lines.append(e(
            f"TRIP: long-short band -- net delta is "
            f"{abs(net_delta) / target:.1%} of target.\n"
            f"  Short leg unchanged. Resize long leg only:\n"
            f"    {long_ticker.upper()}: {shares_long:,.0f} -> {new_long_shares:,d} sh"
        ))
    else:
        lines.append("No trip -- current shares are both within bands.")
 
    lines.append(e(
        "\n(Only foil-decay and long-short are checked here. Drawdown stop and "
        "margin de-risk both need a live position's persisted peak_equity / "
        "actual maintenance margin, which this what-if calculator has no reason to hold.)"
    ))
 
    return "\n".join(lines)

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
 
 
def handle_message(ib, message, token, configured_chat_id):
    text = (message.get("text") or "").strip()
    if not text.startswith("/calc"):
        return
 
    chat_id = message.get("chat", {}).get("id")
    if str(chat_id) != str(configured_chat_id):
        # Same silence-on-mismatch policy as bot.py: an error reply would
        # confirm to a stranger that the bot is live and their message
        # arrived. Log it -- a stream of these is the signal worth watching.
        # configured_chat_id here is TELEGRAM_BASIC_CHAT_ID -- deliberately
        # its own chat, separate from the monitor's TELEGRAM_CHAT_ID alert
        # channel, so /calc traffic never mixes with live-position alerts.
        log.info("unauthorized /calc from chat %s -- ignoring", chat_id)
        return
 
    args = text.split()[1:]
    reply = build_calc_reply(ib, args)
    delivered, error, _ = notify.send_text(token, configured_chat_id)
    if not delivered:
        log.warning("reply to /calc failed: %s", error)
 
 
def run():
    token = os.environ["TELEGRAM_BASIC_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_BASIC_CHAT_ID"]
 
    # No persisted offset: this bot is stateless by design (no
    # StateDirectory=, see the unit file). Starting at 0 means a restart can
    # re-answer at most one in-flight /calc
    offset = 0
    backoff = BACKOFF_START
    ib_backoff = RECONNECT_BACKOFF_START
    
    ib = connect_with_backoff()
 
    log.info("basic_bot: chat_id=%s (TELEGRAM_BASIC_CHAT_ID) clientId=%s",
              chat_id, CALC_CLIENT_ID)
    
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
                        handle_message(ib, update["message"], token, chat_id)
                except DISCONNECT_ERRORS as e:
                    log.warning("IB disconnnected mid-update: %s: %s",
                                type(e).__name__, e)
                except Exception:
                    log.exception("update %s failed; skipping", update.get("update_id"))
                offset = update.get("update_id", offset)
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
