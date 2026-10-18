"""ETF 상위 편입 종목과 비중(야후 파이낸스, yfinance의 funds_data). 이름·티커·비중만 다룬다.

야후가 공시한 상위 편입 종목(보통 10개)과 비중뿐이라 전체 구성이 아니고 공시 시점의 값이다. ETF가 아닌 종목·금/채권 ETF·
한국 ETF처럼 편입 정보가 없는 경우는 status="none"이다. yfinance가 없거나 조회가 실패하면 그 사실을 상태로 알리고 나머지는 계속한다.
조회어에는 종목 코드만 쓰고 계좌 정보는 보내지 않는다.
"""
from __future__ import annotations

import re
import threading
import time

HOLDINGS_CACHE_TTL = 24 * 3600
MAX_HOLDINGS = 15
NAME_MAX = 60
SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-=^]{0,14}$")

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def parse_top_holdings(frame) -> list[dict]:
    """funds_data.top_holdings(인덱스=티커, 열 Name·Holding Percent) → [{symbol, name, weight(0~1)}]. 이상한 행은 건너뛴다.
    비중 합이 1.5를 넘으면 퍼센트 값으로 보고 100으로 나눈다."""
    rows = []
    try:
        items = list(frame.iterrows())
    except AttributeError:
        return []
    for index, row in items:
        symbol = str(index or "").strip()
        try:
            weight = float(row["Holding Percent"])
        except (KeyError, TypeError, ValueError):
            continue
        if not SYMBOL_RE.match(symbol) or not (weight == weight) or weight <= 0:  # weight == weight: NaN 제외
            continue
        try:
            name = str(row["Name"] or "").strip()[:NAME_MAX]
        except KeyError:
            name = ""
        rows.append({"symbol": symbol.upper(), "name": name or symbol, "weight": weight})
    rows = rows[:MAX_HOLDINGS]
    if sum(r["weight"] for r in rows) > 1.5:
        rows = [{**r, "weight": r["weight"] / 100} for r in rows]
    return [r for r in rows if r["weight"] <= 1]


def _default_ticker(symbol: str):
    import yfinance  # 지연 import: 없으면 호출한 쪽이 'unavailable'로 처리한다
    return yfinance.Ticker(symbol)


def fetch_top_holdings(symbol: str, ticker_factory=None) -> dict:
    """한 ETF의 상위 편입 종목. status: ok | none(편입 정보 없음) | unavailable(yfinance 미설치) | error(조회 실패).
    ok·none은 하루 캐시한다(error·unavailable은 다음 질문에서 다시 시도)."""
    key = symbol.upper()
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < HOLDINGS_CACHE_TTL:
            return {**hit[1], "holdings": [dict(h) for h in hit[1]["holdings"]]}
    try:
        frame = (ticker_factory or _default_ticker)(symbol).funds_data.top_holdings
    except ImportError:
        return {"status": "unavailable", "holdings": []}
    except Exception as e:  # yfinance는 자체 예외(YFDataException 등)를 던진다. 데이터가 없다는 뜻과 일시 오류를 구분한다.
        text = str(e).lower()
        status = "none" if ("no fund data" in text or "not a fund" in text or "no data found" in text) else "error"
        result = {"status": status, "holdings": []}
    else:
        holdings = parse_top_holdings(frame)
        result = {"status": "ok" if holdings else "none", "holdings": holdings}
    if result["status"] in ("ok", "none"):
        with _cache_lock:
            _cache[key] = (now, result)
    return {**result, "holdings": [dict(h) for h in result["holdings"]]}
