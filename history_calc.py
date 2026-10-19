"""과거 시세 가정 계산: 'N개월 전에 이 구성이었다면'의 평가금액과 수익률. 읽기 전용 순수 함수(시세·환율·체결 내역은 호출하는 쪽이 넘긴다).

두 가지 기준:
- current: 지금 보유 수량을 그대로 그때도 들고 있었다고 가정(수익률로 읽을 수 있음).
- actual: 그때 실제로 들고 있던 수량을 체결 내역으로 되돌려 계산(그 사이 사고판 돈 때문에 수익률이 아님).
한계: 주식(예수금·현금 취급 종목 제외)만 계산, 종목 시세는 Yahoo 종가이고 지금 평가금액은 토스 값이라 소액 차이가 있다."""
from __future__ import annotations

import bisect
import math
from datetime import date
from typing import Callable

MAX_GAP_DAYS = 10     # 그 날짜 이전 가장 가까운 종가가 이만큼보다 오래됐으면 시세가 없는 것으로 본다(상장 전·거래정지)
MAX_POINTS = 24       # 변화 그래프의 점 개수 상한


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    last = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def price_on(dates: list[date], values: list[float], day: date) -> float | None:
    """day 이전(당일 포함) 가장 가까운 종가. 기록 시작 전이거나 너무 오래됐으면 None."""
    i = bisect.bisect_right(dates, day) - 1
    if i < 0 or (day - dates[i]).days > MAX_GAP_DAYS:
        return None
    return values[i]


def quantity_at(fills: list[dict], symbol: str, qty_now: float, day: date) -> float:
    """지금 수량에서 day 이후의 체결(매수는 빼고 매도는 더함)을 되돌린 그날의 수량."""
    qty = qty_now
    for f in fills:
        if f.get("symbol") == symbol and str(f.get("filled_at") or "")[:10] > day.isoformat():
            qty += -f["quantity"] if f.get("side") == "BUY" else f["quantity"]
    return max(0.0, qty)


def sample_dates(target: date, today: date) -> list[date]:
    """target부터 today 직전까지 매달 같은 날(많으면 간격을 넓혀 MAX_POINTS 안으로)."""
    months = (today.year - target.year) * 12 + today.month - target.month
    step = max(1, math.ceil(months / (MAX_POINTS - 1))) if months > 0 else 1
    days, k = [], 0
    while True:
        d = add_months(target, k)
        if d >= today:
            break
        days.append(d)
        k += step
    return days or [target]


def past_value(rows: list[dict], closes_of: dict[str, dict], fx: Callable[[date], float | None], fills: list[dict] | None,
               basis: str, target: date, today: date) -> dict:
    """rows: [{symbol, name, currency, qty, value_now_krw}], closes_of: 종목 → {날짜: 종가}, fx: 날짜 → 원/달러.
    기준일에 시세(·환율)가 없는 종목은 missing으로 빼고 합계·변화 그래프에서도 뺀다."""
    series = {}
    for sym, closes in closes_of.items():
        if closes:
            dates = sorted(closes)
            series[sym] = (dates, [closes[d] for d in dates])

    def value_at(row: dict, day: date) -> tuple[float, float, float] | None:
        """(그날 수량, 그날 종가, 원화 평가금액). 시세·환율이 없으면 None."""
        got = series.get(row["symbol"])
        price = price_on(got[0], got[1], day) if got else None
        rate = 1.0 if row["currency"] == "KRW" else fx(day)
        if price is None or rate is None:
            return None
        qty = row["qty"] if basis != "actual" else quantity_at(fills or [], row["symbol"], row["qty"], day)
        return qty, price, qty * price * rate

    out_rows, missing = [], []
    for row in rows:
        then = value_at(row, target)
        if then is None:
            missing.append(row["name"] or row["symbol"])
            continue
        qty, price, value = then
        out_rows.append({"symbol": row["symbol"], "name": row["name"], "qty_then": qty, "price_then": price, "value_then_krw": value, "value_now_krw": row["value_now_krw"],
                         "change_pct": ((row["value_now_krw"] / value - 1) * 100 if basis != "actual" and value > 0 else None)})
    used = {r["symbol"] for r in out_rows}
    then_total = sum(r["value_then_krw"] for r in out_rows)
    now_total = sum(r["value_now_krw"] for r in out_rows)
    all_now = sum(r["value_now_krw"] for r in rows)
    points = []
    for day in sample_dates(target, today):
        total = 0.0
        for row in rows:
            if row["symbol"] in used:
                got = value_at(row, day)
                total += got[2] if got else 0.0
        points.append({"date": day.isoformat(), "value_krw": total})
    points.append({"date": today.isoformat(), "value_krw": now_total})
    out_rows.sort(key=lambda r: -max(r["value_then_krw"], r["value_now_krw"]))
    return {"basis": basis, "target_date": target.isoformat(), "today": today.isoformat(), "rows": out_rows, "missing": missing,
            "total_then_krw": then_total, "total_now_krw": now_total, "change_krw": now_total - then_total,
            "change_pct": ((now_total / then_total - 1) * 100 if basis != "actual" and then_total > 0 else None),
            "covered_pct": (now_total / all_now * 100 if all_now else 0.0), "series": points}
