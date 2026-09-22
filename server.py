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
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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

# 세션 저장소: 메모리에만 유지, 디스크 기록 없음.
# { session_id: {"app_key", "app_secret", "token", "issued_at", "last_seen"} }
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_cash_symbols_lock = threading.Lock()  # cash_symbols.json 동시 수정 방지


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


def load_rebalance_config() -> dict:
    """리밸런싱 목표 설정을 읽는다. 파일이 없거나 깨졌으면 기본값(카테고리 없음, 전부 나머지 100%)으로 취급한다.
    (지금은 파일을 직접 편집해서 설정 - 웹 편집 UI는 다음 단계)"""
    default = {"targets": [], "rest_pct": 100.0}
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
        return {"targets": clean_targets, "rest_pct": to_float(data.get("rest_pct"), default["rest_pct"])}
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return default


def compute_rebalance(
    all_rows: list[dict], cash_krw_total: float, cash_symbols: set[str], total_eval: float, config: dict
) -> list[dict]:
    """카테고리별 현재%/목표%를 계산한다. 목표에 없는 나머지 종목+예수금은 "나머지"(전부 현금성이면 "현금") 버킷으로."""
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

    rest_rows = [r for r in all_rows if r["심볼"] not in matched_symbols]
    rest_amount = sum(r["평가금액(원)"] for r in rest_rows) + cash_krw_total
    rest_label = REBALANCE_CASH_LABEL if all(r["심볼"] in cash_symbols for r in rest_rows) else REBALANCE_REST_LABEL
    result.append(
        {
            "label": rest_label,
            "current_pct": (rest_amount / total_eval * 100) if total_eval else 0.0,
            "target_pct": config["rest_pct"],
        }
    )
    return result


def compute_currency_split(all_rows: list[dict], cash_krw_amt: float, cash_usd_amt: float, usd_krw: float, total_eval: float) -> dict:
    """전체 자산 중 원화/달러 통화 비중 (예수금 포함)."""
    krw_amount = cash_krw_amt + sum(r["평가금액(원)"] for r in all_rows if r["통화"] == "KRW")
    usd_amount = cash_usd_amt * usd_krw + sum(r["평가금액(원)"] for r in all_rows if r["통화"] == "USD")
    return {
        "krw_pct": (krw_amount / total_eval * 100) if total_eval else 0.0,
        "usd_pct": (usd_amount / total_eval * 100) if total_eval else 0.0,
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
    currency_split = compute_currency_split(rows, cash_krw_amt, cash_usd_amt, usd_krw, total_eval)

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
