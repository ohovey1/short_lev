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
PAIRS = {
    # Broad indices -- 3x
    "TQQQ": {"leveraged_ticker": "TQQQ", "underlying_ticker": "QQQ", "leverage": 3, "borrow_rate_annual": 0.01},
    "UPRO": {"leveraged_ticker": "UPRO", "underlying_ticker": "SPY", "leverage": 3, "borrow_rate_annual": 0.01},
    "UDOW": {"leveraged_ticker": "UDOW", "underlying_ticker": "DIA", "leverage": 3, "borrow_rate_annual": 0.01},
    "TNA": {"leveraged_ticker": "TNA", "underlying_ticker": "IWM", "leverage": 3, "borrow_rate_annual": 0.01},
    "TMF": {"leveraged_ticker": "TMF", "underlying_ticker": "TLT", "leverage": 3, "borrow_rate_annual": 0.02},
    # High-vol sectors
    "SOXL": {"leveraged_ticker": "SOXL", "underlying_ticker": "SOXX", "leverage": 3, "borrow_rate_annual": 0.03},
    "FAS": {"leveraged_ticker": "FAS", "underlying_ticker": "XLF", "leverage": 3, "borrow_rate_annual": 0.03},
    "ERX": {"leveraged_ticker": "ERX", "underlying_ticker": "XLE", "leverage": 2, "borrow_rate_annual": 0.03},
    # 2x contrasts on the same underlying as a 3x pair (decay vs leverage)
    "QLD": {"leveraged_ticker": "QLD", "underlying_ticker": "QQQ", "leverage": 2, "borrow_rate_annual": 0.01},
    "SSO": {"leveraged_ticker": "SSO", "underlying_ticker": "SPY", "leverage": 2, "borrow_rate_annual": 0.01},
    # Single-stock 2x -- underlying is a stock, not an ETF (see docstring)
    "NVDL": {"leveraged_ticker": "NVDL", "underlying_ticker": "NVDA", "leverage": 2, "borrow_rate_annual": 0.10},
    "TSLL": {"leveraged_ticker": "TSLL", "underlying_ticker": "TSLA", "leverage": 2, "borrow_rate_annual": 0.10},
    "CONL": {"leveraged_ticker": "CONL", "underlying_ticker": "COIN", "leverage": 2, "borrow_rate_annual": 0.10},
}
