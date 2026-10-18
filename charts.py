"""Agent 답변의 그래프 명세 만들기·검증. 화면(agent_charts.js)이 Chart.js로 그리며, 명세에는 숫자와 짧은 글만 담는다(HTML·링크·계좌 정보 없음).

종류: bar(세로 막대, 여러 계열 묶음 가능), hbar(가로 막대), line(선 + 점 설명 markers + 가로선 hlines).
색 토큰: up(오름·초록) down(내림·빨강) neutral accent warn s1~s4. 점마다 색은 datasets[].colors, 보일 글은 datasets[].text로 정한다.
순수 함수이며 다른 기능 모듈을 부르지 않는다.
"""
from __future__ import annotations

import math

TYPES = ("bar", "hbar", "line")
COLORS = ("up", "down", "neutral", "accent", "warn", "s1", "s2", "s3", "s4")
MAX_POINTS = 400
MAX_DATASETS = 4
MAX_MARKERS = 12
TEXT_MAX = 60
UNITS = ("", "%", "%p", "bp", "원", "개")


def _text(value, limit: int = TEXT_MAX) -> str:
    return str(value if value is not None else "")[:limit]


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _color(token, default: str = "neutral") -> str:
    return token if token in COLORS else default


def make(kind: str, title: str, labels: list, datasets: list[dict], unit: str = "", subtitle: str = "", caption: str = "", markers: list | None = None,
         hlines: list | None = None, height: int | None = None, decimals: int | None = None, signed: bool | None = None) -> dict | None:
    """명세를 만들고 검증한다. 점이 없거나 종류가 틀리면 None(그래프를 그리지 않는다)."""
    if kind not in TYPES or not labels:
        return None
    labels = [_text(x, 30) for x in labels[:MAX_POINTS]]
    out_sets = []
    for d in datasets[:MAX_DATASETS]:
        data = [_number(v) for v in (d.get("data") or [])[:len(labels)]]
        data += [None] * (len(labels) - len(data))
        if all(v is None for v in data):
            continue
        item = {"label": _text(d.get("label"), 30), "data": data}
        if d.get("colors"):
            item["colors"] = [_color(c) for c in list(d["colors"])[:len(labels)]]
        else:
            item["color"] = _color(d.get("color"), "s1")
        if d.get("text"):
            item["text"] = [_text(t, 40) for t in list(d["text"])[:len(labels)]]
        out_sets.append(item)
    if not out_sets:
        return None
    spec = {"type": kind, "title": _text(title, 80), "unit": unit if unit in UNITS else "", "labels": labels, "datasets": out_sets}
    if subtitle:
        spec["subtitle"] = _text(subtitle, 120)
    if caption:
        spec["caption"] = _text(caption, 200)
    if height:
        spec["height"] = max(120, min(int(height), 560))
    if isinstance(signed, bool):
        spec["signed"] = signed                # 가격처럼 + 기호를 붙이면 안 되는 그래프는 False
    if isinstance(decimals, int) and not isinstance(decimals, bool) and 0 <= decimals <= 3:
        spec["decimals"] = decimals            # 상관(-1~1)처럼 소수점 자리가 중요한 그래프
    if kind == "line":
        spec["markers"] = [{"index": int(m["index"]), "text": _text(m.get("text"), 40), "color": _color(m.get("color"), "down")}
                           for m in (markers or [])[:MAX_MARKERS] if isinstance(m.get("index"), int) and 0 <= m["index"] < len(labels)]
        spec["hlines"] = [{"value": v, "text": _text(h.get("text"), 30), "color": _color(h.get("color"))}
                          for h in (hlines or [])[:4] for v in [_number(h.get("value"))] if v is not None]
    return spec


def signed_colors(values: list) -> list[str]:
    """양수는 up, 음수는 down, 없거나 0은 neutral."""
    return ["neutral" if v is None or v == 0 else "up" if v > 0 else "down" for v in values]
