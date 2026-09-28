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

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

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
# 사용자별 저장소: data/users/<user_id>/ 아래에 기능별 JSON 파일(아래 *_FILE 이름). user_id는 토스 계좌번호를
# 서버 비밀값으로 HMAC한 값이라 폴더 이름만 봐서는 계좌번호를 알 수 없다. data/는 통째로 .gitignore.
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"
SERVER_SECRET_FILE = DATA_DIR / "server_secret"
LEGACY_MIGRATED_MARKER = DATA_DIR / "legacy_migrated"
CASH_SYMBOLS_FILE = "cash_symbols.json"
CASH_SYMBOLS_MAX = 30
REBALANCE_CONFIG_FILE = "rebalance.json"
REBALANCE_REST_LABEL = "나머지"
REBALANCE_CASH_LABEL = "현금"  # "나머지" 버킷에 예수금/현금 취급 종목만 있을 때 쓰는 표시 이름 (real/과 동일한 관례)
REBALANCE_RESERVED_LABELS = {REBALANCE_REST_LABEL, REBALANCE_CASH_LABEL}
REBALANCE_MAX_CATEGORIES = 30
REBALANCE_MAX_SYMBOLS_PER_CATEGORY = 20
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,10}$")
WATCHLIST_FILE = "watchlist.json"
WATCHLIST_MAX = 30
MA_RULES_FILE = "ma_rules.json"
MA_RULES_MAX = 30
MA_INTERVALS = {"day", "week"}
MA_MAX_PERIOD = {"day": 200, "week": 300}  # 캔들 페이지네이션으로 무리없이 받아올 수 있는 상한
STRATEGY_TEMPLATES_FILE = "strategy_templates.json"
STRATEGY_STATE_FILE = "strategy_state.json"
STRATEGY_MAX_STAGES = 10
WEEKLY_REPORT_FILE = "weekly_report.json"
WEEKLY_REPORT_GROUP_TYPES = {"basic", "sector", "dividend", "risk", "fx_account", "my_account"}  # my_account: 종목은 토스 보유종목에서 자동
WEEKLY_REPORT_ITEM_KINDS = {"price", "rate", "level"}  # rate: 금리(%p 변화), level: 환율·공포지수(고점 대비 대신 52주 위치)
WEEKLY_REPORT_MAX_GROUPS = 12
WEEKLY_REPORT_MAX_ITEMS = 40
WEEKLY_REPORT_MAX_TOTAL = 150
WEEKLY_REPORT_MAX_MA = 6
# 자동 코멘트 기준값 (설정 패널에서 편집). near_ma_pct: 최근접 MA가 ±이 % 안이면 "시험 중",
# calm_position_pct: 공포지수 52주 위치가 이 % 이하면 "평온", sector_top_n: 강세/약세로 뽑을 개수.
WEEKLY_REPORT_COMMENT_DEFAULTS = {"near_ma_pct": 1.0, "calm_position_pct": 20.0, "sector_top_n": 2}
STRATEGY_MAX_TEMPLATES = 20
STRATEGY_INDICATORS = {"MA", "RSI", "CHANGE"}  # 단계별로 쓸 수 있는 지표 종류 (지난번 지표 레지스트리 프로토타입과 동일)
STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
STRATEGY_DEFAULT_TEMPLATES = {
    "templates": {
        "ma-default": {
            "label": "MA 기본 전략 (real/ 설정값)",
            "assign_mode": "order",  # real/은 "몇 번째 MA가 됐든 터치한 순서"로 비율을 매겼음(지표 고정 아님)
            "steps": [
                {"id": "s1", "indicator": "MA", "params": {"interval": "week", "period": 60}, "buy_pct": 5.0},
                {"id": "s2", "indicator": "MA", "params": {"interval": "week", "period": 120}, "buy_pct": 10.0},
                {"id": "s3", "indicator": "MA", "params": {"interval": "week", "period": 200}, "buy_pct": 15.0},
                {"id": "s4", "indicator": "MA", "params": {"interval": "week", "period": 240}, "buy_pct": 20.0},
            ],
            # 회복 판정: steps를 전부 다 돌파한 뒤, 이 중 하나라도 조건이 반대로 뒤집히면(=상방 돌파) recovery_pct 채워 완성.
            "recovery_steps": [
                {"id": "r1", "indicator": "MA", "params": {"interval": "week", "period": 20}},
                {"id": "r2", "indicator": "MA", "params": {"interval": "week", "period": 60}},
                {"id": "r3", "indicator": "MA", "params": {"interval": "week", "period": 120}},
                {"id": "r4", "indicator": "MA", "params": {"interval": "week", "period": 200}},
                {"id": "r5", "indicator": "MA", "params": {"interval": "week", "period": 240}},
            ],
            "recovery_pct": 50.0,
            "multiplier_steps": [],
            "multiplier_factor": 1.0,
        }
    },
    "assignments": {},  # {symbol: template_name} - 배정 안 된 종목은 전략 대상에서 빠짐
    "excluded_symbols": [],
}

# 세션 저장소: 메모리에만 유지, 디스크 기록 없음.
# { session_id: {"user_id", "app_key", "app_secret", "token", "issued_at", "last_seen"} }
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_cash_symbols_lock = threading.Lock()  # cash_symbols.json 동시 수정 방지
_rebalance_lock = threading.Lock()  # rebalance.json 동시 수정 방지
_watchlist_lock = threading.Lock()  # watchlist.json 동시 수정 방지
_ma_rules_lock = threading.Lock()  # ma_rules.json 동시 수정 방지
_strategy_templates_lock = threading.Lock()  # strategy_templates.json 동시 수정 방지
_weekly_report_lock = threading.Lock()  # weekly_report.json 동시 수정 방지
_server_secret_lock = threading.Lock()
_migration_lock = threading.Lock()
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


# ---------- 사용자별 저장소 ----------
# 웹 서비스로 배포할 것을 염두에 두고, 설정·기록 파일을 서버 하나에 공용으로 두지 않고 사용자마다 따로 둔다.
# 사용자 구분은 토스 계좌번호(가장 작은 accountSeq 계좌)로 한다 — 앱키는 재발급하면 바뀌지만 계좌번호는 그대로라서.


class UserStore:
    """사용자 한 명의 파일 위치(data/users/<user_id>/). 파일은 저장할 때 처음 생긴다."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, name: str) -> Path:
        return self.root / name


USER_DATA_FILES = [CASH_SYMBOLS_FILE, REBALANCE_CONFIG_FILE, WATCHLIST_FILE, MA_RULES_FILE,
                   STRATEGY_TEMPLATES_FILE, STRATEGY_STATE_FILE, WEEKLY_REPORT_FILE]
USER_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")


def _server_secret() -> bytes:
    """user_id를 만들 때 쓰는 서버 비밀값. 처음 한 번 만들어 data/server_secret(권한 600)에 둔다."""
    with _server_secret_lock:
        try:
            return bytes.fromhex(SERVER_SECRET_FILE.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_bytes(32)
            SERVER_SECRET_FILE.write_text(secret.hex(), encoding="utf-8")
            os.chmod(SERVER_SECRET_FILE, 0o600)
            return secret


def user_id_for_accounts(accounts: list[dict]) -> str:
    """계좌 목록 → user_id. 계좌번호 자체는 저장하지 않고 HMAC 값만 폴더 이름으로 쓴다."""
    valid = [a for a in accounts if str(a.get("accountNo") or "").strip()]
    if not valid:
        raise ValueError("계좌번호를 찾을 수 없습니다.")
    primary = min(valid, key=lambda a: to_float(a.get("accountSeq"), float("inf")))
    return hmac.new(_server_secret(), str(primary["accountNo"]).strip().encode(), hashlib.sha256).hexdigest()[:24]


def store_for(session: dict) -> UserStore:
    uid = session["user_id"]
    if not USER_ID_PATTERN.fullmatch(uid):  # 경로 조작 방지(세션 값은 서버가 만들지만 방어적으로)
        raise ValueError("잘못된 user_id")
    return UserStore(USERS_DIR / uid)


def migrate_legacy_files(store: UserStore) -> list[str]:
    """사용자별 저장소 도입 전(redo/ 바로 아래)의 설정 파일을 **처음 로그인한 사용자 한 명에게만** 옮긴다.
    그 뒤 로그인하는 다른 사용자는 빈 설정으로 시작. 한 번 옮기면 표시 파일을 남겨 다시 하지 않는다."""
    with _migration_lock:
        if LEGACY_MIGRATED_MARKER.exists():
            return []
        legacy = [name for name in USER_DATA_FILES if (BASE_DIR / name).exists()]
        store.root.mkdir(parents=True, exist_ok=True)
        moved = []
        for name in legacy:
            target = store.path(name)
            if not target.exists():
                (BASE_DIR / name).replace(target)
                moved.append(name)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LEGACY_MIGRATED_MARKER.write_text(json.dumps({"moved": moved, "at": time.time()}), encoding="utf-8")
        return moved


def load_cash_symbols(store: "UserStore") -> list[str]:
    """현금 취급 종목 설정을 읽는다. 파일이 없거나 깨졌으면 빈 목록으로 취급한다(직전 상태를 지우지 않음)."""
    try:
        data = json.loads(store.path(CASH_SYMBOLS_FILE).read_text(encoding="utf-8"))
        symbols = data.get("symbols", [])
        return [s for s in symbols if isinstance(s, str) and s]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def save_cash_symbols(store: "UserStore", symbols: list[str]) -> None:
    with _cash_symbols_lock:
        path = store.path(CASH_SYMBOLS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"symbols": symbols}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def load_watchlist(store: "UserStore") -> list[str]:
    """관심종목 설정을 읽는다. 파일이 없거나 깨졌으면 빈 목록으로 취급한다(직전 상태를 지우지 않음)."""
    try:
        data = json.loads(store.path(WATCHLIST_FILE).read_text(encoding="utf-8"))
        symbols = data.get("symbols", [])
        return [s for s in symbols if isinstance(s, str) and s]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def save_watchlist(store: "UserStore", symbols: list[str]) -> None:
    with _watchlist_lock:
        path = store.path(WATCHLIST_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"symbols": symbols}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def load_ma_rules(store: "UserStore") -> list[dict]:
    """MA 감시 규칙을 읽는다. 파일이 없거나 깨졌으면 빈 목록으로 취급한다(직전 상태를 지우지 않음)."""
    try:
        data = json.loads(store.path(MA_RULES_FILE).read_text(encoding="utf-8"))
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


def save_ma_rules(store: "UserStore", rules: list[dict]) -> None:
    with _ma_rules_lock:
        path = store.path(MA_RULES_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


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


# ---------- 분할매수 전략 (지표 자유 조합 + 종목별 전략 배정) ----------
# "MA 4단계 하방 돌파 → 회복 시 완성"이라는 real/의 로직을 MA 전용으로 하드코딩하지 않고, 각 단계가
# 어떤 지표(MA/RSI/등락률)든 쓸 수 있는 "이름 붙은 전략 템플릿" 여러 개로 일반화했다. 종목마다 원하는
# 템플릿을 배정해서 쓴다(여러 종목이 같은 템플릿을 쓰면 그게 곧 "그룹"). 단계에는 고유 id를 둬서,
# 나중에 단계를 추가/삭제/순서변경해도 기존 진행 상태(state)가 엉뚱한 단계에 매칭되지 않게 한다.


def validate_step(step, allow_buy_pct: bool) -> tuple[dict, str | None]:
    """단계 하나(지표+파라미터, steps면 매수%p 포함)를 검증/정규화한다."""
    if not isinstance(step, dict):
        return {}, "단계 형식이 올바르지 않습니다."
    indicator = step.get("indicator")
    if indicator not in STRATEGY_INDICATORS:
        return {}, f"지원하지 않는 지표입니다: {indicator!r}"
    params = step.get("params")
    if not isinstance(params, dict):
        return {}, "지표 파라미터가 올바르지 않습니다."

    if indicator == "MA":
        interval = params.get("interval")
        if interval not in MA_INTERVALS:
            return {}, "MA 봉 종류는 일봉/주봉이어야 합니다."
        max_period = MA_MAX_PERIOD[interval]
        period = to_float(params.get("period"), -1)
        if not (period.is_integer() and 2 <= period <= max_period):
            return {}, f"MA 기간은 2~{max_period} 사이의 정수여야 합니다."
        clean_params = {"interval": interval, "period": int(period)}
    elif indicator == "RSI":
        period = to_float(params.get("period"), -1)
        if not (period.is_integer() and 2 <= period <= 200):
            return {}, "RSI 기간은 2~200 사이의 정수여야 합니다."
        threshold = to_float(params.get("threshold"), -1)
        if not (0 <= threshold <= 100):
            return {}, "RSI 임계값은 0~100 사이여야 합니다."
        clean_params = {"period": int(period), "threshold": threshold}
    else:  # CHANGE
        threshold_pct = to_float(params.get("threshold_pct"), None)
        if threshold_pct is None or not (-100 <= threshold_pct <= 100):
            return {}, "등락률 기준은 -100~100 사이여야 합니다."
        clean_params = {"threshold_pct": threshold_pct}

    step_id = step.get("id")
    if not (isinstance(step_id, str) and STEP_ID_PATTERN.fullmatch(step_id)):
        step_id = secrets.token_hex(4)
    clean = {"id": step_id, "indicator": indicator, "params": clean_params}

    if allow_buy_pct:
        buy_pct = to_float(step.get("buy_pct"), -1)
        if not (0 < buy_pct <= 100):
            return {}, "매수 비율(%p)은 0보다 크고 100 이하여야 합니다."
        clean["buy_pct"] = buy_pct
    return clean, None


def validate_strategy_templates(data) -> tuple[dict, str | None]:
    """전략 템플릿 전체(템플릿들 + 종목 배정 + 전략 제외 종목)를 검증/정규화한다."""
    if not isinstance(data, dict):
        return {}, "잘못된 요청입니다."
    raw_templates = data.get("templates")
    if not isinstance(raw_templates, dict) or not raw_templates:
        return {}, "전략 템플릿을 최소 1개 이상 등록하세요."
    if len(raw_templates) > STRATEGY_MAX_TEMPLATES:
        return {}, f"전략 템플릿은 최대 {STRATEGY_MAX_TEMPLATES}개까지 만들 수 있습니다."

    clean_templates: dict[str, dict] = {}
    for name, tpl in raw_templates.items():
        if not isinstance(name, str) or not (1 <= len(name) <= 30):
            return {}, "템플릿 이름은 1~30자여야 합니다."
        if not isinstance(tpl, dict):
            return {}, f"템플릿 형식이 올바르지 않습니다: {name!r}"
        label = str(tpl.get("label", name)).strip()[:40] or name
        assign_mode = tpl.get("assign_mode", "fixed")
        if assign_mode not in ("fixed", "order"):
            return {}, f"'{name}' 템플릿의 배정 방식이 올바르지 않습니다."

        raw_steps = tpl.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return {}, f"'{name}' 템플릿에 단계를 최소 1개 이상 등록하세요."
        if len(raw_steps) > STRATEGY_MAX_STAGES:
            return {}, f"'{name}' 템플릿의 단계는 최대 {STRATEGY_MAX_STAGES}개까지 가능합니다."
        clean_steps, seen_ids = [], set()
        for s in raw_steps:
            clean_s, err = validate_step(s, allow_buy_pct=True)
            if err:
                return {}, f"'{name}' 템플릿: {err}"
            if clean_s["id"] in seen_ids:
                clean_s["id"] = secrets.token_hex(4)
            seen_ids.add(clean_s["id"])
            clean_steps.append(clean_s)

        raw_recovery = tpl.get("recovery_steps", [])
        if not isinstance(raw_recovery, list):
            return {}, f"'{name}' 템플릿의 회복 단계 형식이 올바르지 않습니다."
        if len(raw_recovery) > STRATEGY_MAX_STAGES:
            return {}, f"'{name}' 템플릿의 회복 판정 단계는 최대 {STRATEGY_MAX_STAGES}개까지 가능합니다."
        clean_recovery, seen_rec_ids = [], set()
        for s in raw_recovery:
            clean_s, err = validate_step(s, allow_buy_pct=False)
            if err:
                return {}, f"'{name}' 템플릿(회복): {err}"
            if clean_s["id"] in seen_rec_ids:
                clean_s["id"] = secrets.token_hex(4)
            seen_rec_ids.add(clean_s["id"])
            clean_recovery.append(clean_s)

        recovery_pct = to_float(tpl.get("recovery_pct"), 0.0)
        if not (0 <= recovery_pct <= 100):
            return {}, f"'{name}' 템플릿의 회복 매수 비율(%p)은 0~100 사이여야 합니다."
        if raw_recovery and recovery_pct <= 0:
            return {}, f"'{name}' 템플릿: 회복 판정 단계가 있으면 회복 매수 비율도 0보다 커야 합니다."

        step_sum = sum(s["buy_pct"] for s in clean_steps)
        if step_sum + recovery_pct > 100:
            return {}, f"'{name}' 템플릿: 단계별 매수 비율 합계 + 회복 매수 비율이 100%를 넘습니다 (현재 {step_sum + recovery_pct:.1f}%)."

        # 배수 규칙(선택): 회복 판정과 달리 단계를 전부 안 밟아도 먼저 발동할 수 있음 - 한 번이라도
        # 조건이 충족되면(래칫) 그 뒤로는 매수 단계 합계 전체에 배수를 곱해서 계산한다.
        raw_multiplier = tpl.get("multiplier_steps", [])
        if not isinstance(raw_multiplier, list):
            return {}, f"'{name}' 템플릿의 배수 규칙 형식이 올바르지 않습니다."
        if len(raw_multiplier) > STRATEGY_MAX_STAGES:
            return {}, f"'{name}' 템플릿의 배수 규칙 조건은 최대 {STRATEGY_MAX_STAGES}개까지 가능합니다."
        clean_multiplier, seen_mult_ids = [], set()
        for s in raw_multiplier:
            clean_s, err = validate_step(s, allow_buy_pct=False)
            if err:
                return {}, f"'{name}' 템플릿(배수 규칙): {err}"
            if clean_s["id"] in seen_mult_ids:
                clean_s["id"] = secrets.token_hex(4)
            seen_mult_ids.add(clean_s["id"])
            clean_multiplier.append(clean_s)

        multiplier_factor = to_float(tpl.get("multiplier_factor"), 1.0)
        if not (1 <= multiplier_factor <= 10):
            return {}, f"'{name}' 템플릿의 배수는 1~10 사이여야 합니다."
        if raw_multiplier and multiplier_factor <= 1:
            return {}, f"'{name}' 템플릿: 배수 규칙 조건이 있으면 배수는 1보다 커야 합니다."

        clean_templates[name] = {
            "label": label,
            "assign_mode": assign_mode,
            "steps": clean_steps,
            "recovery_steps": clean_recovery,
            "recovery_pct": recovery_pct,
            "multiplier_steps": clean_multiplier,
            "multiplier_factor": multiplier_factor,
        }

    raw_assignments = data.get("assignments", {})
    if not isinstance(raw_assignments, dict):
        return {}, "종목 배정 형식이 올바르지 않습니다."
    clean_assignments: dict[str, str] = {}
    for symbol, tpl_name in raw_assignments.items():
        if not isinstance(symbol, str):
            continue
        sym = symbol.strip().upper()
        if not sym:
            continue
        if not SYMBOL_PATTERN.fullmatch(sym):
            return {}, f"잘못된 종목 코드입니다: {symbol!r}"
        if tpl_name not in clean_templates:
            return {}, f"'{sym}'에 배정된 템플릿 '{tpl_name}'이 존재하지 않습니다."
        clean_assignments[sym] = tpl_name

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

    return {"templates": clean_templates, "assignments": clean_assignments, "excluded_symbols": clean_excluded}, None


def load_strategy_templates(store: "UserStore") -> dict:
    """전략 템플릿+배정을 읽는다. 파일이 없거나 깨졌으면 real/에서 쓰던 기본 템플릿(MA 60/120/200/240주
    5/10/15/20%p, 회복 20/60/120/200/240주 +50%p, 배정 없음)으로 취급한다."""
    try:
        data = json.loads(store.path(STRATEGY_TEMPLATES_FILE).read_text(encoding="utf-8"))
        clean, error = validate_strategy_templates(data)
        return copy.deepcopy(STRATEGY_DEFAULT_TEMPLATES) if error else clean
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return copy.deepcopy(STRATEGY_DEFAULT_TEMPLATES)


def save_strategy_templates(store: "UserStore", data: dict) -> None:
    with _strategy_templates_lock:
        path = store.path(STRATEGY_TEMPLATES_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def load_strategy_state(store: "UserStore") -> dict[str, dict]:
    """종목별 진행 상태(돌파 카운트된 단계 id들 + 회복/배수 발동 여부)를 읽는다. 설정 파일과 달리 시간에
    따라 쌓이는 기록이라, 저장 형식은 {symbol: {"broken_step_ids": [...], "recovered": bool, "multiplier_triggered": bool}}."""
    try:
        data = json.loads(store.path(STRATEGY_STATE_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        clean: dict[str, dict] = {}
        for symbol, entry in data.items():
            ids = entry.get("broken_step_ids") if isinstance(entry, dict) else None
            if isinstance(ids, list):
                clean[symbol] = {
                    "broken_step_ids": [str(i) for i in ids if isinstance(i, str)],
                    "recovered": bool(entry.get("recovered", False)),
                    "multiplier_triggered": bool(entry.get("multiplier_triggered", False)),
                }
        return clean
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return {}


def save_strategy_state(store: "UserStore", state: dict) -> None:
    with _strategy_state_lock:
        path = store.path(STRATEGY_STATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def held_symbols(token: str, accounts: list[dict]) -> set[str]:
    """전략 대상 후보(보유종목 심볼만 가볍게) - fetch_holdings_data 전체 계산은 필요 없어서 따로 둠."""
    symbols: set[str] = set()
    for account in accounts:
        holdings = get_holdings(token, account["accountSeq"])
        for h in holdings.get("items", []):
            if h.get("symbol") and to_float(h.get("quantity")) != 0:
                symbols.add(h["symbol"])
    return symbols


def gather_market_data(token: str, symbol: str, steps: list[dict], cur_price: float) -> dict:
    """이 종목의 단계들을 평가하는 데 필요한 캔들 데이터를 지표 요구사항에 맞춰 한 번씩만 모은다."""
    weekly_needed = max((s["params"]["period"] for s in steps if s["indicator"] == "MA" and s["params"]["interval"] == "week"), default=0)
    daily_ma_needed = max((s["params"]["period"] for s in steps if s["indicator"] == "MA" and s["params"]["interval"] == "day"), default=0)
    rsi_needed = max((s["params"]["period"] + 1 for s in steps if s["indicator"] == "RSI"), default=0)
    change_needed = 2 if any(s["indicator"] == "CHANGE" for s in steps) else 0
    daily_needed = max(daily_ma_needed, rsi_needed, change_needed)

    market_data = {"price": cur_price}
    try:
        if daily_needed:
            market_data["daily_closes"] = fetch_daily_closes(token, symbol, daily_needed)
        if weekly_needed:
            market_data["weekly_closes"] = fetch_weekly_closes(token, symbol, weekly_needed)
    except requests.exceptions.RequestException:
        pass
    return market_data


def eval_step(step: dict, market_data: dict) -> dict:
    """step 하나를 지금 시세로 평가한다. triggered=True면 "매수 조건 충족"(MA 아래/RSI 과매도/큰 하락) 상태.
    회복 판정에 재사용할 때는 호출부에서 `not triggered`로 뒤집어서 쓴다(같은 조건의 반대 = 회복)."""
    indicator = step["indicator"]
    params = step["params"]
    price = market_data.get("price", 0.0)

    if indicator == "MA":
        period = params["period"]
        closes = market_data.get("weekly_closes" if params["interval"] == "week" else "daily_closes", [])
        if len(closes) < period or price <= 0:
            return {"ok": False}
        ma_value = sum(closes[:period]) / period
        diff_pct = (price - ma_value) / ma_value * 100 if ma_value else 0.0
        unit = "주" if params["interval"] == "week" else "일"
        return {"ok": True, "triggered": diff_pct <= 0, "label": f"MA{period}{unit} {diff_pct:+.1f}%"}

    if indicator == "RSI":
        period = params["period"]
        closes = market_data.get("daily_closes", [])
        if len(closes) < period + 1:
            return {"ok": False}
        recent = closes[: period + 1]
        diffs = [recent[i] - recent[i + 1] for i in range(period)]
        gains = [d for d in diffs if d > 0]
        losses = [-d for d in diffs if d < 0]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        return {"ok": True, "triggered": rsi <= params["threshold"], "label": f"RSI({period}) {rsi:.1f}"}

    # CHANGE
    closes = market_data.get("daily_closes", [])
    if len(closes) < 2 or price <= 0:
        return {"ok": False}
    prev_close = closes[1]
    change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
    return {"ok": True, "triggered": change_pct <= params["threshold_pct"], "label": f"등락률 {change_pct:+.1f}%"}


def compute_strategy_rows(
    token: str, templates: dict, assignments: dict, excluded: set[str], state: dict, symbols: list[str]
) -> tuple[list[dict], bool]:
    """종목별로 배정된 템플릿에 따라 분할매수 진행률을 계산하고, 새로 돌파/회복한 게 있으면 state를 갱신한다.
    단계(steps)를 하방 돌파(래칫)할 때마다 매수%p를 누적하고, 전 단계를 다 돌파한 뒤 회복 단계
    (recovery_steps) 중 하나라도 조건이 반대로 뒤집히면(=상방 돌파) 회복 매수%p를 더해 완성한다.
    배정 안 됐거나 전략 제외된 종목은 계산 대상에서 빠진다. 반환: (rows, state가 바뀌었는지)."""
    targets = [s for s in symbols if s not in excluded and s in assignments]
    if not targets:
        return [], False

    info = lookup_stocks(token, targets)
    prices = get_prices(token, targets)
    state_changed = False
    rows: list[dict] = []

    for symbol in targets:
        template = templates[assignments[symbol]]
        steps = template["steps"]
        recovery_steps = template["recovery_steps"]
        recovery_pct = template["recovery_pct"]
        multiplier_steps = template.get("multiplier_steps", [])
        multiplier_factor = template.get("multiplier_factor", 1.0)

        meta = info.get(symbol, {})
        cur_price = prices.get(symbol, 0.0)
        prev_entry = state.get(symbol, {"broken_step_ids": [], "recovered": False, "multiplier_triggered": False})
        broken_ids = set(prev_entry["broken_step_ids"])
        recovered = prev_entry["recovered"]
        multiplier_triggered = prev_entry.get("multiplier_triggered", False)

        row_base = {
            "symbol": symbol,
            "name": meta.get("name", symbol),
            "currency": meta.get("currency", "?"),
            "template": assignments[symbol],
            "template_label": template["label"],
        }
        if cur_price <= 0:
            rows.append({**row_base, "ok": False, "reason": "현재가 조회 실패"})
            continue

        market_data = gather_market_data(token, symbol, steps + recovery_steps + multiplier_steps, cur_price)

        step_results = []
        for step in steps:
            result = eval_step(step, market_data)
            if result.get("ok") and result["triggered"]:  # 한 번 돌파되면(래칫) 계속 카운트 유지
                broken_ids.add(step["id"])
            step_results.append({"id": step["id"], "buy_pct": step["buy_pct"], "broken": step["id"] in broken_ids, **result})

        # 회복 판정: 설정된 단계(steps)를 전부 다 돌파한 뒤에만 시작한다(real/과 동일한 게이팅) - 한
        # 단계만 돌파된 상태에서 곧장 회복 보너스가 붙으면 절반만 채운 채로 100%가 돼버리기 때문.
        recovery_results = []
        if len(broken_ids) >= len(steps) and not recovered:
            for rstep in recovery_steps:
                result = eval_step(rstep, market_data)
                is_recovered = result.get("ok") and not result["triggered"]
                if is_recovered:
                    recovered = True
                recovery_results.append({"id": rstep["id"], "recovered": bool(is_recovered), **result})
        elif recovered:
            recovery_results = [{"id": r["id"], "recovered": True, "ok": True} for r in recovery_steps]

        # 배수 규칙: 회복 판정과 달리 게이팅이 없다(전 단계를 다 안 밟아도 먼저 터질 수 있음) - 한 번이라도
        # 조건이 충족되면(래칫) 그 뒤로는 매수 단계 합계 전체(아래 step_pct_sum)에 배수를 곱한다.
        multiplier_results = []
        if multiplier_steps and not multiplier_triggered:
            for mstep in multiplier_steps:
                result = eval_step(mstep, market_data)
                if result.get("ok") and result["triggered"]:
                    multiplier_triggered = True
                multiplier_results.append({"id": mstep["id"], "triggered": bool(result.get("ok") and result["triggered"]), **result})
        elif multiplier_triggered:
            multiplier_results = [{"id": m["id"], "triggered": True, "ok": True} for m in multiplier_steps]

        if (
            broken_ids != set(prev_entry["broken_step_ids"])
            or recovered != prev_entry["recovered"]
            or multiplier_triggered != prev_entry.get("multiplier_triggered", False)
        ):
            state[symbol] = {"broken_step_ids": sorted(broken_ids), "recovered": recovered, "multiplier_triggered": multiplier_triggered}
            state_changed = True

        # 배정 방식: "fixed"는 각 단계(지표)에 고정된 매수%p(id로 매칭). "order"는 real/처럼 "몇 번째
        # 단계가 됐든 지금까지 몇 개가 트리거됐는지"만 보고 앞에서부터 순서대로 %p를 채워 쓴다(이동평균이
        # 꼬여서 어떤 게 먼저 깨지든 상관없이 "N번째로 닿은 것"이면 동일하게 취급).
        if template.get("assign_mode") == "order":
            step_pct_sum = sum(s["buy_pct"] for s in steps[: len(broken_ids)])
        else:
            step_pct_sum = sum(s["buy_pct"] for s in steps if s["id"] in broken_ids)
        if multiplier_triggered:
            step_pct_sum *= multiplier_factor
        entry_pct = min(100.0, step_pct_sum + (recovery_pct if recovered else 0.0))
        rows.append(
            {
                **row_base,
                "ok": True,
                "price": cur_price,
                "entry_pct": entry_pct,
                "stage_count": len(broken_ids),
                "stage_total": len(steps),
                "steps": step_results,
                "recovered": recovered,
                "recovery_steps": recovery_results,
                "multiplier_triggered": multiplier_triggered,
                "multiplier_factor": multiplier_factor,
                "multiplier_steps": multiplier_results,
            }
        )

    return rows, state_changed


def load_rebalance_config(store: "UserStore") -> dict:
    """리밸런싱 목표 설정을 읽는다. 파일이 없거나 깨졌으면 기본값(카테고리 없음, 미분류 종목은 각각 5%,
    원/달러 목표 비중은 50/50)으로 취급한다."""
    default = {"targets": [], "default_rest_target_pct": 5.0, "krw_target_pct": 50.0}
    try:
        data = json.loads(store.path(REBALANCE_CONFIG_FILE).read_text(encoding="utf-8"))
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
            # 같은 카테고리 안의 중복(예: "aaa", "AAA")은 아래에서 조용히 합치고, 다른 카테고리와 겹칠 때만 에러
            if sym in seen_symbols and seen_symbols[sym] != label:
                return [], f'같은 종목을 두 카테고리에 넣을 수 없습니다: {sym} ("{seen_symbols[sym]}"와(과) "{label}")'
            seen_symbols[sym] = label
            if sym not in symbols:
                symbols.append(sym)

        target_pct = to_float(t.get("target_pct"), -1)
        if not (0 <= target_pct <= 100):
            return [], f'"{label}"의 목표%는 0~100 사이 숫자여야 합니다.'

        clean.append({"label": label, "symbols": symbols, "target_pct": target_pct})

    return clean, None


def save_rebalance_config(store: "UserStore", targets: list[dict], default_rest_target_pct: float, krw_target_pct: float) -> None:
    with _rebalance_lock:
        path = store.path(REBALANCE_CONFIG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
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
        tmp.replace(path)


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


def fetch_holdings_data(token: str, accounts: list[dict], store: "UserStore") -> dict:
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
    cash_symbols = set(load_cash_symbols(store))

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

    rebalance_config = load_rebalance_config(store)
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


# ---------------------------------------------------------------------------
# 주간 리포트
# 지수/금리/VIX/환율 과거 데이터는 토스 API에 없어서 전부 Yahoo Finance(비공식) 일봉으로 계산한다.
# 종목 묶음(그룹)과 종목 목록은 weekly_report.json에 저장하고 대시보드 설정 패널에서 편집한다.
# ---------------------------------------------------------------------------

YAHOO_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.^=\-]{1,20}$")
YAHOO_HISTORY_CACHE_TTL = 30 * 60  # 초. 같은 종목 일봉을 30분 안에 다시 받지 않는다.
WEEKLY_REPORT_CACHE_TTL = 30 * 60
YAHOO_HISTORY_YEARS = 6  # 240주 MA(약 4.6년)를 계산할 수 있을 만큼
WEEKLY_REPORT_MAX_WORKERS = 8


def default_weekly_report_config() -> dict:
    """처음 쓰는 사람용 기본 구성. 실제 값은 전부 weekly_report.json + 설정 패널에서 바꾼다."""
    def items(pairs, kind="price"):
        return [{"symbol": s, "label": l, "kind": kind} for s, l in pairs]

    return {
        "ma_periods": [60, 120, 200, 240],
        "comment_rules": dict(WEEKLY_REPORT_COMMENT_DEFAULTS),
        "benchmark": "^GSPC",
        "rate_symbol": "^TNX",
        "fx_symbol": "KRW=X",
        "groups": [
            {"title": "지수·장기채", "type": "basic", "items":
                items([("^DJI", "다우"), ("^GSPC", "S&P 500"), ("^NDX", "나스닥 100"), ("TLT", "TLT")])
                + items([("^TNX", "10년물 금리"), ("^TYX", "30년물 금리")], "rate")},
            {"title": "원자재", "type": "basic", "items": items([("CL=F", "WTI 원유")])},
            {"title": "섹터 (SPDR)", "type": "sector", "items": items([
                ("XLK", "기술"), ("XLC", "커뮤니케이션"), ("XLV", "헬스케어"), ("XLI", "산업재"),
                ("XLB", "소재"), ("XLY", "임의소비재"), ("XLP", "필수소비재"), ("XLF", "금융"),
                ("XLRE", "부동산"), ("XLE", "에너지"), ("XLU", "유틸리티")])},
            {"title": "빅테크", "type": "sector", "items": items([
                ("AAPL", "애플"), ("MSFT", "마이크로소프트"), ("NVDA", "엔비디아"), ("GOOGL", "알파벳"),
                ("AMZN", "아마존"), ("META", "메타"), ("TSLA", "테슬라"), ("AVGO", "브로드컴")])},
            {"title": "고배당주", "type": "dividend", "items": items([
                ("JEPI", "JEPI"), ("MO", "알트리아"), ("PFE", "화이자"), ("VZ", "버라이즌"), ("O", "리얼티인컴"),
                ("PEP", "펩시코"), ("T", "AT&T"), ("CVX", "셰브론"), ("SCHD", "SCHD"), ("PG", "P&G"),
                ("ABBV", "애브비"), ("XOM", "엑슨모빌"), ("KO", "코카콜라"), ("VYM", "VYM"), ("JNJ", "존슨앤존슨")])},
            {"title": "S&P 500 + 환율 = 원화 계좌", "type": "fx_account", "items":
                items([("^GSPC", "S&P 500")]) + items([("KRW=X", "원/달러")], "level")},
            {"title": "내 계좌", "type": "my_account", "items": []},
            {"title": "위험 신호 (신용·변동성)", "type": "risk", "items":
                items([("^VIX", "VIX (주식 공포 30일)"), ("^VIX3M", "VIX3M (주식 공포 3개월)"),
                       ("^MOVE", "MOVE (채권 공포)")], "level")
                + items([("HYG", "HYG (하이일드)"), ("BKLN", "BKLN (레버리지론)"), ("BIZD", "BIZD (사모대출 BDC)"),
                         ("ORCL", "ORCL (AI 부채)"), ("CRWV", "CRWV (네오클라우드)")])},
        ],
    }


def _normalize_weekly_report_config(data) -> dict:
    """설정 검증 + 정규화. 잘못되면 ValueError(사용자에게 그대로 보여줄 메시지)."""
    if not isinstance(data, dict):
        raise ValueError("설정 형식이 잘못되었습니다.")

    periods = data.get("ma_periods")
    if not isinstance(periods, list) or not 1 <= len(periods) <= WEEKLY_REPORT_MAX_MA:
        raise ValueError(f"MA 기간은 1~{WEEKLY_REPORT_MAX_MA}개여야 합니다.")
    clean_periods: list[int] = []
    for p in periods:
        if isinstance(p, bool) or not isinstance(p, (int, float)) or int(p) != p or not 2 <= p <= MA_MAX_PERIOD["week"]:
            raise ValueError(f"MA 기간은 2~{MA_MAX_PERIOD['week']} 사이 정수여야 합니다.")
        if int(p) in clean_periods:
            raise ValueError(f"MA 기간이 중복되었습니다: {int(p)}")
        clean_periods.append(int(p))
    clean_periods.sort()

    def _sym(v, what):
        if not isinstance(v, str) or not YAHOO_SYMBOL_PATTERN.fullmatch(v.strip()):
            raise ValueError(f"{what} 심볼이 잘못되었습니다: {v!r}")
        return v.strip().upper()

    benchmark = _sym(data.get("benchmark"), "비교 기준")
    rate_symbol = _sym(data.get("rate_symbol"), "배당 비교 금리")
    fx_symbol = _sym(data.get("fx_symbol") or "KRW=X", "원화 환산 환율")  # 예전 파일엔 없음 → 기본값

    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("그룹이 하나 이상 있어야 합니다.")
    if len(groups) > WEEKLY_REPORT_MAX_GROUPS:
        raise ValueError(f"그룹은 최대 {WEEKLY_REPORT_MAX_GROUPS}개까지 만들 수 있습니다.")
    clean_groups = []
    total = 0
    for g in groups:
        if not isinstance(g, dict):
            raise ValueError("그룹 형식이 잘못되었습니다.")
        title = str(g.get("title") or "").strip()
        if not title or len(title) > 40:
            raise ValueError("그룹 이름은 1~40자여야 합니다.")
        gtype = g.get("type")
        if gtype not in WEEKLY_REPORT_GROUP_TYPES:
            raise ValueError(f"'{title}' 그룹의 표 종류가 잘못되었습니다.")
        raw_items = g.get("items")
        if not isinstance(raw_items, list):
            raise ValueError(f"'{title}' 그룹의 종목 목록이 잘못되었습니다.")
        if len(raw_items) > WEEKLY_REPORT_MAX_ITEMS:
            raise ValueError(f"'{title}' 그룹은 종목이 최대 {WEEKLY_REPORT_MAX_ITEMS}개까지 가능합니다.")
        items = []
        seen = set()
        for it in raw_items:
            if not isinstance(it, dict):
                raise ValueError(f"'{title}' 그룹의 종목 형식이 잘못되었습니다.")
            sym = _sym(it.get("symbol"), f"'{title}' 그룹의 종목")
            if sym in seen:
                raise ValueError(f"'{title}' 그룹에 {sym}이(가) 중복되었습니다.")
            seen.add(sym)
            label = str(it.get("label") or "").strip() or sym
            if len(label) > 40:
                raise ValueError(f"{sym}의 표시 이름은 40자 이하여야 합니다.")
            kind = it.get("kind", "price")
            if kind not in WEEKLY_REPORT_ITEM_KINDS:
                raise ValueError(f"{sym}의 값 종류가 잘못되었습니다.")
            items.append({"symbol": sym, "label": label, "kind": kind})
        if gtype == "my_account" and items:
            raise ValueError(f"'{title}'(내 계좌) 그룹은 종목을 직접 넣지 않습니다 — 토스 보유종목으로 자동 채워집니다.")
        if gtype == "my_account" and any(cg["type"] == "my_account" for cg in clean_groups):
            raise ValueError("내 계좌 그룹은 하나만 만들 수 있습니다.")
        if gtype == "fx_account" and len(items) != 2:
            raise ValueError(f"'{title}'(원화 계좌 환산) 그룹은 [주가 지수, 환율] 정확히 2개 종목이어야 합니다.")
        total += len(items)
        clean_groups.append({"title": title, "type": gtype, "items": items})
    if total > WEEKLY_REPORT_MAX_TOTAL:
        raise ValueError(f"전체 종목은 최대 {WEEKLY_REPORT_MAX_TOTAL}개까지 가능합니다.")

    rules = dict(WEEKLY_REPORT_COMMENT_DEFAULTS)
    raw_rules = data.get("comment_rules")
    if raw_rules is not None:  # 예전에 저장된 파일엔 없을 수 있음 → 기본값
        if not isinstance(raw_rules, dict):
            raise ValueError("코멘트 기준 형식이 잘못되었습니다.")
        limits = {"near_ma_pct": (0, 20), "calm_position_pct": (0, 100), "sector_top_n": (0, 10)}
        for k, (lo, hi) in limits.items():
            if k not in raw_rules:
                continue
            v = raw_rules[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"코멘트 기준 '{k}'는 {lo}~{hi} 사이 숫자여야 합니다.")
            rules[k] = int(v) if k == "sector_top_n" else float(v)

    return {"ma_periods": clean_periods, "comment_rules": rules, "benchmark": benchmark,
            "rate_symbol": rate_symbol, "fx_symbol": fx_symbol, "groups": clean_groups}


def _weekly_report_symbols(config: dict) -> set[str]:
    s = {config["benchmark"], config["rate_symbol"], config["fx_symbol"]}
    for g in config["groups"]:
        s.update(it["symbol"] for it in g["items"])
    return s


def load_weekly_report_config(store: "UserStore") -> dict:
    """파일이 없거나 깨졌으면 기본 구성을 쓴다(파일은 저장할 때만 생김)."""
    try:
        return _normalize_weekly_report_config(json.loads(store.path(WEEKLY_REPORT_FILE).read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return default_weekly_report_config()


def save_weekly_report_config(store: "UserStore", config: dict) -> None:
    with _weekly_report_lock:
        path = store.path(WEEKLY_REPORT_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    with _weekly_report_cache_lock:
        _weekly_report_cache.clear()


_yahoo_history_cache: dict[str, dict] = {}
_yahoo_history_lock = threading.Lock()
_weekly_report_cache: dict[str, dict] = {}
_weekly_report_cache_lock = threading.Lock()


def get_yahoo_daily_history(symbol: str, force: bool = False) -> dict:
    """Yahoo 일봉 {bars: [(date, close, adjclose)], divs: [(date, amount)], name}.

    날짜는 거래소 현지 기준(gmtoffset 반영) — 안 그러면 KRW=X처럼 UTC 전날 23시에 찍히는 종목이
    하루씩 밀려서 주봉 묶음이 틀어진다.
    """
    now = time.time()
    if not force:
        with _yahoo_history_lock:
            cached = _yahoo_history_cache.get(symbol)
            if cached and now - cached["ts"] < YAHOO_HISTORY_CACHE_TTL:
                return cached["data"]

    resp = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
        # range=max는 월봉으로 내려오므로 period1/period2로 일봉을 받는다.
        params={"period1": int(now - YAHOO_HISTORY_YEARS * 366 * 86400), "period2": int(now),
                "interval": "1d", "events": "div"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    result = (resp.json().get("chart") or {}).get("result")
    if not result:
        raise ValueError(f"{symbol}: 데이터 없음")
    r = result[0]
    meta = r.get("meta") or {}
    offset = int(meta.get("gmtoffset") or 0)
    indicators = r.get("indicators") or {}
    closes = (indicators.get("quote") or [{}])[0].get("close") or []
    adj = (indicators.get("adjclose") or [{}])[0].get("adjclose") or closes

    by_date: dict[date, tuple] = {}
    for t, c, a in zip(r.get("timestamp") or [], closes, adj):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + offset, tz=timezone.utc).date()
        by_date[d] = (d, float(c), float(a if a is not None else c))
    bars = [by_date[d] for d in sorted(by_date)]
    if len(bars) < 2:
        raise ValueError(f"{symbol}: 데이터 부족")
    divs = [
        (datetime.fromtimestamp(v["date"] + offset, tz=timezone.utc).date(), float(v["amount"]))
        for v in ((r.get("events") or {}).get("dividends") or {}).values()
        if v.get("date") and v.get("amount") is not None
    ]
    data = {"bars": bars, "divs": divs, "name": meta.get("longName") or meta.get("shortName") or symbol}
    with _yahoo_history_lock:
        _yahoo_history_cache[symbol] = {"ts": now, "data": data}
    return data


def report_week_end(now_ny: datetime) -> date:
    """리포트 기준일 = 마지막으로 '끝난' 주의 금요일. 미국 동부시간으로 판단한다 —
    한국 날짜로 보면 토요일 새벽(미국 금요일 장중)에도 이번 주가 끝난 것으로 잘못 잡힌다.
    금요일 17시(ET) 이후나 주말이면 이번 주 금요일, 그 전이면 지난주 금요일."""
    d = now_ny.date()
    wd = d.weekday()
    if wd >= 5 or (wd == 4 and now_ny.hour >= 17):
        return d - timedelta(days=wd - 4)
    return d - timedelta(days=wd + 3)


def _weekly_series(bars: list[tuple], idx: int) -> list[float]:
    """일봉을 ISO 주 단위로 묶어 주의 마지막 거래일 값을 쓴다(fetch_weekly_closes와 같은 방식)."""
    weeks: dict[tuple, float] = {}
    for b in bars:
        weeks[b[0].isocalendar()[:2]] = b[idx]
    return [weeks[k] for k in sorted(weeks)]


def compute_symbol_week(hist: dict, end: date, periods: list[int]) -> dict:
    bars = [b for b in hist["bars"] if b[0] <= end]
    w = _weekly_series(bars, 1)
    wa = _weekly_series(bars, 2)
    if len(w) < 2:
        raise ValueError("주봉 데이터 부족")
    cur, prev = w[-1], w[-2]
    year = [b[1] for b in bars if b[0] > end - timedelta(days=365)]
    hi, lo = max(year), min(year)

    mas = []
    for p in periods:
        if len(w) < p:
            continue
        ma = sum(w[-p:]) / p
        item = {"period": p, "value": ma, "dist_pct": (cur / ma - 1) * 100, "cross": None}
        if len(w) > p:
            prev_ma = sum(w[-p - 1:-1]) / p
            if prev < prev_ma and cur >= ma:
                item["cross"] = "up"
            elif prev >= prev_ma and cur < ma:
                item["cross"] = "down"
        mas.append(item)
    order = [m["period"] for m in sorted(mas, key=lambda m: -m["value"])]  # 차트 위 → 아래
    broken = [m["period"] for m in sorted(mas, key=lambda m: -m["value"]) if m["value"] > cur]
    nearest = min(mas, key=lambda m: abs(m["dist_pct"])) if mas else None

    slope = None
    p0 = periods[0]
    if len(w) >= p0 + 4:
        now_ma = sum(w[-p0:]) / p0
        old_ma = sum(w[-p0 - 4:-4]) / p0
        slope = (now_ma / old_ma - 1) * 100

    ttm = sum(a for d, a in hist["divs"] if end - timedelta(days=365) < d <= end)
    return {
        "name": hist["name"],
        "last_date": bars[-1][0].isoformat(),
        "stale": bars[-1][0] < end - timedelta(days=6),  # 이번 주 거래 기록이 없음
        "close": cur,
        "change_pct": (cur / prev - 1) * 100,
        "change_pt": cur - prev,
        "total_return_pct": (wa[-1] / wa[-2] - 1) * 100,
        "drawdown_52w_pct": (cur / hi - 1) * 100,
        "position_52w_pct": (cur - lo) / (hi - lo) * 100 if hi > lo else None,
        "mas": mas,
        "ma_order": order,
        "ma_alignment": "up" if order == sorted(order) else ("down" if order == sorted(order, reverse=True) else None),
        "broken": broken,
        "nearest": {"period": nearest["period"], "dist_pct": nearest["dist_pct"]} if nearest else None,
        "slope_pct": slope,
        "slope_period": p0,
        "dividend_ttm": ttm,
        "dividend_yield_pct": ttm / cur * 100 if ttm else None,
        "weeks": len(w),
    }


def _fmt_signed(v: float, suffix: str = "%") -> str:
    return f"{v:+.2f}{suffix}"


def build_group_comments(group: dict, rules: dict, bench_symbol: str, rate: dict | None) -> list[str]:
    """숫자에서 바로 나오는 문장만 만든다(해석·전망은 넣지 않음)."""
    rows = [r for r in group["rows"] if "error" not in r]
    out: list[str] = []
    gtype = group["type"]

    if gtype == "sector" and rows and rules["sector_top_n"] > 0:
        n = rules["sector_top_n"]
        ranked = sorted(rows, key=lambda r: -r["rel_pct"]) if all(r["rel_pct"] is not None for r in rows) else []
        if len(ranked) > n:
            top = ", ".join(f"{r['label']} {_fmt_signed(r['rel_pct'], '%p')}" for r in ranked[:n] if r["rel_pct"] > 0)
            bottom = ", ".join(f"{r['label']} {_fmt_signed(r['rel_pct'], '%p')}" for r in ranked[::-1][:n] if r["rel_pct"] < 0)
            if top:
                out.append(f"{bench_symbol} 대비 강세: {top}")
            if bottom:
                out.append(f"{bench_symbol} 대비 약세: {bottom}")

    if gtype == "dividend" and rate:
        with_yield = [r for r in rows if r["spread_pct"] is not None]
        below = [r for r in with_yield if r["spread_pct"] < 0]
        if with_yield:
            out.append(f"{len(with_yield)}개 중 {len(below)}개가 {rate['symbol']}({rate['value']:.2f}%)보다 배당률이 낮음")

    if gtype == "fx_account" and group.get("account_pct") is not None and len(rows) == 2:
        a, b = rows[0]["change_pct"], rows[1]["change_pct"]
        acc = group["account_pct"]
        if a * b < 0:
            out.append(f"{rows[0]['label']} {_fmt_signed(a)}였지만 {rows[1]['label']} {_fmt_signed(b)}로 원화 기준 {_fmt_signed(acc)}")
        elif a != 0 and b != 0:
            out.append(f"{rows[0]['label']}와 {rows[1]['label']}가 같은 방향 → 원화 기준 {_fmt_signed(acc)}로 {'확대' if abs(acc) > abs(a) else '축소'}")

    price = [r for r in rows if r["kind"] == "price" and len(r["mas"]) >= 2]
    down = [r["label"] for r in price if r["ma_alignment"] == "down" and len(r["broken"]) == len(r["mas"])]
    up = [r["label"] for r in price if r["ma_alignment"] == "up" and not r["broken"]]
    if down:
        out.append(f"하락 추세 (역배열 + 모든 선 아래) {len(down)}/{len(rows)}: {', '.join(down)}")
    if up:
        out.append(f"상승 추세 (정배열 + 모든 선 위) {len(up)}/{len(rows)}: {', '.join(up)}")

    last = [f"{r['label']} — {r['ma_order'][-1]}주선" for r in price if len(r["broken"]) == len(r["mas"]) - 1]
    if last:
        out.append(f"마지막 선 하나만 남음: {', '.join(last)}")

    near = [f"{r['label']} {r['nearest']['period']}주 ({r['nearest']['dist_pct']:+.1f}%)"
            for r in rows if r["nearest"] and abs(r["nearest"]["dist_pct"]) <= rules["near_ma_pct"]]
    if near:
        out.append(f"주봉 MA 시험 중 (±{rules['near_ma_pct']:g}% 이내): {', '.join(near)}")
    return out


def build_summary_comments(groups: list[dict], rules: dict) -> list[str]:
    out: list[str] = []
    # 위험 신호 그룹: 공포지수(level)는 평온한데 같은 그룹의 가격 지표(신용 등)가 무너지는 "괴리"
    for g in groups:
        if g["type"] != "risk":
            continue
        rows = [r for r in g["rows"] if "error" not in r]
        calm = [r for r in rows if r["kind"] == "level" and r["position_52w_pct"] is not None
                and r["position_52w_pct"] <= rules["calm_position_pct"]]
        weak = [r for r in rows if r["kind"] == "price" and r["mas"]
                and (len(r["broken"]) == len(r["mas"])
                     or (len(r["mas"]) >= 2 and len(r["broken"]) == len(r["mas"]) - 1)
                     or any(m["cross"] == "down" for m in r["mas"]))]
        if calm and weak:
            calm_txt = ", ".join("%s 52주 위치 %.0f%%" % (r["symbol"].lstrip("^"), r["position_52w_pct"]) for r in calm)
            weak_txt = ", ".join(r["symbol"].lstrip("^") for r in weak)
            out.append(f"괴리: 공포지수는 평온({calm_txt})한데 약세 지표 {len(weak)}개({weak_txt}) — "
                       f"모든 주봉 MA 이탈·이탈 직전이거나 이번 주 하향 이탈")
    seen = set()
    last = []
    for g in groups:
        for r in g["rows"]:
            if "error" in r or r["kind"] != "price" or len(r["mas"]) < 2 or r["symbol"] in seen:
                continue
            if len(r["broken"]) == len(r["mas"]) - 1:
                seen.add(r["symbol"])
                last.append(f"{r['label']} — {r['ma_order'][-1]}주선")
    if last:
        out.append(f"마지막 주봉 MA 하나만 남은 종목: {', '.join(last)}")
    return out


def build_weekly_report(config: dict, now_ny: datetime, force: bool = False) -> dict:
    end = report_week_end(now_ny)
    cache_key = json.dumps(config, sort_keys=True) + end.isoformat()
    if not force:
        with _weekly_report_cache_lock:
            cached = _weekly_report_cache.get(cache_key)
            if cached and time.time() - cached["ts"] < WEEKLY_REPORT_CACHE_TTL:
                return cached["data"]

    symbols = _weekly_report_symbols(config)
    periods = config["ma_periods"]
    metrics: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def work(sym: str):
        try:
            return sym, compute_symbol_week(get_yahoo_daily_history(sym, force=force), end, periods), None
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as e:
            return sym, None, str(e) or type(e).__name__

    with ThreadPoolExecutor(max_workers=WEEKLY_REPORT_MAX_WORKERS) as pool:
        for sym, m, err in pool.map(work, sorted(symbols)):
            if m is not None:
                metrics[sym] = m
            else:
                errors[sym] = err

    bench = metrics.get(config["benchmark"])
    rate = metrics.get(config["rate_symbol"])
    fx = metrics.get(config["fx_symbol"])
    groups_out = []
    summary_all_broken: dict[str, str] = {}
    summary_crosses: list[dict] = []
    seen_cross = set()
    for g in config["groups"]:
        rows = []
        for it in g["items"]:
            m = metrics.get(it["symbol"])
            row = {"symbol": it["symbol"], "label": it["label"], "kind": it["kind"]}
            if m is None:
                row["error"] = errors.get(it["symbol"], "데이터 없음")
                rows.append(row)
                continue
            row.update(m)
            row["rel_pct"] = m["change_pct"] - bench["change_pct"] if bench else None
            row["spread_pct"] = (m["dividend_yield_pct"] - rate["close"]) if (rate and m["dividend_yield_pct"] is not None) else None
            rows.append(row)
            if it["kind"] == "price" and m["mas"] and len(m["broken"]) == len(m["mas"]):
                summary_all_broken.setdefault(it["symbol"], it["label"])
            for ma in m["mas"]:
                key = (it["symbol"], ma["period"])
                if ma["cross"] and key not in seen_cross:
                    seen_cross.add(key)
                    summary_crosses.append({"symbol": it["symbol"], "label": it["label"], "period": ma["period"],
                                            "dir": ma["cross"], "kind": it["kind"]})
        out = {"title": g["title"], "type": g["type"], "rows": rows}
        if g["type"] == "fx_account" and all("error" not in r for r in rows):
            a, b = rows[0]["change_pct"], rows[1]["change_pct"]
            out["account_pct"] = ((1 + a / 100) * (1 + b / 100) - 1) * 100
        groups_out.append(out)

    rules = config["comment_rules"]
    rate_info = {"symbol": config["rate_symbol"], "value": rate["close"]} if rate else None
    for g in groups_out:
        # 내 계좌 그룹은 로그인 세션의 보유종목이 필요해서 여기선 비워두고 fill_account_groups에서 채운다.
        g["comments"] = [] if g["type"] == "my_account" else build_group_comments(g, rules, config["benchmark"], rate_info)

    prev_end = None
    if bench:
        bars = [b for b in get_yahoo_daily_history(config["benchmark"])["bars"] if b[0] <= end]
        cur_week = bars[-1][0].isocalendar()[:2]
        before = [b for b in bars if b[0].isocalendar()[:2] < cur_week]
        prev_end = before[-1][0].isoformat() if before else None

    data = {
        "week_end": end.isoformat(),
        "prev_week_end": prev_end,
        "generated_at": _fmt_as_of(time.time()),
        "ma_periods": periods,
        "benchmark": {"symbol": config["benchmark"], "change_pct": bench["change_pct"] if bench else None},
        "rate": {"symbol": config["rate_symbol"], "value": rate["close"] if rate else None},
        "fx": {"symbol": config["fx_symbol"], "change_pct": fx["change_pct"] if fx else None},
        "groups": groups_out,
        "summary": {
            "all_broken": [{"symbol": s, "label": l} for s, l in summary_all_broken.items()],
            "crosses": summary_crosses,
            "comments": build_summary_comments(groups_out, rules),
        },
        "errors": errors,
    }
    with _weekly_report_cache_lock:
        _weekly_report_cache[cache_key] = {"ts": time.time(), "data": data}
    return data



def toss_to_yahoo_symbol(symbol: str, market: str | None, currency: str) -> str | None:
    """토스 심볼 → Yahoo 심볼. 국내는 시장 구분으로 .KS/.KQ를 붙이고, 해외는 BRK.B → BRK-B처럼 바꾼다."""
    if currency == "KRW":
        return {"KOSPI": f"{symbol}.KS", "KOSDAQ": f"{symbol}.KQ"}.get(market or "")
    return symbol.replace(".", "-")


def fill_account_groups(report: dict, config: dict, holdings: dict, kr_info: dict[str, dict]) -> None:
    """리포트의 my_account 그룹을 토스 보유종목으로 채운다(report를 직접 수정).

    주간 원화 수익 = 해외는 (1 + 배당 포함 주간수익)(1 + 환율 주간변화) − 1, 국내는 주간수익 그대로.
    기여도 = 현재 비중 × 원화 주간수익 — 한 주 동안 보유 수량이 그대로였다고 가정한 근사치.
    """
    targets = [g for g in report["groups"] if g["type"] == "my_account"]
    if not targets:
        return
    end = date.fromisoformat(report["week_end"])
    periods = config["ma_periods"]
    fx_change = report["fx"]["change_pct"]
    total = holdings["totals"]["eval_krw"] or 0.0

    positions = [(r, False) for r in holdings["stocks"]] + [(r, True) for r in holdings["cash"]["stocks"]]

    def work(item):
        r, is_cash = item
        currency = r["통화"]
        ysym = toss_to_yahoo_symbol(r["심볼"], (kr_info.get(r["심볼"]) or {}).get("market"), currency)
        row = {"symbol": r["심볼"], "yahoo_symbol": ysym, "label": r["종목"], "kind": "price", "currency": currency,
               "is_cash": is_cash, "eval_krw": r["평가금액(원)"],
               "weight_pct": r["평가금액(원)"] / total * 100 if total else 0.0}
        if not ysym:
            row["error"] = "Yahoo 심볼로 바꿀 수 없는 시장"
            return row
        try:
            m = compute_symbol_week(get_yahoo_daily_history(ysym), end, periods)
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as e:
            row["error"] = str(e) or type(e).__name__
            return row
        row.update(m)
        tr = m["total_return_pct"]
        if currency == "USD":
            row["krw_change_pct"] = None if fx_change is None else ((1 + tr / 100) * (1 + fx_change / 100) - 1) * 100
        else:
            row["krw_change_pct"] = tr
        return row

    with ThreadPoolExecutor(max_workers=WEEKLY_REPORT_MAX_WORKERS) as pool:
        rows = list(pool.map(work, positions))

    cash = holdings["cash"]
    usd_cash_krw = cash["usd"] * cash["usd_krw_rate"]
    for label, amount, change in (("예수금 (달러)", usd_cash_krw, fx_change), ("예수금 (원화)", cash["krw"], 0.0)):
        if amount > 0:
            rows.append({"symbol": "", "label": label, "kind": "cash", "is_cash": True, "eval_krw": amount,
                         "weight_pct": amount / total * 100 if total else 0.0, "krw_change_pct": change,
                         "mas": [], "broken": [], "nearest": None})

    account_pct = 0.0
    complete = True
    for r in rows:
        if r.get("krw_change_pct") is None:
            complete = False
            r["contribution_pctp"] = None
            continue
        r["contribution_pctp"] = r["weight_pct"] / 100 * r["krw_change_pct"]
        account_pct += r["contribution_pctp"]

    bench = report["benchmark"]
    comments = []
    if complete and bench["change_pct"] is not None:
        comments.append(f"원화 기준 계좌 {_fmt_signed(account_pct)} vs {bench['symbol']} {_fmt_signed(bench['change_pct'])} "
                        f"(차이 {_fmt_signed(account_pct - bench['change_pct'], '%p')})")
    n = config["comment_rules"]["sector_top_n"]
    contrib = sorted([r for r in rows if r.get("contribution_pctp") is not None], key=lambda r: -r["contribution_pctp"])
    if n > 0 and contrib:
        plus = ", ".join(f"{r['label']} {_fmt_signed(r['contribution_pctp'], '%p')}" for r in contrib[:n] if r["contribution_pctp"] > 0)
        minus = ", ".join(f"{r['label']} {_fmt_signed(r['contribution_pctp'], '%p')}" for r in contrib[::-1][:n] if r["contribution_pctp"] < 0)
        if plus:
            comments.append(f"계좌를 끌어올린 종목: {plus}")
        if minus:
            comments.append(f"계좌를 끌어내린 종목: {minus}")
    # 추세·MA 코멘트는 현금 취급 종목(SGOV 등)을 빼고 — 초단기 국채의 "상승 추세"는 의미가 없어서.
    market_rows = [r for r in rows if r["kind"] == "price" and "error" not in r and not r["is_cash"]]
    rate_info = report["rate"] if report["rate"]["value"] is not None else None
    comments += build_group_comments({"type": "my_account", "rows": market_rows}, config["comment_rules"], bench["symbol"], rate_info)

    for g in targets:
        g["rows"] = rows
        g["account"] = {
            "eval_krw": total,
            "profit_loss_krw": holdings["totals"]["profit_loss_krw"],
            "rate_pct": holdings["totals"]["rate_pct"],
            "week_krw_pct": account_pct if complete else None,
        }
        g["comments"] = comments


def validate_weekly_report_symbols(config: dict, known: set[str]) -> list[str]:
    """새로 추가된 심볼만 Yahoo에서 실제로 받아지는지 확인. 실패한 심볼 목록을 돌려준다."""
    new = sorted(_weekly_report_symbols(config) - known)

    def check(sym: str):
        try:
            get_yahoo_daily_history(sym)
            return None
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            return sym

    with ThreadPoolExecutor(max_workers=WEEKLY_REPORT_MAX_WORKERS) as pool:
        return [s for s in pool.map(check, new) if s]




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
                data = call_with_reauth(session, lambda token: fetch_holdings_data(token, accounts, store_for(session)))
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
            self._send_json(200, {"symbols": load_cash_symbols(store_for(session))})
            return

        if path == "/api/rebalance-config":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            self._send_json(200, load_rebalance_config(store_for(session)))
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
            symbols = load_watchlist(store_for(session))
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
            rules = load_ma_rules(store_for(session))
            try:
                rows = call_with_reauth(session, lambda token: compute_ma_rows(token, rules))
                self._send_json(200, {"rows": rows})
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 502
                self._send_json(status, {"error": f"토스 API 오류 ({status})"})
            except requests.RequestException:
                self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
            return

        if path == "/api/strategy-templates":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            self._send_json(200, load_strategy_templates(store_for(session)))
            return

        if path == "/api/weekly-report":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            force = parse_qs(urlsplit(self.path).query).get("refresh") == ["1"]
            config = load_weekly_report_config(store_for(session))
            try:
                # 시장 부분은 30분 캐시를 공유하므로 복사본에 계좌를 채운다.
                report = copy.deepcopy(build_weekly_report(config, datetime.now(ZoneInfo("America/New_York")), force=force))
                if any(g["type"] == "my_account" for g in report["groups"]):
                    try:
                        accounts = call_with_reauth(session, get_accounts)
                        holdings = call_with_reauth(session, lambda token: fetch_holdings_data(token, accounts, store_for(session)))
                        kr = [r["심볼"] for r in holdings["stocks"] + holdings["cash"]["stocks"] if r["통화"] == "KRW"]
                        kr_info = call_with_reauth(session, lambda token: lookup_stocks(token, kr)) if kr else {}
                        fill_account_groups(report, config, holdings, kr_info)
                    except requests.RequestException as e:
                        print(f"[/api/weekly-report] 보유종목 조회 실패: {e}", file=sys.stderr)
                        for g in report["groups"]:
                            if g["type"] == "my_account":
                                g["error"] = "토스 보유종목을 불러오지 못했습니다."
                self._send_json(200, report)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json(500, {"error": "주간 리포트를 만들지 못했습니다."})
            return

        if path == "/api/weekly-report-config":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            return self._send_json(200, {"config": load_weekly_report_config(store_for(session)), "default": default_weekly_report_config(),
                                         "group_types": sorted(WEEKLY_REPORT_GROUP_TYPES),
                                         "item_kinds": sorted(WEEKLY_REPORT_ITEM_KINDS)})

        if path == "/api/strategy":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            data = load_strategy_templates(store_for(session))
            watchlist_symbols = set(load_watchlist(store_for(session)))
            try:
                accounts = call_with_reauth(session, get_accounts)
                held = call_with_reauth(session, lambda token: held_symbols(token, accounts))
                symbols = sorted(held | watchlist_symbols)
                state = load_strategy_state(store_for(session))
                rows, changed = call_with_reauth(
                    session,
                    lambda token: compute_strategy_rows(
                        token, data["templates"], data["assignments"], set(data["excluded_symbols"]), state, symbols
                    ),
                )
                if changed:
                    save_strategy_state(store_for(session), state)
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
        # 주간 리포트 설정은 종목이 최대 150개라 다른 설정보다 크다.
        max_len = 65536 if path == "/api/weekly-report-config" else 8192
        if length > max_len:
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

            # 사용자 구분용 user_id (계좌번호 HMAC). 계좌 조회가 안 되면 저장소를 정할 수 없어 로그인 실패로 처리.
            try:
                user_id = user_id_for_accounts(get_accounts(token))
            except requests.RequestException:
                return self._send_json(502, {"error": "토스 API에서 계좌 정보를 불러올 수 없습니다."})
            except ValueError:
                return self._send_json(400, {"error": "계좌 정보가 없어 로그인할 수 없습니다."})
            moved = migrate_legacy_files(UserStore(USERS_DIR / user_id))
            if moved:
                print(f"[login] 예전 설정 파일을 사용자 저장소로 옮김: {', '.join(moved)}", file=sys.stderr)

            sid = secrets.token_urlsafe(32)
            with _lock:
                _prune_expired()
                _sessions[sid] = {
                    "user_id": user_id,
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

            save_cash_symbols(store_for(session), symbols)
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

            save_rebalance_config(store_for(session), targets, default_rest_target_pct, krw_target_pct)
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

            save_watchlist(store_for(session), symbols)
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

            save_ma_rules(store_for(session), rules)
            return self._send_json(200, {"ok": True, "rules": rules})

        if path == "/api/weekly-report-config":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})
            try:
                config = _normalize_weekly_report_config(data.get("config"))
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            failed = validate_weekly_report_symbols(config, _weekly_report_symbols(load_weekly_report_config(store_for(session))))
            if failed:
                return self._send_json(400, {"error": f"Yahoo Finance에서 데이터를 찾을 수 없는 심볼입니다: {', '.join(failed)}"})
            save_weekly_report_config(store_for(session), config)
            return self._send_json(200, {"ok": True, "config": config})

        if path == "/api/strategy-templates":
            session = _get_session(self)
            if not session:
                return self._send_json(401, {"error": "로그인이 필요합니다."})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "잘못된 요청입니다."})

            clean, error = validate_strategy_templates(data)
            if error:
                return self._send_json(400, {"error": error})

            all_symbols = sorted(set(clean["assignments"]) | set(clean["excluded_symbols"]))
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

            save_strategy_templates(store_for(session), clean)
            return self._send_json(200, {"ok": True, **clean})

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
