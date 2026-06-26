"""Pair registry. Adding a pair = editing this dict.

v1 assumes a positive-leverage fund: shorting it hedges the long underlying
leg to ~delta-neutral. Inverse funds (e.g. SQQQ) would make the book double-long
and need the v2 signed-leverage work -- do not add them yet.
"""

PAIRS = {
    "QQQ": {"leveraged_ticker": "TQQQ", "underlying_ticker": "QQQ", "leverage": 3},
}
