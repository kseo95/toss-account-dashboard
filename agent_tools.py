"""AI Agent가 사용할 읽기 전용 도구 계약과 결과 축약.

LLM에는 토스 토큰·앱키·계좌번호나 대시보드의 전체 응답을 주지 않는다. 서버가 기존의
결정론적 계산을 실행한 뒤, 이 모듈이 허용한 필드만 작은 JSON으로 만들어 전달한다.
"""
from __future__ import annotations

from typing import Callable


TOOL_DEFINITIONS = [
    {
        "name": "get_portfolio_overview",
        "description": "현재 보유자산, 손익, 통화 비중과 비중이 큰 종목을 조회합니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_weekly_risk_report",
        "description": "이번 주 계좌와 시장의 이동평균·교차·위험 신호를 조회합니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "estimate_liquidation_tax",
        "description": "현재 보유종목을 모두 판다고 가정한 세금 추정치와 계산 한계를 조회합니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOL_DEFINITIONS)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def portfolio_overview(data: dict) -> dict:
    """보유 화면 응답에서 Agent에 필요한 비민감 필드만 남긴다."""
    positions = []
    for row in data.get("stocks", []) + data.get("cash", {}).get("stocks", []):
        positions.append({
            "symbol": str(row.get("심볼") or ""),
            "name": str(row.get("종목") or row.get("심볼") or ""),
            "currency": str(row.get("통화") or ""),
            "value_krw": _number(row.get("평가금액(원)")),
            "profit_loss_krw": _number(row.get("손익(원)")),
            "weight_pct": _number(row.get("지분율(%)")),
        })
    positions.sort(key=lambda r: r["weight_pct"] if r["weight_pct"] is not None else -1, reverse=True)
    totals = data.get("totals", {})
    split = data.get("currency_split", {})
    plan = data.get("rebalance_plan", {})
    return {
        "as_of": "current_snapshot",
        "totals": {
            "value_krw": _number(totals.get("eval_krw")),
            "profit_loss_krw": _number(totals.get("profit_loss_krw")),
            "return_pct": _number(totals.get("rate_pct")),
        },
        "currency_split": {
            "krw_pct": _number(split.get("krw_pct")),
            "usd_pct": _number(split.get("usd_pct")),
            "target_krw_pct": _number(split.get("krw_target_pct")),
            "target_usd_pct": _number(split.get("usd_target_pct")),
        },
        "largest_positions": positions[:5],
        "position_count": len(positions),
        "rebalancing": {
            "ready": bool(plan.get("ready")),
            "target_total_pct": _number(plan.get("target_total_pct")),
            "buy_total_krw": _number(plan.get("buy_total_krw")),
            "sell_total_krw": _number(plan.get("sell_total_krw")),
            "rows": [
                {"label": str(r.get("label") or ""), "action": r.get("action"),
                 "difference_krw": _number(r.get("difference_krw"))}
                for r in plan.get("rows", [])
            ],
            "warnings": [str(x) for x in plan.get("warnings", [])],
        },
        "limitations": [
            "해외 종목의 손익은 매입 시점 환율에 따른 환차손익을 정확히 반영하지 않습니다.",
            "현재 조회 시점의 계좌 스냅샷이며 투자 판단이나 주문을 대신하지 않습니다.",
        ],
    }


def weekly_risk_report(data: dict) -> dict:
    """주간 리포트에서 계좌 요약과 규칙 기반 신호만 남긴다."""
    account = None
    account_comments = []
    for group in data.get("groups", []):
        if group.get("type") == "my_account":
            account = group.get("account")
            account_comments = [str(x) for x in group.get("comments", [])]
            break
    summary = data.get("summary", {})
    return {
        "week_end": data.get("week_end"),
        "generated_at": data.get("generated_at"),
        "account": {
            "value_krw": _number((account or {}).get("eval_krw")),
            "profit_loss_krw": _number((account or {}).get("profit_loss_krw")),
            "cumulative_return_pct": _number((account or {}).get("rate_pct")),
            "weekly_return_krw_pct": _number((account or {}).get("week_krw_pct")),
        } if account else None,
        "all_ma_broken": [
            {"symbol": str(x.get("symbol") or ""), "label": str(x.get("label") or "")}
            for x in summary.get("all_broken", [])
        ],
        "crosses": [
            {"symbol": str(x.get("symbol") or ""), "label": str(x.get("label") or ""),
             "period": x.get("period"), "direction": x.get("dir")}
            for x in summary.get("crosses", [])
        ],
        "rule_based_comments": [str(x) for x in summary.get("comments", [])] + account_comments,
        "data_errors": [str(x) for x in data.get("errors", [])],
        "limitations": [
            "신호와 코멘트는 가격·이동평균 등 수치 규칙의 결과이며 전망이나 매매 추천이 아닙니다.",
            "Yahoo Finance 비공식 데이터가 누락되거나 지연될 수 있습니다.",
        ],
    }


def liquidation_tax_estimate(data: dict) -> dict:
    """세금 화면 응답에서 추정 요약·근거 상태·한계만 남긴다."""
    summary = data.get("summary", {})
    rows = sorted(data.get("rows", []), key=lambda r: float(r.get("tax_krw") or 0), reverse=True)
    notes = [x for x in (data.get("realized_note"), data.get("income_note")) if x]
    return {
        "assumption": "현재 보유종목을 지금 모두 매도",
        "summary": {
            "estimated_tax_krw": _number(summary.get("tax_if_sell_all_krw")),
            "capital_gains_tax_krw": _number(summary.get("gain_tax_if_sell_all_krw")),
            "domestic_transaction_and_etf_tax_krw": _number(summary.get("domestic_tax_if_sell_all_krw")),
            "deduction_left_krw": _number(summary.get("deduction_left_krw")),
            "financial_income_ytd_krw": _number(summary.get("financial_income_ytd_krw")),
        },
        "data_sources": {
            "realized_gain": data.get("realized_source"),
            "financial_income": data.get("income_source"),
        },
        "largest_estimated_taxes": [
            {"symbol": str(r.get("symbol") or ""), "name": str(r.get("name") or ""),
             "estimated_tax_krw": _number(r.get("tax_krw")), "notes": [str(x) for x in r.get("notes", [])]}
            for r in rows[:5]
        ],
        "warnings": notes,
        "limitations": [
            "세금은 현재 데이터와 사용자 설정에 근거한 추정치이며 실제 신고 금액이 아닙니다.",
            "해외 손익의 매입 시점 환율, 누락된 체결, ETF 과세표준 차이로 실제 금액과 다를 수 있습니다.",
        ],
    }


_FORMATTERS: dict[str, Callable[[dict], dict]] = {
    "get_portfolio_overview": portfolio_overview,
    "get_weekly_risk_report": weekly_risk_report,
    "estimate_liquidation_tax": liquidation_tax_estimate,
}


def format_tool_result(name: str, payload: dict) -> dict:
    """허용 목록 밖 도구는 실행하지 않고, 허용된 결과만 Agent용 JSON으로 축약한다."""
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        raise ValueError(f"허용되지 않은 Agent 도구입니다: {name}")
    return {"tool": name, "read_only": True, "result": formatter(payload)}


def plan_agent_query(question: str) -> dict:
    """짧은 한국어 질문을 허용된 읽기 도구 호출 계획으로 바꾼다."""
    text = str(question or "").strip()
    if not text:
        raise ValueError("질문을 입력해 주세요.")
    tools, intents = [], []
    if any(word in text for word in ("세금", "양도세", "전부 팔", "다 팔", "전량 매도")):
        tools.append("estimate_liquidation_tax")
        intents.append("tax")
    if any(word in text for word in ("이번 주", "이번주", "위험", "주의", "신호", "점검")):
        tools.extend(("get_portfolio_overview", "get_weekly_risk_report"))
        intents.append("weekly_review")
    if any(word in text for word in ("리밸런싱", "목표 비중", "비중 맞", "자산 구성", "포트폴리오")):
        tools.append("get_portfolio_overview")
        intents.append("rebalancing" if "리밸런싱" in text or "비중 맞" in text else "portfolio")
    tools = list(dict.fromkeys(tools))
    return {"supported": bool(tools), "intents": list(dict.fromkeys(intents)), "tools": tools}


def _won(value) -> str:
    return f"{float(value or 0):,.0f}원"


def _pct(value) -> str:
    return f"{float(value or 0):,.2f}%"


def compose_agent_answer(results: dict[str, dict]) -> dict:
    """도구의 숫자를 바꾸지 않고 사람이 읽을 수 있는 점검 결과로 조합한다."""
    bullets, limitations = [], []
    portfolio = results.get("get_portfolio_overview")
    if portfolio:
        totals = portfolio.get("totals", {})
        bullets.append(f"현재 평가금액은 {_won(totals.get('value_krw'))}, 누적 수익률은 {_pct(totals.get('return_pct'))}입니다.")
        positions = portfolio.get("largest_positions", [])
        if positions:
            top = positions[0]
            bullets.append(f"가장 큰 보유 비중은 {top.get('name') or top.get('symbol')} {_pct(top.get('weight_pct'))}입니다.")
        rebalance = portfolio.get("rebalancing", {})
        if rebalance.get("ready"):
            bullets.append(
                f"설정한 목표 비중 기준으로 총 매수 {_won(rebalance.get('buy_total_krw'))}, "
                f"총 매도 {_won(rebalance.get('sell_total_krw'))}가 필요합니다."
            )
            for row in sorted(rebalance.get("rows", []), key=lambda x: abs(x.get("difference_krw") or 0), reverse=True)[:3]:
                action = {"buy": "매수", "sell": "매도"}.get(row.get("action"), "유지")
                bullets.append(f"{row.get('label')}: {action} {_won(abs(row.get('difference_krw') or 0))}")
        elif rebalance:
            bullets.append("목표 비중 합계가 100%가 아니어서 리밸런싱 금액 계획을 확정할 수 없습니다.")
        limitations.extend(portfolio.get("limitations", []))
        limitations.extend(rebalance.get("warnings", []))

    weekly = results.get("get_weekly_risk_report")
    if weekly:
        account = weekly.get("account")
        if account:
            bullets.append(f"이번 주 계좌 원화 기준 수익률은 {_pct(account.get('weekly_return_krw_pct'))}입니다.")
        broken = weekly.get("all_ma_broken", [])
        if broken:
            labels = ", ".join((x.get("label") or x.get("symbol")) for x in broken[:5])
            bullets.append(f"설정한 모든 이동평균선 아래인 항목: {labels}")
        else:
            bullets.append("설정한 모든 이동평균선을 이탈한 항목은 없습니다.")
        bullets.extend(weekly.get("rule_based_comments", [])[:3])
        if weekly.get("data_errors"):
            bullets.append(f"데이터를 불러오지 못한 항목이 {len(weekly['data_errors'])}개 있습니다.")
        limitations.extend(weekly.get("limitations", []))

    tax = results.get("estimate_liquidation_tax")
    if tax:
        summary = tax.get("summary", {})
        bullets.append(f"현재 전량 매도 가정 추정 세금은 {_won(summary.get('estimated_tax_krw'))}입니다.")
        bullets.append(
            f"해외 양도세 {_won(summary.get('capital_gains_tax_krw'))}, 국내 거래세·ETF 세금 "
            f"{_won(summary.get('domestic_transaction_and_etf_tax_krw'))}로 계산됐습니다."
        )
        limitations.extend(tax.get("warnings", []))
        limitations.extend(tax.get("limitations", []))

    return {
        "headline": "계좌 데이터를 기준으로 확인한 결과입니다." if bullets else "이 질문은 아직 처리할 수 없습니다.",
        "bullets": bullets or ["이번 주 점검, 리밸런싱 계획, 전량 매도 세금 중 하나를 물어보세요."],
        "limitations": list(dict.fromkeys(str(x) for x in limitations if x)),
    }
