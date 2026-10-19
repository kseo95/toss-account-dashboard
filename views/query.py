"""범용 자산 조회 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_query import DIVIDEND_FIELDS
from data_query import FILL_FIELDS
from data_query import build_assets
from data_query import run_query
from portfolio import load_rebalance_config

from ._server import S

if TYPE_CHECKING:
    from server import Request


def query_data_view(req: Request, args: dict) -> dict:
    """자산 표 조회. 체결 내역·배당 추정이 필요한 항목(첫 매수일·매수 횟수·보유 일수·배당)을 쓸 때만 그걸 가져온다."""
    used = {args.get("group_by"), args.get("field"), args.get("filter_field")}
    need_dividends = bool(used & set(DIVIDEND_FIELDS))
    today = S.datetime.now(S.ZoneInfo("Asia/Seoul")).date()
    fills = S.all_fills(req.session) if (used & set(FILL_FIELDS)) or need_dividends else None
    dividends = None
    if need_dividends:
        try:
            dividends = S.dividends_for_year(req.session, today.year)
        except (S.requests.RequestException, ValueError, KeyError):
            dividends = None
    rows = build_assets(S.get_holdings_view(req), load_rebalance_config(req.store), fills, dividends, today)
    out = run_query(rows, args)
    if need_dividends and dividends is None:
        out = {"error": "올해 배당 추정을 계산하지 못했습니다(환율·배당 기록을 받지 못함)."}
    return out
