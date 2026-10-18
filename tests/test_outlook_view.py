"""앞으로의 뷰 도구(서버 경로): 질문에서 종목 찾기(한글 경계), 15년 시세 요청, 카테고리 이름으로 종목 지정, 전체 보기, 실패 격리."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import macro
import test_macro as tm
import test_storage_http as storage_http

N = 900
INFO = {"LULU": {"name": "룰루레몬", "market": "NASDAQ", "currency": "USD"}}


def build_series():
    series = {}
    for i, (symbol, _l, kind, _u, _d) in enumerate(macro.FACTORS):
        series[symbol] = tm.series_from_changes(tm.noise(N, 0.05 if kind == "level" else 0.01, 300 + i), kind)
    for i, symbol in enumerate(("GLDM", "TLT", "LULU", "SPYM", "^GSPC")):
        series[symbol] = tm.series_from_changes(tm.noise(N, 0.01, 400 + i))
    return series


SERIES = build_series()


class MentionsTest(unittest.TestCase):
    def test_single_hangul_label_is_not_matched_inside_other_words(self):
        for text in ("그래서 앞으로의 금 상승/하락 뷰를 하나씩 내봐", "금은 어때?", "금을 더 사야 해?", "금의 전망", "금\n뷰"):
            self.assertTrue(server.mentions(text, "금"), text)
        for text in ("금리가 오르면?", "현금 비중은?", "금요일에 뭐 해", "금값 전망", "입금 내역"):
            self.assertFalse(server.mentions(text, "금"), text)

    def test_tickers_and_names_need_word_boundaries(self):
        self.assertTrue(server.mentions("tlt 전망 알려줘", "TLT"))                         # 대소문자 무시
        self.assertTrue(server.mentions("SH, TLT 뷰", "SH"))
        self.assertFalse(server.mentions("we should wait", "SH"))                       # 영단어 속 SH
        self.assertFalse(server.mentions("settle 뷰", "TLT"))
        self.assertTrue(server.mentions("스트래티지는 어때", "스트래티지"))
        self.assertTrue(server.mentions("KODEX 200 어때?", "KODEX 200"))
        self.assertFalse(server.mentions("뭐든", ""))

    def test_names_with_spaces_and_one_letter_typos(self):
        self.assertTrue(server.mentions("kodex200 뉴스", "KODEX 200"))
        self.assertTrue(server.mentions("KODEX  200 뉴스", "KODEX 200"))
        self.assertFalse(server.mentions("kodex 뉴스", "KODEX 200"))
        for typo, code in (("GLDN", "GLDM"), ("GLDMM", "GLDM"), ("SOXL", "SOXX"), ("GDLM", "GLDM")):   # 바뀜·빠짐·더함·이웃 글자 뒤바뀜
            self.assertTrue(server.typo_mentions(f"{typo} 뉴스", code), (typo, code))
        for text, code in (("GLDM 뉴스", "GLDM"), ("TLY", "TLT"), ("UOF", "UFO"), ("goal", "GLDM"), ("GLD 뉴스", "GLDM")):     # 정확 일치는 mentions의 몫, 3글자 이하·먼 단어·다른 종목일 수 있는 짧은 코드(GLD)는 제외
            self.assertFalse(server.typo_mentions(text, code), (text, code))

    def test_focus_entries_by_symbol_name_and_category(self):
        entries = [{"symbol": s, "name": n} for s, n in (("GLDM", "GLDM"), ("UFO", "UFO"), ("NASA", "NASA"), ("TLT", "TLT"), ("MSTR", "스트래티지"))]
        categories = [{"label": "금", "symbols": ["GLDM"]}, {"label": "우주", "symbols": ["UFO", "NASA"]}]

        def symbols(focus):
            chosen, found = server.focus_entries(entries, categories, focus)
            return [e["symbol"] for e in chosen], found
        self.assertEqual(symbols("앞으로의 금 상승/하락 뷰"), (["GLDM"], True))              # 카테고리 이름 → 그 종목
        self.assertEqual(symbols("우주 전망"), (["UFO", "NASA"], True))
        self.assertEqual(symbols("tlt 뷰"), (["TLT"], True))
        self.assertEqual(symbols("스트래티지 어때"), (["MSTR"], True))
        self.assertEqual(symbols("금, TLT 뷰"), (["GLDM", "TLT"], True))                  # 여러 개
        self.assertEqual(symbols("금리 전망"), (["GLDM", "UFO", "NASA", "TLT", "MSTR"], False))   # 못 찾으면 전체, 찾지 못했다고 알린다
        self.assertEqual(symbols(""), (["GLDM", "UFO", "NASA", "TLT", "MSTR"], False))


class OutlookHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.history_calls = []

        def history(symbol, force=False, years=None):
            self.history_calls.append((symbol, years))
            if symbol not in SERIES:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(SERIES[symbol].items())], "divs": [], "name": symbol}
        patches = {
            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {s: INFO[s] for s in syms if s in INFO}),
            "get_yahoo_daily_history": mock.Mock(side_effect=history),
            "headlines_for_many": mock.Mock(side_effect=lambda entries, **kw: {e["symbol"]: {"headlines": [], "error": False} for e in entries}),
        }
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        def item(symbol, name, amount):
            return {"symbol": symbol, "name": name, "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": amount}}
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [item("GLDM", "GLDM", 123), item("TLT", "TLT", 456), item("SGOV", "SGOV", 789)]}

    def prepare(self):
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_cash_symbols(store, ["SGOV"])
        server.save_watchlist(store, ["LULU"])
        server.save_rebalance_config(store, [{"label": "금", "symbols": ["GLDM"], "target_pct": 10.0},
                                             {"label": "채권", "symbols": ["TLT"], "target_pct": 20.0}], 5.0, 50.0)

    def tool(self, cookie, args=None):
        body = {"name": "get_outlook"} if args is None else {"name": "get_outlook", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_outlook"})[0], 401)

    def test_focus_by_category_name_returns_only_that_category(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.tool(cookie, {"focus": "앞으로의 금 상승/하락 뷰"})
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertEqual([i["symbol"] for i in result["items"]], ["GLDM"])
        self.assertTrue(result["focused"])
        item = result["items"][0]
        self.assertEqual(item["status"], "ok")
        for key in ("trend", "depth", "bands", "analogs", "scenarios", "annual_vol_pct"):
            self.assertIn(key, item)

    def test_no_focus_or_unmatched_focus_shows_all_held_and_watch_but_not_cash_stocks(self):
        cookie = self.login("key-a")
        self.prepare()
        for args in (None, {}, {"focus": "금리 전망"}, {"focus": 123}):
            result = self.tool(cookie, args)[1]["result"]
            self.assertEqual(sorted(i["symbol"] for i in result["items"]), ["GLDM", "LULU", "TLT"], args)   # SGOV(현금 취급) 제외
            self.assertFalse(result["focused"], args)

    def test_assets_use_long_history_and_factors_use_the_default(self):
        cookie = self.login("key-a")
        self.prepare()
        self.tool(cookie, {"focus": "금"})
        by_symbol = dict(self.history_calls)
        self.assertEqual(by_symbol["GLDM"], server.OUTLOOK_HISTORY_YEARS)                   # 과거 통계는 긴 기간
        self.assertEqual(by_symbol["^TNX"], None)                                           # 요인은 기본 기간(캐시 공유)

    def test_short_or_failed_history_is_no_data_for_that_symbol_only(self):
        cookie = self.login("key-a")
        self.prepare()
        original = server.get_yahoo_daily_history.side_effect

        def flaky(symbol, force=False, years=None):
            if symbol == "TLT":
                raise server.requests.ConnectionError("down")
            if symbol == "LULU":
                data = original(symbol, years=years)
                return {**data, "bars": data["bars"][:100]}
            return original(symbol, years=years)
        server.get_yahoo_daily_history.side_effect = flaky
        items = {i["symbol"]: i for i in self.tool(cookie)[1]["result"]["items"]}
        self.assertEqual(items["GLDM"]["status"], "ok")
        self.assertEqual((items["TLT"]["status"], items["LULU"]["status"]), ("no_data", "no_data"))
        self.assertEqual(items["LULU"]["history_days"], 100)

    def test_symbol_limit_is_reported(self):
        cookie = self.login("key-a")
        self.prepare()
        with mock.patch.object(server, "OUTLOOK_MAX_SYMBOLS", 2):
            result = self.tool(cookie)[1]["result"]
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["omitted"], 1)

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        self.prepare()
        text = repr(self.tool(cookie)[1])
        for needle in ("789", "555.0", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)

    def test_rule_based_question_from_the_user_returns_only_gold(self):
        cookie = self.login("key-a")
        self.prepare()
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "그래서 앞으로의 금 상승/하락 뷰를 하나씩 내봐"}, cookie=cookie)
        self.assertEqual((status, payload["mode"]), (200, "rule_based_preview"))
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_outlook"])
        table = next(t for t in payload["answer"]["tables"] if t["title"].startswith("앞으로의 상승/하락 뷰"))
        self.assertEqual([r[0] for r in table["rows"]], ["GLDM"])
        self.assertTrue(any("질문에서 지정한 종목만 보여줍니다: GLDM" in n for n in payload["answer"]["notes"]))
        self.assertTrue(any("예측이나 투자 조언이 아닙니다" in b for b in payload["answer"]["bullets"]))

    def ask(self, cookie, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def gold_monthly(self):
        from datetime import date
        import test_outlook as to
        return {date(1975 + i // 12, i % 12 + 1, 1): v for i, v in enumerate(to.walk(620, drift=0.004, scale=0.04, seed=11))}

    def outlook_table(self, payload):
        return next(t for t in payload["answer"]["tables"] if t["title"].startswith("앞으로의 상승/하락 뷰"))

    def test_gold_uses_the_long_monthly_series_and_says_so(self):
        cookie = self.login("key-a")
        self.prepare()
        server.fetch_gold_monthly.side_effect = lambda *a, **k: self.gold_monthly()
        item = self.tool(cookie, {"focus": "금"})[1]["result"]["items"][0]
        self.assertEqual(item["analogs"]["source"]["kind"], "monthly")
        self.assertEqual(item["analogs"]["source"]["from"][:4], "1975")
        status, payload, _ = self.ask(cookie, "금 앞으로의 전망")
        past = self.outlook_table(payload)["rows"][0][4]["lines"]
        self.assertTrue(any(l.startswith("기준: 금 월간 시세") and "1975~" in l and "월간 근사" in l for l in past), past)
        self.assertTrue(any("월간 시세는 월평균 가격" in l for l in payload["answer"]["limitations"]))

    def test_gold_falls_back_to_its_own_history_when_the_monthly_series_is_unavailable(self):
        cookie = self.login("key-a")
        self.prepare()                                                         # 기본 설정: 금 월간 시세 조회 실패
        status, payload, _ = self.ask(cookie, "금 앞으로의 전망")
        self.assertEqual(status, 200)
        item = self.tool(cookie, {"focus": "금"})[1]["result"]["items"][0]
        self.assertIsNone(item["analogs"]["source"])
        self.assertEqual(item["analogs"]["source_error"], "금 월간 시세(World Bank 월평균)")
        past = self.outlook_table(payload)["rows"][0][4]["lines"]
        self.assertTrue(any("받지 못해 종목 자신의 시세로 계산했습니다" in l for l in past), past)

    def test_index_etf_uses_the_long_index_history(self):
        cookie = self.login("key-a")
        self.prepare()
        snapshot = self.snapshot()
        snapshot["items"].append({"symbol": "SPYM", "name": "SPYM", "currency": "USD", "marketCountry": "US", "quantity": 1, "marketValue": {"amount": 10}})
        with mock.patch.object(server, "fetch_account_snapshot", return_value=snapshot), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            _, payload, _ = self.request("POST", "/api/agent/tool", {"name": "get_outlook", "args": {"focus": "SPYM"}}, cookie=cookie)
        item = payload["result"]["items"][0]
        self.assertEqual((item["analogs"]["source"]["kind"], item["analogs"]["source"]["label"]), ("daily", "S&P500 지수"))
        self.assertIn(("^GSPC", server.LONG_PROXY_YEARS), self.history_calls)                # 지수는 상장 이후 전체 기간으로 받는다
        self.assertIn(("SPYM", server.OUTLOOK_HISTORY_YEARS), self.history_calls)             # 현재 국면은 종목 자신의 시세
        self.assertTrue(any("대용 시세(지수)는 ETF와 같은 상품이 아니며" in l for l in payload["result"]["limitations"]))

    def test_stocks_without_a_long_source_are_untouched_and_never_call_the_gold_lookup(self):
        cookie = self.login("key-a")
        self.prepare()
        server.fetch_gold_monthly.reset_mock()
        item = self.tool(cookie, {"focus": "TLT"})[1]["result"]["items"][0]
        self.assertIsNone(item["analogs"]["source"])
        self.assertIsNone(item["analogs"]["source_error"])
        server.fetch_gold_monthly.assert_not_called()

    def test_rule_based_question_without_keywords_but_naming_a_holding_gets_the_full_picture(self):
        cookie = self.login("key-a")
        self.prepare()
        for question in ("GLDM 어때?", "금 보여줘"):
            status, payload, _ = self.ask(cookie, question)
            self.assertEqual(status, 200, question)
            self.assertEqual(sorted(e["tool"] for e in payload["executions"]),
                             ["get_etf_breakdown", "get_macro_drivers", "get_outlook", "get_weekly_news"], question)
            tables = {t["title"].split(" (")[0]: t for t in payload["answer"]["tables"]}
            self.assertEqual([r[0] for r in tables["앞으로의 상승/하락 뷰"]["rows"]], ["GLDM"], question)
            self.assertEqual([r[0] for r in tables["이번 주 종목별 움직임과 보도"]["rows"]], ["GLDM"], question)        # 뉴스도 그 종목만
            explained = any(c["title"].startswith("GLDM 이번 주") for c in payload["answer"]["charts"])                          # 요인별 기여는 그래프,
            unexplained = any(r[0].startswith("GLDM") for t in payload["answer"]["tables"] if t["title"] == "요인으로 설명하지 못한 종목" for r in t["rows"])   # 설명이 안 되면 표
            self.assertTrue(explained or unexplained, question)
            self.assertTrue(any(c["title"].startswith("GLDM 앞으로의 뷰") for c in payload["answer"]["charts"]), question)
            self.assertNotIn("suggestions", payload["answer"], question)

    def test_news_question_with_a_name_shows_only_that_stock(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.ask(cookie, "GLDM에 관한 뉴스 알려줘")
        self.assertEqual((status, [e["tool"] for e in payload["executions"]]), (200, ["get_weekly_news"]))
        table = payload["answer"]["tables"][0]
        self.assertEqual([r[0] for r in table["rows"]], ["GLDM"])
        self.assertTrue(any("질문에서 지정한 종목만 보여줍니다: GLDM" in n for n in payload["answer"]["notes"]))
        _, everything, _ = self.ask(cookie, "내 종목 뉴스 알려줘")                  # 종목을 안 적으면 전체
        self.assertGreater(len(everything["answer"]["tables"][0]["rows"]), 1)

    def test_one_letter_typo_in_a_ticker_still_finds_the_stock(self):
        cookie = self.login("key-a")
        self.prepare()
        _, payload, _ = self.ask(cookie, "GLDN 뉴스")
        self.assertEqual([r[0] for r in payload["answer"]["tables"][0]["rows"]], ["GLDM"])

    def test_ticker_not_in_the_account_is_reported_instead_of_silently_ignored(self):
        cookie = self.login("key-a")
        self.prepare()
        _, alone, _ = self.ask(cookie, "NVDA 어때?")
        self.assertEqual(alone["executions"], [])
        self.assertIn("NVDA은(는) 보유·관심종목에 없어 볼 수 없습니다", alone["answer"]["bullets"][0])
        self.assertTrue(alone["answer"]["suggestions"])
        _, mixed, _ = self.ask(cookie, "GLDM이랑 NVDA 뉴스")
        self.assertEqual([r[0] for r in mixed["answer"]["tables"][0]["rows"]], ["GLDM"])
        self.assertTrue(any("NVDA은(는) 보유·관심종목에 없어 볼 수 없습니다" in n for n in mixed["answer"]["notes"]))
        _, general, _ = self.ask(cookie, "ETF 뉴스 알려줘")                                # ETF 같은 약어는 종목 코드로 보지 않는다
        self.assertFalse(any("보유·관심종목에 없어" in n for n in general["answer"]["notes"]))

    def test_rule_based_question_naming_nothing_gets_examples_and_no_tool_calls(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.ask(cookie, "안녕 오늘 점심 뭐 먹지")
        self.assertEqual(status, 200)
        self.assertEqual(payload["executions"], [])
        self.assertTrue(any("이렇게 물어보세요" in b for b in payload["answer"]["bullets"]))
        suggestions = payload["answer"]["suggestions"]                                      # 내 종목 이름이 들어간 질문 예시
        self.assertIn("이번 주 무엇을 확인해야 해?", suggestions)
        self.assertTrue(any("GLDM" in s for s in suggestions))
        self.assertLessEqual(len(suggestions), 6)
        _, other, _ = self.ask(cookie, "금리 이야기 좀")   # '금리' 속 '금'은 카테고리로 잡지 않는다
        self.assertNotIn("get_outlook", [e["tool"] for e in other["executions"]])


if __name__ == "__main__":
    unittest.main()
