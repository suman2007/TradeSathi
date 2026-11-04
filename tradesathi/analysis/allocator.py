"""Split capital across valid setups using risk-based position sizing."""


def allocate(results, capital, risk_pct, max_alloc, charges_pct, exclude=()):
    cash, positions = capital, []
    ranked = sorted([r for r in results if r.get("valid") and r["symbol"] not in exclude], key=lambda r: -r["score"])
    for r in ranked:
        entry, stop = r["entry"], r["stop"]
        risk_ps = entry - stop
        if risk_ps <= 0:
            continue
        by_risk = int((capital * risk_pct) // risk_ps)
        by_cap = int(min(capital * max_alloc, cash) // entry)
        qty = max(0, min(by_risk, by_cap))
        if qty == 0:
            continue
        cost = qty * entry
        cash -= cost
        fees = cost * charges_pct
        positions.append({
            "symbol": r["symbol"], "qty": qty, "entry": entry, "stop": stop, "t1": r["t1"], "t2": r["t2"],
            "invested": round(cost, 2), "max_loss": round(qty * risk_ps + fees, 2),
            "profit_t1": round(qty * (r["t1"] - entry) - fees, 2),
            "profit_t2": round(qty * (r["t2"] - entry) - fees, 2),
            "action": r["action"], "limited_by": "risk" if by_risk <= by_cap else "allocation cap",
        })
    invested = sum(p["invested"] for p in positions)
    worst = sum(p["max_loss"] for p in positions)
    return {"positions": positions, "summary": {
        "capital": capital, "invested": round(invested, 2), "cash_left": round(capital - invested, 2),
        "worst_case_loss": round(worst, 2), "worst_case_pct": round(worst / capital * 100, 2) if capital else 0,
    }}
