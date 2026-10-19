"""Agent 답변 그래프(charts.py + agent_tools의 _charts_*): 명세 검증·정리, 답변 종류별 그래프(요인 변화·기여, 요인 순위, ETF 기여, 스크리너 업종, 전망, 과거 반응, 사례, 뉴스, 보유 비중·리밸런싱, 섹터),
계좌·링크가 명세에 새지 않는지, 화면 연결(스크립트 포함·호출)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import charts
import test_agent_tools as tat
import test_etf_agent as te
import test_history_agent as th
from agent_tools import compose_agent_answer, format_tool_result

ROOT = Path(__file__).resolve().parent.parent


def tools():
    return tat.AgentToolsTest("test_macro_tool_is_selected_for_cause_questions")      # 이 파일의 원자료 만드는 도우미만 쓴다


def titles(answer):
    return [c["title"] for c in answer["charts"]]


class MakeTest(unittest.TestCase):
    def test_valid_specs_are_cleaned_and_bounded(self):
        spec = charts.make("hbar", "제목" * 100, ["가" * 100, "나"], [{"label": "x" * 100, "data": [1, float("nan")], "colors": ["up", "<script>"], "text": ["t" * 100, None]}], "%p",
                           subtitle="s" * 500, caption="c" * 500, height=9999)
        self.assertEqual(len(spec["title"]), 80)
        self.assertEqual(len(spec["labels"][0]), 30)
        self.assertEqual(spec["datasets"][0]["data"], [1.0, None])                                    # NaN은 빈 점
        self.assertEqual(spec["datasets"][0]["colors"], ["up", "neutral"])                            # 모르는 색 토큰은 중립
        self.assertEqual((len(spec["subtitle"]), len(spec["caption"]), spec["height"]), (120, 200, 560))
        self.assertEqual(spec["datasets"][0]["text"][1], "")

    def test_bad_input_returns_nothing(self):
        self.assertIsNone(charts.make("pie", "t", ["a"], [{"data": [1]}]))
        self.assertIsNone(charts.make("bar", "t", [], [{"data": []}]))
        self.assertIsNone(charts.make("bar", "t", ["a"], [{"data": [None]}]))                         # 점이 하나도 없으면 그리지 않는다
        self.assertIsNone(charts.make("bar", "t", ["a"], []))

    def test_line_markers_and_hlines_are_validated(self):
        spec = charts.make("line", "t", ["a", "b", "c"], [{"data": [1, 2, 3]}], markers=[{"index": 1, "text": "점", "color": "down"}, {"index": 9, "text": "밖"}, {"index": "x"}],
                           hlines=[{"value": 2.5, "text": "고점", "color": "warn"}, {"value": "abc"}])
        self.assertEqual(spec["markers"], [{"index": 1, "text": "점", "color": "down"}])
        self.assertEqual(spec["hlines"], [{"value": 2.5, "text": "고점", "color": "warn"}])
        self.assertEqual(charts.make("bar", "t", ["a"], [{"data": [1]}])["type"], "bar")
        self.assertNotIn("markers", charts.make("bar", "t", ["a"], [{"data": [1]}]))

    def test_caps_on_points_and_datasets_and_unknown_units(self):
        spec = charts.make("bar", "t", [str(i) for i in range(1000)], [{"data": [1] * 1000}] * 9, unit="달러")
        self.assertEqual((len(spec["labels"]), len(spec["datasets"]), spec["unit"]), (charts.MAX_POINTS, charts.MAX_DATASETS, ""))

    def test_decimals_are_kept_only_when_valid(self):
        self.assertEqual(charts.make("bar", "t", ["a"], [{"data": [1]}], decimals=2)["decimals"], 2)
        for bad in (-1, 9, True, "2", None):
            self.assertNotIn("decimals", charts.make("bar", "t", ["a"], [{"data": [1]}], decimals=bad))

    def test_a_fixed_value_range_is_kept_only_when_numeric(self):
        spec = charts.make("line", "t", ["a", "b"], [{"data": [40, 60]}], ymin=0, ymax=100)
        self.assertEqual((spec["ymin"], spec["ymax"]), (0.0, 100.0))
        bad = charts.make("line", "t", ["a", "b"], [{"data": [40, 60]}], ymin="x", ymax=float("nan"))
        self.assertNotIn("ymin", bad)
        self.assertNotIn("ymax", bad)

    def test_signed_colors(self):
        self.assertEqual(charts.signed_colors([1.5, -2, 0, None]), ["up", "down", "neutral", "neutral"])


class AnswerChartsTest(unittest.TestCase):
    def test_macro_shows_factor_moves_and_contributions_but_only_for_explained_assets(self):
        result = format_tool_result("get_macro_drivers", tools().macro_raw())["result"]
        answer = compose_agent_answer({"get_macro_drivers": result})
        moves, *contrib = answer["charts"]
        self.assertEqual((moves["type"], moves["title"]), ("hbar", "이번 주 시장 요인 변화 (많이 움직인 순)"))
        self.assertEqual(moves["labels"][:2], ["VIX", "유가(WTI)"])
        data = moves["datasets"][0]["data"]
        self.assertAlmostEqual(data[moves["labels"].index("10년물 금리")], -1.46, places=2)           # 금리 -14.6bp는 10bp=1%로 환산해 -1.46
        self.assertEqual(moves["datasets"][0]["text"][moves["labels"].index("10년물 금리")], "-15bp 하락")   # 글은 실제 변화
        self.assertIn("10bp를 1%", moves["caption"])
        self.assertEqual([c["title"].split(" 이번 주")[0] for c in contrib], ["TLT", "GLDM"])           # 설명 안 되는 종목(weak)·데이터 없는 종목은 그래프 없음
        gold = contrib[1]
        self.assertEqual(gold["labels"], ["S&P500 -2.0% 하락", "달러 -0.4% 약세", "그 밖"])
        self.assertEqual(gold["datasets"][0]["text"], ["-1.1%p", "+0.8%p", "-0.3%p"])
        self.assertEqual(gold["datasets"][0]["colors"], ["down", "up", "neutral"])

    def test_drivers_chart_has_one_bar_per_window_for_macro_and_peer_assets(self):
        answer = th.drivers_answer()
        chart = answer["charts"][0]
        self.assertEqual((chart["type"], [d["label"] for d in chart["datasets"]]), ("hbar", ["1년", "3년", "10년"]))
        self.assertEqual(chart["labels"], ["1. 구리", "2. 달러", "3. 5년물 금리", "은 (비슷한 자산)"])
        self.assertEqual(chart["datasets"][0]["data"], [0.57, -0.37, -0.22, 0.8])
        self.assertEqual(chart["decimals"], 2)                                                         # 상관은 소수 둘째 자리까지
        self.assertIn("기준: 금 선물 일봉", chart["subtitle"])

    def test_etf_chart_shows_contributions_and_the_rest(self):
        answer = compose_agent_answer({"get_etf_breakdown": format_tool_result("get_etf_breakdown", te.raw())["result"]})
        chart = answer["charts"][0]
        self.assertTrue(chart["title"].startswith("UFO 이번 주 -6.4%"))
        self.assertEqual(chart["labels"][0], "SPCX 15.4% (-10.2%)")
        self.assertEqual(chart["labels"][-1], "그 밖 44%")
        self.assertEqual(chart["datasets"][0]["data"][0], -1.57)
        self.assertEqual(chart["datasets"][0]["colors"][-1], "neutral")
        self.assertNotIn("OLD", " ".join(chart["labels"]))                                              # 시세 없는 편입 종목은 막대에 넣지 않는다

    def test_screen_chart_shows_the_share_of_each_sector_below_the_averages(self):
        chart = th.screen_answer()["charts"][0]
        self.assertEqual(chart["labels"], ["정보기술", "금융", "에너지"])
        self.assertEqual(chart["datasets"][0]["data"], [25.0, 10 / 70 * 100, 30.0])
        self.assertEqual(chart["datasets"][0]["text"][0], "20/80개 (25%)")
        self.assertIn("MA 60·120·200·240 모두 아래인", chart["title"])
        self.assertEqual(th.screen_answer(th.screen_raw(universe_name="mine"))["charts"], [])            # 내 종목에는 업종 분포 그래프가 없다
        self.assertEqual(th.screen_answer(th.screen_raw(total_matches=0, matches=[]))["charts"], [])

    def test_outlook_chart_lists_scenarios_volatility_band_and_past_median_sorted(self):
        answer = compose_agent_answer({"get_outlook": format_tool_result("get_outlook", tools().outlook_raw())["result"]})
        gold, tlt = answer["charts"][0], answer["charts"][1]
        self.assertTrue(gold["title"].startswith("GLDM 앞으로의 뷰"))
        values = gold["datasets"][0]["data"]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertIn("1개월 변동성 범위 상단", gold["labels"])
        self.assertEqual(max(values), 7.1)
        self.assertFalse(any("과거 유사" in l for l in gold["labels"]))                                  # 표본이 부족하면 과거 통계는 넣지 않는다
        self.assertTrue(any("과거 유사 상황 3개월 후 중앙값 (독립 18회)" in l for l in tlt["labels"]))
        self.assertIn("예측이 아니라", gold["caption"])

    def test_history_charts_show_past_returns_and_the_biggest_macro_events(self):
        answer = th.answer()
        names = titles(answer)
        self.assertTrue(any("과거의 수익률" in t for t in names))
        self.assertTrue(any("거시 이슈" in t for t in names))
        events = next(c for c in answer["charts"] if "거시 이슈" in c["title"])
        values = events["datasets"][0]["data"]
        self.assertEqual(values, sorted(values))

    def test_cases_chart_compares_gold_dollar_and_stocks_per_phase(self):
        chart = th.cases_answer()["charts"][0]
        self.assertEqual((chart["type"], chart["labels"]), ("bar", ["연준 금리 인상기"]))
        self.assertEqual([d["label"] for d in chart["datasets"]], ["금", "달러", "S&P500", "TLT"])      # 종목을 지정했으면 그 종목도
        self.assertEqual([d["data"][0] for d in chart["datasets"]], [49.2, -3.1, 12.0, -0.2])

    def test_news_chart_has_weekly_moves_only_when_there_are_two_or_more(self):
        answer = compose_agent_answer({"get_weekly_news": format_tool_result("get_weekly_news", tools().news_raw())["result"]})
        chart = answer["charts"][0]
        self.assertEqual(chart["labels"], ["스트래티지", "룰루레몬", "GLDM"])                              # 등락이 없는 TLT는 뺀다
        self.assertEqual(chart["datasets"][0]["colors"], ["down", "up", "down"])
        one = tools().news_raw()
        one["items"] = one["items"][:1]
        self.assertEqual(compose_agent_answer({"get_weekly_news": format_tool_result("get_weekly_news", one)["result"]})["charts"], [])

    def test_overview_charts_show_weights_and_rebalancing_amounts(self):
        result = {"totals": {"value_krw": 1000, "return_pct": 1.0},
                  "largest_positions": [{"name": "에이", "symbol": "A", "weight_pct": 40.0}, {"name": "비", "symbol": "B", "weight_pct": 25.0}],
                  "cash": {"weight_pct": 10.0}, "limitations": [],
                  "rebalancing": {"ready": True, "buy_total_krw": 300, "sell_total_krw": 100, "target_total_pct": 100, "warnings": [],
                                  "rows": [{"label": "금", "action": "buy", "difference_krw": 300}, {"label": "주식", "action": "sell", "difference_krw": -100}, {"label": "채권", "action": "hold", "difference_krw": 0}]}}
        answer = compose_agent_answer({"get_portfolio_overview": result})
        weights, moves = answer["charts"]
        self.assertEqual((weights["labels"], weights["datasets"][0]["data"]), (["에이", "비", "현금성"], [40.0, 25.0, 10.0]))
        self.assertEqual((moves["labels"], moves["datasets"][0]["data"]), (["금", "주식"], [300.0, -100.0]))   # 유지는 뺀다, 매수 먼저
        self.assertEqual(moves["datasets"][0]["text"], ["매수 300원", "매도 100원"])
        self.assertEqual(moves["unit"], "원")

    def test_sector_chart(self):
        sector = {"configured": True, "unmapped_count": 0, "limitations": [],
                  "sectors": [{"label": "기술", "exposure_pct": 30.0, "exposure_krw": 300, "holdings": []}, {"label": "금융", "exposure_pct": 10.0, "exposure_krw": 100, "holdings": []}]}
        chart = compose_agent_answer({"get_sector_exposure": sector})["charts"][0]
        self.assertEqual((chart["labels"], chart["datasets"][0]["data"]), (["기술", "금융"], [30.0, 10.0]))

    def test_every_spec_is_json_small_and_free_of_links_or_account_data(self):
        answers = [compose_agent_answer({"get_macro_drivers": format_tool_result("get_macro_drivers", tools().macro_raw())["result"]}), th.drivers_answer(), th.screen_answer(),
                   th.answer(), th.cases_answer(), compose_agent_answer({"get_outlook": format_tool_result("get_outlook", tools().outlook_raw())["result"]}),
                   compose_agent_answer({"get_weekly_news": format_tool_result("get_weekly_news", tools().news_raw())["result"]})]
        for answer in answers:
            text = json.dumps(answer["charts"], ensure_ascii=False)
            self.assertLess(len(text), 20000)
            for needle in ("href", "http", "must-not-leak", "javascript:", "token"):
                self.assertNotIn(needle, text)

    def test_a_chart_only_answer_still_has_a_headline(self):
        answer = compose_agent_answer({"get_sector_trends": format_tool_result("get_sector_trends", th.trend_raw())["result"]})
        self.assertEqual(answer["bullets"], [])
        self.assertEqual(answer["headline"], "계좌 데이터를 기준으로 확인한 결과입니다.")


class FrontendWiringTest(unittest.TestCase):
    def test_the_script_the_container_and_the_call_exist(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('<script src="/static/js/agent_charts.js"></script>', html)
        self.assertLess(html.index("agent_charts.js"), html.index("/static/js/agent.js"))                 # 렌더러가 먼저 불러와진다
        self.assertIn('id="agentCharts"', html)
        js = (ROOT / "static/js/agent.js").read_text(encoding="utf-8")
        self.assertIn("renderAgentCharts($('agentCharts'), data.answer.charts)", js)
        charts_js = (ROOT / "static/js/agent_charts.js").read_text(encoding="utf-8")
        self.assertIn("textContent", charts_js)
        self.assertNotIn("innerHTML =", charts_js.replace("container.innerHTML = ''", ""))                # 글은 textContent로만 넣는다(HTML 해석 없음)
        self.assertIn(".agent-chart", (ROOT / "static/css/app.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
