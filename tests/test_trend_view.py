"""미국 업종 ETF 상태 도구(서버 경로): 업종 ETF 수신과 상태, 질문별 정렬(focus), 이번 질문에서만 기준 바꾸기, 시세 실패 격리, 규칙 기반 질문("상태 어떰?"), 내 업종 노출 질문과의 구분, 계좌 비노출."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import trend
import test_storage_http as storage_http

END = date(2026, 10, 16)
N = 700


def series(f):
    days = []
    d = END
    while len(days) < N:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return {day: f(i) for i, day in enumerate(days)}


PRICES = {s: series(lambda i, k=k: 100.0 * (1.0005 + 0.0004 * k) ** i) for k, (s, _n) in enumerate(trend.SECTOR_ETFS)}
PRICES["XLK"] = series(lambda i: 100.0 * 1.002 ** i if i < N - 12 else 100.0 * 1.002 ** (N - 12) * 0.985 ** (i - (N - 12)))   # 오르다 최근 급락
PRICES["XLU"] = series(lambda i: 300.0 * 0.998 ** i)                                                                       # 계속 하락
PRICES["SPY"] = series(lambda i: 100.0 * 1.001 ** i)
PRICES["TLT"] = series(lambda i: 100.0 * 1.001 ** i)


class TrendHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

        def history(symbol, force=False, years=None):
            self.calls.append((symbol, years))
            if symbol not in PRICES:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(PRICES[symbol].items())], "divs": [], "name": symbol}
        for name, value in {"get_yahoo_daily_history": mock.Mock(side_effect=history),
                            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {})}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [{"symbol": "TLT", "name": "TLT", "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 123}}]}

    def tool(self, cookie, args=None):
        body = {"name": "get_sector_trends"} if args is None else {"name": "get_sector_trends", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def ask(self, cookie, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_sector_trends"})[0], 401)

    def test_sector_etfs_get_states_and_the_ladder(self):
        cookie = self.login("key-a")
        status, payload, _ = self.tool(cookie)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual(result["universe"], "sectors")
        self.assertEqual(len(result["rows"]), 11)
        self.assertEqual(result["insufficient"], [])
        by = {r["symbol"]: r for r in result["rows"]}
        self.assertEqual(by["XLU"]["state"], "하락 유지")
        self.assertNotEqual(by["XLK"]["state"], "상승 유지")                                          # 오르다 최근 급락 → 상승 유지는 아니다
        self.assertIn(by["XLK"]["previous_state"], ("상승 유지", "약화 초입"))                        # 그 전까지는 상승 쪽이었다
        self.assertIn(("SPY", server.TREND_YEARS), self.calls)                                       # 비교 기준 SPY도 받는다
        self.assertGreaterEqual(server.TREND_YEARS, 20)                                              # 전 고점(상장 이후 최고 종가)을 구하려고 시세를 길게 받는다
        self.assertEqual(result["cfg"], trend.DEFAULTS)
        self.assertIsNotNone(result["benchmark"]["r_mid"])
        self.assertTrue(all(r["vs_benchmark_mid"] is not None for r in result["rows"]))

    def test_a_question_can_override_the_defaults_once_and_bad_values_are_ignored(self):
        cookie = self.login("key-a")
        once = self.tool(cookie, {"confirm_weeks": 1, "buffer_pct": 0.5})[1]["result"]
        self.assertEqual((once["cfg"]["confirm_weeks"], once["cfg"]["buffer_pct"]), (1, 0.5))
        again = self.tool(cookie)[1]["result"]
        self.assertEqual(again["cfg"], trend.DEFAULTS)                                               # 다음 질문에는 영향이 없다
        self.assertEqual(self.tool(cookie, {"confirm_weeks": 99, "short_days": "x"})[1]["result"]["cfg"], trend.DEFAULTS)

    def test_focus_changes_the_order_only(self):
        cookie = self.login("key-a")
        base = self.tool(cookie, {"focus": "all"})[1]["result"]
        short = self.tool(cookie, {"focus": "short"})[1]["result"]
        values = [r["metrics"]["r_short"] for r in short["rows"]]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual({r["symbol"]: r["state"] for r in base["rows"]}, {r["symbol"]: r["state"] for r in short["rows"]})

    def test_a_failed_download_is_listed_not_fatal(self):
        cookie = self.login("key-a")
        saved = PRICES.pop("XLE")
        try:
            result = self.tool(cookie)[1]["result"]
        finally:
            PRICES["XLE"] = saved
        self.assertEqual(len(result["rows"]), 10)
        self.assertEqual(result["insufficient"], ["XLE"])

    def test_missing_benchmark_still_works_without_relative_numbers(self):
        cookie = self.login("key-a")
        saved = PRICES.pop("SPY")
        try:
            result = self.tool(cookie)[1]["result"]
        finally:
            PRICES["SPY"] = saved
        self.assertEqual(len(result["rows"]), 11)
        self.assertIsNone(result["benchmark"])

    def test_my_holdings_universe(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"universe": "mine"})[1]["result"]
        self.assertEqual([r["symbol"] for r in result["rows"]], ["TLT"])
        self.assertEqual(result["universe"], "mine")

    def test_the_status_question_that_used_to_return_the_users_own_portfolio_now_gets_the_sector_table(self):
        cookie = self.login("key-a")
        status, payload, _ = self.ask(cookie, "지금 미국 업종별 etf들 상태 어떰?")
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_sector_trends"])               # 내 포트폴리오 도구가 아니다
        table = payload["answer"]["tables"][0]
        self.assertEqual(table["title"], "미국 업종 ETF 11개 전 고점 대비와 추세 상태")
        self.assertEqual(len(table["rows"]), 11)
        self.assertEqual([r[0] for r in table["rows"]], [str(i) for i in range(1, 12)])               # 순위 1~11
        chart = payload["answer"]["charts"][0]
        self.assertEqual((chart["type"], len(chart["labels"])), ("hbar", 11))                         # 잘나가는 순 가로 막대
        self.assertIn("미국 업종 ETF 11개 — 잘나가는 순", chart["title"])

    def test_a_long_term_question_shows_the_long_horizon_first(self):
        cookie = self.login("key-a")
        payload = self.ask(cookie, "미국 업종 ETF 중 장기 추세가 바뀐 곳은?")[1]
        chart = payload["answer"]["charts"][0]
        self.assertEqual([d["label"] for d in chart["datasets"]], ["12개월", "6개월"])                 # 장기를 물으면 긴 기간부터
        self.assertIn("장기 수익률", chart["title"])

    def test_questions_about_my_own_sector_exposure_still_go_to_the_exposure_tool(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "execute_agent_tool", side_effect=lambda req, name, args=None: {"result": {}}) as run:
            for question in ("섹터 노출 보여줘", "업종 비중 보여줘", "내 포트폴리오 섹터 노출은?", "내 업종 상태 어때"):
                run.reset_mock()
                self.ask(cookie, question)
                self.assertNotIn("get_sector_trends", [c.args[1] for c in run.call_args_list], question)

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        text = repr(self.tool(cookie, {"universe": "mine"})[1])
        for needle in ("789", "555.0", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
