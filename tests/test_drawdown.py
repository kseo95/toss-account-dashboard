"""고점 대비 낙폭 분석: 구간 변화(since), 최근 고점·전 고점, 고점 이후 요인 기여, 고점 근처·시세 없음, 서버 경로와 규칙 기반 질문("금이 왜 고점 대비 빠졌어?"), 뉴스 이어 보기, 계좌 비노출."""
from __future__ import annotations

import math
import re
import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import macro
import test_asset_drivers_view as adv
import test_macro as tm
import test_storage_http as storage_http
from agent_tools import is_drawdown_query, plan_agent_query

N = adv.N_LONG
WINDOW = 30            # 마지막 30거래일 동안 달러가 오른다


def build():
    changes = {}
    for i, (symbol, _l, kind, _u, _d) in enumerate(macro.FACTORS):
        changes[symbol] = tm.noise(N, 0.05 if kind == "level" else 0.01, 2000 + i)
    for k in range(N - WINDOW, N):
        changes["DX-Y.NYB"][k] = 0.004 + tm.noise(1, 0.002, 3000 + k)[0]          # 고점 이후 달러 강세
    event = N - 15                                                                              # 금이 크게 빠진 날: 반도체·코스피도 같이 폭락
    changes["^SOX"][event], changes["^KS11"][event] = -0.12, -0.08
    series = {s: adv.path(c, macro.FACTOR_BY_SYMBOL[s][2], 60.0 if macro.FACTOR_BY_SYMBOL[s][2] == "level" else 100.0) for s, c in changes.items()}
    gold = [-0.7 * d + 0.4 * h + 0.0015 + e for d, h, e in zip(changes["DX-Y.NYB"], changes["HG=F"], tm.noise(N, 0.004, 2100))]
    gold[event] = -0.05
    series["GLDM"] = adv.path(gold[-700:])                                                    # 달러에 눌리는 금 ETF(고점은 달러가 오르기 직전)
    silver = changes["SI=F"]
    series["SILVERLIKE"] = adv.path([0.9 * v + e for v, e in zip(silver[-700:], tm.noise(700, 0.002, 2103))])      # 은에 붙어 움직이는 종목(매크로로는 설명 안 됨)
    series["NEARHIGH"] = adv.path([0.001 + e for e in tm.noise(700, 0.002, 2101)])            # 계속 오르는 종목
    old = [0.0] * 700
    old[100] = 0.9                                                                            # 아주 오래전 급등 → 전 고점은 1년보다 앞
    series["OLDPEAK"] = adv.path([c + e for c, e in zip(old, tm.noise(700, 0.003, 2102))])
    return series


SERIES = build()


class SinceTest(unittest.TestCase):
    def test_window_change_since_a_date(self):
        series = {date(2026, 1, d): float(d) for d in range(1, 11)}
        self.assertAlmostEqual(macro.window_change(series, "level", since=date(2026, 1, 4)), 6.0)
        self.assertAlmostEqual(macro.window_change(series, "price", since=date(2026, 1, 5)), math.log(10 / 5))
        self.assertIsNone(macro.window_change(series, "level", since=date(2026, 1, 10)))         # 구간이 비면 None
        self.assertIsNone(macro.window_change(series, "level", since=date(2025, 1, 1)))          # 시작이 시세보다 앞이면 None
        self.assertEqual(macro.window_change(series, "level", 5), 5.0)                          # 기존 N거래일 방식은 그대로


class DrawdownHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.news_calls = []

        def history(symbol, force=False, years=None):
            if symbol not in SERIES:
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(SERIES[symbol].items())], "divs": [], "name": symbol}

        self.around_calls = []

        def around(queries, day, limit=3, get=None, per_query=2):
            self.around_calls.append((list(queries), day))
            return [{"title": f"{day} 코스피 폭락에 사이드카 발동 ({queries[0]})", "source": "테스트", "url": "https://example.com/a", "published": day.isoformat(), "query": queries[0]},
                    {"title": f"{day} 이란 전쟁 공포", "source": "테스트", "url": "https://example.com/b", "published": day.isoformat(), "query": queries[0]}]

        def headlines(items):
            self.news_calls.append([i["symbol"] for i in items])
            return {i["symbol"]: {"headlines": [], "error": None} for i in items}
        for name, value in {"lookup_stocks": mock.Mock(side_effect=lambda token, syms: {}), "get_yahoo_daily_history": mock.Mock(side_effect=history),
                            "headlines_for_many": mock.Mock(side_effect=headlines), "headlines_around": mock.Mock(side_effect=around)}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        def item(symbol, amount):
            return {"symbol": symbol, "name": symbol, "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": amount}}
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0, "items": [item("GLDM", 123), item("NEARHIGH", 456), item("OLDPEAK", 321), item("GONE", 654), item("SILVERLIKE", 987)]}

    def prepare(self):
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_rebalance_config(store, [{"label": "금", "symbols": ["GLDM"], "target_pct": 10.0}], 5.0, 50.0)

    def tool(self, cookie, args=None):
        body = {"name": "get_drawdown_analysis"} if args is None else {"name": "get_drawdown_analysis", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def ask(self, cookie, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_drawdown_analysis"})[0], 401)

    def test_gold_fell_from_its_recent_high_and_the_dollar_explains_it(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.tool(cookie, {"focus": "금이 왜 고점 대비 빠졌어?"})
        self.assertEqual(status, 200, payload)
        item = payload["result"]["items"][0]
        self.assertEqual((item["symbol"], item["status"]), ("GLDM", "ok"))
        self.assertLess(item["drawdown_pct"], -4)
        self.assertTrue(WINDOW - 5 <= item["since_days"] <= WINDOW + 25, item["since_days"])     # 고점은 달러가 오르기 시작한 무렵(잡음으로 며칠 앞설 수 있다)
        self.assertLessEqual(item["low_drawdown_pct"], item["drawdown_pct"])                     # 그 사이 최저는 지금보다 같거나 낮다
        dollar = next(d for d in item["drivers"] if d["label"] == "달러")
        self.assertLess(dollar["contribution_pct"], -2)                                          # 달러 강세가 하락을 설명
        self.assertGreater(dollar["factor_change"], 5)
        biggest = sorted(item["moves"], key=lambda m: abs(m["change"]) / (10 if m["unit"] == "bp" else 1), reverse=True)[:3]
        self.assertIn("달러", [m["label"] for m in biggest])                                     # 고점 이후 가장 많이 움직인 요인 중 하나
        self.assertGreater(item["r2"], 0.2)

    def test_the_biggest_drop_day_is_found_with_the_same_day_market_and_news(self):
        cookie = self.login("key-a")
        self.prepare()
        item = self.tool(cookie, {"focus": "GLDM"})[1]["result"]["items"][0]
        days = item["drop_days"]
        self.assertTrue(1 <= len(days) <= 4)
        self.assertEqual([d["date"] for d in days], sorted(d["date"] for d in days))                # 날짜순
        worst = min(days, key=lambda d: d["change_pct"])
        self.assertLess(worst["change_pct"], -4)                                                     # 심어 둔 -5% 하락일
        labels = " ".join(m["label"] for m in worst["movers"])
        self.assertTrue("반도체" in labels or "코스피" in labels, labels)                            # 같은 날 폭락한 요인
        self.assertTrue(worst["risk_down"])
        self.assertTrue(worst["headlines"])
        queries = [q for queries, day in self.around_calls for q in queries if day.isoformat() == worst["date"]]
        self.assertTrue(any("급락" in q and ("반도체" in q or "코스피" in q) for q in queries), queries)   # 그날 크게 움직인 요인 이름으로 검색

    def test_the_answer_shows_the_drop_day_table_with_links(self):
        cookie = self.login("key-a")
        self.prepare()
        answer = self.ask(cookie, "금이 왜 고점 대비 빠졌어?")[1]["answer"]
        table = next(t for t in answer["tables"] if "크게 빠진 날" in t["title"])
        self.assertEqual(table["columns"][0], "날짜")
        self.assertTrue(any(isinstance(r[3], dict) and r[3].get("links") for r in table["rows"]))
        self.assertTrue(any("원인 후보 (" in b for b in answer["bullets"]))
        self.assertTrue(any("원인의 증명이 아닙니다" in l or "원인이라고 확정할 수 없" in l for l in answer["limitations"]))

    def test_themes_from_the_headlines_are_counted_and_summarised_with_the_statistics(self):
        cookie = self.login("key-a")
        self.prepare()
        item = self.tool(cookie, {"focus": "GLDM"})[1]["result"]["items"][0]
        themes = {c["theme"]: c for c in item["causes"]}
        self.assertIn("증시 급락·투매", themes)                                                       # 모든 큰 하락일 보도에 '폭락·사이드카'
        self.assertIn("지정학·전쟁", themes)
        n = len(item["drop_days"])
        self.assertEqual(len(themes["증시 급락·투매"]["days"]), n)
        self.assertIn("사이드카", themes["증시 급락·투매"]["example"]["title"])
        self.assertNotIn("pool", item["drop_days"][0])                                               # 세는 데 쓴 전체 제목은 결과에 싣지 않는다
        answer = self.ask(cookie, "금이 왜 고점 대비 빠졌어?")[1]["answer"]
        text = "\n".join(answer["bullets"])
        self.assertIn("원인 후보 (", text)
        self.assertNotIn("① 회귀 기여", text)                                                         # 회귀 기여는 그래프로 옮겼다
        self.assertIn("② 평소 관계:", text)                                                      # 회귀 말고 평소 상관으로도 한 번 더
        self.assertIn("③ 보도 주제", text)
        self.assertIn("증시 급락·투매", text)
        self.assertIn("확정 아님", text)

    def test_a_news_search_failure_keeps_the_dates_and_the_market_moves(self):
        cookie = self.login("key-a")
        self.prepare()
        server.headlines_around.side_effect = lambda queries, day, limit=3, get=None, per_query=2: []
        item = self.tool(cookie, {"focus": "GLDM"})[1]["result"]["items"][0]
        self.assertTrue(item["drop_days"])
        self.assertTrue(all(d["headlines"] == [] for d in item["drop_days"]))
        self.assertTrue(any(d["movers"] for d in item["drop_days"]))

    def test_a_same_family_asset_does_not_steal_the_macro_explanation(self):
        cookie = self.login("key-a")
        self.prepare()
        items = {i["symbol"]: i for i in self.tool(cookie, {"focus": "GLDM SILVERLIKE"})[1]["result"]["items"]}
        self.assertEqual(items["GLDM"]["basis"], "macro")                                            # 금은 달러·구리 같은 매크로 요인만으로 설명
        self.assertNotIn("은", [d["label"] for d in items["GLDM"]["drivers"]])
        silver = items["SILVERLIKE"]
        if silver["status"] == "ok":                                                                 # 매크로로 안 되면 시장 전체(은 포함)로 넓혀 보고, 그렇다고 표시
            self.assertEqual(silver["basis"], "market")
            self.assertIn("은", [d["label"] for d in silver["drivers"]])

    def test_all_time_high_is_reported_separately_from_the_recent_high(self):
        cookie = self.login("key-a")
        self.prepare()
        items = {i["symbol"]: i for i in self.tool(cookie, {"focus": "OLDPEAK"})[1]["result"]["items"]}
        old = items["OLDPEAK"]
        self.assertLess(old["ath_date"], old["peak_date"])                                       # 전 고점은 1년보다 앞
        self.assertLess(old["ath_drawdown_pct"], old["drawdown_pct"])                            # 전 고점 대비가 더 깊다

    def test_a_rising_asset_is_near_its_high_and_missing_data_is_isolated(self):
        cookie = self.login("key-a")
        self.prepare()
        items = {i["symbol"]: i for i in self.tool(cookie, {"focus": "NEARHIGH GONE GLDM"})[1]["result"]["items"]}
        self.assertEqual(items["NEARHIGH"]["status"], "near_peak")
        self.assertEqual(items["GONE"]["status"], "no_data")                                     # 시세 없는 종목만 빠진다
        self.assertEqual(items["GLDM"]["status"], "ok")

    def test_the_question_gets_the_breakdown_with_per_day_news_and_no_extra_weekly_news(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.ask(cookie, "금이 왜 고점 대비 빠졌어?")
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_drawdown_analysis"])      # 날짜별 보도가 이미 들어 있어 최근 7일 뉴스 도구를 따로 붙이지 않는다
        text = " ".join(payload["answer"]["bullets"])
        self.assertIn("고점", text)
        self.assertIn("달러", text)
        charts = payload["answer"]["charts"]
        self.assertEqual([c["type"] for c in charts], ["line", "hbar"])                              # 고점 이후 가격선 + 요인별 기여 막대
        line, bars = charts
        self.assertEqual(len(line["labels"]), len(line["datasets"][0]["data"]))
        self.assertTrue(line["markers"] and all(0 <= m["index"] < len(line["labels"]) for m in line["markers"]))     # 큰 하락일 점
        self.assertTrue(any("%" in m["text"] for m in line["markers"]))
        self.assertEqual(line["hlines"][0]["color"], "warn")                                          # 고점선
        self.assertIn("달러", bars["labels"][0])
        self.assertEqual(bars["labels"][-1], "설명 안 된 부분")
        self.assertTrue(bars["datasets"][0]["text"][0].endswith("%p"))
        self.assertIn("달러 강세", text.split("요약:")[1].split("하락 압력")[0])                                               # 풀어 쓴 한 줄 요약
        self.assertTrue(payload["answer"]["bullets"][0].startswith("GLDM: 최근 고점"))
        self.assertEqual(len(payload["answer"]["tables"]), 1)                                       # 표는 '크게 빠진 날' 하나만(요인 변화 표는 뺐다)
        self.assertNotIn("시장 요인 변화", " ".join(t["title"] for t in payload["answer"]["tables"]))
        self.assertTrue(any("원인으로 확정" in l for l in payload["answer"]["limitations"]))
        self.assertEqual(self.news_calls, [])

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        self.prepare()
        text = repr(self.tool(cookie, {"focus": "GLDM OLDPEAK SILVERLIKE NEARHIGH"})[1])
        for needle in ("11100000001", "tok-a", "quantity", "marketValue"):
            self.assertNotIn(needle, text)
        for amount in ("789", "555", "123", "456", "321", "654", "987"):                                 # 보유 금액이 그대로 새지 않는다(시세 소수점 속 숫자와 구분)
            self.assertIsNone(re.search(rf"(?<![\d.]){amount}(?![\d.])", text), amount)


class ComposeCausesTest(unittest.TestCase):
    def item(self, risk_flags):
        days = [{"date": f"2026-03-{10 + i}", "change_pct": -3.0, "risk_down": flag, "movers": [], "headlines": [], "themes": []} for i, flag in enumerate(risk_flags)]
        return {"symbol": "X", "name": "X", "held": True, "status": "weak", "reason": "low_r2", "basis": "macro", "r2": 0.05, "oos_r2": None, "drivers": [], "residual_pct": -9.0,
                "moves": [], "last_date": "2026-04-01", "peak_date": "2026-01-02", "ath_date": "2026-01-02", "low_date": "2026-03-30", "last_close": 90.0, "peak_close": 100.0,
                "ath_close": 100.0, "drawdown_pct": -10.0, "ath_drawdown_pct": -10.0, "low_drawdown_pct": -11.0, "since_days": 60, "drop_days": days, "causes": []}

    def bullets(self, flags):
        from agent_tools import compose_agent_answer, drawdown_result
        return "\n".join(compose_agent_answer({"get_drawdown_analysis": drawdown_result({"items": [self.item(flags)]})})["bullets"])

    def test_the_risk_asset_pattern_line_appears_only_when_most_drop_days_had_it(self):
        self.assertIn("④ 큰 하락일 4일 중 3일은 주식·가상자산", self.bullets([True, True, True, False]))
        self.assertIn("시장 전체가 팔릴 때 같이 팔린 패턴과 일치", self.bullets([True, True, False, False]))
        self.assertNotIn("④", self.bullets([True, False, False, False]))

    def test_with_nothing_to_summarise_only_the_one_line_remains(self):
        text = self.bullets([False, False])
        self.assertIn("요약:", text)
        self.assertNotIn("④", text)


class RoutingTest(unittest.TestCase):
    def test_drawdown_questions_go_to_the_drawdown_tool_only(self):
        for q in ("금이 왜 고점 대비 빠졌어?", "고점 대비 얼마나 빠졌어", "전고점에서 하락한 이유", "UFO 고점 이후 왜 내렸어"):
            self.assertTrue(is_drawdown_query(q), q)
            self.assertEqual(plan_agent_query(q)["tools"], ["get_drawdown_analysis"], q)

    def test_other_questions_keep_their_tools(self):
        self.assertEqual(plan_agent_query("금 왜 올랐어?")["tools"], ["get_macro_drivers", "get_weekly_news", "get_etf_breakdown"])
        self.assertEqual(plan_agent_query("금 고점 찍었는데 앞으로 전망은?")["tools"], ["get_outlook"])                       # 앞날은 뷰 도구
        self.assertEqual(plan_agent_query("지금 미국 업종별 etf들 상태 어떰?")["tools"], ["get_sector_trends"])


if __name__ == "__main__":
    unittest.main()
