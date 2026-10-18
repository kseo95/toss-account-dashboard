"""조건부 반응(response.py·get_factor_response): 구간 나누기·평소와의 비교·검정, 질문 해석('금'과 '금리' 구분)·라우팅, 서버 경로(보유 종목 무관·시세 실패 격리·계좌 비노출)."""
from __future__ import annotations

import math
import random
import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import response
import test_storage_http as storage_http
from agent_tools import compose_agent_answer, format_tool_result, is_response_query, parse_response_query, plan_agent_query

N = 2600


def business_days(n, start=date(2012, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = business_days(N)


def path(changes, kind="price", base=100.0):
    out, v = {}, base
    for d, c in zip(DAYS, changes):
        v = v + c if kind == "level" else v * math.exp(c)
        out[d] = v
    return out


def synthetic():
    rng = random.Random(5)
    rate_ch = [rng.gauss(0, 0.04) for _ in DAYS]                                                      # 금리(%p) 일간 변화
    neg = [-0.3 * r + rng.gauss(0, 0.004) for r in rate_ch]                                           # 금리와 반대로 움직이는 자산
    unrelated = [rng.gauss(0.0003, 0.008) for _ in DAYS]
    return path(rate_ch, "level", 30.0), path(neg), path(unrelated)


RATE, NEG, FLAT = synthetic()


class AnalyzeTest(unittest.TestCase):
    def test_buckets_are_the_top_and_bottom_fifth_of_the_factor_changes(self):
        got = response.analyze(RATE, "level", {"반대": NEG, "무관": FLAT}, 21)
        self.assertGreater(got["thresholds"]["up"], 0)
        self.assertLess(got["thresholds"]["down"], 0)
        row = got["assets"][0]
        n = row["windows"]
        self.assertAlmostEqual(row["up"]["n"] / n, 0.2, delta=0.04)
        self.assertAlmostEqual(row["down"]["n"] / n, 0.2, delta=0.04)
        self.assertEqual(row["up"]["n"] + row["down"]["n"] + row["flat"]["n"], n)

    def test_an_asset_that_moves_against_the_factor_is_weaker_when_the_factor_rises_and_stronger_when_it_falls(self):
        got = {a["name"]: a for a in response.analyze(RATE, "level", {"반대": NEG, "무관": FLAT}, 21)["assets"]}
        self.assertEqual(got["반대"]["up"]["verdict"], "lower")                                          # 금리 큰 상승 구간에서 오른 비율이 평소보다 낮다(구분됨)
        self.assertEqual(got["반대"]["down"]["verdict"], "higher")
        self.assertLess(got["반대"]["up"]["median"], got["반대"]["baseline"]["median"])
        self.assertLess(got["반대"]["correlation"]["1년"], -0.5)
        self.assertEqual(got["무관"]["up"]["verdict"], "mixed")                                          # 상관 없는 자산은 구분 안 됨

    def test_windows_do_not_overlap_and_end_at_the_latest_day(self):
        ws = response.windows(RATE, "level", NEG, 21)
        ends = [w["end"] for w in ws]
        self.assertEqual(ends[-1], DAYS[-1])
        common = sorted(set(RATE) & set(NEG))
        self.assertEqual(len(ws), (len(common) - 1) // 21)
        self.assertTrue(all(common.index(b) - common.index(a) == 21 for a, b in zip(ends, ends[1:])))

    def test_current_window_is_placed_in_a_bucket(self):
        got = response.analyze(RATE, "level", {"반대": NEG}, 21)
        self.assertIn(got["current_bucket"], ("up", "down", "flat"))
        self.assertAlmostEqual(got["current"], (RATE[DAYS[-1]] - RATE[DAYS[-22]]) * 100, places=6)

    def test_short_series_are_listed_not_fatal(self):
        short = {d: v for d, v in NEG.items() if d >= DAYS[-300]}
        got = response.analyze(RATE, "level", {"짧음": short, "반대": NEG}, 21)
        self.assertEqual(got["insufficient"], ["짧음"])
        self.assertEqual([a["name"] for a in got["assets"]], ["반대"])

    def test_too_little_factor_history_returns_nothing(self):
        tiny = {d: v for d, v in RATE.items() if d >= DAYS[-200]}
        got = response.analyze(tiny, "level", {"반대": NEG}, 21)
        self.assertIsNone(got["thresholds"])
        self.assertEqual(got["assets"], [])

    def test_unknown_horizon_falls_back_to_a_month(self):
        self.assertEqual(response.analyze(RATE, "level", {"반대": NEG}, 7)["horizon"], 21)

    def test_binomial_test_basics(self):
        self.assertAlmostEqual(response._binom_p(50, 100, 0.5), 1.0, places=6)
        self.assertLess(response._binom_p(80, 100, 0.5), 0.001)
        self.assertEqual(response._binom_p(0, 0, 0.5), 1.0)


class ResolveTest(unittest.TestCase):
    def test_gold_is_not_confused_with_interest_rates_or_cash(self):
        self.assertEqual(response.resolve_asset("금과"), response.GOLD)
        self.assertEqual(response.resolve_asset("금값"), response.GOLD)
        self.assertIsNone(response.resolve_asset("금리"))
        self.assertIsNone(response.resolve_asset("현금"))
        self.assertEqual(response.resolve_asset("나스닥은")[0], "^IXIC")
        self.assertEqual(response.resolve_factor("미국 금리")[0], "^TNX")
        self.assertEqual(response.resolve_factor("30년물 금리")[0], "^TYX")
        self.assertIsNone(response.resolve_factor("날씨"))


class QuestionTest(unittest.TestCase):
    def test_the_users_question_becomes_arguments(self):
        q = "미국 금리가 오르면 금과 나스닥은 어떻게 됨?"
        self.assertTrue(is_response_query(q))
        self.assertEqual(parse_response_query(q), {"factor": "^TNX", "direction": "up", "assets": ["금", "나스닥"], "horizon_days": 21})
        self.assertEqual(plan_agent_query(q)["tools"], ["get_factor_response"])

    def test_direction_assets_and_horizon_are_read_from_the_sentence(self):
        got = parse_response_query("달러가 내리면 금이랑 비트코인은 3개월 동안 어떻게 돼")
        self.assertEqual((got["factor"], got["direction"], got["horizon_days"]), ("DX-Y.NYB", "down", 63))
        self.assertEqual(got["assets"], ["금", "비트코인"])
        self.assertEqual(parse_response_query("유가가 오르면 코스피는 어떻게 돼")["assets"], ["코스피"])
        self.assertEqual(parse_response_query("금리가 오르면 영향이 어때")["assets"], [])                # 자산을 안 말하면 기본 묶음

    def test_the_factor_is_the_word_before_the_connector_even_when_an_asset_word_is_the_same(self):
        got = parse_response_query("금리가 오르면 달러는 어떻게 돼")
        self.assertEqual(got["factor"], "^TNX")
        self.assertEqual(got["assets"], ["달러(인덱스)"])

    def test_other_questions_keep_their_routes(self):
        self.assertFalse(is_response_query("금리가 오르면 내 포트폴리오는 어떻게 돼?"))                   # 내 보유 종목은 기존 도구
        self.assertFalse(is_response_query("금리 올랐어"))
        self.assertFalse(is_response_query("금이 왜 고점 대비 빠졌어?"))
        self.assertEqual(plan_agent_query("금이 왜 고점 대비 빠졌어?")["tools"], ["get_drawdown_analysis"])


class ResponseHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        series = {"^TNX": RATE, "GC=F": NEG, "^IXIC": FLAT, "^GSPC": FLAT, "^KS11": FLAT, "TLT": NEG, "BTC-USD": FLAT}

        def history(symbol, force=False, years=None):
            self.calls.append((symbol, years))
            if symbol not in series:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(series[symbol].items())], "divs": [], "name": symbol}
        for name, value in {"get_yahoo_daily_history": mock.Mock(side_effect=history), "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {})}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.series = series

    def snapshot(self):
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [{"symbol": "TLT", "name": "TLT", "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 123}}]}

    def tool(self, cookie, args):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", {"name": "get_factor_response", "args": args}, cookie=cookie)

    def ask(self, cookie, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_factor_response", "args": {"factor": "금리"}})[0], 401)

    def test_assets_need_not_be_holdings_and_long_history_is_requested(self):
        cookie = self.login("key-a")
        status, payload, _ = self.tool(cookie, {"factor": "금리", "assets": ["금", "나스닥"]})
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual([a["name"] for a in result["assets"]], ["금", "나스닥"])                           # 보유 종목이 아닌 지수·금도 본다
        self.assertEqual(result["factor"]["name"], "미국 10년물 금리")
        self.assertIn(("^TNX", server.RESPONSE_YEARS), self.calls)
        self.assertGreaterEqual(server.RESPONSE_YEARS, 20)

    def test_default_assets_and_bad_arguments(self):
        cookie = self.login("key-a")
        names = [a["name"] for a in self.tool(cookie, {"factor": "금리"})[1]["result"]["assets"]]
        self.assertEqual(names, ["금", "나스닥", "S&P500", "코스피", "미국 장기채(TLT)", "비트코인"])
        self.assertEqual(self.tool(cookie, {"factor": "날씨"})[0], 400)
        bad = self.tool(cookie, {"factor": "금리", "assets": ["금", "화성부동산"], "horizon_days": 7, "direction": "sideways"})[1]["result"]
        self.assertEqual(bad["unresolved"], ["화성부동산"])
        self.assertEqual((bad["horizon"], bad["direction"]), (21, "up"))

    def test_a_missing_asset_series_is_listed_and_a_missing_factor_is_an_error(self):
        cookie = self.login("key-a")
        saved = self.series.pop("^IXIC")
        try:
            result = self.tool(cookie, {"factor": "금리", "assets": ["금", "나스닥"]})[1]["result"]
            self.assertEqual(result["insufficient"], ["나스닥"])
            self.assertEqual([a["name"] for a in result["assets"]], ["금"])
            saved_rate = self.series.pop("^TNX")
            try:
                self.assertEqual(self.tool(cookie, {"factor": "금리"})[0], 502)
            finally:
                self.series["^TNX"] = saved_rate
        finally:
            self.series["^IXIC"] = saved

    def test_the_users_question_gets_the_conditional_chart_not_the_portfolio(self):
        cookie = self.login("key-a")
        status, payload, _ = self.ask(cookie, "미국 금리가 오르면 금과 나스닥은 어떻게 됨?")
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_factor_response"])
        answer = payload["answer"]
        text = "\n".join(answer["bullets"])
        self.assertIn("미국 10년물 금리가", text)
        self.assertIn("금: 큰 상승 구간에서 평소보다 약함 (통계적으로 구분됨)", text)                   # 금리와 반대로 움직이는 금(합성)은 구분됨
        self.assertIn("나스닥: 큰 상승 구간에서 평소와 뚜렷이 다르지 않음", text)
        self.assertEqual(answer["tables"], [])                                                        # 표 대신 그래프
        chart = answer["charts"][0]
        self.assertEqual((chart["type"], chart["labels"]), ("bar", ["금", "나스닥"]))
        self.assertEqual([d["label"] for d in chart["datasets"]], ["큰 하락 구간", "평소", "큰 상승 구간"])
        self.assertTrue(chart["datasets"][2]["text"][0].startswith("-"))                              # 금: 금리 큰 상승 구간 수익률은 마이너스
        self.assertIn("▼", chart["datasets"][2]["text"][0])                                           # 평소와 구분됨 표시
        self.assertIn("오른", chart["datasets"][2]["text"][0])
        self.assertIn("지금", chart["subtitle"])
        self.assertTrue(any("원인 증명 아님" in chart["caption"] for _ in [0]))
        self.assertTrue(any("원인이라는 증명이 아닙니다" in l for l in answer["limitations"]))

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        text = repr(self.tool(cookie, {"factor": "금리"})[1])
        for needle in ("11100000001", "tok-a", "quantity", "marketValue", "555.0"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
