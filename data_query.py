"""내 자산 표(주식·예수금·현금 취급 종목 한 줄씩)를 묶고 합치는 범용 조회. 모델은 정해진 항목만 골라 명령을 내리고, 실행은 여기서 한다.
읽기 전용이며 계좌번호·키는 표에 없다. 새 질문마다 도구를 추가하지 않아도 '현금 포함 카테고리별 비중' 같은 계산을 하게 하려는 것."""
from __future__ import annotations

from datetime import date

from common import to_float

TEXT_FIELDS = ("kind", "symbol", "name", "category", "currency", "first_buy_date")
NUM_FIELDS = ("value_krw", "weight_pct", "return_pct", "target_pct", "buy_count", "holding_days", "dividend_krw")
FILL_FIELDS = ("first_buy_date", "buy_count", "holding_days")     # 체결 내역이 필요한 항목(서버가 필요할 때만 가져온다)
DIVIDEND_FIELDS = ("dividend_krw",)                              # 올해 배당 추정이 필요한 항목
GROUPS = ("category", "currency", "kind", "symbol")
METRICS = ("sum", "avg", "count", "max", "min")
OPS = ("=", "!=", ">", ">=", "<", "<=", "포함")
MAX_ROWS = 20
CASH_CATEGORY = "현금"
SCHEMA_TEXT = ("항목: kind(주식/현금/현금취급종목), symbol, name, category(리밸런싱 카테고리·현금·미분류), currency(KRW/USD), "
               "value_krw(평가금액 원), weight_pct(전체 자산 대비 %, 현금 포함), return_pct(수익률 %), target_pct(카테고리 목표 %), "
               "first_buy_date(첫 매수일), buy_count(매수 체결 횟수), holding_days(첫 매수 후 지난 일수), dividend_krw(올해 배당 추정 세전 원)")


def buy_stats(fills: list[dict], today: date) -> dict[str, dict]:
    """체결 내역 → 종목별 첫 매수일·매수 횟수·첫 매수 후 일수. API로 볼 수 있는 주문만이라 예전 매수는 빠질 수 있다."""
    stats: dict[str, dict] = {}
    for f in fills:
        if f.get("side") != "BUY" or not f.get("symbol"):
            continue
        day = str(f.get("filled_at") or "")[:10]
        if len(day) != 10:
            continue
        item = stats.setdefault(f["symbol"], {"first": day, "count": 0})
        item["first"] = min(item["first"], day)
        item["count"] += 1
    return {s: {"first_buy_date": v["first"], "buy_count": float(v["count"]), "holding_days": float((today - date.fromisoformat(v["first"])).days)} for s, v in stats.items()}


def build_assets(holdings: dict, config: dict, fills: list[dict] | None = None, dividends: dict | None = None, today: date | None = None) -> list[dict]:
    """보유 화면 데이터 + 리밸런싱 설정 → 자산 표. 현금(예수금 원화·달러, 현금 취급 종목)은 category가 '현금'이고 weight_pct는 전체 자산 기준이다.
    fills(체결 내역)를 주면 첫 매수일·매수 횟수·보유 일수, dividends(올해 배당 추정 결과)를 주면 종목별 배당을 주식 줄에 채운다(없으면 비어 있다)."""
    stats = buy_stats(fills, today or date.today()) if fills is not None else {}
    divs: dict[str, float] = {}
    for r in (dividends or {}).get("rows", []):
        if r.get("gross_krw") is not None:
            divs[r["symbol"]] = divs.get(r["symbol"], 0.0) + r["gross_krw"]
    total = to_float((holdings.get("totals") or {}).get("eval_krw"))
    category_of, target_of = {}, {}
    for t in config.get("targets", []):
        for sym in t["symbols"]:
            category_of[sym] = t["label"]
            target_of[t["label"]] = t["target_pct"]
    cash_target = config.get("cash_target_pct")

    def weight(value: float) -> float:
        return value / total * 100 if total else 0.0

    rows = []
    for r in holdings.get("stocks", []):
        value = to_float(r.get("평가금액(원)"))
        category = category_of.get(r["심볼"], "미분류")
        rows.append({"kind": "주식", "symbol": r["심볼"], "name": r.get("종목") or r["심볼"], "category": category, "currency": r.get("통화") or "",
                     "value_krw": value, "weight_pct": weight(value), "return_pct": to_float(r.get("수익률(%)")), "target_pct": target_of.get(category),
                     **{k: stats.get(r["심볼"], {}).get(k) for k in FILL_FIELDS}, "dividend_krw": divs.get(r["심볼"]) if dividends is not None else None})
    cash = holdings.get("cash") or {}
    rate = to_float(cash.get("usd_krw_rate"))
    lines = [("원화 예수금", "KRW", to_float(cash.get("krw"))), ("달러 예수금", "USD", to_float(cash.get("usd")) * rate)]
    for r in cash.get("stocks", []):
        lines.append((r.get("종목") or r["심볼"], r.get("통화") or "", to_float(r.get("평가금액(원)"))))
    for i, (name, currency, value) in enumerate(lines):
        rows.append({"kind": "현금취급종목" if i >= 2 else "현금", "symbol": "", "name": name, "category": CASH_CATEGORY, "currency": currency,
                     "value_krw": value, "weight_pct": weight(value), "return_pct": None, "target_pct": cash_target,
                     "first_buy_date": None, "buy_count": None, "holding_days": None, "dividend_krw": None})
    return rows


def _matches(row: dict, field: str, op: str, value: str) -> bool:
    have = row.get(field)
    if field in NUM_FIELDS:
        if have is None:
            return False
        want = to_float(value, float("nan"))
        if want != want:
            return False
        return {"=": have == want, "!=": have != want, ">": have > want, ">=": have >= want, "<": have < want, "<=": have <= want}.get(op, False)
    have = str(have or "")
    return {"=": have == value, "!=": have != value, "포함": value in have}.get(op, False)


def _filters(args: dict) -> tuple[list[tuple[str, str, str]], str | None]:
    """filter_*(첫 조건)·filter2_*(두 번째 조건)를 [(항목, 연산, 값)]으로. 형식이 틀리면 (빈 목록, 이유). 둘 다 만족하는 줄만 남긴다(AND)."""
    found = []
    for prefix in ("filter", "filter2"):
        field = args.get(f"{prefix}_field")
        if not field:
            continue
        op, value = args.get(f"{prefix}_op") or "=", str(args.get(f"{prefix}_value") or "")
        if field not in TEXT_FIELDS + NUM_FIELDS or op not in OPS or not value:
            return [], f"{prefix}_field·{prefix}_op·{prefix}_value 형식이 올바르지 않습니다."
        found.append((field, op, value))
    return found, None


def run_query(rows: list[dict], args: dict) -> dict:
    """args: group_by(+group_by2), metric(기본 sum), field(기본 value_krw), filter_*·filter2_* 조건 두 개(AND), order(desc/asc), limit.
    모르는 항목·연산은 error로 알린다(실행하지 않는다). 합계류는 전체(필터 전) 합계 대비 비율을 함께 준다."""
    args = args if isinstance(args, dict) else {}
    group_by = args.get("group_by") or ""
    group_by2 = args.get("group_by2") or ""
    metric = args.get("metric") or "sum"
    field = args.get("field") or "value_krw"
    if group_by and group_by not in GROUPS:
        return {"error": f"group_by는 {', '.join(GROUPS)} 중 하나여야 합니다."}
    if group_by2 and (group_by2 not in GROUPS or not group_by or group_by2 == group_by):
        return {"error": f"group_by2는 group_by와 다른 {', '.join(GROUPS)} 중 하나여야 하고 group_by가 먼저 있어야 합니다."}
    if metric not in METRICS:
        return {"error": f"metric은 {', '.join(METRICS)} 중 하나여야 합니다."}
    if field not in NUM_FIELDS:
        return {"error": f"field는 {', '.join(NUM_FIELDS)} 중 하나여야 합니다."}
    conditions, problem = _filters(args)
    if problem:
        return {"error": problem}
    selected = [r for r in rows if all(_matches(r, f, op, v) for f, op, v in conditions)]
    flt = " 그리고 ".join(f"{f} {op} {v}" for f, op, v in conditions) or None
    grand = sum(to_float(r.get(field)) for r in rows if r.get(field) is not None)
    groups: dict[str, list[dict]] = {}
    for r in selected:
        key = " · ".join(str(r.get(g) or "(없음)") for g in (group_by, group_by2) if g) or "전체"
        groups.setdefault(key, []).append(r)
    out = []
    for label, members in groups.items():
        values = [r[field] for r in members if r.get(field) is not None]
        if metric == "count":
            value = float(len(members))
        elif not values:
            continue
        else:
            value = {"sum": sum(values), "avg": sum(values) / len(values), "max": max(values), "min": min(values)}[metric]
        item = {"label": label, "value": value, "count": len(members)}
        if metric == "sum" and field in ("value_krw", "weight_pct") and grand:
            item["share_pct"] = value / grand * 100
        targets = [r["target_pct"] for r in members if r.get("target_pct") is not None]
        if group_by == "category" and not group_by2 and targets:
            item["target_pct"] = targets[0]
        out.append(item)
    out.sort(key=lambda x: x["value"], reverse=args.get("order") != "asc")
    limit = int(args.get("limit") or MAX_ROWS)
    return {"group_by": group_by, "group_by2": group_by2, "metric": metric, "field": field, "filter": flt, "rows": out[:max(1, min(MAX_ROWS, limit))],
            "matched": len(selected), "total_rows": len(rows), "grand_total": grand}
