"""Run:  streamlit run dashboard.py   (starts its own analysis engine)"""
import hmac
import json
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tradesathi.storage import buylist as bl
from tradesathi import config as cfg
from tradesathi import engine
from tradesathi.storage import holdings as hd
from tradesathi.analysis import paper
from tradesathi.storage import persist
from tradesathi.analysis.history import hist_str
from tradesathi.analysis.indicators import add_all
from tradesathi.analysis.patterns import BIAS, detect
from tradesathi.storage.store import Store
from tradesathi.analysis.strategy import resample

st.set_page_config(page_title="Trading Analyzer", layout="wide")
st.title("Live Trading Analyzer")
store = Store()


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

engine.ensure_started()         # background analysis thread (skipped if TRADESATHI_EXTERNAL_ENGINE=1, i.e. python -m tradesathi.main runs the engine)
st.sidebar.caption(f"Your data is stored in: {persist.backend().name}")

# ---- holdings input (outside the auto-refreshing part so edits are not lost) ----
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
                module.save(pd.concat([module.read(), pd.DataFrame([row])], ignore_index=True))
                st.rerun()

    df = module.read()
    d1, d2 = st.columns([5, 1])
    gone = d1.multiselect("Select rows to delete", list(df.index), key=f"{key}_del",
                          format_func=lambda i: " | ".join(str(v) for v in df.loc[i] if pd.notna(v)))
    d2.write("")
    d2.write("")
    if d2.button("Delete", key=f"{key}_delbtn") and gone:
        module.save(df.drop(index=gone))
        st.session_state.pop(f"{key}_del", None)
        st.session_state.pop(f"{key}_editor", None)
        st.rerun()

    edited = st.data_editor(df, num_rows="dynamic", width="stretch", key=f"{key}_editor", column_config=columns)
    if st.button("Save changes", key=f"{key}_save"):
        module.save(edited)
        st.rerun()


with st.expander("My holdings (shares I already bought): add, edit or delete"):
    st.caption("Add a purchase with the form, edit cells directly, or pick rows and click Delete. Several rows for the "
               "same share are merged at the average price. The engine picks up changes within a minute.")
    manage("hold", hd, {
        "symbol": st.column_config.TextColumn("Symbol (e.g. RELIANCE)"),
        "qty": st.column_config.NumberColumn("Quantity", min_value=1, step=1),
        "buy_price": st.column_config.NumberColumn("Buy price", min_value=0.01, format="%.2f"),
    }, [("symbol", "Symbol", "text"), ("qty", "Quantity", "num"), ("buy_price", "Buy price", "num")],
        lambda r: r["qty"] and r["buy_price"])

with st.expander("Shares I want to BUY: add, edit or delete"):
    st.caption("Fill Amount (rupees to invest) OR Quantity. Max price is optional: the most you will pay per share. "
               "The engine tells you when and at what price to buy, within a minute of saving.")
    manage("buy", bl, {
        "symbol": st.column_config.TextColumn("Symbol (e.g. TCS)"),
        "amount": st.column_config.NumberColumn("Amount to invest", min_value=1.0, step=1000.0, format="%.0f"),
        "qty": st.column_config.NumberColumn("or Quantity", min_value=1, step=1),
        "max_price": st.column_config.NumberColumn("Max price (optional)", min_value=0.01, format="%.2f"),
    }, [("symbol", "Symbol", "text"), ("amount", "Amount to invest", "num"), ("qty", "or Quantity", "num"),
        ("max_price", "Max price (optional)", "num")],
        lambda r: r["amount"] or r["qty"])


@st.fragment(run_every="30s")
def view():
    path = os.path.join(cfg.DATA_DIR, "signals.json")
    if not os.path.exists(path):
        if engine.external():
            st.warning("No data yet. Start the engine first: python -m tradesathi.main")
        elif engine.STATE["last_error"]:
            st.error(f"The engine hit an error: {engine.STATE['last_error']}")
        else:
            st.info("The engine is starting: the first run downloads price history and can take a few minutes. "
                    "This page refreshes by itself.")
        return
    data = json.load(open(path))
    st.caption(f"Last update: {data['updated'][:19]} IST  |  Delayed free data, not investment advice")
    held_rows = {m["symbol"]: m for m in data.get("holdings", []) if "error" not in m}
    wanted_rows = {p["symbol"]: p for p in data.get("buylist", []) if "error" not in p}

    # ---------------- my holdings ----------------
    if data.get("holdings"):
        st.subheader("My holdings")
        for m in data["holdings"]:
            if "error" in m:
                st.error(f"{m['symbol']}: {m['error']}")
        p = data["portfolio"]
        c = st.columns(4)
        c[0].metric("Invested", f"{p['invested']:,.0f}")
        c[1].metric("Current value", f"{p['value']:,.0f}")
        c[2].metric("P&L", f"{p['pnl']:,.0f}", f"{p['pnl_pct']}%")
        c[3].metric("P&L if all stops hit", f"{p['pnl_at_stops']:,.0f}")
        if held_rows:
            st.dataframe(pd.DataFrame([{
                "Symbol": m["symbol"], "Qty": m["qty"], "Buy": m["buy_price"], "Price": m["price"],
                "P&L": m["pnl"], "P&L %": m["pnl_pct"], "Action": m["action"],
                "Stop": m["stop"], "Stop type": m["stop_type"], "T1": m["t1"], "T2": m["t2"],
                "Breakeven": m["breakeven"], "Money at risk": m["money_at_risk"], "History": hist_str(m.get("history"))} for m in held_rows.values()]),
                hide_index=True, width="stretch")
            for m in held_rows.values():
                for w in m["why"]:
                    st.caption(f"{m['symbol']}: {w}")

    # ---------------- shares I want to buy: when to buy ----------------
    if data.get("buylist"):
        st.subheader("Shares I want to buy: when to buy")
        for p in data["buylist"]:
            if "error" in p:
                st.error(f"{p['symbol']}: {p['error']}")
        if wanted_rows:
            go_now = [s for s, p in wanted_rows.items() if p["verdict"] == "BUY NOW" and p.get("confirmed", True)]
            if go_now:
                st.success("BUY signal now: " + ", ".join(go_now))
            st.dataframe(pd.DataFrame([{
                "Symbol": p["symbol"], "Price": p["price"], "Verdict": p["verdict"],
                "Buy zone": f"{p['buy_zone_low']}-{p['buy_zone_high']}", "Limit price": p["limit"],
                "Price must fall %": p["gap_pct"] if p["verdict"] == "PLACE LIMIT ORDER" else None,
                "Stop": p["stop"], "T1": p["t1"], "T2": p["t2"], "Qty": p["qty"], "Cost": p["cost"],
                "Max loss": p["max_loss"], "Max loss % of cost": p["max_loss_pct"],
                "History": hist_str(p.get("history"))} for p in wanted_rows.values()]),
                hide_index=True, width="stretch")
            for p in wanted_rows.values():
                getattr(st, p["level"])(f"**{p['symbol']}: {p['verdict']}**\n\n{p['when']}")
                if p["steps"]:
                    st.markdown("\n".join(f"- {s}" for s in p["steps"]))
                for n in p["notes"]:
                    st.caption(f"{p['symbol']}: {n}")
            st.caption("WAIT rows show the buy zone it would use if the signal turns to BUY. "
                       "A limit order is good for a few trading days; re-check after that.")

    # ---------------- new ideas ----------------
    ok = [r for r in data["results"] if "error" not in r]
    for r in data["results"]:
        if "error" in r and r["symbol"] not in held_rows and r["symbol"] not in data.get("wanted", []):
            st.error(f"{r['symbol']}: {r['error']}")
    ideas = [r for r in ok if r["symbol"] not in data.get("held", []) and r["symbol"] not in data.get("wanted", [])]
    if ideas:
        st.subheader("New ideas")
        st.dataframe(pd.DataFrame([{
            "Symbol": r["symbol"], "Price": r["price"], "Action": r["action"], "Score": r["score"],
            "Entry low": r["entry_low"], "Entry high": r["entry_high"], "Stop": r["stop"],
            "T1": r["t1"], "T2": r["t2"], "Support": r["support"], "Resistance": r["resistance"],
            "RSI": r["rsi"], "History": hist_str(r.get("history"))} for r in ideas]), hide_index=True, width="stretch")
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

    ps = data.get("paper")
    if ps:
        st.subheader("Paper trades (every live BUY NOW followed automatically, no real orders)")
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
        st.caption("This is the real test of the live signal. Judge it only after 50+ closed trades: "
                   "expectancy above 0 with a t-stat of 2 or more.")

    hist = store.read_json("history.json")
    if hist:
        st.subheader("Historical check (same rules replayed on past candles)")
        st.dataframe(pd.DataFrame([{
            "Symbol": k, "Verdict": v["stats"]["edge"], "Test": v["stats"].get("method", "daily"),
            "Trades": v["stats"]["trades"], "Win %": v["stats"]["win_rate"], "Avg win (R)": v["stats"]["avg_win_r"],
            "Avg loss (R)": v["stats"]["avg_loss_r"], "Expectancy (R)": v["stats"]["expectancy_r"],
            "t-stat": v["stats"].get("t_stat"), "Profit factor": v["stats"]["profit_factor"],
            "Max drawdown %": v["stats"]["max_dd_pct"], "Strategy CAGR %": v["stats"]["cagr_pct"],
            "Buy&hold CAGR %": v["stats"].get("buy_hold_cagr_pct"), "Years": v["stats"]["years"],
            "As of": v["asof"]} for k, v in hist.items()]),
            hide_index=True, width="stretch")
        st.caption("Expectancy (R) = average profit per trade in units of the amount risked, after estimated charges. "
                   f"Only verdicts in {', '.join(cfg.ALLOWED_EDGES)} may BUY (EDGE needs a t-stat of "
                   f"{cfg.EDGE_MIN_TSTAT:g}+, i.e. unlikely to be luck).")

    if not ok:
        return
    sym = st.selectbox("Chart", [r["symbol"] for r in ok])
    r = next(x for x in ok if x["symbol"] == sym)
    m = held_rows.get(sym)
    bars = resample(store.load(sym, "1m"), cfg.INTRA_RULE)
    bars = bars[bars.index >= bars.index[-1].normalize() - pd.Timedelta(days=3)]
    b = add_all(bars, intraday=True)

    fig = go.Figure(go.Candlestick(x=b.index, open=b.open, high=b.high, low=b.low, close=b.close, name=sym))
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
    if m:   # you own it: show YOUR buy price, stop and targets
        fig.add_hline(y=m["buy_price"], line_color="royalblue", annotation_text=f"Your buy {m['buy_price']}")
        fig.add_hline(y=m["stop"], line_dash="dash", line_color="red", annotation_text=f"Stop ({m['stop_type']})")
        for k in ("t1", "t2"):
            if m[k]:
                fig.add_hline(y=m[k], line_dash="dash", line_color="green", annotation_text=k.upper())
    else:
        fig.add_hrect(y0=r["entry_low"], y1=r["entry_high"], fillcolor="green", opacity=0.15, line_width=0,
                      annotation_text="Entry zone")
        fig.add_hline(y=r["stop"], line_dash="dash", line_color="red", annotation_text="Stop")
        fig.add_hline(y=r["t1"], line_dash="dash", line_color="green", annotation_text="T1")
        fig.add_hline(y=r["t2"], line_dash="dash", line_color="green", annotation_text="T2")
    wp = wanted_rows.get(sym)
    if wp and wp.get("limit"):
        fig.add_hline(y=wp["limit"], line_color="royalblue", annotation_text=f"Your limit buy {wp['limit']}")
    fig.add_hline(y=r["support"], line_dash="dot", line_color="gray", annotation_text="Support")
    if r["resistance"]:
        fig.add_hline(y=r["resistance"], line_dash="dot", line_color="gray", annotation_text="Resistance")
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.5, 9.25], pattern="hour")])
    fig.update_layout(height=650, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")

    left, right = st.columns(2)
    with left:
        st.markdown("**Why this score**")
        st.write("\n".join(f"- {w}" for w in r["why"]))
    with right:
        st.markdown("**Candle patterns detected**")
        st.write(", ".join(p["name"] for p in r["patterns"]) or "None in the latest candles")


view()
