"""Shares the user WANTS TO BUY. CSV columns: symbol, amount (rupees to invest) OR qty, optional max_price.
Give amount or qty (qty wins if both). max_price = the most you are willing to pay per share.
Lines starting with # are ignored. Several rows for one symbol are merged (amounts added, lowest max_price kept)."""
import io
import pandas as pd

from tradesathi.storage import persist
from tradesathi.storage.holdings import normalize

PATH = "buylist.csv"
COLS = ["symbol", "amount", "qty", "max_price"]


def read(path=PATH):
    text = persist.read_text(path, seed=path)
    df = pd.read_csv(io.StringIO(text), comment="#") if text and text.strip() else pd.DataFrame(columns=COLS)
    df.columns = [str(c).strip().lower() for c in df.columns]
    for c in COLS:
        if c not in df:
            df[c] = None
    df = df[COLS].copy()
    df["symbol"] = df["symbol"].fillna("").astype(str)
    for c in COLS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def save(df, path=PATH):
    df = df.copy()
    df["symbol"] = df["symbol"].fillna("").astype(str).str.strip().str.upper()
    df = df[(df["symbol"] != "") & (df["amount"].notna() | df["qty"].notna())]
    persist.write_text(path, df[COLS].to_csv(index=False))


def load(path=PATH, extra=None):
    """Return {SYMBOL.NS: {"amount": rupees|None, "qty": int|None, "max_price": float|None}}.
    `extra` = ["RELIANCE:50000", "TCS:30000:3900"] (SYMBOL:AMOUNT[:MAXPRICE]) from the command line."""
    df = read(path)
    nan = float("nan")
    rows = []
    for s in extra or []:
        p = s.split(":")
        rows.append({"symbol": p[0], "amount": float(p[1]), "qty": nan,
                     "max_price": float(p[2]) if len(p) > 2 and p[2] else nan})
    if rows:
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df = df[df["symbol"] != ""]
    df = df[(df["amount"].fillna(0) > 0) | (df["qty"].fillna(0) > 0)]
    df = df.assign(symbol=df["symbol"].map(normalize))
    out = {}
    for sym, g in df.groupby("symbol", sort=False):
        amt, q, mp = g["amount"].sum(min_count=1), g["qty"].sum(min_count=1), g["max_price"].min()
        out[sym] = {"amount": None if pd.isna(amt) else float(amt),
                    "qty": None if pd.isna(q) else int(q),
                    "max_price": None if pd.isna(mp) or mp <= 0 else float(mp)}
    return out
