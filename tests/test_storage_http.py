"""사용자별 저장소 + HTTP 레벨 테스트.

실제 서버(Handler)를 임시 포트로 띄우고, 저장 위치는 임시 폴더로, 토스 API 호출은 가짜로 바꾼다.
"""
from __future__ import annotations

import http.client
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import server

HOST_HEADER = "127.0.0.1:8767"  # ALLOWED_HOSTS 검사를 통과하는 값 (실제 포트와 무관)

# 앱키 → (토큰, 계좌번호). 같은 계좌번호를 가진 앱키 두 개 = 앱키를 재발급한 같은 사용자.
FAKE_USERS = {
    "key-a": ("tok-a", "11100000001"),
    "key-a2": ("tok-a2", "11100000001"),
    "key-b": ("tok-b", "22200000002"),
}


class TempDataDirMixin:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        data = self.base / "data"
        for name, value in {
            "BASE_DIR": self.base, "DATA_DIR": data, "USERS_DIR": data / "users",
            "SERVER_SECRET_FILE": data / "server_secret", "LEGACY_MIGRATED_MARKER": data / "legacy_migrated",
        }.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)


class UserIdTest(TempDataDirMixin, unittest.TestCase):
    def test_stable_and_distinct(self):
        a1 = server.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        a2 = server.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        b = server.user_id_for_accounts([{"accountNo": "222", "accountSeq": 1}])
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)
        self.assertRegex(a1, server.USER_ID_PATTERN)
        self.assertNotIn("111", a1)

    def test_uses_smallest_account_seq(self):
        both = server.user_id_for_accounts([{"accountNo": "999", "accountSeq": 2}, {"accountNo": "111", "accountSeq": 1}])
        self.assertEqual(both, server.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}]))

    def test_no_account_raises(self):
        with self.assertRaises(ValueError):
            server.user_id_for_accounts([])
        with self.assertRaises(ValueError):
            server.user_id_for_accounts([{"accountNo": "", "accountSeq": 1}])

    def test_secret_file_is_private_and_reused(self):
        server.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        mode = stat.S_IMODE(os.stat(server.SERVER_SECRET_FILE).st_mode)
        self.assertEqual(mode, 0o600)
        before = server.SERVER_SECRET_FILE.read_text()
        server.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        self.assertEqual(server.SERVER_SECRET_FILE.read_text(), before)

    def test_store_for_rejects_bad_ids(self):
        for bad in ["../x", "ABC", "a" * 23, ""]:
            with self.assertRaises(ValueError):
                server.store_for({"user_id": bad})


class StoreRoundTripTest(TempDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = server.UserStore(server.USERS_DIR / ("a" * 24))

    def test_missing_files_give_defaults(self):
        self.assertEqual(server.load_watchlist(self.store), [])
        self.assertEqual(server.load_cash_symbols(self.store), [])
        self.assertEqual(server.load_ma_rules(self.store), [])
        self.assertEqual(server.load_strategy_state(self.store), {})
        self.assertEqual(server.load_rebalance_config(self.store)["default_rest_target_pct"], 5.0)
        self.assertEqual(server.load_weekly_report_config(self.store), server.default_weekly_report_config())
        self.assertIn("templates", server.load_strategy_templates(self.store))

    def test_round_trips_create_user_dir(self):
        server.save_watchlist(self.store, ["AAPL"])
        server.save_cash_symbols(self.store, ["SGOV"])
        server.save_rebalance_config(self.store, [{"label": "x", "symbols": ["A"], "target_pct": 10.0}], 3.0, 40.0)
        server.save_strategy_state(self.store, {"A": {"broken_step_ids": ["s1"], "recovered": False, "multiplier_triggered": False}})
        self.assertEqual(server.load_watchlist(self.store), ["AAPL"])
        self.assertEqual(server.load_cash_symbols(self.store), ["SGOV"])
        self.assertEqual(server.load_rebalance_config(self.store)["krw_target_pct"], 40.0)
        self.assertEqual(server.load_strategy_state(self.store)["A"]["broken_step_ids"], ["s1"])
        self.assertFalse(list(self.store.root.glob("*.tmp")), "임시 파일이 남으면 안 됨")

    def test_corrupted_file_falls_back(self):
        self.store.root.mkdir(parents=True)
        self.store.path(server.WATCHLIST_FILE).write_text("{깨짐", encoding="utf-8")
        self.store.path(server.WEEKLY_REPORT_FILE).write_text('{"ma_periods": "x"}', encoding="utf-8")
        self.assertEqual(server.load_watchlist(self.store), [])
        self.assertEqual(server.load_weekly_report_config(self.store), server.default_weekly_report_config())

    def test_default_strategy_templates_not_shared_between_loads(self):
        t1 = server.load_strategy_templates(self.store)
        t1["assignments"]["AAPL"] = "ma-default"
        self.assertEqual(server.load_strategy_templates(self.store)["assignments"], {})


class MigrationTest(TempDataDirMixin, unittest.TestCase):
    def test_moves_legacy_files_once_to_first_user(self):
        (self.base / "watchlist.json").write_text(json.dumps({"symbols": ["TLT"]}), encoding="utf-8")
        first = server.UserStore(server.USERS_DIR / ("a" * 24))
        second = server.UserStore(server.USERS_DIR / ("b" * 24))
        self.assertEqual(server.migrate_legacy_files(first), ["watchlist.json"])
        self.assertFalse((self.base / "watchlist.json").exists())
        self.assertEqual(server.load_watchlist(first), ["TLT"])
        self.assertEqual(server.migrate_legacy_files(second), [])
        self.assertEqual(server.load_watchlist(second), [])

    def test_does_not_overwrite_existing_user_file(self):
        (self.base / "watchlist.json").write_text(json.dumps({"symbols": ["OLD"]}), encoding="utf-8")
        store = server.UserStore(server.USERS_DIR / ("a" * 24))
        server.save_watchlist(store, ["NEW"])
        self.assertEqual(server.migrate_legacy_files(store), [])
        self.assertEqual(server.load_watchlist(store), ["NEW"])


class HttpTest(TempDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        server._sessions.clear()
        server._weekly_report_cache.clear()
        tokens = {tok: acct for tok, acct in FAKE_USERS.values()}

        def fake_token(app_key, app_secret):
            if app_key not in FAKE_USERS:
                resp = mock.Mock(status_code=401)
                raise server.requests.HTTPError(response=resp)
            return FAKE_USERS[app_key][0]

        patches = {
            "get_access_token": mock.Mock(side_effect=fake_token),
            "get_accounts": mock.Mock(side_effect=lambda token: [{"accountNo": tokens[token], "accountSeq": 1}]),
            "get_stock_info": mock.Mock(side_effect=lambda token, syms: {s: {"symbol": s} for s in syms}),
            "fetch_watchlist_rows": mock.Mock(side_effect=lambda token, syms: [{"symbol": s} for s in syms]),
            "validate_weekly_report_symbols": mock.Mock(return_value=[]),
        }
        for name, m in patches.items():
            p = mock.patch.object(server, name, m)
            p.start()
            self.addCleanup(p.stop)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def request(self, method, path, body=None, cookie=None, host=HOST_HEADER, origin=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        set_cookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, payload, set_cookie

    def login(self, key):
        status, payload, set_cookie = self.request("POST", "/api/login", {"app_key": key, "app_secret": "s"})
        self.assertEqual(status, 200, payload)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        return set_cookie.split(";")[0]

    def test_login_failure(self):
        status, payload, _ = self.request("POST", "/api/login", {"app_key": "nope", "app_secret": "s"})
        self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/api/login", {"app_key": "", "app_secret": ""})
        self.assertEqual(status, 400)

    def test_requires_login(self):
        for path in ["/api/holdings", "/api/watchlist", "/api/ma-rules", "/api/strategy", "/api/weekly-report",
                     "/api/weekly-report-config", "/api/cash-symbols", "/api/rebalance-config"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 401)
        self.assertEqual(self.request("POST", "/api/watchlist", {"symbols": []})[0], 401)
        self.assertFalse(self.request("GET", "/api/session")[1]["logged_in"])

    def test_host_and_origin_checks(self):
        self.assertEqual(self.request("GET", "/api/session", host="evil.example")[0], 403)
        cookie = self.login("key-a")
        status, _, _ = self.request("POST", "/api/watchlist", {"symbols": ["AAPL"]}, cookie=cookie, origin="http://evil.example")
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/watchlist", {"symbols": ["AAPL"]}, cookie=cookie, origin="http://127.0.0.1:8767")
        self.assertEqual(status, 200)

    def test_body_size_limit(self):
        cookie = self.login("key-a")
        big = json.dumps({"symbols": ["A"] * 3000}).encode()
        self.assertEqual(self.request("POST", "/api/watchlist", cookie=cookie, raw=big)[0], 413)

    def test_users_are_isolated(self):
        a = self.login("key-a")
        b = self.login("key-b")
        self.assertEqual(self.request("POST", "/api/watchlist", {"symbols": ["AAPL"]}, cookie=a)[0], 200)
        self.assertEqual(self.request("GET", "/api/watchlist", cookie=b)[1]["items"], [])
        self.assertEqual(self.request("POST", "/api/watchlist", {"symbols": ["MSFT"]}, cookie=b)[0], 200)
        self.assertEqual([i["symbol"] for i in self.request("GET", "/api/watchlist", cookie=a)[1]["items"]], ["AAPL"])
        self.assertEqual([i["symbol"] for i in self.request("GET", "/api/watchlist", cookie=b)[1]["items"]], ["MSFT"])
        user_dirs = [p.name for p in server.USERS_DIR.iterdir()]
        self.assertEqual(len(user_dirs), 2)
        for name in user_dirs:
            self.assertNotIn("11100000001", name)
            self.assertNotIn("22200000002", name)

    def test_reissued_app_key_is_same_user(self):
        a = self.login("key-a")
        self.request("POST", "/api/watchlist", {"symbols": ["AAPL"]}, cookie=a)
        a2 = self.login("key-a2")
        self.assertEqual([i["symbol"] for i in self.request("GET", "/api/watchlist", cookie=a2)[1]["items"]], ["AAPL"])

    def test_legacy_files_go_to_first_user_only(self):
        (self.base / "watchlist.json").write_text(json.dumps({"symbols": ["TLT"]}), encoding="utf-8")
        a = self.login("key-a")
        b = self.login("key-b")
        self.assertEqual([i["symbol"] for i in self.request("GET", "/api/watchlist", cookie=a)[1]["items"]], ["TLT"])
        self.assertEqual(self.request("GET", "/api/watchlist", cookie=b)[1]["items"], [])

    def test_weekly_report_config_per_user_and_validation(self):
        a = self.login("key-a")
        b = self.login("key-b")
        status, payload, _ = self.request("GET", "/api/weekly-report-config", cookie=a)
        self.assertEqual(status, 200)
        cfg = payload["config"]
        cfg["ma_periods"] = [60, 120]
        self.assertEqual(self.request("POST", "/api/weekly-report-config", {"config": cfg}, cookie=a)[0], 200)
        self.assertEqual(self.request("GET", "/api/weekly-report-config", cookie=a)[1]["config"]["ma_periods"], [60, 120])
        self.assertEqual(self.request("GET", "/api/weekly-report-config", cookie=b)[1]["config"]["ma_periods"], [60, 120, 200, 240])
        cfg["ma_periods"] = [60, 60]
        status, payload, _ = self.request("POST", "/api/weekly-report-config", {"config": cfg}, cookie=a)
        self.assertEqual(status, 400)
        self.assertIn("중복", payload["error"])

    def test_weekly_report_config_accepts_larger_body(self):
        a = self.login("key-a")
        cfg = self.request("GET", "/api/weekly-report-config", cookie=a)[1]["config"]
        cfg["groups"][0]["items"] = [{"symbol": f"S{i}", "label": "가" * 30, "kind": "price"} for i in range(40)]
        self.assertGreater(len(json.dumps({"config": cfg}).encode()), 8192)
        self.assertEqual(self.request("POST", "/api/weekly-report-config", {"config": cfg}, cookie=a)[0], 200)

    def test_logout(self):
        a = self.login("key-a")
        self.assertTrue(self.request("GET", "/api/session", cookie=a)[1]["logged_in"])
        self.request("POST", "/api/logout", {}, cookie=a)
        self.assertFalse(self.request("GET", "/api/session", cookie=a)[1]["logged_in"])


if __name__ == "__main__":
    unittest.main()
