"""긴 대용 시세: 금 월간 CSV 파싱(1975년 이후만), 하루 캐시, 짧은 응답 거부, 종목별 대응표. 네트워크는 쓰지 않는다."""
from __future__ import annotations

import unittest
from datetime import date

import requests

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import long_history as lh


def csv_text(start_year=1960, end_year=2026):
    rows = ["Date,Price"] + [f"{y}-{m:02d},{100 + (y - 1960) * 10 + m}" for y in range(start_year, end_year + 1) for m in range(1, 13)]
    return "\n".join(rows)


class FakeResp:
    def __init__(self, text="", status=200):
        self.text, self.status = text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))


class ParseTest(unittest.TestCase):
    def test_only_1975_and_later_valid_rows_are_kept(self):
        series = lh.parse_gold_monthly("Date,Price\n1971-08,43\n1974-12,186\n1975-01,176.5\n1980-01-01,675\n2026-09,4319\nbad,1\n2000-01,abc\n2001-01,-5\n2002-13,10\n")
        self.assertEqual(sorted(series), [date(1975, 1, 1), date(1980, 1, 1), date(2026, 9, 1)])
        self.assertEqual(series[date(1975, 1, 1)], 176.5)

    def test_missing_columns_give_empty(self):
        self.assertEqual(lh.parse_gold_monthly("x,y\n1,2\n"), {})
        self.assertEqual(lh.parse_gold_monthly(""), {})


class FetchTest(unittest.TestCase):
    def setUp(self):
        lh.clear_cache()

    def test_fetch_is_cached_for_a_day_and_returns_copies(self):
        calls = []

        def get(url, **kw):
            calls.append(url)
            return FakeResp(csv_text())
        first = lh.fetch_gold_monthly(get)
        first.clear()
        second = lh.fetch_gold_monthly(get)
        self.assertEqual(len(calls), 1)
        self.assertEqual(min(second), date(1975, 1, 1))
        self.assertIn("gold-prices", calls[0])

    def test_short_or_failed_response_is_an_error_and_not_cached(self):
        with self.assertRaises(ValueError):
            lh.fetch_gold_monthly(lambda url, **kw: FakeResp(csv_text(2020, 2021)))
        with self.assertRaises(requests.HTTPError):
            lh.fetch_gold_monthly(lambda url, **kw: FakeResp(status=503))
        self.assertEqual(len(lh.fetch_gold_monthly(lambda url, **kw: FakeResp(csv_text()))), 12 * (2026 - 1975 + 1))


class SourceTableTest(unittest.TestCase):
    def test_gold_and_index_etfs_have_long_sources_others_do_not(self):
        self.assertEqual(lh.long_source_for("GLDM")["kind"], "monthly_gold")
        self.assertEqual(lh.long_source_for("gld")["kind"], "monthly_gold")                  # 대소문자 무시
        spym = lh.long_source_for("SPYM")
        self.assertEqual((spym["kind"], spym["symbol"]), ("daily", "^GSPC"))
        self.assertEqual(lh.long_source_for("069500.KS")["symbol"], "^KS11")
        for symbol in ("MSTR", "TLT", "JEPQ", "UFO", "", None):
            self.assertIsNone(lh.long_source_for(symbol), symbol)

    def test_every_proxy_entry_is_well_formed(self):
        for etf, (proxy, label) in lh.DAILY_PROXIES.items():
            self.assertTrue(proxy.startswith("^") and label, etf)


class SameAssetTest(unittest.TestCase):
    def test_etfs_map_to_the_factor_that_is_the_same_asset(self):
        self.assertEqual(lh.same_asset_factors("GLDM"), frozenset({"GC=F"}))
        self.assertEqual(lh.same_asset_factors("gld"), frozenset({"GC=F"}))
        self.assertEqual(lh.same_asset_factors("SPYM"), frozenset({"^GSPC"}))
        self.assertEqual(lh.same_asset_factors("QQQ"), frozenset({"^IXIC"}))
        self.assertEqual(lh.same_asset_factors("069500.KS"), frozenset({"^KS11"}))
        self.assertEqual(lh.same_asset_factors("IBIT"), frozenset({"BTC-USD"}))
        self.assertEqual(lh.same_asset_factors("XLK"), frozenset({"XLK"}))
        for symbol in ("TLT", "MSTR", "JEPQ", "UFO", "", None):
            self.assertEqual(lh.same_asset_factors(symbol), frozenset(), symbol)

    def test_every_mapped_factor_is_a_real_factor(self):
        import macro
        for etf, factors in lh.SAME_ASSET_FACTORS.items():
            for factor in factors:
                self.assertIn(factor, macro.FACTOR_BY_SYMBOL, (etf, factor))


if __name__ == "__main__":
    unittest.main()
