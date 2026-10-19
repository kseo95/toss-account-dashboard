"""범용 자산 조회(query_data): 현금까지 포함한 자산 표 만들기, 묶기·합계·조건, 잘못된 명령 거절, 답변 그래프, 서버 도구 경로."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401
import data_query as dq
import local_model as lm
import test_storage_http as storage_http
from agent_tools import compose_agent_answer, format_tool_result


def holdings():
    stock = lambda sym, name, cur, ev, ret: {"심볼": sym, "종목": name, "통화": cur, "평가금액(원)": ev, "수익률(%)": ret}
    return {"totals": {"eval_krw": 1000.0},
            "stocks": [stock("UFO", "UFO", "USD", 300.0, -20.0), stock("NASA", "NASA", "USD", 200.0, -30.0), stock("TLT", "TLT", "USD", 100.0, 5.0), stock("SEDG", "솔라엣지", "USD", 50.0, -6.0)],
            "cash": {"krw": 150.0, "usd": 0.1, "usd_krw_rate": 1000.0, "stocks": [stock("SGOV", "SGOV", "USD", 100.0, 1.0)]}}


CONFIG = {"targets": [{"label": "우주", "symbols": ["UFO", "NASA"], "target_pct": 10.0}, {"label": "채권", "symbols": ["TLT"], "target_pct": 20.0}], "cash_target_pct": 5.0}


class BuildAssetsTest(unittest.TestCase):
    def test_cash_lines_are_part_of_the_table_with_total_based_weights(self):
        rows = dq.build_assets(holdings(), CONFIG)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual((by_name["UFO"]["category"], by_name["UFO"]["target_pct"], by_name["UFO"]["weight_pct"]), ("우주", 10.0, 30.0))
        self.assertEqual(by_name["솔라엣지"]["category"], "미분류")
        self.assertEqual((by_name["원화 예수금"]["category"], by_name["원화 예수금"]["value_krw"], by_name["원화 예수금"]["kind"]), ("현금", 150.0, "현금"))
        self.assertEqual((by_name["달러 예수금"]["value_krw"], by_name["달러 예수금"]["currency"]), (100.0, "USD"))        # 0.1달러 × 환율 1000
        self.assertEqual((by_name["SGOV"]["kind"], by_name["SGOV"]["category"], by_name["SGOV"]["target_pct"]), ("현금취급종목", "현금", 5.0))
        self.assertEqual(sum(r["weight_pct"] for r in rows), 100.0)                                                     # 현금까지 합치면 전체


class RunQueryTest(unittest.TestCase):
    def setUp(self):
        self.rows = dq.build_assets(holdings(), CONFIG)

    def test_group_by_category_with_cash_gives_share_of_total_and_target(self):
        out = dq.run_query(self.rows, {"group_by": "category"})
        by = {r["label"]: r for r in out["rows"]}
        self.assertEqual((by["우주"]["value"], by["우주"]["share_pct"], by["우주"]["target_pct"]), (500.0, 50.0, 10.0))
        self.assertEqual((by["현금"]["value"], by["현금"]["share_pct"]), (350.0, 35.0))                                  # 원화 150 + 달러 100 + SGOV 100
        self.assertEqual(out["rows"][0]["label"], "우주")                                                                # 큰 순서
        self.assertEqual(out["grand_total"], 1000.0 + 0.0)

    def test_filter_metric_order_and_limit(self):
        out = dq.run_query(self.rows, {"group_by": "symbol", "field": "return_pct", "metric": "min", "filter_field": "kind", "filter_op": "=", "filter_value": "주식", "order": "asc", "limit": 2})
        self.assertEqual([(r["label"], r["value"]) for r in out["rows"]], [("NASA", -30.0), ("UFO", -20.0)])
        self.assertNotIn("share_pct", out["rows"][0])                                                                    # 수익률에는 전체 대비 비율을 붙이지 않는다
        usd = dq.run_query(self.rows, {"filter_field": "currency", "filter_value": "USD"})
        self.assertEqual(usd["rows"][0]["value"], 850.0)                                                                 # 달러 주식 650 + 달러 예수금 100 + SGOV 100
        count = dq.run_query(self.rows, {"group_by": "kind", "metric": "count"})
        self.assertEqual({r["label"]: r["value"] for r in count["rows"]}, {"주식": 4.0, "현금": 2.0, "현금취급종목": 1.0})
        num = dq.run_query(self.rows, {"filter_field": "weight_pct", "filter_op": ">=", "filter_value": "30"})
        self.assertEqual(num["matched"], 1)

    def test_invalid_commands_are_refused_not_executed(self):
        for bad in ({"group_by": "__class__"}, {"metric": "exec"}, {"field": "name"}, {"filter_field": "secret", "filter_op": "=", "filter_value": "x"},
                    {"filter_field": "kind", "filter_op": "like", "filter_value": "x"}, {"filter_field": "kind", "filter_op": "="}):
            self.assertIn("error", dq.run_query(self.rows, bad), bad)
        self.assertEqual(dq.run_query(self.rows, None)["rows"][0]["label"], "전체")                                       # 인자가 없으면 전체 합계


class ComposeQueryTest(unittest.TestCase):
    def answer(self, args):
        raw = dq.run_query(dq.build_assets(holdings(), CONFIG), args)
        return compose_agent_answer({"query_data": format_tool_result("query_data", raw)["result"]})

    def test_category_share_chart_shows_target_next_to_the_share_including_cash(self):
        out = self.answer({"group_by": "category"})
        chart = out["charts"][0]
        self.assertEqual(chart["labels"][:2], ["우주", "현금"])
        self.assertEqual([d["label"] for d in chart["datasets"]], ["목표 비중", "전체 대비 비중"])
        self.assertIn("현금", "\n".join(out["bullets"]))
        self.assertIn("50.0%", "\n".join(out["bullets"]))

    def test_errors_and_empty_results_are_explained(self):
        self.assertIn("실행하지 못했습니다", self.answer({"metric": "exec"})["bullets"][0])
        self.assertIn("조건에 맞는 항목이 없습니다", self.answer({"filter_field": "kind", "filter_value": "없는종류"})["bullets"][0])


class ModelSideTest(unittest.TestCase):
    def test_flat_arguments_pass_the_schema_filter_and_bad_values_are_dropped(self):
        args = lm.clean_args("query_data", {"group_by": "category", "metric": "sum", "field": "weight_pct", "filter_field": "kind", "filter_op": "!=", "filter_value": "주식",
                                            "limit": 5, "evil": "x"}, "q")
        self.assertEqual(args, {"group_by": "category", "metric": "sum", "field": "weight_pct", "filter_field": "kind", "filter_op": "!=", "filter_value": "주식", "limit": 5})
        self.assertEqual(lm.clean_args("query_data", {"group_by": "password", "metric": "drop"}, "q"), {})
        self.assertIn("query_data", lm.FOLLOW_UP_TOOLS)
        self.assertIn("query_data", lm.HINTS)


class QueryToolHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def test_tool_route_runs_the_query_on_the_live_asset_table(self):
        cookie = self.login("key-a")
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_rebalance_config(store, [{"label": "우주", "symbols": ["UFO"], "target_pct": 10.0}], 5.0, 50.0, 5.0)
        snap = {"usd_krw": 1000.0, "cash_krw": 500000.0, "cash_usd": 0.0,
                "items": [{"symbol": "UFO", "name": "UFO", "currency": "USD", "marketCountry": "US", "quantity": 1, "marketValue": {"amount": 500}, "profitLoss": {"rateAfterCost": -0.1}}]}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=snap), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/tool", {"name": "query_data", "args": {"group_by": "category"}}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        rows = {r["label"]: r for r in payload["result"]["rows"]}
        self.assertEqual((rows["우주"]["share_pct"], rows["현금"]["share_pct"]), (50.0, 50.0))
        self.assertEqual(rows["현금"]["target_pct"], 5.0)
        self.assertNotIn("11100000001", repr(payload))


class TwoWayQueryTest(unittest.TestCase):
    def setUp(self):
        self.rows = dq.build_assets(holdings(), CONFIG)

    def test_second_group_makes_category_by_currency_cells(self):
        out = dq.run_query(self.rows, {"group_by": "category", "group_by2": "currency"})
        by = {r["label"]: r for r in out["rows"]}
        self.assertEqual(by["우주 · USD"]["value"], 500.0)
        self.assertEqual((by["현금 · KRW"]["value"], by["현금 · USD"]["value"]), (150.0, 200.0))                    # 달러 예수금 100 + SGOV 100
        self.assertEqual(by["우주 · USD"]["share_pct"], 50.0)
        self.assertNotIn("target_pct", by["우주 · USD"])                                                           # 칸이 쪼개지면 카테고리 목표와 비교하지 않는다

    def test_two_conditions_must_both_hold(self):
        args = {"group_by": "symbol", "filter_field": "kind", "filter_op": "=", "filter_value": "주식", "filter2_field": "return_pct", "filter2_op": "<", "filter2_value": "0"}
        out = dq.run_query(self.rows, args)
        self.assertEqual(sorted(r["label"] for r in out["rows"]), ["NASA", "SEDG", "UFO"])                       # 주식이면서 수익률 음수
        self.assertEqual(out["filter"], "kind = 주식 그리고 return_pct < 0")
        only_second = dq.run_query(self.rows, {"filter2_field": "currency", "filter2_value": "USD"})
        self.assertEqual(only_second["rows"][0]["value"], 850.0)                                                  # 첫 조건 없이 두 번째만 줘도 적용

    def test_bad_second_group_or_condition_is_refused(self):
        for bad in ({"group_by2": "currency"}, {"group_by": "category", "group_by2": "category"}, {"group_by": "category", "group_by2": "name"},
                    {"filter2_field": "kind", "filter2_op": "like", "filter2_value": "x"}, {"filter2_field": "kind", "filter2_op": "="}):
            self.assertIn("error", dq.run_query(self.rows, bad), bad)

    def test_answer_titles_both_groups_and_the_model_can_pass_the_new_arguments(self):
        raw = dq.run_query(self.rows, {"group_by": "category", "group_by2": "currency"})
        out = compose_agent_answer({"query_data": format_tool_result("query_data", raw)["result"]})
        self.assertIn("카테고리×통화별 평가금액 합계", out["bullets"][0])
        args = lm.clean_args("query_data", {"group_by": "category", "group_by2": "currency", "filter2_field": "kind", "filter2_op": "=", "filter2_value": "주식", "x": 1}, "q")
        self.assertEqual(args, {"group_by": "category", "group_by2": "currency", "filter2_field": "kind", "filter2_op": "=", "filter2_value": "주식"})


if __name__ == "__main__":
    unittest.main()
