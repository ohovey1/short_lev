"""Pair registry. Adding a pair = editing this dict.

Keyed by the leveraged ETF ticker (unique, and it's the fund the strategy
shorts). Two pairs can share an underlying (e.g. TQQQ and QLD both on QQQ), so
the underlying ticker can't be the key.

v1 assumes a positive-leverage (bull) fund: shorting it hedges the long
underlying leg to ~delta-neutral. Inverse funds (e.g. SQQQ) would make the book
double-long and need the v2 signed-leverage work -- do not add them yet.

Note: for the sector pairs (FAS, ERX), the underlying ETF and the LETF's
benchmark index are not identical (e.g. FAS tracks a Russell financials index,
XLF tracks the S&P financials sector), so the hedge is only approximately
delta-neutral. Fine for studying decay; just not a perfect offset.

The single-stock pairs (NVDL, TSLL, CONL) hedge against a single stock rather
than an ETF as the underlying. The architecture treats a stock underlying
identically to an ETF underlying -- get_prices, position_pnl, and the backtest
loop are all ticker-agnostic -- so no code changes were needed to add them.
"""

# borrow_rate_annual: indicative reference rates for the short leg, NOT live
# quotes. Hand-refresh from IBKR/iBorrowDesk before trusting an absolute
# number; these are placeholders to make the borrow cost non-zero and
# roughly right-ordered (index/sector ETFs cheap to borrow, single stocks
# much pricier).

# live: True only for pairs actually cleared for live trading once automation
# exists. Everything is False today. The monitor (src/monitor.py) reads a real
# account but only reads -- there is still no order-submission code anywhere in
# the repo (see docs/AUTOMATION.md). This is a manual, reviewed flag: nothing
# should ever flip to True except by a human editing this file and committing it.

# long_rate / short_rate: MAINTENANCE margin required per $1 of notional on
# each leg. Margin is a function of BOTH legs:
#
#     margin_required = long_rate * long_notional + short_rate * short_notional
#
# INITIAL IS NOT MAINTENANCE. This distinction cost us a 45%-too-high
# multiplier on the single-stock pairs and is the whole reason spec 005 exists,
# so it is spelled out rather than left implied. Reg T initial on a single
# stock is 50%; IBKR *maintains* long equity at 25%. Sizing runs against the
# maintenance number, because that is what a live position is marked to
# day-to-day -- the initial rate only gates the opening trade. When adding a
# pair, look up the maintenance rate, never the initial one.
#
#   long leg  (held long):  25% maintenance, ETF or single stock alike
#   short leg (held short): 30% maintenance on the unleveraged fund, scaled by
#                           the fund's leverage factor (2x -> 0.60, 3x -> 0.90)
#
# Note that the single-stock pairs carry exactly the same rates as the 2x ETF
# pairs. That is correct, not a copy-paste error: at maintenance both get the
# 25% long rate. Do not "fix" it back to 0.50 -- see spec 005 and spec 004's
# Result for the observation that settled it.
#
# These are REGULATORY-FORMULA ESTIMATES, not confirmed IBKR house numbers --
# house requirements (especially for single-stock leveraged ETFs) may run
# higher and can be raised without notice. Validated against four paper
# observations in spec 005 (residuals +1.25% to +1.67%, all one sign,
# consistent with a small house add-on). Confirm via a TWS what-if order
# before sizing any live position off these fields.
PAIRS = {
    # Broad indices -- 3x
    "TQQQ": {"leveraged_ticker": "TQQQ", "underlying_ticker": "QQQ", "leverage": 3, "borrow_rate_annual": 0.01, "live": False, "long_rate": 0.25, "short_rate": 0.90},
    "UPRO": {"leveraged_ticker": "UPRO", "underlying_ticker": "SPY", "leverage": 3, "borrow_rate_annual": 0.01, "live": False, "long_rate": 0.25, "short_rate": 0.90},
    "UDOW": {"leveraged_ticker": "UDOW", "underlying_ticker": "DIA", "leverage": 3, "borrow_rate_annual": 0.01, "live": False, "long_rate": 0.25, "short_rate": 0.90},
    "TNA": {"leveraged_ticker": "TNA", "underlying_ticker": "IWM", "leverage": 3, "borrow_rate_annual": 0.01, "live": False, "long_rate": 0.25, "short_rate": 0.90},
    "TMF": {"leveraged_ticker": "TMF", "underlying_ticker": "TLT", "leverage": 3, "borrow_rate_annual": 0.02, "live": False, "long_rate": 0.25, "short_rate": 0.90},
    # High-vol sectors
    "SOXL": {"leveraged_ticker": "SOXL", "underlying_ticker": "SOXX", "leverage": 3, "borrow_rate_annual": 0.03, "live": False, "long_rate": 0.25, "short_rate": 0.90},
    "FAS": {"leveraged_ticker": "FAS", "underlying_ticker": "XLF", "leverage": 3, "borrow_rate_annual": 0.03, "live": False, "long_rate": 0.25, "short_rate": 0.90},
    "ERX": {"leveraged_ticker": "ERX", "underlying_ticker": "XLE", "leverage": 2, "borrow_rate_annual": 0.03, "live": False, "long_rate": 0.25, "short_rate": 0.60},
    # 2x contrasts on the same underlying as a 3x pair (decay vs leverage)
    "QLD": {"leveraged_ticker": "QLD", "underlying_ticker": "QQQ", "leverage": 2, "borrow_rate_annual": 0.01, "live": False, "long_rate": 0.25, "short_rate": 0.60},
    "SSO": {"leveraged_ticker": "SSO", "underlying_ticker": "SPY", "leverage": 2, "borrow_rate_annual": 0.01, "live": False, "long_rate": 0.25, "short_rate": 0.60},
    # Single-stock 2x -- underlying is a stock, not an ETF (see docstring).
    # Same rates as the 2x ETF pairs above: at MAINTENANCE the long leg is 25%
    # either way. The old 0.50 here was the initial requirement. See the
    # initial-vs-maintenance note above before changing these.
    "NVDL": {"leveraged_ticker": "NVDL", "underlying_ticker": "NVDA", "leverage": 2, "borrow_rate_annual": 0.10, "live": False, "long_rate": 0.25, "short_rate": 0.60},
    "TSLL": {"leveraged_ticker": "TSLL", "underlying_ticker": "TSLA", "leverage": 2, "borrow_rate_annual": 0.10, "live": False, "long_rate": 0.25, "short_rate": 0.60},
    "CONL": {"leveraged_ticker": "CONL", "underlying_ticker": "COIN", "leverage": 2, "borrow_rate_annual": 0.10, "live": False, "long_rate": 0.25, "short_rate": 0.60},
}


def margin_multiplier(pair):
    """Sizing denominator: margin required per $1 of short notional.

    This is the two-term margin formula evaluated at ZERO NET DELTA, where
    long = leverage * short:

        long_rate * (leverage * short) + short_rate * short
          = (long_rate * leverage + short_rate) * short

    True at entry and immediately after any reset, which is exactly when
    sizing happens -- so the single-valued form is valid for deriving target
    even though margin itself is a function of both legs. It stops being valid
    as delta drifts, which is why margin_required is computed the two-term way
    everywhere it is *measured* rather than *sized*.

    Derived, never stored: one source of truth with margin_required, so the
    sizing denominator and the margin measurement cannot drift apart.
    """
    return pair["long_rate"] * pair["leverage"] + pair["short_rate"]


# Band parameters. These mirror band.run_band_backtest's signature defaults
# exactly -- the monitor builds its BandParams from these so it cannot drift to
# separate tuning from the backtest. The duplication with band.py's signature is
# deliberate for now: spec 004 forbids touching band.py. Unify the two once a
# spec permits it; until then, changing a value here means changing it there.
DEFAULT_LONG_SHORT_BAND = 0.10
DEFAULT_FOIL_DECAY_BAND = 0.10
DEFAULT_CAPITAL_UTILIZATION = 0.75
DEFAULT_DRAWDOWN_STOP = 0.10
DEFAULT_MARGIN_DERISK = True