"""The buy / hold rules: honest verdicts for shares that cannot pass the history gate, and the 20-day time limit."""
import pandas as pd

from tradesathi import config as cfg
from tradesathi.analysis import buyplan, positions
from tradesathi.analysis.history import NO_HISTORY
from tradesathi.storage import holdings as hd, persist
from tradesathi import engine
from tradesathi.storage.store import Store


def _res(signal, edge, score=2.0):
    hist = {"edge": edge, "years": 5, "trades": 150, "expectancy_r": -0.1, "t_stat": -1.0, "win_rate": 40,
            "history_years": 0.9, "history_bars": 230}
    return {"symbol": "X.NS", "price": 100.0, "atr": 4.0, "signal": signal, "score": score, "why": [], "support": 98.0,
            "resistance": 130.0, "entry_low": 99.0, "entry_high": 101.0, "entry": 100.0, "stop": 95.0, "t1": 107.0,
            "t2": 110.0, "zone_status": "IN_ZONE", "action": signal, "valid": True, "history": hist}


def test_no_edge_share_is_do_not_buy_even_while_signal_is_wait():
    p = buyplan.make(_res("WAIT", "NO EDGE"), 5000)
    assert p["verdict"] == buyplan.NO and "blocked" in p["when"]


def test_weak_edge_share_is_also_blocked():
    assert buyplan.make(_res("WAIT", "WEAK EDGE"), 5000)["verdict"] == buyplan.NO


def test_untestable_share_stays_wait():
    assert buyplan.make(_res("WAIT", NO_HISTORY), 5000)["verdict"] == buyplan.WAIT


def test_proven_edge_share_with_wait_signal_waits():
    assert buyplan.make(_res("WAIT", "EDGE"), 5000)["verdict"] == buyplan.WAIT


def test_proven_edge_buy_in_zone_mentions_time_limit():
    p = buyplan.make(_res("BUY", "EDGE", 6.0), 5000)
    assert p["verdict"] == buyplan.BUY_NOW
    assert any(f"{cfg.BT_MAX_HOLD} trading days" in s for s in p["steps"])


def test_time_limit_exit_for_holdings():
    res = _res("WAIT", "EDGE")
    assert positions.manage(res, 10, 100.0, None, cfg.BT_MAX_HOLD - 1)["action"].startswith("HOLD")
    m = positions.manage(res, 10, 100.0, None, cfg.BT_MAX_HOLD)
    assert m["action"].startswith("EXIT NOW (time limit") and m["days_held"] == cfg.BT_MAX_HOLD


def test_first_seen_date_is_stored_and_kept(workdir, fake_source):
    hd.save(pd.DataFrame([{"symbol": "TCS", "qty": 10, "buy_price": 900.0}]))
    engine.cycle(fake_source, Store(), 0)
    first = persist.read_text("data/stops.json")
    assert '"since"' in first
    engine.cycle(fake_source, Store(), 1, force=True)
    assert persist.read_text("data/stops.json") == first        # same date kept on later cycles
