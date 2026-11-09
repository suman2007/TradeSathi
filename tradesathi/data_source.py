"""Swappable market-data sources. All are free; get_source() tries them in the order set in config.DATA_SOURCES.

UpstoxSource: Upstox public market-data API (no account or login needed for candles). Official NSE/BSE data,
              1-minute history back to Jan 2022, daily history 10+ years.
YahooSource:  Yahoo Finance via yfinance. Used as the fallback (and for anything Upstox does not list)."""
import datetime as dt
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from tradesathi import config as cfg

COLS = ["open", "high", "low", "close", "volume"]


def _empty():
    return pd.DataFrame(columns=COLS)


def _days(period):
    """'7d' -> 7, '10y' -> 3653, '60d' -> 60."""
    n, unit = float(period[:-1]), period[-1]
    return int(n * {"d": 1, "y": 365.25, "m": 30.5}[unit])


class DataSource:
    def fetch(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        """Return OHLCV DataFrame indexed by tz-aware (Asia/Kolkata) timestamps.
        interval: '1m', '5m' or '1d'. period: '7d', '2d', '10y' ..."""
        raise NotImplementedError


class YahooSource(DataSource):
    def fetch(self, symbol, interval, period):
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
        if df is None or df.empty:
            return _empty()
        df = df.rename(columns=str.lower)[COLS]
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx
        df.index = idx.tz_convert(cfg.TZ_NAME)
        return df.dropna(subset=["close"])


class UpstoxSource(DataSource):
    API = "https://api.upstox.com"
    INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/{}.json.gz"
    MINUTE_CHUNK = 28        # minute candles: at most about one month per request
    DAY_CHUNK = 3000         # daily candles: about ten years per request is accepted, stay below that
    FIRST_MINUTE = dt.date(2022, 1, 1)

    def __init__(self, cache_dir=cfg.DATA_DIR):
        self.cache_dir = cache_dir
        self._keys = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ symbol -> instrument key
    def _instrument_map(self, exch):
        with self._lock:                                    # several worker threads may ask at once
            return self._instrument_map_locked(exch)

    def _instrument_map_locked(self, exch):
        if exch not in self._keys:
            path = os.path.join(self.cache_dir, f"upstox_{exch}.json.gz")
            fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400
            if not fresh:
                os.makedirs(self.cache_dir, exist_ok=True)
                urllib.request.urlretrieve(self.INSTRUMENTS.format(exch), path + ".tmp")
                os.replace(path + ".tmp", path)
            rows = json.load(gzip.open(path))
            self._keys[exch] = {r["trading_symbol"]: r["instrument_key"] for r in rows
                                if r.get("segment") == f"{exch}_EQ"}
        return self._keys[exch]

    def key(self, symbol):
        base, _, suffix = symbol.upper().partition(".")
        exch = "BSE" if suffix == "BO" else "NSE"
        k = self._instrument_map(exch).get(base)
        if k is None:
            raise ValueError(f"{symbol} not found in the Upstox {exch} instrument list")
        return k

    # ------------------------------------------------------------ http
    def _get(self, path):
        url = self.API + path
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "trading-analyzer"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    j = json.load(r)
                return j.get("data", {}).get("candles") or []
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:           # rate limited: back off and retry
                    time.sleep(20 * (attempt + 1))
                    continue
                body = e.read().decode(errors="replace")[:200]
                raise ValueError(f"Upstox HTTP {e.code}: {body}") from None
        return []

    @staticmethod
    def _frame(candles):
        if not candles:
            return _empty()
        df = pd.DataFrame([c[:6] for c in candles], columns=["ts"] + COLS)
        df.index = pd.to_datetime(df.pop("ts")).dt.tz_convert(cfg.TZ_NAME)
        df.index.name = None
        return df.astype(float).sort_index()

    def _history(self, key, unit, step, start, end, chunk):
        """Historical candles from start to end (dates, inclusive), requested in chunks."""
        q, parts, a = urllib.parse.quote(key, safe=""), [], start
        while a <= end:
            b = min(a + dt.timedelta(days=chunk), end)
            parts.append(self._frame(self._get(f"/v3/historical-candle/{q}/{unit}/{step}/{b}/{a}")))
            a = b + dt.timedelta(days=1)
            time.sleep(0.2)                                 # stay well inside the free rate limit
        parts = [p for p in parts if not p.empty]
        return pd.concat(parts).sort_index() if parts else _empty()

    def _today(self, key, step):
        q = urllib.parse.quote(key, safe="")
        return self._frame(self._get(f"/v3/historical-candle/intraday/{q}/minutes/{step}"))

    def fetch(self, symbol, interval, period):
        k = self.key(symbol)
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
        start, yday = today - dt.timedelta(days=_days(period)), today - dt.timedelta(days=1)
        if interval in ("1m", "5m"):
            step = interval[:-1]
            hist = self._history(k, "minutes", step, max(start, self.FIRST_MINUTE), yday, self.MINUTE_CHUNK)
            df = pd.concat([hist, self._today(k, step)])
        elif interval == "1d":
            hist = self._history(k, "days", 1, start, yday, self.DAY_CHUNK)
            hist.index = hist.index.normalize()             # older daily candles are stamped 09:15, newer 00:00
            m1 = self._today(k, 1)
            if not m1.empty:                                # today's still-forming daily candle, built from 1-min bars
                ts = pd.Timestamp(today, tz=cfg.TZ_NAME)
                hist.loc[ts] = [m1["open"].iloc[0], m1["high"].max(), m1["low"].min(), m1["close"].iloc[-1],
                                m1["volume"].sum()]
            df = hist
        else:
            raise ValueError(f"interval {interval} not supported by Upstox source")
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.dropna(subset=["close"])


class FallbackSource(DataSource):
    """Try each source in turn; the first one that returns data wins."""

    def __init__(self, sources):
        self.sources = sources

    def fetch(self, symbol, interval, period):
        err = None
        for s in self.sources:
            try:
                df = s.fetch(symbol, interval, period)
                if not df.empty:
                    return df
            except Exception as e:      # network error, unknown symbol ... try the next source
                err = e
        if err:
            print(f"  data fetch failed for {symbol} {interval}: {err}")
        return _empty()


SOURCES = {"upstox": UpstoxSource, "yahoo": YahooSource}


def get_source(names=None):
    return FallbackSource([SOURCES[n]() for n in (names or cfg.DATA_SOURCES)])


# Example for later (paid / login-based APIs plug in the same way):
# class KiteSource(DataSource):
#     def fetch(self, symbol, interval, period): ...  # call kite.historical_data(...)
