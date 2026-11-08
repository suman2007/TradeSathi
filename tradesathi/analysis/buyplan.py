"""'When should I buy?' advice for shares the user wants to buy.
Pure rules on top of strategy.analyze() (plus the historical edge gate). No prediction."""
import math

from tradesathi import config as cfg
from tradesathi.analysis.history import NO_HISTORY

BUY_NOW, LIMIT, WAIT, NO = "BUY NOW", "PLACE LIMIT ORDER", "WAIT", "DO NOT BUY"
LEVEL = {BUY_NOW: "success", LIMIT: "info", WAIT: "warning", NO: "error"}   # Streamlit box colour


def _r(x):
    return None if x is None else round(float(x), 2)


def _weak_points(why, n=4):
    """The score conditions that are currently against the trade."""
    return [w.split("  ", 1)[1] for w in why if w.startswith("-")][:n]


def _history_reason(h):
    """Why the historical check does not back this BUY."""
    if not h:
        return "there is no historical record for this share to check the rules against."
    if h["edge"] == NO_HISTORY:
        return (f"this share has only {h.get('history_years') or 0:.2f} years ({h.get('history_bars') or 0} daily bars) "
                f"of price history. The rules need at least {cfg.MIN_INTRADAY_YEARS:g} year of 5-minute history, or 260 "
                f"daily bars, to be tested, so they could not be checked. This is not a failed test: re-check once it has more history.")
    yrs, n, e, t = h["years"], h["trades"], h["expectancy_r"], h.get("t_stat") or 0
    if h["edge"] == "INSUFFICIENT DATA":
        return f"over the last {yrs:g} years the rules gave only {n} trades here, too few to judge (needs {cfg.MIN_TRADES})."
    if h["edge"] == "WEAK EDGE":
        return (f"over the last {yrs:g} years the rules made only a small profit here ({e:+.2f}R per trade over {n} "
                f"trades) and the t-stat {t:+.1f} is below {cfg.EDGE_MIN_TSTAT:g}, so it may have been luck.")
    return f"over the last {yrs:g} years the same rules lost money here ({e:+.2f}R per trade over {n} trades)."


def _split(entry, zone_low, stop, qty):
    """Two-part buy: half at `entry`, half at the bottom of the zone (better average if price dips further)."""
    if not cfg.SPLIT_ENTRY or qty < 2 or zone_low >= entry * 0.998 or zone_low <= stop:
        return [(entry, qty)]
    low_qty = qty // 2
    return [(entry, qty - low_qty), (zone_low, low_qty)]


def _not_actionable(res, verdict, when, amount, max_price):
    """WAIT keeps the analysis levels as 'what would apply if it turns to BUY'; no order, quantity or cost is shown.
    DO NOT BUY shows no levels at all."""
    keep = verdict == WAIT
    return {
        "symbol": res["symbol"], "price": res["price"], "verdict": verdict, "level": LEVEL[verdict], "when": when,
        "notes": [], "steps": [], "score": res["score"], "signal": res["signal"],
        "buy_zone_low": res["entry_low"], "buy_zone_high": res["entry_high"], "limit": None,
        "stop": res["stop"] if keep else None, "t1": res["t1"] if keep else None, "t2": res["t2"] if keep else None,
        "qty": None, "cost": None, "unused": None, "max_loss": None, "max_loss_pct": None, "profit_t1": None,
        "profit_t2": None, "stop_pct": None, "gap_pct": None, "tranches": [], "support": res["support"],
        "resistance": res["resistance"], "history": res.get("history"), "amount": amount, "max_price": max_price,
        "provisional": keep, "alert": False}


def make(res, amount=None, qty=None, max_price=None):
    price, atr = res["price"], res["atr"]
    lo, hi = res["entry_low"], res["entry_high"]
    sig, status, hist = res["signal"], res["zone_status"], res.get("history")
    score, support, resistance = res["score"], res["support"], res["resistance"]
    max_price = max_price if max_price and max_price > 0 else None
    weak = _weak_points(res["why"])
    notes, steps = [], []
    entry, stop = res["entry"], res["stop"]

    # ---------------- 1. verdict from the analysis ----------------
    if res["action"].startswith("SKIP (no proven"):
        unproven = bool(hist) and hist["edge"] == NO_HISTORY
        before = res.get("action_before_gate", "")
        also = ""
        if before.startswith("SKIP (resistance"):
            also = (f" Also, before the history check: resistance at {resistance} is too close to the buy zone "
                    f"(less than {cfg.MIN_RR}R of room).")
        elif before.startswith(("WAIT", "BUY NOW")):
            also = f" Before the history check the setup was: {before}."
        if unproven:
            return _not_actionable(res, WAIT, "Unverified. The signal says BUY, but " + _history_reason(hist) + also,
                                   amount, max_price)
        return _not_actionable(res, NO, "Do not buy. The signal says BUY, but " + _history_reason(hist) + also,
                               amount, max_price)
    if sig == "AVOID":
        return _not_actionable(res, NO, (
            f"Do not buy now. The trend is weak (score {score:g}). Against you: {'; '.join(weak)}. "
            f"A score of {cfg.BUY_SCORE} or more is needed before this becomes a buy."), amount, max_price)
    if (cfg.REQUIRE_HISTORICAL_EDGE and hist and hist["edge"] != NO_HISTORY and hist["edge"] not in cfg.ALLOWED_EDGES):
        # the gate in history.apply() only runs on BUY signals, so without this a share that can never pass it
        # would show WAIT forever and look as if a buy is coming
        return _not_actionable(res, NO, (
            f"Do not buy. Even if the signal turns to BUY (now {sig}, score {score:g}), it would be blocked: "
            + _history_reason(hist)), amount, max_price)
    if sig == "WAIT":
        return _not_actionable(res, WAIT, (
            f"Not yet. Score is {score:g} and needs {cfg.BUY_SCORE} or more. Still against you: "
            f"{'; '.join(weak) or 'nothing major, but not enough in favour'}. "
            f"If it turns to BUY, the buy zone would be {lo}-{hi}."), amount, max_price)
    if res["action"].startswith("SKIP (resistance"):
        return _not_actionable(res, WAIT, (
            f"Wait. Resistance at {resistance} is too close to the buy zone (less than {cfg.MIN_RR}R of room). "
            f"Buy after a clean close above {resistance}, or on a dip toward support {support}."), amount, max_price)
    if status == "IN_ZONE":
        verdict = BUY_NOW
    elif status == "WAIT_PULLBACK":
        verdict = LIMIT
    else:
        return _not_actionable(res, WAIT, (
            f"Wait. Price {price} has slipped below the buy zone {lo}-{hi}. "
            f"Buy only after it moves back into the zone."), amount, max_price)

    # ---------------- 2. entry price, honouring the user's max price ----------------
    if max_price is not None and entry > max_price:
        entry = round(float(max_price), 2)
        if entry < lo - cfg.EXTENDED_ATR * atr:        # cap is so low that the order would practically never fill
            return _not_actionable(res, WAIT, (
                f"Wait. Your max price {max_price:g} is {(lo - entry) / lo * 100:.1f}% below the buy zone {lo}-{hi}, "
                f"so an order there is unlikely to fill. Raise the max price to at least {lo} to buy in the zone, "
                f"or wait for the price to fall that far."), amount, max_price)
        notes.append(f"The limit is capped at your max price {max_price:g}.")
        verdict = LIMIT
        if entry < lo:
            notes.append(f"Your max price is below the buy zone ({lo}-{hi}); the order may not fill unless price falls further.")
    if entry < res["entry"]:
        stop = min(stop, entry - cfg.ATR_MIN_STOP_MULT * atr)
    risk = entry - stop
    t1, t2 = entry + cfg.T1_R * risk, entry + cfg.T2_R * risk
    gap = price - entry
    gap_pct = gap / price * 100

    # ---------------- 3. how many shares, how much risk ----------------
    if qty is None or qty <= 0:
        qty = int(amount // entry) if amount else 0
    tr = _split(entry, lo, stop, qty) if qty else []
    cost = sum(p * q for p, q in tr)
    fees = cost * cfg.CHARGES_PCT
    max_loss = sum(q * (p - stop) for p, q in tr) + fees
    profit_t1 = sum(q * (t1 - p) for p, q in tr) - fees
    profit_t2 = sum(q * (t2 - p) for p, q in tr) - fees
    stop_pct = risk / entry * 100
    if stop_pct > cfg.MAX_STOP_PCT:
        notes.append(f"The stop is {stop_pct:.1f}% below the entry: if it is hit you lose about that much of this money. "
                     f"Consider a smaller amount.")

    # ---------------- 4. the advice text ----------------
    if verdict == BUY_NOW:
        when = (f"Buy now. Trend, momentum and volume agree and price {price} is inside the buy zone {lo}-{hi}. "
                f"Use a limit order at {entry:.2f} or lower, not a market order.")
    else:
        when = (f"Do not buy at {price}. Place a limit buy at {entry:.2f} and wait for a pullback: "
                f"price must fall {gap_pct:.1f}% ({gap:.2f}) to reach it.")
        above_zone = (price - hi) / atr
        if above_zone > cfg.EXTENDED_ATR:
            notes.append(f"Price is {above_zone:.1f} ATR above the buy zone (extended). A pullback may not come; do not chase it.")
    if qty == 0:
        when = (f"The signal is {verdict}, but your amount ({amount:,.0f}) cannot buy even one share at {entry:.2f}. "
                f"Raise the amount to at least {math.ceil(entry)}.")
    else:
        for k, (p, q) in enumerate(tr):
            if k == 0:
                steps.append(f"{'Buy' if verdict == BUY_NOW else 'Limit order: buy'} {q} shares at {p:.2f}"
                             f"{' or lower' if verdict == BUY_NOW else ''}")
            else:
                steps.append(f"Second part: buy {q} shares at {p:.2f} only if price dips there")
        steps.append(f"Stop-loss {stop:.2f}: place a stop-loss order right after buying; it sells everything "
                     f"if price trades down to {stop:.2f}")
        steps.append(f"Sell half at T1 {t1:.2f} and move your stop to your cost price; sell the rest at T2 {t2:.2f}")
        steps.append(f"Time limit: if neither T2 nor the stop has been hit {cfg.BT_MAX_HOLD} trading days after you buy, "
                     f"sell everything at market (the history check assumes this exit)")
        if verdict == LIMIT:
            steps.append(f"Order is good for {cfg.BT_MAX_WAIT} trading days. If it has not filled, cancel and re-check. "
                         f"Also cancel it if price closes below {stop:.2f}.")

    return {
        "symbol": res["symbol"], "price": price, "verdict": verdict, "level": LEVEL[verdict], "when": when,
        "notes": notes, "steps": steps, "score": score, "signal": sig, "buy_zone_low": lo, "buy_zone_high": hi,
        "limit": _r(entry), "stop": _r(stop), "t1": _r(t1), "t2": _r(t2), "qty": qty, "cost": _r(cost),
        "unused": _r(amount - cost) if amount else None, "max_loss": _r(max_loss),
        "max_loss_pct": _r(max_loss / cost * 100) if cost else None, "profit_t1": _r(profit_t1),
        "profit_t2": _r(profit_t2), "stop_pct": _r(stop_pct), "gap_pct": _r(gap_pct),
        "tranches": [{"price": _r(p), "qty": q} for p, q in tr], "support": support, "resistance": resistance,
        "history": hist, "amount": amount, "max_price": max_price, "provisional": False, "alert": False,
    }
