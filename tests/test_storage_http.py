"""사용자별 저장소 + HTTP 레벨 테스트.

실제 서버(Handler)를 임시 포트로 띄우고, 저장 위치는 임시 폴더로, 토스 API 호출은 가짜로 바꾼다.
"""
from __future__ import annotations

import http.client
from datetime import date, datetime
import json
import os
import re
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import portfolio, server, storage, strategy, watch, weekly_report

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
            p = mock.patch.object(storage, name, value)
            p.start()
            self.addCleanup(p.stop)


class UserIdTest(TempDataDirMixin, unittest.TestCase):
    def test_stable_and_distinct(self):
        a1 = storage.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        a2 = storage.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        b = storage.user_id_for_accounts([{"accountNo": "222", "accountSeq": 1}])
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)
        self.assertRegex(a1, storage.USER_ID_PATTERN)
        self.assertNotIn("111", a1)

    def test_uses_smallest_account_seq(self):
        both = storage.user_id_for_accounts([{"accountNo": "999", "accountSeq": 2}, {"accountNo": "111", "accountSeq": 1}])
        self.assertEqual(both, storage.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}]))

    def test_no_account_raises(self):
        with self.assertRaises(ValueError):
            storage.user_id_for_accounts([])
        with self.assertRaises(ValueError):
            storage.user_id_for_accounts([{"accountNo": "", "accountSeq": 1}])

    def test_secret_file_is_private_and_reused(self):
        storage.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        mode = stat.S_IMODE(os.stat(storage.SERVER_SECRET_FILE).st_mode)
        self.assertEqual(mode, 0o600)
        before = storage.SERVER_SECRET_FILE.read_text()
        storage.user_id_for_accounts([{"accountNo": "111", "accountSeq": 1}])
        self.assertEqual(storage.SERVER_SECRET_FILE.read_text(), before)

    def test_store_for_rejects_bad_ids(self):
        for bad in ["../x", "ABC", "a" * 23, ""]:
            with self.assertRaises(ValueError):
                storage.store_for({"user_id": bad})


class StoreRoundTripTest(TempDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = storage.UserStore(storage.USERS_DIR / ("a" * 24))

    def test_missing_files_give_defaults(self):
        self.assertEqual(watch.load_watchlist(self.store), [])
        self.assertEqual(portfolio.load_cash_symbols(self.store), [])
        self.assertEqual(watch.load_ma_rules(self.store), [])
        self.assertEqual(strategy.load_strategy_state(self.store), {})
        self.assertEqual(portfolio.load_rebalance_config(self.store)["default_rest_target_pct"], 5.0)
        self.assertEqual(weekly_report.load_weekly_report_config(self.store), weekly_report.default_weekly_report_config())
        self.assertIn("templates", strategy.load_strategy_templates(self.store))

    def test_round_trips_create_user_dir(self):
        watch.save_watchlist(self.store, ["AAPL"])
        portfolio.save_cash_symbols(self.store, ["SGOV"])
        portfolio.save_rebalance_config(self.store, [{"label": "x", "symbols": ["A"], "target_pct": 10.0}], 3.0, 40.0)
        strategy.save_strategy_state(self.store, {"A": {"broken_step_ids": ["s1"], "recovered": False, "multiplier_triggered": False}})
        self.assertEqual(watch.load_watchlist(self.store), ["AAPL"])
        self.assertEqual(portfolio.load_cash_symbols(self.store), ["SGOV"])
        self.assertEqual(portfolio.load_rebalance_config(self.store)["krw_target_pct"], 40.0)
        self.assertEqual(strategy.load_strategy_state(self.store)["A"]["broken_step_ids"], ["s1"])
        self.assertFalse(list(self.store.root.glob("*.tmp")), "임시 파일이 남으면 안 됨")

    def test_corrupted_file_falls_back(self):
        self.store.root.mkdir(parents=True)
        self.store.path(watch.WATCHLIST_FILE).write_text("{깨짐", encoding="utf-8")
        self.store.path(weekly_report.WEEKLY_REPORT_FILE).write_text('{"ma_periods": "x"}', encoding="utf-8")
        self.assertEqual(watch.load_watchlist(self.store), [])
        self.assertEqual(weekly_report.load_weekly_report_config(self.store), weekly_report.default_weekly_report_config())

    def test_default_strategy_templates_not_shared_between_loads(self):
        t1 = strategy.load_strategy_templates(self.store)
        t1["assignments"]["AAPL"] = "ma-default"
        self.assertEqual(strategy.load_strategy_templates(self.store)["assignments"], {})


class MigrationTest(TempDataDirMixin, unittest.TestCase):
    def test_moves_legacy_files_once_to_first_user(self):
        (self.base / "watchlist.json").write_text(json.dumps({"symbols": ["TLT"]}), encoding="utf-8")
        first = storage.UserStore(storage.USERS_DIR / ("a" * 24))
        second = storage.UserStore(storage.USERS_DIR / ("b" * 24))
        self.assertEqual(storage.migrate_legacy_files(first), ["watchlist.json"])
        self.assertFalse((self.base / "watchlist.json").exists())
        self.assertEqual(watch.load_watchlist(first), ["TLT"])
        self.assertEqual(storage.migrate_legacy_files(second), [])
        self.assertEqual(watch.load_watchlist(second), [])

    def test_does_not_overwrite_existing_user_file(self):
        (self.base / "watchlist.json").write_text(json.dumps({"symbols": ["OLD"]}), encoding="utf-8")
        store = storage.UserStore(storage.USERS_DIR / ("a" * 24))
        watch.save_watchlist(store, ["NEW"])
        self.assertEqual(storage.migrate_legacy_files(store), [])
        self.assertEqual(watch.load_watchlist(store), ["NEW"])


class HttpTest(TempDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        server._sessions.clear()
        weekly_report._weekly_report_cache.clear()
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
        user_dirs = [p.name for p in storage.USERS_DIR.iterdir()]
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

    def raw_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": HOST_HEADER})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type"), body

    def test_serves_dashboard_and_static_files_only(self):
        status, ctype, body = self.raw_get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        for src in re.findall(r'(?:src|href)="(/static/[^"]+)"', body.decode()):
            with self.subTest(src=src):
                status, ctype, _ = self.raw_get(src)
                self.assertEqual(status, 200)
        for bad in ["/static/../server.py", "/static/%2e%2e/server.py", "/static/js/nope.js", "/static/../data/server_secret"]:
            with self.subTest(bad=bad):
                self.assertEqual(self.raw_get(bad)[0], 404)

    def test_bad_json_bodies(self):
        cookie = self.login("key-a")
        self.assertEqual(self.request("POST", "/api/watchlist", cookie=cookie, raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.request("POST", "/api/watchlist", cookie=cookie, raw=b"{bad")[0], 400)
        status, payload, _ = self.request("POST", "/api/cash-symbols", {"symbols": "SGOV"}, cookie=cookie)
        self.assertEqual((status, payload["error"]), (400, "symbols는 배열이어야 합니다."))
        self.assertEqual(self.request("GET", "/api/nope", cookie=cookie)[0], 404)

    def test_account_snapshot_is_shared_and_expires(self):
        cookie = self.login("key-a")
        snap = {"usd_krw": 1000.0, "cash_krw": 100.0, "cash_usd": 0.0, "items": []}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=snap) as fetch, \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            self.assertEqual(self.request("GET", "/api/holdings", cookie=cookie)[1]["totals"]["eval_krw"], 100.0)
            self.request("GET", "/api/holdings", cookie=cookie)
            self.assertEqual(fetch.call_count, 1, "SNAPSHOT_TTL 안에서는 다시 받지 않음")
            session = next(iter(server._sessions.values()))
            session["snapshot"] = (session["snapshot"][0] - server.SNAPSHOT_TTL - 1, snap)
            self.request("GET", "/api/holdings", cookie=cookie)
            self.assertEqual(fetch.call_count, 2)

    def test_tax_page_and_settings(self):
        cookie = self.login("key-a")
        snap = {"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": [
            {"symbol": "AAPL", "name": "Apple", "currency": "USD", "quantity": 1, "marketCountry": "US",
             "marketValue": {"amount": 5000.0}, "profitLoss": {"amountAfterCost": 3000.0, "rateAfterCost": 1.5}}]}
        fx = {"bars": [(date(2026, 1, 2), 1000.0, 1000.0)], "divs": [], "name": "KRW=X"}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=snap), \
                mock.patch.object(server, "get_closed_orders", return_value=[]), \
                mock.patch.object(server, "get_yahoo_daily_history", return_value=fx), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, data, _ = self.request("GET", "/api/tax", cookie=cookie)
            self.assertEqual(data["realized_source"], "auto")
            self.assertEqual(status, 200)
            self.assertAlmostEqual(data["summary"]["gain_tax_if_sell_all_krw"], (3_000_000 - 2_500_000) * 0.22)
            settings = data["settings"]
            settings["realized_overseas_gain_ytd_krw"] = 2_500_000
            settings["use_manual_realized"] = True
            self.assertEqual(self.request("POST", "/api/tax-settings", {"settings": settings}, cookie=cookie)[0], 200)
            data = self.request("GET", "/api/tax", cookie=cookie)[1]
            self.assertAlmostEqual(data["summary"]["gain_tax_if_sell_all_krw"], 3_000_000 * 0.22)
        settings["overseas_gain_rate_pct"] = 500
        status, payload, _ = self.request("POST", "/api/tax-settings", {"settings": settings}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn("해외 양도세율", payload["error"])

    def test_trades_endpoint(self):
        cookie = self.login("key-a")
        orders = [{"orderId": "1", "symbol": "AAPL", "side": "BUY", "currency": "USD", "status": "FILLED",
                   "execution": {"filledQuantity": "1", "averageFilledPrice": "150", "filledAmount": "150",
                                 "commission": "0.1", "tax": None, "filledAt": "2026-03-02T23:30:00+09:00", "settlementDate": "2026-03-04"}}]
        fx = {"bars": [(date(2026, 1, 2), 1400.0, 1400.0)], "divs": [], "name": "KRW=X"}
        with mock.patch.object(server, "get_closed_orders", return_value=orders) as fetch, \
                mock.patch.object(server, "get_yahoo_daily_history", return_value=fx), \
                mock.patch.object(server, "lookup_stocks", return_value={"AAPL": {"name": "Apple"}}):
            status, data, _ = self.request("GET", "/api/trades?year=2026", cookie=cookie)
            self.request("GET", "/api/trades?year=2026", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["fills"][0]["name"], "Apple")
        self.assertEqual(fetch.call_args.args[2], "2000-01-01")  # 선입선출용 전체 기간
        self.assertEqual(fetch.call_count, 1, "ORDERS_TTL 안에서는 다시 받지 않음")
        self.assertEqual(data["realized"]["sells"], [])
        self.assertEqual(self.request("GET", "/api/trades?year=abc", cookie=cookie)[0], 400)
        self.assertEqual(self.request("GET", "/api/trades?year=1999", cookie=cookie)[0], 400)

    def test_dividends_endpoint_and_tax_income(self):
        cookie = self.login("key-a")
        snap = {"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": [
            {"symbol": "AAPL", "name": "Apple", "currency": "USD", "quantity": 4, "marketCountry": "US",
             "marketValue": {"amount": 800.0}, "profitLoss": {"amountAfterCost": 0.0, "rateAfterCost": 0.0}}]}
        year = datetime.now().year

        def yahoo(sym, force=False):
            if sym == "KRW=X":
                return {"bars": [(date(year, 1, 1), 1000.0, 1000.0)], "divs": [], "name": sym}
            return {"bars": [], "divs": [(date(year, 1, 2), 0.25)], "name": sym}

        with mock.patch.object(server, "fetch_account_snapshot", return_value=snap), \
                mock.patch.object(server, "get_closed_orders", return_value=[]), \
                mock.patch.object(server, "get_yahoo_daily_history", side_effect=yahoo), \
                mock.patch.object(server, "lookup_stocks", return_value={}), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, data, _ = self.request("GET", "/api/dividends", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertAlmostEqual(data["total_gross_krw"], 4 * 0.25 * 1000)   # 체결 내역 없음 → 지금 보유 4주로 보정
            tax_data = self.request("GET", "/api/tax", cookie=cookie)[1]
        self.assertEqual(tax_data["income_source"], "auto")
        self.assertAlmostEqual(tax_data["summary"]["financial_income_ytd_krw"], 1000.0)

    def test_logout(self):
        a = self.login("key-a")
        self.assertTrue(self.request("GET", "/api/session", cookie=a)[1]["logged_in"])
        self.request("POST", "/api/logout", {}, cookie=a)
        self.assertFalse(self.request("GET", "/api/session", cookie=a)[1]["logged_in"])


if __name__ == "__main__":
    unittest.main()
