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

# 세션 저장소: 메모리에만 유지, 디스크 기록 없음.
# { session_id: {"app_key", "app_secret", "token", "issued_at", "last_seen"} }
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_cash_symbols_lock = threading.Lock()  # cash_symbols.json 동시 수정 방지
_rebalance_lock = threading.Lock()  # rebalance.json 동시 수정 방지
_watchlist_lock = threading.Lock()  # watchlist.json 동시 수정 방지


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


def get_daily_candles(token: str, symbol: str, count: int = 2) -> dict:
    return api_get("/api/v1/candles", token, params={"symbol": symbol, "interval": "1d", "count": count}).get(
        "result", {}
    )


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
