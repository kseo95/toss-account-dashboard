"""Agent 도구 계약: 허용 목록, 비민감 결과 축약, 한계 표시."""
from __future__ import annotations

import unittest

from helpers import server
from agent_tools import TOOL_DEFINITIONS, compose_agent_answer, format_tool_result, plan_agent_query


def table_of(answer, title):
    return next(tb for tb in answer["tables"] if tb["title"] == title)


def row_of(table, first_cell):
    return next(r for r in table["rows"] if r[0] == first_cell)


def all_text(answer):
    return " ".join(answer["bullets"] + answer.get("notes", []))


class AgentToolsTest(unittest.TestCase):
    def test_only_seventeen_read_only_tools_are_exposed(self):
        self.assertEqual({t["name"] for t in TOOL_DEFINITIONS}, {
            "get_portfolio_overview", "get_weekly_risk_report", "estimate_liquidation_tax", "get_sector_exposure", "get_weekly_news",
            "get_macro_drivers", "get_outlook", "get_etf_breakdown", "get_macro_history", "get_macro_cases", "get_asset_drivers", "get_ma_screen", "get_sector_trends",
            "get_drawdown_analysis", "get_factor_response", "get_valuation", "get_indicators",
        })
        outlook_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "get_outlook")                  # 종목 지정 인자만 받는다
        self.assertEqual(list(outlook_tool["input_schema"]["properties"]), ["focus"])
        self.assertFalse(outlook_tool["input_schema"]["additionalProperties"])
        etf_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "get_etf_breakdown")
        self.assertEqual(list(etf_tool["input_schema"]["properties"]), ["focus"])
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

    def overview(self, plan):
        raw = {"stocks": [], "cash": {"stocks": []}, "totals": {"eval_krw": 1000, "profit_loss_krw": 0, "rate_pct": 0},
               "currency_split": {}, "rebalance_plan": plan}
        return format_tool_result("get_portfolio_overview", raw)["result"]

    def test_rebalance_message_names_actual_sum_and_exclusions(self):
        plan = {"ready": False, "target_total_pct": 90, "buy_total_krw": 0, "sell_total_krw": 0,
                "rows": [{"label": "주식", "action": "hold", "difference_krw": 0}], "warnings": [],
                "excluded": {"holdings_count": 3, "holdings_pct": 12.5, "cash_pct": 5}}
        answer = compose_agent_answer({"get_portfolio_overview": self.overview(plan)})
        text = all_text(answer)
        self.assertIn("목표를 설정한 카테고리의 비중 합계가 90.00%라서", text)
        self.assertIn("목표를 설정하지 않은 보유종목 3개 (12.50%)와 현금 (5.00%)은 리밸런싱 계산에서 제외", text)

    def test_ready_plan_with_exclusions_does_not_claim_sum_problem(self):
        plan = {"ready": True, "target_total_pct": 100, "buy_total_krw": 10, "sell_total_krw": 0,
                "rows": [{"label": "주식", "action": "buy", "difference_krw": 10}], "warnings": [],
                "excluded": {"holdings_count": 5, "holdings_pct": 25, "cash_pct": 0}}
        text = all_text(compose_agent_answer({"get_portfolio_overview": self.overview(plan)}))
        self.assertNotIn("합계가", text)
        self.assertIn("보유종목 5개 (25.00%)는 리밸런싱 계산에서 제외", text)
        self.assertNotIn("현금", text.split("보유종목 5개")[1].split("제외")[0])

    def test_no_category_message(self):
        plan = {"ready": False, "target_total_pct": 0, "rows": [], "warnings": [],
                "excluded": {"holdings_count": 0, "holdings_pct": 0, "cash_pct": 0}}
        text = " ".join(compose_agent_answer({"get_portfolio_overview": self.overview(plan)})["bullets"])
        self.assertIn("목표를 설정한 카테고리가 없어", text)
        self.assertNotIn("제외했습니다", text)

    def test_cash_designated_stocks_leave_the_position_ranking_and_show_as_cash(self):
        raw = {
            "stocks": [{"심볼": "AAA", "종목": "에이", "통화": "USD", "평가금액(원)": 300, "손익(원)": 0, "지분율(%)": 30}],
            "cash": {"krw": 50, "usd": 0, "total_krw": 650,
                     "stocks": [{"심볼": "SGOV", "종목": "SGOV", "통화": "USD", "평가금액(원)": 600, "손익(원)": 0, "지분율(%)": 60}]},
            "totals": {"eval_krw": 1000, "profit_loss_krw": 0, "rate_pct": 0},
            "currency_split": {}, "rebalance_plan": {},
        }
        result = format_tool_result("get_portfolio_overview", raw)["result"]
        self.assertEqual([p["symbol"] for p in result["largest_positions"]], ["AAA"])  # 60%짜리 SGOV가 1위가 되지 않는다
        self.assertEqual(result["position_count"], 1)
        self.assertEqual(result["cash"]["total_krw"], 650)
        self.assertAlmostEqual(result["cash"]["weight_pct"], 65.0)
        self.assertEqual(result["cash"]["cash_like_stock_count"], 1)
        text = " ".join(compose_agent_answer({"get_portfolio_overview": result})["bullets"])
        self.assertIn("가장 큰 보유 비중은 에이 30.00% 입니다.", text)
        self.assertIn("현금성 자산은 65.00% 입니다.", text)
        self.assertNotIn("SGOV 60", text)

    def test_cash_only_without_cash_stocks_says_deposit(self):
        raw = {"stocks": [{"심볼": "AAA", "종목": "에이", "통화": "KRW", "평가금액(원)": 900, "손익(원)": 0, "지분율(%)": 90}],
               "cash": {"total_krw": 100, "stocks": []}, "totals": {"eval_krw": 1000, "profit_loss_krw": 0, "rate_pct": 0},
               "currency_split": {}, "rebalance_plan": {}}
        text = " ".join(compose_agent_answer({"get_portfolio_overview": format_tool_result("get_portfolio_overview", raw)["result"]})["bullets"])
        self.assertIn("현금성 자산은 10.00% 입니다.", text)

    def test_answer_layout_with_buy_sell_list(self):
        raw = {
            "stocks": [{"심볼": "NASA", "종목": "NASA", "통화": "USD", "평가금액(원)": 160, "손익(원)": -10, "지분율(%)": 16.0}],
            "cash": {"total_krw": 300, "stocks": [{"심볼": "SGOV", "종목": "SGOV", "통화": "USD", "평가금액(원)": 250, "손익(원)": 0, "지분율(%)": 25.0}]},
            "totals": {"eval_krw": 1000, "profit_loss_krw": -100, "rate_pct": -10.0},
            "currency_split": {},
            "rebalance_plan": {
                "ready": True, "target_total_pct": 100, "buy_total_krw": 800, "sell_total_krw": 100, "warnings": [],
                "excluded": {"holdings_count": 3, "holdings_pct": 18.0, "cash_pct": 30.0},
                "rows": [
                    {"label": "코스피", "action": "buy", "difference_krw": 200},
                    {"label": "S&P", "action": "buy", "difference_krw": 600},
                    {"label": "우주", "action": "sell", "difference_krw": -100},
                    {"label": "금", "action": "hold", "difference_krw": 0},
                ],
            },
        }
        result = format_tool_result("get_portfolio_overview", raw)["result"]
        answer = compose_agent_answer({"get_portfolio_overview": result})
        self.assertEqual(answer["bullets"], [
            "현재 평가금액은 1,000원, 누적 수익률은 -10.00%입니다.",
            "가장 큰 보유 비중은 NASA 16.00% 입니다.",
            "현금성 자산은 30.00% 입니다.",
            "설정한 목표 비중 기준으로 총 매수 800원, 총 매도 100원이 필요합니다.",
        ])
        self.assertEqual(answer["notes"], ["목표를 설정하지 않은 보유종목 3개 (18.00%)와 현금 (30.00%)은 리밸런싱 계산에서 제외했습니다."])
        moves = table_of(answer, "매수 & 매도 목록")
        self.assertEqual(moves["columns"], ["카테고리", "구분", "현재 → 목표", "금액(원)", "지금(원)", "대기(원)", "종목 · 수량"])
        self.assertEqual([[r[0], r[1], r[3]] for r in moves["rows"]], [["S&P", "매수", "600"], ["코스피", "매수", "200"], ["우주", "매도", "100"]])  # 변동 금액 순, 유지(hold)는 뺀다
        self.assertEqual(moves["footer"][0][:3], ["매수 합계", "", ""])
        self.assertEqual(moves["footer"][1][:4], ["매도 합계", "", "", "100"])
        self.assertEqual(len(moves["align"]), len(moves["columns"]))
        self.assertTrue(all(len(r) == len(moves["columns"]) for r in moves["rows"] + moves["footer"]))

    def test_buy_sell_list_shows_shares_per_symbol(self):
        plan = {"ready": True, "target_total_pct": 100, "buy_total_krw": 700, "sell_total_krw": 100, "warnings": [],
                "excluded": {"holdings_count": 0, "holdings_pct": 0, "cash_pct": 0}, "quote_errors": ["069500"],
                "rows": [
                    {"label": "S&P", "action": "buy", "difference_krw": 600, "allocations": [
                        {"symbol": "SPYM", "name": "SPYM", "currency": "USD", "amount_krw": 600, "shares": 6.17}]},
                    {"label": "우주", "action": "sell", "difference_krw": -100, "allocations": [
                        {"symbol": "UFO", "name": "UFO", "currency": "USD", "amount_krw": 25, "shares": 1.0},
                        {"symbol": "NASA", "name": "NASA", "currency": "USD", "amount_krw": 75, "shares": 2.5}]},
                    {"label": "코스피", "action": "buy", "difference_krw": 90, "allocations": [
                        {"symbol": "069500", "name": "KODEX 200", "currency": "KRW", "amount_krw": 90, "shares": None}]},
                    {"label": "국내", "action": "buy", "difference_krw": 80, "allocations": [
                        {"symbol": "114800", "name": "KODEX 인버스", "currency": "KRW", "amount_krw": 80, "shares": 3.0}]},
                ]}
        answer = compose_agent_answer({"get_portfolio_overview": self.overview(plan)})
        moves = table_of(answer, "매수 & 매도 목록")
        self.assertEqual(row_of(moves, "S&P")[6], "SPYM 6.17주")
        self.assertEqual(row_of(moves, "우주")[6], "UFO 1.00주 + NASA 2.50주")
        self.assertEqual(row_of(moves, "우주")[4:6], ["-", "-"])   # 매도에는 지금/대기가 없다
        self.assertEqual(row_of(moves, "코스피")[6], "069500 주수 확인 불가")
        self.assertEqual(row_of(moves, "국내")[6], "114800 3주")   # 국내는 정수 주
        text = " ".join(answer["limitations"])
        self.assertIn("현재가를 받지 못해 주수를 계산하지 못한 종목: 069500", text)
        self.assertIn("보유 평가금액 비율로 나눈 값", text)

    def signal_answer(self, signals):
        plan = {"ready": True, "target_total_pct": 100, "buy_total_krw": 100, "sell_total_krw": 0, "warnings": [],
                "excluded": {"holdings_count": 0, "holdings_pct": 0, "cash_pct": 0},
                "rows": [{"label": "비트코인", "action": "buy", "difference_krw": 100, "allocations": []}],
                "signals": signals}
        return compose_agent_answer({"get_portfolio_overview": self.overview(plan)})

    def test_signal_section_explains_recovery_and_separates_history_from_now(self):
        answer = self.signal_answer([{
            "symbol": "MSTR", "status": "ok", "entry_pct": 100.0, "stage_count": 4, "stage_total": 4, "cycle": 1, "recovered": True,
            "steps": [{"label": "MA60주 -20.5%", "broken": True, "below": True, "ok": True},
                      {"label": "MA240주 +3.0%", "broken": True, "below": False, "ok": True}]}])
        sig = table_of(answer, "주봉 MA 신호 (진입 비율)")
        self.assertEqual(sig["columns"], ["종목", "진입 비율", "돌파 단계", "MA60주", "MA240주", "비고"])
        self.assertEqual(row_of(sig, "MSTR"), ["MSTR", "100.00%", "4/4", "-20.5% 돌파 중", "+3.0% (과거 돌파)", "회복 완료"])
        self.assertTrue(any("회복 완료" in x and "100%" in x for x in answer["limitations"]))

    def test_signal_section_marks_insufficient_history_instead_of_showing_ids(self):
        answer = self.signal_answer([{
            "symbol": "JEPQ", "status": "ok", "entry_pct": 0.0, "stage_count": 0, "stage_total": 4, "cycle": 1, "recovered": False,
            "steps": [{"label": "MA60주 +0.4%", "broken": False, "below": False, "ok": True},
                      {"label": "MA240주", "broken": False, "below": False, "ok": False}]}])
        sig = table_of(answer, "주봉 MA 신호 (진입 비율)")
        self.assertEqual(row_of(sig, "JEPQ"), ["JEPQ", "0.00%", "0/4", "+0.4%", "데이터 부족", ""])
        self.assertNotIn("s4", repr(sig))
        self.assertTrue(any("주봉이 부족한 단계" in x for x in answer["limitations"]))

    def test_signal_table_covers_excluded_unavailable_and_none_with_aligned_columns(self):
        answer = self.signal_answer([
            {"symbol": "A", "status": "ok", "entry_pct": 5.0, "stage_count": 1, "stage_total": 4, "cycle": 2, "recovered": False,
             "steps": [{"label": "MA60주 -1.0%", "broken": True, "below": True, "ok": True}]},
            {"symbol": "TLT", "status": "excluded"}, {"symbol": "X", "status": "unavailable"}, {"symbol": "Y", "status": "none"}])
        sig = table_of(answer, "주봉 MA 신호 (진입 비율)")
        self.assertTrue(all(len(r) == len(sig["columns"]) for r in sig["rows"]))
        self.assertEqual(len(sig["align"]), len(sig["columns"]))
        self.assertEqual(row_of(sig, "A")[-1], "2차 사이클")
        self.assertEqual(row_of(sig, "TLT")[1:3], ["-", "-"])
        self.assertIn("전략 제외", row_of(sig, "TLT")[-1])
        self.assertIn("신호 확인 불가", row_of(sig, "X")[-1])
        self.assertIn("적용된 전략 없음", row_of(sig, "Y")[-1])

    def test_basis_column_shows_current_and_target_percent(self):
        plan = {"ready": True, "target_total_pct": 100, "buy_total_krw": 44352, "sell_total_krw": 100, "warnings": [],
                "excluded": {"holdings_count": 0, "holdings_pct": 0, "cash_pct": 0},
                "rows": [{"label": "엔화", "action": "buy", "difference_krw": 44352, "current_pct": 8.4, "target_pct": 10.0, "allocations": []},
                         {"label": "우주", "action": "sell", "difference_krw": -100, "current_pct": 27.35, "target_pct": 10.0, "allocations": []},
                         {"label": "S&P", "action": "buy", "difference_krw": 5, "current_pct": 0.0, "target_pct": 100.0, "allocations": []},
                         {"label": "채권", "action": "buy", "difference_krw": 5, "allocations": []}]}  # 비중 정보가 없으면 '-'
        moves = table_of(compose_agent_answer({"get_portfolio_overview": self.overview(plan)}), "매수 & 매도 목록")
        basis = {r[0]: r[2] for r in moves["rows"]}
        self.assertEqual(basis, {"엔화": "8.4% → 10%", "우주": "27.4% → 10%", "S&P": "0% → 100%", "채권": "-"})

    def news_raw(self):
        return {"items": [
            {"symbol": "MSTR", "name": "스트래티지", "held": True, "weekly_change_pct": -9.84, "news_error": False,
             "headlines": [{"title": "Strategy Stock Fell Nearly 10% in a Day", "source": "TIKR", "published": "2026-10-17", "url": "https://t.example/1"},
                           {"title": "나쁜 링크", "source": "X", "published": "2026-10-17", "url": "javascript:alert(1)"},
                           {"title": "스트래티지, 지수 제외 가능성", "source": "BeInCrypto", "published": "2026-10-15", "url": "https://b.example/2"}]},
            {"symbol": "LULU", "name": "룰루레몬", "held": False, "weekly_change_pct": 6.12, "news_error": False, "headlines": []},
            {"symbol": "GLDM", "name": "GLDM", "held": True, "weekly_change_pct": -1.0, "news_error": True, "headlines": []},
            {"symbol": "TLT", "name": "TLT", "held": True, "weekly_change_pct": None, "news_error": False, "headlines": []},
        ], "omitted": 2, "token": "must-not-leak"}

    def test_news_formatter_sanitizes_links_and_limits(self):
        raw = self.news_raw()
        raw["items"][0]["headlines"][0]["title"] = "가" * 400
        out = format_tool_result("get_weekly_news", raw)
        self.assertTrue(out["read_only"])
        self.assertNotIn("must-not-leak", repr(out))
        heads = out["result"]["items"][0]["headlines"]
        self.assertEqual([h["url"] for h in heads], ["https://t.example/1", "https://b.example/2"])   # javascript: 링크는 버린다
        self.assertEqual(len(heads[0]["title"]), 140)
        self.assertTrue(any("원인으로 확정할 수 없습니다" in x for x in out["result"]["limitations"]))
        self.assertEqual(out["result"]["omitted"], 2)

    def test_news_table_has_links_moves_summary_and_honest_empty_states(self):
        result = format_tool_result("get_weekly_news", self.news_raw())["result"]
        answer = compose_agent_answer({"get_weekly_news": result})
        self.assertIn("이번 주(최근 5거래일) 가장 크게 움직인 종목: 스트래티지 -9.8%, 룰루레몬 +6.1%, GLDM -1.0%.", answer["bullets"])
        table = table_of(answer, "이번 주 종목별 움직임과 보도")
        self.assertEqual(table["columns"], ["종목", "이번 주", "보도 (제목 · 출처 · 날짜)"])
        self.assertEqual(table["wrap"], [False, False, True])
        rows = {r[0]: r for r in table["rows"]}
        mstr = rows["스트래티지 (MSTR)"]
        self.assertEqual(mstr[1], "-9.8%")
        self.assertEqual(mstr[2]["links"][0], {"text": "Strategy Stock Fell Nearly 10% in a Day", "href": "https://t.example/1", "meta": "TIKR · 2026-10-17"})
        self.assertEqual(rows["룰루레몬 (LULU) · 관심"][2], "최근 7일 관련 보도를 찾지 못했습니다")   # 관심종목 표시, 보도 없음
        self.assertEqual(rows["GLDM"][2], "뉴스를 불러오지 못했습니다")                              # 조회 실패는 '없음'과 구분
        self.assertEqual(rows["TLT"][1], "-")
        self.assertTrue(any("2개 종목" in x for x in answer["notes"]))
        self.assertTrue(any("비공식" in x for x in answer["limitations"]))
        self.assertEqual([i for i, r in enumerate(table["rows"]) if r[0] == "TLT"], [3])           # 입력 순서(많이 움직인 순)를 유지

    def test_news_table_is_capped_and_empty_input_is_explained(self):
        items = [{"symbol": f"S{i}", "name": f"S{i}", "held": True, "weekly_change_pct": float(i), "headlines": [], "news_error": False}
                 for i in range(20)]
        answer = compose_agent_answer({"get_weekly_news": format_tool_result("get_weekly_news", {"items": items})["result"]})
        self.assertEqual(len(table_of(answer, "이번 주 종목별 움직임과 보도")["rows"]), 12)
        self.assertTrue(any("8개 종목" in x for x in answer["notes"]))
        empty = compose_agent_answer({"get_weekly_news": format_tool_result("get_weekly_news", {"items": []})["result"]})
        self.assertIn("조회할 보유 종목이나 관심종목이 없습니다", " ".join(empty["bullets"]))
        self.assertEqual(empty["tables"], [])

    def macro_raw(self):
        return {"factors": [{"label": "10년물 금리", "change": -14.6, "unit": "bp", "phrase": "하락"},
                            {"label": "달러", "change": -0.36, "unit": "%", "phrase": "약세"},
                            {"label": "유가(WTI)", "change": 9.9, "unit": "%", "phrase": "상승"},
                            {"label": "VIX", "change": 29.8, "unit": "%", "phrase": "상승"},
                            {"label": "나스닥", "change": -4.6, "unit": "%", "phrase": "하락"}],
                "items": [
                    {"symbol": "TLT", "name": "TLT", "held": True, "status": "ok", "weekly_change_pct": 2.6, "r2": 0.87, "residual_pct": 0.04,
                     "drivers": [{"label": "30년물 금리", "phrase": "하락", "factor_change": -16.4, "kind": "level", "contribution_pct": 2.58}]},
                    {"symbol": "GLDM", "name": "GLDM", "held": True, "status": "ok", "weekly_change_pct": -0.6, "r2": 0.25, "residual_pct": -0.29,
                     "drivers": [{"label": "달러", "phrase": "약세", "factor_change": -0.36, "kind": "price", "contribution_pct": 0.76},
                                 {"label": "S&P500", "phrase": "하락", "factor_change": -1.97, "kind": "price", "contribution_pct": -1.11}]},
                    {"symbol": "069500", "name": "KODEX 200", "held": False, "status": "weak", "weekly_change_pct": -17.4, "r2": 0.11, "drivers": [],
                     "residual_pct": -19.1},
                    {"symbol": "NEW", "name": "신규상장", "held": True, "status": "no_data", "weekly_change_pct": None, "r2": None, "drivers": []},
                ], "omitted": 1, "token": "must-not-leak"}

    def test_macro_formatter_keeps_only_numbers_and_short_names(self):
        out = format_tool_result("get_macro_drivers", self.macro_raw())
        self.assertTrue(out["read_only"])
        self.assertNotIn("must-not-leak", repr(out))
        self.assertEqual(out["result"]["items"][0]["drivers"][0]["unit"], "bp")           # 금리는 bp
        self.assertEqual(out["result"]["items"][1]["drivers"][0]["unit"], "%")
        self.assertTrue(any("원인으로 확정할 수 없습니다" in x for x in out["result"]["limitations"]))

    def test_macro_answer_explains_drivers_with_charts_and_honest_weak_cases(self):
        result = format_tool_result("get_macro_drivers", self.macro_raw())["result"]
        answer = compose_agent_answer({"get_macro_drivers": result})
        self.assertIn("이번 주 시장: VIX +29.8%, 유가(WTI) +9.9%, 나스닥 -4.6%, 10년물 금리 -15bp.", answer["bullets"])   # 많이 움직인 요인 4개
        self.assertEqual([c["title"].split(" 이번 주")[0] for c in answer["charts"][1:]], ["TLT", "GLDM"])                # 요인별 기여는 그래프(자세한 값은 test_charts)
        gold = answer["charts"][2]
        self.assertEqual(gold["labels"][:2], ["S&P500 -2.0% 하락", "달러 -0.4% 약세"])                                   # 기여가 큰 순서
        self.assertEqual(gold["datasets"][0]["text"][:2], ["-1.1%p", "+0.8%p"])
        self.assertEqual(answer["charts"][1]["datasets"][0]["text"], ["+2.6%p", "+0.0%p"])
        table = table_of(answer, "요인으로 설명하지 못한 종목")                                                           # 표에는 설명이 안 되는 종목의 이유만 남는다
        self.assertEqual(table["columns"], ["종목", "이번 주", "이유", "설명력(R²) · 검증"])
        rows = {r[0]: r for r in table["rows"]}
        self.assertEqual(sorted(rows), ["KODEX 200 (069500) · 관심", "신규상장 (NEW)"])
        self.assertIn("거의 설명되지 않음", rows["KODEX 200 (069500) · 관심"][2])                                       # 설명력이 낮으면 정직하게
        self.assertEqual(rows["KODEX 200 (069500) · 관심"][3], "0.11")
        self.assertIn("부족", rows["신규상장 (NEW)"][2])
        self.assertTrue(any("1개 종목" in x for x in answer["notes"]))
        self.assertTrue(any("설명력(R²)이 낮으면 신뢰하지 마세요" in x for x in answer["limitations"]))

    def test_tiny_factor_moves_do_not_show_negative_zero(self):
        from agent_tools import _factor_text
        self.assertEqual(_factor_text(-0.02, "%", "하락"), "+0.0% 하락")
        self.assertEqual(_factor_text(-0.4, "bp"), "+0bp")
        self.assertEqual(_factor_text(-0.36, "%", "약세"), "-0.4% 약세")
        self.assertEqual(_factor_text(-14.6, "bp", "하락"), "-15bp 하락")

    def test_macro_tool_is_selected_for_cause_questions(self):
        for q in ("TLT 왜 떨어졌어?", "금리 때문에 빠진 거야?", "이번 주 요인 알려줘", "달러 영향 있어?", "이번주 무엇을 확인해야해"):
            self.assertIn("get_macro_drivers", plan_agent_query(q)["tools"], q)
        self.assertNotIn("get_macro_drivers", plan_agent_query("전부 팔면 세금은?")["tools"])
        self.assertNotIn("get_macro_drivers", plan_agent_query("위험 신호 점검해줘")["tools"])

    def outlook_raw(self):
        bands = {"1M": {"low_pct": -6.6, "high_pct": 7.1, "low": 77.0, "high": 88.3}, "3M": {"low_pct": -11.2, "high_pct": 12.6, "low": 73.2, "high": 92.8}}

        def item(symbol, name, skew, f3, episodes, relaxed=False, up=(), down=(), r2=0.25, held=True):
            return {"symbol": symbol, "name": name, "held": held, "status": "ok", "history_days": 2000, "price": 82.46, "trend": "below", "depth": "deep",
                    "drawdown_pct": -23.4, "month_change_pct": -4.1, "annual_vol_pct": 24.2, "bands": bands,
                    "analogs": {"relaxed": relaxed, "episodes": episodes, "baseline_3m": {"up_share": 0.68},
                                "forward": {"1M": f3, "3M": f3}, "skew": skew},
                    "scenarios": {"r2": r2, "up": list(up), "down": list(down)}}
        gold_up = [{"label": "달러", "phrase": "약세", "move": "-1.5%", "impact_pct": 3.2}, {"label": "S&P500", "phrase": "상승", "move": "+3.7%", "impact_pct": 2.1}]
        gold_down = [{"label": "달러", "phrase": "강세", "move": "+1.5%", "impact_pct": -3.1}, {"label": "S&P500", "phrase": "하락", "move": "-3.7%", "impact_pct": -2.1}]
        return {"items": [
            item("GLDM", "GLDM", "insufficient", {"n": 30, "median": 3.8, "p25": 1.2, "p75": 8.7, "up_share": 0.87}, 3, up=gold_up, down=gold_down),
            item("TLT", "TLT", "down", {"n": 272, "median": -0.9, "p25": -4.9, "p75": 2.2, "up_share": 0.44}, 18, r2=0.87,
                 up=[{"label": "30년물 금리", "phrase": "하락", "move": "-16bp", "impact_pct": 2.6}],
                 down=[{"label": "30년물 금리", "phrase": "상승", "move": "+16bp", "impact_pct": -2.5}]),
            item("JEPQ", "JEPQ", "up", {"n": 30, "median": 6.8, "p25": 5.6, "p75": 9.5, "up_share": 0.93}, 5, relaxed=True, r2=0.1, held=False),
            {"symbol": "NEW", "name": "신규", "held": True, "status": "no_data", "history_days": 120},
        ], "focus": "금", "focused": True, "omitted": 0, "token": "must-not-leak"}

    def test_outlook_formatter_is_bounded_and_clean(self):
        raw = self.outlook_raw()
        raw["items"] = raw["items"] * 6                                   # 24개 → 12개로 제한
        out = format_tool_result("get_outlook", raw)
        self.assertTrue(out["read_only"])
        self.assertNotIn("must-not-leak", repr(out))
        self.assertEqual(len(out["result"]["items"]), 12)
        self.assertTrue(any("예측이나 투자 조언이 아니라" in x for x in out["result"]["limitations"]))
        bad = self.outlook_raw()
        bad["items"][0]["trend"], bad["items"][0]["analogs"]["skew"] = "<script>", "확실히 오름"  # 알 수 없는 값은 안전한 기본값으로
        item = format_tool_result("get_outlook", bad)["result"]["items"][0]
        self.assertEqual((item["trend"], item["analogs"]["skew"]), ("below", "insufficient"))

    def test_outlook_table_shows_up_and_down_side_by_side_with_honest_statistics(self):
        answer = compose_agent_answer({"get_outlook": format_tool_result("get_outlook", self.outlook_raw())["result"]})
        table = table_of(answer, "앞으로의 상승/하락 뷰 (조건부 시나리오 · 예측 아님)")
        self.assertEqual(table["columns"], ["종목", "현재 상태", "상승 시나리오", "하락 시나리오", "과거 유사 상황 (3개월 후)"])
        rows = {r[0]: r for r in table["rows"]}
        gold = rows["GLDM"]
        self.assertEqual(gold[1]["lines"], ["200일선 아래 · 깊은 조정(고점 대비 -23%)", "최근 1개월 -4.1% · 연 변동성 24%"])
        self.assertEqual(gold[2]["lines"], ["달러 약세(-1.5%) → 약 +3.2%", "S&P500 상승(+3.7%) → 약 +2.1%", "1개월 변동성 범위 상단 +7.1%"])
        self.assertEqual(gold[3]["lines"], ["달러 강세(+1.5%) → 약 -3.1%", "S&P500 하락(-3.7%) → 약 -2.1%", "1개월 변동성 범위 하단 -6.6%"])
        # 표본은 30개지만 독립 사례가 3회뿐이면 판단하지 않는다
        self.assertEqual(gold[4]["lines"], ["표본 부족 — 판단하지 않음", "(유사 시점 30개, 독립 3회)"])
        self.assertEqual(rows["TLT"][4]["lines"], ["3개월 후 중앙값 -0.9% · 상승 비율 44% (표본 272개, 독립 18회)", "평소 상승 비율 68%", "과거 기준 하락 우세"])
        jepq = rows["JEPQ · 관심"]
        self.assertIn("국면 완화", jepq[4]["lines"][0])
        self.assertEqual(jepq[2]["lines"][0], "요인 민감도 낮음(R² 0.10)")                         # 시나리오가 없으면 이유를 말한다
        self.assertIn("시세 기록이 짧아(120거래일)", rows["신규 (NEW)"][1])
        self.assertTrue(any("조건부 시나리오이며 예측이나 투자 조언이 아닙니다" in b for b in answer["bullets"]))
        self.assertTrue(any("질문에서 지정한 종목만 보여줍니다: GLDM, TLT, JEPQ, NEW" in n for n in answer["notes"]))
        self.assertTrue(any("독립 사례가 5회 미만이면 판단하지 않습니다" in x for x in answer["limitations"]))

    def test_outlook_without_a_match_says_all_are_shown_and_empty_is_explained(self):
        raw = self.outlook_raw()
        raw["focused"] = False
        answer = compose_agent_answer({"get_outlook": format_tool_result("get_outlook", raw)["result"]})
        self.assertTrue(any("특정 종목을 찾지 못해" in n for n in answer["notes"]))
        empty = compose_agent_answer({"get_outlook": format_tool_result("get_outlook", {"items": []})["result"]})
        self.assertIn("뷰를 만들 보유 종목이나 관심종목이 없습니다", " ".join(empty["bullets"]))

    def test_outlook_tool_is_selected_for_forward_looking_questions_only(self):
        for q in ("그래서 앞으로의 금 상승/하락 뷰를 하나씩 내봐", "TLT 전망 알려줘", "시나리오 보여줘", "내릴까?", "앞으로 어떻게 될까"):
            self.assertIn("get_outlook", plan_agent_query(q)["tools"], q)
        for q in ("TLT 왜 떨어졌어?", "이번 주 무엇을 확인해야 해", "전부 팔면 세금은?", "리밸런싱 알려줘"):
            self.assertNotIn("get_outlook", plan_agent_query(q)["tools"], q)

    def test_no_moves_means_no_list_section(self):
        plan = {"ready": True, "target_total_pct": 100, "buy_total_krw": 0, "sell_total_krw": 0, "warnings": [],
                "rows": [{"label": "주식", "action": "hold", "difference_krw": 0}],
                "excluded": {"holdings_count": 0, "holdings_pct": 0, "cash_pct": 0}}
        bullets = compose_agent_answer({"get_portfolio_overview": self.overview(plan)})["bullets"]
        self.assertNotIn("매수 & 매도 목록", bullets)

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
        self.assertEqual(plan["tools"], ["get_portfolio_overview", "get_weekly_risk_report", "get_macro_drivers", "get_weekly_news"])  # 이번 주 질문에는 요인과 소식이 붙는다
        self.assertTrue(plan["supported"])
        self.assertIn("macro", plan["intents"])
        self.assertIn("news", plan["intents"])

    def test_news_words_select_the_news_tool_but_plain_risk_questions_do_not(self):
        for q in ("내 종목 소식 알려줘", "왜 떨어졌어?", "관심종목 뉴스", "이번주 무엇을 확인해야해"):
            self.assertIn("get_weekly_news", plan_agent_query(q)["tools"], q)
        self.assertNotIn("get_weekly_news", plan_agent_query("위험 신호 점검해줘")["tools"])
        self.assertNotIn("get_weekly_news", plan_agent_query("전부 팔면 세금은?")["tools"])

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


class RuleModeRoutingTest(unittest.TestCase):
    """규칙 기반 모드의 질문 분류: 공백·유의어 허용, 종목 이름만 말했을 때의 종합 계획, 질문 예시."""

    def test_spacing_and_synonyms_are_accepted(self):
        from agent_tools import plan_agent_query as plan
        for q, tool in (("이번주 뭘 확인해야해", "get_weekly_news"), ("UFO 왜 움직였어?", "get_etf_breakdown"), ("UFO 급락했네", "get_macro_drivers"),
                        ("엔비디아 폭락 이유", "get_weekly_news"), ("앞날이 어떨까 향후 전망", "get_outlook"), ("기사 있어?", "get_weekly_news"),
                        ("오.를.까", "get_outlook"), ("전량 매도하면 세 금", "estimate_liquidation_tax")):
            self.assertIn(tool, plan(q)["tools"], q)

    def test_plain_chatter_is_not_matched(self):
        from agent_tools import plan_agent_query as plan
        for q in ("안녕", "오늘 점심 뭐 먹지", "고마워"):
            self.assertFalse(plan(q)["supported"], q)

    def test_holding_plan_and_suggestions(self):
        from agent_tools import ENTITY_TOOLS, FOCUS_TOOLS, plan_for_holding, suggest_questions
        self.assertEqual(plan_for_holding()["tools"], list(ENTITY_TOOLS))
        self.assertTrue(set(ENTITY_TOOLS) <= FOCUS_TOOLS)
        for name in ENTITY_TOOLS:                                                       # 종목을 지정할 수 있는 도구는 모두 focus 입력을 받는다
            tool = next(t for t in TOOL_DEFINITIONS if t["name"] == name)
            self.assertEqual(list(tool["input_schema"]["properties"]), ["focus"], name)
        out = suggest_questions(["UFO", "UFO", "스트래티지", "가" * 100, ""])
        self.assertEqual(out[0], "이번 주 무엇을 확인해야 해?")
        self.assertIn("UFO 앞으로의 상승/하락 뷰", out)
        self.assertEqual(len(out), len(set(out)))
        self.assertLessEqual(len(out), 6)
        self.assertEqual(suggest_questions([])[0], "이번 주 무엇을 확인해야 해?")

    def test_focused_news_and_macro_say_which_stocks_are_shown(self):
        news = format_tool_result("get_weekly_news", {"items": [{"symbol": "UFO", "name": "UFO", "held": True, "weekly_change_pct": -1.0, "headlines": []}],
                                                      "omitted": 0, "focus": "UFO 뉴스", "focused": True})["result"]
        self.assertTrue(news["focused"])
        answer = compose_agent_answer({"get_weekly_news": news})
        self.assertIn("질문에서 지정한 종목만 보여줍니다: UFO.", answer["notes"])
        macro_result = format_tool_result("get_macro_drivers", {"factors": [], "items": [{"symbol": "UFO", "name": "UFO", "held": True, "status": "no_data"}],
                                                                "omitted": 0, "focus": "UFO", "focused": True})["result"]
        self.assertIn("질문에서 지정한 종목만 보여줍니다: UFO.", compose_agent_answer({"get_macro_drivers": macro_result})["notes"])
        unfocused = format_tool_result("get_weekly_news", {"items": [], "omitted": 0, "focus": "내 종목", "focused": False})["result"]
        self.assertFalse(any("지정한" in n for n in compose_agent_answer({"get_weekly_news": unfocused})["notes"]))   # 못 찾았다는 안내는 뉴스·매크로에선 하지 않는다


class ManyFactorsTest(unittest.TestCase):
    """요인이 40개를 넘어도 이번 주 시장 요인 그래프는 많이 움직인 10개만 보이고, 도구 결과에는 모두 들어간다."""

    def data(self, n=45):
        factors = [{"label": f"요인{i}", "change": float(i + 1) if i % 2 else -float(i + 1), "unit": "%", "phrase": "상승" if i % 2 else "하락"} for i in range(n)]
        return {"factors": factors, "items": [], "omitted": 0, "focus": "", "focused": False}

    def test_formatter_keeps_all_factors_and_the_chart_shows_the_biggest_movers(self):
        result = format_tool_result("get_macro_drivers", self.data())["result"]
        self.assertEqual(len(result["factors"]), 45)                                         # FRED 요인처럼 뒤에 있는 요인이 잘리지 않는다
        answer = compose_agent_answer({"get_macro_drivers": result})
        chart = answer["charts"][0]
        self.assertEqual(chart["title"], "이번 주 시장 요인 변화 (많이 움직인 순)")
        self.assertEqual(len(chart["labels"]), 10)
        self.assertEqual(chart["labels"][0], "요인44")                                        # 가장 많이 움직인 것이 맨 위
        self.assertTrue(any("45개 중 많이 움직인 10개만" in n for n in answer["notes"]))
        self.assertEqual(answer["tables"], [])

    def test_a_short_list_is_shown_in_full_without_a_note(self):
        result = format_tool_result("get_macro_drivers", self.data(5))["result"]
        answer = compose_agent_answer({"get_macro_drivers": result})
        self.assertEqual(len(answer["charts"][0]["labels"]), 5)
        self.assertFalse(any("많이 움직인" in n for n in answer["notes"]))


if __name__ == "__main__":
    unittest.main()
