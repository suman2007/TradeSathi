"""Candlestick pattern detection (pure pandas, no TA-Lib needed)."""
import numpy as np
import pandas as pd

BIAS = {"Hammer": 1, "Bullish engulfing": 1, "Morning star": 1,
        "Shooting star": -1, "Bearish engulfing": -1, "Evening star": -1, "Doji": 0}


def detect(df):
    o, c = df["open"], df["close"]
    return rules(o, df["high"], df["low"], c, o.shift(1), c.shift(1), o.shift(2), c.shift(2))


def rules(o, h, l, c, o1, c1, o2, c2):
    """Pattern flags for candle (o,h,l,c) given the two candles before it (o1,c1 = previous, o2,c2 = the one before)."""
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    b1, b2 = (c1 - o1).abs(), (c2 - o2).abs()
    p = pd.DataFrame(index=o.index)
    p["Doji"] = body <= 0.1 * rng
    p["Hammer"] = (body > 0) & (lower >= 2 * body) & (upper <= 0.1 * rng)
    p["Shooting star"] = (body > 0) & (upper >= 2 * body) & (lower <= 0.1 * rng)
    p["Bullish engulfing"] = (c1 < o1) & (c > o) & (o <= c1) & (c >= o1)
    p["Bearish engulfing"] = (c1 > o1) & (c < o) & (o >= c1) & (c <= o1)
    p["Morning star"] = (c2 < o2) & (b1 <= 0.3 * b2) & (c > o) & (c > (o2 + c2) / 2)
    p["Evening star"] = (c2 > o2) & (b1 <= 0.3 * b2) & (c < o) & (c < (o2 + c2) / 2)
    return p.fillna(False).astype(bool)


def latest(df, lookback=2):
    p = detect(df).iloc[-lookback:]
    return [{"name": n, "bias": BIAS[n]} for n in p.columns if p[n].any()]
