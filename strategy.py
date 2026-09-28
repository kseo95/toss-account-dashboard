"""분할매수 전략 (지표 자유 조합 + 종목별 전략 배정).

"MA 4단계 하방 돌파 → 회복 시 완성"이라는 real/의 로직을 MA 전용으로 하드코딩하지 않고, 각 단계가
어떤 지표(MA/RSI/등락률)든 쓸 수 있는 "이름 붙은 전략 템플릿" 여러 개로 일반화했다. 종목마다 원하는
템플릿을 배정해서 쓴다(여러 종목이 같은 템플릿을 쓰면 그게 곧 "그룹"). 단계에는 고유 id를 둬서,
나중에 단계를 추가/삭제/순서변경해도 기존 진행 상태(state)가 엉뚱한 단계에 매칭되지 않게 한다.
"""
from __future__ import annotations

import copy
import re
import secrets

import requests

from common import MA_INTERVALS, MA_MAX_PERIOD, SYMBOL_PATTERN, parallel_map, to_float
from storage import UserStore
from toss_api import TOSS_MAX_WORKERS, fetch_close_history, get_prices, lookup_stocks

STRATEGY_TEMPLATES_FILE = "strategy_templates.json"
STRATEGY_STATE_FILE = "strategy_state.json"
STRATEGY_MAX_STAGES = 10
STRATEGY_MAX_TEMPLATES = 20
STRATEGY_INDICATORS = {"MA", "RSI", "CHANGE"}
STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
# 진행 상태. cycle: 몇 차 사이클인지(1부터), base_pct: 이번 사이클 시작 시점의 진입 비율(1차는 0),
# armed_step_ids: 회복 완료 뒤 가격이 위에 있는 걸 확인한 단계 — 이걸 다시 하방 돌파하면 다음 사이클 시작.
EMPTY_STATE = {"cycle": 1, "base_pct": 0.0, "broken_step_ids": [], "recovered": False, "multiplier_triggered": False,
               "armed_step_ids": []}


def _ma(sid: str, period: int, buy_pct: float | None = None) -> dict:
    step = {"id": sid, "indicator": "MA", "params": {"interval": "week", "period": period}}
    if buy_pct is not None:
        step["buy_pct"] = buy_pct
    return step


STRATEGY_DEFAULT_TEMPLATES = {
    "templates": {
        "ma-default": {
            "label": "MA 기본 전략 (real/ 설정값)",
            "assign_mode": "order",  # real/은 "몇 번째 MA가 됐든 터치한 순서"로 비율을 매겼음(지표 고정 아님)
            "steps": [_ma("s1", 60, 5.0), _ma("s2", 120, 10.0), _ma("s3", 200, 15.0), _ma("s4", 240, 20.0)],
            # 회복 판정: steps를 전부 다 돌파한 뒤, 이 중 하나라도 조건이 반대로 뒤집히면(=상방 돌파) recovery_pct 채워 완성.
            "recovery_steps": [_ma("r1", 20), _ma("r2", 60), _ma("r3", 120), _ma("r4", 200), _ma("r5", 240)],
            "recovery_pct": 50.0,
            "multiplier_steps": [],
            "multiplier_factor": 1.0,
            # 2차 이후 사이클(n≥2): 회복으로 100%를 채운 뒤 다시 하방 돌파할 때마다 순번별로 매도+매수(real/ 값).
            # 100 → 65 → 45 → 40 → 50%, 회복 +50%p → 100%로 한 사이클이 끝나고 다음 사이클로 반복.
            "cycle_steps": [{"sell_pct": 40.0, "buy_pct": 5.0}, {"sell_pct": 30.0, "buy_pct": 10.0},
                            {"sell_pct": 20.0, "buy_pct": 15.0}, {"sell_pct": 10.0, "buy_pct": 20.0}],
        }
    },
    "assignments": {},  # {symbol: template_name} - 직접 배정(기본 전략보다 우선)
    "default_template": "ma-default",  # 직접 배정 안 된 보유종목에 자동 적용("" = 자동 적용 안 함)
    "excluded_symbols": [],
    "position_pct": {},  # {symbol: 최종 목표 비중(총자산 대비 %)} - 없으면 금액 계산 안 함
}


# ---------- 검증 ----------
def validate_step(step, allow_buy_pct: bool) -> tuple[dict, str | None]:
    """단계 하나(지표+파라미터, steps면 매수%p 포함)를 검증/정규화한다."""
    if not isinstance(step, dict):
        return {}, "단계 형식이 올바르지 않습니다."
    indicator = step.get("indicator")
    if indicator not in STRATEGY_INDICATORS:
        return {}, f"지원하지 않는 지표입니다: {indicator!r}"
    params = step.get("params")
    if not isinstance(params, dict):
        return {}, "지표 파라미터가 올바르지 않습니다."

    if indicator == "MA":
        interval = params.get("interval")
        if interval not in MA_INTERVALS:
            return {}, "MA 봉 종류는 일봉/주봉이어야 합니다."
        max_period = MA_MAX_PERIOD[interval]
        period = to_float(params.get("period"), -1)
        if not (period.is_integer() and 2 <= period <= max_period):
            return {}, f"MA 기간은 2~{max_period} 사이의 정수여야 합니다."
        clean_params = {"interval": interval, "period": int(period)}
    elif indicator == "RSI":
        period = to_float(params.get("period"), -1)
        if not (period.is_integer() and 2 <= period <= 200):
            return {}, "RSI 기간은 2~200 사이의 정수여야 합니다."
        threshold = to_float(params.get("threshold"), -1)
        if not (0 <= threshold <= 100):
            return {}, "RSI 임계값은 0~100 사이여야 합니다."
        clean_params = {"period": int(period), "threshold": threshold}
    else:  # CHANGE
        threshold_pct = to_float(params.get("threshold_pct"), None)
        if threshold_pct is None or not (-100 <= threshold_pct <= 100):
            return {}, "등락률 기준은 -100~100 사이여야 합니다."
        clean_params = {"threshold_pct": threshold_pct}

    step_id = step.get("id")
    if not (isinstance(step_id, str) and STEP_ID_PATTERN.fullmatch(step_id)):
        step_id = secrets.token_hex(4)
    clean = {"id": step_id, "indicator": indicator, "params": clean_params}
    if allow_buy_pct:
        buy_pct = to_float(step.get("buy_pct"), -1)
        if not (0 < buy_pct <= 100):
            return {}, "매수 비율(%p)은 0보다 크고 100 이하여야 합니다."
        clean["buy_pct"] = buy_pct
    return clean, None


# 단계 목록 종류별 에러 문구: (에러 앞 괄호, 개수 초과 때 주어, 형식 오류 때 이름)
_STEP_LIST_LABELS = {
    "steps": ("", "단계는", "단계"),
    "recovery_steps": ("(회복)", "회복 판정 단계는", "회복 단계"),
    "multiplier_steps": ("(배수 규칙)", "배수 규칙 조건은", "배수 규칙"),
}


def _validate_step_list(raw, name: str, kind: str) -> tuple[list[dict], str | None]:
    """템플릿 안의 단계 목록 하나(매수/회복/배수). 매수 단계만 필수이고 매수%p를 가진다. 중복 id는 새로 만든다."""
    tag, count_label, type_label = _STEP_LIST_LABELS[kind]
    is_buy = kind == "steps"
    if is_buy and (not isinstance(raw, list) or not raw):
        return [], f"'{name}' 템플릿에 단계를 최소 1개 이상 등록하세요."
    if not isinstance(raw, list):
        return [], f"'{name}' 템플릿의 {type_label} 형식이 올바르지 않습니다."
    if len(raw) > STRATEGY_MAX_STAGES:
        return [], f"'{name}' 템플릿의 {count_label} 최대 {STRATEGY_MAX_STAGES}개까지 가능합니다."
    clean, seen = [], set()
    for s in raw:
        clean_s, err = validate_step(s, allow_buy_pct=is_buy)
        if err:
            return [], f"'{name}' 템플릿{tag}: {err}"
        if clean_s["id"] in seen:
            clean_s["id"] = secrets.token_hex(4)
        seen.add(clean_s["id"])
        clean.append(clean_s)
    return clean, None


def _validate_template(name, tpl) -> tuple[dict, str | None]:
    if not isinstance(name, str) or not (1 <= len(name) <= 30):
        return {}, "템플릿 이름은 1~30자여야 합니다."
    if not isinstance(tpl, dict):
        return {}, f"템플릿 형식이 올바르지 않습니다: {name!r}"
    assign_mode = tpl.get("assign_mode", "fixed")
    if assign_mode not in ("fixed", "order"):
        return {}, f"'{name}' 템플릿의 배정 방식이 올바르지 않습니다."

    steps, err = _validate_step_list(tpl.get("steps"), name, "steps")
    if err:
        return {}, err
    recovery, err = _validate_step_list(tpl.get("recovery_steps", []), name, "recovery_steps")
    if err:
        return {}, err
    recovery_pct = to_float(tpl.get("recovery_pct"), 0.0)
    if not (0 <= recovery_pct <= 100):
        return {}, f"'{name}' 템플릿의 회복 매수 비율(%p)은 0~100 사이여야 합니다."
    if recovery and recovery_pct <= 0:
        return {}, f"'{name}' 템플릿: 회복 판정 단계가 있으면 회복 매수 비율도 0보다 커야 합니다."
    step_sum = sum(s["buy_pct"] for s in steps)
    if step_sum + recovery_pct > 100:
        return {}, f"'{name}' 템플릿: 단계별 매수 비율 합계 + 회복 매수 비율이 100%를 넘습니다 (현재 {step_sum + recovery_pct:.1f}%)."

    # 배수 규칙(선택): 회복 판정과 달리 단계를 전부 안 밟아도 먼저 발동할 수 있음 - 한 번이라도
    # 조건이 충족되면(래칫) 그 뒤로는 매수 단계 합계 전체에 배수를 곱해서 계산한다.
    multiplier, err = _validate_step_list(tpl.get("multiplier_steps", []), name, "multiplier_steps")
    if err:
        return {}, err
    multiplier_factor = to_float(tpl.get("multiplier_factor"), 1.0)
    if not (1 <= multiplier_factor <= 10):
        return {}, f"'{name}' 템플릿의 배수는 1~10 사이여야 합니다."
    if multiplier and multiplier_factor <= 1:
        return {}, f"'{name}' 템플릿: 배수 규칙 조건이 있으면 배수는 1보다 커야 합니다."

    cycle_steps, err = _validate_cycle_steps(tpl.get("cycle_steps", []), name, len(steps), bool(recovery))
    if err:
        return {}, err

    return {
        "label": str(tpl.get("label", name)).strip()[:40] or name,
        "assign_mode": assign_mode,
        "steps": steps,
        "recovery_steps": recovery,
        "recovery_pct": recovery_pct,
        "multiplier_steps": multiplier,
        "multiplier_factor": multiplier_factor,
        "cycle_steps": cycle_steps,
    }, None


def _validate_cycle_steps(raw, name: str, step_count: int, has_recovery: bool) -> tuple[list[dict], str | None]:
    """2차 이후 사이클 표: 하방 돌파 순번별 {매도%p, 매수%p}. 비어 있으면 1차 사이클만 쓴다(회복 뒤 그대로 유지)."""
    if not isinstance(raw, list):
        return [], f"'{name}' 템플릿의 2차 이후 사이클 형식이 올바르지 않습니다."
    if not raw:
        return [], None
    if len(raw) != step_count:
        return [], f"'{name}' 템플릿: 2차 이후 사이클 표는 매수 단계 수({step_count}개)와 같아야 합니다."
    if not has_recovery:
        return [], f"'{name}' 템플릿: 2차 이후 사이클을 쓰려면 회복 판정 단계가 있어야 합니다(사이클이 끝나야 다음 사이클로 넘어감)."
    clean, level = [], 100.0
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            return [], f"'{name}' 템플릿의 2차 이후 사이클 형식이 올바르지 않습니다."
        sell, buy = to_float(row.get("sell_pct"), -1), to_float(row.get("buy_pct"), -1)
        if not (0 <= sell <= 100 and 0 <= buy <= 100):
            return [], f"'{name}' 템플릿: 2차 이후 사이클 {i}번째의 매도/매수 비율은 0~100 사이여야 합니다."
        level += buy - sell
        if level < 0:
            return [], f"'{name}' 템플릿: 2차 이후 사이클 {i}번째에서 비율이 0% 아래로 내려갑니다(100%에서 시작 기준)."
        clean.append({"sell_pct": sell, "buy_pct": buy})
    return clean, None


def _clean_symbol(symbol) -> str:
    return symbol.strip().upper() if isinstance(symbol, str) else ""


def validate_strategy_templates(data) -> tuple[dict, str | None]:
    """전략 설정 전체(템플릿 + 종목 배정 + 기본 전략 + 제외 종목 + 목표 비중)를 검증/정규화한다."""
    if not isinstance(data, dict):
        return {}, "잘못된 요청입니다."
    raw_templates = data.get("templates")
    if not isinstance(raw_templates, dict) or not raw_templates:
        return {}, "전략 템플릿을 최소 1개 이상 등록하세요."
    if len(raw_templates) > STRATEGY_MAX_TEMPLATES:
        return {}, f"전략 템플릿은 최대 {STRATEGY_MAX_TEMPLATES}개까지 만들 수 있습니다."
    templates: dict[str, dict] = {}
    for name, tpl in raw_templates.items():
        clean, err = _validate_template(name, tpl)
        if err:
            return {}, err
        templates[name] = clean

    raw_assignments = data.get("assignments", {})
    if not isinstance(raw_assignments, dict):
        return {}, "종목 배정 형식이 올바르지 않습니다."
    assignments: dict[str, str] = {}
    for symbol, tpl_name in raw_assignments.items():
        sym = _clean_symbol(symbol)
        if not sym:
            continue
        if not SYMBOL_PATTERN.fullmatch(sym):
            return {}, f"잘못된 종목 코드입니다: {symbol!r}"
        if tpl_name not in templates:
            return {}, f"'{sym}'에 배정된 템플릿 '{tpl_name}'이 존재하지 않습니다."
        assignments[sym] = tpl_name

    raw_excluded = data.get("excluded_symbols", [])
    if not isinstance(raw_excluded, list):
        return {}, "전략 제외 종목은 배열이어야 합니다."
    excluded: list[str] = []
    for s in raw_excluded:
        if not isinstance(s, str):
            return {}, "전략 제외 종목 코드는 문자열이어야 합니다."
        sym = s.strip().upper()
        if not sym:
            continue
        if not SYMBOL_PATTERN.fullmatch(sym):
            return {}, f"잘못된 종목 코드입니다: {s!r}"
        if sym not in excluded:
            excluded.append(sym)

    # 기본 전략: 키가 없던 예전 파일은 기본 템플릿이 있으면 그걸로(보유종목 전체 자동 적용이 기본값).
    default_template = data.get("default_template", "ma-default" if "ma-default" in templates else "")
    if not isinstance(default_template, str) or (default_template and default_template not in templates):
        return {}, "기본 전략 템플릿이 존재하지 않습니다."

    # 종목별 최종 목표 비중(총자산 대비 %): 진입 목표비율 100%일 때 이만큼 들고 있는 게 목표.
    # 기본 전략으로 자동 배정된 보유종목도 쓸 수 있어서 배정 여부는 안 따진다. 전략 제외 종목 값은 버린다.
    raw_position = data.get("position_pct", {})
    if not isinstance(raw_position, dict):
        return {}, "목표 비중 형식이 올바르지 않습니다."
    position: dict[str, float] = {}
    for symbol, pct in raw_position.items():
        sym = _clean_symbol(symbol)
        if not SYMBOL_PATTERN.fullmatch(sym) or sym in excluded or pct is None or pct == "":
            continue
        value = to_float(pct, -1.0)
        if not (0 < value <= 100):
            return {}, f"'{sym}'의 목표 비중은 0보다 크고 100% 이하여야 합니다."
        position[sym] = value
    if sum(position.values()) > 100:
        return {}, f"종목별 목표 비중 합계가 100%를 넘습니다 (현재 {sum(position.values()):.1f}%)."

    return {"templates": templates, "assignments": assignments, "default_template": default_template,
            "excluded_symbols": excluded, "position_pct": position}, None


# ---------- 저장 ----------
def load_strategy_templates(store: UserStore) -> dict:
    """파일이 없거나 깨졌으면 real/에서 쓰던 기본 템플릿(MA 60/120/200/240주 5/10/15/20%p,
    회복 20/60/120/200/240주 +50%p)."""
    data = store.read_json(STRATEGY_TEMPLATES_FILE)
    clean, error = validate_strategy_templates(data) if data is not None else ({}, "없음")
    return copy.deepcopy(STRATEGY_DEFAULT_TEMPLATES) if error else clean


def save_strategy_templates(store: UserStore, data: dict) -> None:
    store.write_json(STRATEGY_TEMPLATES_FILE, data)


def load_strategy_state(store: UserStore) -> dict[str, dict]:
    """종목별 진행 상태 {symbol: EMPTY_STATE 형식}. 설정 파일과 달리 시간에 따라 쌓이는 기록(래칫).
    사이클 필드가 없는 예전 기록은 1차 사이클로 읽는다."""
    data = store.read_json(STRATEGY_STATE_FILE)
    clean: dict[str, dict] = {}
    for symbol, entry in (data.items() if isinstance(data, dict) else []):
        ids = entry.get("broken_step_ids") if isinstance(entry, dict) else None
        if isinstance(ids, list):
            cycle = entry.get("cycle", 1)
            armed = entry.get("armed_step_ids", [])
            clean[symbol] = {
                "cycle": cycle if isinstance(cycle, int) and cycle >= 1 else 1,
                "base_pct": min(100.0, max(0.0, to_float(entry.get("base_pct"), 0.0))),
                "broken_step_ids": [i for i in ids if isinstance(i, str)],
                "recovered": bool(entry.get("recovered", False)),
                "multiplier_triggered": bool(entry.get("multiplier_triggered", False)),
                "armed_step_ids": [i for i in armed if isinstance(i, str)] if isinstance(armed, list) else [],
            }
    return clean


def save_strategy_state(store: UserStore, state: dict) -> None:
    store.write_json(STRATEGY_STATE_FILE, state)


# ---------- 평가 ----------
def effective_assignments(data: dict, held: set[str], cash_symbols: set[str]) -> tuple[dict[str, str], list[str]]:
    """직접 배정 + (직접 배정 안 된 보유종목 → 기본 전략). 전략 제외·현금 취급 종목은 자동 배정에서 뺀다.
    반환: (적용할 배정 전체, 기본 전략으로 자동 배정된 종목들)."""
    assignments = dict(data["assignments"])
    auto: list[str] = []
    default = data.get("default_template", "")
    if default:
        skip = set(data["excluded_symbols"]) | cash_symbols
        for sym in sorted(held):
            if sym not in assignments and sym not in skip:
                assignments[sym] = default
                auto.append(sym)
    return assignments, auto


def gather_market_data(token: str, symbol: str, steps: list[dict], cur_price: float) -> dict:
    """이 종목의 단계들을 평가하는 데 필요한 캔들 데이터를 한 번에 모은다."""
    def longest(pred, extra=0):
        return max((s["params"]["period"] + extra for s in steps if pred(s)), default=0)

    weeks = longest(lambda s: s["indicator"] == "MA" and s["params"]["interval"] == "week")
    days = max(longest(lambda s: s["indicator"] == "MA" and s["params"]["interval"] == "day"),
               longest(lambda s: s["indicator"] == "RSI", extra=1),
               2 if any(s["indicator"] == "CHANGE" for s in steps) else 0)
    market_data = {"price": cur_price}
    if days or weeks:
        try:
            market_data["daily_closes"], market_data["weekly_closes"] = fetch_close_history(token, symbol, days=days, weeks=weeks)
        except requests.exceptions.RequestException:
            pass
    return market_data


def eval_step(step: dict, market_data: dict) -> dict:
    """step 하나를 지금 시세로 평가한다. triggered=True면 "매수 조건 충족"(MA 아래/RSI 과매도/큰 하락) 상태.
    회복 판정에 재사용할 때는 호출부에서 `not triggered`로 뒤집어서 쓴다(같은 조건의 반대 = 회복)."""
    indicator, params = step["indicator"], step["params"]
    price = market_data.get("price", 0.0)

    if indicator == "MA":
        period = params["period"]
        closes = market_data.get("weekly_closes" if params["interval"] == "week" else "daily_closes", [])
        if len(closes) < period or price <= 0:
            return {"ok": False}
        ma_value = sum(closes[:period]) / period
        diff_pct = (price - ma_value) / ma_value * 100 if ma_value else 0.0
        unit = "주" if params["interval"] == "week" else "일"
        return {"ok": True, "triggered": diff_pct <= 0, "label": f"MA{period}{unit} {diff_pct:+.1f}%"}

    closes = market_data.get("daily_closes", [])
    if indicator == "RSI":
        period = params["period"]
        if len(closes) < period + 1:
            return {"ok": False}
        diffs = [closes[i] - closes[i + 1] for i in range(period)]
        avg_gain = sum(d for d in diffs if d > 0) / period
        avg_loss = sum(-d for d in diffs if d < 0) / period
        rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        return {"ok": True, "triggered": rsi <= params["threshold"], "label": f"RSI({period}) {rsi:.1f}"}

    # CHANGE
    if len(closes) < 2 or price <= 0:
        return {"ok": False}
    prev_close = closes[1]
    change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
    return {"ok": True, "triggered": change_pct <= params["threshold_pct"], "label": f"등락률 {change_pct:+.1f}%"}


def _hit(result: dict) -> bool:
    return bool(result.get("ok") and result["triggered"])


def _entry_pct(template: dict, cycle: int, base: float, broken: set[str], recovered: bool, multiplied: bool) -> float:
    """진행 상태 → 진입 목표비율(0~100).
    1차 사이클: 돌파한 단계의 매수%p 합. 배정 방식 "fixed"는 각 단계 고정 %p(id로 매칭), "order"는 real/처럼
    "지금까지 몇 개가 트리거됐는지"만 보고 앞에서부터 채운다(MA가 꼬여서 어떤 게 먼저 깨지든 "N번째"면 동일).
    2차 이후: 사이클 시작 비율(base)에서 돌파 순번별로 매도%p를 빼고 매수%p를 더한다.
    공통: 회복하면 회복 매수%p를 더한다. 배수 규칙이 발동했으면 매수%p에만 배수를 곱한다."""
    scale = template.get("multiplier_factor", 1.0) if multiplied else 1.0
    steps = template["steps"]
    if cycle == 1:
        filled = steps[:len(broken)] if template.get("assign_mode") == "order" else [s for s in steps if s["id"] in broken]
        value = sum(s["buy_pct"] for s in filled) * scale
    else:
        value = base + sum(r["buy_pct"] * scale - r["sell_pct"] for r in template["cycle_steps"][:len(broken)])
    if recovered:
        value += template["recovery_pct"]
    return max(0.0, min(100.0, value))


def _next_move(template: dict, cycle: int, broken: set[str], recovered: bool, multiplied: bool) -> tuple[float, float]:
    """다음 조건이 충족되면 일어날 (매도%p, 매수%p): 다음 하방 돌파, 단계를 다 깼으면 회복 매수."""
    scale = template.get("multiplier_factor", 1.0) if multiplied else 1.0
    steps = template["steps"]
    if len(broken) < len(steps):
        if cycle == 1:
            pending = steps[len(broken):] if template.get("assign_mode") == "order" else [s for s in steps if s["id"] not in broken]
            return 0.0, pending[0]["buy_pct"] * scale
        row = template["cycle_steps"][len(broken)]
        return row["sell_pct"], row["buy_pct"] * scale
    if not recovered and template["recovery_steps"]:
        return 0.0, template["recovery_pct"]
    return 0.0, 0.0


def evaluate_template(template: dict, prev: dict, market_data: dict) -> tuple[dict, dict]:
    """종목 하나를 템플릿대로 평가한다(네트워크 없음). 반환: (표시용 결과, 새 진행 상태).

    단계(steps)를 하방 돌파(래칫)할 때마다 비율을 바꾸고, 전 단계를 다 돌파한 뒤 회복 단계 중 하나라도 조건이
    반대로 뒤집히면(=상방 돌파) 회복 매수%p를 더해 사이클을 끝낸다. 2차 이후 사이클 표(cycle_steps)가 있으면,
    회복 뒤 위로 올라갔던 단계를 다시 하방 돌파하는 순간 다음 사이클(n+1차)이 시작된다."""
    prev = {**EMPTY_STATE, **prev}
    steps, recovery_steps = template["steps"], template["recovery_steps"]
    multiplier_steps = template.get("multiplier_steps", [])
    cycle, base = prev["cycle"], prev["base_pct"]
    broken, armed = set(prev["broken_step_ids"]), set(prev["armed_step_ids"])
    recovered, multiplied = prev["recovered"], prev["multiplier_triggered"]
    results = {s["id"]: eval_step(s, market_data) for s in steps}

    if recovered and template.get("cycle_steps"):
        # 회복 뒤: 가격이 선 위에 있는 걸 확인한 단계만 "무장"하고, 무장된 단계를 다시 뚫으면 새 사이클.
        # (회복 직후 아직 선 아래 있는 단계는 새로 뚫은 게 아니라서 무장 전까지는 카운트하지 않는다.)
        rebroken = {sid for sid in armed if _hit(results[sid])}
        if rebroken:
            base = _entry_pct(template, cycle, base, broken, recovered, multiplied)
            cycle, broken, armed, recovered, multiplied = cycle + 1, rebroken, set(), False, False
        else:
            armed |= {sid for sid, r in results.items() if r.get("ok") and not r["triggered"]}
    elif not recovered:
        broken |= {sid for sid, r in results.items() if _hit(r)}  # 한 번 돌파되면(래칫) 계속 카운트 유지

    step_results = [{"id": s["id"], "buy_pct": s["buy_pct"], "broken": s["id"] in broken, **results[s["id"]]} for s in steps]

    # 회복 판정은 설정된 단계를 전부 다 돌파한 뒤에만 시작한다(real/과 동일한 게이팅) - 한 단계만
    # 돌파된 상태에서 곧장 회복 보너스가 붙으면 절반만 채운 채로 100%가 돼버리기 때문(Log 10 버그).
    recovery_results = []
    if recovered:
        recovery_results = [{"id": r["id"], "recovered": True, "ok": True} for r in recovery_steps]
    elif len(broken) >= len(steps):
        for rstep in recovery_steps:
            result = eval_step(rstep, market_data)
            is_recovered = bool(result.get("ok") and not result["triggered"])
            recovered = recovered or is_recovered
            recovery_results.append({"id": rstep["id"], "recovered": is_recovered, **result})

    # 배수 규칙: 게이팅 없음(전 단계를 다 안 밟아도 먼저 터질 수 있음). 한 번 충족되면 그 사이클 동안 계속 적용.
    multiplier_results = []
    if multiplied:
        multiplier_results = [{"id": m["id"], "triggered": True, "ok": True} for m in multiplier_steps]
    else:
        for mstep in multiplier_steps:
            result = eval_step(mstep, market_data)
            multiplied = multiplied or _hit(result)
            multiplier_results.append({"id": mstep["id"], **result, "triggered": _hit(result)})

    entry_pct = _entry_pct(template, cycle, base, broken, recovered, multiplied)
    next_sell, next_buy = _next_move(template, cycle, broken, recovered, multiplied)
    next_change = max(0.0, min(100.0, entry_pct - next_sell + next_buy)) - entry_pct
    result = {
        "cycle": cycle,
        "entry_pct": entry_pct,
        "next_sell_pct": next_sell,
        "next_buy_pct": max(0.0, min(next_buy, 100.0 - entry_pct)) if cycle == 1 else next_buy,
        "next_change_pct": next_change,
        "stage_count": len(broken),
        "stage_total": len(steps),
        "steps": step_results,
        "recovered": recovered,
        "recovery_steps": recovery_results,
        "multiplier_triggered": multiplied,
        "multiplier_factor": template.get("multiplier_factor", 1.0),
        "multiplier_steps": multiplier_results,
    }
    new_state = {"cycle": cycle, "base_pct": base, "broken_step_ids": sorted(broken), "recovered": recovered,
                 "multiplier_triggered": multiplied, "armed_step_ids": sorted(armed)}
    return result, new_state


def compute_strategy_rows(token: str, templates: dict, assignments: dict, excluded: set[str], state: dict,
                          symbols: list[str]) -> tuple[list[dict], bool]:
    """배정된 종목마다 분할매수 진행률을 계산하고, 새로 돌파/회복한 게 있으면 state를 갱신한다.
    배정 안 됐거나 전략 제외된 종목은 빠진다. 반환: (rows, state가 바뀌었는지)."""
    targets = [s for s in symbols if s not in excluded and s in assignments]
    if not targets:
        return [], False
    info = lookup_stocks(token, targets)
    prices = get_prices(token, targets)

    def market(symbol):
        t = templates[assignments[symbol]]
        price = prices.get(symbol, 0.0)
        if price <= 0:
            return None
        return gather_market_data(token, symbol, t["steps"] + t["recovery_steps"] + t.get("multiplier_steps", []), price)

    market_by_symbol = dict(zip(targets, parallel_map(market, targets, TOSS_MAX_WORKERS)))

    rows: list[dict] = []
    changed = False
    for symbol in targets:
        template = templates[assignments[symbol]]
        meta = info.get(symbol, {})
        row = {"symbol": symbol, "name": meta.get("name", symbol), "currency": meta.get("currency", "?"),
               "template": assignments[symbol], "template_label": template["label"]}
        market_data = market_by_symbol[symbol]
        if market_data is None:
            rows.append({**row, "ok": False, "reason": "현재가 조회 실패"})
            continue
        prev = {**EMPTY_STATE, **state.get(symbol, {})}
        result, new_entry = evaluate_template(template, prev, market_data)
        if new_entry != {**prev, "broken_step_ids": sorted(prev["broken_step_ids"]), "armed_step_ids": sorted(prev["armed_step_ids"])}:
            state[symbol] = new_entry
            changed = True
        rows.append({**row, "ok": True, "price": market_data["price"], **result})
    return rows, changed


def apply_strategy_sizing(rows: list[dict], position_pct: dict[str, float], holdings: dict,
                          sell_tax_rate: dict[str, float] | None = None) -> None:
    """목표 비중이 정해진 종목 행에 "지금 얼마를 더 사야(팔아야) 하나"를 붙인다(rows를 직접 수정).
    지금 있어야 할 비중 = 최종 목표 비중 × 진입 목표비율. 부족분 = 총자산 × 그 비중 − 현재 평가금액.
    총자산은 보유 대시보드와 같은 분모(보유종목 + 예수금). sell_tax_rate(종목별 판 금액 1원당 세금)가 있으면
    매도 신호에 예상 세금을 붙인다(tax.sell_tax_rate_by_symbol)."""
    total = holdings["totals"]["eval_krw"]
    usd_krw = holdings["usd_krw"]
    held_krw: dict[str, float] = {}
    held_currency: dict[str, str] = {}
    for r in holdings["stocks"] + holdings["cash"]["stocks"]:
        held_krw[r["심볼"]] = held_krw.get(r["심볼"], 0.0) + r["평가금액(원)"]
        held_currency[r["심볼"]] = r["통화"]
    for row in rows:
        pct = position_pct.get(row["symbol"])
        if not row.get("ok") or pct is None or total <= 0:
            continue
        currency = row["currency"] if row["currency"] in ("KRW", "USD") else held_currency.get(row["symbol"], "KRW")
        price_krw = row["price"] * (usd_krw if currency == "USD" else 1.0)
        current_krw = held_krw.get(row["symbol"], 0.0)
        target_now_pct = pct * row["entry_pct"] / 100
        gap_krw = total * target_now_pct / 100 - current_krw
        next_krw = total * pct / 100 * row.get("next_change_pct", 0.0) / 100  # 음수면 매도

        def shares(krw: float) -> int:
            return int(krw // price_krw) if price_krw > 0 else 0

        row["sizing"] = {
            "position_pct": pct,
            "full_krw": total * pct / 100,
            "current_pct": current_krw / total * 100,
            "current_krw": current_krw,
            "target_now_pct": target_now_pct,
            "target_now_krw": total * target_now_pct / 100,
            "buy_now_krw": max(0.0, gap_krw),
            "buy_now_shares": shares(max(0.0, gap_krw)),
            "over_krw": max(0.0, -gap_krw),  # 2차 이후 사이클이면 "지금 팔 금액"
            "over_shares": shares(max(0.0, -gap_krw)),
            "next_change_pct_of_total": pct * row.get("next_change_pct", 0.0) / 100,
            "next_change_krw": next_krw,
            "next_change_shares": shares(abs(next_krw)),
        }
        if sell_tax_rate is not None:
            rate = sell_tax_rate.get(row["symbol"], 0.0)
            row["sizing"]["sell_now_tax_krw"] = max(0.0, -gap_krw) * rate if row.get("cycle", 1) >= 2 else 0.0
            row["sizing"]["next_sell_tax_krw"] = max(0.0, -next_krw) * rate
