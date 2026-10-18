"""보조지표 도구(서버 경로·답변): 아무 종목(이름으로 찾기), 지표 선택·기준 바꾸기, 시세 짧음 격리, 한 줄 요약·그래프, 라우팅, 계좌 비노출."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import test_storage_http as storage_http
from agent_tools import compose_agent_answer, format_tool_result, is_indicator_query, parse_indicator_query, plan_agent_query

N = 700


def days(n):
    out, d = [], date(2024, 1, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = days(N)


def series(f, n=N):
    return {d: f(i) for i, d in enumerate(DAYS[-n:])}


UP = series(lambda i: 100 * 1.003 ** i)                                                                  # 꾸준한 상승
WAVE = series(lambda i: 100 + 12 * math.sin(i / 9))                                                       # 오르내림(교차가 자주)
SHORT = series(lambda i: 100 + i, 30)


class IndicatorsHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        server._extra_cache.clear()
        self.addCleanup(server._extra_cache.clear)
        self.calls = []
        table = {"TLT": UP, "NVDA": WAVE, "NEWB": SHORT}

        def history(symbol, force=False, years=None):
            self.calls.append((symbol, years))
            if symbol not in table:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(table[symbol].items())], "divs": [], "name": symbol}
        results = {"엔비디아": [{"symbol": "NVDA", "name": "엔비디아", "market": "NASDAQ", "currency": "USD"}], "신규상장": [{"symbol": "NEWB", "name": "신규상장", "market": "NASDAQ", "currency": "USD"}]}
        for name, value in {"get_yahoo_daily_history": mock.Mock(side_effect=history), "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {}),
                            "search_stocks": mock.Mock(side_effect=lambda token, query, limit=8: results.get(query, []))}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [{"symbol": "TLT", "name": "TLT", "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 123}}]}

    def tool(self, cookie, args):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", {"name": "get_indicators", "args": args}, cookie=cookie)

    def ask(self, cookie, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_indicators"})[0], 401)

    def test_a_holding_gets_all_four_indicators_and_long_enough_history(self):
        cookie = self.login("key-a")
        status, payload, _ = self.tool(cookie, {"focus": "TLT"})
        self.assertEqual(status, 200, payload)
        item = payload["result"]["items"][0]
        self.assertEqual((item["symbol"], item["status"], item["rsi"]["state"]), ("TLT", "ok", "overbought"))        # 꾸준한 상승은 과매수
        self.assertTrue(item["macd"]["above_signal"] and item["ma_cross"]["fast_above"])
        self.assertEqual(len(item["series"]["dates"]), 120)
        self.assertEqual(len(item["series"]["rsi"]), 120)
        self.assertIn(("TLT", server.INDICATOR_YEARS), self.calls)
        self.assertGreaterEqual(server.INDICATOR_YEARS, 2)                                                       # 200일선 교차까지

    def test_any_stock_by_name_and_the_chosen_indicators_and_thresholds(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"focus": "엔비디아 RSI", "indicators": ["rsi", "bogus"], "overbought": 60, "oversold": 40, "rsi_period": 7})[1]["result"]
        item = result["items"][0]
        self.assertEqual((item["symbol"], item["extra"], result["indicators"]), ("NVDA", True, ["rsi"]))
        self.assertEqual((item["params"]["rsi_period"], item["params"]["overbought"], item["params"]["oversold"]), (7.0, 60.0, 40.0))
        bad = self.tool(cookie, {"focus": "TLT", "overbought": 1000, "rsi_period": "x"})[1]["result"]["items"][0]["params"]
        self.assertEqual((bad["overbought"], bad["rsi_period"]), (70.0, 14.0))                                    # 범위 밖·잘못된 값은 기본값

    def test_a_short_history_is_isolated_and_missing_slow_average_is_skipped(self):
        cookie = self.login("key-a")
        items = {i["symbol"]: i for i in self.tool(cookie, {"focus": "TLT 신규상장"})[1]["result"]["items"]}
        self.assertEqual(items["NEWB"]["status"], "no_data")
        self.assertEqual(items["TLT"]["status"], "ok")

    def test_the_question_gets_a_summary_line_and_charts(self):
        cookie = self.login("key-a")
        status, payload, _ = self.ask(cookie, "엔비디아 RSI MACD 볼린저 어때?")
        self.assertEqual((status, [e["tool"] for e in payload["executions"]]), (200, ["get_indicators"]))
        answer = payload["answer"]
        line = next(b for b in answer["bullets"] if b.startswith("엔비디아"))
        self.assertIn("· 조회", line)
        for piece in ("RSI ", "MACD가 시그널", "볼린저 "):
            self.assertIn(piece, line)
        titles = [c["title"] for c in answer["charts"]]
        self.assertEqual(len(titles), 3)
        self.assertTrue(titles[0].startswith("엔비디아 볼린저밴드") and titles[1].startswith("엔비디아 RSI") and titles[2].startswith("엔비디아 MACD"))
        boll, rsi, macd = answer["charts"]
        self.assertEqual([d["label"] for d in boll["datasets"]], ["종가", "상단", "중심", "하단"])
        self.assertEqual([(h["value"], h["color"]) for h in rsi["hlines"]], [(70.0, "down"), (30.0, "up")])
        self.assertEqual((rsi["ymin"], rsi["ymax"]), (0.0, 100.0))                                                  # RSI 축은 0~100(과매도 선도 보이게)
        self.assertEqual(macd["hlines"][0]["value"], 0)
        self.assertTrue(all(len(c["labels"]) == 120 for c in answer["charts"]))
        self.assertTrue(any("매수·매도 신호가 아닙니다" in l for l in answer["limitations"]))

    def test_only_the_asked_indicator_is_drawn(self):
        cookie = self.login("key-a")
        answer = self.ask(cookie, "엔비디아 RSI 알려줘")[1]["answer"]
        self.assertEqual([c["title"].split(" (")[0] for c in answer["charts"]], ["엔비디아 RSI"])
        line = next(b for b in answer["bullets"] if b.startswith("엔비디아"))
        self.assertIn("RSI", line)
        self.assertNotIn("MACD", line)

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        text = repr(self.tool(cookie, {"focus": "TLT"})[1])
        for needle in ("11100000001", "tok-a", "quantity", "marketValue", "555.0"):
            self.assertNotIn(needle, text)


class RoutingAndComposeTest(unittest.TestCase):
    def test_indicator_questions_are_routed(self):
        for q in ("엔비디아 RSI 과매수야?", "TSLA MACD 골든크로스 났어?", "삼성전자 볼린저밴드", "보조지표 보여줘", "지금 과매도 구간이야", "50일선이 200일선 위로 골든크로스 했어"):
            self.assertTrue(is_indicator_query(q), q)
            self.assertEqual(plan_agent_query(q)["tools"], ["get_indicators"], q)

    def test_other_questions_keep_their_routes(self):
        self.assertFalse(is_indicator_query("superman macdonald"))                                               # 영문 단어 속 글자는 아니다
        self.assertEqual(plan_agent_query("주봉 ma 60/120/200/240 보다 밑에 있는 S&P500 소속 주식들은?")["tools"], ["get_ma_screen"])
        self.assertEqual(plan_agent_query("엔비디아 고평가야?")["tools"], ["get_valuation"])
        self.assertEqual(plan_agent_query("금이 왜 고점 대비 빠졌어?")["tools"], ["get_drawdown_analysis"])

    def test_which_indicators_are_asked(self):
        self.assertEqual(parse_indicator_query("엔비디아 RSI 과매수야?")["indicators"], ["rsi"])
        self.assertEqual(parse_indicator_query("RSI랑 MACD 같이")["indicators"], ["rsi", "macd"])
        self.assertEqual(parse_indicator_query("볼린저밴드 하단 이탈")["indicators"], ["bollinger"])
        self.assertEqual(parse_indicator_query("골든크로스 났어?")["indicators"], ["ma_cross"])
        self.assertEqual(parse_indicator_query("보조지표 전부 보여줘")["indicators"], [])                         # 비우면 전부

    def test_formatter_cleans_unknown_values_and_missing_series(self):
        raw = {"items": [{"symbol": "X", "name": "가" * 100, "status": "ok", "close": 10, "as_of": "2026-10-16", "params": {"rsi_period": 14},
                          "rsi": {"value": 55.55, "state": "<script>", "days_in_state": 3}, "bollinger": {"pct_b": 0.5, "state": "weird"},
                          "series": {"dates": ["2026-10-14", "2026-10-15", "2026-10-16"], "close": [1, 2, 3], "rsi": [None, 50.123456, 51]}},
                         {"symbol": "Y", "name": "Y", "status": "weird"}], "omitted": 0, "token": "must-not-leak"}
        out = format_tool_result("get_indicators", raw)["result"]
        self.assertNotIn("must-not-leak", repr(out))
        self.assertEqual((out["items"][0]["rsi"]["state"], out["items"][0]["bollinger"]["state"], len(out["items"][0]["name"])), ("neutral", "inside", 40))
        self.assertEqual(out["items"][1]["status"], "no_data")
        answer = compose_agent_answer({"get_indicators": out})
        self.assertTrue(any("계산하지 못했습니다" in n for n in answer["notes"]))


if __name__ == "__main__":
    unittest.main()
