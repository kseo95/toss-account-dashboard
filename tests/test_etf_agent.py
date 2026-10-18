"""ETF 구성 종목 분해의 Agent 쪽: 결과 정리(링크 안전·길이 제한), 질문 분류, 답변 조합(표·요약·경고·단독/동반 안내)."""
from __future__ import annotations

import unittest

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
from agent_tools import compose_agent_answer, format_tool_result, plan_agent_query


def row(symbol, weight, change, contribution, name=None):
    return {"symbol": symbol, "name": name or symbol, "weight_pct": weight, "change_pct": change, "contribution_pct": contribution}


def raw_item(**over):
    base = {"symbol": "UFO", "name": "UFO", "held": True, "status": "ok", "window": ["2026-10-09", "2026-10-16"], "etf_change_pct": -6.43,
            "rows": [row("SPCX", 15.4, -10.2, -1.57), row("RKLB", 5.4, -25.5, -1.39), row("GRMN", 5.1, 0.04, 0.002), row("OLD", 2.0, None, None)],
            "priced_weight_pct": 56.0, "rest_weight_pct": 44.0, "explained_pct": -2.96, "residual_pct": -3.47, "covered_return_pct": -5.3,
            "rest_implied_pct": -7.9, "flags": ["missing_prices", "bogus"],
            "drivers": {"SPCX": {"status": "ok", "r2": 0.41, "contributions": [{"label": "나스닥", "phrase": "하락", "factor_change": -3.2, "kind": "price", "contribution_pct": -1.1}]},
                        "RKLB": {"status": "weak", "r2": 0.05, "contributions": []}},
            "headlines": {"SPCX": {"error": False, "headlines": [
                {"title": "스페이스 급락", "source": "연합", "published": "2026-10-15", "url": "https://n.example/1"},
                {"title": "나쁜 링크", "source": "X", "published": "2026-10-15", "url": "javascript:alert(1)"}]},
                "RKLB": {"error": True, "headlines": []}}}
    base.update(over)
    return base


def raw(**over):
    base = {"items": [raw_item()], "no_holdings": [{"symbol": "TLT", "name": "TLT", "reason": "none"}], "focus": "", "focused": False, "omitted": 0}
    base.update(over)
    return base


class FormatTest(unittest.TestCase):
    def test_result_is_cleaned_and_unsafe_links_dropped(self):
        result = format_tool_result("get_etf_breakdown", raw())["result"]
        item = result["items"][0]
        self.assertEqual(item["flags"], ["missing_prices"])                      # 모르는 플래그는 버린다
        self.assertEqual([h["url"] for h in item["headlines"]["SPCX"]["headlines"]], ["https://n.example/1"])
        self.assertTrue(item["headlines"]["RKLB"]["error"])
        self.assertEqual(item["drivers"]["SPCX"]["drivers"][0]["unit"], "%")
        self.assertEqual(result["no_holdings"][0]["reason"], "편입 정보 없음")
        self.assertTrue(result["limitations"])

    def test_oversized_input_is_capped(self):
        big = raw_item(rows=[row(f"S{i}", 1, 1, 0.01) for i in range(40)])
        self.assertEqual(len(format_tool_result("get_etf_breakdown", raw(items=[big] * 10))["result"]["items"]), 6)
        self.assertEqual(len(format_tool_result("get_etf_breakdown", raw(items=[big]))["result"]["items"][0]["rows"]), 15)

    def test_failed_item_keeps_only_its_status(self):
        out = format_tool_result("get_etf_breakdown", raw(items=[{"symbol": "ZZZ", "name": "ZZZ", "status": "no_data", "rows": [row("A", 1, 1, 1)]}]))["result"]
        self.assertEqual(out["items"][0], {"symbol": "ZZZ", "name": "ZZZ", "held": False, "extra": False, "status": "no_data"})


class PlanTest(unittest.TestCase):
    def test_etf_questions_select_the_breakdown_tool(self):
        for q in ("UFO 구성 종목 보여줘", "ETF 뭐 담고 있어?", "편입 종목 기여", "UFO 왜 떨어졌어?", "etf 왜 올랐어"):
            self.assertIn("get_etf_breakdown", plan_agent_query(q)["tools"], q)
        for q in ("전부 팔면 세금은?", "금 앞으로의 전망", "이번 주 무엇을 확인해야 해", "리밸런싱 알려줘"):
            self.assertNotIn("get_etf_breakdown", plan_agent_query(q)["tools"], q)


class ComposeTest(unittest.TestCase):
    def answer(self, data=None, **kw):
        result = format_tool_result("get_etf_breakdown", data or raw())["result"]
        return compose_agent_answer({"get_etf_breakdown": result, **kw})

    def test_table_has_contributions_rest_factors_and_links(self):
        answer = self.answer()
        table = answer["tables"][0]
        self.assertTrue(table["title"].startswith("ETF 구성 종목별 기여"))
        head, moves, factors, news = table["rows"][0]
        self.assertEqual(head["lines"], ["UFO", "이번 주 -6.4%", "상위 56% 반영"])
        self.assertEqual(moves["lines"][0], "SPCX 15.4% · -10.2% → -1.57%p")
        self.assertIn("GRMN 5.1% · +0.0% → +0.00%p", moves["lines"])             # -0.0 / 부호 처리
        self.assertIn("OLD 2.0% · 시세 없음", moves["lines"])
        self.assertEqual(moves["lines"][-1], "그 밖 44% → -3.47%p (평균 -7.9% 움직인 셈)")
        self.assertIn("SPCX: 나스닥 -3.2% 하락 → -1.1%p (R² 0.41)", factors["lines"])
        self.assertIn("RKLB: 요인으로 거의 설명되지 않음", factors["lines"])
        self.assertEqual(news["links"][0]["text"], "[SPCX] 스페이스 급락")
        self.assertEqual(news["links"][0]["href"], "https://n.example/1")
        self.assertNotIn("-0.0", repr(answer))

    def test_summary_names_the_biggest_movers_in_the_direction_of_the_move(self):
        text = " ".join(self.answer()["bullets"])
        self.assertIn("SPCX(-1.57%p), RKLB(-1.39%p)가 가장 크게 끌어내렸습니다", text)
        up = raw_item(etf_change_pct=3.0, rows=[row("AAA", 10, 20, 2.0), row("BBB", 10, -5, -0.5)])
        self.assertIn("AAA(+2.00%p)가 가장 크게 끌어올렸습니다", " ".join(self.answer(raw(items=[up]))["bullets"]))
        flat = raw_item(etf_change_pct=-1.0, rows=[row("AAA", 10, 20, 2.0)])
        self.assertIn("기여가 뚜렷하지 않습니다", " ".join(self.answer(raw(items=[flat]))["bullets"]))

    def test_flags_and_missing_funds_are_noted(self):
        item = raw_item(flags=["low_coverage", "diverged", "missing_prices"])
        answer = self.answer(raw(items=[item]))
        notes = " ".join(answer["notes"])
        for part in ("설명 범위가 좁습니다", "쏠렸거나", "시세를 받지 못해", "편입 정보가 없어 분해하지 못한 종목 1개"):
            self.assertIn(part, notes)
        self.assertTrue(answer["limitations"])

    def test_nothing_to_break_down_is_a_bullet_when_asked_alone_and_a_note_when_combined(self):
        empty = raw(items=[], focused=True, focus="채권")
        alone = self.answer(empty)
        self.assertIn("구성 종목으로 나눌 수 없습니다: TLT(편입 정보 없음)", " ".join(alone["bullets"]))
        self.assertNotEqual(alone["headline"], "이 질문은 아직 처리할 수 없습니다.")
        combined = compose_agent_answer({"get_etf_breakdown": format_tool_result("get_etf_breakdown", empty)["result"],
                                         "get_weekly_news": format_tool_result("get_weekly_news", {"items": [], "omitted": 0})["result"]})
        self.assertNotIn("구성 종목으로 나눌 수 없습니다", " ".join(combined["bullets"]))
        self.assertIn("구성 종목으로 나눌 수 없습니다", " ".join(combined["notes"]))

    def test_failed_etf_is_listed_in_notes(self):
        out = self.answer(raw(items=[raw_item(), {"symbol": "ZZZ", "name": "ZZZ", "status": "no_data"}]))
        self.assertIn("분해하지 못한 ETF: ZZZ", " ".join(out["notes"]))


if __name__ == "__main__":
    unittest.main()
