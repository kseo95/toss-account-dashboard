"""ETF 구성 종목 분해 도구(서버 경로): 편입 종목 기여 계산, 큰 기여 종목의 요인·보도, 편입 정보 없는 종목 분리, 종목 지정, 계좌 정보 비노출."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import macro
import test_macro as tm
import test_storage_http as storage_http

N = 300
HOLDINGS = [{"symbol": "AAA", "name": "에이 주식회사", "weight": 0.30}, {"symbol": "BBB", "name": "Bee Corp", "weight": 0.20},
            {"symbol": "CCC", "name": "Sea Inc", "weight": 0.10}, {"symbol": "NOPRICE", "name": "No Price", "weight": 0.05}]


def build_series():
    series = {}
    for i, (symbol, _l, kind, _u, _d) in enumerate(macro.FACTORS):
        series[symbol] = tm.series_from_changes(tm.noise(N, 0.05 if kind == "level" else 0.01, 500 + i), kind)
    for i, symbol in enumerate(("UFO", "TLT", "AAA", "BBB", "CCC")):
        series[symbol] = tm.series_from_changes(tm.noise(N, 0.01, 600 + i))
    return series


SERIES = build_series()


class EtfViewHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.holdings_calls, self.news_calls = [], []

        def history(symbol, force=False, years=None):
            if symbol not in SERIES:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(SERIES[symbol].items())], "divs": [], "name": symbol}

        def holdings(symbol, *a, **k):
            self.holdings_calls.append(symbol)
            return {"status": "ok", "holdings": [dict(h) for h in HOLDINGS]} if symbol == "UFO" else {"status": "none", "holdings": []}

        def headlines(entries, **kw):
            self.news_calls.append(entries)
            return {e["symbol"]: {"headlines": [{"title": f"{e['symbol']} 기사", "source": "S", "url": "https://n.example/" + e["symbol"], "published": "2026-10-15"}],
                                  "error": False} for e in entries}
        patches = {
            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {}),
            "get_yahoo_daily_history": mock.Mock(side_effect=history),
            "fetch_top_holdings": mock.Mock(side_effect=holdings),
            "headlines_for_many": mock.Mock(side_effect=headlines),
        }
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        def item(symbol, name, amount):
            return {"symbol": symbol, "name": name, "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": amount}}
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0, "items": [item("UFO", "UFO", 123), item("TLT", "TLT", 456), item("SGOV", "SGOV", 789)]}

    def prepare(self):
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_cash_symbols(store, ["SGOV"])
        server.save_rebalance_config(store, [{"label": "우주", "symbols": ["UFO"], "target_pct": 10.0}, {"label": "채권", "symbols": ["TLT"], "target_pct": 20.0}], 5.0, 50.0)

    def call(self, cookie, args=None):
        body = {"name": "get_etf_breakdown"} if args is None else {"name": "get_etf_breakdown", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_etf_breakdown"})[0], 401)

    def test_etf_is_broken_down_and_plain_funds_are_listed_as_without_holdings(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.call(cookie)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual([i["symbol"] for i in result["items"]], ["UFO"])
        self.assertEqual([(n["symbol"], n["reason"]) for n in result["no_holdings"]], [("TLT", "편입 정보 없음")])    # SGOV(현금 취급)는 대상 아님
        item = result["items"][0]
        self.assertEqual(item["status"], "ok")
        symbols = [r["symbol"] for r in item["rows"]]
        self.assertEqual(sorted(symbols), ["AAA", "BBB", "CCC", "NOPRICE"])
        self.assertEqual(symbols[-1], "NOPRICE")                                  # 시세 없는 종목은 맨 뒤, 계산에서 제외
        self.assertIn("missing_prices", item["flags"])
        self.assertAlmostEqual(item["priced_weight_pct"], 60.0, places=1)
        contributions = [r["contribution_pct"] for r in item["rows"] if r["contribution_pct"] is not None]
        self.assertAlmostEqual(sum(contributions) + item["residual_pct"], item["etf_change_pct"], places=1)

    def test_top_contributors_get_factor_analysis_and_news_only_for_the_biggest_ones(self):
        cookie = self.login("key-a")
        self.prepare()
        item = self.call(cookie)[1]["result"]["items"][0]
        top3 = [r["symbol"] for r in item["rows"] if r["contribution_pct"] is not None][:3]
        self.assertEqual(sorted(item["drivers"]), sorted(top3))
        for d in item["drivers"].values():
            self.assertIn(d["status"], ("ok", "weak", "no_data"))
        self.assertEqual(len(item["headlines"]), 2)                               # 보도는 가장 큰 2개
        self.assertEqual(sorted(item["headlines"]), sorted(top3[:2]))

    def test_news_queries_contain_only_names_and_tickers(self):
        cookie = self.login("key-a")
        self.prepare()
        self.call(cookie)
        sent = [e for entries in self.news_calls for e in entries]
        self.assertTrue(sent)
        for e in sent:
            self.assertEqual(set(e), {"symbol", "name", "use_yahoo"})
        text = repr(self.news_calls)
        for needle in ("123", "456", "789", "555", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)

    def test_focus_limits_the_holdings_lookups(self):
        cookie = self.login("key-a")
        self.prepare()
        result = self.call(cookie, {"focus": "우주 ETF는 왜 떨어졌어"})[1]["result"]
        self.assertTrue(result["focused"])
        self.assertEqual(self.holdings_calls, ["UFO"])                            # TLT는 조회조차 하지 않는다
        self.assertEqual([i["symbol"] for i in result["items"]], ["UFO"])
        self.holdings_calls.clear()
        result = self.call(cookie, {"focus": 123})[1]["result"]
        self.assertFalse(result["focused"])
        self.assertEqual(sorted(self.holdings_calls), ["TLT", "UFO"])

    def test_focused_on_a_fund_without_holdings_says_so(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.call(cookie, {"focus": "채권"})
        result = payload["result"]
        self.assertEqual((status, result["items"], [n["symbol"] for n in result["no_holdings"]]), (200, [], ["TLT"]))

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        self.prepare()
        text = repr(self.call(cookie)[1])
        for needle in ("789", "555.0", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)

    def test_rule_based_question_names_the_etf_and_gets_the_breakdown_table(self):
        cookie = self.login("key-a")
        self.prepare()
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "UFO 구성 종목 기여 보여줘"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_etf_breakdown"])
        table = next(t for t in payload["answer"]["tables"] if t["title"].startswith("ETF 구성 종목별 기여"))
        self.assertEqual(len(table["rows"]), 1)
        self.assertTrue(any("UFO 이번 주" in b for b in payload["answer"]["bullets"]))

    def test_a_failing_lookup_for_one_etf_does_not_break_the_rest(self):
        cookie = self.login("key-a")
        self.prepare()
        # fetch_top_holdings 자체는 예외를 상태로 바꾸므로(실제 구현) 여기서는 상태로 오류를 흉내 낸다
        server.fetch_top_holdings.side_effect = lambda s, *a, **k: {"status": "error", "holdings": []} if s == "TLT" else \
            {"status": "ok", "holdings": [dict(h) for h in HOLDINGS]}
        result = self.call(cookie)[1]["result"]
        self.assertEqual([i["symbol"] for i in result["items"]], ["UFO"])
        self.assertEqual([(n["symbol"], n["reason"]) for n in result["no_holdings"]], [("TLT", "편입 정보 조회 실패")])


if __name__ == "__main__":
    unittest.main()
