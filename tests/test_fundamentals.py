"""밸류에이션(fundamentals.py): 야후 지표 정리(적자·이상치·단위·종류), 조회 캐시·실패 상태, 섹터 표본 중앙값, 정당 PBR, 개별주·ETF 판정, 표본 뽑기."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import fundamentals as f


def info(**over):
    base = {"quoteType": "EQUITY", "shortName": "Acme Corp", "sector": "Technology", "currency": "USD", "financialCurrency": "USD", "currentPrice": 100.0, "marketCap": 5e9, "trailingPE": 20.0, "forwardPE": 18.0,
            "priceToSalesTrailing12Months": 4.0, "priceToBook": 6.0, "trailingEps": 5.0, "forwardEps": 5.5, "bookValue": 16.0, "returnOnEquity": 0.25, "trailingPegRatio": 1.4}
    base.update(over)
    return base


def stock(**over):
    m = f.clean_info(info(), "ACME")
    m.update(over)
    return m


STATS = {"per": {"median": 20.0, "n": 25}, "psr": {"median": 4.0, "n": 25}, "pbr": {"median": 6.0, "n": 25}}


class CleanInfoTest(unittest.TestCase):
    def test_a_normal_stock(self):
        m = f.clean_info(info(), "acme")
        self.assertEqual((m["symbol"], m["kind"], m["sector"], m["yahoo_sector"]), ("ACME", "stock", "Information Technology", "Technology"))
        self.assertEqual((m["per"], m["forward_per"], m["psr"], m["pbr"], m["eps"], m["bps"], m["peg"]), (20.0, 18.0, 4.0, 6.0, 5.0, 16.0, 1.4))
        self.assertAlmostEqual(m["roe"], 25.0)                                                          # 야후는 비율, 우리는 %
        self.assertEqual(m["flags"], [])

    def test_a_loss_making_company_has_no_per(self):
        m = f.clean_info(info(trailingPE=None, trailingEps=-5.0, forwardPE=3.0), "LOSS")
        self.assertIsNone(m["per"])
        self.assertIn("loss", m["flags"])
        self.assertEqual(m["forward_per"], 3.0)
        self.assertIsNone(f.clean_info(info(trailingPE=12.0, trailingEps=-1.0), "X")["per"])           # EPS가 적자면 PER 값이 있어도 버린다

    def test_outliers_and_bad_values_are_dropped_and_flagged(self):
        m = f.clean_info(info(trailingPE=9999.0, priceToBook=-3.0, returnOnEquity=1.488, priceToSalesTrailing12Months=float("nan")), "X")
        self.assertIsNone(m["per"])
        self.assertIn("per_outlier", m["flags"])
        self.assertIsNone(m["pbr"])
        self.assertIsNone(m["psr"])
        self.assertIn("roe_outlier", m["flags"])
        self.assertAlmostEqual(m["roe"], 148.8)                                                         # 값은 남기고 판단에서만 제외한다

    def test_kinds_and_garbage(self):
        self.assertEqual(f.clean_info(info(quoteType="ETF", sector=None), "SPY")["kind"], "etf")
        self.assertEqual(f.clean_info(info(quoteType="CRYPTOCURRENCY"), "BTC-USD")["kind"], "other")
        garbage = f.clean_info("not a dict", "Z")
        self.assertEqual((garbage["kind"], garbage["per"], garbage["flags"]), ("other", None, []))
        self.assertEqual(len(f.clean_info(info(shortName="가" * 200), "N")["name"]), f.NAME_MAX)

    def test_korean_detection(self):
        self.assertTrue(f.is_korean("005930.KS"))
        self.assertTrue(f.is_korean("035720.kq"))
        self.assertTrue(f.is_korean("ABC", "KRW"))
        self.assertFalse(f.is_korean("AAPL", "USD"))


class FetchInfoTest(unittest.TestCase):
    def setUp(self):
        f.clear_cache()
        self.addCleanup(f.clear_cache)

    def factory(self, data=None, error=None):
        calls = []

        def make(symbol):
            calls.append(symbol)
            obj = mock.Mock()
            if error:
                type(obj).info = mock.PropertyMock(side_effect=error)
            else:
                obj.info = data
            return obj
        return make, calls

    def test_ok_is_cached(self):
        make, calls = self.factory(info())
        first = f.fetch_info("ACME", make)
        second = f.fetch_info("acme", make)
        self.assertEqual((first["status"], second["status"], calls), ("ok", "ok", ["ACME"]))           # 두 번째는 캐시(대소문자 무관)
        self.assertEqual(first["metrics"]["per"], 20.0)

    def test_failures_become_statuses_and_are_not_cached(self):
        make, calls = self.factory(error=RuntimeError("rate limited"))
        self.assertEqual(f.fetch_info("ACME", make), {"status": "error", "metrics": None})
        self.assertEqual(f.fetch_info("ACME", make)["status"], "error")
        self.assertEqual(calls, ["ACME", "ACME"])                                                         # 오류는 다음에 다시 시도
        make, _ = self.factory(error=ImportError("no yfinance"))
        self.assertEqual(f.fetch_info("X", make)["status"], "unavailable")
        make, _ = self.factory({})
        self.assertEqual(f.fetch_info("Y", make)["status"], "error")                                      # 빈 응답

    def test_bad_symbols_are_rejected_without_a_call(self):
        make, calls = self.factory(info())
        for bad in ("", "a b", "x;rm", "가나다", "A" * 40):
            self.assertEqual(f.fetch_info(bad, make)["status"], "error")
        self.assertEqual(calls, [])


class PeerAndRulesTest(unittest.TestCase):
    def peers(self, values):
        return [{"per": v, "psr": v / 5, "pbr": v / 3} for v in values]

    def test_peer_median_needs_enough_peers(self):
        stats = f.peer_stats(self.peers([10, 20, 30, 40, 50, 60, 70, 80, 90]))
        self.assertEqual((stats["per"]["median"], stats["per"]["n"]), (50, 9))
        self.assertAlmostEqual(stats["pbr"]["median"], 50 / 3)
        self.assertEqual(f.peer_stats(self.peers([10, 20, 30])), {})                                      # 표본이 모자라면 비교하지 않는다
        mixed = self.peers([10] * 7) + [{"per": None, "psr": 2.0, "pbr": 1.0}]
        self.assertNotIn("per", f.peer_stats(mixed))                                                      # 지표마다 따로 센다(PER 7개뿐)
        self.assertIn("pbr", f.peer_stats(mixed))

    def test_justified_pbr(self):
        self.assertAlmostEqual(f.justified_pbr(15.0), (0.15 - 0.03) / (0.09 - 0.03))                       # 2.0
        self.assertIsNone(f.justified_pbr(2.0))                                                           # ROE가 성장률 이하
        self.assertIsNone(f.justified_pbr(-30.0))

    def test_verdict_counts_signals_and_needs_two(self):
        sig = lambda v: {"verdict": v}
        self.assertEqual(f.verdict_from([sig("expensive"), sig("expensive"), sig("fair")])["label"], "expensive")
        self.assertEqual(f.verdict_from([sig("cheap"), sig("cheap"), sig("fair"), sig("fair")])["label"], "cheap")
        self.assertEqual(f.verdict_from([sig("expensive"), sig("cheap")])["label"], "fair")
        self.assertEqual(f.verdict_from([sig("fair"), sig("fair"), sig("expensive")])["label"], "fair")    # 1/3 < 0.5
        self.assertEqual(f.verdict_from([sig("expensive")])["label"], "hold")                              # 신호가 하나면 판단하지 않는다
        self.assertEqual(f.verdict_from([])["signals"], 0)

    def test_sample_peers_is_deterministic_same_sector_and_excludes_itself(self):
        universe = [{"symbol": f"T{i}", "sector": "Information Technology"} for i in range(40)] + [{"symbol": f"H{i}", "sector": "Health Care"} for i in range(10)]
        a = f.sample_peers(universe, "Information Technology", "T3", 25)
        b = f.sample_peers(list(reversed(universe)), "Information Technology", "t3", 25)
        self.assertEqual(a, b)                                                                            # 목록 순서와 무관
        self.assertEqual(len(a), 25)
        self.assertNotIn("T3", a)
        self.assertTrue(all(s.startswith("T") for s in a))


class AssessStockTest(unittest.TestCase):
    def test_expensive_against_every_signal(self):
        m = stock(per=40.0, psr=9.0, pbr=14.0, peg=3.0, roe=25.0)
        a = f.assess_stock(m, STATS)
        self.assertEqual(a["verdict"]["label"], "expensive")
        by = {s["key"]: s for s in a["signals"]}
        self.assertEqual(set(by), {"per", "psr", "pbr", "peg", "roe_pbr"})
        self.assertAlmostEqual(by["per"]["premium_pct"], 100.0)
        self.assertEqual(by["peg"]["verdict"], "expensive")
        self.assertAlmostEqual(by["roe_pbr"]["ref"], (0.25 - 0.03) / 0.06)                                  # 정당 PBR 3.67
        self.assertEqual(a["verdict"]["signals"], 5)

    def test_cheap_and_fair(self):
        cheap = f.assess_stock(stock(per=10.0, psr=2.0, pbr=3.0, peg=0.7, roe=30.0), STATS)
        self.assertEqual(cheap["verdict"]["label"], "cheap")
        fair = f.assess_stock(stock(per=21.0, psr=4.2, pbr=6.1, peg=1.4, roe=None), STATS)
        self.assertEqual(fair["verdict"]["label"], "fair")
        self.assertEqual([s["verdict"] for s in fair["signals"]], ["fair"] * 4)   # ±30% 안은 비슷함

    def test_loss_making_stock_skips_per_and_peg_and_says_so(self):
        m = f.clean_info(info(trailingPE=None, trailingEps=-2.0, trailingPegRatio=2.9, returnOnEquity=-0.5), "LOSS")
        a = f.assess_stock(m, STATS)
        keys = {s["key"] for s in a["signals"]}
        self.assertNotIn("per", keys)
        self.assertNotIn("peg", keys)                                                                     # 적자의 PEG는 쓰지 않는다
        self.assertTrue(any("적자" in n for n in a["notes"]))
        self.assertTrue(any("ROE" in n and "성장률 가정" in n for n in a["notes"]))                       # ROE가 낮으면 ROE 평가를 건너뛴 이유

    def test_roe_outlier_is_not_used(self):
        m = f.clean_info(info(returnOnEquity=1.488, priceToBook=46.0), "AAPL")
        a = f.assess_stock(m, STATS)
        self.assertNotIn("roe_pbr", {s["key"] for s in a["signals"]})
        self.assertTrue(any("이상치" in n for n in a["notes"]))

    def test_without_peers_only_growth_and_profitability_remain(self):
        a = f.assess_stock(stock(per=40.0, peg=3.0, roe=10.0, pbr=8.0), None)
        self.assertEqual({s["key"] for s in a["signals"]}, {"peg", "roe_pbr"})
        self.assertTrue(any("비교 표본을 구하지 못해" in n for n in a["notes"]))
        self.assertEqual(f.assess_stock(stock(per=40.0, peg=None, roe=None), None)["verdict"]["label"], "hold")

    def test_korean_stocks_are_not_judged(self):
        m = f.clean_info(info(currency="KRW"), "005930.KS")
        a = f.assess_stock(m, STATS)
        self.assertEqual((a["verdict"]["label"], a["signals"], a["reason"]), ("hold", [], "korean"))


class ImpliedPricesTest(unittest.TestCase):
    def test_every_method_multiplies_the_sector_median_by_the_companys_own_figure(self):
        m = stock(price=100.0, eps=5.0, forward_eps=6.0, psr=4.0, bps=16.0, pbr=6.0, roe=25.0)
        im = f.implied_prices(m, STATS)
        by = {x["key"]: x for x in im["methods"]}
        self.assertAlmostEqual(by["per"]["price"], 5.0 * 20.0)                                              # EPS × 섹터 PER
        self.assertAlmostEqual(by["forward_per"]["price"], 6.0 * 20.0)
        self.assertAlmostEqual(by["psr"]["price"], 100.0 / 4.0 * 4.0)                                       # 주당매출 25 × 섹터 PSR 4
        self.assertAlmostEqual(by["pbr"]["price"], 16.0 * 6.0)
        self.assertAlmostEqual(by["roe_pbr"]["price"], 16.0 * (0.25 - 0.03) / 0.06)                         # BPS × 정당 PBR
        self.assertAlmostEqual(by["per"]["diff_pct"], 0.0)
        self.assertEqual(im["reliable_n"], 5)
        self.assertAlmostEqual(im["low"], 16.0 * 0.22 / 0.06)                                               # 가장 낮은 방법: ROE 정당 PBR × BPS
        self.assertAlmostEqual(im["high"], 120.0)                                                           # 가장 높은 방법: 선행 EPS × 섹터 PER
        self.assertLessEqual(im["low"], im["mid"])
        self.assertLessEqual(im["mid"], im["high"])

    def test_a_range_wider_than_three_times_is_flagged(self):
        narrow = f.implied_prices(stock(price=100.0, eps=5.0, forward_eps=6.0, psr=4.0, bps=None, roe=None), STATS)
        self.assertFalse(narrow["wide"])
        wide = f.implied_prices(stock(price=100.0, eps=5.0, forward_eps=6.0, psr=4.0, bps=60.0, pbr=2.0, roe=None), STATS)         # 100~120 vs BPS 60 × 6 = 360
        self.assertTrue(wide["wide"])

    def test_distorted_book_value_methods_are_excluded_from_the_range(self):
        m = f.clean_info(info(returnOnEquity=1.488, priceToBook=46.0, bookValue=7.36, trailingEps=8.72, forwardEps=9.58, currentPrice=343.0, priceToSalesTrailing12Months=10.7), "AAPL")
        im = f.implied_prices(m, STATS)
        by = {x["key"]: x for x in im["methods"]}
        self.assertFalse(by["pbr"]["reliable"])
        self.assertIn("자사주 매입", by["pbr"]["reason"])
        self.assertFalse(by["roe_pbr"]["reliable"])                                                           # 정당 PBR도 BPS 기반이라 같이 제외(화면에는 '제외'로 보여 준다)
        self.assertEqual(im["reliable_n"], 3)
        good = [x["price"] for x in im["methods"] if x["reliable"]]
        self.assertEqual((im["low"], im["high"]), (min(good), max(good)))                                    # 제외한 방법은 범위에 들어가지 않는다

    def test_a_loss_making_company_has_no_trailing_per_method_and_few_methods_mean_no_range(self):
        m = f.clean_info(info(trailingPE=None, trailingEps=-3.0, forwardEps=None, forwardPE=None, returnOnEquity=-0.4, priceToSalesTrailing12Months=20.0, priceToBook=1.2), "LOSS")
        im = f.implied_prices(m, STATS)
        self.assertEqual({x["key"] for x in im["methods"]}, {"psr", "pbr"})
        self.assertEqual(im["reliable_n"], 2)
        self.assertIsNotNone(im["low"])                                                                      # 방법 2개면 범위를 낸다
        one = f.implied_prices(f.clean_info(info(trailingEps=None, forwardEps=None, trailingPE=None, priceToSalesTrailing12Months=4.0, bookValue=None, returnOnEquity=None), "ONE"), STATS)
        self.assertEqual((one["reliable_n"], one["low"]), (1, None))                                          # 믿을 만한 방법이 하나면 범위를 내지 않는다

    def test_no_price_korean_currency_mismatch_or_no_method_gives_nothing(self):
        self.assertIsNone(f.implied_prices(stock(price=None), STATS))
        self.assertIsNone(f.implied_prices(f.clean_info(info(currency="KRW"), "005930.KS"), STATS))
        self.assertIsNone(f.implied_prices(f.clean_info(info(currency="USD", financialCurrency="TWD"), "TSM"), STATS))   # ADR: 재무 통화가 다르다
        self.assertIsNone(f.implied_prices(stock(eps=None, forward_eps=None, psr=None, bps=None, roe=None), STATS))
        no_stats = f.implied_prices(stock(price=100.0, bps=16.0, roe=25.0, pbr=6.0), None)                     # 섹터 표본이 없어도 ROE 정당 PBR은 가능
        self.assertEqual([x["key"] for x in no_stats["methods"]], ["roe_pbr"])

    def test_assessment_carries_the_implied_prices_for_us_stocks_only(self):
        self.assertIsNotNone(f.assess_stock(stock(), STATS)["implied"])
        self.assertNotIn("implied", f.assess_stock(f.clean_info(info(currency="KRW"), "005930.KS"), STATS))


class AssessEtfTest(unittest.TestCase):
    SPY = {"symbol": "SPY", "per": 24.0, "pbr": 1.8}

    def etf(self, symbol="QQQ", **over):
        m = f.clean_info(info(quoteType="ETF", sector=None, trailingPE=27.0, priceToBook=2.0, trailingEps=None, bookValue=357.0, returnOnEquity=None, trailingPegRatio=None), symbol)
        m.update(over)
        return m

    def test_equity_etf_against_the_market(self):
        a = f.assess_etf(self.etf(per=36.0, pbr=2.7), self.SPY)
        self.assertEqual(a["verdict"]["label"], "expensive")
        self.assertEqual([s["basis"] for s in a["signals"]], ["market", "market"])
        self.assertEqual(f.assess_etf(self.etf(), self.SPY)["verdict"]["label"], "fair")                  # QQQ 27.0 vs 24.0 = +12%

    def test_non_equity_etfs_and_missing_benchmark(self):
        gold = f.assess_etf(self.etf(symbol="GLD", per=None), self.SPY)
        self.assertEqual((gold["verdict"]["label"], gold["reason"]), ("na", "no_equity_multiples"))
        self.assertEqual(f.assess_etf(self.etf(), None)["verdict"]["label"], "hold")
        self.assertEqual(f.assess_etf(self.etf(), {"symbol": "SPY", "per": None, "pbr": 1.8})["reason"], "no_benchmark")

    def test_the_benchmark_itself(self):
        a = f.assess_etf(self.etf(symbol="SPY", per=24.0, pbr=1.8), self.SPY)
        self.assertEqual((a["verdict"]["label"], a["signals"]), ("benchmark", []))


if __name__ == "__main__":
    unittest.main()
