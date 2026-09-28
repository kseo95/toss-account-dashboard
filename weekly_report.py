"""주간 리포트.

지수/금리/VIX/환율 과거 데이터는 토스 API에 없어서 전부 Yahoo Finance(비공식) 일봉으로 계산한다.
종목 묶음(그룹)과 종목 목록은 weekly_report.json에 저장하고 대시보드 설정 패널에서 편집한다.
자동 코멘트는 숫자에서 바로 나오는 문장만 만든다(해석·전망은 넣지 않음).
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

from common import MA_MAX_PERIOD, fmt_as_of, parallel_map
from storage import UserStore

WEEKLY_REPORT_FILE = "weekly_report.json"
WEEKLY_REPORT_GROUP_TYPES = {"basic", "sector", "dividend", "risk", "fx_account", "my_account"}  # my_account: 종목은 토스 보유종목에서 자동
WEEKLY_REPORT_ITEM_KINDS = {"price", "rate", "level"}  # rate: 금리(%p 변화), level: 환율·공포지수(고점 대비 대신 52주 위치)
WEEKLY_REPORT_MAX_GROUPS = 12
WEEKLY_REPORT_MAX_ITEMS = 40
WEEKLY_REPORT_MAX_TOTAL = 150
WEEKLY_REPORT_MAX_MA = 6
# 자동 코멘트 기준값 (설정 패널에서 편집). near_ma_pct: 최근접 MA가 ±이 % 안이면 "시험 중",
# calm_position_pct: 공포지수 52주 위치가 이 % 이하면 "평온", sector_top_n: 강세/약세로 뽑을 개수.
WEEKLY_REPORT_COMMENT_DEFAULTS = {"near_ma_pct": 1.0, "calm_position_pct": 20.0, "sector_top_n": 2}

YAHOO_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.^=\-]{1,20}$")
YAHOO_HISTORY_CACHE_TTL = 30 * 60  # 초. 같은 종목 일봉을 30분 안에 다시 받지 않는다.
WEEKLY_REPORT_CACHE_TTL = 30 * 60
YAHOO_HISTORY_YEARS = 6  # 240주 MA(약 4.6년)를 계산할 수 있을 만큼
WEEKLY_REPORT_MAX_WORKERS = 8


def default_weekly_report_config() -> dict:
    """처음 쓰는 사람용 기본 구성. 실제 값은 전부 weekly_report.json + 설정 패널에서 바꾼다."""
    def items(pairs, kind="price"):
        return [{"symbol": s, "label": l, "kind": kind} for s, l in pairs]

    return {
        "ma_periods": [60, 120, 200, 240],
        "comment_rules": dict(WEEKLY_REPORT_COMMENT_DEFAULTS),
        "benchmark": "^GSPC",
        "rate_symbol": "^TNX",
        "fx_symbol": "KRW=X",
        "groups": [
            {"title": "지수·장기채", "type": "basic", "items":
                items([("^DJI", "다우"), ("^GSPC", "S&P 500"), ("^NDX", "나스닥 100"), ("TLT", "TLT")])
                + items([("^TNX", "10년물 금리"), ("^TYX", "30년물 금리")], "rate")},
            {"title": "원자재", "type": "basic", "items": items([("CL=F", "WTI 원유")])},
            {"title": "섹터 (SPDR)", "type": "sector", "items": items([
                ("XLK", "기술"), ("XLC", "커뮤니케이션"), ("XLV", "헬스케어"), ("XLI", "산업재"),
                ("XLB", "소재"), ("XLY", "임의소비재"), ("XLP", "필수소비재"), ("XLF", "금융"),
                ("XLRE", "부동산"), ("XLE", "에너지"), ("XLU", "유틸리티")])},
            {"title": "빅테크", "type": "sector", "items": items([
                ("AAPL", "애플"), ("MSFT", "마이크로소프트"), ("NVDA", "엔비디아"), ("GOOGL", "알파벳"),
                ("AMZN", "아마존"), ("META", "메타"), ("TSLA", "테슬라"), ("AVGO", "브로드컴")])},
            {"title": "고배당주", "type": "dividend", "items": items([
                ("JEPI", "JEPI"), ("MO", "알트리아"), ("PFE", "화이자"), ("VZ", "버라이즌"), ("O", "리얼티인컴"),
                ("PEP", "펩시코"), ("T", "AT&T"), ("CVX", "셰브론"), ("SCHD", "SCHD"), ("PG", "P&G"),
                ("ABBV", "애브비"), ("XOM", "엑슨모빌"), ("KO", "코카콜라"), ("VYM", "VYM"), ("JNJ", "존슨앤존슨")])},
            {"title": "S&P 500 + 환율 = 원화 계좌", "type": "fx_account", "items":
                items([("^GSPC", "S&P 500")]) + items([("KRW=X", "원/달러")], "level")},
            {"title": "내 계좌", "type": "my_account", "items": []},
            {"title": "위험 신호 (신용·변동성)", "type": "risk", "items":
                items([("^VIX", "VIX (주식 공포 30일)"), ("^VIX3M", "VIX3M (주식 공포 3개월)"),
                       ("^MOVE", "MOVE (채권 공포)")], "level")
                + items([("HYG", "HYG (하이일드)"), ("BKLN", "BKLN (레버리지론)"), ("BIZD", "BIZD (사모대출 BDC)"),
                         ("ORCL", "ORCL (AI 부채)"), ("CRWV", "CRWV (네오클라우드)")])},
        ],
    }


def _normalize_weekly_report_config(data) -> dict:
    """설정 검증 + 정규화. 잘못되면 ValueError(사용자에게 그대로 보여줄 메시지)."""
    if not isinstance(data, dict):
        raise ValueError("설정 형식이 잘못되었습니다.")

    periods = data.get("ma_periods")
    if not isinstance(periods, list) or not 1 <= len(periods) <= WEEKLY_REPORT_MAX_MA:
        raise ValueError(f"MA 기간은 1~{WEEKLY_REPORT_MAX_MA}개여야 합니다.")
    clean_periods: list[int] = []
    for p in periods:
        if isinstance(p, bool) or not isinstance(p, (int, float)) or int(p) != p or not 2 <= p <= MA_MAX_PERIOD["week"]:
            raise ValueError(f"MA 기간은 2~{MA_MAX_PERIOD['week']} 사이 정수여야 합니다.")
        if int(p) in clean_periods:
            raise ValueError(f"MA 기간이 중복되었습니다: {int(p)}")
        clean_periods.append(int(p))
    clean_periods.sort()

    def _sym(v, what):
        if not isinstance(v, str) or not YAHOO_SYMBOL_PATTERN.fullmatch(v.strip()):
            raise ValueError(f"{what} 심볼이 잘못되었습니다: {v!r}")
        return v.strip().upper()

    benchmark = _sym(data.get("benchmark"), "비교 기준")
    rate_symbol = _sym(data.get("rate_symbol"), "배당 비교 금리")
    fx_symbol = _sym(data.get("fx_symbol") or "KRW=X", "원화 환산 환율")  # 예전 파일엔 없음 → 기본값

    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("그룹이 하나 이상 있어야 합니다.")
    if len(groups) > WEEKLY_REPORT_MAX_GROUPS:
        raise ValueError(f"그룹은 최대 {WEEKLY_REPORT_MAX_GROUPS}개까지 만들 수 있습니다.")
    clean_groups = []
    total = 0
    for g in groups:
        if not isinstance(g, dict):
            raise ValueError("그룹 형식이 잘못되었습니다.")
        title = str(g.get("title") or "").strip()
        if not title or len(title) > 40:
            raise ValueError("그룹 이름은 1~40자여야 합니다.")
        gtype = g.get("type")
        if gtype not in WEEKLY_REPORT_GROUP_TYPES:
            raise ValueError(f"'{title}' 그룹의 표 종류가 잘못되었습니다.")
        raw_items = g.get("items")
        if not isinstance(raw_items, list):
            raise ValueError(f"'{title}' 그룹의 종목 목록이 잘못되었습니다.")
        if len(raw_items) > WEEKLY_REPORT_MAX_ITEMS:
            raise ValueError(f"'{title}' 그룹은 종목이 최대 {WEEKLY_REPORT_MAX_ITEMS}개까지 가능합니다.")
        items = []
        seen = set()
        for it in raw_items:
            if not isinstance(it, dict):
                raise ValueError(f"'{title}' 그룹의 종목 형식이 잘못되었습니다.")
            sym = _sym(it.get("symbol"), f"'{title}' 그룹의 종목")
            if sym in seen:
                raise ValueError(f"'{title}' 그룹에 {sym}이(가) 중복되었습니다.")
            seen.add(sym)
            label = str(it.get("label") or "").strip() or sym
            if len(label) > 40:
                raise ValueError(f"{sym}의 표시 이름은 40자 이하여야 합니다.")
            kind = it.get("kind", "price")
            if kind not in WEEKLY_REPORT_ITEM_KINDS:
                raise ValueError(f"{sym}의 값 종류가 잘못되었습니다.")
            items.append({"symbol": sym, "label": label, "kind": kind})
        if gtype == "my_account" and items:
            raise ValueError(f"'{title}'(내 계좌) 그룹은 종목을 직접 넣지 않습니다 — 토스 보유종목으로 자동 채워집니다.")
        if gtype == "my_account" and any(cg["type"] == "my_account" for cg in clean_groups):
            raise ValueError("내 계좌 그룹은 하나만 만들 수 있습니다.")
        if gtype == "fx_account" and len(items) != 2:
            raise ValueError(f"'{title}'(원화 계좌 환산) 그룹은 [주가 지수, 환율] 정확히 2개 종목이어야 합니다.")
        total += len(items)
        clean_groups.append({"title": title, "type": gtype, "items": items})
    if total > WEEKLY_REPORT_MAX_TOTAL:
        raise ValueError(f"전체 종목은 최대 {WEEKLY_REPORT_MAX_TOTAL}개까지 가능합니다.")

    rules = dict(WEEKLY_REPORT_COMMENT_DEFAULTS)
    raw_rules = data.get("comment_rules")
    if raw_rules is not None:  # 예전에 저장된 파일엔 없을 수 있음 → 기본값
        if not isinstance(raw_rules, dict):
            raise ValueError("코멘트 기준 형식이 잘못되었습니다.")
        limits = {"near_ma_pct": (0, 20), "calm_position_pct": (0, 100), "sector_top_n": (0, 10)}
        for k, (lo, hi) in limits.items():
            if k not in raw_rules:
                continue
            v = raw_rules[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"코멘트 기준 '{k}'는 {lo}~{hi} 사이 숫자여야 합니다.")
            rules[k] = int(v) if k == "sector_top_n" else float(v)

    return {"ma_periods": clean_periods, "comment_rules": rules, "benchmark": benchmark,
            "rate_symbol": rate_symbol, "fx_symbol": fx_symbol, "groups": clean_groups}


def _weekly_report_symbols(config: dict) -> set[str]:
    s = {config["benchmark"], config["rate_symbol"], config["fx_symbol"]}
    for g in config["groups"]:
        s.update(it["symbol"] for it in g["items"])
    return s


def load_weekly_report_config(store: UserStore) -> dict:
    """파일이 없거나 깨졌으면 기본 구성을 쓴다(파일은 저장할 때만 생김)."""
    try:
        return _normalize_weekly_report_config(store.read_json(WEEKLY_REPORT_FILE))
    except ValueError:
        return default_weekly_report_config()


def save_weekly_report_config(store: UserStore, config: dict) -> None:
    store.write_json(WEEKLY_REPORT_FILE, config)
    with _weekly_report_cache_lock:
        _weekly_report_cache.clear()


_yahoo_history_cache: dict[str, dict] = {}
_yahoo_history_lock = threading.Lock()
_weekly_report_cache: dict[str, dict] = {}
_weekly_report_cache_lock = threading.Lock()


def get_yahoo_daily_history(symbol: str, force: bool = False) -> dict:
    """Yahoo 일봉 {bars: [(date, close, adjclose)], divs: [(date, amount)], name}.

    날짜는 거래소 현지 기준(gmtoffset 반영) — 안 그러면 KRW=X처럼 UTC 전날 23시에 찍히는 종목이
    하루씩 밀려서 주봉 묶음이 틀어진다.
    """
    now = time.time()
    if not force:
        with _yahoo_history_lock:
            cached = _yahoo_history_cache.get(symbol)
            if cached and now - cached["ts"] < YAHOO_HISTORY_CACHE_TTL:
                return cached["data"]

    resp = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
        # range=max는 월봉으로 내려오므로 period1/period2로 일봉을 받는다.
        params={"period1": int(now - YAHOO_HISTORY_YEARS * 366 * 86400), "period2": int(now),
                "interval": "1d", "events": "div"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    result = (resp.json().get("chart") or {}).get("result")
    if not result:
        raise ValueError(f"{symbol}: 데이터 없음")
    r = result[0]
    meta = r.get("meta") or {}
    offset = int(meta.get("gmtoffset") or 0)
    indicators = r.get("indicators") or {}
    closes = (indicators.get("quote") or [{}])[0].get("close") or []
    adj = (indicators.get("adjclose") or [{}])[0].get("adjclose") or closes

    by_date: dict[date, tuple] = {}
    for t, c, a in zip(r.get("timestamp") or [], closes, adj):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + offset, tz=timezone.utc).date()
        by_date[d] = (d, float(c), float(a if a is not None else c))
    bars = [by_date[d] for d in sorted(by_date)]
    if len(bars) < 2:
        raise ValueError(f"{symbol}: 데이터 부족")
    divs = [
        (datetime.fromtimestamp(v["date"] + offset, tz=timezone.utc).date(), float(v["amount"]))
        for v in ((r.get("events") or {}).get("dividends") or {}).values()
        if v.get("date") and v.get("amount") is not None
    ]
    data = {"bars": bars, "divs": divs, "name": meta.get("longName") or meta.get("shortName") or symbol}
    with _yahoo_history_lock:
        _yahoo_history_cache[symbol] = {"ts": now, "data": data}
    return data


def report_week_end(now_ny: datetime) -> date:
    """리포트 기준일 = 마지막으로 '끝난' 주의 금요일. 미국 동부시간으로 판단한다 —
    한국 날짜로 보면 토요일 새벽(미국 금요일 장중)에도 이번 주가 끝난 것으로 잘못 잡힌다.
    금요일 17시(ET) 이후나 주말이면 이번 주 금요일, 그 전이면 지난주 금요일."""
    d = now_ny.date()
    wd = d.weekday()
    if wd >= 5 or (wd == 4 and now_ny.hour >= 17):
        return d - timedelta(days=wd - 4)
    return d - timedelta(days=wd + 3)


def _weekly_series(bars: list[tuple], idx: int) -> list[float]:
    """일봉을 ISO 주 단위로 묶어 주의 마지막 거래일 값을 쓴다(fetch_weekly_closes와 같은 방식)."""
    weeks: dict[tuple, float] = {}
    for b in bars:
        weeks[b[0].isocalendar()[:2]] = b[idx]
    return [weeks[k] for k in sorted(weeks)]


def compute_symbol_week(hist: dict, end: date, periods: list[int]) -> dict:
    bars = [b for b in hist["bars"] if b[0] <= end]
    w = _weekly_series(bars, 1)
    wa = _weekly_series(bars, 2)
    if len(w) < 2:
        raise ValueError("주봉 데이터 부족")
    cur, prev = w[-1], w[-2]
    year = [b[1] for b in bars if b[0] > end - timedelta(days=365)]
    hi, lo = max(year), min(year)

    mas = []
    for p in periods:
        if len(w) < p:
            continue
        ma = sum(w[-p:]) / p
        item = {"period": p, "value": ma, "dist_pct": (cur / ma - 1) * 100, "cross": None}
        if len(w) > p:
            prev_ma = sum(w[-p - 1:-1]) / p
            if prev < prev_ma and cur >= ma:
                item["cross"] = "up"
            elif prev >= prev_ma and cur < ma:
                item["cross"] = "down"
        mas.append(item)
    order = [m["period"] for m in sorted(mas, key=lambda m: -m["value"])]  # 차트 위 → 아래
    broken = [m["period"] for m in sorted(mas, key=lambda m: -m["value"]) if m["value"] > cur]
    nearest = min(mas, key=lambda m: abs(m["dist_pct"])) if mas else None

    slope = None
    p0 = periods[0]
    if len(w) >= p0 + 4:
        now_ma = sum(w[-p0:]) / p0
        old_ma = sum(w[-p0 - 4:-4]) / p0
        slope = (now_ma / old_ma - 1) * 100

    ttm = sum(a for d, a in hist["divs"] if end - timedelta(days=365) < d <= end)
    return {
        "name": hist["name"],
        "last_date": bars[-1][0].isoformat(),
        "stale": bars[-1][0] < end - timedelta(days=6),  # 이번 주 거래 기록이 없음
        "close": cur,
        "change_pct": (cur / prev - 1) * 100,
        "change_pt": cur - prev,
        "total_return_pct": (wa[-1] / wa[-2] - 1) * 100,
        "drawdown_52w_pct": (cur / hi - 1) * 100,
        "position_52w_pct": (cur - lo) / (hi - lo) * 100 if hi > lo else None,
        "mas": mas,
        "ma_order": order,
        "ma_alignment": "up" if order == sorted(order) else ("down" if order == sorted(order, reverse=True) else None),
        "broken": broken,
        "nearest": {"period": nearest["period"], "dist_pct": nearest["dist_pct"]} if nearest else None,
        "slope_pct": slope,
        "slope_period": p0,
        "dividend_ttm": ttm,
        "dividend_yield_pct": ttm / cur * 100 if ttm else None,
        "weeks": len(w),
    }


def _fmt_signed(v: float, suffix: str = "%") -> str:
    return f"{v:+.2f}{suffix}"


def build_group_comments(group: dict, rules: dict, bench_symbol: str, rate: dict | None) -> list[str]:
    """숫자에서 바로 나오는 문장만 만든다(해석·전망은 넣지 않음)."""
    rows = [r for r in group["rows"] if "error" not in r]
    out: list[str] = []
    gtype = group["type"]

    if gtype == "sector" and rows and rules["sector_top_n"] > 0:
        n = rules["sector_top_n"]
        ranked = sorted(rows, key=lambda r: -r["rel_pct"]) if all(r["rel_pct"] is not None for r in rows) else []
        if len(ranked) > n:
            top = ", ".join(f"{r['label']} {_fmt_signed(r['rel_pct'], '%p')}" for r in ranked[:n] if r["rel_pct"] > 0)
            bottom = ", ".join(f"{r['label']} {_fmt_signed(r['rel_pct'], '%p')}" for r in ranked[::-1][:n] if r["rel_pct"] < 0)
            if top:
                out.append(f"{bench_symbol} 대비 강세: {top}")
            if bottom:
                out.append(f"{bench_symbol} 대비 약세: {bottom}")

    if gtype == "dividend" and rate:
        with_yield = [r for r in rows if r["spread_pct"] is not None]
        below = [r for r in with_yield if r["spread_pct"] < 0]
        if with_yield:
            out.append(f"{len(with_yield)}개 중 {len(below)}개가 {rate['symbol']}({rate['value']:.2f}%)보다 배당률이 낮음")

    if gtype == "fx_account" and group.get("account_pct") is not None and len(rows) == 2:
        a, b = rows[0]["change_pct"], rows[1]["change_pct"]
        acc = group["account_pct"]
        if a * b < 0:
            out.append(f"{rows[0]['label']} {_fmt_signed(a)}였지만 {rows[1]['label']} {_fmt_signed(b)}로 원화 기준 {_fmt_signed(acc)}")
        elif a != 0 and b != 0:
            out.append(f"{rows[0]['label']}와 {rows[1]['label']}가 같은 방향 → 원화 기준 {_fmt_signed(acc)}로 {'확대' if abs(acc) > abs(a) else '축소'}")

    price = [r for r in rows if r["kind"] == "price" and len(r["mas"]) >= 2]
    down = [r["label"] for r in price if r["ma_alignment"] == "down" and len(r["broken"]) == len(r["mas"])]
    up = [r["label"] for r in price if r["ma_alignment"] == "up" and not r["broken"]]
    if down:
        out.append(f"하락 추세 (역배열 + 모든 선 아래) {len(down)}/{len(rows)}: {', '.join(down)}")
    if up:
        out.append(f"상승 추세 (정배열 + 모든 선 위) {len(up)}/{len(rows)}: {', '.join(up)}")

    last = [f"{r['label']} — {r['ma_order'][-1]}주선" for r in price if len(r["broken"]) == len(r["mas"]) - 1]
    if last:
        out.append(f"마지막 선 하나만 남음: {', '.join(last)}")

    near = [f"{r['label']} {r['nearest']['period']}주 ({r['nearest']['dist_pct']:+.1f}%)"
            for r in rows if r["nearest"] and abs(r["nearest"]["dist_pct"]) <= rules["near_ma_pct"]]
    if near:
        out.append(f"주봉 MA 시험 중 (±{rules['near_ma_pct']:g}% 이내): {', '.join(near)}")
    return out


def build_summary_comments(groups: list[dict], rules: dict) -> list[str]:
    out: list[str] = []
    # 위험 신호 그룹: 공포지수(level)는 평온한데 같은 그룹의 가격 지표(신용 등)가 무너지는 "괴리"
    for g in groups:
        if g["type"] != "risk":
            continue
        rows = [r for r in g["rows"] if "error" not in r]
        calm = [r for r in rows if r["kind"] == "level" and r["position_52w_pct"] is not None
                and r["position_52w_pct"] <= rules["calm_position_pct"]]
        weak = [r for r in rows if r["kind"] == "price" and r["mas"]
                and (len(r["broken"]) == len(r["mas"])
                     or (len(r["mas"]) >= 2 and len(r["broken"]) == len(r["mas"]) - 1)
                     or any(m["cross"] == "down" for m in r["mas"]))]
        if calm and weak:
            calm_txt = ", ".join("%s 52주 위치 %.0f%%" % (r["symbol"].lstrip("^"), r["position_52w_pct"]) for r in calm)
            weak_txt = ", ".join(r["symbol"].lstrip("^") for r in weak)
            out.append(f"괴리: 공포지수는 평온({calm_txt})한데 약세 지표 {len(weak)}개({weak_txt}) — "
                       f"모든 주봉 MA 이탈·이탈 직전이거나 이번 주 하향 이탈")
    seen = set()
    last = []
    for g in groups:
        for r in g["rows"]:
            if "error" in r or r["kind"] != "price" or len(r["mas"]) < 2 or r["symbol"] in seen:
                continue
            if len(r["broken"]) == len(r["mas"]) - 1:
                seen.add(r["symbol"])
                last.append(f"{r['label']} — {r['ma_order'][-1]}주선")
    if last:
        out.append(f"마지막 주봉 MA 하나만 남은 종목: {', '.join(last)}")
    return out


def build_weekly_report(config: dict, now_ny: datetime, force: bool = False) -> dict:
    end = report_week_end(now_ny)
    cache_key = json.dumps(config, sort_keys=True) + end.isoformat()
    if not force:
        with _weekly_report_cache_lock:
            cached = _weekly_report_cache.get(cache_key)
            if cached and time.time() - cached["ts"] < WEEKLY_REPORT_CACHE_TTL:
                return cached["data"]

    symbols = _weekly_report_symbols(config)
    periods = config["ma_periods"]
    metrics: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def work(sym: str):
        try:
            return sym, compute_symbol_week(get_yahoo_daily_history(sym, force=force), end, periods), None
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as e:
            return sym, None, str(e) or type(e).__name__

    for sym, m, err in parallel_map(work, sorted(symbols), WEEKLY_REPORT_MAX_WORKERS):
        if m is not None:
            metrics[sym] = m
        else:
            errors[sym] = err

    bench = metrics.get(config["benchmark"])
    rate = metrics.get(config["rate_symbol"])
    fx = metrics.get(config["fx_symbol"])
    groups_out = []
    summary_all_broken: dict[str, str] = {}
    summary_crosses: list[dict] = []
    seen_cross = set()
    for g in config["groups"]:
        rows = []
        for it in g["items"]:
            m = metrics.get(it["symbol"])
            row = {"symbol": it["symbol"], "label": it["label"], "kind": it["kind"]}
            if m is None:
                row["error"] = errors.get(it["symbol"], "데이터 없음")
                rows.append(row)
                continue
            row.update(m)
            row["rel_pct"] = m["change_pct"] - bench["change_pct"] if bench else None
            row["spread_pct"] = (m["dividend_yield_pct"] - rate["close"]) if (rate and m["dividend_yield_pct"] is not None) else None
            rows.append(row)
            if it["kind"] == "price" and m["mas"] and len(m["broken"]) == len(m["mas"]):
                summary_all_broken.setdefault(it["symbol"], it["label"])
            for ma in m["mas"]:
                key = (it["symbol"], ma["period"])
                if ma["cross"] and key not in seen_cross:
                    seen_cross.add(key)
                    summary_crosses.append({"symbol": it["symbol"], "label": it["label"], "period": ma["period"],
                                            "dir": ma["cross"], "kind": it["kind"]})
        out = {"title": g["title"], "type": g["type"], "rows": rows}
        if g["type"] == "fx_account" and all("error" not in r for r in rows):
            a, b = rows[0]["change_pct"], rows[1]["change_pct"]
            out["account_pct"] = ((1 + a / 100) * (1 + b / 100) - 1) * 100
        groups_out.append(out)

    rules = config["comment_rules"]
    rate_info = {"symbol": config["rate_symbol"], "value": rate["close"]} if rate else None
    for g in groups_out:
        # 내 계좌 그룹은 로그인 세션의 보유종목이 필요해서 여기선 비워두고 fill_account_groups에서 채운다.
        g["comments"] = [] if g["type"] == "my_account" else build_group_comments(g, rules, config["benchmark"], rate_info)

    prev_end = None
    if bench:
        bars = [b for b in get_yahoo_daily_history(config["benchmark"])["bars"] if b[0] <= end]
        cur_week = bars[-1][0].isocalendar()[:2]
        before = [b for b in bars if b[0].isocalendar()[:2] < cur_week]
        prev_end = before[-1][0].isoformat() if before else None

    data = {
        "week_end": end.isoformat(),
        "prev_week_end": prev_end,
        "generated_at": fmt_as_of(time.time()),
        "ma_periods": periods,
        "benchmark": {"symbol": config["benchmark"], "change_pct": bench["change_pct"] if bench else None},
        "rate": {"symbol": config["rate_symbol"], "value": rate["close"] if rate else None},
        "fx": {"symbol": config["fx_symbol"], "change_pct": fx["change_pct"] if fx else None},
        "groups": groups_out,
        "summary": {
            "all_broken": [{"symbol": s, "label": l} for s, l in summary_all_broken.items()],
            "crosses": summary_crosses,
            "comments": build_summary_comments(groups_out, rules),
        },
        "errors": errors,
    }
    with _weekly_report_cache_lock:
        _weekly_report_cache[cache_key] = {"ts": time.time(), "data": data}
    return data


def toss_to_yahoo_symbol(symbol: str, market: str | None, currency: str) -> str | None:
    """토스 심볼 → Yahoo 심볼. 국내는 시장 구분으로 .KS/.KQ를 붙이고, 해외는 BRK.B → BRK-B처럼 바꾼다."""
    if currency == "KRW":
        return {"KOSPI": f"{symbol}.KS", "KOSDAQ": f"{symbol}.KQ"}.get(market or "")
    return symbol.replace(".", "-")


def fill_account_groups(report: dict, config: dict, holdings: dict, kr_info: dict[str, dict]) -> None:
    """리포트의 my_account 그룹을 토스 보유종목으로 채운다(report를 직접 수정).

    주간 원화 수익 = 해외는 (1 + 배당 포함 주간수익)(1 + 환율 주간변화) − 1, 국내는 주간수익 그대로.
    기여도 = 현재 비중 × 원화 주간수익 — 한 주 동안 보유 수량이 그대로였다고 가정한 근사치.
    """
    targets = [g for g in report["groups"] if g["type"] == "my_account"]
    if not targets:
        return
    end = date.fromisoformat(report["week_end"])
    periods = config["ma_periods"]
    fx_change = report["fx"]["change_pct"]
    total = holdings["totals"]["eval_krw"] or 0.0

    positions = [(r, False) for r in holdings["stocks"]] + [(r, True) for r in holdings["cash"]["stocks"]]

    def work(item):
        r, is_cash = item
        currency = r["통화"]
        ysym = toss_to_yahoo_symbol(r["심볼"], (kr_info.get(r["심볼"]) or {}).get("market"), currency)
        row = {"symbol": r["심볼"], "yahoo_symbol": ysym, "label": r["종목"], "kind": "price", "currency": currency,
               "is_cash": is_cash, "eval_krw": r["평가금액(원)"],
               "weight_pct": r["평가금액(원)"] / total * 100 if total else 0.0}
        if not ysym:
            row["error"] = "Yahoo 심볼로 바꿀 수 없는 시장"
            return row
        try:
            m = compute_symbol_week(get_yahoo_daily_history(ysym), end, periods)
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as e:
            row["error"] = str(e) or type(e).__name__
            return row
        row.update(m)
        tr = m["total_return_pct"]
        if currency == "USD":
            row["krw_change_pct"] = None if fx_change is None else ((1 + tr / 100) * (1 + fx_change / 100) - 1) * 100
        else:
            row["krw_change_pct"] = tr
        return row

    rows = parallel_map(work, positions, WEEKLY_REPORT_MAX_WORKERS)

    cash = holdings["cash"]
    usd_cash_krw = cash["usd"] * cash["usd_krw_rate"]
    for label, amount, change in (("예수금 (달러)", usd_cash_krw, fx_change), ("예수금 (원화)", cash["krw"], 0.0)):
        if amount > 0:
            rows.append({"symbol": "", "label": label, "kind": "cash", "is_cash": True, "eval_krw": amount,
                         "weight_pct": amount / total * 100 if total else 0.0, "krw_change_pct": change,
                         "mas": [], "broken": [], "nearest": None})

    account_pct = 0.0
    complete = True
    for r in rows:
        if r.get("krw_change_pct") is None:
            complete = False
            r["contribution_pctp"] = None
            continue
        r["contribution_pctp"] = r["weight_pct"] / 100 * r["krw_change_pct"]
        account_pct += r["contribution_pctp"]

    bench = report["benchmark"]
    comments = []
    if complete and bench["change_pct"] is not None:
        comments.append(f"원화 기준 계좌 {_fmt_signed(account_pct)} vs {bench['symbol']} {_fmt_signed(bench['change_pct'])} "
                        f"(차이 {_fmt_signed(account_pct - bench['change_pct'], '%p')})")
    n = config["comment_rules"]["sector_top_n"]
    contrib = sorted([r for r in rows if r.get("contribution_pctp") is not None], key=lambda r: -r["contribution_pctp"])
    if n > 0 and contrib:
        plus = ", ".join(f"{r['label']} {_fmt_signed(r['contribution_pctp'], '%p')}" for r in contrib[:n] if r["contribution_pctp"] > 0)
        minus = ", ".join(f"{r['label']} {_fmt_signed(r['contribution_pctp'], '%p')}" for r in contrib[::-1][:n] if r["contribution_pctp"] < 0)
        if plus:
            comments.append(f"계좌를 끌어올린 종목: {plus}")
        if minus:
            comments.append(f"계좌를 끌어내린 종목: {minus}")
    # 추세·MA 코멘트는 현금 취급 종목(SGOV 등)을 빼고 — 초단기 국채의 "상승 추세"는 의미가 없어서.
    market_rows = [r for r in rows if r["kind"] == "price" and "error" not in r and not r["is_cash"]]
    rate_info = report["rate"] if report["rate"]["value"] is not None else None
    comments += build_group_comments({"type": "my_account", "rows": market_rows}, config["comment_rules"], bench["symbol"], rate_info)

    for g in targets:
        g["rows"] = rows
        g["account"] = {
            "eval_krw": total,
            "profit_loss_krw": holdings["totals"]["profit_loss_krw"],
            "rate_pct": holdings["totals"]["rate_pct"],
            "week_krw_pct": account_pct if complete else None,
        }
        g["comments"] = comments


def validate_weekly_report_symbols(config: dict, known: set[str]) -> list[str]:
    """새로 추가된 심볼만 Yahoo에서 실제로 받아지는지 확인. 실패한 심볼 목록을 돌려준다."""
    new = sorted(_weekly_report_symbols(config) - known)

    def check(sym: str):
        try:
            get_yahoo_daily_history(sym)
            return None
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            return sym

    return [s for s in parallel_map(check, new, WEEKLY_REPORT_MAX_WORKERS) if s]
