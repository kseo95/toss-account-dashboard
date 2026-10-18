"""기술적 보조지표(indicators.py): RSI(와일더 기준 예시값)·MACD·볼린저·이동평균 교차 계산, 상태 이름, 기준 바꾸기 검증, 짧은 시세."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

import indicators as ind

WILDER = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]   # 널리 쓰이는 14일 RSI 예시 자료


def days(n, start=date(2025, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def walk(n, drift=0.001, seed=1):
    import random
    rng = random.Random(seed)
    out = [100.0]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + rng.gauss(drift, 0.01)))
    return out


class RsiTest(unittest.TestCase):
    def test_first_value_is_the_simple_average_ratio_and_later_values_use_wilder_smoothing(self):
        r = ind.rsi(WILDER, 14)
        self.assertIsNone(r[13])                                                                           # 14개 변화가 모여야 첫 값
        changes = [WILDER[i] - WILDER[i - 1] for i in range(1, len(WILDER))]
        gain, loss = sum(max(c, 0) for c in changes[:14]) / 14, sum(max(-c, 0) for c in changes[:14]) / 14
        self.assertAlmostEqual(r[14], 100 - 100 / (1 + gain / loss))                                       # 첫 RSI = 100 − 100 ÷ (1 + 평균 상승 ÷ 평균 하락)
        self.assertAlmostEqual(r[14], 70.5, delta=0.1)                                                     # 널리 알려진 예시값(70.53)과 반올림 오차 안
        gain = (gain * 13 + max(changes[14], 0)) / 14                                                      # 와일더 평활: (이전 평균 × 13 + 오늘) ÷ 14
        loss = (loss * 13 + max(-changes[14], 0)) / 14
        self.assertAlmostEqual(r[15], 100 - 100 / (1 + gain / loss))

    def test_only_rising_is_100_and_only_falling_is_0_and_flat_is_100_by_convention(self):
        self.assertEqual(ind.rsi([float(i) for i in range(1, 40)])[-1], 100.0)
        self.assertEqual(ind.rsi([float(40 - i) for i in range(1, 40)])[-1], 0.0)
        self.assertTrue(all(v is None for v in ind.rsi([1.0, 2.0], 14)))                                   # 시세가 모자라면 None
        for v in ind.rsi(walk(200))[14:]:
            self.assertTrue(0 <= v <= 100)


class MacdAndBandsTest(unittest.TestCase):
    def test_macd_is_the_difference_of_two_emas_and_the_histogram_is_macd_minus_signal(self):
        closes = walk(120)
        line, sig, hist = ind.macd(closes)
        e12, e26 = ind.ema(closes, 12), ind.ema(closes, 26)
        self.assertAlmostEqual(line[60], e12[60] - e26[60])
        self.assertIsNone(line[24])                                                                        # 느린 EMA가 생기는 26번째부터
        self.assertIsNotNone(line[25])
        self.assertIsNone(sig[25 + 7])                                                                     # 시그널은 MACD가 9개 모인 뒤부터
        self.assertIsNotNone(sig[25 + 8])
        self.assertAlmostEqual(hist[80], line[80] - sig[80])

    def test_ema_and_sma_seeds(self):
        values = [float(i) for i in range(1, 21)]
        self.assertAlmostEqual(ind.sma(values, 5)[4], 3.0)
        self.assertAlmostEqual(ind.ema(values, 5)[4], 3.0)                                                 # EMA는 첫 n개 평균에서 시작
        self.assertAlmostEqual(ind.ema(values, 5)[5], 6 * (2 / 6) + 3.0 * (4 / 6))

    def test_bollinger_bands_around_a_constant_and_a_known_window(self):
        mid, up, lo = ind.bollinger([10.0] * 30, 20, 2.0)
        self.assertEqual((mid[25], up[25], lo[25]), (10.0, 10.0, 10.0))                                    # 변동이 없으면 밴드가 붙는다
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        mid, up, lo = ind.bollinger(values, 5, 2.0)
        sd = math.sqrt(2.0)                                                                                 # 모표준편차
        self.assertAlmostEqual(mid[4], 3.0)
        self.assertAlmostEqual(up[4], 3.0 + 2 * sd)
        self.assertAlmostEqual(lo[4], 3.0 - 2 * sd)

    def test_cross_detection(self):
        a = [1, 2, 3, 4, 3, 2, 1]
        b = [2, 2.5, 3, 3, 3, 3, 3]
        cross = ind._last_cross(a, b)
        self.assertEqual(cross["type"], "dead")                                                            # 마지막으로 아래로 뚫은 쪽
        self.assertEqual(ind._last_cross([1, 2, 3, 4], [3, 3, 3, 3])["type"], "golden")
        self.assertIsNone(ind._last_cross([1, 1, 1], [2, 2, 2]))


class AnalyzeTest(unittest.TestCase):
    def test_a_steady_uptrend_is_overbought_above_the_signal_and_a_golden_cross_state(self):
        closes = [100 * 1.004 ** i for i in range(260)]
        a = ind.analyze(days(260), closes)
        self.assertEqual(a["rsi"]["state"], "overbought")
        self.assertGreater(a["rsi"]["days_in_state"], 50)
        self.assertTrue(a["macd"]["above_signal"] and a["macd"]["above_zero"])
        self.assertTrue(a["ma_cross"]["fast_above"])
        self.assertIn(a["bollinger"]["state"], ("near_upper", "above_upper", "inside"))
        self.assertEqual(len(a["series"]["dates"]), ind.SERIES_DAYS)
        self.assertEqual(len(a["series"]["rsi"]), ind.SERIES_DAYS)

    def test_a_downtrend_is_oversold_and_below_the_slow_average(self):
        closes = [200 * 0.996 ** i for i in range(260)]
        a = ind.analyze(days(260), closes)
        self.assertEqual(a["rsi"]["state"], "oversold")
        self.assertFalse(a["macd"]["above_zero"])                                                          # MACD가 0 아래(느린 선이 빠른 선 위)
        self.assertFalse(a["ma_cross"]["fast_above"])

    def test_a_recent_macd_cross_is_reported(self):
        closes = [100 + math.sin(i / 6) * 8 for i in range(220)]
        a = ind.analyze(days(220), closes)
        cross = a["macd"]["cross"]
        self.assertIn(cross["type"], ("golden", "dead"))
        self.assertGreaterEqual(cross["days_ago"], 0)
        self.assertEqual(cross["recent"], cross["days_ago"] <= ind.CROSS_RECENT_DAYS)

    def test_short_or_mismatched_data_and_no_slow_average_yet(self):
        self.assertIsNone(ind.analyze(days(30), walk(30)))
        self.assertIsNone(ind.analyze(days(100), walk(101)))
        a = ind.analyze(days(100), walk(100))
        self.assertNotIn("ma_cross", a)                                                                    # 200일 평균이 아직 없으면 교차는 생략
        self.assertIn("rsi", a)

    def test_params_are_validated(self):
        p = ind.clean_params({"rsi_period": 9, "overbought": 80, "oversold": 20, "bb_k": 2.5, "bb_period": 999, "macd_fast": 3})
        self.assertEqual((p["rsi_period"], p["overbought"], p["oversold"], p["bb_k"], p["bb_period"], p["macd_fast"]), (9, 80.0, 20.0, 2.5, 20, 12))
        self.assertEqual(ind.clean_params({"overbought": 40, "oversold": 60}), ind.DEFAULTS)                # 범위 밖(과매수는 55~95, 과매도는 5~45)이면 기본값
        self.assertEqual(ind.clean_params("x"), ind.DEFAULTS)
        self.assertEqual(ind.clean_params({"rsi_period": True}), ind.DEFAULTS)
        a = ind.analyze(days(120), walk(120), {"overbought": 55.0, "oversold": 45.0})
        self.assertIn(a["rsi"]["state"], ("overbought", "oversold", "neutral"))
        self.assertEqual(a["params"]["overbought"], 55.0)


if __name__ == "__main__":
    unittest.main()
