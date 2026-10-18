"""상장이 최근인 종목의 과거 유사 국면 계산에 쓸 더 긴 대용 시세.

- 금 ETF(GLDM·GLD·IAU 등): 금 월간 가격(미국 달러/트로이온스). 공개 데이터셋 datasets/gold-prices(퍼블릭 도메인, 1960년부터는 World Bank
  Commodity Markets 월평균). 금 가격이 자유화된 1975년 이후만 쓴다(그 전에는 고정 가격이라 변동이 없다).
  월평균 가격이라 월말 종가와 조금 다르고, 한 달에 한 점이라 일간 자료보다 국면 판정이 거칠다(outlook.MONTHLY가 같은 규칙을 월 단위로 근사).
- 지수가 따로 있는 ETF(S&P500·나스닥100·반도체·코스피200 ETF): 그 지수의 야후 일봉(가격 기준, 배당 제외). 지수가 ETF보다 훨씬 길다.
기본 대응표는 코드에 있는 기본값이다(상품이 완전히 같지는 않으므로 답변에 무엇으로 계산했는지 항상 밝힌다). 외부 조회 실패는 호출한 쪽이 처리한다.
"""
from __future__ import annotations

import csv
import io
import threading
import time
from datetime import date

import requests

GOLD_MONTHLY_URL = "https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv"
GOLD_START = date(1975, 1, 1)
GOLD_LABEL = "금 월간 시세(World Bank 월평균)"
REQUEST_TIMEOUT = 15
CACHE_TTL = 24 * 3600
LONG_PROXY_YEARS = 100          # 야후 일봉을 상장 이후 전체로 받기 위한 기간(P1은 상장일보다 앞서도 된다)

GOLD_SYMBOLS = frozenset({"GLDM", "GLD", "IAU", "SGOL", "BAR", "AAAU", "OUNZ", "GLDW"})
# 야후 심볼 → (긴 일봉을 가진 지수 심볼, 화면에 보일 이름)
DAILY_PROXIES = {
    **{s: ("^GSPC", "S&P500 지수") for s in ("SPY", "SPYM", "VOO", "IVV", "SPLG")},
    **{s: ("^NDX", "나스닥100 지수") for s in ("QQQ", "QQQM")},
    **{s: ("^SOX", "필라델피아 반도체지수") for s in ("SOXX", "SMH")},
    **{s: ("^KS11", "코스피 지수") for s in ("069500.KS", "102110.KS", "148020.KS")},
}

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def long_source_for(yahoo_symbol: str | None) -> dict | None:
    """종목의 긴 대용 시세 종류. {"kind": "monthly_gold"} | {"kind": "daily", "symbol", "label"} | None(없음)."""
    symbol = str(yahoo_symbol or "").upper()
    if symbol in GOLD_SYMBOLS:
        return {"kind": "monthly_gold", "label": GOLD_LABEL}
    if symbol in DAILY_PROXIES:
        proxy, label = DAILY_PROXIES[symbol]
        return {"kind": "daily", "symbol": proxy, "label": label}
    return None


def parse_gold_monthly(text: str) -> dict[date, float]:
    """CSV(Date,Price; Date는 YYYY-MM 또는 YYYY-MM-DD) → {그달 1일: 가격}. 1975년 이전과 이상한 행은 버린다."""
    series: dict[date, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            raw = str(row["Date"]).strip()
            year, month = int(raw[:4]), int(raw[5:7])
            price = float(row["Price"])
            day = date(year, month, 1)
        except (KeyError, TypeError, ValueError):
            continue
        if day >= GOLD_START and price > 0:
            series[day] = price
    return series


def fetch_gold_monthly(get=requests.get) -> dict[date, float]:
    """금 월간 가격(1975년 이후), 하루 캐시. 받지 못하거나 너무 짧으면 requests.RequestException/ValueError."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get("gold")
        if hit and now - hit[0] < CACHE_TTL:
            return dict(hit[1])
    resp = get(GOLD_MONTHLY_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    series = parse_gold_monthly(resp.text)
    if len(series) < 120:
        raise ValueError("금 월간 시세가 비정상적으로 짧습니다.")
    with _cache_lock:
        _cache["gold"] = (now, series)
    return dict(series)


# 과거 매크로 충격 분석(shocks.py)은 일봉이 필요해서 금 월간 대신 금 선물 일봉(2000년~)을 쓴다. 지수 대용은 위와 같다.
GOLD_DAILY_PROXY = ("GC=F", "금 선물 일봉")


def event_source_for(yahoo_symbol: str | None) -> dict | None:
    """과거 매크로 충격 분석에 쓸 긴 일봉 대용. {"symbol", "label"} 또는 None(종목 자신의 시세를 쓴다)."""
    symbol = str(yahoo_symbol or "").upper()
    if symbol in GOLD_SYMBOLS:
        return {"symbol": GOLD_DAILY_PROXY[0], "label": GOLD_DAILY_PROXY[1]}
    if symbol in DAILY_PROXIES:
        proxy, label = DAILY_PROXIES[symbol]
        return {"symbol": proxy, "label": label}
    return None


# 요인 후보에서 빼야 하는 '자기 자신'(같은 자산을 따르는 시리즈). 종목과 요인의 상관이 0.97 미만이라 자동 제외(macro.IDENTICAL_CORR)가 안 되는 경우를 위해(예: 금 ETF와 금 선물은 0.92).
SAME_ASSET_FACTORS: dict[str, frozenset] = {
    **{s: frozenset({"GC=F"}) for s in GOLD_SYMBOLS},
    **{s: frozenset({"SI=F"}) for s in ("SLV", "SIVR", "AGQ")},
    **{s: frozenset({"CL=F"}) for s in ("USO", "BNO", "OIL")},
    "UNG": frozenset({"NG=F"}), "CPER": frozenset({"HG=F"}), "FXE": frozenset({"EURUSD=X"}), "FXY": frozenset({"JPY=X"}),
    "FXB": frozenset({"GBPUSD=X"}), "FXA": frozenset({"AUDUSD=X"}), "UUP": frozenset({"DX-Y.NYB"}), "VIXY": frozenset({"^VIX"}), "VXX": frozenset({"^VIX"}),
    **{s: frozenset({"^GSPC"}) for s in ("SPY", "SPYM", "VOO", "IVV", "SPLG")},
    **{s: frozenset({"^IXIC"}) for s in ("QQQ", "QQQM", "ONEQ")},
    **{s: frozenset({"^SOX"}) for s in ("SOXX", "SMH", "SOXL")},
    **{s: frozenset({"^KS11"}) for s in ("069500.KS", "102110.KS", "148020.KS")},
    **{s: frozenset({"BTC-USD"}) for s in ("IBIT", "FBTC", "GBTC", "BITO", "ARKB", "BTCO")},
    **{s: frozenset({"ETH-USD"}) for s in ("ETHA", "FETH", "ETHE")},
    "HYG": frozenset({"HYG"}), "LQD": frozenset({"LQD"}), "TIP": frozenset({"TIP"}), "VNQ": frozenset({"VNQ"}), "EEM": frozenset({"EEM"}),
    **{s: frozenset({s}) for s in ("XLK", "XLF", "XLE", "XLV", "XLU")},
}


def same_asset_factors(yahoo_symbol: str | None) -> frozenset:
    """이 종목이 사실상 같은 자산을 따르는 요인 심볼들(요인 후보에서 뺀다)."""
    return SAME_ASSET_FACTORS.get(str(yahoo_symbol or "").upper(), frozenset())
