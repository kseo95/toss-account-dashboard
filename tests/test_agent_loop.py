"""규칙 기반 모드의 에이전트 루프: 결과를 보고 덧붙여 볼 도구를 고르는 규칙(follow_ups), 여러 결과를 엮은 관찰 문장(observations), 서버에서 이어 부르기와 실패 격리."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import agent_loop
import test_storage_http as storage_http
import test_trend_view as tv


def row(symbol, name, state, from_high=-5.0):
    return {"symbol": symbol, "name": name, "state": state, "metrics": {"from_high": from_high}}


def trend(*rows):
    return {"universe": "sectors", "rows": list(rows)}


def screen(sector="", total=10, evaluated=20, counts=None):
    return {"universe": "sp500", "sector": sector, "total_matches": total, "evaluated": evaluated, "periods": [60, 120],
            "sector_counts": counts or [{"sector": "정보기술", "count": 8, "total": 10}]}


class FollowUpRulesTest(unittest.TestCase):
    def test_a_worsening_sector_leads_to_a_look_inside_that_sector(self):
        steps = agent_loop.follow_ups({"get_sector_trends": trend(row("XLK", "기술", "약화 초입", -9), row("XLRE", "부동산", "하락 전환 확인", -18), row("XLE", "에너지", "상승 유지", 0))})
        self.assertEqual([s["tool"] for s in steps], ["get_ma_screen"])
        self.assertEqual(steps[0]["args"]["sector"], "Real Estate")                                     # 전 고점에서 가장 깊이 빠진 악화 업종
        self.assertIn("부동산(XLRE)", steps[0]["reason"])

    def test_no_worsening_sector_means_no_extra_step(self):
        self.assertEqual(agent_loop.follow_ups({"get_sector_trends": trend(row("XLE", "에너지", "상승 유지"))}), [])

    def test_a_screen_with_matches_leads_to_the_sector_etf_state(self):
        steps = agent_loop.follow_ups({"get_ma_screen": screen()})
        self.assertEqual([s["tool"] for s in steps], ["get_sector_trends"])
        self.assertIn("XLK", steps[0]["reason"])

    def test_already_looked_tools_are_not_asked_again(self):
        self.assertEqual(agent_loop.follow_ups({"get_ma_screen": screen(), "get_sector_trends": trend()}), [])
        self.assertEqual(agent_loop.follow_ups({"get_ma_screen": screen(sector="Real Estate")}), [])         # 업종을 이미 좁혔으면 되돌아가지 않는다
        self.assertEqual(agent_loop.follow_ups({"get_ma_screen": screen(total=0)}), [])

    def test_broken_ma_items_lead_to_their_news(self):
        steps = agent_loop.follow_ups({"get_weekly_risk_report": {"all_ma_broken": [{"label": "금 ETF", "symbol": "GLD"}, {"symbol": "TLT"}]}})
        self.assertEqual(steps[0]["tool"], "get_weekly_news")
        self.assertEqual(steps[0]["args"], {"focus": "금 ETF, TLT"})
        self.assertEqual(agent_loop.follow_ups({"get_weekly_risk_report": {"all_ma_broken": [{"label": "x"}]}, "get_weekly_news": {}}), [])

    def test_extra_steps_are_capped(self):
        many = {"get_weekly_risk_report": {"all_ma_broken": [{"label": "A"}]}, "get_sector_trends": trend(row("XLK", "기술", "약화 초입"))}
        self.assertLessEqual(len(agent_loop.follow_ups(many)), agent_loop.MAX_EXTRA_STEPS)


class ObservationTest(unittest.TestCase):
    def test_summary_counts_signals(self):
        out = agent_loop.observations({"get_sector_trends": trend(row("XLK", "기술", "약화 초입"), row("XLU", "유틸리티", "반등 시도"), row("XLE", "에너지", "상승 유지"))})
        self.assertEqual(len(out), 1)
        self.assertIn("악화 신호 1곳(기술)", out[0])
        self.assertIn("반등 신호 1곳(유틸리티)", out[0])
        self.assertIn("나머지 1곳", out[0])

    def test_no_signal_means_no_summary_line(self):
        self.assertEqual(agent_loop.observations({"get_sector_trends": trend(row("XLE", "에너지", "상승 유지"))}), [])

    def test_etf_and_stocks_agree_or_disagree(self):
        base = {"get_sector_trends": trend(row("XLRE", "부동산", "하락 전환 확인", -18))}
        wide = agent_loop.observations({**base, "get_ma_screen": screen("부동산", total=12, evaluated=20)})
        self.assertTrue(any("같은 방향" in line and "60%" in line for line in wide))
        narrow = agent_loop.observations({**base, "get_ma_screen": screen("부동산", total=2, evaluated=20)})
        self.assertTrue(any("일부 종목이 끌어내린" in line for line in narrow))

    def test_screen_first_links_the_biggest_sector_to_its_etf(self):
        out = agent_loop.observations({"get_ma_screen": screen(), "get_sector_trends": trend(row("XLK", "기술", "약화 초입"))})
        self.assertTrue(any("정보기술" in line and "XLK" in line and "약화 초입" in line for line in out))

    def test_no_forecast_words(self):
        out = " ".join(agent_loop.observations({"get_sector_trends": trend(row("XLRE", "부동산", "하락 전환 확인", -18)), "get_ma_screen": screen("부동산", total=12, evaluated=20)}))
        for word in ("전망", "오를", "내릴", "사세요", "파세요", "매수", "매도"):
            self.assertNotIn(word, out)


UNIVERSE = [{"symbol": f"S{i}", "name": f"종목{i}", "sector": "Utilities", "sub_industry": ""} for i in range(4)]


class AgentLoopHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    snapshot, tool, ask = tv.TrendHttpTest.snapshot, tv.TrendHttpTest.tool, tv.TrendHttpTest.ask    # 추세 도구 시험과 같은 요청 도우미

    def setUp(self):
        super().setUp()

        def history(symbol, force=False, years=None):
            if symbol not in tv.PRICES:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(tv.PRICES[symbol].items())], "divs": [], "name": symbol}
        for name, value in {"get_yahoo_daily_history": mock.Mock(side_effect=history), "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {})}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in {"fetch_sp500": mock.Mock(return_value=[dict(u) for u in UNIVERSE])}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        saved = tv.PRICES["XLRE"]
        tv.PRICES["XLRE"] = tv.series(lambda i: 100.0 * 1.003 ** i if i < tv.N - 25 else 100.0 * 1.003 ** (tv.N - 25) * 0.985 ** (i - (tv.N - 25)))    # 오르다 크게 꺾임(하락 전환 확인)
        self.addCleanup(tv.PRICES.__setitem__, "XLRE", saved)
        for sym in ("S0", "S1", "S2", "S3"):
            tv.PRICES[sym] = tv.series(lambda i: 300.0 * 0.998 ** i)
            self.addCleanup(tv.PRICES.pop, sym, None)

    def test_the_question_about_sectors_continues_into_the_weak_sector(self):
        cookie = self.login("key-a")
        payload = self.ask(cookie, "지금 미국 업종별 etf들 상태 어떰?")[1]
        tools = [e["tool"] for e in payload["executions"]]
        self.assertEqual(tools[0], "get_sector_trends")
        self.assertEqual(tools[1:], ["get_ma_screen"])
        self.assertIn("reason", payload["executions"][1])
        self.assertIn("미국 업종 ETF 11개 — 잘나가는 순", payload["answer"]["charts"][0]["title"])         # 물은 답(그래프)이 맨 위
        self.assertTrue(any(b.startswith("종합:") for b in payload["answer"]["bullets"]))
        self.assertTrue(payload["answer"]["tables"][0]["title"].startswith("미국 업종 ETF"))

    def test_a_failed_extra_step_does_not_break_the_answer(self):
        cookie = self.login("key-a")
        server.fetch_sp500.side_effect = server.ScreenerError("목록 실패")
        status, payload, _ = self.ask(cookie, "지금 미국 업종별 etf들 상태 어떰?")
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_sector_trends"])
        self.assertTrue(any("가져오지 못했습니다" in n for n in payload["answer"]["notes"]))

    def test_tool_endpoint_never_chains(self):
        cookie = self.login("key-a")
        payload = self.tool(cookie)[1]
        self.assertNotIn("get_ma_screen", payload.get("executions", []))


if __name__ == "__main__":
    unittest.main()
