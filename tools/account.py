"""계좌 요약·주간 위험 점검·섹터 노출 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod

from .common import _num, _number, _pct, _qty, _short_pct, _won


DEFINITIONS = [
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
        "name": "get_sector_exposure",
        "description": "사용자가 직접 정한 섹터 매핑 기준으로 섹터별 내 노출 금액·비중과 연결된 보유종목을 조회합니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def portfolio_overview(data: dict) -> dict:
    """보유 화면 응답에서 Agent에 필요한 비민감 필드만 남긴다."""
    positions = []
    for row in data.get("stocks", []):  # 현금 취급으로 지정한 종목(cash.stocks)은 보유 순위에서 뺀다 — 아래 cash에 합쳐서 알린다
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
    cash = data.get("cash", {})
    cash_total = _number(cash.get("total_krw"))
    eval_total = _number(totals.get("eval_krw"))
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
        "cash": {  # 예수금 + 현금 취급으로 지정한 종목
            "total_krw": cash_total,
            "weight_pct": cash_total / eval_total * 100 if cash_total is not None and eval_total else None,
            "cash_like_stock_count": len(cash.get("stocks", [])),
        },
        "rebalancing": {
            "ready": bool(plan.get("ready")),
            "target_total_pct": _number(plan.get("target_total_pct")),
            "buy_total_krw": _number(plan.get("buy_total_krw")),
            "sell_total_krw": _number(plan.get("sell_total_krw")),
            "funding_gap_krw": _number(plan.get("funding_gap_krw")),
            "excluded": {k: _number((plan.get("excluded") or {}).get(k))
                         for k in ("holdings_count", "holdings_pct", "cash_pct")},
            "rows": [
                {"label": str(r.get("label") or ""), "action": r.get("action"),
                 "difference_krw": _number(r.get("difference_krw")),
                 "current_pct": _number(r.get("current_pct")), "target_pct": _number(r.get("target_pct")),
                 "buy_now_krw": _number(r.get("buy_now_krw")), "wait_krw": _number(r.get("wait_krw")),
                 "allocations": [
                     {"symbol": str(a.get("symbol") or ""), "name": str(a.get("name") or ""),
                      "currency": str(a.get("currency") or ""), "amount_krw": _number(a.get("amount_krw")),
                      "shares": _number(a.get("shares")), "signal_status": str(a.get("signal_status") or ""),
                      "entry_pct": _number(a.get("entry_pct")), "buy_now_krw": _number(a.get("buy_now_krw")),
                      "wait_krw": _number(a.get("wait_krw")), "shares_now": _number(a.get("shares_now"))}
                     for a in r.get("allocations", [])
                 ]}
                for r in plan.get("rows", [])
            ],
            "quote_errors": [str(s) for s in plan.get("quote_errors", [])],
            "scaling": {k: (bool(v) if k == "scaled" else _number(v)) for k, v in (plan.get("scaling") or {}).items()},
            "signals": [
                {"symbol": str(s.get("symbol") or ""), "status": str(s.get("status") or ""),
                 "entry_pct": _number(s.get("entry_pct")), "stage_count": _number(s.get("stage_count")),
                 "stage_total": _number(s.get("stage_total")), "cycle": _number(s.get("cycle")),
                 "recovered": bool(s.get("recovered")),
                 "steps": [{"label": str(x.get("label") or ""), "broken": bool(x.get("broken")),
                            "below": bool(x.get("below")), "ok": x.get("ok", True) is not False}
                           for x in s.get("steps", [])]}
                for s in plan.get("signals", [])
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


def sector_exposure(data: dict) -> dict:
    """섹터 노출 응답에서 섹터별 금액·비중과 연결 종목만 남긴다(대표 티커는 섹터 식별용)."""
    sectors = []
    for s in data.get("sectors", []):
        sectors.append({
            "label": str(s.get("label") or ""),
            "proxy": str(s.get("proxy") or ""),
            "exposure_krw": _number(s.get("exposure_krw")),
            "exposure_pct": _number(s.get("exposure_pct")),
            "holdings": [
                {"symbol": str(h.get("symbol") or ""), "name": str(h.get("name") or ""),
                 "assigned_pct": _number(h.get("pct")), "amount_krw": _number(h.get("amount_krw"))}
                for h in s.get("holdings", [])[:5]
            ],
        })
    unmapped = [
        {"symbol": str(u.get("symbol") or ""), "name": str(u.get("name") or ""),
         "weight_pct": _number(u.get("weight_pct"))}
        for u in data.get("unmapped", [])[:5]
    ]
    return {
        "configured": bool(data.get("configured")),
        "sectors": sectors,
        "unmapped_holdings": unmapped,
        "unmapped_count": len(data.get("unmapped", [])),
        "limitations": [
            "섹터 분류와 노출 %는 사용자가 직접 입력한 값이며 자동으로 검증되지 않습니다.",
            "ETF의 노출 %는 추정치이고 실제 구성 종목 비중과 다를 수 있습니다.",
            "현재 조회 시점의 계좌 스냅샷이며 투자 판단이나 주문을 대신하지 않습니다.",
        ],
    }


def _holding_cell(row: dict) -> str:
    """'종목 · 수량' 칸: 종목별 주수(예: UFO 3.80주 + NASA 10.15주). 진입 비율 때문에 지금 살 게 없으면 진입 비율을 보여준다."""
    parts = []
    for a in row.get("allocations") or []:
        symbol = a.get("symbol")
        if row.get("action") == "buy" and a.get("signal_status") == "ok":
            if (a.get("buy_now_krw") or 0) <= 0:
                parts.append(f"{symbol} 진입 {a.get('entry_pct') or 0:g}%")
            else:
                parts.append(f"{symbol} {_qty(a.get('shares_now'), a.get('currency'))}")
        else:
            parts.append(f"{symbol} {_qty(a.get('shares'), a.get('currency'))}")
    return " + ".join(parts)


def _basis_cell(row: dict) -> str:
    """'현재 → 목표' 칸: 이 금액이 어디서 나왔는지(현재 비중과 목표 비중)를 보여준다."""
    if row.get("current_pct") is None or row.get("target_pct") is None:
        return "-"
    return f"{_short_pct(row['current_pct'])} → {_short_pct(row['target_pct'])}"


def _moves_table(moves: list[dict], rebalance: dict) -> dict:
    """매수·매도 목록 표(카테고리당 한 줄). 매도는 지금/대기 칸이 없다."""
    rows = []
    for row in moves:
        amount = abs(row.get("difference_krw") or 0)
        if row["action"] == "buy":
            now = row.get("buy_now_krw") if row.get("buy_now_krw") is not None else amount
            wait = row.get("wait_krw") if row.get("wait_krw") is not None else 0
            rows.append([row.get("label"), "매수", _basis_cell(row), _num(amount), _num(now), _num(wait), _holding_cell(row)])
        else:
            rows.append([row.get("label"), "매도", _basis_cell(row), _num(amount), "-", "-", _holding_cell(row)])
    scaling = rebalance.get("scaling") or {}
    buy_total = rebalance.get("buy_total_krw")
    now_total = scaling.get("buy_now_total_krw") if scaling.get("buy_now_total_krw") is not None else buy_total
    wait_total = scaling.get("wait_total_krw") if scaling.get("wait_total_krw") is not None else 0
    return {
        "title": "매수 & 매도 목록",
        "columns": ["카테고리", "구분", "현재 → 목표", "금액(원)", "지금(원)", "대기(원)", "종목 · 수량"],
        "align": ["l", "l", "r", "r", "r", "r", "l"],
        "rows": rows,
        "footer": [["매수 합계", "", "", _num(buy_total), _num(now_total), _num(wait_total), ""],
                   ["매도 합계", "", "", _num(rebalance.get("sell_total_krw")), "-", "-", ""]],
    }


def _step_cell(step: dict) -> str:
    """단계 칸: 지금 위치(%)와 과거 돌파 기록을 구분한다."""
    value = (step.get("label") or "").partition(" ")[2]
    if step.get("ok") is False:
        return "데이터 부족"
    if step.get("broken") and step.get("below"):
        return f"{value} 돌파 중"
    if step.get("broken"):
        return f"{value} (과거 돌파)"
    return value


def _signals_table(signals: list[dict]) -> dict:
    """주봉 MA 신호 표: 종목별 진입 비율, 돌파 단계 수, 각 MA 대비 위치, 비고(회복 완료·제외 사유)."""
    names: list[str] = []
    for s in signals:
        for step in s.get("steps", []):
            name = (step.get("label") or "").partition(" ")[0]
            if name and name not in names:
                names.append(name)
    rows = []
    for s in signals:
        status = s.get("status")
        if status == "ok":
            cells = {(x.get("label") or "").partition(" ")[0]: _step_cell(x) for x in s.get("steps", [])}
            notes = []
            if s.get("recovered"):
                notes.append("회복 완료")
            if (s.get("cycle") or 1) >= 2:
                notes.append(f"{int(s['cycle'])}차 사이클")
            rows.append([s["symbol"], _pct(s.get("entry_pct")), f"{int(s.get('stage_count') or 0)}/{int(s.get('stage_total') or 0)}"]
                        + [cells.get(n, "-") for n in names] + [", ".join(notes)])
        else:
            note = {"excluded": "전략 제외 — 목표 금액 그대로",
                    "unavailable": "신호 확인 불가 — 목표 금액 그대로"}.get(status, "적용된 전략 없음 — 목표 금액 그대로")
            rows.append([s["symbol"], "-", "-"] + ["-"] * len(names) + [note])
    return {
        "title": "주봉 MA 신호 (진입 비율)",
        "columns": ["종목", "진입 비율", "돌파 단계"] + names + ["비고"],
        "align": ["l", "r", "r"] + ["r"] * len(names) + ["l"],
        "rows": rows,
        "footer": [],
    }


def _charts_overview(portfolio: dict, charts: list) -> None:
    positions = portfolio.get("largest_positions", [])
    labels = [p.get("name") or p.get("symbol") for p in positions if p.get("weight_pct") is not None]
    values = [p["weight_pct"] for p in positions if p.get("weight_pct") is not None]
    cash = (portfolio.get("cash") or {}).get("weight_pct")
    if cash:
        labels.append("현금성")
        values.append(cash)
    if len(values) >= 2:
        charts.append(chart_mod.make("hbar", "보유 비중 상위", labels, [{"label": "비중", "color": "s1", "data": values}], "%", height=len(values) * 32 + 56))
    rebalance = portfolio.get("rebalancing") or {}
    moves = [r for r in rebalance.get("rows", []) if r.get("action") in ("buy", "sell") and r.get("difference_krw")]
    if rebalance.get("ready") and moves:
        moves.sort(key=lambda r: -(r["difference_krw"]))
        values = [r["difference_krw"] for r in moves]
        charts.append(chart_mod.make("hbar", "리밸런싱 — 목표까지 매수(+)·매도(−) 금액", [r.get("label") or "" for r in moves],
                                     [{"label": "금액", "colors": chart_mod.signed_colors(values), "data": values, "text": [f"{'매수' if v > 0 else '매도'} {abs(v):,.0f}원" for v in values]}], "원",
                                     caption="주문은 실행되지 않으며 설정한 목표 비중 기준 계산입니다", height=len(values) * 40 + 60))


def _charts_sector(sector: dict, charts: list) -> None:
    rows = sorted((s for s in sector.get("sectors", []) if s.get("exposure_pct")), key=lambda s: s["exposure_pct"], reverse=True)[:8]
    if sector.get("configured") and len(rows) >= 2:
        charts.append(chart_mod.make("hbar", "섹터별 내 노출 비중", [s.get("label") or "" for s in rows],
                                     [{"label": "노출", "color": "s1", "data": [s["exposure_pct"] for s in rows], "text": [f"{s['exposure_pct']:.1f}% · {_won(s.get('exposure_krw'))}" for s in rows]}], "%",
                                     height=len(rows) * 34 + 56))


def compose_get_portfolio_overview(portfolio, bullets, tables, notes, limitations, charts, results):
    """get_portfolio_overview 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    totals = portfolio.get("totals", {})
    bullets.append(f"현재 평가금액은 {_won(totals.get('value_krw'))}, 누적 수익률은 {_pct(totals.get('return_pct'))}입니다.")
    positions = portfolio.get("largest_positions", [])
    if positions:
        top = positions[0]  # 현금 취급 종목은 순위에서 이미 빠져 있다
        bullets.append(f"가장 큰 보유 비중은 {top.get('name') or top.get('symbol')} {_pct(top.get('weight_pct'))} 입니다.")
    cash = portfolio.get("cash") or {}
    if cash.get("weight_pct") is not None:
        bullets.append(f"현금성 자산은 {_pct(cash.get('weight_pct'))} 입니다.")  # 예수금 + 현금 취급 종목
    rebalance = portfolio.get("rebalancing", {})
    if rebalance.get("ready"):
        bullets.append(
            f"설정한 목표 비중 기준으로 총 매수 {_won(rebalance.get('buy_total_krw'))}, "
            f"총 매도 {_won(rebalance.get('sell_total_krw'))}이 필요합니다."
        )
        scaling = rebalance.get("scaling") or {}
        if scaling.get("scaled"):
            bullets.append(f"주봉 MA 전략을 반영하면 지금 매수는 {_won(scaling.get('buy_now_total_krw'))}, "
                           f"나머지 {_won(scaling.get('wait_total_krw'))}은 진입 조건이 될 때까지 대기합니다.")
        moves = [r for r in rebalance.get("rows", []) if r.get("action") in ("buy", "sell")]
        moves.sort(key=lambda r: abs(r.get("difference_krw") or 0), reverse=True)
        if moves:
            tables.append(_moves_table(moves, rebalance))
            signals = rebalance.get("signals") or []
            if signals:
                tables.append(_signals_table(signals))
            if rebalance.get("quote_errors") and any(r.get("allocations") for r in moves):
                limitations.append("현재가를 받지 못해 주수를 계산하지 못한 종목: " + ", ".join(rebalance["quote_errors"]))
            signals_ok = [s for s in (rebalance.get("signals") or []) if s.get("status") == "ok"]
            if any(s.get("recovered") for s in signals_ok):
                limitations.append("'회복 완료'는 모든 매수 단계를 돌파한 뒤 회복선(MA20·60·120·200·240주) 중 하나라도 위로 올라서면 기록되고, "
                                   "2차 이후 사이클을 설정하지 않았다면 그대로 유지됩니다. 그때 진입 비율은 100%입니다.")
            if any(x.get("ok") is False for s in signals_ok for x in s.get("steps", [])):
                limitations.append("상장 기간이 짧아 이동평균 계산에 필요한 주봉이 부족한 단계는 판정하지 못하며, 돌파로 세지 않습니다.")
            if (rebalance.get("scaling") or {}).get("scaled"):
                limitations.append("지금 매수 금액은 카테고리 매수액을 종목별로 나눈 뒤 각 종목의 진입 비율을 곱한 값입니다. "
                                   "현재 보유분을 반영하는 전략 화면의 계산과 다르며, 보유하지 않은 종목은 과거 돌파 기록 없이 오늘 시세 기준으로만 판정했습니다. "
                                   "전략의 돌파 기록은 이 답변에서 저장하지 않습니다.")
            if any(r.get("allocations") for r in moves):
                limitations.append("주수는 카테고리 안에서 보유 평가금액 비율로 나눈 값입니다(보유 종목이 없으면 설정의 첫 번째 종목). "
                                   "국내 종목은 정수 주(내림), 해외 종목은 소수 둘째 자리(내림)입니다.")
    elif rebalance.get("rows"):
        bullets.append(f"목표를 설정한 카테고리의 비중 합계가 {_pct(rebalance.get('target_total_pct'))}라서 "
                       "리밸런싱 금액 계획을 확정할 수 없습니다. 합계를 100%로 맞춰 주세요.")
    elif rebalance:
        bullets.append("목표를 설정한 카테고리가 없어 리밸런싱 금액 계획을 만들 수 없습니다.")
    excluded = rebalance.get("excluded") or {}
    if rebalance and ((excluded.get("holdings_count") or 0) > 0 or (excluded.get("cash_pct") or 0) > 0):
        parts = []
        if (excluded.get("holdings_count") or 0) > 0:
            parts.append(f"목표를 설정하지 않은 보유종목 {int(excluded['holdings_count'])}개 ({_pct(excluded.get('holdings_pct'))})")
        if (excluded.get("cash_pct") or 0) > 0:
            parts.append(f"현금 ({_pct(excluded.get('cash_pct'))})")
        notes.append("와 ".join(parts) + ("은" if (excluded.get("cash_pct") or 0) > 0 else "는") + " 리밸런싱 계산에서 제외했습니다.")
    _charts_overview(portfolio, charts)
    limitations.extend(portfolio.get("limitations", []))
    limitations.extend(rebalance.get("warnings", []))


def compose_get_weekly_risk_report(weekly, bullets, tables, notes, limitations, charts, results):
    """get_weekly_risk_report 결과를 답변 조각(글·표·그래프)으로 조립한다."""
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


def compose_get_sector_exposure(sector, bullets, tables, notes, limitations, charts, results):
    """get_sector_exposure 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    if not sector.get("configured"):
        bullets.append("섹터 매핑이 설정되어 있지 않습니다. 포트폴리오 탭의 섹터 설정에서 먼저 종목을 섹터에 연결하세요.")
    else:
        for s in sorted(sector.get("sectors", []), key=lambda x: x.get("exposure_pct") or 0, reverse=True)[:5]:
            names = ", ".join(h.get("name") or h.get("symbol") for h in s.get("holdings", [])[:3]) or "연결된 보유종목 없음"
            bullets.append(f"{s.get('label')}: 내 노출 {_won(s.get('exposure_krw'))} ({_pct(s.get('exposure_pct'))}) — {names}")
        if sector.get("unmapped_count"):
            bullets.append(f"섹터에 연결하지 않은 보유종목이 {sector['unmapped_count']}개 있습니다.")
    _charts_sector(sector, charts)
    limitations.extend(sector.get("limitations", []))


FORMATTERS = {"get_portfolio_overview": portfolio_overview, "get_weekly_risk_report": weekly_risk_report, "get_sector_exposure": sector_exposure}
COMPOSERS = {"get_portfolio_overview": compose_get_portfolio_overview, "get_weekly_risk_report": compose_get_weekly_risk_report, "get_sector_exposure": compose_get_sector_exposure}
