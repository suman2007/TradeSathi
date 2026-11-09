"""Public (visitor) mode: private lists per session on top of a shared anonymous price cache."""
import copy
import datetime as dt
import os

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from tradesathi import engine, mode, visitor
from tradesathi.storage import buylist as bl
from tradesathi.storage import holdings as hd
from tradesathi.storage.store import Store
from tradesathi.visitor import ResultCache, SessionList, session_lists, validate_symbol

DASH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Counting:
    """Wraps a data source and counts downloads."""
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def fetch(self, *a):
        self.calls += 1
        return self.inner.fetch(*a)


class NoData:
    def fetch(self, *a):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def hold_df(*rows):
    return pd.DataFrame([{"symbol": s, "qty": q, "buy_price": p} for s, q, p in rows])


def make_cache(workdir, src, clock=None, **kw):
    return ResultCache(src, Store(), sync=True, clock=clock or Clock(), **kw)


# ------------------------------------------------------------------ symbols
@pytest.mark.parametrize("raw,ok", [("tcs", "TCS.NS"), (" TCS.NS ", "TCS.NS"), ("reliance.bo", "RELIANCE.BO"),
                                    ("M&M", "M&M.NS"), ("BAJAJ-AUTO", "BAJAJ-AUTO.NS")])
def test_valid_symbols(raw, ok):
    assert validate_symbol(raw) == ok


@pytest.mark.parametrize("raw", ["", "../etc/passwd", "a b", "TCS.XX", "x" * 30, "TCS/NS", "..", "TCS;DROP", "-TCS",
                                 "C:\\x", "TCS.NS.NS"])
def test_invalid_symbols_are_rejected(raw):
    with pytest.raises(ValueError):
        validate_symbol(raw)


# ------------------------------------------------------------------ shared cache
def test_cache_analyses_once_and_reuses(workdir, fake_source):
    src, clock = Counting(fake_source), Clock()
    cache = make_cache(workdir, src, clock)
    first = cache.get("TCS.NS")
    assert first["status"] == "ok"
    n = src.calls
    assert n > 0
    assert cache.get("TCS.NS")["status"] == "ok" and src.calls == n          # second visitor: no new download
    clock.t += 5000                                                           # data is stale: refreshed once
    assert cache.get("TCS.NS")["t"] == clock.t
    assert src.calls > n


def test_unknown_share_is_an_error_and_not_retried_immediately(workdir):
    src, clock = Counting(NoData()), Clock()
    cache = make_cache(workdir, src, clock)
    r = cache.get("NOPE.NS")
    assert r["status"] == "error" and "no price data" in r["err"]
    n = src.calls
    assert cache.get("NOPE.NS")["status"] == "error" and src.calls == n      # remembered for a while
    clock.t += ResultCache.ERROR_TTL + 1
    cache.get("NOPE.NS")
    assert src.calls > n


def test_server_wide_cap_on_tracked_shares(workdir, fake_source):
    cache = make_cache(workdir, fake_source, max_total=2)
    assert cache.get("TCS.NS")["status"] == "ok" and cache.get("INFY.NS")["status"] == "ok"
    r = cache.get("ITC.NS")
    assert r["status"] == "error" and "as many shares" in r["err"]
    assert cache.get("TCS.NS")["status"] == "ok"                              # existing ones keep working


def test_background_mode_returns_pending_then_result(workdir, fake_source):
    import time
    cache = ResultCache(fake_source, Store(), sync=False, workers=1)
    assert cache.get("TCS.NS")["status"] in ("pending", "ok")
    for _ in range(300):
        if cache.idle():
            break
        time.sleep(0.1)
    assert cache.get("TCS.NS")["status"] == "ok"


# ------------------------------------------------------------------ one visitor's data
def test_visitors_only_see_their_own_shares_and_nothing_is_saved(workdir, fake_source):
    cache = make_cache(workdir, fake_source)
    now = dt.datetime.now(IST)
    a, b = visitor.new_state(), visitor.new_state()
    out_a = visitor.compute(cache, hd.from_df(hold_df(("TCS", 10, 900.0))), {}, now, a)
    out_b = visitor.compute(cache, hd.from_df(hold_df(("INFY", 5, 800.0))), {}, now, b)
    assert [h["symbol"] for h in out_a["holdings"]] == ["TCS.NS"]
    assert [h["symbol"] for h in out_b["holdings"]] == ["INFY.NS"]
    assert "INFY.NS" not in str(out_a) and "TCS.NS" not in str(out_b)
    # nothing about a visitor is written to the server: no list files, no stops, no paper trades
    for f in ("holdings.csv", "buylist.csv", "data/stops.json", "data/buy_state.json", "data/paper_trades.csv",
              "data/signals.json"):
        assert not os.path.exists(f), f


def test_a_visitor_cannot_change_the_shared_result(workdir, fake_source):
    cache = make_cache(workdir, fake_source)
    now = dt.datetime.now(IST)
    before = copy.deepcopy(cache.get("TCS.NS")["res"])
    out = visitor.compute(cache, hd.from_df(hold_df(("TCS", 10, 900.0))), {}, now, visitor.new_state())
    out["results"][0]["why"].append("tampered")
    out["results"][0]["price"] = -1
    assert cache.get("TCS.NS")["res"] == before


def test_state_advances_only_when_the_shared_data_refreshes(workdir, fake_source):
    clock = Clock()
    cache = make_cache(workdir, fake_source, clock)
    now, state = dt.datetime.now(IST), visitor.new_state()
    held = hd.from_df(hold_df(("TCS", 10, 900.0)))
    visitor.compute(cache, held, {}, now, state)
    stamp, first_cur = state["stamp"], copy.deepcopy(state["cur"])
    assert first_cur["stops"] and state["prev"]["stops"] == {}
    visitor.compute(cache, held, {}, now, state)                              # page refresh / list edit: same data
    assert state["stamp"] == stamp and state["prev"]["stops"] == {}
    clock.t += 5000                                                           # the shared data refreshes
    visitor.compute(cache, held, {}, now, state)
    assert state["stamp"] != stamp and state["prev"] == first_cur


def test_pending_and_error_shares_are_reported(workdir, fake_source):
    class Mixed:
        def fetch(self, sym, *a):
            return NoData().fetch() if sym == "BAD.NS" else fake_source.fetch(sym, *a)
    cache = make_cache(workdir, Mixed())
    out = visitor.compute(cache, hd.from_df(hold_df(("TCS", 1, 900.0), ("BAD", 1, 10.0))), {}, dt.datetime.now(IST),
                          visitor.new_state())
    rows = {h["symbol"]: h for h in out["holdings"]}
    assert "error" not in rows["TCS.NS"] and "no price data" in rows["BAD.NS"]["error"]
    assert out["pending"] == []


# ------------------------------------------------------------------ session lists
def test_session_lists_validation_limit_and_isolation():
    sa, sb = {}, {}
    hold_a, buy_a = session_lists(sa)
    hold_b, _ = session_lists(sb)
    hold_a.save(hold_df(("tcs", 10, 900.0)))
    assert hold_b.read().empty                                                # another visitor sees nothing
    assert list(hold_a.read()["symbol"]) == ["TCS"]
    with pytest.raises(ValueError):
        hold_a.save(hold_df(("../x", 1, 1.0)))
    too_many = hold_df(*[(f"S{i}", 1, 10.0) for i in range(cfg_limit() + 1)])
    with pytest.raises(ValueError, match="At most"):
        hold_a.save(too_many)
    assert list(hold_a.read()["symbol"]) == ["TCS"]                           # a rejected save changes nothing
    # the limit counts holdings and buy list together
    hold_a.save(hold_df(*[(f"S{i}", 1, 10.0) for i in range(cfg_limit())]))
    with pytest.raises(ValueError, match="At most"):
        buy_a.save(pd.DataFrame([{"symbol": "EXTRA", "amount": 1000.0, "qty": None, "max_price": None}]))


def cfg_limit():
    from tradesathi import config
    return config.MAX_SYMBOLS_PER_VISITOR


def test_upload_download_roundtrip_and_limits():
    s = {}
    hold, buy = session_lists(s)
    hold.save(hold_df(("TCS", 10, 900.0), ("INFY", 5, 1500.5)))
    saved = hold.csv()
    fresh, _ = session_lists({})
    fresh.upload(saved.encode())
    assert fresh.read().equals(hold.read())
    with pytest.raises(ValueError, match="too large"):
        fresh.upload(b"x" * (visitor.MAX_UPLOAD_BYTES + 1))
    with pytest.raises(ValueError):
        fresh.upload(b"symbol,qty,buy_price\n../../etc/passwd,1,1\n")
    with pytest.raises(ValueError, match="Could not read"):
        fresh.upload(b"\xff\xfe\x00 not a csv \x00")


# ------------------------------------------------------------------ mode switch
def test_mode_switch(monkeypatch):
    monkeypatch.delenv("TRADESATHI_MODE", raising=False)
    assert mode.is_public() == os.path.isdir("/mount/src")                    # automatic: cloud -> public
    monkeypatch.setenv("TRADESATHI_MODE", "public")
    assert mode.is_public()
    monkeypatch.setenv("TRADESATHI_MODE", "owner")
    assert not mode.is_public()
    monkeypatch.setenv("TRADESATHI_MODE", "garbage")                          # unknown value: fall back to automatic
    assert mode.is_public() == os.path.isdir("/mount/src")


def test_cloud_defaults_to_public(monkeypatch):
    monkeypatch.delenv("TRADESATHI_MODE", raising=False)
    monkeypatch.setattr(os.path, "isdir", lambda p: p == "/mount/src")
    assert mode.is_public()


# ------------------------------------------------------------------ the real dashboard in public mode
@pytest.fixture
def public_app(workdir, fake_source, monkeypatch):
    monkeypatch.setenv("TRADESATHI_MODE", "public")
    monkeypatch.delenv("TRADESATHI_EXTERNAL_ENGINE", raising=False)
    visitor.set_shared_cache(make_cache(workdir, fake_source))
    yield
    visitor.set_shared_cache(None)


def new_session():
    at = AppTest.from_file(DASH, default_timeout=60)
    at.session_state["sebi_ack"] = True
    return at


def test_public_dashboard_keeps_visitors_apart(public_app):
    a = new_session()
    a.session_state["v_held"] = hd.clean(hold_df(("ITC", 10, 400.0)))
    a.run()
    assert not a.exception, a.exception
    assert any("ITC" in b.label for b in a.sidebar.button)
    assert any(m.label == "Invested" for m in a.metric)

    b = new_session().run()                                                   # a different visitor, same server
    assert not b.exception, b.exception
    assert not any("ITC" in x.label for x in b.sidebar.button)                # A's private share is invisible to B
    assert any("TCS" in x.label for x in b.sidebar.button)                    # the default list is visible to everyone
    assert any("Welcome" in i.value for i in b.info)
    assert not any(m.label == "Invested" for m in b.metric)                   # B owns nothing

    assert engine.STATE["running"] is False                                   # no background engine in public mode
    assert not os.path.exists("holdings.csv") and not os.path.exists("data/stops.json")


def test_public_manage_page_shows_privacy_notice_and_rejects_bad_symbols(public_app):
    at = new_session()
    at.session_state["page"] = "manage"
    at.run()
    assert not at.exception, at.exception
    assert any("Private to you" in i.value for i in at.info)
    assert any(d.label.startswith("Download my") for d in at.get("download_button"))
    at.text_input(key="hold_add_symbol").input("../../x")
    at.number_input(key="hold_add_qty").set_value(5)
    at.number_input(key="hold_add_buy_price").set_value(100)
    next(b for b in at.get("form_submit_button") if b.label == "Add").click()
    at.run()
    assert not at.exception, at.exception
    assert any("not a valid" in e.value for e in at.error)
    assert "v_held" not in at.session_state or at.session_state["v_held"].empty


def test_owner_mode_is_unchanged(workdir, monkeypatch):
    monkeypatch.setenv("TRADESATHI_MODE", "owner")
    at = new_session().run()
    assert not at.exception, at.exception
    assert any(b.label.startswith("📝") for b in at.sidebar.button)           # owner-only pages still there


# ------------------------------------------------------------------ the built-in default buy list
DEFAULTS = ["TCS.NS", "RELIANCE.NS", "INFY.NS", "WIPRO.NS", "ADANIENT.NS"]


def test_default_list_content():
    from tradesathi import config
    df = visitor.default_buylist()
    assert [f"{x}.NS" for x in df["symbol"]] == DEFAULTS
    assert list(df["qty"]) == [100.0] * 5 and df["amount"].isna().all() and df["max_price"].isna().all()
    assert visitor.default_symbols() == set(DEFAULTS)
    assert all(validate_symbol(sym) for sym, _ in config.PUBLIC_DEFAULT_BUYLIST)       # every default is a valid symbol


def test_every_visitor_gets_the_defaults(workdir, fake_source):
    cache = make_cache(workdir, fake_source)
    now = dt.datetime.now(IST)
    for _ in range(2):                                                         # two different visitors
        sess = {}
        out = visitor.visitor_data(sess, cache, now)
        assert out["wanted"] == DEFAULTS
        assert [b["symbol"] for b in out["buylist"]] == DEFAULTS
        assert out["holdings"] == []


def test_defaults_cannot_be_deleted_edited_or_duplicated():
    sess = {}
    _, buy = session_lists(sess)
    own = pd.DataFrame([{"symbol": "ITC", "qty": 5.0, "amount": None, "max_price": None}])
    buy.save(own)
    assert list(buy.read()["symbol"]) == ["ITC"]                               # the editable table holds only their own rows
    assert list(buy.all_df()["symbol"]) == ["TCS", "RELIANCE", "INFY", "WIPRO", "ADANIENT", "ITC"]
    buy.save(buy.read().iloc[0:0])                                             # "delete everything" in the editor
    assert list(buy.all_df()["symbol"]) == ["TCS", "RELIANCE", "INFY", "WIPRO", "ADANIENT"]
    for clash in ("TCS", "tcs.ns", "RELIANCE"):                                # adding a default again is refused
        with pytest.raises(ValueError, match="default list"):
            buy.save(pd.DataFrame([{"symbol": clash, "qty": 1.0, "amount": None, "max_price": None}]))
    assert buy.locked.equals(visitor.default_buylist())                        # and the defaults themselves never changed


def test_defaults_do_not_count_toward_the_visitor_limit():
    sess = {}
    hold, buy = session_lists(sess)
    hold.save(hold_df(*[(f"S{i}", 1, 10.0) for i in range(cfg_limit())]))      # 10 own shares + 5 defaults: fine
    assert len(hold.read()) == cfg_limit() and len(buy.all_df()) == 5
    with pytest.raises(ValueError, match="At most"):
        hold.save(hold_df(*[(f"S{i}", 1, 10.0) for i in range(cfg_limit() + 1)]))
    hold.save(hold_df(("TCS", 10, 3000.0)))                                    # owning a default share is allowed


def test_download_excludes_defaults_and_upload_ignores_them():
    sess = {}
    _, buy = session_lists(sess)
    buy.save(pd.DataFrame([{"symbol": "ITC", "qty": 5.0, "amount": None, "max_price": None}]))
    assert "TCS" not in buy.csv() and "ITC" in buy.csv()
    _, fresh = session_lists({})
    fresh.upload(b"symbol,amount,qty,max_price\nTCS,,100,\nBEL,5000,,300\n")  # a file that also lists a default
    assert list(fresh.read()["symbol"]) == ["BEL"]
    assert list(fresh.all_df()["symbol"]) == ["TCS", "RELIANCE", "INFY", "WIPRO", "ADANIENT", "BEL"]


def test_public_manage_page_shows_the_default_list(public_app):
    at = new_session()
    at.session_state["page"] = "manage"
    at.run()
    assert not at.exception, at.exception
    assert any("Default buy list" in m.value for m in at.markdown)
    defaults = [d for d in at.dataframe if list(d.value.get("Symbol", [])) == ["TCS", "RELIANCE", "INFY", "WIPRO", "ADANIENT"]]
    assert defaults, "the default list is shown read-only"
    assert not any(list(e.value.get("symbol", [])) == ["TCS"] for e in at.get("arrow_data_frame"))  # not in the editable table


def test_owner_mode_has_no_default_list(workdir, monkeypatch):
    monkeypatch.setenv("TRADESATHI_MODE", "owner")
    at = new_session()
    at.session_state["page"] = "manage"
    at.run()
    assert not at.exception, at.exception
    assert not any("Default buy list" in m.value for m in at.markdown)
