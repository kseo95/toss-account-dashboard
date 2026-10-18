"""ETF 이번 주 등락을 상위 편입 종목의 기여(비중 × 종목 등락)로 나눈다. 순수 계산이며 외부 호출은 하지 않는다.

- 기간은 ETF의 최근 5거래일(시작일·종료일)로 정하고, 편입 종목은 같은 날짜 기준 종가(그날 이전 가장 가까운 값)로 등락을 구한다.
  거래일이 다른 해외 종목이 섞여도 같은 기간으로 맞추기 위해서다.
- 기여(%p) = 비중 × 종목 등락(%). 합산 분해일 뿐 인과의 증명이 아니다.
- 상위 편입 종목은 전체의 일부(보통 50~60%)라서 나머지는 '그 밖'이다. 그 밖의 비중과, 그 밖이 이만큼 움직였다면 맞아떨어지는 암시 수익률을
  같이 알려 준다. 상위 종목 평균과 암시 수익률이 크게 다르면(공시 비중이 오래됐거나 나머지에 큰 움직임이 있었을 수 있음) 플래그를 단다.
"""
from __future__ import annotations

from bisect import bisect_right

TRADING_WEEK = 5
STALE_DAYS = 4            # 종목 시세의 마지막 날짜가 ETF 종료일보다 이만큼 이상 앞서면 오래된 시세로 보고 제외
LOW_COVERAGE = 0.30       # 시세를 받은 편입 종목 비중이 이보다 작으면 설명 범위가 좁다고 알린다
DIVERGENCE_PP = 5.0       # 상위 종목 평균과 나머지 암시 수익률의 차이가 이보다 크면 알린다
MIN_REST_WEIGHT = 0.05    # 나머지 비중이 이보다 작으면 암시 수익률은 의미가 없어 계산하지 않는다


def _value_on_or_before(dates: list, series: dict, target):
    """target 이전(포함) 가장 가까운 날짜의 종가와 그 날짜. 없으면 (None, None)."""
    i = bisect_right(dates, target)
    if i == 0:
        return None, None
    return series[dates[i - 1]], dates[i - 1]


def window_return(series: dict, start, end) -> float | None:
    """start → end 등락(%), 각 날짜는 그날 이전 가장 가까운 종가를 쓴다. 시세가 오래됐거나 모자라면 None."""
    dates = sorted(series)
    a, _ = _value_on_or_before(dates, series, start)
    b, b_date = _value_on_or_before(dates, series, end)
    if not a or not b or a <= 0 or b <= 0 or (end - b_date).days >= STALE_DAYS:
        return None
    return (b / a - 1) * 100


def breakdown(etf_series: dict, holdings: list[dict], constituent_series: dict[str, dict], days: int = TRADING_WEEK) -> dict:
    """한 ETF의 구성 종목 기여. status: ok | no_data(ETF 시세 부족 또는 시세를 받은 편입 종목 없음).
    holdings: [{symbol, name, weight(0~1)}], constituent_series: {티커: {날짜: 종가}}."""
    dates = sorted(etf_series)
    if len(dates) <= days:
        return {"status": "no_data"}
    start, end = dates[-days - 1], dates[-1]
    etf_change = window_return(etf_series, start, end)
    if etf_change is None:
        return {"status": "no_data"}
    rows, missing = [], []
    for h in holdings:
        series = constituent_series.get(h["symbol"])
        change = window_return(series, start, end) if series else None
        if change is None:
            missing.append(h)
            rows.append({"symbol": h["symbol"], "name": h["name"], "weight_pct": h["weight"] * 100, "change_pct": None, "contribution_pct": None})
            continue
        rows.append({"symbol": h["symbol"], "name": h["name"], "weight_pct": h["weight"] * 100, "change_pct": change,
                     "contribution_pct": h["weight"] * change})
    priced = sum(h["weight"] for h in holdings) - sum(h["weight"] for h in missing)
    if priced <= 0:
        return {"status": "no_data", "etf_change_pct": etf_change, "window": [start.isoformat(), end.isoformat()],
                "rows": rows, "priced_weight_pct": 0.0}
    explained = sum(r["contribution_pct"] for r in rows if r["contribution_pct"] is not None)
    residual = etf_change - explained
    covered_return = explained / priced
    rest_weight = max(0.0, 1 - priced)
    rest_implied = residual / rest_weight if rest_weight >= MIN_REST_WEIGHT else None
    flags = []
    if priced < LOW_COVERAGE:
        flags.append("low_coverage")
    if rest_implied is not None and abs(rest_implied - covered_return) > DIVERGENCE_PP:
        flags.append("diverged")
    if missing:
        flags.append("missing_prices")
    rows.sort(key=lambda r: (r["contribution_pct"] is None, -abs(r["contribution_pct"] or 0)))
    return {"status": "ok", "window": [start.isoformat(), end.isoformat()], "etf_change_pct": etf_change, "rows": rows,
            "priced_weight_pct": priced * 100, "rest_weight_pct": rest_weight * 100, "explained_pct": explained,
            "residual_pct": residual, "covered_return_pct": covered_return, "rest_implied_pct": rest_implied,
            "missing": [h["symbol"] for h in missing], "flags": flags}
