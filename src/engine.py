"""Layer 2: pure interval P&L for one two-leg position.

A position is short $X of a 3x leveraged ETF and long $3X of its underlying,
opened at some entry prices and marked at the current prices. position_pnl
returns the interval P&L (entry -> current) for that fixed-size position.

Pure: no I/O, no global state, no date or OHLC awareness. The caller (the
backtest) holds the tranche ladder and decides which prices are entry vs
current.
"""


def position_pnl(lev_entry, lev_now, und_entry, und_now, short_size, long_size):
    """Interval P&L for one position, from entry prices to current prices.

    short_size = $X short on the leveraged ETF; long_size = $3X long on the
    underlying. Price legs only -- borrow is handled separately by the caller
    (see borrow_cost). Returns a breakdown dict so each leg is visible without
    recomputation.
    """
    lev_return = lev_now / lev_entry - 1
    und_return = und_now / und_entry - 1

    short_pnl = -short_size * lev_return  # short profits when the leveraged ETF falls
    long_pnl = long_size * und_return     # long profits when the underlying rises
    net = short_pnl + long_pnl

    return {
        "short_pnl": short_pnl,
        "long_pnl": long_pnl,
        "net": net,
    }


def borrow_cost(notional, rate_annual, days=1):
    """Borrow charge on a position's short notional over days at rate_annual.

    Simple daily accrual on a 360-day basis (the money-market convention):
    notional * rate_annual * days / 360. Applied per open tranche per day by
    the backtest. No default rate -- the caller must pass one explicitly.
    """
    return notional * rate_annual * days / 360
