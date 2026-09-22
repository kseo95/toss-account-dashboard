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

# 세션 저장소: 메모리에만 유지, 디스크 기록 없음.
# { session_id: {"app_key", "app_secret", "token", "issued_at", "last_seen"} }
_sessions: dict[str, dict] = {}
_lock = threading.Lock()


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


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_accounts(token: str) -> list[dict]:
    return api_get("/api/v1/accounts", token).get("result", [])


def _prune_expired() -> None:
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["last_seen"] > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]


def _get_session(handler: "Handler") -> dict | None:
    cookie = handler.headers.get("Cookie", "")
    sid = None
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(f"{SESSION_COOKIE}="):
            sid = part[len(SESSION_COOKIE) + 1 :]
            break
    if not sid:
        return None
    with _lock:
        _prune_expired()
        session = _sessions.get(sid)
        if session:
            session["last_seen"] = time.time()
        return session


def _account_with_valid_token(session: dict) -> str:
    """세션의 토큰으로 계좌 조회를 시도하고, 401이면 앱키/시크릿으로 재발급한다."""
    try:
        get_accounts(session["token"])
        return session["token"]
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            new_token = get_access_token(session["app_key"], session["app_secret"])
            session["token"] = new_token
            return new_token
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
                token = _account_with_valid_token(session)
                accounts = get_accounts(token)
                self._send_json(200, {"accounts": accounts})
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

        if path == "/api/logout":
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith(f"{SESSION_COOKIE}="):
                    sid = part[len(SESSION_COOKIE) + 1 :]
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
