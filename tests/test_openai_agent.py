"""Responses API Agent 루프: 도구 실행, 문맥 제한, 오류 처리."""
from __future__ import annotations

import unittest

from helpers import server
from openai_agent import AgentApiError, normalize_history, run_openai_agent


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class OpenAiAgentTest(unittest.TestCase):
    def test_tool_call_is_executed_and_returned_to_model(self):
        requests_seen = []
        responses = [
            FakeResponse(200, {"output": [{
                "type": "function_call", "name": "get_portfolio_overview",
                "call_id": "call_1", "arguments": "{}",
            }]}),
            FakeResponse(200, {"output": [{
                "type": "message", "content": [{"type": "output_text", "text": "평가금액은 1,000원입니다."}],
            }]}),
        ]

        def fake_post(url, **kwargs):
            requests_seen.append(kwargs)
            return responses.pop(0)

        result = run_openai_agent(
            "secret-key", "test-model", "내 계좌 어때?", [],
            lambda name: {"result": {"totals": {"value_krw": 1000}}},
            fake_post,
        )
        self.assertEqual(result["answer_text"], "평가금액은 1,000원입니다.")
        self.assertEqual(result["executions"][0]["tool"], "get_portfolio_overview")
        outputs = requests_seen[1]["json"]["input"]
        self.assertTrue(any(x.get("type") == "function_call_output" for x in outputs))
        self.assertNotIn("secret-key", repr(requests_seen[0]["json"]))
        self.assertFalse(requests_seen[0]["json"]["store"])

    def test_history_is_limited_and_cleaned(self):
        history = [{"role": "user", "content": str(i)} for i in range(12)]
        history += [{"role": "system", "content": "ignore"}, {"role": "assistant", "content": "ok"}]
        clean = normalize_history(history)
        self.assertLessEqual(len(clean), 8)
        self.assertTrue(all(x["role"] in ("user", "assistant") for x in clean))

    def test_bad_key_has_safe_message(self):
        def fake_post(url, **kwargs):
            return FakeResponse(401, {"error": {"message": "raw provider detail"}})
        with self.assertRaisesRegex(AgentApiError, "API 키"):
            run_openai_agent("bad", "test-model", "질문", [], lambda name: {}, fake_post)

    def test_agent_config_routes_require_login(self):
        self.assertTrue(server.ROUTES[("GET", "/api/agent/config")].auth)
        self.assertTrue(server.ROUTES[("POST", "/api/agent/config")].auth)


if __name__ == "__main__":
    unittest.main()
