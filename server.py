#!/usr/bin/env python3
"""
토스증권 포트폴리오 대시보드 (redo) - 로그인 방식 웹 서버.
절대 0.0.0.0 등으로 외부에 노출하지 말 것 - 127.0.0.1(localhost) 전용.

사용법:
    python3 server.py
    -> http://127.0.0.1:8767 브라우저로 접속, 앱키/시크릿으로 로그인

인증 방식:
    .env에 앱키/시크릿을 미리 넣어두는 대신, 대시보드의 로그인 화면에서
    입력받아 그 자리에서 토스증권 Open API(OAuth2 client_credentials)로
    access_token을 발급받는다. 앱키/시크릿과 토큰은 디스크에 저장하지 않고
    서버 프로세스 메모리(세션)에만 유지한다 -> 서버를 재시작하면 다시 로그인해야 함.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import time
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

HOST = "127.0.0.1"
PORT = 8767
API_BASE = "https://openapi.tossinvest.com"
SESSION_COOKIE = "toss_redo_session"
SESSION_TTL = 60 * 60 * 4  # 4시간 무활동 시 세션 만료

ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
CASH_SYMBOLS_FILE = BASE_DIR / "cash_symbols.json"
CASH_SYMBOLS_MAX = 30
REBALANCE_CONFIG_FILE = BASE_DIR / "rebalance.json"
REBALANCE_REST_LABEL = "나머지"
REBALANCE_CASH_LABEL = "현금"  # "나머지" 버킷에 예수금/현금 취급 종목만 있을 때 쓰는 표시 이름 (real/과 동일한 관례)
REBALANCE_RESERVED_LABELS = {REBALANCE_REST_LABEL, REBALANCE_CASH_LABEL}
REBALANCE_MAX_CATEGORIES = 30
REBALANCE_MAX_SYMBOLS_PER_CATEGORY = 20
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,10}$")
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
WATCHLIST_MAX = 30
MA_RULES_FILE = BASE_DIR / "ma_rules.json"
MA_RULES_MAX = 30
MA_INTERVALS = {"day", "week"}
MA_MAX_PERIOD = {"day": 200, "week": 300}  # 캔들 페이지네이션으로 무리없이 받아올 수 있는 상한
STRATEGY_CONFIG_FILE = BASE_DIR / "strategy_config.json"
STRATEGY_STATE_FILE = BASE_DIR / "strategy_state.json"
STRATEGY_MAX_STAGES = 10
STRATEGY_DEFAULT_CONFIG = {
    "interval": "week",
    "periods": [60, 120, 200, 240],
    "tranches": [5.0, 10.0, 15.0, 20.0],
    "recovery_periods": [20, 60, 120, 200, 240],  # 이 중 하나라도 처음 상방 돌파하면 recovery_pct 채워 완성
    "recovery_pct": 50.0,
    "excluded_symbols": [],
}

# 세션 저장소: 메모리에만 유지, 디스크 기록 없음.
# { session_id: {"app_key", "app_secret", "token", "issued_at", "last_seen"} }
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_cash_symbols_lock = threading.Lock()  # cash_symbols.json 동시 수정 방지
_rebalance_lock = threading.Lock()  # rebalance.json 동시 수정 방지
_watchlist_lock = threading.Lock()  # watchlist.json 동시 수정 방지
_ma_rules_lock = threading.Lock()  # ma_rules.json 동시 수정 방지
_strategy_config_lock = threading.Lock()  # strategy_config.json 동시 수정 방지
_strategy_state_lock = threading.Lock()  # strategy_state.json 동시 수정 방지


def get_access_token(app_key: str, app_secret: str) -> str:
    """토스증권 Open API에 앱키/시크릿으로 access_token을 발급받는다."""
    resp = requests.post(
        f"{API_BASE}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": app_key,
            "client_secret": app_secret,
        },
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
    data = api_get(
        "/api/v1/exchange-rate", token, params={"baseCurrency": "USD", "quoteCurrency": "KRW"}
    ).get("result", {})
    return to_float(data.get("rate"))


def get_buying_power(token: str, account_seq: str, currency: str) -> float:
    data = api_get(
        "/api/v1/buying-power", token, params={"currency": currency}, account_seq=account_seq
    ).get("result", {})
    return to_float(data.get("cashBuyingPower"))


def _fmt_as_of(ts: float) -> str:
    """기준 시각 표기: 오늘이면 HH:MM, 아니면 MM-DD HH:MM (로컬 시간). real/portfolio.py와 동일."""
    t = time.localtime(ts)
    same_day = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M" if same_day else "%m-%d %H:%M", t)


def get_usd_jpy_quote() -> dict:
    """USD/JPY {rate, source, as_of}. 토스 API는 JPY를 지원하지 않아 Yahoo Finance(비공식)를 쓴다."""
    resp = requests.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/JPY=X",
        params={"interval": "1m", "range": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    meta = result["meta"]
    # regularMarketTime은 마감 후에도 갱신돼 실제 마지막 체결보다 늦게 찍히므로, 마지막 1분봉 시각을 기준 시각으로 쓴다.
    candles = [(t, c) for t, c in zip(result.get("timestamp") or [], result["indicators"]["quote"][0]["close"]) if c]
    as_of_ts = candles[-1][0] if candles else float(meta["regularMarketTime"])
    return {"rate": float(meta["regularMarketPrice"]), "source": "Yahoo Finance", "as_of": _fmt_as_of(float(as_of_ts))}


_usd_jpy_cache: dict = {"value": None, "ts": 0.0}
USD_JPY_REFRESH_INTERVAL = 60  # 초. 매 홀딩스 조회마다 외부 API를 부르지 않도록 이 주기로만 갱신.


def get_cached_usd_jpy_quote() -> dict | None:
    """USD/JPY는 토스 API가 아니라 외부(Yahoo)라 실패할 수 있음 - 실패하면 직전 값을 그대로 유지한다."""
    now = time.time()
    if now - _usd_jpy_cache["ts"] >= USD_JPY_REFRESH_INTERVAL:
        try:
            _usd_jpy_cache["value"] = get_usd_jpy_quote()
        except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, ValueError):
            pass
        _usd_jpy_cache["ts"] = now
    return _usd_jpy_cache["value"]


def get_stock_info(token: str, symbols: list[str]) -> dict[str, dict]:
    """심볼 목록의 기본정보(이름/시장 등)를 조회. 리밸런싱 목표 저장 시 존재하는 종목인지 검증하는 용도."""
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


_STOCK_INFO_CACHE: dict[str, tuple[float, dict | None]] = {}
STOCK_INFO_CACHE_TTL = 600.0  # 초. 종목 기본정보는 잘 안 바뀌므로 캐시해서 자동완성이 토스 API 한도(429)를 소진하지 않게 한다.


def lookup_stocks(token: str, symbols: list[str], ttl: float = STOCK_INFO_CACHE_TTL) -> dict[str, dict]:
    """get_stock_info의 캐시 버전. 존재하지 않는 심볼도 결과 없음으로 캐시한다."""
    now = time.time()
    need = [s for s in symbols if s not in _STOCK_INFO_CACHE or now - _STOCK_INFO_CACHE[s][0] > ttl]
    if need:
        info = get_stock_info(token, need)
        for s in need:
            _STOCK_INFO_CACHE[s] = (now, info.get(s))
    return {s: _STOCK_INFO_CACHE[s][1] for s in symbols if _STOCK_INFO_CACHE[s][1]}


def search_stocks(token: str, query: str, limit: int = 8) -> list[dict]:
    """티커/종목명(한글·영문, 일부만 입력해도 됨)으로 후보를 찾는다.
    토스 API 자체에는 검색 기능이 없어서, 후보 코드는 네이버 증권 자동완성(비공식)으로 얻고
    토스 /api/v1/stocks로 실제 거래 가능한(ACTIVE) 종목인지 검증해서 토스 기준 이름/시장/통화를 돌려준다.
    (real/portfolio.py의 search_stocks와 같은 방식)"""
    query = query.strip()[:40]
    if not query:
        return []
    candidates: list[str] = []
    if SYMBOL_PATTERN.fullmatch(query.upper()):
        candidates.append(query.upper())  # 정확한 티커를 직접 입력한 경우도 후보에 포함
    try:
        resp = requests.get(
            "https://ac.stock.naver.com/ac",
            params={"q": query, "target": "stock"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
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
        {
            "symbol": sym,
            "name": info[sym].get("name", sym),
            "market": info[sym].get("market", ""),
            "currency": info[sym].get("currency", ""),
        }
        for sym in candidates
        if sym in info and info[sym].get("status") == "ACTIVE"
    ]


def fetch_watchlist_rows(token: str, symbols: list[str]) -> list[dict]:
    """관심종목의 현재가 + 전일대비 등락률을 조회한다."""
    if not symbols:
        return []
    info = lookup_stocks(token, symbols)
    prices = get_prices(token, symbols)
    rows = []
    for sym in symbols:
        meta = info.get(sym, {})
        cur_price = prices.get(sym, 0.0)
        prev_close = None
        try:
            candles = get_daily_candles(token, sym, count=2).get("candles", [])
            if len(candles) >= 2:
                prev_close = to_float(candles[1].get("closePrice"))
        except requests.exceptions.RequestException:
            pass
        change_pct = ((cur_price - prev_close) / prev_close * 100) if prev_close else 0.0
        rows.append(
            {
                "symbol": sym,
                "name": meta.get("name", sym),
                "currency": meta.get("currency", "?"),
                "price": cur_price,
                "change_pct": change_pct,
            }
        )
    return rows


def fetch_daily_closes(token: str, symbol: str, needed: int) -> list[float]:
    """일봉 종가, 최신이 [0]번. 한 페이지(count=200)로 부족하면 이어서 받아온다."""
    closes: list[float] = []
    before = None
    for _ in range(4):  # 최대 4페이지(800일) - 일봉 기간 상한(MA_MAX_PERIOD["day"]=200)엔 충분한 여유
        data = get_daily_candles(token, symbol, count=200, before=before)
        candles = data.get("candles", [])
        if not candles:
            break
        closes.extend(to_float(c.get("closePrice")) for c in candles)
        if len(closes) >= needed:
            break
        before = data.get("nextBefore")
        if not before:
            break
    return closes


def fetch_weekly_closes(token: str, symbol: str, needed: int) -> list[float]:
    """일봉을 모아 주(ISO week) 단위 종가로 묶는다. 최신 주가 [0]번.
    (토스 캔들 API는 일봉/1분봉만 지원해서 주봉은 직접 묶어야 함 - real/portfolio.py와 동일한 방식)"""
    weekly_closes: list[float] = []
    seen_weeks: set[tuple[int, int]] = set()
    before = None
    for _ in range(8):  # 최대 8페이지(~1,600영업일 ≈ 6년치)까지만 조회
        data = get_daily_candles(token, symbol, count=200, before=before)
        candles = data.get("candles", [])
        if not candles:
            break
        for c in candles:
            try:
                y, m, d = (int(x) for x in c.get("timestamp", "")[:10].split("-"))
                iso_year, iso_week, _ = date(y, m, d).isocalendar()
            except ValueError:
                continue
            key = (iso_year, iso_week)
            if key in seen_weeks:
                continue
            seen_weeks.add(key)
            weekly_closes.append(to_float(c.get("closePrice")))
            if len(weekly_closes) >= needed:
                break
        if len(weekly_closes) >= needed:
            break
        before = data.get("nextBefore")
        if not before:
            break
    return weekly_closes


def compute_ma_rows(token: str, rules: list[dict]) -> list[dict]:
    """MA 규칙별로 이동평균 대비 현재가 이격도를 계산한다. 지표를 MA 하나로 하드코딩하지 않기 위해,
    (일봉/주봉, 기간, 근접 기준%)는 전부 rules(사용자가 화면에서 만든 설정)에서 온다."""
    if not rules:
        return []
    symbols = sorted({r["symbol"] for r in rules})
    info = lookup_stocks(token, symbols)
    prices = get_prices(token, symbols)

    # 같은 종목+봉종류 조합이 규칙 여러 개에 걸쳐 있어도 캔들 조회는 한 번만(가장 큰 기간 기준으로).
    needed_by_key: dict[tuple[str, str], int] = {}
    for r in rules:
        key = (r["symbol"], r["interval"])
        needed_by_key[key] = max(needed_by_key.get(key, 0), r["period"])

    closes_cache: dict[tuple[str, str], list[float]] = {}
    for (symbol, interval), needed in needed_by_key.items():
        try:
            closes_cache[(symbol, interval)] = (
                fetch_daily_closes(token, symbol, needed)
                if interval == "day"
                else fetch_weekly_closes(token, symbol, needed)
            )
        except requests.exceptions.RequestException:
            closes_cache[(symbol, interval)] = []

    rows = []
    for r in rules:
        symbol, interval, period = r["symbol"], r["interval"], r["period"]
        meta = info.get(symbol, {})
        closes = closes_cache.get((symbol, interval), [])
        cur_price = prices.get(symbol, 0.0)
        row = {
            "symbol": symbol,
            "name": meta.get("name", symbol),
            "currency": meta.get("currency", "?"),
            "interval": interval,
            "period": period,
            "proximity_pct": r["proximity_pct"],
        }
        if len(closes) < period or cur_price <= 0:
            unit = "일" if interval == "day" else "주"
            row["ok"] = False
            row["reason"] = f"데이터 부족 ({len(closes)}/{period}{unit})"
        else:
            ma_value = sum(closes[:period]) / period
            diff_pct = (cur_price - ma_value) / ma_value * 100 if ma_value else 0.0
            row["ok"] = True
            row["price"] = cur_price
            row["ma_value"] = ma_value
            row["diff_pct"] = diff_pct
            row["hit"] = abs(diff_pct) <= r["proximity_pct"]
        rows.append(row)
    return rows


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_cash_symbols() -> list[str]:
    """현금 취급 종목 설정을 읽는다. 파일이 없거나 깨졌으면 빈 목록으로 취급한다(직전 상태를 지우지 않음)."""
    try:
        data = json.loads(CASH_SYMBOLS_FILE.read_text(encoding="utf-8"))
        symbols = data.get("symbols", [])
        return [s for s in symbols if isinstance(s, str) and s]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def save_cash_symbols(symbols: list[str]) -> None:
    with _cash_symbols_lock:
        tmp = CASH_SYMBOLS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"symbols": symbols}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CASH_SYMBOLS_FILE)


def load_watchlist() -> list[str]:
    """관심종목 설정을 읽는다. 파일이 없거나 깨졌으면 빈 목록으로 취급한다(직전 상태를 지우지 않음)."""
    try:
        data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        symbols = data.get("symbols", [])
        return [s for s in symbols if isinstance(s, str) and s]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def save_watchlist(symbols: list[str]) -> None:
    with _watchlist_lock:
        tmp = WATCHLIST_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"symbols": symbols}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(WATCHLIST_FILE)


def load_ma_rules() -> list[dict]:
    """MA 감시 규칙을 읽는다. 파일이 없거나 깨졌으면 빈 목록으로 취급한다(직전 상태를 지우지 않음)."""
    try:
        data = json.loads(MA_RULES_FILE.read_text(encoding="utf-8"))
        clean = []
        for r in data.get("rules", []):
            if not isinstance(r, dict):
                continue
            symbol = str(r.get("symbol", "")).strip().upper()
            interval = r.get("interval")
            period = r.get("period")
            proximity_pct = r.get("proximity_pct")
            if symbol and interval in MA_INTERVALS and isinstance(period, (int, float)) and isinstance(proximity_pct, (int, float)):
                clean.append({"symbol": symbol, "interval": interval, "period": int(period), "proximity_pct": to_float(proximity_pct)})
        return clean
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def save_ma_rules(rules: list[dict]) -> None:
    with _ma_rules_lock:
        tmp = MA_RULES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(MA_RULES_FILE)


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
            unit = "일" if interval == "day" else "주"
            return [], f"기간은 2~{max_period}{unit} 사이의 정수여야 합니다."
        proximity_pct = to_float(r.get("proximity_pct"), -1)
        if not (0 <= proximity_pct <= 100):
            return [], "근접 기준%는 0~100 사이 숫자여야 합니다."
        clean.append({"symbol": symbol, "interval": interval, "period": int(period), "proximity_pct": proximity_pct})
    return clean, None


# ---------- 분할매수 전략 (1차 사이클만: 0%->100% 누적 매수, 매도/사이클 리셋 없음) ----------
# real/portfolio.py의 전략은 1차 사이클(0%->100%) 이후 2차 사이클(매도+재매수), 레거시/본절 규칙까지
# 있지만, 여기서는 우선 1차 사이클만 구현한다. 한 번 돌파로 카운트된 단계는 가격이 회복돼도 다시 안
# 풀리는 래칫 방식이라 "이번에 새로 돌파했는지" 구분(real/의 missed_mas/소급 규칙)이 필요 없어 단순하다.


def load_strategy_config() -> dict:
    """전략 템플릿(봉 종류, 단계별 기간+매수%p, 회복 판정 기간+회복%p)과 전략 제외 종목을 읽는다.
    파일이 없거나 깨졌으면 사용자분이 real/에서 쓰던 기본값(주봉 60/120/200/240주 5/10/15/20%p,
    회복은 20/60/120/200/240주 중 하나 상방 돌파 시 +50%p)으로 취급한다."""
    try:
        data = json.loads(STRATEGY_CONFIG_FILE.read_text(encoding="utf-8"))
        interval = data.get("interval")
        periods = data.get("periods")
        tranches = data.get("tranches")
        recovery_periods = data.get("recovery_periods")
        recovery_pct = data.get("recovery_pct")
        excluded = data.get("excluded_symbols")
        if (
            interval in MA_INTERVALS
            and isinstance(periods, list)
            and isinstance(tranches, list)
            and len(periods) == len(tranches) > 0
            and all(isinstance(p, (int, float)) for p in periods)
            and all(isinstance(t, (int, float)) for t in tranches)
            and isinstance(recovery_periods, list)
            and all(isinstance(p, (int, float)) for p in recovery_periods)
            and isinstance(recovery_pct, (int, float))
            and isinstance(excluded, list)
        ):
            return {
                "interval": interval,
                "periods": [int(p) for p in periods],
                "tranches": [to_float(t) for t in tranches],
                "recovery_periods": [int(p) for p in recovery_periods],
                "recovery_pct": to_float(recovery_pct),
                "excluded_symbols": [str(s).strip().upper() for s in excluded if isinstance(s, str) and s.strip()],
            }
        return dict(STRATEGY_DEFAULT_CONFIG)
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return dict(STRATEGY_DEFAULT_CONFIG)


def save_strategy_config(config: dict) -> None:
    with _strategy_config_lock:
        tmp = STRATEGY_CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STRATEGY_CONFIG_FILE)


def validate_strategy_config(data) -> tuple[dict, str | None]:
    """전략 템플릿을 검증/정규화한다. (config, None)이면 통과, ({}, 에러메시지)면 실패."""
    if not isinstance(data, dict):
        return {}, "잘못된 요청입니다."
    interval = data.get("interval")
    if interval not in MA_INTERVALS:
        return {}, "봉 종류는 일봉/주봉이어야 합니다."

    periods = data.get("periods")
    tranches = data.get("tranches")
    if not isinstance(periods, list) or not isinstance(tranches, list):
        return {}, "단계 목록이 올바르지 않습니다."
    if not periods:
        return {}, "단계를 최소 1개 이상 등록하세요."
    if len(periods) != len(tranches):
        return {}, "기간과 매수 비율(%p)의 개수가 같아야 합니다."
    if len(periods) > STRATEGY_MAX_STAGES:
        return {}, f"단계는 최대 {STRATEGY_MAX_STAGES}개까지 설정할 수 있습니다."

    max_period = MA_MAX_PERIOD[interval]
    clean_periods: list[int] = []
    for p in periods:
        pv = to_float(p, -1)
        if not (pv.is_integer() and 2 <= pv <= max_period):
            return {}, f"기간은 2~{max_period} 사이의 정수여야 합니다."
        clean_periods.append(int(pv))
    if len(set(clean_periods)) != len(clean_periods):
        return {}, "같은 기간을 두 번 넣을 수 없습니다."

    clean_tranches: list[float] = []
    for t in tranches:
        tv = to_float(t, -1)
        if not (0 < tv <= 100):
            return {}, "매수 비율(%p)은 0보다 크고 100 이하여야 합니다."
        clean_tranches.append(tv)

    recovery_periods = data.get("recovery_periods")
    if not isinstance(recovery_periods, list) or not recovery_periods:
        return {}, "회복 판정 기간을 최소 1개 이상 등록하세요."
    if len(recovery_periods) > STRATEGY_MAX_STAGES:
        return {}, f"회복 판정 기간은 최대 {STRATEGY_MAX_STAGES}개까지 설정할 수 있습니다."
    clean_recovery_periods: list[int] = []
    for p in recovery_periods:
        pv = to_float(p, -1)
        if not (pv.is_integer() and 2 <= pv <= max_period):
            return {}, f"회복 판정 기간은 2~{max_period} 사이의 정수여야 합니다."
        if int(pv) not in clean_recovery_periods:
            clean_recovery_periods.append(int(pv))

    recovery_pct = to_float(data.get("recovery_pct"), -1)
    if not (0 < recovery_pct <= 100):
        return {}, "회복 매수 비율(%p)은 0보다 크고 100 이하여야 합니다."

    if sum(clean_tranches) + recovery_pct > 100:
        return {}, f"단계별 매수 비율 합계 + 회복 매수 비율이 100%를 넘습니다 (현재 {sum(clean_tranches) + recovery_pct:.1f}%)."

    raw_excluded = data.get("excluded_symbols", [])
    if not isinstance(raw_excluded, list):
        return {}, "전략 제외 종목은 배열이어야 합니다."
    clean_excluded: list[str] = []
    for s in raw_excluded:
        if not isinstance(s, str):
            return {}, "전략 제외 종목 코드는 문자열이어야 합니다."
        sym = s.strip().upper()
        if not sym:
            continue
        if not SYMBOL_PATTERN.fullmatch(sym):
            return {}, f"잘못된 종목 코드입니다: {s!r}"
        if sym not in clean_excluded:
            clean_excluded.append(sym)

    return {
        "interval": interval,
        "periods": clean_periods,
        "tranches": clean_tranches,
        "recovery_periods": clean_recovery_periods,
        "recovery_pct": recovery_pct,
        "excluded_symbols": clean_excluded,
    }, None


def load_strategy_state() -> dict[str, dict]:
    """종목별 진행 상태(지금까지 돌파 카운트된 기간들 + 회복 완료 여부)를 읽는다. 설정 파일과 달리
    시간에 따라 쌓이는 기록이라, 저장 형식은 {symbol: {"broken_periods": [기간, ...], "recovered": bool}}."""
    try:
        data = json.loads(STRATEGY_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        clean: dict[str, dict] = {}
        for symbol, entry in data.items():
            periods = entry.get("broken_periods") if isinstance(entry, dict) else None
            if isinstance(periods, list):
                clean[symbol] = {
                    "broken_periods": [int(p) for p in periods if isinstance(p, (int, float))],
                    "recovered": bool(entry.get("recovered", False)),
                }
        return clean
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return {}


def save_strategy_state(state: dict) -> None:
    with _strategy_state_lock:
        tmp = STRATEGY_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STRATEGY_STATE_FILE)


def held_symbols(token: str, accounts: list[dict]) -> set[str]:
    """전략 대상 후보(보유종목 심볼만 가볍게) - fetch_holdings_data 전체 계산은 필요 없어서 따로 둠."""
    symbols: set[str] = set()
    for account in accounts:
        holdings = get_holdings(token, account["accountSeq"])
        for h in holdings.get("items", []):
            if h.get("symbol") and to_float(h.get("quantity")) != 0:
                symbols.add(h["symbol"])
    return symbols


def compute_strategy_rows(token: str, config: dict, state: dict, symbols: list[str]) -> tuple[list[dict], bool]:
    """분할매수 진행률(1차 사이클만)을 계산하고, 새로 돌파/회복한 게 있으면 state를 갱신한다.
    단계(periods)를 하방 돌파할 때마다 그 단계의 매수%p를 누적하고, 그중 하나라도 이미 돌파된 뒤
    회복 판정 기간(recovery_periods) 중 하나를 처음 상방 돌파하면 남은 몫(recovery_pct)을 채워 완성한다.
    (사이클 리셋/매도 재진입 같은 2차 사이클 이후 규칙은 아직 없음 - 1차 사이클만.)
    반환: (rows, state가 바뀌었는지)."""
    periods = config["periods"]
    tranches = config["tranches"]
    recovery_periods = config["recovery_periods"]
    recovery_pct = config["recovery_pct"]
    interval = config["interval"]
    excluded = set(config["excluded_symbols"])

    targets = [s for s in symbols if s not in excluded]
    if not targets:
        return [], False

    info = lookup_stocks(token, targets)
    prices = get_prices(token, targets)
    needed = max(periods + recovery_periods)
    state_changed = False
    rows: list[dict] = []

    for symbol in targets:
        meta = info.get(symbol, {})
        cur_price = prices.get(symbol, 0.0)
        prev_entry = state.get(symbol, {"broken_periods": [], "recovered": False})
        broken = set(prev_entry["broken_periods"])
        recovered = prev_entry["recovered"]

        if cur_price <= 0:
            rows.append(
                {"symbol": symbol, "name": meta.get("name", symbol), "currency": meta.get("currency", "?"), "ok": False, "reason": "현재가 조회 실패"}
            )
            continue

        try:
            closes = fetch_daily_closes(token, symbol, needed) if interval == "day" else fetch_weekly_closes(token, symbol, needed)
        except requests.exceptions.RequestException:
            closes = []

        def ma_diff(period: int) -> float | None:
            if len(closes) < period:
                return None
            ma_value = sum(closes[:period]) / period
            return (cur_price - ma_value) / ma_value * 100 if ma_value else 0.0

        stages = []
        for period in periods:
            diff_pct = ma_diff(period)
            if diff_pct is None:
                stages.append({"period": period, "ok": False})
                continue
            if diff_pct <= 0:  # MA 아래로 내려간 적이 있으면(래칫) 계속 카운트 유지
                broken.add(period)
            stages.append({"period": period, "ok": True, "diff_pct": diff_pct, "broken": period in broken})

        # 회복 판정: real/portfolio.py와 동일하게 "설정된 단계(periods)를 전부 다 깬 뒤"에만 회복
        # 감시를 시작한다 - 한 단계만 깨진 상태에서 곧장 상방 돌파해도 회복 보너스가 붙으면 안 됨
        # (그러면 50%(단계 일부)+50%(회복)처럼 절반만 채운 상태에서 100%로 잘못 완성돼버림).
        recovery_stages = []
        if len(broken) >= len(periods) and not recovered:
            for period in recovery_periods:
                diff_pct = ma_diff(period)
                if diff_pct is None:
                    recovery_stages.append({"period": period, "ok": False})
                    continue
                triggered = diff_pct > 0
                if triggered:
                    recovered = True
                recovery_stages.append({"period": period, "ok": True, "diff_pct": diff_pct, "triggered": triggered})
        elif recovered:
            recovery_stages = [{"period": p, "ok": True, "triggered": True} for p in recovery_periods]

        if broken != set(prev_entry["broken_periods"]) or recovered != prev_entry["recovered"]:
            state[symbol] = {"broken_periods": sorted(broken), "recovered": recovered}
            state_changed = True

        entry_pct = min(100.0, sum(t for p, t in zip(periods, tranches) if p in broken) + (recovery_pct if recovered else 0.0))
        rows.append(
            {
                "symbol": symbol,
                "name": meta.get("name", symbol),
                "currency": meta.get("currency", "?"),
                "ok": True,
                "price": cur_price,
                "entry_pct": entry_pct,
                "stage_count": len(broken),
                "stage_total": len(periods),
                "stages": stages,
                "recovered": recovered,
                "recovery_stages": recovery_stages,
            }
        )

    return rows, state_changed


def load_rebalance_config() -> dict:
    """리밸런싱 목표 설정을 읽는다. 파일이 없거나 깨졌으면 기본값(카테고리 없음, 미분류 종목은 각각 5%,
    원/달러 목표 비중은 50/50)으로 취급한다."""
    default = {"targets": [], "default_rest_target_pct": 5.0, "krw_target_pct": 50.0}
    try:
        data = json.loads(REBALANCE_CONFIG_FILE.read_text(encoding="utf-8"))
        clean_targets = []
        for t in data.get("targets", []):
            if not isinstance(t, dict):
                continue
            label = str(t.get("label", "")).strip()
            symbols = [s.strip().upper() for s in t.get("symbols", []) if isinstance(s, str) and s.strip()]
            if label and symbols:
                clean_targets.append({"label": label, "symbols": symbols, "target_pct": to_float(t.get("target_pct"))})
        return {
            "targets": clean_targets,
            "default_rest_target_pct": to_float(data.get("default_rest_target_pct"), default["default_rest_target_pct"]),
            "krw_target_pct": to_float(data.get("krw_target_pct"), default["krw_target_pct"]),
        }
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return default


def validate_rebalance_targets(targets) -> tuple[list[dict], str | None]:
    """카테고리 목록을 검증/정규화한다. (targets, None)이면 통과, (빈 목록, 에러메시지)면 실패."""
    if not isinstance(targets, list):
        return [], "targets는 배열이어야 합니다."
    if len(targets) > REBALANCE_MAX_CATEGORIES:
        return [], f"카테고리는 최대 {REBALANCE_MAX_CATEGORIES}개까지 만들 수 있습니다."

    clean: list[dict] = []
    seen_labels: set[str] = set()
    seen_symbols: dict[str, str] = {}  # symbol -> 그 symbol을 먼저 쓴 카테고리 label (중복 검출용)

    for t in targets:
        if not isinstance(t, dict):
            return [], "카테고리 형식이 올바르지 않습니다."

        label = str(t.get("label", "")).strip()
        if not (1 <= len(label) <= 30):
            return [], "카테고리 이름은 1~30자여야 합니다."
        if label in REBALANCE_RESERVED_LABELS:
            return [], f'"{label}"은(는) 예약된 이름이라 카테고리 이름으로 쓸 수 없습니다.'
        if label in seen_labels:
            return [], f'카테고리 이름이 중복됩니다: "{label}"'
        seen_labels.add(label)

        raw_symbols = t.get("symbols", [])
        if not isinstance(raw_symbols, list) or not raw_symbols:
            return [], f'"{label}" 카테고리에 종목을 하나 이상 넣어야 합니다.'
        if len(raw_symbols) > REBALANCE_MAX_SYMBOLS_PER_CATEGORY:
            return [], f'"{label}" 카테고리에는 종목을 최대 {REBALANCE_MAX_SYMBOLS_PER_CATEGORY}개까지 넣을 수 있습니다.'

        symbols: list[str] = []
        for s in raw_symbols:
            if not isinstance(s, str):
                return [], "종목 코드는 문자열이어야 합니다."
            sym = s.strip().upper()
            if not SYMBOL_PATTERN.match(sym):
                return [], f"잘못된 종목 코드입니다: {s!r}"
            if sym in seen_symbols:
                return [], f'같은 종목을 두 카테고리에 넣을 수 없습니다: {sym} ("{seen_symbols[sym]}"와(과) "{label}")'
            seen_symbols[sym] = label
            if sym not in symbols:
                symbols.append(sym)

        target_pct = to_float(t.get("target_pct"), -1)
        if not (0 <= target_pct <= 100):
            return [], f'"{label}"의 목표%는 0~100 사이 숫자여야 합니다.'

        clean.append({"label": label, "symbols": symbols, "target_pct": target_pct})

    return clean, None


def save_rebalance_config(targets: list[dict], default_rest_target_pct: float, krw_target_pct: float) -> None:
    with _rebalance_lock:
        tmp = REBALANCE_CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "targets": targets,
                    "default_rest_target_pct": default_rest_target_pct,
                    "krw_target_pct": krw_target_pct,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(REBALANCE_CONFIG_FILE)


def compute_rebalance(
    all_rows: list[dict], cash_krw_total: float, cash_symbols: set[str], total_eval: float, config: dict
) -> list[dict]:
    """카테고리별 현재%/목표%를 계산한다. 목표 카테고리에 없는 종목/예수금은 하나로 합치지 않고 각각
    개별 항목으로 만들되, 목표%는 전부 config["default_rest_target_pct"](기본 5%)로 고정한다."""
    matched_symbols: set[str] = set()
    result = []
    for t in config["targets"]:
        amount = sum(r["평가금액(원)"] for r in all_rows if r["심볼"] in t["symbols"])
        matched_symbols.update(t["symbols"])
        result.append(
            {
                "label": t["label"],
                "current_pct": (amount / total_eval * 100) if total_eval else 0.0,
                "target_pct": t["target_pct"],
            }
        )

    default_pct = config["default_rest_target_pct"]
    for r in all_rows:
        if r["심볼"] in matched_symbols:
            continue
        # 현금 취급 종목(SGOV 등)은 "현금" 표에 이미 따로 보이니 여기서는 이름 대신 "현금" 라벨을 쓴다.
        label = REBALANCE_CASH_LABEL if r["심볼"] in cash_symbols else r["종목"]
        result.append(
            {
                "label": label,
                "current_pct": (r["평가금액(원)"] / total_eval * 100) if total_eval else 0.0,
                "target_pct": default_pct,
            }
        )

    if cash_krw_total > 0:
        result.append(
            {
                "label": REBALANCE_CASH_LABEL,
                "current_pct": (cash_krw_total / total_eval * 100) if total_eval else 0.0,
                "target_pct": default_pct,
            }
        )

    # 여러 항목이 "현금" 라벨을 가질 수 있으므로(예수금 + 현금 취급 종목들) 하나로 합친다.
    merged: dict[str, dict] = {}
    for r in result:
        if r["label"] in merged:
            merged[r["label"]]["current_pct"] += r["current_pct"]
        else:
            merged[r["label"]] = dict(r)
    return list(merged.values())


def compute_currency_split(
    all_rows: list[dict], cash_krw_amt: float, cash_usd_amt: float, usd_krw: float, total_eval: float, krw_target_pct: float
) -> dict:
    """전체 자산 중 원화/달러 통화 비중 (예수금 포함) + 목표 비중. 달러 목표는 100-원화 목표로 자동 계산."""
    krw_amount = cash_krw_amt + sum(r["평가금액(원)"] for r in all_rows if r["통화"] == "KRW")
    usd_amount = cash_usd_amt * usd_krw + sum(r["평가금액(원)"] for r in all_rows if r["통화"] == "USD")
    return {
        "krw_pct": (krw_amount / total_eval * 100) if total_eval else 0.0,
        "usd_pct": (usd_amount / total_eval * 100) if total_eval else 0.0,
        "krw_target_pct": krw_target_pct,
        "usd_target_pct": 100.0 - krw_target_pct,
    }


def fetch_holdings_data(token: str, accounts: list[dict]) -> dict:
    """계좌 전체의 보유종목 + 예수금 + 총계를 계산한다. real/portfolio.py의 fetch_rows/totals_of와 같은 방식."""
    usd_krw = get_usd_krw_rate(token)
    cash_krw_amt = cash_usd_amt = 0.0
    rows: list[dict] = []

    for account in accounts:
        account_seq = account["accountSeq"]
        cash_krw_amt += get_buying_power(token, account_seq, "KRW")
        cash_usd_amt += get_buying_power(token, account_seq, "USD")

        holdings = get_holdings(token, account_seq)
        for h in holdings.get("items", []):
            currency = h.get("currency", "KRW")
            quantity = to_float(h.get("quantity"))
            if quantity == 0:
                continue
            eval_amount = to_float((h.get("marketValue") or {}).get("amount"))
            # amount/rate는 수수료·세금 반영 전 값이라, 실제 손익에 더 가까운 amountAfterCost/rateAfterCost를 쓴다.
            profit_loss = to_float((h.get("profitLoss") or {}).get("amountAfterCost"))
            profit_loss_rate = to_float((h.get("profitLoss") or {}).get("rateAfterCost")) * 100
            fx = usd_krw if currency == "USD" else 1.0

            rows.append(
                {
                    "심볼": h.get("symbol", ""),
                    "종목": h.get("name", h.get("symbol", "?")),
                    "시장": h.get("marketCountry", "KR" if currency == "KRW" else "US"),
                    "통화": currency,
                    "수량": quantity,
                    "매입단가": to_float(h.get("averagePurchasePrice")),
                    "현재가": to_float(h.get("lastPrice")),
                    "평가금액(원)": eval_amount * fx,
                    "손익(원)": profit_loss * fx,
                    "평가금액(외화)": eval_amount if currency == "USD" else None,
                    "손익(외화)": profit_loss if currency == "USD" else None,
                    "수익률(%)": profit_loss_rate,
                }
            )

    rows.sort(key=lambda r: r["평가금액(원)"], reverse=True)

    cash_krw_total = cash_krw_amt + cash_usd_amt * usd_krw  # 예수금(원화 환산 합계)
    cash_symbols = set(load_cash_symbols())

    # 현금 취급 설정된 종목은 보유종목 표에서 빼서 현금 쪽으로 옮긴다 (rows를 한 번만 순회하며 나눔).
    stock_rows: list[dict] = []
    cash_stock_rows: list[dict] = []
    for r in rows:
        (cash_stock_rows if r["심볼"] in cash_symbols else stock_rows).append(r)

    total_eval = sum(r["평가금액(원)"] for r in rows) + cash_krw_total  # 분모: 보유종목 + 예수금
    for r in rows:
        r["지분율(%)"] = (r["평가금액(원)"] / total_eval * 100) if total_eval else 0.0

    total_pl = sum(r["손익(원)"] for r in rows)
    total_cost = total_eval - total_pl
    total_rate = (total_pl / total_cost * 100) if total_cost else 0.0

    rebalance_config = load_rebalance_config()
    rebalance = compute_rebalance(rows, cash_krw_total, cash_symbols, total_eval, rebalance_config)
    currency_split = compute_currency_split(
        rows, cash_krw_amt, cash_usd_amt, usd_krw, total_eval, rebalance_config["krw_target_pct"]
    )

    return {
        "stocks": stock_rows,
        "cash": {
            "krw": cash_krw_amt,
            "usd": cash_usd_amt,
            "usd_krw_rate": usd_krw,
            "stocks": cash_stock_rows,
            "total_krw": cash_krw_total + sum(r["평가금액(원)"] for r in cash_stock_rows),
        },
        "totals": {
            "eval_krw": total_eval,
            "profit_loss_krw": total_pl,
            "rate_pct": total_rate,
        },
        "rebalance": rebalance,
        "currency_split": currency_split,
        "usd_krw": usd_krw,
        "usd_jpy": get_cached_usd_jpy_quote(),
    }


def _prune_expired() -> None:
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["last_seen"] > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]


def _session_id_from_cookie(handler: "Handler") -> str | None:
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(f"{SESSION_COOKIE}="):
            return part[len(SESSION_COOKIE) + 1 :]
    return None


def _get_session(handler: "Handler") -> dict | None:
    sid = _session_id_from_cookie(handler)
    if not sid:
        return None
    with _lock:
        _prune_expired()
        session = _sessions.get(sid)
        if session:
            session["last_seen"] = time.time()
        return session


def call_with_reauth(session: dict, fn):
    """fn(token)을 실제로 실행해보고, 401이면 그때만 앱키/시크릿으로 재발급해서 한 번 재시도한다.
    (미리 확인용으로 별도 API를 부르지 않음 - real/portfolio.py와 같은 방식. 토스 API 초당 요청 제한을
    불필요하게 두 배로 쓰지 않기 위함.)"""
    try:
        return fn(session["token"])
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            session["token"] = get_access_token(session["app_key"], session["app_secret"])
            return fn(session["token"])
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "TossRedo/0.1"

    def log_message(self, fmt, *args):  # 기본 stderr 로그를 조용히 (앱키/시크릿 노출 방지)
        pass

    # ---- 보안 체크 (real/web_server.py와 동일한 패턴) ----
    def _host_ok(self) -> bool:
        return self.headers.get("Host") in ALLOWED_HOSTS

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def _send_json(self, status: int, payload: dict, set_cookie: str | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _forbidden(self):
        self._send_json(403, {"error": "forbidden"})

    def do_GET(self):
        if not self._host_ok():
            return self._forbidden()

        path = self.path.split("?")[0]

        if path == "/" or path == "/dashboard.html":
            html = DASHBOARD_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if path == "/api/session":
            session = _get_session(self)
            self._send_json(200, {"logged_in": session is not None})
            return

        if path == "/api/accounts":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                accounts = call_with_reauth(session, get_accounts)
                self._send_json(200, {"accounts": accounts})
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 502
                self._send_json(status, {"error": f"토스 API 오류 ({status})"})
            except requests.RequestException:
                self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
            return

        if path == "/api/holdings":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                accounts = call_with_reauth(session, get_accounts)
                data = call_with_reauth(session, lambda token: fetch_holdings_data(token, accounts))
                self._send_json(200, data)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 502
                body = e.response.text if e.response is not None else ""
                print(f"[/api/holdings] 토스 API 오류 {status}: {body[:500]}", file=sys.stderr)
                self._send_json(status, {"error": f"토스 API 오류 ({status})"})
            except requests.RequestException as e:
                print(f"[/api/holdings] 네트워크 오류: {e}", file=sys.stderr)
                self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json(500, {"error": "서버 내부 오류가 발생했습니다."})
            return

        if path == "/api/cash-symbols":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            self._send_json(200, {"symbols": load_cash_symbols()})
            return

        if path == "/api/rebalance-config":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            self._send_json(200, load_rebalance_config())
            return

        if path == "/api/search":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            try:
                results = call_with_reauth(session, lambda token: search_stocks(token, query))
                self._send_json(200, {"results": results})
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 502
                self._send_json(status, {"error": f"토스 API 오류 ({status})"})
            except requests.RequestException:
                self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
            return

        if path == "/api/watchlist":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            symbols = load_watchlist()
            try:
                items = call_with_reauth(session, lambda token: fetch_watchlist_rows(token, symbols))
                self._send_json(200, {"items": items})
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 502
                self._send_json(status, {"error": f"토스 API 오류 ({status})"})
            except requests.RequestException:
                self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
            return

        if path == "/api/ma-rules":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            rules = load_ma_rules()
            try:
                rows = call_with_reauth(session, lambda token: compute_ma_rows(token, rules))
                self._send_json(200, {"rows": rows})
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 502
                self._send_json(status, {"error": f"토스 API 오류 ({status})"})
            except requests.RequestException:
                self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
            return

        if path == "/api/strategy-config":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            self._send_json(200, load_strategy_config())
            return

        if path == "/api/strategy":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            config = load_strategy_config()
            watchlist_symbols = set(load_watchlist())
            try:
                accounts = call_with_reauth(session, get_accounts)
                held = call_with_reauth(session, lambda token: held_symbols(token, accounts))
                symbols = sorted(held | watchlist_symbols)
                state = load_strategy_state()
                rows, changed = call_with_reauth(session, lambda token: compute_strategy_rows(token, config, state, symbols))
                if changed:
                    save_strategy_state(state)
                self._send_json(200, {"rows": rows})
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 502
                self._send_json(status, {"error": f"토스 API 오류 ({status})"})
            except requests.RequestException:
                self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._forbidden()

        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        if length > 8192:
            return self._send_json(413, {"error": "요청이 너무 큽니다."})
        raw = self.rfile.read(length) if length else b""

        if path == "/api/login":
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})

            app_key = (data.get("app_key") or "").strip()
            app_secret = (data.get("app_secret") or "").strip()
            if not app_key or not app_secret:
                return self._send_json(400, {"error": "앱키와 시크릿을 모두 입력하세요."})

            try:
                token = get_access_token(app_key, app_secret)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 401
                return self._send_json(
                    401, {"error": "로그인 실패: 앱키/시크릿을 확인하세요." if status in (400, 401) else f"토스 API 오류 ({status})"}
                )
            except requests.RequestException:
                return self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})

            sid = secrets.token_urlsafe(32)
            with _lock:
                _prune_expired()
                _sessions[sid] = {
                    "app_key": app_key,
                    "app_secret": app_secret,
                    "token": token,
                    "issued_at": time.time(),
                    "last_seen": time.time(),
                }
            cookie = f"{SESSION_COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/"
            return self._send_json(200, {"ok": True}, set_cookie=cookie)

        if path == "/api/cash-symbols":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})

            raw_symbols = data.get("symbols")
            if not isinstance(raw_symbols, list):
                return self._send_json(400, {"error": "symbols는 배열이어야 합니다."})
            if len(raw_symbols) > CASH_SYMBOLS_MAX:
                return self._send_json(400, {"error": f"현금 취급 종목은 최대 {CASH_SYMBOLS_MAX}개까지 설정할 수 있습니다."})

            symbols: list[str] = []
            for s in raw_symbols:
                if not isinstance(s, str):
                    return self._send_json(400, {"error": "종목 코드는 문자열이어야 합니다."})
                sym = s.strip().upper()
                if not sym or len(sym) > 20:
                    return self._send_json(400, {"error": f"잘못된 종목 코드입니다: {s!r}"})
                if sym not in symbols:
                    symbols.append(sym)

            save_cash_symbols(symbols)
            return self._send_json(200, {"ok": True, "symbols": symbols})

        if path == "/api/rebalance-config":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})

            targets, error = validate_rebalance_targets(data.get("targets"))
            if error:
                return self._send_json(400, {"error": error})

            default_rest_target_pct = to_float(data.get("default_rest_target_pct"), -1)
            if not (0 <= default_rest_target_pct <= 100):
                return self._send_json(400, {"error": "미분류 종목 기본 목표%는 0~100 사이 숫자여야 합니다."})

            krw_target_pct = to_float(data.get("krw_target_pct"), -1)
            if not (0 <= krw_target_pct <= 100):
                return self._send_json(400, {"error": "목표 원화 비중%는 0~100 사이 숫자여야 합니다."})

            # 새로 목표에 넣은 종목이 실제 존재하는지 토스 API로 확인 (real/의 관례와 동일)
            all_symbols = sorted({s for t in targets for s in t["symbols"]})
            if all_symbols:
                try:
                    info = call_with_reauth(session, lambda token: get_stock_info(token, all_symbols))
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else 502
                    return self._send_json(status, {"error": f"토스 API 오류 ({status})"})
                except requests.RequestException:
                    return self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
                missing = [s for s in all_symbols if s not in info]
                if missing:
                    return self._send_json(400, {"error": f"존재하지 않는 종목 코드입니다: {', '.join(missing)}"})

            save_rebalance_config(targets, default_rest_target_pct, krw_target_pct)
            return self._send_json(
                200,
                {
                    "ok": True,
                    "targets": targets,
                    "default_rest_target_pct": default_rest_target_pct,
                    "krw_target_pct": krw_target_pct,
                },
            )

        if path == "/api/watchlist":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})

            raw_symbols = data.get("symbols")
            if not isinstance(raw_symbols, list):
                return self._send_json(400, {"error": "symbols는 배열이어야 합니다."})
            if len(raw_symbols) > WATCHLIST_MAX:
                return self._send_json(400, {"error": f"관심종목은 최대 {WATCHLIST_MAX}개까지 등록할 수 있습니다."})

            symbols: list[str] = []
            for s in raw_symbols:
                if not isinstance(s, str):
                    return self._send_json(400, {"error": "종목 코드는 문자열이어야 합니다."})
                sym = s.strip().upper()
                if not SYMBOL_PATTERN.fullmatch(sym):
                    return self._send_json(400, {"error": f"잘못된 종목 코드입니다: {s!r}"})
                if sym not in symbols:
                    symbols.append(sym)

            # 새로 추가된 종목이 실제 존재하는지 토스 API로 확인 (real/의 관례와 동일)
            if symbols:
                try:
                    info = call_with_reauth(session, lambda token: get_stock_info(token, symbols))
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else 502
                    return self._send_json(status, {"error": f"토스 API 오류 ({status})"})
                except requests.RequestException:
                    return self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
                missing = [s for s in symbols if s not in info]
                if missing:
                    return self._send_json(400, {"error": f"존재하지 않는 종목 코드입니다: {', '.join(missing)}"})

            save_watchlist(symbols)
            return self._send_json(200, {"ok": True, "symbols": symbols})

        if path == "/api/ma-rules":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})

            rules, error = validate_ma_rules(data.get("rules"))
            if error:
                return self._send_json(400, {"error": error})

            # 새로 추가된 종목이 실제 존재하는지 토스 API로 확인 (관심종목/리밸런싱과 동일한 관례)
            all_symbols = sorted({r["symbol"] for r in rules})
            if all_symbols:
                try:
                    info = call_with_reauth(session, lambda token: get_stock_info(token, all_symbols))
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else 502
                    return self._send_json(status, {"error": f"토스 API 오류 ({status})"})
                except requests.RequestException:
                    return self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
                missing = [s for s in all_symbols if s not in info]
                if missing:
                    return self._send_json(400, {"error": f"존재하지 않는 종목 코드입니다: {', '.join(missing)}"})

            save_ma_rules(rules)
            return self._send_json(200, {"ok": True, "rules": rules})

        if path == "/api/strategy-config":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})

            config, error = validate_strategy_config(data)
            if error:
                return self._send_json(400, {"error": error})

            if config["excluded_symbols"]:
                try:
                    info = call_with_reauth(session, lambda token: get_stock_info(token, config["excluded_symbols"]))
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else 502
                    return self._send_json(status, {"error": f"토스 API 오류 ({status})"})
                except requests.RequestException:
                    return self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
                missing = [s for s in config["excluded_symbols"] if s not in info]
                if missing:
                    return self._send_json(400, {"error": f"존재하지 않는 종목 코드입니다: {', '.join(missing)}"})

            save_strategy_config(config)
            return self._send_json(200, {"ok": True, **config})

        if path == "/api/logout":
            sid = _session_id_from_cookie(self)
            if sid:
                with _lock:
                    _sessions.pop(sid, None)
            expired_cookie = f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            return self._send_json(200, {"ok": True}, set_cookie=expired_cookie)

        self._send_json(404, {"error": "not found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"http://{HOST}:{PORT} 에서 실행 중 (Ctrl+C로 종료)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
