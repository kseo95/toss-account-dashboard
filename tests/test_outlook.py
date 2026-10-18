"""앞으로의 상승/하락 뷰 재료: 국면 판정, 과거 유사 상황 통계(미래 정보 미사용·표본 부족·완화), 변동성 범위, 요인 시나리오. 네트워크 없음."""
from __future__ import annotations

import math
import unittest

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import macro
import outlook
import test_macro as tm


def walk(n, drift=0.0005, scale=0.01, seed=1):
    return [100.0 * math.exp(sum(c)) for c in _cum(tm.noise(n, scale, seed), drift)]


def _cum(noises, drift):
    total, out = 0.0, []
    for e in noises:
        total += drift + e
        out.append([total])
    return out


class RegimeTest(unittest.TestCase):
    def test_trend_and_depth_buckets_with_boundaries(self):
        flat = [100.0] * 251
        self.assertIsNone(outlook.regime_at(flat, 250 - 1))                          # 시세가 모자라면 판정하지 않는다
        for last, trend, depth in ((96.0, "below", "near"), (94.0, "below", "pullback"), (84.0, "below", "deep")):
            r = outlook.regime_at(flat + [last], 251)
            self.assertEqual((r[0], r[1]), (trend, depth), last)
        rising = [100 * 1.001 ** i for i in range(400)]
        r = outlook.regime_at(rising, 399)
        self.assertEqual((r[0], r[1]), ("above", "near"))
        self.assertAlmostEqual(r[2], 0.0, places=6)

    def test_regime_uses_only_past_data(self):
        closes = walk(600)
        for i in (300, 450, 599):
            self.assertEqual(outlook.regime_at(closes, i), outlook.regime_at(closes[:i + 1], i))   # 뒤의 데이터가 바뀌어도 같다


class DistributionTest(unittest.TestCase):
    def test_distribution_summary(self):
        d = outlook.distribution([-10.0, 0.0, 10.0, 20.0, 30.0])
        self.assertEqual((d["n"], d["median"], d["p25"], d["p75"]), (5, 10.0, 0.0, 20.0))
        self.assertAlmostEqual(d["up_share"], 0.6)                                    # 0은 상승으로 세지 않는다
        self.assertIsNone(outlook.distribution([]))

    def test_skew_thresholds_and_insufficient_samples(self):
        mk = lambda n, share, med: {"n": n, "up_share": share, "median": med}
        self.assertEqual(outlook.skew_of(mk(30, 0.70, 3.0)), "up")
        self.assertEqual(outlook.skew_of(mk(30, 0.40, -2.0)), "down")
        self.assertEqual(outlook.skew_of(mk(30, 0.55, 1.0)), "mixed")
        self.assertEqual(outlook.skew_of(mk(30, 0.70, -1.0)), "mixed")                # 비율은 높아도 중앙값이 음수면 단정하지 않는다
        self.assertEqual(outlook.skew_of(mk(outlook.MIN_ANALOGS - 1, 0.9, 5.0)), "insufficient")
        self.assertEqual(outlook.skew_of(None), "insufficient")
        # 표본은 많아도 독립 사례가 5회 미만이면 판단하지 않는다(비슷한 구간 몇 번에 몰린 표본)
        self.assertEqual(outlook.skew_of(mk(30, 0.87, 3.8), episodes=3), "insufficient")
        self.assertEqual(outlook.skew_of(mk(30, 0.87, 3.8), episodes=outlook.MIN_EPISODES), "up")


class AnalogTest(unittest.TestCase):
    def test_counts_stay_inside_the_data_and_exclude_today(self):
        closes = walk(1500, seed=3)
        a = outlook.analog_stats(closes)
        n = len(closes)
        self.assertLessEqual(a["forward"]["3M"]["n"], (n - 251 - 63) // outlook.SAMPLE_STEP + 1)
        self.assertLessEqual(a["forward"]["1M"]["n"], (n - 251 - 21) // outlook.SAMPLE_STEP + 1)
        self.assertGreaterEqual(a["forward"]["1M"]["n"], a["forward"]["3M"]["n"])      # 더 짧은 기간은 표본이 같거나 많다
        self.assertGreaterEqual(a["episodes"], 1)
        self.assertLessEqual(a["episodes"], a["forward"]["3M"]["n"])
        self.assertIsNotNone(a["baseline_3m"])
        self.assertIn(a["skew"], ("up", "down", "mixed", "insufficient"))

    def test_rare_current_regime_is_relaxed_to_trend_only(self):
        closes = [100 * 1.002 ** i for i in range(800)]
        closes.append(closes[-1] * 0.84)                  # 갑자기 -16%: 200일선 위에 남은 채 깊은 조정 — 이 국면은 과거에 없었다
        a = outlook.analog_stats(closes)
        self.assertTrue(a["relaxed"])
        self.assertGreaterEqual(a["forward"]["3M"]["n"], outlook.MIN_ANALOGS)

    def test_too_short_history_is_insufficient_not_guessed(self):
        a = outlook.analog_stats(walk(330, seed=4))
        self.assertEqual(a["skew"], "insufficient")
        self.assertIsNone(outlook.analog_stats(walk(200)))

    def test_steady_uptrend_has_one_episode_so_no_verdict(self):
        closes = [100 * 1.001 ** i for i in range(1500)]
        a = outlook.analog_stats(closes)
        self.assertEqual(a["episodes"], 1)                         # 같은 국면이 계속 이어졌으므로 독립 사례는 1회
        self.assertEqual(a["skew"], "insufficient")                # 표본은 많아도 독립 사례가 적으면 판단하지 않는다
        self.assertEqual(a["forward"]["3M"]["up_share"], 1.0)
        self.assertAlmostEqual(a["forward"]["3M"]["median"], (1.001 ** 63 - 1) * 100, places=6)


class BandsAndScenariosTest(unittest.TestCase):
    def test_volatility_bands(self):
        closes = [100.0]
        for i in range(120):
            closes.append(closes[-1] * math.exp(0.01 if i % 2 == 0 else -0.01))
        v = outlook.volatility_bands(closes)
        self.assertAlmostEqual(v["annual_vol_pct"], 0.01 * math.sqrt(252) * 100, places=1)
        m = v["bands"]["1M"]
        self.assertAlmostEqual(m["high_pct"], (math.exp(0.01 * math.sqrt(21)) - 1) * 100, places=2)
        self.assertAlmostEqual(m["low"] * m["high"], closes[-1] ** 2, places=6)               # 로그 대칭
        self.assertGreater(v["bands"]["3M"]["high_pct"], m["high_pct"])

    def build(self, weights, scale=0.004):
        factors = {"^TNX": tm.series_from_changes(tm.noise(300, 0.05, 60), "level"), "CL=F": tm.series_from_changes(tm.noise(300, 0.02, 61))}
        ch = {s: macro.daily_changes(factors[s], macro.FACTOR_BY_SYMBOL[s][2]) for s in factors}
        days = sorted(ch["CL=F"])
        eps = tm.noise(len(days), scale, 62)
        asset = tm.series_from_changes([sum(w * ch[s][d] for s, w in weights.items()) + e for d, e in zip(days, eps)])
        return asset, factors

    def test_scenarios_follow_the_sign_of_the_sensitivity(self):
        asset, factors = self.build({"^TNX": -0.15})
        s = outlook.factor_scenarios(asset, factors)
        self.assertGreater(s["r2"], 0.5)
        up, down = s["up"][0], s["down"][0]
        self.assertEqual((up["label"], up["phrase"]), ("10년물 금리", "하락"))             # 금리와 반대로 움직이는 종목: 금리 하락 → 상승
        self.assertEqual((down["label"], down["phrase"]), ("10년물 금리", "상승"))
        self.assertGreater(up["impact_pct"], 0)
        self.assertLess(down["impact_pct"], 0)
        self.assertTrue(up["move"].startswith("-") and up["move"].endswith("bp"))
        self.assertTrue(down["move"].startswith("+"))
        self.assertEqual(s["up"], sorted(s["up"], key=lambda x: x["impact_pct"], reverse=True))

    def test_weak_or_missing_factors_give_no_scenarios(self):
        noise_asset = tm.series_from_changes(tm.noise(300, 0.01, 70))
        factors = {"^TNX": tm.series_from_changes(tm.noise(300, 0.05, 71), "level")}
        s = outlook.factor_scenarios(noise_asset, factors)
        self.assertEqual((s["up"], s["down"]), ([], []))
        self.assertIsNotNone(s["r2"])
        self.assertEqual(outlook.factor_scenarios(noise_asset, {}), {"r2": None, "up": [], "down": []})


class BuildTest(unittest.TestCase):
    def test_ok_result_has_all_parts(self):
        asset = tm.series_from_changes(tm.noise(800, 0.01, 80))
        factors = {"^TNX": tm.series_from_changes(tm.noise(800, 0.05, 81), "level")}
        out = outlook.build_outlook(asset, factors)
        self.assertEqual(out["status"], "ok")
        for key in ("price", "trend", "depth", "drawdown_pct", "month_change_pct", "annual_vol_pct", "bands", "analogs", "scenarios"):
            self.assertIn(key, out)
        self.assertEqual(set(out["bands"]), {"1M", "3M"})

    def test_short_history_is_no_data(self):
        out = outlook.build_outlook(tm.series_from_changes(tm.noise(200, 0.01, 90)), {})
        self.assertEqual((out["status"], out["history_days"]), ("no_data", 201))


class LongSeriesTest(unittest.TestCase):
    """더 긴 대용 시세(월간·지수 일봉)로 과거 유사 국면을 센다. 현재 국면은 종목 자신의 일봉으로 판정한다."""

    def monthly(self, n=620, seed=11):
        from datetime import date
        closes = walk(n, drift=0.004, scale=0.04, seed=seed)
        return {date(1975 + i // 12, i % 12 + 1, 1): v for i, v in enumerate(closes)}

    def asset(self, n=500, seed=3):
        series = tm.series_from_changes(tm.noise(n, 0.01, seed))
        return series

    def factors(self):
        return {}

    def test_monthly_regime_rules_are_the_same_in_months(self):
        flat = [100.0] * 11
        r = outlook.regime_at(flat + [94.0], 11, outlook.MONTHLY["sma"], outlook.MONTHLY["high"])
        self.assertEqual((r[0], r[1]), ("below", "pullback"))
        self.assertIsNone(outlook.regime_at(flat, 10, outlook.MONTHLY["sma"], outlook.MONTHLY["high"]))        # 12개월이 안 되면 판정하지 않는다

    def test_monthly_analogs_have_far_more_independent_episodes_than_a_short_asset_series(self):
        asset = self.asset()
        own = outlook.build_outlook(asset, {})
        longer = outlook.build_outlook(asset, {}, {"kind": "monthly", "series": self.monthly(), "label": "금 월간"})
        self.assertEqual(longer["analogs"]["source"]["kind"], "monthly")
        self.assertEqual(longer["analogs"]["source"]["from"], "1975-01-01")
        self.assertGreater(longer["analogs"]["episodes"], own["analogs"]["episodes"])
        self.assertEqual((longer["trend"], longer["depth"]), (own["trend"], own["depth"]))               # 현재 국면은 종목 자신의 시세로
        self.assertIsNone(own["analogs"]["source"])

    def test_daily_proxy_is_used_when_given(self):
        from datetime import date, timedelta
        proxy = {date(1990, 1, 1) + timedelta(days=i): v for i, v in enumerate(walk(3000, drift=0.0004, seed=5))}
        out = outlook.build_outlook(self.asset(), {}, {"kind": "daily", "series": proxy, "label": "S&P500 지수"})
        self.assertEqual(out["analogs"]["source"]["kind"], "daily")
        self.assertGreater(out["analogs"]["forward"]["3M"]["n"], 100)

    def test_too_short_long_series_falls_back_to_the_asset_and_reports_why(self):
        from datetime import date
        tiny = {date(2020, 1 + i, 1): 100.0 + i for i in range(10)}
        out = outlook.build_outlook(self.asset(), {}, {"kind": "monthly", "series": tiny, "label": "금 월간"}, long_error="금 월간")
        self.assertIsNone(out["analogs"]["source"])
        self.assertEqual(out["analogs"]["source_error"], "금 월간")
        failed = outlook.build_outlook(self.asset(), {}, None, long_error="금 월간")                      # 대용 시세를 못 받은 경우
        self.assertEqual((failed["analogs"]["source"], failed["analogs"]["source_error"]), (None, "금 월간"))

    def test_daily_results_are_unchanged_by_the_generalization(self):
        closes = walk(900, seed=9)
        explicit = outlook.analog_stats(closes, outlook.DAILY)
        self.assertEqual(explicit, outlook.analog_stats(closes))
        self.assertEqual(explicit["forward"]["3M"]["n"], outlook.analog_stats(closes)["forward"]["3M"]["n"])


if __name__ == "__main__":
    unittest.main()
