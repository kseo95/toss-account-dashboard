"""조건부 반응: "미국 금리가 오르면 금과 나스닥은 어떻게 됨?" — 요인(금리·달러·유가 등)이 크게 오른(내린) 달에 주요 자산이 평소와 어떻게 달랐는지.

지난 수십 년의 겹치지 않는 N거래일 구간(기본 21 = 한 달)을 요인 변화 크기로 나눠(상위 20%=큰 상승, 하위 20%=큰 하락, 가운데 60%=보합),
구간마다 자산의 수익률 중앙값·오른 비율·구간 수를 평소(전체)와 비교한다. 오른 비율은 평소 비율과 이항검정(본페로니 보정)으로 구분되는지 본다.
같은 구간에 같이 움직인 기록이며 요인이 원인이라는 증명이 아니다. 순수 계산이며 시세는 호출하는 쪽이 넘기고 다른 기능 모듈을 부르지 않는다.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import date

HORIZONS = (5, 21, 63)
DEFAULT_HORIZON = 21
BUCKET_SHARE = 0.2            # 큰 상승 = 구간 변화 상위 20%, 큰 하락 = 하위 20%
MIN_BUCKET = 8                # 이보다 구간이 적으면 판단하지 않는다
MIN_WINDOWS = 40
ALPHA = 0.05
CORR_WINDOWS = (("1년", 250), ("3년", 750), ("10년", 2500))

# 질문 속 낱말 → (야후 심볼, 화면 이름, 종류: level=값의 차이(금리 bp) | price=수익률). 모두 공백 없는 소문자.
FACTOR_WORDS = {
    "금리": ("^TNX", "미국 10년물 금리", "level"), "국채금리": ("^TNX", "미국 10년물 금리", "level"), "10년물": ("^TNX", "미국 10년물 금리", "level"),
    "장기금리": ("^TYX", "미국 30년물 금리", "level"), "30년물": ("^TYX", "미국 30년물 금리", "level"), "5년물": ("^FVX", "미국 5년물 금리", "level"),
    "단기금리": ("^IRX", "미국 3개월 금리", "level"), "기준금리": ("^IRX", "미국 3개월 금리", "level"),
    "달러": ("DX-Y.NYB", "달러(인덱스)", "price"), "달러인덱스": ("DX-Y.NYB", "달러(인덱스)", "price"),
    "유가": ("CL=F", "유가(WTI)", "price"), "원유": ("CL=F", "유가(WTI)", "price"),
    "vix": ("^VIX", "VIX", "price"), "변동성": ("^VIX", "VIX", "price"), "공포지수": ("^VIX", "VIX", "price"),
    "환율": ("KRW=X", "원/달러 환율", "price"), "원달러": ("KRW=X", "원/달러 환율", "price"),
    "구리": ("HG=F", "구리", "price"),
}
# 자산 낱말 → (심볼, 이름). '금'·'은'은 조사·낱말과 섞이므로 질문 해석 쪽에서 따로 가린다.
ASSET_WORDS = {
    "나스닥": ("^IXIC", "나스닥"), "s&p": ("^GSPC", "S&P500"), "sp500": ("^GSPC", "S&P500"), "에스앤피": ("^GSPC", "S&P500"), "미국주식": ("^GSPC", "S&P500"),
    "러셀": ("^RUT", "러셀2000"), "반도체": ("^SOX", "반도체지수(SOX)"), "코스피": ("^KS11", "코스피"), "코스닥": ("^KQ11", "코스닥"),
    "장기채": ("TLT", "미국 장기채(TLT)"), "tlt": ("TLT", "미국 장기채(TLT)"), "채권": ("TLT", "미국 장기채(TLT)"),
    "비트코인": ("BTC-USD", "비트코인"), "코인": ("BTC-USD", "비트코인"), "금값": ("GC=F", "금"), "금시세": ("GC=F", "금"), "은값": ("SI=F", "은"), "실버": ("SI=F", "은"),
    "리츠": ("VNQ", "리츠(VNQ)"), "신흥국": ("EEM", "신흥국 주식(EEM)"), "원유": ("CL=F", "유가(WTI)"), "달러": ("DX-Y.NYB", "달러(인덱스)"),
}
GOLD = ("GC=F", "금")
DEFAULT_ASSETS = [GOLD, ("^IXIC", "나스닥"), ("^GSPC", "S&P500"), ("^KS11", "코스피"), ("TLT", "미국 장기채(TLT)"), ("BTC-USD", "비트코인")]
MAX_ASSETS = 6


def _binom_p(k: int, n: int, p: float) -> float:
    """양쪽 이항검정 p값(정확 계산, n은 수백 이하)."""
    if n <= 0:
        return 1.0
    pmf = lambda i: math.comb(n, i) * p ** i * (1 - p) ** (n - i)
    observed = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= observed * (1 + 1e-9)))


def _percentile(sorted_values: list[float], q: float) -> float:
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def windows(factor: dict, kind: str, asset: dict, horizon: int) -> list[dict]:
    """겹치지 않는 구간(가장 최근 날부터 거슬러). 공통 거래일 기준. 반환(오래된 것 → 최근): [{"end": 날짜, "factor": 변화(금리 bp | %), "asset": 수익률 %}]"""
    common = sorted(set(factor) & set(asset))
    out = []
    i = len(common) - 1
    while i - horizon >= 0:
        a, b = common[i - horizon], common[i]
        fa, fb, xa, xb = factor[a], factor[b], asset[a], asset[b]
        if xa > 0 and xb > 0 and (kind == "level" or (fa > 0 and fb > 0)):
            out.append({"end": b, "factor": (fb - fa) * 100 if kind == "level" else (fb / fa - 1) * 100, "asset": (xb / xa - 1) * 100})
        i -= horizon
    return out[::-1]


def _factor_changes(factor: dict, kind: str, horizon: int) -> list[float]:
    """요인 자신의 겹치지 않는 구간 변화(금리는 0 이하 값도 허용). 큰 상승·하락의 경계를 정하는 분포."""
    dates = sorted(factor)
    out, i = [], len(dates) - 1
    while i - horizon >= 0:
        a, b = factor[dates[i - horizon]], factor[dates[i]]
        if kind == "level":
            out.append((b - a) * 100)
        elif a > 0 and b > 0:
            out.append((b / a - 1) * 100)
        i -= horizon
    return out


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "median": statistics.median(values) if values else None, "mean": statistics.mean(values) if values else None,
            "up_share": (sum(v > 0 for v in values) / len(values)) if values else None}


def _correlations(factor: dict, kind: str, asset: dict) -> dict:
    """하루 변화끼리의 상관(최근 1년·3년·10년). 금리는 차이, 나머지는 로그 수익률."""
    def daily(series, level):
        dates = sorted(series)
        out = {}
        for prev, cur in zip(dates, dates[1:]):
            if level:
                out[cur] = series[cur] - series[prev]
            elif series[prev] > 0 and series[cur] > 0:
                out[cur] = math.log(series[cur] / series[prev])
        return out
    f, a = daily(factor, kind == "level"), daily(asset, False)
    result = {}
    for label, days in CORR_WINDOWS:
        shared = sorted(set(f) & set(a))[-days:]
        if len(shared) < days * 0.8:
            result[label] = None
            continue
        x, y = [f[d] for d in shared], [a[d] for d in shared]
        mx, my = sum(x) / len(x), sum(y) / len(y)
        sx, sy = math.sqrt(sum((v - mx) ** 2 for v in x)), math.sqrt(sum((v - my) ** 2 for v in y))
        result[label] = sum((p - mx) * (q - my) for p, q in zip(x, y)) / (sx * sy) if sx and sy else None
    return result


def analyze(factor: dict, kind: str, assets: dict[str, dict], horizon: int = DEFAULT_HORIZON) -> dict:
    """요인 하나에 대해 자산별 조건부 반응. assets: {이름: {날짜: 종가}}.
    반환: {"horizon", "thresholds": {"up": 상위 20% 경계, "down": 하위 20% 경계}, "current": 가장 최근 구간 변화, "current_bucket", "assets": [{"name", "windows", "baseline",
    "up": {...}, "down": {...}, "flat": {...}, "correlation": {...}}], "insufficient": [이름…]}. 구간 경계는 요인 기준(자산마다 공통 거래일이 달라도 같은 기준)이다."""
    horizon = horizon if horizon in HORIZONS else DEFAULT_HORIZON
    changes = sorted(_factor_changes(factor, kind, horizon))       # 요인 자신의 구간 변화 분포로 경계를 정한다
    if len(changes) < MIN_WINDOWS:
        return {"horizon": horizon, "thresholds": None, "current": None, "current_bucket": None, "assets": [], "insufficient": list(assets)}
    hi, lo = _percentile(changes, 1 - BUCKET_SHARE), _percentile(changes, BUCKET_SHARE)
    latest = max(factor)
    dates = sorted(factor)
    current = None
    if len(dates) > horizon:
        a, b = factor[dates[-horizon - 1]], factor[dates[-1]]
        current = (b - a) * 100 if kind == "level" else ((b / a - 1) * 100 if a > 0 and b > 0 else None)
    bucket = None if current is None else "up" if current >= hi else "down" if current <= lo else "flat"
    rows, insufficient = [], []
    tests = 0
    for name, series in assets.items():
        ws = windows(factor, kind, series, horizon)
        if len(ws) < MIN_WINDOWS:
            insufficient.append(name)
            continue
        groups = {"up": [w for w in ws if w["factor"] >= hi], "down": [w for w in ws if w["factor"] <= lo], "flat": [w for w in ws if lo < w["factor"] < hi]}
        base = _stats([w["asset"] for w in ws])
        row = {"name": name, "windows": len(ws), "baseline": base, "first": ws[0]["end"].isoformat(), "correlation": _correlations(factor, kind, series)}
        for key, items in groups.items():
            st = _stats([w["asset"] for w in items])
            st["p_value"] = _binom_p(sum(w["asset"] > 0 for w in items), len(items), base["up_share"]) if len(items) >= MIN_BUCKET and 0 < base["up_share"] < 1 else None
            row[key] = st
            if key != "flat" and st["p_value"] is not None:
                tests += 1
        rows.append(row)
    tests = max(tests, 1)
    for row in rows:                                                # 자산 × (큰 상승·큰 하락) 전체 검정 수로 본페로니 보정
        for key in ("up", "down", "flat"):
            st = row[key]
            if key == "flat" or st["p_value"] is None:
                st["verdict"] = "insufficient" if st["n"] < MIN_BUCKET else "mixed"
                continue
            if st["p_value"] * tests < ALPHA:
                st["verdict"] = "higher" if st["up_share"] > row["baseline"]["up_share"] else "lower"
            else:
                st["verdict"] = "mixed"
    return {"horizon": horizon, "thresholds": {"up": hi, "down": lo}, "current": current, "current_bucket": bucket, "as_of": latest.isoformat() if isinstance(latest, date) else None,
            "assets": rows, "insufficient": insufficient, "tests": tests}



def _spaceless(text: str) -> str:
    return re.sub(r"[\s.,!?~'\"]+", "", str(text or "").lower())


GOLD_TOKEN = re.compile(r"^금(?:값|시세|가격)?(?:이랑|하고|이나|이|가|은|는|과|와|을|를|의|도|에|랑|나|만)?$|^(?:gold|gld|gc=f)$")


def resolve_factor(name: str) -> tuple[str, str, str] | None:
    """요인 이름(한글 낱말·야후 심볼) → (심볼, 화면 이름, 종류). 모르면 None."""
    key = _spaceless(name)
    if not key:
        return None
    if key in FACTOR_WORDS:
        return FACTOR_WORDS[key]
    for symbol, label, kind in FACTOR_WORDS.values():
        if key == symbol.lower():
            return symbol, label, kind
    for word in sorted(FACTOR_WORDS, key=len, reverse=True):
        if word in key:
            return FACTOR_WORDS[word]
    return None


def resolve_asset(name: str) -> tuple[str, str] | None:
    """자산 이름 → (심볼, 화면 이름). '금'은 조사가 붙은 낱말(금과·금은·금값)만, '금리'·'현금' 같은 낱말 속 글자는 금이 아니다. 모르면 None."""
    raw = str(name or "").strip().lower()
    if not raw:
        return None
    if any(GOLD_TOKEN.match(tok) for tok in raw.split()):
        return GOLD
    key = _spaceless(raw)
    if key in ASSET_WORDS:
        return ASSET_WORDS[key]
    for word in sorted(ASSET_WORDS, key=len, reverse=True):
        if word in key:
            return ASSET_WORDS[word]
    return None
