"""Paper trading: every live BUY NOW is logged as if you had bought it, then followed minute by minute with the
same exit rules as the backtest (stop first, half at T1 + stop to breakeven, rest at T2, time exit).
Signals blocked by the history gate are logged too (gated=True), so the raw live signal can be judged on its own.
After a few months, data/paper_trades.csv shows whether the live signal really makes money. No orders are placed."""
import io

import numpy as np
import pandas as pd

from tradesathi import config as cfg
from tradesathi.storage import persist

NAME = "paper_trades.csv"
COLS = ["symbol", "opened", "entry", "stop", "t1", "t2", "risk", "score", "gated", "edge", "status", "stop_now",
        "t1_hit", "realized", "days_held", "last_close", "checked_until", "exit_time", "exit_price", "reason", "r"]


def load(store):
    text = persist.read_text(store.key(NAME))
    if not text or not text.strip():
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(io.StringIO(text), dtype=object)       # object: empty columns (exit_time...) would load as float64 and reject text
    for c in COLS:
        if c not in df:
            df[c] = None
    return df[COLS].astype(object)


def save(store, df):
    persist.write_text(store.key(NAME), df.to_csv(index=False))


def record(df, res, now):
    """Open a paper trade when the live engine says BUY NOW (one open paper trade per share)."""
    if not res.get("action_before_gate", res["action"]).startswith("BUY NOW"):
        return df
    if ((df["symbol"] == res["symbol"]) & (df["status"] == "open")).any():
        return df
    h = res.get("history") or {}
    row = {c: None for c in COLS}
    row.update(symbol=res["symbol"], opened=now.isoformat(), entry=res["entry"], stop=res["stop"], t1=res["t1"],
               t2=res["t2"], risk=round(res["entry"] - res["stop"], 4), score=res["score"], gated=not res["valid"],
               edge=h.get("edge"), status="open", stop_now=res["stop"], t1_hit=False, realized=0.0, days_held=0,
               last_close=res["entry"], checked_until=now.isoformat())
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True) if len(df) else pd.DataFrame([row], columns=COLS)


def _close(df, i, ts, px, reason, remaining):
    t = df.loc[i]
    realized = float(t["realized"]) + remaining * (px - float(t["entry"]))
    df.loc[i, ["status", "exit_time", "exit_price", "reason", "realized"]] = ["closed", str(ts), round(px, 2), reason, realized]
    df.loc[i, "r"] = round((realized - float(t["entry"]) * cfg.CHARGES_PCT) / float(t["risk"]), 3)


def update(df, store, src, now, refreshed=()):
    """Walk every open paper trade through the 1-minute candles that closed since the last check."""
    for i in df.index[df["status"] == "open"]:
        t = df.loc[i]
        sym, since = t["symbol"], pd.Timestamp(t["checked_until"])
        gap_days = (now.date() - since.date()).days
        if sym not in refreshed or gap_days > 1:            # engine was off, or the share is no longer watched
            new = src.fetch(sym, "1m", f"{min(gap_days + 1, 25)}d")
            if not new.empty:
                store.merge(sym, "1m", new, keep_days=10)
        m1 = store.load(sym, "1m")
        bars = m1[(m1.index > since) & (m1.index <= now - pd.Timedelta(minutes=1))]
        entry, stop, t1, t2 = (float(t[c]) for c in ("entry", "stop_now", "t1", "t2"))
        t1_hit, days, last_close = str(t["t1_hit"]) == "True", int(t["days_held"]), float(t["last_close"])
        remaining = 0.5 if t1_hit else 1.0
        last_day = since.date()
        for ts, b in bars.iterrows():
            if ts.date() != last_day:                       # a new trading day
                days, last_day = days + 1, ts.date()
                if days > cfg.BT_MAX_HOLD:                  # time exit at the previous session's close
                    _close(df, i, ts, last_close, "time", remaining)
                    break
            if b["low"] <= stop:
                _close(df, i, ts, min(b["open"], stop), "stop after T1" if t1_hit else "stop", remaining)
                break
            if not t1_hit and b["high"] >= t1:
                df.loc[i, "realized"] = 0.5 * (max(b["open"], t1) - entry)
                t1_hit, remaining = True, 0.5
                stop = max(stop, entry * (1 + cfg.CHARGES_PCT))
            if t1_hit and b["high"] >= t2:
                _close(df, i, ts, max(b["open"], t2), "T2", remaining)
                break
            last_close = float(b["close"])
        if df.loc[i, "status"] == "open":
            df.loc[i, ["stop_now", "t1_hit", "days_held", "last_close"]] = [stop, t1_hit, days, last_close]
            if len(bars):
                df.loc[i, "checked_until"] = str(bars.index[-1])
    return df


def summary(df):
    """Closed paper-trade results, all signals and only those the history gate allowed."""
    out = {"open": int((df["status"] == "open").sum())}
    closed = df[df["status"] == "closed"]
    for label, part in (("all", closed), ("allowed", closed[closed["gated"].astype(str) != "True"])):
        r = part["r"].astype(float).to_numpy()
        out[label] = {"trades": len(r), "win_rate": round(float((r > 0).mean() * 100), 1) if len(r) else 0.0,
                      "expectancy_r": round(float(r.mean()), 3) if len(r) else 0.0,
                      "t_stat": round(float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))), 2)
                      if len(r) > 2 and r.std() > 0 else 0.0}
    return out
