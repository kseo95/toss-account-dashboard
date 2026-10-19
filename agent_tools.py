"""AI Agent가 사용할 읽기 전용 도구 계약과 결과 축약.

LLM에는 토스 토큰·앱키·계좌번호나 대시보드의 전체 응답을 주지 않는다. 서버가 기존의
결정론적 계산을 실행한 뒤, 이 모듈이 허용한 필드만 작은 JSON으로 만들어 전달한다.

도구마다 정의·결과 정리·답변 조립은 `tools/` 패키지의 파일에 있다(새 도구는 거기에 파일을 만들고 `tools/__init__.py`의 MODULES에 넣는다).
여기서는 모아서 `TOOL_DEFINITIONS`·`format_tool_result`·`compose_agent_answer`로 내보내고, 질문 해석 규칙(`tools/routing.py`)을 다시 내보낸다.
"""
from __future__ import annotations

from tools import MODULES
from tools.account import *  # noqa: F401,F403  (예전에 이 파일에 있던 결과 정리·상수 이름을 그대로 내보낸다)
from tools.drawdown import *  # noqa: F401,F403
from tools.indicators import *  # noqa: F401,F403
from tools.macro import *  # noqa: F401,F403
from tools.outlook import *  # noqa: F401,F403
from tools.past import *  # noqa: F401,F403
from tools.query import *  # noqa: F401,F403
from tools.rules import *  # noqa: F401,F403
from tools.trend import *  # noqa: F401,F403
from tools.valuation import *  # noqa: F401,F403
from tools.web import *  # noqa: F401,F403
from tools.common import _josa  # noqa: F401
from tools.macro import _driver_direction, _factor_text  # noqa: F401
from tools.trend import _from_high_text  # noqa: F401
from tools.routing import *  # noqa: F401,F403  (is_*_query, parse_*_query, plan_*, INTENT_*, 단어 목록 등)

# 도구 목록의 순서는 Agent에게 보이는 순서이므로 바꾸지 않는다.
TOOL_ORDER = (
    "get_portfolio_overview", "get_weekly_risk_report", "get_outlook", "get_etf_breakdown", "get_sector_trends", "get_ma_screen",
    "get_asset_drivers", "get_macro_cases", "get_macro_history", "get_drawdown_analysis", "get_factor_response", "get_valuation", "get_indicators", "get_my_rules",
    "get_past_value", "query_data", "web_search", "read_page", "get_macro_drivers", "get_weekly_news", "get_sector_exposure",
)
# 답변 조각을 이어 붙이는 순서(먼저 오는 도구의 글이 위에 온다).
COMPOSE_ORDER = (
    "get_portfolio_overview", "get_weekly_risk_report", "get_sector_exposure", "get_outlook", "get_etf_breakdown", "get_sector_trends", "get_ma_screen",
    "get_asset_drivers", "get_macro_cases", "get_macro_history", "get_my_rules", "get_past_value", "query_data", "web_search", "read_page", "get_indicators",
    "get_valuation", "get_factor_response", "get_drawdown_analysis", "get_macro_drivers", "get_weekly_news",
)

_DEFINITIONS = {d["name"]: d for module in MODULES for d in module.DEFINITIONS}
_FORMATTERS = {name: fn for module in MODULES for name, fn in module.FORMATTERS.items()}
_COMPOSERS = {name: fn for module in MODULES for name, fn in module.COMPOSERS.items()}
assert set(_DEFINITIONS) == set(_FORMATTERS) == set(_COMPOSERS) == set(TOOL_ORDER) == set(COMPOSE_ORDER)

TOOL_DEFINITIONS = [_DEFINITIONS[name] for name in TOOL_ORDER]

TOOL_NAMES = {tool["name"] for tool in TOOL_DEFINITIONS}


def format_tool_result(name: str, payload: dict) -> dict:
    """허용 목록 밖 도구는 실행하지 않고, 허용된 결과만 Agent용 JSON으로 축약한다."""
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        raise ValueError(f"허용되지 않은 Agent 도구입니다: {name}")
    return {"tool": name, "read_only": True, "result": formatter(payload)}


def compose_agent_answer(results: dict[str, dict]) -> dict:
    """도구의 숫자를 바꾸지 않고 사람이 읽을 수 있는 점검 결과로 조합한다."""
    bullets, limitations, tables, notes, charts = [], [], [], [], []
    order = list(COMPOSE_ORDER)
    if next(iter(results), None) == "get_ma_screen":    # 먼저 물은 도구의 답이 위에 오도록(질문이 스크리너면 스크리너 먼저, 추세면 추세 먼저)
        a, b = order.index("get_sector_trends"), order.index("get_ma_screen")
        order[a], order[b] = order[b], order[a]
    for name in order:
        data = results.get(name)
        if data:
            _COMPOSERS[name](data, bullets, tables, notes, limitations, charts, results)
    return {
        "headline": "계좌 데이터를 기준으로 확인한 결과입니다." if bullets or charts else "이 질문은 아직 처리할 수 없습니다.",
        "bullets": bullets or ([] if charts else ["이렇게 물어보세요: \"이번 주 뭘 확인해야 해?\", \"금 앞으로의 상승/하락 뷰 보여줘\", \"리밸런싱 얼마씩 사야 해?\", "
                              "\"섹터 노출 보여줘\". 종목 이름만 말해도 앞으로의 뷰를 보여줍니다. "
                              "Anthropic 키를 넣으면 자유로운 문장도 이해합니다."]),
        "tables": tables,
        "charts": [c for c in charts if c],
        "notes": notes,
        "limitations": list(dict.fromkeys(str(x) for x in limitations if x)),
    }
