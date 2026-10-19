"""과거 시세 가정 계산(history_calc)과 자산 표의 체결·배당 항목: 기준일 시세·환율 적용, 그때 실제 수량 되돌리기, 시세 없는 종목 제외, 서버 도구 경로."""
from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from helpers import portfolio, server  # noqa: F401
import data_query as dq
import history_calc as hc
import local_model as lm
import test_storage_http as storage_http
from agent_tools import compose_agent_answer, format_tool_result

TODAY = date(2026, 10, 19)
TARGET = date(2025, 10, 19)


def closes(first_close, last_close):
    return {date(2025, 10, 17): first_close, date(2026, 10, 16): last_close}


ROWS = [{"symbol": "UFO", "name": "UFO", "currency": "USD", "qty": 10.0, "value_now_krw": 15000.0},
        {"symbol": "069500", "name": "KODEX 200", "currency": "KRW", "qty": 5.0, "value_now_krw": 6000.0},
        {"symbol": "NEW", "name": "신규상장", "currency": "USD", "qty": 1.0, "value_now_krw": 900.0}]
CLOSES = {"UFO": closes(20.0, 22.0), "069500": closes(1000.0, 1200.0), "NEW": {date(2026, 9, 1): 5.0}}
FX = lambda day: 1000.0 if day < date(2026, 1, 1) else 1500.0  # noqa: E731
FILLS = [{"symbol": "UFO", "side": "BUY", "quantity": 4.0, "filled_at": "2026-02-03T10:00:00"}, {"symbol": "069500", "side": "SELL", "quantity": 2.0, "filled_at": "2026-03-01T10:00:00"}]


class PastValueTest(unittest.TestCase):
    def test_current_quantities_at_past_prices_and_fx(self):
        out = hc.past_value(ROWS, CLOSES, FX, None, "current", TARGET, TODAY)
        by = {r["symbol"]: r for r in out["rows"]}
        self.assertEqual(by["UFO"]["value_then_krw"], 10 * 20.0 * 1000.0)                       # 10주 × 20달러 × 그날 환율 1000
        self.assertEqual(by["069500"]["value_then_krw"], 5000.0)
        self.assertAlmostEqual(by["UFO"]["change_pct"], (15000 / 200000 - 1) * 100, places=3)
        self.assertEqual(out["missing"], ["신규상장"])                                              # 1년 전엔 상장 전 → 빼고 알려준다
        self.assertEqual(out["total_then_krw"], 205000.0)
        self.assertEqual(out["total_now_krw"], 21000.0)                                         # 빠진 종목의 지금 값도 합계에서 뺀다(같은 종목끼리 비교)
        self.assertAlmostEqual(out["covered_pct"], 21000 / 21900 * 100, places=3)
        self.assertEqual(out["series"][0], {"date": "2025-10-19", "value_krw": 205000.0})
        self.assertEqual(out["series"][-1], {"date": "2026-10-19", "value_krw": 21000.0})        # 마지막 점은 지금 토스 값
        self.assertEqual(out["series"][3]["date"], "2026-01-19")                                # 매달 같은 날

    def test_actual_basis_rewinds_fills_and_has_no_return(self):
        out = hc.past_value(ROWS, CLOSES, FX, FILLS, "actual", TARGET, TODAY)
        by = {r["symbol"]: r for r in out["rows"]}
        self.assertEqual(by["UFO"]["qty_then"], 6.0)                                            # 지금 10주 − 이후 매수 4주
        self.assertEqual(by["069500"]["qty_then"], 7.0)                                         # 지금 5주 + 이후 매도 2주
        self.assertIsNone(out["change_pct"])                                                    # 그 사이 사고판 돈이 섞여 수익률이 아니다
        self.assertIsNone(by["UFO"]["change_pct"])

    def test_missing_fx_drops_dollar_stocks(self):
        out = hc.past_value(ROWS, CLOSES, lambda d: None, None, "current", TARGET, TODAY)
        self.assertEqual([r["symbol"] for r in out["rows"]], ["069500"])
        self.assertIn("UFO", out["missing"])

    def test_helpers(self):
        self.assertEqual(hc.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
        self.assertLessEqual(len(hc.sample_dates(date(2021, 10, 19), TODAY)), hc.MAX_POINTS)
        self.assertIsNone(hc.price_on([date(2026, 1, 1)], [1.0], date(2025, 1, 1)))
        self.assertIsNone(hc.price_on([date(2025, 1, 1)], [1.0], date(2025, 3, 1)))             # 너무 오래된 종가는 시세 없음


class ComposePastTest(unittest.TestCase):
    def test_line_chart_bar_chart_and_notes(self):
        raw = hc.past_value(ROWS, CLOSES, FX, None, "current", TARGET, TODAY)
        raw["months_ago"] = 12
        out = compose_agent_answer({"get_past_value": format_tool_result("get_past_value", raw)["result"]})
        self.assertIn("12개월 전(2025-10-19)에 그대로 들고 있었다면 205,000원 → 지금 21,000원", out["bullets"][0])
        self.assertEqual([c["type"] for c in out["charts"]], ["line", "hbar"])
        self.assertTrue(any("신규상장" in n for n in out["notes"]))
        self.assertTrue(any("조언이 아닙니다" in l for l in out["limitations"]))

    def test_actual_basis_does_not_call_it_a_return(self):
        raw = hc.past_value(ROWS, CLOSES, FX, FILLS, "actual", TARGET, TODAY)
        raw["months_ago"] = 12
        out = compose_agent_answer({"get_past_value": format_tool_result("get_past_value", raw)["result"]})
        self.assertIn("수익률은 아닙니다", out["bullets"][0])
        self.assertEqual([c["type"] for c in out["charts"]], ["line"])

    def test_nothing_to_compute(self):
        out = compose_agent_answer({"get_past_value": format_tool_result("get_past_value", {"rows": [], "missing": ["신규상장"]})["result"]})
        self.assertIn("계산하지 못했습니다", out["bullets"][0])


class AssetTableExtrasTest(unittest.TestCase):
    HOLD = {"totals": {"eval_krw": 1000.0}, "cash": {"krw": 0.0, "usd": 0.0, "usd_krw_rate": 1000.0, "stocks": []},
            "stocks": [{"심볼": "UFO", "종목": "UFO", "통화": "USD", "평가금액(원)": 600.0, "수익률(%)": -5.0}, {"심볼": "TLT", "종목": "TLT", "통화": "USD", "평가금액(원)": 400.0, "수익률(%)": 1.0}]}
    BUYS = [{"symbol": "UFO", "side": "BUY", "quantity": 1, "filled_at": "2026-01-10T09:00:00"}, {"symbol": "UFO", "side": "BUY", "quantity": 1, "filled_at": "2025-10-09T09:00:00"},
            {"symbol": "UFO", "side": "SELL", "quantity": 1, "filled_at": "2025-01-01T09:00:00"}, {"symbol": "TLT", "side": "BUY", "quantity": 1, "filled_at": "2026-10-09T09:00:00"}]
    DIVS = {"rows": [{"symbol": "UFO", "gross_krw": 30.0}, {"symbol": "UFO", "gross_krw": 20.0}, {"symbol": "TLT", "gross_krw": None}]}

    def test_buy_stats_and_dividends_fill_the_stock_rows_only_when_given(self):
        rows = {r["symbol"] or r["name"]: r for r in dq.build_assets(self.HOLD, {"targets": []}, self.BUYS, self.DIVS, TODAY)}
        self.assertEqual((rows["UFO"]["first_buy_date"], rows["UFO"]["buy_count"], rows["UFO"]["holding_days"]), ("2025-10-09", 2.0, 375.0))   # 매도는 세지 않는다
        self.assertEqual(rows["UFO"]["dividend_krw"], 50.0)
        self.assertIsNone(rows["TLT"]["dividend_krw"])                                          # 환율 없는 건은 합계에서 뺀다
        self.assertIsNone(rows["원화 예수금"]["buy_count"])
        bare = {r["symbol"]: r for r in dq.build_assets(self.HOLD, {"targets": []}) if r["symbol"]}
        self.assertIsNone(bare["UFO"]["buy_count"])
        self.assertIsNone(bare["UFO"]["dividend_krw"])

    def test_queries_use_the_new_fields(self):
        rows = dq.build_assets(self.HOLD, {"targets": []}, self.BUYS, self.DIVS, TODAY)
        longest = dq.run_query(rows, {"group_by": "symbol", "field": "holding_days", "metric": "max", "limit": 1})
        self.assertEqual((longest["rows"][0]["label"], longest["rows"][0]["value"]), ("UFO", 375.0))
        since = dq.run_query(rows, {"group_by": "symbol", "metric": "count", "filter_field": "first_buy_date", "filter_op": ">=", "filter_value": "2026-01-01"})
        self.assertEqual(since["matched"], 0)                                                   # 문자열 항목은 =, !=, 포함만 — 비교 연산은 일치 없음으로 처리
        div = dq.run_query(rows, {"group_by": "symbol", "field": "dividend_krw"})
        self.assertEqual([(r["label"], r["value"]) for r in div["rows"]], [("UFO", 50.0)])


class ModelAndServerTest(storage_http.HttpTestBase, unittest.TestCase):
    def test_model_arguments_are_cleaned(self):
        self.assertEqual(lm.clean_args("get_past_value", {"months_ago": 18, "basis": "actual", "evil": 1}, "q"), {"months_ago": 18, "basis": "actual"})
        self.assertEqual(lm.clean_args("get_past_value", {"basis": "rm -rf"}, "q"), {})
        self.assertIn("get_past_value", lm.HINTS)

    SNAP = {"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0,
            "items": [{"symbol": "UFO", "name": "UFO", "currency": "USD", "marketCountry": "US", "quantity": 10, "marketValue": {"amount": 30}, "profitLoss": {"rateAfterCost": 0.1}}]}

    def test_tool_route_prices_the_current_holdings_at_a_past_date(self):
        cookie = self.login("key-a")
        today = server.datetime.now(server.ZoneInfo("Asia/Seoul")).date()
        past = hc.add_months(today, -12)
        bars = [(hc.add_months(today, -30), 1000.0, 1000.0), (today, 1000.0, 1000.0)]
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.SNAP), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None), \
                mock.patch.object(server, "yahoo_closes", return_value={past: 10.0, today: 3.0}), \
                mock.patch.object(server, "get_yahoo_daily_history", return_value={"bars": bars, "divs": []}):
            status, payload, _ = self.request("POST", "/api/agent/tool", {"name": "get_past_value", "args": {"months_ago": 12}}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual((result["months_ago"], result["basis"], result["target_date"]), (12, "current", past.isoformat()))
        self.assertEqual((result["total_then_krw"], result["total_now_krw"], result["change_pct"]), (100000.0, 30000.0, -70.0))   # 10주 × 10달러 × 1000원 → 지금 토스 평가 30,000원
        self.assertNotIn("11100000001", repr(payload))
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.SNAP), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_past_value", "args": {}})[0], 401)

    def test_asset_query_fetches_fills_and_dividends_only_when_a_field_needs_them(self):
        cookie = self.login("key-a")
        fills = [{"symbol": "UFO", "side": "BUY", "quantity": 10.0, "filled_at": "2026-01-10T09:00:00"}]
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.SNAP), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None), \
                mock.patch.object(server, "all_fills", return_value=fills) as fills_mock, \
                mock.patch.object(server, "dividends_for_year", return_value={"rows": [{"symbol": "UFO", "gross_krw": 777.0}]}) as div_mock:
            plain = self.request("POST", "/api/agent/tool", {"name": "query_data", "args": {"group_by": "category"}}, cookie=cookie)[1]["result"]
            self.assertEqual((fills_mock.call_count, div_mock.call_count), (0, 0))              # 필요 없으면 가져오지 않는다
            bought = self.request("POST", "/api/agent/tool", {"name": "query_data", "args": {"group_by": "symbol", "field": "buy_count"}}, cookie=cookie)[1]["result"]
            self.assertEqual((fills_mock.call_count, div_mock.call_count, bought["rows"][0]["value"]), (1, 0, 1.0))
            paid = self.request("POST", "/api/agent/tool", {"name": "query_data", "args": {"group_by": "symbol", "field": "dividend_krw"}}, cookie=cookie)[1]["result"]
            self.assertEqual((div_mock.call_count, paid["rows"][0]["value"]), (1, 777.0))
        self.assertTrue(plain["rows"])
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.SNAP), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None), \
                mock.patch.object(server, "all_fills", return_value=[]), mock.patch.object(server, "dividends_for_year", side_effect=ValueError("환율")):
            failed = self.request("POST", "/api/agent/tool", {"name": "query_data", "args": {"field": "dividend_krw"}}, cookie=cookie)[1]["result"]
        self.assertIn("배당 추정을 계산하지 못했습니다", failed["error"])


if __name__ == "__main__":
    unittest.main()
