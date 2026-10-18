#!/usr/bin/env python3
"""
토스증권 포트폴리오 대시보드 (redo) - 로그인 방식 웹 서버. HTTP 계층만 여기 있고, 계산은 기능별 모듈
(portfolio / watch / strategy / weekly_report)이, 외부 호출은 toss_api가, 파일 저장은 storage가 한다.
절대 0.0.0.0 등으로 외부에 노출하지 말 것 - 127.0.0.1(localhost) 전용.

사용법:
    python3 server.py
    -> http://127.0.0.1:8767 브라우저로 접속, 앱키/시크릿으로 로그인

인증 방식:
    .env에 앱키/시크릿을 미리 넣어두는 대신, 대시보드의 로그인 화면에서 입력받아 그 자리에서
    토스증권 Open API(OAuth2 client_credentials)로 access_token을 발급받는다. 앱키/시크릿과 토큰은
    디스크에 저장하지 않고 서버 프로세스 메모리(세션)에만 유지한다 -> 서버를 재시작하면 다시 로그인해야 함.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests

from agent_loop import follow_ups, observations
from agent_tools import (DRAWDOWN_WORDS, FOCUS_TOOLS, INTENT_LABELS, INTENT_WORDS, VALUATION_WORDS, TEACHABLE_INTENTS, TOOL_DEFINITIONS, TOOL_NAMES, compose_agent_answer, format_tool_result,
                         parse_response_query, parse_screen_query, parse_trend_query, plan_agent_query, plan_for_holding, plan_for_intent, plan_intent_name, suggest_questions)
from intent_examples import DEFAULT_EXAMPLES
from intent_matcher import MIN_CONTENT, IntentMatcher, normalize as normalize_question
from common import ApiError, normalize_symbols, parallel_map, to_float
from dividends import estimate_dividends
from etf_breakdown import breakdown as etf_breakdown
from etf_holdings import fetch_top_holdings
from secret_store import delete_secret as delete_fred_secret, keychain_supported, read_default_key, save_secret as save_fred_secret
from fred import FredError, PREFIX as FRED_PREFIX, SERIES_IDS as FRED_IDS, SLOW_SERIES_IDS as FRED_SLOW_IDS, fetch_series as fetch_fred_series, valid_key as valid_fred_key, yoy_series
from macro import FACTOR_BY_SYMBOL, FACTORS, daily_changes, explain_asset, factor_group, rank_drivers, weekly_factor_moves, window_pressure
from causes import rank_themes, themes_in
from fundamentals import (INFO_CACHE_TTL, assess_etf, assess_stock, fetch_info as fetch_fundamentals, is_korean as is_korean_stock, peer_stats, sample_peers)
from response import (DEFAULT_ASSETS as DEFAULT_RESPONSE_ASSETS, DEFAULT_HORIZON as DEFAULT_RESPONSE_HORIZON, HORIZONS as RESPONSE_HORIZONS, MAX_ASSETS as MAX_RESPONSE_ASSETS,
                      analyze as analyze_response, resolve_asset, resolve_factor)
from symbols import candidate_tokens, choose as choose_symbol
from drawdown import RISK_SYMBOLS, day_movers, search_term, worst_days
from outlook import build_outlook
from shocks import analyze as analyze_shocks
from long_history import LONG_PROXY_YEARS, event_source_for, fetch_gold_monthly, long_source_for, same_asset_factors
from cases import build as build_cases
import trend as trend_mod
from screener import ScreenerError, clean_periods, fetch_sp500, screen as run_screen, weekly_closes
from news import headlines_around, headlines_for_many, name_query, weekly_change_pct
from claude_agent import AgentApiError, DEFAULT_AGENT_MODEL, normalize_history, run_claude_agent
from portfolio import (CASH_SYMBOLS_MAX, allocate_trade_shares, apply_entry_scaling, build_holdings, load_cash_symbols, load_rebalance_config,
                       save_cash_symbols, save_rebalance_config, snapshot_rows, symbols_needing_quotes,
                       validate_rebalance_targets)
from sectors import (compute_exposure, holding_market, index_is_fresh, index_progress, load_sector_index, load_sector_map,
                     new_index, run_scan, save_sector_index, save_sector_map, suggest_sectors, validate_sector_map)
from storage import UserStore, migrate_legacy_files, store_for, store_for_user, user_id_for_accounts
from tax import (DEFAULT_TAX_SETTINGS, DOMESTIC_CLASSES, TAX_REFERENCE, estimate_taxes, load_tax_settings,
                 normalize_tax_settings, save_tax_settings, sell_tax_rate_by_symbol)
from strategy import (apply_strategy_sizing, compute_strategy_rows, effective_assignments, load_strategy_state,
                      load_strategy_templates, save_strategy_state, save_strategy_templates, validate_strategy_templates)
from toss_api import (fetch_account_snapshot, get_access_token, get_accounts, get_closed_orders, get_sector_members,
                      get_prices, get_sectors, get_stock_info, lookup_stocks, search_stocks)
from trades import fills_from_orders, fx_lookup, realized_gains, summarize_fills
from watch import (WATCHLIST_MAX, compute_ma_rows, fetch_watchlist_rows, load_ma_rules, load_watchlist, save_ma_rules,
                   save_watchlist, validate_ma_rules)
from weekly_report import (WEEKLY_REPORT_GROUP_TYPES, WEEKLY_REPORT_ITEM_KINDS, _normalize_weekly_report_config,
                           get_yahoo_daily_history, toss_to_yahoo_symbol,
                           _weekly_report_symbols, build_weekly_report, default_weekly_report_config, fill_account_groups,
                           load_weekly_report_config, save_weekly_report_config, validate_weekly_report_symbols)

HOST = "127.0.0.1"
PORT = 8767
SESSION_COOKIE = "toss_redo_session"
SESSION_TTL = 60 * 60 * 4  # 4시간 무활동 시 세션 만료
ORDERS_TTL = 300.0  # 초. 전체 체결 내역(수백 건일 수 있음)은 5분 동안 재사용
SNAPSHOT_TTL = 15.0  # 초. 토스 계좌 원자료를 이 시간 동안 재사용(한 화면을 그릴 때 보유·전략·리포트가 공유)
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
DEFAULT_MAX_BODY = 8192

WEB_DIR = Path(__file__).resolve().parent
DASHBOARD_HTML = WEB_DIR / "dashboard.html"
STATIC_DIR = WEB_DIR / "static"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}
CASH_SYMBOL_PATTERN = re.compile(r"^.{1,20}$")  # 현금 취급 종목은 보유종목 심볼을 그대로 받으므로 느슨하게

SECTOR_CALL_DELAY = 0.2  # 초. 섹터 색인을 모을 때 토스 호출 사이 간격(수백 번 연속 호출하므로 한도를 아낀다)

# 세션: 메모리에만 유지, 디스크 기록 없음.
# {session_id: {"user_id", "app_key", "app_secret", "token", "accounts", "snapshot", "snapshot_lock", "last_seen"}}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 세션 / 토스 호출 도우미
# ---------------------------------------------------------------------------
def _prune_expired() -> None:
    now = time.time()
    for sid in [sid for sid, s in _sessions.items() if now - s["last_seen"] > SESSION_TTL]:
        del _sessions[sid]


TOSS_MAINTENANCE_MESSAGE = "토스증권 API가 점검 중입니다. 잠시 후 다시 시도해 주세요."


def toss_http_error(e: requests.HTTPError) -> tuple[int, str]:
    """토스 HTTP 오류 → (상태 코드, 사용자에게 보일 문구). 토스가 점검 중이면(code=maintenance) 500으로 응답하므로
    앱 고장처럼 보이지 않게 따로 알린다. 다른 오류의 원문은 화면에 내보내지 않는다(로그에만 남김)."""
    resp = e.response
    status = resp.status_code if resp is not None else 502
    try:
        code = ((resp.json() or {}).get("error") or {}).get("code")
    except Exception:  # 본문이 JSON이 아니면 일반 오류로 취급
        code = None
    if code == "maintenance":
        return 503, TOSS_MAINTENANCE_MESSAGE
    return status, f"토스 API 오류 ({status})"


def call_with_reauth(session: dict, fn: Callable[[str], object]):
    """fn(token)을 실행해보고, 401이면 그때만 앱키/시크릿으로 재발급해서 한 번 재시도한다(미리 확인용
    API를 따로 부르지 않음 - 토스 API 초당 요청 한도를 불필요하게 두 배로 쓰지 않기 위함)."""
    try:
        return fn(session["token"])
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            session["token"] = get_access_token(session["app_key"], session["app_secret"])
            return fn(session["token"])
        raise


def account_snapshot(session: dict) -> dict:
    """토스 계좌 원자료(환율·예수금·보유종목). SNAPSHOT_TTL 동안 재사용하고, 동시에 들어온 요청은
    세션 잠금으로 한 번만 받는다(첫 화면에서 보유·전략 요청이 같이 오기 때문)."""
    with session["snapshot_lock"]:
        cached = session.get("snapshot")
        if cached and time.time() - cached[0] < SNAPSHOT_TTL:
            return cached[1]
        data = call_with_reauth(session, lambda token: fetch_account_snapshot(token, session["accounts"]))
        session["snapshot"] = (time.time(), data)
        return data


def all_fills(session: dict) -> list[dict]:
    """모든 계좌의 전체 기간 체결 내역(선입선출 계산에 예전 매수분이 필요해서 전체 기간). ORDERS_TTL 동안 재사용."""
    with session["snapshot_lock"]:
        cached = session.get("fills")
        if cached and time.time() - cached[0] < ORDERS_TTL:
            return cached[1]
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    fills = []
    for account in session["accounts"]:
        seq = account["accountSeq"]
        fills += fills_from_orders(call_with_reauth(session, lambda token: get_closed_orders(token, seq, "2000-01-01", today)), seq)
    fills.sort(key=lambda f: f["filled_at"], reverse=True)
    session["fills"] = (time.time(), fills)
    return fills


def realized_overseas(session: dict, year: int) -> dict:
    """해외 주식 year년 실현 손익(선입선출, 결제일 환율). 환율은 Yahoo 원/달러 일봉(약 6년치)."""
    fx = fx_lookup(get_yahoo_daily_history("KRW=X")["bars"])
    return realized_gains(all_fills(session), fx, year)


def dividends_for_year(session: dict, year: int) -> dict:
    """year년 배당 추정(체결 내역으로 배당락일 보유 수량 × Yahoo 배당 기록). 세션에 5분 캐시."""
    cache = session.setdefault("dividends", {})
    cached = cache.get(year)
    if cached and time.time() - cached[0] < ORDERS_TTL:
        return cached[1]
    fills = all_fills(session)
    held = snapshot_rows(account_snapshot(session))
    held_qty = {r["심볼"]: r["수량"] for r in held}
    currency_of = {f["symbol"]: f["currency"] for f in fills} | {r["심볼"]: r["통화"] for r in held}
    kr = sorted(s for s, c in currency_of.items() if c == "KRW")
    kr_info = call_with_reauth(session, lambda token: lookup_stocks(token, kr)) if kr else {}

    def history(symbol):
        ysym = toss_to_yahoo_symbol(symbol, (kr_info.get(symbol) or {}).get("market"), currency_of[symbol])
        if not ysym:
            return symbol, None
        try:
            return symbol, get_yahoo_daily_history(ysym)["divs"]
        except (requests.RequestException, ValueError, KeyError):
            return symbol, None

    divs_of, missing = {}, []
    for symbol, divs in parallel_map(history, sorted(currency_of), 8):
        if divs is None:
            missing.append(symbol)
        elif divs:
            divs_of[symbol] = divs
    fx = fx_lookup(get_yahoo_daily_history("KRW=X")["bars"])
    result = estimate_dividends(fills, held_qty, currency_of, divs_of, fx, year)
    if missing:
        result["notes"].append(f"Yahoo 배당 기록을 못 받은 종목: {', '.join(missing)}")
    names = call_with_reauth(session, lambda token: lookup_stocks(token, sorted({r["symbol"] for r in result["rows"]})))
    for r in result["rows"]:
        r["name"] = (names.get(r["symbol"]) or {}).get("name", r["symbol"])
    cache[year] = (time.time(), result)
    return result


def ensure_symbols_exist(session: dict, symbols: list[str]) -> None:
    """저장하려는 종목이 토스에 실제로 있는지 확인(real/의 관례). 없으면 ApiError(400)."""
    if not symbols:
        return
    info = call_with_reauth(session, lambda token: get_stock_info(token, symbols))
    missing = [s for s in symbols if s not in info]
    if missing:
        raise ApiError(400, f"존재하지 않는 종목 코드입니다: {', '.join(missing)}")


def kr_stock_info(session: dict, holdings: dict) -> dict[str, dict]:
    """국내 보유종목의 시장 구분(KOSPI/KOSDAQ 등). 세금·주간 리포트가 쓴다."""
    kr = [r["심볼"] for r in holdings["stocks"] + holdings["cash"]["stocks"] if r["통화"] == "KRW"]
    return call_with_reauth(session, lambda token: lookup_stocks(token, kr)) if kr else {}


def percent_field(data: dict, key: str, what: str) -> float:
    value = to_float(data.get(key), -1)
    if not (0 <= value <= 100):
        raise ApiError(400, f"{what}는 0~100 사이 숫자여야 합니다.")
    return value


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
@dataclass
class Request:
    session: dict | None
    query: dict[str, list[str]]
    body: dict

    @property
    def store(self) -> UserStore:
        return store_for(self.session)


@dataclass
class Route:
    fn: Callable[[Request], object]
    auth: bool
    max_body: int


ROUTES: dict[tuple[str, str], Route] = {}


def route(method: str, path: str, auth: bool = True, max_body: int = DEFAULT_MAX_BODY):
    """핸들러 등록. 핸들러는 payload(dict) 또는 (status, payload[, set_cookie])를 돌려준다."""
    def deco(fn):
        ROUTES[(method, path)] = Route(fn, auth, max_body)
        return fn
    return deco


@route("GET", "/api/session", auth=False)
def get_session_status(req: Request):
    return {"logged_in": req.session is not None}


@route("POST", "/api/login", auth=False)
def login(req: Request):
    app_key = str(req.body.get("app_key") or "").strip()
    app_secret = str(req.body.get("app_secret") or "").strip()
    if not app_key or not app_secret:
        raise ApiError(400, "앱키와 시크릿을 모두 입력하세요.")
    try:
        token = get_access_token(app_key, app_secret)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 401
        if status in (400, 401):
            raise ApiError(401, "로그인 실패: 앱키/시크릿을 확인하세요.")
        status, message = toss_http_error(e)
        raise ApiError(status if status == 503 else 401, message)
    # 사용자 구분용 user_id (계좌번호 HMAC). 계좌 조회가 안 되면 저장소를 정할 수 없어 로그인 실패로 처리.
    try:
        accounts = get_accounts(token)
        user_id = user_id_for_accounts(accounts)
    except requests.RequestException:
        raise ApiError(502, "토스 API에서 계좌 정보를 불러올 수 없습니다.")
    except ValueError:
        raise ApiError(400, "계좌 정보가 없어 로그인할 수 없습니다.")
    moved = migrate_legacy_files(store_for_user(user_id))
    if moved:
        print(f"[login] 예전 설정 파일을 사용자 저장소로 옮김: {', '.join(moved)}", file=sys.stderr)

    sid = secrets.token_urlsafe(32)
    with _sessions_lock:
        _prune_expired()
        _sessions[sid] = {"user_id": user_id, "app_key": app_key, "app_secret": app_secret, "token": token,
                          "accounts": accounts, "snapshot": None, "snapshot_lock": threading.Lock(),
                          "last_seen": time.time()}
    return 200, {"ok": True}, f"{SESSION_COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/"


# ---- 보유 / 현금 / 리밸런싱 ----
@route("GET", "/api/holdings")
def get_holdings_view(req: Request):
    return build_holdings(account_snapshot(req.session), req.store)


@route("GET", "/api/cash-symbols")
def get_cash_symbols(req: Request):
    return {"symbols": load_cash_symbols(req.store)}


@route("POST", "/api/cash-symbols")
def post_cash_symbols(req: Request):
    symbols = normalize_symbols(req.body.get("symbols"), CASH_SYMBOLS_MAX, "현금 취급 종목", CASH_SYMBOL_PATTERN)
    save_cash_symbols(req.store, symbols)
    return {"ok": True, "symbols": symbols}


@route("GET", "/api/rebalance-config")
def get_rebalance_config(req: Request):
    return load_rebalance_config(req.store)


@route("POST", "/api/rebalance-config")
def post_rebalance_config(req: Request):
    targets, error = validate_rebalance_targets(req.body.get("targets"))
    if error:
        raise ApiError(400, error)
    rest_pct = percent_field(req.body, "default_rest_target_pct", "미분류 종목 기본 목표%")
    krw_pct = percent_field(req.body, "krw_target_pct", "목표 원화 비중%")
    ensure_symbols_exist(req.session, sorted({s for t in targets for s in t["symbols"]}))
    save_rebalance_config(req.store, targets, rest_pct, krw_pct)
    return {"ok": True, "targets": targets, "default_rest_target_pct": rest_pct, "krw_target_pct": krw_pct}


# ---- 섹터 매핑 ----
@route("GET", "/api/sector-map")
def get_sector_map(req: Request):
    return load_sector_map(req.store)


@route("POST", "/api/sector-map")
def post_sector_map(req: Request):
    config, error = validate_sector_map(req.body)
    if error:
        raise ApiError(400, error)
    ensure_symbols_exist(req.session, sorted({a["symbol"] for a in config["assignments"]}))
    save_sector_map(req.store, config)
    return {"ok": True, **config}


@route("GET", "/api/sector-exposure")
def get_sector_exposure(req: Request):
    holdings = build_holdings(account_snapshot(req.session), req.store)
    config = load_sector_map(req.store)
    return {**compute_exposure(holdings["stocks"], holdings["totals"]["eval_krw"], config),
            "configured": bool(config["sectors"])}


# ---- 토스 섹터 분류로 자동 채우기 ----
# 색인(종목 → 소속 섹터)은 사용자별 저장소에 하루 한 번 모은다. 수백 번 호출하므로 백그라운드 스레드로 돌리고,
# 화면은 진행 상황을 주기적으로 물어본다. 사용자당 작업은 하나만 돈다.
_sector_jobs: dict[str, dict] = {}
_sector_jobs_lock = threading.Lock()


def _spawn(fn: Callable[[], None]) -> None:
    """백그라운드 실행(테스트는 이 함수를 바꿔서 바로 실행한다)."""
    threading.Thread(target=fn, daemon=True).start()


def _held_markets(session: dict) -> tuple[list[dict], list[str]]:
    rows = snapshot_rows(account_snapshot(session))
    return rows, sorted({holding_market(r) for r in rows})


def _sector_job_status(user_id: str, index: dict | None, markets: list[str]) -> dict:
    with _sector_jobs_lock:
        job = dict(_sector_jobs.get(user_id) or {})
    progress = index_progress(index, markets) if index else {"done": 0, "total": 0, "stocks_done": False, "etfs_done": False}
    return {"state": job.get("state", "idle"), "message": job.get("message", ""), **progress,
            "fresh": index_is_fresh(index), "built_at": (index or {}).get("built_at")}


@route("POST", "/api/sector-index/refresh")
def post_sector_index_refresh(req: Request):
    rows, markets = _held_markets(req.session)
    if not rows:
        raise ApiError(400, "보유 종목이 없어 섹터 정보를 모을 대상이 없습니다.")
    user_id, store, session = req.session["user_id"], req.store, req.session
    with _sector_jobs_lock:
        if (_sector_jobs.get(user_id) or {}).get("state") == "running":
            running = True
        else:
            running = False
            _sector_jobs[user_id] = {"state": "running", "message": ""}
    index = load_sector_index(store)
    if running:
        return _sector_job_status(user_id, index, markets)
    try:
        if req.body.get("force") or not index_is_fresh(index):
            sectors = call_with_reauth(session, get_sectors)
            index = new_index(sectors)
            if not index["sectors"]:
                raise ApiError(502, "토스 섹터 목록을 읽을 수 없습니다.")
            save_sector_index(store, index)
    except Exception:
        with _sector_jobs_lock:
            _sector_jobs.pop(user_id, None)
        raise

    def job():
        try:
            def fetch(sector_id, market, kind):
                time.sleep(SECTOR_CALL_DELAY)
                return call_with_reauth(session, lambda token: get_sector_members(token, sector_id, market, kind))
            run_scan(index, markets, fetch, save=lambda idx: save_sector_index(store, idx))
            state = {"state": "done", "message": ""}
        except requests.RequestException as e:
            code = e.response.status_code if getattr(e, "response", None) is not None else None
            state = {"state": "error", "message": f"토스 섹터 조회 중 오류가 났습니다({code or '네트워크'}). 다시 누르면 이어서 진행합니다."}
        except Exception as e:  # 백그라운드라 여기서 막지 않으면 상태가 영원히 running으로 남는다
            state = {"state": "error", "message": f"섹터 정보를 모으지 못했습니다: {type(e).__name__}"}
        with _sector_jobs_lock:
            _sector_jobs[user_id] = state

    _spawn(job)
    return _sector_job_status(user_id, index, markets)


@route("GET", "/api/sector-suggestions")
def get_sector_suggestions(req: Request):
    rows, markets = _held_markets(req.session)
    index = load_sector_index(req.store)
    status = _sector_job_status(req.session["user_id"], index, markets)
    suggestions = suggest_sectors(rows, index) if index else []
    return {"status": status, "suggestions": suggestions}


# ---- 검색 / 관심종목 / MA 감시 ----
@route("GET", "/api/search")
def get_search(req: Request):
    query = req.query.get("q", [""])[0]
    return {"results": call_with_reauth(req.session, lambda token: search_stocks(token, query))}


@route("GET", "/api/watchlist")
def get_watchlist(req: Request):
    symbols = load_watchlist(req.store)
    return {"items": call_with_reauth(req.session, lambda token: fetch_watchlist_rows(token, symbols))}


@route("POST", "/api/watchlist")
def post_watchlist(req: Request):
    symbols = normalize_symbols(req.body.get("symbols"), WATCHLIST_MAX, "관심종목")
    ensure_symbols_exist(req.session, symbols)
    save_watchlist(req.store, symbols)
    return {"ok": True, "symbols": symbols}


@route("GET", "/api/ma-rules")
def get_ma_rules(req: Request):
    rules = load_ma_rules(req.store)
    return {"rows": call_with_reauth(req.session, lambda token: compute_ma_rows(token, rules))}


@route("POST", "/api/ma-rules")
def post_ma_rules(req: Request):
    rules, error = validate_ma_rules(req.body.get("rules"))
    if error:
        raise ApiError(400, error)
    ensure_symbols_exist(req.session, sorted({r["symbol"] for r in rules}))
    save_ma_rules(req.store, rules)
    return {"ok": True, "rules": rules}


# ---- 분할매수 전략 ----
@route("GET", "/api/strategy-templates")
def get_strategy_templates(req: Request):
    return load_strategy_templates(req.store)


@route("POST", "/api/strategy-templates")
def post_strategy_templates(req: Request):
    clean, error = validate_strategy_templates(req.body)
    if error:
        raise ApiError(400, error)
    ensure_symbols_exist(req.session, sorted(set(clean["assignments"]) | set(clean["excluded_symbols"])))
    save_strategy_templates(req.store, clean)
    return {"ok": True, **clean}


@route("GET", "/api/strategy")
def get_strategy(req: Request):
    store = req.store
    data = load_strategy_templates(store)
    snapshot = account_snapshot(req.session)
    held = {r["심볼"] for r in snapshot_rows(snapshot)}
    symbols = sorted(held | set(load_watchlist(store)))
    assignments, auto = effective_assignments(data, held, set(load_cash_symbols(store)))
    state = load_strategy_state(store)
    rows, changed = call_with_reauth(req.session, lambda token: compute_strategy_rows(
        token, data["templates"], assignments, set(data["excluded_symbols"]), state, symbols))
    if changed:
        save_strategy_state(store, state)
    total_krw = None
    if data["position_pct"]:  # 금액 계산에 총자산이 필요할 때만
        holdings = build_holdings(snapshot, store)
        taxes = estimate_taxes(holdings, load_tax_settings(store), kr_stock_info(req.session, holdings))
        apply_strategy_sizing(rows, data["position_pct"], holdings, sell_tax_rate_by_symbol(taxes))
        total_krw = holdings["totals"]["eval_krw"]
    return {"rows": rows, "auto_assigned": auto, "total_krw": total_krw}


# ---- 세금 ----
@route("GET", "/api/tax")
def get_tax(req: Request):
    settings = load_tax_settings(req.store)
    holdings = build_holdings(account_snapshot(req.session), req.store)
    used = dict(settings)
    realized_source, realized_note = "manual", None
    if not settings["use_manual_realized"]:
        try:
            realized = realized_overseas(req.session, datetime.now(ZoneInfo("Asia/Seoul")).year)
            used["realized_overseas_gain_ytd_krw"] = realized["total_gain_krw"]
            realized_source = "auto"
            if not realized["complete"]:
                realized_note = "매입 기록이나 환율이 없는 매도가 있어 일부만 합산했어요. 체결 내역 카드를 확인하세요."
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"[/api/tax] 실현 손익 자동 계산 실패, 입력값 사용: {e}", file=sys.stderr)
            realized_note = "체결 내역을 불러오지 못해 직접 입력한 값을 썼어요."
    income_source, income_note = "manual", None
    if not settings["use_manual_financial_income"]:
        try:
            used["financial_income_ytd_krw"] = dividends_for_year(req.session, datetime.now(ZoneInfo("Asia/Seoul")).year)["total_gross_krw"]
            income_source = "auto"
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"[/api/tax] 배당 추정 실패, 입력값 사용: {e}", file=sys.stderr)
            income_note = "배당을 추정하지 못해 직접 입력한 값을 썼어요."
    return {"settings": settings, "default": DEFAULT_TAX_SETTINGS, "classes": DOMESTIC_CLASSES, "reference": TAX_REFERENCE,
            "realized_source": realized_source, "realized_note": realized_note,
            "income_source": income_source, "income_note": income_note,
            **estimate_taxes(holdings, used, kr_stock_info(req.session, holdings))}


@route("GET", "/api/trades")
def get_trades(req: Request):
    """올해(또는 ?year=) 체결 내역. 계좌가 여러 개면 전부 합친다."""
    this_year = datetime.now(ZoneInfo("Asia/Seoul")).year
    try:
        year = int(req.query.get("year", [this_year])[0])
    except ValueError:
        raise ApiError(400, "year는 숫자여야 합니다.")
    if not 2000 <= year <= this_year:
        raise ApiError(400, f"year는 2000~{this_year} 사이여야 합니다.")
    fills = [dict(f) for f in all_fills(req.session) if f["filled_at"][:4] == str(year)]
    try:
        realized = realized_overseas(req.session, year)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[/api/trades] 환율을 못 받아 실현 손익 계산 생략: {e}", file=sys.stderr)
        realized = None
    symbols = sorted({f["symbol"] for f in fills} | {s["symbol"] for s in (realized or {}).get("sells", [])})
    info = call_with_reauth(req.session, lambda token: lookup_stocks(token, symbols))
    name = lambda sym: (info.get(sym) or {}).get("name", sym)  # noqa: E731
    for row in fills + (realized or {}).get("sells", []):
        row["name"] = name(row["symbol"])
    return {"year": year, "fills": fills, "summary": summarize_fills(fills), "realized": realized}


@route("GET", "/api/dividends")
def get_dividends(req: Request):
    this_year = datetime.now(ZoneInfo("Asia/Seoul")).year
    try:
        year = int(req.query.get("year", [this_year])[0])
    except ValueError:
        raise ApiError(400, "year는 숫자여야 합니다.")
    if not 2000 <= year <= this_year:
        raise ApiError(400, f"year는 2000~{this_year} 사이여야 합니다.")
    try:
        return dividends_for_year(req.session, year)
    except (ValueError, KeyError):
        raise ApiError(502, "환율 기록을 받지 못해 배당을 추정하지 못했습니다.")


@route("POST", "/api/tax-settings")
def post_tax_settings(req: Request):
    try:
        settings = normalize_tax_settings(req.body.get("settings"))
    except ValueError as e:
        raise ApiError(400, str(e))
    save_tax_settings(req.store, settings)
    return {"ok": True, "settings": settings}


# ---- 주간 리포트 ----
@route("GET", "/api/weekly-report")
def get_weekly_report(req: Request):
    config = load_weekly_report_config(req.store)
    force = req.query.get("refresh") == ["1"]
    try:
        # 시장 부분은 30분 캐시를 공유하므로 복사본에 계좌를 채운다.
        report = copy.deepcopy(build_weekly_report(config, datetime.now(ZoneInfo("America/New_York")), force=force))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        raise ApiError(500, "주간 리포트를 만들지 못했습니다.")
    account_groups = [g for g in report["groups"] if g["type"] == "my_account"]
    if account_groups:
        try:
            holdings = build_holdings(account_snapshot(req.session), req.store)
            fill_account_groups(report, config, holdings, kr_stock_info(req.session, holdings))
        except requests.RequestException as e:
            print(f"[/api/weekly-report] 보유종목 조회 실패: {e}", file=sys.stderr)
            for g in account_groups:
                g["error"] = "토스 보유종목을 불러오지 못했습니다."
    return report


@route("GET", "/api/weekly-report-config")
def get_weekly_report_config(req: Request):
    return {"config": load_weekly_report_config(req.store), "default": default_weekly_report_config(),
            "group_types": sorted(WEEKLY_REPORT_GROUP_TYPES), "item_kinds": sorted(WEEKLY_REPORT_ITEM_KINDS)}


@route("POST", "/api/weekly-report-config", max_body=65536)  # 종목이 최대 150개라 다른 설정보다 크다
def post_weekly_report_config(req: Request):
    try:
        config = _normalize_weekly_report_config(req.body.get("config"))
    except ValueError as e:
        raise ApiError(400, str(e))
    failed = validate_weekly_report_symbols(config, _weekly_report_symbols(load_weekly_report_config(req.store)))
    if failed:
        raise ApiError(400, f"Yahoo Finance에서 데이터를 찾을 수 없는 심볼입니다: {', '.join(failed)}")
    save_weekly_report_config(req.store, config)
    return {"ok": True, "config": config}


# ---- AI Agent 읽기 전용 도구 ----
@route("GET", "/api/agent/tools")
def get_agent_tools(req: Request):
    """LLM 또는 화면이 사용할 수 있는 도구 목록. 쓰기·주문 도구는 의도적으로 제공하지 않는다."""
    return {"tools": TOOL_DEFINITIONS}


_default_fred = {"loaded": False, "key": None, "source": None}
_default_fred_lock = threading.Lock()


def default_fred_key() -> tuple[str | None, str | None]:
    """서버를 켤 때부터 있는 FRED 키(환경변수 또는 키체인)와 출처. 처음 필요할 때 한 번만 읽는다."""
    with _default_fred_lock:
        if not _default_fred["loaded"]:
            key, source = read_default_key(valid_fred_key)
            _default_fred.update(loaded=True, key=key, source=source)
        return _default_fred["key"], _default_fred["source"]


def reset_default_fred_key() -> None:
    with _default_fred_lock:
        _default_fred.update(loaded=False, key=None, source=None)


def effective_fred_key(session: dict) -> tuple[str | None, str | None]:
    """이번 세션에 화면에서 넣은 키가 우선, 없으면 서버 기본 키. (키, 출처)"""
    key = session.get("fred_api_key")
    return (key, "session") if key else default_fred_key()


def fred_status(session: dict) -> dict:
    key, source = effective_fred_key(session)
    return {"fred_configured": bool(key), "fred_source": source, "keychain_supported": keychain_supported()}


@route("GET", "/api/agent/config")
def get_agent_config(req: Request):
    return {
        "configured": bool(req.session.get("anthropic_api_key")),
        "model": req.session.get("agent_model", DEFAULT_AGENT_MODEL),
        **fred_status(req.session),
    }


@route("POST", "/api/agent/config")
def post_agent_config(req: Request):
    api_key = str(req.body.get("api_key") or "").strip()
    model = str(req.body.get("model") or DEFAULT_AGENT_MODEL).strip()
    fred_key = str(req.body.get("fred_key") or "").strip()
    if len(api_key) > 500 or len(model) > 100 or len(fred_key) > 100:
        raise ApiError(400, "Agent 설정값이 너무 깁니다.")
    if fred_key and not valid_fred_key(fred_key):
        raise ApiError(400, "FRED API 키 형식이 올바르지 않습니다(영문 소문자·숫자 32자).")   # 입력값은 메시지에 되돌려 쓰지 않는다
    if api_key:
        req.session["anthropic_api_key"] = api_key
    elif req.body.get("clear_key"):
        req.session.pop("anthropic_api_key", None)
    result = {"ok": True}
    if fred_key:
        req.session["fred_api_key"] = fred_key
        if req.body.get("fred_save"):   # 사용자가 직접 고른 경우에만 키체인에 저장해 다음에 서버를 켤 때 자동 연결한다
            result["fred_saved"] = save_fred_secret(fred_key)
            reset_default_fred_key()
            if not result["fred_saved"]:
                result["fred_save_error"] = "키체인에 저장하지 못했습니다(이번 세션에는 연결됨)."
    elif req.body.get("clear_fred_key"):
        req.session.pop("fred_api_key", None)
    if req.body.get("fred_forget"):
        result["fred_forgotten"] = delete_fred_secret()
        reset_default_fred_key()
    req.session["agent_model"] = model or DEFAULT_AGENT_MODEL
    return {**result, "configured": bool(req.session.get("anthropic_api_key")), "model": req.session["agent_model"], **fred_status(req.session)}


@route("POST", "/api/agent/tool")
def post_agent_tool(req: Request):
    """허용된 도구 하나를 서버에서 실행한다. Toss 자격증명과 원본 응답은 반환하지 않는다."""
    name = str(req.body.get("name") or "").strip()
    if name not in TOOL_NAMES:
        raise ApiError(400, "허용되지 않은 Agent 도구입니다.")
    return execute_agent_tool(req, name, req.body.get("args"))


NEWS_MAX_SYMBOLS = 25  # 보유 + 관심종목이 이보다 많으면 보유 종목부터 이 개수까지만 본다


def portfolio_entries(req: Request) -> tuple[list[dict], int]:
    """뉴스·매크로 요인 도구가 같이 쓰는 종목 목록: 보유 종목(현금 취급 제외) + 관심종목, 최대 NEWS_MAX_SYMBOLS개(보유 우선).
    각 항목: symbol, name, currency, market, held, yahoo(야후 심볼 또는 None). 두 번째 값은 개수 제한으로 빠진 종목 수."""
    session, store = req.session, req.store
    holdings = build_holdings(account_snapshot(session), store)
    held = {r["심볼"]: r for r in holdings["stocks"]}
    watch = [s for s in load_watchlist(store) if s not in held]
    kr_held = [s for s, r in held.items() if r["통화"] == "KRW"]
    wanted = sorted(set(watch) | set(kr_held))
    info = call_with_reauth(session, lambda token: lookup_stocks(token, wanted)) if wanted else {}

    entries = [{"symbol": s, "name": r["종목"], "currency": r["통화"], "market": (info.get(s) or {}).get("market"), "held": True}
               for s, r in held.items()]
    entries += [{"symbol": s, "name": (info.get(s) or {}).get("name", s), "currency": (info.get(s) or {}).get("currency", "?"),
                 "market": (info.get(s) or {}).get("market"), "held": False} for s in watch]
    omitted = max(0, len(entries) - NEWS_MAX_SYMBOLS)
    entries = entries[:NEWS_MAX_SYMBOLS]
    for e in entries:
        e["yahoo"] = toss_to_yahoo_symbol(e["symbol"], e["market"], e["currency"])
    return entries, omitted


def fred_factor_series(session: dict) -> dict[str, dict]:
    """FRED 키가 있으면 FRED 요인 시세({"FRED:DFF": {날짜: 값}, …}), 없거나 받지 못하면 빈 사전(그 요인만 빠지고 나머지는 계속한다)."""
    key, _source = effective_fred_key(session)
    if not key:
        return {}

    def one(series_id):
        try:
            return fetch_fred_series(key, series_id)
        except FredError:
            return None
    got = parallel_map(one, FRED_IDS, 4)
    return {FRED_PREFIX + sid: series for sid, series in zip(FRED_IDS, got) if series}


FACTOR_WORKERS = 6   # 요인 시세 병렬 수신(후보가 40개를 넘어서 4 → 6)


def factors_for(yahoo_symbol: str | None, factor_series: dict) -> dict:
    """이 종목에 쓸 요인 시세: 같은 자산을 따르는 시리즈(금 ETF의 금 선물 등)는 뺀다."""
    drop = same_asset_factors(yahoo_symbol)
    return {k: v for k, v in factor_series.items() if k not in drop} if drop else factor_series


def fred_slow_series(session: dict) -> dict[str, dict]:
    """월간·주간 지표(CPI 전년비·실업률·연준 총자산): 거시 사례집의 국면별 변화에만 쓴다. FRED 키가 없거나 받지 못하면 빈 사전."""
    key, _source = effective_fred_key(session)
    if not key:
        return {}

    def one(series_id):
        try:
            return fetch_fred_series(key, series_id)
        except FredError:
            return None
    got = dict(zip(FRED_SLOW_IDS, parallel_map(one, FRED_SLOW_IDS, 3)))
    out = {}
    if got.get("CPIAUCSL"):
        out[FRED_PREFIX + "CPIYOY"] = yoy_series(got["CPIAUCSL"])
    if got.get("UNRATE"):
        out[FRED_PREFIX + "UNRATE"] = got["UNRATE"]
    if got.get("WALCL"):
        out[FRED_PREFIX + "WALCL"] = got["WALCL"]
    return {k: v for k, v in out.items() if v}


def yahoo_closes(symbol: str, years: int | None = None) -> dict | None:
    """야후 일봉 {날짜: 종가}. 실패하면 None(그 종목만 빠지고 나머지는 계속한다). years를 주면 더 긴 기간(과거 통계용)."""
    try:
        history = get_yahoo_daily_history(symbol) if years is None else get_yahoo_daily_history(symbol, years=years)
        return {bar[0]: bar[1] for bar in history["bars"] if bar[1]}
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def macro_drivers_view(req: Request, args: dict | None = None) -> dict:
    """이번 주 시장 요인(금리·달러·유가·VIX·환율·지수) 변화와, 보유·관심 종목 등락의 요인별 기여 추정. 읽기 전용.
    같이 움직인 정도의 통계 추정이며 인과의 증명이 아니다(설명력 R²를 함께 돌려준다). args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    entries, focused = focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    symbols = [f[0] for f in FACTORS] + [e["yahoo"] for e in entries if e["yahoo"]]
    closes = dict(zip(symbols, parallel_map(yahoo_closes, symbols, FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(fred_factor_series(req.session))
    items = []
    for e in entries:
        series = closes.get(e["yahoo"]) if e["yahoo"] else None
        result = explain_asset(series, factors_for(e["yahoo"], factor_series)) if series else {"status": "no_data", "asset_change_pct": None}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "status": result["status"],
                      "weekly_change_pct": result.get("asset_change_pct"), "r2": result.get("r2"),
                      "drivers": result.get("contributions", []), "residual_pct": result.get("residual_pct")})
    items.sort(key=lambda x: (x["weekly_change_pct"] is None, -abs(x["weekly_change_pct"] or 0), not x["held"]))
    return {"factors": weekly_factor_moves(factor_series), "items": items, "omitted": omitted, "focus": focus, "focused": focused}


VALUATION_MAX = 4              # 한 번에 밸류에이션을 보는 종목 수
VALUATION_PEER_SAMPLE = 25     # 섹터 비교 표본(같은 업종 S&P500 종목) 수
VALUATION_WORKERS = 6
_peer_cache: dict[str, tuple[float, dict, int]] = {}
_peer_lock = threading.Lock()


def sector_peer_stats(sector: str, exclude: str) -> tuple[dict | None, int]:
    """같은 GICS 업종 S&P500 종목 표본의 PER·PSR·PBR 중앙값과 받은 표본 수. 구성 종목 목록이나 표본을 못 받으면 (None, 0). 업종별로 6시간 캐시한다."""
    now = time.time()
    with _peer_lock:
        hit = _peer_cache.get(sector)
        if hit and now - hit[0] < INFO_CACHE_TTL:
            return hit[1], hit[2]
    try:
        constituents = fetch_sp500()
    except ScreenerError:
        return None, 0
    symbols = sample_peers(constituents, sector, exclude, VALUATION_PEER_SAMPLE)
    got = parallel_map(lambda sym: fetch_fundamentals(sym), symbols, VALUATION_WORKERS)
    peers = [g["metrics"] for g in got if g["status"] == "ok" and g["metrics"]["kind"] == "stock"]
    stats = peer_stats(peers)
    if stats:
        with _peer_lock:
            _peer_cache[sector] = (now, stats, len(peers))
    return (stats or None), len(peers)


def valuation_view(req: Request, args: dict | None = None) -> dict:
    """보유·관심 종목의 PER·PSR·PBR·EPS·BPS·ROE와 비싼가·싼가 판단(개별주는 같은 업종 S&P500 표본·성장·수익성 대비, 주식형 ETF는 시장 대비). 읽기 전용.
    야후 비공식 데이터이며 상대 평가다. 조회어에는 종목 코드만 쓴다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 4개)."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    entries, focused = focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - VALUATION_MAX)
    entries = entries[:VALUATION_MAX]
    got = parallel_map(lambda e: fetch_fundamentals(e["yahoo"]), entries, VALUATION_WORKERS)
    bench = None
    if any(g["status"] == "ok" and g["metrics"]["kind"] == "etf" for g in got):
        b = fetch_fundamentals(VALUATION_BENCHMARK)
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
                stats, peer_n = sector_peer_stats(m["sector"], e["yahoo"])
            assessment = assess_stock(m, stats)
        elif m["kind"] == "etf":
            assessment = assess_etf(m, bench)
        else:
            assessment = {"signals": [], "verdict": {"label": "na", "expensive": 0, "cheap": 0, "signals": 0}, "reason": "other", "notes": ["주식·ETF가 아닌 종목은 밸류에이션 대상이 아닙니다"]}
        item.update({"kind": m["kind"], "metrics": {k: m[k] for k in ("per", "forward_per", "psr", "pbr", "eps", "forward_eps", "bps", "roe", "peg", "price", "currency", "category", "sector", "flags")},
                     "assessment": assessment, "peer_n": peer_n})
        items.append(item)
    return {"items": items, "omitted": omitted, "focus": focus, "focused": focused}


VALUATION_BENCHMARK = "SPY"        # ETF의 PER·PBR을 견주는 시장 기준(S&P500 ETF)
RESPONSE_YEARS = 30


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
    closes = dict(zip(wanted, parallel_map(lambda s: yahoo_closes(s, RESPONSE_YEARS), wanted, FACTOR_WORKERS)))
    if not closes.get(symbol):
        raise ApiError(502, f"{label} 시세를 받지 못했습니다.")
    horizon = args.get("horizon_days") if args.get("horizon_days") in RESPONSE_HORIZONS else DEFAULT_RESPONSE_HORIZON
    out = analyze_response(closes[symbol], kind, {name: closes[sym] for sym, name in chosen if closes.get(sym)}, horizon)
    out["insufficient"] += [name for sym, name in chosen if not closes.get(sym)]
    out.update({"factor": {"symbol": symbol, "name": label, "kind": kind}, "direction": "down" if args.get("direction") == "down" else "up", "unresolved": unresolved})
    return out


DRAWDOWN_YEARS = 30          # 전 고점(상장 이후 최고 종가)을 구하려고 종목 시세를 길게 받는다
DRAWDOWN_WINDOW_DAYS = 252   # '최근 고점'은 최근 1년 안의 최고 종가: 요인 분해는 이 고점 날짜부터 지금까지 구간으로 한다
DRAWDOWN_MIN_DAYS = 3        # 고점이 이보다 가까우면 '고점 근처'로 보고 분해하지 않는다
DRAWDOWN_MAX = 4


DROP_NEWS_WORKERS = 4
DROP_NEWS_SCAN = 12          # 날짜마다 주제를 세려고 모으는 제목 수
DROP_NEWS_PER_QUERY = 6


def big_drop_days(entry: dict, series: dict, peak_date, factors: dict) -> list[dict]:
    """고점 이후 가장 크게 빠진 날들: 그날 종목 등락, 같은 날 평소보다 유난히 크게 움직인 시장 요인, 그날 앞뒤의 보도(구글 뉴스 날짜 검색).
    보도는 검색어(종목 이름, 그날 크게 움직인 요인 이름 + 급락·급등)에 걸린 제목일 뿐 원인으로 확정할 수 없다. 검색에 실패해도 날짜와 시장 움직임은 남는다."""
    days = worst_days(series, peak_date)
    changes = {s: daily_changes(v, FACTOR_BY_SYMBOL[s][2]) for s, v in factors.items() if s in FACTOR_BY_SYMBOL}
    meta = {s: FACTOR_BY_SYMBOL[s][1:] for s in changes}

    def one(day):
        movers = day_movers(day["date"], changes, meta)
        queries = [q for q in (name_query(entry["name"], entry["symbol"]),) if q] + [search_term(m["label"], m["change"]) for m in movers[:2]]
        found = headlines_around(queries or [f"{entry['symbol']} 급락"], day["date"], limit=DROP_NEWS_SCAN, per_query=DROP_NEWS_PER_QUERY)   # 많이 받아 주제를 센다(화면에는 앞의 3건만)
        return {"date": day["date"].isoformat(), "change_pct": day["change_pct"], "movers": movers, "headlines": found[:3], "pool": found, "themes": themes_in([h["title"] for h in found]),
                "risk_down": any(m["symbol"] in RISK_SYMBOLS and m["change"] < 0 for m in movers)}
    return parallel_map(one, days, DROP_NEWS_WORKERS)


def price_series(series: dict, peak_date, key_days: list, limit: int = 120) -> dict:
    """고점 직전 3주부터 지금까지의 종가(그래프용). limit개 안팎으로 솎되 고점·마지막·큰 하락일은 반드시 남긴다."""
    dates = [d for d in sorted(series) if d >= peak_date - timedelta(days=21)]
    step = max(1, len(dates) // limit)
    keep = set(dates[::step]) | {dates[0], dates[-1], peak_date} | set(key_days)
    chosen = [d for d in dates if d in keep]
    return {"dates": [d.isoformat() for d in chosen], "closes": [round(series[d], 2) for d in chosen]}


def drawdown_view(req: Request, args: dict | None = None) -> dict:
    """고점 대비 얼마나 빠졌고 왜 빠졌나: 종목의 전 고점(상장 이후)과 최근 1년 고점 대비 낙폭, 최근 고점 날짜부터 지금까지 같은 구간에서
    시장 요인(금리·달러·유가·VIX·환율·지수)이 얼마나 움직였는지와 종목 하락의 요인별 기여 추정(최근 1년 일간 관계의 계수 × 구간 요인 변화). 읽기 전용.
    같이 움직인 정도의 통계 추정이며 인과의 증명이 아니다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 4개)."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    entries, focused = focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - DRAWDOWN_MAX)
    entries = entries[:DRAWDOWN_MAX]
    wanted = list(dict.fromkeys([f[0] for f in FACTORS] + [e["yahoo"] for e in entries]))
    closes = dict(zip(wanted, parallel_map(lambda sym: yahoo_closes(sym, DRAWDOWN_YEARS if sym in {e["yahoo"] for e in entries} else None), wanted, FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(fred_factor_series(req.session))
    items = []
    for e in entries:
        series = closes.get(e["yahoo"])
        item = {"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "status": "no_data"}
        if series and len(series) > 2:
            dates = sorted(series)
            last = dates[-1]
            ath_date = max(dates, key=lambda d: series[d])
            recent = dates[-DRAWDOWN_WINDOW_DAYS:]
            peak_date = max(recent, key=lambda d: series[d])
            since_days = len([d for d in dates if d > peak_date])
            low_date = min((d for d in dates if d >= peak_date), key=lambda d: series[d])
            item.update({"last_date": last.isoformat(), "last_close": series[last], "peak_date": peak_date.isoformat(), "peak_close": series[peak_date],
                         "drawdown_pct": (series[last] / series[peak_date] - 1) * 100, "since_days": since_days,
                         "ath_date": ath_date.isoformat(), "ath_close": series[ath_date], "ath_drawdown_pct": (series[last] / series[ath_date] - 1) * 100,
                         "low_date": low_date.isoformat(), "low_drawdown_pct": (series[low_date] / series[peak_date] - 1) * 100})
            if since_days < DRAWDOWN_MIN_DAYS or series[last] >= series[peak_date]:
                item["status"] = "near_peak"
            else:
                factors = factors_for(e["yahoo"], factor_series)
                macro_only = {k: v for k, v in factors.items() if factor_group(k) == "macro"}    # 먼저 매크로 요인(금리·달러·유가·VIX…)만으로: 은·지수 같은 같은 계열 자산이 설명을 가로채지 않게
                result, basis = explain_asset(series, macro_only, since=peak_date), "macro"
                if result["status"] != "ok":                                                    # 매크로만으로 설명이 안 되면 시장 전체(지수·업종·귀금속 등)까지 넣어 본다
                    wider = explain_asset(series, factors, since=peak_date)
                    if wider["status"] == "ok":
                        result, basis = wider, "market"
                moves = [{**m, "group": factor_group(m["symbol"])} for m in weekly_factor_moves(factors, since=peak_date)]
                item["drop_days"] = big_drop_days(e, series, peak_date, factors)
                item["causes"] = rank_themes(item["drop_days"])
                item["series"] = price_series(series, peak_date, [date.fromisoformat(d["date"]) for d in item["drop_days"]])
                for day in item["drop_days"]:
                    day.pop("pool", None)
                pressure = window_pressure(series, macro_only, peak_date)                       # 회귀에 안 잡힌 요인도 평소 관계로 한 번 더 본다(매크로 요인만)
                item.update({"pressure": pressure[:6],
                             "status": result["status"], "reason": result.get("reason", ""), "r2": result.get("r2"), "oos_r2": result.get("oos_r2"), "basis": basis,
                             "drivers": result.get("contributions", []), "residual_pct": result.get("residual_pct"), "moves": moves})
        items.append(item)
    return {"items": items, "omitted": omitted, "focus": focus, "focused": focused}


OUTLOOK_HISTORY_YEARS = 30   # 종목 자신의 일봉(상장 이후, 최대 30년). 더 긴 대용 시세는 long_history가 따로 알려 준다
OUTLOOK_MAX_SYMBOLS = 12
_JOSA = r"(?:으로|에서|이랑|하고|[은는이가을를의도에와과만랑])?"


def mentions(text: str, term: str) -> bool:
    """질문(text)에 이름·코드(term)가 따로 쓰였는지. '금'이 '금리'·'현금' 속에서 잡히지 않게 한글/영문 경계를 본다
    (뒤에 조사는 허용: 금의, 금을, 금은). 이름에 공백이 있으면 공백 없이 쓴 경우도 받는다(KODEX 200 = kodex200)."""
    term = term.strip()
    if len(term) < 1:
        return False
    pattern = rf"(?<![가-힣a-z0-9]){{body}}{_JOSA}(?![가-힣a-z0-9])"
    lowered = text.lower()
    if re.search(pattern.format(body=re.escape(term.lower())), lowered):
        return True
    squeezed = re.sub(r"\s+", "", term.lower())
    return squeezed != term.lower() and re.search(pattern.format(body=re.escape(squeezed)), re.sub(r"(?<=[a-z0-9])\s+(?=[a-z0-9])", "", lowered)) is not None


def _one_edit_apart(a: str, b: str) -> bool:
    """두 문자열이 글자 하나(바꿈·빠짐·더함)만 다르거나 이웃한 두 글자만 뒤바뀌었는지. 같은 문자열은 False."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] - diff[0] == 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


TYPO_MIN_LENGTH = 4   # 이보다 짧은 코드는 글자 하나만 달라도 전혀 다른 종목이라 오타로 보지 않는다


def typo_mentions(text: str, term: str) -> bool:
    """영문·숫자 코드의 한 글자 오타(GLDN → GLDM, 이웃한 두 글자 뒤바뀜 포함)를 허용하는 느슨한 일치. 정확히 일치하는 경우는 mentions가 맡는다."""
    term = term.strip().lower()
    if len(term) < TYPO_MIN_LENGTH or not re.fullmatch(r"[a-z0-9.\-]+", term):
        return False
    return any(_one_edit_apart(token, term) for token in re.findall(r"[a-z0-9.\-]+", text.lower()) if len(token) >= TYPO_MIN_LENGTH)


EXTRA_MAX = 3                       # 한 질문에서 보유·관심종목이 아닌 종목을 몇 개까지 볼지
EXTRA_CACHE_TTL = 6 * 3600
_extra_cache: dict[str, tuple[float, dict | None]] = {}
_extra_lock = threading.Lock()


def extra_skip_words() -> frozenset:
    """검색에 보내지 않을 낱말: 질문 의도 낱말(고평가·요인·뉴스…)과 종목이 아닌 영문 약어."""
    words = {w for ws in INTENT_WORDS.values() for w in ws} | set(VALUATION_WORDS) | set(DRAWDOWN_WORDS) | {w.lower() for w in NOT_TICKERS}
    return frozenset(words | {"per", "pbr", "psr", "eps", "bps", "roe", "etf", "ma"})


def resolve_extra_entries(req: Request, text: str, entries: list[dict]) -> list[dict]:
    """질문에 적혔지만 보유·관심종목이 아닌 종목(예: "엔비디아 PER 어때?", "TSLA 왜 빠졌어?")을 토스 검증을 거친 검색으로 찾는다.
    검색에는 후보 낱말(종목 이름·코드일 수 있는 것)만 보내고 질문 전체나 계좌 정보는 보내지 않는다. 이름·코드가 정확히 맞는 결과만 받는다.
    찾은 종목은 held=False, extra=True인 항목으로 최대 EXTRA_MAX개. 검색 실패·인증 문제는 그 낱말만 건너뛴다."""
    known = {e["symbol"].lower() for e in entries} | {re.sub(r"\s+", "", e["name"]).lower() for e in entries}
    tokens = [t for t in candidate_tokens(text, extra_skip_words()) if t.lower() not in known and not any(t.lower() in k for k in known if len(k) >= 3)]
    if not tokens:
        return []
    now = time.time()

    def one(token):
        key = token.lower()
        with _extra_lock:
            hit = _extra_cache.get(key)
            if hit and now - hit[0] < EXTRA_CACHE_TTL:
                return hit[1]
        try:
            results = call_with_reauth(req.session, lambda t: search_stocks(t, token, 5))
        except (requests.RequestException, ValueError, KeyError, ApiError):
            return None                                           # 일시 오류는 캐시하지 않는다
        pick = choose_symbol(token, results)
        entry = None
        if pick:
            yahoo = toss_to_yahoo_symbol(pick["symbol"], pick.get("market"), pick.get("currency", ""))
            if yahoo:
                entry = {"symbol": pick["symbol"], "name": pick.get("name") or pick["symbol"], "currency": pick.get("currency", ""), "market": pick.get("market"),
                         "held": False, "extra": True, "yahoo": yahoo}
        with _extra_lock:
            _extra_cache[key] = (now, entry)
        return entry
    found, seen = [], {e["symbol"] for e in entries}
    for entry in parallel_map(one, tokens, 4):
        if entry and entry["symbol"] not in seen:
            seen.add(entry["symbol"])
            found.append(dict(entry))
    return found[:EXTRA_MAX]


def focus_with_extras(req: Request, entries: list[dict], categories: list[dict], focus: str) -> tuple[list[dict], bool]:
    """focus_entries에 '보유·관심종목이 아닌 종목'을 더한다. 질문이 보유·관심종목과 새 종목을 함께 가리키면 둘 다, 새 종목만이면 새 종목만 돌려준다."""
    matched, focused = focus_entries(entries, categories, focus)
    extras = resolve_extra_entries(req, focus, entries) if focus.strip() else []
    if extras:
        return (matched if focused else []) + extras, True
    return matched, focused


def focus_entries(entries: list[dict], categories: list[dict], focus: str) -> tuple[list[dict], bool]:
    """질문에서 가리킨 종목만 고른다: 종목 코드·이름, 또는 리밸런싱 카테고리 이름(그 카테고리의 종목들).
    정확히 일치하는 게 없으면 영문 코드의 한 글자 오타까지 받는다. 아무것도 못 찾으면 전체를 돌려주고 찾았는지(False)를 알린다."""
    if not focus.strip():
        return entries, False
    from_labels = {s for c in categories if mentions(focus, c["label"]) for s in c["symbols"]}
    matched = [e for e in entries if e["symbol"] in from_labels or mentions(focus, e["symbol"]) or mentions(focus, e["name"])]
    if not matched:
        matched = [e for e in entries if typo_mentions(focus, e["symbol"]) or typo_mentions(focus, e["name"])]
    return (matched, True) if matched else (entries, False)


# 영문 대문자 단어 중 종목 코드가 아닌 것(질문에 자주 나오는 약어)
NOT_TICKERS = frozenset({"ETF", "AI", "MA", "RSI", "USD", "KRW", "JPY", "EUR", "CPI", "PPI", "GDP", "FED", "FOMC", "PER", "PBR", "EPS", "IPO",
                         "VIX", "SOX", "S&P", "PDF", "API", "OK", "TV", "IT", "CEO", "QE", "QT", "BTC", "ETH"})


def focus_info(req: Request, question: str) -> dict:
    """규칙 기반 모드용: 질문이 내 종목(코드·이름·카테고리)을 가리키는지(found), 질문 예시에 쓸 내 종목 이름(names),
    질문에 적혔지만 보유·관심종목에 없는 영문 코드(unknown). 계좌 조회가 실패하면 아무것도 못 찾은 것으로 본다."""
    empty = {"found": False, "names": [], "unknown": []}
    try:
        entries, _omitted = portfolio_entries(req)
        categories = load_rebalance_config(req.store)["targets"]
    except (requests.RequestException, ValueError, KeyError):
        return empty
    matched, found = focus_entries(entries, categories, question)
    extras = resolve_extra_entries(req, question, entries)          # 보유·관심종목이 아니어도 이름·코드로 찾을 수 있는 종목이면 '종목을 가리킨 질문'으로 본다
    found = found or bool(extras)
    known = {e["symbol"].upper() for e in entries + extras} | {w.upper() for e in entries + extras for w in re.findall(r"[A-Za-z0-9]+", e["name"])}
    unknown = [tok for tok in dict.fromkeys(re.findall(r"(?<![A-Za-z0-9])[A-Z]{2,6}(?![A-Za-z0-9])", question))
               if tok not in NOT_TICKERS and tok not in known and not any(typo_mentions(tok, e["symbol"]) for e in entries)]
    return {"found": found, "names": [e["name"] for e in entries[:4]], "unknown": unknown[:3]}


def outlook_view(req: Request, args: dict | None = None) -> dict:
    """종목별 앞으로의 상승/하락 뷰 재료(과거 유사 국면 통계 + 요인 민감도 + 변동성 범위). 읽기 전용, 예측이 아니다.
    args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 12개), 없으면 보유·관심 종목 전체."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    categories = load_rebalance_config(req.store)["targets"]
    entries, focused = focus_with_extras(req, entries, categories, focus)
    omitted += max(0, len(entries) - OUTLOOK_MAX_SYMBOLS)
    entries = entries[:OUTLOOK_MAX_SYMBOLS]
    asset_symbols = [e["yahoo"] for e in entries if e["yahoo"]]
    factor_symbols = [f[0] for f in FACTORS]
    long_closes = dict(zip(asset_symbols, parallel_map(lambda s: yahoo_closes(s, OUTLOOK_HISTORY_YEARS), asset_symbols, 4)))
    factor_closes = dict(zip(factor_symbols, parallel_map(yahoo_closes, factor_symbols, FACTOR_WORKERS)))
    factor_series = {s: c for s, c in factor_closes.items() if c}
    factor_series.update(fred_factor_series(req.session))
    sources = {e["yahoo"]: long_source_for(e["yahoo"]) for e in entries if e["yahoo"]}     # 상장이 최근인 종목은 더 긴 대용 시세로 과거를 센다
    proxy_symbols = sorted({s["symbol"] for s in sources.values() if s and s["kind"] == "daily"})
    proxy_closes = dict(zip(proxy_symbols, parallel_map(lambda s: yahoo_closes(s, LONG_PROXY_YEARS), proxy_symbols, 4)))
    gold = None
    if any(s and s["kind"] == "monthly_gold" for s in sources.values()):
        try:
            gold = fetch_gold_monthly()
        except (requests.RequestException, ValueError):
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
        result = build_outlook(series, factors_for(e["yahoo"], factor_series), long_series, long_error) if series else {"status": "no_data", "history_days": 0}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), **result})
    return {"items": items, "focus": focus, "focused": focused, "omitted": omitted}


ETF_MAX_BREAKDOWNS = 6        # 한 번에 구성 종목을 분해하는 ETF 수(질문에서 지정하지 않았을 때)
ETF_TOP_CONTRIBUTORS = 3      # ETF마다 요인을 다시 분석하는 큰 기여 종목 수
ETF_NEWS_PER_ETF = 2          # ETF마다 보도를 찾는 큰 기여 종목 수
ETF_NEWS_MAX_SYMBOLS = 8


def etf_breakdown_view(req: Request, args: dict | None = None) -> dict:
    """보유·관심 ETF의 이번 주 등락을 상위 편입 종목의 기여로 나누고, 큰 기여 종목은 매크로 요인과 보도까지 붙인다. 읽기 전용.
    args["focus"]가 있으면 그 종목·카테고리의 ETF만. 편입 정보가 없는 종목(개별주·금/채권 ETF·한국 ETF 등)은 no_holdings로 알린다.
    야후 조회어에는 종목 코드만, 뉴스 조회어에는 이름·코드만 쓰고 수량·금액·계좌 정보는 보내지 않는다."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    categories = load_rebalance_config(req.store)["targets"]
    entries, focused = focus_with_extras(req, entries, categories, focus)
    entries = [e for e in entries if e["yahoo"]]
    holdings = dict(zip((e["symbol"] for e in entries), parallel_map(lambda e: fetch_top_holdings(e["yahoo"]), entries, 4)))
    etfs = [e for e in entries if holdings[e["symbol"]]["status"] == "ok"]
    no_holdings = [{"symbol": e["symbol"], "name": e["name"], "reason": holdings[e["symbol"]]["status"]}
                   for e in entries if holdings[e["symbol"]]["status"] != "ok"]
    omitted += max(0, len(etfs) - ETF_MAX_BREAKDOWNS)
    etfs = etfs[:ETF_MAX_BREAKDOWNS]
    constituents = sorted({h["symbol"] for e in etfs for h in holdings[e["symbol"]]["holdings"]})
    own_symbols = [e["yahoo"] for e in etfs]
    fetch_list = list(dict.fromkeys(own_symbols + constituents))
    closes = dict(zip(fetch_list, parallel_map(yahoo_closes, fetch_list, 6)))
    items = []
    for e in etfs:
        series = closes.get(e["yahoo"])
        result = etf_breakdown(series, holdings[e["symbol"]]["holdings"], {s: c for s, c in closes.items() if c}) if series else {"status": "no_data"}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), **result, "drivers": {}, "headlines": {}})
    leaders = {i["symbol"]: [r for r in i.get("rows", []) if r["contribution_pct"] is not None][:ETF_TOP_CONTRIBUTORS]
               for i in items if i["status"] == "ok"}
    if any(leaders.values()):   # 큰 기여 종목의 요인 분석(기존 매크로 회귀)과 보도
        factor_closes = dict(zip((f[0] for f in FACTORS), parallel_map(yahoo_closes, [f[0] for f in FACTORS], FACTOR_WORKERS)))
        factor_series = {s: c for s, c in factor_closes.items() if c}
        factor_series.update(fred_factor_series(req.session))
        news_targets, seen = [], set()
        for item in items:
            for row in leaders.get(item["symbol"], []):
                explained = explain_asset(closes[row["symbol"]], factors_for(row["symbol"], factor_series)) if closes.get(row["symbol"]) else {"status": "no_data"}
                item["drivers"][row["symbol"]] = {"status": explained["status"], "r2": explained.get("r2"),
                                                  "contributions": explained.get("contributions", [])[:2]}
            for row in leaders.get(item["symbol"], [])[:ETF_NEWS_PER_ETF]:
                if row["symbol"] not in seen and len(news_targets) < ETF_NEWS_MAX_SYMBOLS:
                    seen.add(row["symbol"])
                    news_targets.append({"symbol": row["symbol"], "name": row["name"], "use_yahoo": True})
        found = headlines_for_many(news_targets) if news_targets else {}
        for item in items:
            for row in leaders.get(item["symbol"], [])[:ETF_NEWS_PER_ETF]:
                if row["symbol"] in found:
                    item["headlines"][row["symbol"]] = found[row["symbol"]]
    return {"items": items, "no_holdings": no_holdings, "focus": focus, "focused": focused, "omitted": omitted}


MACRO_HISTORY_MAX = 6   # 한 번에 과거 충격 반응을 보는 종목 수(요인 14개와 종목별 긴 일봉을 받으므로 제한한다)


def macro_history_view(req: Request, args: dict | None = None) -> dict:
    """지금 큰 변동 중인 매크로 요인과, 과거 같은 방향의 큰 변동 때·이름 붙은 거시 이슈 때 보유·관심 종목이 어떻게 움직였는지. 읽기 전용.
    같이 움직인 기록이며 원인의 증명이 아니다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 6개).
    금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물·지수)으로 본다."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    entries, focused = focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - MACRO_HISTORY_MAX)
    entries = entries[:MACRO_HISTORY_MAX]
    sources = {e["yahoo"]: event_source_for(e["yahoo"]) for e in entries}
    wanted = list(dict.fromkeys([f[0] for f in FACTORS] + [e["yahoo"] for e in entries] + [s["symbol"] for s in sources.values() if s]))
    closes = dict(zip(wanted, parallel_map(lambda s: yahoo_closes(s, LONG_PROXY_YEARS), wanted, FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(fred_factor_series(req.session))
    items = []
    for e in entries:
        own, source = closes.get(e["yahoo"]), sources.get(e["yahoo"])
        proxy = closes.get(source["symbol"]) if source else None
        used, label = (proxy, source["label"]) if proxy and (not own or len(proxy) > len(own)) else (own, None)
        result = analyze_shocks(used, factors_for(e["yahoo"], factor_series)) if used else {"status": "no_data", "history_days": 0}   # 요인을 못 받아도 이름 붙은 이슈 반응은 계산한다
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "source": label, **result})
    return {"items": items, "focus": focus, "focused": focused, "omitted": omitted}


CASES_REFERENCE_SYMBOLS = {"tnx": "^TNX", "irx": "^IRX", "dxy": "DX-Y.NYB", "spx": "^GSPC", "vix": "^VIX", "oil": "CL=F", "xlf": "XLF"}


def macro_cases_view(req: Request, args: dict | None = None) -> dict:
    """거시 사례집: 질문이 지정한 사례(국면별 서사 + 실제 시세로 계산한 금·금리·달러·증시 변화 + 통념 점검)와 지금과 닮은 과거 국면. 읽기 전용.
    args["case"]에 사례 이름·연도·사건 키워드가 있으면 그 사례들(최대 3개), args["focus"]에 종목이 있으면 그 종목의 국면별 변화와 그 뒤 3개월도 함께.
    FRED 키가 있으면 연방기금금리·실질금리·기대인플레이션도 쓴다. 같이 움직인 기록이며 인과의 증명이 아니다."""
    case_text = str((args or {}).get("case") or "").strip()[:300]
    focus = str((args or {}).get("focus") or "").strip()[:200]
    symbols = list(CASES_REFERENCE_SYMBOLS.values()) + ["GC=F"]
    closes = dict(zip(symbols, parallel_map(lambda s: yahoo_closes(s, LONG_PROXY_YEARS), symbols, 4)))
    refs = {key: closes.get(sym) for key, sym in CASES_REFERENCE_SYMBOLS.items()}
    refs.update(fred_factor_series(req.session))
    refs.update(fred_slow_series(req.session))        # CPI 전년비·실업률·연준 총자산(국면별 변화에만 쓴다)
    try:
        monthly_gold = fetch_gold_monthly()
    except (requests.RequestException, ValueError):
        monthly_gold = None
    asset = None
    if focus:
        entries, _omitted = portfolio_entries(req)
        matched, found = focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
        entry = next((e for e in matched if e["yahoo"]), None) if found else None
        series = yahoo_closes(entry["yahoo"], LONG_PROXY_YEARS) if entry else None
        if entry and series:
            asset = {"name": entry["name"] or entry["symbol"], "series": series}
    out = build_cases({k: v for k, v in refs.items() if v}, closes.get("GC=F"), monthly_gold, case_text, asset)
    out["gold_available"] = bool(closes.get("GC=F") or monthly_gold)
    return out


ASSET_DRIVERS_MAX = 4          # 한 번에 요인 순위를 보는 종목 수
ASSET_DRIVERS_YEARS = 20       # 10년 상관을 위해 종목과 요인 모두 넉넉히 받는다


def asset_drivers_view(req: Request, args: dict | None = None) -> dict:
    """이 종목이 평소 가장 민감한 매크로 요인 순위(1년·3년·10년 상관, 일관성, 같이 움직이는 요인 묶음). 읽기 전용, 같이 움직인 정도이며 원인의 증명이 아니다.
    args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만(최대 4개). 금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물·지수)으로 본다."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    entries, focused = focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)
    entries = [e for e in entries if e["yahoo"]]
    omitted += max(0, len(entries) - ASSET_DRIVERS_MAX)
    entries = entries[:ASSET_DRIVERS_MAX]
    sources = {e["yahoo"]: event_source_for(e["yahoo"]) for e in entries}
    wanted = list(dict.fromkeys([f[0] for f in FACTORS] + [e["yahoo"] for e in entries] + [s["symbol"] for s in sources.values() if s]))
    closes = dict(zip(wanted, parallel_map(lambda s: yahoo_closes(s, ASSET_DRIVERS_YEARS), wanted, FACTOR_WORKERS)))
    factor_series = {f[0]: closes[f[0]] for f in FACTORS if closes.get(f[0])}
    factor_series.update(fred_factor_series(req.session))
    items = []
    for e in entries:
        own, source = closes.get(e["yahoo"]), sources.get(e["yahoo"])
        proxy = closes.get(source["symbol"]) if source else None
        used, label = (proxy, source["label"]) if proxy and (not own or len(proxy) > len(own)) else (own, None)
        result = rank_drivers(used, factors_for(e["yahoo"], factor_series)) if used and factor_series else {"status": "no_data"}
        items.append({"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "source": label, **result})
    return {"items": items, "focus": focus, "focused": focused, "omitted": omitted}


SCREEN_YEARS = 6        # 240주(약 4.6년) + 여유
SCREEN_WORKERS = 8
SCREEN_MAX_MATCHES = 80  # 도구 결과에 담는 종목 수(전체 개수는 따로 알린다)


def ma_screen_view(req: Request, args: dict | None = None) -> dict:
    """주봉 이동평균 스크리너: S&P500 구성 종목(또는 내 보유·관심 종목) 중 종가가 지정한 주봉 MA 여러 개의 모두(또는 하나라도) 아래·위에 있는 종목. 읽기 전용.
    args: universe("sp500"|"mine"), periods(예: [60,120,200,240]), side("below"|"above"), match("all"|"any"), sector(영문 GICS 업종, 선택).
    S&P500은 500개 시세를 받아 첫 조회에 30~60초가 걸리고 이후에는 캐시된다. 같이 움직인 위치 정보일 뿐 매매 신호가 아니다."""
    args = args if isinstance(args, dict) else {}
    mine = args.get("universe") == "mine"
    periods = clean_periods(args.get("periods"))
    sector = str(args.get("sector") or "").strip()[:40]
    if mine:
        entries, _omitted = portfolio_entries(req)
        universe = [{"symbol": e["yahoo"], "name": e["name"] or e["symbol"], "sector": ""} for e in entries if e["yahoo"]]
    else:
        try:
            universe = fetch_sp500()
        except ScreenerError as e:
            raise ApiError(502, str(e))

    def fetch(symbol):
        try:
            bars = get_yahoo_daily_history(symbol, years=SCREEN_YEARS)["bars"]
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return None
        return weekly_closes(bars), (bars[-1][0] if bars else None)
    wanted = [u["symbol"] for u in universe if not sector or sector.lower() in str(u.get("sector") or "").lower()]
    got = dict(zip(wanted, parallel_map(fetch, wanted, SCREEN_WORKERS)))
    weekly = {s: v[0] for s, v in got.items() if v is not None}
    as_of = max((v[1] for v in got.values() if v and v[1]), default=None)
    out = run_screen(universe, weekly, periods, str(args.get("side") or "below"), str(args.get("match") or "all"), sector or None)
    out.update({"universe_name": "mine" if mine else "sp500", "sector": sector, "as_of": as_of.isoformat() if as_of else None,
                "total_matches": len(out["matches"])})
    out["symbols"] = [m["symbol"] for m in out["matches"]][:300]       # 표에 넣지 못한 종목도 코드는 모두 알려 준다
    out["matches"] = out["matches"][:SCREEN_MAX_MATCHES]
    return out


TREND_YEARS = 30        # 전 고점(상장 이후 최고 종가)을 구하려고 받을 수 있는 시세를 길게 받는다(업종 ETF는 1998년~). 12개월 수익률·200일선·12주 전 상태에는 2년 남짓이면 충분하다
TREND_UNIVERSE_MAX = 25


def trend_view(req: Request, args: dict | None = None) -> dict:
    """미국 업종 ETF(SPDR 11개) 또는 내 보유·관심 종목의 추세 사다리와 전환 상태. 읽기 전용, 위치·방향 정보일 뿐 매매 신호가 아니다.
    args: universe("sectors"|"mine"), focus("all"|"short"|"mid"|"long": 보이는 순서만 바꾼다), 이번 질문에서만 판정 기준을 바꾸는 값(buffer_pct 등, 올바르지 않으면 무시).
    기본 기준은 trend.DEFAULTS(버퍼 ±2%, 연속 2주 확인, 21·63·252거래일, 50·200일선)."""
    args = args if isinstance(args, dict) else {}
    mine = args.get("universe") == "mine"
    cfg = trend_mod.clean_settings(trend_mod.valid_overrides(args))
    if mine:
        entries, _omitted = portfolio_entries(req)
        listed = [(e["yahoo"], e["name"] or e["symbol"]) for e in entries if e["yahoo"]][:TREND_UNIVERSE_MAX]
    else:
        listed = list(trend_mod.SECTOR_ETFS)
    names = dict(listed)
    wanted = list(dict.fromkeys([s for s, _ in listed] + [trend_mod.BENCHMARK[0]]))
    closes = dict(zip(wanted, parallel_map(lambda s: yahoo_closes(s, TREND_YEARS), wanted, FACTOR_WORKERS)))

    def ordered(series):
        return [series[d] for d in sorted(series)] if series else []
    out = trend_mod.analyze({s: ordered(closes.get(s)) for s, _ in listed if closes.get(s)}, names, cfg, ordered(closes.get(trend_mod.BENCHMARK[0])) or None,
                            str(args.get("focus") or "all"))
    out["insufficient"] += [s for s, _ in listed if not closes.get(s)]     # 시세를 못 받은 종목도 '판단하지 못함'으로 알린다
    last_dates = [max(c) for c in closes.values() if c]
    out.update({"universe": "mine" if mine else "sectors", "as_of": max(last_dates).isoformat() if last_dates else None})
    return out


def weekly_news_view(req: Request, args: dict | None = None) -> dict:
    """보유 종목(현금 취급 제외)과 관심종목의 최근 5거래일 등락률 + 최근 7일 뉴스 헤드라인. 읽기 전용.
    뉴스 조회어에는 종목 이름·코드만 쓰고 수량·금액·계좌 정보는 보내지 않는다. args["focus"]에 종목 이름·코드·카테고리 이름이 있으면 그 종목만."""
    focus = str((args or {}).get("focus") or "").strip()[:200]
    entries, omitted = portfolio_entries(req)
    entries, focused = focus_with_extras(req, entries, load_rebalance_config(req.store)["targets"], focus)

    def change(entry):
        if not entry["yahoo"]:
            return None
        try:
            return weekly_change_pct([bar[1] for bar in get_yahoo_daily_history(entry["yahoo"])["bars"]])
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return None
    changes = parallel_map(change, entries, 4)
    headlines = headlines_for_many([{"symbol": e["symbol"], "name": e["name"], "use_yahoo": e["currency"] != "KRW"} for e in entries])
    items = [{"symbol": e["symbol"], "name": e["name"], "held": e["held"], "extra": e.get("extra", False), "weekly_change_pct": c,
              "headlines": headlines[e["symbol"]]["headlines"], "news_error": headlines[e["symbol"]]["error"]}
             for e, c in zip(entries, changes)]
    items.sort(key=lambda x: (x["weekly_change_pct"] is None, -abs(x["weekly_change_pct"] or 0), not x["held"]))
    return {"items": items, "omitted": omitted, "focus": focus, "focused": focused}


def strategy_signals_for(session: dict, store: UserStore, held: set[str], symbols: list[str]) -> dict[str, dict]:
    """종목별 주봉 MA 전략 신호(진입 비율 등). 읽기 전용이다 — 전략 상태(돌파 기록)는 복사본으로만 계산하고 저장하지 않는다.
    전략 제외·현금 취급 종목은 excluded, 배정이 없으면 none, 시세를 못 받으면 unavailable.
    보유하지 않은 종목은 기본 전략을 배정해서 오늘 시세 기준으로 판정한다(과거 돌파 기록 없음)."""
    data = load_strategy_templates(store)
    cash_symbols = set(load_cash_symbols(store))
    excluded = set(data["excluded_symbols"]) | cash_symbols
    assignments, _ = effective_assignments(data, held, cash_symbols)
    default = data.get("default_template", "")
    if default in data["templates"]:
        for symbol in symbols:
            if symbol not in assignments and symbol not in excluded:
                assignments[symbol] = default
    signals: dict[str, dict] = {}
    targets = []
    for symbol in symbols:
        if symbol in excluded:
            signals[symbol] = {"status": "excluded"}
        elif symbol not in assignments:
            signals[symbol] = {"status": "none"}
        else:
            targets.append(symbol)
    if targets:
        state = copy.deepcopy(load_strategy_state(store))
        try:
            rows, _ = call_with_reauth(session, lambda token: compute_strategy_rows(
                token, data["templates"], assignments, set(data["excluded_symbols"]), state, targets))
        except requests.RequestException:
            rows = []
        by_symbol = {r["symbol"]: r for r in rows}
        for symbol in targets:
            row = by_symbol.get(symbol)
            if row and row.get("ok"):
                template_steps = {s["id"]: s for s in data["templates"][assignments[symbol]]["steps"]}

                def step_name(step):  # 데이터가 부족하면 평가 결과에 label이 없다 → 템플릿 설정에서 이름을 만든다
                    spec = template_steps.get(step.get("id"), {})
                    params = spec.get("params", {})
                    if spec.get("indicator") == "MA":
                        return f"MA{params.get('period')}{'주' if params.get('interval') == 'week' else '일'}"
                    return str(spec.get("indicator") or step.get("id"))
                signals[symbol] = {
                    "status": "ok", "entry_pct": row["entry_pct"], "cycle": row.get("cycle", 1),
                    "stage_count": row.get("stage_count", 0), "stage_total": row.get("stage_total", 0),
                    "recovered": bool(row.get("recovered")),
                    "steps": [{"label": s.get("label") or step_name(s), "broken": bool(s.get("broken")),
                               "below": bool(s.get("ok") and s.get("triggered")), "ok": bool(s.get("ok"))}
                              for s in row.get("steps", [])],
                }
            else:
                signals[symbol] = {"status": "unavailable"}
    return signals


def attach_trade_quantities(session: dict, store: UserStore, holdings: dict) -> None:
    """리밸런싱 계획의 매수·매도 행마다 종목별 금액과 주수(allocations)를 붙인다. 보유 종목이 없는 카테고리를 사야
    할 때만 첫 번째 종목의 현재가를 토스에서 따로 받는다(실패해도 계획은 그대로, 주수만 비움)."""
    plan = holdings.get("rebalance_plan") or {}
    rows = plan.get("rows") or []
    held = holdings["stocks"] + holdings["cash"]["stocks"]
    categories = holdings.get("rebalance_categories") or []
    quotes: dict[str, dict] = {}
    need = symbols_needing_quotes(rows, categories, held)
    if need:
        try:
            info = call_with_reauth(session, lambda token: lookup_stocks(token, need))
            prices = call_with_reauth(session, lambda token: get_prices(token, need))
        except requests.RequestException:
            info, prices = {}, {}
        fx = to_float(holdings.get("usd_krw"))
        for symbol in need:
            currency, price = (info.get(symbol) or {}).get("currency"), prices.get(symbol)
            if currency and price:
                quotes[symbol] = {"name": info[symbol].get("name", symbol), "currency": currency,
                                  "price_krw": price * (fx if currency == "USD" else 1.0)}
    result = allocate_trade_shares(rows, categories, held, quotes)
    for row in rows:
        row["allocations"] = result["allocations"].get(row["label"], [])
    plan["quote_errors"] = result["missing"]
    # 주봉 MA 전략 신호를 반영해 "지금 살 금액"과 "대기 금액"으로 나눈다(매수 행만)
    buy_symbols = sorted({a["symbol"] for r in rows if r.get("action") == "buy" for a in r["allocations"]})
    signals = strategy_signals_for(session, store, {r["심볼"] for r in held}, buy_symbols) if buy_symbols else {}
    plan["scaling"] = apply_entry_scaling(rows, signals)
    plan["signals"] = [{"symbol": s, **signals[s]} for s in buy_symbols]


def execute_agent_tool(req: Request, name: str, args: dict | None = None) -> dict:
    """허용된 도구의 기존 계산을 실행하고 Agent 공개 형식으로 축약한다."""
    if name == "get_portfolio_overview":
        payload = get_holdings_view(req)
        attach_trade_quantities(req.session, req.store, payload)
    elif name == "get_weekly_risk_report":
        payload = get_weekly_report(req)
    elif name == "get_sector_exposure":
        payload = get_sector_exposure(req)
    elif name == "get_weekly_news":
        payload = weekly_news_view(req, args if isinstance(args, dict) else None)
    elif name == "get_macro_drivers":
        payload = macro_drivers_view(req, args if isinstance(args, dict) else None)
    elif name == "get_outlook":
        payload = outlook_view(req, args if isinstance(args, dict) else None)
    elif name == "get_etf_breakdown":
        payload = etf_breakdown_view(req, args if isinstance(args, dict) else None)
    elif name == "get_macro_history":
        payload = macro_history_view(req, args if isinstance(args, dict) else None)
    elif name == "get_macro_cases":
        payload = macro_cases_view(req, args if isinstance(args, dict) else None)
    elif name == "get_asset_drivers":
        payload = asset_drivers_view(req, args if isinstance(args, dict) else None)
    elif name == "get_ma_screen":
        payload = ma_screen_view(req, args if isinstance(args, dict) else None)
    elif name == "get_sector_trends":
        payload = trend_view(req, args if isinstance(args, dict) else None)
    elif name == "get_valuation":
        payload = valuation_view(req, args if isinstance(args, dict) else None)
    elif name == "get_factor_response":
        payload = factor_response_view(req, args if isinstance(args, dict) else None)
    elif name == "get_drawdown_analysis":
        payload = drawdown_view(req, args if isinstance(args, dict) else None)
    else:
        payload = get_tax(req)
    return format_tool_result(name, payload)


# ---------------------------------------------------------------------------
# 규칙 기반 모드의 의도 학습: 기본 예시 문장 + 사용자가 버튼으로 가르친 문장(사용자별 파일, 이 컴퓨터에만 저장)
# ---------------------------------------------------------------------------
INTENTS_FILE = "agent_intents.json"
MAX_LEARNED = 300
MAX_LEARNED_QUESTION = 200
_matcher_cache: dict[tuple, IntentMatcher] = {}
_matcher_lock = threading.Lock()


def load_learned_intents(store: UserStore) -> list[dict]:
    data = store.read_json(INTENTS_FILE)
    rows = data.get("examples", []) if isinstance(data, dict) else []
    return [{"question": r["question"], "intent": r["intent"], "auto": bool(r.get("auto"))} for r in rows
            if isinstance(r, dict) and isinstance(r.get("question"), str) and r.get("intent") in TEACHABLE_INTENTS]


def save_learned_intent(store: UserStore, question: str, intent: str, auto: bool = False) -> int:
    """같은 문장(정규화 기준)이 있으면 의도를 바꾸고, 없으면 더한다. 최대 300개(오래된 것부터 버림). auto=True는 다시 물어본 것에서 자동으로 연결한 항목. 저장한 뒤 개수."""
    key = normalize_question(question)
    rows = [r for r in load_learned_intents(store) if normalize_question(r["question"]) != key]
    rows.append({"question": question[:MAX_LEARNED_QUESTION], "intent": intent, "auto": auto})
    rows = rows[-MAX_LEARNED:]
    store.write_json(INTENTS_FILE, {"examples": rows})
    return len(rows)


def forget_learned_intent(store: UserStore, question: str) -> bool:
    """가르친 문장 하나를 지운다(자동 연결 취소용). 지웠는지 여부."""
    key = normalize_question(question)
    rows = load_learned_intents(store)
    kept = [r for r in rows if normalize_question(r["question"]) != key]
    if len(kept) == len(rows):
        return False
    store.write_json(INTENTS_FILE, {"examples": kept})
    return True


# 놓친 질문 기록(사용자가 켠 경우만): 처리하지 못했거나 문장 유사도로 짐작한 질문을 이 컴퓨터에만 모아 두었다가, 나중에 예시 문장을 한 번에 보강하는 데 쓴다.
MISSES_FILE = "agent_misses.json"
MAX_MISSES = 200
AUTO_LEARN_MIN_SCORE = 0.20     # 못 알아들은 질문을 이어서 물어본 질문과 자동 연결할 때, 이어 물은 의도의 예시와 최소한 이만큼은 비슷해야 한다(잡담 차단)
AUTO_LEARN_WINDOW = 300         # 초. 이 안에 이어서 물어본 경우만


def load_misses(store: UserStore) -> dict:
    data = store.read_json(MISSES_FILE)
    rows = data.get("items", []) if isinstance(data, dict) else []
    return {"enabled": bool(isinstance(data, dict) and data.get("enabled")),
            "items": [r for r in rows if isinstance(r, dict) and isinstance(r.get("question"), str)][-MAX_MISSES:]}


def save_misses(store: UserStore, enabled: bool, items: list[dict]) -> None:
    store.write_json(MISSES_FILE, {"enabled": enabled, "items": items[-MAX_MISSES:]})


def record_miss(store: UserStore, question: str, kind: str, guess: str | None) -> None:
    """기록이 켜져 있을 때만 남긴다. 같은 문장은 한 번만(최신으로 갱신)."""
    state = load_misses(store)
    if not state["enabled"]:
        return
    key = normalize_question(question)
    items = [r for r in state["items"] if normalize_question(r["question"]) != key]
    items.append({"question": question[:MAX_LEARNED_QUESTION], "kind": kind, "guess": guess, "at": time.strftime("%Y-%m-%d %H:%M")})
    save_misses(store, True, items)


def intent_matcher_for(store: UserStore) -> tuple[IntentMatcher, dict[str, str]]:
    """기본 예시 + 이 사용자가 가르친 문장으로 만든 분류기와, 가르친 문장의 정확 일치표({정규화한 문장: 의도}). 같은 학습 내용이면 재사용한다."""
    learned = load_learned_intents(store)
    key = tuple((r["question"], r["intent"]) for r in learned)
    with _matcher_lock:
        matcher = _matcher_cache.get(key)
        if matcher is None:
            examples = {k: list(v) for k, v in DEFAULT_EXAMPLES.items()}
            for r in learned:
                examples.setdefault(r["intent"], []).append(r["question"])
            matcher = IntentMatcher(examples)
            if len(_matcher_cache) >= 16:
                _matcher_cache.clear()
            _matcher_cache[key] = matcher
    taught = {normalize_question(r["question"]): r["intent"] for r in learned if len(normalize_question(r["question"])) >= MIN_CONTENT}
    return matcher, taught


def learn_from_followup(store: UserStore, matcher: IntentMatcher, earlier: str, followup: str, plan: dict) -> dict | None:
    """못 알아들었던(또는 짐작만 했던) 질문 뒤에 키워드로 처리된 질문이 이어지면, 앞 질문을 뒤 질문의 다른 표현으로 연결한다.
    잡담이 엉뚱한 의도에 붙지 않게 앞 질문이 그 의도의 예시와 어느 정도 비슷할 때만(AUTO_LEARN_MIN_SCORE), 같은 질문을 되풀이한 경우와
    종목 종합·여러 의도가 섞인 경우는 제외한다. 연결했으면 화면에 알릴 정보(취소 버튼용 문장 포함), 아니면 None."""
    intent = plan_intent_name(plan)
    if intent is None or intent not in TEACHABLE_INTENTS:
        return None
    if normalize_question(earlier) == normalize_question(followup) or len(normalize_question(earlier)) < MIN_CONTENT:
        return None
    if dict(matcher.scores(earlier)).get(intent, 0.0) < AUTO_LEARN_MIN_SCORE:
        return None
    save_learned_intent(store, earlier, intent, auto=True)
    return {"question": earlier[:MAX_LEARNED_QUESTION], "intent": intent, "label": INTENT_LABELS[intent]}


@route("POST", "/api/agent/teach")
def post_agent_teach(req: Request):
    """"이 질문은 이걸 보여달라는 거였어"를 가르친다(이 사용자의 파일에만 저장). clear=true면 가르친 문장을 모두 지운다."""
    if req.body.get("clear"):
        req.store.write_json(INTENTS_FILE, {"examples": []})
        return {"ok": True, "count": 0}
    if req.body.get("forget"):   # 자동으로 연결된 문장 취소
        return {"ok": True, "forgotten": forget_learned_intent(req.store, str(req.body.get("forget"))[:MAX_LEARNED_QUESTION]),
                "count": len(load_learned_intents(req.store))}
    question = str(req.body.get("question") or "").strip()
    intent = str(req.body.get("intent") or "")
    if intent not in TEACHABLE_INTENTS:
        raise ApiError(400, "알 수 없는 질문 종류입니다.")
    if len(question) > MAX_LEARNED_QUESTION or len(normalize_question(question)) < MIN_CONTENT:
        raise ApiError(400, "가르칠 문장이 너무 짧거나 깁니다(종목 코드·영문을 뺀 한글이 4글자 이상, 200자 이하).")
    return {"ok": True, "count": save_learned_intent(req.store, question, intent)}


@route("GET", "/api/agent/teach")
def get_agent_teach(req: Request):
    rows = load_learned_intents(req.store)
    return {"count": len(rows), "examples": rows[-50:]}


@route("GET", "/api/agent/misses")
def get_agent_misses(req: Request):
    state = load_misses(req.store)
    return {"enabled": state["enabled"], "count": len(state["items"]), "items": state["items"][-100:]}


@route("POST", "/api/agent/misses")
def post_agent_misses(req: Request):
    """놓친 질문 기록 켜기/끄기(enable)와 비우기(clear). 켜 두지 않으면 아무것도 기록하지 않는다."""
    state = load_misses(req.store)
    enabled = bool(req.body["enable"]) if "enable" in req.body else state["enabled"]
    items = [] if req.body.get("clear") else state["items"]
    save_misses(req.store, enabled, items)
    return {"ok": True, "enabled": enabled, "count": len(items)}


@route("POST", "/api/agent/query", max_body=65536)  # 대화 문맥(최근 8개 × 2,000자, 한글은 글자당 3바이트)이 기본 8KB를 넘는다
def post_agent_query(req: Request):
    """API 키가 있으면 LLM 도구 호출, 없으면 규칙 기반 대체 경로로 질문을 처리한다."""
    question = str(req.body.get("question") or "").strip()
    api_key = req.session.get("anthropic_api_key")
    if api_key:
        try:
            return run_claude_agent(
                api_key,
                req.session.get("agent_model", DEFAULT_AGENT_MODEL),
                question,
                normalize_history(req.body.get("history")),
                lambda name, args=None: execute_agent_tool(req, name, args),
            )
        except ValueError as e:
            raise ApiError(400, str(e))
        except AgentApiError as e:
            raise ApiError(502, str(e))
    matcher, taught = intent_matcher_for(req.store)
    via, understood = None, None
    if taught.get(normalize_question(question)):    # 사용자가 가르친 문장은 키워드보다 먼저(가르친 대로 처리)
        via, plan = "taught", plan_for_intent(taught[normalize_question(question)])
    else:
        try:
            plan = plan_agent_query(question)
        except ValueError as e:
            raise ApiError(400, str(e))
    pending = req.session.pop("intent_pending", None)    # 바로 앞에서 처리 못 했거나 짐작만 했던 질문(한 번만 쓰고 지운다)
    learned_now = None
    if via is None and plan["supported"] and pending and time.time() - pending["ts"] <= AUTO_LEARN_WINDOW:
        learned_now = learn_from_followup(req.store, matcher, pending["question"], question, plan)
    info = focus_info(req, question) if (not plan["supported"] or FOCUS_TOOLS & set(plan["tools"])) else {"found": False, "names": [], "unknown": []}
    if not plan["supported"] and info["found"]:   # 키워드는 없지만 내 종목·카테고리를 가리키면 그 종목의 움직임·요인·보도·앞으로의 뷰를 한꺼번에 보여 준다
        plan = plan_for_holding()
    elif not plan["supported"]:                    # 그래도 못 고르면 문장이 비슷한 예시의 의도로(확신이 있을 때만)
        guess = matcher.classify(question)
        if guess and guess["intent"] != "holding":
            via, plan, understood = "similar", plan_for_intent(guess["intent"]), guess
    results, executions = {}, []
    definitions = {item["name"]: item for item in TOOL_DEFINITIONS}

    def run_tool(name, args=None, reason=None):
        output = execute_agent_tool(req, name, args)
        results[name] = output["result"]
        executions.append({"tool": name, "description": definitions[name]["description"], **({"reason": reason} if reason else {})})
    for name in plan["tools"]:
        run_tool(name, (parse_screen_query(question) if name == "get_ma_screen" else parse_trend_query(question) if name == "get_sector_trends"
                        else parse_response_query(question) if name == "get_factor_response"
                        else {"focus": question, "case": question} if name == "get_macro_cases" else {"focus": question}) if (name in FOCUS_TOOLS or name in ("get_ma_screen", "get_sector_trends", "get_factor_response")) else None)  # 규칙 기반 모드는 질문 문장에서 종목·사례·요인을 찾는다
    extra_failed = []
    if plan["supported"]:       # 결과를 보고 덧붙여 볼 도구를 고른다(에이전트 루프, 최대 MAX_EXTRA_STEPS번)
        for step in [s for s in follow_ups(results) if s["tool"] not in results]:
            try:
                run_tool(step["tool"], step["args"], step["reason"])
            except (ApiError, ValueError):
                extra_failed.append(step["reason"])
    answer = compose_agent_answer(results)
    answer["bullets"].extend(observations(results))
    if extra_failed:
        answer["notes"].append("추가로 확인하려던 것을 가져오지 못했습니다: " + " / ".join(extra_failed))
    if info["unknown"]:
        names = ", ".join(info["unknown"])
        if not plan["supported"]:
            answer["bullets"].insert(0, f"{names}은(는) 보유·관심종목에 없어 볼 수 없습니다. 관심종목에 추가하면 같은 방식으로 볼 수 있습니다.")
        else:
            answer["notes"].append(f"{names}은(는) 보유·관심종목에 없어 " + ("볼 수 없습니다" if info["found"] else "전체를 보여줍니다")
                                   + ". 관심종목에 추가하면 같은 방식으로 볼 수 있습니다.")
    if not plan["supported"]:
        answer["suggestions"] = suggest_questions(info["names"])
    if via == "taught":
        answer["notes"].append("가르쳐 주신 대로 처리했습니다.")
    if learned_now:
        answer["learned"] = learned_now
    if via == "similar" or not plan["supported"]:   # 확신이 없는 경우(비슷한 문장으로 짐작했거나 못 알아들음): 바로잡을 수 있게 고르는 버튼을 준다
        req.session["intent_pending"] = {"question": question[:MAX_LEARNED_QUESTION], "ts": time.time()}
        record_miss(req.store, question, "guessed" if via == "similar" else "unplaced", understood["intent"] if understood else None)
        answer["teach"] = {"question": question[:MAX_LEARNED_QUESTION],
                           "understood": {"intent": understood["intent"], "label": INTENT_LABELS[understood["intent"]], "score": round(understood["score"], 2)} if understood else None,
                           "choices": [{"intent": k, "label": INTENT_LABELS[k]} for k in TEACHABLE_INTENTS]}
    return {
        "mode": "rule_based_preview",
        "question": question,
        "plan": plan,
        "executions": executions,
        "answer": answer,
    }


# ---------------------------------------------------------------------------
# HTTP 처리
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "TossRedo/0.2"

    def log_message(self, fmt, *args):  # 기본 stderr 로그를 조용히 (앱키/시크릿 노출 방지)
        pass

    # ---- 응답 ----
    def _send(self, status: int, body: bytes, content_type: str, set_cookie: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict, set_cookie: str | None = None):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", set_cookie)

    # ---- 보안 체크 (real/web_server.py와 동일한 패턴) ----
    def _host_ok(self) -> bool:
        return self.headers.get("Host") in ALLOWED_HOSTS

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def _session_id(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                return value
        return None

    def _session(self) -> dict | None:
        sid = self._session_id()
        if not sid:
            return None
        with _sessions_lock:
            _prune_expired()
            session = _sessions.get(sid)
            if session:
                session["last_seen"] = time.time()
            return session

    # ---- 정적 파일 ----
    def _serve_file(self, path: str) -> bool:
        if path in ("/", "/dashboard.html"):
            self._send(200, DASHBOARD_HTML.read_bytes(), "text/html; charset=utf-8")
            return True
        if path.startswith("/static/"):
            target = (STATIC_DIR / path[len("/static/"):]).resolve()
            if STATIC_DIR.resolve() in target.parents and target.suffix in STATIC_TYPES and target.is_file():
                self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix])
            else:
                self._send_json(404, {"error": "not found"})
            return True
        return False

    # ---- API ----
    def do_GET(self):
        if not self._host_ok():
            return self._send_json(403, {"error": "forbidden"})
        path = urlsplit(self.path).path
        if not self._serve_file(path):
            self._dispatch("GET", path, b"")

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send_json(403, {"error": "forbidden"})
        path = urlsplit(self.path).path
        if path == "/api/logout":
            return self._logout()
        r = ROUTES.get(("POST", path))
        length = int(self.headers.get("Content-Length", 0))
        if length > (r.max_body if r else DEFAULT_MAX_BODY):
            return self._send_json(413, {"error": "요청이 너무 큽니다."})
        self._dispatch("POST", path, self.rfile.read(length) if length else b"")

    def _logout(self):
        sid = self._session_id()
        if sid:
            with _sessions_lock:
                _sessions.pop(sid, None)
        self._send_json(200, {"ok": True}, set_cookie=f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def _dispatch(self, method: str, path: str, raw: bytes):
        r = ROUTES.get((method, path))
        if not r:
            return self._send_json(404, {"error": "not found"})
        session = self._session()
        if r.auth and not session:
            return self._send_json(401, {"error": "로그인이 필요합니다."})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send_json(400, {"error": "잘못된 요청입니다."})

        try:
            result = r.fn(Request(session, parse_qs(urlsplit(self.path).query), body))
        except ApiError as e:
            return self._send_json(e.status, {"error": e.message})
        except requests.HTTPError as e:
            status, message = toss_http_error(e)
            detail = e.response.text[:500] if e.response is not None else ""
            print(f"[{path}] 토스 API 오류 {e.response.status_code if e.response is not None else '?'}: {detail}", file=sys.stderr)
            return self._send_json(status, {"error": message})
        except requests.RequestException as e:
            print(f"[{path}] 네트워크 오류: {e}", file=sys.stderr)
            return self._send_json(502, {"error": "토스 API에 연결할 수 없습니다."})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._send_json(500, {"error": "서버 내부 오류가 발생했습니다."})

        if isinstance(result, tuple):
            self._send_json(*result)
        else:
            self._send_json(200, result)


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
