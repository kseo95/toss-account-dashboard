"""기능 요청 큐: '기능 없음' 답변에서 요청을 남기면 주제별 횟수·예시가 사용자 저장소에 쌓이는지, 검증·상한·지우기, 답변의 request 필드, 화면 연결."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import test_storage_http as storage_http

ROOT = Path(__file__).resolve().parent.parent


class FeatureRequestHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def post(self, cookie, body):
        return self.request("POST", "/api/agent/feature-requests", body, cookie=cookie)

    def get(self, cookie):
        return self.request("GET", "/api/agent/feature-requests", cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("GET", "/api/agent/feature-requests")[0], 401)
        self.assertEqual(self.request("POST", "/api/agent/feature-requests", {"topic": "배당"})[0], 401)

    def test_requests_accumulate_per_topic_with_distinct_recent_examples(self):
        cookie = self.login("key-a")
        self.assertEqual(self.get(cookie)[1]["items"], [])
        self.assertEqual(self.post(cookie, {"topic": "배당", "question": "삼성전자 배당 얼마나 늘었어?"})[1]["count"], 1)
        self.post(cookie, {"topic": "배당", "question": "삼성전자 배당 얼마나 늘었어?"})                                  # 같은 질문은 예시에 한 번만
        self.post(cookie, {"topic": "배당", "question": "애플 배당률 추이"})
        self.post(cookie, {"topic": "배당", "question": "리츠 배당"})
        self.post(cookie, {"topic": "배당", "question": "배당락일은 언제"})
        self.post(cookie, {"topic": "기술적 보조지표", "question": "스토캐스틱 보여줘"})
        items = self.get(cookie)[1]["items"]
        self.assertEqual([(i["topic"], i["count"]) for i in items], [("배당", 5), ("기술적 보조지표", 1)])            # 많이 요청된 순
        self.assertEqual(items[0]["examples"], ["애플 배당률 추이", "리츠 배당", "배당락일은 언제"])                       # 최근 3개
        self.assertRegex(items[0]["last"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_only_known_topics_are_accepted(self):
        cookie = self.login("key-a")
        for bad in ({"topic": "<script>"}, {"topic": ""}, {}, {"topic": 5}):
            self.assertEqual(self.post(cookie, bad)[0], 400, bad)
        self.assertEqual(self.post(cookie, {"topic": "기타", "question": "x" * 1000})[0], 200)
        self.assertLessEqual(len(self.get(cookie)[1]["items"][0]["examples"][0]), server.MAX_LEARNED_QUESTION)

    def test_clear_and_per_user_isolation(self):
        a, b = self.login("key-a"), self.login("key-b")
        self.post(a, {"topic": "배당", "question": "q"})
        self.assertEqual(self.get(b)[1]["items"], [])                                                          # 다른 사용자에게 보이지 않는다
        self.assertEqual(self.post(a, {"clear": True})[1], {"ok": True, "items": []})
        self.assertEqual(self.get(a)[1]["items"], [])

    def test_a_corrupt_file_is_ignored(self):
        cookie = self.login("key-a")
        store = server.store_for(next(iter(server._sessions.values())))
        store.write_json(server.FEATURE_REQUESTS_FILE, {"topics": {"배당": {"count": "x"}, "가짜": {"count": 3}, "실적·재무제표": {"count": 2, "last": "2026-10-18 10:00", "examples": ["a"]}}})
        items = self.get(cookie)[1]["items"]
        self.assertEqual([(i["topic"], i["count"]) for i in items], [("실적·재무제표", 2)])

    def test_the_cap_drops_the_least_requested_topics(self):
        store = server.UserStore(Path(self.make_dir()))
        topics = {t: {"count": i + 1, "last": "", "examples": []} for i, t in enumerate(sorted(server.FEATURE_TOPICS))}
        with mock.patch.object(server, "MAX_FEATURE_TOPICS", 3):
            store.write_json(server.FEATURE_REQUESTS_FILE, {"topics": topics})
            server.record_feature_request(store, "기타", "q")
        kept = server.load_feature_requests(store)
        self.assertEqual(len(kept), 3)
        n = len(server.FEATURE_TOPICS)
        self.assertEqual(sorted(r["count"] for r in kept.values()), [n - 2, n - 1, n])                                 # 요청이 가장 적은 주제부터 버린다(방금 1 늘린 '기타'도 해당)

    def make_dir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return d


class AnswerAndFrontendTest(storage_http.HttpTestBase, unittest.TestCase):
    def ask(self, cookie, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value={"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": []}), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_only_no_feature_answers_carry_a_request_button(self):
        cookie = self.login("key-a")
        request = self.ask(cookie, "삼성전자 배당 얼마나 늘었어?")[1]["answer"]["request"]
        self.assertEqual((request["topic"], request["count"]), ("배당", 0))
        self.assertIn("배당", request["question"])
        self.request("POST", "/api/agent/feature-requests", {"topic": "배당", "question": "x"}, cookie=cookie)
        self.assertEqual(self.ask(cookie, "삼성전자 배당 얼마나 늘었어?")[1]["answer"]["request"]["count"], 1)       # 지금까지 요청 횟수
        for question in ("테슬라 100주 사줘", "오늘 점심 뭐 먹지", "ZZZQ 고평가야?"):
            self.assertNotIn("request", self.ask(cookie, question)[1]["answer"], question)

    def test_the_button_the_list_and_the_calls_exist_in_the_page(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        for element in ('id="agentRequest"', 'id="agentRequestBtn"', 'id="agentRequestViewBtn"', 'id="agentRequestClearBtn"', 'id="agentRequestList"'):
            self.assertIn(element, html)
        js = (ROOT / "static/js/agent.js").read_text(encoding="utf-8")
        self.assertIn("renderAgentRequest(data.answer.request)", js)
        self.assertIn("/api/agent/feature-requests", js)
        self.assertIn("textContent", js[js.index("function renderAgentRequest"):js.index("function renderAgentTeach")])        # 버튼 글은 textContent로


if __name__ == "__main__":
    unittest.main()
