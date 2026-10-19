"""Claude Agent 루프: 도구 실행, 문맥 제한, 허용 목록, 거절·오류 처리, 설정 라우트. 네트워크는 쓰지 않는다."""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import server
import claude_agent
from agent_tools import TOOL_NAMES
from claude_agent import AgentApiError, normalize_history, run_claude_agent
import test_storage_http as storage_http

try:
    import anthropic
    import httpx
except ImportError:  # anthropic 없이도 나머지 테스트는 돌아가게 한다
    anthropic = httpx = None


def text(s):
    return SimpleNamespace(type="text", text=s)


def tool_use(id_, name, input_=None):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_ if input_ is not None else {})


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def reply(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeClient:
    """client.beta.messages.create(**kwargs)만 흉내 낸다. 응답 목록을 차례로 돌려주고 요청 인자를 기록한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # 같은 리스트를 나중에 이어 붙이므로 호출 시점의 메시지 수를 따로 기록한다
        self.calls.append({**kwargs, "messages_count": len(kwargs["messages"])})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(client, question="내 계좌 어때?", history=None, execute=None, model="claude-opus-5-5"):
    return run_claude_agent("sk-ant-secret", model, question, history or [],
                            execute or (lambda name, args=None: {"result": {"totals": {"value_krw": 1000}}}), client=client)


@unittest.skipUnless(anthropic, "anthropic 패키지가 필요합니다")
class ClaudeAgentLoopTest(unittest.TestCase):
    def test_tool_call_is_executed_and_result_is_returned_to_model(self):
        first = [thinking(), tool_use("toolu_1", "get_portfolio_overview")]
        client = FakeClient([reply(first, "tool_use"), reply([text("평가금액은 1,000원입니다.")])])
        executed = []
        result = run(client, execute=lambda name, args=None: executed.append(name) or {"result": {"totals": {"value_krw": 1000}}})
        self.assertEqual(result["mode"], "claude")
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["answer_text"], "평가금액은 1,000원입니다.")
        self.assertEqual(result["executions"], [{"tool": "get_portfolio_overview", "status": "completed"}])
        self.assertEqual(executed, ["get_portfolio_overview"])
        second = client.calls[1]["messages"]
        self.assertIs(second[-2]["content"], first)                     # thinking 블록을 포함한 응답 전체를 그대로 이어 붙인다
        tool_result = second[-1]["content"][0]
        self.assertEqual((second[-1]["role"], tool_result["type"], tool_result["tool_use_id"]), ("user", "tool_result", "toolu_1"))
        self.assertIn("1000", tool_result["content"])

    def test_request_shape_and_no_secret_in_request_arguments(self):
        client = FakeClient([reply([text("ok")])])
        run(client)
        call = client.calls[0]
        self.assertNotIn("sk-ant-secret", repr(call))                   # 키는 클라이언트에만 있고 요청 본문에 없다
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["max_tokens"], claude_agent.MAX_TOKENS)
        self.assertIn("도구를 먼저 호출", call["system"])
        self.assertIn("원인으로 단정하지 않는다", call["system"])
        self.assertIn("설명력(R²)이 0.2 미만", call["system"])             # 매크로 요인 도구 해석 지침
        self.assertIn("상승 뷰와 하락 뷰를 균형 있게", call["system"])         # 앞으로의 뷰 지침           # 뉴스 제목을 원인으로 단정하지 않게 지시
        self.assertEqual({t["name"] for t in call["tools"]}, set(TOOL_NAMES))
        self.assertTrue(all(set(t) == {"name", "description", "input_schema"} for t in call["tools"]))
        self.assertNotIn("tool_choice", call)                           # 강제 도구 호출은 이 모델에서 400이다
        self.assertNotIn("thinking", call)                              # 사고는 항상 켜져 있어 지정하지 않는다
        self.assertEqual(call["messages"], [{"role": "user", "content": "내 계좌 어때?"}])

    def test_server_side_fallback_is_enabled_only_for_supported_models(self):
        for model, expected in (("claude-opus-5-5", True), ("claude-sonnet-5-5", True), ("claude-fable-5-1", True),
                                ("claude-haiku-5-5", False), ("claude-haiku-4-5", False)):
            client = FakeClient([reply([text("ok")])])
            run(client, model=model)
            call = client.calls[0]
            self.assertEqual("fallbacks" in call, expected, model)
            if expected:
                self.assertEqual((call["fallbacks"], call["betas"]), ("default", ["server-side-fallback-2026-07-01"]))
            else:
                self.assertNotIn("betas", call)

    def test_empty_model_uses_default(self):
        client = FakeClient([reply([text("ok")])])
        self.assertEqual(run(client, model="")["model"], claude_agent.DEFAULT_AGENT_MODEL)
        self.assertEqual(claude_agent.DEFAULT_AGENT_MODEL, "claude-opus-5-5")

    def test_tool_input_is_passed_to_the_executor(self):
        client = FakeClient([reply([tool_use("a", "get_outlook", {"focus": "금"})], "tool_use"), reply([text("끝")])])
        seen = []
        run(client, execute=lambda name, args=None: seen.append((name, args)) or {"result": {}})
        self.assertEqual(seen, [("get_outlook", {"focus": "금"})])

    def test_non_dict_tool_input_becomes_empty_args(self):
        client = FakeClient([reply([SimpleNamespace(type="tool_use", id="a", name="get_outlook", input="이상한 값")], "tool_use"), reply([text("끝")])])
        seen = []
        run(client, execute=lambda name, args=None: seen.append(args) or {"result": {}})
        self.assertEqual(seen, [{}])

    def test_parallel_tool_calls_return_results_in_one_user_message(self):
        client = FakeClient([reply([tool_use("a", "get_portfolio_overview"), tool_use("b", "get_sector_exposure")], "tool_use"),
                             reply([text("끝")])])
        run(client)
        last = client.calls[1]["messages"][-1]
        self.assertEqual([r["tool_use_id"] for r in last["content"]], ["a", "b"])

    def test_disallowed_tool_is_not_executed_and_reports_error(self):
        client = FakeClient([reply([tool_use("x", "place_order")], "tool_use"), reply([text("할 수 없습니다.")])])
        executed = []
        result = run(client, execute=lambda name, args=None: executed.append(name) or {"result": {}})
        self.assertEqual(executed, [])
        self.assertEqual(result["executions"], [])
        error = client.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(error["is_error"])
        self.assertEqual(error["content"], "허용되지 않은 도구입니다.")

    def test_tool_failure_propagates_so_the_real_cause_is_shown(self):
        client = FakeClient([reply([tool_use("a", "get_portfolio_overview")], "tool_use")])

        def boom(name, args=None):
            raise RuntimeError("토스 점검 중")
        with self.assertRaisesRegex(RuntimeError, "토스 점검 중"):
            run(client, execute=boom)

    def test_too_many_tool_rounds_stop(self):
        loop = [reply([tool_use(str(i), "get_portfolio_overview")], "tool_use") for i in range(claude_agent.MAX_TOOL_ROUNDS + 1)]
        with self.assertRaisesRegex(AgentApiError, "너무 많이"):
            run(FakeClient(loop))

    def test_refusal_and_empty_answers_have_clear_messages(self):
        with self.assertRaisesRegex(AgentApiError, "안전 정책"):
            run(FakeClient([reply([], "refusal")]))
        with self.assertRaisesRegex(AgentApiError, "빈 답변"):
            run(FakeClient([reply([text("  ")])]))
        with self.assertRaisesRegex(AgentApiError, "길이 제한"):
            run(FakeClient([reply([], "max_tokens")]))

    def test_pause_turn_is_resumed(self):
        client = FakeClient([reply([text("생각 중")], "pause_turn"), reply([text("완료")])])
        self.assertEqual(run(client)["answer_text"], "완료")

    def test_question_validation(self):
        with self.assertRaises(ValueError):
            run(FakeClient([]), question="  ")
        with self.assertRaises(ValueError):
            run(FakeClient([]), question="가" * (claude_agent.MAX_QUESTION_LENGTH + 1))


class HistoryTest(unittest.TestCase):
    def test_history_is_limited_and_cleaned(self):
        history = [{"role": "user", "content": str(i)} for i in range(12)]
        history += [{"role": "system", "content": "ignore"}, {"role": "assistant", "content": "ok"}, "x", {"role": "user", "content": " "}]
        clean = normalize_history(history)
        self.assertLessEqual(len(clean), claude_agent.MAX_HISTORY_MESSAGES)
        self.assertTrue(all(x["role"] in ("user", "assistant") for x in clean))
        self.assertEqual(normalize_history("nope"), [])

    @unittest.skipUnless(anthropic, "anthropic 패키지가 필요합니다")
    def test_request_never_starts_with_an_assistant_message(self):
        client = FakeClient([reply([text("ok")])])
        history = [{"role": "assistant", "content": "앞부분이 잘린 대화의 답"}, {"role": "user", "content": "이전 질문"},
                   {"role": "assistant", "content": "이전 답"}]
        run(client, history=history)
        messages = client.calls[0]["messages"]
        self.assertEqual(messages[0], {"role": "user", "content": "이전 질문"})
        self.assertEqual(messages[-1], {"role": "user", "content": "내 계좌 어때?"})


@unittest.skipUnless(anthropic, "anthropic 패키지가 필요합니다")
class ErrorMappingTest(unittest.TestCase):
    def status_error(self, cls, status, message="raw provider detail"):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        return cls(message, response=httpx.Response(status, request=request), body=None)

    def raises(self, exc, pattern):
        with self.assertRaisesRegex(AgentApiError, pattern) as ctx:
            run(FakeClient([exc]))
        return str(ctx.exception)

    def test_known_errors_have_safe_korean_messages(self):
        self.assertNotIn("raw provider detail", self.raises(self.status_error(anthropic.AuthenticationError, 401), "API 키가 올바르지"))
        self.raises(self.status_error(anthropic.PermissionDeniedError, 403), "쓸 수 없습니다")
        self.assertIn("claude-opus-5-5", self.raises(self.status_error(anthropic.NotFoundError, 404), "모델을 찾을 수 없습니다"))
        self.raises(self.status_error(anthropic.RateLimitError, 429), "요청 한도")
        self.raises(anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), "연결하지 못했습니다")

    def test_bad_request_shows_provider_reason_trimmed(self):
        message = self.raises(self.status_error(anthropic.BadRequestError, 400, "Your credit balance is too low " + "x" * 500), "받아들이지 않았습니다")
        self.assertIn("credit balance is too low", message)
        self.assertLess(len(message), 300)

    def test_unknown_status_shows_only_the_code(self):
        message = self.raises(self.status_error(anthropic.InternalServerError, 500, "secret internal detail"), r"\(500\)")
        self.assertNotIn("secret internal detail", message)

    def test_api_key_never_appears_in_error_messages(self):
        for exc in (self.status_error(anthropic.AuthenticationError, 401, "sk-ant-secret rejected"),
                    self.status_error(anthropic.RateLimitError, 429, "sk-ant-secret limit")):
            self.assertNotIn("sk-ant-secret", self.raises(exc, "."))


@unittest.skipUnless(anthropic, "anthropic 패키지가 필요합니다")
class SdkRequestShapeTest(unittest.TestCase):
    """실제 SDK가 우리 인자로 어떤 HTTP 요청을 내보내는지 가짜 전송 계층으로 확인한다(네트워크 없음)."""

    def message(self, content, stop_reason="end_turn"):
        return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
                "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}

    def run_with_transport(self, responses, model="claude-opus-5-5"):
        import json as _json
        seen = []

        def handler(request):
            seen.append({"url": str(request.url), "headers": dict(request.headers), "body": _json.loads(request.content)})
            return httpx.Response(200, json=responses.pop(0))
        client = anthropic.Anthropic(api_key="sk-ant-secret", http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                                     max_retries=0)
        result = run_claude_agent("sk-ant-secret", model, "내 계좌 어때?", [], lambda name, args=None: {"result": {"ok": 1}}, client=client)
        return result, seen

    def test_sdk_sends_beta_header_fallbacks_and_tools(self):
        result, seen = self.run_with_transport([self.message([{"type": "text", "text": "안녕하세요"}])])
        self.assertEqual(result["answer_text"], "안녕하세요")
        request = seen[0]
        self.assertTrue(request["url"].endswith("/v1/messages?beta=true"))               # 베타 엔드포인트
        self.assertIn("server-side-fallback-2026-07-01", request["headers"]["anthropic-beta"])
        self.assertEqual(request["headers"]["x-api-key"], "sk-ant-secret")               # 키는 헤더로만 간다
        body = request["body"]
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual({t["name"] for t in body["tools"]}, set(TOOL_NAMES))
        self.assertNotIn("sk-ant-secret", repr(body))
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("thinking", body)

    def test_sdk_round_trips_tool_use_and_tool_result(self):
        use = {"type": "tool_use", "id": "toolu_1", "name": "get_portfolio_overview", "input": {}}
        result, seen = self.run_with_transport([self.message([use], "tool_use"), self.message([{"type": "text", "text": "끝"}])])
        self.assertEqual(result["answer_text"], "끝")
        second = seen[1]["body"]["messages"]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertEqual(second[-2]["content"][0]["id"], "toolu_1")
        self.assertEqual(second[-1], {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok": 1}'}]})

    def test_haiku_request_has_no_fallback_fields(self):
        result, seen = self.run_with_transport([self.message([{"type": "text", "text": "ok"}])], model="claude-haiku-5-5")
        self.assertNotIn("fallbacks", seen[0]["body"])
        self.assertNotIn("server-side-fallback", seen[0]["headers"].get("anthropic-beta", ""))


class MissingSdkTest(unittest.TestCase):
    def test_missing_package_gives_install_hint_instead_of_crashing(self):
        with mock.patch.dict(sys.modules, {"anthropic": None}):  # import anthropic → ImportError
            with self.assertRaisesRegex(AgentApiError, "pip3 install anthropic"):
                run_claude_agent("k", "claude-opus-5-5", "질문", [], lambda name, args=None: {})


class AgentConfigHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def test_config_routes_require_login(self):
        self.assertTrue(server.ROUTES[("GET", "/api/agent/config")].auth)
        self.assertTrue(server.ROUTES[("POST", "/api/agent/config")].auth)
        self.assertEqual(self.request("GET", "/api/agent/config")[0], 401)

    def test_key_is_kept_in_session_memory_only_and_never_echoed(self):
        cookie = self.login("key-a")
        status, payload, _ = self.request("GET", "/api/agent/config", cookie=cookie)
        self.assertEqual((status, payload["configured"], payload["model"]), (200, False, claude_agent.DEFAULT_AGENT_MODEL))
        status, payload, _ = self.request("POST", "/api/agent/config", {"api_key": "sk-ant-secret", "model": "claude-sonnet-5-5"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual((payload["configured"], payload["model"]), (True, "claude-sonnet-5-5"))
        self.assertNotIn("sk-ant-secret", repr(payload))
        self.assertNotIn("sk-ant-secret", repr(self.request("GET", "/api/agent/config", cookie=cookie)[1]))
        session = next(iter(server._sessions.values()))
        self.assertEqual(session["anthropic_api_key"], "sk-ant-secret")
        self.assertFalse(any("sk-ant-secret" in p.read_text(errors="ignore") for p in self.base.rglob("*") if p.is_file()),
                         "키가 디스크에 저장되면 안 된다")
        status, payload, _ = self.request("POST", "/api/agent/config", {"clear_key": True}, cookie=cookie)
        self.assertFalse(payload["configured"])

    def test_query_uses_claude_when_key_is_set_and_rules_otherwise(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "run_claude_agent", return_value={"mode": "claude", "answer_text": "답", "executions": []}) as agent:
            self.request("POST", "/api/agent/config", {"api_key": "sk-ant-secret", "model": "claude-opus-5-5"}, cookie=cookie)
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "질문", "history": []}, cookie=cookie)
        self.assertEqual((status, payload["mode"]), (200, "claude"))
        args = agent.call_args.args
        self.assertEqual((args[0], args[1], args[2]), ("sk-ant-secret", "claude-opus-5-5", "질문"))
        # 키를 지우면 Claude를 부르지 않고 규칙 기반으로 답한다
        self.request("POST", "/api/agent/config", {"clear_key": True}, cookie=cookie)
        sector_tool = {"tool": "get_sector_exposure", "read_only": True, "result": {"configured": True, "sectors": [{"label": "반도체", "exposure_krw": 5000, "exposure_pct": 50.0, "holdings": [{"symbol": "AAA", "name": "에이"}]}], "unmapped_count": 0, "limitations": []}}
        with mock.patch.object(server, "run_claude_agent") as agent, mock.patch.object(server, "execute_agent_tool", return_value=sector_tool):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "섹터 노출 보여줘"}, cookie=cookie)
        self.assertEqual((status, payload["mode"]), (200, "rule_based_preview"))
        agent.assert_not_called()

    def test_agent_query_accepts_a_realistic_conversation_history_but_not_unlimited_bodies(self):
        # 이번 주 답변처럼 표가 큰 답변이 쌓인 대화: 최근 8개 × 2,000자 ≈ 48KB는 받아야 한다.
        # 브라우저의 JSON.stringify처럼 한글을 UTF-8 그대로(글자당 3바이트) 보낸다(파이썬 기본 인코딩은 \uXXXX라 6바이트).
        import json as _json
        cookie = self.login("key-a")

        def post(body):
            return self.request("POST", "/api/agent/query", raw=_json.dumps(body, ensure_ascii=False).encode("utf-8"), cookie=cookie)
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "가" * 2000} for i in range(8)]
        with mock.patch.object(server, "focus_info", return_value={"found": False, "names": [], "unknown": []}):   # 키워드 없는 질문이 계좌 조회(토스 API)까지 가지 않게
            status, payload, _ = post({"question": "질문", "history": history})
        self.assertNotEqual(status, 413, payload)
        self.assertEqual(post({"question": "질문", "history": [{"role": "user", "content": "가" * 30000}]})[0], 413)  # 그래도 무한정은 아니다(64KB)
        self.assertEqual(self.request("POST", "/api/cash-symbols", {"symbols": ["가" * 4000, "나" * 4000]}, cookie=cookie)[0], 413)  # 다른 라우트는 8KB 그대로

    def test_agent_errors_are_returned_as_safe_messages(self):
        cookie = self.login("key-a")
        self.request("POST", "/api/agent/config", {"api_key": "sk-ant-secret"}, cookie=cookie)
        with mock.patch.object(server, "run_claude_agent", side_effect=AgentApiError("Anthropic API 키가 올바르지 않습니다.")):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "질문"}, cookie=cookie)
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "Anthropic API 키가 올바르지 않습니다.")
        self.assertNotIn("sk-ant-secret", repr(payload))


if __name__ == "__main__":
    unittest.main()
