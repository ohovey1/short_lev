"""Layer 1: daily OHLC per ticker, cached to ./cache/{ticker}.csv.

get_prices(ticker) is the public seam. It reads the cache if present and
otherwise fetches via _fetch_polygon. Swapping data sources = writing a new
_fetch_* and pointing get_prices at it.

get_borrow_rates() is the second seam: IBKR indicative short-borrow rates,
same cache-first pattern but time-limited (12 hours) since rates move daily.
"""

import datetime
import io
import os
import time
import urllib.request

import pandas as pd
import requests
from dotenv import load_dotenv

import config

load_dotenv()

# Anchor the cache to the project root (parent of src/) so it resolves the same
# regardless of the process working directory.
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache")

BORROW_CACHE = os.path.join(CACHE_DIR, "borrow_rates.csv")
BORROW_HISTORY = os.path.join(CACHE_DIR, "borrow_history.csv")
BORROW_CACHE_MAX_AGE = datetime.timedelta(hours=12)

# Official IBKR shortstock mirrors serving the same usa.txt. ftp2 is tried
# first because ftp3 timed out from the dev network when this was built; the
# second host is the fallback if the first is down.
IBKR_BORROW_HOSTS = ["ftp2.interactivebrokers.com", "ftp3.interactivebrokers.com"]


def get_prices(ticker):
    """Return a daily OHLCV DataFrame indexed by date for ticker.

    Cache-first: read ./cache/{ticker}.csv if present, else fetch from
    Polygon, write the cache, and return.
    """
    path = os.path.join(CACHE_DIR, f"{ticker}.csv")
    if os.path.exists(path):
        return pd.read_csv(path, index_col="date", parse_dates=True)

    df = _fetch_polygon(ticker)
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(path)
    return df


def get_borrow_rates():
    """Return IBKR indicative borrow rates, or None if unavailable.

    DataFrame indexed by ticker with columns fee_rate (annualized fraction,
    e.g. 0.0523), available (int shares), and fetched_at (ISO timestamp of the
    download, same on every row -- the UI shows it). Cache-first like
    get_prices, but the cache expires after 12 hours. On any fetch failure
    (FTP down, parse error) returns None -- callers fall back to config rates.
    """
    if os.path.exists(BORROW_CACHE):
        try:
            cached = pd.read_csv(BORROW_CACHE, index_col="ticker")
            fetched_at = datetime.datetime.fromisoformat(cached["fetched_at"].iloc[0])
            if datetime.datetime.now() - fetched_at < BORROW_CACHE_MAX_AGE:
                return cached
        except Exception:
            pass  # unreadable cache -- fall through and refetch

    try:
        df = _fetch_ibkr_borrow()
    except Exception:
        return None
    df["fetched_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(BORROW_CACHE)
    _append_borrow_history(df)
    return df


def _append_borrow_history(rates):
    """Log today's rate per PAIRS ticker to cache/borrow_history.csv.

    Passive accrual for future backfill work -- nothing reads this file yet.
    One row (date, ticker, fee_rate, available) per leveraged ticker per day;
    a ticker already logged today is skipped, so refetching within a day does
    not duplicate rows.
    """
    today = datetime.date.today().isoformat()
    logged = set()
    if os.path.exists(BORROW_HISTORY):
        hist = pd.read_csv(BORROW_HISTORY, dtype=str)
        logged = set(hist.loc[hist["date"] == today, "ticker"])

    rows = [
        {"date": today, "ticker": t,
         "fee_rate": rates.loc[t, "fee_rate"],
         "available": rates.loc[t, "available"]}
        for t in config.PAIRS
        if t not in logged and t in rates.index
    ]
    if not rows:
        return
    pd.DataFrame(rows).to_csv(
        BORROW_HISTORY, mode="a", header=not os.path.exists(BORROW_HISTORY),
        index=False,
    )


def _fetch_ibkr_borrow():
    """Download IBKR's public short-stock list (usa.txt) and parse it.

    Pipe-delimited. Line 1 is a #BOF stamp (skipped), line 2 is the column
    header (#SYM|...|FEERATE|AVAILABLE|...), the last line is an #EOF footer
    (dropped when its FEERATE fails numeric coercion). FEERATE is in PERCENT
    -- divided by 100 here so fee_rate is an annualized fraction. AVAILABLE
    can be ">10000000"; the ">" is stripped.
    """
    raw = None
    for host in IBKR_BORROW_HOSTS:
        try:
            url = f"ftp://shortstock:@{host}/usa.txt"
            with urllib.request.urlopen(url, timeout=15) as resp:
                raw = resp.read()
            break
        except Exception:
            continue
    if raw is None:
        raise ConnectionError("all IBKR borrow FTP hosts failed")

    df = pd.read_csv(io.BytesIO(raw), sep="|", skiprows=1, dtype=str,
                     encoding="latin-1")
    df = df.rename(columns={"#SYM": "ticker", "FEERATE": "fee_rate",
                            "AVAILABLE": "available"})
    df = df[["ticker", "fee_rate", "available"]]
    df["fee_rate"] = pd.to_numeric(df["fee_rate"], errors="coerce") / 100
    df["available"] = pd.to_numeric(df["available"].str.lstrip(">"), errors="coerce")
    df = df.dropna(subset=["fee_rate"])
    df["available"] = df["available"].fillna(0).astype("int64")
    df = df.set_index("ticker")
    return df[~df.index.duplicated()]


def _fetch_polygon(ticker):
    """Fetch ~2 years of daily aggregates from Polygon.

    Accepts whatever the API returns (the free tier may cap the window).
    Returns a DataFrame indexed by date with open/high/low/close/volume.
    Retries on HTTP 429: the free tier allows ~5 requests/min, and a cold
    cache (e.g. a fresh deploy warming many tickers) trips that limit.
    """
    api_key = os.environ["POLYGON_API_KEY"]
    to = datetime.date.today()
    frm = to - datetime.timedelta(days=730)

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{frm}/{to}"
    )
    for _attempt in range(5):
        resp = requests.get(
            url,
            params={
                "adjusted": "true",
                "sort": "asc",
                "limit": 50000,
                "apiKey": api_key,
            },
        )
        if resp.status_code != 429:
            break
        time.sleep(15)  # wait out the rate-limit window, then retry
    resp.raise_for_status()
    payload = resp.json()

    results = payload.get("results")
    if not results:
        raise ValueError(
            f"Polygon returned no data for {ticker} "
            f"(status={payload.get('status')!r})"
        )

    df = pd.DataFrame(results)
    df["date"] = pd.to_datetime(df["t"], unit="ms").dt.normalize()
    df = df.rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df = df[["date", "open", "high", "low", "close", "volume"]]
    return df.set_index("date").sort_index()
