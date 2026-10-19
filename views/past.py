"""과거 시세 가정 계산 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from common import parallel_map
from history_calc import add_months
from history_calc import past_value
from trades import fx_lookup
from weekly_report import toss_to_yahoo_symbol

from ._server import S

if TYPE_CHECKING:
    from server import Request


def past_value_view(req: Request, args: dict | None = None) -> dict:
    """'N개월 전에 이 구성이었다면' 가정 계산(주식만, 예수금·현금 취급 종목 제외). basis=current(지금 수량 그대로) 또는 actual(체결 내역으로 되돌린 그때 실제 수량). 읽기 전용."""
    args = args if isinstance(args, dict) else {}
    try:
        months = int(args.get("months_ago") or S.PAST_DEFAULT_MONTHS)
    except (TypeError, ValueError):
        months = S.PAST_DEFAULT_MONTHS
    months = max(1, min(S.PAST_MAX_MONTHS, months))
    basis = "actual" if args.get("basis") == "actual" else "current"
    today = S.datetime.now(S.ZoneInfo("Asia/Seoul")).date()
    target = add_months(today, -months)
    years = months // 12 + 2
    stocks = S.get_holdings_view(req).get("stocks", [])
    kr = sorted(r["심볼"] for r in stocks if r["통화"] == "KRW")
    info = S.call_with_reauth(req.session, lambda token: S.lookup_stocks(token, kr)) if kr else {}
    rows, yahoo = [], {}
    for r in stocks:
        ysym = toss_to_yahoo_symbol(r["심볼"], (info.get(r["심볼"]) or {}).get("market"), r["통화"])
        rows.append({"symbol": r["심볼"], "name": r["종목"], "currency": r["통화"], "qty": r["수량"], "value_now_krw": r["평가금액(원)"] or 0.0})
        yahoo[r["심볼"]] = ysym
    fetched = parallel_map(lambda s: S.yahoo_closes(yahoo[s], years) if yahoo[s] else None, [r["symbol"] for r in rows], S.FACTOR_WORKERS)
    closes_of = dict(zip([r["symbol"] for r in rows], fetched))
    try:
        fx = fx_lookup(S.get_yahoo_daily_history("KRW=X", years=years)["bars"])
    except (S.requests.RequestException, ValueError, KeyError):
        fx = lambda day: None                                                                  # noqa: E731  (환율을 못 받으면 달러 종목은 시세 없음으로 빠진다)
    fills = S.all_fills(req.session) if basis == "actual" else None
    out = past_value(rows, closes_of, fx, fills, basis, target, today)
    out["months_ago"] = months
    return out
