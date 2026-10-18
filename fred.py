"""FRED(세인트루이스 연준 경제 데이터) 시계열 조회: 연방기금금리·10년 실질금리·기대인플레이션·장단기 금리차. 선택 기능이다.

사용자가 자기 FRED API 키를 화면에 입력했을 때만 쓰고(키는 서버 메모리 세션에만 보관), 없으면 야후의 대용 시세(3개월물 금리 등)로 계속한다.
FRED는 키를 URL 질의에 넣는 방식만 지원해서, 요청 예외를 사용자에게 그대로 보여 주지 않는다(예외 메시지에 URL과 키가 들어 있다).
받은 값은 공개 통계라 캐시해도 되지만 키는 어디에도 저장하거나 기록하지 않는다. 시세는 호출하는 쪽에 넘기고 다른 기능 모듈은 부르지 않는다.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date

import requests

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_TIMEOUT = 20
CACHE_TTL = 12 * 3600
KEY_PATTERN = re.compile(r"^[a-z0-9]{32}$")   # FRED API 키 형식(소문자·숫자 32자)
START = date(1950, 1, 1)
PREFIX = "FRED:"

# 지원하는 FRED 시리즈 ID(모두 일간, 값의 차이로 다룬다). 화면 이름·방향 표현은 macro.FRED_FACTORS에 있다(다른 기능 모듈을 부르지 않으려고 따로 둠).
SERIES_IDS = ("DFF", "DGS2", "DFII10", "T10YIE", "T10Y2Y")
# 월간·주간 지표: 일간 회귀·상관의 후보로는 맞지 않아(관측 간격이 달라 날짜가 어긋난다) 거시 사례집의 국면별 변화에만 쓴다.
SLOW_SERIES_IDS = ("CPIAUCSL", "UNRATE", "WALCL")   # 소비자물가지수(월), 실업률(월), 연준 총자산(주)

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


class FredError(RuntimeError):
    """사용자에게 보여도 안전한 메시지만 담는다(키·URL 없음)."""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def valid_key(key: str) -> bool:
    return bool(KEY_PATTERN.match(str(key or "")))


def parse_observations(payload) -> dict[date, float]:
    """FRED 응답 {"observations": [{"date": "2008-01-01", "value": "3.94"}, …]} → {날짜: 값}. 값이 "."(결측)이거나 이상한 행은 건너뛴다."""
    out: dict[date, float] = {}
    rows = payload.get("observations") if isinstance(payload, dict) else None
    for row in rows if isinstance(rows, list) else []:
        try:
            out[date.fromisoformat(str(row["date"])[:10])] = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_series(api_key: str, series_id: str, get=requests.get) -> dict[date, float]:
    """한 시리즈의 전체 관측값(1950년~), 12시간 캐시. 실패하면 FredError(안전한 메시지)."""
    if series_id not in SERIES_IDS and series_id not in SLOW_SERIES_IDS:
        raise FredError("지원하지 않는 FRED 시리즈입니다.")
    now = time.time()
    with _cache_lock:
        hit = _cache.get(series_id)
        if hit and now - hit[0] < CACHE_TTL:
            return dict(hit[1])
    try:
        resp = get(FRED_URL, params={"series_id": series_id, "api_key": api_key, "file_type": "json", "observation_start": START.isoformat()},
                   headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        raise FredError("FRED에 연결하지 못했습니다.") from None   # 원인 예외에는 URL(키 포함)이 있어 연결하지 않는다
    if resp.status_code in (400, 401, 403):
        raise FredError("FRED API 키가 올바르지 않거나 요청이 거절되었습니다.")
    if resp.status_code == 429:
        raise FredError("FRED 요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.")
    if resp.status_code >= 400:
        raise FredError(f"FRED 오류 ({resp.status_code})")
    try:
        series = parse_observations(resp.json())
    except ValueError:
        raise FredError("FRED 응답을 읽지 못했습니다.") from None
    if len(series) < 100:
        raise FredError("FRED 시리즈가 비정상적으로 짧습니다.")
    with _cache_lock:
        _cache[series_id] = (now, series)
    return dict(series)


def yoy_series(series: dict[date, float]) -> dict[date, float]:
    """월간 지수(CPI) → 전년 같은 달 대비 상승률(%). 12개월 전 값이 없는 달은 뺀다."""
    out: dict[date, float] = {}
    for day, value in series.items():
        try:
            earlier = series.get(day.replace(year=day.year - 1))
        except ValueError:      # 2월 29일 등
            earlier = None
        if earlier and earlier > 0 and value > 0:
            out[day] = (value / earlier - 1) * 100
    return out
