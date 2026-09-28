"""보유종목·예수금 계산, 현금 취급 종목, 리밸런싱 목표, 원/달러 비중."""
from __future__ import annotations

from common import SYMBOL_PATTERN, to_float
from storage import UserStore
from toss_api import get_cached_usd_jpy_quote

CASH_SYMBOLS_FILE = "cash_symbols.json"
CASH_SYMBOLS_MAX = 30
REBALANCE_CONFIG_FILE = "rebalance.json"
REBALANCE_REST_LABEL = "나머지"
REBALANCE_CASH_LABEL = "현금"  # 예수금/현금 취급 종목을 합쳐 부르는 이름 (real/과 동일한 관례)
REBALANCE_RESERVED_LABELS = {REBALANCE_REST_LABEL, REBALANCE_CASH_LABEL}
REBALANCE_MAX_CATEGORIES = 30
REBALANCE_MAX_SYMBOLS_PER_CATEGORY = 20
REBALANCE_DEFAULT = {"targets": [], "default_rest_target_pct": 5.0, "krw_target_pct": 50.0}


# ---------- 현금 취급 종목 ----------
def load_cash_symbols(store: UserStore) -> list[str]:
    data = store.read_json(CASH_SYMBOLS_FILE)
    symbols = data.get("symbols", []) if isinstance(data, dict) else []
    return [s for s in symbols if isinstance(s, str) and s]


def save_cash_symbols(store: UserStore, symbols: list[str]) -> None:
    store.write_json(CASH_SYMBOLS_FILE, {"symbols": symbols})


# ---------- 리밸런싱 목표 ----------
def load_rebalance_config(store: UserStore) -> dict:
    """파일이 없거나 깨졌으면 기본값(카테고리 없음, 미분류 종목은 각각 5%, 원/달러 목표 50/50)."""
    data = store.read_json(REBALANCE_CONFIG_FILE)
    if not isinstance(data, dict):
        return {**REBALANCE_DEFAULT, "targets": []}
    clean_targets = []
    for t in data.get("targets", []):
        if not isinstance(t, dict):
            continue
        label = str(t.get("label", "")).strip()
        symbols = [s.strip().upper() for s in t.get("symbols", []) if isinstance(s, str) and s.strip()]
        if label and symbols:
            clean_targets.append({"label": label, "symbols": symbols, "target_pct": to_float(t.get("target_pct"))})
    return {
        "targets": clean_targets,
        "default_rest_target_pct": to_float(data.get("default_rest_target_pct"), REBALANCE_DEFAULT["default_rest_target_pct"]),
        "krw_target_pct": to_float(data.get("krw_target_pct"), REBALANCE_DEFAULT["krw_target_pct"]),
    }


def save_rebalance_config(store: UserStore, targets: list[dict], default_rest_target_pct: float, krw_target_pct: float) -> None:
    store.write_json(REBALANCE_CONFIG_FILE, {"targets": targets, "default_rest_target_pct": default_rest_target_pct,
                                             "krw_target_pct": krw_target_pct})


def validate_rebalance_targets(targets) -> tuple[list[dict], str | None]:
    """카테고리 목록을 검증/정규화한다. (targets, None)이면 통과, (빈 목록, 에러메시지)면 실패."""
    if not isinstance(targets, list):
        return [], "targets는 배열이어야 합니다."
    if len(targets) > REBALANCE_MAX_CATEGORIES:
        return [], f"카테고리는 최대 {REBALANCE_MAX_CATEGORIES}개까지 만들 수 있습니다."

    clean: list[dict] = []
    seen_labels: set[str] = set()
    seen_symbols: dict[str, str] = {}  # symbol -> 그 symbol을 먼저 쓴 카테고리 label (중복 검출용)
    for t in targets:
        if not isinstance(t, dict):
            return [], "카테고리 형식이 올바르지 않습니다."
        label = str(t.get("label", "")).strip()
        if not (1 <= len(label) <= 30):
            return [], "카테고리 이름은 1~30자여야 합니다."
        if label in REBALANCE_RESERVED_LABELS:
            return [], f'"{label}"은(는) 예약된 이름이라 카테고리 이름으로 쓸 수 없습니다.'
        if label in seen_labels:
            return [], f'카테고리 이름이 중복됩니다: "{label}"'
        seen_labels.add(label)

        raw_symbols = t.get("symbols", [])
        if not isinstance(raw_symbols, list) or not raw_symbols:
            return [], f'"{label}" 카테고리에 종목을 하나 이상 넣어야 합니다.'
        if len(raw_symbols) > REBALANCE_MAX_SYMBOLS_PER_CATEGORY:
            return [], f'"{label}" 카테고리에는 종목을 최대 {REBALANCE_MAX_SYMBOLS_PER_CATEGORY}개까지 넣을 수 있습니다.'
        symbols: list[str] = []
        for s in raw_symbols:
            if not isinstance(s, str):
                return [], "종목 코드는 문자열이어야 합니다."
            sym = s.strip().upper()
            if not SYMBOL_PATTERN.match(sym):
                return [], f"잘못된 종목 코드입니다: {s!r}"
            # 같은 카테고리 안의 중복(예: "aaa", "AAA")은 조용히 합치고, 다른 카테고리와 겹칠 때만 에러
            if sym in seen_symbols and seen_symbols[sym] != label:
                return [], f'같은 종목을 두 카테고리에 넣을 수 없습니다: {sym} ("{seen_symbols[sym]}"와(과) "{label}")'
            seen_symbols[sym] = label
            if sym not in symbols:
                symbols.append(sym)

        target_pct = to_float(t.get("target_pct"), -1)
        if not (0 <= target_pct <= 100):
            return [], f'"{label}"의 목표%는 0~100 사이 숫자여야 합니다.'
        clean.append({"label": label, "symbols": symbols, "target_pct": target_pct})
    return clean, None


def compute_rebalance(all_rows: list[dict], cash_krw_total: float, cash_symbols: set[str], total_eval: float,
                      config: dict) -> list[dict]:
    """카테고리별 현재%/목표%. 카테고리에 없는 종목은 각각 개별 항목(목표% = default_rest_target_pct),
    예수금과 현금 취급 종목은 "현금" 하나로 합친다."""
    def pct(amount: float) -> float:
        return amount / total_eval * 100 if total_eval else 0.0

    matched: set[str] = set()
    result = []
    for t in config["targets"]:
        amount = sum(r["평가금액(원)"] for r in all_rows if r["심볼"] in t["symbols"])
        matched.update(t["symbols"])
        result.append({"label": t["label"], "current_pct": pct(amount), "target_pct": t["target_pct"]})

    default_pct = config["default_rest_target_pct"]
    for r in all_rows:
        if r["심볼"] not in matched:
            label = REBALANCE_CASH_LABEL if r["심볼"] in cash_symbols else r["종목"]
            result.append({"label": label, "current_pct": pct(r["평가금액(원)"]), "target_pct": default_pct})
    if cash_krw_total > 0:
        result.append({"label": REBALANCE_CASH_LABEL, "current_pct": pct(cash_krw_total), "target_pct": default_pct})

    merged: dict[str, dict] = {}
    for r in result:
        if r["label"] in merged:
            merged[r["label"]]["current_pct"] += r["current_pct"]
        else:
            merged[r["label"]] = dict(r)
    return list(merged.values())


def compute_currency_split(all_rows: list[dict], cash_krw_amt: float, cash_usd_amt: float, usd_krw: float,
                           total_eval: float, krw_target_pct: float) -> dict:
    """전체 자산 중 원화/달러 통화 비중 (예수금 포함) + 목표 비중. 달러 목표는 100-원화 목표."""
    krw_amount = cash_krw_amt + sum(r["평가금액(원)"] for r in all_rows if r["통화"] == "KRW")
    usd_amount = cash_usd_amt * usd_krw + sum(r["평가금액(원)"] for r in all_rows if r["통화"] == "USD")
    return {
        "krw_pct": (krw_amount / total_eval * 100) if total_eval else 0.0,
        "usd_pct": (usd_amount / total_eval * 100) if total_eval else 0.0,
        "krw_target_pct": krw_target_pct,
        "usd_target_pct": 100.0 - krw_target_pct,
    }


# ---------- 보유종목 ----------
def snapshot_rows(snapshot: dict) -> list[dict]:
    """토스 보유종목 원자료 → 표 행(평가금액 내림차순). 수량 0인 종목은 뺀다."""
    usd_krw = snapshot["usd_krw"]
    rows = []
    for h in snapshot["items"]:
        quantity = to_float(h.get("quantity"))
        if quantity == 0:
            continue
        currency = h.get("currency", "KRW")
        eval_amount = to_float((h.get("marketValue") or {}).get("amount"))
        # amount/rate는 수수료·세금 반영 전 값이라, 실제 손익에 더 가까운 amountAfterCost/rateAfterCost를 쓴다.
        profit_loss = to_float((h.get("profitLoss") or {}).get("amountAfterCost"))
        fx = usd_krw if currency == "USD" else 1.0
        rows.append({
            "심볼": h.get("symbol", ""),
            "종목": h.get("name", h.get("symbol", "?")),
            "시장": h.get("marketCountry", "KR" if currency == "KRW" else "US"),
            "통화": currency,
            "수량": quantity,
            "매입단가": to_float(h.get("averagePurchasePrice")),
            "현재가": to_float(h.get("lastPrice")),
            "평가금액(원)": eval_amount * fx,
            "손익(원)": profit_loss * fx,
            "평가금액(외화)": eval_amount if currency == "USD" else None,
            "손익(외화)": profit_loss if currency == "USD" else None,
            "수익률(%)": to_float((h.get("profitLoss") or {}).get("rateAfterCost")) * 100,
        })
    rows.sort(key=lambda r: r["평가금액(원)"], reverse=True)
    return rows


def build_holdings(snapshot: dict, store: UserStore) -> dict:
    """계좌 원자료 + 사용자 설정 → 보유 화면 데이터(종목/현금/총계/리밸런싱/통화 비중).
    real/portfolio.py의 fetch_rows/totals_of와 같은 방식. 분모는 보유종목 + 예수금."""
    usd_krw = snapshot["usd_krw"]
    cash_krw_amt, cash_usd_amt = snapshot["cash_krw"], snapshot["cash_usd"]
    rows = snapshot_rows(snapshot)
    cash_krw_total = cash_krw_amt + cash_usd_amt * usd_krw
    cash_symbols = set(load_cash_symbols(store))
    stock_rows = [r for r in rows if r["심볼"] not in cash_symbols]
    cash_stock_rows = [r for r in rows if r["심볼"] in cash_symbols]

    total_eval = sum(r["평가금액(원)"] for r in rows) + cash_krw_total
    for r in rows:
        r["지분율(%)"] = (r["평가금액(원)"] / total_eval * 100) if total_eval else 0.0
    total_pl = sum(r["손익(원)"] for r in rows)
    total_cost = total_eval - total_pl

    config = load_rebalance_config(store)
    return {
        "stocks": stock_rows,
        "cash": {
            "krw": cash_krw_amt,
            "usd": cash_usd_amt,
            "usd_krw_rate": usd_krw,
            "stocks": cash_stock_rows,
            "total_krw": cash_krw_total + sum(r["평가금액(원)"] for r in cash_stock_rows),
        },
        "totals": {
            "eval_krw": total_eval,
            "profit_loss_krw": total_pl,
            "rate_pct": (total_pl / total_cost * 100) if total_cost else 0.0,
        },
        "rebalance": compute_rebalance(rows, cash_krw_total, cash_symbols, total_eval, config),
        "currency_split": compute_currency_split(rows, cash_krw_amt, cash_usd_amt, usd_krw, total_eval, config["krw_target_pct"]),
        "usd_krw": usd_krw,
        "usd_jpy": get_cached_usd_jpy_quote(),
    }
