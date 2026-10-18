"""FRED 연동: 응답 파싱, 오류 메시지에 키·URL이 새지 않음, 캐시, 설정 경로(키 형식 검사·세션 보관·되돌려 보내지 않음), 요인 분석에 FRED 요인 합류. 네트워크는 쓰지 않는다."""
from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

import requests

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import fred
import macro
import test_macro as tm
import test_storage_http as storage_http

KEY = "a" * 32


def observations(n=300, start=date(2024, 1, 1), step=1):
    from datetime import timedelta
    return {"observations": [{"date": (start + timedelta(days=i)).isoformat(), "value": f"{4 + i * 0.001:.3f}"} for i in range(n)]}


class FakeResp:
    def __init__(self, payload=None, status=200, bad_json=False):
        self.payload, self.status_code, self.bad_json = payload, status, bad_json

    def json(self):
        if self.bad_json:
            raise ValueError("not json")
        return self.payload


class ParseTest(unittest.TestCase):
    def test_missing_values_and_bad_rows_are_skipped(self):
        out = fred.parse_observations({"observations": [{"date": "2008-01-01", "value": "3.94"}, {"date": "2008-01-02", "value": "."},
                                                        {"date": "bad", "value": "1"}, {"value": "2"}, "x", {"date": "2008-01-03", "value": "abc"},
                                                        {"date": "2008-01-04T00:00:00", "value": "4.1"}]})
        self.assertEqual(out, {date(2008, 1, 1): 3.94, date(2008, 1, 4): 4.1})
        self.assertEqual(fred.parse_observations(None), {})
        self.assertEqual(fred.parse_observations({"observations": "x"}), {})

    def test_key_format(self):
        self.assertTrue(fred.valid_key(KEY))
        for bad in ("", "short", "A" * 32, "a" * 31, "a" * 33, "a" * 31 + "!", None):
            self.assertFalse(fred.valid_key(bad), bad)


class FetchTest(unittest.TestCase):
    def setUp(self):
        fred.clear_cache()

    def test_fetch_and_cache_per_series(self):
        calls = []

        def get(url, **kw):
            calls.append(kw["params"]["series_id"])
            return FakeResp(observations())
        first = fred.fetch_series(KEY, "DFF", get)
        first.clear()
        self.assertEqual(len(fred.fetch_series(KEY, "DFF", get)), 300)           # 복사본이라 캐시는 그대로
        fred.fetch_series(KEY, "DFII10", get)
        self.assertEqual(calls, ["DFF", "DFII10"])

    def test_http_errors_become_safe_messages_without_the_key(self):
        cases = {400: "키가 올바르지", 401: "키가 올바르지", 403: "키가 올바르지", 429: "한도", 500: "FRED 오류 (500)"}
        for status, text in cases.items():
            fred.clear_cache()
            with self.assertRaises(fred.FredError) as ctx:
                fred.fetch_series(KEY, "DFF", lambda url, **kw: FakeResp(status=status))
            self.assertIn(text, str(ctx.exception))
            self.assertNotIn(KEY, str(ctx.exception))

    def test_connection_errors_never_leak_the_url_or_key(self):
        def boom(url, **kw):
            raise requests.ConnectionError(f"HTTPSConnectionPool: {url}?api_key={KEY} failed")
        with self.assertRaises(fred.FredError) as ctx:
            fred.fetch_series(KEY, "DFF", boom)
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertNotIn("api_key", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)                      # 원인 예외(URL 포함)를 연결하지 않는다

    def test_bad_json_short_series_and_unknown_id(self):
        with self.assertRaises(fred.FredError):
            fred.fetch_series(KEY, "DFF", lambda url, **kw: FakeResp(bad_json=True))
        with self.assertRaises(fred.FredError):
            fred.fetch_series(KEY, "DFF", lambda url, **kw: FakeResp(observations(n=10)))
        with self.assertRaises(fred.FredError):
            fred.fetch_series(KEY, "GDP", lambda url, **kw: FakeResp(observations()))
        self.assertEqual(set(fred.SERIES_IDS), {f[0].split(":")[1] for f in macro.FRED_FACTORS})   # 두 곳의 목록이 어긋나지 않는다


class SlowSeriesTest(unittest.TestCase):
    def setUp(self):
        fred.clear_cache()

    def test_ids_and_the_two_lists_do_not_overlap(self):
        self.assertIn("DGS2", fred.SERIES_IDS)
        self.assertEqual(set(fred.SLOW_SERIES_IDS), {"CPIAUCSL", "UNRATE", "WALCL"})
        self.assertFalse(set(fred.SERIES_IDS) & set(fred.SLOW_SERIES_IDS))                  # 일간 요인에 월간·주간 지표가 섞이면 날짜가 어긋난다

    def test_slow_series_can_be_fetched_and_cached_separately(self):
        calls = []

        def get(url, **kw):
            calls.append(kw["params"]["series_id"])
            return FakeResp(observations(n=300, step=7))
        self.assertEqual(len(fred.fetch_series(KEY, "CPIAUCSL", get)), 300)
        fred.fetch_series(KEY, "CPIAUCSL", get)
        self.assertEqual(calls, ["CPIAUCSL"])

    def test_year_over_year_from_a_monthly_index(self):
        series = {date(2020 + i // 12, i % 12 + 1, 1): 100.0 * 1.003 ** i for i in range(30)}
        yoy = fred.yoy_series(series)
        self.assertEqual(min(yoy), date(2021, 1, 1))                                       # 12개월 전 값이 없는 첫 해는 뺀다
        self.assertAlmostEqual(yoy[date(2021, 1, 1)], (1.003 ** 12 - 1) * 100)
        self.assertEqual(len(yoy), 30 - 12)
        self.assertEqual(fred.yoy_series({}), {})
        self.assertEqual(fred.yoy_series({date(2020, 2, 29): 100.0, date(2021, 2, 28): 103.0}), {})   # 윤일은 건너뛴다(오류 없이)


class FredHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    N = 320

    def setUp(self):
        super().setUp()
        series = {}
        for i, (symbol, _l, kind, _u, _d) in enumerate(macro.FACTORS):
            series[symbol] = tm.series_from_changes(tm.noise(self.N, 0.05 if kind == "level" else 0.01, 900 + i), kind)
        dff = tm.noise(self.N, 0.03, 950)
        series["FRED:DFF"] = tm.series_from_changes(dff, "level")
        series["TLT"] = tm.series_from_changes([-0.4 * c + e for c, e in zip(dff, tm.noise(self.N, 0.004, 951))])   # TLT는 연방기금금리를 따라 움직인다
        self.series = series

        def history(symbol, force=False, years=None):
            if symbol not in series or symbol.startswith("FRED:"):
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(series[symbol].items())], "divs": [], "name": symbol}
        self.fred_calls = []

        def fetch(key, series_id, *a, **k):
            self.fred_calls.append((key, series_id))
            if series_id != "DFF":
                raise fred.FredError("테스트: 받지 못함")
            return dict(series["FRED:DFF"])
        for name, value in {"lookup_stocks": mock.Mock(side_effect=lambda token, syms: {}),
                            "get_yahoo_daily_history": mock.Mock(side_effect=history),
                            "headlines_for_many": mock.Mock(side_effect=lambda entries, **kw: {e["symbol"]: {"headlines": [], "error": False} for e in entries}),
                            "fetch_fred_series": mock.Mock(side_effect=fetch)}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        return {"usd_krw": 1000.0, "cash_krw": 1.0, "cash_usd": 0.0,
                "items": [{"symbol": "TLT", "name": "TLT", "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 123}}]}

    def macro_tool(self, cookie):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", {"name": "get_macro_drivers"}, cookie=cookie)

    # --- 설정 경로 ---
    def test_config_validates_stores_and_clears_the_key_without_echoing_it(self):
        cookie = self.login("key-a")
        self.assertFalse(self.request("GET", "/api/agent/config", cookie=cookie)[1]["fred_configured"])
        status, payload, _ = self.request("POST", "/api/agent/config", {"fred_key": "너무 짧은 키"}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertNotIn("너무 짧은 키", repr(payload))
        self.assertFalse(self.request("GET", "/api/agent/config", cookie=cookie)[1]["fred_configured"])
        status, payload, _ = self.request("POST", "/api/agent/config", {"fred_key": KEY}, cookie=cookie)
        self.assertEqual((status, payload["fred_configured"]), (200, True))
        self.assertNotIn(KEY, repr(payload))
        self.assertNotIn(KEY, repr(self.request("GET", "/api/agent/config", cookie=cookie)[1]))
        status, payload, _ = self.request("POST", "/api/agent/config", {"clear_fred_key": True}, cookie=cookie)
        self.assertFalse(payload["fred_configured"])

    def test_fred_key_is_per_session_and_independent_of_the_anthropic_key(self):
        cookie_a, cookie_b = self.login("key-a"), self.login("key-b")
        self.request("POST", "/api/agent/config", {"fred_key": KEY}, cookie=cookie_a)
        self.assertTrue(self.request("GET", "/api/agent/config", cookie=cookie_a)[1]["fred_configured"])
        self.assertFalse(self.request("GET", "/api/agent/config", cookie=cookie_b)[1]["fred_configured"])
        self.assertFalse(self.request("GET", "/api/agent/config", cookie=cookie_a)[1]["configured"])           # AI 키와 무관

    def test_fred_key_is_not_written_to_disk(self):
        cookie = self.login("key-a")
        self.request("POST", "/api/agent/config", {"fred_key": KEY}, cookie=cookie)
        import os
        self.macro_tool(cookie)                                                  # 요인 조회까지 거친 뒤에도
        scanned = 0
        for root, _dirs, files in os.walk(self.base):                           # 임시 데이터 폴더 어디에도 키가 없다
            for name in files:
                scanned += 1
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(KEY.encode(), f.read(), name)
        self.assertGreater(scanned, 0)


    # --- 자동 연결(환경변수·키체인) ---
    def config(self, cookie):
        return self.request("GET", "/api/agent/config", cookie=cookie)[1]

    def test_default_key_connects_without_typing_and_is_read_only_once(self):
        server.read_default_key.return_value = (KEY, "keychain")
        cookie = self.login("key-a")
        status = self.config(cookie)
        self.assertEqual((status["fred_configured"], status["fred_source"]), (True, "keychain"))
        self.assertNotIn(KEY, repr(status))
        self.config(cookie)
        self.macro_tool(cookie)
        self.assertEqual(server.read_default_key.call_count, 1)                           # 켜고 처음 한 번만 읽는다
        self.assertTrue(self.fred_calls)
        self.assertTrue(all(key == KEY for key, _ in self.fred_calls))                     # 세션 키 없이도 기본 키로 조회

    def test_session_key_wins_over_the_default_key(self):
        server.read_default_key.return_value = (KEY, "env")
        cookie = self.login("key-a")
        other = "e" * 32
        self.request("POST", "/api/agent/config", {"fred_key": other}, cookie=cookie)
        self.assertEqual(self.config(cookie)["fred_source"], "session")
        self.macro_tool(cookie)
        self.assertTrue(all(key == other for key, _ in self.fred_calls))
        self.request("POST", "/api/agent/config", {"clear_fred_key": True}, cookie=cookie)     # 세션 키를 지우면 기본 키로 돌아간다
        self.assertEqual(self.config(cookie)["fred_source"], "env")

    def test_saving_to_the_keychain_is_opt_in_and_failures_are_reported_without_the_key(self):
        cookie = self.login("key-a")
        self.request("POST", "/api/agent/config", {"fred_key": KEY}, cookie=cookie)
        server.save_fred_secret.assert_not_called()                                        # 체크하지 않으면 저장하지 않는다
        status, payload, _ = self.request("POST", "/api/agent/config", {"fred_key": KEY, "fred_save": True}, cookie=cookie)
        self.assertEqual((status, payload["fred_saved"]), (200, True))
        server.save_fred_secret.assert_called_once_with(KEY)
        self.assertNotIn(KEY, repr(payload))
        server.save_fred_secret.return_value = False
        payload = self.request("POST", "/api/agent/config", {"fred_key": KEY, "fred_save": True}, cookie=cookie)[1]
        self.assertFalse(payload["fred_saved"])
        self.assertIn("키체인에 저장하지 못했습니다", payload["fred_save_error"])
        self.assertTrue(payload["fred_configured"])                                       # 저장은 실패해도 이번 세션에는 연결됨
        self.assertNotIn(KEY, repr(payload))

    def test_invalid_keys_are_never_saved(self):
        cookie = self.login("key-a")
        status, payload, _ = self.request("POST", "/api/agent/config", {"fred_key": "x" * 10, "fred_save": True}, cookie=cookie)
        self.assertEqual(status, 400)
        server.save_fred_secret.assert_not_called()

    def test_forgetting_deletes_the_saved_key_and_refreshes_the_default(self):
        server.read_default_key.return_value = (KEY, "keychain")
        cookie = self.login("key-a")
        self.assertTrue(self.config(cookie)["fred_configured"])
        server.read_default_key.return_value = (None, None)                               # 삭제 뒤에는 키체인에 없다
        payload = self.request("POST", "/api/agent/config", {"fred_forget": True}, cookie=cookie)[1]
        server.delete_fred_secret.assert_called_once_with()
        self.assertTrue(payload["fred_forgotten"])
        self.assertFalse(payload["fred_configured"])
        self.assertIsNone(payload["fred_source"])

    def test_keychain_support_flag_is_reported(self):
        cookie = self.login("key-a")
        self.assertTrue(self.config(cookie)["keychain_supported"])
        server.keychain_supported.return_value = False
        self.assertFalse(self.config(cookie)["keychain_supported"])

    # --- 요인 분석 합류 ---
    def test_without_a_key_fred_is_never_called_and_factors_are_unchanged(self):
        cookie = self.login("key-a")
        status, payload, _ = self.macro_tool(cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.fred_calls, [])
        self.assertEqual({f["label"] for f in payload["result"]["factors"]}, {f[1] for f in macro.FACTORS})

    def test_with_a_key_available_fred_factors_join_and_failed_ones_are_skipped(self):
        cookie = self.login("key-a")
        self.request("POST", "/api/agent/config", {"fred_key": KEY}, cookie=cookie)
        status, payload, _ = self.macro_tool(cookie)
        self.assertEqual(status, 200, payload)
        labels = {f["label"] for f in payload["result"]["factors"]}
        self.assertIn("연방기금금리", labels)                                         # 받은 요인은 이번 주 시장 요인 표에 들어간다
        self.assertNotIn("10년 실질금리", labels)                                      # 못 받은 요인만 빠지고 나머지는 계속한다
        self.assertEqual({sid for key, sid in self.fred_calls}, set(fred.SERIES_IDS))
        self.assertTrue(all(key == KEY for key, _ in self.fred_calls))
        tlt = next(i for i in payload["result"]["items"] if i["symbol"] == "TLT")
        self.assertEqual(tlt["drivers"][0]["label"], "연방기금금리")                    # 새 요인이 설명 요인으로 뽑힌다
        self.assertNotIn(KEY, repr(payload))


if __name__ == "__main__":
    unittest.main()
