"""Backtest the entry / stop / target rules on past candles.

Two modes:
  daily     the score rebuilt from DAILY candles (VWAP / 5-min inputs replaced by daily equivalents). Long history.
  intraday  the REAL live rules replayed on 5-minute candles (Upstox history, from Jan 2022): 5-min VWAP, EMAs,
            MACD, RSI, volume, patterns, plus today's daily trend as it looked at that moment. This tests what the
            live engine actually tells you to do.

No look-ahead: a signal comes from a closed candle, the buy order is live from the next candle.
Conservative fills: if stop and target are both inside one candle the stop is assumed to hit first;
the trade that fills on a candle can only be stopped out on that candle, not take profit.
Exit plan = same as live: half off at T1 (stop moves to breakeven), rest at T2, or time exit.

  python -m tradesathi.analysis.backtest --symbols RELIANCE TCS INFY --years 5
  python -m tradesathi.analysis.backtest --universe nifty50 --intraday          (broad, less biased test)
"""
import argparse
import datetime as dt
import io
import os
import urllib.request

import numpy as np
import pandas as pd

from tradesathi import config as cfg
from tradesathi.analysis.indicators import add_all
from tradesathi.analysis.patterns import BIAS, detect, rules
from tradesathi.analysis.strategy import key_levels, nearest_levels

_tried = set()
FIRST_5M = dt.date(2022, 1, 1)          # Upstox minute history starts here


# ----------------------------------------------------------------- data
def ensure_daily(src, store, sym, refresh_recent=False):
    """Make sure the stored daily file holds long history (10y); afterwards only top up the newest days."""
    d = store.load(sym, "1d")
    if d.empty or (sym not in _tried and len(d) < cfg.MIN_HISTORY_BARS):
        _tried.add(sym)
        period = cfg.HISTORY_PERIOD
    elif refresh_recent:
        period = "10d"
    else:
        return d
    nd = src.fetch(sym, "1d", period)
    return store.merge(sym, "1d", nd) if not nd.empty else d


def ensure_5m(src, store, sym, now):
    """Keep a long 5-minute history file (first run downloads everything since 2022, later runs only the gap)."""
    m5 = store.load(sym, "5m")
    last = m5.index[-1].date() if len(m5) else FIRST_5M - dt.timedelta(days=1)
    if last >= now.date() - dt.timedelta(days=1):
        return m5
    new = src.fetch(sym, "5m", f"{(now.date() - last).days + 1}d")
    return store.merge(sym, "5m", new) if not new.empty else m5


def completed(daily, now):
    """Drop today's still-forming daily candle."""
    if len(daily) and daily.index[-1].date() == now.date() and now.time() < dt.time(15, 30):
        return daily.iloc[:-1]
    return daily


def universe(name):
    """Current members of an NSE index, e.g. nifty50, nifty100, nifty500 (official list from NSE)."""
    url = f"https://archives.nseindia.com/content/indices/ind_{name.lower()}list.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        df = pd.read_csv(io.StringIO(r.read().decode()))
    return [s + ".NS" for s in df["Symbol"].str.strip()]


# ----------------------------------------------------------------- signal (daily version of the live score)
def daily_scores(d):
    """Score per bar from data up to that bar only (all indicators are causal). Max about +8 / min -8.
    Intraday-only inputs (VWAP, 5-min EMAs) are replaced by close-vs-EMA20, which is the daily equivalent."""
    s = pd.Series(0.0, index=d.index)
    s += np.where(d["close"] > d["ema50"], 1, -1)
    s += np.where(d["ema20"] > d["ema50"], 1, -1)
    s += np.where(d["close"] > d["ema200"], 1, -1)
    s += np.where(d["close"] > d["ema20"], 1, -1)
    s += np.where(d["macd_hist"] > 0, 1, -1)
    r = d["rsi"]
    s += np.where((r >= 45) & (r <= 70), 1, 0) + np.where(r > 75, -1, 0) + np.where(r < 40, -1, 0)
    vr = d["volume"] / d["vol_avg20"]
    s += np.where(vr > 1.5, np.where(d["close"] >= d["open"], 1, -1), 0)
    pat = detect(d)
    net = sum(pat[n].astype(int) * b for n, b in BIAS.items())
    return s + net.clip(-1, 1)


# ----------------------------------------------------------------- signal (intraday = the live score)
def intraday_scores(b, d):
    """Live score (strategy.analyze) for every closed 5-min bar, vectorised.
    b = 5-min bars with add_all(intraday=True); d = completed daily bars with add_all().
    Daily inputs are what analyze() sees during the session: EMAs updated with the current price, and today's
    forming daily candle for the daily pattern. Daily ATR is yesterday's value (today's is still forming)."""
    price = b["close"]
    day = b.index.normalize()
    pos = d.index.searchsorted(day, side="left") - 1            # previous completed daily bar
    ok = pos >= 0
    pos = np.clip(pos, 0, None)

    def prev(col, back=0):
        p = np.clip(pos - back, 0, None)
        return pd.Series(d[col].to_numpy()[p], index=b.index)

    e = {n: prev(f"ema{n}") + 2 / (n + 1) * (price - prev(f"ema{n}")) for n in (20, 50, 200)}
    s = pd.Series(0.0, index=b.index)
    s += np.where(price > e[50], 1, -1)
    s += np.where(e[20] > e[50], 1, -1)
    s += np.where(pd.Series(pos, index=b.index) >= 199, np.where(price > e[200], 1, -1), 0)
    s += np.where(price > b["vwap"], 1, -1)
    s += np.where(b["ema20"] > b["ema50"], 1, -1)
    s += np.where(b["macd_hist"] > 0, 1, -1)
    r = b["rsi"]
    s += np.where((r >= 45) & (r <= 70), 1, 0) + np.where(r > 75, -1, 0) + np.where(r < 40, -1, 0)
    vr = b["volume"] / b["vol_avg20"]
    s += np.where((b["vol_avg20"] > 0) & (vr > 1.5), np.where(b["close"] >= b["open"], 1, -1), 0)
    pat = detect(b)
    pat = pat | pat.shift(1, fill_value=False)                  # live looks at the last 2 closed 5-min candles
    s += sum(pat[n].astype(int) * v for n, v in BIAS.items()).clip(-1, 1)
    g = b.groupby(day)
    dp = rules(g["open"].transform("first"), g["high"].cummax(), g["low"].cummin(), price,
               prev("open"), prev("close"), prev("open", 1), prev("close", 1))
    s += sum(dp[n].astype(int) * v for n, v in BIAS.items()).clip(-1, 1)
    s[~ok] = np.nan
    return s, prev("atr").to_numpy(), pos


# ----------------------------------------------------------------- simulation
def _walk(O, H, L, C, j0, end, entry, stop, t1, t2, data_end=False):
    """Manage one trade from fill bar j0 to bar `end` (time exit). Returns (realized per share, reason, exit bar)."""
    realized, remaining, t1_done, stop_cur, reason = 0.0, 1.0, False, stop, "time"
    for k in range(j0, end + 1):
        if L[k] <= stop_cur:                                   # stop first (conservative)
            px = O[k] if (k > j0 and O[k] <= stop_cur) else stop_cur   # gap below stop fills at the open
            realized += remaining * (px - entry)
            reason, end = ("stop after T1" if t1_done else "stop"), k
            remaining = 0.0
            break
        if k > j0:
            if not t1_done and H[k] >= t1:
                px = O[k] if O[k] >= t1 else t1
                realized += 0.5 * (px - entry)
                remaining, t1_done = 0.5, True
                stop_cur = max(stop_cur, entry * (1 + cfg.CHARGES_PCT))   # breakeven
            if t1_done and H[k] >= t2:
                px = O[k] if O[k] >= t2 else t2
                realized += remaining * (px - entry)
                reason, end, remaining = "T2", k, 0.0
                break
    if remaining > 0:                                          # time exit
        realized += remaining * (C[end] - entry)
        reason = "open at data end" if data_end else "time"
    return realized, reason, end


def _fill(O, L, a, b, limit, stop):
    """First bar in [a, b) where a buy limit fills. Returns (bar, price) or None (also None on a gap below the stop)."""
    if a >= b:
        return None
    hit = L[a:b] <= limit
    if not hit.any():
        return None
    j = a + int(np.argmax(hit))
    if O[j] <= limit:
        return (j, O[j]) if O[j] > stop else None
    return j, limit


def _trade(idx, sc, entry, stop, risk, t1, t2, j0, end, realized, reason, i, days):
    return {
        "signal_date": str(idx[i]), "entry_date": str(idx[j0]), "exit_date": str(idx[end]),
        "score": float(sc), "entry": round(float(entry), 2), "stop": round(float(stop), 2),
        "t1": round(float(t1), 2), "t2": round(float(t2), 2), "pnl_per_share": round(float(realized), 2),
        "r": float((realized - entry * cfg.CHARGES_PCT) / risk),
        "ret_pct": float(realized / entry - cfg.CHARGES_PCT), "risk_pct": float(risk / entry),
        "reason": reason, "bars": int(days)}


def _plan(price, atr, support, resistance, anchors):
    """Live entry / stop / targets (same formulas as strategy.analyze). None when resistance is too close."""
    support = support if support is not None else price - 2 * atr
    cands = [v for v in [support] + anchors if pd.notna(v) and price - 1.5 * atr <= v <= price * 1.001]
    anchor = max(cands) if cands else price
    band = cfg.ENTRY_BAND * atr
    lo, hi = anchor - band, anchor + band
    limit = min(max(price, lo), hi)
    stop = max(support - band, limit - cfg.ATR_STOP_MULT * atr)
    stop = min(stop, limit - cfg.ATR_MIN_STOP_MULT * atr)
    risk = limit - stop
    if risk <= 0 or (resistance is not None and (resistance - limit) / risk < cfg.MIN_RR):
        return None
    status = "IN_ZONE" if lo <= price <= hi else "WAIT_PULLBACK" if price > hi else "BELOW_ZONE"
    return limit, stop, risk, limit + cfg.T1_R * risk, limit + cfg.T2_R * risk, status


def run(daily, years=cfg.BACKTEST_YEARS, buy_score=cfg.BUY_SCORE):
    """Daily-candle backtest."""
    n = len(daily)
    empty = {"metrics": metrics([], 0, None), "trades": []}
    if n < 260:
        return empty
    cutoff = daily.index[-1] - pd.Timedelta(days=int(years * 365.25))
    start = max(200, int(daily.index.searchsorted(cutoff)))     # first 200 bars = indicator warm-up
    if start >= n - 30:
        return empty
    d = add_all(daily)
    sc = daily_scores(d).to_numpy()
    O, H, L, C = (daily[c].to_numpy(float) for c in ("open", "high", "low", "close"))
    ema20, atr_a, idx = d["ema20"].to_numpy(), d["atr"].to_numpy(), daily.index
    trades, i = [], start
    while i < n - 1:
        if sc[i] < buy_score or not atr_a[i] > 0:
            i += 1
            continue
        price = C[i]
        lv = key_levels(daily.iloc[: i + 1], idx[i] + pd.Timedelta(days=1))
        p = _plan(price, atr_a[i], *nearest_levels(lv, price), [ema20[i]])
        if p is None:
            i += 1
            continue
        limit, stop, risk, t1, t2, _ = p
        fill = _fill(O, L, i + 1, min(i + 1 + cfg.BT_MAX_WAIT, n), limit, stop)
        if fill is None:
            i += 1
            continue
        j0, entry = fill
        end = min(j0 + cfg.BT_MAX_HOLD, n - 1)
        realized, reason, end = _walk(O, H, L, C, j0, end, entry, stop, t1, t2, j0 + cfg.BT_MAX_HOLD > n - 1)
        trades.append(_trade(idx, sc[i], entry, stop, risk, t1, t2, j0, end, realized, reason, i, end - j0))
        i = end + 1                                             # one position at a time per share
    span = (idx[-1] - idx[start]).days / 365.25
    out = {"metrics": metrics(trades, span, C[-1] / C[start] - 1), "trades": trades}
    out["metrics"]["method"] = "daily"
    return out


def intraday_run(m5, daily, years=cfg.BACKTEST_YEARS, buy_score=cfg.BUY_SCORE):
    """The live rules replayed on 5-minute candles. Holding / waiting limits are counted in trading days."""
    empty = {"metrics": dict(metrics([], 0, None), method="intraday 5m"), "trades": []}
    daily = daily.copy()
    daily.index = daily.index.normalize()
    m5 = m5[m5.index.normalize() > daily.index[0]] if len(daily) else m5
    if len(m5) < 2000 or len(daily) < 260:
        return empty
    b, d = add_all(m5, intraday=True), add_all(daily)
    sc, atr_a, pos = intraday_scores(b, d)
    sc = sc.to_numpy()
    O, H, L, C = (m5[c].to_numpy(float) for c in ("open", "high", "low", "close"))
    vwap, ema20, idx = b["vwap"].to_numpy(), b["ema20"].to_numpy(), m5.index
    day_id = pd.factorize(idx.normalize())[0]
    ndays = day_id[-1] + 1
    last_bar = np.searchsorted(day_id, np.arange(ndays), side="right") - 1   # last 5-min bar of each day
    cutoff = idx[-1] - pd.Timedelta(days=int(years * 365.25))
    start = max(100, int(idx.searchsorted(cutoff)))
    n, trades, levels = len(m5), [], {}
    cand = np.flatnonzero((sc >= buy_score) & (atr_a > 0))
    cand, nxt = cand[cand >= start], start
    for i in cand:
        if i < nxt or i >= n - 1:
            continue
        price, atr = C[i], atr_a[i]
        if pos[i] not in levels:
            levels[pos[i]] = key_levels(daily.iloc[: pos[i] + 1], idx[i])
        p = _plan(price, atr, *nearest_levels(levels[pos[i]], price), [vwap[i], ema20[i]])
        if p is None or p[5] == "BELOW_ZONE":
            continue
        limit, stop, risk, t1, t2, _ = p
        wait_end = last_bar[min(day_id[i] + cfg.BT_MAX_WAIT, ndays - 1)] + 1
        fill = _fill(O, L, i + 1, wait_end, limit, stop)
        if fill is None:
            continue
        j0, entry = fill
        end = last_bar[min(day_id[j0] + cfg.BT_MAX_HOLD, ndays - 1)]
        realized, reason, end = _walk(O, H, L, C, j0, end, entry, stop, t1, t2,
                                      day_id[j0] + cfg.BT_MAX_HOLD > ndays - 1)
        trades.append(_trade(idx, sc[i], entry, stop, risk, t1, t2, j0, end, realized, reason, i,
                             day_id[end] - day_id[j0]))
        nxt = end + 1
    span = (idx[-1] - idx[start]).days / 365.25
    out = {"metrics": metrics(trades, span, C[-1] / C[start] - 1), "trades": trades}
    out["metrics"]["method"] = "intraday 5m"
    return out


# ----------------------------------------------------------------- statistics
def tstat(r):
    r = np.asarray(r, float)
    return round(float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))), 2) if len(r) > 2 and r.std() > 0 else 0.0


def metrics(trades, span_years, buy_hold):
    m = {"trades": len(trades), "years": round(float(span_years), 2)}
    if buy_hold is not None and span_years > 0:
        m["buy_hold_pct"] = round(float(buy_hold) * 100, 1)
        m["buy_hold_cagr_pct"] = round(float(((1 + buy_hold) ** (1 / span_years) - 1) * 100), 1)
    if not trades:
        m.update(win_rate=0.0, avg_win_r=0.0, avg_loss_r=0.0, expectancy_r=0.0, profit_factor=None,
                 total_return_pct=0.0, cagr_pct=0.0, max_dd_pct=0.0, avg_bars=0.0, t_stat=0.0)
        m["edge"] = "INSUFFICIENT DATA"
        return m
    r = np.array([t["r"] for t in trades])
    w = r > 0
    gl = -r[~w].sum()
    eq = peak = 1.0
    mdd = 0.0
    for t in sorted(trades, key=lambda t: t["exit_date"]):      # position-sized equity curve
        f = min(cfg.RISK_PCT / t["risk_pct"], cfg.MAX_ALLOC)
        eq *= 1 + f * t["ret_pct"]
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    m.update(
        win_rate=round(float(w.mean() * 100), 1),
        avg_win_r=round(float(r[w].mean()), 2) if w.any() else 0.0,
        avg_loss_r=round(float(r[~w].mean()), 2) if (~w).any() else 0.0,
        expectancy_r=round(float(r.mean()), 3),
        profit_factor=round(float(r[w].sum() / gl), 2) if gl > 0 else None,
        total_return_pct=round((eq - 1) * 100, 1),
        cagr_pct=round(float((eq ** (1 / span_years) - 1) * 100), 1) if span_years > 0 else None,
        t_stat=tstat(r), max_dd_pct=round(mdd * 100, 1), avg_bars=round(float(np.mean([t["bars"] for t in trades])), 1))
    m["edge"] = edge_label(m)
    return m


def edge_label(m):
    if m["trades"] < cfg.MIN_TRADES:
        return "INSUFFICIENT DATA"
    pf = m["profit_factor"] if m["profit_factor"] is not None else 99
    # t-stat guards against luck: ~50 trades of a coin-flip system can easily show +0.2R by chance
    if m["expectancy_r"] >= cfg.EDGE_MIN_EXPECTANCY and pf >= cfg.EDGE_MIN_PF and m["t_stat"] >= cfg.EDGE_MIN_TSTAT:
        return "EDGE"
    return "WEAK EDGE" if m["expectancy_r"] > 0 else "NO EDGE"


def run_symbol(src, store, sym, now, years=cfg.BACKTEST_YEARS, buy_score=cfg.BUY_SCORE, intraday=False):
    d = completed(ensure_daily(src, store, sym), now)
    if intraday:
        m5 = ensure_5m(src, store, sym, now)
        m5 = m5[m5.index.date < now.date()]                    # completed sessions only
        return intraday_run(m5, d, years, buy_score)
    return run(d, years, buy_score)


# ----------------------------------------------------------------- command line
def _line(label, ts):
    r = [t["r"] for t in ts]
    if not r:
        return f"{label:<26} no trades"
    return (f"{label:<26}{len(r):>6} trades | win {np.mean(np.array(r) > 0) * 100:5.1f}% | "
            f"expectancy {np.mean(r):+.3f}R | t-stat {tstat(r):+.2f}")


def main():
    from tradesathi.storage import holdings as hd
    from tradesathi.data_source import get_source
    from tradesathi.storage.store import Store
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--universe", default=None, help="test every current member of an NSE index: nifty50, nifty100, nifty500")
    ap.add_argument("--intraday", action="store_true", help="replay the real live rules on 5-min candles (from 2022)")
    ap.add_argument("--years", type=float, default=cfg.BACKTEST_YEARS)
    ap.add_argument("--buy-score", type=float, default=cfg.BUY_SCORE)
    a = ap.parse_args()
    if a.universe:
        syms = universe(a.universe)
    else:
        syms = [hd.normalize(s) for s in (a.symbols or list(hd.load()) + cfg.DEFAULT_SYMBOLS)]
    syms = list(dict.fromkeys(syms))
    src, store = get_source(), Store()
    now = pd.Timestamp.now(tz=cfg.TZ_NAME)
    allt = []
    mode = "5-MINUTE candles, real live rules" if a.intraday else "DAILY candles"
    print(f"\nBacktest: last {a.years:g} years of {mode}, BUY when score >= {a.buy_score:g}, "
          f"charges {cfg.CHARGES_PCT * 100:.2f}% per round trip\n")
    print(f"{'SYMBOL':<13}{'TRADES':>7}{'WIN%':>7}{'AVG WIN':>9}{'AVG LOSS':>9}{'EXPECT R':>9}{'T':>6}{'PF':>6}"
          f"{'MAXDD%':>8}{'CAGR%':>7}{'B&H CAGR%':>10}  EDGE")
    for sym in syms:
        try:
            res = run_symbol(src, store, sym, now, a.years, a.buy_score, a.intraday)
        except Exception as e:
            print(f"{sym:<13} ERROR: {e}")
            continue
        m = res["metrics"]
        allt += [dict(t, symbol=sym) for t in res["trades"]]
        pf = "-" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
        print(f"{sym:<13}{m['trades']:>7}{m['win_rate']:>7.1f}{m['avg_win_r']:>9.2f}{m['avg_loss_r']:>9.2f}"
              f"{m['expectancy_r']:>9.2f}{m['t_stat']:>6.1f}{pf:>6}{m['max_dd_pct']:>8.1f}{m['cagr_pct'] or 0:>7.1f}"
              f"{m.get('buy_hold_cagr_pct', 0):>10.1f}  {m['edge']}")
    if allt:
        pm = metrics(allt, a.years, None)
        print(f"\nALL SHARES POOLED: {pm['trades']} trades | win {pm['win_rate']}% | expectancy {pm['expectancy_r']:+.3f}R "
              f"| t-stat {pm['t_stat']:+.2f} | profit factor {pm['profit_factor']} | avg hold {pm['avg_bars']} days "
              f"| verdict: {pm['edge']}")
        dates = sorted(t["entry_date"] for t in allt)
        lo, hi = pd.Timestamp(dates[0]), pd.Timestamp(dates[-1])
        mid = str(lo + (hi - lo) / 2)
        print("\nFirst half vs second half of the period - a real edge should show up in BOTH:")
        print(_line(f"  until {mid[:10]}", [t for t in allt if t["entry_date"] < mid]))
        print(_line(f"  after {mid[:10]}", [t for t in allt if t["entry_date"] >= mid]))
        print("\nBy year (pooled, by exit date)  - consistency matters more than the total:")
        for y in sorted({t["exit_date"][:4] for t in allt}):
            print(_line(f"  {y}", [t for t in allt if t["exit_date"][:4] == y]))
        by_reason = pd.Series([t["reason"] for t in allt]).value_counts().to_dict()
        print(f"\nHow trades ended: {by_reason}")
        pd.DataFrame(allt).to_csv(os.path.join(cfg.DATA_DIR, "backtest_trades.csv"), index=False)
        print(f"Every trade saved to {cfg.DATA_DIR}/backtest_trades.csv (open in Excel to inspect)")
    if a.universe:
        print(f"\nNote: {a.universe} = TODAY's members, so shares that dropped out of the index are missing "
              "(survivorship bias makes results look a little better than reality).")
    print("\nResults are net of estimated charges but ignore slippage, taxes and trade overlap across shares. "
          "Past results do not guarantee future results.")


if __name__ == "__main__":
    main()
