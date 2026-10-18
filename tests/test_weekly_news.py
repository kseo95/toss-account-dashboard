"""이번 주 뉴스 도구: 보유(현금 취급 제외) + 관심종목 모으기, 등락률, 국내/해외 조회 분기, 계좌 정보 비전송, 실패 격리."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import test_storage_http as storage_http

INFO = {"069500": {"name": "KODEX 200", "market": "KOSPI", "currency": "KRW"},
        "LULU": {"name": "룰루레몬", "market": "NASDAQ", "currency": "USD"}}
# 야후 심볼 → 최근 7개 종가(마지막이 최신). 5거래일 등락률 = 마지막 / 끝에서 6번째 - 1
CLOSES = {"MSTR": [100, 100, 100, 100, 100, 100, 90], "GLDM": [100, 100, 100, 100, 100, 100, 102],
          "069500.KS": [100, 100, 100, 100, 100, 100, 101], "LULU": [100, 100, 100, 100, 100, 100, 106]}


def history(symbol):
    if symbol not in CLOSES:
        raise ValueError("데이터 없음")
    return {"bars": [(f"2026-10-{i + 1:02d}", c, c) for i, c in enumerate(CLOSES[symbol])], "divs": [], "name": symbol}


class WeeklyNewsHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.entries_seen = []

        def headlines(entries, **kw):
            self.entries_seen = [dict(e) for e in entries]
            return {e["symbol"]: {"headlines": [{"title": f"{e['symbol']} 기사", "source": "매체", "url": "https://n.example/" + e["symbol"],
                                                  "published": "2026-10-17"}], "error": False} for e in entries}
        patches = {
            "lookup_stocks": mock.Mock(side_effect=lambda token, syms: {s: INFO[s] for s in syms if s in INFO}),
            "get_yahoo_daily_history": mock.Mock(side_effect=history),
            "headlines_for_many": mock.Mock(side_effect=headlines),
        }
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self):
        def item(symbol, name, currency, country, amount):
            return {"symbol": symbol, "name": name, "currency": currency, "marketCountry": country, "quantity": 7,
                    "marketValue": {"amount": amount}}
        items = [item("MSTR", "스트래티지", "USD", "US", 123), item("GLDM", "GLDM", "USD", "US", 456),
                 item("SGOV", "SGOV", "USD", "US", 789), item("069500", "KODEX 200", "KRW", "KR", 100000)]
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0, "items": items}

    def prepare(self, cookie, watch=("LULU",), cash=("SGOV",)):
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_cash_symbols(store, list(cash))
        server.save_watchlist(store, list(watch))

    def tool(self, cookie):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", {"name": "get_weekly_news"}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_weekly_news"})[0], 401)

    def test_collects_held_and_watchlist_but_not_cash_designated_stocks(self):
        cookie = self.login("key-a")
        self.prepare(cookie)
        status, payload, _ = self.tool(cookie)
        self.assertEqual(status, 200, payload)
        items = payload["result"]["items"]
        self.assertEqual([i["symbol"] for i in items], ["MSTR", "LULU", "GLDM", "069500"])   # 많이 움직인 순
        self.assertNotIn("SGOV", [i["symbol"] for i in items])
        self.assertEqual({i["symbol"]: i["held"] for i in items}, {"MSTR": True, "LULU": False, "GLDM": True, "069500": True})
        self.assertEqual([round(i["weekly_change_pct"], 1) for i in items], [-10.0, 6.0, 2.0, 1.0])
        self.assertEqual(items[0]["headlines"][0], {"title": "MSTR 기사", "source": "매체", "published": "2026-10-17", "url": "https://n.example/MSTR"})

    def test_queries_contain_only_names_and_codes_never_account_data(self):
        cookie = self.login("key-a")
        self.prepare(cookie)
        self.tool(cookie)
        by_symbol = {e["symbol"]: e for e in self.entries_seen}
        self.assertEqual(set(by_symbol["MSTR"]), {"symbol", "name", "use_yahoo"})          # 수량·금액·계좌 키가 없다
        self.assertEqual(by_symbol["MSTR"], {"symbol": "MSTR", "name": "스트래티지", "use_yahoo": True})
        self.assertEqual(by_symbol["069500"], {"symbol": "069500", "name": "KODEX 200", "use_yahoo": False})   # 국내는 구글 뉴스만
        for needle in ("123", "456", "100000", "555", "11100000001"):
            self.assertNotIn(needle, repr(self.entries_seen))
        self.assertEqual(by_symbol["LULU"]["name"], "룰루레몬")                                  # 관심종목 이름은 토스 종목정보에서

    def test_yahoo_symbols_follow_market(self):
        cookie = self.login("key-a")
        self.prepare(cookie)
        self.tool(cookie)
        asked = sorted(c.args[0] for c in server.get_yahoo_daily_history.call_args_list)
        self.assertEqual(asked, ["069500.KS", "GLDM", "LULU", "MSTR"])                        # 코스피는 .KS

    def test_history_failure_only_blanks_that_symbols_change(self):
        cookie = self.login("key-a")
        self.prepare(cookie)
        original = server.get_yahoo_daily_history.side_effect

        def flaky(symbol):
            if symbol == "GLDM":
                raise server.requests.ConnectionError("down")
            return original(symbol)
        server.get_yahoo_daily_history.side_effect = flaky
        items = self.tool(cookie)[1]["result"]["items"]
        gldm = next(i for i in items if i["symbol"] == "GLDM")
        self.assertIsNone(gldm["weekly_change_pct"])
        self.assertEqual(items[-1]["symbol"], "GLDM")                                          # 등락률을 모르면 맨 뒤
        self.assertEqual(len(items), 4)

    def test_symbol_limit_reports_how_many_were_left_out(self):
        cookie = self.login("key-a")
        self.prepare(cookie)
        with mock.patch.object(server, "NEWS_MAX_SYMBOLS", 2):
            result = self.tool(cookie)[1]["result"]
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["omitted"], 2)
        self.assertEqual({i["held"] for i in result["items"]}, {True})                         # 보유 종목이 먼저 남는다

    def test_rule_based_question_returns_news_table(self):
        cookie = self.login("key-a")
        self.prepare(cookie)
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "내 종목 소식 알려줘"}, cookie=cookie)
        self.assertEqual((status, payload["mode"]), (200, "rule_based_preview"))
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_weekly_news"])
        table = next(t for t in payload["answer"]["tables"] if t["title"] == "이번 주 종목별 움직임과 보도")
        mstr = next(r for r in table["rows"] if r[0].endswith("(MSTR)"))
        self.assertEqual(mstr[1], "-10.0%")
        self.assertEqual(mstr[2]["links"][0]["href"], "https://n.example/MSTR")
        self.assertTrue(any("가장 크게 움직인 종목" in b for b in payload["answer"]["bullets"]))


if __name__ == "__main__":
    unittest.main()
