"""Shared test helpers: an offline fake market-data source and an isolated working folder per test."""
import numpy as np
import pandas as pd
import pytest

from tradesathi.storage import persist

IST = "Asia/Kolkata"
EMPTY = ["open", "high", "low", "close", "volume"]


class FakeSource:
    """Synthetic random-walk candles, so tests never touch the network."""

    def fetch(self, symbol, interval, period):
        n, unit = int(float(period[:-1])), period[-1]
        days = n * (365 if unit == "y" else 1)
        rng = np.random.default_rng(sum(map(ord, symbol)))
        today = pd.Timestamp.now(tz=IST).normalize()
        if interval == "1d":
            idx = pd.bdate_range(end=today, periods=max(int(days * 0.69), 3), tz=IST)
            per = 1
        elif interval == "1m":
            ds = pd.bdate_range(end=today, periods=max(min(days, 10) * 5 // 7 + 1, 2), tz=IST)
            idx = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=15 + m) for d in ds for m in range(375)])
            idx = idx[idx <= pd.Timestamp.now(tz=IST)]
            per = 375
        else:
            return pd.DataFrame(columns=EMPTY)           # no 5-minute history offline
        steps = rng.normal(0.0002 / per, 0.01 / np.sqrt(per), len(idx))
        close = 1000 * np.exp(np.cumsum(steps))
        open_ = np.r_[close[0], close[:-1]]
        spread = np.abs(rng.normal(0, 0.003 / np.sqrt(per), len(idx))) * close
        return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread,
                             "low": np.minimum(open_, close) - spread, "close": close,
                             "volume": rng.integers(1000, 5000, len(idx)).astype(float)}, index=idx)


def _reset_secrets():
    """Streamlit parses secrets.toml once per process: forget it so one test's secrets never leak into the next."""
    import streamlit as st
    try:
        st.secrets._reset()
    except Exception:
        pass


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run in an empty folder (holdings.csv, data/ ... are relative paths) with local file storage."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADESATHI_EXTERNAL_ENGINE", "1")
    monkeypatch.setenv("TRADESATHI_STORAGE", "local")
    persist.reset()
    _reset_secrets()
    yield tmp_path
    persist.reset()
    _reset_secrets()


@pytest.fixture
def fake_source():
    return FakeSource()
