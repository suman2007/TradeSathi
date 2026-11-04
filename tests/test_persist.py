import pandas as pd

from tradesathi.storage import buylist as bl
from tradesathi.storage import holdings as hd
from tradesathi.storage import persist
from tradesathi.storage.persist import SheetsBackend


class FakeWorksheet:
    """In-memory stand-in for a gspread worksheet."""
    def __init__(self):
        self.values, self.writes = [], 0

    def get_all_values(self):
        return [[str(c) for c in r] for r in self.values]

    def clear(self):
        self.values = []

    def update(self, values, range_name, raw=True):
        self.values, self.writes = [list(r) for r in values], self.writes + 1


def sheets_backend():
    b = SheetsBackend({}, "x")
    b._ws_obj = FakeWorksheet()
    return b


def test_local_roundtrip(workdir):
    hd.save(pd.DataFrame([{"symbol": "tcs", "qty": 2, "buy_price": 100.0}]))
    assert (workdir / "holdings.csv").exists()
    assert hd.load() == {"TCS.NS": {"qty": 2, "buy_price": 100.0}}


def test_local_sees_external_edits(workdir):
    hd.save(pd.DataFrame([{"symbol": "TCS", "qty": 1, "buy_price": 10.0}]))
    (workdir / "holdings.csv").write_text("symbol,qty,buy_price\nINFY,5,50\n")        # edited in Notepad
    assert list(hd.load()) == ["INFY.NS"]


def test_sheets_roundtrip_and_chunking(workdir):
    b = sheets_backend()
    persist.use(b)
    big = "x" * (SheetsBackend.CHUNK * 2 + 5)
    persist.write_text("data/big.csv", big)
    persist.write_text("holdings.csv", "symbol,qty,buy_price\nTCS,1,10\n")
    persist.use(b)                                          # fresh process: cache empty, read from the "sheet"
    assert persist.read_text("data/big.csv") == big
    assert persist.read_text("missing.csv") is None
    persist.write_text("holdings.csv", "symbol,qty,buy_price\n")   # overwrite keeps the other key
    persist.use(b)
    assert persist.read_text("data/big.csv") == big
    assert persist.read_text("holdings.csv") == "symbol,qty,buy_price\n"


def test_sheets_skips_unchanged_writes_and_seeds_from_local(workdir):
    (workdir / "holdings.csv").write_text("symbol,qty,buy_price\nSBIN,3,700\n")
    b = sheets_backend()
    persist.use(b)
    assert list(hd.load()) == ["SBIN.NS"]                  # empty sheet -> seeded from the committed CSV
    persist.write_text("a.json", "{}")
    persist.write_text("a.json", "{}")
    assert b._ws_obj.writes == 1


def test_buylist_roundtrip(workdir):
    persist.use(sheets_backend())
    bl.save(pd.DataFrame([{"symbol": "ppl", "amount": 10000.0, "qty": None, "max_price": 300.0}]))
    assert bl.load()["PPL.NS"] == {"amount": 10000.0, "qty": None, "max_price": 300.0}
