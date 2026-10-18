"""거시 사례집: 국면별 이야기(해석)와, 그 국면에서 실제 시세가 어떻게 움직였는지(계산), 통념이 맞았는지 점검, 지금과 닮은 국면 찾기.

- 사례의 사건·날짜는 널리 알려진 것만 넣었고, 이야기(story)와 요약(summary)은 **해석**이다. 숫자는 시세에서 계산해 이야기와 나란히 놓아
  서사가 틀리면 숫자로 드러나게 한다(예: "금리 오르면 금이 빠진다"는 통념이 금리 인상기에 실제로는 반대였던 경우).
- 통념 점검(conventions): 큰 변동이 있었던 국면에서만 판정하고(금리 ±25bp, 달러 ±3%, 증시 −10% 이하 또는 VIX +50% 이상이면서 증시 하락, 금 ±3% 미만은 뚜렷하지 않음),
  기준을 넘지 않으면 판정하지 않는다.
- 지금과 닮은 국면(similar_phases): 최근 126거래일(약 6개월) 요인 변화를 각 요인의 역사 속 126거래일 변화 분포(σ)로 나눈 z로 바꿔 국면의 z와 비교한다.
  국면은 길이가 다르므로 σ를 √(길이/126)로 보정한다. 금(결과로 보는 자산)은 비교에서 뺀다. 요인이 4개 미만이면 판단하지 않는다.
- 외부 호출은 하지 않는다(시세는 호출하는 쪽이 넘긴다). 시세는 {날짜: 값}, 금은 일봉(2000년~)과 월평균(1975년~)을 함께 받아 가능한 쪽을 쓴다.
같이 움직인 기록이며 인과의 증명이 아니고, 닮은 국면이 같은 결말을 보장하지 않는다.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from datetime import date, timedelta

HORIZON = 126                  # 거래일. 지금과 비교할 최근 변화의 길이(약 6개월)
MIN_SHARED = 4                 # 비교에 쓸 요인이 이보다 적으면 닮은 국면을 판단하지 않는다
MIN_PHASE_TRADING_DAYS = 15    # 이보다 짧은 국면은 비교에서 뺀다
Z_CAP = 4.0
AFTER_DAYS = 91                # 국면이 끝난 뒤 약 3개월
DAILY_STALE, MONTHLY_STALE = 7, 35
GOLD_DAILY_FROM = date(2000, 9, 1)

# 비교·표시에 쓰는 기준 요인: (키, 이름, 종류). level은 값의 차이(금리 %p → bp로 표시), price는 로그 수익률(%로 표시).
REFERENCE = [
    ("gold", "금", "price"), ("tnx", "10년 금리", "level"), ("irx", "3개월 금리", "level"), ("dxy", "달러", "price"),
    ("spx", "S&P500", "price"), ("vix", "VIX", "price"), ("oil", "유가(WTI)", "price"),
    ("xlf", "금융업종(XLF)", "price"),
    ("FRED:DFF", "연방기금금리", "level"), ("FRED:DGS2", "2년물 금리", "level"), ("FRED:DFII10", "10년 실질금리", "level"), ("FRED:T10YIE", "기대인플레이션", "level"),
    ("FRED:CPIYOY", "CPI 전년비", "level"), ("FRED:UNRATE", "실업률", "level"), ("FRED:WALCL", "연준 총자산", "price"),
]
SLOW_KEYS = frozenset({"FRED:CPIYOY", "FRED:UNRATE", "FRED:WALCL"})   # 월간·주간 지표: 국면별 변화에만 쓴다(126거래일 단위 닮은 정도 계산에서는 뺀다)
SLOW_STALE = 45
LABELS = {k: label for k, label, _ in REFERENCE}
KINDS = {k: kind for k, _l, kind in REFERENCE}
MATCH_EXCLUDE = {"gold"} | SLOW_KEYS       # 결과로 보는 자산과, 관측 간격이 달라 126거래일 단위로 비교할 수 없는 월간·주간 지표는 닮은 정도 계산에서 뺀다


def _d(y, m, day):
    return date(y, m, day)


CASES = [
    {"id": "gfc-2008", "title": "2004~2009 서브프라임·금융위기·QE1",
     "keywords": ("2004", "2005", "2006", "2007", "2008", "2009", "서브프라임", "금융위기", "리먼", "베어스턴스", "qe1", "금융패닉"),
     "summary": "금리를 천천히 올리던 시기에 주택시장이 꺾이고, 신용경색과 리먼 파산으로 패닉이 온 뒤 양적완화로 반등한 한 사이클.",
     "phases": [
         {"name": "연준 금리 인상기", "start": _d(2004, 6, 30), "end": _d(2006, 6, 29),
          "story": "연준이 2004년 6월부터 2006년 6월까지 17차례 연속으로 금리를 올렸다(1.00%→5.25%). 주택시장은 이 시기에 과열됐다."},
         {"name": "인상 종료·주택시장 균열", "start": _d(2006, 6, 29), "end": _d(2007, 8, 8),
          "story": "인상이 멈췄고 미국 주택가격이 2006년 중반부터 꺾이면서 서브프라임 연체가 늘기 시작했다."},
         {"name": "신용경색과 금리 인하 시작", "start": _d(2007, 8, 9), "end": _d(2008, 3, 14),
          "story": "2007년 8월 9일 BNP파리바가 펀드 환매를 중단하며 신용경색이 시작됐다. 연준은 9월 18일 첫 인하를 했고 2008년 3월 베어스턴스가 구제됐다."},
         {"name": "베어스턴스~리먼 파산~금융 패닉", "start": _d(2008, 3, 17), "end": _d(2008, 11, 20),
          "story": "2008년 9월 15일 리먼브러더스가 파산하며 패닉이 왔다. 달러 현금 수요가 급증해 달러가 오르고 위험자산과 함께 금도 팔렸다는 해석이 일반적이다."},
         {"name": "QE1 이후", "start": _d(2008, 11, 25), "end": _d(2009, 12, 2),
          "story": "2008년 11월 25일 연준이 정부기관 채권·주택담보증권 매입(QE1)을 발표했고 12월에 기준금리를 사실상 0으로 내렸다. 이후 달러 약세와 증시 반등이 나타났다."},
     ]},
    {"id": "taper-2013", "title": "2012~2013 QE3와 테이퍼 텐트럼",
     "keywords": ("2012", "2013", "qe3", "테이퍼", "텐트럼", "버냉키"),
     "summary": "무제한 양적완화 속에서도 금은 꺾였고, 양적완화 축소 시사에 장기금리가 급등한 시기.",
     "phases": [
         {"name": "QE3(무제한 양적완화) 시작", "start": _d(2012, 9, 13), "end": _d(2013, 5, 21),
          "story": "2012년 9월 13일 연준이 무기한 양적완화(QE3)를 시작했다. 증시는 올랐지만 금은 2013년 4월에 급락했다."},
         {"name": "테이퍼링 시사와 금리 급등", "start": _d(2013, 5, 21), "end": _d(2013, 9, 5),
          "story": "2013년 5월 22일 버냉키 의장이 의회에서 양적완화 축소 가능성을 언급하자 장기금리가 급등하고 신흥국 통화가 약세를 보였다."},
         {"name": "테이퍼링 연기~개시", "start": _d(2013, 9, 17), "end": _d(2013, 12, 18),
          "story": "연준은 9월 18일 축소를 연기했다가 12월 18일 월 100억 달러 규모 축소를 발표하며 시작했다."},
     ]},
    {"id": "china-2015", "title": "2015~2016 중국 쇼크와 첫 금리 인상",
     "keywords": ("2015", "2016", "위안화", "중국쇼크", "첫인상"),
     "summary": "위안화 절하와 중국 증시 불안이 글로벌 증시를 흔들었고, 연준의 9년 만의 첫 인상과 유가 급락이 겹친 시기.",
     "phases": [
         {"name": "위안화 평가절하·글로벌 증시 급락", "start": _d(2015, 8, 10), "end": _d(2015, 9, 30),
          "story": "2015년 8월 11일 중국이 위안화를 평가절하했고 8월 24일 미국 증시가 장중 급락했다."},
         {"name": "연준 첫 인상·유가 급락", "start": _d(2015, 12, 15), "end": _d(2016, 2, 11),
          "story": "2015년 12월 16일 연준이 9년 만에 처음 금리를 올렸고, 유가가 2016년 2월 초까지 크게 떨어지며 증시가 흔들렸다."},
     ]},
    {"id": "fed-2018", "title": "2018~2019 긴축 공포와 연준의 방향 전환",
     "keywords": ("2018", "2019", "피벗", "긴축공포", "파월"),
     "summary": "연준 긴축에 대한 공포로 4분기에 증시가 급락한 뒤, 연준이 방향을 바꾸고 2019년 7월 인하로 이어진 시기.",
     "phases": [
         {"name": "긴축 공포 4분기 급락", "start": _d(2018, 10, 2), "end": _d(2018, 12, 24),
          "story": "2018년 10월 파월 의장이 중립금리까지는 갈 길이 멀다고 말한 것으로 받아들여지며 증시가 급락했고 12월 24일 저점을 찍었다."},
         {"name": "방향 전환(피벗)과 인하", "start": _d(2018, 12, 24), "end": _d(2019, 7, 31),
          "story": "2019년 1월 파월 의장이 '인내심'을 언급하며 긴축 속도를 늦췄고, 7월 31일 10년 만에 금리를 인하했다."},
     ]},
    {"id": "covid-2020", "title": "2020 코로나 패닉과 무제한 양적완화",
     "keywords": ("2020", "코로나", "covid", "무제한qe", "팬데믹"),
     "summary": "패닉 때는 금도 함께 급락했지만, 무제한 양적완화와 제로금리 이후 금이 사상 최고가를 찍은 뒤 금리 반등기에 내려온 시기.",
     "phases": [
         {"name": "코로나 패닉", "start": _d(2020, 2, 19), "end": _d(2020, 3, 23),
          "story": "미국 증시가 한 달 만에 3분의 1 넘게 빠졌다. 3월 중순에는 달러 현금 수요로 금도 함께 급락했다."},
         {"name": "무제한 QE~금 사상 최고", "start": _d(2020, 3, 23), "end": _d(2020, 8, 6),
          "story": "2020년 3월 23일 연준이 무제한 양적완화를 발표했다. 금리는 사실상 0에 머물고 달러가 약해지며 금이 8월에 당시 사상 최고가를 기록했다."},
         {"name": "경기 회복 기대와 금리 반등", "start": _d(2020, 8, 6), "end": _d(2021, 3, 31),
          "story": "백신 기대와 대규모 재정지원으로 장기금리가 빠르게 반등했고 금은 내려왔다."},
     ]},
    {"id": "inflation-2022", "title": "2021~2023 인플레이션과 급격한 긴축",
     "keywords": ("2021", "2022", "인플레이션", "급격한긴축", "우크라이나", "긴축기"),
     "summary": "인플레이션 대응으로 연준이 가파르게 금리를 올려 주식·채권·금이 함께 떨어졌다가, 정점 확인 후 반등한 시기.",
     "phases": [
         {"name": "긴축 예고", "start": _d(2021, 11, 3), "end": _d(2022, 3, 15),
          "story": "2021년 11월 3일 연준이 테이퍼링 시작을 발표했고, 2022년 2월 24일 러시아의 우크라이나 침공으로 유가가 급등했다."},
         {"name": "급격한 금리 인상기", "start": _d(2022, 3, 16), "end": _d(2022, 10, 14),
          "story": "2022년 3월 16일 첫 인상 이후 0.75%p씩 큰 폭으로 올렸다. 달러가 강세를 보이며 주식·채권·금이 함께 하락했다."},
         {"name": "정점 확인 후 반등", "start": _d(2022, 10, 14), "end": _d(2023, 2, 2),
          "story": "10월 중순 이후 인플레이션 둔화 기대로 금리 상승이 꺾이고 달러가 약해졌다."},
     ]},
    {"id": "svb-2023", "title": "2023 실리콘밸리은행(SVB) 파산",
     "keywords": ("svb", "실리콘밸리", "은행위기", "퍼스트리퍼블릭"),
     "summary": "은행 위기로 안전자산 선호가 커져 금리가 급락하고 금이 올랐던 시기.",
     "phases": [
         {"name": "SVB 파산과 은행 위기", "start": _d(2023, 3, 8), "end": _d(2023, 5, 4),
          "story": "2023년 3월 10일 SVB가 파산했고 시그니처은행과 퍼스트리퍼블릭으로 불안이 번졌다. 안전자산 선호로 국채금리가 떨어졌다."},
     ]},
    {"id": "carry-2024", "title": "2024 엔캐리 청산",
     "keywords": ("엔캐리", "2024", "닛케이", "일본은행"),
     "summary": "일본은행의 금리 인상과 미국 고용 부진이 겹쳐 엔화가 급등하고 증시가 하루 만에 급락했다가 곧 진정된 사건.",
     "phases": [
         {"name": "엔캐리 청산 쇼크", "start": _d(2024, 7, 31), "end": _d(2024, 8, 9),
          "story": "7월 31일 일본은행이 금리를 올렸고 미국 고용 지표가 부진했다. 8월 5일 닛케이지수가 하루 12% 급락하고 VIX가 급등했지만 곧 진정됐다."},
     ]},
    {"id": "tariff-2025", "title": "2025 미국 상호관세 쇼크",
     "keywords": ("2025", "관세", "상호관세", "트럼프", "해방의날"),
     "summary": "관세 발표로 증시가 급락하고 달러가 약해졌다가 유예 발표로 반등한 시기. 이 기간 금은 사상 최고가를 경신했다.",
     "phases": [
         {"name": "상호관세 발표와 90일 유예", "start": _d(2025, 4, 1), "end": _d(2025, 4, 30),
          "story": "2025년 4월 2일 상호관세가 발표되며 증시가 급락했고, 4월 9일 90일 유예 발표로 반등했다. 달러가 약해지고 금이 4월 하순에 사상 최고가를 기록했다."},
     ]},
    {"id": "volcker-1979", "title": "1979~1983 볼커 긴축과 금 폭락",
     "keywords": ("1979", "1980", "1981", "1982", "1983", "볼커", "스태그플레이션", "오일쇼크"),
     "summary": "인플레이션을 잡으려 연준이 초고금리로 돌아서자 금이 반 토막 났고, 증시는 1982년 8월 바닥을 찍고 대세 상승을 시작한 시기. 금은 월평균 가격 기준이다.",
     "phases": [
         {"name": "긴축 전환과 금 폭등", "start": _d(1979, 9, 28), "end": _d(1980, 1, 31),
          "story": "1979년 10월 6일 볼커 의장이 통화량 목표 긴축으로 전환했다. 인플레이션 공포 속에 금은 1980년 1월에 당시 사상 최고가를 찍었다."},
         {"name": "초고금리 지속·금 폭락", "start": _d(1980, 1, 31), "end": _d(1982, 8, 12),
          "story": "연방기금금리가 한때 20% 안팎까지 올랐고 금은 절반 아래로 떨어졌다. 주식은 1982년 8월 12일 바닥을 찍었다."},
         {"name": "금리 하락과 증시 대세 상승", "start": _d(1982, 8, 12), "end": _d(1983, 12, 30),
          "story": "금리가 내려가며 증시가 대세 상승을 시작했다."},
     ]},
    {"id": "asia-1997", "title": "1997~1998 아시아 외환위기와 LTCM",
     "keywords": ("1997", "1998", "아시아", "외환위기", "imf", "ltcm", "러시아"),
     "summary": "태국에서 시작된 외환위기가 한국을 포함한 아시아로 번졌고, 러시아 디폴트와 LTCM 사태로 글로벌 시장이 흔들린 시기.",
     "phases": [
         {"name": "아시아 외환위기", "start": _d(1997, 7, 2), "end": _d(1998, 1, 30),
          "story": "1997년 7월 2일 태국이 바트화 변동환율제로 전환하며 시작됐다. 한국은 11월에 IMF 구제금융을 신청했다."},
         {"name": "러시아 디폴트·LTCM 사태", "start": _d(1998, 8, 17), "end": _d(1998, 10, 15),
          "story": "1998년 8월 17일 러시아가 디폴트를 선언했고 9월에 헤지펀드 LTCM이 구제 조치를 받았다. 연준은 가을에 세 차례 금리를 내렸다."},
     ]},
    {"id": "dotcom-2000", "title": "2000~2002 닷컴 버블 붕괴와 금리 인하",
     "keywords": ("2000", "2001", "2002", "닷컴", "버블", "9.11", "911"),
     "summary": "기술주 거품이 꺼지고 연준이 2001년에 연속으로 금리를 내렸지만 증시는 2002년 10월에야 바닥을 찍은 시기.",
     "phases": [
         {"name": "거품 정점과 붕괴 시작", "start": _d(2000, 3, 10), "end": _d(2001, 1, 2),
          "story": "2000년 3월 10일 나스닥이 정점을 찍은 뒤 기술주가 급락하기 시작했다."},
         {"name": "연속 금리 인하·9·11", "start": _d(2001, 1, 3), "end": _d(2001, 12, 31),
          "story": "연준이 2001년에 11차례 금리를 내렸고 9월 11일 테러가 일어났다. 12월에는 엔론이 파산했다."},
         {"name": "바닥 탐색", "start": _d(2002, 1, 1), "end": _d(2002, 10, 9),
          "story": "기업 회계 스캔들이 이어졌고 S&P500은 2002년 10월 9일에 저점을 찍었다."},
     ]},
]
CASE_BY_ID = {c["id"]: c for c in CASES}


# ---------------------------------------------------------------------------
# 시세 → 국면 변화
# ---------------------------------------------------------------------------
def prep(series: dict | None) -> tuple[list, dict] | None:
    """정렬한 날짜 목록과 시세를 한 번만 준비한다. 비었으면 None."""
    if not series:
        return None
    return sorted(series), series


def _value_at(prepared, day: date, stale: int):
    """day 이전(포함) 가장 가까운 값과 그 날짜. 시세가 day보다 늦게 시작하거나 stale일보다 오래 끊겼으면 (None, None)."""
    dates, series = prepared
    i = bisect_right(dates, day)
    if i == 0 or (day - dates[i - 1]).days > stale:
        return None, None
    return series[dates[i - 1]], dates[i - 1]


def change(prepared, kind: str, start: date, end: date, stale: int = DAILY_STALE) -> float | None:
    """start → end 변화. level은 값의 차이(%p), price는 로그 수익률. 시세가 모자라면 None."""
    if prepared is None:
        return None
    a, _ = _value_at(prepared, start, stale)
    b, _ = _value_at(prepared, end, stale)
    if a is None or b is None:
        return None
    if kind == "level":
        return b - a
    return math.log(b / a) if a > 0 and b > 0 else None


def display(value: float, kind: str) -> tuple[float, str]:
    return (value * 100, "bp") if kind == "level" else ((math.exp(value) - 1) * 100, "%")


def gold_change(daily, monthly, start: date, end: date) -> float | None:
    """금 변화: 두 날짜가 모두 2000년 9월 이후면 선물 일봉, 아니면 월평균 가격(1975년~, 한 달에 한 점이라 거칠다)."""
    if daily is not None and start >= GOLD_DAILY_FROM:
        got = change(daily, "price", start, end)
        if got is not None:
            return got
    return change(monthly, "price", start, end, MONTHLY_STALE) if monthly is not None else None


def phase_moves(refs: dict, daily_gold, monthly_gold, start: date, end: date) -> list[dict]:
    """국면 동안의 기준 요인 변화. refs: {키: 준비된 시세}. 시세가 없는 요인은 건너뛴다. [{key, label, value, unit, raw}]."""
    out = []
    for key, label, kind in REFERENCE:
        raw = (gold_change(daily_gold, monthly_gold, start, end) if key == "gold"
               else change(refs.get(key), kind, start, end, SLOW_STALE if key in SLOW_KEYS else DAILY_STALE))
        if raw is None:
            continue
        value, unit = display(raw, kind)
        out.append({"key": key, "label": label, "value": value, "unit": unit, "raw": raw})
    return out


def forward_change(prepared, kind: str, end: date, days: int = AFTER_DAYS, stale: int = DAILY_STALE) -> float | None:
    """국면이 끝난 뒤 약 3개월 변화(%/bp). 그만큼의 시세가 없으면 None."""
    if prepared is None:
        return None
    target = end + timedelta(days=days)
    if target > prepared[0][-1]:
        return None
    raw = change(prepared, kind, end, target, stale)
    return None if raw is None else display(raw, kind)[0]


# ---------------------------------------------------------------------------
# 통념 점검
# ---------------------------------------------------------------------------
RATE_BP, DOLLAR_PCT, EQUITY_PCT, VIX_PCT, GOLD_CLEAR_PCT = 25.0, 3.0, -10.0, 50.0, 3.0


def conventions(moves: list[dict]) -> list[dict]:
    """금에 관한 통념이 이 국면에서 맞았는지. 큰 변동이 있었던 국면에서만 판정한다.
    반환: [{"rule", "held": True|False|None(금 움직임이 뚜렷하지 않음), "detail"}]."""
    m = {x["key"]: x["value"] for x in moves}
    gold = m.get("gold")
    if gold is None:
        return []
    clear = abs(gold) >= GOLD_CLEAR_PCT
    out = []

    def add(rule, expected_down_when_up: bool, driver: float, detail: str):
        held = None if not clear else ((driver > 0) != (gold > 0)) if expected_down_when_up else ((driver > 0) == (gold > 0))
        out.append({"rule": rule, "held": held, "detail": detail + f", 금 {gold:+.0f}%"})
    if "tnx" in m and abs(m["tnx"]) >= RATE_BP:
        add("금리가 오르면 금이 빠지고 내리면 오른다", True, m["tnx"], f"10년 금리 {m['tnx']:+.0f}bp")
    if "FRED:DFII10" in m and abs(m["FRED:DFII10"]) >= RATE_BP:
        add("실질금리가 오르면 금이 빠지고 내리면 오른다", True, m["FRED:DFII10"], f"10년 실질금리 {m['FRED:DFII10']:+.0f}bp")
    if "dxy" in m and abs(m["dxy"]) >= DOLLAR_PCT:
        add("달러가 강하면 금이 빠지고 약하면 오른다", True, m["dxy"], f"달러 {m['dxy']:+.0f}%")
    # 증시가 크게 빠졌거나, VIX가 급등하면서 증시도 내렸을 때만 위험 회피로 본다(증시가 오르는 중의 VIX 상승은 해당 없음)
    risk_off = ("spx" in m and m["spx"] <= EQUITY_PCT) or ("vix" in m and m["vix"] >= VIX_PCT and m.get("spx", 0.0) <= 0)
    if risk_off:
        add("위험을 피할 때(증시 급락·VIX 급등) 금은 안전자산으로 오른다", False, 1.0,
            "증시 " + (f"{m['spx']:+.0f}%" if "spx" in m else "-") + " · VIX " + (f"{m['vix']:+.0f}%" if "vix" in m else "-"))
    return out


# ---------------------------------------------------------------------------
# 지금과 닮은 국면
# ---------------------------------------------------------------------------
def sigma(prepared, kind: str, horizon: int = HORIZON) -> float | None:
    """그 요인의 역사 속 126거래일 변화(level은 차이, price는 로그)의 표준편차. 겹치지 않게 21거래일 간격으로 뽑는다."""
    if prepared is None:
        return None
    dates, series = prepared
    vals = []
    for i in range(horizon, len(dates), 21):
        a, b = series[dates[i - horizon]], series[dates[i]]
        if kind == "level":
            vals.append(b - a)
        elif a > 0 and b > 0:
            vals.append(math.log(b / a))
    return statistics.pstdev(vals) if len(vals) >= 12 else None


def current_changes(refs: dict, horizon: int = HORIZON) -> dict[str, float]:
    """각 기준 요인의 최근 126거래일 변화(원 단위: level 차이·price 로그). 시세가 최신이 아니거나 모자라면 건너뛴다."""
    out = {}
    latest = max((p[0][-1] for p in refs.values() if p), default=None)
    for key, _label, kind in REFERENCE:
        p = refs.get(key)
        if key in SLOW_KEYS or not p or len(p[0]) <= horizon or (latest - p[0][-1]).days > DAILY_STALE:
            continue
        a, b = p[1][p[0][-horizon - 1]], p[1][p[0][-1]]
        if kind == "level":
            out[key] = b - a
        elif a > 0 and b > 0:
            out[key] = math.log(b / a)
    return out


def similar_phases(refs: dict, daily_gold, monthly_gold, cases=None, top: int = 3, today: date | None = None) -> list[dict]:
    """지금(최근 6개월)과 요인 움직임이 가장 닮은 과거 국면 top개. 각 항목에 닮은 요인 비교와 그 뒤 3개월 금 변화를 담는다."""
    cases = cases or CASES
    now = current_changes(refs)
    sig = {k: sigma(refs.get(k), KINDS[k]) for k in now}
    keys = [k for k in now if k not in MATCH_EXCLUDE and sig.get(k)]
    now_z = {k: max(-Z_CAP, min(Z_CAP, now[k] / sig[k])) for k in keys}
    out = []
    for case in cases:
        for ph in case["phases"]:
            trading_days = (ph["end"] - ph["start"]).days * 252 / 365
            if trading_days < MIN_PHASE_TRADING_DAYS:
                continue
            scale = math.sqrt(trading_days / HORIZON)
            then, shared = {}, []
            for k in keys:
                raw = change(refs.get(k), KINDS[k], ph["start"], ph["end"])
                if raw is None:
                    continue
                then[k] = max(-Z_CAP, min(Z_CAP, raw / (sig[k] * scale)))
                shared.append(k)
            if len(shared) < MIN_SHARED:
                continue
            distance = math.sqrt(sum((now_z[k] - then[k]) ** 2 for k in shared) / len(shared))
            compare = []
            for k in sorted(shared, key=lambda k: abs(now_z[k] - then[k])):
                raw_then = change(refs.get(k), KINDS[k], ph["start"], ph["end"])
                compare.append({"key": k, "label": LABELS[k], "now": display(now[k], KINDS[k])[0], "then": display(raw_then, KINDS[k])[0],
                                "unit": display(0.0, KINDS[k])[1], "close": abs(now_z[k] - then[k]) < 1.0})
            after = forward_change(prep_gold(daily_gold, monthly_gold, ph["end"]), "price", ph["end"],
                                   stale=DAILY_STALE if ph["end"] >= GOLD_DAILY_FROM else MONTHLY_STALE)
            out.append({"case_id": case["id"], "case_title": case["title"], "phase": ph["name"], "start": ph["start"].isoformat(),
                        "end": ph["end"].isoformat(), "distance": distance, "shared": len(shared), "compare": compare, "gold_after_pct": after})
    out.sort(key=lambda r: r["distance"])
    return out[:top]


def prep_gold(daily, monthly, end: date):
    """국면 끝난 뒤의 금 시세: 끝이 2000년 9월 이후면 일봉, 아니면 월평균."""
    return daily if (daily is not None and end >= GOLD_DAILY_FROM) else monthly


# ---------------------------------------------------------------------------
# 사례 선택·국면 조립
# ---------------------------------------------------------------------------
def _compact(text: str) -> str:
    import re
    return re.sub(r"[\s.,!?~\-_'\"]+", "", str(text or "").lower())


def select_cases(question: str, cases=None) -> list[dict]:
    """질문에 사례 이름·연도·사건 키워드가 있으면 그 사례들(최대 3개). 연도만 겹치는 경우도 포함한다."""
    compact = _compact(question)
    if not compact:
        return []
    return [c for c in (cases or CASES) if any(k in compact for k in c["keywords"])][:3]


def build_case(case: dict, refs: dict, daily_gold, monthly_gold, asset: dict | None = None) -> dict:
    """사례 하나의 국면별 서사 + 계산된 변화 + 통념 점검. asset: 내 종목({"name", "prepared"})이 있으면 그 변화와 그 뒤 3개월도 붙인다."""
    phases = []
    for ph in case["phases"]:
        moves = phase_moves(refs, daily_gold, monthly_gold, ph["start"], ph["end"])
        item = {"name": ph["name"], "start": ph["start"].isoformat(), "end": ph["end"].isoformat(), "story": ph["story"],
                "moves": [{k: v for k, v in m.items() if k != "raw"} for m in moves], "conventions": conventions(moves)}
        gold_after = forward_change(prep_gold(daily_gold, monthly_gold, ph["end"]), "price", ph["end"],
                                    stale=DAILY_STALE if ph["end"] >= GOLD_DAILY_FROM else MONTHLY_STALE)
        item["gold_after_pct"] = gold_after
        if asset and asset.get("prepared"):
            raw = change(asset["prepared"], "price", ph["start"], ph["end"])
            item["asset_move_pct"] = display(raw, "price")[0] if raw is not None else None
            item["asset_after_pct"] = forward_change(asset["prepared"], "price", ph["end"])
        phases.append(item)
    return {"id": case["id"], "title": case["title"], "summary": case["summary"], "phases": phases}


def build(refs_raw: dict, daily_gold_raw, monthly_gold_raw, question: str = "", asset: dict | None = None, cases=None,
          today: date | None = None) -> dict:
    """도구 결과: 질문이 지정한 사례(있으면) + 지금과 닮은 국면. 시세는 {키: {날짜: 값}} 원본으로 받는다."""
    refs = {k: prep(v) for k, v in refs_raw.items()}
    daily, monthly = prep(daily_gold_raw), prep(monthly_gold_raw)
    if daily is not None:
        refs["gold"] = daily        # 현재 6개월 변화 계산에 쓰는 금(닮은 정도에서는 제외된다)
    chosen = select_cases(question, cases)
    asset_ready = {"name": asset["name"], "prepared": prep(asset.get("series"))} if asset else None
    return {
        "named": bool(chosen),
        "cases": [build_case(c, refs, daily, monthly, asset_ready) for c in chosen],
        "similar": similar_phases(refs, daily, monthly, cases),
        "available": [{"id": c["id"], "title": c["title"]} for c in (cases or CASES)],
        "asset": asset["name"] if asset else None,
        "has_fred": any(k.startswith("FRED:") for k in refs_raw if refs_raw[k]),
    }
