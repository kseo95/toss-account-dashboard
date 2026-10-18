"""이동평균 스크리너: S&P500 구성 종목(또는 내 종목) 중 주봉 MA 여러 개의 위·아래에 있는 종목 찾기.

- 구성 종목 목록: 공개 데이터셋 datasets/s-and-p-500-companies(퍼블릭 도메인, 위키피디아 기반이라 공식 S&P 자료가 아니고 최신 변경이 며칠 늦을 수 있다).
- 주봉은 앱의 다른 곳과 같은 방식: 일봉을 ISO 주 단위로 묶어 주의 마지막 거래일 값을 쓴다. 이번 주는 진행 중이라 최근 종가가 이번 주 값이다.
- 조건: 종가가 지정한 모든(또는 하나라도) 기간의 단순이동평균보다 아래(또는 위)인가. 주봉이 기간보다 적은 종목은 판단하지 않고 따로 센다.
순수 계산이며 시세는 호출하는 쪽이 넘기고 다른 기능 모듈을 부르지 않는다(구성 종목 수신만 이 모듈이 한다).
"""
from __future__ import annotations

import csv
import io
import re
import threading
import time

import requests

CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
REQUEST_TIMEOUT = 20
CACHE_TTL = 24 * 3600
MIN_CONSTITUENTS = 400          # 이보다 적게 받으면 형식이 바뀐 것으로 보고 실패 처리
DEFAULT_PERIODS = (60, 120, 200, 240)
MIN_PERIOD, MAX_PERIOD, MAX_PERIODS = 2, 300, 8
SECTOR_KO = {   # 질문 속 한국어 업종 → GICS 업종(영문, 데이터셋 표기)
    "정보기술": "Information Technology", "기술": "Information Technology", "it": "Information Technology",
    "헬스케어": "Health Care", "의료": "Health Care", "금융": "Financials", "에너지": "Energy", "산업재": "Industrials",
    "경기소비재": "Consumer Discretionary", "필수소비재": "Consumer Staples", "소비재": "Consumer Discretionary", "유틸리티": "Utilities",
    "부동산": "Real Estate", "리츠": "Real Estate", "소재": "Materials", "통신": "Communication Services",
}
SECTOR_LABELS = {  # 화면 표시용
    "Information Technology": "정보기술", "Health Care": "헬스케어", "Financials": "금융", "Energy": "에너지", "Industrials": "산업재",
    "Consumer Discretionary": "경기소비재", "Consumer Staples": "필수소비재", "Utilities": "유틸리티", "Real Estate": "부동산",
    "Materials": "소재", "Communication Services": "통신",
}

_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()


class ScreenerError(RuntimeError):
    """사용자에게 보여도 안전한 메시지만 담는다."""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def parse_constituents(text: str) -> list[dict]:
    """CSV(Symbol,Security,GICS Sector,GICS Sub-Industry,…) → [{symbol(야후 표기: BRK.B→BRK-B), name, sector, sub_industry}]. 이상한 행은 건너뛴다."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        symbol = str(row.get("Symbol") or "").strip().upper().replace(".", "-")
        if not re.fullmatch(r"[A-Z0-9\-]{1,10}", symbol):
            continue
        out.append({"symbol": symbol, "name": str(row.get("Security") or "")[:60].strip() or symbol,
                    "sector": str(row.get("GICS Sector") or "")[:40].strip(), "sub_industry": str(row.get("GICS Sub-Industry") or "")[:60].strip()})
    return out


def fetch_sp500(get=requests.get) -> list[dict]:
    """S&P500 구성 종목, 하루 캐시. 받지 못하거나 너무 적으면 ScreenerError."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get("sp500")
        if hit and now - hit[0] < CACHE_TTL:
            return [dict(r) for r in hit[1]]
    try:
        resp = get(CONSTITUENTS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        raise ScreenerError("S&P500 구성 종목 목록을 받지 못했습니다.") from None
    rows = parse_constituents(resp.text)
    if len(rows) < MIN_CONSTITUENTS:
        raise ScreenerError("S&P500 구성 종목 목록 형식이 달라졌거나 너무 짧습니다.")
    with _cache_lock:
        _cache["sp500"] = (now, rows)
    return [dict(r) for r in rows]


def weekly_closes(bars: list[tuple]) -> list[float]:
    """일봉 [(날짜, 종가, …)] → 주봉 종가 목록(오래된 것 → 최근). ISO 주 단위로 묶어 주의 마지막 거래일 값. 종가가 없거나 0 이하인 날은 건너뛴다."""
    weeks: dict[tuple, float] = {}
    for bar in sorted(bars, key=lambda b: b[0]):
        if bar[1] and bar[1] > 0:
            weeks[bar[0].isocalendar()[:2]] = float(bar[1])
    return [weeks[k] for k in sorted(weeks)]


def clean_periods(periods) -> list[int]:
    """기간 목록 검증: 정수 2~300, 중복 제거·오름차순, 최대 8개. 비었으면 기본값(60·120·200·240주)."""
    out = sorted({int(p) for p in (periods or []) if isinstance(p, (int, float)) and not isinstance(p, bool) and int(p) == p and MIN_PERIOD <= p <= MAX_PERIOD})
    return out[:MAX_PERIODS] or list(DEFAULT_PERIODS)


def evaluate(closes: list[float], periods: list[int]) -> dict | None:
    """한 종목의 현재가와 기간별 주봉 이동평균 대비 위치. 주봉이 가장 긴 기간보다 적으면 None(판단하지 않음).
    반환: {"close", "mas": [{"period", "value", "dist_pct"}]} — dist_pct = (종가 / MA − 1) × 100, 음수면 MA 아래."""
    if not closes or len(closes) < max(periods):
        return None
    close = closes[-1]
    mas = []
    for p in periods:
        ma = sum(closes[-p:]) / p
        mas.append({"period": p, "value": ma, "dist_pct": (close / ma - 1) * 100})
    return {"close": close, "mas": mas}


def screen(universe: list[dict], weekly: dict[str, list[float]], periods: list[int], side: str = "below", match: str = "all",
           sector: str | None = None) -> dict:
    """조건에 맞는 종목 찾기. universe: [{symbol,name,sector,…}], weekly: {심볼: 주봉 종가 목록(없으면 시세를 못 받은 종목)}.
    side: below(종가가 MA보다 아래) | above, match: all(모든 기간) | any(하나라도), sector: GICS 업종(영문, 일부 일치).
    반환: {"matches": [...정렬됨], "universe_size", "evaluated", "insufficient", "failed", "sector_counts"}."""
    periods = clean_periods(periods)
    side = "above" if side == "above" else "below"
    match = "any" if match == "any" else "all"
    wanted = (sector or "").strip().lower()
    pool = [u for u in universe if not wanted or wanted in str(u.get("sector") or "").lower()]
    matches, evaluated, insufficient, failed = [], 0, 0, 0
    sector_total: dict[str, int] = {}
    sector_hit: dict[str, int] = {}
    for u in pool:
        closes = weekly.get(u["symbol"])
        if closes is None:
            failed += 1
            continue
        got = evaluate(closes, periods)
        if got is None:
            insufficient += 1
            continue
        evaluated += 1
        sec = u.get("sector") or "기타"
        sector_total[sec] = sector_total.get(sec, 0) + 1
        flags = [(m["dist_pct"] < 0) if side == "below" else (m["dist_pct"] > 0) for m in got["mas"]]
        if all(flags) if match == "all" else any(flags):
            matches.append({"symbol": u["symbol"], "name": u.get("name") or u["symbol"], "sector": sec, "close": got["close"], "mas": got["mas"],
                            "hit": [m["period"] for m, f in zip(got["mas"], flags) if f]})
            sector_hit[sec] = sector_hit.get(sec, 0) + 1
    # 가장 깊이 아래(또는 위)에 있는 종목 먼저: 가장 긴 기간 MA와의 거리 기준
    matches.sort(key=lambda m: m["mas"][-1]["dist_pct"], reverse=(side == "above"))
    sector_counts = sorted(({"sector": s, "count": sector_hit[s], "total": sector_total[s]} for s in sector_hit), key=lambda x: (-x["count"], x["sector"]))
    return {"matches": matches, "universe_size": len(pool), "evaluated": evaluated, "insufficient": insufficient, "failed": failed,
            "sector_counts": sector_counts, "periods": periods, "side": side, "match": match}
