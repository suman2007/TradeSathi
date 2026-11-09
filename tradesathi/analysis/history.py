"""Attach each share's historical record to its live signal (cached once per day)."""
from tradesathi import config as cfg
from tradesathi.analysis import backtest

KEYS = ("edge", "trades", "win_rate", "expectancy_r", "profit_factor", "max_dd_pct", "cagr_pct",
        "buy_hold_cagr_pct", "years", "avg_win_r", "avg_loss_r", "t_stat", "method", "history_bars", "history_years")
NO_HISTORY = "NOT ENOUGH HISTORY"       # share too new to be tested (not the same as "tested, found nothing")


def _settings():
    """Cached results are thrown away when any of these change."""
    return [cfg.BACKTEST_YEARS, cfg.HISTORY_TEST, cfg.CHARGES_PCT, cfg.BUY_SCORE, cfg.MIN_TRADES, cfg.EDGE_MIN_TSTAT, "v2"]   # "v2": NOT ENOUGH HISTORY label


def stats_for(src, store, sym, now):
    """Backtest stats for one share. Never raises: a failure just means no history is shown.
    Uses the intraday replay of the live rules when enough 5-min history exists, otherwise the daily version."""
    try:
        cache = store.read_json("history.json")
        e = cache.get(sym)
        if e and e["asof"] == now.date().isoformat() and e.get("settings") == _settings():
            return e["stats"]
        print(f"  running historical check for {sym} ...")
        d = backtest.completed(store.load(sym, "1d"), now)
        stats = None
        if cfg.HISTORY_TEST == "intraday":
            m5 = backtest.ensure_5m(src, store, sym, now)
            m5 = m5[m5.index.date < now.date()] if len(m5) else m5      # no 5-min data at all: use the daily test
            if len(m5) and (m5.index[-1] - m5.index[0]).days / 365.25 >= cfg.MIN_INTRADAY_YEARS:
                stats = backtest.intraday_run(m5, d, cfg.BACKTEST_YEARS)["metrics"]
        if stats is None:
            stats = backtest.run(d, cfg.BACKTEST_YEARS)["metrics"]
        if not stats.get("trades") and not stats.get("years"):
            # no test could run at all (newly listed share): say so, instead of "0 trades over 0 years"
            stats["edge"] = NO_HISTORY
            stats["history_bars"] = len(d)
            stats["history_years"] = round((d.index[-1] - d.index[0]).days / 365.25, 2) if len(d) else 0.0
        cache[sym] = {"asof": now.date().isoformat(), "settings": _settings(), "stats": stats}
        store.write_json("history.json", cache)
        return stats
    except Exception as ex:
        print(f"  historical check failed for {sym}: {ex}")
        return None


def hist_str(h):
    if not h:
        return "-"
    if h["edge"] == NO_HISTORY:
        return f"{NO_HISTORY} ({h.get('history_years') or 0:.2f}y, untested)"
    return f"{h['edge']} n={h['trades']} win {h['win_rate']:.0f}% E={h['expectancy_r']:+.2f}R t={h.get('t_stat') or 0:+.1f}"


def apply(res, stats):
    """Add the history to the result and block BUY signals on shares without a proven historical edge."""
    res["action_before_gate"] = res["action"]
    if stats:
        res["history"] = {k: stats.get(k) for k in KEYS}
        if stats["edge"] == NO_HISTORY:
            res["why"].append(f"History: {hist_str(res['history'])}")
        else:
            res["why"].append(f"History ({stats['years']}y, {stats.get('method', 'daily')}): {hist_str(res['history'])}, "
                              f"max drawdown {stats['max_dd_pct']}%")
    edge = stats["edge"] if stats else None                 # no record at all = not proven either
    if cfg.REQUIRE_HISTORICAL_EDGE and res["signal"] == "BUY" and edge not in cfg.ALLOWED_EDGES:
        res["valid"] = False
        res["action"] = "SKIP (no proven historical edge)"
