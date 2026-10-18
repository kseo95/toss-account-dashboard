"""고점 이후 큰 하락일 찾기와 그날 시장 움직임: 종목이 크게 빠진 날을 고르고, 같은 날 평소(최근 1년 일간 변동)보다 유난히 크게 움직인 시장 요인을 붙인다.

사건 이름은 시세로 알 수 없으므로, 어느 날 무엇이 같이 크게 움직였는지까지만 알려 주고 그 날짜의 보도는 호출하는 쪽이 따로 찾는다.
같이 움직인 날을 보여 줄 뿐 원인의 증명이 아니다. 순수 계산이며 시세·요인 일간 변화는 호출하는 쪽이 넘긴다(다른 기능 모듈을 부르지 않는다).
"""
from __future__ import annotations

import math
from datetime import date

MAX_DAYS = 4                # 보여 주는 큰 하락일 수
MIN_DROP_PCT = 1.0          # 이보다 덜 빠진 날은 '큰 하락일'로 보지 않는다
MIN_GAP_DAYS = 3            # 이미 고른 날과 이 일수 안쪽인 날은 같은 사건으로 보고 건너뛴다(연속 하락의 중복 방지)
MOVER_MIN_Z = 2.0           # 평소 일간 변동(표준편차)의 이 배수 이상 움직인 요인만 '같은 날 크게 움직임'
MOVERS_PER_DAY = 3
STD_WINDOW = 250
RISK_SYMBOLS = frozenset({"^GSPC", "^IXIC", "^RUT", "^SOX", "^KS11", "^KQ11", "^N225", "^STOXX50E", "000001.SS", "^HSI", "EEM", "XLK", "XLF", "BTC-USD", "ETH-USD"})   # 위험자산(주식·가상자산) 쪽 요인
MIN_STD_OBS = 60


def worst_days(series: dict[date, float], since: date, count: int = MAX_DAYS, min_drop_pct: float = MIN_DROP_PCT) -> list[dict]:
    """since(고점 날짜) 이후 하루 하락이 가장 컸던 날들(많이 빠진 순으로 골라 날짜순으로 돌려준다). [{"date", "change_pct"}]"""
    dates = sorted(series)
    moves = []
    for prev, cur in zip(dates, dates[1:]):
        if cur > since and series[prev] > 0 and series[cur] > 0:
            moves.append((cur, (series[cur] / series[prev] - 1) * 100))
    picked: list[tuple[date, float]] = []
    for day, pct in sorted(moves, key=lambda m: m[1]):
        if pct > -min_drop_pct or len(picked) >= count:
            break
        if all(abs((day - other).days) > MIN_GAP_DAYS for other, _ in picked):
            picked.append((day, pct))
    return [{"date": day, "change_pct": pct} for day, pct in sorted(picked)]


def day_movers(day: date, changes: dict[str, dict[date, float]], meta: dict[str, tuple], top: int = MOVERS_PER_DAY, min_z: float = MOVER_MIN_Z) -> list[dict]:
    """그날 평소보다 유난히 크게 움직인 요인. changes: {심볼: {날짜: 일간 변화(price=로그수익률, level=차이)}}, meta: {심볼: (이름, kind, 상승말, 하락말)}.
    z = 그날 변화 / 직전 1년 일간 변화의 표준편차. |z|가 큰 순. 반환 변화는 price는 %, level(금리)은 bp."""
    out = []
    for symbol, daily in changes.items():
        if day not in daily or symbol not in meta:
            continue
        past = [v for d, v in sorted(daily.items()) if d < day][-STD_WINDOW:]
        if len(past) < MIN_STD_OBS:
            continue
        mean = sum(past) / len(past)
        std = math.sqrt(sum((v - mean) ** 2 for v in past) / (len(past) - 1))
        if std <= 0:
            continue
        z = daily[day] / std
        if abs(z) < min_z:
            continue
        label, kind, up, down = meta[symbol]
        value = daily[day] * 100 if kind == "level" else (math.exp(daily[day]) - 1) * 100
        out.append({"symbol": symbol, "label": label, "change": value, "unit": "bp" if kind == "level" else "%", "phrase": up if daily[day] >= 0 else down, "z": z})
    out.sort(key=lambda m: abs(m["z"]), reverse=True)
    return out[:top]


def search_term(label: str, change: float) -> str:
    """같은 날 크게 움직인 요인 이름 → 보도 검색어(예: '반도체지수(SOX)' 하락 → '반도체지수 급락')."""
    base = label.split("(")[0].strip()
    return f"{base} {'급락' if change < 0 else '급등'}"
