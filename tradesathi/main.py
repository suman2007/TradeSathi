"""Live engine. Examples:
  python -m tradesathi.main --symbols RELIANCE TCS INFY --capital 200000
  python -m tradesathi.main --symbols SBIN --capital 100000 --holding RELIANCE:10:2450.5 --holding TCS:5:3900
  python -m tradesathi.main --buy RELIANCE:50000:2950 --buy TCS:30000      (SYMBOL:AMOUNT[:MAXPRICE] = shares you WANT to buy)
Holdings live in holdings.csv (symbol,qty,buy_price). Shares you want to buy live in buylist.csv
(symbol,amount,qty,max_price). Both files are re-read every cycle, so you can edit them while running."""
import argparse
import datetime as dt
import time
from zoneinfo import ZoneInfo

import pandas as pd

from tradesathi import config as cfg
from tradesathi.analysis import backtest
from tradesathi.storage import buylist as bl
from tradesathi.analysis import buyplan
from tradesathi.analysis import history
from tradesathi.storage import holdings as hd
from tradesathi.analysis import paper
from tradesathi.analysis.allocator import allocate
from tradesathi.data_source import get_source
from tradesathi.analysis.positions import manage, summarize
from tradesathi.storage.store import Store
from tradesathi.analysis.strategy import analyze

IST = ZoneInfo(cfg.TZ_NAME)
normalize = hd.normalize


def market_open(now):
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)  # exchange holidays not handled


def refresh(src, store, sym, daily_due):
    m1_old = store.load(sym, "1m")
    new = src.fetch(sym, "1m", "7d" if m1_old.empty else "2d")   # only the newest candles after first run
    if not new.empty:
        store.merge(sym, "1m", new, keep_days=10)
    backtest.ensure_daily(src, store, sym, refresh_recent=daily_due)   # keeps 10y of daily history
    if store.load(sym, "1m").empty or store.load(sym, "1d").empty:
        raise ValueError("no price data found - check the symbol spelling (use the NSE symbol, e.g. RELIANCE)")


def run_cycle(src, store, symbols, capital, now, daily_due, held=None, wants=None):
    held, wants = held or {}, wants or {}
    watch = list(dict.fromkeys(list(symbols) + list(held) + list(wants)))   # ideas + what you own + what you want to buy
    results = []
    for sym in watch:
        try:
            refresh(src, store, sym, daily_due)
            res = analyze(sym, store.load(sym, "1d"), store.load(sym, "1m"), pd.Timestamp(now))
            history.apply(res, history.stats_for(src, store, sym, pd.Timestamp(now)))   # historical record + edge gate
            results.append(res)
        except Exception as e:  # one bad symbol must not stop the loop
            results.append({"symbol": sym, "error": str(e)})
    by_sym = {r["symbol"]: r for r in results if "error" not in r}

    # --- manage positions you already hold (stops only ever move up, remembered between cycles) ---
    prev, new_stops, rows = store.read_json("stops.json"), {}, []
    for sym, h in held.items():
        r = by_sym.get(sym)
        if r is None:
            rows.append({"symbol": sym, "qty": h["qty"], "buy_price": round(h["buy_price"], 2),
                         "error": next((x["error"] for x in results if x["symbol"] == sym), "no data")})
            continue
        key = f"{sym}|{h['buy_price']:.2f}"
        m = manage(r, h["qty"], h["buy_price"], prev.get(key))
        new_stops[key] = {"stop": m["stop"], "r": m["r"]}
        rows.append(m)
    store.write_json("stops.json", new_stops)

    # --- "when should I buy?" for the shares you asked about ---
    # a BUY NOW must hold CONFIRM_CYCLES cycles in a row before it is confirmed / alerted (filters one-minute flickers)
    prev_v, new_v, buys = store.read_json("buy_state.json"), {}, []
    for sym, w in wants.items():
        r = by_sym.get(sym)
        if r is None:
            buys.append({"symbol": sym, "error": next((x["error"] for x in results if x["symbol"] == sym), "no data")})
            if sym in prev_v:
                new_v[sym] = prev_v[sym]
            continue
        p = buyplan.make(r, w["amount"], w["qty"], w["max_price"])
        last = prev_v.get(sym)
        last = last if isinstance(last, dict) else {"verdict": last, "streak": 1}
        streak = last["streak"] + 1 if last["verdict"] == p["verdict"] else 1
        p["confirmed"] = p["verdict"] != buyplan.BUY_NOW or streak >= cfg.CONFIRM_CYCLES
        p["alert"] = p["verdict"] == buyplan.BUY_NOW and streak == cfg.CONFIRM_CYCLES
        if not p["confirmed"]:
            p["notes"].insert(0, f"New signal: wait until it has held for {cfg.CONFIRM_CYCLES} cycles "
                                 f"(about {cfg.CONFIRM_CYCLES * cfg.REFRESH_SECONDS // 60} min) before acting.")
        new_v[sym] = {"verdict": p["verdict"], "streak": streak}
        buys.append(p)
    store.write_json("buy_state.json", new_v)

    # --- new-entry plan for the rest of the list (held / wanted symbols have their own sections) ---
    plan = allocate(results, capital, cfg.RISK_PCT, cfg.MAX_ALLOC, cfg.CHARGES_PCT, exclude=set(held) | set(wants))

    # --- paper trading: follow every live BUY NOW as if it had been bought ---
    pt = paper.update(paper.load(store), store, src, pd.Timestamp(now), refreshed=set(watch))
    if market_open(now):                       # only prices you could actually have bought at
        for r in by_sym.values():
            pt = paper.record(pt, r, pd.Timestamp(now))
    paper.save(store, pt)

    out = {"updated": now.isoformat(), "capital": capital, "results": results, "plan": plan,
           "holdings": rows, "portfolio": summarize(rows), "held": list(held),
           "buylist": buys, "wanted": list(wants), "paper": paper.summary(pt)}
    store.write_json("signals.json", out)
    return out


def _f(v):
    return f"{v:.2f}" if v is not None else "-"


def _m(v):
    return f"{v:,.0f}" if v is not None else "-"


def print_buylist(out):
    plans = out.get("buylist") or []
    if not plans:
        return
    print("--- SHARES YOU WANT TO BUY: WHEN TO BUY ---")
    print(f"{'SYMBOL':<13}{'PRICE':>9}  {'VERDICT':<19}{'BUY ZONE':<17}{'LIMIT':>9}{'STOP':>9}{'T1':>9}{'T2':>9}"
          f"{'QTY':>6}{'COST':>10}{'MAX LOSS':>10}  HISTORY")
    for p in plans:
        if "error" in p:
            print(f"{p['symbol']:<13} ERROR: {p['error']}")
            continue
        zone = f"{p['buy_zone_low']:.2f}-{p['buy_zone_high']:.2f}"
        print(f"{p['symbol']:<13}{p['price']:>9.2f}  {p['verdict']:<19}{zone:<17}{_f(p['limit']):>9}{_f(p['stop']):>9}"
              f"{_f(p['t1']):>9}{_f(p['t2']):>9}{_m(p['qty']):>6}{_m(p['cost']):>10}{_m(p['max_loss']):>10}"
              f"  {history.hist_str(p.get('history'))}")
        pad = f"{'':<13}"
        print(f"{pad}  >> {p['when']}")
        if p.get("provisional") and p["stop"]:
            print(f"{pad}     (stop / T1 / T2 shown are what would apply if it turns to BUY; no order yet)")
        for s in p["steps"]:
            print(f"{pad}     - {s}")
        for n in p["notes"]:
            print(f"{pad}     note: {n}")
        if p["verdict"] in (buyplan.BUY_NOW, buyplan.LIMIT) and p["qty"]:
            print(f"{pad}     risk: if the stop is hit you lose about {_m(p['max_loss'])} ({p['max_loss_pct']}% of the money); "
                  f"profit at T1 {_m(p['profit_t1'])} / T2 {_m(p['profit_t2'])}")
        if p.get("alert"):
            print(f"{pad}  *** ALERT: {p['symbol']} is now in its buy zone - BUY signal ***")


def print_table(out):
    print(f"\n=== {out['updated'][:19]} IST | free capital for new trades {out['capital']:,.0f} ===")
    if out["holdings"]:
        print("--- YOUR HOLDINGS ---")
        print(f"{'SYMBOL':<13}{'QTY':>6}{'BUY':>10}{'PRICE':>10}{'P&L':>10}{'P&L%':>8}  {'ACTION':<46}{'STOP':>9}{'T1':>9}{'T2':>9}  HISTORY")
        for m in out["holdings"]:
            if "error" in m:
                print(f"{m['symbol']:<13}{m['qty']:>6}{m['buy_price']:>10.2f}  ERROR: {m['error']}")
                continue
            print(f"{m['symbol']:<13}{m['qty']:>6}{m['buy_price']:>10.2f}{m['price']:>10.2f}{m['pnl']:>10,.0f}"
                  f"{m['pnl_pct']:>7.1f}%  {m['action']:<46}{m['stop']:>9.2f}{_f(m['t1']):>9}{_f(m['t2']):>9}  {history.hist_str(m.get('history'))}")
            for w in m["why"]:
                print(f"{'':<13}  note: {w}")
        p = out["portfolio"]
        print(f"  Invested {p['invested']:,.0f} | value {p['value']:,.0f} | P&L {p['pnl']:,.0f} ({p['pnl_pct']}%) | "
              f"if all stops hit: {p['pnl_at_stops']:,.0f}")
    print_buylist(out)
    print("--- NEW IDEAS ---")
    print(f"{'SYMBOL':<13}{'PRICE':>9}  {'ACTION':<28}{'SCORE':>5}  {'ENTRY ZONE':<17}{'STOP':>9}{'T1':>9}{'T2':>9}  HISTORY")
    for r in out["results"]:
        if r["symbol"] in out["held"] or r["symbol"] in out.get("wanted", []):
            continue                                   # already shown in its own section
        elif "error" in r:
            print(f"{r['symbol']:<13} ERROR: {r['error']}")
        else:
            zone = f"{r['entry_low']:.2f}-{r['entry_high']:.2f}"
            print(f"{r['symbol']:<13}{r['price']:>9.2f}  {r['action']:<28}{r['score']:>5.0f}  {zone:<17}"
                  f"{r['stop']:>9.2f}{r['t1']:>9.2f}{r['t2']:>9.2f}  {history.hist_str(r.get('history'))}")
    pl = out["plan"]
    for p in pl["positions"]:
        print(f"  -> {p['symbol']}: buy {p['qty']} @ ~{p['entry']:.2f} | invest {p['invested']:,.0f} | "
              f"max loss {p['max_loss']:,.0f} | profit T1 {p['profit_t1']:,.0f} / T2 {p['profit_t2']:,.0f}")
    s = pl["summary"]
    print(f"  New buys: invest {s['invested']:,.0f} | cash left {s['cash_left']:,.0f} | "
          f"worst-case loss {s['worst_case_loss']:,.0f} ({s['worst_case_pct']}%)")
    ps = out.get("paper")
    if ps:
        a, g = ps["all"], ps["allowed"]
        print(f"--- PAPER TRADES (live BUY NOW signals followed automatically, {cfg.DATA_DIR}/{paper.NAME}) ---")
        print(f"  open {ps['open']} | closed {a['trades']}: win {a['win_rate']}% expectancy {a['expectancy_r']:+.3f}R "
              f"t-stat {a['t_stat']:+.2f} | allowed by history gate: {g['trades']} trades, {g['expectancy_r']:+.3f}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=[], help="shares to scan for NEW entries")
    ap.add_argument("--capital", type=float, default=cfg.DEFAULT_CAPITAL, help="free cash for new trades")
    ap.add_argument("--holdings", default=hd.PATH, help="CSV with symbol,qty,buy_price of shares you already own")
    ap.add_argument("--holding", action="append", default=[], metavar="SYMBOL:QTY:BUYPRICE",
                    help="add a holding on the command line (repeatable)")
    ap.add_argument("--buylist", default=bl.PATH, help="CSV with symbol,amount,qty,max_price of shares you WANT to buy")
    ap.add_argument("--buy", action="append", default=[], metavar="SYMBOL:AMOUNT[:MAXPRICE]",
                    help="a share you want to buy, e.g. RELIANCE:50000:2950 (repeatable)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--force", action="store_true", help="run even when market is closed")
    a = ap.parse_args()
    symbols = [normalize(s) for s in (a.symbols or [])]
    if not symbols and not a.holding and not a.buy:
        nothing_else = not hd.load(a.holdings) and not bl.load(a.buylist)
        symbols = [normalize(s) for s in cfg.DEFAULT_SYMBOLS] if nothing_else else []
    src, store, n, held, wants = get_source(), Store(), 0, {}, {}
    try:
        while True:
            now = dt.datetime.now(IST)
            if a.force or market_open(now) or n == 0:   # first cycle always runs so you see output
                try:
                    held = hd.load(a.holdings, a.holding)  # re-read every cycle: edit the CSV while running
                except Exception as e:                     # e.g. file locked by Excel: keep last known holdings
                    print(f"WARNING: could not read {a.holdings} ({e}); using previous holdings")
                try:
                    wants = bl.load(a.buylist, a.buy)
                except Exception as e:
                    print(f"WARNING: could not read {a.buylist} ({e}); using previous buy list")
                out = run_cycle(src, store, symbols, a.capital, now, daily_due=(n % 15 == 0), held=held, wants=wants)
                print_table(out)
                if not market_open(now):
                    print("(market closed - showing last available data)")
                n += 1
            else:
                print(f"\r{now:%H:%M:%S} market closed, waiting...", end="", flush=True)
            if a.once:
                break
            time.sleep(cfg.REFRESH_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
