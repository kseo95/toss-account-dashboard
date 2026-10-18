"""규칙 기반 모드의 에이전트 루프: 도구 결과를 보고 다음에 볼 도구를 정하고(follow_ups), 여러 도구 결과를 엮은 관찰 문장을 만든다(observations).

모델 없이(비용 0원) 정해 둔 규칙으로 '결과 → 다음 행동'을 잇는다. 규칙은 숫자 비교만 하고 전망·매매 판단은 하지 않는다.
순수 계산이며 도구 호출은 server가 한다(이 모듈은 다른 기능 모듈을 부르지 않는다).
"""
from __future__ import annotations

MAX_EXTRA_STEPS = 2          # 처음 계획 뒤에 덧붙여 볼 수 있는 도구 수
WORSENING = ("약화 초입", "하락 전환 확인")
IMPROVING = ("반등 시도", "상승 전환 초입")
BROAD_SHARE_PCT = 50.0       # 업종 종목 중 이만큼 이상이 이동평균 아래면 '넓게 약하다'
NARROW_SHARE_PCT = 30.0      # 이만큼 미만이면 '일부만 약하다'
DRILL_PERIODS = [60, 120]    # 업종 안쪽을 볼 때 쓰는 주봉 이동평균
NAMES_SHOWN = 3

# 업종 ETF ↔ 스크리너 업종(영문 GICS, 한국어 이름)
SECTOR_OF_ETF = {
    "XLK": ("Information Technology", "정보기술"), "XLC": ("Communication Services", "통신"), "XLY": ("Consumer Discretionary", "경기소비재"),
    "XLF": ("Financials", "금융"), "XLI": ("Industrials", "산업재"), "XLV": ("Health Care", "헬스케어"), "XLP": ("Consumer Staples", "필수소비재"),
    "XLU": ("Utilities", "유틸리티"), "XLE": ("Energy", "에너지"), "XLB": ("Materials", "소재"), "XLRE": ("Real Estate", "부동산"),
}
ETF_OF_SECTOR_KO = {ko: symbol for symbol, (_en, ko) in SECTOR_OF_ETF.items()}


def _names(items: list[str]) -> str:
    return ", ".join(items[:NAMES_SHOWN]) + (f" 외 {len(items) - NAMES_SHOWN}곳" if len(items) > NAMES_SHOWN else "")


def follow_ups(results: dict[str, dict]) -> list[dict]:
    """지금까지의 결과를 보고 덧붙여 볼 도구. [{tool, args, reason}] — 이미 부른 도구는 다시 부르지 않는다(server가 거른다)."""
    out = []
    trend, screen, weekly = results.get("get_sector_trends"), results.get("get_ma_screen"), results.get("get_weekly_risk_report")
    if trend and trend.get("universe") == "sectors" and not screen:
        weak = [r for r in trend.get("rows", []) if r["state"] in WORSENING and r["symbol"] in SECTOR_OF_ETF]
        if weak:    # 악화 신호가 있는 업종 중 전 고점에서 가장 깊이 빠진 곳의 안쪽(개별 종목)을 본다
            worst = min(weak, key=lambda r: r["metrics"].get("from_high") if r["metrics"].get("from_high") is not None else 0)
            out.append({"tool": "get_ma_screen", "args": {"universe": "sp500", "sector": SECTOR_OF_ETF[worst["symbol"]][0], "periods": DRILL_PERIODS, "side": "below", "match": "all"},
                        "reason": f"{worst['name']}({worst['symbol']})이 {worst['state']}라서, S&P500 안의 같은 업종 종목이 얼마나 이동평균 아래인지 확인"})
    if screen and screen.get("universe") == "sp500" and not screen.get("sector") and screen.get("total_matches") and not trend:
        top = next((s for s in screen.get("sector_counts", []) if s["sector"] in ETF_OF_SECTOR_KO and s["total"]), None)
        if top:
            out.append({"tool": "get_sector_trends", "args": {"universe": "sectors", "focus": "all"},
                        "reason": f"이동평균 아래 종목이 가장 많은 {top['sector']}의 업종 ETF({ETF_OF_SECTOR_KO[top['sector']]}) 상태를 함께 확인"})
    if weekly and weekly.get("all_ma_broken") and "get_weekly_news" not in results:
        labels = [str(x.get("label") or x.get("symbol") or "") for x in weekly["all_ma_broken"] if x.get("label") or x.get("symbol")]
        if labels:
            out.append({"tool": "get_weekly_news", "args": {"focus": ", ".join(labels[:NAMES_SHOWN])},
                        "reason": f"모든 이동평균선을 이탈한 {_names(labels)}에 이번 주 보도가 있었는지 확인"})
    return out[:MAX_EXTRA_STEPS]


def observations(results: dict[str, dict]) -> list[str]:
    """여러 도구 결과를 엮어 숫자로 확인되는 사실만 적은 문장. 전망·매매 판단은 하지 않는다."""
    out = []
    trend, screen = results.get("get_sector_trends"), results.get("get_ma_screen")
    rows = (trend or {}).get("rows") or []
    if rows and any(r["state"] in WORSENING + IMPROVING for r in rows):
        worse = [f"{r['name']}" for r in rows if r["state"] in WORSENING]
        better = [f"{r['name']}" for r in rows if r["state"] in IMPROVING]
        parts = [f"악화 신호 {len(worse)}곳" + (f"({_names(worse)})" if worse else ""), f"반등 신호 {len(better)}곳" + (f"({_names(better)})" if better else "")]
        out.append(f"종합: {len(rows)}곳 중 " + ", ".join(parts) + f", 나머지 {len(rows) - len(worse) - len(better)}곳은 신호 없음.")
    if trend and screen and screen.get("sector") and screen.get("evaluated"):
        symbol = ETF_OF_SECTOR_KO.get(screen["sector"])
        row = next((r for r in rows if r["symbol"] == symbol), None)
        if row:
            share = screen["total_matches"] / screen["evaluated"] * 100
            plist = "·".join(str(p) for p in screen["periods"])
            line = f"종합: {screen['sector']} 업종은 ETF({symbol})가 '{row['state']}'이고, S&P500 {screen['sector']} 종목 {screen['evaluated']}개 중 {screen['total_matches']}개({share:.0f}%)가 주봉 MA {plist} 모두 아래입니다."
            if row["state"] in WORSENING and share >= BROAD_SHARE_PCT:
                line += " ETF의 약화 신호와 개별 종목 분포가 같은 방향입니다."
            elif row["state"] in WORSENING and share < NARROW_SHARE_PCT:
                line += " ETF는 약해졌지만 개별 종목 다수는 아직 이동평균 위입니다(일부 종목이 끌어내린 모양)."
            out.append(line)
    elif trend and screen and not screen.get("sector") and screen.get("total_matches"):
        top = next((s for s in screen.get("sector_counts", []) if s["sector"] in ETF_OF_SECTOR_KO and s["total"]), None)
        row = next((r for r in rows if top and r["symbol"] == ETF_OF_SECTOR_KO[top["sector"]]), None)
        if top and row:
            out.append(f"종합: 이동평균 아래 종목이 가장 많은 {top['sector']}({top['count']}개, 업종 {top['total']}개 중 {top['count'] / top['total'] * 100:.0f}%)의 ETF({row['symbol']})는 '{row['state']}'입니다.")
    return out
