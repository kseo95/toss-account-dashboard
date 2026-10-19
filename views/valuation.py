"""밸류에이션 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from common import parallel_map
from fundamentals import assess_etf
from fundamentals import assess_stock
from fundamentals import is_korean as is_korean_stock
from portfolio import load_rebalance_config

from ._server import S

if TYPE_CHECKING:
    from server import Request


def valuation_view(req: Request, args: dict | None = None) -> dict:
    """보유·관심 종목의 PER·PSR·PBR·EPS·BPS·ROE와 비싼가·싼가 판단(개별주는 같은 업종 S&P500 표본·성장·수익성 대비, 주식형 ETF는 시장 대비). 읽기 전용.
    야후 비공식 데이터이며 상대 평가다. 조회어에는 종목 코드만 쓴다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 4개)."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    entries, focused = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - S.VALUATION_MAX)
    entries = entries[:S.VALUATION_MAX]
    got = parallel_map(lambda e: S.fetch_fundamentals(e["yahoo"]), entries, S.VALUATION_WORKERS)
    bench = None
    if any(g["status"] == "ok" and g["metrics"]["kind"] == "etf" for g in got):
        b = S.fetch_fundamentals(S.VALUATION_BENCHMARK)
        bench = b["metrics"] if b["status"] == "ok" else None
    items = []
    for e, g in zip(entries, got):
        item = {"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "status": g["status"]}
        if g["status"] != "ok":
            items.append(item)
            continue
        m = g["metrics"]
        peer_n = 0
        if m["kind"] == "stock":
            stats = None
            if m["sector"] and not is_korean_stock(e["yahoo"], m.get("currency")):
                stats, peer_n = S.sector_peer_stats(m["sector"], e["yahoo"])
            assessment = assess_stock(m, stats)
        elif m["kind"] == "etf":
            assessment = assess_etf(m, bench)
        else:
            assessment = {"signals": [], "verdict": {"label": "na", "expensive": 0, "cheap": 0, "signals": 0}, "reason": "other", "notes": ["주식·ETF가 아닌 종목은 밸류에이션 대상이 아닙니다"]}
        item.update({"kind": m["kind"], "metrics": {k: m[k] for k in ("per", "forward_per", "psr", "pbr", "eps", "forward_eps", "bps", "roe", "peg", "price", "currency", "category", "sector", "flags")},
                     "assessment": assessment, "peer_n": peer_n})
        items.append(item)
    return {"items": items, "omitted": omitted, "focus": focus, "focused": focused}
