"""종목 전망·ETF 구성 종목 분해 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from common import parallel_map
from etf_breakdown import breakdown as etf_breakdown
from long_history import long_source_for
from macro import FACTORS
from macro import explain_asset
from outlook import build_outlook
from portfolio import load_rebalance_config

from ._server import S

if TYPE_CHECKING:
    from server import Request


def outlook_view(req: Request, args: dict | None = None) -> dict:
    """종목별 앞으로의 상승/하락 뷰 재료(과거 유사 국면 통계 + 요인 민감도 + 변동성 범위). 읽기 전용, 예측이 아니다.
    args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 12개), 없으면 보유·관심 종목 전체."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    categories = load_rebalance_config(req.store)["targets"]
    entries, focused = S.focus_with_extras(req, entries, categories, focus)
    omitted += max(0, len(entries) - S.OUTLOOK_MAX_SYMBOLS)
    entries = entries[:S.OUTLOOK_MAX_SYMBOLS]
    asset_symbols = [e["yahoo"] for e in entries if e["yahoo"]]
    factor_symbols = [f[0] for f in FACTORS]
    long_closes = dict(zip(asset_symbols, parallel_map(lambda s: S.yahoo_closes(s, S.OUTLOOK_HISTORY_YEARS), asset_symbols, 4)))
    factor_closes = dict(zip(factor_symbols, parallel_map(S.yahoo_closes, factor_symbols, S.FACTOR_WORKERS)))
    factor_series = {s: c for s, c in factor_closes.items() if c}
    factor_series.update(S.fred_factor_series(req.session))
    sources = {e["yahoo"]: long_source_for(e["yahoo"]) for e in entries if e["yahoo"]}     # 상장이 최근인 종목은 더 긴 대용 시세로 과거를 센다
    proxy_symbols = sorted({s["symbol"] for s in sources.values() if s and s["kind"] == "daily"})
    proxy_closes = dict(zip(proxy_symbols, parallel_map(lambda s: S.yahoo_closes(s, S.LONG_PROXY_YEARS), proxy_symbols, 4)))
    gold = None
    if any(s and s["kind"] == "monthly_gold" for s in sources.values()):
        try:
            gold = S.fetch_gold_monthly()
        except (S.requests.RequestException, ValueError):
            gold = None
    items = []
    for e in entries:
        series = long_closes.get(e["yahoo"]) if e["yahoo"] else None
        source = sources.get(e["yahoo"]) if e["yahoo"] else None
        long_series = long_error = None
        if source and source["kind"] == "monthly_gold":
            long_series, long_error = ({"kind": "monthly", "series": gold, "label": source["label"]} if gold else None), source["label"]
        elif source:
            closes = proxy_closes.get(source["symbol"])
            long_series, long_error = ({"kind": "daily", "series": closes, "label": source["label"]} if closes else None), source["label"]
        result = build_outlook(series, S.factors_for(e["yahoo"], factor_series), long_series, long_error) if series else {"status": "no_data", "history_days": 0}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), **result})
    return {"items": items, "focus": focus, "focused": focused, "omitted": omitted}


def etf_breakdown_view(req: Request, args: dict | None = None) -> dict:
    """보유·관심 ETF의 이번 주 등락을 상위 편입 종목의 기여로 나누고, 큰 기여 종목은 매크로 요인과 보도까지 붙인다. 읽기 전용.
    args["focus"]가 있으면 그 종목·카테고리의 ETF만. 편입 정보가 없는 종목(개별주·금/채권 ETF·한국 ETF 등)은 no_holdings로 알린다.
    야후 조회어에는 종목 코드만, 뉴스 조회어에는 이름·코드만 쓰고 수량·금액·계좌 정보는 보내지 않는다."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    categories = load_rebalance_config(req.store)["targets"]
    entries, focused = S.focus_with_extras(req, entries, categories, focus)
    entries = [e for e in entries if e["yahoo"]]
    holdings = dict(zip((e["symbol"] for e in entries), parallel_map(lambda e: S.fetch_top_holdings(e["yahoo"]), entries, 4)))
    etfs = [e for e in entries if holdings[e["symbol"]]["status"] == "ok"]
    no_holdings = [{"symbol": e["symbol"], "name": e["name"], "reason": holdings[e["symbol"]]["status"]}
                   for e in entries if holdings[e["symbol"]]["status"] != "ok"]
    omitted += max(0, len(etfs) - S.ETF_MAX_BREAKDOWNS)
    etfs = etfs[:S.ETF_MAX_BREAKDOWNS]
    constituents = sorted({h["symbol"] for e in etfs for h in holdings[e["symbol"]]["holdings"]})
    own_symbols = [e["yahoo"] for e in etfs]
    fetch_list = list(dict.fromkeys(own_symbols + constituents))
    closes = dict(zip(fetch_list, parallel_map(S.yahoo_closes, fetch_list, 6)))
    items = []
    for e in etfs:
        series = closes.get(e["yahoo"])
        result = etf_breakdown(series, holdings[e["symbol"]]["holdings"], {s: c for s, c in closes.items() if c}) if series else {"status": "no_data"}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), **result, "drivers": {}, "headlines": {}})
    leaders = {i["symbol"]: [r for r in i.get("rows", []) if r["contribution_pct"] is not None][:S.ETF_TOP_CONTRIBUTORS]
               for i in items if i["status"] == "ok"}
    if any(leaders.values()):   # 큰 기여 종목의 요인 분석(기존 매크로 회귀)과 보도
        factor_closes = dict(zip((f[0] for f in FACTORS), parallel_map(S.yahoo_closes, [f[0] for f in FACTORS], S.FACTOR_WORKERS)))
        factor_series = {s: c for s, c in factor_closes.items() if c}
        factor_series.update(S.fred_factor_series(req.session))
        news_targets, seen = [], set()
        for item in items:
            for row in leaders.get(item["symbol"], []):
                explained = explain_asset(closes[row["symbol"]], S.factors_for(row["symbol"], factor_series)) if closes.get(row["symbol"]) else {"status": "no_data"}
                item["drivers"][row["symbol"]] = {"status": explained["status"], "r2": explained.get("r2"),
                                                  "contributions": explained.get("contributions", [])[:2]}
            for row in leaders.get(item["symbol"], [])[:S.ETF_NEWS_PER_ETF]:
                if row["symbol"] not in seen and len(news_targets) < S.ETF_NEWS_MAX_SYMBOLS:
                    seen.add(row["symbol"])
                    news_targets.append({"symbol": row["symbol"], "name": row["name"], "use_yahoo": True})
        found = S.headlines_for_many(news_targets) if news_targets else {}
        for item in items:
            for row in leaders.get(item["symbol"], [])[:S.ETF_NEWS_PER_ETF]:
                if row["symbol"] in found:
                    item["headlines"][row["symbol"]] = found[row["symbol"]]
    return {"items": items, "no_holdings": no_holdings, "focus": focus, "focused": focused, "omitted": omitted}
