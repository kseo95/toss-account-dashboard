"""밸류에이션(PER·PBR 등) 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from .common import _number, _round


DEFINITIONS = [
    {
        "name": "get_valuation",
        "description": "보유·관심 종목의 PER(후행·선행)·PSR·PBR·EPS·BPS·ROE와 비싼가·싼가(고평가·저평가) 판단을 조회합니다. 개별주는 같은 업종 S&P500 종목 표본의 중앙값 대비 PER·PSR·PBR, 성장 대비(PEG), 수익성 대비(ROE로 정당화되는 PBR)의 신호를 모아 '고평가 쪽·저평가 쪽·비슷함·판단 보류'로 말하고, 주식형 ETF는 PER·PBR을 시장(S&P500 ETF) 대비로만 봅니다(EPS·BPS·ROE 없음, 금·채권 ETF는 해당 없음). 적자 종목은 PER을 계산하지 않고, 한국 종목은 야후 재무 데이터가 불완전해 지표만 보여 주고 판단하지 않습니다. 야후 비공식 데이터 기반의 상대 평가이며 적정 주가나 투자 조언이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다(최대 4개).",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: AAPL, 엔비디아, SPY). 비우면 보유·관심 종목(최대 4개)"}}, "additionalProperties": False},
    },
]


VALUATION_LABEL = {"expensive": "고평가 쪽 (비싸 보임)", "cheap": "저평가 쪽 (싸 보임)", "fair": "비슷하거나 혼조 (적정 범위)", "hold": "판단 보류", "na": "해당 없음", "benchmark": "비교 기준"}


VALUATION_REASONS = ("", "korean", "no_equity_multiples", "no_benchmark", "benchmark", "other")


def valuation_result(data: dict) -> dict:
    """밸류에이션 결과에서 숫자와 짧은 글만 남긴다(종목 4개, 신호 6개까지)."""
    items = []
    for it in (data.get("items") or [])[:4]:
        row = {"symbol": str(it.get("symbol") or "")[:15], "name": str(it.get("name") or "")[:40], "held": bool(it.get("held")), "extra": bool(it.get("extra")),
               "status": it.get("status") if it.get("status") in ("ok", "error", "unavailable") else "error"}
        if row["status"] == "ok":
            m = it.get("metrics") or {}
            a = it.get("assessment") or {}
            v = a.get("verdict") or {}
            row.update({
                "kind": it.get("kind") if it.get("kind") in ("stock", "etf", "other") else "other", "peer_n": int(_number(it.get("peer_n")) or 0),
                "metrics": {k: _round(m.get(k), 2) for k in ("per", "forward_per", "psr", "pbr", "eps", "forward_eps", "bps", "roe", "peg", "price")},
                "currency": str(m.get("currency") or "")[:5], "category": str(m.get("category") or "")[:40], "sector": str(m.get("sector") or "")[:30],
                "flags": [f for f in (m.get("flags") or []) if f in ("loss", "per_outlier", "roe_outlier")],
                "verdict": {"label": v.get("label") if v.get("label") in VALUATION_LABEL else "hold", "expensive": int(_number(v.get("expensive")) or 0),
                            "cheap": int(_number(v.get("cheap")) or 0), "signals": int(_number(v.get("signals")) or 0)},
                "reason": a.get("reason") if a.get("reason") in VALUATION_REASONS else "",
                "signals": [{"key": str(x.get("key") or "")[:12], "label": str(x.get("label") or "")[:20], "basis": x.get("basis") if x.get("basis") in ("sector", "growth", "profitability", "market") else "sector",
                             "value": _round(x.get("value"), 2), "ref": _round(x.get("ref"), 2), "ref_label": str(x.get("ref_label") or "")[:60],
                             "premium_pct": _round(x.get("premium_pct"), 1), "verdict": x.get("verdict") if x.get("verdict") in ("expensive", "cheap", "fair") else "fair"}
                            for x in (a.get("signals") or [])[:6]],
                "notes": [str(n)[:140] for n in (a.get("notes") or [])[:4]]})
            im = a.get("implied") if isinstance(a.get("implied"), dict) else None
            row["implied"] = ({"price": _round(im.get("price"), 2), "currency": str(im.get("currency") or "")[:5], "reliable_n": int(_number(im.get("reliable_n")) or 0),
                               "low": _round(im.get("low"), 2), "high": _round(im.get("high"), 2), "mid": _round(im.get("mid"), 2), "wide": bool(im.get("wide")),
                               "methods": [{"key": str(x.get("key") or "")[:12], "label": str(x.get("label") or "")[:24], "price": _round(x.get("price"), 2), "diff_pct": _round(x.get("diff_pct"), 1),
                                            "reliable": bool(x.get("reliable")), "reason": str(x.get("reason") or "")[:80]} for x in (im.get("methods") or [])[:6]]} if im else None)
        items.append(row)
    return {
        "items": items, "omitted": int(_number(data.get("omitted")) or 0), "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "assumptions": {"band_pct": 30, "cost_of_equity_pct": 9, "long_growth_pct": 3, "peg_cheap": 1, "peg_expensive": 2},
        "limitations": [
            "야후 파이낸스(비공식) 지표라 지연·누락·오류가 있을 수 있고, 후행 PER은 지난 12개월 이익 기준입니다. 적자 종목은 PER을 계산하지 않습니다.",
            "'비싸 보임·싸 보임'은 정해 둔 기준(같은 업종 S&P500 표본 중앙값 대비 ±30%, PEG 1·2, ROE로 정당화되는 PBR = (ROE − 3%) ÷ (9% − 3%))에 대는 상대 평가이며 적정 주가가 아닙니다. 신호가 2개 미만이면 판단하지 않습니다.",
            "성장주는 PER·PSR이 높은 것이 평범하고, 경기 민감 업종은 이익이 정점일 때 PER이 낮아 보입니다. 자사주 매입이 많으면 자기자본이 작아 PBR·ROE가 왜곡됩니다. 업종 표본은 S&P500 일부(최대 25개)입니다.",
            "ETF의 PER·PBR은 편입 종목 지표의 평균이라 개별 종목과 의미가 다르고, EPS·BPS·ROE는 ETF에 없습니다. 투자 조언이 아닙니다.",
            "'섹터 배수 기준 추정 가격'은 같은 업종 S&P500 표본의 중앙값 배수를 이 회사의 EPS·선행 EPS·주당매출·BPS에 그대로 곱한 값입니다. 이 회사가 업종 평균 배수를 받아야 한다는 가정이라 이익률이 높거나 성장이 빠른 회사는 낮게 나오는 것이 자연스럽고, 방법마다 크게 다를 수 있어 하나의 적정 주가가 아니라 범위로만 봅니다. 믿을 수 없는 방법(적자·BPS 왜곡)은 범위에서 뺍니다.",
        ],
    }


VALUATION_BAR_CAP = 300.0


VALUATION_BASIS_TEXT = {"sector": "섹터", "market": "시장", "growth": "성장", "profitability": "수익성"}


def _metric_text(value, digits: int = 1, suffix: str = "") -> str:
    return "-" if value is None else f"{value:,.{digits}f}{suffix}"


def _compose_valuation(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """종목마다 한 줄 판정 + '기준 대비 얼마나 비싼가' 막대 그래프 + 지표 표(PER·PSR·PBR·EPS·BPS·ROE). 상대 평가이며 적정 주가가 아니다."""
    items = data.get("items") or []
    if not items:
        bullets.append("밸류에이션을 볼 보유 종목이나 관심종목이 없습니다.")
        return
    rows = []
    for it in items:
        name = it["name"] or it["symbol"]
        if it["status"] != "ok":
            notes.append(f"{name}: 야후에서 지표를 받지 못했습니다" + (" (yfinance 미설치)" if it["status"] == "unavailable" else "") + ".")
            continue
        v, m = it["verdict"], it["metrics"]
        kind = {"stock": "개별주", "etf": "ETF", "other": "기타"}[it["kind"]]
        text = VALUATION_LABEL[v["label"]]
        line = f"{name}({kind}): {text}"
        if v["signals"]:
            line += f" — 신호 {v['signals']}개 중 비싼 쪽 {v['expensive']}·싼 쪽 {v['cheap']}"
        bullets.append(line)
        for n in it["notes"]:
            notes.append(f"{name}: {n}.")
        if "loss" in it["flags"] and "적자" not in " ".join(it["notes"]):
            notes.append(f"{name}: 적자(EPS {_metric_text(m['eps'], 2)})라 PER을 계산하지 않았습니다.")
        shown = [x for x in it["signals"]]
        if shown:
            def bar(x):
                if x["premium_pct"] is not None:
                    return x["premium_pct"], f"기준 {x['ref']:.1f} ({x['premium_pct']:+.0f}%)"
                return (x["value"] / 1.5 - 1) * 100, f"PEG {x['value']:.1f} (1↓싸다·2↑비싸다)"
            values = [max(-100.0, min(VALUATION_BAR_CAP, bar(x)[0])) for x in shown]      # 너무 긴 막대가 나머지를 눌러 버리지 않게 자른다(글은 실제 값)
            capped = any(bar(x)[0] > VALUATION_BAR_CAP for x in shown)
            colors = ["warn" if x["verdict"] == "expensive" else "accent" if x["verdict"] == "cheap" else "neutral" for x in shown]
            charts.append(chart_mod.make("hbar", f"{name} — 기준 대비 얼마나 비싼가 (%)", [f"{x['label']} {x['value']:.1f} ({VALUATION_BASIS_TEXT[x['basis']]})" for x in shown],
                                         [{"label": "기준 대비", "colors": colors, "data": values, "text": [bar(x)[1] for x in shown]}], "%",
                                         subtitle=f"{text}" + (f" · 섹터 표본 {it['peer_n']}개" if it["peer_n"] else ""),
                                         caption="주황=비싼 쪽 · 파랑=싼 쪽 · 회색=비슷(±30% 안)" + (f" · 막대는 +{VALUATION_BAR_CAP:.0f}%까지만 그림(글은 실제 값)" if capped else "") + ". 상대 평가이며 적정 주가·투자 조언이 아닙니다",
                                         height=len(shown) * 40 + 70))
        im = it.get("implied")
        if im and im["methods"]:
            sym = {"USD": "$", "KRW": "₩"}.get(im["currency"], "")
            fmt = lambda v: f"{sym}{v:,.0f}" if sym else f"{v:,.0f} {im['currency']}".strip()
            methods = sorted(im["methods"], key=lambda x: -x["price"])
            labels = ["현재가"] + [x["label"] + ("" if x["reliable"] else " (제외)") for x in methods]
            values = [im["price"]] + [x["price"] for x in methods]
            colors = ["accent"] + [("neutral" if not x["reliable"] else "up" if x["diff_pct"] > 0 else "down") for x in methods]
            texts = [fmt(im["price"])] + [f"{fmt(x['price'])} ({x['diff_pct']:+.0f}%)" for x in methods]
            excluded = list(dict.fromkeys(x["reason"] for x in methods if not x["reliable"] and x["reason"]))
            if im["low"] is not None:
                bullets.append(f"   섹터 배수 기준 추정 범위 {fmt(im['low'])}~{fmt(im['high'])} (중앙 {fmt(im['mid'])}, 방법 {im['reliable_n']}개) — 현재가 {fmt(im['price'])}"
                               + (" · 방법 간 차이가 3배를 넘어 참고하기 어렵습니다" if im.get("wide") else ""))
                subtitle = f"추정 범위 {fmt(im['low'])}~{fmt(im['high'])} · 방법 {im['reliable_n']}개" + (" · 방법 간 차이가 커 참고하기 어려움" if im.get("wide") else "")
            else:
                subtitle = "믿을 만한 방법이 2개 미만이라 범위를 내지 않습니다"
            charts.append(chart_mod.make("hbar", f"{name} — 섹터 배수 기준 추정 가격 (현재가 대비 %)", labels,
                                         [{"label": "추정 가격", "colors": colors, "data": values, "text": texts}], "", subtitle=subtitle,
                                         caption="초록=현재가보다 높게 추정 · 빨강=낮게 추정 · 회색=신뢰 낮아 범위에서 제외. 섹터 중앙값 배수를 이 회사에 그대로 적용한 추정이며 적정 주가·투자 조언이 아닙니다"
                                                 + (" · 제외 이유: " + "; ".join(excluded) if excluded else ""), height=len(labels) * 36 + 70, decimals=0, signed=False))
        per = _metric_text(m["per"]) + (f" / 선행 {_metric_text(m['forward_per'])}" if m["forward_per"] is not None else "")
        cur = f" {it['currency']}" if it["currency"] and it["currency"] != "USD" else ""
        rows.append([f"{name} ({it['symbol']})", kind, per, _metric_text(m["psr"]), _metric_text(m["pbr"]), _metric_text(m["eps"], 2) + (cur if m["eps"] is not None else ""),
                     _metric_text(m["bps"], 2) + (cur if m["bps"] is not None else ""), _metric_text(m["roe"], 1, "%"), text])
    if rows:
        tables.append({"title": "밸류에이션 지표 (야후 파이낸스 · PER 후행 / 선행)", "columns": ["종목", "구분", "PER", "PSR", "PBR", "EPS", "BPS", "ROE", "판정"],
                       "align": ["l", "l", "r", "r", "r", "r", "r", "r", "l"], "wrap": [False, False, False, False, False, False, False, False, True], "rows": rows, "footer": []})
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    if data.get("omitted"):
        notes.append(f"그 밖의 {data['omitted']}개 종목은 넣지 않았습니다(최대 4개).")
    limitations.extend(data.get("limitations", []))


def compose_get_valuation(valuation_data, bullets, tables, notes, limitations, charts, results):
    """get_valuation 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_valuation(valuation_data, bullets, tables, notes, limitations, charts)


FORMATTERS = {"get_valuation": valuation_result}
COMPOSERS = {"get_valuation": compose_get_valuation}
