"""내 원칙(목표 비중·전략) 대조 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from portfolio import load_rebalance_config

from ._server import S

if TYPE_CHECKING:
    from server import Request


def my_rules_view(req: Request, args: dict | None = None) -> dict:
    """내가 정해 둔 기준(리밸런싱 목표 비중, 분할매수 전략)과 현재 보유를 대조한 숫자. 읽기 전용이며 판단하지 않는다(어느 기준에 해당하는지까지만).
    카테고리별 목표 비중 대비 현재 비중(목표 대상 안 기준), 종목별 전략 단계·진입 비율·주봉 이동평균 대비 위치, 목표가 없는 종목."""
    holdings = S.get_holdings_view(req)
    stocks = holdings.get("stocks", [])
    total = holdings["totals"]["eval_krw"] or 0.0
    config = load_rebalance_config(req.store)
    value = {r["심볼"]: r["평가금액(원)"] or 0.0 for r in stocks}
    by_symbol = {r["심볼"]: r for r in stocks}
    category_of = {}
    targets = []
    universe = 0.0
    for t in config["targets"]:
        members = [sym for sym in t["symbols"] if sym in value]
        for sym in t["symbols"]:
            category_of.setdefault(sym, t["label"])
        amount = sum(value[sym] for sym in members)
        universe += amount
        targets.append({"label": t["label"], "symbols": list(t["symbols"]), "held_symbols": members, "target_pct": t["target_pct"], "value_krw": amount})
    if config.get("cash_target_pct") is not None:                                              # 현금 목표를 정했으면 현금도 목표 대상에 넣는다
        cash = holdings["cash"]["total_krw"] or 0.0
        universe += cash
        targets.append({"label": "현금", "symbols": [], "held_symbols": [], "target_pct": config["cash_target_pct"], "value_krw": cash})
    for t in targets:
        t["actual_pct"] = t["value_krw"] / universe * 100 if universe else 0.0                 # 목표 대상(카테고리에 넣은 보유종목) 안에서의 비중
        t["actual_total_pct"] = t["value_krw"] / total * 100 if total else 0.0                 # 예수금·현금까지 포함한 전체 대비 비중
        t["diff_pct"] = t["actual_pct"] - (t["target_pct"] or 0.0)
        t["status"] = "over" if t["diff_pct"] > S.RULES_BAND_PCT else "under" if t["diff_pct"] < -S.RULES_BAND_PCT else "near"
    held = set(value)
    signals = S.strategy_signals_for(req.session, req.store, held, sorted(held)) if held else {}
    data = S.load_strategy_templates(req.store)
    template = (data.get("templates") or {}).get(data.get("default_template") or "") or {}
    steps = [f"MA{st['params']['period']}{'주' if st['params']['interval'] == 'week' else '일'} 아래 +{st.get('buy_pct', 0):g}%p" for st in template.get("steps", []) if st.get("indicator") == "MA"]
    rows = []
    for sym in sorted(held, key=lambda x: -value[x]):
        r, sig = by_symbol[sym], signals.get(sym, {})
        entry = {"status": sig.get("status", "none")}
        if sig.get("status") == "ok":
            ma = []
            for step in sig.get("steps", []):
                m = S._MA_LABEL.search(step.get("label") or "")
                if m:
                    ma.append({"period": int(m.group(1)), "unit": m.group(2), "dist_pct": float(m.group(3))})
            entry.update({"entry_pct": sig.get("entry_pct"), "stage": sig.get("stage_count"), "stage_total": sig.get("stage_total"), "recovered": bool(sig.get("recovered")), "ma": ma})
        rows.append({"symbol": sym, "name": r["종목"], "weight_pct": r.get("지분율(%)"), "value_krw": value[sym], "return_pct": r.get("수익률(%)"),
                     "category": category_of.get(sym), "strategy": entry})
    return {"targets": targets, "universe_krw": universe, "total_krw": total, "holdings": rows,
            "unclassified": [x["symbol"] for x in rows if not x["category"]], "strategy_steps": steps,
            "recovery_pct": template.get("recovery_pct"), "band_pct": S.RULES_BAND_PCT}
