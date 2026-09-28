"""배당 추정 테스트: 배당락일 보유 수량, 예전 보유분 보정, 원천징수, 연도·환율."""
from __future__ import annotations

import unittest
from datetime import date

from helpers import dividends


def fill(sym, side, qty, day, cur="USD"):
    return {"symbol": sym, "side": side, "quantity": qty, "currency": cur, "filled_at": day + "T23:40:00+09:00"}


def fx_on(d):
    return 1400.0 if d.year >= 2026 else None


class SharesOnTest(unittest.TestCase):
    def test_bought_on_ex_date_not_entitled(self):
        fills = [fill("A", "BUY", 10, "2026-03-01"), fill("A", "BUY", 5, "2026-03-10"), fill("A", "SELL", 3, "2026-04-01")]
        self.assertEqual(dividends.shares_on(fills, "A", date(2026, 3, 10), 0), 10)   # 배당락일 당일 매수분 제외
        self.assertEqual(dividends.shares_on(fills, "A", date(2026, 3, 11), 0), 15)
        self.assertEqual(dividends.shares_on(fills, "A", date(2026, 5, 1), 2), 14)    # offset 포함
        self.assertEqual(dividends.shares_on(fills, "A", date(2026, 1, 1), 0), 0)


class EstimateTest(unittest.TestCase):
    def test_us_and_kr_withholding(self):
        fills = [fill("SGOV", "BUY", 10, "2026-01-05"), fill("005930", "BUY", 3, "2026-01-05", "KRW")]
        divs = {"SGOV": [(date(2026, 2, 1), 0.4), (date(2025, 12, 1), 0.4)], "005930": [(date(2026, 3, 30), 361.0)]}
        r = dividends.estimate_dividends(fills, {"SGOV": 10, "005930": 3}, {"SGOV": "USD", "005930": "KRW"}, divs, fx_on, 2026)
        by = {x["symbol"]: x for x in r["rows"]}
        self.assertEqual(len(r["rows"]), 2)                                  # 2025년 배당은 제외
        self.assertAlmostEqual(by["SGOV"]["gross_krw"], 10 * 0.4 * 1400)
        self.assertAlmostEqual(by["SGOV"]["net_krw"], 10 * 0.4 * 1400 * 0.85)
        self.assertAlmostEqual(by["005930"]["gross_krw"], 3 * 361)
        self.assertAlmostEqual(by["005930"]["withheld_krw"], 3 * 361 * 0.154)
        self.assertAlmostEqual(r["total_gross_krw"], 5600 + 1083)

    def test_offset_for_shares_not_in_history(self):
        r = dividends.estimate_dividends([], {"TLT": 7}, {"TLT": "USD"}, {"TLT": [(date(2026, 6, 1), 0.3)]}, fx_on, 2026)
        self.assertEqual(r["rows"][0]["shares"], 7)
        self.assertTrue(any("7주" in n for n in r["notes"]))

    def test_missing_fx_excluded_from_total(self):
        r = dividends.estimate_dividends([fill("A", "BUY", 1, "2026-01-02")], {"A": 1}, {"A": "USD"},
                                         {"A": [(date(2026, 2, 1), 1.0)]}, lambda d: None, 2026)
        self.assertIsNone(r["rows"][0]["gross_krw"])
        self.assertEqual(r["total_gross_krw"], 0)
        self.assertTrue(r["notes"])


if __name__ == "__main__":
    unittest.main()
