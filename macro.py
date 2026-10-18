"""매크로 요인 분해: 종목의 최근 5거래일 등락을 금리·달러·유가·VIX·환율·지수 변화와 통계적으로 연결한다.

방법: 최근 약 250거래일의 일간 수익률을 후보 요인에 단계적으로(최대 3개) 회귀시켜 가장 잘 설명하는 요인을 고르고, 이번 주 요인
변화에 회귀계수(β)를 곱해 요인별 기여(%p)를 추정한다. 이것은 **같이 움직인 정도의 통계 추정**이지 인과의 증명이 아니다 — 설명력(R²)이
낮으면 그렇다고 표시하고, 종목 고유 이슈는 뉴스로 확인하게 한다. 외부 호출은 하지 않는다(순수 계산, 시세는 호출하는 쪽이 넘긴다).
"""
from __future__ import annotations

import math
from statistics import NormalDist, pstdev as statistics_pstdev

TRADING_WEEK = 5
TRAIN_DAYS = 250
MIN_OBSERVATIONS = 120
MAX_FACTORS = 3
MIN_R2_GAIN = 0.03          # 요인을 하나 더 넣을 때 설명력이 이만큼은 늘어야 한다
IDENTICAL_CORR = 0.97       # 종목과 거의 같은 움직임인 시리즈(예: 금 ETF의 금 선물)는 '요인'이 아니라 같은 것이라 제외
WEAK_R2 = 0.2               # 이보다 낮으면 "요인으로 거의 설명되지 않음"으로 표시한다
MIN_SHOWN_CONTRIBUTION = 0.3  # %p. 이보다 작은 기여는 '그 밖'에 합친다
MIN_T = 2.0                 # 회귀계수의 t값 절댓값이 이보다 작으면(우연과 구분 안 됨) 요인에서 뺀다. 후보가 많으면 아래 required_t로 더 엄격해진다
OOS_MIN_R2 = 0.05           # 검증 구간(뒤 1/3)에서 이 설명력은 나와야 '믿을 만함'으로 본다
TRAIN_SHARE = 2 / 3         # 앞 2/3로 계수를 구하고 뒤 1/3으로 검증한다
MIN_TRAIN, MIN_TEST = 60, 30

# 후보 요인: (야후 심볼, 짧은 이름, 종류, 오르면, 내리면). 종류 level=값의 차이(금리 %p), price=로그 수익률.
FACTORS = [
    # 미국 금리(값의 차이, bp) — 만기가 이웃한 금리는 서로 거의 같이 움직여 순위에서 대표 하나로 묶인다
    ("^IRX", "3개월 금리", "level", "상승", "하락"),
    ("^FVX", "5년물 금리", "level", "상승", "하락"),
    ("^TNX", "10년물 금리", "level", "상승", "하락"),
    ("^TYX", "30년물 금리", "level", "상승", "하락"),
    # 통화(달러 대비 가치, 로그 수익률)
    ("DX-Y.NYB", "달러", "price", "강세", "약세"),
    ("KRW=X", "원/달러", "price", "상승(원화 약세)", "하락(원화 강세)"),
    ("JPY=X", "달러/엔", "price", "상승(엔화 약세)", "하락(엔화 강세)"),
    ("CNY=X", "달러/위안", "price", "상승(위안 약세)", "하락(위안 강세)"),
    ("EURUSD=X", "유로/달러", "price", "상승(유로 강세)", "하락(유로 약세)"),
    ("GBPUSD=X", "파운드/달러", "price", "상승(파운드 강세)", "하락(파운드 약세)"),
    ("AUDUSD=X", "호주달러/달러(위험 선호 통화)", "price", "상승(호주달러 강세)", "하락(호주달러 약세)"),
    # 원자재
    ("CL=F", "유가(WTI)", "price", "상승", "하락"),
    ("NG=F", "천연가스", "price", "상승", "하락"),
    ("HG=F", "구리", "price", "상승", "하락"),
    ("GC=F", "금", "price", "상승", "하락"),
    ("SI=F", "은", "price", "상승", "하락"),
    # 위험·심리·변동성
    ("^VIX", "VIX", "price", "상승", "하락"),
    ("^MOVE", "채권 변동성(MOVE)", "price", "상승", "하락"),
    # 주가지수
    ("^GSPC", "S&P500", "price", "상승", "하락"),
    ("^IXIC", "나스닥", "price", "상승", "하락"),
    ("^RUT", "러셀2000(미국 중소형주)", "price", "상승", "하락"),
    ("^SOX", "반도체지수(SOX)", "price", "상승", "하락"),
    ("^KS11", "코스피", "price", "상승", "하락"),
    ("^KQ11", "코스닥", "price", "상승", "하락"),
    ("^N225", "닛케이225", "price", "상승", "하락"),
    ("^STOXX50E", "유로스톡스50", "price", "상승", "하락"),
    ("000001.SS", "상하이종합", "price", "상승", "하락"),
    ("^HSI", "항셍", "price", "상승", "하락"),
    ("EEM", "신흥국 주식(EEM)", "price", "상승", "하락"),
    # 미국 업종 ETF
    ("XLK", "기술 업종(XLK)", "price", "상승", "하락"),
    ("XLF", "금융 업종(XLF)", "price", "상승", "하락"),
    ("XLE", "에너지 업종(XLE)", "price", "상승", "하락"),
    ("XLV", "헬스케어 업종(XLV)", "price", "상승", "하락"),
    ("XLU", "유틸리티 업종(XLU, 방어주)", "price", "상승", "하락"),
    ("VNQ", "리츠(부동산, VNQ)", "price", "상승", "하락"),
    # 채권·신용·가상자산
    ("TIP", "물가연동채(TIP)", "price", "상승", "하락"),
    ("LQD", "투자등급 회사채(LQD)", "price", "상승", "하락"),
    ("HYG", "하이일드 채권(신용 위험 선호)", "price", "상승", "하락"),
    ("BTC-USD", "비트코인", "price", "상승", "하락"),
    ("ETH-USD", "이더리움", "price", "상승", "하락"),
]
# FRED 요인(사용자가 FRED 키를 넣었을 때만 시세가 들어온다). 심볼은 "FRED:<시리즈 ID>", 모두 값의 차이(level). 야후 요인(FACTORS)과 같은 모양.
FRED_FACTORS = [
    ("FRED:DFF", "연방기금금리", "level", "상승", "하락"),
    ("FRED:DGS2", "2년물 금리", "level", "상승", "하락"),
    ("FRED:DFII10", "10년 실질금리", "level", "상승", "하락"),
    ("FRED:T10YIE", "10년 기대인플레이션", "level", "상승", "하락"),
    ("FRED:T10Y2Y", "장단기 금리차(10년-2년)", "level", "확대", "축소"),
]
ALL_FACTORS = FACTORS + FRED_FACTORS

# 요인 순위에서 '매크로 요인'과 '함께 움직이는 비슷한 자산·시장'을 따로 낸다(같은 계열 자산이 매크로 요인을 가리지 않게).
# 매크로: 금리·통화·에너지와 산업용 원자재·변동성(VIX·MOVE)·FRED 지표. 그 밖(귀금속·주가지수·업종 ETF·채권/신용 ETF·가상자산·리츠)은 자산·시장 쪽이다.
PEER_SYMBOLS = frozenset({
    "GC=F", "SI=F", "^GSPC", "^IXIC", "^RUT", "^SOX", "^KS11", "^KQ11", "^N225", "^STOXX50E", "000001.SS", "^HSI", "EEM",
    "XLK", "XLF", "XLE", "XLV", "XLU", "VNQ", "TIP", "LQD", "HYG", "BTC-USD", "ETH-USD",
})


def factor_group(symbol: str) -> str:
    """요인의 계열: "peer"(비슷한 자산·시장) 또는 "macro"(매크로 요인)."""
    return "peer" if symbol in PEER_SYMBOLS else "macro"
FACTOR_BY_SYMBOL = {f[0]: f for f in ALL_FACTORS}


def daily_changes(series: dict, kind: str) -> dict:
    """{날짜: 종가} → {날짜: 전일 대비 변화}. level은 차이(금리 %p), price는 로그 수익률."""
    dates = sorted(series)
    out = {}
    for prev, cur in zip(dates, dates[1:]):
        a, b = series[prev], series[cur]
        if kind == "level":
            out[cur] = b - a
        elif a and b and a > 0 and b > 0:
            out[cur] = math.log(b / a)
    return out


def window_change(series: dict, kind: str, days: int = TRADING_WEEK, since=None) -> float | None:
    """최근 N거래일 변화(level: 값의 차이, price: 로그 수익률). since(날짜)를 주면 그 날짜(또는 그 전 마지막 거래일)부터 지금까지.
    데이터가 모자라거나 구간이 비면 None."""
    dates = sorted(series)
    if since is not None:
        start = [d for d in dates if d <= since]
        if not start or start[-1] == dates[-1]:
            return None
        a, b = series[start[-1]], series[dates[-1]]
    else:
        if len(dates) <= days:
            return None
        a, b = series[dates[-days - 1]], series[dates[-1]]
    if kind == "level":
        return b - a
    return math.log(b / a) if a and b and a > 0 and b > 0 else None


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(matrix)
    rows = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(rows[r][i]))
        if abs(rows[pivot][i]) < 1e-12:
            return None  # 요인들이 서로 거의 같아 풀 수 없다
        rows[i], rows[pivot] = rows[pivot], rows[i]
        for r in range(i + 1, n):
            f = rows[r][i] / rows[i][i]
            for c in range(i, n + 1):
                rows[r][c] -= f * rows[i][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (rows[i][n] - sum(rows[i][c] * x[c] for c in range(i + 1, n))) / rows[i][i]
    return x


def required_t(candidates: int, alpha: float = 0.05) -> float:
    """후보 요인이 많을수록 우연히 유의해 보이는 것이 늘어나므로(다중 비교), 후보 수로 나눈 유의수준(본페로니)에 해당하는 t 기준을 쓴다.
    후보 1~3개면 2.0 근처, 40개면 약 3.0. 표본이 250개 안팎이라 정규분포 근사를 쓴다."""
    return max(MIN_T, NormalDist().inv_cdf(1 - alpha / 2 / max(1, candidates)))


def _inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    """정사각 행렬의 역행렬(가우스 소거를 단위벡터마다 적용). 풀 수 없으면 None."""
    n = len(matrix)
    columns = []
    for j in range(n):
        col = _solve(matrix, [1.0 if i == j else 0.0 for i in range(n)])
        if col is None:
            return None
        columns.append(col)
    return [[columns[j][i] for j in range(n)] for i in range(n)]


def ols_detail(y: list[float], columns: list[list[float]]) -> dict | None:
    """절편 포함 최소제곱과 계수의 불확실성. 반환: {"beta": [절편, β1…], "r2", "se": [표준오차…], "t": [t값…], "n"} 또는 풀 수 없으면 None.
    표준오차는 보통의 가정(오차가 서로 독립·분산 일정) 기준이라 일간 금융 수익률에서는 다소 낙관적일 수 있다."""
    n, k = len(y), len(columns)
    rows = [[1.0] + [col[i] for col in columns] for i in range(n)]
    gram = [[sum(r[i] * r[j] for r in rows) for j in range(k + 1)] for i in range(k + 1)]
    rhs = [sum(r[i] * y[idx] for idx, r in enumerate(rows)) for i in range(k + 1)]
    beta = _solve(gram, rhs)
    if beta is None:
        return None
    mean = sum(y) / n
    sst = sum((v - mean) ** 2 for v in y)
    sse = sum((y[i] - sum(beta[j] * rows[i][j] for j in range(k + 1))) ** 2 for i in range(n))
    se, t = [None] * (k + 1), [None] * (k + 1)
    inverse = _inverse(gram)
    if inverse is not None and n > k + 1:
        sigma2 = sse / (n - k - 1)
        for j in range(k + 1):
            var = sigma2 * inverse[j][j]
            se[j] = math.sqrt(var) if var > 0 else None
            t[j] = beta[j] / se[j] if se[j] else None
    return {"beta": beta, "r2": (1 - sse / sst if sst else 0.0), "se": se, "t": t, "n": n}


def ols(y: list[float], columns: list[list[float]]) -> tuple[list[float], float] | None:
    """절편 포함 최소제곱. 반환: ([절편, β1, β2, …], R²) 또는 풀 수 없으면 None."""
    detail = ols_detail(y, columns)
    return (detail["beta"], detail["r2"]) if detail else None


def correlation(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa, sb = math.sqrt(sum((x - ma) ** 2 for x in a)), math.sqrt(sum((x - mb) ** 2 for x in b))
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb) if sa and sb else 0.0


def fit_factors(asset_changes: dict, factor_changes: dict[str, dict], train_days: int = TRAIN_DAYS,
                max_factors: int = MAX_FACTORS) -> dict | None:
    """종목 일간 수익률을 가장 잘 설명하는 요인(단계적 선택)과 회귀계수. 겹치는 거래일이 모자라면 None.
    반환: {"factors": [심볼…], "betas": [β…], "r2": R², "observations": n}."""
    shared = set(asset_changes)
    for changes in factor_changes.values():
        shared &= set(changes)
    dates = sorted(shared)[-train_days:]
    if len(dates) < MIN_OBSERVATIONS:
        return None
    y = [asset_changes[d] for d in dates]
    candidates = {}
    for symbol, changes in factor_changes.items():
        column = [changes[d] for d in dates]
        if abs(correlation(y, column)) < IDENTICAL_CORR:  # 거의 같은 시리즈는 제외
            candidates[symbol] = column
    chosen: list[str] = []
    best_r2, best_betas = 0.0, [0.0]
    for _ in range(max_factors):
        trials = []
        for symbol, column in candidates.items():
            if symbol in chosen:
                continue
            fit = ols(y, [candidates[c] for c in chosen] + [column])
            if fit:
                trials.append((fit[1], symbol, fit[0]))
        if not trials:
            break
        r2, symbol, betas = max(trials, key=lambda t: t[0])
        if r2 - best_r2 < MIN_R2_GAIN:
            break
        chosen.append(symbol)
        best_r2, best_betas = r2, betas
    t_values = [None] * len(chosen)
    min_t = required_t(len(candidates))
    while chosen:                                   # 우연과 구분되지 않는 요인(|t|가 후보 수에 따른 기준 미만)은 가장 약한 것부터 뺀다
        detail = ols_detail(y, [candidates[c] for c in chosen])
        if detail is None:
            break
        t_values = detail["t"][1:]
        weakest = min(range(len(chosen)), key=lambda j: abs(t_values[j]) if t_values[j] is not None else 0.0)
        if t_values[weakest] is not None and abs(t_values[weakest]) >= min_t:
            best_r2, best_betas = detail["r2"], detail["beta"]
            break
        del chosen[weakest]
    if not chosen:
        best_r2, best_betas, t_values = 0.0, [0.0], []
    oos_r2, stable = validate([candidates[c] for c in chosen], y) if chosen else (None, [])
    return {"factors": chosen, "betas": best_betas[1:], "r2": best_r2, "observations": len(dates),
            "t": t_values, "oos_r2": oos_r2, "stable": stable}


def validate(columns: list[list[float]], y: list[float]) -> tuple[float | None, list[bool]]:
    """같은 요인으로 (1) 앞 2/3로 계수를 구해 뒤 1/3을 얼마나 맞히는지(검증 설명력, 음수면 평균보다도 못 맞힘),
    (2) 앞·뒤 절반에서 따로 구한 계수의 부호가 같은지(안정성)를 본다. 표본이 모자라면 (None, 요인 수만큼 False)."""
    n = len(y)
    cut = int(n * TRAIN_SHARE)
    oos = None
    if cut >= MIN_TRAIN and n - cut >= MIN_TEST:
        train = ols_detail(y[:cut], [c[:cut] for c in columns])
        if train:
            b = train["beta"]
            mean = sum(y[:cut]) / cut
            sse = sum((y[i] - (b[0] + sum(b[j + 1] * columns[j][i] for j in range(len(columns))))) ** 2 for i in range(cut, n))
            sst = sum((y[i] - mean) ** 2 for i in range(cut, n))
            oos = 1 - sse / sst if sst else None
    half = n // 2
    first, second = ols_detail(y[:half], [c[:half] for c in columns]), ols_detail(y[half:], [c[half:] for c in columns])
    stable = [bool(first and second and first["beta"][j + 1] * second["beta"][j + 1] > 0) for j in range(len(columns))]
    return oos, stable


def attribute(asset_series: dict, factor_series: dict[str, dict], fit: dict, days: int = TRADING_WEEK, since=None) -> dict | None:
    """이번 주(최근 N거래일) 등락을 요인별 기여(%p)와 그 밖(잔차)으로 나눈다. 로그 수익률 기준이라 합이 정확히 맞는다."""
    total = window_change(asset_series, "price", days, since)
    if total is None:
        return None
    contributions, explained = [], 0.0
    t_values, stable = fit.get("t") or [], fit.get("stable") or []
    for idx, (symbol, beta) in enumerate(zip(fit["factors"], fit["betas"])):
        _, label, kind, up, down = FACTOR_BY_SYMBOL[symbol]
        change = window_change(factor_series[symbol], kind, days, since)
        if change is None:
            continue
        value = beta * change
        explained += value
        contributions.append({"symbol": symbol, "label": label, "kind": kind, "direction": "up" if change >= 0 else "down",
                              "phrase": up if change >= 0 else down, "beta": beta,
                              "factor_change": (math.exp(change) - 1) * 100 if kind == "price" else change * 100,  # price: %, level: bp
                              "contribution_pct": value * 100,
                              "t": t_values[idx] if idx < len(t_values) else None, "stable": stable[idx] if idx < len(stable) else None})
    contributions.sort(key=lambda c: abs(c["contribution_pct"]), reverse=True)
    return {"asset_change_pct": (math.exp(total) - 1) * 100, "log_change_pct": total * 100, "contributions": contributions,
            "residual_pct": (total - explained) * 100}


def explain_asset(asset_series: dict, factor_series: dict[str, dict], days: int = TRADING_WEEK, since=None) -> dict:
    """한 종목의 요인 분해 결과(기본은 이번 주, days로 더 긴 구간 — 예: 고점 이후). 계수는 같은 최근 1년 일간 관계에서 구한다. 데이터가 모자라거나 설명력이 낮으면 그 사실을 상태로 알린다.
    status: ok | weak(설명력 낮음) | no_data(데이터 부족)."""
    asset_changes = daily_changes(asset_series, "price")
    factor_changes = {s: daily_changes(series, FACTOR_BY_SYMBOL[s][2]) for s, series in factor_series.items() if s in FACTOR_BY_SYMBOL}
    weekly = window_change(asset_series, "price", days, since)
    if weekly is None:
        return {"status": "no_data", "asset_change_pct": None}
    result = {"asset_change_pct": (math.exp(weekly) - 1) * 100, "r2": None, "oos_r2": None, "reason": "", "observations": 0, "contributions": [], "residual_pct": None}
    if not factor_changes:
        return {**result, "status": "no_data"}
    fit = fit_factors(asset_changes, factor_changes)
    if fit is None:
        return {**result, "status": "no_data"}
    attribution = attribute(asset_series, factor_series, fit, days, since)
    if attribution is None:
        return {**result, "status": "no_data"}
    explained_in_sample = bool(fit["factors"]) and fit["r2"] >= WEAK_R2
    holds_out = fit.get("oos_r2") is None or fit["oos_r2"] >= OOS_MIN_R2
    status = "ok" if explained_in_sample and holds_out else "weak"
    reason = "" if status == "ok" else ("low_r2" if not explained_in_sample else "unstable")   # unstable: 표본 안 설명력은 있으나 검증 구간에서는 못 맞힘
    return {**result, "status": status, "reason": reason, "r2": fit["r2"], "oos_r2": fit.get("oos_r2"), "observations": fit["observations"],
            "contributions": attribution["contributions"], "residual_pct": attribution["residual_pct"]}


PRESSURE_MIN_CORR = 0.2      # 최근 1년 일간 상관이 이보다 약하면 '평소 같이 움직이는 요인'으로 보지 않는다
PRESSURE_MIN_SIGMA = 1.5     # 구간 변화가 평소 일간 변동 × √(거래일 수)의 이 배수 이상이어야 '크게 움직인 요인'


def window_pressure(asset_series: dict, factor_series: dict[str, dict], since, exclude=(), train_days: int = TRAIN_DAYS) -> list[dict]:
    """회귀(여러 요인 동시 선택)에 안 잡혔더라도, 평소 이 종목과 같이 움직이던 요인이 since(날짜) 이후 크게 움직였는지 단순 상관으로 본다.
    압력 방향 = 상관 부호 × 요인 변화 부호(음수면 종목에 하락 쪽, 양수면 상승 쪽). 큰 순. 같이 움직인 정도일 뿐 원인의 증명이 아니다.
    반환: [{"symbol","label","change"(가격 %, 금리 bp),"unit","phrase","corr","sigma","direction": "down"|"up"}]"""
    asset_changes = daily_changes(asset_series, "price")
    out = []
    for symbol, series in factor_series.items():
        if symbol in exclude or symbol not in FACTOR_BY_SYMBOL:
            continue
        _s, label, kind, up, down = FACTOR_BY_SYMBOL[symbol]
        changes = daily_changes(series, kind)
        shared = sorted(set(asset_changes) & set(changes))[-train_days:]
        if len(shared) < MIN_OBSERVATIONS:
            continue
        x = [changes[d] for d in shared]
        corr = correlation([asset_changes[d] for d in shared], x)
        change = window_change(series, kind, since=since)
        days = len([d for d in changes if d > since])
        mean = sum(x) / len(x)
        sd = math.sqrt(sum((v - mean) ** 2 for v in x) / (len(x) - 1))
        if change is None or days <= 0 or sd <= 0 or abs(corr) < PRESSURE_MIN_CORR:
            continue
        sigma = change / (sd * math.sqrt(days))
        if abs(sigma) < PRESSURE_MIN_SIGMA:
            continue
        shown = change * 100 if kind == "level" else (math.exp(change) - 1) * 100
        out.append({"symbol": symbol, "label": label, "change": shown, "unit": "bp" if kind == "level" else "%", "phrase": up if change >= 0 else down,
                    "corr": corr, "sigma": sigma, "direction": "down" if corr * sigma < 0 else "up"})
    out.sort(key=lambda m: abs(m["corr"] * m["sigma"]), reverse=True)
    return out


def weekly_factor_moves(factor_series: dict[str, dict], days: int = TRADING_WEEK, since=None) -> list[dict]:
    """최근 N거래일(기본 이번 주) 시장 요인 변화(표시용). 금리는 bp, 나머지는 %."""
    moves = []
    for symbol, label, kind, up, down in ALL_FACTORS:
        series = factor_series.get(symbol)
        change = window_change(series, kind, days, since) if series else None
        if change is None:
            continue
        value = change * 100 if kind == "level" else (math.exp(change) - 1) * 100
        moves.append({"symbol": symbol, "label": label, "kind": kind, "change": value,
                      "unit": "bp" if kind == "level" else "%", "phrase": up if change >= 0 else down})
    return moves

# ---------------------------------------------------------------------------
# 평소에 어떤 요인에 가장 민감한가(순위)
# ---------------------------------------------------------------------------
RANK_WINDOWS = (("1년", 250), ("3년", 750), ("10년", 2500))
MIN_WINDOW_SHARE = 0.8        # 창(window)의 80% 이상 관측값이 있어야 그 창의 상관을 계산한다(10년 창은 시세가 그만큼 길 때만)
MIN_WINDOW_OBS = 120
CONSISTENT_MIN_CORR = 0.10    # 모든 창에서 이 이상의 상관이 같은 방향이어야 '일관됨'
INCONSISTENT_PENALTY = 0.5    # 일관되지 않은 요인의 순위 점수에 곱한다
REDUNDANT_CORR = 0.7          # 이미 뽑은 요인과 이만큼 같이 움직이는 요인은 대표 하나로 묶는다(5·10·30년물 금리 등)
REDUNDANCY_DAYS = 750


def _window_corr(a: dict, b: dict, days: int) -> float | None:
    shared = sorted(set(a) & set(b))[-days:]
    if len(shared) < max(MIN_WINDOW_OBS, int(days * MIN_WINDOW_SHARE)):
        return None
    return correlation([a[d] for d in shared], [b[d] for d in shared])


def mid_corr(corr: dict, windows) -> float:
    """반응 크기에 쓸 상관: 가운데 창(3년)이 있으면 그것, 없으면 가장 긴 창부터 있는 것."""
    middle = corr.get(windows[1][0]) if len(windows) > 1 else None
    if middle is not None:
        return middle
    return next(c for c in (corr.get(label) for label, _d in reversed(windows)) if c is not None)


def rank_drivers(asset_series: dict, factor_series: dict[str, dict], top: int = 3, windows=RANK_WINDOWS) -> dict:
    """이 자산이 평소 가장 민감한 요인 순위. 요인마다 1년·3년·10년 일간 상관을 구해 모든 창에서 같은 방향인지(일관성)와 평균 상관 크기로 순위를 매기고,
    서로 거의 같이 움직이는 요인은 대표 하나로 묶는다. 같이 움직인 정도이며 원인의 증명이 아니다. 시세가 모자라면 status=no_data.
    매크로 요인(drivers, 금리·통화·원자재·변동성)과 함께 움직이는 비슷한 자산·시장(peers, 귀금속·주가지수·업종·채권 ETF·가상자산)은 따로 순위를 낸다.
    반환: {"status", "drivers": [...], "peers": [...], "others": 순위 밖 매크로 요인, "model": {…}|None, "asset_vol_1m_pct"}; 각 항목 {symbol,label,kind,up,down,corr:{창:값},score,consistent,reaction_1m_pct,absorbed:[이름]}"""
    asset_ch = daily_changes(asset_series, "price")
    if len(asset_ch) < MIN_WINDOW_OBS:
        return {"status": "no_data"}
    fac_ch = {s: daily_changes(series, FACTOR_BY_SYMBOL[s][2]) for s, series in factor_series.items() if s in FACTOR_BY_SYMBOL}
    recent = sorted(asset_ch)[-REDUNDANCY_DAYS:]
    sigma_daily = statistics_pstdev([asset_ch[d] for d in recent])
    vol_1m = sigma_daily * math.sqrt(21)
    rows = []
    for symbol, ch in fac_ch.items():
        corr = {label: _window_corr(asset_ch, ch, days) for label, days in windows}
        known = [c for c in corr.values() if c is not None]
        if not known or max(abs(c) for c in known) >= IDENTICAL_CORR:      # 시세가 없거나, 자산과 사실상 같은 시리즈(같은 자산의 지수·선물)는 요인이 아니다
            continue
        signs = {c > 0 for c in known}
        consistent = len(signs) == 1 and min(abs(c) for c in known) >= CONSISTENT_MIN_CORR
        score = sum(abs(c) for c in known) / len(known)
        _, label, kind, up, down = FACTOR_BY_SYMBOL[symbol]
        rows.append({"symbol": symbol, "label": label, "kind": kind, "up": up, "down": down, "corr": corr, "consistent": consistent,
                     "score": score * (1.0 if consistent else INCONSISTENT_PENALTY), "raw_score": score,
                     "reaction_1m_pct": (math.exp(mid_corr(corr, windows) * vol_1m) - 1) * 100})   # 요인이 1표준편차 움직일 때 자산의 평균 반응 = 상관 × 자산의 1개월 변동폭
    rows.sort(key=lambda r: r["score"], reverse=True)

    def pick(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
        chosen, rest = [], []
        for row in candidates:
            ch = fac_ch[row["symbol"]]
            twin = next((c for c in chosen if abs(_window_corr(fac_ch[c["symbol"]], ch, REDUNDANCY_DAYS) or 0.0) >= REDUNDANT_CORR), None)
            if twin is not None:
                twin.setdefault("absorbed", []).append(row["label"])
            elif len(chosen) < top:
                chosen.append(row)
            else:
                rest.append(row)
        return chosen, rest
    chosen, others = pick([r for r in rows if factor_group(r["symbol"]) == "macro"])
    peers, _rest = pick([r for r in rows if factor_group(r["symbol"]) == "peer"])
    fit = fit_factors(asset_ch, fac_ch)
    model = None
    if fit and fit["factors"]:
        model = {"factors": [FACTOR_BY_SYMBOL[s][1] for s in fit["factors"]], "r2": fit["r2"], "oos_r2": fit.get("oos_r2")}
    return {"status": "ok", "drivers": chosen, "peers": peers, "others": others[:3], "model": model, "asset_vol_1m_pct": (math.exp(vol_1m) - 1) * 100,
            "windows": [label for label, _d in windows]}
