"""Claude API(Anthropic SDK)를 이용한 읽기 전용 포트폴리오 Agent 실행 루프.

모델에는 agent_tools.py가 허용한 읽기 전용 도구만 준다. 서버가 도구를 실행해 축약 결과만 돌려주고, 토스 앱키·토큰·
계좌번호와 원본 응답은 모델에 넘기지 않는다. anthropic 패키지는 AI 모드를 쓸 때만 불러온다(없으면 규칙 기반 모드는
그대로 동작하고, AI 모드는 설치 안내를 보여준다).
"""
from __future__ import annotations

import json
from typing import Callable

from agent_tools import TOOL_DEFINITIONS, TOOL_NAMES

DEFAULT_AGENT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 4
MAX_HISTORY_MESSAGES = 8
MAX_QUESTION_LENGTH = 1000
REQUEST_TIMEOUT_SECONDS = 90.0
# 안전 분류기가 요청을 거절하면 서버가 같은 요청을 다른 모델로 다시 돌리게 하는 옵션(베타, "default" 형태).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODEL_PREFIXES = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5-5")
INSTALL_HINT = "anthropic 패키지가 설치되어 있지 않습니다. 터미널에서 `pip3 install anthropic`을 실행한 뒤 서버를 다시 시작하세요."

SYSTEM_INSTRUCTIONS = """당신은 사용자의 토스증권 포트폴리오를 점검하는 한국어 AI Agent다.
- 계좌 수치나 현재 상태에 관한 답은 반드시 제공된 읽기 전용 도구를 먼저 호출하고 그 결과만 근거로 쓴다.
- 도구 결과에 없는 숫자, 종목, 원인, 전망을 지어내지 않는다.
- 매수·매도를 지시하거나 수익을 보장하지 않는다. 사용자가 설정한 목표에 따른 계산과 위험 신호를 설명한다.
- 세금은 추정치이며 실제 신고 금액이 아님을 밝힌다.
- 답은 간결하게 쓰되, 결론과 근거 및 중요한 계산 한계를 포함한다.
- 답은 화면에 글자 그대로 표시된다. 마크다운 표('|')나 별표 강조는 쓰지 말고 짧은 문장과 줄바꿈으로 쓴다.
- 앞으로의 뷰 질문에는 상승 뷰와 하락 뷰를 균형 있게 모두 쓰고, 도구 결과의 숫자(과거 유사 상황 통계, 요인 시나리오, 변동성 범위)만 근거로 쓴다. 목표가나 확률을 새로 만들지 않고, 표본 부족이면 판단하지 않는다고 말하며, 마지막에 예측이나 투자 조언이 아님을 한 줄로 밝힌다.
- 매크로 요인 도구의 기여는 최근 1년 일간 수익률로 추정한 '같이 움직인 정도'다. 설명력(R²)이 0.2 미만이면 요인으로 설명되지 않는다고 말하고, 원인으로 단정하지 않는다. 숫자(%p, bp, R²)는 도구 결과 그대로 쓴다.
- ETF가 왜 올랐는지·떨어졌는지, 무엇을 담고 있는지 물으면 구성 종목 기여 도구를 쓴다. 기여(%p)는 비중 × 종목 등락의 합산 분해이고 상위 편입 종목은 일부(공시 시점)라 '그 밖'이 남는다는 점을 밝히며, 도구 숫자를 그대로 쓰고 종목이 움직인 이유를 지어내지 않는다. 큰 기여 종목의 보도는 출처와 함께 '~라는 보도가 있었다'로만 전한다.\n- 요인 순위 도구(평소 가장 민감한 요인)는 '무엇에 가장 영향을 받나' 질문에 쓴다. 결과의 drivers는 매크로 요인(금리·통화·원자재·변동성), peers는 함께 움직이는 비슷한 자산·시장(은·주가지수·업종 ETF 등)이니 구분해서 말한다(peers는 같은 계열이라 같이 움직이는 것이지 원인이 아니다). 상관은 같이 움직인 정도일 뿐 원인이 아니고, 모든 창(1·3·10년)에서 같은 방향인 '일관됨' 요인을 중심으로 말하며, 최근에만 강한 관계는 그렇다고 밝힌다. '이번 주 무엇 때문에 움직였나'는 요인 분해 도구가 맡는다. 도구에 없는 요인이나 수치를 지어내지 않는다.\n- 거시 사례집 도구는 과거 사례의 국면별 이야기(해석)와 시세로 계산한 숫자, 통념 점검(✓·✗)을 준다. 이야기는 해석이라고 밝히고, 숫자와 어긋나는 통념('금리 오르면 금 하락' 등)은 어긋났다고 그대로 말한다. 지금과 닮은 국면은 요인 움직임의 닮은 정도일 뿐 같은 결말을 보장하지 않으며 예측이 아니다. 도구에 없는 사건이나 원인을 지어내지 않는다.\n- 과거 매크로 반응 도구는 지금 큰 변동 중인 요인과 과거 같은 변동 때의 기록, 가장 크게 반응했던 거시 이슈를 준다. 같이 움직인 기록이지 원인의 증명이 아니며(큰 변동은 위기 같은 여러 일이 겹친 시기에 몰린다), 독립 사례가 부족하거나 평소와 통계적으로 구분되지 않으면 그대로 말한다. '왜 떨어졌어/올랐어'에는 요인 분석 도구와 함께 쓰되 도구 숫자만 근거로 하고, 이 일이 원인이라고 단정하지 않는다.\n- 뉴스 도구의 헤드라인은 '~라는 보도가 있었다'처럼 출처와 함께 전하고, 가격 변동의 원인으로 단정하지 않는다. 관련 보도가 없거나 조회에 실패했으면 그대로 말한다.
- 고점 대비 얼마나 빠졌고 왜 빠졌는지 물으면 고점 낙폭 도구를 쓴다. 최근 고점 날짜부터 지금까지 구간의 요인 기여(%p)는 최근 1년 관계로 추정한 같이 움직인 정도이고 구간이 길수록 관계가 바뀌었을 수 있다는 점을 밝힌다. 설명력이 낮거나 '그 밖'이 크면 요인으로 설명되지 않는다고 말하고, 최근 보도 도구도 불러 '~라는 보도가 있었다'로만 전한다(뉴스는 최근 7일뿐). 고점 낙폭 도구가 준 큰 하락일(그날 시장 움직임과 날짜별 보도 제목, 반복 주제)과 통계 요인을 종합해 '원인 후보'를 정리하되 어디까지나 후보로 쓰고, 원인을 단정하거나 도구 결과에 없는 사건을 지어내지 않는다.
- '금리가 오르면 금·나스닥은?'처럼 요인이 움직일 때 자산의 반응을 물으면 조건부 반응 도구를 쓴다(보유 종목이 아니어도 된다). 같은 구간에 같이 움직인 기록이고 요인이 왜 움직였는지(경기·물가·긴축)에 따라 달라질 수 있다는 점, '평소와 구분됨'은 오른 구간 비율의 차이라는 점, 구간 수가 적으면 우연일 수 있다는 점을 밝히고 도구 숫자만 쓴다.
- 종목이 고평가인지 저평가인지, PER·PSR·PBR·EPS·BPS·ROE를 물으면 밸류에이션 도구를 쓴다. 도구가 준 신호(섹터 중앙값 대비·PEG·ROE 대비 PBR)와 판정 라벨만 근거로 쓰고, 상대 평가이며 적정 주가나 투자 조언이 아니라는 점, 적자·ROE 이상치·한국 종목은 판단하지 않는다는 점, 야후 비공식 데이터라는 점을 밝힌다. 목표가를 새로 만들지 않는다.
- 도구로 답할 수 없는 질문이면 가능한 범위를 솔직하게 설명한다."""


class AgentApiError(RuntimeError):
    pass


def _tools() -> list[dict]:
    return [{"name": item["name"], "description": item["description"], "input_schema": item["input_schema"]}
            for item in TOOL_DEFINITIONS]


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


def _make_client(api_key: str):
    try:
        import anthropic
    except ImportError as e:
        raise AgentApiError(INSTALL_HINT) from e
    return anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)


def _supports_fallbacks(model: str) -> bool:
    return model.startswith(FALLBACK_MODEL_PREFIXES)


def _create(client, model: str, messages: list):
    """메시지 요청 한 번. SDK 오류는 화면에 그대로 보여도 안전한 한국어 문구로 바꾼다(키·원문은 내보내지 않음)."""
    try:
        import anthropic
    except ImportError as e:
        raise AgentApiError(INSTALL_HINT) from e

    kwargs = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM_INSTRUCTIONS, "tools": _tools(), "messages": messages}
    if _supports_fallbacks(model):
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    try:
        return client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise AgentApiError("Anthropic API 키가 올바르지 않습니다.") from e
    except anthropic.PermissionDeniedError as e:
        raise AgentApiError("이 API 키로는 요청한 모델을 쓸 수 없습니다. 모델 이름과 키의 권한을 확인하세요.") from e
    except anthropic.NotFoundError as e:
        raise AgentApiError(f"모델을 찾을 수 없습니다: {model}") from e
    except anthropic.RateLimitError as e:
        raise AgentApiError("Anthropic API 요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.") from e
    except anthropic.BadRequestError as e:
        # 잔액 부족처럼 사용자가 알아야 하는 사유가 400으로 온다. 공급자가 준 설명만 짧게 보여준다.
        detail = str(getattr(e, "message", "") or "")[:200]
        raise AgentApiError("Anthropic API가 요청을 받아들이지 않았습니다" + (f": {detail}" if detail else ".")) from e
    except anthropic.APIConnectionError as e:
        raise AgentApiError("Anthropic API에 연결하지 못했습니다.") from e
    except anthropic.APIStatusError as e:
        raise AgentApiError(f"Anthropic API 오류 ({e.status_code})") from e


def _answer_text(response) -> str:
    return "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text" and block.text).strip()


def run_claude_agent(api_key: str, model: str, question: str, history: list[dict],
                     execute_tool: Callable[[str, dict], dict], client=None) -> dict:
    """모델의 도구 호출을 최대 4회 실행하고 최종 한국어 답변과 실행 추적을 반환한다.
    `client`는 테스트에서 가짜를 넣기 위한 인자다."""
    question = str(question or "").strip()
    if not question:
        raise ValueError("질문을 입력해 주세요.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ValueError(f"질문은 {MAX_QUESTION_LENGTH}자 이하여야 합니다.")
    model = (model or DEFAULT_AGENT_MODEL).strip()
    client = client or _make_client(api_key)

    past = normalize_history(history)
    while past and past[0]["role"] != "user":  # Messages API의 첫 메시지는 user여야 한다
        past.pop(0)
    messages: list = past + [{"role": "user", "content": question}]
    trace = []
    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = _create(client, model, messages)
        if response.stop_reason == "refusal":
            raise AgentApiError("안전 정책 때문에 모델이 이 질문에 답하지 않았습니다. 질문을 바꿔서 다시 시도해 보세요.")
        calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if not calls:
            if response.stop_reason == "pause_turn":  # 서버 도구가 없어 드물지만, 일시 정지면 이어서 요청한다
                messages.append({"role": "assistant", "content": response.content})
                continue
            answer = _answer_text(response)
            if not answer:
                raise AgentApiError("Agent가 빈 답변을 반환했습니다." if response.stop_reason != "max_tokens"
                                    else "답변이 길이 제한에 걸려 잘렸습니다.")
            return {"mode": "claude", "model": model, "answer_text": answer, "executions": trace}
        # thinking 블록을 포함한 응답 전체를 그대로 이어 붙인다(이어지는 요청에서 바꾸면 안 된다)
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            if call.name not in TOOL_NAMES:
                results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                "content": "허용되지 않은 도구입니다."})
                continue
            result = execute_tool(call.name, dict(call.input) if isinstance(call.input, dict) else {})["result"]
            trace.append({"tool": call.name, "status": "completed"})
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False)})
        messages.append({"role": "user", "content": results})  # 같은 턴의 도구 결과는 한 메시지에 모은다
    raise AgentApiError("Agent가 도구를 너무 많이 호출해 중단했습니다.")
