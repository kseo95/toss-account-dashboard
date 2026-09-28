"""관심종목과 MA 감시.

MA 감시는 지표를 MA 하나로 하드코딩하지 않기 위해 (일봉/주봉, 기간, 근접 기준%)를 전부 사용자가 화면에서
만든 규칙에서 가져온다.
"""
from __future__ import annotations

import requests

from common import MA_INTERVALS, MA_MAX_PERIOD, SYMBOL_PATTERN, parallel_map, to_float
from storage import UserStore
from toss_api import TOSS_MAX_WORKERS, fetch_close_history, get_prices, lookup_stocks

WATCHLIST_FILE = "watchlist.json"
WATCHLIST_MAX = 30
MA_RULES_FILE = "ma_rules.json"
MA_RULES_MAX = 30


def _unit(interval: str) -> str:
    return "일" if interval == "day" else "주"


def _closes_or_empty(token: str, symbol: str, days: int = 0, weeks: int = 0) -> tuple[list[float], list[float]]:
    try:
        return fetch_close_history(token, symbol, days=days, weeks=weeks)
    except requests.exceptions.RequestException:
        return [], []


# ---------- 관심종목 ----------
def load_watchlist(store: UserStore) -> list[str]:
    data = store.read_json(WATCHLIST_FILE)
    symbols = data.get("symbols", []) if isinstance(data, dict) else []
    return [s for s in symbols if isinstance(s, str) and s]


def save_watchlist(store: UserStore, symbols: list[str]) -> None:
    store.write_json(WATCHLIST_FILE, {"symbols": symbols})


def fetch_watchlist_rows(token: str, symbols: list[str]) -> list[dict]:
    """관심종목의 현재가 + 전일대비 등락률."""
    if not symbols:
        return []
    info = lookup_stocks(token, symbols)
    prices = get_prices(token, symbols)
    daily = parallel_map(lambda s: _closes_or_empty(token, s, days=2)[0], symbols, TOSS_MAX_WORKERS)
    rows = []
    for sym, closes in zip(symbols, daily):
        meta = info.get(sym, {})
        cur_price = prices.get(sym, 0.0)
        prev_close = closes[1] if len(closes) >= 2 else None
        rows.append({
            "symbol": sym,
            "name": meta.get("name", sym),
            "currency": meta.get("currency", "?"),
            "price": cur_price,
            "change_pct": ((cur_price - prev_close) / prev_close * 100) if prev_close else 0.0,
        })
    return rows


# ---------- MA 감시 ----------
def load_ma_rules(store: UserStore) -> list[dict]:
    data = store.read_json(MA_RULES_FILE)
    clean = []
    for r in (data.get("rules", []) if isinstance(data, dict) else []):
        if not isinstance(r, dict):
            continue
        symbol = str(r.get("symbol", "")).strip().upper()
        interval, period, proximity_pct = r.get("interval"), r.get("period"), r.get("proximity_pct")
        if symbol and interval in MA_INTERVALS and isinstance(period, (int, float)) and isinstance(proximity_pct, (int, float)):
            clean.append({"symbol": symbol, "interval": interval, "period": int(period), "proximity_pct": to_float(proximity_pct)})
    return clean


def save_ma_rules(store: UserStore, rules: list[dict]) -> None:
    store.write_json(MA_RULES_FILE, {"rules": rules})


def validate_ma_rules(rules) -> tuple[list[dict], str | None]:
    """MA 규칙 목록을 검증/정규화한다. (rules, None)이면 통과, (빈 목록, 에러메시지)면 실패."""
    if not isinstance(rules, list):
        return [], "rules는 배열이어야 합니다."
    if len(rules) > MA_RULES_MAX:
        return [], f"MA 규칙은 최대 {MA_RULES_MAX}개까지 등록할 수 있습니다."
    clean: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            return [], "규칙 형식이 올바르지 않습니다."
        symbol = str(r.get("symbol", "")).strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            return [], f"잘못된 종목 코드입니다: {r.get('symbol')!r}"
        interval = r.get("interval")
        if interval not in MA_INTERVALS:
            return [], "봉 종류는 일봉/주봉이어야 합니다."
        max_period = MA_MAX_PERIOD[interval]
        period = to_float(r.get("period"), -1)
        if not (period.is_integer() and 2 <= period <= max_period):
            return [], f"기간은 2~{max_period}{_unit(interval)} 사이의 정수여야 합니다."
        proximity_pct = to_float(r.get("proximity_pct"), -1)
        if not (0 <= proximity_pct <= 100):
            return [], "근접 기준%는 0~100 사이 숫자여야 합니다."
        clean.append({"symbol": symbol, "interval": interval, "period": int(period), "proximity_pct": proximity_pct})
    return clean, None


def compute_ma_rows(token: str, rules: list[dict]) -> list[dict]:
    """MA 규칙별로 이동평균 대비 현재가 이격도를 계산한다."""
    if not rules:
        return []
    symbols = sorted({r["symbol"] for r in rules})
    info = lookup_stocks(token, symbols)
    prices = get_prices(token, symbols)

    # 종목마다 캔들은 한 번만(일봉·주봉 중 가장 긴 기간 기준으로) 받는다.
    need = {s: {"day": 0, "week": 0} for s in symbols}
    for r in rules:
        need[r["symbol"]][r["interval"]] = max(need[r["symbol"]][r["interval"]], r["period"])
    history = dict(zip(symbols, parallel_map(
        lambda s: _closes_or_empty(token, s, days=need[s]["day"], weeks=need[s]["week"]), symbols, TOSS_MAX_WORKERS)))

    rows = []
    for r in rules:
        symbol, interval, period = r["symbol"], r["interval"], r["period"]
        meta = info.get(symbol, {})
        daily, weekly = history[symbol]
        closes = daily if interval == "day" else weekly
        cur_price = prices.get(symbol, 0.0)
        row = {"symbol": symbol, "name": meta.get("name", symbol), "currency": meta.get("currency", "?"),
               "interval": interval, "period": period, "proximity_pct": r["proximity_pct"]}
        if len(closes) < period or cur_price <= 0:
            row.update(ok=False, reason=f"데이터 부족 ({min(len(closes), period)}/{period}{_unit(interval)})")
        else:
            ma_value = sum(closes[:period]) / period
            diff_pct = (cur_price - ma_value) / ma_value * 100 if ma_value else 0.0
            row.update(ok=True, price=cur_price, ma_value=ma_value, diff_pct=diff_pct, hit=abs(diff_pct) <= r["proximity_pct"])
        rows.append(row)
    return rows
