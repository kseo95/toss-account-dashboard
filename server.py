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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests

from common import ApiError, normalize_symbols, parallel_map, to_float
from dividends import estimate_dividends
from portfolio import (CASH_SYMBOLS_MAX, build_holdings, load_cash_symbols, load_rebalance_config, save_cash_symbols,
                       save_rebalance_config, snapshot_rows, validate_rebalance_targets)
from storage import UserStore, migrate_legacy_files, store_for, store_for_user, user_id_for_accounts
from tax import (DEFAULT_TAX_SETTINGS, DOMESTIC_CLASSES, TAX_REFERENCE, estimate_taxes, load_tax_settings,
                 normalize_tax_settings, save_tax_settings, sell_tax_rate_by_symbol)
from strategy import (apply_strategy_sizing, compute_strategy_rows, effective_assignments, load_strategy_state,
                      load_strategy_templates, save_strategy_state, save_strategy_templates, validate_strategy_templates)
from toss_api import (fetch_account_snapshot, get_access_token, get_accounts, get_closed_orders, get_stock_info, lookup_stocks,
                      search_stocks)
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
        raise ApiError(401, "로그인 실패: 앱키/시크릿을 확인하세요." if status in (400, 401) else f"토스 API 오류 ({status})")
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
            status = e.response.status_code if e.response is not None else 502
            detail = e.response.text[:500] if e.response is not None else ""
            print(f"[{path}] 토스 API 오류 {status}: {detail}", file=sys.stderr)
            return self._send_json(status, {"error": f"토스 API 오류 ({status})"})
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
