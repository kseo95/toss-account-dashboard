"""보조지표(RSI·MACD·볼린저) 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from indicators import DEFAULTS as INDICATOR_DEFAULTS
from .common import _number, _origin_tag, _round


DEFINITIONS = [
    {
        "name": "get_indicators",
        "description": "종목의 기술적 보조지표를 조회합니다: RSI(기본 14일, 70 이상 과매수·30 이하 과매도), MACD(12·26·9, 시그널 위·아래와 최근 골든/데드크로스), 볼린저밴드(20일 ±2σ, %B 위치), 이동평균 교차(50일선·200일선, 마지막 골든/데드크로스 날짜). 종가로만 계산하고 현재 상태 이름과 최근 120거래일 시계열(그래프용)을 줍니다. 가격 흐름의 요약일 뿐 미래를 알려 주지 않으며(강한 추세에서는 과매수·과매도가 오래 이어짐) 매수·매도 신호가 아닙니다. 스토캐스틱·피보나치·캔들 패턴·지지/저항선은 없습니다. 보유·관심종목이 아니어도 focus에 이름·코드를 주면 됩니다(최대 4개).",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "focus": {"type": "string", "description": "볼 종목 이름·코드(예: 엔비디아, TSLA, GLDM). 비우면 보유·관심 종목(최대 4개)"},
            "indicators": {"type": "array", "items": {"type": "string", "enum": ["rsi", "macd", "bollinger", "ma_cross"]}, "description": "보고 싶은 지표. 비우면 전부"},
            "rsi_period": {"type": "integer", "minimum": 5, "maximum": 30, "description": "RSI 기간(기본 14)"},
            "overbought": {"type": "number", "minimum": 55, "maximum": 95, "description": "과매수 기준(기본 70)"},
            "oversold": {"type": "number", "minimum": 5, "maximum": 45, "description": "과매도 기준(기본 30)"}},
                         "additionalProperties": False},
    },
]


def indicators_result(data: dict) -> dict:
    """보조지표 결과에서 숫자와 상태 이름만 남긴다(종목 4개, 시계열 120점)."""
    def num(value, digits=2):
        return _round(value, digits)

    def series(values):
        return [None if v is None else _round(v, 4) for v in (values or [])[:130]]
    items = []
    for it in (data.get("items") or [])[:4]:
        row = {"symbol": str(it.get("symbol") or "")[:15], "name": str(it.get("name") or "")[:40], "held": bool(it.get("held")), "extra": bool(it.get("extra")),
               "status": "ok" if it.get("status") == "ok" else "no_data"}
        if row["status"] == "ok":
            r, m, b, c = it.get("rsi"), it.get("macd"), it.get("bollinger"), it.get("ma_cross")
            row.update({"as_of": str(it.get("as_of") or "")[:10], "close": num(it.get("close")), "params": {k: num(v) for k, v in (it.get("params") or {}).items()}})
            if r:
                row["rsi"] = {"value": num(r.get("value"), 1), "state": r.get("state") if r.get("state") in ("overbought", "oversold", "neutral") else "neutral",
                              "days_in_state": int(_number(r.get("days_in_state")) or 0), "above_50": bool(r.get("above_50"))}
            if m:
                cross = m.get("cross") or None
                row["macd"] = {"macd": num(m.get("macd"), 3), "signal": num(m.get("signal"), 3), "hist": num(m.get("hist"), 3), "above_signal": bool(m.get("above_signal")),
                               "above_zero": bool(m.get("above_zero")), "hist_rising": bool(m.get("hist_rising")),
                               "cross": {"type": cross.get("type") if cross.get("type") in ("golden", "dead") else "golden", "days_ago": int(_number(cross.get("days_ago")) or 0),
                                         "date": str(cross.get("date") or "")[:10], "recent": bool(cross.get("recent")), "in_window": bool(cross.get("in_window"))} if cross else None}
            if b:
                row["bollinger"] = {"pct_b": num(b.get("pct_b"), 2), "state": b.get("state") if b.get("state") in ("above_upper", "below_lower", "near_upper", "near_lower", "inside") else "inside",
                                    "upper": num(b.get("upper")), "mid": num(b.get("mid")), "lower": num(b.get("lower")), "bandwidth_pct": num(b.get("bandwidth_pct"), 1)}
            if c:
                lc = c.get("last_cross")
                row["ma_cross"] = {"fast": int(_number(c.get("fast")) or 50), "slow": int(_number(c.get("slow")) or 200), "fast_above": bool(c.get("fast_above")),
                                   "last_cross": {"type": lc.get("type") if lc.get("type") in ("golden", "dead") else "golden", "days_ago": int(_number(lc.get("days_ago")) or 0),
                                                  "date": str(lc.get("date") or "")[:10]} if lc else None}
            ser = it.get("series") or {}
            row["series"] = {"dates": [str(d)[:10] for d in (ser.get("dates") or [])[:130]], **{k: series(ser.get(k)) for k in ("close", "rsi", "macd", "signal", "hist", "upper", "mid", "lower")}}
        items.append(row)
    return {
        "items": items, "omitted": int(_number(data.get("omitted")) or 0), "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "indicators": [k for k in (data.get("indicators") or []) if k in ("rsi", "macd", "bollinger", "ma_cross")],
        "limitations": [
            "보조지표는 과거 가격을 숫자로 요약한 것이며 미래를 알려 주지 않습니다. 과매수·과매도는 '최근 상승·하락이 빨랐다'는 뜻일 뿐 반전을 뜻하지 않고, 강한 추세에서는 오래 이어집니다. 매수·매도 신호가 아닙니다.",
            "기준은 널리 쓰이는 값(RSI 14일 70/30, MACD 12·26·9, 볼린저 20일 ±2σ, 이동평균 50/200일)이며 종가만 사용합니다(고가·저가가 필요한 스토캐스틱 등은 없습니다). 기준을 바꾸면 결과가 달라집니다.",
            "이동평균 교차(골든·데드크로스)는 이미 오른·내린 뒤에 나타나는 후행 신호라 거짓 신호도 많습니다. 시세가 짧은 종목은 일부 지표를 계산하지 못합니다.",
        ],
    }


INDICATOR_RSI_TEXT = {"overbought": "과매수", "oversold": "과매도", "neutral": "중립"}


INDICATOR_BB_TEXT = {"above_upper": "상단 돌파", "near_upper": "상단 근접", "inside": "밴드 안", "near_lower": "하단 근접", "below_lower": "하단 이탈"}


INDICATOR_CROSS_TEXT = {"golden": "골든크로스", "dead": "데드크로스"}


INDICATOR_CHART_ITEMS = 2          # 그래프는 앞의 두 종목까지(종목마다 최대 3개)


def _compose_indicators(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """종목마다 한 줄 요약(RSI·MACD·볼린저·이동평균 교차 상태) + 볼린저 가격 그래프·RSI 그래프·MACD 그래프. 신호가 아니라 상태 요약이다."""
    items = data.get("items") or []
    wanted = data.get("indicators") or ["rsi", "macd", "bollinger", "ma_cross"]
    if not items:
        bullets.append("보조지표를 볼 종목이 없습니다(보유·관심종목이 없고 질문에서 종목을 찾지 못했습니다).")
    shown = 0
    for it in items:
        name = it["name"] or it["symbol"]
        if it["status"] != "ok":
            notes.append(f"{name}: 시세가 짧거나 받지 못해 보조지표를 계산하지 못했습니다.")
            continue
        parts = []
        r, m, b, c = it.get("rsi"), it.get("macd"), it.get("bollinger"), it.get("ma_cross")
        if r and "rsi" in wanted:
            parts.append(f"RSI {r['value']:.0f} ({INDICATOR_RSI_TEXT[r['state']]}" + (f", {r['days_in_state']}일째" if r["state"] != "neutral" and r["days_in_state"] > 1 else "") + ")")
        if m and "macd" in wanted:
            text = "MACD가 시그널 " + ("위" if m["above_signal"] else "아래")
            if m["cross"] and m["cross"]["recent"]:
                text += f" ({m['cross']['days_ago']}일 전 {INDICATOR_CROSS_TEXT[m['cross']['type']]})"
            parts.append(text)
        if b and "bollinger" in wanted:
            parts.append(f"볼린저 {INDICATOR_BB_TEXT[b['state']]} (%B {b['pct_b']:.2f})")
        if c and "ma_cross" in wanted:
            text = f"{c['fast']}일선이 {c['slow']}일선 " + ("위" if c["fast_above"] else "아래")
            if c["last_cross"]:
                text += f" (마지막 {INDICATOR_CROSS_TEXT[c['last_cross']['type']]} {c['last_cross']['date']})"
            parts.append(text)
        bullets.append(f"{name}{_origin_tag(it)}: " + (" · ".join(parts) if parts else "시세가 짧아 선택한 지표를 계산하지 못했습니다"))
        ser = it["series"]
        P = {**INDICATOR_DEFAULTS, **{k: v for k, v in it.get("params", {}).items() if v is not None}}
        if shown >= INDICATOR_CHART_ITEMS or len(ser["dates"]) < 3:
            continue
        shown += 1
        labels = [d[2:] for d in ser["dates"]]
        if b and "bollinger" in wanted:
            charts.append(chart_mod.make("line", f"{name} 볼린저밴드 ({P['bb_period']:.0f}일 ±{P['bb_k']:g}σ)", labels,
                                         [{"label": "종가", "color": "s1", "data": ser["close"]}, {"label": "상단", "color": "s2", "data": ser["upper"]},
                                          {"label": "중심", "color": "s3", "data": ser["mid"]}, {"label": "하단", "color": "s2", "data": ser["lower"]}], "",
                                         subtitle=f"%B {b['pct_b']:.2f} · {INDICATOR_BB_TEXT[b['state']]} (0=하단, 1=상단)", signed=False))
        if r and "rsi" in wanted:
            charts.append(chart_mod.make("line", f"{name} RSI ({P['rsi_period']:.0f}일)", labels, [{"label": "RSI", "color": "s1", "data": ser["rsi"]}], "",
                                         subtitle=f"{r['value']:.0f} · {INDICATOR_RSI_TEXT[r['state']]}",
                                         hlines=[{"value": P["overbought"], "text": f"과매수 {P['overbought']:.0f}", "color": "down"},
                                                 {"value": P["oversold"], "text": f"과매도 {P['oversold']:.0f}", "color": "up"}],
                                         height=220, signed=False, decimals=0, ymin=0, ymax=100,
                                         caption="과매수·과매도는 최근 상승·하락이 빨랐다는 뜻일 뿐 반전을 보장하지 않습니다"))
        if m and "macd" in wanted:
            markers = []
            if m["cross"] and m["cross"]["in_window"]:
                index = len(ser["dates"]) - 1 - m["cross"]["days_ago"]
                markers.append({"index": index, "text": f"{m['cross']['date'][5:]} {INDICATOR_CROSS_TEXT[m['cross']['type']]}", "color": "up" if m["cross"]["type"] == "golden" else "down"})
            charts.append(chart_mod.make("line", f"{name} MACD ({P['macd_fast']:.0f}·{P['macd_slow']:.0f}·{P['macd_signal']:.0f})", labels,
                                         [{"label": "MACD", "color": "s1", "data": ser["macd"]}, {"label": "시그널", "color": "s3", "data": ser["signal"]}], "",
                                         subtitle="MACD가 시그널 " + ("위" if m["above_signal"] else "아래"), markers=markers, hlines=[{"value": 0, "text": "0", "color": "neutral"}],
                                         height=220, decimals=2))
    if data.get("omitted"):
        notes.append(f"그 밖의 {data['omitted']}개 종목은 넣지 않았습니다(최대 4개).")
    limitations.extend(data.get("limitations", []))


def compose_get_indicators(indicators_data, bullets, tables, notes, limitations, charts, results):
    """get_indicators 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_indicators(indicators_data, bullets, tables, notes, limitations, charts)


FORMATTERS = {"get_indicators": indicators_result}
COMPOSERS = {"get_indicators": compose_get_indicators}
