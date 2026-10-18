"""과거 매크로 충격 도구의 Agent 쪽: 결과 정리, 질문 분류(과거 질문에 이유 도구가 따라붙지 않게), 표·요약 조합, 요인 분석의 검증 표시."""
from __future__ import annotations

import unittest

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
from agent_tools import FOCUS_TOOLS, compose_agent_answer, format_tool_result, plan_agent_query


def shock(**over):
    base = {"label": "30년물 금리", "direction": "up", "phrase": "상승", "current_change": 42.0, "unit": "bp", "percentile": 0.96, "episodes": 18,
            "median_return": -4.1, "neg_share": 0.83, "p_value": 0.0004, "verdict": "down",
            "examples": [{"date": "2022-06-14", "label": "연준 급격한 긴축·인플레이션", "factor_change": 80.0, "asset_return": -12.3},
                         {"date": "2013-06-24", "label": None, "factor_change": 70.0, "asset_return": -9.0}]}
    base.update(over)
    return base


def item(**over):
    base = {"symbol": "TLT", "name": "TLT", "held": True, "status": "ok", "history_days": 6000, "source": None, "history_from": "2002-07-30",
            "events_covered": 12, "baseline": {"median": -0.1, "neg_share": 0.51}, "active": [shock()],
            "worst_events": [{"name": "연준 급격한 긴축·인플레이션", "start": "2022-01-03", "end": "2022-10-14", "return_pct": -33.5, "z": -3.2}],
            "best_events": [{"name": "코로나19 쇼크", "start": "2020-02-19", "end": "2020-03-23", "return_pct": 14.1, "z": 3.0}]}
    base.update(over)
    return base


def raw(items=None, **over):
    base = {"items": items if items is not None else [item()], "focus": "", "focused": False, "omitted": 0}
    base.update(over)
    return base


def answer(data=None):
    return compose_agent_answer({"get_macro_history": format_tool_result("get_macro_history", data or raw())["result"]})


class FormatTest(unittest.TestCase):
    def test_numbers_are_rounded_and_strings_capped(self):
        out = format_tool_result("get_macro_history", raw([item(active=[shock(label="가" * 200, verdict="이상한값", current_change=42.123456)],
                                                                 source="나" * 100)]))["result"]
        s = out["items"][0]["active"][0]
        self.assertEqual(len(s["label"]), 30)
        self.assertEqual(s["verdict"], "insufficient")                      # 모르는 값은 판단 보류로
        self.assertEqual(s["current_change"], 42.1)
        self.assertEqual(len(out["items"][0]["source"]), 40)
        self.assertTrue(out["limitations"])

    def test_caps_on_items_and_nested_lists(self):
        many = [shock() for _ in range(20)]
        out = format_tool_result("get_macro_history", raw([item(active=many) for _ in range(10)]))["result"]
        self.assertEqual(len(out["items"]), 6)
        self.assertEqual(len(out["items"][0]["active"]), 6)

    def test_failed_item_keeps_only_status(self):
        out = format_tool_result("get_macro_history", raw([{"symbol": "ZZZ", "name": "ZZZ", "status": "no_data", "active": [shock()]}]))["result"]
        self.assertEqual(set(out["items"][0]), {"symbol", "name", "held", "extra", "status", "history_days", "source"})


class PlanTest(unittest.TestCase):
    def test_past_questions_select_the_history_tool(self):
        for q in ("TLT 과거에는 어땠어?", "비슷한 사례 알려줘", "리먼 쇼크 때 내 종목은", "예전에도 이랬어?"):
            self.assertIn("get_macro_history", plan_agent_query(q)["tools"], q)
        for q in ("이번 주 점검", "UFO 뉴스", "전부 팔면 세금은?", "금 앞으로의 전망"):
            self.assertNotIn("get_macro_history", plan_agent_query(q)["tools"], q)
        self.assertIn("get_macro_history", FOCUS_TOOLS)
        for q in ("코로나 때 금은?", "2008년 금융위기 때 금은?"):                              # 사례를 묻는 말에는 사례집이 답하고 통계 도구는 따라붙지 않는다
            tools = plan_agent_query(q)["tools"]
            self.assertIn("get_macro_cases", tools, q)
            self.assertNotIn("get_macro_history", tools, q)

    def test_a_movement_word_in_a_past_question_does_not_add_why_tools(self):
        tools = plan_agent_query("TLT 과거 금리 급등 때 어땠어")["tools"]
        self.assertNotIn("get_weekly_news", tools)
        self.assertNotIn("get_etf_breakdown", tools)
        why = plan_agent_query("TLT 왜 떨어졌어 과거에도 그랬어?")["tools"]                    # 이유를 묻는 말이 있으면 같이 부른다
        for tool in ("get_macro_drivers", "get_weekly_news", "get_macro_history"):
            self.assertIn(tool, why)
        self.assertIn("get_weekly_news", plan_agent_query("UFO 급락했네")["tools"])             # 과거 질문이 아니면 이전 동작 그대로


class ComposeTest(unittest.TestCase):
    def test_main_table_and_event_table(self):
        out = answer()
        main, events = out["tables"]
        self.assertTrue(main["title"].startswith("지금 큰 변동 중인 요인과, 과거 같은 변동 때"))
        head, now, past, examples = main["rows"][0]
        self.assertEqual(head["lines"][0], "TLT")
        self.assertIn("2002년~ 시세", head["lines"][-1])
        self.assertEqual(now["lines"][0], "30년물 금리 상승 +42bp")
        self.assertIn("상위 4%", now["lines"][1])
        self.assertEqual(past["lines"][0], "중앙값 -4.1% · 하락 83% (평소 51%)")
        self.assertIn("독립 사례 18회 · 통계적으로 구분됨 → 하락 쪽", past["lines"][1])
        self.assertEqual(examples["lines"][0], "2022-06 연준 급격한 긴축·인플레이션: 요인 +80bp → 이 종목 -12.3%")
        self.assertEqual(examples["lines"][1], "2013-06 요인 +70bp → 이 종목 -9.0%")                # 이름 없는 사례는 날짜만
        self.assertEqual(events["rows"][0][1]["lines"], ["연준 급격한 긴축·인플레이션 -33.5% (z -3.2)"])
        self.assertEqual(events["rows"][0][2]["lines"], ["코로나19 쇼크 +14.1% (z +3.0)"])

    def test_significant_findings_become_bullets_with_the_disclaimer(self):
        text = " ".join(answer()["bullets"])
        self.assertIn("TLT: 최근 1개월 30년물 금리 상승(+42bp) — 과거 같은 변동 18회 중 83%에서 하락, 중앙값 -4.1%(평소 하락 51%)", text)
        self.assertIn("원인은 아닙니다", text)

    def test_no_significant_pattern_and_insufficient_episodes_say_so(self):
        mixed = raw([item(active=[shock(verdict="mixed", p_value=0.4)])])
        self.assertIn("뚜렷하게 구분되는 패턴은 없습니다", " ".join(answer(mixed)["bullets"]))
        few = raw([item(active=[shock(verdict="insufficient", episodes=3, median_return=None, neg_share=None, p_value=None, examples=[])])])
        lines = answer(few)["tables"][0]["rows"][0][2]["lines"]
        self.assertEqual(lines[0], "독립 사례 부족 — 판단하지 않음")
        self.assertIn("3회", lines[1])

    def test_no_active_shock_and_source_and_failures(self):
        calm = raw([item(active=[], source="금 선물 일봉", symbol="GLDM", name="GLDM")])
        out = answer(calm)
        row = out["tables"][0]["rows"][0]
        self.assertIn("기준: 금 선물 일봉", row[0]["lines"])
        self.assertEqual(row[1], "지금 큰 변동 중인 요인이 없습니다")
        failed = raw([item(), {"symbol": "ZZZ", "name": "ZZZ", "status": "no_data", "history_days": 10}])
        self.assertIn("계산하지 못한 종목: ZZZ", " ".join(answer(failed)["notes"]))

    def test_significant_ones_are_listed_first_and_rows_are_capped(self):
        weak = [shock(label=f"요인{i}", verdict="mixed", p_value=0.5, percentile=0.91) for i in range(8)]
        strong = shock(label="진짜요인", p_value=0.001)
        rows = answer(raw([item(active=weak[:3] + [strong] + weak[3:])]))["tables"][0]["rows"]
        self.assertEqual(len(rows), 4)
        self.assertIn("진짜요인", rows[0][1]["lines"][0])

    def test_empty_and_focus_notes(self):
        self.assertIn("볼 보유 종목이나 관심종목이 없습니다", " ".join(answer(raw([]))["bullets"]))
        self.assertIn("질문에서 지정한 종목만 보여줍니다: TLT.", answer(raw(focused=True))["notes"])


def cases_raw(**over):
    phase = {"name": "연준 금리 인상기", "start": "2004-06-30", "end": "2006-06-29", "story": "연준이 17차례 연속으로 금리를 올렸다.",
             "moves": [{"key": "gold", "label": "금", "value": 49.2, "unit": "%"}, {"key": "tnx", "label": "10년 금리", "value": 58.0, "unit": "bp"},
                       {"key": "dxy", "label": "달러", "value": -3.1, "unit": "%"}, {"key": "spx", "label": "S&P500", "value": 12.0, "unit": "%"},
                       {"key": "vix", "label": "VIX", "value": -9.0, "unit": "%"}, {"key": "FRED:DFF", "label": "연방기금금리", "value": 356.0, "unit": "bp"}],
             "conventions": [{"rule": "금리가 오르면 금이 빠지고 내리면 오른다", "held": False, "detail": "10년 금리 +58bp, 금 +49%"},
                             {"rule": "달러가 강하면 금이 빠지고 약하면 오른다", "held": True, "detail": "달러 -3%, 금 +49%"},
                             {"rule": "위험을 피할 때 금은 안전자산으로 오른다", "held": None, "detail": "증시 -12% · VIX -"}],
             "gold_after_pct": 3.2, "asset_move_pct": -0.2, "asset_after_pct": 7.2}
    base = {"named": True, "cases": [{"id": "gfc-2008", "title": "2004~2009 서브프라임·금융위기·QE1", "summary": "금리 인상 → 균열 → 패닉 → QE", "phases": [phase]}],
            "similar": [{"case_title": "2020 코로나", "phase": "경기 회복 기대와 금리 반등", "start": "2020-08-06", "end": "2021-03-31", "distance": 0.46, "shared": 6,
                         "gold_after_pct": 3.4, "compare": [{"label": "10년 금리", "now": 85.0, "then": 121.0, "unit": "bp", "close": True},
                                                            {"label": "VIX", "now": 3.0, "then": -14.0, "unit": "%", "close": False}]}],
            "available": [{"id": "gfc-2008", "title": "2004~2009 서브프라임·금융위기·QE1"}], "asset": "TLT", "has_fred": False, "gold_available": True}
    base.update(over)
    return base


def cases_answer(data=None):
    return compose_agent_answer({"get_macro_cases": format_tool_result("get_macro_cases", data or cases_raw())["result"]})


class CasesFormatTest(unittest.TestCase):
    def test_strings_are_capped_and_bad_values_dropped(self):
        raw_data = cases_raw()
        raw_data["cases"][0]["phases"][0]["story"] = "가" * 1000
        raw_data["cases"][0]["phases"][0]["moves"].append({"key": "x", "label": "y", "value": "abc", "unit": "%"})
        raw_data["cases"][0]["phases"][0]["conventions"][0]["held"] = "yes"
        out = format_tool_result("get_macro_cases", raw_data)["result"]
        phase = out["cases"][0]["phases"][0]
        self.assertEqual(len(phase["story"]), 400)
        self.assertNotIn("x", [m["key"] for m in phase["moves"]])
        self.assertIsNone(phase["conventions"][0]["held"])
        self.assertTrue(out["limitations"])

    def test_case_count_is_capped(self):
        many = cases_raw()
        many["cases"] = many["cases"] * 6
        self.assertEqual(len(format_tool_result("get_macro_cases", many)["result"]["cases"]), 3)


class CasesPlanTest(unittest.TestCase):
    def test_tool_takes_case_and_focus(self):
        from agent_tools import TOOL_DEFINITIONS
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "get_macro_cases")
        self.assertEqual(sorted(tool["input_schema"]["properties"]), ["case", "focus"])
        self.assertIn("get_macro_cases", FOCUS_TOOLS)

    def test_case_questions_route_to_the_cases_tool(self):
        for q in ("2008년 금융위기 때 금은?", "지금과 비슷한 과거는?", "볼커 긴축 때 금은 어땠어", "코로나 때 금", "SVB 사태", "닷컴 버블", "사례집 보여줘", "엔캐리 청산 때",
                  "현재 시장상황이 어느 시점이랑 비슷해 보여?", "요즘 장이 언제랑 닮았어?", "지금 시장은 과거 어느 때와 유사해?", "최근 상황이 어떤 국면이랑 똑같아?"):
            self.assertIn("get_macro_cases", plan_agent_query(q)["tools"], q)
        for q in ("UFO 뉴스", "금 앞으로의 전망", "2026년 금 전망", "전부 팔면 세금은?", "TLT랑 비슷한 종목 알려줘", "비슷한 사례 알려줘", "이 종목이랑 닮은 ETF"):
            self.assertNotIn("get_macro_cases", plan_agent_query(q)["tools"], q)
        self.assertEqual(plan_agent_query("TLT랑 비슷한 종목 알려줘")["tools"], [])                       # 종목 추천은 이 도구들의 일이 아니다
        self.assertNotIn("get_macro_history", plan_agent_query("현재 시장상황이 어느 시점이랑 비슷해 보여?")["tools"])   # 통계 도구가 따라붙지 않는다


class CasesComposeTest(unittest.TestCase):
    def test_case_table_has_story_numbers_conventions_and_after(self):
        out = cases_answer()
        table = out["tables"][0]
        self.assertTrue(table["title"].startswith("사례: 2004~2009 서브프라임·금융위기·QE1 (이야기는 해석"))
        name, story, numbers, checks = table["rows"][0]
        self.assertEqual(name["lines"], ["연준 금리 인상기", "2004-06-30 ~ 2006-06-29"])
        self.assertIn("17차례", story)
        self.assertEqual(numbers["lines"][0], "금 +49% · 달러 -3%")
        self.assertEqual(numbers["lines"][1], "10년 금리 +58bp")
        self.assertIn("연방기금금리 +356bp", " ".join(numbers["lines"]))                                   # FRED 요인이 있으면 같이
        self.assertIn("TLT -0.2% → 그 뒤 3개월 +7.2%", numbers["lines"])
        self.assertEqual(checks["lines"][0], "✗ 달랐음: 금리가 오르면 금이 빠지고 내리면 오른다 (10년 금리 +58bp, 금 +49%)")
        self.assertTrue(checks["lines"][1].startswith("✓ 맞음"))
        self.assertTrue(checks["lines"][2].startswith("· 뚜렷하지 않음"))
        self.assertEqual(numbers["lines"][-1], "금은 국면이 끝난 뒤 3개월간 +3.2%")

    def test_mismatches_are_called_out_in_the_summary_bullets(self):
        text = " ".join(cases_answer()["bullets"])
        self.assertIn("통념과 달랐던 점 — 연준 금리 인상기: '금리가 오르면 금이 빠지고 내리면 오른다'는 맞지 않았습니다", text)
        self.assertIn("(해석)", text)
        self.assertNotIn("-0.0", repr(cases_answer()))

    def test_similar_phases_table_and_disclaimer(self):
        out = cases_answer()
        table = out["tables"][1]
        self.assertTrue(table["title"].startswith("지금(최근 6개월)과 요인 움직임이 닮은"))
        phase, score, diff = table["rows"][0]
        self.assertTrue(phase["lines"][0].startswith("1. 2020 코로나"))
        self.assertIn("2020-08-06 ~ 2021-03-31", phase["lines"][1])
        self.assertIn("0.46", score)
        self.assertIn("10년 금리: 지금 +85bp ↔ 그때 +121bp (비슷)", diff["lines"])
        self.assertEqual(diff["lines"][-1], "그 국면이 끝난 뒤 3개월간 금 +3.4%")
        self.assertIn("같은 결말을 뜻하지는 않으며 예측이 아닙니다", " ".join(out["bullets"]))

    def test_fred_hint_appears_only_without_a_key(self):
        self.assertIn("FRED 키를 넣으면", " ".join(cases_answer()["notes"]))
        self.assertNotIn("FRED 키를 넣으면", " ".join(cases_answer(cases_raw(has_fred=True))["notes"]))

    def test_unnamed_question_lists_the_available_cases(self):
        out = cases_answer(cases_raw(named=False, cases=[]))
        self.assertIn("사례집: 2004~2009 서브프라임·금융위기·QE1", " ".join(out["bullets"]))
        self.assertEqual(len(out["tables"]), 1)                                                            # 닮은 국면 표만

    def test_nothing_to_compare_says_so(self):
        out = cases_answer(cases_raw(named=False, cases=[], similar=[], available=[]))
        self.assertIn("닮은 국면을 찾기에는 비교할 요인 시세가 모자랍니다", " ".join(out["notes"]))


def drivers_raw(**over):
    d1 = {"symbol": "HG=F", "label": "구리", "up": "상승", "down": "하락", "corr": {"1년": 0.57, "3년": 0.40, "10년": 0.30}, "consistent": True,
          "reaction_1m_pct": 2.5, "absorbed": []}
    d2 = {"symbol": "DX-Y.NYB", "label": "달러", "up": "강세", "down": "약세", "corr": {"1년": -0.37, "3년": -0.37, "10년": -0.39}, "consistent": True,
          "reaction_1m_pct": -2.3, "absorbed": []}
    d3 = {"symbol": "^FVX", "label": "5년물 금리", "up": "상승", "down": "하락", "corr": {"1년": -0.22, "3년": -0.21, "10년": -0.30}, "consistent": False,
          "reaction_1m_pct": -1.3, "absorbed": ["10년물 금리", "30년물 금리"]}
    item = {"symbol": "GLDM", "name": "금", "held": True, "status": "ok", "source": "금 선물 일봉", "asset_vol_1m_pct": 6.5, "windows": ["1년", "3년", "10년"],
            "drivers": [d1, d2, d3], "peers": [{**d1, "symbol": "SI=F", "label": "은", "corr": {"1년": 0.80, "3년": 0.78, "10년": 0.77}, "reaction_1m_pct": 4.9}],
            "others": [{**d1, "label": "유가(WTI)", "corr": {"1년": -0.13, "3년": -0.12, "10년": -0.11}}],
            "model": {"factors": ["구리", "달러"], "r2": 0.35, "oos_r2": 0.39}}
    base = {"items": [item], "focus": "금", "focused": True, "omitted": 0}
    base.update(over)
    return base


def drivers_answer(data=None):
    return compose_agent_answer({"get_asset_drivers": format_tool_result("get_asset_drivers", data or drivers_raw())["result"]})


class AssetDriversTest(unittest.TestCase):
    def test_josa_follows_the_final_consonant(self):
        from agent_tools import _driver_direction, _josa
        self.assertEqual((_josa("구리", "이", "가"), _josa("금", "이", "가"), _josa("달러", "이", "가")), ("가", "이", "가"))
        self.assertEqual((_josa("금", "은", "는"), _josa("달러", "은", "는")), ("은", "는"))
        self.assertEqual(_josa("GLDM", "은", "는"), "은(는)")                                          # 영문 코드는 둘 다
        self.assertEqual(_driver_direction({"label": "구리", "corr": {"1년": 0.5}}, "금"), "구리가 오르면 금은 오르는 경향")
        self.assertEqual(_driver_direction({"label": "5년물 금리", "corr": {"1년": -0.2, "3년": -0.3}}, "TLT"), "5년물 금리가 오르면 TLT은(는) 내리는 경향")

    def test_routing_separates_typical_sensitivity_from_this_weeks_attribution(self):
        for q in ("금이 가장 영향을 많이 받는 요소 3개가 뭘까?", "TLT는 뭐에 민감해?", "금이랑 구리 상관관계", "UFO를 좌우하는 주요 요인", "가장 영향받는 요인 순위"):
            tools = plan_agent_query(q)["tools"]
            self.assertEqual(tools, ["get_asset_drivers"], q)                                          # 이번 주 요인 분해·뉴스는 따라붙지 않는다
        self.assertIn("get_macro_drivers", plan_agent_query("이번 주 금 영향 요인")["tools"])
        self.assertIn("get_macro_drivers", plan_agent_query("금 왜 떨어졌어 영향이 뭐야")["tools"])
        self.assertIn("get_asset_drivers", FOCUS_TOOLS)

    def test_chart_and_summary(self):
        out = drivers_answer()
        self.assertEqual(out["tables"], [])                                                            # 표 대신 그래프
        chart = out["charts"][0]
        self.assertEqual(chart["title"], "금 — 평소 영향받는 요인 (일간 수익률 상관, +같이 · -반대로)")
        self.assertEqual(chart["labels"], ["1. 구리", "2. 달러", "3. 5년물 금리", "은 (비슷한 자산)"])        # 매크로 3개 다음에 비슷한 자산
        self.assertEqual([d["label"] for d in chart["datasets"]], ["1년", "3년", "10년"])
        self.assertEqual(chart["datasets"][1]["data"], [0.40, -0.37, -0.21, 0.78])
        self.assertEqual(out["bullets"][0], "금이(가) 평소 가장 영향받는 매크로 요인: 구리(+), 달러(−), 5년물 금리(−) — 막대는 일간 상관(+같이·−반대로), 원인은 아닙니다.")

    def test_a_recently_stronger_relationship_is_flagged_and_notes_carry_the_model(self):
        out = drivers_answer()
        self.assertIn("구리의 영향은 최근 1년 상관 +0.57로 10년 상관 +0.30보다 훨씬 강해", " ".join(out["bullets"]))
        notes = " ".join(out["notes"])
        self.assertIn("구리, 달러로 변동의 35%를 설명하고, 뒤 구간 검증에서는 39%", notes)
        self.assertIn("순위 밖에서 상관이 컸던 요인: 유가(WTI)(-0.12)", notes)
        self.assertIn("질문에서 지정한 종목만 보여줍니다: GLDM.", notes)
        self.assertNotIn("-0.0", repr(out))

    def test_failed_item_and_empty_and_caps(self):
        bad = drivers_raw(items=[{"symbol": "ZZZ", "name": "ZZZ", "status": "no_data"}, drivers_raw()["items"][0]])
        self.assertIn("계산하지 못한 종목: ZZZ", " ".join(drivers_answer(bad)["notes"]))
        self.assertIn("볼 보유 종목이나 관심종목이 없습니다", " ".join(drivers_answer(drivers_raw(items=[]))["bullets"]))
        many = drivers_raw()
        many["items"][0]["drivers"] = many["items"][0]["drivers"] * 3
        out = format_tool_result("get_asset_drivers", many)["result"]
        self.assertEqual(len(out["items"][0]["drivers"]), 3)
        self.assertTrue(out["limitations"])


def screen_raw(**over):
    def m(symbol, name, sector, close, dists):
        return {"symbol": symbol, "name": name, "sector": sector, "close": close, "hit": [60, 120, 200, 240],
                "mas": [{"period": p, "value": close / (1 + d / 100), "dist_pct": d} for p, d in zip((60, 120, 200, 240), dists)]}
    base = {"universe_name": "sp500", "periods": [60, 120, 200, 240], "side": "below", "match": "all", "sector": "", "universe_size": 503, "evaluated": 490,
            "insufficient": 8, "failed": 5, "total_matches": 52, "as_of": "2026-10-16",
            "matches": [m("AAA", "에이 Inc", "Information Technology", 123.456, [-12.34, -9.0, -5.5, -0.04]), m("BBB", "BBB", "Financials", 50.0, [-3.0, -2.0, -1.0, -0.5])],
            "symbols": ["AAA", "BBB"] + [f"X{i}" for i in range(50)],
            "sector_counts": [{"sector": "Information Technology", "count": 20, "total": 80}, {"sector": "Financials", "count": 10, "total": 70}, {"sector": "Energy", "count": 6, "total": 20}]}
    base.update(over)
    return base


def screen_answer(data=None):
    return compose_agent_answer({"get_ma_screen": format_tool_result("get_ma_screen", data or screen_raw())["result"]})


class MaScreenAgentTest(unittest.TestCase):
    def test_the_users_sentence_is_parsed_into_arguments(self):
        from agent_tools import parse_screen_query
        self.assertEqual(parse_screen_query("지금 주봉 ma 60/120/200/240 보다 밑에 있는 S&P500 소속 주식들은?"),
                         {"universe": "sp500", "periods": [60, 120, 200, 240], "side": "below", "match": "all", "sector": ""})
        got = parse_screen_query("200주선 위에 있는 내 종목 중 기술주 하나라도")
        self.assertEqual((got["universe"], got["periods"], got["side"], got["match"], got["sector"]), ("mine", [200], "above", "any", "Information Technology"))
        self.assertEqual(parse_screen_query("이평선 아래 종목들")["periods"], [60, 120, 200, 240])                 # 기간이 없으면 기본값
        self.assertEqual(parse_screen_query("S&P500 중 이평 120 아래 상위 10개 종목")["periods"], [120])           # S&P500의 500, 상위 10개는 기간이 아님
        self.assertEqual(parse_screen_query("MA 1000 아래")["periods"], [60, 120, 200, 240])                      # 범위 밖 숫자는 무시
        self.assertEqual(parse_screen_query("주봉 20 이평선 이탈한 종목 위에서")["side"], "below")                  # 아래 말이 먼저 나오면 아래

    def test_routing_picks_the_screener_only_for_multi_stock_ma_questions(self):
        for q in ("지금 주봉 ma 60/120/200/240 보다 밑에 있는 S&P500 소속 주식들은?", "200주선 밑으로 내려간 종목들", "주봉 ma 60 120 이탈한 종목 하나라도",
                  "내 종목 중 주봉 ma 200 위에 있는 건?", "이동평균 아래에 있는 종목 찾아줘"):
            self.assertEqual(plan_agent_query(q)["tools"], ["get_ma_screen"], q)                              # 다른 도구가 따라붙지 않는다
        for q in ("MSTR 주봉 MA 아래야?", "MSTR 종목 주봉 MA 200 아래야", "이번 주 점검", "TLT 앞으로 전망", "금이 가장 영향을 많이 받는 요소 3개가 뭘까?", "주봉 ma가 뭐야"):
            self.assertNotIn("get_ma_screen", plan_agent_query(q)["tools"], q)

    def test_tool_definition_and_intent_registration(self):
        from agent_tools import INTENT_PLANS, TEACHABLE_INTENTS, TOOL_DEFINITIONS, plan_for_intent
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "get_ma_screen")
        self.assertEqual(sorted(tool["input_schema"]["properties"]), ["match", "periods", "sector", "side", "universe"])
        self.assertTrue(tool["read_only"])
        self.assertIn("screen", INTENT_PLANS)
        self.assertIn("screen", TEACHABLE_INTENTS)
        self.assertEqual(plan_for_intent("screen")["tools"], ["get_ma_screen"])

    def test_formatter_caps_and_cleans(self):
        raw = screen_raw(matches=screen_raw()["matches"] * 40, periods=[240, 60, "x"], sector="Information Technology")
        out = format_tool_result("get_ma_screen", raw)["result"]
        self.assertEqual(len(out["matches"]), 60)
        self.assertEqual(out["sector"], "정보기술")
        self.assertEqual(out["matches"][0]["sector"], "정보기술")
        self.assertEqual(out["periods"], [240, 60])                                                          # 이상한 기간("x")은 버리고 나머지는 그대로
        self.assertTrue(out["limitations"])

    def test_summary_table_and_notes(self):
        out = screen_answer()
        text = " ".join(out["bullets"])
        self.assertIn("S&P500 구성 종목 503개 중 주봉 240주 이상 시세가 있는 490개를 보면, 주봉 MA 60·120·200·240 모두보다 아래에 있는 종목은 52개(11%)입니다 (기준일 2026-10-16, 종가 기준).", text)
        self.assertNotIn("업종별로는", text)                                                                  # 업종 분포는 그래프로 옮겼다
        table = out["tables"][0]
        self.assertEqual(table["columns"], ["종목", "업종", "종가", "주봉 MA 대비"])
        self.assertEqual(table["rows"][0][0], "에이 Inc (AAA)")
        self.assertEqual(table["rows"][0][3]["lines"][0], "MA60 -12.3% · MA120 -9.0% · MA200 -5.5% · MA240 -0.04%")   # 0.1% 미만은 소수 둘째 자리까지(아래인데 +0.0으로 보이지 않게)
        self.assertEqual(table["rows"][1][0], "BBB")                                                          # 이름이 코드와 같으면 한 번만
        notes = " ".join(out["notes"])
        self.assertIn("해당 종목 52개 중 2개만 표에 보여줍니다", notes)
        self.assertIn("표에 없는 나머지 해당 종목 50개: X0, X1, X2", notes)                                  # 표에 못 넣은 종목도 코드로는 모두 알려 준다
        self.assertNotIn("AAA,", notes)
        self.assertIn("판단하지 않은 종목 8개", notes)
        self.assertIn("시세를 받지 못해 빠진 종목 5개", notes)
        self.assertIn("매수·매도 신호", " ".join(out["limitations"]))

    def test_above_any_mine_and_empty_variants(self):
        up = screen_answer(screen_raw(side="above", match="any", periods=[60, 200]))
        self.assertIn("주봉 MA 60·200 중 하나라도보다 위에 있는 종목은", " ".join(up["bullets"]))
        self.assertTrue(up["tables"][0]["title"].startswith("주봉 MA 60·200 중 하나라도보다 위에 있는 S&P500 구성 종목 (가장 많이 위인 순"))
        mine = screen_answer(screen_raw(universe_name="mine", universe_size=4, evaluated=4, total_matches=2, sector_counts=[]))
        self.assertIn("내 보유·관심 종목 4개 중", " ".join(mine["bullets"]))
        self.assertNotIn("업종별로는", " ".join(mine["bullets"]))                                              # 내 종목에는 업종 분포를 붙이지 않는다
        none = screen_answer(screen_raw(evaluated=0, total_matches=0, matches=[], sector_counts=[]))
        self.assertIn("계산할 수 있는 종목이 없습니다", " ".join(none["bullets"]))
        self.assertEqual(none["tables"], [])
        zero = screen_answer(screen_raw(total_matches=0, matches=[], sector_counts=[]))
        self.assertIn("종목은 0개(0%)입니다", " ".join(zero["bullets"]))


def trend_raw(**over):
    def row(symbol, name, state, since, prev, prev_ago, **m):
        base = {"close": 100.0, "r_week": 1.0, "r_short": 1.0, "r_mid": 4.0, "r_half": 10.0, "r_long": 20.0, "dist_fast": 3.0, "dist_slow": 9.0, "from_high": -2.0}
        base.update(m)
        return {"symbol": symbol, "name": name, "state": state, "since_weeks": since, "confirmed": since >= 2, "previous_state": prev, "previous_weeks_ago": prev_ago,
                "metrics": base, "vs_benchmark_mid": 1.5}
    import trend
    rows = [row("XLE", "에너지", "상승 유지", 2, "약화 초입", 2, r_week=4.9),
            row("XLK", "기술", "약화 초입", 1, "상승 유지", 1, r_week=-7.7, r_short=-2.4, dist_fast=-3.4),
            row("XLU", "유틸리티", "반등 시도", 1, "하락 유지", 1, r_mid=-6.1, r_long=-9.1, dist_slow=-4.7)]
    base = {"universe": "sectors", "focus": "all", "as_of": "2026-10-16", "cfg": dict(trend.DEFAULTS), "rows": rows,
            "groups": {"상승 유지": ["XLE"], "약화 초입": ["XLK"], "반등 시도": ["XLU"]},
            "changed": [], "insufficient": [], "benchmark": {"name": "S&P500", "metrics": {"r_mid": 2.7}},
            "states": [{"state": s, "meaning": m} for s, m in trend.STATES]}
    base.update(over)
    return base


def trend_answer(data=None):
    return compose_agent_answer({"get_sector_trends": format_tool_result("get_sector_trends", data or trend_raw())["result"]})


class SectorTrendAgentTest(unittest.TestCase):
    def test_the_status_question_that_returned_the_users_portfolio_is_routed_to_the_market_etfs(self):
        for q in ("지금 미국 업종별 etf들 상태 어떰?", "미국 섹터 ETF 현황 보여줘", "스파이더 업종 etf 요즘 어때", "미국 업종 ETF 지금 분위기"):
            self.assertEqual(plan_agent_query(q)["tools"], ["get_sector_trends"], q)

    def test_routing_separates_etf_trend_questions_from_sector_exposure(self):
        for q in ("미국 업종 ETF 잘나가는 순으로 알려줘", "XLK 추세전환 됐어?", "섹터 로테이션 어떻게 돼", "진짜 장기 추세가 바뀐 업종은", "업종별 모멘텀 순위", "어느 업종이 꺾였어"):
            self.assertEqual(plan_agent_query(q)["tools"], ["get_sector_trends"], q)
        for q in ("업종 비중 보여줘", "내 포트폴리오 섹터 노출은?", "섹터 쏠림 알려줘", "내 업종 상태 어때", "업종 어때", "섹터 상태"):
            self.assertNotIn("get_sector_trends", plan_agent_query(q)["tools"], q)              # 내 업종 노출·비중, ETF·미국을 말하지 않은 막연한 '상태'는 섹터 노출 도구
        self.assertIn("get_sector_exposure", plan_agent_query("섹터 노출 보여줘")["tools"])
        for q in ("이번 주 점검", "금 앞으로 전망", "UFO 뉴스", "UFO 구성 종목 기여 보여줘"):
            self.assertNotIn("get_sector_trends", plan_agent_query(q)["tools"], q)

    def test_parsing_universe_and_focus(self):
        from agent_tools import parse_trend_query
        self.assertEqual(parse_trend_query("업종 ETF 잘나가는 순"), {"universe": "sectors", "focus": "all"})
        self.assertEqual(parse_trend_query("진짜 장기 추세가 바뀐 업종은")["focus"], "long")
        self.assertEqual(parse_trend_query("단기로 꺾인 업종은")["focus"], "short")
        self.assertEqual(parse_trend_query("중기 모멘텀 센 섹터")["focus"], "mid")

    def test_tool_definition_and_intent_registration(self):
        from agent_tools import INTENT_PLANS, TEACHABLE_INTENTS, TOOL_DEFINITIONS, plan_for_intent
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "get_sector_trends")
        self.assertEqual(set(tool["input_schema"]["properties"]), {"universe", "focus", "buffer_pct", "confirm_weeks"})
        self.assertTrue(tool["read_only"])
        self.assertIn("내 포트폴리오의 업종 노출이 아닙니다", tool["description"])                      # AI 모드가 두 도구를 혼동하지 않게
        self.assertIn("trend", INTENT_PLANS)
        self.assertIn("trend", TEACHABLE_INTENTS)
        self.assertEqual(plan_for_intent("trend")["tools"], ["get_sector_trends"])

    def test_formatter_cleans_unknown_states_and_caps_rows(self):
        import trend
        raw_data = trend_raw()
        raw_data["rows"][0]["state"] = "이상한상태"
        raw_data["rows"] = raw_data["rows"] * 12
        out = format_tool_result("get_sector_trends", raw_data)["result"]
        self.assertEqual(len(out["rows"]), 25)
        self.assertEqual(out["rows"][0]["state"], "장기선 부근")                                  # 모르는 상태는 중립으로
        self.assertEqual(out["cfg"], trend.DEFAULTS)
        self.assertTrue(out["limitations"])
        self.assertEqual(len(out["states"]), 10)

    def test_a_ranking_bar_chart_and_a_slim_table_with_one_row_per_etf(self):
        out = trend_answer()
        self.assertEqual(out["bullets"], [])                                                              # 글 요약은 그래프 제목·범례로 옮겼다
        chart = out["charts"][0]
        self.assertEqual((chart["type"], chart["title"]), ("hbar", "미국 업종 ETF 3개 — 잘나가는 순 (3개월·6개월·12개월 평균 순위)"))
        self.assertEqual(chart["labels"], ["에너지(XLE)", "기술(XLK)", "유틸리티(XLU)"])
        self.assertEqual([d["label"] for d in chart["datasets"]], ["3개월", "12개월"])
        self.assertEqual(chart["datasets"][1]["data"], [20.0, 20.0, -9.1])
        self.assertEqual(chart["datasets"][1]["text"], ["+20.0% · 상승 유지", "+20.0% · 약화 초입 ⚠", "-9.1% · 반등 시도 ↗"])      # 막대 끝 글: 수익률 · 상태 · 신호
        self.assertIn("⚠악화 ↗반등", chart["caption"])
        table = out["tables"][0]
        self.assertEqual(table["title"], "미국 업종 ETF 3개 전 고점 대비와 추세 상태")
        self.assertEqual(table["columns"], ["순위", "종목", "전 고점 대비", "상태"])
        self.assertEqual(table["align"], ["r", "l", "r", "l"])
        self.assertEqual(table["rows"][0], ["1", "에너지(XLE)", "-2.0%", "상승 유지"])                           # 신호 없는 종목은 상태 한 단어
        self.assertEqual(table["rows"][1][:3], ["2", "기술(XLK)", "-2.0%"])
        self.assertEqual(table["rows"][1][3], {"lines": ["약화 초입 ⚠", "상승 유지에서 1주째 · 미확정"]})        # 악화 신호는 ⚠와 직전 상태
        self.assertEqual(table["rows"][2][3], {"lines": ["반등 시도 ↗", "하락 유지에서 1주째 · 미확정"]})         # 반등 신호는 ↗

    def test_confirmed_signals_and_unknown_previous_state_are_written_compactly(self):
        raw_data = trend_raw()
        raw_data["rows"][2]["since_weeks"] = 2
        raw_data["rows"][2]["confirmed"] = True
        raw_data["rows"][1]["previous_state"] = None
        table = trend_answer(raw_data)["tables"][0]
        self.assertEqual(table["rows"][2][3]["lines"], ["반등 시도 ↗", "하락 유지에서 2주째 · 확인"])
        self.assertEqual(table["rows"][1][3]["lines"], ["약화 초입 ⚠", "1주째 · 미확정"])                              # 직전 상태를 모르면 주수만

    def test_drawdown_text_uses_zero_at_the_high_and_a_minus_sign_below_it(self):
        from agent_tools import _from_high_text
        self.assertEqual((_from_high_text(0.0), _from_high_text(-0.03), _from_high_text(-9.24), _from_high_text(-0.3), _from_high_text(None)), ("0.0%", "0.0%", "-9.2%", "-0.3%", "-"))
        raw_data = trend_raw()
        raw_data["rows"][0]["metrics"]["from_high"] = 0.0
        raw_data["rows"][1]["metrics"]["from_high"] = -9.2
        rows = trend_answer(raw_data)["tables"][0]["rows"]
        self.assertEqual((rows[0][2], rows[1][2]), ("0.0%", "-9.2%"))

    def test_the_all_time_high_is_explained_in_the_limitations_and_the_tool_description(self):
        from agent_tools import TOOL_DEFINITIONS
        limits = " ".join(trend_answer()["limitations"])
        self.assertIn("'전 고점'은 야후 시세가 있는 전체 기간(상장 이후)의 최고 종가", limits)
        self.assertIn("상장이 짧은 업종", limits)
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "get_sector_trends")
        self.assertIn("전 고점(상장 이후 최고 종가)", tool["description"])
        self.assertNotIn("52주", " ".join(trend_answer()["bullets"] + [trend_answer()["charts"][0]["caption"]]))

    def test_focus_changes_which_horizons_are_charted_and_the_order_title_but_not_the_labels(self):
        expected = {"short": ("1주", "1개월", "단기 수익률"), "mid": ("3개월", "6개월", "중기 수익률"), "long": ("12개월", "6개월", "장기 수익률"), "all": ("3개월", "12개월", "평균 순위")}
        for focus, (first, second, order) in expected.items():
            out = trend_answer(trend_raw(focus=focus))
            chart = out["charts"][0]
            self.assertEqual([d["label"] for d in chart["datasets"]], [first, second], focus)
            self.assertIn(order, chart["title"], focus)
            self.assertEqual(out["tables"][0]["rows"][1][3]["lines"][0], "약화 초입 ⚠", focus)                      # 상태는 그대로

    def test_notes_legend_and_empty_cases(self):
        out = trend_answer()
        notes = " ".join(out["notes"])
        self.assertEqual(out["notes"][0], "판정 기준: 이동평균(50일·200일) ±2% 안은 이탈·회복으로 세지 않고, 같은 상태가 연속 2주면 확인, 아니면 미확정.")
        self.assertIn("상태 뜻: 상승 유지=단기·중기·장기 모두 상승, 장기선 위", notes)
        self.assertNotIn("하락 유지=", notes)                                                        # 나온 상태의 뜻만
        self.assertNotIn("설정에서", notes)                                                          # 설정 화면은 없다
        self.assertIn("매수·매도 신호", " ".join(out["limitations"]))
        none = trend_answer(trend_raw(rows=[], groups={}, insufficient=["XLK"]))
        self.assertIn("추세를 계산할 만큼의 시세(252거래일 이상)가 없습니다", " ".join(none["bullets"]))
        self.assertEqual(none["tables"], [])
        partial = trend_answer(trend_raw(insufficient=["XLRE"]))
        self.assertIn("시세가 모자라 판단하지 못한 종목: XLRE", " ".join(partial["notes"]))


class MacroRigorDisplayTest(unittest.TestCase):
    """요인 분석 표: 검증 설명력과 불안정 표시."""

    def result(self, **over):
        row = {"symbol": "TLT", "name": "TLT", "held": True, "status": "ok", "weekly_change_pct": 2.7, "r2": 0.88, "oos_r2": 0.85, "reason": "",
               "drivers": [{"label": "30년물 금리", "phrase": "하락", "factor_change": -16.0, "kind": "level", "contribution_pct": 2.6, "t": -42.3, "stable": True},
                           {"label": "달러", "phrase": "약세", "factor_change": -1.0, "kind": "price", "contribution_pct": 0.5, "t": -2.4, "stable": False}],
               "residual_pct": -0.4}
        row.update(over)
        return format_tool_result("get_macro_drivers", {"factors": [], "items": [row], "omitted": 0})["result"]

    def table(self, **over):
        return compose_agent_answer({"get_macro_drivers": self.result(**over)})["tables"][-1]

    def test_validation_r2_and_unstable_factor_are_shown(self):
        chart = compose_agent_answer({"get_macro_drivers": self.result()})["charts"][0]
        self.assertEqual(chart["subtitle"], "설명력 R² 0.88 (검증 0.85)")
        self.assertFalse(any("(관계 불안정)" in l for l in chart["labels"] if "30년물" in l))
        self.assertTrue(any("달러" in l and "(관계 불안정)" in l for l in chart["labels"]))

    def test_unstable_and_weak_statuses_have_different_messages(self):
        unstable = self.table(status="weak", reason="unstable", drivers=[], oos_r2=-0.1)["rows"][0]
        self.assertIn("뒤 구간 검증에서는 맞지 않아", unstable[2])
        self.assertEqual(unstable[3], "0.88 (검증 -0.10)")
        weak = self.table(status="weak", reason="low_r2", drivers=[], r2=0.05, oos_r2=None)["rows"][0]
        self.assertIn("거의 설명되지 않음", weak[2])
        self.assertEqual(weak[3], "0.05")


if __name__ == "__main__":
    unittest.main()
