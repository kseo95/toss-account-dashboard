"""질문 문장 → 어떤 도구를 부를지 정하는 규칙(키워드·질문 해석·계획). agent_tools.py에서 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import re
import response as response_mod

from cases import CASES
from screener import DEFAULT_PERIODS, MAX_PERIOD, MAX_PERIODS, MIN_PERIOD, SECTOR_KO
from .common import _compact


# 질문 속 단어 → 부를 도구. 모두 공백 없는 소문자로 적는다(_compact와 같은 모양). 맞는 의도는 모두 실행한다.
WHY_STRICT = ("왜올", "왜떨", "왜빠", "왜하락", "왜상승", "왜내", "왜급", "왜움직", "이유", "원인", "때문")          # 이유를 묻는 말


MOVE_WORDS = ("급락", "급등", "폭락", "폭등", "빠졌", "빠진", "떨어졌", "떨어진", "올랐", "오른", "내렸", "하락했", "상승했", "움직였", "움직인", "무슨일")   # 움직임을 말할 뿐 이유를 묻는 건 아닐 수 있음


WHY_WORDS = WHY_STRICT + MOVE_WORDS


INTENT_WORDS = {
    "weekly_review": ("이번주", "금주", "위험", "주의", "신호", "점검", "체크"),
    "portfolio": ("리밸런싱", "목표비중", "비중맞", "자산구성", "포트폴리오", "얼마씩", "더사야", "더팔아야"),
    "outlook": ("뷰", "전망", "앞으로", "시나리오", "예측", "오를까", "내릴까", "오를것", "내릴것", "상승/하락", "상승·하락", "향후",
                "앞날", "떨어질까", "오르려나", "내리려나", "더갈까", "가능성"),
    "macro": ("요인", "금리", "달러", "유가", "환율", "영향", "이번주", "무엇을확인", "뭘확인") + WHY_WORDS,
    "news": ("소식", "뉴스", "이슈", "재료", "기사", "보도", "헤드라인", "이번주", "무엇을확인", "뭘확인") + WHY_WORDS,
    "etf": ("구성종목", "편입", "담고있", "담은종목", "종목별기여", "etf", "안에뭐", "무엇을담") + WHY_WORDS,
    "sector": ("섹터", "업종", "노출"),
    "drivers": ("영향을많이", "영향을가장", "가장영향", "영향받는", "영향을받", "영향받", "영향이큰", "영향큰", "영향주는", "영향을주는", "민감", "상관관계", "상관계수",
                "연관성", "연관된", "주요요인", "주요요소", "핵심요인", "핵심요소", "좌우", "결정하는", "움직이게하는", "움직이는요인", "요소", "요인순위", "영향순위"),
    "cases": ("사례집", "지금과비슷", "어느때와", "어떤때와", "서브프라임", "금융위기", "테이퍼", "텐트럼", "엔캐리", "관세", "볼커", "svb", "실리콘밸리",
              "닷컴", "ltcm", "아시아외환", "피벗", "스태그플레이션", "양적완화", "qe"),
    "history": ("과거", "역사", "예전", "그때", "당시", "사례", "쇼크", "충격", "위기", "리먼", "코로나", "닷컴", "이번처럼", "같은상황", "전에도"),
}


ENTITY_TOOLS = ("get_weekly_news", "get_macro_drivers", "get_outlook", "get_etf_breakdown")   # 종목 이름만 말했을 때 한꺼번에 보여 주는 도구


FOCUS_TOOLS = frozenset(ENTITY_TOOLS) | {"get_macro_history", "get_macro_cases", "get_asset_drivers", "get_drawdown_analysis", "get_valuation", "get_indicators"}                                                         # 질문에서 종목을 지정해 좁혀 볼 수 있는 도구


SIMILAR_WORDS = ("비슷", "닮", "유사", "같은시기", "같은때", "똑같")                                             # 견주는 말


NOW_WORDS = ("현재", "지금", "요즘", "오늘", "최근", "시장", "장세", "상황", "어느시", "어느때", "어느해", "어느국면", "언제", "어떤시", "어떤때", "어떤국면", "과거", "시점", "국면", "시기")   # 지금·시점을 가리키는 말


CASE_YEARS = frozenset(k for c in CASES for k in c["keywords"] if k.isdigit() and len(k) == 4)


CASE_WORDS = tuple(sorted({k for c in CASES for k in c["keywords"] if not k.isdigit()}))


INTENT_WORDS["cases"] = tuple(dict.fromkeys(INTENT_WORDS["cases"] + CASE_WORDS))   # 사례집의 사건 이름도 사례를 묻는 말로 본다


# 고점 대비 낙폭 질문: "금이 왜 고점 대비 빠졌어?" — 이번 주 움직임이 아니라 고점 이후 구간을 요인으로 나눠 본다
DRAWDOWN_WORDS = ("고점대비", "고점에서", "고점이후", "고점부터", "전고점", "최고점", "고점찍", "고점이", "고점보다", "낙폭", "고점아래", "고점근처", "얼마나빠졌", "얼마나떨어졌", "얼마나내렸")


def is_drawdown_query(text: str) -> bool:
    """고점 대비 낙폭을 묻는 말(왜 빠졌는지 포함)이면 낙폭 분석 도구로 본다. 업종 ETF 현황·스크리너 질문과 내 업종 노출 질문은 앞에서 따로 걸러진다."""
    compact = _compact(text)
    return any(w in compact for w in DRAWDOWN_WORDS)


# 이동평균 스크리너 질문: "주봉 ma 60/120/200/240 보다 밑에 있는 S&P500 소속 주식들은?" — 종목 하나가 아니라 여러 종목을 훑어 조건에 맞는 것을 찾는 질문
_MA_MENTION = re.compile(r"(주봉|일봉|주선|이평|이동평균|\bma\s*\d|\bma\b|\d+\s*(주|일)?\s*(선|이평|ma))", re.I)


_SCREEN_BELOW = ("밑", "아래", "이하", "하회", "이탈", "깨진", "깨졌", "못미", "낮은", "미만")


_SCREEN_ABOVE = ("위에", "위로", "위의", "위인", "이상", "상회", "돌파", "넘은", "넘어", "높은")


_SCREEN_SCOPE = ("s&p", "sp500", "에스앤피", "소속", "구성종목", "종목들", "주식들", "전체", "모든", "찾아", "리스트", "스크리", "어떤종목", "어떤주식", "어디", "내종목", "내가", "보유", "관심")


_SCREEN_WEAK_SCOPE = ("종목", "주식")                       # '종목' 한 마디는 특정 종목 코드가 같이 없을 때만 여러 종목을 훑는 말로 본다


_TICKER = re.compile(r"\b(?!MA\b|ETF\b)[A-Z]{2,5}\b")


_SCREEN_ANY = ("하나라도", "어느하나", "중하나", "일부", "한개라도", "하나만", "한개만")


_SCREEN_MINE = ("내종목", "내가", "보유", "관심", "내주식")


def is_screen_query(text: str) -> bool:
    """주봉 MA 같은 이동평균 말에 위·아래 조건과 '여러 종목을 훑는' 말이 함께 있으면 스크리너 질문으로 본다(종목 하나를 묻는 질문은 제외)."""
    compact = _compact(text)
    if not (_MA_MENTION.search(str(text or "")) and any(w in compact for w in _SCREEN_BELOW + _SCREEN_ABOVE)):
        return False
    return any(w in compact for w in _SCREEN_SCOPE) or (any(w in compact for w in _SCREEN_WEAK_SCOPE) and not _TICKER.search(str(text or "")))


def parse_screen_query(text: str) -> dict:
    """질문 문장 → 스크리너 인자 {universe, periods, side, match, sector}. 기간을 말하지 않으면 60·120·200·240주,
    아래/위를 말하지 않으면 아래, '하나라도'가 없으면 모두, 범위를 말하지 않으면 S&P500(내 종목이라고 하면 내 보유·관심)."""
    raw = str(text or "")
    compact = _compact(raw)
    cleaned = re.sub(r"s\s*&\s*p\s*500|sp\s*500|상위\s*\d+|\d+\s*(개|등|%|년|달|월|일간)", " ", raw, flags=re.I)
    numbers = [int(n) for n in re.findall(r"(?<![\d.])\d{1,3}(?![\d.])", cleaned) if MIN_PERIOD <= int(n) <= MAX_PERIOD]
    periods = sorted(set(numbers))[:MAX_PERIODS] or list(DEFAULT_PERIODS)
    below = [compact.find(w) for w in _SCREEN_BELOW if w in compact]
    above = [compact.find(w) for w in _SCREEN_ABOVE if w in compact]
    side = "above" if above and (not below or min(above) < min(below)) else "below"
    sp = any(w in compact for w in ("s&p", "sp500", "에스앤피"))
    universe = "mine" if (any(w in compact for w in _SCREEN_MINE) and not sp) else "sp500"
    sector = next((SECTOR_KO[k] for k in sorted(SECTOR_KO, key=len, reverse=True) if k in compact and k != "it"), "")
    return {"universe": universe, "periods": periods, "side": side, "match": "any" if any(w in compact for w in _SCREEN_ANY) else "all", "sector": sector}


# 미국 업종·섹터 ETF 상태·추세 질문: "지금 미국 업종별 etf들 상태 어떰?", "업종 ETF 잘나가는 순", "XLK 추세전환 됐어?" — 내 포트폴리오의 업종 노출 질문과 구분한다
_TREND_SECTOR_WORDS = ("업종", "섹터", "스파이더", "sector", "xlk", "xlc", "xly", "xlf", "xli", "xlv", "xlp", "xlu", "xle", "xlb", "xlre")


_TREND_WORDS = ("잘나가", "못나가", "잘나", "순위", "모멘텀", "추세", "로테이션", "강세", "약세", "전환", "꺾", "반등", "강한", "약한", "돈이몰", "상승세", "하락세", "주도", "소외")


_TREND_STATUS_WORDS = ("상태", "어때", "어떰", "어떻", "현황", "동향", "흐름", "분위기", "요즘", "지금")       # 상태를 묻는 말은 시장 ETF를 가리키는 말(etf·미국 등)이 같이 있을 때만


_TREND_MARKET_WORDS = ("etf", "미국", "spdr", "스파이더", "xl", "시장")


_TREND_EXPOSURE_WORDS = ("노출", "비중", "내포트폴리오", "매핑", "쏠림", "내종목", "내가")      # 내 포트폴리오의 업종 노출·비중 질문은 섹터 노출 도구로


def is_trend_query(text: str) -> bool:
    """미국 업종·섹터 ETF를 두고 잘나가는 순·추세·전환·모멘텀·상태를 묻는 말이면 추세 도구로 본다(내 노출·비중을 묻는 말은 제외)."""
    compact = _compact(text)
    if not any(w in compact for w in _TREND_SECTOR_WORDS) or any(w in compact for w in _TREND_EXPOSURE_WORDS):
        return False
    return any(w in compact for w in _TREND_WORDS) or (any(w in compact for w in _TREND_STATUS_WORDS) and any(w in compact for w in _TREND_MARKET_WORDS))


def parse_trend_query(text: str) -> dict:
    """질문 문장 → 추세 도구 인자 {universe, focus}. '내 종목·보유·관심'이면 내 종목, 그 밖은 업종 ETF.
    '단기·중기·장기'를 말하면 그 기간 순서로 보여 준다(상태 판정 기준은 그대로)."""
    compact = _compact(text)
    universe = "mine" if any(w in compact for w in ("내종목", "내가", "보유", "관심", "내주식")) else "sectors"
    focus = "short" if any(w in compact for w in ("단기", "최근꺾", "이번주", "이번달", "1개월")) else "long" if any(w in compact for w in ("장기", "진짜", "12개월", "1년", "200일", "주봉")) \
        else "mid" if any(w in compact for w in ("중기", "3개월", "6개월", "분기")) else "all"
    return {"universe": universe, "focus": focus}


# 조건부 반응 질문: "미국 금리가 오르면 금과 나스닥은 어떻게 됨?" — 요인이 오르거나 내릴 때 자산의 평소 반응(내 보유 종목 질문은 제외)
RESPONSE_UP = ("오르면", "올라가면", "상승하면", "급등하면", "높아지면", "치솟으면", "뛰면", "강해지면", "커지면", "오른다면", "올라간다면", "오를때", "상승할때", "올랐을때")


RESPONSE_DOWN = ("내리면", "내려가면", "떨어지면", "하락하면", "급락하면", "낮아지면", "약해지면", "작아지면", "내린다면", "떨어진다면", "내릴때", "하락할때", "떨어졌을때", "내렸을때", "빠지면")


RESPONSE_REACTION = ("어떻게", "어떻", "영향", "반응", "어찌", "어떤", "어때")


RESPONSE_OWN = ("내포트폴리오", "내종목", "보유", "내자산", "내가", "내계좌", "내돈")


def _response_parts(text: str):
    """(방향, 요인 (심볼, 이름, 종류), 요인 낱말 끝 위치, 연결어 위치) — 연결어(오르면…) 바로 앞에 나온 요인 낱말이 조건을 거는 요인. 못 찾으면 None."""
    raw = str(text or "").lower()
    compact = re.sub(r"[\s.,!?~'\"]+", "", raw)
    hits = [(compact.find(w), w, "up") for w in RESPONSE_UP if w in compact] + [(compact.find(w), w, "down") for w in RESPONSE_DOWN if w in compact]
    if not hits:
        return None
    conn, _word, direction = min(hits)
    masked = compact
    best = None
    for word in sorted(response_mod.FACTOR_WORDS, key=len, reverse=True):
        for m in re.finditer(re.escape(word), masked):
            if m.end() <= conn and (best is None or m.end() > best[0]):
                best = (m.end(), word)
            masked = masked[:m.start()] + "_" * (m.end() - m.start()) + masked[m.end():]
    if best is None:
        return None
    return direction, response_mod.FACTOR_WORDS[best[1]], best[0], conn


def is_response_query(text: str) -> bool:
    """요인이 오르면(내리면) 자산이 어떻게 되는지 묻는 말: 요인 낱말 + 오르면/내리면 같은 연결어 + (자산 이름이나 '어떻게·영향' 같은 반응 말). 내 보유 종목을 묻는 말은 제외."""
    compact = _compact(text)
    if any(w in compact for w in RESPONSE_OWN):
        return False
    parts = _response_parts(text)
    if parts is None:
        return False
    return any(w in compact for w in RESPONSE_REACTION) or bool(parse_response_query(text)["assets"])


def parse_response_query(text: str) -> dict:
    """질문 문장 → 조건부 반응 도구 인자 {factor, direction, assets, horizon_days}. 자산을 말하지 않으면 비운다(기본 자산 묶음)."""
    parts = _response_parts(text)
    raw = str(text or "")
    factor_name, direction = "", "up"
    spans = []
    if parts:
        direction, (symbol, label, _kind), _end, _conn = parts
        factor_name = symbol
    compact = _compact(raw)
    words = [w for w in raw.replace("?", " ").replace(",", " ").split()]
    assets, seen = [], set()
    factor_word = next((w for w in sorted(response_mod.FACTOR_WORDS, key=len, reverse=True) if parts and response_mod.FACTOR_WORDS[w] == parts[1] and w in compact), "")
    masked_tokens = []
    for token in words:                     # 조건을 거는 요인 낱말이 든 토큰("금리가")은 자산 후보에서 뺀다
        stripped = _compact(token)
        masked_tokens.append(" " if factor_word and factor_word in stripped and not any(a in stripped for a in response_mod.ASSET_WORDS if a != factor_word) else token)
    joined = " ".join(masked_tokens)
    candidates = [t for t in masked_tokens if t.strip()] + [joined]
    for token in candidates:
        found = response_mod.resolve_asset(token)
        if found and found[0] not in seen and not (parts and found[0] == parts[1][0]):
            seen.add(found[0])
            assets.append(found[1])
    horizon = 5 if any(w in compact for w in ("일주일", "한주", "주간", "1주")) else 63 if any(w in compact for w in ("3개월", "석달", "분기")) else 21
    return {"factor": factor_name, "direction": direction, "assets": assets[:response_mod.MAX_ASSETS], "horizon_days": horizon}


# 밸류에이션 질문: "AAPL 고평가야?", "삼성전자 PER 알려줘", "내 종목 저평가인 게 있어?" — PER·PSR·PBR·EPS·BPS·ROE와 비싼가·싼가
VALUATION_WORDS = ("고평가", "저평가", "과대평가", "과소평가", "밸류에이션", "밸류", "적정가", "적정주가", "비싸", "비싼", "싸다", "싼가", "싸냐", "저렴", "거품", "overvalue", "undervalue",
                   "주가수익비율", "주가순자산비율", "주가매출비율", "주당순이익", "주당순자산", "자기자본이익률")


_VALUATION_METRIC = re.compile(r"(?<![a-z])(per|pbr|psr|eps|bps|roe)(?![a-z])")


def is_valuation_query(text: str) -> bool:
    """고평가·저평가·밸류에이션 같은 말이나 PER·PBR·PSR·EPS·BPS·ROE가 나오는 질문. 이번 주 요인·고점 대비·업종 ETF 현황 질문은 앞에서 따로 걸러진다."""
    compact = _compact(text)
    return any(w in compact for w in VALUATION_WORDS) or bool(_VALUATION_METRIC.search(str(text or "").lower()))


# 기술적 보조지표 질문: "엔비디아 RSI 과매수야?", "TSLA MACD 골든크로스 났어?", "볼린저밴드 어때" — 종가로 계산하는 RSI·MACD·볼린저·이동평균 교차
INDICATOR_WORDS = ("볼린저", "과매수", "과매도", "보조지표", "골든크로스", "데드크로스", "이평선교차", "이동평균교차", "엠에이씨디")


_INDICATOR_LATIN = re.compile(r"(?<![a-z])(rsi|macd)(?![a-z])")


def is_indicator_query(text: str) -> bool:
    compact = _compact(text)
    return any(w in compact for w in INDICATOR_WORDS) or bool(_INDICATOR_LATIN.search(str(text or "").lower()))


def parse_indicator_query(text: str) -> dict:
    """질문 문장 → 지표 도구 인자 {focus, indicators}. 말한 지표만(보조지표라고 하거나 특정 지표를 말하지 않으면 전부)."""
    compact = _compact(text)
    raw = str(text or "").lower()
    picked = []
    if "rsi" in raw or "과매수" in compact or "과매도" in compact:
        picked.append("rsi")
    if "macd" in raw or "엠에이씨디" in compact:
        picked.append("macd")
    if "볼린저" in compact:
        picked.append("bollinger")
    if any(w in compact for w in ("골든크로스", "데드크로스", "이평선교차", "이동평균교차")):
        picked.append("ma_cross")
    return {"focus": str(text or ""), "indicators": [] if "보조지표" in compact else picked}


# 내가 정한 원칙·기준과 보유를 대조하는 질문: "내가 매매 원칙을 위반한 종목이 있어?", "목표 비중대로야?" — 대시보드에 설정해 둔 목표 비중·분할매수 전략을 읽는다
RULES_WORDS = ("내원칙", "매매원칙", "원칙을위반", "원칙위반", "원칙대로", "원칙에어긋", "원칙을어", "원칙을지", "원칙점검", "내가정한", "정해둔원칙", "정한원칙", "내기준대로", "목표비중대로", "목표대로", "전략단계")


def is_rules_query(text: str) -> bool:
    compact = _compact(text)
    return any(w in compact for w in RULES_WORDS)


# 매매 판단을 묻는 질문: "지금 엔비디아 사도 돼?", "UFO 팔까?", "계속 들고 있어도 돼?" — 사라·팔라는 결론은 내리지 않고 판단 재료(밸류에이션·보조지표·낙폭·내 기준)를 한 번에 모아 보여 준다.
# 주문 실행("사줘", "팔아줘")은 unknown_policy가 먼저 거절한다.
DECISION_WORDS = ("사도돼", "사도되", "사도괜찮", "사도될", "살까", "사야", "사볼까", "팔까", "팔아야", "팔아도", "팔지말", "들고있어도", "들고가도", "보유해도", "홀딩해도",
                  "매수해도", "매도해도", "매수할까", "매도할까", "추가매수", "더사도", "더살까", "담아도", "손절해야", "익절해야", "손절할까", "익절할까")


DECISION_TOOLS = ("get_valuation", "get_indicators", "get_drawdown_analysis", "get_my_rules")


DECISION_NOTICE = "매수·매도 결론은 내리지 않고 판단 재료만 모았습니다. 밸류에이션·보조지표·고점 대비 낙폭·내가 정한 기준을 보시고 직접 판단해 주세요."


def is_decision_query(text: str) -> bool:
    compact = _compact(text)
    return any(w in compact for w in DECISION_WORDS)


def plan_agent_query(question: str) -> dict:
    """짧은 한국어 질문을 허용된 읽기 도구 호출 계획으로 바꾼다(단어 일치 방식, 공백·유의어 허용)."""
    text = str(question or "").strip()
    if not text:
        raise ValueError("질문을 입력해 주세요.")
    compact = _compact(text)
    if is_rules_query(text):      # 내가 정해 둔 원칙(목표 비중·분할매수 전략)과 보유를 대조
        return {"supported": True, "intents": ["my_rules"], "tools": ["get_my_rules"]}
    if is_decision_query(text):   # "사도 돼?", "팔까?" — 결론 없이 판단 재료 묶음
        return {"supported": True, "intents": ["decision"], "tools": list(DECISION_TOOLS)}
    if is_indicator_query(text) and not is_trend_query(text) and not is_screen_query(text):   # RSI·MACD·볼린저·골든크로스 같은 보조지표 질문(아무 종목)
        return {"supported": True, "intents": ["indicators"], "tools": ["get_indicators"]}
    if is_valuation_query(text) and not is_trend_query(text) and not is_screen_query(text):   # 비싼가·싼가·PER 같은 밸류에이션 질문(보유·관심 종목)
        return {"supported": True, "intents": ["valuation"], "tools": ["get_valuation"]}
    if is_response_query(text):   # "금리가 오르면 금과 나스닥은?" — 요인이 움직일 때 자산의 평소 반응(보유 종목과 무관한 시장 질문)
        return {"supported": True, "intents": ["response"], "tools": ["get_factor_response"]}
    if is_screen_query(text):     # 여러 종목을 훑는 이동평균 질문은 이 도구 하나만(다른 도구의 단어와 겹쳐도 붙이지 않는다)
        return {"supported": True, "intents": ["screen"], "tools": ["get_ma_screen"]}
    if is_trend_query(text):      # 미국 업종 ETF의 상태·잘나가는 순·추세·전환 질문도 이 도구 하나만
        return {"supported": True, "intents": ["trend"], "tools": ["get_sector_trends"]}
    if is_drawdown_query(text) and not any(w in compact for w in ("앞으로", "전망", "오를까", "내릴까", "과거에", "리먼", "2008")):   # 앞날·과거 사례를 묻는 말은 해당 도구로
        return {"supported": True, "intents": ["drawdown"], "tools": ["get_drawdown_analysis"]}
    hit = {name: any(w in compact for w in words) for name, words in INTENT_WORDS.items()}
    if not hit["cases"]:   # 사례집에 있는 연도(2008·2020 등)가 말에 나오면 그 사례를 묻는 것으로 본다
        hit["cases"] = any(y in compact for y in CASE_YEARS)
    if not hit["cases"] and any(w in compact for w in SIMILAR_WORDS) and any(w in compact for w in NOW_WORDS):
        hit["cases"] = True        # "현재 시장상황이 어느 시점이랑 비슷해 보여?"처럼 지금을 과거와 견주는 말
    if hit["cases"] and not any(w in compact for w in ("쇼크", "충격", "같은변동", "큰변동", "이슈")):
        hit["history"] = False     # 사례를 묻는 말에는 사례집이 답한다(과거 충격 통계 도구는 '큰 변동·쇼크'를 직접 물을 때만)
    if hit["drivers"] and not any(w in compact for w in WHY_STRICT + ("이번주", "이번달", "이번주에")):
        # "금이 가장 영향을 많이 받는 요소는?"은 평소의 민감도 순위를 묻는 것: 이번 주 요인 분해·뉴스·ETF 도구는 붙이지 않는다('영향'·'요인' 단어가 겹쳐서 붙던 것)
        for name in ("macro", "news", "etf", "history"):
            hit[name] = False
    if hit["history"] and not any(w in compact for w in WHY_STRICT):
        # 과거 질문("금리 급등 때 어땠어")의 '급등'은 이유를 묻는 게 아니다: 움직임 단어만으로는 요인·뉴스·ETF 도구를 붙이지 않는다
        for name in ("macro", "news", "etf"):
            others = tuple(w for w in INTENT_WORDS[name] if w not in MOVE_WORDS)
            hit[name] = any(w in compact for w in others)
    tools, intents = [], []
    if hit["weekly_review"]:
        tools.extend(("get_portfolio_overview", "get_weekly_risk_report"))
        intents.append("weekly_review")
    if hit["portfolio"]:
        tools.append("get_portfolio_overview")
        intents.append("rebalancing" if "리밸런싱" in compact or "비중맞" in compact else "portfolio")
    for name, tool in (("outlook", "get_outlook"), ("macro", "get_macro_drivers"), ("news", "get_weekly_news"),
                       ("etf", "get_etf_breakdown"), ("history", "get_macro_history"), ("cases", "get_macro_cases"), ("drivers", "get_asset_drivers"),
                       ("sector", "get_sector_exposure")):
        if hit[name]:
            tools.append(tool)
            intents.append(name)
    tools = list(dict.fromkeys(tools))
    return {"supported": bool(tools), "intents": list(dict.fromkeys(intents)), "tools": tools}


def plan_for_holding() -> dict:
    """키워드는 없지만 질문이 내 종목을 가리킬 때: 그 종목의 이번 주 움직임·요인·보도·앞으로의 뷰(ETF면 구성 종목)를 한꺼번에."""
    return {"supported": True, "intents": ["holding"], "tools": list(ENTITY_TOOLS)}


# 의도 → 부를 도구와 화면에 보일 이름. 문장 유사도 매칭(intent_matcher)이 고른 의도와 사용자가 가르친 의도를 도구 호출 계획으로 바꾼다.
INTENT_PLANS = {
    "weekly": ["get_portfolio_overview", "get_weekly_risk_report", "get_macro_drivers", "get_weekly_news"],
    "rebalancing": ["get_portfolio_overview"],
    "sector": ["get_sector_exposure"],
    "news": ["get_weekly_news"],
    "why": ["get_macro_drivers", "get_weekly_news", "get_etf_breakdown"],
    "outlook": ["get_outlook"],
    "etf": ["get_etf_breakdown"],
    "history": ["get_macro_history"],
    "cases": ["get_macro_cases"],
    "drivers": ["get_asset_drivers"],
    "screen": ["get_ma_screen"],
    "trend": ["get_sector_trends"],
    "drawdown": ["get_drawdown_analysis"],
    "response": ["get_factor_response"],
    "valuation": ["get_valuation"],
    "indicators": ["get_indicators"],
    "my_rules": ["get_my_rules"],
    "decision": list(DECISION_TOOLS),
    "holding": list(ENTITY_TOOLS),
}


INTENT_LABELS = {
    "weekly": "이번 주 점검", "rebalancing": "리밸런싱 계획", "sector": "섹터 노출", "news": "종목 뉴스",
    "why": "왜 올랐나·내렸나", "outlook": "앞으로의 뷰", "etf": "ETF 구성 종목", "history": "과거 충격 때 반응",
    "cases": "지금과 닮은 과거(사례집)", "drivers": "가장 영향받는 요인", "screen": "이동평균 스크리너", "trend": "미국 업종 ETF 상태", "drawdown": "고점 대비 낙폭 원인", "response": "요인이 움직일 때 자산 반응", "valuation": "밸류에이션(고평가·저평가)", "indicators": "보조지표(RSI·MACD·볼린저)", "my_rules": "내 원칙(목표 비중·전략) 대조", "decision": "매매 판단 재료 모아보기", "holding": "종목 종합",
}


TEACHABLE_INTENTS = [k for k in INTENT_PLANS if k != "holding"]   # 종목 종합은 종목 이름이 있어야 하므로 가르치는 선택지에서 뺀다


def plan_for_intent(intent: str) -> dict:
    """의도 이름 → 도구 호출 계획(plan_agent_query와 같은 모양). 모르는 의도는 지원하지 않는 계획."""
    tools = INTENT_PLANS.get(intent)
    return {"supported": bool(tools), "intents": [intent] if tools else [], "tools": list(tools or [])}


# 키워드로 계획한 질문의 의도 집합 → 가르칠 수 있는 질문 종류. 한 가지로 정해지는 경우만 자동 학습에 쓴다.
PLAN_INTENT_NAMES = {
    frozenset({"weekly_review"}): "weekly", frozenset({"weekly_review", "macro", "news"}): "weekly",
    frozenset({"rebalancing"}): "rebalancing", frozenset({"portfolio"}): "rebalancing", frozenset({"sector"}): "sector", frozenset({"news"}): "news",
    frozenset({"outlook"}): "outlook", frozenset({"etf"}): "etf", frozenset({"history"}): "history", frozenset({"cases"}): "cases",
    frozenset({"macro", "news", "etf"}): "why", frozenset({"macro"}): "why", frozenset({"drivers"}): "drivers", frozenset({"screen"}): "screen", frozenset({"trend"}): "trend", frozenset({"drawdown"}): "drawdown", frozenset({"response"}): "response", frozenset({"valuation"}): "valuation", frozenset({"indicators"}): "indicators", frozenset({"my_rules"}): "my_rules", frozenset({"decision"}): "decision",
}


def plan_intent_name(plan: dict) -> str | None:
    """키워드 계획이 가리키는 질문 종류(한 가지로 정해질 때만). 종목 종합·여러 의도가 섞인 경우는 None."""
    return PLAN_INTENT_NAMES.get(frozenset(plan.get("intents") or []))


def suggest_questions(names: list[str], limit: int = 6) -> list[str]:
    """못 알아들은 질문 뒤에 보여 줄 질문 예시(내 종목 이름 포함). 이름은 길이를 제한하고 중복을 없앤다."""
    out = ["이번 주 무엇을 확인해야 해?"]
    for name in list(dict.fromkeys(str(n).strip()[:30] for n in names if str(n).strip()))[:2]:
        out += [f"{name} 앞으로의 상승/하락 뷰", f"{name} 왜 움직였어?"]
    out += ["리밸런싱 계획 보여줘", "섹터 노출 보여줘"]
    return out[:limit]
