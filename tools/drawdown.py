"""고점 대비 낙폭 원인 분석 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from news import HEADLINES_PER_SYMBOL, SOURCE_MAX, TITLE_MAX, safe_url
from .common import _number, _round
from .macro import _factor_text, _r2_text
from .trend import _from_high_text


DEFINITIONS = [
    {
        "name": "get_drawdown_analysis",
        "description": "보유·관심 종목이 고점 대비 얼마나 빠졌고 왜 빠졌는지 조회합니다: 최근 1년 고점의 날짜·가격과 현재 낙폭, 그 사이 최저점, 상장 이후 전 고점 대비 낙폭, 같은 구간(고점 날짜~지금)에서 금리·달러·유가·VIX·환율·지수가 얼마나 움직였는지, 종목 하락의 요인별 기여 추정(%p, 최근 1년 일간 관계의 계수 × 구간 요인 변화)과 설명력(R²)·검증 설명력을 줍니다. 통계적 추정이며 인과의 증명이 아닙니다. 요인으로 설명되지 않으면(R² 낮음) 그렇다고 알리며, 그때는 get_weekly_news로 최근 보도를 같이 보세요(뉴스는 최근 7일만). '왜 올랐나·내렸나'가 이번 주면 get_macro_drivers를, 고점 대비 낙폭이면 이 도구를 씁니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: 금, TLT, UFO). 비우면 보유·관심 종목(최대 4개)"}}, "additionalProperties": False},
    },
]


def drawdown_result(data: dict) -> dict:
    """고점 대비 낙폭과 구간 요인 분해만 남긴다(숫자·날짜·짧은 이름, 종목 4개까지)."""
    items = []
    for it in (data.get("items") or [])[:4]:
        drivers = [{"label": str(d.get("label") or ""), "phrase": str(d.get("phrase") or ""), "factor_change": _number(d.get("factor_change")),
                    "unit": "bp" if d.get("kind") == "level" else "%", "contribution_pct": _number(d.get("contribution_pct")), "t": _round(d.get("t"), 1),
                    "stable": d.get("stable") if isinstance(d.get("stable"), bool) else None} for d in (it.get("drivers") or [])[:3]]
        moves = [{"label": str(m.get("label") or ""), "change": _number(m.get("change")), "unit": "bp" if m.get("unit") == "bp" else "%", "phrase": str(m.get("phrase") or ""),
                  "group": "peer" if m.get("group") == "peer" else "macro"} for m in (it.get("moves") or [])[:60]]
        row = {"symbol": str(it.get("symbol") or "")[:12], "name": str(it.get("name") or "")[:40], "held": bool(it.get("held")), "extra": bool(it.get("extra")),
               "status": it.get("status") if it.get("status") in ("ok", "weak", "near_peak", "no_data") else "no_data",
               "reason": it.get("reason") if it.get("reason") in ("low_r2", "unstable") else "", "basis": "market" if it.get("basis") == "market" else "macro",
               "r2": _number(it.get("r2")), "oos_r2": _round(it.get("oos_r2")), "drivers": drivers, "residual_pct": _round(it.get("residual_pct")), "moves": moves}
        for key in ("last_date", "peak_date", "ath_date", "low_date"):
            row[key] = str(it.get(key) or "")[:10] or None
        for key in ("last_close", "peak_close", "ath_close", "drawdown_pct", "ath_drawdown_pct", "low_drawdown_pct"):
            row[key] = _round(it.get(key), 2)
        row["since_days"] = int(_number(it.get("since_days")) or 0)
        ser = it.get("series") if isinstance(it.get("series"), dict) else {}
        row["series"] = {"dates": [str(d)[:10] for d in (ser.get("dates") or [])[:140]], "closes": [_round(c, 2) for c in (ser.get("closes") or [])[:140]]}
        row["pressure"] = [{"label": str(m.get("label") or "")[:40], "change": _number(m.get("change")), "unit": "bp" if m.get("unit") == "bp" else "%", "phrase": str(m.get("phrase") or "")[:30],
                            "corr": _round(m.get("corr"), 2), "sigma": _round(m.get("sigma"), 1), "direction": "down" if m.get("direction") == "down" else "up"} for m in (it.get("pressure") or [])[:6]]
        row["causes"] = []
        for c in (it.get("causes") or [])[:4]:
            ex = c.get("example") or {}
            url = safe_url(ex.get("url"))
            row["causes"].append({"theme": str(c.get("theme") or "")[:30], "days": [str(d)[:10] for d in (c.get("days") or [])[:4]], "biggest_day": str(c.get("biggest_day") or "")[:10],
                                  "example": {"title": str(ex["title"])[:TITLE_MAX], "source": str(ex.get("source") or "")[:SOURCE_MAX], "url": url} if ex.get("title") and url else None})
        row["drop_days"] = []
        for day in (it.get("drop_days") or [])[:4]:
            headlines = []
            for h in (day.get("headlines") or [])[:HEADLINES_PER_SYMBOL]:
                url = safe_url(h.get("url"))
                if url and h.get("title"):
                    headlines.append({"title": str(h["title"])[:TITLE_MAX], "source": str(h.get("source") or "")[:SOURCE_MAX], "published": str(h.get("published") or "")[:10], "url": url})
            row["drop_days"].append({"date": str(day.get("date") or "")[:10], "change_pct": _round(day.get("change_pct"), 1), "risk_down": bool(day.get("risk_down")),
                                     "movers": [{"label": str(m.get("label") or "")[:40], "change": _number(m.get("change")), "unit": "bp" if m.get("unit") == "bp" else "%",
                                                 "phrase": str(m.get("phrase") or "")[:30]} for m in (day.get("movers") or [])[:3]],
                                     "headlines": headlines})
        items.append(row)
    return {
        "items": items, "omitted": int(_number(data.get("omitted")) or 0), "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "limitations": [
            "최근 고점은 최근 1년(252거래일) 안의 최고 종가이고, 전 고점은 야후 시세가 있는 전체 기간(상장 이후)의 최고 종가입니다(가격 기준, 배당 제외).",
            "요인별 기여는 최근 약 1년 일간 수익률의 통계적 관계(회귀)로 추정한 값에 고점 이후 구간의 요인 변화를 곱한 것입니다. 같이 움직인 정도일 뿐 원인으로 확정할 수 없고, 구간이 길수록 관계가 바뀌었을 가능성이 커집니다. 설명력(R²)이 낮으면 신뢰하지 마세요.",
            "요인 표는 서술일 뿐입니다: 고점 이후 많이 움직인 요인이 곧 하락의 원인이라는 뜻이 아닙니다(종목과의 관계는 기여 추정으로 따로 봅니다).",
            "큰 하락일은 고점 이후 하루 하락폭이 큰 순(1% 이상, 같은 사건의 연속일은 하나로)이고, '같은 날 크게 움직인 시장'은 평소 일간 변동의 2배 이상 움직인 요인입니다. 같은 날 같이 움직인 기록일 뿐 원인의 증명이 아닙니다.",
            "날짜별 보도는 구글 뉴스 검색(비공식)을 그날 앞뒤 하루로 한정해 종목 이름과 그날 크게 움직인 요인 이름(+급락·급등)으로 찾은 제목입니다. 검색어에 걸린 제목일 뿐 그 사건이 하락의 원인이라고 확정할 수 없고, 오래된 날짜는 기사가 빠질 수 있습니다.",
        ],
    }


def _compose_drawdown(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """종목마다: 최근 고점 대비 낙폭 → 같은 구간 요인 기여(통계 추정) → 그 구간 시장 요인 변화 표. 설명이 안 되면 그렇다고 말하고 뉴스를 보라고 한다."""
    items = data.get("items") or []
    if not items:
        bullets.append("고점 대비를 볼 종목이 없습니다(보유·관심 종목에 없거나 시세를 받지 못했습니다).")
    for it in items:
        name = it["name"] or it["symbol"]
        if it["status"] == "no_data" or it.get("peak_date") is None:
            bullets.append(f"{name}: 시세가 모자라 고점 대비를 계산하지 못했습니다.")
            continue
        if it["status"] == "near_peak":
            bullets.append(f"{name}: 최근 1년 고점({it['peak_date']}) 근처입니다 — 고점 대비 {_from_high_text(it['drawdown_pct'])}.")
            continue
        head = (f"{name}: 최근 고점 {it['peak_date']}({it['peak_close']:,.2f}) 대비 {_from_high_text(it['drawdown_pct'])} "
                f"(고점 이후 {it['since_days']}거래일, 그 사이 최저 {_from_high_text(it['low_drawdown_pct'])})")
        bullets.append(head)
        if it.get("ath_date") and it["ath_date"] != it["peak_date"]:
            bullets.append(f"   상장 이후 전 고점({it['ath_date']}) 대비로는 {_from_high_text(it['ath_drawdown_pct'])}")
        days = it.get("drop_days") or []
        causes = it.get("causes") or []
        risk_days = sum(1 for d in days if d["risk_down"])
        drivers = sorted(it["drivers"], key=lambda d: abs(d.get("contribution_pct") or 0), reverse=True) if it["status"] == "ok" else []
        shown = [d for d in drivers if abs(d.get("contribution_pct") or 0) >= 0.3] or drivers[:1]
        reg_down = [d for d in shown if d["contribution_pct"] < 0]
        reg_up = [d for d in shown if d["contribution_pct"] >= 0]
        rest = (it.get("residual_pct") or 0) + sum(d["contribution_pct"] for d in drivers if d not in shown) if drivers else None
        press_down = [m for m in it.get("pressure") or [] if m["direction"] == "down"]
        reg_labels = {d["label"] for d in reg_down}
        extra_down = [m for m in press_down if m["label"] not in reg_labels][:3]
        wording = "시장 전체 흐름(지수·업종·귀금속 포함)" if it.get("basis") == "market" else "금리·달러 같은 매크로 요인"
        names = [f"{d['label']} {d['phrase']}" for d in reg_down] + [f"{m['label']} {m['phrase']}" for m in extra_down]
        summary = ("통계로는 " + ", ".join(names) + " 쪽이 하락 압력으로 보입니다" if names else "금리·달러 같은 매크로 요인에서는 뚜렷한 하락 압력을 찾지 못했습니다")
        if rest is not None and rest < 0 and abs(rest) > sum(abs(d["contribution_pct"]) for d in reg_down):
            summary += f". 다만 하락폭의 더 큰 부분({rest:+.1f}%p)은 요인으로 설명되지 않아 큰 하락일 보도를 같이 봅니다"
        elif it["status"] != "ok":
            summary += ". 회귀 모형으로는 설명이 안 되거나 검증에서 믿기 어려워(" + ("관계가 바뀌었을 수 있음" if it.get("reason") == "unstable" else f"설명력 R² {_r2_text(it)}") + ") 보도를 같이 봅니다"
        bullets.append("   요약: " + summary + ".")
        lines = []
        ser = it.get("series") or {}
        if len(ser.get("dates") or []) >= 3:   # 고점 이후 가격선: 고점선, 큰 하락일 점(날짜·낙폭·그날 가장 많이 겹친 보도 주제)
            dates = ser["dates"]
            markers = []
            for d in days:
                if d["date"] in dates:
                    theme = next((c["theme"] for c in causes if d["date"] in c["days"]), "")
                    markers.append({"index": dates.index(d["date"]), "text": f"{d['date'][5:]} {d['change_pct']:+.1f}%" + (f" {theme}" if theme else ""), "color": "down"})
            charts.append(chart_mod.make("line", f"{name} 고점 이후 가격", [d[2:] for d in dates], [{"label": name, "color": "s1", "data": ser["closes"]}], "",
                                         subtitle=f"고점 {it['peak_date']} 대비 {_from_high_text(it['drawdown_pct'])} · 빨간 점=크게 빠진 날과 그날 반복된 보도 주제",
                                         markers=markers, hlines=[{"value": it["peak_close"], "text": f"고점 {it['peak_close']:,.2f}", "color": "warn"}]))
        if drivers:    # '왜 빠졌나' 막대: 요인별 기여(%p)와 설명 안 된 부분
            labels = [f"{d['label']} {d['phrase']}" for d in shown] + ["설명 안 된 부분"]
            values = [d["contribution_pct"] for d in shown] + [rest]
            charts.append(chart_mod.make("hbar", f"{name} 하락의 요인별 기여 (통계 추정, %p)", labels,
                                         [{"label": "기여", "colors": chart_mod.signed_colors(values[:-1]) + ["neutral"], "data": values, "text": [f"{v:+.1f}%p" for v in values]}], "%p",
                                         subtitle=f"설명력 R² {_r2_text(it)}" + (" · 시장 전체 흐름 기준" if it.get("basis") == "market" else ""),
                                         caption="같이 움직인 정도의 통계 추정이며 원인의 증명이 아닙니다", height=max(160, len(labels) * 44 + 50)))
        pressure = (it.get("pressure") or [])[:3]
        if pressure:
            lines.append("② 평소 관계: " + ", ".join(f"{m['label']} {_factor_text(m['change'], m['unit'], m['phrase'])}(상관 {m['corr']:+.2f}) → " + ("하락 쪽" if m["direction"] == "down" else "받쳐 줌") for m in pressure))
        if causes:
            lines.append(f"③ 보도 주제(큰 하락일 {len(days)}일 중): " + ", ".join(f"{c['theme']} {len(c['days'])}일" for c in causes))
        if days and risk_days * 2 >= len(days):
            lines.append(f"④ 큰 하락일 {len(days)}일 중 {risk_days}일은 주식·가상자산도 동반 급락 — 시장 전체가 팔릴 때 같이 팔린 패턴과 일치")
        if lines:
            bullets.append("   원인 후보 (확정 아님):")
            bullets.extend("   " + line for line in lines)
        if days:
            rows = []
            for d in days:
                movers = [f"{m['label'].split('(')[0]} {_factor_text(m['change'], m['unit'], m['phrase'])}" for m in d["movers"]]
                news = ({"links": [{"text": h["title"], "href": h["url"], "meta": " · ".join(x for x in (h.get("source"), h.get("published")) if x)} for h in d["headlines"]]}
                        if d["headlines"] else "그날 앞뒤 보도를 찾지 못했습니다")
                rows.append([d["date"], f"{d['change_pct']:+.1f}%", {"lines": movers} if movers else "눈에 띄는 요인 없음", news])
            tables.append({"title": f"{name} 고점 이후 크게 빠진 날 — 그날 시장과 보도 (참고용)",
                           "columns": ["날짜", "종목", "같은 날 크게 움직인 시장", "그날 앞뒤 보도"], "align": ["l", "r", "l", "l"],
                           "wrap": [False, False, True, True], "rows": rows, "footer": []})
    if data.get("omitted"):
        notes.append(f"그 밖의 {data['omitted']}개 종목은 보지 않았습니다.")
    limitations.extend(data.get("limitations", []))


def compose_get_drawdown_analysis(drawdown_result_data, bullets, tables, notes, limitations, charts, results):
    """get_drawdown_analysis 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_drawdown(drawdown_result_data, bullets, tables, notes, limitations, charts)


FORMATTERS = {"get_drawdown_analysis": drawdown_result}
COMPOSERS = {"get_drawdown_analysis": compose_get_drawdown_analysis}
