"""Layer 1: daily OHLC per ticker, cached to ./cache/{ticker}.csv.

get_prices(ticker) is the public seam. It reads the cache if present and
otherwise fetches via _fetch_polygon. Swapping data sources = writing a new
_fetch_* and pointing get_prices at it.
"""

import datetime
import os

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = "cache"


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


def _fetch_polygon(ticker):
    """Fetch ~2 years of daily aggregates from Polygon.

    Accepts whatever the API returns (the free tier may cap the window).
    Returns a DataFrame indexed by date with open/high/low/close/volume.
    """
    api_key = os.environ["POLYGON_API_KEY"]
    to = datetime.date.today()
    frm = to - datetime.timedelta(days=730)

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{frm}/{to}"
    )
    resp = requests.get(
        url,
        params={
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": api_key,
        },
    )
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
