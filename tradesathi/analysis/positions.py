"""Manage shares you already own: P&L, trailing stop, targets from YOUR cost price, hold/book/exit call."""
from tradesathi import config as cfg


def manage(res, qty, buy, prev=None, days_held=None):
    """res = strategy.analyze() output for the symbol. prev = what we stored last cycle: {"stop", "r"}
    (an older plain number is read as the stop). Stops only move up; 1R is fixed the first time the holding is seen,
    so targets do not drift as volatility changes."""
    price, atr = res["price"], res["atr"]
    support, resistance = res["support"], res["resistance"]
    if not isinstance(prev, dict):
        prev = {"stop": prev}
    prev_stop = prev.get("stop")
    band = cfg.ENTRY_BAND * atr
    R = prev.get("r") or cfg.ATR_STOP_MULT * atr   # 1R = planned risk per share from your cost
    breakeven = buy * (1 + cfg.CHARGES_PCT)     # price where you get out even after charges
    base_stop = buy - R
    why = []

    if price > base_stop:
        mode = "normal"
        stop, kind = base_stop, "initial (cost - 1R)"
        struct = support - band
        if struct > stop and struct <= price - cfg.ATR_MIN_STOP_MULT * atr:
            stop, kind = struct, "below support"
        gain_r = (price - buy) / R
        if gain_r >= 1 and breakeven > stop:
            stop, kind = breakeven, "breakeven"
            why.append("Up 1R or more: stop moved to breakeven, so this trade can no longer lose money")
        if gain_r >= 2 and price - R > stop:
            stop, kind = price - R, "trailing"
            why.append("Up 2R or more: stop now trails 1R below price")
        t1, t2 = buy + cfg.T1_R * R, buy + cfg.T2_R * R
    else:
        mode = "recovery"
        stop, kind = min(support - band, price - cfg.ATR_MIN_STOP_MULT * atr), "recovery stop (below support)"
        ahead = sorted(v for v in (breakeven, resistance) if v and v > price)
        t1 = ahead[0] if ahead else None
        t2 = ahead[1] if len(ahead) > 1 else None
        why.append("Price is already below your planned stop (cost - 1R): recovery mode. "
                   "Targets are resistance / your breakeven, stop is just under support")

    if prev_stop and prev_stop > stop:          # never lower a stop
        stop, kind = prev_stop, "trailing (locked in)"

    sig, profit = res["signal"], price > buy
    if price <= stop:                           # same rule as the backtest and the buy plan: a touch of the stop exits
        action = "EXIT NOW (stop hit)"
    elif days_held is not None and days_held >= cfg.BT_MAX_HOLD:   # the history check closes every trade at this age
        action = f"EXIT NOW (time limit: held {days_held} trading days)"
        why.append(f"Held {days_held} trading days: past the {cfg.BT_MAX_HOLD}-day limit the strategy was tested with")
    elif sig == "AVOID":
        action = "EXIT / REDUCE (trend weak)"
        if not profit:
            why.append("Trend is weak and you are below cost: do not average down")
    elif mode == "normal" and price >= t2:
        action = "BOOK PROFIT (T2 reached)"
    elif mode == "normal" and price >= t1:
        action = "BOOK PARTIAL (T1 reached), trail stop"
    elif resistance and profit and resistance - price <= band:
        action = "BOOK PARTIAL (at resistance)"
    elif res["valid"] and res["zone_status"] != "BELOW_ZONE":
        action = f"HOLD (can add on pullback {res['entry_low']}-{res['entry_high']})"
    else:
        action = "HOLD"

    invested, value = qty * buy, qty * price
    return {
        "symbol": res["symbol"], "qty": qty, "buy_price": round(buy, 2), "price": price,
        "invested": round(invested, 2), "value": round(value, 2),
        "pnl": round(value - invested, 2), "pnl_pct": round((price / buy - 1) * 100, 2),
        "breakeven": round(breakeven, 2), "mode": mode, "action": action,
        "signal": sig, "score": res["score"],
        "stop": round(stop, 2), "stop_type": kind, "r": round(R, 4), "days_held": days_held,
        "t1": round(t1, 2) if t1 else None, "t2": round(t2, 2) if t2 else None,
        "support": support, "resistance": resistance,
        "money_at_risk": round(max(0.0, qty * (price - stop)), 2),
        "pnl_at_stop": round(qty * (stop - buy), 2),
        "profit_t1": round(qty * (t1 - buy), 2) if t1 else None,
        "profit_t2": round(qty * (t2 - buy), 2) if t2 else None,
        "why": why, "history": res.get("history"),
    }


def summarize(rows):
    rows = [r for r in rows if "error" not in r]
    inv = sum(r["invested"] for r in rows)
    val = sum(r["value"] for r in rows)
    return {"invested": round(inv, 2), "value": round(val, 2), "pnl": round(val - inv, 2),
            "pnl_pct": round((val / inv - 1) * 100, 2) if inv else 0,
            "money_at_risk": round(sum(r["money_at_risk"] for r in rows), 2),
            "pnl_at_stops": round(sum(r["pnl_at_stop"] for r in rows), 2)}
