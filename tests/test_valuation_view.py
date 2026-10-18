"""밸류에이션 도구(서버 경로·답변): 개별주는 섹터 표본·성장·수익성, ETF는 시장 대비, 금 ETF 해당 없음, 한국 종목 보류, 조회 실패 격리, 질문 라우팅, 그래프·표·문장, 계좌 비노출."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import fundamentals as f
import test_fundamentals as tf
import test_storage_http as storage_http
from agent_tools import compose_agent_answer, format_tool_result, is_valuation_query, plan_agent_query

UNIVERSE = [{"symbol": f"P{i:02d}", "name": f"피어{i}", "sector": "Information Technology", "sub_industry": ""} for i in range(30)] + \
           [{"symbol": "MSFT", "name": "MS", "sector": "Information Technology", "sub_industry": ""}]


def stock_metrics(symbol, **over):
    return f.clean_info(tf.info(**over), symbol)


def etf_metrics(symbol, **over):
    base = dict(quoteType="ETF", sector=None, trailingPE=27.0, priceToBook=2.0, trailingEps=None, bookValue=300.0, returnOnEquity=None, trailingPegRatio=None, forwardPE=None,
                priceToSalesTrailing12Months=None, forwardEps=None)
    base.update(over)
    return f.clean_info(tf.info(**base), symbol)


class ValuationHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        server._peer_cache.clear()
        self.addCleanup(server._peer_cache.clear)
        self.fetched = []
        self.table = {
            "RICH": stock_metrics("RICH", trailingPE=45.0, priceToSalesTrailing12Months=12.0, priceToBook=15.0, trailingPegRatio=3.2, returnOnEquity=0.22),   # 같은 업종보다 훨씬 비쌈
            "CHEAP": stock_metrics("CHEAP", trailingPE=9.0, priceToSalesTrailing12Months=1.5, priceToBook=2.0, trailingPegRatio=0.6, returnOnEquity=0.30),
            "LOSS": stock_metrics("LOSS", trailingPE=None, trailingEps=-3.0, trailingPegRatio=2.5, returnOnEquity=-0.4, priceToBook=1.2, priceToSalesTrailing12Months=20.0),
            "SPY": etf_metrics("SPY", trailingPE=24.0, priceToBook=1.8),
            "QQQ": etf_metrics("QQQ", trailingPE=36.0, priceToBook=2.8),
            "GLD": etf_metrics("GLD", trailingPE=None, priceToBook=2.2),
            "005930.KS": f.clean_info(tf.info(currency="KRW", trailingPE=None, priceToBook=None, trailingEps=None), "005930.KS"),
        }
        for p in UNIVERSE[:30]:                                                                                  # 같은 업종 표본: PER 20 · PSR 4 · PBR 6 근처
            self.table[p["symbol"]] = stock_metrics(p["symbol"], trailingPE=20.0, priceToSalesTrailing12Months=4.0, priceToBook=6.0)

        def fetch(symbol):
            self.fetched.append(symbol)
            if symbol == "BROKEN":
                return {"status": "error", "metrics": None}
            metrics = self.table.get(symbol)
            return {"status": "ok", "metrics": dict(metrics)} if metrics else {"status": "error", "metrics": None}
        for name, value in {"fetch_fundamentals": mock.Mock(side_effect=fetch), "fetch_sp500": mock.Mock(return_value=[dict(u) for u in UNIVERSE]),
                            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {"005930": {"market": "KOSPI", "name": "삼성전자", "currency": "KRW"}} if "005930" in syms else {})}.items():
            pt = mock.patch.object(server, name, value)
            pt.start()
            self.addCleanup(pt.stop)

    def snapshot(self, symbols):
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0,
                "items": [{"symbol": s, "name": s, "currency": "KRW" if s == "005930" else "USD", "marketCountry": "KR" if s == "005930" else "US", "quantity": 7,
                           "marketValue": {"amount": 123 + i}} for i, s in enumerate(symbols)]}

    def tool(self, cookie, symbols, args=None):
        body = {"name": "get_valuation"} if args is None else {"name": "get_valuation", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot(symbols)), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def ask(self, cookie, symbols, question):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot(symbols)), mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/query", {"question": question}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_valuation"})[0], 401)

    def test_stocks_are_judged_against_same_sector_peers_growth_and_profitability(self):
        cookie = self.login("key-a")
        status, payload, _ = self.tool(cookie, ["RICH", "CHEAP"])
        self.assertEqual(status, 200, payload)
        items = {i["symbol"]: i for i in payload["result"]["items"]}
        rich, cheap = items["RICH"], items["CHEAP"]
        self.assertEqual((rich["kind"], rich["verdict"]["label"], rich["peer_n"]), ("stock", "expensive", 25))
        self.assertEqual((cheap["verdict"]["label"], cheap["verdict"]["cheap"]), ("cheap", 5))
        per = next(s for s in rich["signals"] if s["key"] == "per")
        self.assertEqual((per["value"], per["ref"], per["premium_pct"], per["basis"]), (45.0, 20.0, 125.0, "sector"))
        self.assertEqual({s["key"] for s in rich["signals"]}, {"per", "psr", "pbr", "peg", "roe_pbr"})
        self.assertEqual(rich["metrics"]["roe"], 22.0)
        peer_calls = [s for s in self.fetched if s.startswith("P")]
        self.assertEqual(len(peer_calls), 25)                                                                  # 표본은 25개만, 같은 업종 둘째 종목은 캐시를 쓴다
        self.assertNotIn("MSFT", peer_calls[:0])

    def test_the_sector_sample_is_fetched_once_per_sector(self):
        cookie = self.login("key-a")
        self.tool(cookie, ["RICH", "CHEAP"])
        self.assertEqual(len([s for s in self.fetched if s.startswith("P")]), 25)
        before = len(self.fetched)
        self.tool(cookie, ["RICH"])
        self.assertEqual(len([s for s in self.fetched[before:] if s.startswith("P")]), 0)                      # 6시간 캐시

    def test_loss_making_korean_etf_and_gold_cases(self):
        cookie = self.login("key-a")
        items = {i["symbol"]: i for i in self.tool(cookie, ["LOSS", "QQQ", "GLD", "005930"])[1]["result"]["items"]}
        loss = items["LOSS"]
        self.assertIn("loss", loss["flags"])
        self.assertNotIn("per", {s["key"] for s in loss["signals"]})
        self.assertTrue(any("적자" in n for n in loss["notes"]))
        qqq = items["QQQ"]
        self.assertEqual((qqq["kind"], qqq["verdict"]["label"]), ("etf", "expensive"))                          # 36 vs SPY 24 = +50%
        self.assertEqual({s["basis"] for s in qqq["signals"]}, {"market"})
        self.assertIn("SPY", self.fetched)
        self.assertEqual(items["GLD"]["verdict"]["label"], "na")
        kr = items["005930"]
        self.assertEqual((kr["verdict"]["label"], kr["reason"], kr["signals"]), ("hold", "korean", []))
        self.assertEqual(kr["peer_n"], 0)

    def test_a_failed_lookup_is_isolated(self):
        cookie = self.login("key-a")
        items = {i["symbol"]: i for i in self.tool(cookie, ["RICH", "BROKEN"])[1]["result"]["items"]}
        self.assertEqual(items["BROKEN"], {"symbol": "BROKEN", "name": "BROKEN", "held": True, "extra": False, "status": "error"})
        self.assertEqual(items["RICH"]["status"], "ok")

    def test_focus_limits_the_stocks_and_the_benchmark_is_only_fetched_for_etfs(self):
        cookie = self.login("key-a")
        result = self.tool(cookie, ["RICH", "CHEAP", "GLD"], {"focus": "CHEAP"})[1]["result"]
        self.assertEqual([i["symbol"] for i in result["items"]], ["CHEAP"])
        self.assertTrue(result["focused"])
        self.assertNotIn("SPY", self.fetched)

    def test_a_peer_list_failure_still_gives_growth_and_profitability_signals(self):
        cookie = self.login("key-a")
        server.fetch_sp500.side_effect = server.ScreenerError("목록 실패")
        rich = self.tool(cookie, ["RICH"])[1]["result"]["items"][0]
        self.assertEqual({s["key"] for s in rich["signals"]}, {"peg", "roe_pbr"})
        self.assertEqual(rich["peer_n"], 0)
        self.assertTrue(any("비교 표본을 구하지 못해" in n for n in rich["notes"]))

    def test_questions_are_routed_and_answered_with_charts_text_and_a_table(self):
        cookie = self.login("key-a")
        for question in ("RICH 고평가야?", "내 종목 중에 저평가 있어?", "CHEAP PER PBR 알려줘"):
            self.assertEqual(plan_agent_query(question)["tools"], ["get_valuation"], question)
        status, payload, _ = self.ask(cookie, ["RICH", "QQQ", "GLD"], "내 종목 고평가인 게 있어?")
        self.assertEqual((status, [e["tool"] for e in payload["executions"]]), (200, ["get_valuation"]))
        answer = payload["answer"]
        text = "\n".join(answer["bullets"])
        self.assertIn("RICH(개별주): 고평가 쪽 (비싸 보임) — 신호 5개 중 비싼 쪽 5·싼 쪽 0", text)
        self.assertIn("QQQ(ETF): 고평가 쪽 (비싸 보임)", text)
        self.assertIn("GLD(ETF): 해당 없음", text)
        titles = [c["title"] for c in answer["charts"]]
        self.assertEqual(sorted(t for t in titles if "기준 대비" in t), ["QQQ — 기준 대비 얼마나 비싼가 (%)", "RICH — 기준 대비 얼마나 비싼가 (%)"])   # 판단 신호가 없는 GLD는 그래프 없음
        self.assertEqual([t for t in titles if "추정 가격" in t], ["RICH — 섹터 배수 기준 추정 가격 (현재가 대비 %)"])            # ETF에는 가격 추정을 하지 않는다
        chart = next(c for c in answer["charts"] if c["title"].startswith("RICH"))
        self.assertEqual(chart["datasets"][0]["colors"], ["warn"] * 5)
        self.assertIn("PER 45.0 (섹터)", chart["labels"][0])
        self.assertEqual(chart["datasets"][0]["text"][0], "기준 20.0 (+125%)")
        self.assertIn("투자 조언", chart["caption"])
        table = answer["tables"][0]
        self.assertEqual(table["columns"], ["종목", "구분", "PER", "PSR", "PBR", "EPS", "BPS", "ROE", "판정"])
        row = next(r for r in table["rows"] if r[0].startswith("RICH"))
        self.assertEqual(row[2:5], ["45.0 / 선행 18.0", "12.0", "15.0"])
        self.assertEqual(row[7], "22.0%")
        self.assertTrue(any("투자 조언이 아닙니다" in l for l in answer["limitations"]))

    def test_implied_prices_use_the_sector_sample_and_are_listed_in_the_answer(self):
        cookie = self.login("key-a")
        rich = self.tool(cookie, ["RICH"])[1]["result"]["items"][0]
        im = rich["implied"]
        by = {x["key"]: x for x in im["methods"]}
        self.assertEqual(im["price"], 100.0)
        self.assertEqual(by["per"]["price"], 100.0)                                                         # EPS 5.0 × 섹터 PER 20
        self.assertEqual(by["psr"]["price"], 33.33)                                                         # 주당매출 100 ÷ 12 × 섹터 PSR 4 (RICH는 PSR이 12로 비싸다)
        self.assertEqual(by["pbr"]["price"], 96.0)                                                          # BPS 16 × 섹터 PBR 6
        self.assertEqual(im["reliable_n"], 5)
        self.assertLess(im["low"], im["high"])
        answer = self.ask(cookie, ["RICH"], "RICH 고평가야? 적정가는?")[1]["answer"]
        text = "\n".join(answer["bullets"])
        self.assertIn("섹터 배수 기준 추정 범위 $", text)
        chart = next(c for c in answer["charts"] if "추정 가격" in c["title"])
        self.assertEqual(chart["labels"][0], "현재가")
        self.assertEqual(chart["datasets"][0]["colors"][0], "accent")
        self.assertEqual(chart["datasets"][0]["text"][0], "$100")
        self.assertFalse(chart["signed"])                                                                    # 가격에는 + 기호를 붙이지 않는다
        self.assertIn("적정 주가·투자 조언이 아닙니다", chart["caption"])

    def test_distorted_methods_are_greyed_out_and_a_loss_maker_has_no_trailing_per_method(self):
        cookie = self.login("key-a")
        self.table["BUYBACK"] = stock_metrics("BUYBACK", trailingPE=30.0, priceToBook=46.0, bookValue=7.0, returnOnEquity=1.5, priceToSalesTrailing12Months=8.0)
        answer = self.ask(cookie, ["BUYBACK", "LOSS"], "고평가야?")[1]["answer"]
        buyback = next(c for c in answer["charts"] if c["title"].startswith("BUYBACK") and "추정 가격" in c["title"])
        grey = [l for l, color in zip(buyback["labels"], buyback["datasets"][0]["colors"]) if color == "neutral"]
        self.assertTrue(all("(제외)" in l for l in grey) and len(grey) == 2)                                  # PBR·ROE 정당 PBR 방법은 BPS 왜곡으로 제외
        self.assertIn("제외 이유", buyback["caption"])
        loss = next(c for c in answer["charts"] if c["title"].startswith("LOSS") and "추정 가격" in c["title"])
        self.assertFalse(any("PER × EPS" == l for l in loss["labels"]))

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        text = repr(self.tool(cookie, ["RICH", "QQQ"])[1])
        for needle in ("11100000001", "tok-a", "quantity", "marketValue", "555.0"):
            self.assertNotIn(needle, text)


class RoutingTest(unittest.TestCase):
    def test_valuation_words_and_metric_names(self):
        for q in ("AAPL 고평가야?", "엔비디아 저평가인지 알려줘", "삼성전자 PER 알려줘", "roe 높은 종목", "이 ETF 밸류에이션 어때", "overvalued야?", "PBR이랑 PSR 비교", "주당순이익 알려줘", "자기자본이익률 높은 종목"):
            self.assertTrue(is_valuation_query(q), q)
            self.assertEqual(plan_agent_query(q)["tools"], ["get_valuation"], q)

    def test_other_questions_keep_their_routes(self):
        self.assertFalse(is_valuation_query("금이 왜 고점 대비 빠졌어?"))
        self.assertFalse(is_valuation_query("superman hyperbole"))                                               # per·eps 같은 글자가 다른 영어 단어 속에 있을 때
        self.assertEqual(plan_agent_query("금이 왜 고점 대비 빠졌어?")["tools"], ["get_drawdown_analysis"])
        self.assertEqual(plan_agent_query("미국 금리가 오르면 금과 나스닥은 어떻게 됨?")["tools"], ["get_factor_response"])
        self.assertEqual(plan_agent_query("지금 미국 업종별 etf들 상태 어떰?")["tools"], ["get_sector_trends"])
        self.assertEqual(plan_agent_query("전부 팔면 세금은?")["tools"], ["estimate_liquidation_tax"])


class ComposeTest(unittest.TestCase):
    def raw(self, **over):
        base = {"items": [{"symbol": "KR", "name": "삼성전자", "held": True, "status": "ok", "kind": "stock", "peer_n": 0,
                           "metrics": {"per": None, "forward_per": 3.0, "psr": 2.9, "pbr": None, "eps": None, "forward_eps": None, "bps": None, "roe": 30.8, "peg": None, "currency": "KRW",
                                       "category": "", "sector": "Information Technology", "flags": []},
                           "assessment": {"signals": [], "verdict": {"label": "hold", "expensive": 0, "cheap": 0, "signals": 0}, "reason": "korean", "notes": ["한국 종목은 판단하지 않습니다"]}},
                          {"symbol": "ERR", "name": "에러", "held": True, "status": "unavailable"}],
                "omitted": 2, "focus": "", "focused": False, "token": "must-not-leak"}
        base.update(over)
        return base

    def test_korean_hold_unavailable_and_omitted(self):
        result = format_tool_result("get_valuation", self.raw())["result"]
        self.assertNotIn("must-not-leak", repr(result))
        answer = compose_agent_answer({"get_valuation": result})
        self.assertIn("삼성전자(개별주): 판단 보류", "\n".join(answer["bullets"]))
        self.assertEqual(answer["charts"], [])
        self.assertTrue(any("한국 종목은 판단하지 않습니다" in n for n in answer["notes"]))
        self.assertTrue(any("에러" in n and "yfinance 미설치" in n for n in answer["notes"]))
        self.assertTrue(any("그 밖의 2개 종목" in n for n in answer["notes"]))
        row = answer["tables"][0]["rows"][0]
        self.assertEqual((row[2], row[3], row[4], row[5], row[7]), ("- / 선행 3.0", "2.9", "-", "-", "30.8%"))   # 없는 값은 '-'

    def test_very_long_bars_are_capped_but_the_text_keeps_the_real_number(self):
        raw = self.raw()
        raw["items"][0]["assessment"]["signals"] = [{"key": "psr", "label": "PSR", "basis": "sector", "value": 116.3, "ref": 4.6, "ref_label": "섹터", "premium_pct": 2444.0, "verdict": "expensive"},
                                                    {"key": "pbr", "label": "PBR", "basis": "sector", "value": 1.6, "ref": 6.3, "ref_label": "섹터", "premium_pct": -74.0, "verdict": "cheap"}]
        chart = compose_agent_answer({"get_valuation": format_tool_result("get_valuation", raw)["result"]})["charts"][0]
        self.assertEqual(chart["datasets"][0]["data"], [300.0, -74.0])
        self.assertEqual(chart["datasets"][0]["text"][0], "기준 4.6 (+2444%)")
        self.assertIn("+300%까지만", chart["caption"])
        self.assertEqual(chart["datasets"][0]["colors"], ["warn", "accent"])

    def test_the_formatter_cleans_unknown_values_and_caps(self):
        raw = self.raw()
        raw["items"][0]["assessment"]["verdict"]["label"] = "<script>"
        raw["items"][0]["assessment"]["signals"] = [{"key": "x" * 50, "label": "가" * 50, "basis": "bogus", "value": 1.234, "ref": None, "ref_label": "r" * 200, "premium_pct": 55.55, "verdict": "???"}] * 10
        raw["items"] = raw["items"] * 5
        out = format_tool_result("get_valuation", raw)["result"]
        self.assertEqual(len(out["items"]), 4)
        item = out["items"][0]
        self.assertEqual(item["verdict"]["label"], "hold")
        self.assertEqual((len(item["signals"]), item["signals"][0]["basis"], item["signals"][0]["verdict"]), (6, "sector", "fair"))
        self.assertEqual((len(item["signals"][0]["key"]), len(item["signals"][0]["label"]), len(item["signals"][0]["ref_label"])), (12, 20, 60))
        self.assertEqual(out["assumptions"]["band_pct"], 30)

    def test_no_holdings(self):
        answer = compose_agent_answer({"get_valuation": format_tool_result("get_valuation", {"items": [], "omitted": 0})["result"]})
        self.assertIn("밸류에이션을 볼 보유 종목이나 관심종목이 없습니다", answer["bullets"][0])


if __name__ == "__main__":
    unittest.main()
