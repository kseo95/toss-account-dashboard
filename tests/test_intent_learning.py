"""규칙 기반 모드의 의도 학습(서버 경로): 문장 유사도로 짐작, 확신 없을 때 고르는 버튼, 가르친 문장 우선 처리·사용자별 저장·한도·삭제, 키워드로 처리된 질문에는 버튼 없음."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import test_storage_http as storage_http

NOT_FOUND = {"found": False, "names": ["GLDM"], "unknown": []}
SIMILAR_ONLY = "얘가 이렇게 약해진 까닭이 궁금해"            # 키워드는 없고 문장이 '왜 떨어졌나' 예시와 비슷하다
NOTHING = "점심 메뉴 추천해줘"


class IntentTestBase(storage_http.HttpTestBase):
    """서버를 임시 포트로 띄우고, 도구 실행(계좌 조회)과 종목 찾기를 가짜로 바꾸는 공통 설정. 테스트 클래스는 unittest.TestCase와 함께 상속한다."""

    def setUp(self):
        super().setUp()
        self.ran = []

        def fake_tool(req, name, args=None):
            self.ran.append((name, args))
            return {"result": {}}
        for name, value in {"execute_agent_tool": mock.Mock(side_effect=fake_tool),
                            "focus_info": mock.Mock(return_value=dict(NOT_FOUND))}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def ask(self, cookie, question):
        self.ran.clear()
        status, payload, _ = self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        return payload

    def teach(self, cookie, question, intent):
        return self.request("POST", "/api/agent/teach", {"question": question, "intent": intent}, cookie=cookie)

    def tools(self):
        return [n for n, _a in self.ran]


class IntentLearningTest(IntentTestBase, unittest.TestCase):
    # --- 문장 유사도로 짐작 ---
    def test_similar_sentence_is_understood_and_offers_a_way_to_correct_it(self):
        cookie = self.login("key-a")
        payload = self.ask(cookie, SIMILAR_ONLY)
        self.assertEqual(self.tools(), ["get_macro_drivers", "get_weekly_news", "get_etf_breakdown"])       # '왜' 의도의 도구들
        teach = payload["answer"]["teach"]
        self.assertEqual(teach["understood"]["intent"], "why")
        self.assertEqual(teach["understood"]["label"], "왜 올랐나·내렸나")
        self.assertGreater(teach["understood"]["score"], 0.3)
        self.assertEqual(teach["question"], SIMILAR_ONLY)
        self.assertEqual([c["intent"] for c in teach["choices"]], list(server.TEACHABLE_INTENTS))
        self.assertNotIn("holding", [c["intent"] for c in teach["choices"]])
        self.assertNotIn("suggestions", payload["answer"])                                                  # 처리했으니 예시 버튼은 없다

    def test_a_sentence_it_cannot_place_asks_what_you_meant(self):
        cookie = self.login("key-a")
        payload = self.ask(cookie, NOTHING)
        self.assertEqual(self.tools(), [])
        self.assertIsNone(payload["answer"]["teach"]["understood"])
        self.assertTrue(payload["answer"]["suggestions"])
        self.assertEqual(len(payload["answer"]["teach"]["choices"]), len(server.TEACHABLE_INTENTS))

    def test_keyword_questions_do_not_show_the_teach_buttons(self):
        cookie = self.login("key-a")
        payload = self.ask(cookie, "섹터 노출 보여줘")
        self.assertEqual(self.tools(), ["get_sector_exposure"])
        self.assertNotIn("teach", payload["answer"])

    def test_a_guess_of_holding_is_ignored_because_it_needs_a_named_stock(self):
        cookie = self.login("key-a")
        with mock.patch.object(server.IntentMatcher, "classify", return_value={"intent": "holding", "score": 0.9, "runner_up": None}):
            payload = self.ask(cookie, NOTHING)
        self.assertEqual(self.tools(), [])
        self.assertIsNone(payload["answer"]["teach"]["understood"])

    # --- 가르치기 ---
    def test_taught_sentence_is_handled_as_taught_even_against_keywords(self):
        cookie = self.login("key-a")
        sentence = "섹터 노출 말고 뉴스가 궁금해"                      # 키워드('섹터')는 섹터 도구를 가리키지만
        self.assertEqual(self.teach(cookie, sentence, "news")[0], 200)
        payload = self.ask(cookie, sentence)
        self.assertEqual(self.tools(), ["get_weekly_news"])                  # 가르친 대로 처리
        self.assertIn("가르쳐 주신 대로 처리했습니다.", payload["answer"]["notes"])
        self.assertNotIn("teach", payload["answer"])

    def test_teaching_makes_a_previously_unplaceable_sentence_work_and_it_generalizes_a_little(self):
        cookie = self.login("key-a")
        sentence = "주머니 사정 두루 살펴보기"
        self.ask(cookie, sentence)
        self.assertEqual(self.tools(), [])                                   # 가르치기 전에는 처리하지 못한다
        status, payload, _ = self.teach(cookie, sentence, "weekly")
        self.assertEqual((status, payload["count"]), (200, 1))
        self.ask(cookie, sentence)
        self.assertIn("get_weekly_risk_report", self.tools())
        self.ask(cookie, "주머니 사정 두루 살펴보기 해줘")                   # 군더더기가 달라도 같은 문장으로 취급
        self.assertIn("get_weekly_risk_report", self.tools())

    def test_same_sentence_is_updated_not_duplicated_and_old_ones_drop_at_the_cap(self):
        cookie = self.login("key-a")
        self.teach(cookie, "주머니 사정 두루 살펴보기", "weekly")
        self.assertEqual(self.teach(cookie, "주머니 사정 두루 살펴보기!!", "sector")[1]["count"], 1)           # 부호만 달라도 같은 문장 → 의도 갱신
        self.ask(cookie, "주머니 사정 두루 살펴보기")
        self.assertEqual(self.tools(), ["get_sector_exposure"])
        for i in range(server.MAX_LEARNED + 5):
            self.teach(cookie, f"테스트용 학습 문장 번호 {'가나다라마바사아자차카타파하'[i % 14]}{'가' * (i // 14)} 끝", "news")
        count = self.request("GET", "/api/agent/teach", cookie=cookie)[1]["count"]
        self.assertEqual(count, server.MAX_LEARNED)

    def test_validation(self):
        cookie = self.login("key-a")
        for body in ({"question": "주머니 사정 두루 살펴보기", "intent": "holding"}, {"question": "주머니 사정 두루 살펴보기", "intent": "없는의도"},
                     {"question": "UFO", "intent": "news"}, {"question": "안녕", "intent": "news"}, {"question": "가" * 300, "intent": "news"},
                     {"question": "", "intent": "news"}, {"intent": "news"}):
            self.assertEqual(self.request("POST", "/api/agent/teach", body, cookie=cookie)[0], 400, body)
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie)[1]["count"], 0)

    def test_learning_is_per_user_and_can_be_cleared(self):
        cookie_a, cookie_b = self.login("key-a"), self.login("key-b")
        self.teach(cookie_a, "주머니 사정 두루 살펴보기", "weekly")
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie_a)[1]["count"], 1)
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie_b)[1]["count"], 0)             # 다른 사용자에게는 없다
        self.ask(cookie_b, "주머니 사정 두루 살펴보기")
        self.assertEqual(self.tools(), [])
        self.assertEqual(self.request("POST", "/api/agent/teach", {"clear": True}, cookie=cookie_a)[1]["count"], 0)
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie_a)[1]["count"], 0)

    def test_requires_login_and_corrupt_file_is_ignored(self):
        self.assertEqual(self.request("POST", "/api/agent/teach", {"question": "주머니 사정 두루 살펴보기", "intent": "news"})[0], 401)
        self.assertEqual(self.request("GET", "/api/agent/teach")[0], 401)
        cookie = self.login("key-a")
        store = server.store_for(next(iter(server._sessions.values())))
        store.path(server.INTENTS_FILE).write_text("{깨진 json", encoding="utf-8")
        self.assertEqual(self.ask(cookie, NOTHING)["answer"]["teach"]["choices"] != [], True)                # 깨진 학습 파일은 없는 것으로
        self.assertEqual(self.teach(cookie, "주머니 사정 두루 살펴보기", "news")[1]["count"], 1)               # 다시 저장하면 복구

    def test_ai_mode_is_untouched(self):
        cookie = self.login("key-a")
        self.request("POST", "/api/agent/config", {"api_key": "sk-ant-secret"}, cookie=cookie)
        with mock.patch.object(server, "run_claude_agent", return_value={"mode": "claude", "answer_text": "ok", "executions": []}) as run:
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": NOTHING}, cookie=cookie)
        self.assertEqual((status, payload["mode"]), (200, "claude"))
        run.assert_called_once()
        self.assertNotIn("teach", payload)


class AutoLearningTest(IntentTestBase, unittest.TestCase):
    """못 알아들은 질문 뒤에 키워드로 다시 물으면 자동 연결(안전장치·취소·한 번만), 놓친 질문 기록(기본 꺼짐)."""
    UNPLACED = "주머니 사정 두루 살펴보기"               # 어떤 키워드도 없고 '이번 주 점검' 예시와 어느 정도 비슷하다
    FOLLOWUP = "이번 주 무엇을 확인해야 해?"            # 키워드로 처리되는 '이번 주 점검'

    def session_of(self):
        return next(iter(server._sessions.values()))

    def test_followup_question_links_the_earlier_one_and_tells_the_user(self):
        cookie = self.login("key-a")
        self.ask(cookie, self.UNPLACED)
        self.assertEqual(self.tools(), [])
        payload = self.ask(cookie, self.FOLLOWUP)
        self.assertEqual(payload["answer"]["learned"], {"question": self.UNPLACED, "intent": "weekly", "label": "이번 주 점검"})
        rows = self.request("GET", "/api/agent/teach", cookie=cookie)[1]["examples"]
        self.assertEqual(rows, [{"question": self.UNPLACED, "intent": "weekly", "auto": True}])            # 자동 연결 표시
        self.ask(cookie, self.UNPLACED)                                                                    # 이제 앞 질문이 바로 처리된다
        self.assertIn("get_weekly_risk_report", self.tools())

    def test_it_is_one_shot_the_pending_question_is_used_once(self):
        cookie = self.login("key-a")
        self.ask(cookie, self.UNPLACED)
        self.ask(cookie, self.FOLLOWUP)
        again = self.ask(cookie, "섹터 노출 보여줘")
        self.assertNotIn("learned", again["answer"])
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie)[1]["count"], 1)

    def test_chitchat_is_not_linked_to_whatever_comes_next(self):
        cookie = self.login("key-a")
        self.ask(cookie, NOTHING)                                                                          # 잡담: 어떤 의도와도 비슷하지 않다
        payload = self.ask(cookie, self.FOLLOWUP)
        self.assertNotIn("learned", payload["answer"])
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie)[1]["count"], 0)

    def test_stale_pending_question_is_not_linked(self):
        cookie = self.login("key-a")
        self.ask(cookie, self.UNPLACED)
        self.session_of()["intent_pending"]["ts"] -= server.AUTO_LEARN_WINDOW + 5
        self.assertNotIn("learned", self.ask(cookie, self.FOLLOWUP)["answer"])

    def test_only_keyword_handled_followups_count(self):
        cookie = self.login("key-a")
        self.ask(cookie, self.UNPLACED)
        similar = self.ask(cookie, SIMILAR_ONLY)                                                           # 후속 질문도 짐작이면 연결하지 않는다
        self.assertNotIn("learned", similar["answer"])
        self.ask(cookie, self.UNPLACED)
        server.focus_info.return_value = {"found": True, "names": ["GLDM"], "unknown": []}
        self.assertNotIn("learned", self.ask(cookie, "GLDM 어때")["answer"])                                # 종목 종합은 하나의 의도로 정해지지 않는다
        server.focus_info.return_value = dict(NOT_FOUND)
        self.ask(cookie, self.UNPLACED)
        self.assertNotIn("learned", self.ask(cookie, self.UNPLACED)["answer"])                              # 같은 질문을 되풀이한 것은 연결하지 않는다
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie)[1]["count"], 0)

    def test_a_guess_confirmed_by_a_keyword_question_is_remembered(self):
        cookie = self.login("key-a")
        self.ask(cookie, SIMILAR_ONLY)                                                                     # 유사도로 '왜' 라고 짐작
        payload = self.ask(cookie, "이 종목 왜 떨어졌어?")
        self.assertEqual(payload["answer"]["learned"]["intent"], "why")

    def test_the_link_can_be_cancelled(self):
        cookie = self.login("key-a")
        self.ask(cookie, self.UNPLACED)
        self.ask(cookie, self.FOLLOWUP)
        status, payload, _ = self.request("POST", "/api/agent/teach", {"forget": self.UNPLACED + "!"}, cookie=cookie)     # 부호가 달라도 같은 문장
        self.assertEqual((status, payload["forgotten"], payload["count"]), (200, True, 0))
        self.ask(cookie, self.UNPLACED)
        self.assertEqual(self.tools(), [])                                                                 # 다시 못 알아듣는다
        self.assertFalse(self.request("POST", "/api/agent/teach", {"forget": "없는 문장입니다 정말로"}, cookie=cookie)[1]["forgotten"])

    def test_pending_question_belongs_to_one_session(self):
        cookie_a, cookie_b = self.login("key-a"), self.login("key-b")
        self.ask(cookie_a, self.UNPLACED)
        self.assertNotIn("learned", self.ask(cookie_b, self.FOLLOWUP)["answer"])                           # B에게는 앞 질문이 없다
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie_b)[1]["count"], 0)
        self.assertEqual(self.request("GET", "/api/agent/teach", cookie=cookie_a)[1]["count"], 0)

    # --- 놓친 질문 기록 ---
    def test_misses_are_not_recorded_unless_the_user_turns_it_on(self):
        cookie = self.login("key-a")
        self.ask(cookie, NOTHING)
        state = self.request("GET", "/api/agent/misses", cookie=cookie)[1]
        self.assertEqual((state["enabled"], state["count"]), (False, 0))
        store = server.store_for(self.session_of())
        self.assertFalse(store.path(server.MISSES_FILE).exists())                                           # 파일조차 만들지 않는다

    def test_enabled_misses_log_unplaced_and_guessed_questions_once_each(self):
        cookie = self.login("key-a")
        self.assertTrue(self.request("POST", "/api/agent/misses", {"enable": True}, cookie=cookie)[1]["enabled"])
        self.ask(cookie, NOTHING)
        self.ask(cookie, SIMILAR_ONLY)
        self.ask(cookie, NOTHING + "!")                                                                     # 같은 문장은 한 번만
        self.ask(cookie, "섹터 노출 보여줘")                                                                # 키워드로 처리된 질문은 기록하지 않는다
        items = self.request("GET", "/api/agent/misses", cookie=cookie)[1]["items"]
        self.assertEqual([(r["kind"], r["guess"]) for r in items], [("guessed", "why"), ("unplaced", None)])
        self.assertTrue(all(r["at"] for r in items))
        self.assertEqual(self.request("POST", "/api/agent/misses", {"enable": False}, cookie=cookie)[1]["count"], 2)   # 끄면 기록은 남고 새로 쌓이지 않는다
        self.ask(cookie, "영화 추천해줘 심심한데")
        self.assertEqual(self.request("GET", "/api/agent/misses", cookie=cookie)[1]["count"], 2)

    def test_misses_clear_cap_and_per_user_isolation(self):
        cookie_a, cookie_b = self.login("key-a"), self.login("key-b")
        self.request("POST", "/api/agent/misses", {"enable": True}, cookie=cookie_a)
        self.ask(cookie_a, NOTHING)
        self.assertEqual(self.request("GET", "/api/agent/misses", cookie=cookie_b)[1]["count"], 0)
        self.assertEqual(self.request("POST", "/api/agent/misses", {"clear": True}, cookie=cookie_a)[1]["count"], 0)
        self.assertTrue(self.request("GET", "/api/agent/misses", cookie=cookie_a)[1]["enabled"])            # 비워도 켜 둔 상태는 유지
        for i in range(server.MAX_MISSES + 5):
            self.ask(cookie_a, f"점심 메뉴 추천 요청 {'가나다라마바사아자차카타파하'[i % 14]}{'가' * (i // 14)} 부탁")
        self.assertEqual(self.request("GET", "/api/agent/misses", cookie=cookie_a)[1]["count"], server.MAX_MISSES)

    def test_misses_requires_login_and_corrupt_file_is_ignored(self):
        self.assertEqual(self.request("GET", "/api/agent/misses")[0], 401)
        self.assertEqual(self.request("POST", "/api/agent/misses", {"enable": True})[0], 401)
        cookie = self.login("key-a")
        store = server.store_for(self.session_of())
        store.path(server.MISSES_FILE).write_text("{깨짐", encoding="utf-8")
        self.assertEqual(self.request("GET", "/api/agent/misses", cookie=cookie)[1]["count"], 0)
        self.assertTrue(self.request("POST", "/api/agent/misses", {"enable": True}, cookie=cookie)[1]["enabled"])


if __name__ == "__main__":
    unittest.main()
