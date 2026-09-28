"""주식 거래 세금 추정 (개인 거주자, 국내 증권사 기준).

세율은 2026-09-28에 법제처 원문으로 확인한 값을 **기본값**으로만 두고, 실제 값은 사용자별 설정 파일 + 화면에서
바꾼다(세법이 바뀌면 화면에서 고치면 됨). 출처는 TAX_REFERENCE 참고.

계산은 "지금 전부 판다면"을 가정한 추정이다. 한계:
- 해외 종목의 원화 손익은 토스 손익(외화) × 현재 환율 — 실제 양도세는 산 날·판 날 환율로 계산하므로 환차익만큼 다를 수 있다.
- 국내 "기타 ETF" 세금은 매매차익 기준 상한값 — 실제는 과세표준기준가격 증가분과 매매차익 중 작은 쪽이라 더 적을 수 있다.
"""
from __future__ import annotations

import re

from common import to_float
from storage import UserStore

TAX_SETTINGS_FILE = "tax_settings.json"

DEFAULT_TAX_SETTINGS = {
    # 국내 주식을 팔 때(판 금액 기준 %). 증권거래세법 시행령 제5조 + 농어촌특별세법 제5조①5호.
    "transaction_tax_pct": {"KOSPI": 0.20, "KOSDAQ": 0.20, "KONEX": 0.10},
    # 해외 주식·ETF 양도소득세(국세 20% + 지방 2%). 소득세법 제104조①12호, 지방세법 제103조의3.
    "overseas_gain_rate_pct": 22.0,
    # 양도소득 기본공제(주식 전체 합쳐 연 1회). 소득세법 제103조.
    "gain_deduction_krw": 2_500_000.0,
    # 국내 상장 "기타 ETF" 매매차익·분배금 = 배당소득(14% + 지방 1.4%). 소득세법 제129조, 지방세법 제103조의13.
    "etf_other_rate_pct": 15.4,
    "dividend_rate_pct": 15.4,
    # 이자·배당 합계가 이 금액을 넘으면 종합과세. 소득세법 제14조③6호.
    "financial_income_threshold_krw": 20_000_000.0,
    # 대주주(한 종목 50억 원 이상 등, 소득세법 시행령 제157조)면 국내 주식도 양도세 대상.
    "is_major_shareholder": False,
    # 사용자가 직접 넣는 올해 값(증권사 앱에서 확인): 이미 판 해외 주식 손익 합계(손실은 음수), 받은 이자·배당 합계.
    "realized_overseas_gain_ytd_krw": 0.0,
    # True면 위 값을 직접 입력한 대로 쓰고, False면 토스 체결 내역에서 자동 계산한 값을 쓴다.
    "use_manual_realized": False,
    # True면 financial_income_ytd_krw를 직접 입력한 대로, False면 배당 추정(체결 내역 × Yahoo 배당 기록) 세전 합계를 쓴다.
    "use_manual_financial_income": False,
    "financial_income_ytd_krw": 0.0,
    # 국내 ETF 종류 직접 지정 {symbol: "etf_equity" | "etf_other" | "stock"} — 없으면 이름으로 추정.
    "domestic_classes": {},
}

DOMESTIC_CLASSES = {"stock": "국내 주식", "etf_equity": "국내주식형 ETF", "etf_other": "기타 ETF"}
ETF_BRANDS = re.compile(r"^(KODEX|TIGER|PLUS|ACE|RISE|SOL|HANARO|KOSEF|ARIRANG|KBSTAR|TIMEFOLIO|WON|1Q|BNK|KIWOOM|"
                        r"HK|마이다스|파워|TREX|FOCUS|UNICORN|에셋플러스|VITA|DAISHIN343|히어로즈|KCGI|WOORI|IBK)\b",
                        re.IGNORECASE)
# 이 단어가 들어간 국내 ETF는 국내 주식 매매차익 비과세 대상이 아닐 가능성이 커서 "기타 ETF"로 추정한다.
OTHER_ETF_HINTS = re.compile(r"미국|나스닥|S&P|다우|필라델피아|차이나|중국|항셍|일본|니케이|인도|베트남|유로|글로벌|선진|신흥|"
                             r"채권|국채|국고채|회사채|단기|머니마켓|CD금리|KOFR|금리|달러|엔화|엔선물|원유|WTI|금현물|골드|은선물|실버|구리|원자재|"
                             r"리츠|TDF|TRF|혼합", re.IGNORECASE)

# 화면의 "주식 거래 세금 한눈에" 표. 2026-09-28 법제처(국가법령정보센터) 원문 확인.
TAX_REFERENCE = [
    {"what": "국내 주식 팔 때 — 증권거래세",
     "rate": "판 금액의 0.20% (코스피 0.05% + 농어촌특별세 0.15%, 코스닥 0.20%, 코넥스 0.10%)",
     "when": "팔 때마다 자동으로 빠짐(이익·손실 무관)",
     "law": "증권거래세법 시행령 제5조(2026.1.2 시행), 농어촌특별세법 제5조①5호"},
    {"what": "국내 ETF 팔 때 — 증권거래세", "rate": "없음",
     "when": "ETF는 주권이 아니라 과세 대상 아님", "law": "증권거래세법 제2조"},
    {"what": "국내 주식 양도소득세", "rate": "일반 투자자 없음 · 대주주는 20%(3억 초과분 25%) + 지방 10%",
     "when": "대주주 = 직전 연말 한 종목 50억 원 이상(또는 지분율 기준)",
     "law": "소득세법 제104조①11호, 시행령 제157조"},
    {"what": "해외 주식·해외 상장 ETF 양도소득세",
     "rate": "22% (국세 20% + 지방 2%)",
     "when": "1년(1~12월) 번 돈 − 잃은 돈 − 250만 원 공제. 다음 해 5월 신고(증권사 대행 가능). 원화 기준이라 환율 차이도 포함",
     "law": "소득세법 제104조①12호·제103조, 지방세법 제103조의3"},
    {"what": "국내 상장 ETF 매매차익",
     "rate": "국내주식형: 없음 · 기타(해외지수·채권·원자재 등): 15.4%",
     "when": "기타 ETF는 판 시점에 원천징수(매매차익과 과세표준기준가격 증가분 중 작은 쪽)",
     "law": "소득세법 시행령 제26조의2④, 제129조"},
    {"what": "배당·분배금", "rate": "15.4% (14% + 지방 1.4%) 원천징수",
     "when": "미국 배당은 미국에서 15% 떼고 들어와 한국 추가 없음(14%보다 높아서)",
     "law": "소득세법 제129조①2호, 지방세법 제103조의13"},
    {"what": "금융소득 종합과세",
     "rate": "이자+배당이 연 2,000만 원 초과분은 다른 소득과 합산(최고 45% + 지방)",
     "when": "다음 해 5월 종합소득세 신고", "law": "소득세법 제14조③6호"},
    {"what": "고배당기업 배당 분리과세 (신설)",
     "rate": "2천만 원 이하 14% · 3억 이하 20% · 50억 이하 25% · 초과 30%",
     "when": "요건 갖춘 국내 상장사 배당, 2028년 사업연도까지. 신청하면 종합과세에서 빠짐",
     "law": "조세특례제한법 제104조의27(2025.12.23 신설)"},
    {"what": "금융투자소득세", "rate": "폐지", "when": "시행 전에 폐지됨", "law": "소득세법 제87조의2 삭제(2024.12.31)"},
]


# ---------- 설정 ----------
def _num(v, lo: float, hi: float, what: str) -> float:
    x = to_float(v, float("nan"))
    if not (lo <= x <= hi):
        raise ValueError(f"{what}는 {lo:g}~{hi:g} 사이 숫자여야 합니다.")
    return x


def normalize_tax_settings(data) -> dict:
    """검증 + 정규화. 없는 키는 기본값. 잘못되면 ValueError(화면에 그대로 보여줄 메시지)."""
    if not isinstance(data, dict):
        raise ValueError("세금 설정 형식이 잘못되었습니다.")
    d = DEFAULT_TAX_SETTINGS
    tt = data.get("transaction_tax_pct", d["transaction_tax_pct"])
    if not isinstance(tt, dict):
        raise ValueError("증권거래세 형식이 잘못되었습니다.")
    classes = data.get("domestic_classes", {})
    if not isinstance(classes, dict):
        raise ValueError("국내 종목 종류 형식이 잘못되었습니다.")
    clean_classes = {}
    for sym, cls in classes.items():
        if cls not in DOMESTIC_CLASSES:
            raise ValueError(f"{sym}의 종류가 잘못되었습니다.")
        clean_classes[str(sym).strip().upper()[:20]] = cls
    return {
        "transaction_tax_pct": {m: _num(tt.get(m, d["transaction_tax_pct"][m]), 0, 5, f"{m} 증권거래세율")
                                for m in d["transaction_tax_pct"]},
        "overseas_gain_rate_pct": _num(data.get("overseas_gain_rate_pct", d["overseas_gain_rate_pct"]), 0, 100, "해외 양도세율"),
        "gain_deduction_krw": _num(data.get("gain_deduction_krw", d["gain_deduction_krw"]), 0, 1e10, "기본공제"),
        "etf_other_rate_pct": _num(data.get("etf_other_rate_pct", d["etf_other_rate_pct"]), 0, 100, "기타 ETF 세율"),
        "dividend_rate_pct": _num(data.get("dividend_rate_pct", d["dividend_rate_pct"]), 0, 100, "배당 세율"),
        "financial_income_threshold_krw": _num(data.get("financial_income_threshold_krw", d["financial_income_threshold_krw"]),
                                               0, 1e12, "종합과세 기준금액"),
        "is_major_shareholder": bool(data.get("is_major_shareholder", False)),
        "use_manual_realized": bool(data.get("use_manual_realized", False)),
        "use_manual_financial_income": bool(data.get("use_manual_financial_income", False)),
        "realized_overseas_gain_ytd_krw": _num(data.get("realized_overseas_gain_ytd_krw", 0), -1e12, 1e12, "올해 실현 해외 손익"),
        "financial_income_ytd_krw": _num(data.get("financial_income_ytd_krw", 0), 0, 1e12, "올해 이자·배당 합계"),
        "domestic_classes": clean_classes,
    }


def load_tax_settings(store: UserStore) -> dict:
    data = store.read_json(TAX_SETTINGS_FILE)
    try:
        return normalize_tax_settings(data if isinstance(data, dict) else {})
    except ValueError:
        return normalize_tax_settings({})


def save_tax_settings(store: UserStore, settings: dict) -> None:
    store.write_json(TAX_SETTINGS_FILE, settings)


# ---------- 계산 ----------
def guess_domestic_class(name: str) -> str:
    """이름으로 국내 종목 종류 추정: ETF 브랜드로 시작하면 ETF, 해외·채권·원자재 등 단어가 있으면 기타 ETF."""
    if not ETF_BRANDS.search(name.strip()):
        return "stock"
    return "etf_other" if OTHER_ETF_HINTS.search(name) else "etf_equity"


def _gain_tax(total_gain: float, deduction: float, rate_pct: float) -> float:
    return max(0.0, total_gain - deduction) * rate_pct / 100


def estimate_taxes(holdings: dict, settings: dict, kr_info: dict[str, dict]) -> dict:
    """보유종목을 지금 전부 판다고 할 때의 세금 추정. 행별 세금 + 요약.
    해외(와 대주주면 국내 주식) 양도세는 연간 합산·공제라서, 행별 금액은 "이 종목만 추가로 판다면 늘어나는 세금"."""
    s = settings
    positions = holdings["stocks"] + holdings["cash"]["stocks"]
    realized = s["realized_overseas_gain_ytd_krw"]
    deduction, gain_rate = s["gain_deduction_krw"], s["overseas_gain_rate_pct"]

    rows, pooled_gains = [], []
    for r in positions:
        sym, gain, value = r["심볼"], r["손익(원)"], r["평가금액(원)"]
        row = {"symbol": sym, "name": r["종목"], "value_krw": value, "gain_krw": gain, "tax_krw": 0.0, "notes": []}
        if r["통화"] != "KRW":
            row["kind"] = "overseas"
            row["kind_label"] = "해외 주식·ETF"
            pooled_gains.append((row, gain))
            row["notes"].append("원화 손익 = 외화 손익 × 현재 환율(산 날 환율 차이 미반영)")
        else:
            cls = s["domestic_classes"].get(sym) or guess_domestic_class(r["종목"])
            row["kind"], row["kind_label"] = cls, DOMESTIC_CLASSES[cls]
            row["class_guessed"] = sym not in s["domestic_classes"]
            if cls == "stock":
                market = (kr_info.get(sym) or {}).get("market", "KOSPI")
                pct = s["transaction_tax_pct"].get(market, s["transaction_tax_pct"]["KOSPI"])
                row["tax_krw"] = value * pct / 100
                row["notes"].append(f"증권거래세 {pct:g}% ({market})")
                if s["is_major_shareholder"]:
                    pooled_gains.append((row, gain))
                    row["notes"].append("대주주: 양도세 합산(22%로 근사)")
            elif cls == "etf_other":
                row["tax_krw"] = max(0.0, gain) * s["etf_other_rate_pct"] / 100
                row["notes"].append(f"매매차익 {s['etf_other_rate_pct']:g}% 기준 상한(실제는 더 적을 수 있음)")
            else:
                row["notes"].append("국내주식형 ETF: 매매차익 비과세")
        rows.append(row)

    # 여기까지 tax_krw는 팔 때 바로 떼는 세금(증권거래세·기타 ETF)뿐.
    domestic_sell_tax = sum(r["tax_krw"] for r in rows)

    # 연간 합산 양도세: 올해 이미 실현한 손익 + 지금 다 팔 때 손익 − 공제.
    pooled_total = sum(g for _, g in pooled_gains)
    base_tax = _gain_tax(realized, deduction, gain_rate)
    all_tax = _gain_tax(realized + pooled_total, deduction, gain_rate)
    for row, gain in pooled_gains:
        row["tax_krw"] += _gain_tax(realized + gain, deduction, gain_rate) - base_tax

    total_value = sum(r["value_krw"] for r in rows)
    return {
        "rows": rows,
        "summary": {
            "realized_overseas_gain_ytd_krw": realized,
            "tax_on_realized_krw": base_tax,
            "deduction_left_krw": max(0.0, deduction - max(0.0, realized)),
            "unrealized_pooled_gain_krw": pooled_total,
            "gain_tax_if_sell_all_krw": all_tax - base_tax,
            "gain_tax_year_total_if_sell_all_krw": all_tax,
            "domestic_tax_if_sell_all_krw": domestic_sell_tax,
            "tax_if_sell_all_krw": (all_tax - base_tax) + domestic_sell_tax,
            "value_krw": total_value,
            "financial_income_ytd_krw": s["financial_income_ytd_krw"],
            "financial_income_left_krw": max(0.0, s["financial_income_threshold_krw"] - s["financial_income_ytd_krw"]),
        },
    }


def sell_tax_rate_by_symbol(estimate: dict) -> dict[str, float]:
    """종목별 "판 금액 1원당 세금" 근사(전량 매도 세금 ÷ 평가금액). 일부만 팔 때 세금을 비례로 추정하는 데 쓴다."""
    return {r["symbol"]: (r["tax_krw"] / r["value_krw"] if r["value_krw"] > 0 else 0.0) for r in estimate["rows"]}
