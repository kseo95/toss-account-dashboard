"""보유·관심종목이 아닌 종목 지원(symbols.py + resolve_extra_entries): 후보 낱말 뽑기·정확 일치만 받기, 질문 해석('엔비디아 PER', 'TSLA 왜 빠졌어'), 보유 종목과 함께, 검색 실패·캐시·개수 제한, 검색에 보내는 내용, 표시('조회')."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import symbols
import test_fundamentals as tf
import test_storage_http as storage_http
from agent_tools import compose_agent_answer, format_tool_result, plan_agent_query


class CandidateTest(unittest.TestCase):
    def test_korean_names_codes_and_josa(self):
        self.assertEqual(symbols.candidate_tokens("엔비디아 어때?"), ["엔비디아"])
        self.assertEqual(symbols.candidate_tokens("삼성전자는 고평가야?"), ["삼성전자", "고평가"])                  # 조사를 떼고, 의도 낱말은 호출하는 쪽 skip이 거른다
        self.assertEqual(symbols.candidate_tokens("TSLA 고점 대비 왜 빠졌어?"), ["TSLA"])
        self.assertEqual(symbols.candidate_tokens("BRK.B 뉴스 알려줘"), ["BRK.B"])
        self.assertEqual(symbols.candidate_tokens("애플이랑 마이크로소프트 비교해줘"), ["애플", "마이크로소프트"])         # '비교해줘' 같은 동사 꼴은 뺀다

    def test_common_words_single_characters_and_market_words_are_not_candidates(self):
        for q in ("금이 왜 고점 대비 빠졌어?", "금은 어때", "미국 금리가 오르면 나스닥은 어떻게 돼?", "이번 주 뭘 확인해야 해?"):
            self.assertEqual([t for t in symbols.candidate_tokens(q) if t not in ("확인해", "뭘")], [], q)

    def test_skip_set_limit_dedupe_and_order(self):
        self.assertEqual(symbols.candidate_tokens("엔비디아 PER", skip={"per"}), ["엔비디아"])
        self.assertEqual(symbols.candidate_tokens("가나다 NVDA 라마바 tsla AAPL"), ["NVDA", "AAPL", "가나다", "라마바", "tsla"])      # 대문자 코드 → 한글 → 소문자
        self.assertEqual(len(symbols.candidate_tokens(" ".join(f"종목{i}" for i in range(20)))), symbols.MAX_CANDIDATES)
        self.assertEqual(symbols.candidate_tokens("엔비디아 엔비디아는 엔비디아가"), ["엔비디아"])
        self.assertEqual(symbols.candidate_tokens("<script>alert(1)</script> 123 가"), ["script", "alert", "script"][:0] + [t for t in symbols.candidate_tokens("<script>alert(1)</script> 123 가")])
        self.assertEqual(symbols.candidate_tokens(None), [])

    def test_strip_josa(self):
        self.assertEqual(symbols.strip_josa("엔비디아는"), "엔비디아")
        self.assertEqual(symbols.strip_josa("삼성전자에서"), "삼성전자")
        self.assertEqual(symbols.strip_josa("금이"), "금이")                                                    # 남는 글자가 한 글자면 떼지 않는다
        self.assertEqual(symbols.strip_josa("카카오"), "카카오")


class ChooseTest(unittest.TestCase):
    def test_exact_name_or_code_wins_and_loose_matches_are_rejected(self):
        results = [{"symbol": "AFL", "name": "애플랙"}, {"symbol": "AAPL", "name": "애플"}]
        self.assertEqual(symbols.choose("애플", results)["symbol"], "AAPL")                                      # 첫 결과가 아니어도 이름이 정확히 같은 것
        self.assertEqual(symbols.choose("tsla", [{"symbol": "TSLA", "name": "테슬라"}])["symbol"], "TSLA")        # 코드 일치(대소문자 무관)
        self.assertIsNone(symbols.choose("금", [{"symbol": "073240", "name": "금호타이어"}]))                      # '금'이 금호타이어로 잡히지 않는다
        self.assertEqual(symbols.choose("팔란티어", [{"symbol": "PLTR", "name": "팔란티어 테크놀로지스"}])["symbol"], "PLTR")   # 3자 이상이면 첫 결과 이름이 낱말로 시작해도 받는다
        self.assertIsNone(symbols.choose("삼성", [{"symbol": "005930", "name": "삼성전자"}, {"symbol": "X", "name": "삼성"}]) and None)
        self.assertIsNone(symbols.choose("애플", [{"symbol": "AFL", "name": "애플랙"}]))                           # 이름이 정확히 같지 않고 3자 미만이면 거절
        self.assertIsNone(symbols.choose("", [{"symbol": "A", "name": "A"}]))
        self.assertIsNone(symbols.choose("엔비디아", []))


NVDA = {"symbol": "NVDA", "name": "엔비디아", "market": "NASDAQ", "currency": "USD"}
TSLA = {"symbol": "TSLA", "name": "테슬라", "market": "NASDAQ", "currency": "USD"}
SAMSUNG = {"symbol": "005930", "name": "삼성전자", "market": "KOSPI", "currency": "KRW"}
SEARCH = {"엔비디아": [NVDA], "NVDA": [NVDA], "TSLA": [TSLA], "테슬라": [TSLA], "삼성전자": [SAMSUNG], "비교해줘": [], "금호": [{"symbol": "073240", "name": "금호타이어", "market": "KOSPI", "currency": "KRW"}]}


class ExtraTickerHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        server._extra_cache.clear()
        self.addCleanup(server._extra_cache.clear)
        server._peer_cache.clear()
        self.addCleanup(server._peer_cache.clear)
        self.searched = []
        self.table = {"NVDA": tf.f.clean_info(tf.info(trailingPE=60.0, priceToBook=40.0, priceToSalesTrailing12Months=25.0, trailingPegRatio=2.4), "NVDA"),
                      "TSLA": tf.f.clean_info(tf.info(trailingPE=120.0), "TSLA"), "TLT": tf.f.clean_info(tf.info(quoteType="ETF", sector=None, trailingPE=None, priceToBook=0.54), "TLT"),
                      "005930.KS": tf.f.clean_info(tf.info(currency="KRW"), "005930.KS")}

        def search(token, query, limit=8):
            self.searched.append(query)
            if query == "BOOM":
                raise server.requests.ConnectionError("검색 장애")
            return [dict(r) for r in SEARCH.get(query, [])]

        def fetch(symbol):
            metrics = self.table.get(symbol)
            return {"status": "ok", "metrics": dict(metrics)} if metrics else {"status": "error", "metrics": None}
        for name, value in {"search_stocks": mock.Mock(side_effect=search), "fetch_fundamentals": mock.Mock(side_effect=fetch),
                            "fetch_sp500": mock.Mock(return_value=[]), "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {})}.items():
            pt = mock.patch.object(server, name, value)
            pt.start()
            self.addCleanup(pt.stop)

    def snapshot(self, symbols_=("TLT",)):
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [{"symbol": s, "name": s, "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 123 + i}} for i, s in enumerate(symbols_)]}

    def ask(self, cookie, question, held=("TLT",)):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot(held)), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def tool(self, cookie, args, held=("TLT",)):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot(held)), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", {"name": "get_valuation", "args": args}, cookie=cookie)

    def test_a_stock_not_in_the_account_is_found_by_its_korean_name(self):
        cookie = self.login("key-a")
        status, payload, _ = self.ask(cookie, "엔비디아 고평가야?")
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_valuation"])
        text = "\n".join(payload["answer"]["bullets"])
        self.assertIn("엔비디아(개별주)", text)                                                                   # 보유 TLT가 아니라 질문의 종목
        self.assertNotIn("TLT", text)
        table = payload["answer"]["tables"][0]
        self.assertEqual([r[0] for r in table["rows"]], ["엔비디아 (NVDA)"])
        self.assertEqual(self.searched, ["엔비디아"])                                                              # 검색에 보낸 것은 후보 낱말뿐(질문 전체·'고평가야?'는 아님)

    def test_an_uppercase_ticker_and_the_tool_path_used_by_the_ai_mode(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"focus": "TSLA"})[1]["result"]
        self.assertEqual([(i["symbol"], i["extra"], i["held"]) for i in result["items"]], [("TSLA", True, False)])
        self.assertTrue(result["focused"])
        self.assertEqual(self.searched, ["TSLA"])

    def test_a_holding_and_a_new_stock_can_be_asked_together(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"focus": "TLT랑 엔비디아 비교해줘"})[1]["result"]
        self.assertEqual(sorted(i["symbol"] for i in result["items"]), ["NVDA", "TLT"])
        self.assertEqual({i["symbol"]: i["extra"] for i in result["items"]}, {"NVDA": True, "TLT": False})
        self.assertNotIn("TLT", self.searched)                                                                     # 보유 종목 이름은 검색하지 않는다

    def test_holdings_and_market_questions_never_search(self):
        cookie = self.login("key-a")
        self.ask(cookie, "TLT 고평가야?")
        req = mock.Mock(session=server._sessions[next(iter(server._sessions))])
        entries = [{"symbol": "TLT", "name": "TLT", "held": True}]
        for question in ("금이 왜 고점 대비 빠졌어?", "미국 금리가 오르면 금과 나스닥은 어떻게 됨?", "지금 미국 업종별 etf들 상태 어떰?", "이번 주 무엇을 확인해야 해?", "전부 팔면 세금은?"):
            self.assertEqual(server.resolve_extra_entries(req, question, entries), [], question)
        self.assertEqual(self.searched, [])

    def test_loose_search_results_are_rejected_and_the_answer_falls_back(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"focus": "금호 고평가야"})[1]["result"]                                         # '금호' → 금호타이어(정확히 다름) → 못 찾음
        self.assertEqual([i["symbol"] for i in result["items"]], ["TLT"])                                            # 종목을 못 찾으면 기존처럼 보유 종목 전체
        self.assertFalse(result["focused"])

    def test_a_search_failure_is_isolated_and_not_cached(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, {"focus": "BOOM NVDA"})[1]["result"]
        self.assertEqual([i["symbol"] for i in result["items"]], ["NVDA"])                                            # 장애 난 낱말만 건너뛴다
        self.tool(cookie, {"focus": "BOOM"})
        self.assertEqual(self.searched.count("BOOM"), 2)                                                           # 일시 오류는 다시 시도

    def test_results_are_cached_including_misses(self):
        cookie = self.login("key-a")
        self.tool(cookie, {"focus": "엔비디아 금호"})
        self.tool(cookie, {"focus": "엔비디아 금호"})
        self.assertEqual(sorted(self.searched), ["금호", "엔비디아"])                                                  # 두 번째 질문은 검색 없이

    def test_at_most_three_new_stocks_per_question(self):
        cookie = self.login("key-a")
        for sym in ("AAA", "BBB", "CCC", "DDD"):
            SEARCH[sym] = [{"symbol": sym, "name": f"{sym}사", "market": "NASDAQ", "currency": "USD"}]
            self.table[sym] = tf.f.clean_info(tf.info(), sym)
            self.addCleanup(SEARCH.pop, sym, None)
        result = self.tool(cookie, {"focus": "AAA BBB CCC DDD"})[1]["result"]
        self.assertEqual(len(result["items"]), 3)

    def test_korean_stocks_are_resolved_to_yahoo_symbols_and_not_judged(self):
        cookie = self.login("key-a")
        item = self.tool(cookie, {"focus": "삼성전자 PER"})[1]["result"]["items"][0]
        self.assertEqual((item["symbol"], item["verdict"]["label"], item["reason"]), ("005930", "hold", "korean"))        # 005930.KS로 조회, 판단은 보류

    def test_new_stocks_are_labelled_as_looked_up_not_as_watchlist(self):
        result = {"items": [{"symbol": "NVDA", "name": "엔비디아", "held": False, "extra": True, "weekly_change_pct": 3.1, "headlines": [], "news_error": False},
                            {"symbol": "W", "name": "관심", "held": False, "extra": False, "weekly_change_pct": 1.0, "headlines": [], "news_error": False},
                            {"symbol": "H", "name": "보유", "held": True, "weekly_change_pct": 2.0, "headlines": [], "news_error": False}], "omitted": 0}
        answer = compose_agent_answer({"get_weekly_news": format_tool_result("get_weekly_news", result)["result"]})
        labels = [r[0] for r in answer["tables"][0]["rows"]]
        self.assertEqual(labels, ["엔비디아 (NVDA) · 조회", "관심 (W) · 관심", "보유 (H)"])

    def test_the_question_counts_as_pointing_at_a_stock_for_routing(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            req = mock.Mock(session=server._sessions[next(iter(server._sessions))], store=server.store_for(next(iter(server._sessions.values()))))
            info = server.focus_info(req, "엔비디아 어때?")
        self.assertTrue(info["found"])
        self.assertEqual(info["unknown"], [])
        self.assertEqual(plan_agent_query("엔비디아 어때?")["supported"], False)                                      # 키워드는 없지만 종목을 가리키므로 종목 종합으로 간다


if __name__ == "__main__":
    unittest.main()
