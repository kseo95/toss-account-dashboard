"""섹터 매핑: 내 보유 종목을 섹터에 직접 연결하고, 섹터별 내 노출 금액·비중을 계산한다.

섹터 분류를 외부(Yahoo 등)에서 자동으로 가져오지 않는다 — 한국 종목은 분류가 거칠고 ETF는 구성 비중이
없어서, 사용자가 화면에서 직접 정한 값이 기준이다. ETF의 노출 %는 사용자가 입력한 추정치다.

저장 형식(sector_map.json):
    {"sectors": [{"label": "반도체", "proxy": "SOXX"}],
     "assignments": [{"symbol": "005930", "sector": "반도체", "pct": 100}]}
proxy는 그 섹터의 가격 흐름을 대표하는 Yahoo 티커(선택). 한 종목은 여러 섹터에 걸칠 수 있고, 한 종목의
노출 % 합계는 100을 넘을 수 없다.
"""
from __future__ import annotations

import re
import time

from common import SYMBOL_PATTERN, to_float
from storage import UserStore

SECTOR_MAP_FILE = "sector_map.json"
SECTOR_MAX = 30
SECTOR_LABEL_MAX = 30
ASSIGNMENT_MAX = 150
PROXY_PATTERN = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")  # Yahoo 티커(^GSPC, GC=F 같은 형태 포함)
PCT_SUM_TOLERANCE = 1e-6
TOSS_ID_PATTERN = re.compile(r"^[0-9]{1,10}$")  # 토스 섹터 ID(토스 분류에서 가져온 섹터만 가진다)

SECTOR_INDEX_FILE = "sector_index.json"
SECTOR_INDEX_TTL = 24 * 3600  # 토스 문서가 섹터 분류는 하루 한 번만 받으라고 권장한다
INDEX_MARKETS = ("KR", "US")


def load_sector_map(store: UserStore) -> dict:
    """파일이 없거나 깨졌으면 빈 설정. 읽을 수 없는 항목은 건너뛴다."""
    data = store.read_json(SECTOR_MAP_FILE)
    if not isinstance(data, dict):
        return {"sectors": [], "assignments": []}
    sectors, labels = [], set()
    for s in data.get("sectors", []):
        if not isinstance(s, dict):
            continue
        label = str(s.get("label", "")).strip()
        if label and label not in labels:
            labels.add(label)
            sector = {"label": label, "proxy": str(s.get("proxy") or "").strip()}
            toss_id = str(s.get("toss_id") or "").strip()
            if TOSS_ID_PATTERN.fullmatch(toss_id):
                sector["toss_id"] = toss_id
            sectors.append(sector)
    assignments = []
    for a in data.get("assignments", []):
        if not isinstance(a, dict):
            continue
        symbol = str(a.get("symbol", "")).strip().upper()
        sector = str(a.get("sector", "")).strip()
        pct = to_float(a.get("pct"), 0)
        if symbol and sector in labels and 0 < pct <= 100:
            assignments.append({"symbol": symbol, "sector": sector, "pct": pct})
    return {"sectors": sectors, "assignments": assignments}


def save_sector_map(store: UserStore, config: dict) -> None:
    store.write_json(SECTOR_MAP_FILE, {"sectors": config["sectors"], "assignments": config["assignments"]})


def validate_sector_map(raw) -> tuple[dict, str | None]:
    """설정을 검증/정규화한다. (config, None)이면 통과, (빈 설정, 에러메시지)면 실패."""
    empty = {"sectors": [], "assignments": []}
    if not isinstance(raw, dict):
        return empty, "섹터 설정 형식이 올바르지 않습니다."
    raw_sectors, raw_assignments = raw.get("sectors", []), raw.get("assignments", [])
    if not isinstance(raw_sectors, list) or not isinstance(raw_assignments, list):
        return empty, "sectors와 assignments는 배열이어야 합니다."
    if len(raw_sectors) > SECTOR_MAX:
        return empty, f"섹터는 최대 {SECTOR_MAX}개까지 만들 수 있습니다."
    if len(raw_assignments) > ASSIGNMENT_MAX:
        return empty, f"종목 배정은 최대 {ASSIGNMENT_MAX}개까지 등록할 수 있습니다."

    sectors: list[dict] = []
    labels: set[str] = set()
    for s in raw_sectors:
        if not isinstance(s, dict):
            return empty, "섹터 형식이 올바르지 않습니다."
        label = str(s.get("label", "")).strip()
        if not (1 <= len(label) <= SECTOR_LABEL_MAX):
            return empty, f"섹터 이름은 1~{SECTOR_LABEL_MAX}자여야 합니다."
        if label in labels:
            return empty, f'섹터 이름이 중복됩니다: "{label}"'
        labels.add(label)
        proxy = str(s.get("proxy") or "").strip().upper()
        if proxy and not PROXY_PATTERN.fullmatch(proxy):
            return empty, f'"{label}"의 대표 티커 형식이 올바르지 않습니다: {proxy!r}'
        sector = {"label": label, "proxy": proxy}
        toss_id = str(s.get("toss_id") or "").strip()
        if toss_id:
            if not TOSS_ID_PATTERN.fullmatch(toss_id):
                return empty, f'"{label}"의 토스 섹터 ID 형식이 올바르지 않습니다.'
            sector["toss_id"] = toss_id
        sectors.append(sector)

    assignments: list[dict] = []
    seen: set[tuple[str, str]] = set()
    pct_by_symbol: dict[str, float] = {}
    for a in raw_assignments:
        if not isinstance(a, dict):
            return empty, "종목 배정 형식이 올바르지 않습니다."
        symbol = str(a.get("symbol", "")).strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            return empty, f"잘못된 종목 코드입니다: {a.get('symbol')!r}"
        sector = str(a.get("sector", "")).strip()
        if sector not in labels:
            return empty, f'{symbol}: 존재하지 않는 섹터입니다: "{sector}"'
        if (symbol, sector) in seen:
            return empty, f'{symbol}을(를) "{sector}"에 두 번 배정할 수 없습니다.'
        seen.add((symbol, sector))
        pct = to_float(a.get("pct"), -1)
        if not (0 < pct <= 100):
            return empty, f"{symbol}의 노출%는 0보다 크고 100 이하인 숫자여야 합니다."
        pct_by_symbol[symbol] = pct_by_symbol.get(symbol, 0.0) + pct
        if pct_by_symbol[symbol] > 100 + PCT_SUM_TOLERANCE:
            return empty, f"{symbol}의 섹터별 노출% 합계가 100을 넘습니다."
        assignments.append({"symbol": symbol, "sector": sector, "pct": pct})
    return {"sectors": sectors, "assignments": assignments}, None


def compute_exposure(stock_rows: list[dict], total_eval: float, config: dict) -> dict:
    """섹터별 내 노출 금액·비중과 연결된 보유 종목, 어느 섹터에도 안 묶인 종목.

    stock_rows는 build_holdings의 보유 행(현금 취급 종목 제외). 분모(total_eval)는 보유종목 + 예수금이라
    다른 화면의 지분율과 같은 기준이다. 노출 금액 = 평가금액(원) × 노출% / 100.
    """
    by_symbol: dict[str, list[dict]] = {}
    for a in config["assignments"]:
        by_symbol.setdefault(a["symbol"], []).append(a)

    def pct_of(amount: float) -> float:
        return amount / total_eval * 100 if total_eval else 0.0

    sectors = []
    for s in config["sectors"]:
        holdings = []
        for r in stock_rows:
            for a in by_symbol.get(r["심볼"], []):
                if a["sector"] == s["label"]:
                    amount = r["평가금액(원)"] * a["pct"] / 100
                    holdings.append({"symbol": r["심볼"], "name": r["종목"], "pct": a["pct"], "amount_krw": amount})
        holdings.sort(key=lambda h: h["amount_krw"], reverse=True)
        exposure = sum(h["amount_krw"] for h in holdings)
        sectors.append({"label": s["label"], "proxy": s["proxy"], "exposure_krw": exposure,
                        "exposure_pct": pct_of(exposure), "holdings": holdings})

    unmapped = [{"symbol": r["심볼"], "name": r["종목"], "amount_krw": r["평가금액(원)"],
                 "weight_pct": pct_of(r["평가금액(원)"])}
                for r in stock_rows if r["심볼"] not in by_symbol]
    unmapped.sort(key=lambda u: u["amount_krw"], reverse=True)
    return {"sectors": sectors, "unmapped": unmapped}


# ---------------------------------------------------------------------------
# 토스 섹터 분류 색인 (자동 채우기용)
#
# 토스 API엔 "이 종목이 어느 섹터냐"를 바로 알려주는 엔드포인트가 없어서, 섹터마다 구성 종목/ETF를 받아
# "종목 → 소속 섹터" 방향으로 뒤집어 저장한다. 구성 종목 조회는 하위 섹터를 포함하므로 대분류(depth 0)만
# 훑으면 되고, 구성 ETF 조회는 하위 섹터를 포함하지 않아서 모든 섹터를 훑어야 한다.
# 저장 형식(sector_index.json):
#   {"built_at": 시각, "sectors": [{"id","name","depth","parent"}],
#    "scanned": ["stocks|KR|169", ...],            # 이미 훑은 (종류|시장|섹터) — 중단 후 이어하기용
#    "members": {"stocks": {"KR": {"005930": ["169"]}}, "etfs": {...}}}
# ---------------------------------------------------------------------------
def new_index(toss_sectors: list[dict], now: float | None = None) -> dict:
    """토스 섹터 목록으로 빈 색인을 만든다. 형식이 이상한 항목은 건너뛴다."""
    sectors, seen = [], set()
    for s in toss_sectors:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("sectorId", "")).strip()
        name = str(s.get("name", "")).strip()
        depth = s.get("depth")
        if not TOSS_ID_PATTERN.fullmatch(sid) or not name or depth not in (0, 1, 2) or sid in seen:
            continue
        seen.add(sid)
        parent = s.get("parentSectorId")
        sectors.append({"id": sid, "name": name, "depth": depth, "parent": str(parent) if parent is not None else None})
    return {"built_at": time.time() if now is None else now, "sectors": sectors, "scanned": [],
            "members": {"stocks": {m: {} for m in INDEX_MARKETS}, "etfs": {m: {} for m in INDEX_MARKETS}}}


def load_sector_index(store: UserStore) -> dict | None:
    """저장된 색인. 없거나 형식이 깨졌으면 None(호출하는 쪽이 새로 만든다)."""
    data = store.read_json(SECTOR_INDEX_FILE)
    if not isinstance(data, dict) or not isinstance(data.get("sectors"), list) or not isinstance(data.get("scanned"), list):
        return None
    members = data.get("members")
    if not isinstance(members, dict) or any(not isinstance((members.get(k) or {}).get(m), dict)
                                            for k in ("stocks", "etfs") for m in INDEX_MARKETS):
        return None
    return data


def save_sector_index(store: UserStore, index: dict) -> None:
    store.write_json(SECTOR_INDEX_FILE, index)


def index_is_fresh(index: dict | None, now: float | None = None) -> bool:
    if not index or not index.get("sectors"):
        return False
    return (time.time() if now is None else now) - to_float(index.get("built_at")) < SECTOR_INDEX_TTL


def scan_tasks(index: dict, markets) -> list[tuple[str, str, str]]:
    """훑어야 할 (종류, 시장, 섹터ID) 전체 목록. 주식(대분류만)을 먼저, ETF(전체 섹터)를 나중에."""
    markets = [m for m in INDEX_MARKETS if m in set(markets)]
    top = [s["id"] for s in index["sectors"] if s["depth"] == 0]
    ordered = [s["id"] for s in sorted(index["sectors"], key=lambda s: (s["depth"], int(s["id"])))]
    return ([("stocks", m, sid) for m in markets for sid in top]
            + [("etfs", m, sid) for m in markets for sid in ordered])


def _task_key(task: tuple[str, str, str]) -> str:
    return "|".join(task)


def index_progress(index: dict, markets) -> dict:
    """수집 진행 상황: 전체/완료 작업 수와 주식·ETF 분류가 각각 끝났는지."""
    tasks = scan_tasks(index, markets)
    scanned = set(index["scanned"])
    done_by = {"stocks": [0, 0], "etfs": [0, 0]}  # [완료, 전체]
    for t in tasks:
        done_by[t[0]][1] += 1
        done_by[t[0]][0] += _task_key(t) in scanned
    done = done_by["stocks"][0] + done_by["etfs"][0]
    total = done_by["stocks"][1] + done_by["etfs"][1]
    return {"done": done, "total": total,
            "stocks_done": bool(total) and done_by["stocks"][0] == done_by["stocks"][1],
            "etfs_done": bool(total) and done_by["etfs"][0] == done_by["etfs"][1]}


def run_scan(index: dict, markets, fetch_members, on_progress=None, save=None, save_every: int = 20) -> None:
    """아직 안 훑은 작업을 차례로 실행해 색인에 채운다. fetch_members(섹터ID, 시장, 종류) → 항목 목록.
    예외가 나면 그대로 올린다 — 그때까지 모은 건 save로 저장하고, 다음에 이어서 한다."""
    scanned = set(index["scanned"])
    pending = [t for t in scan_tasks(index, markets) if _task_key(t) not in scanned]
    since_save = 0
    try:
        for kind, market, sector_id in pending:
            for item in fetch_members(sector_id, market, kind):
                symbol = str(item.get("symbol", "")).strip().upper()
                if symbol:
                    ids = index["members"][kind][market].setdefault(symbol, [])
                    if sector_id not in ids:
                        ids.append(sector_id)
            index["scanned"].append(_task_key((kind, market, sector_id)))
            since_save += 1
            if save and since_save >= save_every:
                save(index)
                since_save = 0
            if on_progress:
                on_progress(index_progress(index, markets))
    finally:
        if save and since_save:
            save(index)


def holding_market(row: dict) -> str:
    """보유 행의 시장(KR/US). 시장 값이 없으면 통화로 추정."""
    market = str(row.get("시장") or "").upper()
    if market in INDEX_MARKETS:
        return market
    return "KR" if row.get("통화") == "KRW" else "US"


def suggest_sectors(held_rows: list[dict], index: dict) -> list[dict]:
    """보유 종목마다 토스 분류상 소속 섹터 후보. 주식은 대분류, ETF는 소속된 모든 섹터(깊이 표시).
    종류를 모르면(kind None) 분류가 없거나 아직 다 못 훑은 것이다 — pending으로 구분한다."""
    by_id = {s["id"]: s for s in index["sectors"]}
    markets = {holding_market(r) for r in held_rows}
    scanned = set(index["scanned"])
    all_done_by_market = {}  # 그 시장의 주식·ETF 섹터를 전부 훑었는지
    for m in markets:
        tasks = scan_tasks(index, [m])
        all_done_by_market[m] = bool(tasks) and all(_task_key(t) in scanned for t in tasks)

    def candidates(ids):
        found = [by_id[i] for i in ids if i in by_id]
        found.sort(key=lambda s: (s["depth"], int(s["id"])))
        return [{"sector_id": s["id"], "name": s["name"], "depth": s["depth"]} for s in found]

    result = []
    for r in held_rows:
        symbol, market = r["심볼"], holding_market(r)
        stock_ids = index["members"]["stocks"].get(market, {}).get(symbol.upper(), [])
        etf_ids = index["members"]["etfs"].get(market, {}).get(symbol.upper(), [])
        kind = "stock" if stock_ids else "etf" if etf_ids else None
        result.append({
            "symbol": symbol, "name": r["종목"], "market": market, "kind": kind,
            "candidates": candidates(stock_ids if kind == "stock" else etf_ids),
            "pending": kind is None and not all_done_by_market.get(market, False),
        })
    return result
