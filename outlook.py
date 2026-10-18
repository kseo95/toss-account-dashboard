"""종목별 앞으로의 상승/하락 뷰: 과거 유사 국면 통계 + 요인 민감도 시나리오 + 변동성 범위. **예측이 아니다.**

- 과거 유사 국면: 지금과 같은 국면(200일선 위/아래 × 52주 고점 대비 낙폭 구간)이었던 과거 시점들에서 이후 1·3개월 수익률이
  어땠는지(중앙값·사분위·상승 비율). 유사 시점이 15개 미만이면 200일선 위/아래만으로 완화하고, 그래도 모자라면 "표본 부족"이다.
  각 시점 이후의 데이터만 쓰며(미래 정보를 끌어오지 않음) 표본은 5거래일 간격으로 뽑아 겹침을 줄이고, 독립 사례 수(30거래일 이상 떨어진 묶음)를 같이 알린다.
- 요인 민감도: macro.py의 회귀 계수로 "달러가 1개월 표준편차만큼 오르면/내리면 이 종목은 약 몇 %" (설명력 R² 0.2 이상일 때만).
- 더 긴 과거: 상장이 최근인 종목(금 ETF 등)은 긴 대용 시세(금 월간 가격 1975~, 지수 일봉)로 과거 유사 국면을 계산할 수 있다
  (long_series). 월간 자료는 같은 규칙을 월 단위로 근사한다(10개월 평균선·12개월 고점·1·3개월 뒤). 현재 국면은 항상 종목 자신의 일봉으로 판정한다.
- 변동성 범위: 최근 60일 일간 변동성으로 1·3개월 ±1표준편차 범위(방향 예측이 아니라 폭).
외부 호출은 하지 않는다(시세는 호출하는 쪽이 넘긴다).
"""
from __future__ import annotations

import math
import statistics

import macro

HORIZONS = {"1M": 21, "3M": 63}
MIN_HISTORY = 400          # 거래일. 국면 판정(252일)과 유사 상황 표본을 위한 최소 시세 길이
MIN_ANALOGS = 15
MIN_EPISODES = 5           # 독립 사례가 이보다 적으면 표본이 많아 보여도 판단하지 않는다(비슷한 구간 몇 번에 몰린 것)
SAMPLE_STEP = 5            # 유사 시점을 5거래일 간격으로 뽑아 겹침을 줄인다
EPISODE_GAP = 30           # 거래일. 이보다 떨어져 있으면 별개의 사례로 센다
NEAR_HIGH_PCT = -5.0       # 52주 고점 대비 이보다 덜 빠졌으면 '고점 근처'
DEEP_PULLBACK_PCT = -15.0  # 이보다 더 빠졌으면 '깊은 조정'
UP_SHARE = 0.65
DOWN_SHARE = 0.45
VOL_WINDOW = 60
MAX_SCENARIO_FACTORS = 3

# 과거 유사 국면을 세는 단위. 일봉은 200일선·52주 고점, 월간은 같은 규칙의 월 단위 근사(10개월 평균선·12개월 고점).
DAILY = {"kind": "daily", "sma": 200, "high": 252, "h1": 21, "h3": 63, "step": 5, "gap": 30, "min_points": MIN_HISTORY}
MONTHLY = {"kind": "monthly", "sma": 10, "high": 12, "h1": 1, "h3": 3, "step": 1, "gap": 2, "min_points": 120}

DEPTH_TEXT = {"near": "고점 근처", "pullback": "조정 구간", "deep": "깊은 조정"}
TREND_TEXT = {"above": "200일선 위", "below": "200일선 아래"}


def regime_at(closes: list[float], i: int, sma: int = 200, high: int = 252) -> tuple[str, str, float] | None:
    """i번째 시점의 국면: (추세 above/below, 낙폭 구간 near/pullback/deep, 고점 대비 %). 시세가 부족하면 None.
    기본은 일봉(200일선·252일 고점)이고, 월간 자료는 sma=10·high=12로 같은 규칙을 월 단위로 쓴다."""
    if i < high - 1:
        return None
    avg = sum(closes[i - sma + 1:i + 1]) / sma
    drawdown = (closes[i] / max(closes[i - high + 1:i + 1]) - 1) * 100
    depth = "near" if drawdown >= NEAR_HIGH_PCT else "pullback" if drawdown > DEEP_PULLBACK_PCT else "deep"
    return ("above" if closes[i] > avg else "below"), depth, drawdown


def _percentile(sorted_values: list[float], p: float) -> float:
    k = (len(sorted_values) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def distribution(values: list[float]) -> dict | None:
    """수익률(%) 목록의 분포 요약."""
    if not values:
        return None
    ordered = sorted(values)
    return {"n": len(ordered), "median": _percentile(ordered, 0.5), "p25": _percentile(ordered, 0.25),
            "p75": _percentile(ordered, 0.75), "up_share": sum(v > 0 for v in ordered) / len(ordered)}


def skew_of(stats_3m: dict | None, episodes: int | None = None) -> str:
    """과거 유사 상황 3개월 후 결과로 본 기울기: up / down / mixed / insufficient.
    표본이 모자라거나(15개 미만) 독립 사례가 5회 미만이면 판단하지 않는다."""
    if not stats_3m or stats_3m["n"] < MIN_ANALOGS or (episodes is not None and episodes < MIN_EPISODES):
        return "insufficient"
    if stats_3m["up_share"] >= UP_SHARE and stats_3m["median"] > 0:
        return "up"
    if stats_3m["up_share"] <= DOWN_SHARE and stats_3m["median"] < 0:
        return "down"
    return "mixed"


def analog_stats(closes: list[float], cfg: dict = DAILY, current: tuple | None = None) -> dict | None:
    """지금과 같은 국면이었던 과거 시점들의 이후 1·3개월 수익률 분포. 시세가 모자라면 None.
    cfg: DAILY(기본)나 MONTHLY. current: 현재 국면을 따로 줄 때(대용 시세의 마지막 값이 아니라 종목 자신의 국면을 쓰려고)."""
    n, start = len(closes), cfg["high"] - 1
    horizons = {"1M": cfg["h1"], "3M": cfg["h3"]}
    regimes = [regime_at(closes, i, cfg["sma"], cfg["high"]) for i in range(n)]
    current = current or regimes[-1]
    if current is None:
        return None

    def pick(match) -> list[int]:
        chosen, last = [], -10 ** 9
        for i in range(start, n - 1):  # 마지막 시점(지금)은 제외
            r = regimes[i]
            if r and match(r) and i - last >= cfg["step"]:
                chosen.append(i)
                last = i
        return chosen

    index, relaxed = pick(lambda r: r[:2] == current[:2]), False
    if len([i for i in index if i + horizons["3M"] < n]) < MIN_ANALOGS:
        index, relaxed = pick(lambda r: r[0] == current[0]), True   # 추세 위/아래만으로 완화
    forward = {name: distribution([(closes[i + h] / closes[i] - 1) * 100 for i in index if i + h < n]) for name, h in horizons.items()}
    usable = [i for i in index if i + horizons["3M"] < n]
    episodes = (1 + sum(1 for a, b in zip(usable, usable[1:]) if b - a > cfg["gap"])) if usable else 0
    baseline_idx = range(start, n - horizons["3M"], cfg["step"])
    baseline = distribution([(closes[i + horizons["3M"]] / closes[i] - 1) * 100 for i in baseline_idx])
    return {"relaxed": relaxed, "forward": forward, "episodes": episodes, "baseline_3m": baseline,
            "skew": skew_of(forward["3M"], episodes)}


def volatility_bands(closes: list[float]) -> dict:
    """최근 60일 일간 변동성으로 본 1·3개월 ±1표준편차 범위(가격과 %). 방향 예측이 아니다."""
    rets = [math.log(b / a) for a, b in zip(closes[-VOL_WINDOW - 1:], closes[-VOL_WINDOW:]) if a > 0 and b > 0]
    sigma = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    price = closes[-1]
    bands = {}
    for name, h in HORIZONS.items():
        s = sigma * math.sqrt(h)
        bands[name] = {"low": price * math.exp(-s), "high": price * math.exp(s),
                       "low_pct": (math.exp(-s) - 1) * 100, "high_pct": (math.exp(s) - 1) * 100}
    return {"annual_vol_pct": sigma * math.sqrt(252) * 100, "bands": bands}


def factor_scenarios(asset_series: dict, factor_series: dict[str, dict]) -> dict:
    """요인이 1개월 표준편차만큼 움직일 때 이 종목이 어떻게 움직였는지(회귀 계수 기준). 설명력이 낮으면 비워 둔다.
    반환: {"r2", "up": [...], "down": [...]}, 각 항목: 요인·방향·크기·예상 영향(%)."""
    empty = {"r2": None, "up": [], "down": []}
    factor_changes = {s: macro.daily_changes(series, macro.FACTOR_BY_SYMBOL[s][2])
                      for s, series in factor_series.items() if s in macro.FACTOR_BY_SYMBOL}
    fit = macro.fit_factors(macro.daily_changes(asset_series, "price"), factor_changes) if factor_changes else None
    if not fit:
        return empty
    unverified = fit.get("oos_r2") is not None and fit["oos_r2"] < macro.OOS_MIN_R2    # 검증 구간에서 못 맞히면 시나리오를 만들지 않는다
    if not fit["factors"] or fit["r2"] < macro.WEAK_R2 or unverified:
        return {**empty, "r2": fit["r2"]}
    up, down = [], []
    for symbol, beta in list(zip(fit["factors"], fit["betas"]))[:MAX_SCENARIO_FACTORS]:
        _, label, kind, up_phrase, down_phrase = macro.FACTOR_BY_SYMBOL[symbol]
        daily = [v for _d, v in sorted(factor_changes[symbol].items())][-macro.TRAIN_DAYS:]
        shock = statistics.pstdev(daily) * math.sqrt(HORIZONS["1M"]) if len(daily) > 1 else 0.0
        if shock == 0:
            continue
        size = f"{shock * 100:.0f}bp" if kind == "level" else f"{(math.exp(shock) - 1) * 100:.1f}%"
        for sign, phrase in ((1, up_phrase), (-1, down_phrase)):
            impact = (math.exp(beta * sign * shock) - 1) * 100
            item = {"label": label, "phrase": phrase, "move": ("+" if sign > 0 else "-") + size, "impact_pct": impact}
            (up if impact > 0 else down).append(item)
    up.sort(key=lambda x: x["impact_pct"], reverse=True)
    down.sort(key=lambda x: x["impact_pct"])
    return {"r2": fit["r2"], "up": up, "down": down}


def build_outlook(asset_series: dict, factor_series: dict[str, dict], long_series: dict | None = None, long_error: str | None = None) -> dict:
    """한 종목의 앞으로의 상승/하락 뷰 재료. status: ok | no_data(시세 기록이 짧음).
    long_series: 과거 유사 국면 계산에 쓸 더 긴 대용 시세 {"kind": "daily"|"monthly", "series": {날짜: 값}, "label": 이름}.
    쓰면 analogs["source"]에 무엇으로 계산했는지 남기고, 대용 시세를 못 받았으면(long_error) 자체 시세로 계산하고 그 사실을 남긴다."""
    dates = sorted(asset_series)
    closes = [asset_series[d] for d in dates if asset_series[d] and asset_series[d] > 0]
    if len(closes) < MIN_HISTORY:
        return {"status": "no_data", "history_days": len(closes)}
    regime = regime_at(closes, len(closes) - 1)
    analogs = analog_stats(closes)
    source = None
    if long_series:
        cfg = MONTHLY if long_series.get("kind") == "monthly" else DAILY
        long_dates = sorted(d for d, v in long_series["series"].items() if v and v > 0)
        long_closes = [long_series["series"][d] for d in long_dates]
        longer = analog_stats(long_closes, cfg, current=regime) if len(long_closes) >= cfg["min_points"] else None
        if longer:
            analogs = longer
            source = {"label": str(long_series.get("label") or ""), "kind": cfg["kind"], "from": long_dates[0].isoformat(),
                      "to": long_dates[-1].isoformat(), "points": len(long_closes)}
    if analogs is not None:
        analogs["source"] = source
        analogs["source_error"] = long_error if (long_error and source is None) else None
    month = (closes[-1] / closes[-22] - 1) * 100 if len(closes) > 22 else None
    vol = volatility_bands(closes)
    return {"status": "ok", "price": closes[-1], "history_days": len(closes), "trend": regime[0], "depth": regime[1],
            "drawdown_pct": regime[2], "month_change_pct": month, "annual_vol_pct": vol["annual_vol_pct"], "bands": vol["bands"],
            "analogs": analogs, "scenarios": factor_scenarios(asset_series, factor_series)}
