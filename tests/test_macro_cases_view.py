"""거시 사례집 도구(서버 경로): 긴 시세 수신, 지정한 사례·종목 열, FRED 요인 합류, 일부 시세 실패 격리, 규칙 기반 질문, 계좌 비노출."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import fred
import test_macro as tm
import test_storage_http as storage_http

N = 7200


def business_days(n, start=date(2000, 1, 3)):
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


DAYS = business_days(N)


def make(seed, kind="price", base=100.0):
    vals, v = [], base
    for e in tm.noise(N, 0.04 if kind == "level" else 0.01, seed):
        v = v + e if kind == "level" else v * math.exp(e)
        vals.append(max(v, 0.5))
    return dict(zip(DAYS, vals))


SERIES = {"^TNX": make(1, "level", 4.0), "^IRX": make(2, "level", 3.0), "DX-Y.NYB": make(3), "^GSPC": make(4), "^VIX": make(5), "CL=F": make(6),
          "GC=F": make(7), "TLT": make(8)}


class MacroCasesHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls, self.fred_calls = [], []

        def history(symbol, force=False, years=None):
            self.calls.append((symbol, years))
            if symbol not in SERIES:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(SERIES[symbol].items())], "divs": [], "name": symbol}

        def fetch(key, series_id, *a, **k):
            self.fred_calls.append((key, series_id))
            if series_id == "DFF":
                return {d: 2.0 + 0.0005 * i for i, d in enumerate(DAYS)}
            if series_id in ("CPIAUCSL", "UNRATE", "WALCL"):                                  # 월간·주간 지표(합성)
                months = sorted({d.replace(day=1) for d in DAYS})
                return {m: (100.0 * 1.002 ** i if series_id == "CPIAUCSL" else 4.0 + 0.01 * i if series_id == "UNRATE" else 1000.0 * 1.001 ** i)
                        for i, m in enumerate(months)}
            raise fred.FredError("테스트: 받지 못함")
        for name, value in {"lookup_stocks": mock.Mock(side_effect=lambda token, syms: {}),
                            "get_yahoo_daily_history": mock.Mock(side_effect=history),
                            "fetch_fred_series": mock.Mock(side_effect=fetch)}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [{"symbol": "TLT", "name": "TLT", "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 123}}]}

    def tool(self, cookie, args=None):
        body = {"name": "get_macro_cases"} if args is None else {"name": "get_macro_cases", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_macro_cases"})[0], 401)

    def test_named_case_returns_phases_with_numbers_conventions_and_similar_phases(self):
        cookie = self.login("key-a")
        status, payload, _ = self.tool(cookie, {"case": "2008년 금융위기"})
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertTrue(result["named"])
        case = result["cases"][0]
        self.assertEqual((case["id"], len(case["phases"])), ("gfc-2008", 5))
        first = case["phases"][0]
        self.assertTrue(first["story"])
        self.assertTrue({"gold", "tnx", "dxy", "spx"} <= {m["key"] for m in first["moves"]})
        self.assertIn(("^TNX", server.LONG_PROXY_YEARS), self.calls)                                          # 긴 기간으로 받는다
        self.assertEqual(len(result["similar"]), 3)
        self.assertTrue(all(s["shared"] >= 4 for s in result["similar"]))
        self.assertEqual(len(result["available"]), 12)

    def test_without_a_case_only_similar_phases_and_the_case_list_come_back(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {})[1]["result"]
        self.assertFalse(result["named"])
        self.assertEqual(result["cases"], [])
        self.assertEqual(len(result["similar"]), 3)

    def test_focus_adds_the_asset_move_and_what_came_after(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"case": "코로나", "focus": "TLT"})[1]["result"]
        self.assertEqual(result["asset"], "TLT")
        phase = result["cases"][0]["phases"][0]
        self.assertIsNotNone(phase["asset_move_pct"])
        self.assertIsNotNone(phase["asset_after_pct"])
        self.assertIn(("TLT", server.LONG_PROXY_YEARS), self.calls)
        unknown = self.tool(cookie, {"case": "코로나", "focus": "NOSUCH"})[1]["result"]                       # 못 찾은 종목은 열만 빠진다
        self.assertIsNone(unknown["asset"])

    def test_fred_factors_join_only_with_a_key(self):
        cookie = self.login("key-a")
        no_key = self.tool(cookie, {"case": "2008"})[1]["result"]
        self.assertEqual(self.fred_calls, [])
        self.assertFalse(no_key["has_fred"])
        self.request("POST", "/api/agent/config", {"fred_key": "b" * 32}, cookie=cookie)
        with_key = self.tool(cookie, {"case": "2008"})[1]["result"]
        self.assertTrue(with_key["has_fred"])
        self.assertIn("FRED:DFF", {m["key"] for p in with_key["cases"][0]["phases"] for m in p["moves"]})
        self.assertNotIn("b" * 32, repr(with_key))

    def test_failed_reference_downloads_are_isolated(self):
        cookie = self.login("key-a")
        saved = {s: SERIES.pop(s) for s in ("^VIX", "CL=F", "^IRX")}
        try:
            status, payload, _ = self.tool(cookie, {"case": "2008"})
        finally:
            SERIES.update(saved)
        self.assertEqual(status, 200, payload)
        keys = {m["key"] for p in payload["result"]["cases"][0]["phases"] for m in p["moves"]}
        self.assertTrue({"gold", "tnx"} <= keys)
        self.assertFalse(keys & {"vix", "oil", "irx"})

    def test_everything_down_still_returns_a_valid_empty_result(self):
        cookie = self.login("key-a")
        saved = dict(SERIES)
        SERIES.clear()
        try:
            status, payload, _ = self.tool(cookie, {"case": "2008"})
        finally:
            SERIES.update(saved)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["similar"], [])
        self.assertFalse(payload["result"]["gold_available"])

    def test_rule_based_question_gets_the_case_table_and_similar_table(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "2008년 금융위기 때 금은 어땠어?"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_macro_cases"])
        titles = [t["title"] for t in payload["answer"]["tables"]]
        self.assertTrue(titles[0].startswith("사례: 2004~2009 서브프라임·금융위기·QE1"), titles)
        self.assertTrue(any(t.startswith("지금(최근 6개월)과 요인 움직임이 닮은") for t in titles), titles)
        self.assertTrue(any("해석" in b for b in payload["answer"]["bullets"]))
        self.assertTrue(any("FRED 키를 넣으면" in n for n in payload["answer"]["notes"]))

    def test_monthly_fred_indicators_join_the_phase_numbers_only_with_a_key(self):
        cookie = self.login("key-a")
        without = {m["key"] for p in self.tool(cookie, {"case": "2008"})[1]["result"]["cases"][0]["phases"] for m in p["moves"]}
        self.assertFalse(without & {"FRED:CPIYOY", "FRED:UNRATE", "FRED:WALCL"})
        self.request("POST", "/api/agent/config", {"fred_key": "b" * 32}, cookie=cookie)
        result = self.tool(cookie, {"case": "2008"})[1]["result"]
        with_key = {m["key"] for p in result["cases"][0]["phases"] for m in p["moves"]}
        self.assertTrue({"FRED:CPIYOY", "FRED:UNRATE", "FRED:WALCL"} <= with_key)
        self.assertEqual(len(result["similar"]), 3)                                           # 느린 지표가 닮은 국면 계산을 깨지 않는다
        self.assertIn(("XLF", server.LONG_PROXY_YEARS), self.calls)

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        text = repr(self.tool(cookie, {"case": "2008", "focus": "TLT"})[1])
        for needle in ("789", "555.0", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
