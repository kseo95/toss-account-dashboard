"""리밸런싱 매수·매도 목록의 종목별 주수: 설정 저장 → 보유 조회 → (필요한 종목만) 현재가 조회 → Agent 답변."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import test_storage_http as storage_http


def moves_rows(answer):
    table = next(tb for tb in answer["tables"] if tb["title"] == "매수 & 매도 목록")
    return {r[0]: r for r in table["rows"]}, table


def signal_rows(answer):
    table = next(tb for tb in answer["tables"] if tb["title"] == "주봉 MA 신호 (진입 비율)")
    return {r[0]: r for r in table["rows"]}, table


class TradeQuantitiesHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.price_calls = []

        def prices(token, symbols):
            self.price_calls.append(list(symbols))
            return {"SPYM": 80.0}  # USD

        self.strategy_calls = []

        def strategy_rows(token, templates, assignments, excluded, state, symbols):
            self.strategy_calls.append({"symbols": list(symbols), "assignments": dict(assignments), "excluded": set(excluded)})
            state["SPYM"] = {"cycle": 1}  # 읽기 전용 확인용: 계산 중 상태를 바꿔도 저장되면 안 된다
            return [self.signal_row(s) for s in symbols if s in self.signal_entry], True

        patches = {
            "compute_strategy_rows": mock.Mock(side_effect=strategy_rows),
            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {s: {"name": f"{s} 이름", "currency": "USD"} for s in syms}),
            "get_prices": mock.Mock(side_effect=prices),
        }
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    signal_entry = {"SPYM": 25.0}  # 종목 → 진입 비율. 빈 사전이면 신호 계산 실패(행 없음)

    def signal_row(self, symbol):
        return {"symbol": symbol, "ok": True, "entry_pct": self.signal_entry[symbol], "cycle": 1, "stage_count": 1, "stage_total": 4,
                "steps": [{"id": "a", "label": "MA60주 -3.0%", "broken": True, "ok": True, "triggered": True},
                          {"id": "b", "label": "MA120주 +5.0%", "broken": False, "ok": True, "triggered": False}]}

    def snapshot(self):
        items = [
            {"symbol": "UFO", "name": "UFO", "currency": "USD", "marketCountry": "US", "quantity": 4, "marketValue": {"amount": 100}},
            {"symbol": "NASA", "name": "NASA", "currency": "USD", "marketCountry": "US", "quantity": 10, "marketValue": {"amount": 300}},
        ]
        return {"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": items}

    def setup_config(self, cookie):
        status, payload, _ = self.request("POST", "/api/rebalance-config", {
            "targets": [{"label": "우주", "symbols": ["UFO", "NASA"], "target_pct": 40},
                        {"label": "S&P", "symbols": ["SPYM", "SH"], "target_pct": 60}],
            "default_rest_target_pct": 5, "krw_target_pct": 50}, cookie=cookie)
        self.assertEqual(status, 200, payload)

    def run_tool(self, cookie):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", {"name": "get_portfolio_overview"}, cookie=cookie)

    def test_tool_result_has_allocations_and_fetches_only_unheld_buy_symbol(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        status, payload, _ = self.run_tool(cookie)
        self.assertEqual(status, 200, payload)
        rows = {r["label"]: r for r in payload["result"]["rebalancing"]["rows"]}
        sell = {a["symbol"]: a for a in rows["우주"]["allocations"]}
        self.assertEqual(rows["우주"]["action"], "sell")
        self.assertAlmostEqual(sell["UFO"]["amount_krw"], 60_000.0)    # 총 40만원 중 40% 목표 → 24만원 매도, 보유 비율 1:3
        self.assertEqual(sell["UFO"]["shares"], 2.4)                   # 60,000원 / 25,000원
        self.assertEqual(sell["NASA"]["shares"], 6.0)                  # 180,000원 / 30,000원
        buy = rows["S&P"]["allocations"]
        self.assertEqual([a["symbol"] for a in buy], ["SPYM"])         # 보유가 없으니 설정의 첫 종목, SH(인버스)는 아님
        self.assertEqual(buy[0]["shares"], 3.0)                        # 240,000원 / (80달러 × 1,000원)
        self.assertEqual(self.price_calls, [["SPYM"]], "보유 종목의 단가는 평가금액/수량으로 구하고, 시세는 필요한 종목만 받는다")
        self.assertEqual(payload["result"]["rebalancing"]["quote_errors"], [])

    def test_rule_based_answer_lists_symbols_and_shares(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "리밸런싱 알려줘"}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        answer = payload["answer"]
        rows, table = moves_rows(answer)
        self.assertEqual(rows["S&P"], ["S&P", "매수", "0% → 60%", "240,000", "60,000", "180,000", "SPYM 0.75주"])
        self.assertEqual(rows["우주"], ["우주", "매도", "100% → 40%", "240,000", "-", "-", "UFO 2.40주 + NASA 6.00주"])
        self.assertIn("설정한 목표 비중 기준으로 총 매수 240,000원, 총 매도 240,000원이 필요합니다.", answer["bullets"])
        self.assertTrue(any("보유 평가금액 비율" in x for x in answer["limitations"]))

    def test_strategy_entry_ratio_scales_buy_amount_and_leaves_wait(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        status, payload, _ = self.run_tool(cookie)
        self.assertEqual(status, 200, payload)
        rebalancing = payload["result"]["rebalancing"]
        row = {r["label"]: r for r in rebalancing["rows"]}["S&P"]
        part = row["allocations"][0]
        self.assertEqual(part["signal_status"], "ok")
        self.assertAlmostEqual(row["buy_now_krw"], 60_000.0)             # 240,000원 × 진입 25%
        self.assertAlmostEqual(row["wait_krw"], 180_000.0)
        self.assertEqual(part["shares_now"], 0.75)                       # 지금 살 60,000원 / 80,000원
        self.assertEqual(part["shares"], 3.0)                            # 전액 기준 주수도 남아 있다
        self.assertTrue(rebalancing["scaling"]["scaled"])
        self.assertAlmostEqual(rebalancing["scaling"]["buy_now_total_krw"], 60_000.0)
        self.assertAlmostEqual(rebalancing["scaling"]["wait_total_krw"], 180_000.0)
        # 매도 행은 전략으로 줄이지 않는다
        sell = {r["label"]: r for r in rebalancing["rows"]}["우주"]
        self.assertIsNone(sell["buy_now_krw"])
        # 보유하지 않은 종목에는 기본 전략이 배정되어 평가된다
        self.assertEqual(self.strategy_calls[0]["symbols"], ["SPYM"])
        self.assertEqual(self.strategy_calls[0]["assignments"]["SPYM"], "ma-default")

    def test_answer_text_shows_now_wait_and_signal_section(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "리밸런싱 알려줘"}, cookie=cookie)
        answer = payload["answer"]
        rows, table = moves_rows(answer)
        self.assertEqual(rows["S&P"], ["S&P", "매수", "0% → 60%", "240,000", "60,000", "180,000", "SPYM 0.75주"])
        self.assertEqual(table["footer"][0], ["매수 합계", "", "", "240,000", "60,000", "180,000", ""])   # 지금/대기 합계
        self.assertIn("주봉 MA 전략을 반영하면 지금 매수는 60,000원, 나머지 180,000원은 진입 조건이 될 때까지 대기합니다.", answer["bullets"])
        sig, sig_table = signal_rows(answer)
        self.assertEqual(sig_table["columns"], ["종목", "진입 비율", "돌파 단계", "MA60주", "MA120주", "비고"])
        self.assertEqual(sig["SPYM"], ["SPYM", "25.00%", "1/4", "-3.0% 돌파 중", "+5.0%", ""])
        self.assertEqual(rows["우주"][6], "UFO 2.40주 + NASA 6.00주")  # 매도 줄은 그대로
        self.assertTrue(any("진입 비율을 곱한 값" in x for x in answer["limitations"]))

    def test_insufficient_history_step_gets_name_from_template_not_id(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        row = self.signal_row("SPYM")
        row["steps"] = [{"id": "s1", "label": "MA60주 +0.4%", "broken": False, "ok": True, "triggered": False},
                        {"id": "s4", "broken": False, "ok": False}]  # 평가 불가 단계에는 label이 없다
        row["recovered"] = False
        with mock.patch.object(server, "compute_strategy_rows", mock.Mock(return_value=([row], False))):
            status, payload, _ = self.run_tool(cookie)
        self.assertEqual(status, 200, payload)
        signal = payload["result"]["rebalancing"]["signals"][0]
        self.assertEqual([s["label"] for s in signal["steps"]], ["MA60주 +0.4%", "MA240주"])  # ma-default의 s4 = MA240주
        self.assertEqual([s["ok"] for s in signal["steps"]], [True, False])
        self.assertFalse(signal["recovered"])

    def test_agent_never_saves_strategy_state(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        self.run_tool(cookie)
        store = server.store_for(next(iter(server._sessions.values())))
        self.assertIsNone(store.read_json("strategy_state.json"), "에이전트는 읽기 전용이라 돌파 기록을 저장하지 않는다")

    def test_excluded_symbol_keeps_full_amount(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        store = server.store_for(next(iter(server._sessions.values())))
        data = server.load_strategy_templates(store)
        data["excluded_symbols"] = ["SPYM"]
        server.save_strategy_templates(store, data)
        status, payload, _ = self.run_tool(cookie)
        rebalancing = payload["result"]["rebalancing"]
        part = {r["label"]: r for r in rebalancing["rows"]}["S&P"]["allocations"][0]
        self.assertEqual(part["signal_status"], "excluded")
        self.assertAlmostEqual(part["buy_now_krw"], 240_000.0)           # 줄이지 않는다
        self.assertAlmostEqual(part["wait_krw"], 0.0)
        self.assertFalse(rebalancing["scaling"]["scaled"])
        self.assertEqual(self.strategy_calls, [], "전략 제외 종목은 시세·이동평균을 조회하지 않는다")
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            answer = self.request("POST", "/api/agent/query", {"question": "리밸런싱 알려줘"}, cookie=cookie)[1]["answer"]
        rows, _ = moves_rows(answer)
        self.assertEqual(rows["S&P"], ["S&P", "매수", "0% → 60%", "240,000", "240,000", "0", "SPYM 3.00주"])   # 줄이지 않는다
        sig, _ = signal_rows(answer)
        self.assertEqual(sig["SPYM"][1:3], ["-", "-"])
        self.assertIn("전략 제외", sig["SPYM"][-1])

    def test_signal_failure_keeps_full_amount_and_says_so(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        with mock.patch.object(server, "compute_strategy_rows", mock.Mock(side_effect=server.requests.ConnectionError("down"))):
            status, payload, _ = self.run_tool(cookie)
            self.assertEqual(status, 200, payload)
            part = {r["label"]: r for r in payload["result"]["rebalancing"]["rows"]}["S&P"]["allocations"][0]
            self.assertEqual(part["signal_status"], "unavailable")
            self.assertAlmostEqual(part["buy_now_krw"], 240_000.0)
            with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                    mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
                answer = self.request("POST", "/api/agent/query", {"question": "리밸런싱 알려줘"}, cookie=cookie)[1]["answer"]
        sig, _ = signal_rows(answer)
        self.assertIn("신호 확인 불가", sig["SPYM"][-1])
        rows, _ = moves_rows(answer)
        self.assertEqual(rows["S&P"][3:6], ["240,000", "240,000", "0"])

    def test_price_lookup_failure_keeps_plan_and_marks_shares_unavailable(self):
        cookie = self.login("key-a")
        self.setup_config(cookie)
        with mock.patch.object(server, "get_prices", mock.Mock(side_effect=server.requests.ConnectionError("down"))):
            status, payload, _ = self.run_tool(cookie)
        self.assertEqual(status, 200, payload)
        rebalancing = payload["result"]["rebalancing"]
        self.assertTrue(rebalancing["ready"])
        self.assertEqual(rebalancing["quote_errors"], ["SPYM"])
        buy = {r["label"]: r for r in rebalancing["rows"]}["S&P"]["allocations"][0]
        self.assertIsNone(buy["shares"])
        sells = {r["label"]: r for r in rebalancing["rows"]}["우주"]["allocations"]
        self.assertTrue(all(a["shares"] is not None for a in sells), "보유 종목의 주수는 시세 조회와 무관하게 계산된다")


if __name__ == "__main__":
    unittest.main()
