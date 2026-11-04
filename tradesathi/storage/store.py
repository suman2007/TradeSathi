"""Plain-file storage: one CSV per symbol/timeframe + JSON for results. No database."""
import json
import os
import pandas as pd
from tradesathi import config as cfg
from tradesathi.storage import persist

PERSISTED = ("stops.json", "buy_state.json")     # small state you would miss after a restart: kept via persist.py


class Store:
    def __init__(self, root=cfg.DATA_DIR):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _p(self, symbol, tf):
        return os.path.join(self.root, f"{symbol.replace('.', '_')}_{tf}.csv")

    def load(self, symbol, tf):
        p = self._p(symbol, tf)
        if not os.path.exists(p):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.read_csv(p, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(cfg.TZ_NAME)
        return df

    def merge(self, symbol, tf, new, keep_days=None):
        old = self.load(symbol, tf)
        df = pd.concat([old, new]) if len(old) else new.copy()     # concat with an empty frame turns prices into text
        df = df[~df.index.duplicated(keep="last")].sort_index().astype(float)
        if keep_days:
            df = df[df.index >= df.index[-1] - pd.Timedelta(days=keep_days)]
        self._atomic(self._p(symbol, tf), lambda f: df.to_csv(f))
        return df

    def key(self, name):
        return f"{self.root}/{name}"

    def write_json(self, name, obj):
        if name in PERSISTED:
            persist.write_text(self.key(name), json.dumps(obj, indent=1))
            return
        def w(f):
            with open(f, "w") as fh:
                json.dump(obj, fh, indent=1)
        self._atomic(os.path.join(self.root, name), w)

    def read_json(self, name):
        try:
            if name in PERSISTED:
                return json.loads(persist.read_text(self.key(name)) or "{}")
            with open(os.path.join(self.root, name)) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _atomic(path, writer):
        tmp = path + ".tmp"
        writer(tmp)
        os.replace(tmp, path)  # dashboard never reads a half-written file
