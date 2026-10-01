"""OpenAI Responses API를 이용한 읽기 전용 포트폴리오 Agent 실행 루프."""
from __future__ import annotations

import json
from typing import Callable

import requests

from agent_tools import TOOL_DEFINITIONS, TOOL_NAMES

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_AGENT_MODEL = "gpt-6-luna"
MAX_TOOL_ROUNDS = 4
MAX_HISTORY_MESSAGES = 8
MAX_QUESTION_LENGTH = 1000

SYSTEM_INSTRUCTIONS = """당신은 사용자의 토스증권 포트폴리오를 점검하는 한국어 AI Agent다.
- 계좌 수치나 현재 상태에 관한 답은 반드시 제공된 읽기 전용 도구를 먼저 호출하고 그 결과만 근거로 쓴다.
- 도구 결과에 없는 숫자, 종목, 원인, 전망을 지어내지 않는다.
- 매수·매도를 지시하거나 수익을 보장하지 않는다. 사용자가 설정한 목표에 따른 계산과 위험 신호를 설명한다.
- 세금은 추정치이며 실제 신고 금액이 아님을 밝힌다.
- 답은 간결하게 쓰되, 결론과 근거 및 중요한 계산 한계를 포함한다.
- 도구로 답할 수 없는 질문이면 가능한 범위를 솔직하게 설명한다."""


class AgentApiError(RuntimeError):
    pass


def _response_tools() -> list[dict]:
    return [{
        "type": "function",
        "name": item["name"],
        "description": item["description"],
        "parameters": item["input_schema"],
        "strict": True,
    } for item in TOOL_DEFINITIONS]


def normalize_history(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    clean = []
    for item in value[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if content:
            clean.append({"role": item["role"], "content": content[:2000]})
    return clean


def _post_response(api_key: str, payload: dict, request_post: Callable = requests.post) -> dict:
    try:
        response = request_post(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
    except requests.RequestException as e:
        raise AgentApiError("OpenAI API에 연결하지 못했습니다.") from e
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message")
        except (ValueError, AttributeError):
            detail = None
        if response.status_code == 401:
            raise AgentApiError("OpenAI API 키가 올바르지 않습니다.")
        raise AgentApiError(f"OpenAI API 오류 ({response.status_code})" + (f": {detail}" if detail else ""))
    try:
        return response.json()
    except ValueError as e:
        raise AgentApiError("OpenAI API 응답을 해석하지 못했습니다.") from e


def _output_text(response: dict) -> str:
    if response.get("output_text"):
        return str(response["output_text"]).strip()
    chunks = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                chunks.append(str(content["text"]))
    return "\n".join(chunks).strip()


def run_openai_agent(api_key: str, model: str, question: str, history: list[dict],
                     execute_tool: Callable[[str], dict], request_post: Callable = requests.post) -> dict:
    """모델의 함수 호출을 최대 4회 실행하고 최종 한국어 답변과 실행 추적을 반환한다."""
    question = str(question or "").strip()
    if not question:
        raise ValueError("질문을 입력해 주세요.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ValueError(f"질문은 {MAX_QUESTION_LENGTH}자 이하여야 합니다.")
    input_items = normalize_history(history) + [{"role": "user", "content": question}]
    trace = []
    base = {
        "model": model or DEFAULT_AGENT_MODEL,
        "instructions": SYSTEM_INSTRUCTIONS,
        "tools": _response_tools(),
        "tool_choice": "auto",
        "store": False,
        "reasoning": {"effort": "low"},
    }
    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = _post_response(api_key, {**base, "input": input_items}, request_post)
        output = response.get("output", [])
        calls = [item for item in output if item.get("type") == "function_call"]
        input_items.extend(output)
        if not calls:
            answer = _output_text(response)
            if not answer:
                raise AgentApiError("Agent가 빈 답변을 반환했습니다.")
            return {
                "mode": "openai",
                "model": base["model"],
                "answer_text": answer,
                "executions": trace,
            }
        for call in calls:
            name = str(call.get("name") or "")
            call_id = str(call.get("call_id") or "")
            if name not in TOOL_NAMES or not call_id:
                result = {"error": "허용되지 않은 도구입니다."}
            else:
                result = execute_tool(name)["result"]
                trace.append({"tool": name, "status": "completed"})
            input_items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
    raise AgentApiError("Agent가 도구를 너무 많이 호출해 중단했습니다.")
