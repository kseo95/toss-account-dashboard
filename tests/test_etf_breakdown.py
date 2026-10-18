"""ETF 구성 종목 기여 계산: 합산 분해, 같은 날짜 기준 정렬, 오래된 시세 제외, 그 밖의 암시 수익률, 경고 플래그."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import etf_breakdown as eb

END = date(2026, 10, 16)   # 금요일


def trading_days(n, end=END):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def series(days, start_value, end_value):
    """첫날 start_value, 마지막 날 end_value, 사이는 직선."""
    n = len(days) - 1
    return {d: start_value + (end_value - start_value) * i / n for i, d in enumerate(days)}


DAYS = trading_days(10)
H = [{"symbol": "AAA", "name": "에이", "weight": 0.30}, {"symbol": "BBB", "name": "비", "weight": 0.20}]


class WindowReturnTest(unittest.TestCase):
    def test_uses_the_closest_close_on_or_before_each_date(self):
        s = {date(2026, 10, 9): 100.0, date(2026, 10, 15): 110.0}
        self.assertAlmostEqual(eb.window_return(s, date(2026, 10, 10), date(2026, 10, 16)), 10.0)   # 시작=10/9 값, 종료=10/15 값

    def test_stale_or_short_series_gives_none(self):
        s = {date(2026, 10, 1): 100.0, date(2026, 10, 2): 100.0}
        self.assertIsNone(eb.window_return(s, date(2026, 10, 9), date(2026, 10, 16)))                # 종료일보다 4일 이상 앞선 시세
        self.assertIsNone(eb.window_return({date(2026, 10, 15): 5.0}, date(2026, 10, 9), date(2026, 10, 16)))   # 시작일 이전 값 없음
        self.assertIsNone(eb.window_return({}, date(2026, 10, 9), date(2026, 10, 16)))


class BreakdownTest(unittest.TestCase):
    def etf(self, start=100.0, end=94.0):
        return series(DAYS, start, end)

    def test_contributions_are_weight_times_change_and_sum_with_the_rest(self):
        cons = {"AAA": series(DAYS, 100, 80), "BBB": series(DAYS, 50, 55)}     # 기간 5거래일은 직선의 뒤 5/9 구간
        result = eb.breakdown(self.etf(), H, cons)
        self.assertEqual(result["status"], "ok")
        rows = {r["symbol"]: r for r in result["rows"]}
        a_change = (cons["AAA"][DAYS[-1]] / cons["AAA"][DAYS[-6]] - 1) * 100
        self.assertAlmostEqual(rows["AAA"]["change_pct"], a_change)
        self.assertAlmostEqual(rows["AAA"]["contribution_pct"], 0.30 * a_change)
        self.assertAlmostEqual(result["explained_pct"] + result["residual_pct"], result["etf_change_pct"])   # 분해가 정확히 맞는다
        self.assertAlmostEqual(result["priced_weight_pct"], 50.0)
        self.assertAlmostEqual(result["rest_weight_pct"], 50.0)
        self.assertEqual([r["symbol"] for r in result["rows"]][0], "AAA")       # 기여 절댓값이 큰 순

    def test_rest_implied_return_closes_the_arithmetic(self):
        cons = {"AAA": series(DAYS, 100, 90), "BBB": series(DAYS, 100, 90)}
        r = eb.breakdown(self.etf(), H, cons)
        self.assertAlmostEqual(r["rest_implied_pct"] * 0.5 + r["explained_pct"], r["etf_change_pct"])
        self.assertAlmostEqual(r["covered_return_pct"] * 0.5, r["explained_pct"])

    def test_holding_without_price_is_listed_and_excluded_from_the_weights(self):
        r = eb.breakdown(self.etf(), H, {"AAA": series(DAYS, 100, 90)})
        self.assertEqual(r["missing"], ["BBB"])
        self.assertIn("missing_prices", r["flags"])
        self.assertAlmostEqual(r["priced_weight_pct"], 30.0)
        missing_row = next(x for x in r["rows"] if x["symbol"] == "BBB")
        self.assertIsNone(missing_row["contribution_pct"])
        self.assertEqual(r["rows"][-1]["symbol"], "BBB")                         # 시세 없는 행은 맨 뒤

    def test_foreign_constituent_with_different_holidays_is_aligned_by_date(self):
        # 시작 직전 거래일에 값이 없는(휴장) 종목: 그날 이전 가장 가까운 종가를 쓴다
        days = [d for d in DAYS if d != DAYS[-6]]
        cons = {"AAA": series(days, 100, 90), "BBB": series(DAYS, 100, 100)}
        r = eb.breakdown(self.etf(), H, cons)
        self.assertEqual(r["status"], "ok")
        self.assertIsNotNone(next(x for x in r["rows"] if x["symbol"] == "AAA")["change_pct"])

    def test_stale_constituent_is_treated_as_missing(self):
        old_days = trading_days(10, end=END - timedelta(days=10))
        r = eb.breakdown(self.etf(), H, {"AAA": series(old_days, 100, 90), "BBB": series(DAYS, 100, 90)})
        self.assertEqual(r["missing"], ["AAA"])

    def test_flags_low_coverage_and_divergence(self):
        small = [{"symbol": "AAA", "name": "a", "weight": 0.10}]
        r = eb.breakdown(self.etf(), small, {"AAA": series(DAYS, 100, 100)})
        self.assertIn("low_coverage", r["flags"])
        # 상위 종목은 크게 떨어졌는데 ETF 전체는 거의 안 움직임 → 그 밖이 반대로 움직인 셈
        cons = {"AAA": series(DAYS, 100, 60), "BBB": series(DAYS, 100, 60)}
        r = eb.breakdown(self.etf(100, 100.0), H, cons)
        self.assertIn("diverged", r["flags"])
        self.assertGreater(r["rest_implied_pct"], 0)
        calm = eb.breakdown(self.etf(), H, {"AAA": series(DAYS, 100, 94), "BBB": series(DAYS, 100, 94)})
        self.assertNotIn("diverged", calm["flags"])

    def test_rest_implied_is_skipped_when_almost_everything_is_covered(self):
        full = [{"symbol": "AAA", "name": "a", "weight": 0.97}]
        r = eb.breakdown(self.etf(), full, {"AAA": series(DAYS, 100, 94)})
        self.assertIsNone(r["rest_implied_pct"])

    def test_no_data_cases(self):
        self.assertEqual(eb.breakdown({DAYS[0]: 1.0}, H, {})["status"], "no_data")           # ETF 시세 부족
        r = eb.breakdown(self.etf(), H, {})                                                  # 편입 종목 시세 전부 없음
        self.assertEqual(r["status"], "no_data")
        self.assertEqual(len(r["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
