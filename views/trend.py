"""업종 추세·이동평균 스크리너 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import trend as trend_mod
from common import ApiError
from common import parallel_map
from screener import clean_periods
from screener import screen as run_screen

from ._server import S

if TYPE_CHECKING:
    from server import Request


def ma_screen_view(req: Request, args: dict | None = None) -> dict:
    """주봉 이동평균 스크리너: S&P500 구성 종목(또는 내 보유·관심 종목) 중 종가가 지정한 주봉 MA 여러 개의 모두(또는 하나라도) 아래·위에 있는 종목. 읽기 전용.
    args: universe("sp500"|"mine"), periods(예: [60,120,200,240]), side("below"|"above"), match("all"|"any"), sector(영문 GICS 업종, 선택).
    S&P500은 500개 시세를 받아 첫 조회에 30~60초가 걸리고 이후에는 캐시된다. 같이 움직인 위치 정보일 뿐 매매 신호가 아니다."""
    args = args if isinstance(args, dict) else {}
    mine = args.get("universe") == "mine"
    periods = clean_periods(args.get("periods"))
    sector = str(args.get("sector") or "").strip()[:40]
    if mine:
        entries, _omitted = S.portfolio_entries(req)
        universe = [{"symbol": e["yahoo"], "name": e["name"] or e["symbol"], "sector": ""} for e in entries if e["yahoo"]]
    else:
        try:
            universe = S.fetch_sp500()
        except S.ScreenerError as e:
            raise ApiError(502, str(e))

    def fetch(symbol):
        try:
            bars = S.get_yahoo_daily_history(symbol, years=S.SCREEN_YEARS)["bars"]
        except (S.requests.RequestException, ValueError, KeyError, IndexError):
            return None
        return S.weekly_closes(bars), (bars[-1][0] if bars else None)
    wanted = [u["symbol"] for u in universe if not sector or sector.lower() in str(u.get("sector") or "").lower()]
    got = dict(zip(wanted, parallel_map(fetch, wanted, S.SCREEN_WORKERS)))
    weekly = {s: v[0] for s, v in got.items() if v is not None}
    as_of = max((v[1] for v in got.values() if v and v[1]), default=None)
    out = run_screen(universe, weekly, periods, str(args.get("side") or "below"), str(args.get("match") or "all"), sector or None)
    out.update({"universe_name": "mine" if mine else "sp500", "sector": sector, "as_of": as_of.isoformat() if as_of else None,
                "total_matches": len(out["matches"])})
    out["symbols"] = [m["symbol"] for m in out["matches"]][:300]       # 표에 넣지 못한 종목도 코드는 모두 알려 준다
    out["matches"] = out["matches"][:S.SCREEN_MAX_MATCHES]
    return out


def trend_view(req: Request, args: dict | None = None) -> dict:
    """미국 업종 ETF(SPDR 11개) 또는 내 보유·관심 종목의 추세 사다리와 전환 상태. 읽기 전용, 위치·방향 정보일 뿐 매매 신호가 아니다.
    args: universe("sectors"|"mine"), focus("all"|"short"|"mid"|"long": 보이는 순서만 바꾼다), 이번 질문에서만 판정 기준을 바꾸는 값(buffer_pct 등, 올바르지 않으면 무시).
    기본 기준은 trend.DEFAULTS(버퍼 ±2%, 연속 2주 확인, 21·63·252거래일, 50·200일선)."""
    args = args if isinstance(args, dict) else {}
    mine = args.get("universe") == "mine"
    cfg = trend_mod.clean_settings(trend_mod.valid_overrides(args))
    if mine:
        entries, _omitted = S.portfolio_entries(req)
        listed = [(e["yahoo"], e["name"] or e["symbol"]) for e in entries if e["yahoo"]][:S.TREND_UNIVERSE_MAX]
    else:
        listed = list(trend_mod.SECTOR_ETFS)
    names = dict(listed)
    wanted = list(dict.fromkeys([s for s, _ in listed] + [trend_mod.BENCHMARK[0]]))
    closes = dict(zip(wanted, parallel_map(lambda s: S.yahoo_closes(s, S.TREND_YEARS), wanted, S.FACTOR_WORKERS)))

    def ordered(series):
        return [series[d] for d in sorted(series)] if series else []
    out = trend_mod.analyze({s: ordered(closes.get(s)) for s, _ in listed if closes.get(s)}, names, cfg, ordered(closes.get(trend_mod.BENCHMARK[0])) or None,
                            str(args.get("focus") or "all"))
    out["insufficient"] += [s for s, _ in listed if not closes.get(s)]     # 시세를 못 받은 종목도 '판단하지 못함'으로 알린다
    last_dates = [max(c) for c in closes.values() if c]
    out.update({"universe": "mine" if mine else "sectors", "as_of": max(last_dates).isoformat() if last_dates else None})
    return out
