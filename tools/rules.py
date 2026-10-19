"""내 원칙(목표 비중·전략) 대조 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from .common import _number, _round


DEFINITIONS = [
    {
        "name": "get_my_rules",
        "description": "내가 직접 정해 둔 기준 — 리밸런싱 카테고리별 목표 비중, 분할매수 전략(주봉 이동평균 단계별 진입 비율) — 과 현재 보유를 대조한 숫자를 조회합니다. '내 원칙', '내가 정한 기준', '목표 비중대로인지', '원칙을 어긴 종목' 같은 질문에 씁니다: 카테고리별 목표 대비 현재 비중(목표 대상 안 기준과 전체 기준), 종목별 전략 단계·진입 비율·주봉 이동평균 대비 위치, 카테고리에 안 넣은 종목. 어느 기준에 해당하는지만 알려 주고 잘잘못이나 심리(포모 등)는 판단하지 않으며, 현재 스냅샷이라 매수 시점·순서는 모릅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def my_rules_result(data: dict) -> dict:
    """내 기준(목표 비중·전략)과 보유 대조에서 숫자와 짧은 이름만 남긴다(카테고리 12개·종목 15개까지)."""
    targets = []
    for t in (data.get("targets") or [])[:12]:
        targets.append({"label": str(t.get("label") or "")[:20], "symbols": [str(x)[:12] for x in (t.get("symbols") or [])[:8]],
                        "held_symbols": [str(x)[:12] for x in (t.get("held_symbols") or [])[:8]], "target_pct": _round(t.get("target_pct"), 1),
                        "actual_pct": _round(t.get("actual_pct"), 1), "actual_total_pct": _round(t.get("actual_total_pct"), 1), "diff_pct": _round(t.get("diff_pct"), 1),
                        "value_krw": _round(t.get("value_krw"), 0), "status": t.get("status") if t.get("status") in ("over", "under", "near") else "near"})
    rows = []
    for r in (data.get("holdings") or [])[:15]:
        st = r.get("strategy") if isinstance(r.get("strategy"), dict) else {}
        strategy = {"status": st.get("status") if st.get("status") in ("ok", "none", "excluded", "unavailable") else "none"}
        if strategy["status"] == "ok":
            strategy.update({"entry_pct": _round(st.get("entry_pct"), 1), "stage": int(_number(st.get("stage")) or 0), "stage_total": int(_number(st.get("stage_total")) or 0),
                             "recovered": bool(st.get("recovered")),
                             "ma": [{"period": int(_number(m.get("period")) or 0), "unit": "일" if m.get("unit") == "일" else "주", "dist_pct": _round(m.get("dist_pct"), 1)}
                                    for m in (st.get("ma") or [])[:6] if _number(m.get("dist_pct")) is not None]})
        rows.append({"symbol": str(r.get("symbol") or "")[:12], "name": str(r.get("name") or "")[:40], "weight_pct": _round(r.get("weight_pct"), 1), "return_pct": _round(r.get("return_pct"), 1),
                     "category": (str(r["category"])[:20] if r.get("category") else None), "strategy": strategy})
    return {
        "targets": targets, "holdings": rows, "unclassified": [str(x)[:12] for x in (data.get("unclassified") or [])[:15]],
        "strategy_steps": [str(x)[:40] for x in (data.get("strategy_steps") or [])[:8]], "recovery_pct": _round(data.get("recovery_pct"), 1), "band_pct": _round(data.get("band_pct"), 1),
        "limitations": [
            "현재 스냅샷(보유 비중과 지금의 이동평균 위치)만 봅니다. 언제 얼마에 샀는지, 전략을 만들기 전에 산 것인지는 모릅니다.",
            "'목표 대상 안 비중'은 리밸런싱 카테고리에 넣은 보유종목만으로 계산한 비중이고, 현금·카테고리에 안 넣은 종목은 빠집니다(전체 기준 비중은 따로 표시).",
            "어느 기준에 해당하는지만 알려 드립니다. 포모 같은 심리나 잘잘못은 숫자로 증명되지 않아 판단하지 않습니다.",
        ],
    }


RULES_HOT_ABOVE_MA_PCT = 20.0     # 주봉 장기 이동평균(200·240주)보다 이만큼 이상 위면 '올라 있는 구간'


RULES_BIG_WEIGHT_PCT = 5.0


RULES_LONG_PERIODS = (200, 240)


def _long_ma_dist(row: dict) -> float | None:
    """종목의 주봉 200·240선 대비 위치 중 더 큰 쪽(%)."""
    dists = [m["dist_pct"] for m in row["strategy"].get("ma", []) if m["period"] in RULES_LONG_PERIODS and m["unit"] == "주"]
    return max(dists) if dists else None


def _compose_rules(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """내가 정한 기준(목표 비중·분할매수 전략)과 보유의 대조: 해당하는 수치를 짧게 + 목표 대비 비중 그래프 + 이동평균 대비 위치 그래프. 판단·심리는 말하지 않는다."""
    targets, holdings = data.get("targets") or [], data.get("holdings") or []
    if not targets and not holdings:
        bullets.append("대조할 기준이나 보유 종목이 없습니다(리밸런싱 목표 카테고리와 분할매수 전략을 먼저 설정해 주세요).")
        return
    bullets.append("정해 둔 기준(목표 비중·분할매수 전략)과 현재 보유를 대조했습니다. 어느 기준에 해당하는지만 알려 드리고 잘잘못이나 심리는 판단하지 않습니다.")
    off = sorted((t for t in targets if t["status"] != "near" and t["target_pct"] is not None), key=lambda t: -abs(t["diff_pct"]))[:4]
    for t in off:
        held = "예수금·현금 취급 종목" if t["label"] == "현금" and not t["symbols"] else ", ".join(t["held_symbols"]) or "보유 없음"
        bullets.append(f"{t['label']}({held}): 목표 {t['target_pct']:.0f}% → 현재 {t['actual_pct']:.1f}% (목표 대상 안 기준, 전체의 {t['actual_total_pct']:.1f}%) — {abs(t['diff_pct']):.1f}%p {'초과' if t['diff_pct'] > 0 else '부족'}")
    over_labels = {t["label"] for t in targets if t["status"] == "over"}
    for r in holdings:
        st = r["strategy"]
        long_dist = _long_ma_dist(r)
        if st["status"] == "ok" and long_dist is not None and long_dist >= RULES_HOT_ABOVE_MA_PCT and (r["weight_pct"] or 0) >= RULES_BIG_WEIGHT_PCT:
            bullets.append(f"{r['name']}: 분할매수 전략은 {st['stage']}/{st['stage_total']}단계(진입 {st['entry_pct']:g}%)인데 주봉 200·240선보다 {long_dist:+.1f}% 위에 있고 비중은 {r['weight_pct']:.1f}%"
                           + (f" — 목표를 넘은 '{r['category']}' 카테고리" if r["category"] in over_labels else ""))
        elif st["status"] == "unavailable" or (st["status"] == "ok" and not st["ma"]):      # 전략이 배정되지 않았거나 제외한 종목은 신호 없음으로 보지 않는다
            if (r["weight_pct"] or 0) >= RULES_BIG_WEIGHT_PCT:
                bullets.append(f"{r['name']}: 전략 신호가 없는데(주봉 데이터 부족 등) 비중이 {r['weight_pct']:.1f}%" + (f" — 목표를 넘은 '{r['category']}' 카테고리" if r["category"] in over_labels else ""))
    if data.get("unclassified"):
        bullets.append("목표 카테고리에 넣지 않은 보유종목: " + ", ".join(data["unclassified"][:8]))
    if targets:
        sets = [{"label": "목표 비중", "color": "s2", "data": [t["target_pct"] for t in targets], "text": [f"{t['target_pct']:.0f}%" for t in targets]},
                {"label": "현재(목표 대상 안)", "colors": ["warn" if t["status"] == "over" else "accent" if t["status"] == "under" else "up" for t in targets],
                 "data": [t["actual_pct"] for t in targets], "text": [f"{t['actual_pct']:.1f}%\n{t['diff_pct']:+.1f}%p" for t in targets]}]
        charts.append(chart_mod.make("bar", "카테고리별 목표 비중과 현재 비중", [t["label"] for t in targets], sets, "%", subtitle="주황=목표 초과 · 파랑=목표 미달 · 초록=목표 근처(±" + f"{data.get('band_pct') or 3:g}%p)",
                                     caption="현재 비중은 카테고리에 넣은 보유종목끼리의 비중입니다(현금·미분류 제외)", decimals=1, signed=False))
    ma_rows = [(r, d) for r in holdings for d in [next((m["dist_pct"] for m in r["strategy"].get("ma", []) if m["period"] == 200 and m["unit"] == "주"), None)] if d is not None]
    if ma_rows:
        values = [d for _, d in ma_rows]
        charts.append(chart_mod.make("hbar", "보유 종목의 주봉 200선 대비 위치 (분할매수 규칙은 이평선 아래로 이탈할 때 진입)", [r["name"] for r, _ in ma_rows],
                                     [{"label": "200주선 대비", "colors": ["warn" if v >= RULES_HOT_ABOVE_MA_PCT else "accent" if v < 0 else "neutral" for v in values], "data": values,
                                       "text": [f"{v:+.1f}% · {r['strategy']['stage']}/{r['strategy']['stage_total']}단계 · 비중 {(r['weight_pct'] or 0):.1f}%" for (r, v) in ma_rows]}], "%",
                                     caption="막대 글: 이평선 대비 위치 · 전략 단계 · 전체 대비 비중", height=len(ma_rows) * 40 + 70))
    if data.get("strategy_steps"):
        notes.append("분할매수 전략: " + " → ".join(data["strategy_steps"]) + (f", 회복 시 +{data['recovery_pct']:g}%p" if data.get("recovery_pct") else "") + ".")
    limitations.extend(data.get("limitations", []))


def compose_get_my_rules(rules_data, bullets, tables, notes, limitations, charts, results):
    """get_my_rules 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_rules(rules_data, bullets, tables, notes, limitations, charts)


FORMATTERS = {"get_my_rules": my_rules_result}
COMPOSERS = {"get_my_rules": compose_get_my_rules}
