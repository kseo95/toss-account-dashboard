"""업종 추세·이동평균 스크리너 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import charts as chart_mod
import trend as trend_mod

from screener import DEFAULT_PERIODS, MAX_PERIOD, MAX_PERIODS, MIN_PERIOD, SECTOR_LABELS
from .common import _number, _round, _signed


DEFINITIONS = [
    {
        "name": "get_sector_trends",
        "description": "미국 업종 ETF(SPDR 11개: XLK·XLC·XLY·XLF·XLI·XLV·XLP·XLU·XLE·XLB·XLRE)나 내 보유·관심 종목의 현재 상태를 조회합니다 — 시장 전체의 업종 ETF 상태를 묻는 질문에 씁니다(내 포트폴리오의 업종 노출이 아닙니다). 1주·단기(기본 1개월)·중기(3개월)·6개월·장기(12개월) 수익률, 50일선·200일선 대비, 전 고점(상장 이후 최고 종가) 대비, S&P500 대비 중기 성과를 주고, 기간 사이의 엇갈림으로 상태를 붙입니다(상승 유지·약화 초입·조정 중·상승 전환 초입·장기선 회복 시도·반등 시도·약세 속 혼조·하락 전환 확인·하락 유지·장기선 부근). 같은 상태가 몇 주째인지(연속 2주 이상이면 확인, 아니면 미확정)와 직전 상태도 줍니다. focus는 보이는 정렬 순서만 바꿉니다(short·mid·long). 위치·방향 정보일 뿐 매매 신호나 전망이 아닙니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "universe": {"type": "string", "enum": ["sectors", "mine"], "description": "sectors=미국 SPDR 업종 ETF 11개(기본), mine=내 보유·관심 종목"},
            "focus": {"type": "string", "enum": ["all", "short", "mid", "long"], "description": "all=3·6·12개월 평균 순위(기본), short=단기 수익률 순, mid=중기, long=장기. 상태 판정 기준은 바뀌지 않음"},
            "buffer_pct": {"type": "number", "minimum": 0, "maximum": 10, "description": "이동평균 ±버퍼% 안은 이탈·회복으로 세지 않음(기본 2)"},
            "confirm_weeks": {"type": "integer", "minimum": 1, "maximum": 8, "description": "같은 상태가 연속 몇 주 유지돼야 확인인지(기본 2)"}},
                         "additionalProperties": False},
    },
    {
        "name": "get_ma_screen",
        "description": "이동평균 스크리너: S&P500 구성 종목(503개, 공개 데이터셋 기준)이나 내 보유·관심 종목 중, 종가가 지정한 주봉 이동평균(예: 60·120·200·240주) 모두 또는 하나라도 아래(또는 위)에 있는 종목을 찾습니다. 종목별 종가와 MA 대비 위치(%), 업종 분포, 주봉이 모자라 판단하지 못한 종목 수를 줍니다. 첫 조회는 500개 시세를 받느라 30~60초 걸릴 수 있고 이후 캐시됩니다. 위치 정보일 뿐 매매 신호가 아닙니다.",
        "read_only": True,
        "input_schema": {"type": "object", "properties": {
            "universe": {"type": "string", "enum": ["sp500", "mine"], "description": "sp500=S&P500 구성 종목(기본), mine=내 보유·관심 종목"},
            "periods": {"type": "array", "items": {"type": "integer", "minimum": 2, "maximum": 300}, "maxItems": 8, "description": "주봉 이동평균 기간 목록(예: [60,120,200,240]). 비우면 60·120·200·240"},
            "side": {"type": "string", "enum": ["below", "above"], "description": "below=종가가 MA 아래(기본), above=위"},
            "match": {"type": "string", "enum": ["all", "any"], "description": "all=모든 기간 조건 충족(기본), any=하나라도"},
            "sector": {"type": "string", "description": "GICS 업종 영문 이름 일부(예: Information Technology). 선택"}},
                         "additionalProperties": False},
    },
]


def ma_screen_result(data: dict) -> dict:
    """스크리너 결과에서 숫자와 짧은 이름만 남긴다(종목 60개까지, 업종 분포 12개까지)."""
    periods = [int(p) for p in (data.get("periods") or []) if isinstance(p, int) and MIN_PERIOD <= p <= MAX_PERIOD][:MAX_PERIODS] or list(DEFAULT_PERIODS)
    matches = []
    for m in (data.get("matches") or [])[:60]:
        matches.append({"symbol": str(m.get("symbol") or "")[:12], "name": str(m.get("name") or "")[:50],
                        "sector": SECTOR_LABELS.get(m.get("sector"), str(m.get("sector") or "")[:30]), "close": _round(m.get("close")),
                        "mas": [{"period": int(x["period"]), "value": _round(x.get("value")), "dist_pct": _round(x.get("dist_pct"), 2)}
                                for x in (m.get("mas") or [])[:MAX_PERIODS] if isinstance(x, dict) and _number(x.get("dist_pct")) is not None],
                        "hit": [int(h) for h in (m.get("hit") or [])[:MAX_PERIODS] if isinstance(h, int)]})
    return {
        "universe": "mine" if data.get("universe_name") == "mine" else "sp500", "periods": periods,
        "side": "above" if data.get("side") == "above" else "below", "match": "any" if data.get("match") == "any" else "all",
        "sector": SECTOR_LABELS.get(data.get("sector"), str(data.get("sector") or "")[:30]) if data.get("sector") else "",
        "universe_size": int(_number(data.get("universe_size")) or 0), "evaluated": int(_number(data.get("evaluated")) or 0),
        "insufficient": int(_number(data.get("insufficient")) or 0), "failed": int(_number(data.get("failed")) or 0),
        "total_matches": int(_number(data.get("total_matches")) or len(matches)), "as_of": str(data.get("as_of") or "")[:10] or None,
        "matches": matches, "symbols": [str(s)[:12] for s in (data.get("symbols") or [])[:300]],
        "sector_counts": [{"sector": SECTOR_LABELS.get(s.get("sector"), str(s.get("sector") or "")[:30]), "count": int(_number(s.get("count")) or 0),
                           "total": int(_number(s.get("total")) or 0)} for s in (data.get("sector_counts") or [])[:12]],
        "limitations": [
            "구성 종목 목록은 공개 데이터셋(위키피디아 기반, 매일 갱신)이라 공식 S&P 자료가 아니고 최근 편입·편출이 며칠 늦게 반영될 수 있습니다.",
            "주봉은 일봉을 ISO 주 단위로 묶어 주의 마지막 거래일 종가를 쓰고(이번 주는 최근 종가), 단순이동평균입니다. 주봉이 가장 긴 기간보다 적은 종목(신규 상장 등)은 판단하지 않고 따로 셉니다.",
            "이 목록은 가격이 이동평균 아래·위에 있다는 위치 정보일 뿐이며 매수·매도 신호나 전망이 아닙니다. 야후에서 시세를 받지 못한 종목은 빠집니다.",
        ],
    }


def sector_trends_result(data: dict) -> dict:
    """추세 사다리 결과에서 숫자와 짧은 이름만 남긴다(종목 25개까지)."""
    state_names = set(trend_mod.STATE_NAMES)

    def metrics(m):
        m = m if isinstance(m, dict) else {}
        return {k: _round(m.get(k), 1) for k in ("close", "r_week", "r_short", "r_mid", "r_half", "r_long", "dist_fast", "dist_slow", "from_high")}
    rows = []
    for r in (data.get("rows") or [])[:25]:
        state = r.get("state") if r.get("state") in state_names else "장기선 부근"
        prev = r.get("previous_state") if r.get("previous_state") in state_names else None
        rows.append({"symbol": str(r.get("symbol") or "")[:12], "name": str(r.get("name") or "")[:40], "state": state,
                     "since_weeks": int(_number(r.get("since_weeks")) or 1), "confirmed": bool(r.get("confirmed")), "previous_state": prev,
                     "previous_weeks_ago": int(_number(r.get("previous_weeks_ago")) or 0) or None, "metrics": metrics(r.get("metrics")),
                     "vs_benchmark_mid": _round(r.get("vs_benchmark_mid"), 1)})
    cfg = trend_mod.clean_settings(data.get("cfg"))
    bench = data.get("benchmark") or {}
    return {
        "universe": "mine" if data.get("universe") == "mine" else "sectors", "focus": data.get("focus") if data.get("focus") in trend_mod.FOCUS_VALUES else "all",
        "as_of": str(data.get("as_of") or "")[:10] or None, "cfg": cfg, "rows": rows,
        "groups": {s: [str(x)[:12] for x in syms[:30]] for s, syms in (data.get("groups") or {}).items() if s in state_names},
        "changed": [{"symbol": str(c.get("symbol") or "")[:12], "name": str(c.get("name") or "")[:40], "from": c.get("from") if c.get("from") in state_names else None,
                     "to": c.get("to") if c.get("to") in state_names else None, "weeks_ago": int(_number(c.get("weeks_ago")) or 1), "confirmed": bool(c.get("confirmed"))}
                    for c in (data.get("changed") or [])[:25]],
        "insufficient": [str(s)[:12] for s in (data.get("insufficient") or [])[:25]],
        "benchmark": {"name": str(bench.get("name") or "")[:20], "r_mid": _round((bench.get("metrics") or {}).get("r_mid"), 1)} if bench else None,
        "states": [{"state": s, "meaning": m} for s, m in trend_mod.STATES],
        "limitations": [
            "상태 이름과 판정 기준(버퍼·연속 주수·기간)은 정해 둔 규칙이며 어떤 기준도 미래의 추세전환을 보장하지 않습니다. 기간이 짧을수록 빠르지만 거짓 신호가 늘고 길수록 늦습니다.",
            f"기간은 거래일 수입니다(1개월=21일, 3개월=63일, 6개월=126일, 12개월=252일). 이동평균은 {cfg['ma_fast']}일·{cfg['ma_slow']}일 단순이동평균, 이동평균 ±{cfg['buffer_pct']:g}% 안은 이탈·회복으로 세지 않고, 같은 상태가 연속 {cfg['confirm_weeks']}주 이상일 때만 확인으로 봅니다.",
            "'전 고점'은 야후 시세가 있는 전체 기간(상장 이후)의 최고 종가이며 가격 기준이라 배당은 반영되지 않습니다. 상장이 짧은 업종(예: 부동산 2015년~, 커뮤니케이션 2018년~)은 그 이후 기준입니다.",
            "가격 기준(배당 제외)이라 배당이 큰 업종(유틸리티·필수소비재·부동산)의 총수익은 이보다 조금 높습니다. 위치·방향 정보일 뿐 매수·매도 신호가 아닙니다.",
        ],
    }


SCREEN_TABLE_ROWS = 40


def _dist_text(value: float) -> str:
    """MA 대비 %: 0.1% 미만이면 소수 둘째 자리까지(아래인데 '+0.0'으로 보이는 것을 막는다)."""
    return f"{value:+.2f}" if abs(value) < 0.1 else f"{value:+.1f}"


def _compose_screen(data: dict, bullets: list, tables: list, notes: list, limitations: list) -> None:
    """이동평균 스크리너 결과: 개수·업종 분포·종목 표(가장 깊이 아래인 순). 위치 정보일 뿐 매매 신호가 아니다."""
    periods = data["periods"]
    plist = "·".join(str(p) for p in periods)
    scope = "내 보유·관심 종목" if data["universe"] == "mine" else "S&P500 구성 종목"
    if data.get("sector"):
        scope += f"({data['sector']})"
    side_text = ("아래" if data["side"] == "below" else "위") + "에 있는"
    cond = f"주봉 MA {plist}" + (" 중 하나라도" if data["match"] == "any" else " 모두") + f"보다 {side_text}"
    total, evaluated = data["total_matches"], data["evaluated"]
    if not evaluated:
        bullets.append(f"{scope}에서 주봉 MA {plist}를 계산할 수 있는 종목이 없습니다(시세를 받지 못했거나 주봉이 {max(periods)}주보다 짧음).")
    else:
        share = total / evaluated * 100
        bullets.append(f"{scope} {data['universe_size']}개 중 주봉 {max(periods)}주 이상 시세가 있는 {evaluated}개를 보면, {cond} 종목은 {total}개({share:.0f}%)입니다"
                       + (f" (기준일 {data['as_of']}, 종가 기준)." if data.get("as_of") else "."))
    rows = []
    for m in data["matches"][:SCREEN_TABLE_ROWS]:
        label = f"{m['name']} ({m['symbol']})" if m["name"] and m["name"] != m["symbol"] else m["symbol"]
        rows.append([label, m["sector"] or "-", f"{m['close']:,.2f}", {"lines": [" · ".join(f"MA{x['period']} {_dist_text(x['dist_pct'])}%" for x in m["mas"])]}])
    if rows:
        tables.append({"title": f"{cond} {scope} ({'가장 깊이 아래인 순' if data['side'] == 'below' else '가장 많이 위인 순'} · 종가가 MA보다 몇 % 아래(−)·위(+)인지)",
                       "columns": ["종목", "업종", "종가", "주봉 MA 대비"], "align": ["l", "l", "r", "l"], "wrap": [False, False, False, True], "rows": rows, "footer": []})
    if total > len(rows):
        notes.append(f"해당 종목 {total}개 중 {len(rows)}개만 표에 보여줍니다(가장 깊이 {'아래' if data['side'] == 'below' else '위'}인 순).")
        rest = [s for s in data.get("symbols", []) if s not in {m["symbol"] for m in data["matches"][:len(rows)]}]
        if rest:
            notes.append(f"표에 없는 나머지 해당 종목 {len(rest)}개: " + ", ".join(rest[:150]) + ("…" if len(rest) > 150 else ""))
    if data["insufficient"]:
        notes.append(f"주봉이 {max(periods)}주보다 짧아 판단하지 않은 종목 {data['insufficient']}개(신규 상장 등).")
    if data["failed"]:
        notes.append(f"야후에서 시세를 받지 못해 빠진 종목 {data['failed']}개.")
    limitations.extend(data.get("limitations", []))


HORIZON_NAMES = {5: "1주", 10: "2주", 21: "1개월", 42: "2개월", 63: "3개월", 126: "6개월", 189: "9개월", 252: "12개월", 504: "24개월"}


FOCUS_TEXT = {"short": "단기(최근 흐름)", "mid": "중기", "long": "장기"}


WORSENING_STATES = ("약화 초입", "하락 전환 확인")   # ⚠: 악화 신호


IMPROVING_STATES = ("반등 시도", "상승 전환 초입")    # ↗: 반등·개선 신호


def _horizon_name(days: int) -> str:
    return HORIZON_NAMES.get(days, f"{days}거래일")


def _from_high_text(value) -> str:
    """전 고점(상장 이후 최고 종가) 대비 %: 고점이면 0.0%, 아니면 -9.2%처럼 마이너스로."""
    if value is None:
        return "-"
    return "0.0%" if abs(value) < 0.05 else f"{value:.1f}%"


def _return_text(value) -> str:
    return "-" if value is None else f"{_signed(value)}%"


def _compose_trend(data: dict, bullets: list, tables: list, notes: list, limitations: list, charts: list | None = None) -> None:
    """미국 업종 ETF(또는 내 종목)를 잘나가는 순으로 한 줄씩: 순위·업종·두 기간 수익률·전 고점 대비·상태. 전환 신호는 ⚠(악화)·↗(반등)로 표시한다."""
    rows, cfg = data.get("rows") or [], data["cfg"]
    scope = "내 보유·관심 종목" if data["universe"] == "mine" else f"미국 업종 ETF {len(rows) + len(data['insufficient'])}개"
    if not rows:
        bullets.append(f"{scope}의 추세를 계산할 만큼의 시세({max(cfg['long_days'], cfg['ma_slow'])}거래일 이상)가 없습니다.")
        return
    focus = data.get("focus") or "all"
    cols = {"all": ("r_mid", "r_long"), "short": ("r_week", "r_short"), "mid": ("r_mid", "r_half"), "long": ("r_long", "r_half")}[focus]
    horizon = {"r_week": "1주", "r_short": _horizon_name(cfg["short_days"]), "r_mid": _horizon_name(cfg["mid_days"]), "r_half": "6개월", "r_long": _horizon_name(cfg["long_days"])}
    order_text = {"all": f"{_horizon_name(cfg['mid_days'])}·6개월·{_horizon_name(cfg['long_days'])} 평균 순위", "short": "단기 수익률", "mid": "중기 수익률", "long": "장기 수익률"}[focus]
    if charts is None:
        bullets.append(f"{scope} — 잘나가는 순 ({order_text}" + (f", {data['as_of']} 종가" if data.get("as_of") else "") + ")")
        bullets.append("⚠ 악화 신호(약화 초입·하락 전환 확인) · ↗ 반등 신호(반등 시도·상승 전환 초입) · 숫자는 전 고점 대비")
    table_rows = []
    for rank, r in enumerate(rows, 1):
        m = r["metrics"]
        mark = " ⚠" if r["state"] in WORSENING_STATES else " ↗" if r["state"] in IMPROVING_STATES else ""
        if mark:   # 신호가 있는 종목만 직전 상태와 확인 여부를 덧붙인다
            since = f"{r['since_weeks']}주째 · " + ("확인" if r["confirmed"] else "미확정")
            detail = f"{r['previous_state']}에서 {since}" if r.get("previous_state") and r["since_weeks"] <= 4 else since
            state_cell = {"lines": [r["state"] + mark, detail]}
        else:
            state_cell = r["state"]
        name = f"{r['name']}({r['symbol']})" if data["universe"] == "sectors" else r["name"]
        table_rows.append([str(rank), name, _return_text(m.get(cols[0])), _return_text(m.get(cols[1])), _from_high_text(m.get("from_high")), state_cell])
    if charts is not None:    # 잘나가는 순 가로 막대: 두 기간 수익률 + 상태·전 고점 대비를 막대 끝 글로
        labels = [f"{r['name']}({r['symbol']})" if data["universe"] == "sectors" else r["name"] for r in rows]
        marks = [" ⚠" if r["state"] in WORSENING_STATES else " ↗" if r["state"] in IMPROVING_STATES else "" for r in rows]
        first = [r["metrics"].get(cols[0]) for r in rows]
        second = [r["metrics"].get(cols[1]) for r in rows]
        charts.append(chart_mod.make("hbar", f"{scope} — 잘나가는 순 ({order_text})", labels,
                                     [{"label": horizon[cols[0]], "color": "s2", "data": first},
                                      {"label": horizon[cols[1]], "colors": chart_mod.signed_colors(second), "data": second,
                                       "text": [("" if v is None else f"{v:+.1f}% · {r['state']}{m}") for v, r, m in zip(second, rows, marks)]}],
                                     "%", subtitle=(f"{data['as_of']} 종가 기준" if data.get("as_of") else ""),
                                     caption="막대 글: " + horizon[cols[1]] + " 수익률 · 상태(⚠악화 ↗반등 신호) · 회색 막대는 " + horizon[cols[0]] + " 수익률",
                                     height=max(200, len(rows) * 44 + 60)))
        for r_ in table_rows:           # 그래프에 있는 두 수익률 열은 표에서 뺀다
            del r_[2:4]
    tables.append({"title": f"{scope} 전 고점 대비와 추세 상태",
                   "columns": ["순위", "종목", "전 고점 대비", "상태"] if charts is not None else ["순위", "종목", horizon[cols[0]], horizon[cols[1]], "전 고점 대비", "상태"],
                   "align": ["r", "l", "r", "l"] if charts is not None else ["r", "l", "r", "r", "r", "l"],
                   "wrap": [False, False, False, True] if charts is not None else [False, False, False, False, False, True], "rows": table_rows, "footer": []})
    notes.insert(0, f"판정 기준: 이동평균({cfg['ma_fast']}일·{cfg['ma_slow']}일) ±{cfg['buffer_pct']:g}% 안은 이탈·회복으로 세지 않고, 같은 상태가 연속 {cfg['confirm_weeks']}주면 확인, 아니면 미확정.")
    present = [s for s in data.get("states", []) if s["state"] in data["groups"]]
    if present:
        notes.append("상태 뜻: " + " / ".join(f"{s['state']}={s['meaning']}" for s in present))
    if data["insufficient"]:
        notes.append("시세가 모자라 판단하지 못한 종목: " + ", ".join(data["insufficient"]))
    limitations.extend(data.get("limitations", []))


def _charts_screen(data: dict, charts: list) -> None:
    top = [s for s in (data.get("sector_counts") or []) if s["total"]][:8]
    if data.get("universe") != "sp500" or not data.get("total_matches") or not top:
        return
    shares = [s["count"] / s["total"] * 100 for s in top]
    plist = "·".join(str(p) for p in data["periods"])
    charts.append(chart_mod.make("hbar", f"주봉 MA {plist} {'모두' if data['match'] == 'all' else '하나라도'} {'아래' if data['side'] == 'below' else '위'}인 종목의 업종별 비율 (S&P500)",
                                 [s["sector"] for s in top],
                                 [{"label": "업종 안 비율", "color": "s1", "data": shares, "text": [f"{s['count']}/{s['total']}개 ({v:.0f}%)" for s, v in zip(top, shares)]}], "%",
                                 subtitle=f"전체 {data['total_matches']}개 / {data['evaluated']}개" + (f" · 기준일 {data['as_of']}" if data.get("as_of") else ""),
                                 caption="위치 정보일 뿐 매수·매도 신호가 아닙니다", height=len(top) * 34 + 60))


def compose_get_sector_trends(trend_result, bullets, tables, notes, limitations, charts, results):
    """get_sector_trends 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_trend(trend_result, bullets, tables, notes, limitations, charts)


def compose_get_ma_screen(screen_result, bullets, tables, notes, limitations, charts, results):
    """get_ma_screen 결과를 답변 조각(글·표·그래프)으로 조립한다."""
    _compose_screen(screen_result, bullets, tables, notes, limitations)
    _charts_screen(screen_result, charts)


FORMATTERS = {"get_sector_trends": sector_trends_result, "get_ma_screen": ma_screen_result}
COMPOSERS = {"get_sector_trends": compose_get_sector_trends, "get_ma_screen": compose_get_ma_screen}
