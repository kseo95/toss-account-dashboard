"""과거 시세 가정 계산 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from .common import _number, _round, _won


DEFINITIONS = [
    {
        "name": "get_past_value",
        "description": "'N개월(N년) 전에 지금 이 구성이었다면 얼마였나', '작년에 이 종목들을 들고 있었다면 수익률은' 같은 과거 시세 가정 계산입니다. 보유 주식(예수금·현금 취급 종목 제외)의 지금 수량을 과거 종가(Yahoo)에 곱해 그때 평가금액과 지금까지의 수익률, 월별 변화를 돌려줍니다. basis=actual이면 체결 내역으로 그때 실제 들고 있던 수량을 되돌려 계산하며(그 사이 사고판 돈 때문에 수익률은 아님), 기본은 지금 수량 그대로입니다. 시세가 없는 종목은 빠지고 이름을 알려 줍니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "months_ago": {"type": "integer", "minimum": 1, "maximum": 60, "description": "몇 개월 전(1년=12, 기본 12)"},
            "basis": {"type": "string", "enum": ["current", "actual"], "description": "current=지금 수량 그대로(기본), actual=그때 실제 수량"}},
            "additionalProperties": False},
    },
]


def past_value_result(data: dict) -> dict:
    """과거 시세 가정 계산에서 숫자와 짧은 이름만 남긴다(종목 20개·월별 24점까지)."""
    rows = [{"symbol": str(r.get("symbol") or "")[:12], "name": str(r.get("name") or "")[:30], "qty_then": _round(r.get("qty_then"), 4), "price_then": _round(r.get("price_then"), 2),
             "value_then_krw": _round(r.get("value_then_krw"), 0), "value_now_krw": _round(r.get("value_now_krw"), 0), "change_pct": _round(r.get("change_pct"), 1)}
            for r in (data.get("rows") or [])[:20]]
    return {"basis": "actual" if data.get("basis") == "actual" else "current", "months_ago": int(_number(data.get("months_ago")) or 0), "target_date": str(data.get("target_date") or "")[:10],
            "today": str(data.get("today") or "")[:10], "rows": rows, "missing": [str(x)[:30] for x in (data.get("missing") or [])[:12]],
            "total_then_krw": _round(data.get("total_then_krw"), 0), "total_now_krw": _round(data.get("total_now_krw"), 0), "change_krw": _round(data.get("change_krw"), 0),
            "change_pct": _round(data.get("change_pct"), 1), "covered_pct": _round(data.get("covered_pct"), 1),
            "series": [{"date": str(p.get("date") or "")[:10], "value_krw": _round(p.get("value_krw"), 0)} for p in (data.get("series") or [])[:30]]}


def _compose_past(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """과거 시세 가정 계산: 월별 평가금액 선 그래프 + 종목별 변화 그래프, 요약은 한 줄. 지금 수량을 그때도 들고 있었다는 가정이며 조언이 아니다."""
    rows = data.get("rows") or []
    if not rows:
        bullets.append("그때 시세를 받을 수 있는 보유 종목이 없어 계산하지 못했습니다" + (f" (빠진 종목: {', '.join(data['missing'][:5])})." if data.get("missing") else "."))
        return
    actual = data.get("basis") == "actual"
    when = f"{data.get('months_ago')}개월 전({data.get('target_date')})"
    if actual:
        bullets.append(f"{when} 실제로 들고 있던 수량(체결 내역으로 되돌림)으로 계산한 주식 평가금액은 {_won(data['total_then_krw'])}, 지금은 {_won(data['total_now_krw'])}입니다. 그 사이 사고판 돈이 섞여 있어 수익률은 아닙니다.")
    else:
        sign = "+" if (data.get("change_krw") or 0) >= 0 else "-"
        bullets.append(f"지금 보유 수량을 {when}에 그대로 들고 있었다면 {_won(data['total_then_krw'])} → 지금 {_won(data['total_now_krw'])} ({sign}{abs(data['change_krw']):,.0f}원, {data['change_pct']:+.1f}%)입니다.")
    series = data.get("series") or []
    if len(series) >= 2:
        charts.append(chart_mod.make("line", f"{when}부터 지금까지 주식 평가금액" + (" (그때 실제 수량)" if actual else " (지금 수량 고정)"), [p["date"][2:7] for p in series],
                                     [{"label": "평가금액", "color": "accent", "data": [p["value_krw"] for p in series]}], "원", decimals=0, signed=False,
                                     caption="월별 같은 날 종가 × 수량 × 그날 환율. 마지막 점은 지금 토스 평가금액"))
    changes = [r for r in rows if r.get("change_pct") is not None][:12]
    if changes:
        values = [r["change_pct"] for r in changes]
        charts.append(chart_mod.make("hbar", f"종목별 {data.get('months_ago')}개월 전 대비 변화", [r["name"] or r["symbol"] for r in changes],
                                     [{"label": "변화", "colors": chart_mod.signed_colors(values), "data": values,
                                       "text": [f"{r['change_pct']:+.1f}% · {_won(r['value_then_krw'])} → {_won(r['value_now_krw'])}" for r in changes]}], "%", height=len(changes) * 36 + 60))
    if data.get("missing"):
        notes.append("그때 시세(또는 환율)가 없어 뺀 종목: " + ", ".join(data["missing"][:8]) + f". 계산에 쓴 종목은 지금 주식 평가금액의 {data.get('covered_pct', 0):.0f}%입니다.")
    limitations.append("과거 종가(Yahoo)와 지금 토스 평가금액을 비교한 가정 계산이라 소액 차이가 있고, 예수금·현금 취급 종목과 배당·수수료·세금은 빠집니다. 조언이 아닙니다.")


def compose_get_past_value(past_data, bullets, tables, notes, limitations, charts, results):
    """get_past_value 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_past(past_data, bullets, tables, notes, limitations, charts)


FORMATTERS = {"get_past_value": past_value_result}
COMPOSERS = {"get_past_value": compose_get_past_value}
