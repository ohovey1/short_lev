"""Bitwise regression digest for the band backtest (src/band.py).

Run from the project root:
    .venv/Scripts/python.exe scripts/hash_band.py

Prints a SHA-256 per pair-arm, per pair, and one grand digest over every raw
float the band backtest produces. Capture it before a refactor and compare
after: identical digests prove the change is bit-for-bit behavior-neutral.

Why this exists alongside verify_band.py: that script's printed output rounds
at ~6 decimals, so a text diff can hide a change in the 12th digit. This
hashes float.hex(), which round-trips losslessly, so nothing rounds away.

Five parameter arms, chosen so the digest actually reaches every branch of the
trigger chain on the current cached window:

    cu=0.75 defaults                    the shipped configuration
    cu=1.0, closing rules off           the knobs-off path
    cu=0.90 defaults                    de-risk fires genuinely
    cu=1.0 defaults                     de-risk fires constantly
    cu=0.75, drawdown_stop=0.005        the ONLY arm that stops any pair

That last arm is load-bearing. At every default-ish setting no pair stops on
this window, so without it the terminal branch would be invisible to the
digest and a wrong final_target (or a wrongly-advanced segment entry price)
on a stopped run would pass silently.

Trade rows are hashed individually, not just the turnover totals: turnover is
a sum and could net an error out, but a wrong traded_lev on a single row moves
the digest immediately.
"""

import hashlib
import os
import sys

# Make the src/ modules importable when run from the project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pandas as pd

import band
import config

BASE_CAPITAL = 10000.0

# (label, kwargs) -- see the module docstring for why each arm is here.
ARMS = [
    ("cu0.75-defaults", dict(capital_utilization=0.75)),
    ("cu1.00-noclose", dict(capital_utilization=1.0, drawdown_stop=None,
                            margin_derisk=False)),
    ("cu0.90-defaults", dict(capital_utilization=0.90)),
    ("cu1.00-defaults", dict(capital_utilization=1.0)),
    ("cu0.75-stop0.005", dict(capital_utilization=0.75, drawdown_stop=0.005)),
]

# Trade-row columns hashed per row. Order is fixed and must not be sorted.
TRADE_FLOAT_COLS = ["lev_entry", "lev_exit", "und_entry", "und_exit",
                    "short_pnl", "long_pnl", "total_pnl",
                    "traded_lev", "traded_und"]


def feed_float(h, x):
    """Hash one float by its exact hex form, so nothing rounds away."""
    h.update(float(x).hex().encode())
    h.update(b"|")


def feed_obj(h, x):
    """Hash a non-float (int, bool, str, date, None) by its repr."""
    h.update(repr(x).encode())
    h.update(b"|")


def feed_date(h, d):
    """Hash a date/timestamp as ISO, or None."""
    feed_obj(h, None if d is None else pd.Timestamp(d).isoformat())


def digest_run(r):
    """SHA-256 over every value one run_band_backtest result carries."""
    h = hashlib.sha256()

    for v in r["equity_curve"].to_numpy():
        feed_float(h, v)
    for v in r["margin_cushion"].to_numpy():
        feed_float(h, v)

    for key in ["pct_return", "borrow_paid", "notional_days", "max_drawdown",
                "worst_day", "turnover_lev", "turnover_und",
                "breakeven_borrow", "min_margin_cushion", "final_target"]:
        feed_float(h, r[key])

    for key in ["n_trades", "n_derisk", "stopped", "margin_breached"]:
        feed_obj(h, r[key])

    feed_date(h, r["stop_date"])
    feed_date(h, r["margin_breach_date"])

    t = r["trades"]
    feed_obj(h, len(t))
    for row in t.itertuples(index=False):
        feed_obj(h, row.trigger)
        feed_date(h, row.open_date)
        feed_date(h, row.close_date)
        for col in TRADE_FLOAT_COLS:
            feed_float(h, getattr(row, col))

    return h.hexdigest()


def main():
    grand = hashlib.sha256()

    for pair_key in sorted(config.PAIRS):
        pair = config.PAIRS[pair_key]
        rate = pair["borrow_rate_annual"]
        per_pair = hashlib.sha256()

        for label, kwargs in ARMS:
            r = band.run_band_backtest(
                pair_key, base_capital=BASE_CAPITAL,
                borrow_rate_annual=rate, **kwargs
            )
            d = digest_run(r)
            per_pair.update(d.encode())
            grand.update(d.encode())
            print(f"{pair_key:6s} {label:18s} {d}"
                  f"  (trades {r['n_trades']}, derisk {r['n_derisk']}, "
                  f"stopped {r['stopped']})")

        print(f"{pair_key:6s} {'PAIR':18s} {per_pair.hexdigest()}")
        print()

    print("=" * 60)
    print(f"GRAND {grand.hexdigest()}")


if __name__ == "__main__":
    main()
