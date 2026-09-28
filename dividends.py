"""배당 추정: 토스 API에는 배당·입출금 내역이 없어서, 체결 내역으로 "배당락일에 몇 주를 들고 있었나"를 되짚고
Yahoo의 종목별 배당 기록(배당락일, 주당 금액)을 곱해 추정한다.

한계(화면에 표시):
- Yahoo 배당 날짜는 배당락일. 실제 입금은 보통 몇 주 뒤라 연말 배당은 다음 해에 들어올 수 있다(여기서는 배당락일 연도로 셈).
- 원화 환산은 배당락일 환율(실제는 입금일 환율).
- API에 없는 예전 거래·입고분은 "지금 보유 수량 − 체결로 계산한 수량"을 처음부터 들고 있던 것으로 본다.
- 국내 ETF 분배금은 Yahoo 기록이 빠지거나 다를 수 있다.

세금: 미국 배당은 미국에서 15% 원천징수(한국 세율 14%보다 높아 국내 추가 없음), 국내는 15.4%(14% + 지방 1.4%).
이자·배당 합계(세전)가 2천만 원을 넘으면 종합과세(소득세법 제14조③6호) — 그래서 세전 금액을 합산한다.
"""
from __future__ import annotations

from datetime import date
from typing import Callable

US_WITHHOLDING_PCT = 15.0  # 한미 조세조약 배당 원천징수(일반 개인)
KR_WITHHOLDING_PCT = 15.4  # 소득세법 제129조 14% + 지방세법 제103조의13 1.4%


def _trade_date(f: dict) -> date:
    return date.fromisoformat(f["filled_at"][:10])


def shares_on(fills: list[dict], symbol: str, ex_date: date, offset: float) -> float:
    """배당락일 전날까지 체결된 매수 − 매도 + offset(API에 없는 예전 보유분). 배당락일에 산 건 배당을 못 받는다."""
    qty = offset
    for f in fills:
        if f["symbol"] == symbol and _trade_date(f) < ex_date:
            qty += f["quantity"] if f["side"] == "BUY" else -f["quantity"]
    return max(0.0, qty)


def estimate_dividends(fills: list[dict], held_qty: dict[str, float], currency_of: dict[str, str],
                       divs_of: dict[str, list[tuple]], fx_on: Callable[[date], float | None], year: int) -> dict:
    """year년 배당 추정. held_qty = 지금 보유 수량(체결 내역에 없는 예전 보유분을 맞추는 데 씀),
    currency_of = 종목 통화, divs_of = 종목별 [(배당락일, 주당 금액)] (Yahoo), fx_on = 날짜 → 원/달러."""
    rows, notes = [], []
    for symbol, divs in sorted(divs_of.items()):
        traded = sum(f["quantity"] if f["side"] == "BUY" else -f["quantity"] for f in fills if f["symbol"] == symbol)
        offset = held_qty.get(symbol, 0.0) - traded
        if offset > 1e-9:
            notes.append(f"{symbol}: 체결 내역에 없는 {offset:g}주는 올해 처음부터 들고 있던 것으로 계산")
        currency = currency_of.get(symbol, "USD")
        for ex_date, per_share in divs:
            if ex_date.year != year:
                continue
            shares = shares_on(fills, symbol, ex_date, max(0.0, offset))
            if shares <= 0:
                continue
            gross = shares * per_share
            rate = 1.0 if currency == "KRW" else fx_on(ex_date)
            gross_krw = None if rate is None else gross * rate
            withhold_pct = KR_WITHHOLDING_PCT if currency == "KRW" else US_WITHHOLDING_PCT
            rows.append({
                "symbol": symbol, "ex_date": ex_date.isoformat(), "per_share": per_share, "currency": currency,
                "shares": shares, "gross": gross, "gross_krw": gross_krw, "withholding_pct": withhold_pct,
                "withheld_krw": None if gross_krw is None else gross_krw * withhold_pct / 100,
                "net_krw": None if gross_krw is None else gross_krw * (1 - withhold_pct / 100),
            })
    rows.sort(key=lambda r: r["ex_date"], reverse=True)
    known = [r for r in rows if r["gross_krw"] is not None]
    if len(known) < len(rows):
        notes.append(f"환율을 못 구한 {len(rows) - len(known)}건은 합계에서 뺐어요.")
    return {
        "year": year,
        "rows": rows,
        "total_gross_krw": sum(r["gross_krw"] for r in known),
        "total_withheld_krw": sum(r["withheld_krw"] for r in known),
        "total_net_krw": sum(r["net_krw"] for r in known),
        "notes": notes,
    }
