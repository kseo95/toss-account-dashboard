"""이동평균 스크리너 도구(서버 경로): 구성 종목·시세 수신, 조건 판정, 짧은 시세·수신 실패 분리, 내 종목 범위, 구성 종목 수신 실패, 규칙 기반 질문, 계좌 비노출."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import screener
import test_storage_http as storage_http

END = date(2026, 10, 16)
N_DAYS = 6 * 365


def bars(f, n=N_DAYS):
    start = END - timedelta(days=n - 1)
    return [(start + timedelta(days=i), f(i), f(i)) for i in range(n) if (start + timedelta(days=i)).weekday() < 5]


PRICES = {
    "DOWNA": bars(lambda i: 300.0 - 0.1 * i),                           # 계속 하락 → 모든 주봉 MA 아래
    "DOWNB": bars(lambda i: 300.0 - 0.1 * i if i < N_DAYS - 30 else 300.0 - 0.1 * (N_DAYS - 30) - 0.2 * (i - (N_DAYS - 30))),   # 막판에 더 하락
    "UPPER": bars(lambda i: 50.0 + 0.1 * i),                            # 계속 상승 → 모든 MA 위
    "NEWCO": bars(lambda i: 20.0 + i, n=120),                           # 상장 4개월: 주봉 240개가 안 됨
    "TLT": bars(lambda i: 100.0 - 0.05 * i),                            # 내 보유 종목(미국 ETF)
}
UNIVERSE = [{"symbol": s, "name": n, "sector": sec, "sub_industry": ""} for s, n, sec in
            (("DOWNA", "다운에이", "Information Technology"), ("DOWNB", "다운비", "Financials"), ("UPPER", "업퍼", "Information Technology"),
             ("NEWCO", "뉴코", "Energy"), ("GONE", "수신실패", "Energy"))]


class MaScreenHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.history_calls = []

        def history(symbol, force=False, years=None):
            self.history_calls.append((symbol, years))
            if symbol not in PRICES:
                raise ValueError("데이터 없음")
            return {"bars": PRICES[symbol], "divs": [], "name": symbol}
        for name, value in {"fetch_sp500": mock.Mock(return_value=[dict(u) for u in UNIVERSE]),
                            "get_yahoo_daily_history": mock.Mock(side_effect=history),
                            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {})}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [{"symbol": "TLT", "name": "TLT", "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 123}}]}

    def tool(self, cookie, args=None):
        body = {"name": "get_ma_screen"} if args is None else {"name": "get_ma_screen", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_ma_screen"})[0], 401)

    def test_default_screen_is_sp500_below_all_four_weekly_averages(self):
        cookie = self.login("key-a")
        status, payload, _ = self.tool(cookie)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual((result["universe"], result["side"], result["match"], result["periods"]), ("sp500", "below", "all", [60, 120, 200, 240]))
        self.assertEqual([m["symbol"] for m in result["matches"]], ["DOWNB", "DOWNA"])                        # 더 깊이 아래인 것 먼저(DOWNB는 막판에 더 하락)
        self.assertEqual((result["universe_size"], result["evaluated"], result["insufficient"], result["failed"]), (5, 3, 1, 1))
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(result["symbols"], ["DOWNB", "DOWNA"])
        self.assertTrue(result["as_of"].startswith("2026-10"))
        self.assertIn(("DOWNA", server.SCREEN_YEARS), self.history_calls)                                     # 240주를 덮는 기간으로 받는다
        self.assertEqual(result["matches"][0]["sector"], "금융")                                              # 업종은 한글로
        self.assertTrue(all(len(m["mas"]) == 4 and m["mas"][0]["dist_pct"] < 0 for m in result["matches"]))

    def test_above_any_periods_and_sector_arguments(self):
        cookie = self.login("key-a")
        up = self.tool(cookie, {"side": "above", "periods": [60, 200]})[1]["result"]
        self.assertEqual([m["symbol"] for m in up["matches"]], ["UPPER"])
        self.assertEqual(up["periods"], [60, 200])
        tech = self.tool(cookie, {"sector": "Information Technology"})[1]["result"]
        self.assertEqual([m["symbol"] for m in tech["matches"]], ["DOWNA"])
        self.assertEqual((tech["universe_size"], tech["sector"]), (2, "정보기술"))

    def test_short_history_and_failed_downloads_are_counted_not_fatal(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"periods": [15]})[1]["result"]
        self.assertEqual(result["insufficient"], 0)                                                          # 15주면 상장 4개월(17주)짜리도 판단할 수 있다
        self.assertEqual(result["failed"], 1)

    def test_my_holdings_universe(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"universe": "mine", "periods": [60, 120]})[1]["result"]
        self.assertEqual(result["universe"], "mine")
        self.assertEqual([m["symbol"] for m in result["matches"]], ["TLT"])
        server.fetch_sp500.assert_not_called()                                                               # 내 종목만 볼 때는 구성 종목 목록을 받지 않는다

    def test_constituent_list_failure_is_a_clear_error(self):
        cookie = self.login("key-a")
        server.fetch_sp500.side_effect = screener.ScreenerError("S&P500 구성 종목 목록을 받지 못했습니다.")
        status, payload, _ = self.tool(cookie)
        self.assertEqual(status, 502)
        self.assertIn("구성 종목 목록을 받지 못했습니다", payload["error"])

    def test_bad_arguments_fall_back_to_defaults(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"periods": ["x", 1, 999], "side": "sideways", "match": 5, "universe": "kospi"})[1]["result"]
        self.assertEqual((result["periods"], result["side"], result["match"], result["universe"]), ([60, 120, 200, 240], "below", "all", "sp500"))
        self.assertEqual(self.tool(cookie, "문자열 인자")[0], 200)

    def test_the_users_own_question_is_turned_into_arguments(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "지금 주봉 ma 60/120/200/240 보다 밑에 있는 S&P500 소속 주식들은?"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["executions"][0]["tool"], "get_ma_screen")                              # 물은 도구가 먼저, 뒤에 덧붙여 본 도구가 올 수 있다
        table = payload["answer"]["tables"][0]
        self.assertTrue(table["title"].startswith("주봉 MA 60·120·200·240 모두보다 아래에 있는 S&P500 구성 종목"))
        self.assertEqual([r[0] for r in table["rows"]], ["다운비 (DOWNB)", "다운에이 (DOWNA)"])
        self.assertTrue(any("S&P500 구성 종목 5개 중" in b and "2개" in b for b in payload["answer"]["bullets"]))
        self.assertTrue(any("매수·매도 신호" in l for l in payload["answer"]["limitations"]))

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        text = repr(self.tool(cookie, {"universe": "mine"})[1])
        for needle in ("789", "555.0", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
