"""로컬 모델(local_model.py + 서버 연결): 도구 목록·인자 정리·응답 해석·상태 확인·요약의 숫자 근거 검사, 서버에서 계획 → 실행 → 요약, 되돌아가기(꺼짐·실패·도구 없음)."""
from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import local_model as lm
import test_storage_http as storage_http
from agent_tools import TOOL_DEFINITIONS, TOOL_NAMES


def reply(message, status=200):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = {"message": message}
    resp.raise_for_status = lambda: None if status < 400 else (_ for _ in ()).throw(requests.HTTPError("오류"))
    return resp


class ToolSpecTest(unittest.TestCase):
    def test_every_tool_is_offered_compactly_with_a_one_line_hint(self):
        specs = lm.tool_specs()
        self.assertEqual({s["function"]["name"] for s in specs}, set(TOOL_NAMES))
        self.assertEqual(set(lm.HINTS), set(TOOL_NAMES))                                                 # 새 도구를 만들면 안내도 같이
        self.assertLess(len(json.dumps(specs, ensure_ascii=False)), 7500)                              # 작은 모델의 문맥을 아낀다
        indicators = next(s for s in specs if s["function"]["name"] == "get_indicators")["function"]
        self.assertEqual(indicators["parameters"]["properties"]["indicators"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(indicators["parameters"]["properties"]["focus"]["description"], "종목 이름·코드")
        response = next(s for s in specs if s["function"]["name"] == "get_factor_response")["function"]["parameters"]
        self.assertEqual(response["required"], ["factor"])
        self.assertEqual(response["properties"]["direction"]["enum"], ["up", "down"])


class CleanArgsTest(unittest.TestCase):
    def test_unknown_keys_bad_types_and_values_outside_the_schema_are_dropped(self):
        got = lm.clean_args("get_factor_response", {"factor": "금리", "direction": "sideways", "assets": ["금", "", "나스닥", {"x": 1}], "horizon_days": 7, "bogus": 1}, "질문")
        self.assertEqual(got, {"factor": "금리", "assets": ["금", "나스닥"]})                               # 방향·기간은 허용값 밖이라 버린다
        self.assertEqual(lm.clean_args("get_indicators", {"indicators": ["rsi", "bogus"], "rsi_period": 10.0, "overbought": "x"}, "q")["indicators"], ["rsi"])
        self.assertEqual(lm.clean_args("get_indicators", {"rsi_period": 10.0}, "q")["rsi_period"], 10)
        self.assertNotIn("rsi_period", lm.clean_args("get_indicators", {"rsi_period": 10.5}, "q"))

    def test_focus_tools_get_the_model_choice_first_and_the_question_after(self):
        got = lm.clean_args("get_valuation", {"focus": "엔비디아"}, "엔비디아 PER 어때?")
        self.assertEqual(got, {"focus": "엔비디아 엔비디아 PER 어때?"})
        self.assertEqual(lm.clean_args("get_valuation", {}, "내 종목 PER")["focus"], "내 종목 PER")           # 모델이 종목을 안 골라도 질문으로 찾는다
        self.assertNotIn("focus", lm.clean_args("get_sector_exposure", {"focus": "x"}, "q"))          # focus를 받지 않는 도구
        self.assertLessEqual(len(lm.clean_args("get_valuation", {"focus": "가" * 500}, "나" * 500)["focus"]), 200)

    def test_arguments_may_arrive_as_a_json_string_and_garbage_is_ignored(self):
        self.assertEqual(lm.clean_args("get_factor_response", '{"factor": "달러"}', "q"), {"factor": "달러"})
        self.assertEqual(lm.clean_args("get_factor_response", "not json", "q"), {})
        self.assertEqual(lm.clean_args("get_factor_response", None, "q"), {})
        self.assertEqual(lm.clean_args("nope", {"a": 1}, "q"), {})


class ParseCallsTest(unittest.TestCase):
    def test_only_allowed_tools_deduped_and_capped(self):
        message = {"tool_calls": [{"function": {"name": "get_valuation", "arguments": {"focus": "A"}}}, {"function": {"name": "place_order", "arguments": {}}},
                                  {"function": {"name": "get_valuation", "arguments": {"focus": "B"}}}, {"function": {"name": "get_outlook", "arguments": {}}},
                                  {"function": {"name": "get_weekly_news", "arguments": {}}}, {"function": {"name": "get_macro_drivers", "arguments": {}}}, {}, None]}
        calls = lm.parse_calls(message, "질문")
        self.assertEqual([c["name"] for c in calls], ["get_valuation", "get_outlook", "get_weekly_news"])     # 주문 같은 도구는 없고 3개까지
        self.assertEqual(calls[0]["args"]["focus"], "A 질문")
        self.assertEqual(lm.parse_calls({"content": "해당없음"}, "q"), [])
        self.assertEqual(lm.parse_calls(None, "q"), [])


class TextCallTest(unittest.TestCase):
    def test_calls_written_as_text_are_recovered_with_cleaned_arguments(self):
        got = lm.parse_text_calls('get_factor_response(factor="금리", direction="up", assets=["금","나스닥"])', "질문")
        self.assertEqual(got, [{"name": "get_factor_response", "args": {"factor": "금리", "direction": "up", "assets": ["금", "나스닥"]}}])
        self.assertEqual(lm.parse_text_calls("get_sector_exposure", "q"), [{"name": "get_sector_exposure", "args": {}}])
        self.assertEqual([c["name"] for c in lm.parse_text_calls("get_portfolio_overview, get_weekly_risk_report", "q")], ["get_portfolio_overview", "get_weekly_risk_report"])
        none = lm.parse_text_calls("get_valuation(focus=None)", "내 종목 PER")
        self.assertEqual(none, [{"name": "get_valuation", "args": {"focus": "내 종목 PER"}}])                  # None은 비우고 질문으로 찾는다
        bad = lm.parse_text_calls('get_indicators(focus="테슬라", indicators=["bolinger"], rsi_period=99)', "q")[0]["args"]
        self.assertNotIn("indicators", bad)                                                                  # 철자 틀린 값·범위 밖 값은 버린다(도구 쪽에서 기본값)
        self.assertEqual(lm.parse_text_calls("place_order(symbol=1) 해당없음", "q"), [])                       # 모르는 도구 이름은 무시
        self.assertEqual(lm.parse_text_calls("", "q"), [])

    def test_plan_falls_back_to_the_text_form_when_there_is_no_structured_call(self):
        post = mock.Mock(return_value=reply({"content": 'get_valuation(focus="엔비디아")'}))
        got = lm.plan("엔비디아 PER", [], post=post)
        self.assertEqual([c["name"] for c in got["calls"]], ["get_valuation"])
        self.assertEqual(got["reply"], 'get_valuation(focus="엔비디아")')


class StatusAndPlanTest(unittest.TestCase):
    def setUp(self):
        lm.reset_status_cache()
        self.addCleanup(lm.reset_status_cache)

    def get(self, names):
        resp = mock.Mock()
        resp.json.return_value = {"models": [{"name": n} for n in names]}
        resp.raise_for_status = lambda: None
        return mock.Mock(return_value=resp)

    def test_status_reachable_ready_missing_and_down(self):
        self.assertEqual(lm.status("qwen2.5:7b-instruct", self.get(["qwen2.5:7b-instruct"]))["ready"], True)
        lm.reset_status_cache()
        got = lm.status("qwen2.5:7b-instruct", self.get(["llama3:8b"]))
        self.assertEqual((got["reachable"], got["ready"]), (True, False))                                  # 켜져 있지만 모델이 없다
        lm.reset_status_cache()
        down = mock.Mock(side_effect=requests.ConnectionError("꺼짐"))
        got = lm.status("qwen2.5:7b-instruct", down)
        self.assertEqual((got["reachable"], got["ready"]), (False, False))

    def test_status_is_cached_briefly(self):
        get = self.get(["qwen2.5:7b-instruct"])
        t = [100.0]
        lm.status("qwen2.5:7b-instruct", get, now=lambda: t[0])
        t[0] = 105.0
        lm.status("qwen2.5:7b-instruct", get, now=lambda: t[0])
        self.assertEqual(get.call_count, 1)
        t[0] = 120.0
        lm.status("qwen2.5:7b-instruct", get, now=lambda: t[0])
        self.assertEqual(get.call_count, 2)

    def test_plan_sends_system_history_question_and_tools_to_localhost_only(self):
        post = mock.Mock(return_value=reply({"tool_calls": [{"function": {"name": "get_valuation", "arguments": {"focus": "엔비디아"}}}]}))
        history = [{"role": "user", "content": f"이전 질문 {i}"} for i in range(10)]
        got = lm.plan("엔비디아 PER 어때?", history, "qwen2.5:7b-instruct", post)
        self.assertEqual([c["name"] for c in got["calls"]], ["get_valuation"])
        url, payload = post.call_args.args[0], post.call_args.kwargs["json"]
        self.assertTrue(url.startswith("http://127.0.0.1:11434/"))
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][-1], {"role": "user", "content": "엔비디아 PER 어때?"})
        self.assertEqual(len(payload["messages"]), 1 + lm.MAX_HISTORY + 1)                                  # 이전 대화는 최근 6개만
        self.assertEqual(len(payload["tools"]), len(TOOL_NAMES))
        self.assertFalse(payload["stream"])
        text = json.dumps(payload, ensure_ascii=False)
        for needle in ("tok-a", "앱키", "계좌번호", "api_key"):
            self.assertNotIn(needle, text)

    def test_plan_without_tools_returns_the_models_words_and_errors_propagate(self):
        got = lm.plan("점심 뭐 먹지", [], post=mock.Mock(return_value=reply({"content": "해당없음"})))
        self.assertEqual((got["calls"], got["reply"]), ([], "해당없음"))
        with self.assertRaises(requests.RequestException):
            lm.plan("q", [], post=mock.Mock(return_value=reply({}, status=500)))


class GroundingTest(unittest.TestCase):
    FACTS = "애플(개별주): 고평가 쪽 — 신호 4개 중 비싼 쪽 4\n섹터 배수 기준 추정 범위 $146~$272 (중앙 $248) — 현재가 $343\nPER 39.4 · 기준 28.4 (+39%)"

    def test_numbers_in_a_summary_must_exist_in_the_facts(self):
        self.assertTrue(lm.grounded("애플은 PER 39.4로 섹터 기준 28.4보다 약 39% 비쌉니다.", self.FACTS))
        self.assertTrue(lm.grounded("현재가는 $343이고 추정 범위는 $146~$272입니다.", self.FACTS))
        self.assertFalse(lm.grounded("애플의 적정 주가는 $300입니다.", self.FACTS))                           # 지어낸 숫자
        self.assertFalse(lm.grounded("PER이 55입니다.", self.FACTS))
        self.assertTrue(lm.grounded("숫자 없는 문장입니다.", self.FACTS))
        self.assertTrue(lm.grounded("4개 중 4개입니다.", self.FACTS))                                        # 한 자리 숫자는 문장 구조라 검사하지 않는다

    def test_summarize_keeps_only_grounded_nonempty_text_and_trims_the_input(self):
        ok = mock.Mock(return_value=reply({"content": "애플은 섹터 PER 28.4보다 높은 39.4라서 비싸 보입니다."}))
        self.assertIn("39.4", lm.summarize("애플 고평가야?", self.FACTS, post=ok))
        refuse = mock.Mock(return_value=reply({"content": "이 결과로는 질문에 직접 답할 수 없습니다."}))
        self.assertIsNone(lm.summarize("q", self.FACTS, post=refuse))                                         # 모델이 답이 안 된다고 하면 요약을 붙이지 않는다
        bad = mock.Mock(return_value=reply({"content": "애플의 목표가는 500달러입니다."}))
        self.assertIsNone(lm.summarize("q", self.FACTS, post=bad))
        self.assertIsNone(lm.summarize("q", self.FACTS, post=mock.Mock(return_value=reply({"content": "네"}))))
        long = "가" * 10000
        lm.summarize("q", long, post=ok)
        sent = ok.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertLess(len(sent), lm.SUMMARY_INPUT_MAX + 600)


class LocalServerTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        lm.reset_status_cache()
        self.addCleanup(lm.reset_status_cache)
        self.calls = [{"name": "get_sector_exposure", "args": {}}]
        self.final = None
        self.sector = {"configured": True, "sectors": [{"label": "반도체", "exposure_krw": 5000, "exposure_pct": 50.0, "holdings": [{"symbol": "AAA", "name": "에이"}]},
                                                      {"label": "에너지", "exposure_krw": 3000, "exposure_pct": 30.0, "holdings": [{"symbol": "BBB", "name": "비"}]}], "unmapped_count": 0, "limitations": []}
        self.executed = []

        def execute(req, name, args=None):
            self.executed.append((name, args))
            return {"result": self.sector}
        self.state = {"enabled": True, "reachable": True, "ready": True, "model": "qwen2.5:7b-instruct"}
        for name, value in {"try_local_plan": mock.Mock(side_effect=lambda req, q, h: {"calls": list(self.calls), "messages": [{"role": "user", "content": q}], "assistant": {}} if self.calls else None),
                            "local_follow_up": mock.Mock(side_effect=lambda req, q, plan, calls, results, run: self.final),
                            "local_state": mock.Mock(side_effect=lambda store: dict(self.state)),
                            "local_summary": mock.Mock(return_value="반도체 노출은 5,000원입니다."),
                            "execute_agent_tool": mock.Mock(side_effect=execute)}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def ask(self, cookie, question, history=None):
        body = {"question": question} if history is None else {"question": question, "history": history}
        with mock.patch.object(server, "fetch_account_snapshot", return_value={"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": []}), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", body, cookie=cookie)

    def test_the_local_models_tool_choice_is_executed_and_summarised(self):
        cookie = self.login("key-a")
        status, payload, _ = self.ask(cookie, "내 투자가 어디에 쏠려 있는지 정리해줘")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "local_model")
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_sector_exposure"])
        self.assertEqual(self.executed[0][0], "get_sector_exposure")
        bullets = payload["answer"]["bullets"]
        self.assertEqual(bullets[0], "요약: 반도체 노출은 5,000원입니다.")                                       # 모델 요약이 맨 위
        self.assertTrue(any("반도체: 내 노출 5,000원" in b for b in bullets))                       # 아래는 도구가 만든 숫자
        self.assertTrue(payload["answer"]["charts"])
        self.assertTrue(any("로컬 모델(qwen2.5:7b-instruct)이 질문을 이해해 도구를 골랐습니다" in n for n in payload["answer"]["notes"]))
        self.assertNotIn("teach", payload["answer"])                                                          # 로컬이 골랐으면 가르치기 버튼 없음

    def test_the_models_arguments_reach_the_tool(self):
        cookie = self.login("key-a")
        self.calls = [{"name": "get_valuation", "args": {"focus": "엔비디아 엔비디아 PER"}}]
        self.ask(cookie, "엔비디아 PER")
        self.assertEqual(self.executed[0], ("get_valuation", {"focus": "엔비디아 엔비디아 PER"}))

    def test_without_a_plan_or_when_disabled_or_unready_the_rule_based_path_runs(self):
        cookie = self.login("key-a")
        self.calls = []
        payload = self.ask(cookie, "섹터 노출 보여줘")[1]
        self.assertEqual(payload["mode"], "rule_based_preview")                                               # 모델이 도구를 못 골랐다 → 규칙 기반
        self.assertEqual(payload["plan"]["tools"], ["get_sector_exposure"])
        unknown = self.ask(cookie, "오늘 점심 뭐 먹지")[1]
        self.assertEqual(unknown["answer"]["unknown_kind"], "unclear")                                        # 못 알아들은 질문은 정책 응답

    def test_trade_requests_are_refused_even_if_the_model_would_pick_a_tool(self):
        cookie = self.login("key-a")
        self.calls = [{"name": "get_sector_exposure", "args": {}}]                                       # 모델이 조회 도구를 골라도
        payload = self.ask(cookie, "엔비디아 지금 당장 전량 매도해줘")[1]
        self.assertEqual(payload["executions"], [])
        self.assertEqual(payload["answer"]["unknown_kind"], "trade")
        server.try_local_plan.assert_not_called()                                                             # 모델에 묻지도 않는다

    def test_a_summary_that_cannot_be_grounded_is_left_out(self):
        cookie = self.login("key-a")
        server.local_summary.return_value = None
        bullets = self.ask(cookie, "정리하면 섹터는")[1]["answer"]["bullets"]
        self.assertFalse(any(b.startswith("요약:") for b in bullets))

    def test_a_taught_sentence_beats_the_model(self):
        cookie = self.login("key-a")
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_learned_intent(store, "이상한 말투 질문", "sector")
        payload = self.ask(cookie, "이상한 말투 질문")[1]
        self.assertEqual(payload["mode"], "rule_based_preview")
        self.assertEqual(payload["plan"]["tools"], ["get_sector_exposure"])

    def test_ai_key_mode_is_untouched_by_the_local_model(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "run_claude_agent", return_value={"mode": "claude", "answer_text": "ok", "executions": []}) as run:
            self.request("POST", "/api/agent/config", {"api_key": "sk-ant-test"}, cookie=cookie)
            status, payload, _ = self.ask(cookie, "엔비디아 PER")
        self.assertEqual((status, payload["mode"]), (200, "claude"))
        self.assertTrue(run.called)
        server.try_local_plan.assert_not_called()


class LocalSettingsHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        lm.reset_status_cache()
        self.addCleanup(lm.reset_status_cache)

    def test_status_toggle_and_the_config_payload(self):
        cookie = self.login("key-a")
        status, payload, _ = self.request("GET", "/api/agent/local", cookie=cookie)      # 기본 시험 환경: Ollama 없음
        if True:
            self.assertEqual((status, payload["enabled"], payload["reachable"], payload["ready"]), (200, True, False, False))
            lm.reset_status_cache()
            self.assertEqual(self.request("POST", "/api/agent/local", {"enabled": False}, cookie=cookie)[1]["enabled"], False)
            lm.reset_status_cache()
            cfg = self.request("GET", "/api/agent/config", cookie=cookie)[1]
            self.assertEqual((cfg["local"]["enabled"], cfg["configured"]), (False, False))
            self.assertEqual(self.request("POST", "/api/agent/local", {"enabled": True}, cookie=cookie)[1]["enabled"], True)

    def test_requires_login(self):
        self.assertEqual(self.request("GET", "/api/agent/local")[0], 401)
        self.assertEqual(self.request("POST", "/api/agent/local", {"enabled": True})[0], 401)

    def test_the_page_has_the_toggle(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        self.assertIn('id="agentLocalEnable"', (root / "dashboard.html").read_text(encoding="utf-8"))
        self.assertIn("/api/agent/local", (root / "static/js/agent.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
