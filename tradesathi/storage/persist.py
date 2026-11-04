"""Where your own data lives (holdings, buy list, paper trades, trailing stops).

Local run      -> plain files next to the app (the default; nothing to configure).
Streamlit Cloud -> its disk is wiped on every restart, so put this data in a Google Sheet instead:
                   add a [storage] section to the app secrets (see .streamlit/secrets.toml.example).

Everything else (price candles, signals.json, history.json) is re-creatable and stays on local disk.
Keys are relative paths such as "holdings.csv" or "data/paper_trades.csv"."""
import os
import threading

_lock = threading.RLock()
_cache = {}          # key -> text last read from / written to the backend (this process is the only writer)
_backend = None


class LocalBackend:
    name = "local files (lost on Streamlit Cloud restarts)"
    cached = False          # files can be edited in Excel/Notepad while the app runs, so always re-read them

    def read(self, key):
        try:
            with open(key, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def write(self, key, text):
        folder = os.path.dirname(key)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = key + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, key)                       # a reader never sees a half-written file


class SheetsBackend:
    """One worksheet called `tradesathi_kv` with rows: key | part number | text. Long texts are split in parts
    because a cell holds at most 50,000 characters."""
    name = "Google Sheets"
    cached = True           # slow and rate limited: this process is the only writer, so remember what it knows
    TAB, CHUNK = "tradesathi_kv", 40000

    def __init__(self, service_account, spreadsheet):
        self._info, self._spreadsheet, self._ws_obj = dict(service_account), spreadsheet, None

    def _ws(self):
        if self._ws_obj is None:
            import gspread
            gc = gspread.service_account_from_dict(self._info)
            sh = gc.open_by_url(self._spreadsheet) if str(self._spreadsheet).startswith("http") \
                else gc.open_by_key(self._spreadsheet)
            try:
                self._ws_obj = sh.worksheet(self.TAB)
            except gspread.WorksheetNotFound:
                self._ws_obj = sh.add_worksheet(self.TAB, rows=200, cols=3)
        return self._ws_obj

    def _rows(self):
        return [r + [""] * (3 - len(r)) for r in self._ws().get_all_values() if r and r[0] and r[0] != "key"]

    def read(self, key):
        parts = sorted(((int(r[1] or 0), r[2]) for r in self._rows() if r[0] == key))
        return "".join(p[1] for p in parts) if parts else None

    def write(self, key, text):
        keep = [r for r in self._rows() if r[0] != key]
        new = [[key, i, text[o:o + self.CHUNK]] for i, o in enumerate(range(0, max(len(text), 1), self.CHUNK))]
        ws = self._ws()
        ws.clear()
        ws.update(values=[["key", "part", "text"]] + keep + new, range_name="A1", raw=True)


def _from_secrets():
    """A SheetsBackend when the app secrets say so, else None."""
    if os.environ.get("TRADESATHI_STORAGE", "").lower() == "local":
        return None
    try:
        import streamlit as st
        cfg = st.secrets.get("storage")
        if cfg and cfg.get("service_account") and cfg.get("spreadsheet"):
            return SheetsBackend(cfg["service_account"], cfg["spreadsheet"])
    except Exception:                              # no secrets file / streamlit missing: run locally
        pass
    return None


def backend():
    global _backend
    with _lock:
        if _backend is None:
            _backend = _from_secrets() or LocalBackend()
        return _backend


def use(b):
    """Pick a backend explicitly (tests) and forget everything cached."""
    global _backend
    with _lock:
        _backend = b
        _cache.clear()


def reset():
    use(None)


def read_text(key, seed=None):
    """Text stored under `key`, or None. `seed` = local file used the first time when the backend has nothing yet
    (so the holdings.csv you committed becomes the starting content of a new Google Sheet)."""
    with _lock:
        b = backend()
        if not b.cached:
            return b.read(key)
        if key not in _cache:
            text = b.read(key)
            if text is None and seed:
                text = LocalBackend().read(seed)
            _cache[key] = text
        return _cache[key]


def write_text(key, text):
    with _lock:
        b = backend()
        if b.cached and _cache.get(key) == text:
            return                                  # unchanged: spare the network call
        b.write(key, text)
        if b.cached:
            _cache[key] = text
