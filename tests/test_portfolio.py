"""리밸런싱·통화 비중·설정 검증 테스트."""
from __future__ import annotations

import unittest

from helpers import server


def stock(sym, name, cur, ev):
    return {"심볼": sym, "종목": name, "통화": cur, "평가금액(원)": ev}


class ComputeRebalanceTest(unittest.TestCase):
    def setUp(self):
        self.rows = [stock("AAA", "에이", "USD", 400.0), stock("BBB", "비", "USD", 200.0),
                     stock("CCC", "씨", "KRW", 100.0), stock("SGOV", "SGOV", "USD", 200.0)]
        self.config = {"targets": [{"label": "묶음", "symbols": ["AAA", "BBB"], "target_pct": 50.0}],
                       "default_rest_target_pct": 5.0, "krw_target_pct": 30.0}

    def test_categories_rest_and_cash_merge(self):
        result = {r["label"]: r for r in server.compute_rebalance(self.rows, 100.0, {"SGOV"}, 1000.0, self.config)}
        self.assertAlmostEqual(result["묶음"]["current_pct"], 60.0)
        self.assertEqual(result["묶음"]["target_pct"], 50.0)
        self.assertAlmostEqual(result["씨"]["current_pct"], 10.0)
        self.assertEqual(result["씨"]["target_pct"], 5.0)
        # 현금 취급 종목(SGOV 20%) + 예수금(10%) → "현금" 하나로 합쳐짐
        self.assertAlmostEqual(result["현금"]["current_pct"], 30.0)
        self.assertEqual(len(result), 3)

    def test_zero_total_does_not_divide_by_zero(self):
        for r in server.compute_rebalance(self.rows, 0.0, set(), 0.0, self.config):
            self.assertEqual(r["current_pct"], 0.0)


class CurrencySplitTest(unittest.TestCase):
    def test_split_includes_cash(self):
        rows = [stock("A", "A", "USD", 600.0), stock("K", "K", "KRW", 200.0)]
        s = server.compute_currency_split(rows, cash_krw_amt=100.0, cash_usd_amt=0.1, usd_krw=1000.0, total_eval=1000.0, krw_target_pct=30.0)
        self.assertAlmostEqual(s["krw_pct"], 30.0)
        self.assertAlmostEqual(s["usd_pct"], 70.0)
        self.assertEqual(s["usd_target_pct"], 70.0)


class ValidateRebalanceTargetsTest(unittest.TestCase):
    def test_valid(self):
        clean, err = server.validate_rebalance_targets([{"label": " 묶음 ", "symbols": ["aaa", "AAA", "bbb"], "target_pct": 10}])
        self.assertIsNone(err)
        self.assertEqual(clean, [{"label": "묶음", "symbols": ["AAA", "BBB"], "target_pct": 10.0}])

    def test_rejections(self):
        cases = [
            ([{"label": "현금", "symbols": ["A"], "target_pct": 1}], "예약된 이름"),
            ([{"label": "x", "symbols": ["A"], "target_pct": 1}, {"label": "x", "symbols": ["B"], "target_pct": 1}], "중복"),
            ([{"label": "x", "symbols": ["A"], "target_pct": 1}, {"label": "y", "symbols": ["A"], "target_pct": 1}], "두 카테고리"),
            ([{"label": "x", "symbols": [], "target_pct": 1}], "하나 이상"),
            ([{"label": "x", "symbols": ["A"], "target_pct": 101}], "목표%"),
            ("nope", "배열"),
        ]
        for targets, frag in cases:
            with self.subTest(frag=frag):
                _, err = server.validate_rebalance_targets(targets)
                self.assertIn(frag, err)


class ValidateMaRulesTest(unittest.TestCase):
    def test_valid(self):
        clean, err = server.validate_ma_rules([{"symbol": "tlt", "interval": "week", "period": 60, "proximity_pct": 2}])
        self.assertIsNone(err)
        self.assertEqual(clean[0]["symbol"], "TLT")

    def test_rejections(self):
        cases = [
            ({"symbol": "TLT", "interval": "month", "period": 60, "proximity_pct": 2}, "봉 종류"),
            ({"symbol": "TLT", "interval": "day", "period": 201, "proximity_pct": 2}, "기간"),
            ({"symbol": "TLT", "interval": "week", "period": 60.5, "proximity_pct": 2}, "기간"),
            ({"symbol": "T L", "interval": "week", "period": 60, "proximity_pct": 2}, "종목 코드"),
            ({"symbol": "TLT", "interval": "week", "period": 60, "proximity_pct": -1}, "근접"),
        ]
        for rule, frag in cases:
            with self.subTest(frag=frag):
                _, err = server.validate_ma_rules([rule])
                self.assertIn(frag, err)


class ToFloatTest(unittest.TestCase):
    def test_to_float(self):
        self.assertEqual(server.to_float("1.5"), 1.5)
        self.assertEqual(server.to_float(None), 0.0)
        self.assertEqual(server.to_float("x", -1), -1)


if __name__ == "__main__":
    unittest.main()
