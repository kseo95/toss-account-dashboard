"""밸류에이션: PER·PSR·PBR·EPS·BPS·ROE(야후 파이낸스 yfinance)와 '비싼가·싼가' 판단.

판단은 한 숫자를 기준선에 대는 것이 아니라 서로 다른 관점의 신호를 모아 본다:
  - 섹터 비교: 같은 업종 S&P500 종목(표본)의 중앙값 대비 PER·PSR·PBR이 얼마나 비싼지(±band% 안은 비슷함)
  - 성장 대비: PEG(PER ÷ 이익성장률)가 1 미만이면 싼 쪽, 2 초과면 비싼 쪽
  - 수익성 대비: ROE로 정당한 PBR(= (ROE − g) ÷ (요구수익률 − g))을 구해 실제 PBR과 비교
ETF는 EPS·BPS·ROE가 없어 PER·PBR만 시장(S&P500 ETF) 대비로 본다(주식형 ETF만, 금·채권 ETF는 해당 없음).
같은 신호를 가격으로도 바꿔 준다(implied_prices): 섹터 중앙값 배수를 이 회사의 EPS·선행 EPS·주당매출·BPS에 곱한 추정 가격과, ROE로 정당화되는 PBR × BPS.
신뢰하기 어려운 방법(적자·BPS 왜곡 등)은 빼고 이유를 남기며, 믿을 만한 방법이 2개 미만이면 범위를 내지 않는다. 적정 주가가 아니라 '이 기준으로는 이 정도'라는 범위다.
신호가 2개 미만이거나 한국 종목처럼 야후 재무 데이터가 불완전하면 '판단 보류'. 상대 평가이며 투자 조언이 아니다.
순수 계산이 대부분이고 야후 조회(fetch_info)만 이 모듈이 한다(지연 import·하루 안 캐시, 실패는 상태로 알림). 조회어에는 종목 코드만 쓴다.
"""
from __future__ import annotations

import math
import re
import statistics
import threading
import time

INFO_CACHE_TTL = 6 * 3600
SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-=^]{0,14}$")
NAME_MAX = 60

# 야후 업종 이름 → GICS 업종(스크리너의 S&P500 구성 종목 표기)
GICS_OF_YAHOO = {
    "Technology": "Information Technology", "Healthcare": "Health Care", "Financial Services": "Financials", "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples", "Communication Services": "Communication Services", "Industrials": "Industrials", "Energy": "Energy",
    "Utilities": "Utilities", "Real Estate": "Real Estate", "Basic Materials": "Materials",
}
BAND = 0.30                  # 비교 대상 대비 ±30% 안은 '비슷함'
COST_OF_EQUITY = 0.09        # 정당 PBR 계산의 요구수익률
LONG_GROWTH = 0.03           # 정당 PBR 계산의 장기 성장률
PEG_CHEAP, PEG_EXPENSIVE = 1.0, 2.0
MIN_SIGNALS = 2              # 신호가 이보다 적으면 판단하지 않는다
VERDICT_SCORE = 0.5          # (비싼 신호 − 싼 신호) ÷ 신호 수가 이 이상이면 고평가 쪽, 이 이하(−)면 저평가 쪽
MIN_PEERS = 8
PER_MAX, PBR_MAX, PSR_MAX = 500.0, 200.0, 500.0     # 이를 넘는 값은 이상치로 보고 버린다
ROE_OUTLIER_PCT = 100.0      # 자사주 매입 등으로 자기자본이 작아 생기는 비정상적으로 큰 ROE

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _positive(value, cap: float):
    v = _num(value)
    return v if v is not None and 0 < v <= cap else None


def is_korean(symbol: str, currency: str | None = None) -> bool:
    return symbol.upper().endswith((".KS", ".KQ")) or (currency or "").upper() == "KRW"


def clean_info(info, symbol: str = "") -> dict:
    """야후 info → 정리된 지표. 모든 숫자는 float|None이고, 적자·이상치는 None으로 두되 이유를 flags에 남긴다.
    kind: stock | etf | other. roe는 %(야후는 비율)."""
    info = info if isinstance(info, dict) else {}
    quote = str(info.get("quoteType") or "").upper()
    kind = "stock" if quote == "EQUITY" else "etf" if quote in ("ETF", "MUTUALFUND") else "other"
    flags = []
    eps, forward_eps = _num(info.get("trailingEps")), _num(info.get("forwardEps"))
    per = _positive(info.get("trailingPE"), PER_MAX)
    if _num(info.get("trailingPE")) is not None and per is None:
        flags.append("per_outlier")
    if eps is not None and eps <= 0:
        per = None
        flags.append("loss")
    forward_per = _positive(info.get("forwardPE"), PER_MAX)
    roe = _num(info.get("returnOnEquity"))
    roe = roe * 100 if roe is not None else None
    if roe is not None and abs(roe) > ROE_OUTLIER_PCT:
        flags.append("roe_outlier")
    peg = _positive(info.get("trailingPegRatio"), 50.0) or _positive(info.get("pegRatio"), 50.0)
    sector_raw = str(info.get("sector") or "")
    currency = str(info.get("currency") or "")[:5]
    price = _positive(info.get("currentPrice"), 1e9) or _positive(info.get("regularMarketPrice"), 1e9) or _positive(info.get("previousClose"), 1e9)
    return {"symbol": str(symbol).upper()[:15], "name": str(info.get("shortName") or info.get("longName") or "")[:NAME_MAX], "kind": kind,
            "category": str(info.get("category") or "")[:40], "currency": currency, "sector": GICS_OF_YAHOO.get(sector_raw), "yahoo_sector": sector_raw[:30],
            "price": price, "financial_currency": str(info.get("financialCurrency") or "")[:5], "market_cap": _num(info.get("marketCap")), "per": per, "forward_per": forward_per, "psr": _positive(info.get("priceToSalesTrailing12Months"), PSR_MAX),
            "pbr": _positive(info.get("priceToBook"), PBR_MAX), "eps": eps, "forward_eps": forward_eps, "bps": _num(info.get("bookValue")), "roe": roe, "peg": peg, "flags": flags}


def _default_ticker(symbol: str):
    import yfinance  # 지연 import: 없으면 호출한 쪽이 'unavailable'로 처리한다
    return yfinance.Ticker(symbol)


def fetch_info(symbol: str, ticker_factory=None) -> dict:
    """한 종목의 정리된 지표. status: ok | unavailable(yfinance 미설치) | error(조회 실패). ok는 6시간 캐시(error·unavailable은 다음에 다시 시도)."""
    if not SYMBOL_RE.match(str(symbol or "")):
        return {"status": "error", "metrics": None}
    key = symbol.upper()
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < INFO_CACHE_TTL:
            return {"status": "ok", "metrics": dict(hit[1])}
    try:
        info = (ticker_factory or _default_ticker)(symbol).info
    except ImportError:
        return {"status": "unavailable", "metrics": None}
    except Exception:  # yfinance는 자체 예외를 던진다(요청 제한·없는 종목 등). 이유는 알리지 않고 상태만 남긴다.
        return {"status": "error", "metrics": None}
    if not isinstance(info, dict) or not info:
        return {"status": "error", "metrics": None}
    metrics = clean_info(info, symbol)
    with _cache_lock:
        _cache[key] = (now, metrics)
    return {"status": "ok", "metrics": dict(metrics)}


def peer_stats(peers: list[dict]) -> dict:
    """섹터 비교 표본의 중앙값. {지표: {"median", "n"}} — 표본이 MIN_PEERS개 미만인 지표는 넣지 않는다."""
    out = {}
    for key in ("per", "psr", "pbr"):
        values = [p[key] for p in peers if p.get(key) is not None]
        if len(values) >= MIN_PEERS:
            out[key] = {"median": statistics.median(values), "n": len(values)}
    return out


def _signal(key: str, label: str, basis: str, value: float, ref: float, band: float, ref_label: str) -> dict:
    premium = (value / ref - 1) * 100
    verdict = "expensive" if premium > band * 100 else "cheap" if premium < -band * 100 else "fair"
    return {"key": key, "label": label, "basis": basis, "value": value, "ref": ref, "ref_label": ref_label, "premium_pct": premium, "verdict": verdict}


def justified_pbr(roe_pct: float, cost: float = COST_OF_EQUITY, growth: float = LONG_GROWTH) -> float | None:
    """ROE로 정당화되는 PBR = (ROE − g) ÷ (요구수익률 − g). ROE가 성장률 이하면 None(이 방식으로는 평가하지 않는다)."""
    roe = roe_pct / 100
    return (roe - growth) / (cost - growth) if roe > growth and cost > growth else None


def verdict_from(signals: list[dict]) -> dict:
    cheap = sum(1 for s in signals if s["verdict"] == "cheap")
    expensive = sum(1 for s in signals if s["verdict"] == "expensive")
    n = len(signals)
    if n < MIN_SIGNALS:
        return {"label": "hold", "expensive": expensive, "cheap": cheap, "signals": n}
    score = (expensive - cheap) / n
    return {"label": "expensive" if score >= VERDICT_SCORE else "cheap" if score <= -VERDICT_SCORE else "fair", "expensive": expensive, "cheap": cheap, "signals": n}


BPS_DISTORTED_PBR = 30.0      # PBR이 이보다 크면 자기자본이 지나치게 작아(자사주 매입 등) BPS 기반 가격은 믿지 않는다
MIN_IMPLIED_METHODS = 2
WIDE_RANGE_RATIO = 3.0       # 가장 높은 추정이 가장 낮은 추정의 이 배수를 넘으면 방법 간 차이가 너무 커 참고하기 어렵다고 알린다


def implied_prices(m: dict, stats: dict | None, cost: float = COST_OF_EQUITY, growth: float = LONG_GROWTH) -> dict | None:
    """섹터 중앙값 배수 × 이 회사 지표 = 추정 가격(방법별). 현재가·통화가 맞지 않거나 한국 종목이면 None.
    방법: PER(후행 EPS) · PER(선행 EPS) · PSR(주당매출 = 현재가 ÷ PSR) · PBR(BPS) · ROE 정당 PBR × BPS.
    reliable=False인 방법(적자·BPS 왜곡·ROE 이상치)은 범위에서 뺀다. 믿을 만한 방법이 2개 미만이면 low·high는 None."""
    price = m.get("price")
    if not price or is_korean(m["symbol"], m.get("currency")):
        return None
    if m.get("financial_currency") and m.get("currency") and m["financial_currency"].upper() != m["currency"].upper():
        return None      # ADR처럼 재무 통화와 주가 통화가 다르면 EPS·BPS에 곱한 값이 틀린다
    stats = stats or {}
    distorted = "roe_outlier" in m["flags"] or (m.get("pbr") or 0) > BPS_DISTORTED_PBR
    methods = []

    def add(key, label, value, reliable=True, reason=""):
        methods.append({"key": key, "label": label, "price": value, "diff_pct": (value / price - 1) * 100, "reliable": reliable, "reason": reason})
    per_ref = stats.get("per")
    if per_ref and m.get("eps") is not None:
        if m["eps"] > 0:
            add("per", "PER × EPS", m["eps"] * per_ref["median"])
    if per_ref and m.get("forward_eps") is not None and m["forward_eps"] > 0:
        add("forward_per", "PER × 선행 EPS", m["forward_eps"] * per_ref["median"])
    if stats.get("psr") and m.get("psr"):
        add("psr", "PSR × 주당매출", price / m["psr"] * stats["psr"]["median"])
    if stats.get("pbr") and m.get("bps") and m["bps"] > 0:
        add("pbr", "PBR × BPS", m["bps"] * stats["pbr"]["median"], not distorted, "자기자본이 작아(자사주 매입 등) BPS 기반 가격이 왜곡됨" if distorted else "")
    fair_pbr = justified_pbr(m["roe"], cost, growth) if m.get("roe") is not None else None
    if fair_pbr and m.get("bps") and m["bps"] > 0:
        add("roe_pbr", "ROE 정당 PBR × BPS", m["bps"] * fair_pbr, not distorted, "자기자본이 작아(자사주 매입 등) BPS 기반 가격이 왜곡됨" if distorted else "")
    if not methods:
        return None
    good = [x["price"] for x in methods if x["reliable"]]
    enough = len(good) >= MIN_IMPLIED_METHODS
    return {"price": price, "currency": m.get("currency") or "", "methods": methods, "reliable_n": len(good),
            "low": min(good) if enough else None, "high": max(good) if enough else None, "mid": statistics.median(good) if enough else None,
            "wide": bool(enough and min(good) > 0 and max(good) / min(good) > WIDE_RANGE_RATIO)}


def assess_stock(m: dict, stats: dict | None, band: float = BAND, cost: float = COST_OF_EQUITY, growth: float = LONG_GROWTH) -> dict:
    """개별 종목의 신호와 판단. 한국 종목은 야후 재무 데이터가 불완전해 판단하지 않는다(지표만 보여 줌)."""
    notes = []
    if is_korean(m["symbol"], m.get("currency")):
        return {"signals": [], "verdict": {"label": "hold", "expensive": 0, "cheap": 0, "signals": 0}, "reason": "korean", "notes": ["한국 종목은 야후 재무 데이터가 불완전하고 단위가 맞지 않을 수 있어 판단하지 않습니다"]}
    signals = []
    for key, label in (("per", "PER"), ("psr", "PSR"), ("pbr", "PBR")):
        ref = (stats or {}).get(key)
        if m.get(key) is not None and ref:
            signals.append(_signal(key, label, "sector", m[key], ref["median"], band, f"섹터 중앙값(S&P500 {ref['n']}개)"))
    if m.get("peg") is not None and "loss" not in m["flags"]:     # 적자면 PEG는 선행 이익 기대에 기댄 값이라 쓰지 않는다
        verdict = "cheap" if m["peg"] < PEG_CHEAP else "expensive" if m["peg"] > PEG_EXPENSIVE else "fair"
        signals.append({"key": "peg", "label": "PEG", "basis": "growth", "value": m["peg"], "ref": None, "ref_label": f"{PEG_CHEAP:g} 미만 싼 쪽 · {PEG_EXPENSIVE:g} 초과 비싼 쪽", "premium_pct": None, "verdict": verdict})
    if m.get("roe") is not None and m.get("pbr") is not None and "roe_outlier" not in m["flags"]:
        fair_pbr = justified_pbr(m["roe"], cost, growth)
        if fair_pbr:
            signals.append(_signal("roe_pbr", "ROE 대비 PBR", "profitability", m["pbr"], fair_pbr, band, f"ROE {m['roe']:.0f}%로 정당화되는 PBR"))
        else:
            notes.append(f"ROE {m['roe']:.0f}%가 성장률 가정({growth * 100:.0f}%) 이하라 ROE 기준 평가를 하지 않았습니다")
    if "loss" in m["flags"]:
        notes.append("적자(EPS 0 이하)라 PER·PEG는 계산하지 않았습니다")
    if "roe_outlier" in m["flags"]:
        notes.append("ROE가 100%를 넘어 자기자본이 작은(자사주 매입 등) 이상치로 보고 ROE 기준 평가를 하지 않았습니다")
    if not stats:
        notes.append("같은 업종 비교 표본을 구하지 못해 섹터 비교는 하지 않았습니다")
    return {"signals": signals, "verdict": verdict_from(signals), "reason": "", "notes": notes, "implied": implied_prices(m, stats, cost, growth)}


def assess_etf(m: dict, benchmark: dict | None, band: float = BAND) -> dict:
    """주식형 ETF의 PER·PBR을 시장(S&P500 ETF) 대비로. EPS·BPS·ROE는 ETF에 없다. 금·채권 ETF처럼 PER·PBR이 없거나 의미 없으면 해당 없음."""
    if m.get("per") is None or m.get("pbr") is None:
        return {"signals": [], "verdict": {"label": "na", "expensive": 0, "cheap": 0, "signals": 0}, "reason": "no_equity_multiples", "notes": ["PER·PBR이 없는 ETF(금·채권·원자재 등)는 밸류에이션 대상이 아닙니다"]}
    if not benchmark or benchmark.get("per") is None or benchmark.get("pbr") is None:
        return {"signals": [], "verdict": {"label": "hold", "expensive": 0, "cheap": 0, "signals": 0}, "reason": "no_benchmark", "notes": ["비교할 시장(S&P500 ETF) 지표를 받지 못했습니다"]}
    if m["symbol"] == benchmark["symbol"]:
        return {"signals": [], "verdict": {"label": "benchmark", "expensive": 0, "cheap": 0, "signals": 0}, "reason": "benchmark", "notes": ["비교 기준 자신입니다"]}
    signals = [_signal("per", "PER", "market", m["per"], benchmark["per"], band, f"S&P500({benchmark['symbol']})"),
               _signal("pbr", "PBR", "market", m["pbr"], benchmark["pbr"], band, f"S&P500({benchmark['symbol']})")]
    return {"signals": signals, "verdict": verdict_from(signals), "reason": "", "notes": ["ETF의 PER·PBR은 편입 종목 지표의 평균이라 개별 종목과 의미가 다릅니다"]}


def sample_peers(constituents: list[dict], sector: str, exclude: str, limit: int) -> list[str]:
    """같은 GICS 업종의 S&P500 구성 종목 코드 표본(결정적 — 코드 순서와 무관하게 같은 표본). 자기 자신은 뺀다."""
    pool = sorted((c["symbol"] for c in constituents if c.get("sector") == sector and c["symbol"].upper() != exclude.upper()),
                  key=lambda s: (sum(ord(ch) * (i + 7) for i, ch in enumerate(s)) % 9973, s))
    return pool[:limit]
