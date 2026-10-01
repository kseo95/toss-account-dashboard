"""Agent 도구 계약: 허용 목록, 비민감 결과 축약, 한계 표시."""
from __future__ import annotations

import unittest

from helpers import server
from agent_tools import TOOL_DEFINITIONS, compose_agent_answer, format_tool_result, plan_agent_query


class AgentToolsTest(unittest.TestCase):
    def test_only_three_read_only_tools_are_exposed(self):
        self.assertEqual({t["name"] for t in TOOL_DEFINITIONS}, {
            "get_portfolio_overview", "get_weekly_risk_report", "estimate_liquidation_tax",
        })
        self.assertTrue(all(t["read_only"] is True for t in TOOL_DEFINITIONS))
        self.assertNotIn("order", " ".join(t["name"] for t in TOOL_DEFINITIONS))

    def test_portfolio_result_is_small_and_has_no_credentials(self):
        raw = {
            "stocks": [
                {"심볼": "AAA", "종목": "에이", "통화": "USD", "평가금액(원)": 700,
                 "손익(원)": 100, "지분율(%)": 70},
                {"심볼": "BBB", "종목": "비", "통화": "KRW", "평가금액(원)": 200,
                 "손익(원)": -10, "지분율(%)": 20},
            ],
            "cash": {"stocks": []},
            "totals": {"eval_krw": 1000, "profit_loss_krw": 90, "rate_pct": 9.89},
            "currency_split": {"krw_pct": 30, "usd_pct": 70, "krw_target_pct": 40, "usd_target_pct": 60},
            "rebalance_plan": {"ready": True, "target_total_pct": 100, "buy_total_krw": 100,
                               "sell_total_krw": 100, "rows": [{"label": "에이", "action": "sell",
                                                                   "difference_krw": -100}], "warnings": []},
            "app_key": "must-not-leak", "token": "must-not-leak",
        }
        out = format_tool_result("get_portfolio_overview", raw)
        self.assertEqual(out["result"]["largest_positions"][0]["symbol"], "AAA")
        self.assertEqual(out["result"]["position_count"], 2)
        self.assertTrue(out["result"]["rebalancing"]["ready"])
        self.assertNotIn("must-not-leak", repr(out))
        self.assertNotIn("app_key", repr(out))

    def test_weekly_result_keeps_signals_and_limitations(self):
        raw = {
            "week_end": "2026-09-25", "generated_at": "2026-09-26 10:00",
            "groups": [{"type": "my_account", "account": {"eval_krw": 1000, "week_krw_pct": -1.2},
                        "comments": ["계좌 약세"]}],
            "summary": {"all_broken": [{"symbol": "AAA", "label": "에이"}],
                        "crosses": [{"symbol": "BBB", "label": "비", "period": 60, "dir": "down"}],
                        "comments": ["시장 약세"]},
            "errors": ["CCC 데이터 없음"],
        }
        out = format_tool_result("get_weekly_risk_report", raw)["result"]
        self.assertEqual(out["all_ma_broken"][0]["symbol"], "AAA")
        self.assertEqual(out["crosses"][0]["direction"], "down")
        self.assertIn("계좌 약세", out["rule_based_comments"])
        self.assertTrue(out["limitations"])

    def test_tax_result_marks_estimate_and_incomplete_data(self):
        raw = {
            "summary": {"tax_if_sell_all_krw": 300, "gain_tax_if_sell_all_krw": 250,
                        "domestic_tax_if_sell_all_krw": 50, "deduction_left_krw": 0,
                        "financial_income_ytd_krw": 100},
            "rows": [{"symbol": "AAA", "name": "에이", "tax_krw": 300, "notes": ["근사"]}],
            "realized_source": "auto", "income_source": "manual",
            "realized_note": "일부만 합산했어요.", "income_note": None,
        }
        out = format_tool_result("estimate_liquidation_tax", raw)["result"]
        self.assertEqual(out["summary"]["estimated_tax_krw"], 300.0)
        self.assertIn("일부만 합산했어요.", out["warnings"])
        self.assertIn("추정치", out["limitations"][0])

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            format_tool_result("place_order", {})

    def test_routes_require_login(self):
        self.assertTrue(server.ROUTES[("GET", "/api/agent/tools")].auth)
        self.assertTrue(server.ROUTES[("POST", "/api/agent/tool")].auth)
        self.assertTrue(server.ROUTES[("POST", "/api/agent/query")].auth)

    def test_query_plan_selects_weekly_tools_without_duplicates(self):
        plan = plan_agent_query("이번 주 포트폴리오에서 무엇을 점검해야 해?")
        self.assertEqual(plan["tools"], ["get_portfolio_overview", "get_weekly_risk_report"])
        self.assertTrue(plan["supported"])

    def test_query_plan_selects_rebalance_and_tax_tools(self):
        self.assertEqual(plan_agent_query("리밸런싱 계획 보여줘")["tools"], ["get_portfolio_overview"])
        self.assertEqual(plan_agent_query("전부 팔면 세금은?")["tools"], ["estimate_liquidation_tax"])

    def test_unknown_question_returns_supported_examples_without_running_tools(self):
        plan = plan_agent_query("오늘 점심 뭐 먹지?")
        self.assertFalse(plan["supported"])
        answer = compose_agent_answer({})
        self.assertIn("리밸런싱", answer["bullets"][0])

    def test_composed_answer_uses_tool_numbers_and_keeps_limitations(self):
        answer = compose_agent_answer({
            "get_portfolio_overview": {
                "totals": {"value_krw": 1000, "return_pct": 5},
                "largest_positions": [{"name": "에이", "weight_pct": 60}],
                "rebalancing": {"ready": True, "buy_total_krw": 100, "sell_total_krw": 100,
                                "rows": [{"label": "에이", "action": "sell", "difference_krw": -100}],
                                "warnings": ["주문 아님"]},
                "limitations": ["현재 스냅샷"],
            },
        })
        self.assertTrue(any("1,000원" in item for item in answer["bullets"]))
        self.assertTrue(any("매도 100원" in item for item in answer["bullets"]))
        self.assertEqual(answer["limitations"], ["현재 스냅샷", "주문 아님"])


if __name__ == "__main__":
    unittest.main()
