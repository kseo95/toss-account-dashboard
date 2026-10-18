"""문장 유사도로 질문의 의도를 고른다(규칙 기반 모드의 보조). 외부 호출·모델·비용 없이 글자 n-gram 코사인 유사도만 쓴다.

- 의도마다 예시 문장을 여러 개 두고, 질문과 가장 비슷한 예시의 의도를 고른다. 한국어는 조사·어미가 달라져도 글자 2~3개 조각이 많이 겹쳐서
  "내 종목 왜 이렇게 빠졌어" ≈ "하락한 이유가 뭐야" 같은 표현 차이를 어느 정도 흡수한다(의미를 이해하는 것은 아니다).
- 영문·숫자 단어(종목 코드)는 문장 모양과 무관하므로 빼고 비교한다.
- 사용자가 가르친 문장(learned)은 정확히 같은 문장이면 바로 그 의도로 쓴다(유사도 1.0).
- 확신이 부족하면(점수가 낮거나 1·2위가 비슷하면) 고르지 않는다 — 잘못 고르느니 되묻는 편이 낫다.
순수 계산이며 다른 기능 모듈을 부르지 않는다.
"""
from __future__ import annotations

import math
import re
from collections import Counter

NGRAM_SIZES = (2, 3)
MIN_SCORE = 0.30          # 가장 비슷한 예시와의 유사도가 이보다 낮으면 고르지 않는다
MIN_MARGIN = 0.05         # 1위 의도가 2위 의도보다 이만큼은 앞서야 한다
MAX_QUESTION = 300


# 질문 끝의 군더더기(뜻 없이 자주 붙는 말). 이게 겹쳤다고 비슷한 질문으로 보면 엉뚱한 의도로 가므로 빼고 비교한다.
FILLERS = frozenset({"좀", "줘", "해줘", "알려줘", "보여줘", "말해줘", "뭐야", "뭐지", "뭔가", "뭐", "어때", "어떄", "있어", "있나", "있을까", "있지", "해", "해봐",
                     "궁금해", "궁금한데", "인가", "인가요", "이야", "야", "요", "나요", "까", "지", "좀요", "같아", "것같아", "내", "나", "네", "는", "은", "이", "가"})
MIN_CONTENT = 4           # 군더더기를 뺀 글자가 이보다 적으면(예: "고마워") 비교하지 않는다


def normalize(text: str) -> str:
    """영문·숫자 단어와 군더더기를 빼고 소문자로, 공백·문장부호를 없앤다(한글과 남은 글자만)."""
    text = re.sub(r"[A-Za-z0-9.\-]+", " ", str(text or "")[:MAX_QUESTION]).lower()
    words = [re.sub(r"[^0-9a-z가-힣]", "", w) for w in re.split(r"\s+", text)]
    return "".join(w for w in words if w and w not in FILLERS)


def grams(norm: str) -> Counter:
    out: Counter = Counter()
    for n in NGRAM_SIZES:
        for i in range(len(norm) - n + 1):
            out[norm[i:i + n]] += 1
    return out


class IntentMatcher:
    """예시 문장 {의도: [문장…]}으로 만든 분류기. 만든 뒤에는 바뀌지 않는다."""

    def __init__(self, examples: dict[str, list[str]]):
        docs = [(intent, normalize(s)) for intent, sentences in examples.items() for s in sentences if normalize(s)]
        self.exact = {norm: intent for intent, norm in docs}
        counts = [(intent, grams(norm)) for intent, norm in docs]
        df: Counter = Counter()
        for _intent, c in counts:
            df.update(c.keys())
        n = len(counts)
        self.idf = {g: math.log((n + 1) / (d + 1)) + 1 for g, d in df.items()}
        self.vectors = [(intent, *self._vector(c)) for intent, c in counts]

    def _vector(self, counter: Counter) -> tuple[dict, float]:
        vec = {g: (1 + math.log(c)) * self.idf.get(g, 1.0) for g, c in counter.items()}
        return vec, math.sqrt(sum(v * v for v in vec.values()))

    def scores(self, question: str) -> list[tuple[str, float]]:
        """의도별 가장 높은 유사도, 높은 순. 비교할 글자가 없으면 빈 목록."""
        norm = normalize(question)
        if len(norm) < MIN_CONTENT:
            return []
        if norm in self.exact:
            return [(self.exact[norm], 1.0)]
        vec, size = self._vector(grams(norm))
        if not size:
            return []
        best: dict[str, float] = {}
        for intent, other, other_size in self.vectors:
            if not other_size:
                continue
            dot = sum(w * other.get(g, 0.0) for g, w in vec.items())
            sim = dot / (size * other_size)
            if sim > best.get(intent, 0.0):
                best[intent] = sim
        return sorted(best.items(), key=lambda kv: kv[1], reverse=True)

    def classify(self, question: str, min_score: float = MIN_SCORE, min_margin: float = MIN_MARGIN) -> dict | None:
        """{"intent", "score", "runner_up"} 또는 확신이 없으면 None."""
        ranked = self.scores(question)
        if not ranked or ranked[0][1] < min_score:
            return None
        top = ranked[0]
        second = ranked[1] if len(ranked) > 1 else (None, 0.0)
        if top[1] - second[1] < min_margin:
            return None
        return {"intent": top[0], "score": top[1], "runner_up": second[0]}
