"""추세 사다리와 전환 상태: 여러 기간의 수익률(1주·단기·중기·6개월·장기)과 50일선·200일선 위치를 한 번에 보고, 기간 사이의 엇갈림으로 상태 이름을 붙인다.

추세전환은 기간 하나가 아니라 짧은 기간과 긴 기간의 방향이 달라지는 것으로 본다:
  상승 유지(모두 +) → 약화 초입(장기·중기 +, 단기 −) → 조정 중 → 하락 전환 확인(장기선 이탈) → 하락 유지 → 반등 시도(장기 −, 단기 +) → 상승 전환 초입(장기선 회복).
거짓 신호를 줄이는 장치:
  - 버퍼(buffer_pct): 이동평균을 ±버퍼% 안에서 넘나든 것은 이탈·회복으로 세지 않는다(장기선 부근).
  - 확인 주수(confirm_weeks): 같은 상태가 그 주수만큼 연속 유지될 때만 '확인'으로 본다(그 전에는 미확정).
기본 기준은 DEFAULTS이고, 질문 하나에서만 도구 인자로 바꿀 수 있다(valid_overrides가 올바른 값만 받는다).
기간은 거래일 수다(1주=5, 1개월≈21, 3개월≈63, 6개월=126, 12개월≈252). 기준은 내가 정한 것이며 어떤 기준도 미래의 전환을 보장하지 않는다.
순수 계산이며 시세는 호출하는 쪽이 넘기고 다른 기능 모듈을 부르지 않는다.
"""
from __future__ import annotations

DEFAULTS = {"buffer_pct": 2.0, "confirm_weeks": 2, "short_days": 21, "mid_days": 63, "long_days": 252, "ma_fast": 50, "ma_slow": 200}
RANGES = {"buffer_pct": (0.0, 10.0), "confirm_weeks": (1, 8), "short_days": (5, 63), "mid_days": (21, 189), "long_days": (126, 504), "ma_fast": (10, 100), "ma_slow": (100, 300)}
INT_KEYS = ("confirm_weeks", "short_days", "mid_days", "long_days", "ma_fast", "ma_slow")
WEEK_DAYS = 5
HALF_YEAR_DAYS = 126
LOOKBACK_WEEKS = 12            # 상태가 얼마나 오래 유지됐는지(연속 주수)를 거슬러 올라가 보는 최대 주수
FOCUS_VALUES = ("all", "short", "mid", "long")

# 미국 SPDR 업종 ETF 11개(티커, 이름). 비교 기준은 SPY.
SECTOR_ETFS = [("XLK", "기술"), ("XLC", "커뮤니케이션"), ("XLY", "경기소비재"), ("XLF", "금융"), ("XLI", "산업재"), ("XLV", "헬스케어"),
               ("XLP", "필수소비재"), ("XLU", "유틸리티"), ("XLE", "에너지"), ("XLB", "소재"), ("XLRE", "부동산")]
BENCHMARK = ("SPY", "S&P500")

STATES = [   # (이름, 뜻) — 화면 범례와 요약 순서
    ("상승 유지", "단기·중기·장기 모두 상승, 장기선 위"),
    ("약화 초입", "장기·중기는 상승인데 단기가 꺾이거나 50일선을 이탈"),
    ("조정 중", "장기선 위·12개월은 상승이지만 3개월이 하락"),
    ("상승 전환 초입", "장기선을 회복했고 단기·중기가 상승(12개월은 아직 하락)"),
    ("장기선 회복 시도", "장기선 위로 올라왔지만 중기가 약함"),
    ("반등 시도", "장기선 아래에서 단기만 반등"),
    ("약세 속 혼조", "장기선 아래에서 단기·중기 방향이 엇갈림"),
    ("하락 전환 확인", "12개월은 아직 상승이지만 장기선을 이탈하고 단기·중기가 하락"),
    ("하락 유지", "장기선 아래, 12개월·중기·단기 모두 하락"),
    ("장기선 부근", "장기선 ±버퍼 안(방향 탐색)"),
]
STATE_NAMES = [s for s, _ in STATES]


def clean_settings(raw) -> dict:
    """설정 검증: 범위 안의 숫자만 받고, 이상한 값은 기본값으로. 단기 < 중기 < 장기가 되도록 보정한다."""
    out = dict(DEFAULTS)
    out.update(valid_overrides(raw))
    if not out["short_days"] < out["mid_days"] < out["long_days"]:
        out.update({k: DEFAULTS[k] for k in ("short_days", "mid_days", "long_days")})
    if out["ma_fast"] >= out["ma_slow"]:
        out.update({k: DEFAULTS[k] for k in ("ma_fast", "ma_slow")})
    return out


def valid_overrides(raw) -> dict:
    """질문 하나에서만 바꾸려는 설정 중 범위·형식이 올바른 것만 남긴다(올바르지 않은 값은 무시되어 기본 기준이 쓰인다)."""
    out = {}
    if isinstance(raw, dict):
        for key, (lo, hi) in RANGES.items():
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                continue
            if key in INT_KEYS and int(value) != value:
                continue
            out[key] = int(value) if key in INT_KEYS else float(value)
    return out


def _ret(closes: list[float], i: int, days: int) -> float | None:
    j = i - days
    return (closes[i] / closes[j] - 1) * 100 if j >= 0 and closes[j] > 0 else None


def metrics_at(closes: list[float], i: int, cfg: dict) -> dict | None:
    """i번째 거래일 시점의 수익률·이동평균 대비·전 고점 대비(받은 시세 전체 중 최고 종가, 즉 상장 이후 고점). 장기 수익률과 느린 이동평균을 계산할 만큼의 시세가 없으면 None."""
    need = max(cfg["long_days"], cfg["ma_slow"] - 1)
    if i < need or i >= len(closes):
        return None
    fast = sum(closes[i - cfg["ma_fast"] + 1:i + 1]) / cfg["ma_fast"]
    slow = sum(closes[i - cfg["ma_slow"] + 1:i + 1]) / cfg["ma_slow"]
    peak = max(closes[:i + 1])
    return {"close": closes[i], "r_week": _ret(closes, i, WEEK_DAYS), "r_short": _ret(closes, i, cfg["short_days"]), "r_mid": _ret(closes, i, cfg["mid_days"]),
            "r_half": _ret(closes, i, HALF_YEAR_DAYS), "r_long": _ret(closes, i, cfg["long_days"]),
            "dist_fast": (closes[i] / fast - 1) * 100, "dist_slow": (closes[i] / slow - 1) * 100, "from_high": (closes[i] / peak - 1) * 100}


def classify(m: dict, buffer_pct: float) -> str:
    """지표 → 상태 이름. 이동평균은 ±버퍼% 안이면 '부근'으로 보고 이탈·회복으로 세지 않는다."""
    b = buffer_pct
    long_up, mid_up, short_up = m["r_long"] > 0, m["r_mid"] > 0, m["r_short"] > 0
    above_slow, below_slow = m["dist_slow"] > b, m["dist_slow"] < -b
    fast_broken = m["dist_fast"] < -b
    if above_slow and long_up:
        if mid_up and short_up and not fast_broken:
            return "상승 유지"
        return "약화 초입" if mid_up else "조정 중"
    if above_slow:
        return "상승 전환 초입" if (mid_up and short_up) else "장기선 회복 시도"
    if below_slow:
        if short_up and not fast_broken:
            return "반등 시도"
        if not short_up and not mid_up:
            return "하락 전환 확인" if long_up else "하락 유지"
        return "약세 속 혼조"
    return "장기선 부근"


def trend_state(closes: list[float], cfg: dict, lookback_weeks: int = LOOKBACK_WEEKS) -> dict | None:
    """현재 상태와, 그 상태가 연속 몇 주 유지됐는지·직전 상태. 확인(confirmed)은 연속 주수가 confirm_weeks 이상일 때.
    시세가 모자라 현재 상태를 못 구하면 None. 과거 시점은 주 단위(5거래일 간격)로 거슬러 올라가며 구한다."""
    last = len(closes) - 1
    now = metrics_at(closes, last, cfg)
    if now is None:
        return None
    state = classify(now, cfg["buffer_pct"])
    streak, previous, previous_weeks_ago = 1, None, None
    for k in range(1, lookback_weeks + 1):
        m = metrics_at(closes, last - WEEK_DAYS * k, cfg)
        if m is None:
            break
        past = classify(m, cfg["buffer_pct"])
        if past == state and previous is None:
            streak += 1
        elif previous is None:
            previous, previous_weeks_ago = past, k
            break
    return {"state": state, "since_weeks": streak, "confirmed": streak >= cfg["confirm_weeks"], "previous_state": previous,
            "previous_weeks_ago": previous_weeks_ago, "metrics": now, "streak_capped": streak > lookback_weeks}


def _average_rank(rows: list[dict], keys: tuple) -> dict[str, float]:
    ranks = {r["symbol"]: [] for r in rows}
    for key in keys:
        usable = sorted((r for r in rows if r["metrics"].get(key) is not None), key=lambda r: r["metrics"][key], reverse=True)
        for pos, r in enumerate(usable, 1):
            ranks[r["symbol"]].append(pos)
    return {s: (sum(v) / len(v) if v else 99.0) for s, v in ranks.items()}


def analyze(series: dict[str, list[float]], names: dict[str, str], cfg: dict, benchmark: list[float] | None = None, focus: str = "all") -> dict:
    """여러 종목의 추세 사다리와 상태. series: {심볼: 종가 목록(오래된 것 → 최근)}, names: {심볼: 이름}.
    focus는 정렬 기준(보이는 순서)만 바꾼다: all=3·6·12개월 평균 순위, short=단기 수익률, mid=중기, long=장기. 상태 판정 기준은 항상 같다.
    반환: {"rows": [...], "groups": {상태: [심볼…]}, "changed": [최근 4주 안에 상태가 바뀐 종목], "insufficient": [...], "benchmark": {...}|None, "cfg", "focus"}."""
    focus = focus if focus in FOCUS_VALUES else "all"
    rows, insufficient = [], []
    for symbol, closes in series.items():
        got = trend_state(closes, cfg)
        if got is None:
            insufficient.append(symbol)
            continue
        rows.append({"symbol": symbol, "name": names.get(symbol, symbol), **got})
    bench = None
    if benchmark:
        bm = metrics_at(benchmark, len(benchmark) - 1, cfg)
        bench = {"name": BENCHMARK[1], "metrics": bm} if bm else None
    if bench:
        for r in rows:
            r["vs_benchmark_mid"] = (r["metrics"]["r_mid"] - bench["metrics"]["r_mid"]) if r["metrics"]["r_mid"] is not None and bench["metrics"]["r_mid"] is not None else None
    if focus == "all":
        avg = _average_rank(rows, ("r_mid", "r_half", "r_long"))
        rows.sort(key=lambda r: (avg[r["symbol"]], -(r["metrics"]["r_mid"] or 0)))
        for r in rows:
            r["rank_score"] = avg[r["symbol"]]
    else:
        key = {"short": "r_short", "mid": "r_mid", "long": "r_long"}[focus]
        rows.sort(key=lambda r: -(r["metrics"][key] if r["metrics"][key] is not None else -1e9))
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(r["state"], []).append(r["symbol"])
    changed = [{"symbol": r["symbol"], "name": r["name"], "from": r["previous_state"], "to": r["state"], "weeks_ago": r["since_weeks"], "confirmed": r["confirmed"]}
               for r in rows if r["previous_state"] and r["since_weeks"] <= 4]
    return {"rows": rows, "groups": groups, "changed": changed, "insufficient": insufficient, "benchmark": bench, "cfg": cfg, "focus": focus}
