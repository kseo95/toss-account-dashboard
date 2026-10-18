"""기술적 보조지표: RSI(와일더)·MACD·볼린저밴드·이동평균 교차(50/200일). 종가만으로 계산하고 상태 이름을 붙인다.

지표는 가격 흐름을 숫자로 요약한 것이며 미래를 알려 주지 않는다: 과매수(RSI 70↑)는 '최근 상승이 빨랐다'는 뜻이지 곧 하락한다는 뜻이 아니고,
과매도도 마찬가지다. 강한 추세에서는 과매수·과매도가 오래 이어진다. 기본 기준(14일·70/30, 12·26·9, 20일 ±2σ, 50/200일)은 널리 쓰이는 값이고 질문에서 바꿀 수 있다.
순수 계산이며 시세는 호출하는 쪽이 넘긴다(다른 기능 모듈을 부르지 않는다).
"""
from __future__ import annotations

import math

SERIES_DAYS = 120               # 그래프용으로 돌려주는 최근 거래일 수
MIN_BARS = 60                   # 이보다 짧으면 계산하지 않는다
CROSS_RECENT_DAYS = 10          # 이 안에 교차가 있으면 '최근 교차'로 알린다
DEFAULTS = {"rsi_period": 14, "overbought": 70.0, "oversold": 30.0, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "bb_period": 20, "bb_k": 2.0, "ma_fast": 50, "ma_slow": 200}
RANGES = {"rsi_period": (5, 30), "overbought": (55.0, 95.0), "oversold": (5.0, 45.0), "bb_period": (10, 60), "bb_k": (1.0, 3.0)}
INDICATORS = ("rsi", "macd", "bollinger", "ma_cross")


def clean_params(raw) -> dict:
    """질문에서 바꾼 기준 중 범위 안의 숫자만 받고 나머지는 기본값. 과매수는 과매도보다 커야 한다."""
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key, (lo, hi) in RANGES.items():
            v = raw.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi:
                out[key] = int(v) if key in ("rsi_period", "bb_period") else float(v)
    if out["overbought"] <= out["oversold"]:
        out["overbought"], out["oversold"] = DEFAULTS["overbought"], DEFAULTS["oversold"]
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    """지수이동평균(첫 n개의 단순평균에서 시작). n개가 모자라면 앞쪽은 None."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    running = sum(values[:n])
    out[n - 1] = running / n
    for i in range(n, len(values)):
        running += values[i] - values[i - n]
        out[i] = running / n
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """와일더 RSI(0~100). 평균 상승·하락을 지수 방식으로 이어 가며, 하락이 전혀 없으면 100."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period

    def value(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    out[period] = value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = value(avg_gain, avg_loss)
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list, list, list]:
    """MACD선(빠른 EMA − 느린 EMA), 시그널선(MACD의 EMA), 히스토그램(MACD − 시그널)."""
    f, s = ema(closes, fast), ema(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[float | None] = [None] * len(closes)
    if start is not None:
        part = ema([v for v in line[start:]], signal)
        for i, v in enumerate(part):
            sig[start + i] = v
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> tuple[list, list, list]:
    """중심선(단순이동평균)과 ±k 표준편차 상단·하단 밴드(모표준편차)."""
    mid = sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((v - m) ** 2 for v in window) / period)
        upper[i], lower[i] = m + k * sd, m - k * sd
    return mid, upper, lower


def _last_cross(a: list, b: list) -> dict | None:
    """두 선(a가 b를 위로 뚫으면 golden, 아래로 뚫으면 dead)의 가장 최근 교차. 반환: {"type", "index"(교차가 일어난 날의 인덱스)}"""
    last = None
    for i in range(1, len(a)):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        before, now = a[i - 1] - b[i - 1], a[i] - b[i]
        if before <= 0 < now:
            last = {"type": "golden", "index": i}
        elif before >= 0 > now:
            last = {"type": "dead", "index": i}
    return last


def _tail(values: list, n: int) -> list:
    return [None if v is None else round(v, 4) for v in values[-n:]]


def analyze(dates: list, closes: list[float], params: dict | None = None) -> dict | None:
    """한 종목의 지표·상태·그래프용 최근 시계열. 종가가 MIN_BARS개 미만이면 None. dates는 closes와 같은 길이(오래된 것 → 최근)."""
    p = clean_params(params)
    n = len(closes)
    if n < MIN_BARS or len(dates) != n:
        return None
    r = rsi(closes, p["rsi_period"])
    line, sig, hist = macd(closes, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    mid, upper, lower = bollinger(closes, p["bb_period"], p["bb_k"])
    fast_ma, slow_ma = sma(closes, p["ma_fast"]), sma(closes, p["ma_slow"])
    last = n - 1
    out: dict = {"params": p, "as_of": dates[last].isoformat() if hasattr(dates[last], "isoformat") else str(dates[last]), "close": closes[last], "bars": n}
    rv = r[last]
    if rv is not None:
        state = "overbought" if rv >= p["overbought"] else "oversold" if rv <= p["oversold"] else "neutral"
        days = 0
        for v in reversed(r):
            if v is None or not ((state == "overbought" and v >= p["overbought"]) or (state == "oversold" and v <= p["oversold"]) or (state == "neutral" and p["oversold"] < v < p["overbought"])):
                break
            days += 1
        out["rsi"] = {"value": rv, "state": state, "days_in_state": days, "above_50": rv > 50}
    if line[last] is not None and sig[last] is not None:
        cross = _last_cross(line, sig)
        cross_info = None
        if cross:
            ago = last - cross["index"]
            cross_info = {"type": cross["type"], "days_ago": ago, "date": dates[cross["index"]].isoformat() if hasattr(dates[cross["index"]], "isoformat") else str(dates[cross["index"]]),
                          "recent": ago <= CROSS_RECENT_DAYS, "in_window": ago < SERIES_DAYS}
        out["macd"] = {"macd": line[last], "signal": sig[last], "hist": hist[last], "above_signal": line[last] > sig[last], "above_zero": line[last] > 0,
                       "hist_rising": bool(hist[last] is not None and hist[last - 1] is not None and hist[last] > hist[last - 1]), "cross": cross_info}
    if upper[last] is not None and upper[last] > lower[last]:
        pct_b = (closes[last] - lower[last]) / (upper[last] - lower[last])
        state = "above_upper" if pct_b > 1 else "below_lower" if pct_b < 0 else "near_upper" if pct_b >= 0.8 else "near_lower" if pct_b <= 0.2 else "inside"
        out["bollinger"] = {"pct_b": pct_b, "state": state, "upper": upper[last], "mid": mid[last], "lower": lower[last], "bandwidth_pct": (upper[last] - lower[last]) / mid[last] * 100}
    if fast_ma[last] is not None and slow_ma[last] is not None:
        cross = _last_cross(fast_ma, slow_ma)
        out["ma_cross"] = {"fast": p["ma_fast"], "slow": p["ma_slow"], "fast_value": fast_ma[last], "slow_value": slow_ma[last], "fast_above": fast_ma[last] > slow_ma[last],
                           "last_cross": ({"type": cross["type"], "days_ago": last - cross["index"],
                                           "date": dates[cross["index"]].isoformat() if hasattr(dates[cross["index"]], "isoformat") else str(dates[cross["index"]])} if cross else None)}
    cut = min(SERIES_DAYS, n)
    out["series"] = {"dates": [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in dates[-cut:]], "close": _tail(closes, cut), "rsi": _tail(r, cut),
                     "macd": _tail(line, cut), "signal": _tail(sig, cut), "hist": _tail(hist, cut), "upper": _tail(upper, cut), "mid": _tail(mid, cut), "lower": _tail(lower, cut)}
    return out
