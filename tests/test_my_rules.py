"""내 원칙 대조(get_my_rules)와 로컬 모델의 여러 번 생각하는 루프: 목표 비중·분할매수 전략 대조 숫자, 답변 문장·그래프, 라우팅, 도구 결과를 보고 다음 행동을 정하는 루프, 최종 답 검사(판단·심리·지어낸 숫자 차단)."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import local_model as lm
import test_storage_http as storage_http
from agent_tools import compose_agent_answer, format_tool_result, plan_agent_query


def target(label, symbols, pct, held, actual, total_pct):
    diff = actual - pct
    return {"label": label, "symbols": symbols, "held_symbols": held, "target_pct": pct, "actual_pct": actual, "actual_total_pct": total_pct, "diff_pct": diff,
            "value_krw": 0, "status": "over" if diff > 3 else "under" if diff < -3 else "near"}


def holding(symbol, name, weight, ret, category, stage=None, entry=None, ma=()):
    strategy = {"status": "none"}
    if stage is not None:
        strategy = {"status": "ok", "entry_pct": entry, "stage": stage, "stage_total": 4, "recovered": False,
                    "ma": [{"period": p, "unit": "주", "dist_pct": d} for p, d in ma]}
    return {"symbol": symbol, "name": name, "weight_pct": weight, "return_pct": ret, "category": category, "strategy": strategy}


def raw():
    return {"targets": [target("우주", ["UFO", "NASA"], 10, ["UFO", "NASA"], 52.8, 27.4), target("채권", ["TLT"], 20, ["TLT"], 22.3, 11.6),
                        target("S&P", ["SPYM"], 20, [], 0.0, 0.0), target("금", ["GLDM"], 10, ["GLDM"], 1.8, 0.9)],
            "holdings": [holding("NASA", "NASA", 16.0, -36.1, "우주", 0, 0.0), holding("TLT", "TLT", 11.6, -1.0, "채권"),
                         holding("UFO", "UFO", 11.4, -25.2, "우주", 1, 5.0, [(60, -11.1), (120, 16.9), (200, 43.6), (240, 49.0)]),
                         holding("SEDG", "솔라엣지", 4.5, -5.8, None, 3, 30.0, [(60, -21.7), (200, -63.0)]),
                         holding("GLDM", "GLDM", 0.9, -3.5, "금", 1, 5.0, [(60, -4.3), (200, 39.5)])],
            "unclassified": ["SEDG"], "strategy_steps": ["MA60주 아래 +5%p", "MA120주 아래 +10%p"], "recovery_pct": 50.0, "band_pct": 3.0}


class ComposeRulesTest(unittest.TestCase):
    def answer(self, data=None):
        return compose_agent_answer({"get_my_rules": format_tool_result("get_my_rules", data or raw())["result"]})

    def test_the_numbers_that_match_the_users_own_criteria_are_listed_without_blame(self):
        text = "\n".join(self.answer()["bullets"])
        self.assertIn("잘잘못이나 심리는 판단하지 않습니다", text)
        self.assertIn("우주(UFO, NASA): 목표 10% → 현재 52.8% (목표 대상 안 기준, 전체의 27.4%) — 42.8%p 초과", text)
        self.assertIn("S&P(보유 없음): 목표 20% → 현재 0.0%", text)
        self.assertIn("UFO: 분할매수 전략은 1/4단계(진입 5%)인데 주봉 200·240선보다 +49.0% 위에 있고 비중은 11.4% — 목표를 넘은 '우주' 카테고리", text)
        self.assertIn("NASA: 전략 신호가 없는데(주봉 데이터 부족 등) 비중이 16.0% — 목표를 넘은 '우주' 카테고리", text)
        self.assertNotIn("TLT:", text)                                                                          # 전략 대상이 아닌 종목(배정 없음)은 신호 없음으로 세지 않는다
        self.assertIn("목표 카테고리에 넣지 않은 보유종목: SEDG", text)
        for word in ("위반했", "포모성", "잘못했"):
            self.assertNotIn(word, text)

    def test_charts_show_target_vs_actual_and_the_position_against_the_200_week_average(self):
        answer = self.answer()
        target_chart, ma_chart = answer["charts"]
        self.assertEqual(target_chart["labels"], ["우주", "채권", "S&P", "금"])
        self.assertEqual([d["label"] for d in target_chart["datasets"]], ["목표 비중", "현재(목표 대상 안)"])
        self.assertEqual(target_chart["datasets"][1]["colors"], ["warn", "up", "accent", "accent"])               # 초과·근처·미달
        self.assertEqual(target_chart["datasets"][1]["text"][0], "52.8%\n+42.8%p")
        self.assertEqual(ma_chart["labels"], ["UFO", "솔라엣지", "GLDM"])                                         # 200주선 값이 있는 종목만
        self.assertEqual(ma_chart["datasets"][0]["data"], [43.6, -63.0, 39.5])
        self.assertEqual(ma_chart["datasets"][0]["colors"], ["warn", "accent", "warn"])
        self.assertEqual(ma_chart["datasets"][0]["text"][0], "+43.6% · 1/4단계 · 비중 11.4%")
        self.assertTrue(any("분할매수 전략: MA60주 아래 +5%p → MA120주 아래 +10%p, 회복 시 +50%p." == n for n in self.answer()["notes"]))
        self.assertTrue(any("포모 같은 심리나 잘잘못은 숫자로 증명되지 않아 판단하지 않습니다" in l for l in answer["limitations"]))

    def test_nothing_set_up_is_explained(self):
        out = self.answer({"targets": [], "holdings": [], "unclassified": []})
        self.assertIn("대조할 기준이나 보유 종목이 없습니다", out["bullets"][0])

    def test_formatter_caps_and_cleans(self):
        data = raw()
        data["holdings"] = data["holdings"] * 6
        data["holdings"][0]["strategy"]["status"] = "weird"
        data["token"] = "must-not-leak"
        out = format_tool_result("get_my_rules", data)["result"]
        self.assertEqual(len(out["holdings"]), 15)
        self.assertEqual(out["holdings"][0]["strategy"], {"status": "none"})
        self.assertNotIn("must-not-leak", repr(out))


class RoutingTest(unittest.TestCase):
    def test_questions_about_my_own_rules_use_the_rules_tool(self):
        for q in ("내가 매매 원칙을 위반한 주식이 있어?", "목표 비중대로 가고 있어?", "내가 정한 원칙 점검해줘", "원칙대로 샀는지 봐줘", "내 기준대로 보유 중이야?"):
            self.assertEqual(plan_agent_query(q)["tools"], ["get_my_rules"], q)
        self.assertEqual(plan_agent_query("엔비디아 PER 어때?")["tools"], ["get_valuation"])
        self.assertEqual(plan_agent_query("리밸런싱 계획 보여줘")["tools"], ["get_portfolio_overview"])


class RulesViewHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def snapshot(self):
        def item(symbol, amount, pl=-100):
            return {"symbol": symbol, "name": symbol, "currency": "USD", "marketCountry": "US", "quantity": 1, "marketValue": {"amount": amount}, "profitLoss": {"rateAfterCost": -0.1}}
        return {"usd_krw": 1000.0, "cash_krw": 100.0, "cash_usd": 0.0, "items": [item("UFO", 300), item("NASA", 400), item("TLT", 200), item("SEDG", 100)]}

    def test_targets_strategy_and_unclassified_holdings_are_compared(self):
        cookie = self.login("key-a")
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_rebalance_config(store, [{"label": "우주", "symbols": ["UFO", "NASA"], "target_pct": 10.0}, {"label": "채권", "symbols": ["TLT"], "target_pct": 90.0}], 5.0, 50.0)
        signals = {"UFO": {"status": "ok", "entry_pct": 5.0, "stage_count": 1, "stage_total": 4, "recovered": False,
                           "steps": [{"label": "MA60주 -11.1%", "broken": True}, {"label": "MA200주 +43.6%", "broken": False}]},
                   "NASA": {"status": "ok", "entry_pct": 0.0, "stage_count": 0, "stage_total": 4, "steps": [{"label": "MA60주"}]},
                   "TLT": {"status": "excluded"}, "SEDG": {"status": "none"}}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None), \
                mock.patch.object(server, "strategy_signals_for", return_value=signals):
            status, payload, _ = self.request("POST", "/api/agent/tool", {"name": "get_my_rules"}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        targets = {t["label"]: t for t in result["targets"]}
        self.assertEqual((targets["우주"]["status"], targets["채권"]["status"]), ("over", "under"))                # 목표 대상(700원) 안에서 우주 700/900
        self.assertAlmostEqual(targets["우주"]["actual_pct"], 700 / 900 * 100, places=1)
        self.assertAlmostEqual(targets["우주"]["diff_pct"], 700 / 900 * 100 - 10, places=1)
        rows = {r["symbol"]: r for r in result["holdings"]}
        self.assertEqual(rows["UFO"]["category"], "우주")
        self.assertEqual((rows["UFO"]["strategy"]["stage"], rows["UFO"]["strategy"]["entry_pct"]), (1, 5.0))
        self.assertEqual([m["dist_pct"] for m in rows["UFO"]["strategy"]["ma"]], [-11.1, 43.6])                  # 라벨에서 이평선 대비 위치를 읽는다
        self.assertEqual(rows["NASA"]["strategy"]["ma"], [])                                                       # 데이터 부족
        self.assertIsNone(rows["SEDG"]["category"])
        self.assertEqual(result["unclassified"], ["SEDG"])
        text = repr(payload)
        for needle in ("11100000001", "tok-a", "quantity", "marketValue"):
            self.assertNotIn(needle, text)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_my_rules"})[0], 401)


class ObserveTest(unittest.TestCase):
    def reply(self, message):
        resp = mock.Mock()
        resp.json.return_value = {"message": message}
        resp.raise_for_status = lambda: None
        return mock.Mock(return_value=resp)

    def test_tool_results_are_shown_to_the_model_and_it_can_finish_or_ask_for_more(self):
        calls = [{"name": "get_my_rules", "args": {}}]
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "질문"}]
        done = lm.observe(messages, calls, {"get_my_rules": "우주 목표 10% → 현재 52.8%"}, "질문", post=self.reply({"content": "우주가 목표를 초과했습니다."}))
        self.assertEqual((done["calls"], done["final"]), ([], "우주가 목표를 초과했습니다."))
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["name"], "get_my_rules")
        self.assertEqual(messages[3], {"role": "tool", "name": "get_my_rules", "content": "우주 목표 10% → 현재 52.8%"})
        self.assertIn("심리", messages[4]["content"])                                                              # 최종 답에서 심리·잘못 판단을 쓰지 말라고 지시
        self.assertEqual(messages[-1], {"role": "assistant", "content": "우주가 목표를 초과했습니다."})
        more = lm.observe([{"role": "user", "content": "q"}], calls, {}, "q", post=self.reply({"tool_calls": [{"function": {"name": "get_indicators", "arguments": {"focus": "UFO"}}}]}))
        self.assertEqual([c["name"] for c in more["calls"]], ["get_indicators"])
        self.assertEqual(more["final"], "")

    def test_the_follow_up_request_offers_a_smaller_tool_list_and_only_localhost(self):
        post = self.reply({"content": "끝"})
        lm.observe([{"role": "user", "content": "q"}], [{"name": "get_my_rules", "args": {}}], {}, "q", post=post)
        payload = post.call_args.kwargs["json"]
        self.assertEqual({t["function"]["name"] for t in payload["tools"]}, set(lm.FOLLOW_UP_TOOLS))
        self.assertTrue(post.call_args.args[0].startswith("http://127.0.0.1:11434/"))
        self.assertLessEqual(payload["options"]["num_predict"], 300)


class FinalAnswerGuardTest(unittest.TestCase):
    FACTS = "우주 목표 10% → 현재 52.8% — 42.8%p 초과. UFO 1/4단계 주봉 200선보다 +43.6% 위"

    def test_judgement_psychology_timing_guesses_and_invented_numbers_are_rejected(self):
        self.assertIsNotNone(lm.usable_final("우주 카테고리가 목표 10%를 42.8%p 초과했습니다.", self.FACTS))
        for bad in ("포모성 투자로 볼 수 있는 징후입니다. 우주 52.8%.", "원칙을 위반했습니다. 우주 52.8%.", "이평선 위에서 매수한 상태입니다.", "우주가 목표를 60%p 초과했습니다.", "충동적으로 샀습니다."):
            self.assertIsNone(lm.usable_final(bad, self.FACTS), bad)

    def test_a_cannot_answer_sentence_is_removed_but_the_rest_is_kept(self):
        self.assertEqual(lm.usable_final("우주가 목표를 초과했습니다. 직접 답할 수 없습니다.", self.FACTS), "우주가 목표를 초과했습니다.")
        self.assertIsNone(lm.usable_final("직접 답할 수 없습니다.", self.FACTS))
        self.assertIsNone(lm.usable_final("", self.FACTS))


class LoopServerTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.executed = []
        tax = {"summary": {"estimated_tax_krw": 5000, "capital_gains_tax_krw": 4000, "domestic_transaction_and_etf_tax_krw": 1000}, "warnings": [], "limitations": []}

        def execute(req, name, args=None):
            self.executed.append(name)
            return {"result": tax}
        self.steps = []
        self.state = {"enabled": True, "reachable": True, "ready": True, "model": "qwen2.5:7b-instruct"}

        def observe(messages, calls, digests, question, model, post=None):
            self.seen = (calls, digests)
            return self.steps.pop(0)
        for name, value in {"try_local_plan": mock.Mock(side_effect=lambda req, q, h: {"calls": [{"name": "estimate_liquidation_tax", "args": {}}], "messages": [{"role": "user", "content": q}], "assistant": {}}),
                            "local_state": mock.Mock(side_effect=lambda store: dict(self.state)),
                            "execute_agent_tool": mock.Mock(side_effect=execute)}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(lm, "observe", side_effect=observe)
        p.start()
        self.addCleanup(p.stop)

    def ask(self, cookie, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value={"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": []}), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_the_model_sees_the_tool_result_digest_and_its_final_answer_is_used(self):
        cookie = self.login("key-a")
        self.steps = [{"calls": [], "final": "추정 세금은 5,000원입니다."}]
        payload = self.ask(cookie, "정리하면 세금은")[1]
        self.assertEqual(payload["answer"]["bullets"][0], "요약: 추정 세금은 5,000원입니다.")
        calls, digests = self.seen
        self.assertIn("전량 매도 가정 추정 세금은 5,000원", digests["estimate_liquidation_tax"])                     # 모델은 도구가 만든 한국어 요약 글을 본다
        self.assertEqual(self.executed, ["estimate_liquidation_tax"])

    def test_a_requested_extra_tool_runs_once_and_the_loop_is_capped(self):
        cookie = self.login("key-a")
        self.steps = [{"calls": [{"name": "get_sector_exposure", "args": {}}], "final": ""}, {"calls": [{"name": "get_sector_exposure", "args": {}}, {"name": "get_weekly_news", "args": {}}], "final": ""},
                      {"calls": [{"name": "get_outlook", "args": {}}], "final": ""}]
        payload = self.ask(cookie, "자세히")[1]
        tools = [e["tool"] for e in payload["executions"]]
        self.assertEqual(tools[0], "estimate_liquidation_tax")
        self.assertEqual(len(tools), len(set(tools)))                                                            # 같은 도구를 다시 부르지 않는다
        self.assertLessEqual(len(tools), 1 + lm.MAX_ROUNDS * 2)
        self.assertTrue(any(e.get("reason") == "로컬 모델이 결과를 보고 추가로 선택" for e in payload["executions"][1:]))

    def test_an_ungrounded_or_failed_final_answer_falls_back_to_the_plain_answer(self):
        cookie = self.login("key-a")
        self.steps = [{"calls": [], "final": "추정 세금은 9,999원입니다."}]                                        # 도구 결과에 없는 숫자
        with mock.patch.object(server, "local_summary", return_value=None):
            bullets = self.ask(cookie, "세금")[1]["answer"]["bullets"]
        self.assertFalse(any(b.startswith("요약:") for b in bullets))
        self.assertTrue(any("전량 매도 가정 추정 세금은 5,000원" in b for b in bullets))                          # 도구가 만든 답은 그대로
        import requests as rq
        with mock.patch.object(lm, "observe", side_effect=rq.ConnectionError("꺼짐")), mock.patch.object(server, "local_summary", return_value=None):
            payload = self.ask(cookie, "세금")[1]
        self.assertEqual(payload["mode"], "local_model")
        self.assertTrue(payload["answer"]["bullets"])


if __name__ == "__main__":
    unittest.main()
