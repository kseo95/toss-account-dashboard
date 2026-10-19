"""매크로 요인·사례집·요인 순위·요인 반응·종목 뉴스 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from cases import build as build_cases
from common import ApiError
from common import parallel_map
from long_history import event_source_for
from macro import FACTORS
from macro import explain_asset
from macro import rank_drivers
from macro import weekly_factor_moves
from portfolio import load_rebalance_config
from response import DEFAULT_ASSETS as DEFAULT_RESPONSE_ASSETS
from response import DEFAULT_HORIZON as DEFAULT_RESPONSE_HORIZON
from response import HORIZONS as RESPONSE_HORIZONS
from response import MAX_ASSETS as MAX_RESPONSE_ASSETS
from response import analyze as analyze_response
from response import resolve_asset
from response import resolve_factor
from shocks import analyze as analyze_shocks

from ._server import S

if TYPE_CHECKING:
    from server import Request


def macro_drivers_view(req: Request, args: dict | None = None) -> dict:
    """이번 주 시장 요인(금리·달러·유가·VIX·환율·지수) 변화와, 보유·관심 종목 등락의 요인별 기여 추정. 읽기 전용.
    같이 움직인 정도의 통계 추정이며 인과의 증명이 아니다(설명력 R²를 함께 돌려준다). args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    entries, focused = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    symbols = [f[0] for f in FACTORS] + [e["yahoo"] for e in entries if e["yahoo"]]
    closes = dict(zip(symbols, parallel_map(S.yahoo_closes, symbols, S.FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(S.fred_factor_series(req.session))
    items = []
    for e in entries:
        series = closes.get(e["yahoo"]) if e["yahoo"] else None
        result = explain_asset(series, S.factors_for(e["yahoo"], factor_series)) if series else {"status": "no_data", "asset_change_pct": None}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "status": result["status"],
                      "weekly_change_pct": result.get("asset_change_pct"), "r2": result.get("r2"),
                      "drivers": result.get("contributions", []), "residual_pct": result.get("residual_pct")})
    items.sort(key=lambda x: (x["weekly_change_pct"] is None, -abs(x["weekly_change_pct"] or 0), not x["held"]))
    return {"factors": weekly_factor_moves(factor_series), "items": items, "omitted": omitted, "focus": focus, "focused": focused}


def macro_history_view(req: Request, args: dict | None = None) -> dict:
    """지금 큰 변동 중인 매크로 요인과, 과거 같은 방향의 큰 변동 때·이름 붙은 거시 이슈 때 보유·관심 종목이 어떻게 움직였는지. 읽기 전용.
    같이 움직인 기록이며 원인의 증명이 아니다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 6개).
    금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물·지수)으로 본다."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    entries, focused = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - S.MACRO_HISTORY_MAX)
    entries = entries[:S.MACRO_HISTORY_MAX]
    sources = {e["yahoo"]: event_source_for(e["yahoo"]) for e in entries}
    wanted = list(dict.fromkeys([f[0] for f in FACTORS] + [e["yahoo"] for e in entries] + [s["symbol"] for s in sources.values() if s]))
    closes = dict(zip(wanted, parallel_map(lambda s: S.yahoo_closes(s, S.LONG_PROXY_YEARS), wanted, S.FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(S.fred_factor_series(req.session))
    items = []
    for e in entries:
        own, source = closes.get(e["yahoo"]), sources.get(e["yahoo"])
        proxy = closes.get(source["symbol"]) if source else None
        used, label = (proxy, source["label"]) if proxy and (not own or len(proxy) > len(own)) else (own, None)
        result = analyze_shocks(used, S.factors_for(e["yahoo"], factor_series)) if used else {"status": "no_data", "history_days": 0}   # 요인을 못 받아도 이름 붙은 이슈 반응은 계산한다
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "source": label, **result})
    return {"items": items, "focus": focus, "focused": focused, "omitted": omitted}


def macro_cases_view(req: Request, args: dict | None = None) -> dict:
    """거시 사례집: 질문이 지정한 사례(국면별 서사 + 실제 시세로 계산한 금·금리·달러·증시 변화 + 통념 점검)와 지금과 닮은 과거 국면. 읽기 전용.
    args["case"]에 사례 이름·연도·사건 키워드가 있으면 그 사례들(최대 3개), args["focus"]에 종목이 있으면 그 종목의 국면별 변화와 그 뒤 3개월도 함께.
    FRED 키가 있으면 연방기금금리·실질금리·기대인플레이션도 쓴다. 같이 움직인 기록이며 인과의 증명이 아니다."""
    case_text = str((args or {}).get("case") or "").strip()[:300]
    focus = str((args or {}).get("focus") or "").strip()[:200]
    symbols = list(S.CASES_REFERENCE_SYMBOLS.values()) + ["GC=F"]
    closes = dict(zip(symbols, parallel_map(lambda s: S.yahoo_closes(s, S.LONG_PROXY_YEARS), symbols, 4)))
    refs = {key: closes.get(sym) for key, sym in S.CASES_REFERENCE_SYMBOLS.items()}
    refs.update(S.fred_factor_series(req.session))
    refs.update(S.fred_slow_series(req.session))        # CPI 전년비·실업률·연준 총자산(국면별 변화에만 쓴다)
    try:
        monthly_gold = S.fetch_gold_monthly()
    except (S.requests.RequestException, ValueError):
        monthly_gold = None
    asset = None
    if focus:
        entries, _omitted = S.portfolio_entries(req)
        matched, found = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
        entry = next((e for e in matched if e["yahoo"]), None) if found else None
        series = S.yahoo_closes(entry["yahoo"], S.LONG_PROXY_YEARS) if entry else None
        if entry and series:
            asset = {"name": entry["name"] or entry["symbol"], "series": series}
    out = build_cases({k: v for k, v in refs.items() if v}, closes.get("GC=F"), monthly_gold, case_text, asset)
    out["gold_available"] = bool(closes.get("GC=F") or monthly_gold)
    return out


def asset_drivers_view(req: Request, args: dict | None = None) -> dict:
    """이 종목이 평소 가장 민감한 매크로 요인 순위(1년·3년·10년 상관, 일관성, 같이 움직이는 요인 묶음). 읽기 전용, 같이 움직인 정도이며 원인의 증명이 아니다.
    args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 4개). 금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물·지수)으로 본다."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    entries, focused = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - S.ASSET_DRIVERS_MAX)
    entries = entries[:S.ASSET_DRIVERS_MAX]
    sources = {e["yahoo"]: event_source_for(e["yahoo"]) for e in entries}
    wanted = list(dict.fromkeys([f[0] for f in FACTORS] + [e["yahoo"] for e in entries] + [s["symbol"] for s in sources.values() if s]))
    closes = dict(zip(wanted, parallel_map(lambda s: S.yahoo_closes(s, S.ASSET_DRIVERS_YEARS), wanted, S.FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(S.fred_factor_series(req.session))
    items = []
    for e in entries:
        own, source = closes.get(e["yahoo"]), sources.get(e["yahoo"])
        proxy = closes.get(source["symbol"]) if source else None
        used, label = (proxy, source["label"]) if proxy and (not own or len(proxy) > len(own)) else (own, None)
        result = rank_drivers(used, S.factors_for(e["yahoo"], factor_series)) if used and factor_series else {"status": "no_data"}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "source": label, **result})
    return {"items": items, "focus": focus, "focused": focused, "omitted": omitted}


def factor_response_view(req: Request, args: dict | None = None) -> dict:
    """요인(금리·달러·유가·VIX·환율·구리)이 크게 오른/내린 구간에서 주요 자산이 평소와 어떻게 달랐는지(조건부 반응). 보유 종목과 무관한 시장 통계, 읽기 전용.
    args: factor(이름이나 심볼), direction("up"|"down"), assets(이름 목록, 비우면 금·나스닥·S&P500·코스피·장기채·비트코인), horizon_days(5|21|63)."""
    args = args if isinstance(args, dict) else {}
    found = resolve_factor(str(args.get("factor") or ""))
    if found is None:
        raise ApiError(400, "요인을 알 수 없습니다. 금리·달러·유가·VIX·환율·구리 중 하나를 말해 주세요.")
    symbol, label, kind = found
    names = [str(a) for a in (args.get("assets") or []) if isinstance(a, str)][:MAX_RESPONSE_ASSETS]
    chosen, unresolved = [], []
    for name in names:
        hit = resolve_asset(name)
        if hit is None:
            unresolved.append(name[:30])
        elif hit[0] != symbol and hit not in chosen:
            chosen.append(hit)
    chosen = chosen or [a for a in DEFAULT_RESPONSE_ASSETS if a[0] != symbol]
    wanted = [symbol] + [a[0] for a in chosen]
    closes = dict(zip(wanted, parallel_map(lambda s: S.yahoo_closes(s, S.RESPONSE_YEARS), wanted, S.FACTOR_WORKERS)))
    if not closes.get(symbol):
        raise ApiError(502, f"{label} 시세를 받지 못했습니다.")
    horizon = args.get("horizon_days") if args.get("horizon_days") in RESPONSE_HORIZONS else DEFAULT_RESPONSE_HORIZON
    out = analyze_response(closes[symbol], kind, {name: closes[sym] for sym, name in chosen if closes.get(sym)}, horizon)
    out["insufficient"] += [name for sym, name in chosen if not closes.get(sym)]
    out.update({"factor": {"symbol": symbol, "name": label, "kind": kind}, "direction": "down" if args.get("direction") == "down" else "up", "unresolved": unresolved})
    return out


def weekly_news_view(req: Request, args: dict | None = None) -> dict:
    """보유 종목(현금 취급 제외)과 관심종목의 최근 5거래일 등락률 + 최근 7일 뉴스 헤드라인. 읽기 전용.
    뉴스 조회어에는 종목 이름·코드만 쓰고 수량·금액·계좌 정보는 보내지 않는다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    entries, focused = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)

    def change(entry):
        if not entry["yahoo"]:
            return None
        try:
            return S.weekly_change_pct([bar[1] for bar in S.get_yahoo_daily_history(entry["yahoo"])["bars"]])
        except (S.requests.RequestException, ValueError, KeyError, IndexError):
            return None
    changes = parallel_map(change, entries, 4)
    headlines = S.headlines_for_many([{"symbol": e["symbol"], "name": e["name"], "use_yahoo": e["currency"] != "KRW"} for e in entries])
    items = [{"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "weekly_change_pct": c,
              "headlines": headlines[e["symbol"]]["headlines"], "news_error": headlines[e["symbol"]]["error"]}
             for e, c in zip(entries, changes)]
    items.sort(key=lambda x: (x["weekly_change_pct"] is None, -abs(x["weekly_change_pct"] or 0), not x["held"]))
    return {"items": items, "omitted": omitted, "focus": focus, "focused": focused}
