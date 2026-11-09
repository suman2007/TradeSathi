"""Run:  streamlit run dashboard.py   (keep main.py running in another terminal)

Layout: left sidebar = names only (click one to open it); main area = one page at a time:
Overview (portfolio + chart + holdings / buy list / buy-and-hold vs strategy), a share's detail page,
New ideas, Paper trades, Historical check, Manage lists."""
import datetime as dt
import hmac
import json
import os
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tradesathi.storage import buylist as bl
from tradesathi import config as cfg
from tradesathi import engine, mode, visitor
from tradesathi.storage import holdings as hd
from tradesathi.analysis import paper
from tradesathi.storage import persist
from tradesathi.analysis.history import NO_HISTORY, hist_str
from tradesathi.analysis.indicators import add_all
from tradesathi.analysis.patterns import BIAS, detect
from tradesathi.storage.store import Store
from tradesathi.analysis.strategy import resample

st.set_page_config(page_title="Trading Analyzer", layout="wide", initial_sidebar_state="expanded")
store = Store()
IST = ZoneInfo(cfg.TZ_NAME)
PUBLIC = mode.is_public()      # public URL: every visitor's lists live only in their own browser tab


def _secret(name):
    try:
        return st.secrets.get(name)
    except Exception:           # no secrets file when running locally
        return None


# ---- optional password (set app_password in the app secrets; recommended on a public URL) ----
_pw = _secret("app_password")
if _pw and not st.session_state.get("authed"):
    typed = st.text_input("Password", type="password")
    if typed and hmac.compare_digest(str(typed), str(_pw)):
        st.session_state["authed"] = True
        st.rerun()
    elif typed:
        st.error("Wrong password.")
    st.stop()

if not PUBLIC:
    engine.ensure_started()     # background analysis thread (skipped if TRADESATHI_EXTERNAL_ENGINE=1, i.e. python -m tradesathi.main runs the engine)
VERDICT_DOT = {"BUY NOW": "🟢", "PLACE LIMIT ORDER": "🔵", "WAIT": "🟡", "DO NOT BUY": "🔴"}
st.session_state.setdefault("page", "overview")
st.session_state.setdefault("sym", None)


# ------------------------------------------------------------------ helpers
def load_data():
    if PUBLIC:
        return visitor.visitor_data(st.session_state)
    path = os.path.join(cfg.DATA_DIR, "signals.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):                     # engine is writing the file right now
        return None


def split(data):
    """held_rows, wanted_rows, ok results, new ideas."""
    held = {m["symbol"]: m for m in data.get("holdings", []) if "error" not in m}
    wanted = {p["symbol"]: p for p in data.get("buylist", []) if "error" not in p}
    ok = [r for r in data["results"] if "error" not in r]
    ideas = [r for r in ok if r["symbol"] not in data.get("held", []) and r["symbol"] not in data.get("wanted", [])]
    return held, wanted, ok, ideas


def short(sym):
    return sym.replace(".NS", "")


def num(x, fmt="{:,.2f}"):
    return "-" if x is None or (isinstance(x, float) and pd.isna(x)) else fmt.format(x)


def _age(sec):
    sec = int(max(sec, 0))
    if sec < 90:
        return f"{sec}s ago"
    if sec < 5400:
        return f"{sec // 60} min ago"
    if sec < 172800:
        return f"{sec // 3600} h ago"
    return f"{sec // 86400} days ago"


def market_state():
    now = dt.datetime.now(IST)
    return now, cfg.market_open(now)


def status_bar(updated_iso):
    """Date + time of the data, how old it is, and whether the market is open."""
    upd = dt.datetime.fromisoformat(updated_iso).astimezone(IST)
    now, is_open = market_state()
    age = (now - upd).total_seconds()
    c = st.columns([3, 2, 2])
    c[0].metric("Data as of (IST)", upd.strftime("%d %b %Y, %H:%M:%S"), upd.strftime("%A"), delta_color="off")
    c[1].metric("Age of data", _age(age))
    c[2].metric("Market", "OPEN" if is_open else "CLOSED", f"now {now.strftime('%d %b %H:%M:%S')}", delta_color="off")
    if is_open and not PUBLIC and age > 3 * cfg.REFRESH_SECONDS:
        st.error(f"Data is stale: last updated {_age(age)} while the market is open. "
                 "The analysis engine may have stopped: reload the page" +
                 (" or restart python -m tradesathi.main." if engine.external() else "."))
    elif not is_open:
        st.caption("Market closed: prices are from the last session (Mon-Fri 09:15-15:30 IST, plus special sessions in config). "
                   "Delayed free data, not investment advice.")
    else:
        st.caption("Delayed free data, not investment advice.")


# ------------------------------------------------------------------ sidebar (names only)
def go_to(page, sym=None):
    st.session_state.page, st.session_state.sym = page, sym


def nav(label, page, sym=None, scope=""):
    active = st.session_state.page == page and st.session_state.sym == sym
    st.button(label, key=f"nav_{scope}_{page}_{sym}", on_click=go_to, args=(page, sym),
              type="primary" if active else "secondary", width="stretch")


def sidebar(data):
    with st.sidebar:
        st.markdown("### Trading Analyzer")
        st.markdown(":violet-background[🤖 **AI-generated suggestions** · guide only · subject to market risk]")
        now, is_open = market_state()
        if data:
            upd = dt.datetime.fromisoformat(data["updated"]).astimezone(IST)
            st.caption(f"{'🟢 Market open' if is_open else '⚪ Market closed'} · updated {upd:%d %b %H:%M:%S}")
        nav("📊 Overview", "overview")
        if data:
            held, wanted, _, ideas = split(data)
            if held:
                with st.expander(f"My holdings ({len(held)})", expanded=True):
                    for s, m in held.items():
                        nav(f"{'🟢' if (m['pnl'] or 0) >= 0 else '🔴'} {short(s)}  {m['pnl_pct']:+.1f}%", "share", s, "held")
            if wanted:
                with st.expander(f"Want to buy ({len(wanted)})", expanded=True):
                    for s, p in wanted.items():
                        nav(f"{VERDICT_DOT.get(p['verdict'], '⚪')} {short(s)}  {p['verdict'].title()}", "share", s, "want")
            if ideas:
                with st.expander(f"New ideas ({len(ideas)})", expanded=False):
                    for r in ideas:
                        nav(f"{short(r['symbol'])}  {r['action'][:18]}", "share", r["symbol"], "idea")
        st.divider()
        if not PUBLIC:
            nav("📝 Paper trades", "paper")
        nav("🧪 Historical check", "history")
        nav("🛠 Manage lists", "manage")
        if not PUBLIC:
            nav("💡 New ideas table", "ideas")
        if PUBLIC:
            st.caption("🔒 Private to you: your lists exist only in this browser tab. Nothing is saved on the server; "
                       "download them under Manage lists to keep them.")
        else:
            st.caption(f"Your data is stored in: {persist.backend().name}")


# ------------------------------------------------------------------ chart
def price_chart(sym, r, held_row=None, want_row=None, height=520):
    span = st.radio("Range", ["Live", "1D", "5D", "1M", "1Y"], horizontal=True, key=f"rng_{sym}_{height}",
                    label_visibility="collapsed",
                    help="Live = last 90 min with a tick every 2 min · 1D/5D = intraday candles · 1M/1Y = daily candles")
    intraday = span in ("Live", "1D", "5D")
    if intraday:
        bars = resample(store.load(sym, "1m"), cfg.INTRA_RULE)
        if not bars.empty and span != "Live":
            sessions = sorted(set(bars.index.normalize()))[-(5 if span == "5D" else 1):]
            bars = bars[bars.index.normalize() >= sessions[0]]
    else:
        bars = store.load(sym, "1d")
        if not bars.empty:
            bars = bars[bars.index >= bars.index[-1] - pd.Timedelta(days=30 if span == "1M" else 365)]
    if bars.empty:
        st.info(f"No {'intraday' if intraday else 'daily'} data stored for {sym} yet.")
        return
    b = add_all(bars, intraday=intraday)
    fig = go.Figure(go.Candlestick(x=b.index, open=b.open, high=b.high, low=b.low, close=b.close, name=sym))
    if intraday:
        fig.add_trace(go.Scatter(x=b.index, y=b.vwap, name="VWAP", line=dict(width=1.2)))
    fig.add_trace(go.Scatter(x=b.index, y=b.ema20, name="EMA20", line=dict(width=1.2)))
    pat = detect(b)
    for name, bias in BIAS.items():
        if bias == 0:
            continue
        idx = pat.index[pat[name]]
        y = b.loc[idx, "low"] * 0.998 if bias > 0 else b.loc[idx, "high"] * 1.002
        fig.add_trace(go.Scatter(x=idx, y=y, mode="markers", name=name, hovertext=name,
                                 marker=dict(symbol="triangle-up" if bias > 0 else "triangle-down", size=10)))
    if held_row:   # you own it: show YOUR buy price, stop and targets
        fig.add_hline(y=held_row["buy_price"], line_color="royalblue", annotation_text=f"Your buy {held_row['buy_price']}")
        fig.add_hline(y=held_row["stop"], line_dash="dash", line_color="red", annotation_text=f"Stop ({held_row['stop_type']})")
        for k in ("t1", "t2"):
            if held_row[k]:
                fig.add_hline(y=held_row[k], line_dash="dash", line_color="green", annotation_text=k.upper())
    else:
        fig.add_hrect(y0=r["entry_low"], y1=r["entry_high"], fillcolor="green", opacity=0.15, line_width=0,
                      annotation_text="Entry zone")
        fig.add_hline(y=r["stop"], line_dash="dash", line_color="red", annotation_text="Stop")
        fig.add_hline(y=r["t1"], line_dash="dash", line_color="green", annotation_text="T1")
        fig.add_hline(y=r["t2"], line_dash="dash", line_color="green", annotation_text="T2")
    if want_row and want_row.get("limit"):
        fig.add_hline(y=want_row["limit"], line_color="royalblue", annotation_text=f"Your limit buy {want_row['limit']}")
    fig.add_hline(y=r["support"], line_dash="dot", line_color="gray", annotation_text="Support")
    if r["resistance"]:
        fig.add_hline(y=r["resistance"], line_dash="dot", line_color="gray", annotation_text="Resistance")
    if intraday:
        days = pd.date_range(b.index[0].normalize(), b.index[-1].normalize() + pd.Timedelta(days=1), freq="D")
        off_days = [d.strftime("%Y-%m-%d") for d in days
                    if d.weekday() >= 5 and d.strftime("%Y-%m-%d") not in cfg.SPECIAL_SESSIONS]   # weekends, except special sessions
        fig.update_xaxes(rangebreaks=[dict(values=off_days), dict(bounds=[15.5, 9.25], pattern="hour")])
        if span == "Live":    # last 90 min, a tick every 2 minutes, date included
            fig.update_xaxes(dtick=2 * 60 * 1000, tickformat="%d %b %H:%M", tickangle=-90, tickfont=dict(size=9),
                             range=[b.index[-1] - pd.Timedelta(minutes=90), b.index[-1] + pd.Timedelta(minutes=10)])
        elif span == "1D":
            fig.update_xaxes(dtick=30 * 60 * 1000, tickformat="%H:%M<br>%d %b")
        else:
            fig.update_xaxes(tickformat="%d %b<br>%H:%M")
    else:
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])], tickformat="%d %b %Y" if span == "1Y" else "%d %b")
    fig.update_layout(height=height, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")


def buy_hold_chart(symbols):
    """Strategy CAGR vs simply buying and holding, from the historical check."""
    hist = store.read_json("history.json") or {}
    rows = []
    for s in symbols:
        st_ = (hist.get(s) or {}).get("stats") or {}
        if st_.get("years") and (st_.get("cagr_pct") is not None or st_.get("buy_hold_cagr_pct") is not None):
            rows.append((short(s), st_.get("cagr_pct"), st_.get("buy_hold_cagr_pct")))
    if not rows:
        st.info("No historical results yet for these shares (new listings need about a year of history).")
        return
    names, strat, bh = zip(*rows)
    fig = go.Figure([go.Bar(x=names, y=strat, name="This strategy (CAGR %)"),
                     go.Bar(x=names, y=bh, name="Buy & hold (CAGR %)")])
    fig.update_layout(barmode="group", height=340, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="% per year")
    st.plotly_chart(fig, width="stretch")
    st.caption("Each share's past results (about 5 years). Where the strategy bar is below buy & hold, simply holding "
               "the share would have done better. Shares not listed have too little history to test.")


# ------------------------------------------------------------------ pages
def page_overview(data):
    held, wanted, ok, _ = split(data)
    status_bar(data["updated"])
    problems = [f"{m['symbol']}: {m['error']}" for m in data.get("holdings", []) if "error" in m] + \
               [f"{p['symbol']}: {p['error']}" for p in data.get("buylist", []) if "error" in p]
    if problems:
        with st.expander(f"Data problems ({len(problems)})"):
            for t in problems:
                st.error(t)
    if held:
        p = data["portfolio"]
        c = st.columns(4)
        c[0].metric("Invested", f"{p['invested']:,.0f}")
        c[1].metric("Current value", f"{p['value']:,.0f}")
        c[2].metric("P&L", f"{p['pnl']:,.0f}", f"{p['pnl_pct']}%")
        c[3].metric("P&L if all stops hit", f"{p['pnl_at_stops']:,.0f}")
    go_now = [s for s, p in wanted.items() if p["verdict"] == "BUY NOW" and p.get("confirmed", True)]
    if go_now:
        st.success("BUY signal now: " + ", ".join(go_now))

    by_sym = {r["symbol"]: r for r in ok}
    order = [s for s in list(held) + list(wanted) + [r["symbol"] for r in ok] if s in by_sym]
    order = list(dict.fromkeys(order))                     # first holding first, then buy list, then the rest
    if order:
        sym = st.selectbox("Chart", order, key="ov_sym", format_func=short)
        price_chart(sym, by_sym[sym], held.get(sym), wanted.get(sym))

    t1, t2, t3 = st.tabs(["My holdings", "Want to buy", "Buy & hold vs strategy"])
    with t1:
        if held:
            st.dataframe(pd.DataFrame([{"Symbol": short(s), "Qty": m["qty"], "Buy": m["buy_price"], "Price": m["price"],
                                        "P&L %": m["pnl_pct"], "Action": m["action"], "Stop": m["stop"], "T1": m["t1"]}
                                       for s, m in held.items()]), hide_index=True, width="stretch")
            st.caption("Click a name in the left pane for the full detail of a share.")
        else:
            st.info("No holdings yet. Add them under Manage lists.")
    with t2:
        if wanted:
            st.dataframe(pd.DataFrame([{"Symbol": short(s), "Price": p["price"], "Verdict": p["verdict"],
                                        "Buy zone": f"{p['buy_zone_low']}-{p['buy_zone_high']}", "Limit": p["limit"],
                                        "Stop": p["stop"], "History": hist_str(p.get("history"))}
                                       for s, p in wanted.items()]), hide_index=True, width="stretch")
            st.caption("WAIT rows show the buy zone it would use if the signal turns to BUY. "
                       "Click a name in the left pane for the reason behind each verdict.")
        else:
            st.info("Nothing on your buy list. Add shares under Manage lists.")
    with t3:
        buy_hold_chart(list(held) + list(wanted))


def page_share(data):
    sym = st.session_state.sym
    held, wanted, ok, _ = split(data)
    r = next((x for x in ok if x["symbol"] == sym), None)
    if sym is None or r is None:
        st.info("That share is no longer in the data. Pick another from the left pane.")
        return
    m, p = held.get(sym), wanted.get(sym)
    status_bar(data["updated"])
    st.subheader(sym)
    if m:
        c = st.columns(5)
        c[0].metric("Price", num(m["price"]))
        c[1].metric("P&L", num(m["pnl"], "{:,.0f}"), f"{m['pnl_pct']}%")
        c[2].metric("Qty x buy", f"{m['qty']:g} x {num(m['buy_price'])}")
        c[3].metric(f"Stop ({m['stop_type']})", num(m["stop"]))
        c[4].metric("Targets T1 / T2", f"{num(m['t1'])} / {num(m['t2'])}")
        c = st.columns(3)
        c[0].metric("Breakeven", num(m["breakeven"]))
        c[1].metric("Money at risk", num(m["money_at_risk"], "{:,.0f}"))
        c[2].metric("Action", m["action"])
    if p:
        getattr(st, p["level"])(f"**{short(sym)}: {p['verdict']}**\n\n{p['when']}")
        c = st.columns(5)
        c[0].metric("Price", num(p["price"]))
        c[1].metric("Buy zone", f"{num(p['buy_zone_low'])}-{num(p['buy_zone_high'])}")
        c[2].metric("Limit price", num(p["limit"]))
        c[3].metric("Stop", num(p["stop"]))
        c[4].metric("T1 / T2", f"{num(p['t1'])} / {num(p['t2'])}")
        if p["qty"]:
            c = st.columns(4)
            c[0].metric("Qty", p["qty"])
            c[1].metric("Cost", num(p["cost"], "{:,.0f}"))
            c[2].metric("Max loss", num(p["max_loss"], "{:,.0f}"), f"{p['max_loss_pct']}% of cost", delta_color="off")
            c[3].metric("Price must fall %", num(p["gap_pct"]) if p["verdict"] == "PLACE LIMIT ORDER" else "-")
        if p["steps"]:
            st.markdown("\n".join(f"- {s}" for s in p["steps"]))
        for n in p["notes"]:
            st.caption(n)
    if not m and not p:
        c = st.columns(5)
        c[0].metric("Price", num(r["price"]))
        c[1].metric("Action", r["action"])
        c[2].metric("Score", r["score"])
        c[3].metric("Entry zone", f"{num(r['entry_low'])}-{num(r['entry_high'])}")
        c[4].metric("Stop / T1", f"{num(r['stop'])} / {num(r['t1'])}")

    price_chart(sym, r, m, p, height=480)
    tw, tp, th = st.tabs(["Why this score", "Candle patterns", "Historical check"])
    with tw:
        st.write("\n".join(f"- {w}" for w in r["why"]))
    with tp:
        st.write(", ".join(x["name"] for x in r["patterns"]) or "None in the latest candles")
    with th:
        s = ((store.read_json("history.json") or {}).get(sym) or {}).get("stats")
        if not s:
            st.info("No historical check stored for this share yet.")
        elif s.get("edge") == NO_HISTORY:
            st.warning(f"{NO_HISTORY}: {s.get('history_years', 0):.2f} years, {s.get('history_bars', 0)} daily bars. "
                       "The rules cannot be tested yet.")
        else:
            c = st.columns(5)
            c[0].metric("Verdict", s["edge"])
            c[1].metric("Trades", s["trades"], f"win {s['win_rate']}%", delta_color="off")
            c[2].metric("Expectancy (R)", f"{s['expectancy_r']:+.3f}", f"t-stat {s.get('t_stat') or 0:+.2f}", delta_color="off")
            c[3].metric("Strategy vs buy & hold (CAGR)", f"{num(s['cagr_pct'], '{:.1f}')}% vs {num(s.get('buy_hold_cagr_pct'), '{:.1f}')}%")
            c[4].metric("Max drawdown", f"{s['max_dd_pct']}%")


def page_ideas(data):
    _, _, _, ideas = split(data)
    for r in data["results"]:
        if "error" in r and r["symbol"] not in data.get("held", []) and r["symbol"] not in data.get("wanted", []):
            st.error(f"{r['symbol']}: {r['error']}")
    if not ideas:
        st.info("No new ideas right now.")
        return
    st.dataframe(pd.DataFrame([{
        "Symbol": short(r["symbol"]), "Price": r["price"], "Action": r["action"], "Score": r["score"],
        "Entry low": r["entry_low"], "Entry high": r["entry_high"], "Stop": r["stop"], "T1": r["t1"], "T2": r["t2"],
        "Support": r["support"], "Resistance": r["resistance"], "RSI": r["rsi"], "History": hist_str(r.get("history"))}
        for r in ideas]), hide_index=True, width="stretch")
    pl = data["plan"]
    s = pl["summary"]
    c = st.columns(4)
    c[0].metric("Free capital", f"{s['capital']:,.0f}")
    c[1].metric("New buys", f"{s['invested']:,.0f}")
    c[2].metric("Cash left", f"{s['cash_left']:,.0f}")
    c[3].metric("Worst-case loss", f"{s['worst_case_loss']:,.0f}", f"-{s['worst_case_pct']}%", delta_color="inverse")
    if pl["positions"]:
        st.dataframe(pd.DataFrame(pl["positions"]), hide_index=True, width="stretch")
    else:
        st.info("No valid BUY setups right now, so no new capital is allocated.")


def page_paper(data):
    ps = data.get("paper")
    if not ps:
        st.info("No paper trades yet.")
        return
    a, g = ps["all"], ps["allowed"]
    c = st.columns(4)
    c[0].metric("Open", ps["open"])
    c[1].metric("Closed trades", a["trades"], f"win {a['win_rate']}%")
    c[2].metric("Expectancy (R)", f"{a['expectancy_r']:+.3f}", f"t-stat {a['t_stat']:+.2f}")
    c[3].metric("Allowed by history gate", g["trades"], f"{g['expectancy_r']:+.3f}R")
    pt = paper.load(store)
    if len(pt):
        st.dataframe(pt[["symbol", "opened", "entry", "stop", "t1", "t2", "gated", "edge", "status", "t1_hit",
                         "exit_time", "exit_price", "reason", "r"]].iloc[::-1], hide_index=True, width="stretch")
    st.caption("Every live BUY NOW is followed automatically, no real orders. Judge it only after 50+ closed trades: "
               "expectancy above 0 with a t-stat of 2 or more.")


def page_history(data):
    hist = store.read_json("history.json")
    if PUBLIC:                                        # history.json also holds shares other visitors looked at
        hist = visitor.visitor_history(st.session_state, hist)
    if not hist:
        st.info("No historical check yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Symbol": short(k), "Verdict": v["stats"]["edge"], "Test": v["stats"].get("method") or "-",
        "Trades": v["stats"]["trades"], "Win %": v["stats"]["win_rate"], "Avg win (R)": v["stats"]["avg_win_r"],
        "Avg loss (R)": v["stats"]["avg_loss_r"], "Expectancy (R)": v["stats"]["expectancy_r"],
        "t-stat": v["stats"].get("t_stat"), "Profit factor": v["stats"]["profit_factor"],
        "Max drawdown %": v["stats"]["max_dd_pct"], "Strategy CAGR %": v["stats"]["cagr_pct"],
        "Buy&hold CAGR %": v["stats"].get("buy_hold_cagr_pct"), "Years": v["stats"]["years"],
        "As of": v["asof"]} for k, v in hist.items()]), hide_index=True, width="stretch")
    st.caption("Same rules replayed on past candles. Expectancy (R) = average profit per trade in units of the amount "
               f"risked, after estimated charges. Only verdicts in {', '.join(cfg.ALLOWED_EDGES)} may BUY "
               f"(EDGE needs a t-stat of {cfg.EDGE_MIN_TSTAT:g}+, i.e. unlikely to be luck).")


# ---- list editing: outside the auto-refreshing part so edits are not lost ----
def _save(module, df):
    try:
        module.save(df)
        return True
    except ValueError as e:                           # visitor mode: invalid symbol or too many shares
        st.error(str(e))
        return False


def manage(key, module, columns, add_fields, ready):
    """Add form + editable table with a Delete tick-box. `ready(row)` says whether a new row is complete."""
    with st.form(f"{key}_add", clear_on_submit=True):
        cols = st.columns(len(add_fields) + 1)
        row = {}
        for c, (name, label, kind) in zip(cols, add_fields):
            if kind == "text":
                row[name] = c.text_input(label, key=f"{key}_add_{name}")
            else:
                row[name] = c.number_input(label, min_value=0.0, value=0.0, key=f"{key}_add_{name}")
        cols[-1].write("")
        if cols[-1].form_submit_button("Add"):
            row = {k: (v.strip().upper() if isinstance(v, str) else (v or None)) for k, v in row.items()}
            if not row["symbol"] or not ready(row):
                st.error("Fill in the symbol and the required numbers.")
            else:
                if _save(module, pd.concat([module.read(), pd.DataFrame([row])], ignore_index=True)):
                    st.rerun()

    df = module.read()
    d1, d2 = st.columns([5, 1])
    gone = d1.multiselect("Select rows to delete", list(df.index), key=f"{key}_del",
                          format_func=lambda i: " | ".join(str(v) for v in df.loc[i] if pd.notna(v)))
    d2.write("")
    d2.write("")
    if d2.button("Delete", key=f"{key}_delbtn") and gone:
        if _save(module, df.drop(index=gone)):
            st.session_state.pop(f"{key}_del", None)
            st.session_state.pop(f"{key}_editor", None)
            st.rerun()

    edited = st.data_editor(df, num_rows="dynamic", width="stretch", key=f"{key}_editor", column_config=columns)
    if st.button("Save changes", key=f"{key}_save"):
        if _save(module, edited):
            st.rerun()


def backup_section(hold, buy):
    """Visitor mode: the only way to keep a list is a file on the visitor's own device."""
    st.divider()
    st.subheader("Keep your lists")
    st.caption("Nothing is saved on the server, so your lists disappear when you close or refresh this tab. "
               "Download them to your own device and upload them next time.")
    cols = st.columns(2)
    for col, label, lst, fname, key in ((cols[0], "holdings", hold, "holdings.csv", "hold"),
                                        (cols[1], "buy list", buy, "buylist.csv", "buy")):
        col.download_button(f"Download my {label}", lst.csv(), file_name=fname, mime="text/csv", key=f"dl_{key}")
        up = col.file_uploader(f"Upload {label} (CSV)", type="csv", key=f"up_{key}")
        if up is not None:
            fid = getattr(up, "file_id", None) or f"{up.name}:{up.size}"
            if st.session_state.get(f"up_{key}_done") != fid:
                try:
                    lst.upload(up.getvalue())
                    st.session_state[f"up_{key}_done"] = fid
                    st.rerun()
                except ValueError as e:
                    col.error(str(e))


def page_manage():
    hold_mod, buy_mod = visitor.session_lists(st.session_state) if PUBLIC else (hd, bl)
    if PUBLIC:
        st.info(f"🔒 **Private to you.** The lists you add exist only in this browser tab: nobody else can see them "
                f"and nothing is stored on the server. Up to {cfg.MAX_SYMBOLS_PER_VISITOR} different shares of your own "
                f"(holdings and buy list together; the default list does not count).")
    st.subheader("My holdings (shares I already bought)")
    st.caption("Add a purchase with the form, edit cells directly, or pick rows and click Delete. Several rows for the "
               "same share are merged at the average price. The engine picks up changes within a minute.")
    manage("hold", hold_mod, {
        "symbol": st.column_config.TextColumn("Symbol (e.g. RELIANCE)"),
        "qty": st.column_config.NumberColumn("Quantity", min_value=1, step=1),
        "buy_price": st.column_config.NumberColumn("Buy price", min_value=0.01, format="%.2f"),
    }, [("symbol", "Symbol", "text"), ("qty", "Quantity", "num"), ("buy_price", "Buy price", "num")],
        lambda r: r["qty"] and r["buy_price"])
    st.divider()
    st.subheader("Shares I want to BUY")
    if PUBLIC:
        st.markdown("**Default buy list**: shown to every visitor, cannot be changed or removed.")
        st.dataframe(buy_mod.locked.rename(columns={"symbol": "Symbol", "amount": "Amount to invest", "qty": "Quantity",
                                                    "max_price": "Max price (optional)"}),
                     hide_index=True, width="stretch")
        st.markdown("**Your own additions**")
    st.caption("Fill Amount (rupees to invest) OR Quantity. Max price is optional: the most you will pay per share. "
               "The engine tells you when and at what price to buy, within a minute of saving.")
    manage("buy", buy_mod, {
        "symbol": st.column_config.TextColumn("Symbol (e.g. TCS)"),
        "amount": st.column_config.NumberColumn("Amount to invest", min_value=1.0, step=1000.0, format="%.0f"),
        "qty": st.column_config.NumberColumn("or Quantity", min_value=1, step=1),
        "max_price": st.column_config.NumberColumn("Max price (optional)", min_value=0.01, format="%.2f"),
    }, [("symbol", "Symbol", "text"), ("amount", "Amount to invest", "num"), ("qty", "or Quantity", "num"),
        ("max_price", "Max price (optional)", "num")],
        lambda r: r["amount"] or r["qty"])
    if PUBLIC:
        backup_section(hold_mod, buy_mod)


# ------------------------------------------------------------------ main
PAGES = {"overview": page_overview, "share": page_share, "ideas": page_ideas, "paper": page_paper,
         "history": page_history}


def _content():
    data = load_data()
    if data is None:
        if engine.external():
            st.warning("No data yet. Start the engine first: python -m tradesathi.main")
        elif engine.STATE["last_error"]:
            st.error(f"The engine hit an error: {engine.STATE['last_error']}")
        else:
            st.info("The engine is starting: the first run downloads price history and can take a few minutes. "
                    "This page refreshes by itself.")
        return
    if data["updated"] != st.session_state.get("sb_ts"):
        st.rerun()                                      # new engine cycle: rebuild the left pane too
    if PUBLIC and not visitor.has_own_lists(st.session_state):
        st.info("👋 **Welcome.** You are looking at the default buy list. Add the shares you own or want to buy under "
                "**🛠 Manage lists** (left pane); your lists stay private to this browser tab.")
    if data.get("pending"):
        st.info(f"Analysing {len(data['pending'])} share(s): {', '.join(short(x) for x in data['pending'])}. "
                "The first analysis of a share downloads its price history and can take a minute. "
                "This page updates by itself.")
    PAGES[st.session_state.page](data)


SEBI_NOTICE = """
**This is a DEMO application for learning/education only.** It is **not** registered with SEBI as a
Research Analyst, Investment Adviser or Stock Broker, and nothing shown here is investment advice, a
recommendation, or a solicitation to buy or sell any security. No real orders are placed.

**This is only for guide purpose. We can guide you, based on your preferred stock, on when to buy and sell.
Any investment is purely your own decision and is subject to market risk.**

**Please also note**
- Buy/sell levels, entry zones, stop-losses and targets are indicative, rule-based outputs, not assured outcomes.
- You alone are responsible for your trading decisions and any resulting profit or loss. The creators accept no liability for losses.
- Consider consulting a SEBI-registered Investment Adviser before investing, especially if you are unsure about your risk appetite.
- Only invest money you can afford to lose; never borrow to trade.
- Use stop-losses and position sizing, and avoid putting all your capital in a single stock.

**Investor protection reminders (based on SEBI investor-awareness guidance)**
- *Investments in the securities market are subject to market risks. Read all related documents carefully before investing.*
- Past performance and back-tests do not guarantee future results. You can lose part or all of your capital.
- Derivatives (F&O) trading carries high risk: SEBI studies show most individual traders incur net losses.
- Beware of anyone promising **assured/guaranteed returns** or "sure-shot" tips; such claims are a red flag.
- Deal only with **SEBI-registered** intermediaries. Verify registration on the SEBI website (sebi.gov.in -> Intermediaries/Market Participants).
- Never share your passwords, OTPs or demat/bank details with anyone claiming to offer trading tips.
- Make sure your KYC is complete and trade only through your own account with a registered broker.
- Market data here is free, delayed and may be inaccurate. Verify prices with your broker/exchange before acting.
- Grievances: first approach the intermediary/exchange, then escalate via SEBI SCORES (scores.sebi.gov.in) or the SMARTODR portal.
"""


AI_NOTICE = ("🤖 :violet-background[**ALL SUGGESTIONS IN THIS APP ARE GENERATED BY AI / AUTOMATED RULES.** "
             "They are not reviewed by a human or a SEBI-registered professional, and AI can be wrong.]")


@st.dialog("SEBI Investor Notice & Risk Disclosure", width="large", dismissible=False)
def sebi_gate():
    st.markdown(AI_NOTICE)
    st.markdown(SEBI_NOTICE)
    ok = st.checkbox("I have read and understood the above. I understand this is a demo and not investment advice.")
    if st.button("Continue to trading page", type="primary", disabled=not ok):
        st.session_state.sebi_ack = True
        st.rerun()


if not st.session_state.get("sebi_ack"):
    sebi_gate()
    st.stop()

_data = load_data()
sidebar(_data)
st.session_state.sb_ts = _data["updated"] if _data else None
if st.session_state.page == "manage":
    page_manage()
else:
    st.fragment(run_every="3s" if (_data or {}).get("pending") else "30s")(_content)()

st.divider()
st.caption("Created by **Suman Karmakar** · Contact: sumank.turbo@gmail.com · Demo for educational/guide purposes only · AI-generated suggestions · "
           "Investments are subject to market risk. Read all related documents carefully.")
