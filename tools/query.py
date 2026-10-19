"""범용 자산 조회 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from .common import _number, _round, _won


DEFINITIONS = [
    {
        "name": "query_data",
        "description": "내 자산 표(주식·원화/달러 예수금·현금 취급 종목 한 줄씩)를 묶고 합치는 범용 조회입니다. 다른 도구에 없는 계산 — 예: '현금 포함 카테고리별 비중', '달러 자산 합계', '수익률 가장 나쁜 종목', '카테고리별 평균 수익률' — 에 씁니다. 항목: kind(주식/현금/현금취급종목), symbol, name, category(리밸런싱 카테고리·현금·미분류), currency(KRW/USD), value_krw(평가금액 원), weight_pct(전체 자산 대비 %, 현금 포함), return_pct(수익률 %), target_pct(카테고리 목표 %), first_buy_date(첫 매수일)·buy_count(매수 체결 횟수)·holding_days(첫 매수 후 일수)·dividend_krw(올해 배당 추정 세전 원)는 체결 내역·배당 기록이 있는 주식만. group_by로 묶고 metric(sum/avg/count/max/min)을 field에 적용하며, filter_*로 한 가지 조건만 걸 수 있습니다. 합계는 전체 대비 비율도 함께 돌려줍니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "group_by": {"type": "string", "enum": ["category", "currency", "kind", "symbol"], "description": "묶을 기준. 비우면 전체 하나로"},
            "group_by2": {"type": "string", "enum": ["category", "currency", "kind", "symbol"], "description": "두 번째 묶을 기준(group_by와 다른 것, 예: 카테고리×통화)"},
            "metric": {"type": "string", "enum": ["sum", "avg", "count", "max", "min"], "description": "기본 sum"},
            "field": {"type": "string", "enum": ["value_krw", "weight_pct", "return_pct", "target_pct", "buy_count", "holding_days", "dividend_krw"], "description": "기본 value_krw"},
            "filter_field": {"type": "string", "enum": ["kind", "symbol", "name", "category", "currency", "first_buy_date", "value_krw", "weight_pct", "return_pct", "target_pct", "buy_count", "holding_days", "dividend_krw"], "description": "조건을 걸 항목"},
            "filter_op": {"type": "string", "enum": ["=", "!=", ">", ">=", "<", "<=", "포함"]},
            "filter_value": {"type": "string", "description": "조건 값(예: 현금, USD, 10)"},
            "filter2_field": {"type": "string", "enum": ["kind", "symbol", "name", "category", "currency", "value_krw", "weight_pct", "return_pct", "target_pct"], "description": "두 번째 조건(첫 조건과 둘 다 만족)"},
            "filter2_op": {"type": "string", "enum": ["=", "!=", ">", ">=", "<", "<=", "포함"]},
            "filter2_value": {"type": "string", "description": "두 번째 조건 값"},
            "order": {"type": "string", "enum": ["desc", "asc"], "description": "기본 desc"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            "additionalProperties": False},
    },
]


def query_data_result(data: dict) -> dict:
    """범용 조회 결과에서 숫자와 짧은 이름만 남긴다(20줄까지)."""
    if data.get("error"):
        return {"error": str(data["error"])[:200]}
    rows = [{"label": str(r.get("label") or "")[:30], "value": _round(r.get("value"), 2), "count": int(_number(r.get("count")) or 0),
             "share_pct": _round(r.get("share_pct"), 1), "target_pct": _round(r.get("target_pct"), 1)} for r in (data.get("rows") or [])[:20]]
    return {"group_by": str(data.get("group_by") or "")[:20], "group_by2": str(data.get("group_by2") or "")[:20], "metric": str(data.get("metric") or "")[:10], "field": str(data.get("field") or "")[:20],
            "filter": (str(data["filter"])[:80] if data.get("filter") else None), "rows": rows, "matched": int(_number(data.get("matched")) or 0),
            "total_rows": int(_number(data.get("total_rows")) or 0), "grand_total": _round(data.get("grand_total"), 2)}


QUERY_FIELD_NAMES = {"value_krw": "평가금액", "weight_pct": "전체 대비 비중", "return_pct": "수익률", "target_pct": "목표 비중", "buy_count": "매수 체결 횟수", "holding_days": "보유 일수", "dividend_krw": "올해 배당 추정"}


QUERY_METRIC_NAMES = {"sum": "합계", "avg": "평균", "count": "개수", "max": "최댓값", "min": "최솟값"}


QUERY_GROUP_NAMES = {"category": "카테고리", "currency": "통화", "kind": "종류", "symbol": "종목"}


def _compose_query(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """범용 조회: 묶음별 값(합계는 전체 대비 %와 목표)을 그래프 하나와 한 줄로. 항목이 틀려 실행하지 못했으면 이유를 알린다."""
    if data.get("error"):
        bullets.append("조회 명령을 실행하지 못했습니다: " + data["error"])
        return
    rows = data.get("rows") or []
    if not rows:
        bullets.append("조건에 맞는 항목이 없습니다" + (f" ({data['filter']})." if data.get("filter") else "."))
        return
    field, metric = data["field"], data["metric"]
    money = field in ("value_krw", "dividend_krw") and metric != "count"
    pct_unit = field in ("weight_pct", "return_pct", "target_pct") and metric != "count"
    group = "×".join(QUERY_GROUP_NAMES[g] for g in (data.get("group_by"), data.get("group_by2")) if g in QUERY_GROUP_NAMES) or "전체"
    share = all(r.get("share_pct") is not None for r in rows)
    title = f"{group}별 {QUERY_FIELD_NAMES[field]} {QUERY_METRIC_NAMES[metric]}" if data.get("group_by") else f"{QUERY_FIELD_NAMES[field]} {QUERY_METRIC_NAMES[metric]}"
    bullets.append(title + (f" (조건: {data['filter']})" if data.get("filter") else "") + " — " + ", ".join(
        f"{r['label']} " + (_won(r["value"]) if money else f"{r['value']:.1f}{'%' if pct_unit else ''}" if metric != "count" else f"{r['value']:.0f}개")
        + (f"({r['share_pct']:.1f}%)" if share and money else "") for r in rows[:6]))
    if share and money:
        sets = [{"label": "전체 대비 비중", "color": "accent", "data": [r["share_pct"] for r in rows], "text": [f"{r['share_pct']:.1f}% · {_won(r['value'])}" for r in rows]}]
        if any(r.get("target_pct") is not None for r in rows):                       # 목표가 없는 묶음(미분류 등)은 빈칸
            sets.insert(0, {"label": "목표 비중", "color": "s2", "data": [r.get("target_pct") for r in rows], "text": [f"{r['target_pct']:.0f}%" if r.get("target_pct") is not None else "" for r in rows]})
        charts.append(chart_mod.make("bar", title + " (전체 자산 대비 %)", [r["label"] for r in rows], sets, "%", caption="현금(예수금·현금 취급 종목)을 포함한 전체 자산 기준", decimals=1, signed=False))
    elif metric != "count":
        charts.append(chart_mod.make("hbar", title, [r["label"] for r in rows], [{"label": QUERY_METRIC_NAMES[metric], "color": "accent", "data": [r["value"] for r in rows],
                                                                             "text": [_won(r["value"]) if money else f"{r['value']:.1f}{'%' if pct_unit else ''}" for r in rows]}],
                                     "%" if pct_unit else "", height=len(rows) * 40 + 70, decimals=1))
    limitations.append("자산 표를 묶고 합친 계산이며, 해석이나 조언이 아닙니다.")


def compose_query_data(query_data, bullets, tables, notes, limitations, charts, results):
    """query_data 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_query(query_data, bullets, tables, notes, limitations, charts)


FORMATTERS = {"query_data": query_data_result}
COMPOSERS = {"query_data": compose_query_data}
