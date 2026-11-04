"""The analysis engine as a background thread, so the dashboard works on its own (Streamlit Community Cloud only
runs `streamlit run dashboard.py`). To run the engine in a separate terminal instead (python -m tradesathi.main), set
TRADESATHI_EXTERNAL_ENGINE=1 for the dashboard so it does not start a second one."""
import datetime as dt
import os
import threading
import time
import traceback

from tradesathi import config as cfg
from tradesathi.storage import buylist as bl
from tradesathi.storage import holdings as hd
from tradesathi.main import IST, market_open, normalize, run_cycle

STATE = {"running": False, "cycles": 0, "last_cycle": None, "last_error": None}
_lock = threading.Lock()


def cycle(src, store, n, now=None, force=False):
    """One engine pass. Returns the result dict, or None when the market is closed (and it is not the first pass)."""
    now = now or dt.datetime.now(IST)
    if not (force or n == 0 or market_open(now)):
        return None
    held, wants = hd.load(), bl.load()                   # re-read every pass: edits in the dashboard apply at once
    symbols = [normalize(s) for s in cfg.DEFAULT_SYMBOLS] if not held and not wants else []
    return run_cycle(src, store, symbols, cfg.DEFAULT_CAPITAL, now, daily_due=(n % 15 == 0), held=held, wants=wants)


def _loop(src, store):
    n = 0
    while True:
        try:
            if cycle(src, store, n) is not None:
                n += 1
                STATE.update(cycles=STATE["cycles"] + 1, last_cycle=dt.datetime.now(IST).isoformat(), last_error=None)
        except Exception as e:                           # keep the thread alive whatever happens
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        time.sleep(cfg.REFRESH_SECONDS)


def external():
    return os.environ.get("TRADESATHI_EXTERNAL_ENGINE") == "1"


def ensure_started(src=None, store=None):
    """Start the background thread once per process (safe to call on every Streamlit rerun)."""
    if external():
        return False
    with _lock:
        if STATE["running"]:
            return False
        from tradesathi.data_source import get_source
        from tradesathi.storage.store import Store
        STATE["running"] = True
        threading.Thread(target=_loop, args=(src or get_source(), store or Store()), daemon=True,
                         name="tradesathi-engine").start()
        return True
