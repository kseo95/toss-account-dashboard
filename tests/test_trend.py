"""추세 사다리·전환 상태(순수 계산): 기준 검증, 지표 계산, 상태 판정표(버퍼 경계), 연속 주수·직전 상태·확인, 정렬·그룹·최근 변화·상대 성과. 네트워크 없음."""
from __future__ import annotations

import unittest

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import trend as tr

CFG = tr.clean_settings({})


def metrics(**over):
    base = {"close": 100.0, "r_week": 1.0, "r_short": 1.0, "r_mid": 1.0, "r_half": 1.0, "r_long": 1.0, "dist_fast": 3.0, "dist_slow": 8.0, "from_high": -1.0}
    base.update(over)
    return base


class SettingsTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(tr.clean_settings(None), tr.DEFAULTS)
        got = tr.clean_settings({"buffer_pct": 3.5, "confirm_weeks": 3, "short_days": 10})
        self.assertEqual((got["buffer_pct"], got["confirm_weeks"], got["short_days"]), (3.5, 3, 10))
        bad = tr.clean_settings({"buffer_pct": 99, "confirm_weeks": 2.5, "mid_days": "x", "long_days": True, "ma_fast": None})
        self.assertEqual(bad, tr.DEFAULTS)                                                  # 범위 밖·소수·문자·불리언은 기본값

    def test_periods_must_stay_ordered(self):
        mixed = tr.clean_settings({"short_days": 60, "mid_days": 30})                       # 단기 ≥ 중기면 기간 세 개를 기본값으로 되돌린다
        self.assertEqual((mixed["short_days"], mixed["mid_days"], mixed["long_days"]), (21, 63, 252))
        ma = tr.clean_settings({"ma_fast": 100, "ma_slow": 100})
        self.assertEqual((ma["ma_fast"], ma["ma_slow"]), (50, 200))

    def test_only_valid_overrides_pass(self):
        self.assertEqual(tr.valid_overrides({"confirm_weeks": 3, "buffer_pct": 1.5, "mid_days": 90}), {"confirm_weeks": 3, "buffer_pct": 1.5, "mid_days": 90})
        self.assertEqual(tr.valid_overrides({"confirm_weeks": 99, "buffer_pct": "x", "short_days": True, "ma_fast": 12.5, "ma_slow": None, "foo": 1}), {})
        self.assertEqual(tr.valid_overrides(None), {})
        self.assertEqual(tr.valid_overrides({"confirm_weeks": 2.0}), {"confirm_weeks": 2})                  # 정수로 읽히는 실수는 받는다


class MetricsTest(unittest.TestCase):
    def test_returns_and_distances(self):
        closes = [100.0 + i for i in range(400)]
        m = tr.metrics_at(closes, 399, CFG)
        self.assertAlmostEqual(m["r_week"], (499 / 494 - 1) * 100)
        self.assertAlmostEqual(m["r_long"], (499 / 247 - 1) * 100)
        self.assertAlmostEqual(m["dist_slow"], (499 / (sum(closes[-200:]) / 200) - 1) * 100)
        self.assertAlmostEqual(m["dist_fast"], (499 / (sum(closes[-50:]) / 50) - 1) * 100)
        self.assertEqual(m["from_high"], 0.0)

    def test_the_high_is_the_highest_close_in_the_whole_history_not_just_52_weeks(self):
        # 400거래일 전에 정점(250), 이후 하락·횡보: 52주(252거래일) 창에는 정점이 없지만 전 고점은 그 정점이다
        closes = [100.0 + i for i in range(150)] + [250.0 - 0.5 * i for i in range(300)] + [100.0] * 250
        i = len(closes) - 1
        peak = max(closes)
        self.assertEqual(peak, 250.0)
        self.assertGreater(i - closes.index(peak), 252)                                           # 정점이 52주보다 오래전
        m = tr.metrics_at(closes, i, CFG)
        self.assertAlmostEqual(m["from_high"], (100.0 / 250.0 - 1) * 100)
        self.assertLess(m["from_high"], -50)                                                      # 52주 기준이었다면 0%에 가까웠을 값

    def test_the_high_at_an_earlier_point_uses_only_the_data_up_to_that_point(self):
        closes = [100.0 + i for i in range(300)] + [50.0] * 20
        early = tr.metrics_at(closes, 299, CFG)
        self.assertEqual(early["from_high"], 0.0)                                                  # 그 시점에서는 고점이었다(미래 데이터를 쓰지 않는다)
        self.assertLess(tr.metrics_at(closes, 319, CFG)["from_high"], -50)

    def test_not_enough_history_gives_none(self):
        self.assertIsNone(tr.metrics_at([1.0] * 100, 99, CFG))
        self.assertIsNone(tr.metrics_at([1.0] * 300, 250, CFG))                             # 12개월(252거래일) 수익률을 못 구함
        self.assertIsNotNone(tr.metrics_at([1.0] * 300, 252, CFG))


class ClassifyTest(unittest.TestCase):
    def test_every_state_is_reachable_with_the_expected_shape(self):
        c = lambda **kw: tr.classify(metrics(**kw), 2.0)
        self.assertEqual(c(), "상승 유지")
        self.assertEqual(c(r_short=-1.0), "약화 초입")                                      # 단기만 꺾임
        self.assertEqual(c(dist_fast=-3.0), "약화 초입")                                    # 50일선 이탈
        self.assertEqual(c(r_mid=-1.0, r_short=-1.0), "조정 중")                            # 장기선 위·12개월 상승, 3개월 하락
        self.assertEqual(c(r_long=-1.0, r_mid=1.0, r_short=1.0), "상승 전환 초입")          # 장기선을 회복했고 12개월은 아직 하락
        self.assertEqual(c(r_long=-1.0, r_mid=-1.0), "장기선 회복 시도")
        self.assertEqual(c(dist_slow=-5.0, r_long=-1.0, r_mid=-1.0, r_short=1.0), "반등 시도")
        self.assertEqual(c(dist_slow=-5.0, r_long=-1.0, r_mid=1.0, r_short=-1.0), "약세 속 혼조")
        self.assertEqual(c(dist_slow=-5.0, r_long=1.0, r_mid=-1.0, r_short=-1.0), "하락 전환 확인")   # 12개월은 아직 상승
        self.assertEqual(c(dist_slow=-5.0, r_long=-1.0, r_mid=-1.0, r_short=-1.0), "하락 유지")
        self.assertEqual(c(dist_slow=0.5), "장기선 부근")

    def test_buffer_decides_what_counts_as_a_break(self):
        m = metrics(dist_slow=-1.5, r_long=-1.0, r_mid=-1.0, r_short=-1.0)
        self.assertEqual(tr.classify(m, 2.0), "장기선 부근")                                 # 버퍼 2% 안이면 이탈로 세지 않는다
        self.assertEqual(tr.classify(m, 1.0), "하락 유지")                                   # 버퍼를 줄이면 이탈
        self.assertEqual(tr.classify(metrics(dist_slow=2.5), 3.0), "장기선 부근")           # 위쪽도 같은 버퍼
        self.assertEqual(tr.classify(metrics(dist_fast=-1.5), 2.0), "상승 유지")             # 50일선도 버퍼 안이면 이탈이 아님
        self.assertEqual(tr.classify(metrics(dist_fast=-1.5), 1.0), "약화 초입")

    def test_state_legend_is_complete(self):
        self.assertEqual([s for s, _ in tr.STATES], tr.STATE_NAMES)
        self.assertEqual(len(tr.STATE_NAMES), len(set(tr.STATE_NAMES)))
        self.assertTrue(all(meaning for _s, meaning in tr.STATES))


def rising_then_falling(n_up=520, n_down=14, up=0.002, down=0.012):
    closes = [100.0]
    for _ in range(n_up):
        closes.append(closes[-1] * (1 + up))
    for _ in range(n_down):
        closes.append(closes[-1] * (1 - down))
    return closes


class TrendStateTest(unittest.TestCase):
    def independent_states(self, closes, cfg, weeks=12):
        last = len(closes) - 1
        return [tr.classify(tr.metrics_at(closes, last - 5 * k, cfg), cfg["buffer_pct"]) for k in range(weeks + 1)]

    def test_streak_and_previous_state_match_an_independent_recomputation(self):
        closes = rising_then_falling()
        got = tr.trend_state(closes, CFG)
        states = self.independent_states(closes, CFG)
        streak = next(k for k, s in enumerate(states) if s != states[0])                     # 맨 앞에서 같은 상태가 이어진 주 수
        self.assertEqual(got["state"], states[0])
        self.assertEqual(got["since_weeks"], streak)
        self.assertEqual(got["previous_state"], states[streak])
        self.assertEqual(got["previous_weeks_ago"], streak)
        self.assertNotEqual(got["state"], "상승 유지")                                        # 급락 직후라 상승 유지는 아니다

    def test_confirmation_needs_enough_consecutive_weeks(self):
        closes = rising_then_falling()
        strict = tr.trend_state(closes, tr.clean_settings({"confirm_weeks": 8}))
        loose = tr.trend_state(closes, tr.clean_settings({"confirm_weeks": 1}))
        self.assertTrue(loose["confirmed"])
        self.assertLess(strict["since_weeks"], 8)                                              # 급락이 8주나 이어진 시나리오가 아니므로
        self.assertFalse(strict["confirmed"])

    def test_a_steady_uptrend_has_a_long_streak_without_previous_state(self):
        closes = [100.0 * 1.002 ** i for i in range(700)]
        got = tr.trend_state(closes, CFG)
        self.assertEqual((got["state"], got["previous_state"]), ("상승 유지", None))
        self.assertTrue(got["streak_capped"])
        self.assertEqual(got["since_weeks"], tr.LOOKBACK_WEEKS + 1)

    def test_short_history_returns_none(self):
        self.assertIsNone(tr.trend_state([100.0] * 200, CFG))


def series_set():
    return {
        "UP": [100.0 * 1.002 ** i for i in range(700)],
        "DOWN": [300.0 * 0.998 ** i for i in range(700)],
        "FADE": rising_then_falling(),
        "SHORT": [100.0] * 100,
    }


class AnalyzeTest(unittest.TestCase):
    NAMES = {"UP": "상승형", "DOWN": "하락형", "FADE": "꺾임형", "SHORT": "짧음"}
    BENCH = [100.0 * 1.001 ** i for i in range(700)]

    def test_groups_insufficient_and_benchmark(self):
        out = tr.analyze(series_set(), self.NAMES, CFG, self.BENCH)
        self.assertEqual(out["insufficient"], ["SHORT"])
        self.assertEqual(out["groups"]["상승 유지"], ["UP"])
        self.assertEqual(out["groups"]["하락 유지"], ["DOWN"])
        up = next(r for r in out["rows"] if r["symbol"] == "UP")
        bench_mid = out["benchmark"]["metrics"]["r_mid"]
        self.assertAlmostEqual(up["vs_benchmark_mid"], up["metrics"]["r_mid"] - bench_mid)
        self.assertGreater(up["vs_benchmark_mid"], 0)                                          # 시장보다 빠르게 오른다

    def test_default_order_is_average_rank_of_mid_half_and_long_returns(self):
        out = tr.analyze(series_set(), self.NAMES, CFG, None)
        self.assertEqual([r["symbol"] for r in out["rows"]][0], "UP")
        self.assertEqual([r["symbol"] for r in out["rows"]][-1], "DOWN")
        self.assertIsNone(out["benchmark"])
        self.assertTrue(all("rank_score" in r for r in out["rows"]))

    def test_focus_changes_only_the_order_not_the_states(self):
        base = tr.analyze(series_set(), self.NAMES, CFG, None, "all")
        states = {r["symbol"]: r["state"] for r in base["rows"]}
        for focus, key in (("short", "r_short"), ("mid", "r_mid"), ("long", "r_long")):
            out = tr.analyze(series_set(), self.NAMES, CFG, None, focus)
            values = [r["metrics"][key] for r in out["rows"]]
            self.assertEqual(values, sorted(values, reverse=True), focus)
            self.assertEqual({r["symbol"]: r["state"] for r in out["rows"]}, states, focus)       # 상태 판정 기준은 그대로
        self.assertEqual(tr.analyze(series_set(), self.NAMES, CFG, None, "nonsense")["focus"], "all")

    def test_recent_changes_list_only_streaks_of_four_weeks_or_less(self):
        out = tr.analyze(series_set(), self.NAMES, CFG, None)
        changed = {c["symbol"]: c for c in out["changed"]}
        self.assertIn("FADE", changed)
        self.assertLessEqual(changed["FADE"]["weeks_ago"], 4)
        self.assertNotEqual(changed["FADE"]["from"], changed["FADE"]["to"])
        self.assertNotIn("UP", changed)                                                        # 오래 같은 상태인 종목은 변화가 아님

    def test_empty_input(self):
        out = tr.analyze({}, {}, CFG, None)
        self.assertEqual((out["rows"], out["groups"], out["changed"]), ([], {}, []))


if __name__ == "__main__":
    unittest.main()
