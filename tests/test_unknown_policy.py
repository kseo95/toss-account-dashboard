"""못 답하는 질문의 정책(unknown_policy.py + 서버): 기능 없음·주문·종목 못 찾음·못 알아들음을 각각 다르게 알리고, 대안 질문을 주고, 엉뚱한 종합 답을 하지 않는다."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import test_storage_http as storage_http
import unknown_policy as up


class MatchLimitTest(unittest.TestCase):
    def test_topics_without_a_tool(self):
        cases = {"삼성전자 배당 얼마나 늘었어?": "배당", "엔비디아 스토캐스틱 알려줘": "기술적 보조지표", "테슬라 목표주가 얼마야": "목표주가·애널리스트", "애플 영업이익 추이": "실적·재무제표",
                 "내 환차손익 알려줘": "환차손익", "공매도 많은 종목": "공매도·옵션·신용", "절세 방법 알려줘": "절세·신고", "내일 코스피 몇퍼 오를까": "가격 예측"}
        for question, topic in cases.items():
            got = up.match_limit(question)
            self.assertEqual((got["kind"], got["topic"]), ("no_feature", topic), question)
            self.assertTrue(got["alternatives"])

    def test_trade_requests_are_refused(self):
        for question in ("테슬라 100주 사줘", "전부 팔아줘", "삼성전자 매수해줘", "주문 넣어줘"):
            self.assertEqual(up.match_limit(question)["kind"], "trade", question)

    def test_questions_that_have_tools_are_not_limits(self):
        for question in ("금이 왜 고점 대비 빠졌어?", "엔비디아 PER 어때?", "엔비디아 RSI 과매수야?", "TSLA MACD 골든크로스 났어?", "볼린저밴드 상단 돌파했어?", "이번 주 무엇을 확인해야 해?", "미국 금리가 오르면 금과 나스닥은 어떻게 됨?", "전부 팔면 세금은?", "리밸런싱 계획 보여줘"):
            self.assertIsNone(up.match_limit(question), question)


class RespondTest(unittest.TestCase):
    def test_no_feature_says_so_and_offers_alternatives_for_the_named_stock(self):
        r = up.respond("삼성전자 배당", limit=up.match_limit("삼성전자 배당"), names=["GLDM"], subject=["삼성전자"])
        self.assertEqual((r["kind"], r["headline"]), ("no_feature", "배당 기능은 아직 없습니다"))
        self.assertIn("배당 내역이나 배당률 추이를 보는 기능은 아직 없습니다", r["bullets"][0])
        self.assertEqual(r["suggestions"], ["삼성전자 PER PBR ROE 알려줘", "삼성전자 고평가야?"])                    # 질문에 적은 종목이 우선, 내 종목이 다음

    def test_trade_is_refused_with_the_plan_as_the_alternative(self):
        r = up.respond("사줘", limit=up.match_limit("테슬라 사줘"))
        self.assertEqual((r["kind"], r["suggestions"]), ("trade", ["리밸런싱 계획 보여줘"]))
        self.assertIn("읽기 전용", r["bullets"][0])

    def test_not_found_asks_to_check_the_name(self):
        r = up.respond("ZZZQ 고평가야?", unknown=["ZZZQ"], names=["GLDM"])
        self.assertEqual((r["kind"], r["headline"]), ("not_found", "종목을 찾지 못했습니다"))
        self.assertIn("'ZZZQ'을(를) 종목으로 찾지 못했습니다", r["bullets"][0])
        self.assertIn("엔비디아 PER 어때?", r["suggestions"])

    def test_unclear_with_candidates_asks_back_and_without_lists_capabilities(self):
        asked = up.respond("꼭대기에서 왜 내려왔어?", candidates=[{"label": "고점 대비 낙폭 원인", "example": "금이 왜 고점 대비 빠졌어?"}])
        self.assertEqual(asked["kind"], "unclear")
        self.assertIn("혹시 이런 질문인가요?", asked["bullets"][0])
        self.assertIn("· 고점 대비 낙폭 원인", asked["bullets"])
        self.assertEqual(asked["suggestions"][0], "금이 왜 고점 대비 빠졌어?")
        lost = up.respond("오늘 점심 뭐 먹지")
        self.assertEqual(lost["headline"], "아직 답할 수 없는 질문입니다")
        self.assertEqual(len([b for b in lost["bullets"] if b.startswith("· ")]), 3)                               # 지금 가능한 것 3묶음
        self.assertTrue(all("{n}" not in s for s in lost["suggestions"]))
        self.assertEqual(len(lost["suggestions"]), len(set(lost["suggestions"])))                                 # 중복 없음


class UnknownHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        server._extra_cache.clear()
        self.addCleanup(server._extra_cache.clear)

    def ask(self, cookie, question, search=None):
        results = {"삼성전자": [{"symbol": "005930", "name": "삼성전자", "market": "KOSPI", "currency": "KRW"}]} if search is None else search
        with mock.patch.object(server, "fetch_account_snapshot", return_value={"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": [
                {"symbol": "GLDM", "name": "GLDM", "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": 100}}]}), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None), \
                mock.patch.object(server, "search_stocks", side_effect=lambda token, query, limit=8: results.get(query, [])):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_a_named_stock_with_a_missing_feature_gets_no_stock_summary_just_the_honest_answer(self):
        cookie = self.login("key-a")
        status, payload, _ = self.ask(cookie, "삼성전자 배당 얼마나 늘었어?")
        self.assertEqual(status, 200)
        self.assertEqual(payload["executions"], [])                                                               # 종목 종합(뉴스·요인…)으로 엉뚱하게 가지 않는다
        answer = payload["answer"]
        self.assertEqual((answer["unknown_kind"], answer["headline"]), ("no_feature", "배당 기능은 아직 없습니다"))
        self.assertEqual(answer["suggestions"], ["삼성전자 PER PBR ROE 알려줘", "삼성전자 고평가야?"])
        self.assertNotIn("teach", answer)                                                                         # 기능이 없다고 알린 질문엔 고르기 버튼을 주지 않는다

    def test_trade_request(self):
        cookie = self.login("key-a")
        answer = self.ask(cookie, "GLDM 10주 사줘")[1]["answer"]
        self.assertEqual((answer["unknown_kind"], answer["suggestions"]), ("trade", ["리밸런싱 계획 보여줘"]))

    def test_a_question_nobody_understands_lists_what_is_possible_and_keeps_the_teach_buttons(self):
        cookie = self.login("key-a")
        payload = self.ask(cookie, "오늘 점심 뭐 먹지")[1]
        answer = payload["answer"]
        self.assertEqual((answer["unknown_kind"], payload["plan"]["supported"]), ("unclear", False))
        self.assertEqual(sum(1 for b in answer["bullets"] if b.startswith("· ")), 3)
        self.assertIn("teach", answer)                                                                            # 못 알아들은 질문은 가르칠 수 있다
        self.assertTrue(any(s.startswith("GLDM") for s in answer["suggestions"]))                                  # 내 종목으로 만든 예시

    def test_a_similar_looking_question_asks_back_with_candidates(self):
        cookie = self.login("key-a")

        class Stub:                                                                                                # 확신(0.30)은 못 하지만 어렴풋이 비슷한 의도 둘
            def scores(self, question):
                return [("drawdown", 0.22), ("holding", 0.21), ("outlook", 0.15), ("tax", 0.05)]

            def classify(self, question, *a, **k):
                return None
        with mock.patch.object(server, "intent_matcher_for", return_value=(Stub(), {})), \
                mock.patch.object(server, "plan_agent_query", return_value={"supported": False, "intents": [], "tools": []}):          # 키워드로는 못 고르는 문장
            payload = self.ask(cookie, "꼭대기에서 왜 내려왔어?")[1]
        answer = payload["answer"]
        self.assertEqual(answer["unknown_kind"], "unclear")
        self.assertEqual(answer["headline"], "질문을 정확히 이해하지 못했습니다")
        self.assertEqual([b for b in answer["bullets"] if b.startswith("· ")], ["· 고점 대비 낙폭 원인", "· 앞으로의 뷰"])    # 종목 종합(holding)은 후보에서 뺀다, 많아야 2개
        self.assertEqual(answer["suggestions"][0], server.DEFAULT_EXAMPLES["drawdown"][0])                       # 후보의 예시 질문을 누를 수 있게
        self.assertIn("teach", answer)

    def test_supported_questions_with_a_missing_part_keep_their_answer_and_add_a_note(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "execute_agent_tool", side_effect=lambda req, name, args=None: {"result": {}}):
            payload = self.ask(cookie, "전부 팔면 세금은? 절세 방법도 알려줘")[1]
        self.assertEqual(payload["plan"]["tools"], ["estimate_liquidation_tax"])
        self.assertNotIn("unknown_kind", payload["answer"])
        self.assertTrue(any(n.startswith("※ 세금 신고 절차나 절세 전략") for n in payload["answer"]["notes"]))

    def test_an_unresolvable_ticker_in_a_supported_question_is_reported(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "execute_agent_tool", side_effect=lambda req, name, args=None: {"result": {}}):
            payload = self.ask(cookie, "ZZZQ 고평가야?", search={})[1]
        self.assertTrue(any("ZZZQ을(를) 종목으로 찾지 못했습니다" in n and "전체를 보여줍니다" in n for n in payload["answer"]["notes"]))

    def test_misses_are_recorded_with_their_kind_when_enabled(self):
        cookie = self.login("key-a")
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_misses(store, True, [])
        self.ask(cookie, "삼성전자 배당 얼마나 늘었어?")
        self.ask(cookie, "오늘 점심 뭐 먹지")
        kinds = [(m["kind"]) for m in server.load_misses(store)["items"]]
        self.assertEqual(kinds, ["no_feature:배당", "unplaced"])


if __name__ == "__main__":
    unittest.main()
