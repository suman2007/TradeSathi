"""Holdings input: a simple CSV (symbol,qty,buy_price). Edit it in Excel/Notepad or from the dashboard.
Several rows for the same symbol (bought in lots) are merged into one position at the weighted average price."""
import io
import pandas as pd

from tradesathi.storage import persist

PATH = "holdings.csv"
COLS = ["symbol", "qty", "buy_price"]


def normalize(sym):
    s = str(sym).strip().upper()
    return s if s.endswith((".NS", ".BO")) else s + ".NS"


def read(path=PATH):
    text = persist.read_text(path, seed=path)
    df = pd.read_csv(io.StringIO(text)) if text and text.strip() else pd.DataFrame(columns=COLS)
    df.columns = [str(c).strip().lower() for c in df.columns]
    for c in COLS:
        if c not in df:
            df[c] = None
    df = df[COLS].copy()
    df["symbol"] = df["symbol"].fillna("").astype(str)
    for c in ("qty", "buy_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def save(df, path=PATH):
    df = df.copy()
    df["symbol"] = df["symbol"].fillna("").astype(str).str.strip().str.upper()
    df = df[df["symbol"] != ""].dropna(subset=["qty", "buy_price"])
    persist.write_text(path, df[COLS].to_csv(index=False))


def load(path=PATH, extra=None):
    """Return {SYMBOL.NS: {"qty": q, "buy_price": avg}}. `extra` = ["RELIANCE:10:2450.5", ...] from the CLI."""
    df = read(path)
    rows = []
    for s in extra or []:
        sym, qty, price = s.split(":")
        rows.append({"symbol": sym, "qty": float(qty), "buy_price": float(price)})
    if rows:
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    df = df.dropna()
    df = df[(df["qty"] > 0) & (df["buy_price"] > 0) & (df["symbol"].str.strip() != "")]
    df = df.assign(symbol=df["symbol"].map(normalize))
    out = {}
    for sym, g in df.groupby("symbol"):
        q = float(g["qty"].sum())
        out[sym] = {"qty": int(q) if q.is_integer() else q,
                    "buy_price": float((g["qty"] * g["buy_price"]).sum() / q)}
    return out
