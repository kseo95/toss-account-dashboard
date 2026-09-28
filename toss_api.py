"""토스증권 Open API 호출 + 외부 시세(네이버 자동완성, Yahoo USD/JPY).

여기 함수들은 토큰을 받아 원자료만 돌려준다. 계산(비중, 전략 등)은 각 기능 모듈이 한다.
"""
from __future__ import annotations

import threading
import time
from datetime import date

import requests

from common import SYMBOL_PATTERN, fmt_as_of, parallel_map, to_float

API_BASE = "https://openapi.tossinvest.com"
TOSS_MAX_WORKERS = 4  # 동시에 보내는 토스 요청 수. 초당 한도(429)는 api_get이 잠깐 쉬고 재시도한다.
STOCK_INFO_CACHE_TTL = 600.0  # 종목 기본정보는 잘 안 바뀌어서 캐시(자동완성이 한도를 소진하지 않게)
CANDLE_CACHE_TTL = 300.0  # 일봉 기록 캐시. MA 감시·분할매수·관심종목이 같은 종목 캔들을 다시 받지 않게.
CANDLE_MAX_PAGES = 8  # 페이지당 200일 → 최대 ~1,600영업일(주봉 300개 ≈ 6년치)
USD_JPY_REFRESH_INTERVAL = 60  # 초. 매 조회마다 외부 API를 부르지 않도록 이 주기로만 갱신.


def get_access_token(app_key: str, app_secret: str) -> str:
    """토스증권 Open API에 앱키/시크릿으로 access_token을 발급받는다."""
    resp = requests.post(
        f"{API_BASE}/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": app_key, "client_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(path: str, token: str, params: dict | None = None, account_seq: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if account_seq:
        headers["X-Tossinvest-Account"] = str(account_seq)
    # 토스 API의 초당 요청 제한(429)에 걸리면 잠깐 쉬었다 재시도한다(문서상 "매초 갱신").
    last_exc: requests.HTTPError | None = None
    for attempt in range(3):
        resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=10)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        last_exc = requests.HTTPError(response=resp)
        time.sleep(1.0 + attempt * 0.5)
    raise last_exc


def get_accounts(token: str) -> list[dict]:
    return api_get("/api/v1/accounts", token).get("result", [])


def get_holdings(token: str, account_seq: str) -> dict:
    return api_get("/api/v1/holdings", token, account_seq=account_seq).get("result", {})


def get_usd_krw_rate(token: str) -> float:
    data = api_get("/api/v1/exchange-rate", token, params={"baseCurrency": "USD", "quoteCurrency": "KRW"}).get("result", {})
    return to_float(data.get("rate"))


def get_buying_power(token: str, account_seq: str, currency: str) -> float:
    data = api_get("/api/v1/buying-power", token, params={"currency": currency}, account_seq=account_seq).get("result", {})
    return to_float(data.get("cashBuyingPower"))


def get_stock_info(token: str, symbols: list[str]) -> dict[str, dict]:
    """심볼 목록의 기본정보(이름/시장/통화/상태). 존재하지 않는 심볼은 결과에 없다."""
    if not symbols:
        return {}
    result = api_get("/api/v1/stocks", token, params={"symbols": ",".join(symbols)}).get("result", [])
    return {item["symbol"]: item for item in result}


def get_prices(token: str, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    result = api_get("/api/v1/prices", token, params={"symbols": ",".join(symbols)}).get("result", [])
    return {item["symbol"]: to_float(item.get("lastPrice")) for item in result}


def get_daily_candles(token: str, symbol: str, count: int = 2, before: str | None = None) -> dict:
    params = {"symbol": symbol, "interval": "1d", "count": count}
    if before:
        params["before"] = before
    return api_get("/api/v1/candles", token, params=params).get("result", {})


ORDERS_PAGE_LIMIT = 100  # API 최대
ORDERS_MAX_PAGES = 50    # 계좌당 최대 5,000건까지만(무한 루프 방지)


def get_closed_orders(token: str, account_seq: str, date_from: str, date_to: str) -> list[dict]:
    """종료된 주문(체결·취소·거부 등) 전체, 주문일(KST) date_from~date_to. 커서로 끝까지 넘긴다.
    Open API가 지원하는 호가 유형(지정가·시장가·장마감지정가)으로 넣은 주문만 나온다(시간외 종가 등은 빠짐)."""
    orders: list[dict] = []
    cursor = None
    for _ in range(ORDERS_MAX_PAGES):
        params = {"status": "CLOSED", "from": date_from, "to": date_to, "limit": ORDERS_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        result = api_get("/api/v1/orders", token, params=params, account_seq=account_seq).get("result", {})
        orders.extend(result.get("orders", []))
        cursor = result.get("nextCursor")
        if not result.get("hasNext") or not cursor:
            break
    return orders


def fetch_account_snapshot(token: str, accounts: list[dict]) -> dict:
    """계좌 원자료: 환율 + 계좌별 예수금(원/달러) + 보유종목. 요청들을 병렬로 보낸다."""
    jobs = [("fx", None)] + [(kind, a["accountSeq"]) for a in accounts for kind in ("KRW", "USD", "holdings")]

    def run(job):
        kind, seq = job
        if kind == "fx":
            return get_usd_krw_rate(token)
        if kind == "holdings":
            return get_holdings(token, seq).get("items", [])
        return get_buying_power(token, seq, kind)

    results = dict(zip(jobs, parallel_map(run, jobs, TOSS_MAX_WORKERS)))
    return {
        "usd_krw": results[("fx", None)],
        "cash_krw": sum(v for (k, _), v in results.items() if k == "KRW"),
        "cash_usd": sum(v for (k, _), v in results.items() if k == "USD"),
        "items": [it for (k, _), v in results.items() if k == "holdings" for it in v],
    }


# ---------- 종목 정보 캐시 / 검색 ----------
_stock_info_cache: dict[str, tuple[float, dict | None]] = {}
_stock_info_lock = threading.Lock()


def lookup_stocks(token: str, symbols: list[str], ttl: float = STOCK_INFO_CACHE_TTL) -> dict[str, dict]:
    """get_stock_info의 캐시 버전. 존재하지 않는 심볼도 결과 없음으로 캐시한다."""
    now = time.time()
    with _stock_info_lock:
        need = [s for s in symbols if s not in _stock_info_cache or now - _stock_info_cache[s][0] > ttl]
    if need:
        info = get_stock_info(token, need)
        with _stock_info_lock:
            for s in need:
                _stock_info_cache[s] = (now, info.get(s))
    with _stock_info_lock:
        return {s: _stock_info_cache[s][1] for s in symbols if _stock_info_cache.get(s, (0, None))[1]}


def search_stocks(token: str, query: str, limit: int = 8) -> list[dict]:
    """티커/종목명(한글·영문, 일부만 입력해도 됨)으로 후보를 찾는다.
    토스 API 자체에는 검색 기능이 없어서, 후보 코드는 네이버 증권 자동완성(비공식)으로 얻고
    토스 /api/v1/stocks로 실제 거래 가능한(ACTIVE) 종목인지 검증해서 토스 기준 이름/시장/통화를 돌려준다."""
    query = query.strip()[:40]
    if not query:
        return []
    candidates: list[str] = []
    if SYMBOL_PATTERN.fullmatch(query.upper()):
        candidates.append(query.upper())  # 정확한 티커를 직접 입력한 경우도 후보에 포함
    try:
        resp = requests.get("https://ac.stock.naver.com/ac", params={"q": query, "target": "stock"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        for it in resp.json().get("items", []):
            code = it.get("code")
            if code and it.get("nationCode") in ("KOR", "USA") and SYMBOL_PATTERN.fullmatch(code):
                candidates.append(code)
    except (requests.exceptions.RequestException, ValueError):
        pass  # 검색 소스 장애 시에는 정확한 티커 입력만 동작
    candidates = list(dict.fromkeys(candidates))[:limit]
    if not candidates:
        return []
    info = lookup_stocks(token, candidates)
    return [
        {"symbol": sym, "name": info[sym].get("name", sym), "market": info[sym].get("market", ""),
         "currency": info[sym].get("currency", "")}
        for sym in candidates
        if sym in info and info[sym].get("status") == "ACTIVE"
    ]


# ---------- 일봉/주봉 종가 ----------
_candle_cache: dict[str, dict] = {}
_candle_lock = threading.Lock()


def _week_key(timestamp: str) -> tuple[int, int] | None:
    try:
        y, m, d = (int(x) for x in timestamp[:10].split("-"))
        return date(y, m, d).isocalendar()[:2]
    except ValueError:
        return None


def _weekly_from(bars: list[tuple]) -> list[float]:
    """최신순 일봉 → 최신순 주봉 종가. 한 주에서 처음 만나는(=그 주 마지막 거래일) 종가를 쓴다."""
    seen: set = set()
    out: list[float] = []
    for key, close in bars:
        if key is not None and key not in seen:
            seen.add(key)
            out.append(close)
    return out


def fetch_close_history(token: str, symbol: str, days: int = 0, weeks: int = 0) -> tuple[list[float], list[float]]:
    """(일봉 종가, 주봉 종가), 둘 다 최신이 [0]. 토스 캔들 API는 일봉/1분봉만 있어서 주봉은 일봉을 ISO 주로
    묶는다(real/portfolio.py와 같은 방식). 같은 일봉 페이지에서 둘 다 만들고 5분 캐시해서, 한 화면을
    그릴 때 같은 종목 캔들을 여러 번 받지 않는다. 요청한 개수보다 길게 돌려줄 수 있다."""
    def enough(bars, exhausted):
        return exhausted or (len(bars) >= days and len(_weekly_from(bars)) >= weeks)

    now = time.time()
    with _candle_lock:
        cached = _candle_cache.get(symbol)
    if cached and now - cached["ts"] < CANDLE_CACHE_TTL and enough(cached["bars"], cached["exhausted"]):
        bars = cached["bars"]
    else:
        bars: list[tuple] = []
        exhausted = False
        before = None
        for _ in range(CANDLE_MAX_PAGES):
            data = get_daily_candles(token, symbol, count=200, before=before)
            candles = data.get("candles", [])
            if not candles:
                exhausted = True
                break
            bars.extend((_week_key(c.get("timestamp", "")), to_float(c.get("closePrice"))) for c in candles)
            if enough(bars, False):
                break
            before = data.get("nextBefore")
            if not before:
                exhausted = True
                break
        with _candle_lock:
            _candle_cache[symbol] = {"ts": now, "bars": bars, "exhausted": exhausted}
    return [c for _, c in bars], _weekly_from(bars)


# ---------- USD/JPY (Yahoo) ----------
_usd_jpy_cache: dict = {"value": None, "ts": 0.0}


def get_usd_jpy_quote() -> dict:
    """USD/JPY {rate, source, as_of}. 토스 API는 JPY를 지원하지 않아 Yahoo Finance(비공식)를 쓴다."""
    resp = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/JPY=X",
                        params={"interval": "1m", "range": "1d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    meta = result["meta"]
    # regularMarketTime은 마감 후에도 갱신돼 실제 마지막 체결보다 늦게 찍히므로, 마지막 1분봉 시각을 기준 시각으로 쓴다.
    candles = [(t, c) for t, c in zip(result.get("timestamp") or [], result["indicators"]["quote"][0]["close"]) if c]
    as_of_ts = candles[-1][0] if candles else float(meta["regularMarketTime"])
    return {"rate": float(meta["regularMarketPrice"]), "source": "Yahoo Finance", "as_of": fmt_as_of(float(as_of_ts))}


def get_cached_usd_jpy_quote() -> dict | None:
    """외부(Yahoo)라 실패할 수 있음 - 실패하면 직전 값을 그대로 유지한다."""
    now = time.time()
    if now - _usd_jpy_cache["ts"] >= USD_JPY_REFRESH_INTERVAL:
        try:
            _usd_jpy_cache["value"] = get_usd_jpy_quote()
        except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, ValueError):
            pass
        _usd_jpy_cache["ts"] = now
    return _usd_jpy_cache["value"]
