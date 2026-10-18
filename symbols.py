"""질문 속 종목 이름·코드 찾기(보유·관심종목이 아닌 종목용): 질문에서 후보 낱말을 뽑고, 검색 결과 중 정확히 맞는 것만 고른다.

"엔비디아 PER 어때?", "TSLA 고점 대비 왜 빠졌어?"처럼 보유하지 않은 종목을 물을 때 쓴다. 후보 낱말만 검색하고(질문 전체나 계좌 정보는 보내지 않는다),
검색 결과는 이름이나 코드가 정확히 같을 때만 받아들인다(예: '금'이 '금호타이어'로 잡히지 않게).
순수 계산이며 검색 자체는 호출하는 쪽이 한다(다른 기능 모듈을 부르지 않는다).
"""
from __future__ import annotations

import re

MAX_CANDIDATES = 5
MIN_PREFIX = 3                 # 이름이 후보 낱말로 '시작'하는 결과를 받으려면 후보가 이 글자 수 이상이어야 한다
TOKEN_RE = re.compile(r"[A-Za-z가-힣][A-Za-z가-힣0-9.\-]*")      # SK하이닉스·LG에너지솔루션·KB금융처럼 영문과 한글이 붙은 이름도 한 낱말
JOSA = ("들은", "들이", "들을", "에서는", "에서의", "으로는", "이라는", "에서", "들", "으로", "이랑", "이나", "한테", "보다", "처럼", "까지", "부터", "은", "는", "이", "가", "을", "를", "의", "도", "에", "와", "과", "만", "로", "랑", "나", "야", "요")
# 질문에 자주 나오는 낱말(종목 이름이 아니다). 정확 일치만 거르므로 모자라도 검색 결과가 정확하지 않으면 버려진다.
VERB_ENDINGS = ("줘", "해봐", "봐", "까", "요", "니", "네", "면", "해")      # 동사·어미 꼴(알려줘·비교해줘·어때요)은 종목 이름이 아니다
STOP = frozenset({
    "어때", "어떻게", "알려줘", "알려", "보여줘", "보여", "지금", "요즘", "오늘", "왜", "빠졌어", "떨어졌어", "올랐어", "내렸어", "오르면", "내리면", "고점", "대비", "얼마나", "주가", "주식", "종목",
    "전망", "뉴스", "소식", "요인", "영향", "분석", "비교", "어디", "뭐야", "뭔가", "가격", "적정", "이번", "이번주", "최근", "현재", "그리고", "그럼", "근데", "그런데", "혹시", "좀", "정도", "많이", "가장",
    "내가", "내", "나의", "관심", "보유", "포트폴리오", "미국", "한국", "시장", "업종", "섹터", "상태", "추세", "수익률", "하락", "상승", "급락", "급등", "폭락", "폭등", "이유", "원인", "때문", "해줘", "해봐",
    "나스닥", "코스피", "코스닥", "금리", "달러", "유가", "환율", "구리", "비트코인", "이더리움", "원유", "금값", "은값",
    "어떰", "어땠어", "어떤", "무엇", "무슨", "뭘", "뭐", "전부", "모두", "전체", "업종별", "섹터별", "종목별", "확인", "점검", "팔면", "사면", "세금", "리밸런싱", "비중",
    "있어", "없어", "인가", "인지", "일까", "할까", "될까", "같아", "같은", "아니면", "또는", "그냥", "다시", "먼저", "다음", "비싼", "싼", "비싸", "저렴", "거품", "기준", "차이", "순위", "목록", "리스트",
})


def strip_josa(token: str) -> str:
    """한글 낱말 끝의 조사를 떼어 본다(떼고 남는 글자가 2자 이상일 때만): 엔비디아는 → 엔비디아."""
    for j in sorted(JOSA, key=len, reverse=True):
        if token.endswith(j) and len(token) - len(j) >= 2:
            return token[:-len(j)]
    return token


def candidate_tokens(text: str, skip: frozenset | set = frozenset(), limit: int = MAX_CANDIDATES) -> list[str]:
    """질문에서 종목 이름·코드일 수 있는 낱말(순서: 영문 대문자 코드 → 한글 이름 → 영문 소문자). 흔한 질문 낱말·skip(예: 이미 알려진 의도 낱말, 내 종목 이름)은 뺀다.
    한글은 조사를 떼고 2자 이상, 영문 대문자는 1~6자(BRK.B 같은 점·하이픈 허용), 영문 소문자는 3자 이상."""
    upper, korean, lower = [], [], []
    seen = set()
    skip_keys = {x.lower() for x in skip}
    for raw in TOKEN_RE.findall(str(text or "")[:400]):
        if re.search(r"[가-힣]", raw):
            token = strip_josa(raw)
            one_char_with_josa = len(token) == 2 and token[1:] in JOSA        # '금이'·'금은'처럼 한 글자 낱말에 조사가 붙은 것(종목 이름이 아니다)
            bucket = korean if len(token) >= 2 and not token.endswith(VERB_ENDINGS) and not one_char_with_josa else None
        elif raw.isupper():
            token = raw
            bucket = upper if 1 <= len(re.sub(r"[.\-]", "", raw)) <= 6 else None
        else:
            token = raw
            bucket = lower if len(raw) >= 3 and raw.replace(".", "").replace("-", "").isalpha() else None
        key = token.lower()
        if bucket is None or key in seen or raw in STOP or token in STOP or key in skip_keys or raw.lower() in skip_keys:
            continue                                                          # 조사를 뗀 꼴(고평)과 원래 꼴(고평가) 모두 흔한 낱말과 견준다
        seen.add(key)
        bucket.append(token)
    return (upper + korean + lower)[:limit]


def _squash(text: str) -> str:
    return re.sub(r"[\s·.\-_]+", "", str(text or "")).lower()


def choose(token: str, results: list[dict]) -> dict | None:
    """검색 결과 중 이 낱말이 가리키는 종목: 코드나 이름이 정확히 같은 것, 없으면 첫 결과의 이름이 낱말로 시작하고 낱말이 3자 이상일 때. 없으면 None.
    results: [{"symbol", "name", ...}] (검색이 관련도 순으로 준다고 본다)."""
    key = _squash(token)
    if not key or not results:
        return None
    for r in results:
        if _squash(r.get("symbol")) == key or _squash(r.get("name")) == key:
            return r
    first = results[0]
    if len(key) >= MIN_PREFIX and _squash(first.get("name")).startswith(key):
        return first
    return None
