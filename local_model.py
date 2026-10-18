"""로컬 모델(Ollama) 두뇌: 이 컴퓨터에서 도는 오픈 모델이 질문에 맞는 읽기 전용 도구와 인자를 고른다(API 요금 없음).

역할 분담: 모델은 '어떤 도구를 어떤 인자로 부를지'만 정한다. 숫자·표·그래프는 기존 도구와 답변 조립 코드가 만들고,
모델의 한두 문장 요약은 도구 결과에 있는 숫자만 쓰는지 검사한 뒤에만 붙인다(지어낸 숫자는 버린다).
통신은 로컬 주소(127.0.0.1)로만 하며, 모델에는 질문과 도구 목록만 준다(계좌·키·원본 응답은 주지 않는다).
모델이 꺼져 있거나 실패하면 호출하는 쪽이 기존 규칙 기반 처리로 되돌아간다.
"""
from __future__ import annotations

import json
import re
import time

import requests

from agent_tools import FOCUS_TOOLS, TOOL_DEFINITIONS, TOOL_NAMES

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"
PLAN_TIMEOUT = 120.0            # 첫 호출은 모델을 메모리에 올리느라 오래 걸릴 수 있다
SUMMARY_TIMEOUT = 90.0
STATUS_TTL = 10.0
MAX_CALLS = 3
MAX_HISTORY = 6
NUM_CTX = 6144
SUMMARY_INPUT_MAX = 2800
SUMMARY_MAX = 520

# 도구 한 줄 안내(전체 설명은 길어서 작은 모델의 문맥을 낭비한다). 모두 읽기 전용이다.
HINTS = {
    "get_portfolio_overview": "내 보유자산·손익·비중, 리밸런싱(매수/매도 금액) 계획",
    "get_weekly_risk_report": "이번 주 내 계좌·시장의 이동평균 이탈 같은 위험 신호 점검",
    "estimate_liquidation_tax": "지금 전부 팔면 내야 할 세금 추정",
    "get_sector_exposure": "내 포트폴리오의 섹터별 노출 비중",
    "get_weekly_news": "종목의 이번 주 등락과 최근 뉴스",
    "get_macro_drivers": "종목의 이번 주 움직임을 금리·달러·유가 등 요인으로 분해",
    "get_outlook": "종목의 앞으로의 상승·하락 시나리오(전망)",
    "get_etf_breakdown": "ETF가 이번 주 왜 움직였는지 구성 종목 기여로 분해",
    "get_macro_history": "큰 매크로 변동 때 종목이 과거 어떻게 움직였는지",
    "get_macro_cases": "2008 금융위기·2020 코로나·2022 긴축 같은 과거 사건·시기 때 금과 시장이 어땠는지, 지금과 닮은 시기",
    "get_asset_drivers": "종목이 평소 가장 영향받는 요인 순위",
    "get_ma_screen": "S&P500 중 주봉 이동평균 아래·위인 종목 찾기",
    "get_sector_trends": "미국 업종 ETF 11개의 지금 상태·잘나가는 순",
    "get_drawdown_analysis": "종목이 고점 대비 얼마나 빠졌고 왜 빠졌는지",
    "get_factor_response": "금리·달러·유가가 오르거나 내리면 금·나스닥 같은 자산이 평소 어떻게 반응하는지(과거 특정 사건·시기 질문은 해당 아님)",
    "get_valuation": "종목의 PER·PBR·PSR·EPS·ROE와 고평가·저평가 판단",
    "get_indicators": "종목의 RSI·MACD·볼린저·골든크로스 같은 보조지표",
    "get_my_rules": "내가 정해 둔 원칙(리밸런싱 목표 비중, 분할매수 전략 단계)과 현재 보유를 대조 — '내 원칙·내 기준·목표대로인지' 질문",
}

SYSTEM = """너는 토스증권 대시보드의 질문 분석기다. 사용자의 한국어 질문에 알맞은 도구를 함수 호출(function calling)로 부른다. 글로 답하지 말고, 도구가 필요하면 반드시 함수 호출을 사용한다.
규칙:
- 도구는 질문에 필요한 것만, 최대 3개 부른다. 도구를 부르는 단계에서는 숫자나 사실을 말하지 않는다.
- 도구 결과를 받으면: 질문에 답하기에 충분하면 최종 답을 쓰고, 더 필요하면 도구를 더 부른다(최대 2번 더). 최종 답은 한국어 3문장 이내로, 도구 결과에 있는 사실·숫자만 쓴다. 정해 둔 기준에 해당하는지까지만 말하고 잘잘못·심리(포모 등)는 단정하지 않으며, 결과로 답할 수 없으면 '직접 답할 수 없습니다'라고 쓴다.
- 종목 이름이나 코드는 사용자가 쓴 그대로 focus에 넣는다(내가 보유하지 않은 종목도 된다). 종목이 없으면 focus는 비운다.
- '내 포트폴리오·내 종목·내 계좌'를 묻는 질문은 focus 없이 부른다.
- '내 원칙을 위반한 종목', '목표 비중대로인지', '내가 정한 기준' 같은 질문은 내 원칙 대조 도구를 쓴다(포모 같은 심리는 판단하지 못하니 도구 결과의 수치만 말한다).
- 투자 조언 요청, 배당 내역, 실적·재무제표, 목표주가, 주문 실행, 가격 예측처럼 도구로 할 수 없는 질문이거나 투자와 무관하면 도구를 부르지 말고 '해당없음'이라고만 답한다.
- 2008 금융위기·코로나 같은 과거 사건이나 시기를 묻는 질문은 사례집, 종목이 왜 올랐나·내렸나(고점 언급 없음)는 요인 분해와 뉴스를 쓴다.
- 종목의 고평가·저평가·PER·PBR은 밸류에이션, RSI·MACD·볼린저·골든크로스는 보조지표, 고점 대비 하락 이유는 낙폭 원인, 앞으로의 시나리오는 전망, 이번 주 왜 움직였나는 요인 분해, '금리·달러가 오르면 ~는?'은 요인 반응 도구를 쓴다."""

_status_cache: dict = {"at": 0.0, "value": None}


# 결과를 보고 이어서 더 부를 만한 도구(전부 보여 주면 입력이 길어 느리다). 질문의 첫 도구를 고를 때는 전체를 쓴다.
FOLLOW_UP_TOOLS = ("get_portfolio_overview", "get_my_rules", "get_weekly_news", "get_drawdown_analysis", "get_valuation", "get_indicators", "get_macro_drivers", "get_outlook")


def tool_specs(names=None) -> list[dict]:
    """Ollama 도구 형식(JSON 스키마). 설명은 한 줄 안내로 줄이고, 인자는 이름·형식·허용값만 남긴다. names를 주면 그 도구만."""
    specs = []
    for tool in TOOL_DEFINITIONS:
        if names is not None and tool["name"] not in names:
            continue
        props = {}
        for key, spec in (tool["input_schema"].get("properties") or {}).items():
            slim = {"type": spec.get("type", "string")}
            if "enum" in spec:
                slim["enum"] = spec["enum"]
            if spec.get("type") == "array":
                slim["items"] = {"type": (spec.get("items") or {}).get("type", "string")}
            if key == "focus":
                slim["description"] = "종목 이름·코드"
            props[key] = slim
        specs.append({"type": "function", "function": {"name": tool["name"], "description": HINTS.get(tool["name"], tool["description"][:80]),
                                                       "parameters": {"type": "object", "properties": props, "required": [k for k in tool["input_schema"].get("required", []) if k in props]}}})
    return specs


def clean_args(name: str, raw, question: str) -> dict:
    """모델이 준 인자를 도구의 인자 스키마로 정리한다: 모르는 키·잘못된 형식·허용 밖 값은 버린다. focus 도구는 질문 문장도 붙여 종목 찾기를 돕는다."""
    tool = next((t for t in TOOL_DEFINITIONS if t["name"] == name), None)
    if tool is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    props = tool["input_schema"].get("properties") or {}
    out = {}
    for key, spec in props.items():
        if key not in raw:
            continue
        value, kind = raw[key], spec.get("type")
        if kind == "string" and isinstance(value, str) and value.strip():
            if "enum" in spec and value not in spec["enum"]:
                continue
            out[key] = value.strip()[:200]
        elif kind == "integer" and isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) == value:
            if "enum" in spec and int(value) not in spec["enum"]:
                continue
            out[key] = int(value)
        elif kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
        elif kind == "array" and isinstance(value, list):
            allowed = (spec.get("items") or {}).get("enum")
            items = [str(v).strip()[:30] for v in value if isinstance(v, (str, int, float)) and str(v).strip() and (not allowed or str(v) in allowed)][:8]
            if items:
                out[key] = items
    if name in FOCUS_TOOLS:
        out["focus"] = (out.get("focus", "") + " " + question).strip()[:200]      # 모델이 고른 이름을 앞에, 질문 전체를 뒤에(종목 찾기·내 종목 이름 매칭용)
    return out


def parse_calls(message: dict, question: str) -> list[dict]:
    """모델 응답의 tool_calls → [{name, args}](허용된 도구만, 같은 도구 중복 제거, 최대 MAX_CALLS)."""
    calls, seen = [], set()
    for tc in (message or {}).get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        name = fn.get("name")
        if name in TOOL_NAMES and name not in seen:
            seen.add(name)
            calls.append({"name": name, "args": clean_args(name, fn.get("arguments"), question)})
    return calls[:MAX_CALLS]


_CALL_TEXT = re.compile(r"([a-z_]+)\s*(?:\(([^()]*)\))?")
_KV = re.compile(r"([a-z_]+)\s*=\s*(\[[^\]]*\]|\"[^\"]*\"|'[^']*'|[-\d.]+|None|null)")


def parse_text_calls(text: str, question: str) -> list[dict]:
    """모델이 구조화된 호출 대신 'get_valuation(focus="엔비디아")'처럼 글로 쓴 경우를 되살린다(허용된 도구 이름만, 인자는 같은 방식으로 정리)."""
    calls, seen = [], set()
    for name, body in _CALL_TEXT.findall(str(text or "")):
        if name not in TOOL_NAMES or name in seen:
            continue
        args = {}
        for key, raw in _KV.findall(body or ""):
            if raw in ("None", "null"):
                continue
            try:
                args[key] = json.loads(raw.replace("'", '"')) if raw[:1] in "[\"'" else float(raw)
            except ValueError:
                continue
        seen.add(name)
        calls.append({"name": name, "args": clean_args(name, args, question)})
    return calls[:MAX_CALLS]


MAX_ROUNDS = 2                  # 계획 뒤에 도구 결과를 보고 더 부를 수 있는 횟수
FINAL_MAX = 520


def _assistant_with_calls(calls: list[dict]) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": c["name"], "arguments": c["args"]}} for c in calls]}


def observe(messages: list[dict], calls: list[dict], digests: dict[str, str], question: str, model: str = DEFAULT_MODEL, post=None) -> dict:
    """도구 결과(한국어 요약 글)를 모델에게 보여 주고 다음 행동을 받는다: 더 부를 도구({"calls"}) 또는 최종 답({"final"}).
    messages는 plan()이 돌려준 대화(질문까지)이며 호출하는 쪽이 계속 이어 붙여 쓴다(이 함수가 도구 호출과 결과를 덧붙인다)."""
    messages.append(_assistant_with_calls(calls))
    for c in calls:
        messages.append({"role": "tool", "name": c["name"], "content": str(digests.get(c["name"]) or "결과 없음")[:1600]})
    messages.append({"role": "user", "content": "위 도구 결과로 질문에 답할 수 있으면 최종 답을 2문장 이내로 쓰고, 더 필요한 도구가 있으면 부르세요. "
                                                "최종 답에는 초과·부족·이평선 위 같은 수치 표현만 쓰고, 포모·충동 같은 심리, 잘못했다는 판단, 언제 샀는지에 대한 추측은 쓰지 마세요."})
    message = _chat({"model": model, "messages": messages, "tools": tool_specs(FOLLOW_UP_TOOLS), "stream": False, "keep_alive": "30m",
                     "options": {"temperature": 0.1, "num_ctx": NUM_CTX, "num_predict": 260}}, PLAN_TIMEOUT, post)
    text = str(message.get("content") or "").strip()
    new = parse_calls(message, question) or parse_text_calls(text, question)
    if new:
        return {"calls": new, "final": ""}
    messages.append({"role": "assistant", "content": text})
    return {"calls": [], "final": re.sub(r"\s+", " ", text)[:FINAL_MAX]}


JUDGEMENT_WORDS = ("포모", "fomo", "충동", "징후", "잘못", "실수", "위반했", "위반한 것", "위반입니다", "위반하고", "어겼", "매수한 상태", "매수했", "샀습니다", "샀다", "추격 매수")


def usable_final(text: str, facts: str) -> str | None:
    """모델의 최종 답을 쓸 수 있는지: '직접 답할 수 없다'는 문장은 빼고, 남은 글이 비어 있지 않으며, 잘잘못·심리·매수 시점 추측이 없고, 숫자가 도구 결과에 모두 있어야 한다."""
    sentences = [x.strip() for x in re.split(r"(?<=[.!?。])\s+", re.sub(r"\s+", " ", str(text or "")).strip()) if x.strip()]
    t = " ".join(x for x in sentences if "직접 답할 수 없" not in x and "해당없음" not in x)
    if len(t) < 8 or any(w in t.lower() for w in JUDGEMENT_WORDS) or not grounded(t, facts[:12000]):
        return None
    return t


def status(model: str = DEFAULT_MODEL, get=None, now=time.time) -> dict:
    """Ollama가 켜져 있는지, 모델이 받아져 있는지. 10초 동안 결과를 재사용한다."""
    get = get or requests.get                      # 호출 시점에 고른다(시험에서 바꿔 끼울 수 있게)
    t = now()
    if _status_cache["value"] is not None and t - _status_cache["at"] < STATUS_TTL and _status_cache["value"]["model"] == model:
        return dict(_status_cache["value"])
    value = {"reachable": False, "ready": False, "model": model, "models": []}
    try:
        resp = get(OLLAMA_URL + "/api/tags", timeout=2)
        resp.raise_for_status()
        names = [str(m.get("name") or "") for m in (resp.json().get("models") or []) if isinstance(m, dict)]
        value.update({"reachable": True, "models": names[:20], "ready": any(n == model or n.split(":")[0] == model.split(":")[0] and ":" not in model for n in names)})
    except (requests.RequestException, ValueError):
        pass
    _status_cache.update({"at": t, "value": value})
    return dict(value)


def reset_status_cache() -> None:
    _status_cache.update({"at": 0.0, "value": None})


def _chat(payload: dict, timeout: float, post=None) -> dict:
    resp = (post or requests.post)(OLLAMA_URL + "/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    return body.get("message") or {}


def plan(question: str, history: list[dict], model: str = DEFAULT_MODEL, post=None) -> dict:
    """질문에 맞는 도구 호출 계획. 반환: {"calls": [{name, args}], "reply": 모델의 글(도구를 안 불렀을 때)}. 실패하면 예외(호출하는 쪽이 규칙 기반으로 되돌아간다)."""
    messages = [{"role": "system", "content": SYSTEM}]
    for h in (history or [])[-MAX_HISTORY:]:
        if h.get("role") in ("user", "assistant") and str(h.get("content") or "").strip():
            messages.append({"role": h["role"], "content": str(h["content"])[:600]})
    messages.append({"role": "user", "content": question})
    message = _chat({"model": model, "messages": messages, "tools": tool_specs(), "stream": False, "keep_alive": "30m",
                     "options": {"temperature": 0.1, "num_ctx": NUM_CTX}}, PLAN_TIMEOUT, post)
    reply = str(message.get("content") or "").strip()
    calls = parse_calls(message, question) or parse_text_calls(reply, question)       # 글로 쓴 호출도 되살린다
    return {"calls": calls, "reply": reply[:300], "messages": messages, "assistant": message}


_NUMBER = re.compile(r"\d[\d,]*\.?\d*")


def numbers_in(text: str) -> set[str]:
    """글 속 숫자(자리 쉼표·소수점 정리, 한 자리 정수는 무시)."""
    out = set()
    for m in _NUMBER.findall(str(text or "")):
        n = m.replace(",", "").rstrip(".")
        if len(n.replace(".", "")) >= 2:
            out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def grounded(summary: str, source: str) -> bool:
    """요약의 모든 숫자가 도구 결과 글에 있는지(지어낸 숫자 방지). 반올림 차이는 소수 첫째 자리까지 같으면 인정한다."""
    have = numbers_in(source)
    have |= {x.split(".")[0] for x in have if "." in x} | {f"{float(x):.1f}".rstrip("0").rstrip(".") for x in have if re.fullmatch(r"\d+\.?\d*", x)}
    return all(n in have for n in numbers_in(summary))


def summarize(question: str, facts: str, model: str = DEFAULT_MODEL, post=None) -> str | None:
    """도구 결과(한국어 요약 줄들)로 질문에 직접 답하는 두 문장 이하 요약. 결과에 없는 숫자가 있거나 비었으면 None."""
    facts = facts[:SUMMARY_INPUT_MAX]
    prompt = ("아래는 도구가 계산한 결과다. 이 결과에 있는 사실만으로 사용자의 질문에 직접 답하는 두 문장 이내의 한국어 요약을 써라. "
              "결과에 없는 숫자·사실·원인은 쓰지 말고, 매수·매도 권유나 확정적인 예측은 하지 마라. 결과가 질문에 직접 답하지 못하면 '직접 답할 수 없습니다'라고만 써라.\n\n"
              f"질문: {question}\n\n결과:\n{facts}")
    message = _chat({"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False, "keep_alive": "30m",
                     "options": {"temperature": 0.1, "num_ctx": NUM_CTX, "num_predict": 200}}, SUMMARY_TIMEOUT, post)
    return usable_final(str(message.get("content") or "")[:SUMMARY_MAX], facts)
