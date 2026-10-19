"""종목 전망·ETF 구성 종목 분해 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from outlook import DEPTH_TEXT, MIN_ANALOGS, MIN_EPISODES, TREND_TEXT
from news import HEADLINES_PER_SYMBOL, SOURCE_MAX, TITLE_MAX, safe_url
from .common import _number, _origin_tag, _round, _signed
from .macro import _factor_text


DEFINITIONS = [
    {
        "name": "get_outlook",
        "description": "보유·관심 종목의 앞으로의 상승/하락 뷰 재료를 조회합니다: 현재 국면, 과거 유사 국면에서 이후 1·3개월 수익률 통계(독립 사례 수 포함), 요인(달러·금리 등) 민감도 시나리오, 변동성 범위. 예측이 아니라 조건부 시나리오이며 투자 조언이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "보고 싶은 종목 이름·코드·카테고리 이름(예: 금, TLT, 스트래티지). 비우면 보유·관심 종목 전체"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_etf_breakdown",
        "description": "보유·관심 ETF의 이번 주(최근 5거래일) 등락을 야후가 공시한 상위 편입 종목의 기여(비중 × 종목 등락, %p)로 나누고, 큰 기여 종목의 매크로 요인과 최근 보도를 붙입니다. 상위 편입 종목은 전체의 일부이고 공시 시점 비중이라 '그 밖'이 남으며, 합산 분해일 뿐 원인의 증명이 아닙니다. 편입 정보가 없는 종목(개별주·금/채권 ETF·한국 ETF 등)은 대상이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 ETF만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "분해할 ETF 이름·코드·카테고리 이름(예: UFO, 우주). 비우면 보유·관심 ETF 전체"}},
                         "additionalProperties": False},
    },
]


def outlook_view_result(data: dict) -> dict:
    """뷰 재료에서 숫자와 짧은 이름만 남긴다(종목 12개, 시나리오 요인 3개까지)."""
    items = []
    for it in data.get("items", [])[:12]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")), "extra": bool(it.get("extra")),
                "status": "ok" if it.get("status") == "ok" else "no_data", "history_days": int(_number(it.get("history_days")) or 0)}
        if base["status"] == "ok":
            an = it.get("analogs") or {}
            fwd = {k: ({m: _round((an.get("forward") or {}).get(k, {}).get(m)) if m != "n" else int(_number((an.get("forward") or {}).get(k, {}).get(m)) or 0)
                        for m in ("n", "median", "p25", "p75", "up_share")} if (an.get("forward") or {}).get(k) else None)
                   for k in ("1M", "3M")}
            scen = it.get("scenarios") or {}
            clean = lambda rows: [{"label": str(r.get("label") or ""), "phrase": str(r.get("phrase") or ""), "move": str(r.get("move") or ""),
                                   "impact_pct": _round(r.get("impact_pct"))} for r in (rows or [])[:3]]
            base.update({
                "price": _round(it.get("price")), "trend": it.get("trend") if it.get("trend") in TREND_TEXT else "below",
                "depth": it.get("depth") if it.get("depth") in DEPTH_TEXT else "pullback", "drawdown_pct": _round(it.get("drawdown_pct"), 1),
                "month_change_pct": _round(it.get("month_change_pct"), 1), "annual_vol_pct": _round(it.get("annual_vol_pct"), 0),
                "bands": {k: {"low_pct": _round((it.get("bands") or {}).get(k, {}).get("low_pct"), 1),
                              "high_pct": _round((it.get("bands") or {}).get(k, {}).get("high_pct"), 1),
                              "low": _round((it.get("bands") or {}).get(k, {}).get("low")), "high": _round((it.get("bands") or {}).get(k, {}).get("high"))}
                          for k in ("1M", "3M")},
                "analogs": {"relaxed": bool(an.get("relaxed")), "episodes": int(_number(an.get("episodes")) or 0), "forward": fwd,
                            "source": ({"label": str(an["source"].get("label") or "")[:60], "kind": "monthly" if an["source"].get("kind") == "monthly" else "daily",
                                        "from": str(an["source"].get("from") or "")[:10], "to": str(an["source"].get("to") or "")[:10],
                                        "points": int(_number(an["source"].get("points")) or 0)} if isinstance(an.get("source"), dict) else None),
                            "source_error": str(an.get("source_error") or "")[:60] or None,
                            "baseline_up_share_3m": _round((an.get("baseline_3m") or {}).get("up_share")),
                            "skew": an.get("skew") if an.get("skew") in ("up", "down", "mixed", "insufficient") else "insufficient"},
                "scenarios": {"r2": _round(scen.get("r2")), "up": clean(scen.get("up")), "down": clean(scen.get("down"))},
            })
        items.append(base)
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "상승/하락 뷰는 예측이나 투자 조언이 아니라, 과거 통계·요인 민감도·변동성으로 만든 조건부 시나리오입니다.",
            f"과거 유사 상황은 200일선 위치와 52주 고점 대비 낙폭 구간이 같았던 시점의 이후 수익률입니다. 기본은 같은 종목의 상장 이후 시세(최대 30년)이고, 금 ETF·주요 지수 ETF는 더 긴 대용 시세(금 월간 시세 1975년~, 지수 일봉)를 쓰며 표에 기준을 밝힙니다. 유사 시점이 {MIN_ANALOGS}개 미만이거나 독립 사례가 {MIN_EPISODES}회 미만이면 판단하지 않습니다.",
            "요인 시나리오는 최근 1년 회귀 계수 기준이며 설명력(R²) 0.2 미만이면 만들지 않습니다. 시장 구조가 바뀌면 달라질 수 있습니다.",
            "변동성 범위는 최근 60일 일간 변동성의 ±1표준편차로 폭만 보여주며 방향을 예측하지 않습니다.",
        ] + (["월간 시세는 월평균 가격이고 한 달에 한 점이라, 같은 규칙(200일선·52주 고점)을 10개월 평균선·12개월 고점으로 근사해 일간 자료보다 국면 판정이 거칩니다. 금 가격이 자유화된 1975년 이후만 씁니다."]
             if any((i.get("analogs") or {}).get("source", {}) and i["analogs"]["source"]["kind"] == "monthly" for i in items if i.get("status") == "ok") else [])
          + (["대용 시세(지수)는 ETF와 같은 상품이 아니며 가격 기준(배당 제외)입니다. 현재 국면은 항상 종목 자신의 시세로 판정합니다."]
             if any((i.get("analogs") or {}).get("source", {}) and i["analogs"]["source"]["kind"] == "daily" for i in items if i.get("status") == "ok") else []),
    }


def etf_breakdown_result(data: dict) -> dict:
    """ETF 구성 종목 기여에서 숫자와 짧은 이름만 남긴다(ETF 6개, 편입 종목 15개, 큰 기여 종목 요인 2개·보도 3건까지)."""
    items = []
    for it in data.get("items", [])[:6]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")), "extra": bool(it.get("extra")),
                "status": "ok" if it.get("status") == "ok" else "no_data"}
        if base["status"] == "ok":
            rows = [{"symbol": str(r.get("symbol") or "")[:15], "name": str(r.get("name") or "")[:60], "weight_pct": _round(r.get("weight_pct")),
                     "change_pct": _round(r.get("change_pct")), "contribution_pct": _round(r.get("contribution_pct"), 3)}
                    for r in it.get("rows", [])[:15]]
            drivers = {}
            for sym, d in (it.get("drivers") or {}).items():
                drivers[str(sym)[:15]] = {
                    "status": "ok" if d.get("status") == "ok" else str(d.get("status") or "no_data"), "r2": _round(d.get("r2"), 2),
                    "drivers": [{"label": str(c.get("label") or ""), "phrase": str(c.get("phrase") or ""), "factor_change": _round(c.get("factor_change")),
                                 "unit": "bp" if c.get("kind") == "level" else "%", "contribution_pct": _round(c.get("contribution_pct"), 2)}
                                for c in (d.get("contributions") or [])[:2]]}
            headlines = {}
            for sym, block in (it.get("headlines") or {}).items():
                headlines[str(sym)[:15]] = {"error": bool(block.get("error")), "headlines": [
                    {"title": str(h["title"])[:TITLE_MAX], "source": str(h.get("source") or "")[:SOURCE_MAX],
                     "published": str(h.get("published") or "")[:10], "url": safe_url(h.get("url"))}
                    for h in (block.get("headlines") or [])[:HEADLINES_PER_SYMBOL] if h.get("title") and safe_url(h.get("url"))]}
            base.update({
                "window": [str(x)[:10] for x in (it.get("window") or [])[:2]], "etf_change_pct": _round(it.get("etf_change_pct")),
                "rows": rows, "priced_weight_pct": _round(it.get("priced_weight_pct")), "rest_weight_pct": _round(it.get("rest_weight_pct")),
                "explained_pct": _round(it.get("explained_pct"), 2), "residual_pct": _round(it.get("residual_pct"), 2),
                "covered_return_pct": _round(it.get("covered_return_pct")), "rest_implied_pct": _round(it.get("rest_implied_pct")),
                "flags": [f for f in it.get("flags", []) if f in ("low_coverage", "diverged", "missing_prices")],
                "drivers": drivers, "headlines": headlines})
        items.append(base)
    reasons = {"none": "편입 정보 없음", "unavailable": "편입 조회 도구(yfinance) 없음", "error": "편입 정보 조회 실패"}
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "no_holdings": [{"symbol": str(n.get("symbol") or ""), "name": str(n.get("name") or ""), "reason": reasons.get(n.get("reason"), "편입 정보 없음")}
                        for n in data.get("no_holdings", [])[:30]],
        "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "편입 종목과 비중은 야후가 공시한 상위 종목(보통 10개)이라 ETF 전체의 일부이고 공시 시점 값입니다. 나머지는 '그 밖'으로 남습니다.",
            "기여는 비중 × 종목 등락의 합산 분해이며, 같은 기간(ETF의 최근 5거래일 날짜 기준)으로 맞춰 계산했습니다. 원인의 증명이 아니고, 종목이 왜 움직였는지는 보도나 요인으로 따로 봐야 합니다.",
            "큰 기여 종목의 요인은 그 종목 자체의 최근 1년 회귀(설명력 R²)이며, 해외 종목은 현지 통화 기준 등락입니다.",
            "금·채권 ETF, 한국 ETF, 개별주는 야후에 편입 정보가 없어 분해하지 못합니다.",
        ],
    }


SKEW_TEXT = {"up": "과거 기준 상승 우세", "down": "과거 기준 하락 우세", "mixed": "과거 기준 혼조", "insufficient": "표본 부족 — 판단하지 않음"}


def _scenario_line(s: dict) -> str:
    return f"{s['label']} {s['phrase']}({s['move']}) → 약 {s['impact_pct']:+.1f}%"


def _compose_outlook(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """종목별 앞으로의 상승/하락 뷰 표. 상승·하락 시나리오를 나란히, 과거 통계는 표본 수와 함께, 근거가 모자라면 판단하지 않는다."""
    items = data.get("items") or []
    if not items:
        bullets.append("뷰를 만들 보유 종목이나 관심종목이 없습니다.")
        return
    rows = []
    for i in items:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        label += _origin_tag(i)
        if i.get("status") != "ok":
            rows.append([label, "시세 기록이 짧아(" + f"{i.get('history_days', 0)}거래일) 계산하지 못했습니다", "-", "-", "-"])
            continue
        state = {"lines": [f"{TREND_TEXT[i['trend']]} · {DEPTH_TEXT[i['depth']]}(고점 대비 {i['drawdown_pct']:.0f}%)",
                           f"최근 1개월 {i['month_change_pct']:+.1f}% · 연 변동성 {i['annual_vol_pct']:.0f}%" if i.get("month_change_pct") is not None
                           else f"연 변동성 {i['annual_vol_pct']:.0f}%"]}
        scen, bands = i["scenarios"], i["bands"]["1M"]
        up = [_scenario_line(s) for s in scen["up"][:2]] + [f"1개월 변동성 범위 상단 {bands['high_pct']:+.1f}%"]
        down = [_scenario_line(s) for s in scen["down"][:2]] + [f"1개월 변동성 범위 하단 {bands['low_pct']:+.1f}%"]
        if not scen["up"] and not scen["down"]:
            note = f"요인 민감도 낮음(R² {scen['r2']:.2f})" if scen.get("r2") is not None else "요인 데이터 없음"
            up.insert(0, note)
        an = i["analogs"]
        f3 = (an.get("forward") or {}).get("3M")
        if f3 and an.get("skew") != "insufficient":
            past = [f"3개월 후 중앙값 {f3['median']:+.1f}% · 상승 비율 {f3['up_share'] * 100:.0f}% (표본 {f3['n']}개, 독립 {an['episodes']}회{' · 국면 완화' if an.get('relaxed') else ''})",
                    f"평소 상승 비율 {(an.get('baseline_up_share_3m') or 0) * 100:.0f}%", SKEW_TEXT[an["skew"]]]
        else:
            n = f3["n"] if f3 else 0
            past = [SKEW_TEXT["insufficient"], f"(유사 시점 {n}개, 독립 {an.get('episodes', 0)}회)"]
        src = an.get("source")
        if src:
            past.append(f"기준: {src['label']} {src['from'][:4]}~{src['to'][:4]}" + (" (월간 근사)" if src["kind"] == "monthly" else " (가격 기준)"))
        elif an.get("source_error"):
            past.append(f"{an['source_error']}를 받지 못해 종목 자신의 시세로 계산했습니다")
        rows.append([label, state, {"lines": up}, {"lines": down}, {"lines": past}])
    tables.append({"title": "앞으로의 상승/하락 뷰 (조건부 시나리오 · 예측 아님)",
                   "columns": ["종목", "현재 상태", "상승 시나리오", "하락 시나리오", "과거 유사 상황 (3개월 후)"],
                   "align": ["l", "l", "l", "l", "l"], "wrap": [False, True, True, True, True], "rows": rows, "footer": []})
    bullets.append("앞으로의 뷰: 과거 통계·요인 민감도·변동성으로 만든 조건부 시나리오이며 예측이나 투자 조언이 아닙니다.")
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    elif data.get("focus"):
        notes.append("질문에서 특정 종목을 찾지 못해 보유·관심 종목 전체를 보여줍니다.")
    if data.get("omitted"):
        notes.append(f"그 밖의 {int(data['omitted'])}개 종목은 표에 넣지 않았습니다(최대 12개).")
    limitations.extend(data.get("limitations", []))


ETF_FLAG_TEXT = {
    "low_coverage": "시세를 받은 상위 편입 종목의 비중이 작아 설명 범위가 좁습니다.",
    "diverged": "상위 편입 종목의 평균 등락과 '그 밖'의 암시 등락이 크게 달라, 움직임이 상위 종목에 쏠렸거나 공시 비중이 오래됐을 수 있습니다(많이 움직인 종목은 지금 비중이 공시보다 작아 기여가 크게 계산될 수 있음).",
    "missing_prices": "일부 편입 종목은 시세를 받지 못해 계산에서 빠졌습니다(상장 전 종목·선물·비상장 등).",
}


ETF_TABLE_ROWS = 6


ETF_SHOWN_ROWS = 6


def _compose_etf(data: dict, bullets: list, tables: list, notes: list, limitations: list, standalone: bool = True) -> None:
    """ETF 이번 주 등락을 상위 편입 종목 기여로 나눈 표와 요약. 합산 분해이며 원인으로 단정하지 않는다.
    standalone=False(다른 도구와 함께 호출됨)이면 '나눌 수 없음' 안내는 본문이 아니라 메모로만 남긴다."""
    items = data.get("items") or []
    ok = [i for i in items if i.get("status") == "ok"]
    rows = []
    for i in ok[:ETF_TABLE_ROWS]:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        label += _origin_tag(i)
        moves = []
        for r in (i.get("rows") or [])[:ETF_SHOWN_ROWS]:
            if r.get("contribution_pct") is None:
                moves.append(f"{r['symbol']} {r['weight_pct']:.1f}% · 시세 없음")
            else:
                moves.append(f"{r['symbol']} {r['weight_pct']:.1f}% · {_signed(r['change_pct'])}% → {_signed(r['contribution_pct'], 2)}%p")
        rest = f"그 밖 {i['rest_weight_pct']:.0f}% → {_signed(i['residual_pct'], 2)}%p"
        if i.get("rest_implied_pct") is not None:
            rest += f" (평균 {_signed(i['rest_implied_pct'])}% 움직인 셈)"
        moves.append(rest)
        factor_lines = []
        for r in [r for r in (i.get("rows") or []) if r.get("contribution_pct") is not None][:3]:
            d = (i.get("drivers") or {}).get(r["symbol"])
            if not d:
                continue
            if d.get("status") == "ok" and d.get("drivers"):
                top = max(d["drivers"], key=lambda x: abs(x.get("contribution_pct") or 0))
                factor_lines.append(f"{r['symbol']}: {top['label']} {_factor_text(top['factor_change'], top['unit'], top['phrase'])}"
                                    f" → {_signed(top['contribution_pct'])}%p (R² {d['r2']:.2f})")
            else:
                factor_lines.append(f"{r['symbol']}: 요인으로 거의 설명되지 않음")
        links = []
        for sym, block in (i.get("headlines") or {}).items():
            for h in block.get("headlines", []):
                links.append({"text": f"[{sym}] {h['title']}", "href": h["url"],
                              "meta": " · ".join(x for x in (h.get("source"), h.get("published")) if x)})
        news_cell = {"links": links} if links else "큰 기여 종목의 최근 7일 보도를 찾지 못했습니다"
        head = {"lines": [label, f"이번 주 {_signed(i['etf_change_pct'])}%", f"상위 {i['priced_weight_pct']:.0f}% 반영"]}
        rows.append([head, {"lines": moves}, {"lines": factor_lines} if factor_lines else "-", news_cell])
        # 요약 문장: ETF가 오른 주면 올린 종목, 내린 주면 내린 종목을 먼저
        sign = 1 if i["etf_change_pct"] >= 0 else -1
        leaders = [r for r in i["rows"] if r.get("contribution_pct") is not None and r["contribution_pct"] * sign > 0][:2]
        if leaders:
            names = ", ".join(f"{r['symbol']}({_signed(r['contribution_pct'], 2)}%p)" for r in leaders)
            bullets.append(f"{i['name'] or i['symbol']} 이번 주 {_signed(i['etf_change_pct'])}%: 상위 편입 종목 중 {names}가 가장 크게 "
                           f"{'끌어올렸' if sign > 0 else '끌어내렸'}습니다(상위 {i['priced_weight_pct']:.0f}% 반영, 합산 분해이며 원인 확정이 아닙니다).")
        else:
            bullets.append(f"{i['name'] or i['symbol']} 이번 주 {_signed(i['etf_change_pct'])}%: 상위 편입 종목의 기여가 뚜렷하지 않습니다"
                           f"(상위 {i['priced_weight_pct']:.0f}% 반영).")
        for flag in i.get("flags", []):
            notes.append(f"{i['symbol']}: {ETF_FLAG_TEXT[flag]}")
    if rows:
        tables.append({"title": "ETF 구성 종목별 기여 (공시 상위 편입 종목 기준 · 비중 · 종목 등락 → 기여)",
                       "columns": ["ETF", "구성 종목 기여", "큰 기여 종목의 요인", "큰 기여 종목 보도"],
                       "align": ["l", "l", "l", "l"], "wrap": [True, True, True, True], "rows": rows, "footer": []})
    failed = [i for i in items if i.get("status") != "ok"]
    if failed:
        notes.append("시세가 부족해 분해하지 못한 ETF: " + ", ".join(i["symbol"] for i in failed))
    missing = data.get("no_holdings") or []
    if not items:
        if data.get("focused") and missing:
            reasons = ", ".join(f"{n['name'] or n['symbol']}({n['reason']})" for n in missing[:5])
            message = f"지정한 종목은 구성 종목으로 나눌 수 없습니다: {reasons}."
        else:
            message = "구성 종목으로 나눌 수 있는 보유·관심 ETF가 없습니다(개별주·금/채권 ETF·한국 ETF는 편입 정보가 없습니다)."
        (bullets if standalone else notes).append(message)
    elif missing:
        notes.append(f"편입 정보가 없어 분해하지 못한 종목 {len(missing)}개(개별주·금/채권 ETF·한국 ETF 등): "
                     + ", ".join(n["symbol"] for n in missing[:8]) + ("…" if len(missing) > 8 else ""))
    if int(data.get("omitted") or 0) > 0:
        notes.append(f"그 밖의 {data['omitted']}개 종목은 이번 분해에 넣지 않았습니다.")
    limitations.extend(data.get("limitations", []))


def _charts_etf(data: dict, charts: list) -> None:
    for i in [x for x in (data.get("items") or []) if x.get("status") == "ok"][:3]:
        rows = [r for r in (i.get("rows") or [])[:ETF_SHOWN_ROWS] if r.get("contribution_pct") is not None]
        if not rows:
            continue
        values = [r["contribution_pct"] for r in rows] + [i.get("residual_pct")]
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} 이번 주 {i['etf_change_pct']:+.1f}% — 구성 종목별 기여 (%p)",
                                     [f"{r['symbol']} {r['weight_pct']:.1f}% ({r['change_pct']:+.1f}%)" for r in rows] + [f"그 밖 {i['rest_weight_pct']:.0f}%"],
                                     [{"label": "기여", "colors": chart_mod.signed_colors(values[:-1]) + ["neutral"], "data": values, "text": [f"{v:+.2f}%p" if v is not None else "" for v in values]}], "%p",
                                     subtitle=f"공시 상위 편입 종목 {i['priced_weight_pct']:.0f}% 반영", caption="비중 × 종목 등락의 합산 분해이며 원인의 증명이 아닙니다", height=len(values) * 38 + 60))


def _charts_outlook(data: dict, charts: list) -> None:
    for i in [x for x in (data.get("items") or []) if x.get("status") == "ok"][:3]:
        scen, band = i["scenarios"], i["bands"]["1M"]
        points = [(f"{s['label']} {s['phrase']}", s["impact_pct"]) for s in scen["up"][:2] + scen["down"][:2]]
        points += [("1개월 변동성 범위 상단", band["high_pct"]), ("1개월 변동성 범위 하단", band["low_pct"])]
        f3 = ((i.get("analogs") or {}).get("forward") or {}).get("3M")
        if f3 and (i.get("analogs") or {}).get("skew") != "insufficient":
            points.append((f"과거 유사 상황 3개월 후 중앙값 (독립 {i['analogs']['episodes']}회)", f3["median"]))
        points.sort(key=lambda p: -(p[1] if p[1] is not None else 0))
        values = [p[1] for p in points]
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} 앞으로의 뷰 — 시나리오와 변동성 범위 (%)", [p[0] for p in points],
                                     [{"label": "예상 변화", "colors": chart_mod.signed_colors(values), "data": values}], "%",
                                     subtitle=f"{TREND_TEXT[i['trend']]} · {DEPTH_TEXT[i['depth']]}(고점 대비 {i['drawdown_pct']:.0f}%)",
                                     caption="예측이 아니라 요인 민감도·변동성·과거 통계로 만든 조건부 시나리오입니다(투자 조언 아님)", height=len(points) * 34 + 60))


def compose_get_outlook(outlook_result, bullets, tables, notes, limitations, charts, results):
    """get_outlook 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_outlook(outlook_result, bullets, tables, notes, limitations)
    _charts_outlook(outlook_result, charts)


def compose_get_etf_breakdown(etf_result, bullets, tables, notes, limitations, charts, results):
    """get_etf_breakdown 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_etf(etf_result, bullets, tables, notes, limitations, standalone=set(results) == {"get_etf_breakdown"})
    _charts_etf(etf_result, charts)


FORMATTERS = {"get_outlook": outlook_view_result, "get_etf_breakdown": etf_breakdown_result}
COMPOSERS = {"get_outlook": compose_get_outlook, "get_etf_breakdown": compose_get_etf_breakdown}
