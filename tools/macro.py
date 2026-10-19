"""매크로 요인·사례집·요인 순위·요인 반응·종목 뉴스 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from news import HEADLINES_PER_SYMBOL, SOURCE_MAX, TITLE_MAX, safe_url
from .common import _josa, _number, _origin_tag, _round, _signed


DEFINITIONS = [
    {
        "name": "get_asset_drivers",
        "description": "보유·관심 종목이 평소 가장 민감한 매크로 요인 순위(기본 3개)를 조회합니다. 요인마다 최근 1년·3년·10년 일간 상관과 모든 창에서 같은 방향인지(일관성), 요인이 1표준편차 움직일 때 종목의 평균 반응, 같이 움직여서 대표 하나로 묶은 요인(예: 5·10·30년물 금리), 1년 회귀 모형의 설명력과 검증 설명력을 줍니다. '이번 주 무엇 때문에 움직였나'가 아니라 '평소 무엇에 가장 영향을 받나'에 쓰며, 같이 움직인 정도이고 원인의 증명이 아닙니다. 금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물·지수)을 씁니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: 금, TLT, UFO). 비우면 보유·관심 종목(최대 4개)"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_macro_cases",
        "description": "거시 사례집: 2008 금융위기, 2013 테이퍼 텐트럼, 2020 코로나, 2022 긴축, 1980년대 볼커 긴축 등 12개 사례의 국면별 이야기(해석)와, 그 국면에서 금·금리·달러·증시·VIX가 실제로 얼마나 움직였는지(시세로 계산), '금리 오르면 금 하락' 같은 통념이 맞았는지 점검, 그 뒤 3개월 금 변화를 줍니다. 또 지금(최근 6개월)과 요인 움직임이 가장 닮은 과거 국면을 찾아 줍니다. case에 사례 이름·연도·사건 키워드를 주면 그 사례를, focus에 종목을 주면 그 종목의 국면별 변화도 함께 봅니다. 이야기는 해석이고 닮은 국면이 같은 결말을 보장하지 않으며 예측이 아닙니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "case": {"type": "string", "description": "보고 싶은 사례의 이름·연도·사건(예: 2008 금융위기, 코로나, 볼커). 비우면 지금과 닮은 국면만"},
            "focus": {"type": "string", "description": "국면별 변화를 같이 볼 종목 이름·코드·카테고리 이름(예: TLT, 금). 선택"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_macro_history",
        "description": "지금 큰 변동 중인 매크로 요인(최근 1개월 금리·달러·유가·VIX·환율·지수가 그 요인 자신의 역사에서 상위·하위 10%)과, 과거 같은 방향의 큰 변동 때 보유·관심 종목이 어떻게 움직였는지(독립 사례 수·중앙값·평소와의 통계적 차이)를, 이름 붙은 거시 이슈(리먼·코로나·긴축 등)와 함께 조회합니다. 이 종목이 가장 크게 떨어지거나 오른 과거 이슈도 줍니다. 같이 움직인 기록이며 원인의 증명이 아닙니다. 금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물 2000년~, 지수)을 씁니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: TLT, 금, 스트래티지). 비우면 보유·관심 종목(최대 6개)"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_factor_response",
        "description": "'미국 금리가 오르면 금과 나스닥은 어떻게 돼?'처럼 요인(금리·달러·유가·VIX·환율·구리)이 크게 오른/내린 때 주요 자산(금·나스닥·S&P500·코스피·장기채·비트코인 등)이 평소와 어떻게 달랐는지 조회합니다. 보유 종목과 무관하게 지난 수십 년(자산마다 시세가 있는 기간)의 겹치지 않는 구간(기본 한 달)을 요인 변화가 상위 20%(큰 상승)·하위 20%(큰 하락)·보통으로 나눠, 구간 수와 수익률 중앙값·오른 비율을 평소와 비교하고(이항검정, 본페로니 보정), 일간 상관(1년·3년·10년)과 지금 구간이 어디에 해당하는지 줍니다. 같은 구간에 같이 움직인 기록이며 원인의 증명이 아니고, 요인이 왜 움직였는지(경기·물가·긴축)에 따라 달라질 수 있습니다. 보유 종목의 이번 주 요인 분해는 get_macro_drivers, 보유 종목이 과거 큰 변동 때 어땠는지는 get_macro_history를 씁니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "factor": {"type": "string", "description": "조건을 거는 요인(예: 금리, 30년물, 달러, 유가, VIX, 환율, 구리)"},
            "direction": {"type": "string", "enum": ["up", "down"], "description": "질문이 묻는 방향: up=오르면, down=내리면(표에는 둘 다 나옴)"},
            "assets": {"type": "array", "items": {"type": "string"}, "maxItems": 6, "description": "반응을 볼 자산 이름(예: 금, 나스닥, S&P500, 코스피, 장기채, 비트코인, 반도체). 비우면 금·나스닥·S&P500·코스피·장기채·비트코인"},
            "horizon_days": {"type": "integer", "enum": [5, 21, 63], "description": "구간 길이(거래일): 5=일주일, 21=한 달(기본), 63=3개월"}},
                         "required": ["factor"], "additionalProperties": False},
    },
    {
        "name": "get_macro_drivers",
        "description": "이번 주 금리·달러·유가·VIX·환율·지수 변화와, 보유·관심 종목의 등락이 그 요인들과 얼마나 같이 움직였는지(요인별 기여 추정, 설명력 R²)를 조회합니다. 통계적 추정이며 인과의 증명이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "보고 싶은 종목 이름·코드·카테고리 이름(예: UFO, 금, 스트래티지). 비우면 보유·관심 종목 전체"}}, "additionalProperties": False},
    },
    {
        "name": "get_weekly_news",
        "description": "보유 종목과 관심종목의 최근 5거래일 등락률과 최근 7일 뉴스 헤드라인(제목·출처·날짜·링크)을 조회합니다. 보도 제목은 참고용이며 가격 변동의 원인으로 확정할 수 없습니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "보고 싶은 종목 이름·코드·카테고리 이름(예: UFO, 금, 스트래티지). 비우면 보유·관심 종목 전체"}}, "additionalProperties": False},
    },
]


def macro_history_result(data: dict) -> dict:
    """과거 매크로 충격 반응에서 숫자와 짧은 이름만 남긴다(종목 6개, 종목당 요인 6개·대표 사례 3개·이슈 3개까지)."""
    def ev(rows):
        return [{"name": str(r.get("name") or "")[:80], "start": str(r.get("start") or "")[:10], "end": str(r.get("end") or "")[:10],
                 "return_pct": _round(r.get("return_pct"), 1), "z": _round(r.get("z"), 1)} for r in (rows or [])[:3]]
    items = []
    for it in data.get("items", [])[:6]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")), "extra": bool(it.get("extra")),
                "status": "ok" if it.get("status") == "ok" else "no_data", "history_days": int(_number(it.get("history_days")) or 0),
                "source": str(it.get("source") or "")[:40] or None}
        if base["status"] == "ok":
            active = []
            for s in (it.get("active") or [])[:6]:
                active.append({
                    "label": str(s.get("label") or "")[:30], "direction": "up" if s.get("direction") == "up" else "down",
                    "phrase": str(s.get("phrase") or "")[:30], "current_change": _round(s.get("current_change"), 1),
                    "unit": "bp" if s.get("unit") == "bp" else "%", "percentile": _round(s.get("percentile"), 3),
                    "episodes": int(_number(s.get("episodes")) or 0), "median_return": _round(s.get("median_return"), 1),
                    "neg_share": _round(s.get("neg_share"), 3), "p_value": _round(s.get("p_value"), 3),
                    "verdict": s.get("verdict") if s.get("verdict") in ("up", "down", "mixed", "insufficient") else "insufficient",
                    "examples": [{"date": str(e.get("date") or "")[:10], "label": str(e.get("label") or "")[:80] or None,
                                  "factor_change": _round(e.get("factor_change"), 1), "asset_return": _round(e.get("asset_return"), 1)}
                                 for e in (s.get("examples") or [])[:3]]})
            b = it.get("baseline") or {}
            base.update({"history_from": str(it.get("history_from") or "")[:10], "events_covered": int(_number(it.get("events_covered")) or 0),
                         "baseline": {"median": _round(b.get("median"), 1), "neg_share": _round(b.get("neg_share"), 3)} if b else None,
                         "active": active, "worst_events": ev(it.get("worst_events")), "best_events": ev(it.get("best_events"))})
        items.append(base)
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "같이 움직인 과거 기록이며 원인의 증명이 아닙니다. 큰 변동은 여러 일이 겹친 시기(위기·긴축 등)에 함께 나타나서, 어느 한 요인 때문이라고 말할 수 없습니다.",
            "'큰 변동'은 최근 1개월(21거래일) 변화가 그 요인 자신의 역사에서 상위·하위 10%인 경우입니다. 21거래일 이상 떨어진 것을 별개 사례로 세고, 독립 사례가 5회 미만이면 판단하지 않습니다.",
            "평소(아무 21거래일)의 하락 비율과 비교한 이항 검정을 하고, 한 종목에서 큰 변동 중인 요인 수만큼 p값에 곱해(본페로니 보정) 0.05 미만일 때만 '통계적으로 구분됨'으로 표시합니다. 그래도 같은 위기 시기가 여러 사례에 겹쳐 있어 실제 독립성은 표시된 사례 수보다 낮을 수 있습니다.",
            "이름 붙은 거시 이슈의 기간은 시장 반응이 두드러진 대략의 구간이며, 목록에 없는 이슈는 이름이 붙지 않습니다(2025년 4월까지의 이슈만 담았습니다).",
            "금 ETF는 금 선물 일봉(2000년~), 지수 ETF는 지수 일봉(가격 기준, 배당 제외)으로 대신 계산했고 표에 밝힙니다. 상장이 최근인 그 밖의 종목은 자신의 시세(상장 이후)만 씁니다.",
        ],
    }


def asset_drivers_result(data: dict) -> dict:
    """요인 순위에서 숫자와 짧은 이름만 남긴다(종목 4개, 매크로 요인 3개·비슷한 자산 3개 + 순위 밖 3개까지)."""
    def row(d):
        corr = {str(k)[:6]: _round(v, 2) for k, v in (d.get("corr") or {}).items() if _number(v) is not None}
        return {"symbol": str(d.get("symbol") or "")[:20], "label": str(d.get("label") or "")[:30], "up": str(d.get("up") or "")[:20], "down": str(d.get("down") or "")[:20],
                "corr": corr, "consistent": bool(d.get("consistent")), "reaction_1m_pct": _round(d.get("reaction_1m_pct"), 1),
                "absorbed": [str(x)[:30] for x in (d.get("absorbed") or [])[:4]]}
    items = []
    for it in data.get("items", [])[:4]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")), "extra": bool(it.get("extra")),
                "status": "ok" if it.get("status") == "ok" else "no_data", "source": str(it.get("source") or "")[:40] or None}
        if base["status"] == "ok":
            model = it.get("model") or {}
            base.update({"drivers": [row(d) for d in (it.get("drivers") or [])[:3]], "peers": [row(d) for d in (it.get("peers") or [])[:3]],
                         "others": [row(d) for d in (it.get("others") or [])[:3]],
                         "asset_vol_1m_pct": _round(it.get("asset_vol_1m_pct"), 1), "windows": [str(w)[:6] for w in (it.get("windows") or [])[:4]],
                         "model": {"factors": [str(x)[:30] for x in (model.get("factors") or [])[:3]], "r2": _round(model.get("r2")), "oos_r2": _round(model.get("oos_r2"))} if model else None})
        items.append(base)
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")), "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "같이 움직인 정도(일간 수익률 상관)이며 원인의 증명이 아닙니다. 요인끼리도 서로 영향을 주고받습니다.",
            "순위는 1년·3년·10년 상관의 평균 크기이고, 모든 창에서 같은 방향(각각 0.1 이상)인 요인만 '일관됨'으로 보며 일관되지 않으면 점수를 절반으로 줄입니다. 시세가 짧아 없는 창은 빼고 계산합니다.",
            "매크로 요인(금리·통화·에너지와 산업용 원자재·VIX·MOVE·FRED 지표)과 함께 움직이는 비슷한 자산·시장(귀금속·주가지수·업종·채권 ETF·가상자산)은 따로 순위를 냅니다 — 같은 계열 자산(금과 은 등)이 매크로 요인을 가리지 않게 하려는 것이고, 비슷한 자산은 같은 계열이라 같이 움직이는 것이지 원인이 아닙니다. 서로 0.7 이상 같이 움직이는 요인(예: 5·10·30년물 금리, 나스닥·S&P500)은 각 계열 안에서 점수가 높은 하나로 묶었습니다. 종목과 사실상 같은 시리즈(같은 자산의 지수·선물)는 요인에서 제외합니다.",
            "'1표준편차 반응'은 요인이 1개월 동안 평소 크기(표준편차)만큼 움직일 때 종목이 평균적으로 보인 반응(3년 상관 × 종목의 1개월 변동폭)입니다. 실제 움직임은 이보다 크게 벗어날 수 있습니다.",
            "금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물 2000년대~, 지수)으로 계산했고 표에 밝힙니다. 상관은 시기에 따라 달라져(예: 구리와 금은 1년 상관이 10년보다 큼) 최근에는 더 강하거나 약할 수 있습니다.",
        ],
    }


def macro_cases_result(data: dict) -> dict:
    """사례집 결과에서 숫자와 짧은 글만 남긴다(사례 3개, 국면 6개, 닮은 국면 3개까지)."""
    def moves(rows):
        return [{"key": str(m.get("key") or "")[:20], "label": str(m.get("label") or "")[:20], "value": _round(m.get("value"), 1),
                 "unit": "bp" if m.get("unit") == "bp" else "%"} for m in (rows or [])[:12] if _number(m.get("value")) is not None]
    cases = []
    for c in (data.get("cases") or [])[:3]:
        phases = []
        for p in (c.get("phases") or [])[:6]:
            phases.append({
                "name": str(p.get("name") or "")[:60], "start": str(p.get("start") or "")[:10], "end": str(p.get("end") or "")[:10],
                "story": str(p.get("story") or "")[:400], "moves": moves(p.get("moves")),
                "conventions": [{"rule": str(v.get("rule") or "")[:80], "held": v.get("held") if isinstance(v.get("held"), bool) else None,
                                 "detail": str(v.get("detail") or "")[:100]} for v in (p.get("conventions") or [])[:5]],
                "gold_after_pct": _round(p.get("gold_after_pct"), 1),
                "asset_move_pct": _round(p.get("asset_move_pct"), 1), "asset_after_pct": _round(p.get("asset_after_pct"), 1)})
        cases.append({"id": str(c.get("id") or "")[:30], "title": str(c.get("title") or "")[:80], "summary": str(c.get("summary") or "")[:300], "phases": phases})
    similar = []
    for s in (data.get("similar") or [])[:3]:
        similar.append({"case_title": str(s.get("case_title") or "")[:80], "phase": str(s.get("phase") or "")[:60], "start": str(s.get("start") or "")[:10],
                        "end": str(s.get("end") or "")[:10], "distance": _round(s.get("distance")), "shared": int(_number(s.get("shared")) or 0),
                        "gold_after_pct": _round(s.get("gold_after_pct"), 1),
                        "compare": [{"label": str(c.get("label") or "")[:20], "now": _round(c.get("now"), 1), "then": _round(c.get("then"), 1),
                                     "unit": "bp" if c.get("unit") == "bp" else "%", "close": bool(c.get("close"))} for c in (s.get("compare") or [])[:6]]})
    return {
        "named": bool(data.get("named")), "cases": cases, "similar": similar,
        "available": [{"id": str(a.get("id") or "")[:30], "title": str(a.get("title") or "")[:80]} for a in (data.get("available") or [])[:20]],
        "asset": str(data.get("asset") or "")[:60] or None, "has_fred": bool(data.get("has_fred")), "gold_available": bool(data.get("gold_available", True)),
        "limitations": [
            "사건과 날짜는 널리 알려진 것만 넣었고, 국면별 이야기와 사례 요약은 해석입니다. 숫자는 시세에서 계산했고 이야기와 어긋나는 부분은 '통념 점검'에 드러납니다.",
            "금은 2000년 9월 이후 구간은 금 선물 일봉, 그 이전은 월평균 가격(1975년~)이라 거칩니다. 3개월 금리는 연방기금금리 대신 쓴 대용입니다(FRED 키를 넣으면 연방기금금리·실질금리·기대인플레이션도 함께 봅니다).",
            "통념 점검은 큰 변동이 있었던 국면(금리 ±25bp, 달러 ±3%, 증시 −10% 이하 등)에서만 판정하고, 금 변화가 ±3% 미만이면 뚜렷하지 않음으로 둡니다.",
            "지금과 닮은 국면은 최근 6개월 요인 변화를 각 요인의 역사 속 분포로 표준화해 비교한 것이며(요인 4개 이상), 같은 결말을 보장하지 않고 예측이 아닙니다. 사례는 12개 안팎이라 통계가 아니라 참고 사례입니다.",
        ],
    }


def macro_drivers(data: dict) -> dict:
    """요인 변화와 종목별 요인 기여만 남긴다(숫자와 짧은 이름만)."""
    factors = [{"label": str(f.get("label") or ""), "change": _number(f.get("change")), "unit": "bp" if f.get("unit") == "bp" else "%",
                "phrase": str(f.get("phrase") or "")} for f in data.get("factors", [])[:60]]
    items = []
    for it in data.get("items", [])[:30]:
        drivers = [{"label": str(d.get("label") or ""), "phrase": str(d.get("phrase") or ""),
                    "factor_change": _number(d.get("factor_change")), "unit": "bp" if d.get("kind") == "level" else "%",
                    "contribution_pct": _number(d.get("contribution_pct")), "t": _round(d.get("t"), 1),
                    "stable": d.get("stable") if isinstance(d.get("stable"), bool) else None} for d in it.get("drivers", [])[:3]]
        items.append({"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")), "extra": bool(it.get("extra")),
                      "status": str(it.get("status") or "no_data"), "weekly_change_pct": _number(it.get("weekly_change_pct")),
                      "r2": _number(it.get("r2")), "oos_r2": _round(it.get("oos_r2")),
                      "reason": it.get("reason") if it.get("reason") in ("low_r2", "unstable") else "",
                      "drivers": drivers, "residual_pct": _number(it.get("residual_pct"))})
    return {
        "factors": factors, "items": items, "omitted": int(_number(data.get("omitted")) or 0),
        "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "limitations": [
            "요인별 기여는 최근 약 1년 일간 수익률의 통계적 관계(회귀)로 추정한 값이며, 같이 움직인 정도일 뿐 원인으로 확정할 수 없습니다. 설명력(R²)이 낮으면 신뢰하지 마세요.",
            "요인은 계수의 t값이 2 이상(우연과 구분됨)인 것만 남기고, 앞 2/3로 구한 계수가 뒤 1/3을 맞히는지(검증 설명력)와 앞·뒤 절반에서 계수 부호가 같은지(안정성)를 함께 봅니다. 검증 설명력이 0.05 미만이면 표본 안에서 설명돼도 믿을 만하지 않다고 표시합니다. t값은 일간 수익률의 자기상관을 보정하지 않아 다소 낙관적일 수 있습니다.",
            "종목과 거의 같은 움직임인 시리즈(예: 같은 자산의 지수나 선물)는 요인에서 제외했습니다.",
            "국내 종목과 환율은 미국 시장과 거래 시간이 달라 시차 때문에 설명력이 낮게 나올 수 있습니다.",
        ],
    }


def weekly_news(data: dict) -> dict:
    """종목별 등락률과 헤드라인만 남긴다. 링크는 http(s)만, 제목·출처는 길이를 제한한다."""
    items = []
    for it in data.get("items", [])[:30]:
        headlines = []
        for h in it.get("headlines", [])[:HEADLINES_PER_SYMBOL]:
            url = safe_url(h.get("url"))
            if url and h.get("title"):
                headlines.append({"title": str(h["title"])[:TITLE_MAX], "source": str(h.get("source") or "")[:SOURCE_MAX],
                                  "published": str(h.get("published") or "")[:10], "url": url})
        items.append({"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")), "extra": bool(it.get("extra")),
                      "weekly_change_pct": _number(it.get("weekly_change_pct")), "headlines": headlines,
                      "news_error": bool(it.get("news_error"))})
    return {
        "items": items,
        "omitted": int(_number(data.get("omitted")) or 0),
        "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "limitations": [
            "보도 제목은 야후 파이낸스와 구글 뉴스 검색 결과(비공식)이며 가격 변동의 원인으로 확정할 수 없습니다.",
            "등락률은 최근 5거래일 기준이고, 조회어에는 종목 이름·코드만 쓰며 계좌 정보는 보내지 않습니다.",
            "종목 이름이 티커뿐인 ETF는 야후 기사만 조회하고, 국내 종목은 구글 뉴스만 조회합니다. 비인기 ETF는 관련 보도가 없을 수 있습니다.",
        ],
    }


def factor_response_result(data: dict) -> dict:
    """조건부 반응 결과에서 숫자와 짧은 이름만 남긴다(자산 6개까지)."""
    def stat(st):
        st = st if isinstance(st, dict) else {}
        return {"n": int(_number(st.get("n")) or 0), "median": _round(st.get("median"), 1), "up_share": _round(st.get("up_share"), 3),
                "verdict": st.get("verdict") if st.get("verdict") in ("higher", "lower", "mixed", "insufficient") else "mixed"}
    factor = data.get("factor") if isinstance(data.get("factor"), dict) else {}
    th = data.get("thresholds") if isinstance(data.get("thresholds"), dict) else None
    assets = []
    for a in (data.get("assets") or [])[:6]:
        base = a.get("baseline") or {}
        assets.append({"name": str(a.get("name") or "")[:30], "windows": int(_number(a.get("windows")) or 0), "first": str(a.get("first") or "")[:10],
                       "baseline": {"median": _round(base.get("median"), 1), "up_share": _round(base.get("up_share"), 3)},
                       "up": stat(a.get("up")), "down": stat(a.get("down")), "flat": stat(a.get("flat")),
                       "correlation": {k: _round(v, 2) for k, v in (a.get("correlation") or {}).items()}})
    return {
        "factor": {"name": str(factor.get("name") or "")[:30], "kind": "level" if factor.get("kind") == "level" else "price"},
        "direction": "down" if data.get("direction") == "down" else "up", "horizon": int(_number(data.get("horizon")) or 21),
        "thresholds": {"up": _round(th.get("up"), 1), "down": _round(th.get("down"), 1)} if th else None,
        "current": _round(data.get("current"), 1), "current_bucket": data.get("current_bucket") if data.get("current_bucket") in ("up", "down", "flat") else None,
        "as_of": str(data.get("as_of") or "")[:10] or None, "assets": assets, "insufficient": [str(x)[:30] for x in (data.get("insufficient") or [])[:6]],
        "unresolved": [str(x)[:30] for x in (data.get("unresolved") or [])[:6]],
        "limitations": [
            "같은 구간에 같이 움직인 기록이며 요인이 원인이라는 증명이 아닙니다. 같은 요인이 오르더라도 왜 올랐는지(경기 호조·물가·긴축·위험 회피)에 따라 자산의 반응이 달랐을 수 있습니다.",
            "큰 상승·큰 하락은 그 요인의 구간 변화 분포에서 상위·하위 20%입니다. 구간은 겹치지 않게 최근부터 거슬러 잡았고, 자산마다 시세가 있는 기간이 달라 구간 수가 다릅니다. 구간 수가 적으면(8개 미만) 판단하지 않습니다.",
            "'평소와 구분됨'은 오른 구간 비율이 전체 평균과 이항검정으로 달랐는지(자산 × 큰 상승·큰 하락 전체 검정 수로 본페로니 보정)이며, 수익률의 크기가 아니라 방향의 차이입니다. 표본이 작은 구간은 우연일 수 있습니다.",
            "금·원유·구리는 선물, 달러는 달러 인덱스, 장기채는 TLT 가격(배당 제외) 기준이고 환율·시차 때문에 코스피 등 한국 지수는 미국 요인과의 시차가 있을 수 있습니다. 투자 조언이 아닙니다.",
        ],
    }


MACRO_TABLE_ROWS = 12


MACRO_FACTOR_ROWS = 12   # 이번 주 시장 요인 표에 보이는 최대 개수(요인이 40개를 넘어 많이 움직인 것만)


MACRO_WEAK_MESSAGE = "시장·매크로 요인으로 거의 설명되지 않음 (종목 고유 이슈일 수 있어 뉴스를 확인하세요)"


MACRO_UNSTABLE_MESSAGE = "최근 1년 안에서는 요인과 같이 움직였지만, 뒤 구간 검증에서는 맞지 않아 믿기 어렵습니다 (관계가 바뀌었을 수 있어 뉴스를 확인하세요)"


def _factor_text(value, unit: str, phrase: str = "") -> str:
    rounded = round(value) if unit == "bp" else round(value, 1)
    rounded = rounded + 0.0 if rounded else 0.0  # -0.0이 "-0.0%"로 보이지 않게
    text = f"{rounded:+.0f}bp" if unit == "bp" else f"{rounded:+.1f}%"
    return f"{text} {phrase}".strip()


def _r2_text(item: dict) -> str:
    """설명력 칸: 표본 안 R²와 검증 구간 설명력(앞 2/3로 구한 계수가 뒤 1/3을 맞힌 정도)."""
    if item.get("r2") is None:
        return "-"
    return f"{item['r2']:.2f}" + (f" (검증 {item['oos_r2']:.2f})" if item.get("oos_r2") is not None else "")


def _compose_macro(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """이번 주 시장 요인 표와, 종목별 요인 기여 표(통계 추정). 설명력이 낮으면 그렇다고 표시한다."""
    factors = [f for f in data.get("factors", []) if f.get("change") is not None]
    if factors:
        # 금리 10bp를 가격 1%와 비슷한 크기로 보고 많이 움직인 요인부터
        ranked = sorted(factors, key=lambda f: abs(f["change"]) / (10 if f["unit"] == "bp" else 1), reverse=True)
        bullets.append("이번 주 시장: " + ", ".join(f"{f['label']} {_factor_text(f['change'], f['unit'])}" for f in ranked[:4]) + ".")
        if len(ranked) > MACRO_FACTOR_ROWS:
            notes.append(f"이번 주 시장 요인은 전체 {len(ranked)}개 중 많이 움직인 10개만 그래프에 보여줍니다.")
    items = data.get("items") or []
    rows = []
    for i in items[:MACRO_TABLE_ROWS]:
        if i.get("status") == "ok" and i.get("drivers"):     # 요인별 기여는 그래프로 보여 준다 — 표에는 설명이 안 되는 종목의 이유만 남긴다
            continue
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        label += _origin_tag(i)
        change = f"{i['weekly_change_pct']:+.1f}%" if i.get("weekly_change_pct") is not None else "-"
        if i.get("status") == "no_data":
            cell, r2 = "시세나 요인 데이터가 부족해 계산하지 못했습니다", "-"
        elif i.get("status") == "weak" or not i.get("drivers"):
            cell = MACRO_UNSTABLE_MESSAGE if i.get("reason") == "unstable" else MACRO_WEAK_MESSAGE
            r2 = _r2_text(i)
        else:
            drivers = sorted(i["drivers"], key=lambda d: abs(d.get("contribution_pct") or 0), reverse=True)  # 기여가 큰 요인부터
            shown = [d for d in drivers if abs(d.get("contribution_pct") or 0) >= 0.3] or drivers[:1]
            lines = [f"{d['label']} {_factor_text(d['factor_change'], d['unit'], d['phrase'])} → {d['contribution_pct']:+.1f}%p"
                     + (" (관계 불안정)" if d.get("stable") is False else "") for d in shown]
            rest = (i.get("residual_pct") or 0) + sum(d["contribution_pct"] for d in drivers if d not in shown)
            lines.append(f"그 밖 {rest:+.1f}%p")
            cell, r2 = {"lines": lines}, _r2_text(i)
        rows.append([label, change, cell, r2])
    if rows:
        tables.append({"title": "요인으로 설명하지 못한 종목", "columns": ["종목", "이번 주", "이유", "설명력(R²) · 검증"],
                       "align": ["l", "r", "l", "r"], "wrap": [False, False, True, False], "rows": rows, "footer": []})
    hidden = max(0, len(items) - MACRO_TABLE_ROWS) + int(data.get("omitted") or 0)
    if hidden > 0:
        notes.append(f"그 밖의 {hidden}개 종목은 넣지 않았습니다(많이 움직인 종목부터 표시).")
    if data.get("focused") and items:
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    limitations.extend(data.get("limitations", []))


NEWS_TABLE_ROWS = 12


def _compose_news(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """이번 주 종목별 움직임과 보도 표. 많이 움직인 종목부터, 보도 제목은 링크로(원인으로 단정하지 않는다)."""
    items = data.get("items") or []
    if not items:
        bullets.append("이번 주 뉴스를 조회할 보유 종목이나 관심종목이 없습니다.")
        return
    moved = [i for i in items if i.get("weekly_change_pct") is not None]
    if moved:
        top = sorted(moved, key=lambda i: abs(i["weekly_change_pct"]), reverse=True)[:3]
        bullets.append("이번 주(최근 5거래일) 가장 크게 움직인 종목: "
                       + ", ".join(f"{i.get('name') or i['symbol']} {i['weekly_change_pct']:+.1f}%" for i in top) + ".")
    rows = []
    for i in items[:NEWS_TABLE_ROWS]:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        label += _origin_tag(i)
        change = f"{i['weekly_change_pct']:+.1f}%" if i.get("weekly_change_pct") is not None else "-"
        if i.get("headlines"):
            cell = {"links": [{"text": h["title"], "href": h["url"], "meta": " · ".join(x for x in (h.get("source"), h.get("published")) if x)}
                              for h in i["headlines"]]}
        else:
            cell = "뉴스를 불러오지 못했습니다" if i.get("news_error") else "최근 7일 관련 보도를 찾지 못했습니다"
        rows.append([label, change, cell])
    tables.append({"title": "이번 주 종목별 움직임과 보도", "columns": ["종목", "이번 주", "보도 (제목 · 출처 · 날짜)"],
                   "align": ["l", "r", "l"], "wrap": [False, False, True], "rows": rows, "footer": []})
    hidden = len(items) - len(rows) + int(data.get("omitted") or 0)
    if hidden > 0:
        notes.append(f"그 밖의 {hidden}개 종목은 표에 넣지 않았습니다(많이 움직인 종목부터 표시).")
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    limitations.extend(data.get("limitations", []))


HISTORY_VERDICT_TEXT = {"down": "통계적으로 구분됨 → 하락 쪽", "up": "통계적으로 구분됨 → 상승 쪽", "mixed": "평소와 통계적으로 구분되지 않음",
                        "insufficient": "독립 사례 부족 — 판단하지 않음"}


HISTORY_ROWS_PER_ASSET = 4


def _history_sort_key(s: dict):
    """표시 순서: 통계적으로 구분되는 것 먼저(p 작은 순), 그다음 지금 가장 극단적인 변동."""
    significant = s.get("verdict") in ("up", "down")
    return (not significant, s["p_value"] if significant and s.get("p_value") is not None else 0.0, -max(s["percentile"], 1 - s["percentile"]))


def _compose_history(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """지금 큰 변동 중인 요인과 과거 같은 변동 때의 이 종목, 그리고 가장 크게 반응했던 거시 이슈. 같이 움직인 기록이며 원인으로 단정하지 않는다."""
    items = data.get("items") or []
    if not items:
        bullets.append("과거 매크로 반응을 볼 보유 종목이나 관심종목이 없습니다.")
        return
    rows, event_rows, failed = [], [], []
    for i in items:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        label += _origin_tag(i)
        if i.get("status") != "ok":
            failed.append(i["symbol"])
            continue
        head = {"lines": [label] + ([f"기준: {i['source']}"] if i.get("source") else []) + [f"{i['history_from'][:4]}년~ 시세"]}
        active = sorted(i.get("active") or [], key=_history_sort_key)
        shown = active[:HISTORY_ROWS_PER_ASSET]
        significant = [s for s in active if s.get("verdict") in ("up", "down")]
        base = i.get("baseline") or {}
        for n, s in enumerate(shown):
            now = {"lines": [f"{s['label']} {s['phrase']} {s['current_change']:+.0f}{s['unit']}",
                             f"(최근 1개월 · 역사 속 {'상위' if s['direction'] == 'up' else '하위'} {(1 - s['percentile'] if s['direction'] == 'up' else s['percentile']) * 100:.0f}%)"]}
            if s.get("median_return") is None:
                past = {"lines": [HISTORY_VERDICT_TEXT["insufficient"], f"(과거 같은 변동 {s['episodes']}회)"]}
            else:
                past = {"lines": [f"중앙값 {s['median_return']:+.1f}% · 하락 {s['neg_share'] * 100:.0f}% (평소 {(base.get('neg_share') or 0) * 100:.0f}%)",
                                  f"독립 사례 {s['episodes']}회 · {HISTORY_VERDICT_TEXT[s['verdict']]}"]}
            examples = {"lines": [f"{e['date'][:7]} {e['label'] + ': ' if e.get('label') else ''}요인 {e['factor_change']:+.0f}{s['unit']} → 이 종목 {e['asset_return']:+.1f}%"
                                  for e in s.get("examples", [])]} if s.get("examples") else "-"
            rows.append([head if n == 0 else "", now, past, examples])
        if not shown:
            rows.append([head, "지금 큰 변동 중인 요인이 없습니다", "-", "-"])
        for s in significant[:2]:
            bullets.append(f"{i['name'] or i['symbol']}: 최근 1개월 {s['label']} {s['phrase']}({s['current_change']:+.0f}{s['unit']}) — 과거 같은 변동 {s['episodes']}회 중 "
                           f"{s['neg_share'] * 100:.0f}%에서 하락, 중앙값 {s['median_return']:+.1f}%(평소 하락 {(base.get('neg_share') or 0) * 100:.0f}%). "
                           "같이 움직인 기록이며 원인은 아닙니다.")
        if not significant and shown:
            bullets.append(f"{i['name'] or i['symbol']}: 지금 큰 변동 중인 요인에 대해 과거 기록으로 뚜렷하게 구분되는 패턴은 없습니다.")
        worst, best = i.get("worst_events") or [], i.get("best_events") or []
        fmt = lambda e: f"{e['name']} {e['return_pct']:+.1f}% (z {e['z']:+.1f})"
        if worst or best:
            event_rows.append([head["lines"][0], {"lines": [fmt(e) for e in worst]} if worst else "-", {"lines": [fmt(e) for e in best]} if best else "-"])
    if rows:
        tables.append({"title": "지금 큰 변동 중인 요인과, 과거 같은 변동 때 이 종목 (같이 움직인 기록 · 원인 증명 아님)",
                       "columns": ["종목", "지금의 큰 변동", "과거 같은 방향의 큰 변동 때", "대표 사례 (요인이 가장 극단적이었던 때)"],
                       "align": ["l", "l", "l", "l"], "wrap": [True, True, True, True], "rows": rows, "footer": []})
    if event_rows:
        tables.append({"title": "이 종목이 가장 크게 반응했던 거시 이슈 (z = 수익률 ÷ 평소 변동성)",
                       "columns": ["종목", "가장 크게 떨어진 이슈", "가장 크게 오른 이슈"], "align": ["l", "l", "l"],
                       "wrap": [False, True, True], "rows": event_rows, "footer": []})
    if failed:
        notes.append("시세 기록이 짧거나 받지 못해 계산하지 못한 종목: " + ", ".join(failed))
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    if int(data.get("omitted") or 0) > 0:
        notes.append(f"그 밖의 {int(data['omitted'])}개 종목은 넣지 않았습니다(최대 6개).")
    limitations.extend(data.get("limitations", []))


CASE_MOVE_ROWS = (("gold", "dxy"), ("tnx", "irx"), ("spx", "vix"), ("oil", "xlf"), ("FRED:DFF", "FRED:DGS2"), ("FRED:DFII10", "FRED:T10YIE"),
                  ("FRED:CPIYOY", "FRED:UNRATE"), ("FRED:WALCL",))


CONVENTION_MARK = {True: "✓ 맞음", False: "✗ 달랐음", None: "· 뚜렷하지 않음"}


def _move_lines(moves: list[dict]) -> list[str]:
    by_key = {m["key"]: m for m in moves}
    lines = []
    for group in CASE_MOVE_ROWS:
        parts = [f"{by_key[k]['label']} {_signed(by_key[k]['value'], 0)}{by_key[k]['unit']}" for k in group if k in by_key]
        if parts:
            lines.append(" · ".join(parts))
    return lines or ["시세 기록이 없는 구간입니다"]


def _compose_cases(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """사례집: 지정한 사례의 국면별 서사·숫자·통념 점검, 그리고 지금과 닮은 과거 국면. 이야기는 해석이고 닮은 국면이 같은 결말을 보장하지 않는다."""
    cases, similar, asset = data.get("cases") or [], data.get("similar") or [], data.get("asset")
    for c in cases:
        bullets.append(f"{c['title']}: {c['summary']} (해석)")
        wrong = [(p, v) for p in c["phases"] for v in p["conventions"] if v["held"] is False]
        for p, v in wrong[:3]:
            bullets.append(f"통념과 달랐던 점 — {p['name']}: '{v['rule']}'는 맞지 않았습니다({v['detail']}).")
        rows = []
        for p in c["phases"]:
            numbers = _move_lines(p["moves"])
            if asset and p.get("asset_move_pct") is not None:
                numbers.append(f"{asset} {_signed(p['asset_move_pct'])}%" + (f" → 그 뒤 3개월 {_signed(p['asset_after_pct'])}%" if p.get("asset_after_pct") is not None else ""))
            checks = [f"{CONVENTION_MARK[v['held']]}: {v['rule']} ({v['detail']})" for v in p["conventions"]] or ["판정할 만큼 큰 변동이 없었습니다"]
            if p.get("gold_after_pct") is not None:
                numbers.append(f"금은 국면이 끝난 뒤 3개월간 {_signed(p['gold_after_pct'])}%")
            rows.append([{"lines": [p["name"], f"{p['start']} ~ {p['end']}"]}, p["story"], {"lines": numbers}, {"lines": checks}])
        tables.append({"title": f"사례: {c['title']} (이야기는 해석 · 숫자는 시세 계산)",
                       "columns": ["국면", "무슨 일 (해석)", "그때 숫자", "통념 점검 (금)"],
                       "align": ["l", "l", "l", "l"], "wrap": [True, True, True, True], "rows": rows, "footer": []})
    if similar:
        rows = []
        for n, s in enumerate(similar, 1):
            diff = [f"{x['label']}: 지금 {_signed(x['now'], 0)}{x['unit']} ↔ 그때 {_signed(x['then'], 0)}{x['unit']}" + (" (비슷)" if x["close"] else "") for x in s["compare"][:5]]
            if s.get("gold_after_pct") is not None:
                diff.append(f"그 국면이 끝난 뒤 3개월간 금 {_signed(s['gold_after_pct'])}%")
            rows.append([{"lines": [f"{n}. {s['case_title']}", f"{s['phase']} ({s['start']} ~ {s['end']})"]},
                         f"{s['distance']:.2f} (낮을수록 닮음 · 요인 {s['shared']}개)", {"lines": diff}])
        tables.append({"title": "지금(최근 6개월)과 요인 움직임이 닮은 과거 국면 (같은 결말을 보장하지 않음)",
                       "columns": ["과거 국면", "닮은 정도", "지금 ↔ 그때 (최근 6개월 vs 그 국면 전체)"],
                       "align": ["l", "l", "l"], "wrap": [True, False, True], "rows": rows, "footer": []})
        top = similar[0]
        bullets.append(f"지금과 요인 움직임이 가장 닮은 과거 국면: {top['case_title']} · {top['phase']}"
                       + (f". 그 국면이 끝난 뒤 3개월간 금은 {_signed(top['gold_after_pct'])}%였습니다" if top.get("gold_after_pct") is not None else "")
                       + ". 닮았다는 것이 같은 결말을 뜻하지는 않으며 예측이 아닙니다.")
    elif not cases:
        notes.append("지금과 닮은 국면을 찾기에는 비교할 요인 시세가 모자랍니다.")
    if not cases and data.get("available"):
        bullets.append("사례집: " + ", ".join(a["title"] for a in data["available"][:12]) + ". 사례 이름이나 연도로 물어보세요(예: 2008 금융위기 때 금은?).")
    if len(cases) >= 3:
        notes.append("사례는 한 번에 3개까지 보여줍니다.")
    if not data.get("has_fred"):
        notes.append("FRED 키를 넣으면 연방기금금리·10년 실질금리·기대인플레이션도 사례 숫자와 통념 점검에 함께 씁니다(Agent 탭 AI 연결 설정).")
    limitations.extend(data.get("limitations", []))


DRIVER_ROWS_PER_ASSET = 3


def _driver_direction(d: dict, name: str) -> str:
    """상관 부호로 '요인이 오르면 이 종목은 오르는/내리는 경향' 문장."""
    positive = sum(d["corr"].values()) > 0
    return f"{d['label']}{_josa(d['label'], '이', '가')} 오르면 {name}{_josa(name, '은', '는')} " + ("오르는" if positive else "내리는") + " 경향"


def _compose_drivers(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """종목이 평소 가장 민감한 요인 순위. 요인마다 창별 상관·일관성·1표준편차 반응·묶은 요인을 보여 주고 원인으로 단정하지 않는다."""
    items = data.get("items") or []
    if not items:
        bullets.append("요인 순위를 볼 보유 종목이나 관심종목이 없습니다.")
        return
    rows, failed = [], []
    for i in items:
        name = i["name"] or i["symbol"]
        if i.get("status") != "ok" or not (i.get("drivers") or i.get("peers")):
            failed.append(i["symbol"])
            continue
        best = i["drivers"]
        if best:
            bullets.append(f"{name}이(가) 평소 가장 영향받는 매크로 요인: " + ", ".join(f"{d['label']}({'+' if sum(d['corr'].values()) > 0 else '−'})" for d in best) + " — 막대는 일간 상관(+같이·−반대로), 원인은 아닙니다.")
        weakening = [d for d in best if d["consistent"] and len(d["corr"]) >= 2 and abs(list(d["corr"].values())[0]) > 1.5 * abs(list(d["corr"].values())[-1])]
        for d in weakening[:1]:
            first, last = list(d["corr"].items())[0], list(d["corr"].items())[-1]
            bullets.append(f"{d['label']}의 영향은 최근 {first[0]} 상관 {_signed(first[1], 2)}로 {last[0]} 상관 {_signed(last[1], 2)}보다 훨씬 강해 최근에 두드러진 관계일 수 있습니다.")
        if i.get("model"):
            m = i["model"]
            notes.append(f"{i['symbol']}: 최근 1년 회귀 모형은 {', '.join(m['factors'])}로 변동의 {m['r2'] * 100:.0f}%를 설명하고, 뒤 구간 검증에서는 "
                         + (f"{m['oos_r2'] * 100:.0f}%" if m.get("oos_r2") is not None else "검증 불가") + "입니다(나머지는 요인으로 설명되지 않는 종목 고유 움직임).")
        if i.get("others"):
            notes.append(f"{i['symbol']}: 순위 밖에서 상관이 컸던 요인: " + ", ".join(f"{d['label']}({_signed(sum(d['corr'].values()) / len(d['corr']), 2)})" for d in i["others"]) + ".")
    if failed:
        notes.append("시세가 짧거나 받지 못해 계산하지 못한 종목: " + ", ".join(failed))
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    if int(data.get("omitted") or 0) > 0:
        notes.append(f"그 밖의 {int(data['omitted'])}개 종목은 넣지 않았습니다(최대 4개).")
    limitations.extend(data.get("limitations", []))


HORIZON_TEXT = {5: "일주일(5거래일)", 21: "한 달(21거래일)", 63: "3개월(63거래일)"}


RESPONSE_VERDICT = {"higher": "평소보다 강함 (통계적으로 구분됨)", "lower": "평소보다 약함 (통계적으로 구분됨)", "mixed": "평소와 뚜렷이 다르지 않음", "insufficient": "구간이 적어 판단하지 않음"}


RESPONSE_MARK = {"higher": " ▲", "lower": " ▼"}


BUCKET_TEXT = {"up": "큰 상승 구간(상위 20%)", "down": "큰 하락 구간(하위 20%)", "flat": "보통 구간(큰 변화 아님)"}


BUCKET_SHORT = {"up": "큰 상승 구간", "down": "큰 하락 구간"}


def _change_text(value, kind: str) -> str:
    return "-" if value is None else (f"{value:+.0f}bp" if kind == "level" else f"{value:+.1f}%")


def _compose_response(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """요인이 크게 오른/내린 구간에서 자산의 평소 대비 반응: 묶음 막대 그래프(큰 하락·평소·큰 상승 구간의 수익률 중앙값) + 자산마다 한 줄 판정."""
    factor, kind = data["factor"]["name"], data["factor"]["kind"]
    th, rows = data.get("thresholds"), data.get("assets") or []
    if not th or not rows:
        bullets.append(f"{factor}의 구간 변화를 구할 만큼 시세가 없거나 자산 시세를 받지 못했습니다.")
        limitations.extend(data.get("limitations", []))
        return
    horizon = HORIZON_TEXT.get(data["horizon"], f"{data['horizon']}거래일")
    asked = data["direction"]
    bullets.append(f"{factor}{_josa(factor, '이', '가')} {horizon} 동안 {_change_text(th['up'], kind)} 이상 오른 때(상위 20%)·{_change_text(th['down'], kind)} 이하로 내린 때(하위 20%)의 자산 반응입니다.")
    for r in rows:
        st = r[asked]
        if st["median"] is None:
            continue
        corr = r["correlation"].get("1년")
        bullets.append(f"{r['name']}: {BUCKET_SHORT[asked]}에서 {RESPONSE_VERDICT[st['verdict']]}" + ("" if corr is None else f" · 일간 상관(1년) {corr:+.2f}"))
    def mark(st):
        return RESPONSE_MARK.get(st["verdict"], "")
    def text(st):
        return "" if st["median"] is None else f"{st['median']:+.1f}%{mark(st)}\n오른 {st['up_share'] * 100:.0f}%"
    sets = [{"label": "큰 하락 구간", "color": "s1", "data": [r["down"]["median"] for r in rows], "text": [text(r["down"]) for r in rows]},
            {"label": "평소", "color": "s2", "data": [r["baseline"]["median"] for r in rows], "text": [f"{r['baseline']['median']:+.1f}%\n오른 {r['baseline']['up_share'] * 100:.0f}%" for r in rows]},
            {"label": "큰 상승 구간", "color": "s3", "data": [r["up"]["median"] for r in rows], "text": [text(r["up"]) for r in rows]}]
    now = ""
    if data.get("current") is not None and data.get("current_bucket"):
        now = f"지금 {_change_text(data['current'], kind)} → {BUCKET_TEXT[data['current_bucket']]}"
    charts.append(chart_mod.make("bar", f"{factor} {horizon}: 큰 하락·평소·큰 상승 구간의 자산 수익률(중앙값)", [r["name"] for r in rows], sets, "%", subtitle=now,
                                 caption="▲▼=평소와 통계적으로 구분됨 · '오른 N%'=수익률이 플러스였던 구간 비율 · 같은 구간에 같이 움직인 기록(원인 증명 아님)"))
    if data.get("insufficient"):
        notes.append("시세가 모자라 뺀 자산: " + ", ".join(data["insufficient"]))
    if data.get("unresolved"):
        notes.append("알 수 없어 뺀 자산 이름: " + ", ".join(data["unresolved"]))
    limitations.extend(data.get("limitations", []))


# ---------------------------------------------------------------------------
# 그래프(charts): 숫자는 도구 결과 그대로, 글은 막대 끝 설명으로 옮긴다. 표는 세부 내용으로 아래에 남는다.
# ---------------------------------------------------------------------------
def _norm_factor(f: dict) -> float:
    return f["change"] / (10 if f["unit"] == "bp" else 1)      # 금리 10bp = 가격 1%로 환산해 막대 길이만 맞춘다


def _charts_macro(data: dict, charts: list) -> None:
    factors = [f for f in data.get("factors", []) if f.get("change") is not None]
    if factors:
        ranked = sorted(factors, key=lambda f: abs(_norm_factor(f)), reverse=True)[:10]
        values = [_norm_factor(f) for f in ranked]
        charts.append(chart_mod.make("hbar", "이번 주 시장 요인 변화 (많이 움직인 순)", [f["label"] for f in ranked],
                                     [{"label": "변화", "colors": chart_mod.signed_colors(values), "data": values, "text": [_factor_text(f["change"], f["unit"], f["phrase"]) for f in ranked]}], "",
                                     caption="막대 길이는 금리 10bp를 1%로 환산해 맞춘 것이고, 글은 실제 변화입니다", height=len(ranked) * 30 + 56))
    for i in (data.get("items") or [])[:3]:
        if i.get("status") != "ok" or not i.get("drivers"):
            continue
        drivers = sorted(i["drivers"], key=lambda d: abs(d.get("contribution_pct") or 0), reverse=True)
        shown = [d for d in drivers if abs(d.get("contribution_pct") or 0) >= 0.3] or drivers[:1]
        rest = (i.get("residual_pct") or 0) + sum(d["contribution_pct"] for d in drivers if d not in shown)
        values = [d["contribution_pct"] for d in shown] + [rest]
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} 이번 주 {i['weekly_change_pct']:+.1f}% — 요인별 기여 (통계 추정, %p)",
                                     [f"{d['label']} {_factor_text(d['factor_change'], d['unit'], d['phrase'])}" + (" (관계 불안정)" if d.get("stable") is False else "") for d in shown] + ["그 밖"],
                                     [{"label": "기여", "colors": chart_mod.signed_colors(values[:-1]) + ["neutral"], "data": values, "text": [f"{v:+.1f}%p" for v in values]}], "%p",
                                     subtitle=f"설명력 R² {_r2_text(i)}", caption="같이 움직인 정도의 통계 추정이며 원인의 증명이 아닙니다", height=len(values) * 40 + 60))


def _charts_drivers(data: dict, charts: list) -> None:
    for i in data.get("items") or []:
        if i.get("status") != "ok" or not (i.get("drivers") or i.get("peers")):
            continue
        entries = [(f"{n}. {d['label']}", d) for n, d in enumerate(i["drivers"], 1)] + [(f"{d['label']} (비슷한 자산)", d) for d in (i.get("peers") or [])]
        windows = list(i.get("windows") or [])
        if not entries or not windows:
            continue
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} — 평소 영향받는 요인 (일간 수익률 상관, +같이 · -반대로)", [label for label, _ in entries],
                                     [{"label": w, "color": f"s{k + 1}", "data": [d["corr"].get(w) for _, d in entries]} for k, w in enumerate(windows[:3])], "",
                                     subtitle=(f"기준: {i['source']}" if i.get("source") else ""), caption="같이 움직인 정도이며 원인의 증명이 아닙니다. 비슷한 자산은 같은 계열이라 같이 움직이는 것입니다",
                                     height=len(entries) * (len(windows) * 16 + 14) + 70, decimals=2))


def _charts_history(data: dict, charts: list) -> None:
    for i in [x for x in (data.get("items") or []) if x.get("status") == "ok"][:3]:
        base = i.get("baseline") or {}
        shown = [s for s in sorted(i.get("active") or [], key=_history_sort_key) if s.get("median_return") is not None][:HISTORY_ROWS_PER_ASSET]
        if shown:
            values = [s["median_return"] for s in shown]
            charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} — 지금 큰 변동 중인 요인과 같은 방향이던 과거의 수익률 (중앙값)",
                                         [f"{s['label']} {s['phrase']} {s['current_change']:+.0f}{s['unit']}" for s in shown],
                                         [{"label": "과거 중앙값", "colors": chart_mod.signed_colors(values), "data": values,
                                           "text": [f"{s['median_return']:+.1f}% · 하락 {s['neg_share'] * 100:.0f}% ({s['episodes']}회)" for s in shown]}], "%",
                                         caption=f"평소 하락 비율 {(base.get('neg_share') or 0) * 100:.0f}% · 같이 움직인 기록이며 원인의 증명이 아닙니다", height=len(shown) * 40 + 60))
        events = [(e, "worst") for e in (i.get("worst_events") or [])] + [(e, "best") for e in (i.get("best_events") or [])]
        if events:
            events.sort(key=lambda p: p[0]["return_pct"])
            values = [e["return_pct"] for e, _ in events]
            charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} — 가장 크게 반응했던 거시 이슈 (수익률)", [e["name"] for e, _ in events],
                                         [{"label": "수익률", "colors": chart_mod.signed_colors(values), "data": values, "text": [f"{e['return_pct']:+.1f}% (z {e['z']:+.1f})" for e, _ in events]}], "%",
                                         caption="z = 수익률 ÷ 평소 변동성", height=len(events) * 34 + 60))


def _charts_cases(data: dict, charts: list) -> None:
    for c in data.get("cases") or []:
        phases = [p for p in c["phases"] if p.get("moves")]
        if not phases:
            continue
        def pick(p, key):
            return next((m["value"] for m in p["moves"] if m["key"] == key), None)
        sets = [{"label": "금", "color": "s3", "data": [pick(p, "gold") for p in phases]}, {"label": "달러", "color": "s1", "data": [pick(p, "dxy") for p in phases]},
                {"label": "S&P500", "color": "s2", "data": [pick(p, "spx") for p in phases]}]
        asset = data.get("asset")
        if asset and any(p.get("asset_move_pct") is not None for p in phases):
            sets.append({"label": str(asset)[:20], "color": "s4", "data": [p.get("asset_move_pct") for p in phases]})
        charts.append(chart_mod.make("bar", f"{c['title']} — 국면별 금·달러·S&P500 변화 (%)", [p["name"] for p in phases], sets, "%",
                                     caption="이야기(해석)는 아래 표, 숫자는 시세로 계산한 값입니다. 닮은 국면이 같은 결말을 보장하지 않습니다", height=300))


def _charts_news(data: dict, charts: list) -> None:
    moved = [i for i in (data.get("items") or []) if i.get("weekly_change_pct") is not None][:12]
    if len(moved) < 2:
        return
    values = [i["weekly_change_pct"] for i in moved]
    charts.append(chart_mod.make("hbar", "이번 주(최근 5거래일) 종목별 등락", [i["name"] or i["symbol"] for i in moved],
                                 [{"label": "등락", "colors": chart_mod.signed_colors(values), "data": values}], "%", height=len(moved) * 30 + 56))


def compose_get_asset_drivers(drivers_result, bullets, tables, notes, limitations, charts, results):
    """get_asset_drivers 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_drivers(drivers_result, bullets, tables, notes, limitations)
    _charts_drivers(drivers_result, charts)


def compose_get_macro_cases(cases_result, bullets, tables, notes, limitations, charts, results):
    """get_macro_cases 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_cases(cases_result, bullets, tables, notes, limitations)
    _charts_cases(cases_result, charts)


def compose_get_macro_history(history_result, bullets, tables, notes, limitations, charts, results):
    """get_macro_history 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_history(history_result, bullets, tables, notes, limitations)
    _charts_history(history_result, charts)


def compose_get_factor_response(response_data, bullets, tables, notes, limitations, charts, results):
    """get_factor_response 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_response(response_data, bullets, tables, notes, limitations, charts)


def compose_get_macro_drivers(macro_result, bullets, tables, notes, limitations, charts, results):
    """get_macro_drivers 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_macro(macro_result, bullets, tables, notes, limitations)
    _charts_macro(macro_result, charts)


def compose_get_weekly_news(weekly_news_result, bullets, tables, notes, limitations, charts, results):
    """get_weekly_news 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_news(weekly_news_result, bullets, tables, notes, limitations)
    _charts_news(weekly_news_result, charts)


FORMATTERS = {"get_asset_drivers": asset_drivers_result, "get_macro_cases": macro_cases_result, "get_macro_history": macro_history_result, "get_factor_response": factor_response_result, "get_macro_drivers": macro_drivers, "get_weekly_news": weekly_news}
COMPOSERS = {"get_asset_drivers": compose_get_asset_drivers, "get_macro_cases": compose_get_macro_cases, "get_macro_history": compose_get_macro_history, "get_factor_response": compose_get_factor_response, "get_macro_drivers": compose_get_macro_drivers, "get_weekly_news": compose_get_weekly_news}
