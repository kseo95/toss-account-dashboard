"""체결 내역: 토스 "종료된 주문" 중 실제로 체결된 것만 골라 표 행으로 만든다.

토스 Open API에는 별도의 거래내역 API가 없고, 주문 목록(status=CLOSED)의 execution에 체결 수량·평균가·금액·
수수료·세금·결제일이 들어 있다. 체결 시점 환율은 없어서 원화 손익은 결제일 환율로 계산한다.

해외 주식 실현 손익(양도세 계산용) 규칙 — 2026-09-28 법제처 원문 확인:
- 먼저 산 것부터 판 것으로 본다(선입선출, 소득세법 시행령 제162조⑤).
- 양도일·취득일 = 대금을 청산한 날 = 결제일(시행령 제162조①). 연도 구분도 결제일 기준.
- 원화 환산은 그날의 기준환율(시행령 제178조의5) — 여기서는 Yahoo 일별 원/달러 종가로 근사한다.
- 수수료·거래세는 필요경비: 매수 수수료는 취득가액에 더하고, 매도 수수료·세금은 양도가액에서 뺀다.
"""
from __future__ import annotations

import bisect
from collections import deque
from datetime import date
from typing import Callable

from common import to_float


def fills_from_orders(orders: list[dict], account_seq: str | None = None) -> list[dict]:
    """주문 목록 → 체결 행(체결 수량 > 0). 부분 체결 후 취소된 주문도 체결된 만큼은 포함. 최신 체결이 앞."""
    fills = []
    for o in orders:
        ex = o.get("execution") or {}
        qty = to_float(ex.get("filledQuantity"))
        if qty <= 0:
            continue
        price = to_float(ex.get("averageFilledPrice"))
        fills.append({
            "order_id": o.get("orderId", ""),
            "account": account_seq,
            "symbol": o.get("symbol", ""),
            "side": o.get("side", ""),
            "currency": o.get("currency", "KRW"),
            "quantity": qty,
            "price": price,
            "amount": to_float(ex.get("filledAmount"), qty * price),
            "commission": to_float(ex.get("commission")),
            "tax": to_float(ex.get("tax")),
            "filled_at": ex.get("filledAt") or o.get("orderedAt") or "",
            "settlement_date": ex.get("settlementDate"),
            "status": o.get("status", ""),
        })
    fills.sort(key=lambda f: f["filled_at"], reverse=True)
    return fills


def summarize_fills(fills: list[dict]) -> dict:
    """통화·매수/매도별 건수와 금액·수수료·세금 합계."""
    out: dict[str, dict] = {}
    for f in fills:
        key = f"{f['currency']}_{f['side']}"
        s = out.setdefault(key, {"currency": f["currency"], "side": f["side"], "count": 0, "amount": 0.0,
                                 "commission": 0.0, "tax": 0.0})
        s["count"] += 1
        s["amount"] += f["amount"]
        s["commission"] += f["commission"]
        s["tax"] += f["tax"]
    return {"by_kind": sorted(out.values(), key=lambda s: (s["currency"], s["side"])),
            "first_filled_at": min((f["filled_at"] for f in fills), default=None),
            "last_filled_at": max((f["filled_at"] for f in fills), default=None)}


def _settle_date(f: dict) -> date:
    """결제일(없으면 체결일)."""
    return date.fromisoformat((f.get("settlement_date") or f["filled_at"])[:10])


def realized_gains(fills: list[dict], fx_on: Callable[[date], float | None], year: int, currency: str = "USD") -> dict:
    """해외(currency) 종목의 year년 실현 손익(원화, 선입선출). fills는 전체 기간이어야 예전 매수분까지 맞는다.
    fx_on(날짜)는 그날(또는 직전 거래일) 원/통화 환율, 모르면 None.

    반환: sells(매도별 손익), total_gain_krw, 그리고 계산이 불완전한 이유들(missing_buys: 매수 기록보다 많이 판
    수량 — 예전 거래가 API에 없거나 입고 등 / missing_fx: 환율을 못 구한 날짜)."""
    lots: dict[str, deque] = {}
    sells, missing_buys, missing_fx = [], [], set()

    def krw(amount: float, d: date) -> float | None:
        rate = fx_on(d)
        if rate is None:
            missing_fx.add(d.isoformat())
        return None if rate is None else amount * rate

    ordered = sorted((f for f in fills if f["currency"] == currency), key=lambda f: (_settle_date(f), f["filled_at"]))
    for f in ordered:
        d = _settle_date(f)
        queue = lots.setdefault(f["symbol"], deque())
        if f["side"] == "BUY":
            cost = krw(f["amount"] + f["commission"] + f["tax"], d)
            queue.append([f["quantity"], None if cost is None else cost / f["quantity"]])
            continue
        # 매도: 앞에서부터 매수분을 꺼내 쓴다.
        need, cost_krw, cost_known = f["quantity"], 0.0, True
        while need > 1e-9 and queue:
            lot = queue[0]
            take = min(need, lot[0])
            if lot[1] is None:
                cost_known = False
            else:
                cost_krw += take * lot[1]
            lot[0] -= take
            need -= take
            if lot[0] <= 1e-9:
                queue.popleft()
        if need > 1e-9:
            cost_known = False
            missing_buys.append({"symbol": f["symbol"], "quantity": need, "date": d.isoformat()})
        if d.year != year:
            continue
        proceeds = krw(f["amount"] - f["commission"] - f["tax"], d)
        gain = proceeds - cost_krw if (proceeds is not None and cost_known) else None
        sells.append({"symbol": f["symbol"], "settlement_date": d.isoformat(), "quantity": f["quantity"],
                      "proceeds_krw": proceeds, "cost_krw": cost_krw if cost_known else None, "gain_krw": gain,
                      "fx": fx_on(d)})
    known = [s["gain_krw"] for s in sells if s["gain_krw"] is not None]
    return {
        "year": year,
        "sells": sells,
        "total_gain_krw": sum(known),
        "complete": len(known) == len(sells) and not missing_buys,
        "unknown_count": len(sells) - len(known),
        "missing_buys": [m for m in missing_buys if m["date"][:4] == str(year)],
        "missing_fx": sorted(missing_fx),
    }


def fx_lookup(bars: list[tuple]) -> Callable[[date], float | None]:
    """Yahoo 일봉 [(날짜, 종가, ...)] → 날짜의 환율(그날이 휴일이면 직전 거래일). 기록보다 이전이면 None."""
    dates = [b[0] for b in bars]
    closes = [b[1] for b in bars]

    def on(d: date) -> float | None:
        i = bisect.bisect_right(dates, d) - 1
        return closes[i] if i >= 0 else None
    return on
