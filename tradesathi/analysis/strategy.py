"""Signal scoring + entry zone / stop / targets for one symbol."""
import numpy as np
import pandas as pd
from tradesathi import config as cfg
from tradesathi.analysis.indicators import add_all
from tradesathi.analysis.patterns import latest


def resample(m1, rule=cfg.INTRA_RULE, now=None):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    bars = m1.resample(rule).agg(agg).dropna(subset=["close"])
    # drop the candle that is still forming so patterns/indicators use closed candles only
    if now is not None and len(bars) and bars.index[-1] + pd.Timedelta(rule) > now:
        bars = bars.iloc[:-1]
    return bars


def key_levels(daily, now):
    last_is_today = daily.index[-1].date() == now.date() and len(daily) > 1
    prev = daily.iloc[-2] if last_is_today else daily.iloc[-1]
    p = (prev.high + prev.low + prev.close) / 3
    lv = [("Prev high", prev.high), ("Prev low", prev.low), ("Pivot", p),
          ("S1", 2 * p - prev.high), ("R1", 2 * p - prev.low)]
    y = daily.iloc[-252:]
    lv += [("52w high", y.high.max()), ("52w low", y.low.min())]
    d = daily.iloc[-120:]
    hi_fr = d.high == d.high.rolling(7, center=True).max()
    lo_fr = d.low == d.low.rolling(7, center=True).min()
    lv += [("Swing high", v) for v in d.high[hi_fr]] + [("Swing low", v) for v in d.low[lo_fr]]
    return [(n, float(v)) for n, v in lv if pd.notna(v)]


STRUCTURAL = ("Swing high", "52w high")


def nearest_levels(levels, price):
    """Support = nearest level of any kind below price (prev low, pivots, swings...).
    Resistance = nearest STRUCTURAL high above price. Yesterday's high / pivots are intraday noise that would
    veto almost every multi-day trade, so they are not used as the resistance filter."""
    below = [v for _, v in levels if v < price]
    above = [v for n, v in levels if v > price and n in STRUCTURAL]
    return (max(below) if below else None), (min(above) if above else None)


def _r(x):
    return None if x is None or pd.isna(x) else round(float(x), 2)


def analyze(symbol, daily, m1, now):
    bars = resample(m1, cfg.INTRA_RULE, now)
    if len(bars) < 30 or len(daily) < 60:
        raise ValueError(f"not enough data yet (intraday bars={len(bars)}, daily={len(daily)})")
    b, d = add_all(bars, intraday=True), add_all(daily)
    price = float(m1["close"].iloc[-1])
    x, dl = b.iloc[-1], d.iloc[-1]
    atr_v = float(dl["atr"] if cfg.ATR_SOURCE == "daily" else x["atr"])

    score, why = 0.0, []

    def add(pts, text):
        nonlocal score
        score += pts
        why.append(f"{pts:+g}  {text}")

    def check(cond, up, down):
        add(1 if cond else -1, up if cond else down)

    # --- daily trend ---
    check(dl.close > dl.ema50, "Daily close above EMA50", "Daily close below EMA50")
    check(dl.ema20 > dl.ema50, "Daily EMA20 above EMA50", "Daily EMA20 below EMA50")
    if len(daily) >= 200:
        check(dl.close > dl.ema200, "Daily close above EMA200", "Daily close below EMA200")
    # --- intraday momentum ---
    check(price > x.vwap, "Price above VWAP", "Price below VWAP")
    check(x.ema20 > x.ema50, "5m EMA20 above EMA50", "5m EMA20 below EMA50")
    check(x.macd_hist > 0, "5m MACD histogram positive", "5m MACD histogram negative")
    if 45 <= x.rsi <= 70:
        add(1, f"RSI healthy ({x.rsi:.0f})")
    elif x.rsi > 75:
        add(-1, f"RSI overbought ({x.rsi:.0f})")
    elif x.rsi < 40:
        add(-1, f"RSI weak ({x.rsi:.0f})")
    # --- volume ---
    if pd.notna(x.vol_avg20) and x.vol_avg20 > 0 and x.volume / x.vol_avg20 > 1.5:
        add(1 if x.close >= x.open else -1, "Volume spike on " + ("green" if x.close >= x.open else "red") + " candle")
    # --- candlestick patterns (intraday and daily) ---
    pats_i, pats_d = latest(b, 2), latest(d, 1)
    for tag, pats in (("5m", pats_i), ("daily", pats_d)):
        net = max(-1, min(1, sum(p["bias"] for p in pats)))
        if net:
            add(net, f"{tag} pattern: " + ", ".join(p["name"] for p in pats if p["bias"]))

    signal = "BUY" if score >= cfg.BUY_SCORE else "AVOID" if score <= cfg.AVOID_SCORE else "WAIT"

    # --- levels ---
    levels = key_levels(daily, now)
    support, resistance = nearest_levels(levels, price)
    support = support if support is not None else price - 2 * atr_v

    # --- entry zone: pullback anchor = highest of support / VWAP / EMA20 that is near and below price ---
    cands = [v for v in (support, x.vwap, x.ema20) if pd.notna(v) and price - 1.5 * atr_v <= v <= price * 1.001]
    anchor = max(cands) if cands else price
    band = cfg.ENTRY_BAND * atr_v
    lo_e, hi_e = anchor - band, anchor + band
    entry = min(max(price, lo_e), hi_e)
    status = "IN_ZONE" if lo_e <= price <= hi_e else "WAIT_PULLBACK" if price > hi_e else "BELOW_ZONE"

    # --- stop and targets ---
    stop = max(support - band, entry - cfg.ATR_STOP_MULT * atr_v)         # the tighter of the two
    stop = min(stop, entry - cfg.ATR_MIN_STOP_MULT * atr_v)                # but outside noise
    risk = entry - stop
    t1, t2 = entry + cfg.T1_R * risk, entry + cfg.T2_R * risk
    rr_res = (resistance - entry) / risk if resistance else None
    valid = signal == "BUY" and (rr_res is None or rr_res >= cfg.MIN_RR)

    if signal != "BUY":
        action = signal
    elif not valid:
        action = "SKIP (resistance too close)"
    else:
        action = {"IN_ZONE": "BUY NOW (in zone)", "WAIT_PULLBACK": "WAIT FOR PULLBACK",
                  "BELOW_ZONE": "WAIT (below zone)"}[status]

    return {
        "symbol": symbol, "price": _r(price), "signal": signal, "action": action, "valid": bool(valid),
        "score": score, "why": why,
        "atr": _r(atr_v), "rsi": _r(x.rsi), "vwap": _r(x.vwap), "ema20_5m": _r(x.ema20),
        "support": _r(support), "resistance": _r(resistance),
        "entry_low": _r(lo_e), "entry_high": _r(hi_e), "entry": _r(entry), "zone_status": status,
        "stop": _r(stop), "t1": _r(t1), "t2": _r(t2), "rr_to_resistance": _r(rr_res),
        "patterns": pats_i + pats_d,
    }
