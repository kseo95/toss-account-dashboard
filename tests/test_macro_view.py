"""매크로 요인 도구(서버 경로): 보유+관심 수집 → 야후 시세(가짜) → 요인 분해 → 답변. 일부 조회 실패 격리, 계좌 정보 비노출."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import macro
import test_macro as tm
import test_storage_http as storage_http

N = 300
INFO = {"069500": {"name": "KODEX 200", "market": "KOSPI", "currency": "KRW"},
        "LULU": {"name": "룰루레몬", "market": "NASDAQ", "currency": "USD"}}


def build_series():
    """요인 9개 + 자산 시세(TLT는 30년물 금리에 반응, 나머지는 잡음)."""
    series = {}
    for i, (symbol, _l, kind, _u, _d) in enumerate(macro.FACTORS):
        series[symbol] = tm.series_from_changes(tm.noise(N, 0.05 if kind == "level" else 0.01, 100 + i), kind)
    tyx = macro.daily_changes(series["^TYX"], "level")
    days = sorted(tyx)
    eps = tm.noise(len(days), 0.004, 200)
    series["TLT"] = tm.series_from_changes([-0.15 * tyx[d] + e for d, e in zip(days, eps)])
    series["069500.KS"] = tm.series_from_changes(tm.noise(N, 0.01, 201))
    series["LULU"] = tm.series_from_changes(tm.noise(N, 0.01, 202))
    return series


SERIES = build_series()


def history(symbol):
    if symbol not in SERIES:
        raise ValueError("데이터 없음")
    return {"bars": [(d, v, v) for d, v in sorted(SERIES[symbol].items())], "divs": [], "name": symbol}


class MacroViewHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patches = {
            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {s: INFO[s] for s in syms if s in INFO}),
            "get_yahoo_daily_history": mock.Mock(side_effect=history),
            "headlines_for_many": mock.Mock(side_effect=lambda entries, **kw: {e["symbol"]: {"headlines": [], "error": False} for e in entries}),
        }
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        def item(symbol, name, currency, country, amount):
            return {"symbol": symbol, "name": name, "currency": currency, "marketCountry": country, "quantity": 7, "marketValue": {"amount": amount}}
        items = [item("TLT", "TLT", "USD", "US", 123), item("SGOV", "SGOV", "USD", "US", 789), item("069500", "KODEX 200", "KRW", "KR", 100000)]
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0, "items": items}

    def prepare(self):
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_cash_symbols(store, ["SGOV"])
        server.save_watchlist(store, ["LULU"])

    def tool(self, cookie, name="get_macro_drivers"):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", {"name": name}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_macro_drivers"})[0], 401)

    def test_factors_and_items_are_computed_for_held_and_watchlist_but_not_cash_stocks(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.tool(cookie)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual({f["label"] for f in result["factors"]}, {f[1] for f in macro.FACTORS})
        units = {f["label"]: f["unit"] for f in result["factors"]}
        self.assertEqual((units["10년물 금리"], units["달러"]), ("bp", "%"))
        symbols = [i["symbol"] for i in result["items"]]
        self.assertEqual(sorted(symbols), ["069500", "LULU", "TLT"])                          # SGOV(현금 취급) 제외
        self.assertEqual(next(i["held"] for i in result["items"] if i["symbol"] == "LULU"), False)
        moves = [abs(i["weekly_change_pct"]) for i in result["items"]]
        self.assertEqual(moves, sorted(moves, reverse=True))                                 # 많이 움직인 순

    def test_asset_driven_by_a_factor_is_attributed_and_noise_assets_are_weak(self):
        cookie = self.login("key-a")
        self.prepare()
        items = {i["symbol"]: i for i in self.tool(cookie)[1]["result"]["items"]}
        tlt = items["TLT"]
        self.assertEqual(tlt["status"], "ok")
        self.assertEqual(tlt["drivers"][0]["label"], "30년물 금리")
        self.assertGreater(tlt["r2"], 0.7)
        self.assertEqual(tlt["drivers"][0]["unit"], "bp")
        self.assertEqual(items["069500"]["status"], "weak")                                  # 잡음 종목은 '설명 안 됨'으로 정직하게
        self.assertLess(items["069500"]["r2"], macro.WEAK_R2)

    def test_yahoo_symbols_requested_follow_market_and_factor_list(self):
        cookie = self.login("key-a")
        self.prepare()
        self.tool(cookie)
        asked = {c.args[0] for c in server.get_yahoo_daily_history.call_args_list}
        self.assertEqual(asked, {f[0] for f in macro.FACTORS} | {"TLT", "069500.KS", "LULU"})

    def test_failed_asset_history_only_marks_that_asset_no_data(self):
        cookie = self.login("key-a")
        self.prepare()
        original = server.get_yahoo_daily_history.side_effect

        def flaky(symbol):
            if symbol == "TLT":
                raise server.requests.ConnectionError("down")
            return original(symbol)
        server.get_yahoo_daily_history.side_effect = flaky
        items = {i["symbol"]: i for i in self.tool(cookie)[1]["result"]["items"]}
        self.assertEqual(items["TLT"]["status"], "no_data")
        self.assertIsNone(items["TLT"]["weekly_change_pct"])
        self.assertIn(items["069500"]["status"], ("weak", "ok"))
        self.assertEqual(list(items).index("TLT"), 2)                                         # 등락률을 모르면 맨 뒤

    def test_missing_factor_series_is_skipped_but_others_still_work(self):
        cookie = self.login("key-a")
        self.prepare()
        original = server.get_yahoo_daily_history.side_effect

        def flaky(symbol):
            if symbol == "CL=F":
                raise server.requests.ConnectionError("down")
            return original(symbol)
        server.get_yahoo_daily_history.side_effect = flaky
        result = self.tool(cookie)[1]["result"]
        self.assertNotIn("유가(WTI)", {f["label"] for f in result["factors"]})
        self.assertEqual(next(i for i in result["items"] if i["symbol"] == "TLT")["status"], "ok")

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        self.prepare()
        text = repr(self.tool(cookie)[1])
        import re
        for needle in ("123", "789", "100000", "555", "11100000001", "tok-a"):
            self.assertIsNone(re.search(rf"(?<![0-9.]){needle}(?![0-9])", text), needle)          # 계산된 소수점 숫자와 겹치는 우연은 제외하고 값 그대로 나온 것만 본다

    def test_rule_based_question_returns_market_and_driver_tables_together_with_news(self):
        cookie = self.login("key-a")
        self.prepare()
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "TLT 왜 떨어졌어?"}, cookie=cookie)
        self.assertEqual((status, payload["mode"]), (200, "rule_based_preview"))
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_macro_drivers", "get_weekly_news", "get_etf_breakdown"])   # "왜"는 ETF 구성 종목 분해도 같이 부른다
        titles = [t["title"] for t in payload["answer"]["tables"]]
        self.assertEqual(titles, ["이번 주 종목별 움직임과 보도"])                                          # 시장 요인·요인별 기여는 그래프로
        chart_titles = [c["title"] for c in payload["answer"]["charts"]]
        self.assertEqual(chart_titles[0], "이번 주 시장 요인 변화 (많이 움직인 순)")
        tlt = next(c for c in payload["answer"]["charts"] if c["title"].startswith("TLT 이번 주"))
        self.assertIn("30년물 금리", tlt["labels"][0])
        self.assertTrue(any(b.startswith("이번 주 시장:") for b in payload["answer"]["bullets"]))


if __name__ == "__main__":
    unittest.main()
