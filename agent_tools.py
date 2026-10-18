"""AI Agent가 사용할 읽기 전용 도구 계약과 결과 축약.

LLM에는 토스 토큰·앱키·계좌번호나 대시보드의 전체 응답을 주지 않는다. 서버가 기존의
결정론적 계산을 실행한 뒤, 이 모듈이 허용한 필드만 작은 JSON으로 만들어 전달한다.
"""
from __future__ import annotations

import re
from typing import Callable

from cases import CASES
from news import HEADLINES_PER_SYMBOL, SOURCE_MAX, TITLE_MAX, safe_url
import charts as chart_mod
import trend as trend_mod
import response as response_mod
from screener import DEFAULT_PERIODS, MAX_PERIOD, MAX_PERIODS, MIN_PERIOD, SECTOR_KO, SECTOR_LABELS
from outlook import DEPTH_TEXT, MIN_ANALOGS, MIN_EPISODES, TREND_TEXT


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
    {
        "name": "get_outlook",
        "description": "보유·관심 종목의 앞으로의 상승/하락 뷰 재료를 조회합니다: 현재 국면, 과거 유사 국면에서 이후 1·3개월 수익률 통계(독립 사례 수 포함), 요인(달러·금리 등) 민감도 시나리오, 변동성 범위. 예측이 아니라 조건부 시나리오이며 투자 조언이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "보고 싶은 종목 이름·코드·카테고리 이름(예: 금, TLT, 스트래티지). 비우면 보유·관심 종목 전체"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_etf_breakdown",
        "description": "보유·관심 ETF의 이번 주(최근 5거래일) 등락을 야후가 공시한 상위 편입 종목의 기여(비중 × 종목 등락, %p)로 나누고, 큰 기여 종목의 매크로 요인과 최근 보도를 붙입니다. 상위 편입 종목은 전체의 일부이고 공시 시점 비중이라 '그 밖'이 남으며, 합산 분해일 뿐 원인의 증명이 아닙니다. 편입 정보가 없는 종목(개별주·금/채권 ETF·한국 ETF 등)은 대상이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 ETF만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "분해할 ETF 이름·코드·카테고리 이름(예: UFO, 우주). 비우면 보유·관심 ETF 전체"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_sector_trends",
        "description": "미국 업종 ETF(SPDR 11개: XLK·XLC·XLY·XLF·XLI·XLV·XLP·XLU·XLE·XLB·XLRE)나 내 보유·관심 종목의 현재 상태를 조회합니다 — 시장 전체의 업종 ETF 상태를 묻는 질문에 씁니다(내 포트폴리오의 업종 노출이 아닙니다). 1주·단기(기본 1개월)·중기(3개월)·6개월·장기(12개월) 수익률, 50일선·200일선 대비, 전 고점(상장 이후 최고 종가) 대비, S&P500 대비 중기 성과를 주고, 기간 사이의 엇갈림으로 상태를 붙입니다(상승 유지·약화 초입·조정 중·상승 전환 초입·장기선 회복 시도·반등 시도·약세 속 혼조·하락 전환 확인·하락 유지·장기선 부근). 같은 상태가 몇 주째인지(연속 2주 이상이면 확인, 아니면 미확정)와 직전 상태도 줍니다. focus는 보이는 정렬 순서만 바꿉니다(short·mid·long). 위치·방향 정보일 뿐 매매 신호나 전망이 아닙니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "universe": {"type": "string", "enum": ["sectors", "mine"], "description": "sectors=미국 SPDR 업종 ETF 11개(기본), mine=내 보유·관심 종목"},
            "focus": {"type": "string", "enum": ["all", "short", "mid", "long"], "description": "all=3·6·12개월 평균 순위(기본), short=단기 수익률 순, mid=중기, long=장기. 상태 판정 기준은 바뀌지 않음"},
            "buffer_pct": {"type": "number", "minimum": 0, "maximum": 10, "description": "이동평균 ±버퍼% 안은 이탈·회복으로 세지 않음(기본 2)"},
            "confirm_weeks": {"type": "integer", "minimum": 1, "maximum": 8, "description": "같은 상태가 연속 몇 주 유지돼야 확인인지(기본 2)"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_ma_screen",
        "description": "이동평균 스크리너: S&P500 구성 종목(503개, 공개 데이터셋 기준)이나 내 보유·관심 종목 중, 종가가 지정한 주봉 이동평균(예: 60·120·200·240주) 모두 또는 하나라도 아래(또는 위)에 있는 종목을 찾습니다. 종목별 종가와 MA 대비 위치(%), 업종 분포, 주봉이 모자라 판단하지 못한 종목 수를 줍니다. 첫 조회는 500개 시세를 받느라 30~60초 걸릴 수 있고 이후 캐시됩니다. 위치 정보일 뿐 매매 신호가 아닙니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "universe": {"type": "string", "enum": ["sp500", "mine"], "description": "sp500=S&P500 구성 종목(기본), mine=내 보유·관심 종목"},
            "periods": {"type": "array", "items": {"type": "integer", "minimum": 2, "maximum": 300}, "maxItems": 8, "description": "주봉 이동평균 기간 목록(예: [60,120,200,240]). 비우면 60·120·200·240"},
            "side": {"type": "string", "enum": ["below", "above"], "description": "below=종가가 MA 아래(기본), above=위"},
            "match": {"type": "string", "enum": ["all", "any"], "description": "all=모든 기간 조건 충족(기본), any=하나라도"},
            "sector": {"type": "string", "description": "GICS 업종 영문 이름 일부(예: Information Technology). 선택"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_asset_drivers",
        "description": "보유·관심 종목이 평소 가장 민감한 매크로 요인 순위(기본 3개)를 조회합니다. 요인마다 최근 1년·3년·10년 일간 상관과 모든 창에서 같은 방향인지(일관성), 요인이 1표준편차 움직일 때 종목의 평균 반응, 같이 움직여서 대표 하나로 묶은 요인(예: 5·10·30년물 금리), 1년 회귀 모형의 설명력과 검증 설명력을 줍니다. '이번 주 무엇 때문에 움직였나'가 아니라 '평소 무엇에 가장 영향을 받나'에 쓰며, 같이 움직인 정도이고 원인의 증명이 아닙니다. 금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물·지수)을 씁니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: 금, TLT, UFO). 비우면 보유·관심 종목(최대 4개)"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_macro_cases",
        "description": "거시 사례집: 2008 금융위기, 2013 테이퍼 텐트럼, 2020 코로나, 2022 긴축, 1980년대 볼커 긴축 등 12개 사례의 국면별 이야기(해석)와, 그 국면에서 금·금리·달러·증시·VIX가 실제로 얼마나 움직였는지(시세로 계산), '금리 오르면 금 하락' 같은 통념이 맞았는지 점검, 그 뒤 3개월 금 변화를 줍니다. 또 지금(최근 6개월)과 요인 움직임이 가장 닮은 과거 국면을 찾아 줍니다. case에 사례 이름·연도·사건 키워드를 주면 그 사례를, focus에 종목을 주면 그 종목의 국면별 변화도 함께 봅니다. 이야기는 해석이고 닮은 국면이 같은 결말을 보장하지 않으며 예측이 아닙니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "case": {"type": "string", "description": "보고 싶은 사례의 이름·연도·사건(예: 2008 금융위기, 코로나, 볼커). 비우면 지금과 닮은 국면만"},
            "focus": {"type": "string", "description": "국면별 변화를 같이 볼 종목 이름·코드·카테고리 이름(예: TLT, 금). 선택"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_macro_history",
        "description": "지금 큰 변동 중인 매크로 요인(최근 1개월 금리·달러·유가·VIX·환율·지수가 그 요인 자신의 역사에서 상위·하위 10%)과, 과거 같은 방향의 큰 변동 때 보유·관심 종목이 어떻게 움직였는지(독립 사례 수·중앙값·평소와의 통계적 차이)를, 이름 붙은 거시 이슈(리먼·코로나·긴축 등)와 함께 조회합니다. 이 종목이 가장 크게 떨어지거나 오른 과거 이슈도 줍니다. 같이 움직인 기록이며 원인의 증명이 아닙니다. 금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물 2000년~, 지수)을 씁니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: TLT, 금, 스트래티지). 비우면 보유·관심 종목(최대 6개)"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_drawdown_analysis",
        "description": "보유·관심 종목이 고점 대비 얼마나 빠졌고 왜 빠졌는지 조회합니다: 최근 1년 고점의 날짜·가격과 현재 낙폭, 그 사이 최저점, 상장 이후 전 고점 대비 낙폭, 같은 구간(고점 날짜~지금)에서 금리·달러·유가·VIX·환율·지수가 얼마나 움직였는지, 종목 하락의 요인별 기여 추정(%p, 최근 1년 일간 관계의 계수 × 구간 요인 변화)과 설명력(R²)·검증 설명력을 줍니다. 통계적 추정이며 인과의 증명이 아닙니다. 요인으로 설명되지 않으면(R² 낮음) 그렇다고 알리며, 그때는 get_weekly_news로 최근 보도를 같이 보세요(뉴스는 최근 7일만). '왜 올랐나·내렸나'가 이번 주면 get_macro_drivers를, 고점 대비 낙폭이면 이 도구를 씁니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: 금, TLT, UFO). 비우면 보유·관심 종목(최대 4개)"}}, "additionalProperties": False},
    },
    {
        "name": "get_factor_response",
        "description": "'미국 금리가 오르면 금과 나스닥은 어떻게 돼?'처럼 요인(금리·달러·유가·VIX·환율·구리)이 크게 오른/내린 때 주요 자산(금·나스닥·S&P500·코스피·장기채·비트코인 등)이 평소와 어떻게 달랐는지 조회합니다. 보유 종목과 무관하게 지난 수십 년(자산마다 시세가 있는 기간)의 겹치지 않는 구간(기본 한 달)을 요인 변화가 상위 20%(큰 상승)·하위 20%(큰 하락)·보통으로 나눠, 구간 수와 수익률 중앙값·오른 비율을 평소와 비교하고(이항검정, 본페로니 보정), 일간 상관(1년·3년·10년)과 지금 구간이 어디에 해당하는지 줍니다. 같은 구간에 같이 움직인 기록이며 원인의 증명이 아니고, 요인이 왜 움직였는지(경기·물가·긴축)에 따라 달라질 수 있습니다. 보유 종목의 이번 주 요인 분해는 get_macro_drivers, 보유 종목이 과거 큰 변동 때 어땠는지는 get_macro_history를 씁니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "factor": {"type": "string", "description": "조건을 거는 요인(예: 금리, 30년물, 달러, 유가, VIX, 환율, 구리)"},
            "direction": {"type": "string", "enum": ["up", "down"], "description": "질문이 묻는 방향: up=오르면, down=내리면(표에는 둘 다 나옴)"},
            "assets": {"type": "array", "items": {"type": "string"}, "maxItems": 6, "description": "반응을 볼 자산 이름(예: 금, 나스닥, S&P500, 코스피, 장기채, 비트코인, 반도체). 비우면 금·나스닥·S&P500·코스피·장기채·비트코인"},
            "horizon_days": {"type": "integer", "enum": [5, 21, 63], "description": "구간 길이(거래일): 5=일주일, 21=한 달(기본), 63=3개월"}},
                         "required": ["factor"], "additionalProperties": False},
    },
    {
        "name": "get_valuation",
        "description": "보유·관심 종목의 PER(후행·선행)·PSR·PBR·EPS·BPS·ROE와 비싼가·싼가(고평가·저평가) 판단을 조회합니다. 개별주는 같은 업종 S&P500 종목 표본의 중앙값 대비 PER·PSR·PBR, 성장 대비(PEG), 수익성 대비(ROE로 정당화되는 PBR)의 신호를 모아 '고평가 쪽·저평가 쪽·비슷함·판단 보류'로 말하고, 주식형 ETF는 PER·PBR을 시장(S&P500 ETF) 대비로만 봅니다(EPS·BPS·ROE 없음, 금·채권 ETF는 해당 없음). 적자 종목은 PER을 계산하지 않고, 한국 종목은 야후 재무 데이터가 불완전해 지표만 보여 주고 판단하지 않습니다. 야후 비공식 데이터 기반의 상대 평가이며 적정 주가나 투자 조언이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다(최대 4개).",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "볼 종목 이름·코드·카테고리 이름(예: AAPL, 엔비디아, SPY). 비우면 보유·관심 종목(최대 4개)"}}, "additionalProperties": False},
    },
    {
        "name": "get_macro_drivers",
        "description": "이번 주 금리·달러·유가·VIX·환율·지수 변화와, 보유·관심 종목의 등락이 그 요인들과 얼마나 같이 움직였는지(요인별 기여 추정, 설명력 R²)를 조회합니다. 통계적 추정이며 인과의 증명이 아닙니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "보고 싶은 종목 이름·코드·카테고리 이름(예: UFO, 금, 스트래티지). 비우면 보유·관심 종목 전체"}}, "additionalProperties": False},
    },
    {
        "name": "get_weekly_news",
        "description": "보유 종목과 관심종목의 최근 5거래일 등락률과 최근 7일 뉴스 헤드라인(제목·출처·날짜·링크)을 조회합니다. 보도 제목은 참고용이며 가격 변동의 원인으로 확정할 수 없습니다. focus에 종목 이름·코드·카테고리 이름을 주면 그 종목만 봅니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "보고 싶은 종목 이름·코드·카테고리 이름(예: UFO, 금, 스트래티지). 비우면 보유·관심 종목 전체"}}, "additionalProperties": False},
    },
    {
        "name": "get_sector_exposure",
        "description": "사용자가 직접 정한 섹터 매핑 기준으로 섹터별 내 노출 금액·비중과 연결된 보유종목을 조회합니다.",
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


def _round(value, digits: int = 2):
    value = _number(value)
    return None if value is None else round(value, digits)


def outlook_view_result(data: dict) -> dict:
    """뷰 재료에서 숫자와 짧은 이름만 남긴다(종목 12개, 시나리오 요인 3개까지)."""
    items = []
    for it in data.get("items", [])[:12]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")),
                "status": "ok" if it.get("status") == "ok" else "no_data", "history_days": int(_number(it.get("history_days")) or 0)}
        if base["status"] == "ok":
            an = it.get("analogs") or {}
            fwd = {k: ({m: _round((an.get("forward") or {}).get(k, {}).get(m)) if m != "n" else int(_number((an.get("forward") or {}).get(k, {}).get(m)) or 0)
                        for m in ("n", "median", "p25", "p75", "up_share")} if (an.get("forward") or {}).get(k) else None)
                   for k in ("1M", "3M")}
            scen = it.get("scenarios") or {}
            clean = lambda rows: [{"label": str(r.get("label") or ""), "phrase": str(r.get("phrase") or ""), "move": str(r.get("move") or ""),
                                   "impact_pct": _round(r.get("impact_pct"))} for r in (rows or [])[:3]]
            base.update({
                "price": _round(it.get("price")), "trend": it.get("trend") if it.get("trend") in TREND_TEXT else "below",
                "depth": it.get("depth") if it.get("depth") in DEPTH_TEXT else "pullback", "drawdown_pct": _round(it.get("drawdown_pct"), 1),
                "month_change_pct": _round(it.get("month_change_pct"), 1), "annual_vol_pct": _round(it.get("annual_vol_pct"), 0),
                "bands": {k: {"low_pct": _round((it.get("bands") or {}).get(k, {}).get("low_pct"), 1),
                              "high_pct": _round((it.get("bands") or {}).get(k, {}).get("high_pct"), 1),
                              "low": _round((it.get("bands") or {}).get(k, {}).get("low")), "high": _round((it.get("bands") or {}).get(k, {}).get("high"))}
                          for k in ("1M", "3M")},
                "analogs": {"relaxed": bool(an.get("relaxed")), "episodes": int(_number(an.get("episodes")) or 0), "forward": fwd,
                            "source": ({"label": str(an["source"].get("label") or "")[:60], "kind": "monthly" if an["source"].get("kind") == "monthly" else "daily",
                                        "from": str(an["source"].get("from") or "")[:10], "to": str(an["source"].get("to") or "")[:10],
                                        "points": int(_number(an["source"].get("points")) or 0)} if isinstance(an.get("source"), dict) else None),
                            "source_error": str(an.get("source_error") or "")[:60] or None,
                            "baseline_up_share_3m": _round((an.get("baseline_3m") or {}).get("up_share")),
                            "skew": an.get("skew") if an.get("skew") in ("up", "down", "mixed", "insufficient") else "insufficient"},
                "scenarios": {"r2": _round(scen.get("r2")), "up": clean(scen.get("up")), "down": clean(scen.get("down"))},
            })
        items.append(base)
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "상승/하락 뷰는 예측이나 투자 조언이 아니라, 과거 통계·요인 민감도·변동성으로 만든 조건부 시나리오입니다.",
            f"과거 유사 상황은 200일선 위치와 52주 고점 대비 낙폭 구간이 같았던 시점의 이후 수익률입니다. 기본은 같은 종목의 상장 이후 시세(최대 30년)이고, 금 ETF·주요 지수 ETF는 더 긴 대용 시세(금 월간 시세 1975년~, 지수 일봉)를 쓰며 표에 기준을 밝힙니다. 유사 시점이 {MIN_ANALOGS}개 미만이거나 독립 사례가 {MIN_EPISODES}회 미만이면 판단하지 않습니다.",
            "요인 시나리오는 최근 1년 회귀 계수 기준이며 설명력(R²) 0.2 미만이면 만들지 않습니다. 시장 구조가 바뀌면 달라질 수 있습니다.",
            "변동성 범위는 최근 60일 일간 변동성의 ±1표준편차로 폭만 보여주며 방향을 예측하지 않습니다.",
        ] + (["월간 시세는 월평균 가격이고 한 달에 한 점이라, 같은 규칙(200일선·52주 고점)을 10개월 평균선·12개월 고점으로 근사해 일간 자료보다 국면 판정이 거칩니다. 금 가격이 자유화된 1975년 이후만 씁니다."]
             if any((i.get("analogs") or {}).get("source", {}) and i["analogs"]["source"]["kind"] == "monthly" for i in items if i.get("status") == "ok") else [])
          + (["대용 시세(지수)는 ETF와 같은 상품이 아니며 가격 기준(배당 제외)입니다. 현재 국면은 항상 종목 자신의 시세로 판정합니다."]
             if any((i.get("analogs") or {}).get("source", {}) and i["analogs"]["source"]["kind"] == "daily" for i in items if i.get("status") == "ok") else []),
    }


def macro_history_result(data: dict) -> dict:
    """과거 매크로 충격 반응에서 숫자와 짧은 이름만 남긴다(종목 6개, 종목당 요인 6개·대표 사례 3개·이슈 3개까지)."""
    def ev(rows):
        return [{"name": str(r.get("name") or "")[:80], "start": str(r.get("start") or "")[:10], "end": str(r.get("end") or "")[:10],
                 "return_pct": _round(r.get("return_pct"), 1), "z": _round(r.get("z"), 1)} for r in (rows or [])[:3]]
    items = []
    for it in data.get("items", [])[:6]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")),
                "status": "ok" if it.get("status") == "ok" else "no_data", "history_days": int(_number(it.get("history_days")) or 0),
                "source": str(it.get("source") or "")[:40] or None}
        if base["status"] == "ok":
            active = []
            for s in (it.get("active") or [])[:6]:
                active.append({
                    "label": str(s.get("label") or "")[:30], "direction": "up" if s.get("direction") == "up" else "down",
                    "phrase": str(s.get("phrase") or "")[:30], "current_change": _round(s.get("current_change"), 1),
                    "unit": "bp" if s.get("unit") == "bp" else "%", "percentile": _round(s.get("percentile"), 3),
                    "episodes": int(_number(s.get("episodes")) or 0), "median_return": _round(s.get("median_return"), 1),
                    "neg_share": _round(s.get("neg_share"), 3), "p_value": _round(s.get("p_value"), 3),
                    "verdict": s.get("verdict") if s.get("verdict") in ("up", "down", "mixed", "insufficient") else "insufficient",
                    "examples": [{"date": str(e.get("date") or "")[:10], "label": str(e.get("label") or "")[:80] or None,
                                  "factor_change": _round(e.get("factor_change"), 1), "asset_return": _round(e.get("asset_return"), 1)}
                                 for e in (s.get("examples") or [])[:3]]})
            b = it.get("baseline") or {}
            base.update({"history_from": str(it.get("history_from") or "")[:10], "events_covered": int(_number(it.get("events_covered")) or 0),
                         "baseline": {"median": _round(b.get("median"), 1), "neg_share": _round(b.get("neg_share"), 3)} if b else None,
                         "active": active, "worst_events": ev(it.get("worst_events")), "best_events": ev(it.get("best_events"))})
        items.append(base)
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "같이 움직인 과거 기록이며 원인의 증명이 아닙니다. 큰 변동은 여러 일이 겹친 시기(위기·긴축 등)에 함께 나타나서, 어느 한 요인 때문이라고 말할 수 없습니다.",
            "'큰 변동'은 최근 1개월(21거래일) 변화가 그 요인 자신의 역사에서 상위·하위 10%인 경우입니다. 21거래일 이상 떨어진 것을 별개 사례로 세고, 독립 사례가 5회 미만이면 판단하지 않습니다.",
            "평소(아무 21거래일)의 하락 비율과 비교한 이항 검정을 하고, 한 종목에서 큰 변동 중인 요인 수만큼 p값에 곱해(본페로니 보정) 0.05 미만일 때만 '통계적으로 구분됨'으로 표시합니다. 그래도 같은 위기 시기가 여러 사례에 겹쳐 있어 실제 독립성은 표시된 사례 수보다 낮을 수 있습니다.",
            "이름 붙은 거시 이슈의 기간은 시장 반응이 두드러진 대략의 구간이며, 목록에 없는 이슈는 이름이 붙지 않습니다(2025년 4월까지의 이슈만 담았습니다).",
            "금 ETF는 금 선물 일봉(2000년~), 지수 ETF는 지수 일봉(가격 기준, 배당 제외)으로 대신 계산했고 표에 밝힙니다. 상장이 최근인 그 밖의 종목은 자신의 시세(상장 이후)만 씁니다.",
        ],
    }


def asset_drivers_result(data: dict) -> dict:
    """요인 순위에서 숫자와 짧은 이름만 남긴다(종목 4개, 매크로 요인 3개·비슷한 자산 3개 + 순위 밖 3개까지)."""
    def row(d):
        corr = {str(k)[:6]: _round(v, 2) for k, v in (d.get("corr") or {}).items() if _number(v) is not None}
        return {"symbol": str(d.get("symbol") or "")[:20], "label": str(d.get("label") or "")[:30], "up": str(d.get("up") or "")[:20], "down": str(d.get("down") or "")[:20],
                "corr": corr, "consistent": bool(d.get("consistent")), "reaction_1m_pct": _round(d.get("reaction_1m_pct"), 1),
                "absorbed": [str(x)[:30] for x in (d.get("absorbed") or [])[:4]]}
    items = []
    for it in data.get("items", [])[:4]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")),
                "status": "ok" if it.get("status") == "ok" else "no_data", "source": str(it.get("source") or "")[:40] or None}
        if base["status"] == "ok":
            model = it.get("model") or {}
            base.update({"drivers": [row(d) for d in (it.get("drivers") or [])[:3]], "peers": [row(d) for d in (it.get("peers") or [])[:3]],
                         "others": [row(d) for d in (it.get("others") or [])[:3]],
                         "asset_vol_1m_pct": _round(it.get("asset_vol_1m_pct"), 1), "windows": [str(w)[:6] for w in (it.get("windows") or [])[:4]],
                         "model": {"factors": [str(x)[:30] for x in (model.get("factors") or [])[:3]], "r2": _round(model.get("r2")), "oos_r2": _round(model.get("oos_r2"))} if model else None})
        items.append(base)
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")), "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "같이 움직인 정도(일간 수익률 상관)이며 원인의 증명이 아닙니다. 요인끼리도 서로 영향을 주고받습니다.",
            "순위는 1년·3년·10년 상관의 평균 크기이고, 모든 창에서 같은 방향(각각 0.1 이상)인 요인만 '일관됨'으로 보며 일관되지 않으면 점수를 절반으로 줄입니다. 시세가 짧아 없는 창은 빼고 계산합니다.",
            "매크로 요인(금리·통화·에너지와 산업용 원자재·VIX·MOVE·FRED 지표)과 함께 움직이는 비슷한 자산·시장(귀금속·주가지수·업종·채권 ETF·가상자산)은 따로 순위를 냅니다 — 같은 계열 자산(금과 은 등)이 매크로 요인을 가리지 않게 하려는 것이고, 비슷한 자산은 같은 계열이라 같이 움직이는 것이지 원인이 아닙니다. 서로 0.7 이상 같이 움직이는 요인(예: 5·10·30년물 금리, 나스닥·S&P500)은 각 계열 안에서 점수가 높은 하나로 묶었습니다. 종목과 사실상 같은 시리즈(같은 자산의 지수·선물)는 요인에서 제외합니다.",
            "'1표준편차 반응'은 요인이 1개월 동안 평소 크기(표준편차)만큼 움직일 때 종목이 평균적으로 보인 반응(3년 상관 × 종목의 1개월 변동폭)입니다. 실제 움직임은 이보다 크게 벗어날 수 있습니다.",
            "금 ETF·주요 지수 ETF는 더 긴 대용 일봉(금 선물 2000년대~, 지수)으로 계산했고 표에 밝힙니다. 상관은 시기에 따라 달라져(예: 구리와 금은 1년 상관이 10년보다 큼) 최근에는 더 강하거나 약할 수 있습니다.",
        ],
    }


def macro_cases_result(data: dict) -> dict:
    """사례집 결과에서 숫자와 짧은 글만 남긴다(사례 3개, 국면 6개, 닮은 국면 3개까지)."""
    def moves(rows):
        return [{"key": str(m.get("key") or "")[:20], "label": str(m.get("label") or "")[:20], "value": _round(m.get("value"), 1),
                 "unit": "bp" if m.get("unit") == "bp" else "%"} for m in (rows or [])[:12] if _number(m.get("value")) is not None]
    cases = []
    for c in (data.get("cases") or [])[:3]:
        phases = []
        for p in (c.get("phases") or [])[:6]:
            phases.append({
                "name": str(p.get("name") or "")[:60], "start": str(p.get("start") or "")[:10], "end": str(p.get("end") or "")[:10],
                "story": str(p.get("story") or "")[:400], "moves": moves(p.get("moves")),
                "conventions": [{"rule": str(v.get("rule") or "")[:80], "held": v.get("held") if isinstance(v.get("held"), bool) else None,
                                 "detail": str(v.get("detail") or "")[:100]} for v in (p.get("conventions") or [])[:5]],
                "gold_after_pct": _round(p.get("gold_after_pct"), 1),
                "asset_move_pct": _round(p.get("asset_move_pct"), 1), "asset_after_pct": _round(p.get("asset_after_pct"), 1)})
        cases.append({"id": str(c.get("id") or "")[:30], "title": str(c.get("title") or "")[:80], "summary": str(c.get("summary") or "")[:300], "phases": phases})
    similar = []
    for s in (data.get("similar") or [])[:3]:
        similar.append({"case_title": str(s.get("case_title") or "")[:80], "phase": str(s.get("phase") or "")[:60], "start": str(s.get("start") or "")[:10],
                        "end": str(s.get("end") or "")[:10], "distance": _round(s.get("distance")), "shared": int(_number(s.get("shared")) or 0),
                        "gold_after_pct": _round(s.get("gold_after_pct"), 1),
                        "compare": [{"label": str(c.get("label") or "")[:20], "now": _round(c.get("now"), 1), "then": _round(c.get("then"), 1),
                                     "unit": "bp" if c.get("unit") == "bp" else "%", "close": bool(c.get("close"))} for c in (s.get("compare") or [])[:6]]})
    return {
        "named": bool(data.get("named")), "cases": cases, "similar": similar,
        "available": [{"id": str(a.get("id") or "")[:30], "title": str(a.get("title") or "")[:80]} for a in (data.get("available") or [])[:20]],
        "asset": str(data.get("asset") or "")[:60] or None, "has_fred": bool(data.get("has_fred")), "gold_available": bool(data.get("gold_available", True)),
        "limitations": [
            "사건과 날짜는 널리 알려진 것만 넣었고, 국면별 이야기와 사례 요약은 해석입니다. 숫자는 시세에서 계산했고 이야기와 어긋나는 부분은 '통념 점검'에 드러납니다.",
            "금은 2000년 9월 이후 구간은 금 선물 일봉, 그 이전은 월평균 가격(1975년~)이라 거칩니다. 3개월 금리는 연방기금금리 대신 쓴 대용입니다(FRED 키를 넣으면 연방기금금리·실질금리·기대인플레이션도 함께 봅니다).",
            "통념 점검은 큰 변동이 있었던 국면(금리 ±25bp, 달러 ±3%, 증시 −10% 이하 등)에서만 판정하고, 금 변화가 ±3% 미만이면 뚜렷하지 않음으로 둡니다.",
            "지금과 닮은 국면은 최근 6개월 요인 변화를 각 요인의 역사 속 분포로 표준화해 비교한 것이며(요인 4개 이상), 같은 결말을 보장하지 않고 예측이 아닙니다. 사례는 12개 안팎이라 통계가 아니라 참고 사례입니다.",
        ],
    }


def ma_screen_result(data: dict) -> dict:
    """스크리너 결과에서 숫자와 짧은 이름만 남긴다(종목 60개까지, 업종 분포 12개까지)."""
    periods = [int(p) for p in (data.get("periods") or []) if isinstance(p, int) and MIN_PERIOD <= p <= MAX_PERIOD][:MAX_PERIODS] or list(DEFAULT_PERIODS)
    matches = []
    for m in (data.get("matches") or [])[:60]:
        matches.append({"symbol": str(m.get("symbol") or "")[:12], "name": str(m.get("name") or "")[:50],
                        "sector": SECTOR_LABELS.get(m.get("sector"), str(m.get("sector") or "")[:30]), "close": _round(m.get("close")),
                        "mas": [{"period": int(x["period"]), "value": _round(x.get("value")), "dist_pct": _round(x.get("dist_pct"), 2)}
                                for x in (m.get("mas") or [])[:MAX_PERIODS] if isinstance(x, dict) and _number(x.get("dist_pct")) is not None],
                        "hit": [int(h) for h in (m.get("hit") or [])[:MAX_PERIODS] if isinstance(h, int)]})
    return {
        "universe": "mine" if data.get("universe_name") == "mine" else "sp500", "periods": periods,
        "side": "above" if data.get("side") == "above" else "below", "match": "any" if data.get("match") == "any" else "all",
        "sector": SECTOR_LABELS.get(data.get("sector"), str(data.get("sector") or "")[:30]) if data.get("sector") else "",
        "universe_size": int(_number(data.get("universe_size")) or 0), "evaluated": int(_number(data.get("evaluated")) or 0),
        "insufficient": int(_number(data.get("insufficient")) or 0), "failed": int(_number(data.get("failed")) or 0),
        "total_matches": int(_number(data.get("total_matches")) or len(matches)), "as_of": str(data.get("as_of") or "")[:10] or None,
        "matches": matches, "symbols": [str(s)[:12] for s in (data.get("symbols") or [])[:300]],
        "sector_counts": [{"sector": SECTOR_LABELS.get(s.get("sector"), str(s.get("sector") or "")[:30]), "count": int(_number(s.get("count")) or 0),
                           "total": int(_number(s.get("total")) or 0)} for s in (data.get("sector_counts") or [])[:12]],
        "limitations": [
            "구성 종목 목록은 공개 데이터셋(위키피디아 기반, 매일 갱신)이라 공식 S&P 자료가 아니고 최근 편입·편출이 며칠 늦게 반영될 수 있습니다.",
            "주봉은 일봉을 ISO 주 단위로 묶어 주의 마지막 거래일 종가를 쓰고(이번 주는 최근 종가), 단순이동평균입니다. 주봉이 가장 긴 기간보다 적은 종목(신규 상장 등)은 판단하지 않고 따로 셉니다.",
            "이 목록은 가격이 이동평균 아래·위에 있다는 위치 정보일 뿐이며 매수·매도 신호나 전망이 아닙니다. 야후에서 시세를 받지 못한 종목은 빠집니다.",
        ],
    }


def sector_trends_result(data: dict) -> dict:
    """추세 사다리 결과에서 숫자와 짧은 이름만 남긴다(종목 25개까지)."""
    state_names = set(trend_mod.STATE_NAMES)

    def metrics(m):
        m = m if isinstance(m, dict) else {}
        return {k: _round(m.get(k), 1) for k in ("close", "r_week", "r_short", "r_mid", "r_half", "r_long", "dist_fast", "dist_slow", "from_high")}
    rows = []
    for r in (data.get("rows") or [])[:25]:
        state = r.get("state") if r.get("state") in state_names else "장기선 부근"
        prev = r.get("previous_state") if r.get("previous_state") in state_names else None
        rows.append({"symbol": str(r.get("symbol") or "")[:12], "name": str(r.get("name") or "")[:40], "state": state,
                     "since_weeks": int(_number(r.get("since_weeks")) or 1), "confirmed": bool(r.get("confirmed")), "previous_state": prev,
                     "previous_weeks_ago": int(_number(r.get("previous_weeks_ago")) or 0) or None, "metrics": metrics(r.get("metrics")),
                     "vs_benchmark_mid": _round(r.get("vs_benchmark_mid"), 1)})
    cfg = trend_mod.clean_settings(data.get("cfg"))
    bench = data.get("benchmark") or {}
    return {
        "universe": "mine" if data.get("universe") == "mine" else "sectors", "focus": data.get("focus") if data.get("focus") in trend_mod.FOCUS_VALUES else "all",
        "as_of": str(data.get("as_of") or "")[:10] or None, "cfg": cfg, "rows": rows,
        "groups": {s: [str(x)[:12] for x in syms[:30]] for s, syms in (data.get("groups") or {}).items() if s in state_names},
        "changed": [{"symbol": str(c.get("symbol") or "")[:12], "name": str(c.get("name") or "")[:40], "from": c.get("from") if c.get("from") in state_names else None,
                     "to": c.get("to") if c.get("to") in state_names else None, "weeks_ago": int(_number(c.get("weeks_ago")) or 1), "confirmed": bool(c.get("confirmed"))}
                    for c in (data.get("changed") or [])[:25]],
        "insufficient": [str(s)[:12] for s in (data.get("insufficient") or [])[:25]],
        "benchmark": {"name": str(bench.get("name") or "")[:20], "r_mid": _round((bench.get("metrics") or {}).get("r_mid"), 1)} if bench else None,
        "states": [{"state": s, "meaning": m} for s, m in trend_mod.STATES],
        "limitations": [
            "상태 이름과 판정 기준(버퍼·연속 주수·기간)은 정해 둔 규칙이며 어떤 기준도 미래의 추세전환을 보장하지 않습니다. 기간이 짧을수록 빠르지만 거짓 신호가 늘고 길수록 늦습니다.",
            f"기간은 거래일 수입니다(1개월=21일, 3개월=63일, 6개월=126일, 12개월=252일). 이동평균은 {cfg['ma_fast']}일·{cfg['ma_slow']}일 단순이동평균, 이동평균 ±{cfg['buffer_pct']:g}% 안은 이탈·회복으로 세지 않고, 같은 상태가 연속 {cfg['confirm_weeks']}주 이상일 때만 확인으로 봅니다.",
            "'전 고점'은 야후 시세가 있는 전체 기간(상장 이후)의 최고 종가이며 가격 기준이라 배당은 반영되지 않습니다. 상장이 짧은 업종(예: 부동산 2015년~, 커뮤니케이션 2018년~)은 그 이후 기준입니다.",
            "가격 기준(배당 제외)이라 배당이 큰 업종(유틸리티·필수소비재·부동산)의 총수익은 이보다 조금 높습니다. 위치·방향 정보일 뿐 매수·매도 신호가 아닙니다.",
        ],
    }


def etf_breakdown_result(data: dict) -> dict:
    """ETF 구성 종목 기여에서 숫자와 짧은 이름만 남긴다(ETF 6개, 편입 종목 15개, 큰 기여 종목 요인 2개·보도 3건까지)."""
    items = []
    for it in data.get("items", [])[:6]:
        base = {"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")),
                "status": "ok" if it.get("status") == "ok" else "no_data"}
        if base["status"] == "ok":
            rows = [{"symbol": str(r.get("symbol") or "")[:15], "name": str(r.get("name") or "")[:60], "weight_pct": _round(r.get("weight_pct")),
                     "change_pct": _round(r.get("change_pct")), "contribution_pct": _round(r.get("contribution_pct"), 3)}
                    for r in it.get("rows", [])[:15]]
            drivers = {}
            for sym, d in (it.get("drivers") or {}).items():
                drivers[str(sym)[:15]] = {
                    "status": "ok" if d.get("status") == "ok" else str(d.get("status") or "no_data"), "r2": _round(d.get("r2"), 2),
                    "drivers": [{"label": str(c.get("label") or ""), "phrase": str(c.get("phrase") or ""), "factor_change": _round(c.get("factor_change")),
                                 "unit": "bp" if c.get("kind") == "level" else "%", "contribution_pct": _round(c.get("contribution_pct"), 2)}
                                for c in (d.get("contributions") or [])[:2]]}
            headlines = {}
            for sym, block in (it.get("headlines") or {}).items():
                headlines[str(sym)[:15]] = {"error": bool(block.get("error")), "headlines": [
                    {"title": str(h["title"])[:TITLE_MAX], "source": str(h.get("source") or "")[:SOURCE_MAX],
                     "published": str(h.get("published") or "")[:10], "url": safe_url(h.get("url"))}
                    for h in (block.get("headlines") or [])[:HEADLINES_PER_SYMBOL] if h.get("title") and safe_url(h.get("url"))]}
            base.update({
                "window": [str(x)[:10] for x in (it.get("window") or [])[:2]], "etf_change_pct": _round(it.get("etf_change_pct")),
                "rows": rows, "priced_weight_pct": _round(it.get("priced_weight_pct")), "rest_weight_pct": _round(it.get("rest_weight_pct")),
                "explained_pct": _round(it.get("explained_pct"), 2), "residual_pct": _round(it.get("residual_pct"), 2),
                "covered_return_pct": _round(it.get("covered_return_pct")), "rest_implied_pct": _round(it.get("rest_implied_pct")),
                "flags": [f for f in it.get("flags", []) if f in ("low_coverage", "diverged", "missing_prices")],
                "drivers": drivers, "headlines": headlines})
        items.append(base)
    reasons = {"none": "편입 정보 없음", "unavailable": "편입 조회 도구(yfinance) 없음", "error": "편입 정보 조회 실패"}
    return {
        "items": items, "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "no_holdings": [{"symbol": str(n.get("symbol") or ""), "name": str(n.get("name") or ""), "reason": reasons.get(n.get("reason"), "편입 정보 없음")}
                        for n in data.get("no_holdings", [])[:30]],
        "omitted": int(_number(data.get("omitted")) or 0),
        "limitations": [
            "편입 종목과 비중은 야후가 공시한 상위 종목(보통 10개)이라 ETF 전체의 일부이고 공시 시점 값입니다. 나머지는 '그 밖'으로 남습니다.",
            "기여는 비중 × 종목 등락의 합산 분해이며, 같은 기간(ETF의 최근 5거래일 날짜 기준)으로 맞춰 계산했습니다. 원인의 증명이 아니고, 종목이 왜 움직였는지는 보도나 요인으로 따로 봐야 합니다.",
            "큰 기여 종목의 요인은 그 종목 자체의 최근 1년 회귀(설명력 R²)이며, 해외 종목은 현지 통화 기준 등락입니다.",
            "금·채권 ETF, 한국 ETF, 개별주는 야후에 편입 정보가 없어 분해하지 못합니다.",
        ],
    }


def macro_drivers(data: dict) -> dict:
    """요인 변화와 종목별 요인 기여만 남긴다(숫자와 짧은 이름만)."""
    factors = [{"label": str(f.get("label") or ""), "change": _number(f.get("change")), "unit": "bp" if f.get("unit") == "bp" else "%",
                "phrase": str(f.get("phrase") or "")} for f in data.get("factors", [])[:60]]
    items = []
    for it in data.get("items", [])[:30]:
        drivers = [{"label": str(d.get("label") or ""), "phrase": str(d.get("phrase") or ""),
                    "factor_change": _number(d.get("factor_change")), "unit": "bp" if d.get("kind") == "level" else "%",
                    "contribution_pct": _number(d.get("contribution_pct")), "t": _round(d.get("t"), 1),
                    "stable": d.get("stable") if isinstance(d.get("stable"), bool) else None} for d in it.get("drivers", [])[:3]]
        items.append({"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")),
                      "status": str(it.get("status") or "no_data"), "weekly_change_pct": _number(it.get("weekly_change_pct")),
                      "r2": _number(it.get("r2")), "oos_r2": _round(it.get("oos_r2")),
                      "reason": it.get("reason") if it.get("reason") in ("low_r2", "unstable") else "",
                      "drivers": drivers, "residual_pct": _number(it.get("residual_pct"))})
    return {
        "factors": factors, "items": items, "omitted": int(_number(data.get("omitted")) or 0),
        "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "limitations": [
            "요인별 기여는 최근 약 1년 일간 수익률의 통계적 관계(회귀)로 추정한 값이며, 같이 움직인 정도일 뿐 원인으로 확정할 수 없습니다. 설명력(R²)이 낮으면 신뢰하지 마세요.",
            "요인은 계수의 t값이 2 이상(우연과 구분됨)인 것만 남기고, 앞 2/3로 구한 계수가 뒤 1/3을 맞히는지(검증 설명력)와 앞·뒤 절반에서 계수 부호가 같은지(안정성)를 함께 봅니다. 검증 설명력이 0.05 미만이면 표본 안에서 설명돼도 믿을 만하지 않다고 표시합니다. t값은 일간 수익률의 자기상관을 보정하지 않아 다소 낙관적일 수 있습니다.",
            "종목과 거의 같은 움직임인 시리즈(예: 같은 자산의 지수나 선물)는 요인에서 제외했습니다.",
            "국내 종목과 환율은 미국 시장과 거래 시간이 달라 시차 때문에 설명력이 낮게 나올 수 있습니다.",
        ],
    }


def weekly_news(data: dict) -> dict:
    """종목별 등락률과 헤드라인만 남긴다. 링크는 http(s)만, 제목·출처는 길이를 제한한다."""
    items = []
    for it in data.get("items", [])[:30]:
        headlines = []
        for h in it.get("headlines", [])[:HEADLINES_PER_SYMBOL]:
            url = safe_url(h.get("url"))
            if url and h.get("title"):
                headlines.append({"title": str(h["title"])[:TITLE_MAX], "source": str(h.get("source") or "")[:SOURCE_MAX],
                                  "published": str(h.get("published") or "")[:10], "url": url})
        items.append({"symbol": str(it.get("symbol") or ""), "name": str(it.get("name") or ""), "held": bool(it.get("held")),
                      "weekly_change_pct": _number(it.get("weekly_change_pct")), "headlines": headlines,
                      "news_error": bool(it.get("news_error"))})
    return {
        "items": items,
        "omitted": int(_number(data.get("omitted")) or 0),
        "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "limitations": [
            "보도 제목은 야후 파이낸스와 구글 뉴스 검색 결과(비공식)이며 가격 변동의 원인으로 확정할 수 없습니다.",
            "등락률은 최근 5거래일 기준이고, 조회어에는 종목 이름·코드만 쓰며 계좌 정보는 보내지 않습니다.",
            "종목 이름이 티커뿐인 ETF는 야후 기사만 조회하고, 국내 종목은 구글 뉴스만 조회합니다. 비인기 ETF는 관련 보도가 없을 수 있습니다.",
        ],
    }


def drawdown_result(data: dict) -> dict:
    """고점 대비 낙폭과 구간 요인 분해만 남긴다(숫자·날짜·짧은 이름, 종목 4개까지)."""
    items = []
    for it in (data.get("items") or [])[:4]:
        drivers = [{"label": str(d.get("label") or ""), "phrase": str(d.get("phrase") or ""), "factor_change": _number(d.get("factor_change")),
                    "unit": "bp" if d.get("kind") == "level" else "%", "contribution_pct": _number(d.get("contribution_pct")), "t": _round(d.get("t"), 1),
                    "stable": d.get("stable") if isinstance(d.get("stable"), bool) else None} for d in (it.get("drivers") or [])[:3]]
        moves = [{"label": str(m.get("label") or ""), "change": _number(m.get("change")), "unit": "bp" if m.get("unit") == "bp" else "%", "phrase": str(m.get("phrase") or ""),
                  "group": "peer" if m.get("group") == "peer" else "macro"} for m in (it.get("moves") or [])[:60]]
        row = {"symbol": str(it.get("symbol") or "")[:12], "name": str(it.get("name") or "")[:40], "held": bool(it.get("held")),
               "status": it.get("status") if it.get("status") in ("ok", "weak", "near_peak", "no_data") else "no_data",
               "reason": it.get("reason") if it.get("reason") in ("low_r2", "unstable") else "", "basis": "market" if it.get("basis") == "market" else "macro",
               "r2": _number(it.get("r2")), "oos_r2": _round(it.get("oos_r2")), "drivers": drivers, "residual_pct": _round(it.get("residual_pct")), "moves": moves}
        for key in ("last_date", "peak_date", "ath_date", "low_date"):
            row[key] = str(it.get(key) or "")[:10] or None
        for key in ("last_close", "peak_close", "ath_close", "drawdown_pct", "ath_drawdown_pct", "low_drawdown_pct"):
            row[key] = _round(it.get(key), 2)
        row["since_days"] = int(_number(it.get("since_days")) or 0)
        ser = it.get("series") if isinstance(it.get("series"), dict) else {}
        row["series"] = {"dates": [str(d)[:10] for d in (ser.get("dates") or [])[:140]], "closes": [_round(c, 2) for c in (ser.get("closes") or [])[:140]]}
        row["pressure"] = [{"label": str(m.get("label") or "")[:40], "change": _number(m.get("change")), "unit": "bp" if m.get("unit") == "bp" else "%", "phrase": str(m.get("phrase") or "")[:30],
                            "corr": _round(m.get("corr"), 2), "sigma": _round(m.get("sigma"), 1), "direction": "down" if m.get("direction") == "down" else "up"} for m in (it.get("pressure") or [])[:6]]
        row["causes"] = []
        for c in (it.get("causes") or [])[:4]:
            ex = c.get("example") or {}
            url = safe_url(ex.get("url"))
            row["causes"].append({"theme": str(c.get("theme") or "")[:30], "days": [str(d)[:10] for d in (c.get("days") or [])[:4]], "biggest_day": str(c.get("biggest_day") or "")[:10],
                                  "example": {"title": str(ex["title"])[:TITLE_MAX], "source": str(ex.get("source") or "")[:SOURCE_MAX], "url": url} if ex.get("title") and url else None})
        row["drop_days"] = []
        for day in (it.get("drop_days") or [])[:4]:
            headlines = []
            for h in (day.get("headlines") or [])[:HEADLINES_PER_SYMBOL]:
                url = safe_url(h.get("url"))
                if url and h.get("title"):
                    headlines.append({"title": str(h["title"])[:TITLE_MAX], "source": str(h.get("source") or "")[:SOURCE_MAX], "published": str(h.get("published") or "")[:10], "url": url})
            row["drop_days"].append({"date": str(day.get("date") or "")[:10], "change_pct": _round(day.get("change_pct"), 1), "risk_down": bool(day.get("risk_down")),
                                     "movers": [{"label": str(m.get("label") or "")[:40], "change": _number(m.get("change")), "unit": "bp" if m.get("unit") == "bp" else "%",
                                                 "phrase": str(m.get("phrase") or "")[:30]} for m in (day.get("movers") or [])[:3]],
                                     "headlines": headlines})
        items.append(row)
    return {
        "items": items, "omitted": int(_number(data.get("omitted")) or 0), "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "limitations": [
            "최근 고점은 최근 1년(252거래일) 안의 최고 종가이고, 전 고점은 야후 시세가 있는 전체 기간(상장 이후)의 최고 종가입니다(가격 기준, 배당 제외).",
            "요인별 기여는 최근 약 1년 일간 수익률의 통계적 관계(회귀)로 추정한 값에 고점 이후 구간의 요인 변화를 곱한 것입니다. 같이 움직인 정도일 뿐 원인으로 확정할 수 없고, 구간이 길수록 관계가 바뀌었을 가능성이 커집니다. 설명력(R²)이 낮으면 신뢰하지 마세요.",
            "요인 표는 서술일 뿐입니다: 고점 이후 많이 움직인 요인이 곧 하락의 원인이라는 뜻이 아닙니다(종목과의 관계는 기여 추정으로 따로 봅니다).",
            "큰 하락일은 고점 이후 하루 하락폭이 큰 순(1% 이상, 같은 사건의 연속일은 하나로)이고, '같은 날 크게 움직인 시장'은 평소 일간 변동의 2배 이상 움직인 요인입니다. 같은 날 같이 움직인 기록일 뿐 원인의 증명이 아닙니다.",
            "날짜별 보도는 구글 뉴스 검색(비공식)을 그날 앞뒤 하루로 한정해 종목 이름과 그날 크게 움직인 요인 이름(+급락·급등)으로 찾은 제목입니다. 검색어에 걸린 제목일 뿐 그 사건이 하락의 원인이라고 확정할 수 없고, 오래된 날짜는 기사가 빠질 수 있습니다.",
        ],
    }


def factor_response_result(data: dict) -> dict:
    """조건부 반응 결과에서 숫자와 짧은 이름만 남긴다(자산 6개까지)."""
    def stat(st):
        st = st if isinstance(st, dict) else {}
        return {"n": int(_number(st.get("n")) or 0), "median": _round(st.get("median"), 1), "up_share": _round(st.get("up_share"), 3),
                "verdict": st.get("verdict") if st.get("verdict") in ("higher", "lower", "mixed", "insufficient") else "mixed"}
    factor = data.get("factor") if isinstance(data.get("factor"), dict) else {}
    th = data.get("thresholds") if isinstance(data.get("thresholds"), dict) else None
    assets = []
    for a in (data.get("assets") or [])[:6]:
        base = a.get("baseline") or {}
        assets.append({"name": str(a.get("name") or "")[:30], "windows": int(_number(a.get("windows")) or 0), "first": str(a.get("first") or "")[:10],
                       "baseline": {"median": _round(base.get("median"), 1), "up_share": _round(base.get("up_share"), 3)},
                       "up": stat(a.get("up")), "down": stat(a.get("down")), "flat": stat(a.get("flat")),
                       "correlation": {k: _round(v, 2) for k, v in (a.get("correlation") or {}).items()}})
    return {
        "factor": {"name": str(factor.get("name") or "")[:30], "kind": "level" if factor.get("kind") == "level" else "price"},
        "direction": "down" if data.get("direction") == "down" else "up", "horizon": int(_number(data.get("horizon")) or 21),
        "thresholds": {"up": _round(th.get("up"), 1), "down": _round(th.get("down"), 1)} if th else None,
        "current": _round(data.get("current"), 1), "current_bucket": data.get("current_bucket") if data.get("current_bucket") in ("up", "down", "flat") else None,
        "as_of": str(data.get("as_of") or "")[:10] or None, "assets": assets, "insufficient": [str(x)[:30] for x in (data.get("insufficient") or [])[:6]],
        "unresolved": [str(x)[:30] for x in (data.get("unresolved") or [])[:6]],
        "limitations": [
            "같은 구간에 같이 움직인 기록이며 요인이 원인이라는 증명이 아닙니다. 같은 요인이 오르더라도 왜 올랐는지(경기 호조·물가·긴축·위험 회피)에 따라 자산의 반응이 달랐을 수 있습니다.",
            "큰 상승·큰 하락은 그 요인의 구간 변화 분포에서 상위·하위 20%입니다. 구간은 겹치지 않게 최근부터 거슬러 잡았고, 자산마다 시세가 있는 기간이 달라 구간 수가 다릅니다. 구간 수가 적으면(8개 미만) 판단하지 않습니다.",
            "'평소와 구분됨'은 오른 구간 비율이 전체 평균과 이항검정으로 달랐는지(자산 × 큰 상승·큰 하락 전체 검정 수로 본페로니 보정)이며, 수익률의 크기가 아니라 방향의 차이입니다. 표본이 작은 구간은 우연일 수 있습니다.",
            "금·원유·구리는 선물, 달러는 달러 인덱스, 장기채는 TLT 가격(배당 제외) 기준이고 환율·시차 때문에 코스피 등 한국 지수는 미국 요인과의 시차가 있을 수 있습니다. 투자 조언이 아닙니다.",
        ],
    }


VALUATION_LABEL = {"expensive": "고평가 쪽 (비싸 보임)", "cheap": "저평가 쪽 (싸 보임)", "fair": "비슷하거나 혼조 (적정 범위)", "hold": "판단 보류", "na": "해당 없음", "benchmark": "비교 기준"}
VALUATION_REASONS = ("", "korean", "no_equity_multiples", "no_benchmark", "benchmark", "other")


def valuation_result(data: dict) -> dict:
    """밸류에이션 결과에서 숫자와 짧은 글만 남긴다(종목 4개, 신호 6개까지)."""
    items = []
    for it in (data.get("items") or [])[:4]:
        row = {"symbol": str(it.get("symbol") or "")[:15], "name": str(it.get("name") or "")[:40], "held": bool(it.get("held")),
               "status": it.get("status") if it.get("status") in ("ok", "error", "unavailable") else "error"}
        if row["status"] == "ok":
            m = it.get("metrics") or {}
            a = it.get("assessment") or {}
            v = a.get("verdict") or {}
            row.update({
                "kind": it.get("kind") if it.get("kind") in ("stock", "etf", "other") else "other", "peer_n": int(_number(it.get("peer_n")) or 0),
                "metrics": {k: _round(m.get(k), 2) for k in ("per", "forward_per", "psr", "pbr", "eps", "forward_eps", "bps", "roe", "peg", "price")},
                "currency": str(m.get("currency") or "")[:5], "category": str(m.get("category") or "")[:40], "sector": str(m.get("sector") or "")[:30],
                "flags": [f for f in (m.get("flags") or []) if f in ("loss", "per_outlier", "roe_outlier")],
                "verdict": {"label": v.get("label") if v.get("label") in VALUATION_LABEL else "hold", "expensive": int(_number(v.get("expensive")) or 0),
                            "cheap": int(_number(v.get("cheap")) or 0), "signals": int(_number(v.get("signals")) or 0)},
                "reason": a.get("reason") if a.get("reason") in VALUATION_REASONS else "",
                "signals": [{"key": str(x.get("key") or "")[:12], "label": str(x.get("label") or "")[:20], "basis": x.get("basis") if x.get("basis") in ("sector", "growth", "profitability", "market") else "sector",
                             "value": _round(x.get("value"), 2), "ref": _round(x.get("ref"), 2), "ref_label": str(x.get("ref_label") or "")[:60],
                             "premium_pct": _round(x.get("premium_pct"), 1), "verdict": x.get("verdict") if x.get("verdict") in ("expensive", "cheap", "fair") else "fair"}
                            for x in (a.get("signals") or [])[:6]],
                "notes": [str(n)[:140] for n in (a.get("notes") or [])[:4]]})
            im = a.get("implied") if isinstance(a.get("implied"), dict) else None
            row["implied"] = ({"price": _round(im.get("price"), 2), "currency": str(im.get("currency") or "")[:5], "reliable_n": int(_number(im.get("reliable_n")) or 0),
                               "low": _round(im.get("low"), 2), "high": _round(im.get("high"), 2), "mid": _round(im.get("mid"), 2), "wide": bool(im.get("wide")),
                               "methods": [{"key": str(x.get("key") or "")[:12], "label": str(x.get("label") or "")[:24], "price": _round(x.get("price"), 2), "diff_pct": _round(x.get("diff_pct"), 1),
                                            "reliable": bool(x.get("reliable")), "reason": str(x.get("reason") or "")[:80]} for x in (im.get("methods") or [])[:6]]} if im else None)
        items.append(row)
    return {
        "items": items, "omitted": int(_number(data.get("omitted")) or 0), "focus": str(data.get("focus") or "")[:100], "focused": bool(data.get("focused")),
        "assumptions": {"band_pct": 30, "cost_of_equity_pct": 9, "long_growth_pct": 3, "peg_cheap": 1, "peg_expensive": 2},
        "limitations": [
            "야후 파이낸스(비공식) 지표라 지연·누락·오류가 있을 수 있고, 후행 PER은 지난 12개월 이익 기준입니다. 적자 종목은 PER을 계산하지 않습니다.",
            "'비싸 보임·싸 보임'은 정해 둔 기준(같은 업종 S&P500 표본 중앙값 대비 ±30%, PEG 1·2, ROE로 정당화되는 PBR = (ROE − 3%) ÷ (9% − 3%))에 대는 상대 평가이며 적정 주가가 아닙니다. 신호가 2개 미만이면 판단하지 않습니다.",
            "성장주는 PER·PSR이 높은 것이 평범하고, 경기 민감 업종은 이익이 정점일 때 PER이 낮아 보입니다. 자사주 매입이 많으면 자기자본이 작아 PBR·ROE가 왜곡됩니다. 업종 표본은 S&P500 일부(최대 25개)입니다.",
            "ETF의 PER·PBR은 편입 종목 지표의 평균이라 개별 종목과 의미가 다르고, EPS·BPS·ROE는 ETF에 없습니다. 투자 조언이 아닙니다.",
            "'섹터 배수 기준 추정 가격'은 같은 업종 S&P500 표본의 중앙값 배수를 이 회사의 EPS·선행 EPS·주당매출·BPS에 그대로 곱한 값입니다. 이 회사가 업종 평균 배수를 받아야 한다는 가정이라 이익률이 높거나 성장이 빠른 회사는 낮게 나오는 것이 자연스럽고, 방법마다 크게 다를 수 있어 하나의 적정 주가가 아니라 범위로만 봅니다. 믿을 수 없는 방법(적자·BPS 왜곡)은 범위에서 뺍니다.",
        ],
    }


_FORMATTERS: dict[str, Callable[[dict], dict]] = {
    "get_valuation": valuation_result,
    "get_factor_response": factor_response_result,
    "get_drawdown_analysis": drawdown_result,
    "get_portfolio_overview": portfolio_overview,
    "get_weekly_risk_report": weekly_risk_report,
    "estimate_liquidation_tax": liquidation_tax_estimate,
    "get_sector_exposure": sector_exposure,
    "get_weekly_news": weekly_news,
    "get_macro_drivers": macro_drivers,
    "get_outlook": outlook_view_result,
    "get_etf_breakdown": etf_breakdown_result,
    "get_macro_history": macro_history_result,
    "get_macro_cases": macro_cases_result,
    "get_asset_drivers": asset_drivers_result,
    "get_ma_screen": ma_screen_result,
    "get_sector_trends": sector_trends_result,
}


def format_tool_result(name: str, payload: dict) -> dict:
    """허용 목록 밖 도구는 실행하지 않고, 허용된 결과만 Agent용 JSON으로 축약한다."""
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        raise ValueError(f"허용되지 않은 Agent 도구입니다: {name}")
    return {"tool": name, "read_only": True, "result": formatter(payload)}


def _compact(text: str) -> str:
    """키워드 비교용: 소문자로 바꾸고 공백·문장부호를 없앤다(이번 주 = 이번주, 왜 떨어 = 왜떨어)."""
    return re.sub(r"[\s.,!?~\-_'\"]+", "", str(text or "").lower())


# 질문 속 단어 → 부를 도구. 모두 공백 없는 소문자로 적는다(_compact와 같은 모양). 맞는 의도는 모두 실행한다.
WHY_STRICT = ("왜올", "왜떨", "왜빠", "왜하락", "왜상승", "왜내", "왜급", "왜움직", "이유", "원인", "때문")          # 이유를 묻는 말
MOVE_WORDS = ("급락", "급등", "폭락", "폭등", "빠졌", "빠진", "떨어졌", "떨어진", "올랐", "오른", "내렸", "하락했", "상승했", "움직였", "움직인", "무슨일")   # 움직임을 말할 뿐 이유를 묻는 건 아닐 수 있음
WHY_WORDS = WHY_STRICT + MOVE_WORDS
INTENT_WORDS = {
    "tax": ("세금", "양도세", "전부팔", "다팔", "전량매도", "전체매도", "청산"),
    "weekly_review": ("이번주", "금주", "위험", "주의", "신호", "점검", "체크"),
    "portfolio": ("리밸런싱", "목표비중", "비중맞", "자산구성", "포트폴리오", "얼마씩", "더사야", "더팔아야"),
    "outlook": ("뷰", "전망", "앞으로", "시나리오", "예측", "오를까", "내릴까", "오를것", "내릴것", "상승/하락", "상승·하락", "향후",
                "앞날", "떨어질까", "오르려나", "내리려나", "더갈까", "가능성"),
    "macro": ("요인", "금리", "달러", "유가", "환율", "영향", "이번주", "무엇을확인", "뭘확인") + WHY_WORDS,
    "news": ("소식", "뉴스", "이슈", "재료", "기사", "보도", "헤드라인", "이번주", "무엇을확인", "뭘확인") + WHY_WORDS,
    "etf": ("구성종목", "편입", "담고있", "담은종목", "종목별기여", "etf", "안에뭐", "무엇을담") + WHY_WORDS,
    "sector": ("섹터", "업종", "노출"),
    "drivers": ("영향을많이", "영향을가장", "가장영향", "영향받는", "영향을받", "영향받", "영향이큰", "영향큰", "영향주는", "영향을주는", "민감", "상관관계", "상관계수",
                "연관성", "연관된", "주요요인", "주요요소", "핵심요인", "핵심요소", "좌우", "결정하는", "움직이게하는", "움직이는요인", "요소", "요인순위", "영향순위"),
    "cases": ("사례집", "지금과비슷", "어느때와", "어떤때와", "서브프라임", "금융위기", "테이퍼", "텐트럼", "엔캐리", "관세", "볼커", "svb", "실리콘밸리",
              "닷컴", "ltcm", "아시아외환", "피벗", "스태그플레이션", "양적완화", "qe"),
    "history": ("과거", "역사", "예전", "그때", "당시", "사례", "쇼크", "충격", "위기", "리먼", "코로나", "닷컴", "이번처럼", "같은상황", "전에도"),
}
ENTITY_TOOLS = ("get_weekly_news", "get_macro_drivers", "get_outlook", "get_etf_breakdown")   # 종목 이름만 말했을 때 한꺼번에 보여 주는 도구
FOCUS_TOOLS = frozenset(ENTITY_TOOLS) | {"get_macro_history", "get_macro_cases", "get_asset_drivers", "get_drawdown_analysis", "get_valuation"}                                                         # 질문에서 종목을 지정해 좁혀 볼 수 있는 도구


SIMILAR_WORDS = ("비슷", "닮", "유사", "같은시기", "같은때", "똑같")                                             # 견주는 말
NOW_WORDS = ("현재", "지금", "요즘", "오늘", "최근", "시장", "장세", "상황", "어느시", "어느때", "어느해", "어느국면", "언제", "어떤시", "어떤때", "어떤국면", "과거", "시점", "국면", "시기")   # 지금·시점을 가리키는 말
CASE_YEARS = frozenset(k for c in CASES for k in c["keywords"] if k.isdigit() and len(k) == 4)
CASE_WORDS = tuple(sorted({k for c in CASES for k in c["keywords"] if not k.isdigit()}))
INTENT_WORDS["cases"] = tuple(dict.fromkeys(INTENT_WORDS["cases"] + CASE_WORDS))   # 사례집의 사건 이름도 사례를 묻는 말로 본다


# 고점 대비 낙폭 질문: "금이 왜 고점 대비 빠졌어?" — 이번 주 움직임이 아니라 고점 이후 구간을 요인으로 나눠 본다
DRAWDOWN_WORDS = ("고점대비", "고점에서", "고점이후", "고점부터", "전고점", "최고점", "고점찍", "고점이", "고점보다", "낙폭", "고점아래", "고점근처", "얼마나빠졌", "얼마나떨어졌", "얼마나내렸")


def is_drawdown_query(text: str) -> bool:
    """고점 대비 낙폭을 묻는 말(왜 빠졌는지 포함)이면 낙폭 분석 도구로 본다. 업종 ETF 현황·스크리너 질문과 내 업종 노출 질문은 앞에서 따로 걸러진다."""
    compact = _compact(text)
    return any(w in compact for w in DRAWDOWN_WORDS)


# 이동평균 스크리너 질문: "주봉 ma 60/120/200/240 보다 밑에 있는 S&P500 소속 주식들은?" — 종목 하나가 아니라 여러 종목을 훑어 조건에 맞는 것을 찾는 질문
_MA_MENTION = re.compile(r"(주봉|일봉|주선|이평|이동평균|\bma\s*\d|\bma\b|\d+\s*(주|일)?\s*(선|이평|ma))", re.I)
_SCREEN_BELOW = ("밑", "아래", "이하", "하회", "이탈", "깨진", "깨졌", "못미", "낮은", "미만")
_SCREEN_ABOVE = ("위에", "위로", "위의", "위인", "이상", "상회", "돌파", "넘은", "넘어", "높은")
_SCREEN_SCOPE = ("s&p", "sp500", "에스앤피", "소속", "구성종목", "종목들", "주식들", "전체", "모든", "찾아", "리스트", "스크리", "어떤종목", "어떤주식", "어디", "내종목", "내가", "보유", "관심")
_SCREEN_WEAK_SCOPE = ("종목", "주식")                       # '종목' 한 마디는 특정 종목 코드가 같이 없을 때만 여러 종목을 훑는 말로 본다
_TICKER = re.compile(r"\b(?!MA\b|ETF\b)[A-Z]{2,5}\b")
_SCREEN_ANY = ("하나라도", "어느하나", "중하나", "일부", "한개라도", "하나만", "한개만")
_SCREEN_MINE = ("내종목", "내가", "보유", "관심", "내주식")


def is_screen_query(text: str) -> bool:
    """주봉 MA 같은 이동평균 말에 위·아래 조건과 '여러 종목을 훑는' 말이 함께 있으면 스크리너 질문으로 본다(종목 하나를 묻는 질문은 제외)."""
    compact = _compact(text)
    if not (_MA_MENTION.search(str(text or "")) and any(w in compact for w in _SCREEN_BELOW + _SCREEN_ABOVE)):
        return False
    return any(w in compact for w in _SCREEN_SCOPE) or (any(w in compact for w in _SCREEN_WEAK_SCOPE) and not _TICKER.search(str(text or "")))


def parse_screen_query(text: str) -> dict:
    """질문 문장 → 스크리너 인자 {universe, periods, side, match, sector}. 기간을 말하지 않으면 60·120·200·240주,
    아래/위를 말하지 않으면 아래, '하나라도'가 없으면 모두, 범위를 말하지 않으면 S&P500(내 종목이라고 하면 내 보유·관심)."""
    raw = str(text or "")
    compact = _compact(raw)
    cleaned = re.sub(r"s\s*&\s*p\s*500|sp\s*500|상위\s*\d+|\d+\s*(개|등|%|년|달|월|일간)", " ", raw, flags=re.I)
    numbers = [int(n) for n in re.findall(r"(?<![\d.])\d{1,3}(?![\d.])", cleaned) if MIN_PERIOD <= int(n) <= MAX_PERIOD]
    periods = sorted(set(numbers))[:MAX_PERIODS] or list(DEFAULT_PERIODS)
    below = [compact.find(w) for w in _SCREEN_BELOW if w in compact]
    above = [compact.find(w) for w in _SCREEN_ABOVE if w in compact]
    side = "above" if above and (not below or min(above) < min(below)) else "below"
    sp = any(w in compact for w in ("s&p", "sp500", "에스앤피"))
    universe = "mine" if (any(w in compact for w in _SCREEN_MINE) and not sp) else "sp500"
    sector = next((SECTOR_KO[k] for k in sorted(SECTOR_KO, key=len, reverse=True) if k in compact and k != "it"), "")
    return {"universe": universe, "periods": periods, "side": side, "match": "any" if any(w in compact for w in _SCREEN_ANY) else "all", "sector": sector}


# 미국 업종·섹터 ETF 상태·추세 질문: "지금 미국 업종별 etf들 상태 어떰?", "업종 ETF 잘나가는 순", "XLK 추세전환 됐어?" — 내 포트폴리오의 업종 노출 질문과 구분한다
_TREND_SECTOR_WORDS = ("업종", "섹터", "스파이더", "sector", "xlk", "xlc", "xly", "xlf", "xli", "xlv", "xlp", "xlu", "xle", "xlb", "xlre")
_TREND_WORDS = ("잘나가", "못나가", "잘나", "순위", "모멘텀", "추세", "로테이션", "강세", "약세", "전환", "꺾", "반등", "강한", "약한", "돈이몰", "상승세", "하락세", "주도", "소외")
_TREND_STATUS_WORDS = ("상태", "어때", "어떰", "어떻", "현황", "동향", "흐름", "분위기", "요즘", "지금")       # 상태를 묻는 말은 시장 ETF를 가리키는 말(etf·미국 등)이 같이 있을 때만
_TREND_MARKET_WORDS = ("etf", "미국", "spdr", "스파이더", "xl", "시장")
_TREND_EXPOSURE_WORDS = ("노출", "비중", "내포트폴리오", "매핑", "쏠림", "내종목", "내가")      # 내 포트폴리오의 업종 노출·비중 질문은 섹터 노출 도구로


def is_trend_query(text: str) -> bool:
    """미국 업종·섹터 ETF를 두고 잘나가는 순·추세·전환·모멘텀·상태를 묻는 말이면 추세 도구로 본다(내 노출·비중을 묻는 말은 제외)."""
    compact = _compact(text)
    if not any(w in compact for w in _TREND_SECTOR_WORDS) or any(w in compact for w in _TREND_EXPOSURE_WORDS):
        return False
    return any(w in compact for w in _TREND_WORDS) or (any(w in compact for w in _TREND_STATUS_WORDS) and any(w in compact for w in _TREND_MARKET_WORDS))


def parse_trend_query(text: str) -> dict:
    """질문 문장 → 추세 도구 인자 {universe, focus}. '내 종목·보유·관심'이면 내 종목, 그 밖은 업종 ETF.
    '단기·중기·장기'를 말하면 그 기간 순서로 보여 준다(상태 판정 기준은 그대로)."""
    compact = _compact(text)
    universe = "mine" if any(w in compact for w in ("내종목", "내가", "보유", "관심", "내주식")) else "sectors"
    focus = "short" if any(w in compact for w in ("단기", "최근꺾", "이번주", "이번달", "1개월")) else "long" if any(w in compact for w in ("장기", "진짜", "12개월", "1년", "200일", "주봉")) \
        else "mid" if any(w in compact for w in ("중기", "3개월", "6개월", "분기")) else "all"
    return {"universe": universe, "focus": focus}


# 조건부 반응 질문: "미국 금리가 오르면 금과 나스닥은 어떻게 됨?" — 요인이 오르거나 내릴 때 자산의 평소 반응(내 보유 종목 질문은 제외)
RESPONSE_UP = ("오르면", "올라가면", "상승하면", "급등하면", "높아지면", "치솟으면", "뛰면", "강해지면", "커지면", "오른다면", "올라간다면", "오를때", "상승할때", "올랐을때")
RESPONSE_DOWN = ("내리면", "내려가면", "떨어지면", "하락하면", "급락하면", "낮아지면", "약해지면", "작아지면", "내린다면", "떨어진다면", "내릴때", "하락할때", "떨어졌을때", "내렸을때", "빠지면")
RESPONSE_REACTION = ("어떻게", "어떻", "영향", "반응", "어찌", "어떤", "어때")
RESPONSE_OWN = ("내포트폴리오", "내종목", "보유", "내자산", "내가", "내계좌", "내돈")


def _response_parts(text: str):
    """(방향, 요인 (심볼, 이름, 종류), 요인 낱말 끝 위치, 연결어 위치) — 연결어(오르면…) 바로 앞에 나온 요인 낱말이 조건을 거는 요인. 못 찾으면 None."""
    raw = str(text or "").lower()
    compact = re.sub(r"[\s.,!?~'\"]+", "", raw)
    hits = [(compact.find(w), w, "up") for w in RESPONSE_UP if w in compact] + [(compact.find(w), w, "down") for w in RESPONSE_DOWN if w in compact]
    if not hits:
        return None
    conn, _word, direction = min(hits)
    masked = compact
    best = None
    for word in sorted(response_mod.FACTOR_WORDS, key=len, reverse=True):
        for m in re.finditer(re.escape(word), masked):
            if m.end() <= conn and (best is None or m.end() > best[0]):
                best = (m.end(), word)
            masked = masked[:m.start()] + "_" * (m.end() - m.start()) + masked[m.end():]
    if best is None:
        return None
    return direction, response_mod.FACTOR_WORDS[best[1]], best[0], conn


def is_response_query(text: str) -> bool:
    """요인이 오르면(내리면) 자산이 어떻게 되는지 묻는 말: 요인 낱말 + 오르면/내리면 같은 연결어 + (자산 이름이나 '어떻게·영향' 같은 반응 말). 내 보유 종목을 묻는 말은 제외."""
    compact = _compact(text)
    if any(w in compact for w in RESPONSE_OWN):
        return False
    parts = _response_parts(text)
    if parts is None:
        return False
    return any(w in compact for w in RESPONSE_REACTION) or bool(parse_response_query(text)["assets"])


def parse_response_query(text: str) -> dict:
    """질문 문장 → 조건부 반응 도구 인자 {factor, direction, assets, horizon_days}. 자산을 말하지 않으면 비운다(기본 자산 묶음)."""
    parts = _response_parts(text)
    raw = str(text or "")
    factor_name, direction = "", "up"
    spans = []
    if parts:
        direction, (symbol, label, _kind), _end, _conn = parts
        factor_name = symbol
    compact = _compact(raw)
    words = [w for w in raw.replace("?", " ").replace(",", " ").split()]
    assets, seen = [], set()
    factor_word = next((w for w in sorted(response_mod.FACTOR_WORDS, key=len, reverse=True) if parts and response_mod.FACTOR_WORDS[w] == parts[1] and w in compact), "")
    masked_tokens = []
    for token in words:                     # 조건을 거는 요인 낱말이 든 토큰("금리가")은 자산 후보에서 뺀다
        stripped = _compact(token)
        masked_tokens.append(" " if factor_word and factor_word in stripped and not any(a in stripped for a in response_mod.ASSET_WORDS if a != factor_word) else token)
    joined = " ".join(masked_tokens)
    candidates = [t for t in masked_tokens if t.strip()] + [joined]
    for token in candidates:
        found = response_mod.resolve_asset(token)
        if found and found[0] not in seen and not (parts and found[0] == parts[1][0]):
            seen.add(found[0])
            assets.append(found[1])
    horizon = 5 if any(w in compact for w in ("일주일", "한주", "주간", "1주")) else 63 if any(w in compact for w in ("3개월", "석달", "분기")) else 21
    return {"factor": factor_name, "direction": direction, "assets": assets[:response_mod.MAX_ASSETS], "horizon_days": horizon}


# 밸류에이션 질문: "AAPL 고평가야?", "삼성전자 PER 알려줘", "내 종목 저평가인 게 있어?" — PER·PSR·PBR·EPS·BPS·ROE와 비싼가·싼가
VALUATION_WORDS = ("고평가", "저평가", "과대평가", "과소평가", "밸류에이션", "밸류", "적정가", "적정주가", "비싸", "비싼", "싸다", "싼가", "싸냐", "저렴", "거품", "overvalue", "undervalue",
                   "주가수익비율", "주가순자산비율", "주가매출비율", "주당순이익", "주당순자산", "자기자본이익률")
_VALUATION_METRIC = re.compile(r"(?<![a-z])(per|pbr|psr|eps|bps|roe)(?![a-z])")


def is_valuation_query(text: str) -> bool:
    """고평가·저평가·밸류에이션 같은 말이나 PER·PBR·PSR·EPS·BPS·ROE가 나오는 질문. 이번 주 요인·고점 대비·업종 ETF 현황 질문은 앞에서 따로 걸러진다."""
    compact = _compact(text)
    return any(w in compact for w in VALUATION_WORDS) or bool(_VALUATION_METRIC.search(str(text or "").lower()))


def plan_agent_query(question: str) -> dict:
    """짧은 한국어 질문을 허용된 읽기 도구 호출 계획으로 바꾼다(단어 일치 방식, 공백·유의어 허용)."""
    text = str(question or "").strip()
    if not text:
        raise ValueError("질문을 입력해 주세요.")
    compact = _compact(text)
    if is_valuation_query(text) and not is_trend_query(text) and not is_screen_query(text):   # 비싼가·싼가·PER 같은 밸류에이션 질문(보유·관심 종목)
        return {"supported": True, "intents": ["valuation"], "tools": ["get_valuation"]}
    if is_response_query(text):   # "금리가 오르면 금과 나스닥은?" — 요인이 움직일 때 자산의 평소 반응(보유 종목과 무관한 시장 질문)
        return {"supported": True, "intents": ["response"], "tools": ["get_factor_response"]}
    if is_screen_query(text):     # 여러 종목을 훑는 이동평균 질문은 이 도구 하나만(다른 도구의 단어와 겹쳐도 붙이지 않는다)
        return {"supported": True, "intents": ["screen"], "tools": ["get_ma_screen"]}
    if is_trend_query(text):      # 미국 업종 ETF의 상태·잘나가는 순·추세·전환 질문도 이 도구 하나만
        return {"supported": True, "intents": ["trend"], "tools": ["get_sector_trends"]}
    if is_drawdown_query(text) and not any(w in compact for w in ("앞으로", "전망", "오를까", "내릴까", "과거에", "리먼", "2008")):   # 앞날·과거 사례를 묻는 말은 해당 도구로
        return {"supported": True, "intents": ["drawdown"], "tools": ["get_drawdown_analysis"]}
    hit = {name: any(w in compact for w in words) for name, words in INTENT_WORDS.items()}
    if not hit["cases"]:   # 사례집에 있는 연도(2008·2020 등)가 말에 나오면 그 사례를 묻는 것으로 본다
        hit["cases"] = any(y in compact for y in CASE_YEARS)
    if not hit["cases"] and any(w in compact for w in SIMILAR_WORDS) and any(w in compact for w in NOW_WORDS):
        hit["cases"] = True        # "현재 시장상황이 어느 시점이랑 비슷해 보여?"처럼 지금을 과거와 견주는 말
    if hit["cases"] and not any(w in compact for w in ("쇼크", "충격", "같은변동", "큰변동", "이슈")):
        hit["history"] = False     # 사례를 묻는 말에는 사례집이 답한다(과거 충격 통계 도구는 '큰 변동·쇼크'를 직접 물을 때만)
    if hit["drivers"] and not any(w in compact for w in WHY_STRICT + ("이번주", "이번달", "이번주에")):
        # "금이 가장 영향을 많이 받는 요소는?"은 평소의 민감도 순위를 묻는 것: 이번 주 요인 분해·뉴스·ETF 도구는 붙이지 않는다('영향'·'요인' 단어가 겹쳐서 붙던 것)
        for name in ("macro", "news", "etf", "history"):
            hit[name] = False
    if hit["history"] and not any(w in compact for w in WHY_STRICT):
        # 과거 질문("금리 급등 때 어땠어")의 '급등'은 이유를 묻는 게 아니다: 움직임 단어만으로는 요인·뉴스·ETF 도구를 붙이지 않는다
        for name in ("macro", "news", "etf"):
            others = tuple(w for w in INTENT_WORDS[name] if w not in MOVE_WORDS)
            hit[name] = any(w in compact for w in others)
    tools, intents = [], []
    if hit["tax"]:
        tools.append("estimate_liquidation_tax")
        intents.append("tax")
    if hit["weekly_review"]:
        tools.extend(("get_portfolio_overview", "get_weekly_risk_report"))
        intents.append("weekly_review")
    if hit["portfolio"]:
        tools.append("get_portfolio_overview")
        intents.append("rebalancing" if "리밸런싱" in compact or "비중맞" in compact else "portfolio")
    for name, tool in (("outlook", "get_outlook"), ("macro", "get_macro_drivers"), ("news", "get_weekly_news"),
                       ("etf", "get_etf_breakdown"), ("history", "get_macro_history"), ("cases", "get_macro_cases"), ("drivers", "get_asset_drivers"),
                       ("sector", "get_sector_exposure")):
        if hit[name]:
            tools.append(tool)
            intents.append(name)
    tools = list(dict.fromkeys(tools))
    return {"supported": bool(tools), "intents": list(dict.fromkeys(intents)), "tools": tools}


def plan_for_holding() -> dict:
    """키워드는 없지만 질문이 내 종목을 가리킬 때: 그 종목의 이번 주 움직임·요인·보도·앞으로의 뷰(ETF면 구성 종목)를 한꺼번에."""
    return {"supported": True, "intents": ["holding"], "tools": list(ENTITY_TOOLS)}


# 의도 → 부를 도구와 화면에 보일 이름. 문장 유사도 매칭(intent_matcher)이 고른 의도와 사용자가 가르친 의도를 도구 호출 계획으로 바꾼다.
INTENT_PLANS = {
    "weekly": ["get_portfolio_overview", "get_weekly_risk_report", "get_macro_drivers", "get_weekly_news"],
    "rebalancing": ["get_portfolio_overview"],
    "tax": ["estimate_liquidation_tax"],
    "sector": ["get_sector_exposure"],
    "news": ["get_weekly_news"],
    "why": ["get_macro_drivers", "get_weekly_news", "get_etf_breakdown"],
    "outlook": ["get_outlook"],
    "etf": ["get_etf_breakdown"],
    "history": ["get_macro_history"],
    "cases": ["get_macro_cases"],
    "drivers": ["get_asset_drivers"],
    "screen": ["get_ma_screen"],
    "trend": ["get_sector_trends"],
    "drawdown": ["get_drawdown_analysis"],
    "response": ["get_factor_response"],
    "valuation": ["get_valuation"],
    "holding": list(ENTITY_TOOLS),
}
INTENT_LABELS = {
    "weekly": "이번 주 점검", "rebalancing": "리밸런싱 계획", "tax": "세금 추정", "sector": "섹터 노출", "news": "종목 뉴스",
    "why": "왜 올랐나·내렸나", "outlook": "앞으로의 뷰", "etf": "ETF 구성 종목", "history": "과거 충격 때 반응",
    "cases": "지금과 닮은 과거(사례집)", "drivers": "가장 영향받는 요인", "screen": "이동평균 스크리너", "trend": "미국 업종 ETF 상태", "drawdown": "고점 대비 낙폭 원인", "response": "요인이 움직일 때 자산 반응", "valuation": "밸류에이션(고평가·저평가)", "holding": "종목 종합",
}
TEACHABLE_INTENTS = [k for k in INTENT_PLANS if k != "holding"]   # 종목 종합은 종목 이름이 있어야 하므로 가르치는 선택지에서 뺀다


def plan_for_intent(intent: str) -> dict:
    """의도 이름 → 도구 호출 계획(plan_agent_query와 같은 모양). 모르는 의도는 지원하지 않는 계획."""
    tools = INTENT_PLANS.get(intent)
    return {"supported": bool(tools), "intents": [intent] if tools else [], "tools": list(tools or [])}


# 키워드로 계획한 질문의 의도 집합 → 가르칠 수 있는 질문 종류. 한 가지로 정해지는 경우만 자동 학습에 쓴다.
PLAN_INTENT_NAMES = {
    frozenset({"tax"}): "tax", frozenset({"weekly_review"}): "weekly", frozenset({"weekly_review", "macro", "news"}): "weekly",
    frozenset({"rebalancing"}): "rebalancing", frozenset({"portfolio"}): "rebalancing", frozenset({"sector"}): "sector", frozenset({"news"}): "news",
    frozenset({"outlook"}): "outlook", frozenset({"etf"}): "etf", frozenset({"history"}): "history", frozenset({"cases"}): "cases",
    frozenset({"macro", "news", "etf"}): "why", frozenset({"macro"}): "why", frozenset({"drivers"}): "drivers", frozenset({"screen"}): "screen", frozenset({"trend"}): "trend", frozenset({"drawdown"}): "drawdown", frozenset({"response"}): "response", frozenset({"valuation"}): "valuation",
}


def plan_intent_name(plan: dict) -> str | None:
    """키워드 계획이 가리키는 질문 종류(한 가지로 정해질 때만). 종목 종합·여러 의도가 섞인 경우는 None."""
    return PLAN_INTENT_NAMES.get(frozenset(plan.get("intents") or []))


def suggest_questions(names: list[str], limit: int = 6) -> list[str]:
    """못 알아들은 질문 뒤에 보여 줄 질문 예시(내 종목 이름 포함). 이름은 길이를 제한하고 중복을 없앤다."""
    out = ["이번 주 무엇을 확인해야 해?"]
    for name in list(dict.fromkeys(str(n).strip()[:30] for n in names if str(n).strip()))[:2]:
        out += [f"{name} 앞으로의 상승/하락 뷰", f"{name} 왜 움직였어?"]
    out += ["리밸런싱 계획 보여줘", "전부 팔면 세금은?"]
    return out[:limit]


def _won(value) -> str:
    return f"{float(value or 0):,.0f}원"


def _pct(value) -> str:
    return f"{float(value or 0):,.2f}%"


def _entry_text(allocations: list[dict]) -> str:
    """" (SPYM 진입 25.00% 0.75주 + …)" — 지금 살 금액의 주수. 지금 살 게 없으면 진입 비율만."""
    parts = []
    for a in allocations:
        if a.get("signal_status") != "ok":
            now = a.get("shares_now")
            parts.append(f"{a.get('symbol')} 전략 미적용" + (f" {now:,.2f}주" if now else ""))
            continue
        head = f"{a.get('symbol')} 진입 {_pct(a.get('entry_pct'))}"
        now_krw, shares = a.get("buy_now_krw") or 0, a.get("shares_now")
        if now_krw <= 0:
            parts.append(head)
        elif shares is None:
            parts.append(f"{head} 주수 확인 불가")
        elif a.get("currency") == "KRW":
            parts.append(f"{head} {shares:,.0f}주")
        else:
            parts.append(f"{head} {shares:,.2f}주")
    return " (" + " + ".join(parts) + ")" if parts else ""


def _num(value) -> str:
    return f"{float(value or 0):,.0f}"


def _qty(shares, currency: str | None) -> str:
    """주수 표기. 국내(KRW)는 정수 주, 해외는 소수 둘째 자리. 모르면 '주수 확인 불가'."""
    if shares is None:
        return "주수 확인 불가"
    return f"{shares:,.0f}주" if currency == "KRW" else f"{shares:,.2f}주"


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


def _short_pct(value) -> str:
    return f"{float(value):.1f}".rstrip("0").rstrip(".") + "%"


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


SKEW_TEXT = {"up": "과거 기준 상승 우세", "down": "과거 기준 하락 우세", "mixed": "과거 기준 혼조", "insufficient": "표본 부족 — 판단하지 않음"}


def _scenario_line(s: dict) -> str:
    return f"{s['label']} {s['phrase']}({s['move']}) → 약 {s['impact_pct']:+.1f}%"


def _compose_outlook(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """종목별 앞으로의 상승/하락 뷰 표. 상승·하락 시나리오를 나란히, 과거 통계는 표본 수와 함께, 근거가 모자라면 판단하지 않는다."""
    items = data.get("items") or []
    if not items:
        bullets.append("뷰를 만들 보유 종목이나 관심종목이 없습니다.")
        return
    rows = []
    for i in items:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        if not i.get("held"):
            label += " · 관심"
        if i.get("status") != "ok":
            rows.append([label, "시세 기록이 짧아(" + f"{i.get('history_days', 0)}거래일) 계산하지 못했습니다", "-", "-", "-"])
            continue
        state = {"lines": [f"{TREND_TEXT[i['trend']]} · {DEPTH_TEXT[i['depth']]}(고점 대비 {i['drawdown_pct']:.0f}%)",
                           f"최근 1개월 {i['month_change_pct']:+.1f}% · 연 변동성 {i['annual_vol_pct']:.0f}%" if i.get("month_change_pct") is not None
                           else f"연 변동성 {i['annual_vol_pct']:.0f}%"]}
        scen, bands = i["scenarios"], i["bands"]["1M"]
        up = [_scenario_line(s) for s in scen["up"][:2]] + [f"1개월 변동성 범위 상단 {bands['high_pct']:+.1f}%"]
        down = [_scenario_line(s) for s in scen["down"][:2]] + [f"1개월 변동성 범위 하단 {bands['low_pct']:+.1f}%"]
        if not scen["up"] and not scen["down"]:
            note = f"요인 민감도 낮음(R² {scen['r2']:.2f})" if scen.get("r2") is not None else "요인 데이터 없음"
            up.insert(0, note)
        an = i["analogs"]
        f3 = (an.get("forward") or {}).get("3M")
        if f3 and an.get("skew") != "insufficient":
            past = [f"3개월 후 중앙값 {f3['median']:+.1f}% · 상승 비율 {f3['up_share'] * 100:.0f}% (표본 {f3['n']}개, 독립 {an['episodes']}회{' · 국면 완화' if an.get('relaxed') else ''})",
                    f"평소 상승 비율 {(an.get('baseline_up_share_3m') or 0) * 100:.0f}%", SKEW_TEXT[an["skew"]]]
        else:
            n = f3["n"] if f3 else 0
            past = [SKEW_TEXT["insufficient"], f"(유사 시점 {n}개, 독립 {an.get('episodes', 0)}회)"]
        src = an.get("source")
        if src:
            past.append(f"기준: {src['label']} {src['from'][:4]}~{src['to'][:4]}" + (" (월간 근사)" if src["kind"] == "monthly" else " (가격 기준)"))
        elif an.get("source_error"):
            past.append(f"{an['source_error']}를 받지 못해 종목 자신의 시세로 계산했습니다")
        rows.append([label, state, {"lines": up}, {"lines": down}, {"lines": past}])
    tables.append({"title": "앞으로의 상승/하락 뷰 (조건부 시나리오 · 예측 아님)",
                   "columns": ["종목", "현재 상태", "상승 시나리오", "하락 시나리오", "과거 유사 상황 (3개월 후)"],
                   "align": ["l", "l", "l", "l", "l"], "wrap": [False, True, True, True, True], "rows": rows, "footer": []})
    bullets.append("앞으로의 뷰: 과거 통계·요인 민감도·변동성으로 만든 조건부 시나리오이며 예측이나 투자 조언이 아닙니다.")
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    elif data.get("focus"):
        notes.append("질문에서 특정 종목을 찾지 못해 보유·관심 종목 전체를 보여줍니다.")
    if data.get("omitted"):
        notes.append(f"그 밖의 {int(data['omitted'])}개 종목은 표에 넣지 않았습니다(최대 12개).")
    limitations.extend(data.get("limitations", []))


MACRO_TABLE_ROWS = 12
MACRO_FACTOR_ROWS = 12   # 이번 주 시장 요인 표에 보이는 최대 개수(요인이 40개를 넘어 많이 움직인 것만)
MACRO_WEAK_MESSAGE = "시장·매크로 요인으로 거의 설명되지 않음 (종목 고유 이슈일 수 있어 뉴스를 확인하세요)"
MACRO_UNSTABLE_MESSAGE = "최근 1년 안에서는 요인과 같이 움직였지만, 뒤 구간 검증에서는 맞지 않아 믿기 어렵습니다 (관계가 바뀌었을 수 있어 뉴스를 확인하세요)"


def _factor_text(value, unit: str, phrase: str = "") -> str:
    rounded = round(value) if unit == "bp" else round(value, 1)
    rounded = rounded + 0.0 if rounded else 0.0  # -0.0이 "-0.0%"로 보이지 않게
    text = f"{rounded:+.0f}bp" if unit == "bp" else f"{rounded:+.1f}%"
    return f"{text} {phrase}".strip()


def _r2_text(item: dict) -> str:
    """설명력 칸: 표본 안 R²와 검증 구간 설명력(앞 2/3로 구한 계수가 뒤 1/3을 맞힌 정도)."""
    if item.get("r2") is None:
        return "-"
    return f"{item['r2']:.2f}" + (f" (검증 {item['oos_r2']:.2f})" if item.get("oos_r2") is not None else "")


def _compose_macro(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """이번 주 시장 요인 표와, 종목별 요인 기여 표(통계 추정). 설명력이 낮으면 그렇다고 표시한다."""
    factors = [f for f in data.get("factors", []) if f.get("change") is not None]
    if factors:
        # 금리 10bp를 가격 1%와 비슷한 크기로 보고 많이 움직인 요인부터
        ranked = sorted(factors, key=lambda f: abs(f["change"]) / (10 if f["unit"] == "bp" else 1), reverse=True)
        bullets.append("이번 주 시장: " + ", ".join(f"{f['label']} {_factor_text(f['change'], f['unit'])}" for f in ranked[:4]) + ".")
        if len(ranked) > MACRO_FACTOR_ROWS:
            notes.append(f"이번 주 시장 요인은 전체 {len(ranked)}개 중 많이 움직인 10개만 그래프에 보여줍니다.")
    items = data.get("items") or []
    rows = []
    for i in items[:MACRO_TABLE_ROWS]:
        if i.get("status") == "ok" and i.get("drivers"):     # 요인별 기여는 그래프로 보여 준다 — 표에는 설명이 안 되는 종목의 이유만 남긴다
            continue
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        if not i.get("held"):
            label += " · 관심"
        change = f"{i['weekly_change_pct']:+.1f}%" if i.get("weekly_change_pct") is not None else "-"
        if i.get("status") == "no_data":
            cell, r2 = "시세나 요인 데이터가 부족해 계산하지 못했습니다", "-"
        elif i.get("status") == "weak" or not i.get("drivers"):
            cell = MACRO_UNSTABLE_MESSAGE if i.get("reason") == "unstable" else MACRO_WEAK_MESSAGE
            r2 = _r2_text(i)
        else:
            drivers = sorted(i["drivers"], key=lambda d: abs(d.get("contribution_pct") or 0), reverse=True)  # 기여가 큰 요인부터
            shown = [d for d in drivers if abs(d.get("contribution_pct") or 0) >= 0.3] or drivers[:1]
            lines = [f"{d['label']} {_factor_text(d['factor_change'], d['unit'], d['phrase'])} → {d['contribution_pct']:+.1f}%p"
                     + (" (관계 불안정)" if d.get("stable") is False else "") for d in shown]
            rest = (i.get("residual_pct") or 0) + sum(d["contribution_pct"] for d in drivers if d not in shown)
            lines.append(f"그 밖 {rest:+.1f}%p")
            cell, r2 = {"lines": lines}, _r2_text(i)
        rows.append([label, change, cell, r2])
    if rows:
        tables.append({"title": "요인으로 설명하지 못한 종목", "columns": ["종목", "이번 주", "이유", "설명력(R²) · 검증"],
                       "align": ["l", "r", "l", "r"], "wrap": [False, False, True, False], "rows": rows, "footer": []})
    hidden = max(0, len(items) - MACRO_TABLE_ROWS) + int(data.get("omitted") or 0)
    if hidden > 0:
        notes.append(f"그 밖의 {hidden}개 종목은 넣지 않았습니다(많이 움직인 종목부터 표시).")
    if data.get("focused") and items:
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    limitations.extend(data.get("limitations", []))


NEWS_TABLE_ROWS = 12


def _compose_news(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """이번 주 종목별 움직임과 보도 표. 많이 움직인 종목부터, 보도 제목은 링크로(원인으로 단정하지 않는다)."""
    items = data.get("items") or []
    if not items:
        bullets.append("이번 주 뉴스를 조회할 보유 종목이나 관심종목이 없습니다.")
        return
    moved = [i for i in items if i.get("weekly_change_pct") is not None]
    if moved:
        top = sorted(moved, key=lambda i: abs(i["weekly_change_pct"]), reverse=True)[:3]
        bullets.append("이번 주(최근 5거래일) 가장 크게 움직인 종목: "
                       + ", ".join(f"{i.get('name') or i['symbol']} {i['weekly_change_pct']:+.1f}%" for i in top) + ".")
    rows = []
    for i in items[:NEWS_TABLE_ROWS]:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        if not i.get("held"):
            label += " · 관심"
        change = f"{i['weekly_change_pct']:+.1f}%" if i.get("weekly_change_pct") is not None else "-"
        if i.get("headlines"):
            cell = {"links": [{"text": h["title"], "href": h["url"], "meta": " · ".join(x for x in (h.get("source"), h.get("published")) if x)}
                              for h in i["headlines"]]}
        else:
            cell = "뉴스를 불러오지 못했습니다" if i.get("news_error") else "최근 7일 관련 보도를 찾지 못했습니다"
        rows.append([label, change, cell])
    tables.append({"title": "이번 주 종목별 움직임과 보도", "columns": ["종목", "이번 주", "보도 (제목 · 출처 · 날짜)"],
                   "align": ["l", "r", "l"], "wrap": [False, False, True], "rows": rows, "footer": []})
    hidden = len(items) - len(rows) + int(data.get("omitted") or 0)
    if hidden > 0:
        notes.append(f"그 밖의 {hidden}개 종목은 표에 넣지 않았습니다(많이 움직인 종목부터 표시).")
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    limitations.extend(data.get("limitations", []))


HISTORY_VERDICT_TEXT = {"down": "통계적으로 구분됨 → 하락 쪽", "up": "통계적으로 구분됨 → 상승 쪽", "mixed": "평소와 통계적으로 구분되지 않음",
                        "insufficient": "독립 사례 부족 — 판단하지 않음"}
HISTORY_ROWS_PER_ASSET = 4


def _history_sort_key(s: dict):
    """표시 순서: 통계적으로 구분되는 것 먼저(p 작은 순), 그다음 지금 가장 극단적인 변동."""
    significant = s.get("verdict") in ("up", "down")
    return (not significant, s["p_value"] if significant and s.get("p_value") is not None else 0.0, -max(s["percentile"], 1 - s["percentile"]))


def _compose_history(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """지금 큰 변동 중인 요인과 과거 같은 변동 때의 이 종목, 그리고 가장 크게 반응했던 거시 이슈. 같이 움직인 기록이며 원인으로 단정하지 않는다."""
    items = data.get("items") or []
    if not items:
        bullets.append("과거 매크로 반응을 볼 보유 종목이나 관심종목이 없습니다.")
        return
    rows, event_rows, failed = [], [], []
    for i in items:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        if not i.get("held"):
            label += " · 관심"
        if i.get("status") != "ok":
            failed.append(i["symbol"])
            continue
        head = {"lines": [label] + ([f"기준: {i['source']}"] if i.get("source") else []) + [f"{i['history_from'][:4]}년~ 시세"]}
        active = sorted(i.get("active") or [], key=_history_sort_key)
        shown = active[:HISTORY_ROWS_PER_ASSET]
        significant = [s for s in active if s.get("verdict") in ("up", "down")]
        base = i.get("baseline") or {}
        for n, s in enumerate(shown):
            now = {"lines": [f"{s['label']} {s['phrase']} {s['current_change']:+.0f}{s['unit']}",
                             f"(최근 1개월 · 역사 속 {'상위' if s['direction'] == 'up' else '하위'} {(1 - s['percentile'] if s['direction'] == 'up' else s['percentile']) * 100:.0f}%)"]}
            if s.get("median_return") is None:
                past = {"lines": [HISTORY_VERDICT_TEXT["insufficient"], f"(과거 같은 변동 {s['episodes']}회)"]}
            else:
                past = {"lines": [f"중앙값 {s['median_return']:+.1f}% · 하락 {s['neg_share'] * 100:.0f}% (평소 {(base.get('neg_share') or 0) * 100:.0f}%)",
                                  f"독립 사례 {s['episodes']}회 · {HISTORY_VERDICT_TEXT[s['verdict']]}"]}
            examples = {"lines": [f"{e['date'][:7]} {e['label'] + ': ' if e.get('label') else ''}요인 {e['factor_change']:+.0f}{s['unit']} → 이 종목 {e['asset_return']:+.1f}%"
                                  for e in s.get("examples", [])]} if s.get("examples") else "-"
            rows.append([head if n == 0 else "", now, past, examples])
        if not shown:
            rows.append([head, "지금 큰 변동 중인 요인이 없습니다", "-", "-"])
        for s in significant[:2]:
            bullets.append(f"{i['name'] or i['symbol']}: 최근 1개월 {s['label']} {s['phrase']}({s['current_change']:+.0f}{s['unit']}) — 과거 같은 변동 {s['episodes']}회 중 "
                           f"{s['neg_share'] * 100:.0f}%에서 하락, 중앙값 {s['median_return']:+.1f}%(평소 하락 {(base.get('neg_share') or 0) * 100:.0f}%). "
                           "같이 움직인 기록이며 원인은 아닙니다.")
        if not significant and shown:
            bullets.append(f"{i['name'] or i['symbol']}: 지금 큰 변동 중인 요인에 대해 과거 기록으로 뚜렷하게 구분되는 패턴은 없습니다.")
        worst, best = i.get("worst_events") or [], i.get("best_events") or []
        fmt = lambda e: f"{e['name']} {e['return_pct']:+.1f}% (z {e['z']:+.1f})"
        if worst or best:
            event_rows.append([head["lines"][0], {"lines": [fmt(e) for e in worst]} if worst else "-", {"lines": [fmt(e) for e in best]} if best else "-"])
    if rows:
        tables.append({"title": "지금 큰 변동 중인 요인과, 과거 같은 변동 때 이 종목 (같이 움직인 기록 · 원인 증명 아님)",
                       "columns": ["종목", "지금의 큰 변동", "과거 같은 방향의 큰 변동 때", "대표 사례 (요인이 가장 극단적이었던 때)"],
                       "align": ["l", "l", "l", "l"], "wrap": [True, True, True, True], "rows": rows, "footer": []})
    if event_rows:
        tables.append({"title": "이 종목이 가장 크게 반응했던 거시 이슈 (z = 수익률 ÷ 평소 변동성)",
                       "columns": ["종목", "가장 크게 떨어진 이슈", "가장 크게 오른 이슈"], "align": ["l", "l", "l"],
                       "wrap": [False, True, True], "rows": event_rows, "footer": []})
    if failed:
        notes.append("시세 기록이 짧거나 받지 못해 계산하지 못한 종목: " + ", ".join(failed))
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    if int(data.get("omitted") or 0) > 0:
        notes.append(f"그 밖의 {int(data['omitted'])}개 종목은 넣지 않았습니다(최대 6개).")
    limitations.extend(data.get("limitations", []))


CASE_MOVE_ROWS = (("gold", "dxy"), ("tnx", "irx"), ("spx", "vix"), ("oil", "xlf"), ("FRED:DFF", "FRED:DGS2"), ("FRED:DFII10", "FRED:T10YIE"),
                  ("FRED:CPIYOY", "FRED:UNRATE"), ("FRED:WALCL",))
CONVENTION_MARK = {True: "✓ 맞음", False: "✗ 달랐음", None: "· 뚜렷하지 않음"}


def _move_lines(moves: list[dict]) -> list[str]:
    by_key = {m["key"]: m for m in moves}
    lines = []
    for group in CASE_MOVE_ROWS:
        parts = [f"{by_key[k]['label']} {_signed(by_key[k]['value'], 0)}{by_key[k]['unit']}" for k in group if k in by_key]
        if parts:
            lines.append(" · ".join(parts))
    return lines or ["시세 기록이 없는 구간입니다"]


def _compose_cases(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """사례집: 지정한 사례의 국면별 서사·숫자·통념 점검, 그리고 지금과 닮은 과거 국면. 이야기는 해석이고 닮은 국면이 같은 결말을 보장하지 않는다."""
    cases, similar, asset = data.get("cases") or [], data.get("similar") or [], data.get("asset")
    for c in cases:
        bullets.append(f"{c['title']}: {c['summary']} (해석)")
        wrong = [(p, v) for p in c["phases"] for v in p["conventions"] if v["held"] is False]
        for p, v in wrong[:3]:
            bullets.append(f"통념과 달랐던 점 — {p['name']}: '{v['rule']}'는 맞지 않았습니다({v['detail']}).")
        rows = []
        for p in c["phases"]:
            numbers = _move_lines(p["moves"])
            if asset and p.get("asset_move_pct") is not None:
                numbers.append(f"{asset} {_signed(p['asset_move_pct'])}%" + (f" → 그 뒤 3개월 {_signed(p['asset_after_pct'])}%" if p.get("asset_after_pct") is not None else ""))
            checks = [f"{CONVENTION_MARK[v['held']]}: {v['rule']} ({v['detail']})" for v in p["conventions"]] or ["판정할 만큼 큰 변동이 없었습니다"]
            if p.get("gold_after_pct") is not None:
                numbers.append(f"금은 국면이 끝난 뒤 3개월간 {_signed(p['gold_after_pct'])}%")
            rows.append([{"lines": [p["name"], f"{p['start']} ~ {p['end']}"]}, p["story"], {"lines": numbers}, {"lines": checks}])
        tables.append({"title": f"사례: {c['title']} (이야기는 해석 · 숫자는 시세 계산)",
                       "columns": ["국면", "무슨 일 (해석)", "그때 숫자", "통념 점검 (금)"],
                       "align": ["l", "l", "l", "l"], "wrap": [True, True, True, True], "rows": rows, "footer": []})
    if similar:
        rows = []
        for n, s in enumerate(similar, 1):
            diff = [f"{x['label']}: 지금 {_signed(x['now'], 0)}{x['unit']} ↔ 그때 {_signed(x['then'], 0)}{x['unit']}" + (" (비슷)" if x["close"] else "") for x in s["compare"][:5]]
            if s.get("gold_after_pct") is not None:
                diff.append(f"그 국면이 끝난 뒤 3개월간 금 {_signed(s['gold_after_pct'])}%")
            rows.append([{"lines": [f"{n}. {s['case_title']}", f"{s['phase']} ({s['start']} ~ {s['end']})"]},
                         f"{s['distance']:.2f} (낮을수록 닮음 · 요인 {s['shared']}개)", {"lines": diff}])
        tables.append({"title": "지금(최근 6개월)과 요인 움직임이 닮은 과거 국면 (같은 결말을 보장하지 않음)",
                       "columns": ["과거 국면", "닮은 정도", "지금 ↔ 그때 (최근 6개월 vs 그 국면 전체)"],
                       "align": ["l", "l", "l"], "wrap": [True, False, True], "rows": rows, "footer": []})
        top = similar[0]
        bullets.append(f"지금과 요인 움직임이 가장 닮은 과거 국면: {top['case_title']} · {top['phase']}"
                       + (f". 그 국면이 끝난 뒤 3개월간 금은 {_signed(top['gold_after_pct'])}%였습니다" if top.get("gold_after_pct") is not None else "")
                       + ". 닮았다는 것이 같은 결말을 뜻하지는 않으며 예측이 아닙니다.")
    elif not cases:
        notes.append("지금과 닮은 국면을 찾기에는 비교할 요인 시세가 모자랍니다.")
    if not cases and data.get("available"):
        bullets.append("사례집: " + ", ".join(a["title"] for a in data["available"][:12]) + ". 사례 이름이나 연도로 물어보세요(예: 2008 금융위기 때 금은?).")
    if len(cases) >= 3:
        notes.append("사례는 한 번에 3개까지 보여줍니다.")
    if not data.get("has_fred"):
        notes.append("FRED 키를 넣으면 연방기금금리·10년 실질금리·기대인플레이션도 사례 숫자와 통념 점검에 함께 씁니다(Agent 탭 AI 연결 설정).")
    limitations.extend(data.get("limitations", []))


DRIVER_ROWS_PER_ASSET = 3


def _josa(word: str, with_final: str, without_final: str) -> str:
    """받침 유무에 맞는 조사(이/가, 은/는). 마지막 글자가 한글이 아니면(영문 코드 등) 둘 다 쓴다."""
    last = str(word or "")[-1:]
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 else without_final
    return f"{with_final}({without_final})"


def _driver_direction(d: dict, name: str) -> str:
    """상관 부호로 '요인이 오르면 이 종목은 오르는/내리는 경향' 문장."""
    positive = sum(d["corr"].values()) > 0
    return f"{d['label']}{_josa(d['label'], '이', '가')} 오르면 {name}{_josa(name, '은', '는')} " + ("오르는" if positive else "내리는") + " 경향"


def _compose_drivers(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """종목이 평소 가장 민감한 요인 순위. 요인마다 창별 상관·일관성·1표준편차 반응·묶은 요인을 보여 주고 원인으로 단정하지 않는다."""
    items = data.get("items") or []
    if not items:
        bullets.append("요인 순위를 볼 보유 종목이나 관심종목이 없습니다.")
        return
    rows, failed = [], []
    for i in items:
        name = i["name"] or i["symbol"]
        if i.get("status") != "ok" or not (i.get("drivers") or i.get("peers")):
            failed.append(i["symbol"])
            continue
        best = i["drivers"]
        if best:
            bullets.append(f"{name}이(가) 평소 가장 영향받는 매크로 요인: " + ", ".join(f"{d['label']}({'+' if sum(d['corr'].values()) > 0 else '−'})" for d in best) + " — 막대는 일간 상관(+같이·−반대로), 원인은 아닙니다.")
        weakening = [d for d in best if d["consistent"] and len(d["corr"]) >= 2 and abs(list(d["corr"].values())[0]) > 1.5 * abs(list(d["corr"].values())[-1])]
        for d in weakening[:1]:
            first, last = list(d["corr"].items())[0], list(d["corr"].items())[-1]
            bullets.append(f"{d['label']}의 영향은 최근 {first[0]} 상관 {_signed(first[1], 2)}로 {last[0]} 상관 {_signed(last[1], 2)}보다 훨씬 강해 최근에 두드러진 관계일 수 있습니다.")
        if i.get("model"):
            m = i["model"]
            notes.append(f"{i['symbol']}: 최근 1년 회귀 모형은 {', '.join(m['factors'])}로 변동의 {m['r2'] * 100:.0f}%를 설명하고, 뒤 구간 검증에서는 "
                         + (f"{m['oos_r2'] * 100:.0f}%" if m.get("oos_r2") is not None else "검증 불가") + "입니다(나머지는 요인으로 설명되지 않는 종목 고유 움직임).")
        if i.get("others"):
            notes.append(f"{i['symbol']}: 순위 밖에서 상관이 컸던 요인: " + ", ".join(f"{d['label']}({_signed(sum(d['corr'].values()) / len(d['corr']), 2)})" for d in i["others"]) + ".")
    if failed:
        notes.append("시세가 짧거나 받지 못해 계산하지 못한 종목: " + ", ".join(failed))
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    if int(data.get("omitted") or 0) > 0:
        notes.append(f"그 밖의 {int(data['omitted'])}개 종목은 넣지 않았습니다(최대 4개).")
    limitations.extend(data.get("limitations", []))


SCREEN_TABLE_ROWS = 40


def _dist_text(value: float) -> str:
    """MA 대비 %: 0.1% 미만이면 소수 둘째 자리까지(아래인데 '+0.0'으로 보이는 것을 막는다)."""
    return f"{value:+.2f}" if abs(value) < 0.1 else f"{value:+.1f}"


def _compose_screen(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """이동평균 스크리너 결과: 개수·업종 분포·종목 표(가장 깊이 아래인 순). 위치 정보일 뿐 매매 신호가 아니다."""
    periods = data["periods"]
    plist = "·".join(str(p) for p in periods)
    scope = "내 보유·관심 종목" if data["universe"] == "mine" else "S&P500 구성 종목"
    if data.get("sector"):
        scope += f"({data['sector']})"
    side_text = ("아래" if data["side"] == "below" else "위") + "에 있는"
    cond = f"주봉 MA {plist}" + (" 중 하나라도" if data["match"] == "any" else " 모두") + f"보다 {side_text}"
    total, evaluated = data["total_matches"], data["evaluated"]
    if not evaluated:
        bullets.append(f"{scope}에서 주봉 MA {plist}를 계산할 수 있는 종목이 없습니다(시세를 받지 못했거나 주봉이 {max(periods)}주보다 짧음).")
    else:
        share = total / evaluated * 100
        bullets.append(f"{scope} {data['universe_size']}개 중 주봉 {max(periods)}주 이상 시세가 있는 {evaluated}개를 보면, {cond} 종목은 {total}개({share:.0f}%)입니다"
                       + (f" (기준일 {data['as_of']}, 종가 기준)." if data.get("as_of") else "."))
    rows = []
    for m in data["matches"][:SCREEN_TABLE_ROWS]:
        label = f"{m['name']} ({m['symbol']})" if m["name"] and m["name"] != m["symbol"] else m["symbol"]
        rows.append([label, m["sector"] or "-", f"{m['close']:,.2f}", {"lines": [" · ".join(f"MA{x['period']} {_dist_text(x['dist_pct'])}%" for x in m["mas"])]}])
    if rows:
        tables.append({"title": f"{cond} {scope} ({'가장 깊이 아래인 순' if data['side'] == 'below' else '가장 많이 위인 순'} · 종가가 MA보다 몇 % 아래(−)·위(+)인지)",
                       "columns": ["종목", "업종", "종가", "주봉 MA 대비"], "align": ["l", "l", "r", "l"], "wrap": [False, False, False, True], "rows": rows, "footer": []})
    if total > len(rows):
        notes.append(f"해당 종목 {total}개 중 {len(rows)}개만 표에 보여줍니다(가장 깊이 {'아래' if data['side'] == 'below' else '위'}인 순).")
        rest = [s for s in data.get("symbols", []) if s not in {m["symbol"] for m in data["matches"][:len(rows)]}]
        if rest:
            notes.append(f"표에 없는 나머지 해당 종목 {len(rest)}개: " + ", ".join(rest[:150]) + ("…" if len(rest) > 150 else ""))
    if data["insufficient"]:
        notes.append(f"주봉이 {max(periods)}주보다 짧아 판단하지 않은 종목 {data['insufficient']}개(신규 상장 등).")
    if data["failed"]:
        notes.append(f"야후에서 시세를 받지 못해 빠진 종목 {data['failed']}개.")
    limitations.extend(data.get("limitations", []))


HORIZON_NAMES = {5: "1주", 10: "2주", 21: "1개월", 42: "2개월", 63: "3개월", 126: "6개월", 189: "9개월", 252: "12개월", 504: "24개월"}
FOCUS_TEXT = {"short": "단기(최근 흐름)", "mid": "중기", "long": "장기"}
WORSENING_STATES = ("약화 초입", "하락 전환 확인")   # ⚠: 악화 신호
IMPROVING_STATES = ("반등 시도", "상승 전환 초입")    # ↗: 반등·개선 신호


def _horizon_name(days: int) -> str:
    return HORIZON_NAMES.get(days, f"{days}거래일")


def _from_high_text(value) -> str:
    """전 고점(상장 이후 최고 종가) 대비 %: 고점이면 0.0%, 아니면 -9.2%처럼 마이너스로."""
    if value is None:
        return "-"
    return "0.0%" if abs(value) < 0.05 else f"{value:.1f}%"


def _return_text(value) -> str:
    return "-" if value is None else f"{_signed(value)}%"


def _compose_trend(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list | None = None) -> None:
    """미국 업종 ETF(또는 내 종목)를 잘나가는 순으로 한 줄씩: 순위·업종·두 기간 수익률·전 고점 대비·상태. 전환 신호는 ⚠(악화)·↗(반등)로 표시한다."""
    rows, cfg = data.get("rows") or [], data["cfg"]
    scope = "내 보유·관심 종목" if data["universe"] == "mine" else f"미국 업종 ETF {len(rows) + len(data['insufficient'])}개"
    if not rows:
        bullets.append(f"{scope}의 추세를 계산할 만큼의 시세({max(cfg['long_days'], cfg['ma_slow'])}거래일 이상)가 없습니다.")
        return
    focus = data.get("focus") or "all"
    cols = {"all": ("r_mid", "r_long"), "short": ("r_week", "r_short"), "mid": ("r_mid", "r_half"), "long": ("r_long", "r_half")}[focus]
    horizon = {"r_week": "1주", "r_short": _horizon_name(cfg["short_days"]), "r_mid": _horizon_name(cfg["mid_days"]), "r_half": "6개월", "r_long": _horizon_name(cfg["long_days"])}
    order_text = {"all": f"{_horizon_name(cfg['mid_days'])}·6개월·{_horizon_name(cfg['long_days'])} 평균 순위", "short": "단기 수익률", "mid": "중기 수익률", "long": "장기 수익률"}[focus]
    if charts is None:
        bullets.append(f"{scope} — 잘나가는 순 ({order_text}" + (f", {data['as_of']} 종가" if data.get("as_of") else "") + ")")
        bullets.append("⚠ 악화 신호(약화 초입·하락 전환 확인) · ↗ 반등 신호(반등 시도·상승 전환 초입) · 숫자는 전 고점 대비")
    table_rows = []
    for rank, r in enumerate(rows, 1):
        m = r["metrics"]
        mark = " ⚠" if r["state"] in WORSENING_STATES else " ↗" if r["state"] in IMPROVING_STATES else ""
        if mark:   # 신호가 있는 종목만 직전 상태와 확인 여부를 덧붙인다
            since = f"{r['since_weeks']}주째 · " + ("확인" if r["confirmed"] else "미확정")
            detail = f"{r['previous_state']}에서 {since}" if r.get("previous_state") and r["since_weeks"] <= 4 else since
            state_cell = {"lines": [r["state"] + mark, detail]}
        else:
            state_cell = r["state"]
        name = f"{r['name']}({r['symbol']})" if data["universe"] == "sectors" else r["name"]
        table_rows.append([str(rank), name, _return_text(m.get(cols[0])), _return_text(m.get(cols[1])), _from_high_text(m.get("from_high")), state_cell])
    if charts is not None:    # 잘나가는 순 가로 막대: 두 기간 수익률 + 상태·전 고점 대비를 막대 끝 글로
        labels = [f"{r['name']}({r['symbol']})" if data["universe"] == "sectors" else r["name"] for r in rows]
        marks = [" ⚠" if r["state"] in WORSENING_STATES else " ↗" if r["state"] in IMPROVING_STATES else "" for r in rows]
        first = [r["metrics"].get(cols[0]) for r in rows]
        second = [r["metrics"].get(cols[1]) for r in rows]
        charts.append(chart_mod.make("hbar", f"{scope} — 잘나가는 순 ({order_text})", labels,
                                     [{"label": horizon[cols[0]], "color": "s2", "data": first},
                                      {"label": horizon[cols[1]], "colors": chart_mod.signed_colors(second), "data": second,
                                       "text": [("" if v is None else f"{v:+.1f}% · {r['state']}{m}") for v, r, m in zip(second, rows, marks)]}],
                                     "%", subtitle=(f"{data['as_of']} 종가 기준" if data.get("as_of") else ""),
                                     caption="막대 글: " + horizon[cols[1]] + " 수익률 · 상태(⚠악화 ↗반등 신호) · 회색 막대는 " + horizon[cols[0]] + " 수익률",
                                     height=max(200, len(rows) * 44 + 60)))
        for r_ in table_rows:           # 그래프에 있는 두 수익률 열은 표에서 뺀다
            del r_[2:4]
    tables.append({"title": f"{scope} 전 고점 대비와 추세 상태",
                   "columns": ["순위", "종목", "전 고점 대비", "상태"] if charts is not None else ["순위", "종목", horizon[cols[0]], horizon[cols[1]], "전 고점 대비", "상태"],
                   "align": ["r", "l", "r", "l"] if charts is not None else ["r", "l", "r", "r", "r", "l"],
                   "wrap": [False, False, False, True] if charts is not None else [False, False, False, False, False, True], "rows": table_rows, "footer": []})
    notes.insert(0, f"판정 기준: 이동평균({cfg['ma_fast']}일·{cfg['ma_slow']}일) ±{cfg['buffer_pct']:g}% 안은 이탈·회복으로 세지 않고, 같은 상태가 연속 {cfg['confirm_weeks']}주면 확인, 아니면 미확정.")
    present = [s for s in data.get("states", []) if s["state"] in data["groups"]]
    if present:
        notes.append("상태 뜻: " + " / ".join(f"{s['state']}={s['meaning']}" for s in present))
    if data["insufficient"]:
        notes.append("시세가 모자라 판단하지 못한 종목: " + ", ".join(data["insufficient"]))
    limitations.extend(data.get("limitations", []))



def _compose_drawdown(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """종목마다: 최근 고점 대비 낙폭 → 같은 구간 요인 기여(통계 추정) → 그 구간 시장 요인 변화 표. 설명이 안 되면 그렇다고 말하고 뉴스를 보라고 한다."""
    items = data.get("items") or []
    if not items:
        bullets.append("고점 대비를 볼 종목이 없습니다(보유·관심 종목에 없거나 시세를 받지 못했습니다).")
    for it in items:
        name = it["name"] or it["symbol"]
        if it["status"] == "no_data" or it.get("peak_date") is None:
            bullets.append(f"{name}: 시세가 모자라 고점 대비를 계산하지 못했습니다.")
            continue
        if it["status"] == "near_peak":
            bullets.append(f"{name}: 최근 1년 고점({it['peak_date']}) 근처입니다 — 고점 대비 {_from_high_text(it['drawdown_pct'])}.")
            continue
        head = (f"{name}: 최근 고점 {it['peak_date']}({it['peak_close']:,.2f}) 대비 {_from_high_text(it['drawdown_pct'])} "
                f"(고점 이후 {it['since_days']}거래일, 그 사이 최저 {_from_high_text(it['low_drawdown_pct'])})")
        bullets.append(head)
        if it.get("ath_date") and it["ath_date"] != it["peak_date"]:
            bullets.append(f"   상장 이후 전 고점({it['ath_date']}) 대비로는 {_from_high_text(it['ath_drawdown_pct'])}")
        days = it.get("drop_days") or []
        causes = it.get("causes") or []
        risk_days = sum(1 for d in days if d["risk_down"])
        drivers = sorted(it["drivers"], key=lambda d: abs(d.get("contribution_pct") or 0), reverse=True) if it["status"] == "ok" else []
        shown = [d for d in drivers if abs(d.get("contribution_pct") or 0) >= 0.3] or drivers[:1]
        reg_down = [d for d in shown if d["contribution_pct"] < 0]
        reg_up = [d for d in shown if d["contribution_pct"] >= 0]
        rest = (it.get("residual_pct") or 0) + sum(d["contribution_pct"] for d in drivers if d not in shown) if drivers else None
        press_down = [m for m in it.get("pressure") or [] if m["direction"] == "down"]
        reg_labels = {d["label"] for d in reg_down}
        extra_down = [m for m in press_down if m["label"] not in reg_labels][:3]
        wording = "시장 전체 흐름(지수·업종·귀금속 포함)" if it.get("basis") == "market" else "금리·달러 같은 매크로 요인"
        names = [f"{d['label']} {d['phrase']}" for d in reg_down] + [f"{m['label']} {m['phrase']}" for m in extra_down]
        summary = ("통계로는 " + ", ".join(names) + " 쪽이 하락 압력으로 보입니다" if names else "금리·달러 같은 매크로 요인에서는 뚜렷한 하락 압력을 찾지 못했습니다")
        if rest is not None and rest < 0 and abs(rest) > sum(abs(d["contribution_pct"]) for d in reg_down):
            summary += f". 다만 하락폭의 더 큰 부분({rest:+.1f}%p)은 요인으로 설명되지 않아 큰 하락일 보도를 같이 봅니다"
        elif it["status"] != "ok":
            summary += ". 회귀 모형으로는 설명이 안 되거나 검증에서 믿기 어려워(" + ("관계가 바뀌었을 수 있음" if it.get("reason") == "unstable" else f"설명력 R² {_r2_text(it)}") + ") 보도를 같이 봅니다"
        bullets.append("   요약: " + summary + ".")
        lines = []
        ser = it.get("series") or {}
        if len(ser.get("dates") or []) >= 3:   # 고점 이후 가격선: 고점선, 큰 하락일 점(날짜·낙폭·그날 가장 많이 겹친 보도 주제)
            dates = ser["dates"]
            markers = []
            for d in days:
                if d["date"] in dates:
                    theme = next((c["theme"] for c in causes if d["date"] in c["days"]), "")
                    markers.append({"index": dates.index(d["date"]), "text": f"{d['date'][5:]} {d['change_pct']:+.1f}%" + (f" {theme}" if theme else ""), "color": "down"})
            charts.append(chart_mod.make("line", f"{name} 고점 이후 가격", [d[2:] for d in dates], [{"label": name, "color": "s1", "data": ser["closes"]}], "",
                                         subtitle=f"고점 {it['peak_date']} 대비 {_from_high_text(it['drawdown_pct'])} · 빨간 점=크게 빠진 날과 그날 반복된 보도 주제",
                                         markers=markers, hlines=[{"value": it["peak_close"], "text": f"고점 {it['peak_close']:,.2f}", "color": "warn"}]))
        if drivers:    # '왜 빠졌나' 막대: 요인별 기여(%p)와 설명 안 된 부분
            labels = [f"{d['label']} {d['phrase']}" for d in shown] + ["설명 안 된 부분"]
            values = [d["contribution_pct"] for d in shown] + [rest]
            charts.append(chart_mod.make("hbar", f"{name} 하락의 요인별 기여 (통계 추정, %p)", labels,
                                         [{"label": "기여", "colors": chart_mod.signed_colors(values[:-1]) + ["neutral"], "data": values, "text": [f"{v:+.1f}%p" for v in values]}], "%p",
                                         subtitle=f"설명력 R² {_r2_text(it)}" + (" · 시장 전체 흐름 기준" if it.get("basis") == "market" else ""),
                                         caption="같이 움직인 정도의 통계 추정이며 원인의 증명이 아닙니다", height=max(160, len(labels) * 44 + 50)))
        pressure = (it.get("pressure") or [])[:3]
        if pressure:
            lines.append("② 평소 관계: " + ", ".join(f"{m['label']} {_factor_text(m['change'], m['unit'], m['phrase'])}(상관 {m['corr']:+.2f}) → " + ("하락 쪽" if m["direction"] == "down" else "받쳐 줌") for m in pressure))
        if causes:
            lines.append(f"③ 보도 주제(큰 하락일 {len(days)}일 중): " + ", ".join(f"{c['theme']} {len(c['days'])}일" for c in causes))
        if days and risk_days * 2 >= len(days):
            lines.append(f"④ 큰 하락일 {len(days)}일 중 {risk_days}일은 주식·가상자산도 동반 급락 — 시장 전체가 팔릴 때 같이 팔린 패턴과 일치")
        if lines:
            bullets.append("   원인 후보 (확정 아님):")
            bullets.extend("   " + line for line in lines)
        if days:
            rows = []
            for d in days:
                movers = [f"{m['label'].split('(')[0]} {_factor_text(m['change'], m['unit'], m['phrase'])}" for m in d["movers"]]
                news = ({"links": [{"text": h["title"], "href": h["url"], "meta": " · ".join(x for x in (h.get("source"), h.get("published")) if x)} for h in d["headlines"]]}
                        if d["headlines"] else "그날 앞뒤 보도를 찾지 못했습니다")
                rows.append([d["date"], f"{d['change_pct']:+.1f}%", {"lines": movers} if movers else "눈에 띄는 요인 없음", news])
            tables.append({"title": f"{name} 고점 이후 크게 빠진 날 — 그날 시장과 보도 (참고용)",
                           "columns": ["날짜", "종목", "같은 날 크게 움직인 시장", "그날 앞뒤 보도"], "align": ["l", "r", "l", "l"],
                           "wrap": [False, False, True, True], "rows": rows, "footer": []})
    if data.get("omitted"):
        notes.append(f"그 밖의 {data['omitted']}개 종목은 보지 않았습니다.")
    limitations.extend(data.get("limitations", []))


HORIZON_TEXT = {5: "일주일(5거래일)", 21: "한 달(21거래일)", 63: "3개월(63거래일)"}
RESPONSE_VERDICT = {"higher": "평소보다 강함 (통계적으로 구분됨)", "lower": "평소보다 약함 (통계적으로 구분됨)", "mixed": "평소와 뚜렷이 다르지 않음", "insufficient": "구간이 적어 판단하지 않음"}
RESPONSE_MARK = {"higher": " ▲", "lower": " ▼"}
BUCKET_TEXT = {"up": "큰 상승 구간(상위 20%)", "down": "큰 하락 구간(하위 20%)", "flat": "보통 구간(큰 변화 아님)"}
BUCKET_SHORT = {"up": "큰 상승 구간", "down": "큰 하락 구간"}


def _change_text(value, kind: str) -> str:
    return "-" if value is None else (f"{value:+.0f}bp" if kind == "level" else f"{value:+.1f}%")


def _compose_response(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """요인이 크게 오른/내린 구간에서 자산의 평소 대비 반응: 묶음 막대 그래프(큰 하락·평소·큰 상승 구간의 수익률 중앙값) + 자산마다 한 줄 판정."""
    factor, kind = data["factor"]["name"], data["factor"]["kind"]
    th, rows = data.get("thresholds"), data.get("assets") or []
    if not th or not rows:
        bullets.append(f"{factor}의 구간 변화를 구할 만큼 시세가 없거나 자산 시세를 받지 못했습니다.")
        limitations.extend(data.get("limitations", []))
        return
    horizon = HORIZON_TEXT.get(data["horizon"], f"{data['horizon']}거래일")
    asked = data["direction"]
    bullets.append(f"{factor}{_josa(factor, '이', '가')} {horizon} 동안 {_change_text(th['up'], kind)} 이상 오른 때(상위 20%)·{_change_text(th['down'], kind)} 이하로 내린 때(하위 20%)의 자산 반응입니다.")
    for r in rows:
        st = r[asked]
        if st["median"] is None:
            continue
        corr = r["correlation"].get("1년")
        bullets.append(f"{r['name']}: {BUCKET_SHORT[asked]}에서 {RESPONSE_VERDICT[st['verdict']]}" + ("" if corr is None else f" · 일간 상관(1년) {corr:+.2f}"))
    def mark(st):
        return RESPONSE_MARK.get(st["verdict"], "")
    def text(st):
        return "" if st["median"] is None else f"{st['median']:+.1f}%{mark(st)}\n오른 {st['up_share'] * 100:.0f}%"
    sets = [{"label": "큰 하락 구간", "color": "s1", "data": [r["down"]["median"] for r in rows], "text": [text(r["down"]) for r in rows]},
            {"label": "평소", "color": "s2", "data": [r["baseline"]["median"] for r in rows], "text": [f"{r['baseline']['median']:+.1f}%\n오른 {r['baseline']['up_share'] * 100:.0f}%" for r in rows]},
            {"label": "큰 상승 구간", "color": "s3", "data": [r["up"]["median"] for r in rows], "text": [text(r["up"]) for r in rows]}]
    now = ""
    if data.get("current") is not None and data.get("current_bucket"):
        now = f"지금 {_change_text(data['current'], kind)} → {BUCKET_TEXT[data['current_bucket']]}"
    charts.append(chart_mod.make("bar", f"{factor} {horizon}: 큰 하락·평소·큰 상승 구간의 자산 수익률(중앙값)", [r["name"] for r in rows], sets, "%", subtitle=now,
                                 caption="▲▼=평소와 통계적으로 구분됨 · '오른 N%'=수익률이 플러스였던 구간 비율 · 같은 구간에 같이 움직인 기록(원인 증명 아님)"))
    if data.get("insufficient"):
        notes.append("시세가 모자라 뺀 자산: " + ", ".join(data["insufficient"]))
    if data.get("unresolved"):
        notes.append("알 수 없어 뺀 자산 이름: " + ", ".join(data["unresolved"]))
    limitations.extend(data.get("limitations", []))


VALUATION_BAR_CAP = 300.0
VALUATION_BASIS_TEXT = {"sector": "섹터", "market": "시장", "growth": "성장", "profitability": "수익성"}


def _metric_text(value, digits: int = 1, suffix: str = "") -> str:
    return "-" if value is None else f"{value:,.{digits}f}{suffix}"


def _compose_valuation(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list) -> None:
    """종목마다 한 줄 판정 + '기준 대비 얼마나 비싼가' 막대 그래프 + 지표 표(PER·PSR·PBR·EPS·BPS·ROE). 상대 평가이며 적정 주가가 아니다."""
    items = data.get("items") or []
    if not items:
        bullets.append("밸류에이션을 볼 보유 종목이나 관심종목이 없습니다.")
        return
    rows = []
    for it in items:
        name = it["name"] or it["symbol"]
        if it["status"] != "ok":
            notes.append(f"{name}: 야후에서 지표를 받지 못했습니다" + (" (yfinance 미설치)" if it["status"] == "unavailable" else "") + ".")
            continue
        v, m = it["verdict"], it["metrics"]
        kind = {"stock": "개별주", "etf": "ETF", "other": "기타"}[it["kind"]]
        text = VALUATION_LABEL[v["label"]]
        line = f"{name}({kind}): {text}"
        if v["signals"]:
            line += f" — 신호 {v['signals']}개 중 비싼 쪽 {v['expensive']}·싼 쪽 {v['cheap']}"
        bullets.append(line)
        for n in it["notes"]:
            notes.append(f"{name}: {n}.")
        if "loss" in it["flags"] and "적자" not in " ".join(it["notes"]):
            notes.append(f"{name}: 적자(EPS {_metric_text(m['eps'], 2)})라 PER을 계산하지 않았습니다.")
        shown = [x for x in it["signals"]]
        if shown:
            def bar(x):
                if x["premium_pct"] is not None:
                    return x["premium_pct"], f"기준 {x['ref']:.1f} ({x['premium_pct']:+.0f}%)"
                return (x["value"] / 1.5 - 1) * 100, f"PEG {x['value']:.1f} (1↓싸다·2↑비싸다)"
            values = [max(-100.0, min(VALUATION_BAR_CAP, bar(x)[0])) for x in shown]      # 너무 긴 막대가 나머지를 눌러 버리지 않게 자른다(글은 실제 값)
            capped = any(bar(x)[0] > VALUATION_BAR_CAP for x in shown)
            colors = ["warn" if x["verdict"] == "expensive" else "accent" if x["verdict"] == "cheap" else "neutral" for x in shown]
            charts.append(chart_mod.make("hbar", f"{name} — 기준 대비 얼마나 비싼가 (%)", [f"{x['label']} {x['value']:.1f} ({VALUATION_BASIS_TEXT[x['basis']]})" for x in shown],
                                         [{"label": "기준 대비", "colors": colors, "data": values, "text": [bar(x)[1] for x in shown]}], "%",
                                         subtitle=f"{text}" + (f" · 섹터 표본 {it['peer_n']}개" if it["peer_n"] else ""),
                                         caption="주황=비싼 쪽 · 파랑=싼 쪽 · 회색=비슷(±30% 안)" + (f" · 막대는 +{VALUATION_BAR_CAP:.0f}%까지만 그림(글은 실제 값)" if capped else "") + ". 상대 평가이며 적정 주가·투자 조언이 아닙니다",
                                         height=len(shown) * 40 + 70))
        im = it.get("implied")
        if im and im["methods"]:
            sym = {"USD": "$", "KRW": "₩"}.get(im["currency"], "")
            fmt = lambda v: f"{sym}{v:,.0f}" if sym else f"{v:,.0f} {im['currency']}".strip()
            methods = sorted(im["methods"], key=lambda x: -x["price"])
            labels = ["현재가"] + [x["label"] + ("" if x["reliable"] else " (제외)") for x in methods]
            values = [im["price"]] + [x["price"] for x in methods]
            colors = ["accent"] + [("neutral" if not x["reliable"] else "up" if x["diff_pct"] > 0 else "down") for x in methods]
            texts = [fmt(im["price"])] + [f"{fmt(x['price'])} ({x['diff_pct']:+.0f}%)" for x in methods]
            excluded = list(dict.fromkeys(x["reason"] for x in methods if not x["reliable"] and x["reason"]))
            if im["low"] is not None:
                bullets.append(f"   섹터 배수 기준 추정 범위 {fmt(im['low'])}~{fmt(im['high'])} (중앙 {fmt(im['mid'])}, 방법 {im['reliable_n']}개) — 현재가 {fmt(im['price'])}"
                               + (" · 방법 간 차이가 3배를 넘어 참고하기 어렵습니다" if im.get("wide") else ""))
                subtitle = f"추정 범위 {fmt(im['low'])}~{fmt(im['high'])} · 방법 {im['reliable_n']}개" + (" · 방법 간 차이가 커 참고하기 어려움" if im.get("wide") else "")
            else:
                subtitle = "믿을 만한 방법이 2개 미만이라 범위를 내지 않습니다"
            charts.append(chart_mod.make("hbar", f"{name} — 섹터 배수 기준 추정 가격 (현재가 대비 %)", labels,
                                         [{"label": "추정 가격", "colors": colors, "data": values, "text": texts}], "", subtitle=subtitle,
                                         caption="초록=현재가보다 높게 추정 · 빨강=낮게 추정 · 회색=신뢰 낮아 범위에서 제외. 섹터 중앙값 배수를 이 회사에 그대로 적용한 추정이며 적정 주가·투자 조언이 아닙니다"
                                                 + (" · 제외 이유: " + "; ".join(excluded) if excluded else ""), height=len(labels) * 36 + 70, decimals=0, signed=False))
        per = _metric_text(m["per"]) + (f" / 선행 {_metric_text(m['forward_per'])}" if m["forward_per"] is not None else "")
        cur = f" {it['currency']}" if it["currency"] and it["currency"] != "USD" else ""
        rows.append([f"{name} ({it['symbol']})", kind, per, _metric_text(m["psr"]), _metric_text(m["pbr"]), _metric_text(m["eps"], 2) + (cur if m["eps"] is not None else ""),
                     _metric_text(m["bps"], 2) + (cur if m["bps"] is not None else ""), _metric_text(m["roe"], 1, "%"), text])
    if rows:
        tables.append({"title": "밸류에이션 지표 (야후 파이낸스 · PER 후행 / 선행)", "columns": ["종목", "구분", "PER", "PSR", "PBR", "EPS", "BPS", "ROE", "판정"],
                       "align": ["l", "l", "r", "r", "r", "r", "r", "r", "l"], "wrap": [False, False, False, False, False, False, False, False, True], "rows": rows, "footer": []})
    if data.get("focused"):
        notes.append("질문에서 지정한 종목만 보여줍니다: " + ", ".join(i["symbol"] for i in items) + ".")
    if data.get("omitted"):
        notes.append(f"그 밖의 {data['omitted']}개 종목은 넣지 않았습니다(최대 4개).")
    limitations.extend(data.get("limitations", []))


ETF_FLAG_TEXT = {
    "low_coverage": "시세를 받은 상위 편입 종목의 비중이 작아 설명 범위가 좁습니다.",
    "diverged": "상위 편입 종목의 평균 등락과 '그 밖'의 암시 등락이 크게 달라, 움직임이 상위 종목에 쏠렸거나 공시 비중이 오래됐을 수 있습니다(많이 움직인 종목은 지금 비중이 공시보다 작아 기여가 크게 계산될 수 있음).",
    "missing_prices": "일부 편입 종목은 시세를 받지 못해 계산에서 빠졌습니다(상장 전 종목·선물·비상장 등).",
}
ETF_TABLE_ROWS = 6
ETF_SHOWN_ROWS = 6


def _signed(value, digits: int = 1) -> str:
    rounded = round(float(value), digits) + 0.0  # -0.0이 "-0.0"으로 보이지 않게
    return f"{rounded:+.{digits}f}"


def _compose_etf(data: dict, bullets: list, tables: list, notes: list, limitations: list, standalone: bool = True) -> None:
    """ETF 이번 주 등락을 상위 편입 종목 기여로 나눈 표와 요약. 합산 분해이며 원인으로 단정하지 않는다.
    standalone=False(다른 도구와 함께 호출됨)이면 '나눌 수 없음' 안내는 본문이 아니라 메모로만 남긴다."""
    items = data.get("items") or []
    ok = [i for i in items if i.get("status") == "ok"]
    rows = []
    for i in ok[:ETF_TABLE_ROWS]:
        label = f"{i['name']} ({i['symbol']})" if i.get("name") and i["name"] != i["symbol"] else i["symbol"]
        if not i.get("held"):
            label += " · 관심"
        moves = []
        for r in (i.get("rows") or [])[:ETF_SHOWN_ROWS]:
            if r.get("contribution_pct") is None:
                moves.append(f"{r['symbol']} {r['weight_pct']:.1f}% · 시세 없음")
            else:
                moves.append(f"{r['symbol']} {r['weight_pct']:.1f}% · {_signed(r['change_pct'])}% → {_signed(r['contribution_pct'], 2)}%p")
        rest = f"그 밖 {i['rest_weight_pct']:.0f}% → {_signed(i['residual_pct'], 2)}%p"
        if i.get("rest_implied_pct") is not None:
            rest += f" (평균 {_signed(i['rest_implied_pct'])}% 움직인 셈)"
        moves.append(rest)
        factor_lines = []
        for r in [r for r in (i.get("rows") or []) if r.get("contribution_pct") is not None][:3]:
            d = (i.get("drivers") or {}).get(r["symbol"])
            if not d:
                continue
            if d.get("status") == "ok" and d.get("drivers"):
                top = max(d["drivers"], key=lambda x: abs(x.get("contribution_pct") or 0))
                factor_lines.append(f"{r['symbol']}: {top['label']} {_factor_text(top['factor_change'], top['unit'], top['phrase'])}"
                                    f" → {_signed(top['contribution_pct'])}%p (R² {d['r2']:.2f})")
            else:
                factor_lines.append(f"{r['symbol']}: 요인으로 거의 설명되지 않음")
        links = []
        for sym, block in (i.get("headlines") or {}).items():
            for h in block.get("headlines", []):
                links.append({"text": f"[{sym}] {h['title']}", "href": h["url"],
                              "meta": " · ".join(x for x in (h.get("source"), h.get("published")) if x)})
        news_cell = {"links": links} if links else "큰 기여 종목의 최근 7일 보도를 찾지 못했습니다"
        head = {"lines": [label, f"이번 주 {_signed(i['etf_change_pct'])}%", f"상위 {i['priced_weight_pct']:.0f}% 반영"]}
        rows.append([head, {"lines": moves}, {"lines": factor_lines} if factor_lines else "-", news_cell])
        # 요약 문장: ETF가 오른 주면 올린 종목, 내린 주면 내린 종목을 먼저
        sign = 1 if i["etf_change_pct"] >= 0 else -1
        leaders = [r for r in i["rows"] if r.get("contribution_pct") is not None and r["contribution_pct"] * sign > 0][:2]
        if leaders:
            names = ", ".join(f"{r['symbol']}({_signed(r['contribution_pct'], 2)}%p)" for r in leaders)
            bullets.append(f"{i['name'] or i['symbol']} 이번 주 {_signed(i['etf_change_pct'])}%: 상위 편입 종목 중 {names}가 가장 크게 "
                           f"{'끌어올렸' if sign > 0 else '끌어내렸'}습니다(상위 {i['priced_weight_pct']:.0f}% 반영, 합산 분해이며 원인 확정이 아닙니다).")
        else:
            bullets.append(f"{i['name'] or i['symbol']} 이번 주 {_signed(i['etf_change_pct'])}%: 상위 편입 종목의 기여가 뚜렷하지 않습니다"
                           f"(상위 {i['priced_weight_pct']:.0f}% 반영).")
        for flag in i.get("flags", []):
            notes.append(f"{i['symbol']}: {ETF_FLAG_TEXT[flag]}")
    if rows:
        tables.append({"title": "ETF 구성 종목별 기여 (공시 상위 편입 종목 기준 · 비중 · 종목 등락 → 기여)",
                       "columns": ["ETF", "구성 종목 기여", "큰 기여 종목의 요인", "큰 기여 종목 보도"],
                       "align": ["l", "l", "l", "l"], "wrap": [True, True, True, True], "rows": rows, "footer": []})
    failed = [i for i in items if i.get("status") != "ok"]
    if failed:
        notes.append("시세가 부족해 분해하지 못한 ETF: " + ", ".join(i["symbol"] for i in failed))
    missing = data.get("no_holdings") or []
    if not items:
        if data.get("focused") and missing:
            reasons = ", ".join(f"{n['name'] or n['symbol']}({n['reason']})" for n in missing[:5])
            message = f"지정한 종목은 구성 종목으로 나눌 수 없습니다: {reasons}."
        else:
            message = "구성 종목으로 나눌 수 있는 보유·관심 ETF가 없습니다(개별주·금/채권 ETF·한국 ETF는 편입 정보가 없습니다)."
        (bullets if standalone else notes).append(message)
    elif missing:
        notes.append(f"편입 정보가 없어 분해하지 못한 종목 {len(missing)}개(개별주·금/채권 ETF·한국 ETF 등): "
                     + ", ".join(n["symbol"] for n in missing[:8]) + ("…" if len(missing) > 8 else ""))
    if int(data.get("omitted") or 0) > 0:
        notes.append(f"그 밖의 {data['omitted']}개 종목은 이번 분해에 넣지 않았습니다.")
    limitations.extend(data.get("limitations", []))


# ---------------------------------------------------------------------------
# 그래프(charts): 숫자는 도구 결과 그대로, 글은 막대 끝 설명으로 옮긴다. 표는 세부 내용으로 아래에 남는다.
# ---------------------------------------------------------------------------
def _norm_factor(f: dict) -> float:
    return f["change"] / (10 if f["unit"] == "bp" else 1)      # 금리 10bp = 가격 1%로 환산해 막대 길이만 맞춘다


def _charts_macro(data: dict, charts: list) -> None:
    factors = [f for f in data.get("factors", []) if f.get("change") is not None]
    if factors:
        ranked = sorted(factors, key=lambda f: abs(_norm_factor(f)), reverse=True)[:10]
        values = [_norm_factor(f) for f in ranked]
        charts.append(chart_mod.make("hbar", "이번 주 시장 요인 변화 (많이 움직인 순)", [f["label"] for f in ranked],
                                     [{"label": "변화", "colors": chart_mod.signed_colors(values), "data": values, "text": [_factor_text(f["change"], f["unit"], f["phrase"]) for f in ranked]}], "",
                                     caption="막대 길이는 금리 10bp를 1%로 환산해 맞춘 것이고, 글은 실제 변화입니다", height=len(ranked) * 30 + 56))
    for i in (data.get("items") or [])[:3]:
        if i.get("status") != "ok" or not i.get("drivers"):
            continue
        drivers = sorted(i["drivers"], key=lambda d: abs(d.get("contribution_pct") or 0), reverse=True)
        shown = [d for d in drivers if abs(d.get("contribution_pct") or 0) >= 0.3] or drivers[:1]
        rest = (i.get("residual_pct") or 0) + sum(d["contribution_pct"] for d in drivers if d not in shown)
        values = [d["contribution_pct"] for d in shown] + [rest]
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} 이번 주 {i['weekly_change_pct']:+.1f}% — 요인별 기여 (통계 추정, %p)",
                                     [f"{d['label']} {_factor_text(d['factor_change'], d['unit'], d['phrase'])}" + (" (관계 불안정)" if d.get("stable") is False else "") for d in shown] + ["그 밖"],
                                     [{"label": "기여", "colors": chart_mod.signed_colors(values[:-1]) + ["neutral"], "data": values, "text": [f"{v:+.1f}%p" for v in values]}], "%p",
                                     subtitle=f"설명력 R² {_r2_text(i)}", caption="같이 움직인 정도의 통계 추정이며 원인의 증명이 아닙니다", height=len(values) * 40 + 60))


def _charts_drivers(data: dict, charts: list) -> None:
    for i in data.get("items") or []:
        if i.get("status") != "ok" or not (i.get("drivers") or i.get("peers")):
            continue
        entries = [(f"{n}. {d['label']}", d) for n, d in enumerate(i["drivers"], 1)] + [(f"{d['label']} (비슷한 자산)", d) for d in (i.get("peers") or [])]
        windows = list(i.get("windows") or [])
        if not entries or not windows:
            continue
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} — 평소 영향받는 요인 (일간 수익률 상관, +같이 · -반대로)", [label for label, _ in entries],
                                     [{"label": w, "color": f"s{k + 1}", "data": [d["corr"].get(w) for _, d in entries]} for k, w in enumerate(windows[:3])], "",
                                     subtitle=(f"기준: {i['source']}" if i.get("source") else ""), caption="같이 움직인 정도이며 원인의 증명이 아닙니다. 비슷한 자산은 같은 계열이라 같이 움직이는 것입니다",
                                     height=len(entries) * (len(windows) * 16 + 14) + 70, decimals=2))


def _charts_etf(data: dict, charts: list) -> None:
    for i in [x for x in (data.get("items") or []) if x.get("status") == "ok"][:3]:
        rows = [r for r in (i.get("rows") or [])[:ETF_SHOWN_ROWS] if r.get("contribution_pct") is not None]
        if not rows:
            continue
        values = [r["contribution_pct"] for r in rows] + [i.get("residual_pct")]
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} 이번 주 {i['etf_change_pct']:+.1f}% — 구성 종목별 기여 (%p)",
                                     [f"{r['symbol']} {r['weight_pct']:.1f}% ({r['change_pct']:+.1f}%)" for r in rows] + [f"그 밖 {i['rest_weight_pct']:.0f}%"],
                                     [{"label": "기여", "colors": chart_mod.signed_colors(values[:-1]) + ["neutral"], "data": values, "text": [f"{v:+.2f}%p" if v is not None else "" for v in values]}], "%p",
                                     subtitle=f"공시 상위 편입 종목 {i['priced_weight_pct']:.0f}% 반영", caption="비중 × 종목 등락의 합산 분해이며 원인의 증명이 아닙니다", height=len(values) * 38 + 60))


def _charts_screen(data: dict, charts: list) -> None:
    top = [s for s in (data.get("sector_counts") or []) if s["total"]][:8]
    if data.get("universe") != "sp500" or not data.get("total_matches") or not top:
        return
    shares = [s["count"] / s["total"] * 100 for s in top]
    plist = "·".join(str(p) for p in data["periods"])
    charts.append(chart_mod.make("hbar", f"주봉 MA {plist} {'모두' if data['match'] == 'all' else '하나라도'} {'아래' if data['side'] == 'below' else '위'}인 종목의 업종별 비율 (S&P500)",
                                 [s["sector"] for s in top],
                                 [{"label": "업종 안 비율", "color": "s1", "data": shares, "text": [f"{s['count']}/{s['total']}개 ({v:.0f}%)" for s, v in zip(top, shares)]}], "%",
                                 subtitle=f"전체 {data['total_matches']}개 / {data['evaluated']}개" + (f" · 기준일 {data['as_of']}" if data.get("as_of") else ""),
                                 caption="위치 정보일 뿐 매수·매도 신호가 아닙니다", height=len(top) * 34 + 60))


def _charts_outlook(data: dict, charts: list) -> None:
    for i in [x for x in (data.get("items") or []) if x.get("status") == "ok"][:3]:
        scen, band = i["scenarios"], i["bands"]["1M"]
        points = [(f"{s['label']} {s['phrase']}", s["impact_pct"]) for s in scen["up"][:2] + scen["down"][:2]]
        points += [("1개월 변동성 범위 상단", band["high_pct"]), ("1개월 변동성 범위 하단", band["low_pct"])]
        f3 = ((i.get("analogs") or {}).get("forward") or {}).get("3M")
        if f3 and (i.get("analogs") or {}).get("skew") != "insufficient":
            points.append((f"과거 유사 상황 3개월 후 중앙값 (독립 {i['analogs']['episodes']}회)", f3["median"]))
        points.sort(key=lambda p: -(p[1] if p[1] is not None else 0))
        values = [p[1] for p in points]
        charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} 앞으로의 뷰 — 시나리오와 변동성 범위 (%)", [p[0] for p in points],
                                     [{"label": "예상 변화", "colors": chart_mod.signed_colors(values), "data": values}], "%",
                                     subtitle=f"{TREND_TEXT[i['trend']]} · {DEPTH_TEXT[i['depth']]}(고점 대비 {i['drawdown_pct']:.0f}%)",
                                     caption="예측이 아니라 요인 민감도·변동성·과거 통계로 만든 조건부 시나리오입니다(투자 조언 아님)", height=len(points) * 34 + 60))


def _charts_history(data: dict, charts: list) -> None:
    for i in [x for x in (data.get("items") or []) if x.get("status") == "ok"][:3]:
        base = i.get("baseline") or {}
        shown = [s for s in sorted(i.get("active") or [], key=_history_sort_key) if s.get("median_return") is not None][:HISTORY_ROWS_PER_ASSET]
        if shown:
            values = [s["median_return"] for s in shown]
            charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} — 지금 큰 변동 중인 요인과 같은 방향이던 과거의 수익률 (중앙값)",
                                         [f"{s['label']} {s['phrase']} {s['current_change']:+.0f}{s['unit']}" for s in shown],
                                         [{"label": "과거 중앙값", "colors": chart_mod.signed_colors(values), "data": values,
                                           "text": [f"{s['median_return']:+.1f}% · 하락 {s['neg_share'] * 100:.0f}% ({s['episodes']}회)" for s in shown]}], "%",
                                         caption=f"평소 하락 비율 {(base.get('neg_share') or 0) * 100:.0f}% · 같이 움직인 기록이며 원인의 증명이 아닙니다", height=len(shown) * 40 + 60))
        events = [(e, "worst") for e in (i.get("worst_events") or [])] + [(e, "best") for e in (i.get("best_events") or [])]
        if events:
            events.sort(key=lambda p: p[0]["return_pct"])
            values = [e["return_pct"] for e, _ in events]
            charts.append(chart_mod.make("hbar", f"{i['name'] or i['symbol']} — 가장 크게 반응했던 거시 이슈 (수익률)", [e["name"] for e, _ in events],
                                         [{"label": "수익률", "colors": chart_mod.signed_colors(values), "data": values, "text": [f"{e['return_pct']:+.1f}% (z {e['z']:+.1f})" for e, _ in events]}], "%",
                                         caption="z = 수익률 ÷ 평소 변동성", height=len(events) * 34 + 60))


def _charts_cases(data: dict, charts: list) -> None:
    for c in data.get("cases") or []:
        phases = [p for p in c["phases"] if p.get("moves")]
        if not phases:
            continue
        def pick(p, key):
            return next((m["value"] for m in p["moves"] if m["key"] == key), None)
        sets = [{"label": "금", "color": "s3", "data": [pick(p, "gold") for p in phases]}, {"label": "달러", "color": "s1", "data": [pick(p, "dxy") for p in phases]},
                {"label": "S&P500", "color": "s2", "data": [pick(p, "spx") for p in phases]}]
        asset = data.get("asset")
        if asset and any(p.get("asset_move_pct") is not None for p in phases):
            sets.append({"label": str(asset)[:20], "color": "s4", "data": [p.get("asset_move_pct") for p in phases]})
        charts.append(chart_mod.make("bar", f"{c['title']} — 국면별 금·달러·S&P500 변화 (%)", [p["name"] for p in phases], sets, "%",
                                     caption="이야기(해석)는 아래 표, 숫자는 시세로 계산한 값입니다. 닮은 국면이 같은 결말을 보장하지 않습니다", height=300))


def _charts_news(data: dict, charts: list) -> None:
    moved = [i for i in (data.get("items") or []) if i.get("weekly_change_pct") is not None][:12]
    if len(moved) < 2:
        return
    values = [i["weekly_change_pct"] for i in moved]
    charts.append(chart_mod.make("hbar", "이번 주(최근 5거래일) 종목별 등락", [i["name"] or i["symbol"] for i in moved],
                                 [{"label": "등락", "colors": chart_mod.signed_colors(values), "data": values}], "%", height=len(moved) * 30 + 56))


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


def _charts_tax(tax: dict, charts: list) -> None:
    summary = tax.get("summary", {})
    pairs = [("해외 양도세", summary.get("capital_gains_tax_krw")), ("국내 거래세·ETF 세금", summary.get("domestic_transaction_and_etf_tax_krw"))]
    pairs = [(n, v) for n, v in pairs if isinstance(v, (int, float)) and v]
    if len(pairs) >= 1:
        charts.append(chart_mod.make("hbar", f"전량 매도 가정 추정 세금 {_won(summary.get('estimated_tax_krw'))}", [n for n, _ in pairs],
                                     [{"label": "세금", "color": "warn", "data": [v for _, v in pairs], "text": [f"{v:,.0f}원" for _, v in pairs]}], "원",
                                     caption="추정치이며 실제 신고 금액이 아닙니다", height=len(pairs) * 44 + 60))


def _charts_sector(sector: dict, charts: list) -> None:
    rows = sorted((s for s in sector.get("sectors", []) if s.get("exposure_pct")), key=lambda s: s["exposure_pct"], reverse=True)[:8]
    if sector.get("configured") and len(rows) >= 2:
        charts.append(chart_mod.make("hbar", "섹터별 내 노출 비중", [s.get("label") or "" for s in rows],
                                     [{"label": "노출", "color": "s1", "data": [s["exposure_pct"] for s in rows], "text": [f"{s['exposure_pct']:.1f}% · {_won(s.get('exposure_krw'))}" for s in rows]}], "%",
                                     height=len(rows) * 34 + 56))


def compose_agent_answer(results: dict[str, dict]) -> dict:
    """도구의 숫자를 바꾸지 않고 사람이 읽을 수 있는 점검 결과로 조합한다."""
    bullets, limitations, tables, notes, charts = [], [], [], [], []
    portfolio = results.get("get_portfolio_overview")
    if portfolio:
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

    sector = results.get("get_sector_exposure")
    if sector:
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

    outlook_result = results.get("get_outlook")
    if outlook_result:
        _compose_outlook(outlook_result, bullets, tables, notes, limitations)
        _charts_outlook(outlook_result, charts)

    etf_result = results.get("get_etf_breakdown")
    if etf_result:
        _compose_etf(etf_result, bullets, tables, notes, limitations, standalone=set(results) == {"get_etf_breakdown"})
        _charts_etf(etf_result, charts)

    trend_result, screen_result = results.get("get_sector_trends"), results.get("get_ma_screen")
    pair = [("get_sector_trends", trend_result, _compose_trend), ("get_ma_screen", screen_result, _compose_screen)]
    if next(iter(results), None) == "get_ma_screen":    # 먼저 물은 도구의 답이 위에 오도록(질문이 스크리너면 스크리너 먼저, 추세면 추세 먼저)
        pair.reverse()
    for _name, result, compose in pair:
        if result:
            compose(result, bullets, tables, notes, limitations, charts) if compose is _compose_trend else compose(result, bullets, tables, notes, limitations)
            if compose is _compose_screen:
                _charts_screen(result, charts)

    drivers_result = results.get("get_asset_drivers")
    if drivers_result:
        _compose_drivers(drivers_result, bullets, tables, notes, limitations)
        _charts_drivers(drivers_result, charts)

    cases_result = results.get("get_macro_cases")
    if cases_result:
        _compose_cases(cases_result, bullets, tables, notes, limitations)
        _charts_cases(cases_result, charts)

    history_result = results.get("get_macro_history")
    if history_result:
        _compose_history(history_result, bullets, tables, notes, limitations)
        _charts_history(history_result, charts)

    valuation_data = results.get("get_valuation")
    if valuation_data:
        _compose_valuation(valuation_data, bullets, tables, notes, limitations, charts)

    response_data = results.get("get_factor_response")
    if response_data:
        _compose_response(response_data, bullets, tables, notes, limitations, charts)

    drawdown_result_data = results.get("get_drawdown_analysis")
    if drawdown_result_data:
        _compose_drawdown(drawdown_result_data, bullets, tables, notes, limitations, charts)

    macro_result = results.get("get_macro_drivers")
    if macro_result:
        _compose_macro(macro_result, bullets, tables, notes, limitations)
        _charts_macro(macro_result, charts)

    weekly_news_result = results.get("get_weekly_news")
    if weekly_news_result:
        _compose_news(weekly_news_result, bullets, tables, notes, limitations)
        _charts_news(weekly_news_result, charts)

    tax = results.get("estimate_liquidation_tax")
    if tax:
        summary = tax.get("summary", {})
        bullets.append(f"현재 전량 매도 가정 추정 세금은 {_won(summary.get('estimated_tax_krw'))}입니다.")
        bullets.append(
            f"해외 양도세 {_won(summary.get('capital_gains_tax_krw'))}, 국내 거래세·ETF 세금 "
            f"{_won(summary.get('domestic_transaction_and_etf_tax_krw'))}로 계산됐습니다."
        )
        _charts_tax(tax, charts)
        limitations.extend(tax.get("warnings", []))
        limitations.extend(tax.get("limitations", []))

    return {
        "headline": "계좌 데이터를 기준으로 확인한 결과입니다." if bullets or charts else "이 질문은 아직 처리할 수 없습니다.",
        "bullets": bullets or ([] if charts else ["이렇게 물어보세요: \"이번 주 뭘 확인해야 해?\", \"금 앞으로의 상승/하락 뷰 보여줘\", \"리밸런싱 얼마씩 사야 해?\", "
                              "\"지금 다 팔면 세금은?\", \"섹터 노출 보여줘\". 종목 이름만 말해도 앞으로의 뷰를 보여줍니다. "
                              "Anthropic 키를 넣으면 자유로운 문장도 이해합니다."]),
        "tables": tables,
        "charts": [c for c in charts if c],
        "notes": notes,
        "limitations": list(dict.fromkeys(str(x) for x in limitations if x)),
    }
