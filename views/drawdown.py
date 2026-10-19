"""고점 대비 낙폭 원인 분석 도구 실행(*_view). server.py에서 나눈 모듈이며 동작은 같다. server.py의 이름은 S.이름으로 부른다(테스트가 server의 이름을 바꿔 끼워도 그대로 먹도록 호출할 때 찾는다)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from causes import rank_themes
from common import parallel_map
from macro import FACTORS
from macro import explain_asset
from macro import factor_group
from macro import weekly_factor_moves
from macro import window_pressure
from portfolio import load_rebalance_config

from ._server import S

if TYPE_CHECKING:
    from server import Request


def drawdown_view(req: Request, args: dict | None = None) -> dict:
    """고점 대비 얼마나 빠졌고 왜 빠졌나: 종목의 전 고점(상장 이후)과 최근 1년 고점 대비 낙폭, 최근 고점 날짜부터 지금까지 같은 구간에서
    시장 요인(금리·달러·유가·VIX·환율·지수)이 얼마나 움직였는지와 종목 하락의 요인별 기여 추정(최근 1년 일간 관계의 계수 × 구간 요인 변화). 읽기 전용.
    같이 움직인 정도의 통계 추정이며 인과의 증명이 아니다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 4개)."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = S.portfolio_entries(req)
    entries, focused = S.focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - S.DRAWDOWN_MAX)
    entries = entries[:S.DRAWDOWN_MAX]
    wanted = list(dict.fromkeys([f[0] for f in FACTORS] + [e["yahoo"] for e in entries]))
    closes = dict(zip(wanted, parallel_map(lambda sym: S.yahoo_closes(sym, S.DRAWDOWN_YEARS if sym in {e["yahoo"] for e in entries} else None), wanted, S.FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(S.fred_factor_series(req.session))
    items = []
    for e in entries:
        series = closes.get(e["yahoo"])
        item = {"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "status": "no_data"}
        if series and len(series) > 2:
            dates = sorted(series)
            last = dates[-1]
            ath_date = max(dates, key=lambda d: series[d])
            recent = dates[-S.DRAWDOWN_WINDOW_DAYS:]
            peak_date = max(recent, key=lambda d: series[d])
            since_days = len([d for d in dates if d > peak_date])
            low_date = min((d for d in dates if d >= peak_date), key=lambda d: series[d])
            item.update({"last_date": last.isoformat(), "last_close": series[last], "peak_date": peak_date.isoformat(), "peak_close": series[peak_date],
                         "drawdown_pct": (series[last] / series[peak_date] - 1) * 100, "since_days": since_days,
                         "ath_date": ath_date.isoformat(), "ath_close": series[ath_date], "ath_drawdown_pct": (series[last] / series[ath_date] - 1) * 100,
                         "low_date": low_date.isoformat(), "low_drawdown_pct": (series[low_date] / series[peak_date] - 1) * 100})
            if since_days < S.DRAWDOWN_MIN_DAYS or series[last] >= series[peak_date]:
                item["status"] = "near_peak"
            else:
                factors = S.factors_for(e["yahoo"], factor_series)
                macro_only = {k: v for k, v in factors.items() if factor_group(k) == "macro"}    # 먼저 매크로 요인(금리·달러·유가·VIX…)만으로: 은·지수 같은 같은 계열 자산이 설명을 가로채지 않게
                result, basis = explain_asset(series, macro_only, since=peak_date), "macro"
                if result["status"] != "ok":                                                    # 매크로만으로 설명이 안 되면 시장 전체(지수·업종·귀금속 등)까지 넣어 본다
                    wider = explain_asset(series, factors, since=peak_date)
                    if wider["status"] == "ok":
                        result, basis = wider, "market"
                moves = [{**m, "group": factor_group(m["symbol"])} for m in weekly_factor_moves(factors, since=peak_date)]
                item["drop_days"] = S.big_drop_days(e, series, peak_date, factors)
                item["causes"] = rank_themes(item["drop_days"])
                item["series"] = S.price_series(series, peak_date, [S.date.fromisoformat(d["date"]) for d in item["drop_days"]])
                for day in item["drop_days"]:
                    day.pop("pool", None)
                pressure = window_pressure(series, macro_only, peak_date)                       # 회귀에 안 잡힌 요인도 평소 관계로 한 번 더 본다(매크로 요인만)
                item.update({"pressure": pressure[:6],
                             "status": result["status"], "reason": result.get("reason", ""), "r2": result.get("r2"), "oos_r2": result.get("oos_r2"), "basis": basis,
                             "drivers": result.get("contributions", []), "residual_pct": result.get("residual_pct"), "moves": moves})
        items.append(item)
    return {"items": items, "omitted": omitted, "focus": focus, "focused": focused}
