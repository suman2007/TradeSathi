"""Public (visitor) mode: private lists per browser tab on top of a shared, anonymous price cache.

Shared by everyone (contains nothing personal): candles and the per-share analysis in `ResultCache`.
Private to one visitor (kept in their Streamlit session only, never written to disk): holdings, buy list, trailing
stops and buy-confirmation counters. Shares are analysed in background threads, so a visitor's page never blocks."""
import copy
import datetime as dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from tradesathi import config as cfg
from tradesathi.analysis.allocator import allocate
from tradesathi.analysis.positions import summarize
from tradesathi.main import IST, analyze_symbol, manage_holdings, plan_buys
from tradesathi.storage import buylist as bl
from tradesathi.storage import holdings as hd

_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
MAX_UPLOAD_BYTES = 200_000


# ------------------------------------------------------------------ symbols
def validate_symbol(raw):
    """'tcs' / 'TCS.NS' / 'RELIANCE.BO' -> 'TCS.NS' / 'RELIANCE.BO'. Raises ValueError for anything else.
    Strict on purpose: the symbol becomes part of a file name on the server."""
    s = str(raw).strip().upper()
    base, _, suffix = s.partition(".")
    if suffix not in ("", "NS", "BO") or not _SYMBOL.match(base):
        raise ValueError(f"'{str(raw)[:25]}' is not a valid NSE/BSE symbol (letters and digits only, e.g. RELIANCE or TCS.NS)")
    return f"{base}.{suffix or 'NS'}"


def default_symbols():
    """Normalised symbols of the built-in default buy list."""
    return {validate_symbol(sym) for sym, _ in cfg.PUBLIC_DEFAULT_BUYLIST}


def default_buylist():
    """The built-in buy list as a tidy DataFrame (same columns as a buy list file)."""
    rows = [{"symbol": sym, "amount": None, "qty": float(qty), "max_price": None} for sym, qty in cfg.PUBLIC_DEFAULT_BUYLIST]
    return bl.clean(pd.DataFrame(rows, columns=bl.COLS))


# ------------------------------------------------------------------ shared analysis cache
class ResultCache:
    """One analysed result per share, refreshed in the background and reused by every visitor.
    get() never blocks: it returns the latest result (possibly slightly stale), an error, or 'pending'."""

    ERROR_TTL = 600

    def __init__(self, src, store, ttl_open=None, ttl_closed=None, max_total=None, workers=2, sync=False,
                 clock=time.time):
        self.src, self.store, self.sync, self.clock = src, store, sync, clock
        self.ttl_open = cfg.REFRESH_SECONDS if ttl_open is None else ttl_open
        self.ttl_closed = cfg.VISITOR_TTL_CLOSED if ttl_closed is None else ttl_closed
        self.max_total = cfg.MAX_TOTAL_SYMBOLS if max_total is None else max_total
        self._lock = threading.RLock()
        self._ok, self._bad, self._busy = {}, {}, set()
        self._pool = None if sync else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tradesathi-visitor")

    def _ttl(self):
        return self.ttl_open if cfg.market_open(dt.datetime.now(IST)) else self.ttl_closed

    def get(self, sym):
        """-> {"status": "ok"|"pending"|"error", "res": result|None, "err": text|None, "t": epoch seconds|None}"""
        start = False
        with self._lock:
            now_t = self.clock()
            bad = self._bad.get(sym)
            if bad and now_t - bad[1] < self.ERROR_TTL:
                return {"status": "error", "res": None, "err": bad[0], "t": bad[1]}
            entry = self._ok.get(sym)
            if (entry is None or now_t - entry["t"] > self._ttl()) and sym not in self._busy:
                if entry is None and len(self._ok) + len(self._busy) >= self.max_total:
                    return {"status": "error", "res": None, "t": None,
                            "err": "The server is tracking as many shares as it can right now. Try again later."}
                self._busy.add(sym)
                start = True
            out = ({"status": "ok", "res": entry["res"], "err": None, "t": entry["t"]} if entry
                   else {"status": "pending", "res": None, "err": None, "t": None})
        if start:
            if self.sync:
                self._work(sym)
                return self.get(sym)
            self._pool.submit(self._work, sym)
        return out

    def _work(self, sym):
        try:
            with self._lock:
                entry = self._ok.get(sym)
                daily_due = entry is None or self.clock() - entry.get("daily_t", 0) > 900
            now = dt.datetime.now(IST)
            res = analyze_symbol(self.src, self.store, sym, now, daily_due)
            with self._lock:
                t = self.clock()
                self._ok[sym] = {"res": res, "t": t, "daily_t": t if daily_due else (entry or {}).get("daily_t", 0)}
                self._bad.pop(sym, None)
        except Exception as e:
            with self._lock:
                if sym not in self._ok:                     # keep serving the old result if we have one
                    self._bad[sym] = (str(e) or type(e).__name__, self.clock())
        finally:
            with self._lock:
                self._busy.discard(sym)

    def idle(self):
        with self._lock:
            return not self._busy


_default = None
_default_lock = threading.Lock()


def shared_cache():
    """The one ResultCache of this server process (built on first use with the real data sources)."""
    global _default
    with _default_lock:
        if _default is None:
            from tradesathi.data_source import get_source
            from tradesathi.storage.store import Store
            _default = ResultCache(get_source(), Store())
        return _default


def set_shared_cache(cache):
    """Replace the shared cache (tests)."""
    global _default
    with _default_lock:
        _default = cache


# ------------------------------------------------------------------ one visitor's view
def new_state():
    return {"stamp": None, "prev": {"stops": {}, "buy": {}}, "cur": {"stops": {}, "buy": {}}, "t0": None}


def compute(cache, held, wants, now, state, capital=None):
    """The same dict the owner engine writes to signals.json, built for ONE visitor from their own lists.
    `held` / `wants` as returned by holdings.from_df / buylist.from_df. `state` is that visitor's private memory
    (trailing stops, buy-confirmation counters); it advances only when the shared data has refreshed, so a page
    refresh is not mistaken for a new engine cycle."""
    capital = cfg.DEFAULT_CAPITAL if capital is None else capital
    watch = list(dict.fromkeys(list(held) + list(wants)))
    results, by_sym, pending, stamps = [], {}, [], []
    for sym in watch:
        r = cache.get(sym)
        if r["status"] == "ok":
            res = copy.deepcopy(r["res"])                    # the shared result must never be modified for others
            results.append(res)
            by_sym[sym] = res
            stamps.append(r["t"])
        elif r["status"] == "pending":
            pending.append(sym)
            results.append({"symbol": sym, "error": "Analysing... the first analysis of a share can take a minute."})
        else:
            results.append({"symbol": sym, "error": r["err"]})
    stamp = max(stamps) if stamps else None

    if not pending and stamp is not None:                    # the data refreshed: last result becomes "previous"
        if state["stamp"] is None:
            state["stamp"] = stamp
        elif stamp != state["stamp"]:
            state["prev"], state["stamp"] = state["cur"], stamp
    rows, new_stops = manage_holdings(by_sym, results, held, state["prev"]["stops"], now, track_days=False)
    buys, new_buy = plan_buys(by_sym, results, wants, state["prev"]["buy"])
    state["cur"] = {"stops": new_stops, "buy": new_buy}

    if state["t0"] is None:
        state["t0"] = now.isoformat()
    updated = dt.datetime.fromtimestamp(stamp, IST).isoformat() if stamp else state["t0"]
    plan = allocate([], capital, cfg.RISK_PCT, cfg.MAX_ALLOC, cfg.CHARGES_PCT)
    return {"updated": updated, "capital": capital, "results": results, "plan": plan, "holdings": rows,
            "portfolio": summarize(rows), "held": list(held), "buylist": buys, "wanted": list(wants),
            "paper": None, "pending": pending, "public": True}


# ------------------------------------------------------------------ a visitor's lists (session only, never saved)
class SessionList:
    """Drop-in for the holdings / buylist modules in the dashboard (read / save), backed by st.session_state.
    `locked` = rows every visitor always has (the default buy list). They are not part of read() / save(): they cannot
    be edited or deleted, and all_df() adds them back in front of the visitor's own rows."""

    def __init__(self, session, key, module, other_key, other_module, locked=None):
        self.session, self.key, self.module = session, key, module
        self.other_key, self.other_module, self.locked = other_key, other_module, locked

    def read(self):
        df = self.session.get(self.key)
        return df.copy() if df is not None else self.module.parse("")

    def all_df(self):
        """Locked default rows first, then the visitor's own."""
        mine = self.read()
        if self.locked is None or self.locked.empty:
            return mine
        return pd.concat([self.locked, mine], ignore_index=True)

    def _symbols(self, df):
        return {validate_symbol(s) for s in df["symbol"]}

    def save(self, df):
        df = self.module.clean(df)
        mine = self._symbols(df)                              # raises ValueError for a bad symbol
        if self.locked is not None:
            clash = mine & default_symbols()
            if clash:
                raise ValueError(f"{', '.join(sorted(x.split('.')[0] for x in clash))} is already on the default list "
                                 f"above (it is always shown and cannot be changed or removed).")
        other = self.session.get(self.other_key)
        theirs = self._symbols(self.other_module.clean(other)) if other is not None else set()
        if len((mine | theirs) - default_symbols()) > cfg.MAX_SYMBOLS_PER_VISITOR:
            raise ValueError(f"At most {cfg.MAX_SYMBOLS_PER_VISITOR} different shares of your own can be tracked "
                             f"(holdings and buy list together, the default list does not count). Remove one first.")
        self.session[self.key] = self.module.parse(df.to_csv(index=False))      # same column types as a loaded file

    def upload(self, raw_bytes):
        """Replace the list with an uploaded CSV. Raises ValueError with a readable message."""
        if len(raw_bytes) > MAX_UPLOAD_BYTES:
            raise ValueError("That file is too large (the limit is 200 KB).")
        try:
            df = self.module.parse(raw_bytes.decode("utf-8-sig"))
        except Exception:
            raise ValueError("Could not read that file. Upload the CSV you downloaded from this page.") from None
        if self.locked is not None:                           # default rows are always present: ignore them in a file
            known = {hd.normalize(x) for x in self.locked["symbol"]}
            df = df[~df["symbol"].astype(str).map(lambda x: hd.normalize(x) in known)]
        self.save(df)

    def csv(self):
        """The visitor's own rows only (the default list is built in, so it is not part of their file)."""
        return self.module.clean(self.read()).to_csv(index=False)


def session_lists(session):
    """(holdings list, buy list). The buy list always contains the built-in default rows."""
    return (SessionList(session, "v_held", hd, "v_wants", bl),
            SessionList(session, "v_wants", bl, "v_held", hd, locked=default_buylist()))


def has_own_lists(session):
    return any(session.get(k) is not None and len(session.get(k)) for k in ("v_held", "v_wants"))


def visitor_data(session, cache=None, now=None):
    """Everything the dashboard needs for this visitor, from their session lists."""
    hold, buy = session_lists(session)
    state = session.setdefault("v_state", new_state())
    held, wants = hd.from_df(hold.read()), bl.from_df(buy.all_df())
    return compute(cache or shared_cache(), held, wants, now or dt.datetime.now(IST), state)


def visitor_history(session, stored):
    """The shared history.json holds every share any visitor looked at: show a visitor only their own shares."""
    hold, buy = session_lists(session)
    mine = set(hd.from_df(hold.read())) | set(bl.from_df(buy.all_df()))
    return {k: v for k, v in (stored or {}).items() if k in mine}
