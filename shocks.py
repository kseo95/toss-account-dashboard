"""과거 매크로 충격 때 이 종목은 어떻게 움직였나: "이번에 금리가 크게 올랐는데, 예전에 금리가 크게 올랐을 때 이 종목은?"

방법(통계적 사실이며 인과의 증명이 아니다. 순수 계산이고 시세는 호출하는 쪽이 넘긴다):
1. 각 요인(금리·달러·유가·VIX·환율·지수 등)의 최근 1개월(21거래일) 변화를 그 요인의 전체 역사와 비교해, 상위 10%(급등)·하위 10%(급락)를 '큰 변동'으로 본다.
   금리는 값의 차이(bp), 나머지는 로그 수익률이라 요인마다 역사 속 분포로 기준이 정해진다(고정 숫자 기준을 쓰지 않는다).
2. 지금 큰 변동 중인 요인마다, 역사 속에서 같은 방향의 큰 변동이 있었던 구간(21거래일 이상 떨어진 것은 별개의 사례로 센다)을 모아
   그 구간 동안 이 종목의 수익률 분포를 본다. 평소(아무 21거래일)의 분포와 부호 검정(이항분포)으로 비교해 우연인지 가려 낸다.
   독립 사례가 5회 미만이면 판단하지 않는다.
3. 각 사례의 날짜가 이름 붙은 거시 이슈(리먼·코로나·긴축 등, 대략의 기간)와 겹치면 그 이름을 붙여 "그때 그 일"로 알아볼 수 있게 한다.
4. 이름 붙은 이슈별로 이 종목이 그 기간 동안 얼마나 움직였는지(변동성으로 나눈 z 포함)를 모아 가장 크게 떨어지거나 오른 이슈를 보여 준다.
같은 시기에 여러 일이 겹치므로 어느 하나가 원인이라는 뜻이 아니다. 가격 기준(배당 제외)이다.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, timedelta

import macro

HORIZON = 21               # 거래일. '최근 1개월' 변화의 길이
BIG_UP, BIG_DOWN = 0.90, 0.10   # 요인 자신의 역사에서 상위·하위 10%
EPISODE_GAP = 21           # 거래일. 이보다 떨어진 큰 변동은 별개의 사례
MIN_EPISODES = 5
MIN_FACTOR_HISTORY = 500   # 거래일
SIGNIFICANT_P = 0.05      # 같은 종목에서 큰 변동 중인 요인 수만큼 동시에 검정하므로 p값에 그 수를 곱해(본페로니 보정) 이 기준과 비교한다
LABEL_MARGIN_DAYS = 45     # 사례 날짜가 이슈 기간 앞뒤 이 일수 안이면 그 이슈의 이름을 붙인다
TOP_EXAMPLES = 3
STALE_DAYS = 7

# 이름 붙은 거시 이슈(대략의 기간): (시작, 끝, 이름). 사실로 확인되는 사건만 넣었고 기간은 시장 반응이 두드러진 구간이라 정확한 날짜가 아니다.
MACRO_EVENTS = [
    (date(1987, 10, 14), date(1987, 10, 30), "블랙 먼데이(1987 주가 폭락)"),
    (date(1990, 8, 2), date(1990, 10, 31), "걸프전·유가 급등"),
    (date(1994, 2, 4), date(1994, 12, 31), "1994 채권 시장 급락(연준 급격한 금리 인상)"),
    (date(1997, 7, 2), date(1998, 1, 31), "아시아 외환위기"),
    (date(1998, 8, 17), date(1998, 10, 15), "러시아 디폴트·LTCM 사태"),
    (date(2000, 3, 10), date(2002, 10, 9), "닷컴 버블 붕괴"),
    (date(2001, 9, 11), date(2001, 9, 21), "9·11 테러"),
    (date(2007, 8, 9), date(2008, 9, 12), "서브프라임 위기 확산"),
    (date(2008, 9, 15), date(2009, 3, 9), "리먼 사태·글로벌 금융위기"),
    (date(2010, 5, 6), date(2010, 6, 30), "유럽 재정위기(그리스)·플래시 크래시"),
    (date(2011, 8, 5), date(2011, 10, 3), "미국 신용등급 강등·유럽 위기"),
    (date(2013, 5, 22), date(2013, 9, 5), "테이퍼 텐트럼(연준 양적완화 축소 우려)"),
    (date(2014, 6, 20), date(2015, 2, 13), "유가 급락(공급 확대)"),
    (date(2015, 8, 11), date(2015, 9, 30), "중국 위안화 평가절하·증시 급락"),
    (date(2016, 6, 23), date(2016, 7, 6), "브렉시트 국민투표"),
    (date(2018, 2, 2), date(2018, 2, 9), "볼마겟돈(VIX 급등)"),
    (date(2018, 10, 3), date(2018, 12, 24), "연준 긴축 우려 4분기 급락"),
    (date(2020, 2, 19), date(2020, 3, 23), "코로나19 쇼크"),
    (date(2022, 1, 3), date(2022, 10, 14), "연준 급격한 긴축·인플레이션"),
    (date(2022, 2, 24), date(2022, 3, 8), "러시아의 우크라이나 침공"),
    (date(2023, 3, 8), date(2023, 3, 24), "실리콘밸리은행(SVB) 파산·은행 위기"),
    (date(2024, 8, 2), date(2024, 8, 9), "엔캐리 청산·고용 쇼크"),
    (date(2025, 4, 2), date(2025, 4, 30), "미국 상호관세 쇼크"),
]


def _percentile(sorted_values: list[float], p: float) -> float:
    k = (len(sorted_values) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def window_changes(series: dict, kind: str, horizon: int = HORIZON) -> list[tuple]:
    """[(끝 날짜, 변화, 시작 날짜)]. level은 값의 차이(%p), price는 로그 수익률. 값이 이상한 구간은 건너뛴다."""
    ds = sorted(series)
    out = []
    for i in range(horizon, len(ds)):
        a, b = series[ds[i - horizon]], series[ds[i]]
        if kind == "level":
            value = b - a
        elif a and b and a > 0 and b > 0:
            value = math.log(b / a)
        else:
            continue
        out.append((ds[i], value, ds[i - horizon]))
    return out


def display_change(value: float, kind: str) -> tuple[float, str]:
    """화면용 변화: 금리는 bp, 나머지는 %."""
    return (value * 100, "bp") if kind == "level" else ((math.exp(value) - 1) * 100, "%")


def window_return(series: dict, dates: list, start, end) -> float | None:
    """start → end 등락(%), 각 날짜는 그날 이전 가장 가까운 종가. 시세가 start보다 늦게 시작하거나 end 직전에 끊기면 None."""
    i, j = bisect_right(dates, start), bisect_right(dates, end)
    if i == 0 or j == 0:
        return None
    a, b, last = series[dates[i - 1]], series[dates[j - 1]], dates[j - 1]
    if not a or not b or a <= 0 or b <= 0 or (end - last).days >= STALE_DAYS:
        return None
    return (b / a - 1) * 100


def episodes(changes: list[tuple], threshold: float, up: bool) -> list[dict]:
    """임계값을 넘은 날들을 21거래일 이상 떨어진 묶음(사례)으로 나눈다. 묶음마다 가장 극단적인 날을 대표로.
    각 사례: {"peak": 대표일, "start": 그 변화의 시작일, "value": 변화, "last_index": 묶음의 마지막 위치}."""
    hits = [i for i, (_d, v, _s) in enumerate(changes) if (v >= threshold if up else v <= threshold)]
    groups: list[list[int]] = []
    for i in hits:
        if groups and i - groups[-1][-1] <= EPISODE_GAP:
            groups[-1].append(i)
        else:
            groups.append([i])
    out = []
    for g in groups:
        pick = max(g, key=lambda i: changes[i][1]) if up else min(g, key=lambda i: changes[i][1])
        d, v, s = changes[pick]
        out.append({"peak": d, "start": s, "value": v, "last_index": g[-1]})
    return out


def sign_test_p(negatives: int, n: int, base_share: float) -> float:
    """하락 횟수가 평소 하락 비율(base_share) 기준으로 이만큼 치우칠 확률(양측 이항 검정, 한쪽 꼬리를 두 배). 판단 불가면 1."""
    if n <= 0 or not 0 < base_share < 1:
        return 1.0
    pmf = lambda k: math.comb(n, k) * base_share ** k * (1 - base_share) ** (n - k)
    lower = sum(pmf(k) for k in range(0, negatives + 1))
    upper = sum(pmf(k) for k in range(negatives, n + 1))
    return min(1.0, 2 * min(lower, upper))


def label_for(day: date, events=MACRO_EVENTS) -> str | None:
    """사례 날짜가 이름 붙은 이슈의 기간(앞뒤 45일 여유)에 들면 그 이름. 여러 개면 날짜가 기간 안에 있는 것을 먼저."""
    margin = timedelta(days=LABEL_MARGIN_DAYS)
    inside = [e for e in events if e[0] <= day <= e[1]]
    near = [e for e in events if e[0] - margin <= day <= e[1] + margin]
    pick = (inside or near or [None])[0]
    return pick[2] if pick else None


def baseline(asset: dict, dates: list, horizon: int = HORIZON) -> dict | None:
    """평소의 21거래일 수익률 분포(5거래일 간격 표본): 중앙값(%)과 하락 비율."""
    rets = [(asset[dates[i]] / asset[dates[i - horizon]] - 1) * 100 for i in range(horizon, len(dates), 5)
            if asset[dates[i]] > 0 and asset[dates[i - horizon]] > 0]
    if len(rets) < 30:
        return None
    ordered = sorted(rets)
    return {"median": _percentile(ordered, 0.5), "neg_share": sum(r < 0 for r in rets) / len(rets), "n": len(rets)}


def verdict_of(n: int, median: float, neg_share: float, base_neg: float, p_value: float) -> str:
    """down/up: 평소와 통계적으로 구분되게 치우침(보정된 p값 기준), mixed: 구분되지 않음, insufficient: 독립 사례 부족."""
    if n < MIN_EPISODES:
        return "insufficient"
    if p_value < SIGNIFICANT_P:
        if neg_share > base_neg and median < 0:
            return "down"
        if neg_share < base_neg and median > 0:
            return "up"
    return "mixed"


def significance_key(entry: dict) -> tuple:
    """표시·상한 순서: 평소와 통계적으로 구분되는 것 먼저(p값 작은 순), 그다음 지금 더 극단적인 변동 순."""
    significant = entry.get("verdict") in ("up", "down")
    return (not significant, entry["p_value"] if significant else 0.0, -max(entry["percentile"], 1 - entry["percentile"]))


def active_shocks(asset: dict, factor_series: dict[str, dict], base: dict, events=MACRO_EVENTS) -> list[dict]:
    """지금 큰 변동 중인 요인과, 과거 같은 방향의 큰 변동 때 이 종목의 움직임."""
    asset_dates = sorted(asset)
    result = []
    for symbol, series in factor_series.items():
        spec = macro.FACTOR_BY_SYMBOL.get(symbol)
        if not spec or len(series) < MIN_FACTOR_HISTORY:
            continue
        _, label, kind, up_phrase, down_phrase = spec
        changes = window_changes(series, kind)
        if len(changes) < MIN_FACTOR_HISTORY - HORIZON or (asset_dates[-1] - changes[-1][0]).days >= STALE_DAYS:
            continue
        ordered = sorted(v for _d, v, _s in changes)
        hi, lo = _percentile(ordered, BIG_UP), _percentile(ordered, BIG_DOWN)
        current = changes[-1][1]
        if lo < current < hi:
            continue
        up = current >= hi
        rank = sum(v <= current for v in ordered) / len(ordered)
        past = [e for e in episodes(changes, hi if up else lo, up) if e["last_index"] < len(changes) - 1 - EPISODE_GAP]   # 지금 진행 중인 묶음은 뺀다
        rows = []
        for e in past:
            r = window_return(asset, asset_dates, e["start"], e["peak"])
            if r is not None:
                rows.append({"date": e["peak"], "factor_value": e["value"], "asset_return": r, "label": label_for(e["peak"], events)})
        shown_value, unit = display_change(current, kind)
        entry = {"symbol": symbol, "label": label, "direction": "up" if up else "down", "phrase": up_phrase if up else down_phrase,
                 "current_change": shown_value, "unit": unit, "percentile": rank, "episodes": len(rows)}
        if rows:
            ordered_ret = sorted(r["asset_return"] for r in rows)
            negatives = sum(r < 0 for r in ordered_ret)
            neg_share = negatives / len(rows)
            p_value = sign_test_p(negatives, len(rows), base["neg_share"]) if base else 1.0
            entry.update({"median_return": _percentile(ordered_ret, 0.5), "neg_share": neg_share, "p_value": p_value, "verdict": None})
            # 대표 사례는 요인이 가장 극단적이었던 때(종목 고유 사건으로 유독 크게 움직인 때가 뽑히지 않게)
            entry["examples"] = [{"date": r["date"].isoformat(), "label": r["label"], "asset_return": r["asset_return"],
                                  "factor_change": display_change(r["factor_value"], kind)[0]}
                                 for r in sorted(rows, key=lambda r: abs(r["factor_value"]), reverse=True)[:TOP_EXAMPLES]]
        else:
            entry.update({"median_return": None, "neg_share": None, "p_value": None, "verdict": "insufficient", "examples": []})
        result.append(entry)
    tests = sum(1 for s in result if s.get("p_value") is not None and s["episodes"] >= MIN_EPISODES)      # 이 종목에서 동시에 검정한 요인 수
    for s in result:
        if s["verdict"] is None:
            s["p_adjusted"] = min(1.0, s["p_value"] * max(1, tests))
            s["verdict"] = verdict_of(s["episodes"], s["median_return"], s["neg_share"], base["neg_share"], s["p_adjusted"])
        else:
            s["p_adjusted"] = None
    result.sort(key=significance_key)
    return result


def event_reactions(asset: dict, events=MACRO_EVENTS) -> list[dict]:
    """이름 붙은 이슈 기간 동안 이 종목의 수익률과 z(로그 수익률 ÷ (일간 변동성 × √거래일 수)). 시세가 그 기간을 덮는 이슈만."""
    dates = sorted(asset)
    logs = [math.log(asset[b] / asset[a]) for a, b in zip(dates, dates[1:]) if asset[a] > 0 and asset[b] > 0]
    if len(logs) < 250:
        return []
    mean = sum(logs) / len(logs)
    sigma = math.sqrt(sum((x - mean) ** 2 for x in logs) / (len(logs) - 1))
    out = []
    for start, end, name in events:
        r = window_return(asset, dates, start - timedelta(days=1), end)
        if r is None:
            continue
        days = max(1, bisect_right(dates, end) - bisect_right(dates, start - timedelta(days=1)))
        z = math.log(1 + r / 100) / (sigma * math.sqrt(days)) if sigma and r > -100 else 0.0
        out.append({"name": name, "start": start.isoformat(), "end": end.isoformat(), "return_pct": r, "z": z, "days": days})
    return out


def analyze(asset_series: dict, factor_series: dict[str, dict], events=MACRO_EVENTS) -> dict:
    """한 종목의 과거 매크로 충격 반응. status: ok | no_data(시세가 짧음)."""
    asset = {d: v for d, v in asset_series.items() if v and v > 0}
    if len(asset) < MIN_FACTOR_HISTORY:
        return {"status": "no_data", "history_days": len(asset)}
    dates = sorted(asset)
    base = baseline(asset, dates)
    reactions = event_reactions(asset, events)
    by_z = sorted(reactions, key=lambda r: r["z"])
    return {"status": "ok", "history_from": dates[0].isoformat(), "history_days": len(asset), "baseline": base,
            "active": active_shocks(asset, factor_series, base, events) if base else [],
            "worst_events": [r for r in by_z[:3] if r["return_pct"] < 0], "best_events": [r for r in by_z[::-1][:3] if r["return_pct"] > 0],
            "events_covered": len(reactions)}
