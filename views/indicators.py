"""보조지표 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from common import parallel_map
from indicators import INDICATORS
from indicators import analyze as analyze_indicators
from portfolio import load_rebalance_config

from ._server import S

if TYPE_CHECKING:
    from server import Request


def indicators_view(req: Request, args: dict | None = None) -> dict:
    """RSI·MACD·볼린저밴드·이동평균(50/200일) 교차와 상태, 그래프용 최근 120거래일 시계열. 보유·관심종목이 아니어도 이름·코드로 찾는다(최대 4개). 읽기 전용.
    args: focus(종목), indicators(["rsi","macd","bollinger","ma_cross"] 중 일부, 비우면 전부), rsi_period·overbought·oversold·bb_period·bb_k(기준 바꾸기, 범위 안의 값만)."""
    args = args if isinstance(args, dict) else {}
    focus = str(args.get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    entries, focused = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - S.INDICATOR_MAX)
    entries = entries[:S.INDICATOR_MAX]
    closes = parallel_map(lambda e: S.yahoo_closes(e["yahoo"], S.INDICATOR_YEARS), entries, S.FACTOR_WORKERS)
    wanted = [k for k in (args.get("indicators") or []) if k in INDICATORS] or list(INDICATORS)
    items = []
    for e, c in zip(entries, closes):
        item = {"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "status": "no_data"}
        if c:
            dates = sorted(c)
            got = analyze_indicators(dates, [c[d] for d in dates], args)
            if got:
                item.update(got)
                item["status"] = "ok"
        items.append(item)
    return {"items": items, "omitted": omitted, "focus": focus, "focused": focused, "indicators": wanted}
