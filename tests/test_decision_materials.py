"""'사도 돼?' 같은 매매 판단 질문: 결론 없이 판단 재료 도구 묶음으로 계획하고, 답변 맨 앞에 알리며, 사라·팔라는 말은 모델 답에서 거른다."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401
import local_model as lm
import test_storage_http as storage_http
import unknown_policy as up
from agent_tools import DECISION_NOTICE, DECISION_TOOLS, INTENT_PLANS, is_decision_query, plan_agent_query, plan_for_intent


class PlanTest(unittest.TestCase):
    def test_decision_questions_plan_the_four_material_tools(self):
        for q in ("지금 엔비디아 사도 돼?", "UFO 팔까?", "TLT 계속 들고 있어도 돼?", "금 더 담아도 될까", "이 종목 손절해야 해?", "추가매수 해도 돼?"):
            plan = plan_agent_query(q)
            self.assertEqual((plan["intents"], plan["tools"]), (["decision"], list(DECISION_TOOLS)), q)
        self.assertEqual(plan_for_intent("decision")["tools"], list(DECISION_TOOLS))
        self.assertEqual(INTENT_PLANS["decision"], list(DECISION_TOOLS))

    def test_other_questions_keep_their_own_plans(self):
        self.assertEqual(plan_agent_query("엔비디아 고평가야?")["tools"], ["get_valuation"])
        self.assertEqual(plan_agent_query("내가 정한 원칙대로 사고 있어?")["tools"], ["get_my_rules"])
        self.assertFalse(is_decision_query("엔비디아 PER 알려줘"))

    def test_orders_are_still_refused_before_any_planning(self):
        self.assertTrue(up.match_limit("엔비디아 사줘")["exclusive"])
        self.assertIsNone(up.match_limit("지금 엔비디아 사도 돼?"))                                                  # 질문이지 주문이 아니다


class AnswerTest(storage_http.HttpTestBase, unittest.TestCase):
    def test_the_notice_comes_first_and_the_four_tools_run(self):
        cookie = self.login("key-a")
        ran = []

        def execute(req, name, args=None):
            ran.append(name)
            return {"result": {}}
        with mock.patch.object(server, "execute_agent_tool", side_effect=execute), \
                mock.patch.object(server, "fetch_account_snapshot", return_value={"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": []}), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "지금 엔비디아 사도 돼?"}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answer"]["bullets"][0], DECISION_NOTICE)
        self.assertEqual(ran, list(DECISION_TOOLS))
        self.assertNotIn("사세요", "".join(payload["answer"]["bullets"]))

    def test_a_normal_question_has_no_notice(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "execute_agent_tool", return_value={"result": {}}), \
                mock.patch.object(server, "fetch_account_snapshot", return_value={"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": []}), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            payload = self.request("POST", "/api/agent/query", {"question": "섹터 노출 보여줘"}, cookie=cookie)[1]
        self.assertNotIn(DECISION_NOTICE, payload["answer"]["bullets"])


class ModelGuardTest(unittest.TestCase):
    FACTS = "엔비디아 PER 45.2 업종 중앙값 30.1 RSI 68.4"

    def test_buy_or_sell_verdicts_are_rejected_but_plain_facts_pass(self):
        for bad in ("지금 사세요. PER 45.2입니다.", "RSI 68.4라서 파는 게 좋습니다.", "PER 45.2로 매수 적기입니다.", "지금은 매수하세요 PER 45.2"):
            self.assertIsNone(lm.usable_final(bad, self.FACTS), bad)
        self.assertIsNotNone(lm.usable_final("PER 45.2는 업종 중앙값 30.1보다 높고 RSI는 68.4입니다.", self.FACTS))

    def test_system_prompt_tells_the_model_to_gather_materials_without_a_verdict(self):
        self.assertIn("판단 재료 도구 4개", lm.SYSTEM)
        for name in ("get_valuation", "get_indicators", "get_drawdown_analysis", "get_my_rules"):
            self.assertIn(name, lm.SYSTEM)


if __name__ == "__main__":
    unittest.main()
