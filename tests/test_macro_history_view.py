"""과거 매크로 충격 도구(서버 경로): 긴 일봉 수신, 금·지수 ETF의 긴 대용 일봉, 종목 지정, 요인 부족·시세 부족 격리, 계좌 정보 비노출."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import macro
import test_macro as tm
import test_storage_http as storage_http

N_LONG, N_SHORT = 900, 600


def build_series():
    series = {}
    for i, (symbol, _l, kind, _u, _d) in enumerate(macro.FACTORS):
        series[symbol] = tm.series_from_changes(tm.noise(N_LONG, 0.05 if kind == "level" else 0.01, 700 + i), kind)
    series["TLT"] = tm.series_from_changes(tm.noise(N_LONG, 0.01, 801))
    series["GLDM"] = tm.series_from_changes(tm.noise(N_SHORT, 0.01, 802))        # 상장이 짧은 금 ETF
    series["GC=F"] = tm.series_from_changes(tm.noise(N_LONG, 0.01, 803))         # 더 긴 금 선물
    series["SHORT"] = tm.series_from_changes(tm.noise(300, 0.01, 804))           # 너무 짧은 시세
    return series


SERIES = build_series()


class MacroHistoryHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

        def history(symbol, force=False, years=None):
            self.calls.append((symbol, years))
            if symbol not in SERIES:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(SERIES[symbol].items())], "divs": [], "name": symbol}
        for name, value in {"lookup_stocks": mock.Mock(side_effect=lambda token, syms: {}),
                            "get_yahoo_daily_history": mock.Mock(side_effect=history)}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self, extra=()):
        def item(symbol, amount):
            return {"symbol": symbol, "name": symbol, "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": amount}}
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [item("GLDM", 123), item("TLT", 456), item("SGOV", 789)] + [item(s, 1) for s in extra]}

    def prepare(self):
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_cash_symbols(store, ["SGOV"])
        server.save_rebalance_config(store, [{"label": "금", "symbols": ["GLDM"], "target_pct": 10.0}, {"label": "채권", "symbols": ["TLT"], "target_pct": 20.0}], 5.0, 50.0)

    def call(self, cookie, args=None, extra=()):
        body = {"name": "get_macro_history"} if args is None else {"name": "get_macro_history", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot(extra)), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_macro_history"})[0], 401)

    def test_items_for_held_stocks_without_cash_stocks_and_long_requests(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.call(cookie)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual(sorted(i["symbol"] for i in result["items"]), ["GLDM", "TLT"])                      # SGOV(현금 취급) 제외
        self.assertTrue(all(i["status"] == "ok" for i in result["items"]))
        self.assertIn(("TLT", server.LONG_PROXY_YEARS), self.calls)                                           # 종목과 요인 모두 긴 기간으로 받는다
        self.assertIn(("^TNX", server.LONG_PROXY_YEARS), self.calls)

    def test_gold_etf_uses_the_longer_gold_futures_series_and_says_so(self):
        cookie = self.login("key-a")
        self.prepare()
        items = {i["symbol"]: i for i in self.call(cookie)[1]["result"]["items"]}
        self.assertEqual(items["GLDM"]["source"], "금 선물 일봉")
        self.assertEqual(items["GLDM"]["history_days"], N_LONG + 1)                                               # 짧은 GLDM이 아니라 긴 GC=F 기준
        self.assertIsNone(items["TLT"]["source"])
        self.assertIn(("GC=F", server.LONG_PROXY_YEARS), self.calls)

    def test_proxy_is_not_used_when_it_is_not_longer_than_the_etf(self):
        cookie = self.login("key-a")
        self.prepare()
        SERIES["GC=F"], backup = tm.series_from_changes(tm.noise(300, 0.01, 805)), SERIES["GC=F"]
        try:
            gldm = next(i for i in self.call(cookie)[1]["result"]["items"] if i["symbol"] == "GLDM")
        finally:
            SERIES["GC=F"] = backup
        self.assertIsNone(gldm["source"])
        self.assertEqual(gldm["history_days"], N_SHORT + 1)

    def test_short_history_is_isolated_as_no_data(self):
        cookie = self.login("key-a")
        self.prepare()
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_watchlist(store, ["SHORT"])
        with mock.patch.object(server, "lookup_stocks", side_effect=lambda token, syms: {"SHORT": {"name": "짧은종목", "market": "NASDAQ", "currency": "USD"}}):
            items = {i["symbol"]: i for i in self.call(cookie)[1]["result"]["items"]}
        self.assertEqual(items["SHORT"]["status"], "no_data")
        self.assertEqual(items["TLT"]["status"], "ok")

    def test_focus_by_category_name(self):
        cookie = self.login("key-a")
        self.prepare()
        result = self.call(cookie, {"focus": "채권 과거에는 어땠어"})[1]["result"]
        self.assertEqual([i["symbol"] for i in result["items"]], ["TLT"])
        self.assertTrue(result["focused"])

    def test_failed_factor_downloads_do_not_break_the_tool(self):
        cookie = self.login("key-a")
        self.prepare()
        saved = {s: SERIES.pop(s) for s in [f[0] for f in macro.FACTORS]}
        try:
            result = self.call(cookie)[1]["result"]
        finally:
            SERIES.update(saved)
        self.assertEqual({i["symbol"]: i["status"] for i in result["items"]}, {"GLDM": "ok", "TLT": "ok"})
        for item in result["items"]:
            self.assertEqual(item["active"], [])                                                           # 요인이 없으니 지금 큰 변동도 없다
            self.assertEqual(item["status"], "ok")                                                         # 이름 붙은 이슈 반응은 요인 없이도 계산한다

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        self.prepare()
        text = repr(self.call(cookie)[1])
        for needle in ("789", "555.0", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)

    def test_rule_based_question_about_the_past_gets_both_tables(self):
        cookie = self.login("key-a")
        self.prepare()
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "채권은 과거 쇼크 때 어땠어?"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_macro_history"])
        titles = [t["title"] for t in payload["answer"]["tables"]]
        self.assertTrue(any(t.startswith("이 종목이 가장 크게 반응했던 거시 이슈") for t in titles) or any(t.startswith("지금 큰 변동 중인 요인") for t in titles), titles)
        self.assertTrue(any("같이 움직인 과거 기록" in l for l in payload["answer"]["limitations"]))


if __name__ == "__main__":
    unittest.main()
