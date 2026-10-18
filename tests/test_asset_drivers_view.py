"""요인 순위 도구(서버 경로): 긴 시세 수신, 금 ETF의 긴 대용 일봉, 종목 지정, FRED 요인 합류, 시세 실패 격리, 규칙 기반 질문, 계좌 비노출."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import fred
import macro
import test_macro as tm
import test_storage_http as storage_http

N_LONG, N_SHORT = 3200, 700


def business_days(n, start=date(2008, 1, 1)):
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


DAYS = business_days(N_LONG)


def path(changes, kind="price", base=100.0, days=None):
    days = days or DAYS[-len(changes):]
    out, v = [], base
    for c in changes:
        v = v + c if kind == "level" else v * math.exp(c)
        out.append(max(v, 0.5))
    return dict(zip(days, out))


def build():
    changes = {}
    for i, (symbol, _l, kind, _u, _d) in enumerate(macro.FACTORS):
        changes[symbol] = tm.noise(N_LONG, 0.05 if kind == "level" else 0.01, 1000 + i)
    series = {s: path(c, macro.FACTOR_BY_SYMBOL[s][2], 60.0 if macro.FACTOR_BY_SYMBOL[s][2] == "level" else 100.0) for s, c in changes.items()}   # 금리 걷기는 값이 잘리지 않게 기준을 높게
    # TLT: 30년물 금리에 반응(−), 달러에 약하게(−)
    tlt = [-0.4 * a - 0.5 * b + e for a, b, e in zip(changes["^TYX"], changes["DX-Y.NYB"], tm.noise(N_LONG, 0.003, 1100))]
    series["TLT"] = path(tlt)
    gc = [-0.7 * d + 0.4 * h + e for d, h, e in zip(changes["DX-Y.NYB"], changes["HG=F"], tm.noise(N_LONG, 0.004, 1101))]
    series["GC=F"] = path(gc)                                                                          # 긴 금 선물
    series["GLDM"] = path(gc[-N_SHORT:])                                                               # 짧은 금 ETF(같은 움직임)
    dff = tm.noise(N_LONG, 0.03, 1102)
    series["FRED:DFF"] = path(dff, "level", 60.0)
    series["DFFASSET"] = path([-0.6 * a + e for a, e in zip(dff, tm.noise(N_LONG, 0.012, 1103))])      # 연방기금금리에 반응하는 종목
    return series


SERIES = build()


class AssetDriversHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls, self.fred_calls = [], []

        def history(symbol, force=False, years=None):
            self.calls.append((symbol, years))
            if symbol not in SERIES or symbol.startswith("FRED:"):
                raise ValueError("데이터 없음")
            return {"bars": [(d, v, v) for d, v in sorted(SERIES[symbol].items())], "divs": [], "name": symbol}

        def fetch(key, series_id, *a, **k):
            self.fred_calls.append((key, series_id))
            if series_id == "DFF":
                return dict(SERIES["FRED:DFF"])
            raise fred.FredError("테스트: 받지 못함")
        for name, value in {"lookup_stocks": mock.Mock(side_effect=lambda token, syms: {}),
                            "get_yahoo_daily_history": mock.Mock(side_effect=history),
                            "fetch_fred_series": mock.Mock(side_effect=fetch)}.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self, extra=()):
        def item(symbol, amount):
            return {"symbol": symbol, "name": symbol, "currency": "USD", "marketCountry": "US", "quantity": 7, "marketValue": {"amount": amount}}
        return {"usd_krw": 1000.0, "cash_krw": 555.0, "cash_usd": 0.0, "items": [item("GLDM", 123), item("TLT", 456), item("SGOV", 789)] + [item(s, 1) for s in extra]}

    def prepare(self):
        store = server.store_for(next(iter(server._sessions.values())))
        server.save_cash_symbols(store, ["SGOV"])
        server.save_rebalance_config(store, [{"label": "금", "symbols": ["GLDM"], "target_pct": 10.0}, {"label": "채권", "symbols": ["TLT"], "target_pct": 20.0}], 5.0, 50.0)

    def call(self, cookie, args=None, extra=()):
        body = {"name": "get_asset_drivers"} if args is None else {"name": "get_asset_drivers", "args": args}
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot(extra)), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            return self.request("POST", "/api/agent/tool", body, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/agent/tool", {"name": "get_asset_drivers"})[0], 401)

    def test_focus_by_category_returns_ranked_drivers_with_long_requests(self):
        cookie = self.login("key-a")
        self.prepare()
        status, payload, _ = self.call(cookie, {"focus": "금이 가장 영향을 많이 받는 요소 3개가 뭘까?"})
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        self.assertTrue(result["focused"])
        item = result["items"][0]
        self.assertEqual((item["symbol"], item["status"]), ("GLDM", "ok"))
        self.assertEqual(item["source"], "금 선물 일봉")                                                   # 짧은 GLDM이 아니라 긴 금 선물 기준
        labels = [d["label"] for d in item["drivers"]]
        self.assertEqual(len(labels), 3)
        self.assertEqual(set(labels[:2]), {"달러", "구리"})                                                # 이 종목을 움직이게 만든 두 요인이 위 두 자리
        self.assertIn(("GC=F", server.ASSET_DRIVERS_YEARS), self.calls)
        self.assertTrue(all(set(d["corr"]) <= {"1년", "3년", "10년"} for d in item["drivers"]))
        self.assertIn("달러", item["model"]["factors"])

    def test_tlt_ranks_the_long_yield_first_and_groups_neighbouring_maturities(self):
        cookie = self.login("key-a")
        self.prepare()
        tlt = self.call(cookie, {"focus": "TLT"})[1]["result"]["items"][0]
        top = tlt["drivers"][0]
        self.assertEqual(top["label"], "30년물 금리")
        self.assertLess(top["corr"]["1년"], -0.5)
        self.assertTrue(top["consistent"])

    def test_short_or_missing_series_is_isolated(self):
        cookie = self.login("key-a")
        self.prepare()
        saved = SERIES.pop("TLT")
        try:
            items = {i["symbol"]: i for i in self.call(cookie)[1]["result"]["items"]}
        finally:
            SERIES["TLT"] = saved
        self.assertEqual(items["TLT"]["status"], "no_data")
        self.assertEqual(items["GLDM"]["status"], "ok")

    def test_fred_factor_joins_with_a_key_and_not_without(self):
        cookie = self.login("key-a")
        without = self.call(cookie, {"focus": "DFFASSET"}, extra=("DFFASSET",))[1]["result"]["items"][0]
        self.assertEqual(self.fred_calls, [])
        self.assertNotIn("연방기금금리", [d["label"] for d in without["drivers"] + without["others"]])
        self.request("POST", "/api/agent/config", {"fred_key": "d" * 32}, cookie=cookie)
        with_key = self.call(cookie, {"focus": "DFFASSET"}, extra=("DFFASSET",))[1]["result"]["items"][0]
        self.assertEqual(with_key["drivers"][0]["label"], "연방기금금리")
        self.assertNotIn("d" * 32, repr(with_key))

    def test_rule_based_question_gets_the_ranking_chart_and_summary(self):
        cookie = self.login("key-a")
        self.prepare()
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            status, payload, _ = self.request("POST", "/api/agent/query", {"question": "금이 가장 영향을 많이 받는 요소 3개가 뭘까?"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([e["tool"] for e in payload["executions"]], ["get_asset_drivers"])
        self.assertTrue(payload["answer"]["charts"][0]["title"].startswith("GLDM — 평소 영향받는 요인") or "평소 영향받는 요인" in payload["answer"]["charts"][0]["title"])
        self.assertEqual(payload["answer"]["tables"], [])
        self.assertTrue(any("평소 가장 영향받는 매크로 요인" in b for b in payload["answer"]["bullets"]))
        self.assertTrue(any("원인은 아닙니다" in b for b in payload["answer"]["bullets"]))

    def test_the_factor_that_is_the_same_asset_is_left_out_of_a_gold_etfs_ranking(self):
        cookie = self.login("key-a")
        self.prepare()
        gc_changes = macro.daily_changes(SERIES["GC=F"], "price")
        days = sorted(gc_changes)
        own = path([0.85 * gc_changes[d] + e for d, e in zip(days, tm.noise(len(days), 0.004, 1200))], days=days)     # GC=F와 상관 0.9대(0.97 미만이라 자동 제외는 안 됨)
        saved = SERIES["GLDM"], SERIES["GC=F"]
        SERIES["GLDM"] = own
        SERIES["GC=F"] = {d: v for d, v in saved[1].items() if d >= DAYS[-600]}                       # 요인용 금 선물은 더 짧게 → 이 종목은 자신의 시세를 쓴다
        try:
            item = self.call(cookie, {"focus": "금"})[1]["result"]["items"][0]
            with mock.patch.object(server, "same_asset_factors", return_value=frozenset()):
                leaked = self.call(cookie, {"focus": "금"})[1]["result"]["items"][0]
        finally:
            SERIES["GLDM"], SERIES["GC=F"] = saved
        self.assertIsNone(item["source"])
        self.assertNotIn("금", [d["label"] for d in item["drivers"] + item["peers"] + item["others"]])  # 같은 자산인 '금' 요인은 어디에도 없다
        self.assertEqual(leaked["peers"][0]["label"], "금")                                              # 제외하지 않으면 비슷한 자산 1위가 되어 순위를 망친다

    def test_factors_for_only_drops_what_belongs_to_the_same_asset(self):
        series = {"GC=F": {1: 1.0}, "DX-Y.NYB": {1: 1.0}, "HG=F": {1: 1.0}}
        self.assertEqual(sorted(server.factors_for("GLDM", series)), ["DX-Y.NYB", "HG=F"])
        self.assertEqual(sorted(server.factors_for("TLT", series)), ["DX-Y.NYB", "GC=F", "HG=F"])
        self.assertIs(server.factors_for("TLT", series), series)

    def test_a_gold_etfs_peers_are_listed_apart_from_its_macro_drivers(self):
        cookie = self.login("key-a")
        self.prepare()
        series = {s: path(c) for s, c in (("SI=F", [0.8 * a + e for a, e in zip(macro.daily_changes(SERIES["GC=F"], "price").values(), tm.noise(N_LONG - 1, 0.004, 1300))]),)}
        SERIES["SI=F"] = series["SI=F"]                                                                 # 금과 거의 같이 움직이는 은
        try:
            item = self.call(cookie, {"focus": "금"})[1]["result"]["items"][0]
        finally:
            SERIES["SI=F"] = path(tm.noise(N_LONG, 0.01, 1007))
        self.assertIn("은", [d["label"] for d in item["peers"]])                                        # 은은 비슷한 자산
        self.assertNotIn("은", [d["label"] for d in item["drivers"]])                                   # 매크로 순위를 가리지 않는다
        self.assertEqual(set(d["label"] for d in item["drivers"][:2]), {"달러", "구리"})

    def test_result_contains_no_account_data(self):
        cookie = self.login("key-a")
        self.prepare()
        text = repr(self.call(cookie)[1])
        for needle in ("789", "555.0", "11100000001", "tok-a", "quantity"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
