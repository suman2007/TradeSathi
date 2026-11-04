import datetime as dt
import json

import pandas as pd

from tradesathi.storage import buylist as bl
from tradesathi import engine
from tradesathi.storage import holdings as hd
from tradesathi.analysis import paper
from tradesathi.storage import persist
from tradesathi.storage.store import Store


def test_one_cycle_writes_signals(workdir, fake_source):
    hd.save(pd.DataFrame([{"symbol": "TCS", "qty": 10, "buy_price": 900.0}]))
    bl.save(pd.DataFrame([{"symbol": "INFY", "amount": 20000.0, "qty": None, "max_price": None}]))
    assert engine.cycle(fake_source, Store(), 0) is not None
    sig = json.load(open(workdir / "data" / "signals.json"))
    assert [h["symbol"] for h in sig["holdings"]] == ["TCS.NS"]
    assert [b["symbol"] for b in sig["buylist"]] == ["INFY.NS"]
    assert not any("error" in r for r in sig["results"]), sig["results"]
    assert persist.read_text("data/stops.json")                      # trailing stops are persisted
    assert paper.load(Store()).columns.tolist() == paper.COLS


def test_default_symbols_when_nothing_configured(workdir, fake_source):
    out = engine.cycle(fake_source, Store(), 0)
    assert len(out["results"]) == 5 and not any("error" in r for r in out["results"])


def test_market_closed_skips_after_first_pass(workdir, fake_source):
    sunday = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert engine.cycle(fake_source, Store(), 1, now=sunday) is None


def test_no_thread_when_external_engine(workdir):
    assert engine.ensure_started() is False
