"""문장 유사도 의도 분류: 정규화(영문 단어·군더더기 제거), 정확 일치 우선, 확신이 없으면 고르지 않음, 처음 보는 문장 정확도(회귀 방지), 예시 데이터 무결성."""
from __future__ import annotations

import unittest

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import agent_tools
import intent_matcher as im
from intent_examples import DEFAULT_EXAMPLES

# 예시 문장을 쓸 때 보지 않은 새 문장(정답 의도). None은 어떤 의도로도 처리하면 안 되는 질문.
HELD_OUT = [
    ("이번 주에 신경 쓸 만한 게 있을까", "weekly"), ("내 계좌 위험한 부분 점검해봐", "weekly"),
    ("목표한 비율이랑 다른 데가 어디야", "rebalancing"), ("뭘 얼마나 사야 균형이 맞아", "rebalancing"),
    ("다 정리하면 세금 뭉텅이로 나오려나", "tax"), ("매도 차익에 붙는 세금 계산해줘", "tax"),
    ("내가 투자한 분야가 어떻게 분포돼 있어", "sector"), ("한쪽 업종에 쏠렸는지 알려줘", "sector"),
    ("요새 이 종목 기사 좀 찾아봐", "news"), ("최근에 무슨 이슈가 터졌는지 알고 싶어", "news"),
    ("얘가 이렇게 약해진 까닭이 궁금해", "why"), ("올라간 이유 좀 분석해줘", "why"), ("갑자기 내린 건 뭐 때문이야", "why"),
    ("앞으로 더 갈 수 있을까", "outlook"), ("이후 시나리오가 어떻게 돼", "outlook"), ("조만간 방향이 어느 쪽일까", "outlook"),
    ("이 ETF는 안에 뭘 들고 있는 거야", "etf"), ("어떤 구성 종목이 큰 비중이야", "etf"), ("편입 종목 중 제일 많이 빠진 건", "etf"),
    ("옛날 위기에서는 이 종목이 어땠는지 궁금해", "history"), ("과거에 이런 충격이 오면 보통 어떻게 돼", "history"),
    ("지금 분위기가 예전 어느 때랑 닮았을까", "cases"), ("과거 장세랑 견주면 지금은 어느 구간이야", "cases"), ("옛날 어느 해와 비슷하게 흘러가고 있어", "cases"),
    ("안녕하세요", None), ("점심 메뉴 추천해줘", None), ("오늘 기분 어때", None), ("네 이름이 뭐니", None),
    ("영화 추천해줘", None), ("로또 번호 알려줘", None), ("내일 날씨 알려줘", None), ("고마워", None), ("너 이름이 뭐야", None),
]
MATCHER = im.IntentMatcher(DEFAULT_EXAMPLES)


class NormalizeTest(unittest.TestCase):
    def test_english_words_and_fillers_are_dropped(self):
        self.assertEqual(im.normalize("UFO 왜 이렇게 빠졌어 좀 알려줘!"), "왜이렇게빠졌어")
        self.assertEqual(im.normalize("TLT, GLDM 어때?"), "")
        self.assertEqual(im.normalize("  "), "")
        self.assertEqual(im.normalize(None), "")
        self.assertEqual(im.normalize("가" * 1000), "가" * im.MAX_QUESTION)                      # 길이 제한

    def test_too_little_content_is_never_classified(self):
        self.assertEqual(MATCHER.scores("고마워"), [])
        self.assertIsNone(MATCHER.classify("UFO"))
        self.assertIsNone(MATCHER.classify(""))


class ClassifyTest(unittest.TestCase):
    def test_exact_example_wins_with_full_score(self):
        r = MATCHER.classify(DEFAULT_EXAMPLES["tax"][0])
        self.assertEqual((r["intent"], r["score"]), ("tax", 1.0))
        r = MATCHER.classify("  " + DEFAULT_EXAMPLES["tax"][0] + "!!  ")                      # 공백·문장부호는 무시
        self.assertEqual(r["intent"], "tax")

    def test_ambiguous_or_weak_matches_are_declined(self):
        examples = {"a": ["서울 날씨 알려줘 오늘"], "b": ["서울 날씨 알려줘 내일"]}
        self.assertIsNone(im.IntentMatcher(examples).classify("서울 날씨 알려줘 모레", min_score=0.1, min_margin=0.2))   # 1·2위가 비슷하면 고르지 않는다
        self.assertIsNone(MATCHER.classify("제주도 맛집 알려줘 근처에"))

    def test_held_out_sentences_accuracy_and_no_false_positives(self):
        right = wrong = declined = unrelated_ok = 0
        unrelated = sum(1 for _q, gold in HELD_OUT if gold is None)
        for q, gold in HELD_OUT:
            r = MATCHER.classify(q)
            got = r["intent"] if r else None
            if gold is None:
                unrelated_ok += got is None
            elif got == gold:
                right += 1
            elif got is None:
                declined += 1
            else:
                wrong += 1
        total = len(HELD_OUT) - unrelated
        self.assertEqual(unrelated_ok, unrelated, "무관한 질문을 어떤 의도로 처리하면 안 된다")
        self.assertGreaterEqual(right / total, 0.6)                                           # 현재 약 68%: 이보다 나빠지면 회귀
        self.assertLessEqual(wrong / total, 0.12)                                             # 오답은 드물어야 한다(모르면 보류가 낫다)

    def test_learned_sentences_extend_the_examples(self):
        sentence = "주머니 사정 두루 살펴보기"
        self.assertIsNone(MATCHER.classify(sentence))
        extended = im.IntentMatcher({**{k: list(v) for k, v in DEFAULT_EXAMPLES.items()}, "weekly": DEFAULT_EXAMPLES["weekly"] + [sentence]})
        self.assertEqual(extended.classify(sentence)["intent"], "weekly")
        self.assertEqual(extended.classify("주머니 사정 두루 살펴보기 해줘")["intent"], "weekly")      # 군더더기가 달라도 같은 문장으로


class ExamplesDataTest(unittest.TestCase):
    def test_every_intent_has_a_plan_and_enough_distinct_examples(self):
        self.assertEqual(set(DEFAULT_EXAMPLES), set(agent_tools.INTENT_PLANS))
        for intent, sentences in DEFAULT_EXAMPLES.items():
            self.assertGreaterEqual(len(sentences), 12, intent)
            self.assertEqual(len(sentences), len(set(sentences)), intent)
            self.assertTrue(all(len(im.normalize(s)) >= im.MIN_CONTENT for s in sentences), intent)        # 비교 가능한 길이
            self.assertTrue(set(agent_tools.INTENT_PLANS[intent]) <= agent_tools.TOOL_NAMES, intent)

    def test_no_sentence_belongs_to_two_intents(self):
        seen = {}
        for intent, sentences in DEFAULT_EXAMPLES.items():
            for s in sentences:
                key = im.normalize(s)
                self.assertNotIn(key, seen, f"{s}: {seen.get(key)}와 {intent}에 중복")
                seen[key] = intent

    def test_teachable_intents_exclude_holding_and_labels_exist(self):
        self.assertNotIn("holding", agent_tools.TEACHABLE_INTENTS)
        self.assertEqual(set(agent_tools.INTENT_LABELS), set(agent_tools.INTENT_PLANS))
        self.assertEqual(agent_tools.plan_for_intent("cases")["tools"], ["get_macro_cases"])
        self.assertFalse(agent_tools.plan_for_intent("nope")["supported"])


if __name__ == "__main__":
    unittest.main()
