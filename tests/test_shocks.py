"""과거 매크로 충격 반응: 요인 자신의 역사 기준 큰 변동 판정, 사례 묶기, 부호 검정, 이슈 이름 붙이기, 진행 중인 사례 제외. 네트워크 없음."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import shocks
import test_macro as tm


def business_days(n, start=date(2000, 1, 3)):
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def build(n=1800, beta=-0.1, active=True, seed=1):
    """금리(요인)가 오르면 자산이 내리는 합성 시세. active면 마지막 15일 동안 금리가 크게 오른다."""
    days = business_days(n)
    df = tm.noise(n, 0.05, seed)
    if active:
        for i in range(n - 15, n):
            df[i] += 0.05
    tnx, level = {}, 4.0
    for d, c in zip(days, df):
        level += c
        tnx[d] = level
    asset_ret = [beta * c + e for c, e in zip(df, tm.noise(n, 0.004, seed + 1))]
    asset, price = {}, 100.0
    for d, r in zip(days, asset_ret):
        price *= math.exp(r)
        asset[d] = price
    return asset, {"^TNX": tnx}


class BasicsTest(unittest.TestCase):
    def test_window_changes_by_kind(self):
        days = business_days(30)
        level = {d: 4.0 + 0.01 * i for i, d in enumerate(days)}
        price = {d: 100.0 * 1.01 ** i for i, d in enumerate(days)}
        end, change, start = shocks.window_changes(level, "level")[0]
        self.assertAlmostEqual(change, 0.21)
        self.assertEqual((start, end), (days[0], days[21]))
        self.assertAlmostEqual(shocks.window_changes(price, "price")[0][1], 21 * math.log(1.01))
        self.assertEqual(shocks.window_changes({days[0]: -1.0, days[21]: 2.0}, "price"), [])        # 가격이 0 이하면 건너뜀
        self.assertEqual(shocks.display_change(0.4, "level"), (40.0, "bp"))
        self.assertAlmostEqual(shocks.display_change(math.log(1.1), "price")[0], 10.0)

    def test_episodes_split_on_gaps_and_pick_the_extreme_day(self):
        days = business_days(200)
        values = [0.0] * 200
        for i in (10, 11, 12, 40, 41, 100):
            values[i] = 1.0
        values[11] = 3.0
        changes = [(d, v, d) for d, v in zip(days, values)]
        eps = shocks.episodes(changes, 0.5, up=True)
        self.assertEqual(len(eps), 3)                                                       # 10~12 / 40~41 / 100 (21거래일 넘게 떨어짐)
        self.assertEqual((eps[0]["peak"], eps[0]["value"]), (days[11], 3.0))
        close = [(d, v, d) for d, v in zip(days, [1.0 if i in (10, 25) else 0.0 for i in range(200)])]
        self.assertEqual(len(shocks.episodes(close, 0.5, up=True)), 1)                       # 15거래일 간격은 같은 사례
        down = shocks.episodes([(d, -v, d) for d, v in zip(days, values)], -0.5, up=False)
        self.assertEqual(down[0]["value"], -3.0)

    def test_sign_test(self):
        self.assertAlmostEqual(shocks.sign_test_p(10, 10, 0.5), 2 * 0.5 ** 10)
        self.assertAlmostEqual(shocks.sign_test_p(5, 10, 0.5), 1.0)
        self.assertLess(shocks.sign_test_p(1, 20, 0.5), 0.001)                                # 하락이 유난히 적은 쪽도 잡는다
        self.assertEqual(shocks.sign_test_p(3, 0, 0.5), 1.0)
        self.assertEqual(shocks.sign_test_p(3, 5, 0.0), 1.0)

    def test_significant_shocks_sort_first_so_the_display_cap_never_drops_them(self):
        mk = lambda verdict, p, pct: {"verdict": verdict, "p_value": p, "percentile": pct}
        rows = [mk("mixed", 0.5, 0.99), mk("down", 0.04, 0.92), mk("insufficient", None, 0.995), mk("up", 0.001, 0.05), mk("mixed", 0.3, 0.91)]
        ordered = sorted(rows, key=shocks.significance_key)
        self.assertEqual([r["p_value"] for r in ordered[:2]], [0.001, 0.04])                      # 유의한 것(p 작은 순)이 앞
        self.assertEqual([r["percentile"] for r in ordered[2:]], [0.995, 0.99, 0.91])             # 나머지는 더 극단적인 순

    def test_event_labels(self):
        self.assertEqual(shocks.label_for(date(2020, 3, 10)), "코로나19 쇼크")
        self.assertIn("리먼", shocks.label_for(date(2008, 10, 24)))
        self.assertEqual(shocks.label_for(date(2020, 4, 20)), "코로나19 쇼크")                  # 기간 뒤 45일 여유
        self.assertIsNone(shocks.label_for(date(2006, 6, 13)))
        custom = [(date(2010, 1, 1), date(2010, 1, 10), "가짜 이슈")]
        self.assertEqual(shocks.label_for(date(2010, 1, 5), custom), "가짜 이슈")

    def test_verdict_rules(self):
        v = shocks.verdict_of
        self.assertEqual(v(4, -5.0, 0.9, 0.5, 0.001), "insufficient")                         # 독립 사례 5회 미만
        self.assertEqual(v(10, -5.0, 0.9, 0.5, 0.001), "down")
        self.assertEqual(v(10, 4.0, 0.1, 0.5, 0.001), "up")
        self.assertEqual(v(10, -5.0, 0.9, 0.5, 0.2), "mixed")                                 # 우연과 구분 안 됨
        self.assertEqual(v(10, 2.0, 0.9, 0.5, 0.001), "mixed")                                # 하락 비율은 높아도 중앙값이 양수면 단정하지 않는다


class AnalyzeTest(unittest.TestCase):
    def test_active_shock_and_historical_response(self):
        asset, factors = build(beta=-0.1)
        out = shocks.analyze(asset, factors)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["history_from"], "2000-01-03")
        s = next(x for x in out["active"] if x["symbol"] == "^TNX")
        self.assertEqual((s["direction"], s["unit"]), ("up", "bp"))
        self.assertGreater(s["current_change"], 30)
        self.assertGreaterEqual(s["episodes"], shocks.MIN_EPISODES)
        self.assertEqual(s["verdict"], "down")                                                # 금리가 오른 구간에서 자산은 일관되게 내렸다
        self.assertLess(s["median_return"], 0)
        self.assertLess(s["p_value"], 0.01)
        self.assertLessEqual(len(s["examples"]), shocks.TOP_EXAMPLES)
        sizes = [abs(e["factor_change"]) for e in s["examples"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))                                  # 대표 사례는 요인이 가장 극단적이었던 때

    def test_p_values_are_corrected_for_the_number_of_simultaneous_tests(self):
        # 요인 하나만 검정하면 p=0.03이 유의하지만, 큰 변동 중인 요인이 3개면 보정 후 0.09가 되어 유의하지 않다
        base_neg = 0.5
        self.assertEqual(shocks.verdict_of(10, -3.0, 0.85, base_neg, min(1.0, 0.03 * 1)), "down")
        self.assertEqual(shocks.verdict_of(10, -3.0, 0.85, base_neg, min(1.0, 0.03 * 3)), "mixed")
        asset, factors = build(beta=-0.1)
        active = shocks.analyze(asset, factors)["active"]
        self.assertEqual(len(active), 1)                                                          # 요인이 하나뿐이면 보정 배수는 1
        self.assertAlmostEqual(active[0]["p_adjusted"], active[0]["p_value"], places=12)

    def test_without_a_current_shock_nothing_is_active(self):
        asset, factors = build(active=False, seed=5)
        out = shocks.analyze(asset, factors)
        self.assertEqual([x for x in out["active"] if x["direction"] == "up" and x["current_change"] > 60], [])

    def test_the_ongoing_episode_is_not_counted_as_history(self):
        asset, factors = build(beta=-0.1)
        s = shocks.analyze(asset, factors)["active"][0]
        last_day = max(factors["^TNX"])
        for e in s["examples"]:
            self.assertLess((last_day - date.fromisoformat(e["date"])).days, 10 ** 6)
            self.assertGreater((last_day - date.fromisoformat(e["date"])).days, 21)           # 지금 진행 중인 묶음의 날짜는 없다

    def test_unrelated_asset_is_not_called_significant(self):
        asset, factors = build(beta=0.0, seed=7)
        out = shocks.analyze(asset, factors)
        flagged = [s for s in out["active"] if s["verdict"] in ("up", "down")]
        self.assertLessEqual(len(flagged), 1)                                                   # 무관하면 거의 유의하지 않다(우연 1개 정도는 허용)

    def test_short_asset_history_is_no_data(self):
        asset, factors = build(n=300)
        self.assertEqual(shocks.analyze(asset, factors)["status"], "no_data")

    def test_event_reactions_cover_only_events_inside_the_history(self):
        asset, factors = build()
        events = [(date(2000, 6, 1), date(2000, 6, 30), "안의 이슈"), (date(1990, 1, 1), date(1990, 2, 1), "시세 이전 이슈"),
                  (date(2030, 1, 1), date(2030, 2, 1), "시세 이후 이슈")]
        rows = shocks.event_reactions(asset, events)
        self.assertEqual([r["name"] for r in rows], ["안의 이슈"])
        self.assertEqual(rows[0]["days"], 22)                                                   # 2000년 6월의 거래일 수
        crash = dict(asset)
        for d in business_days(1800):
            if date(2000, 6, 1) <= d <= date(2000, 6, 30):
                crash[d] = asset[d] * 0.7
        worst = shocks.analyze(crash, factors, events)["worst_events"]
        self.assertEqual(worst[0]["name"], "안의 이슈")
        self.assertLess(worst[0]["z"], -2)

    def test_stale_factor_is_ignored(self):
        asset, factors = build()
        last = max(factors["^TNX"])
        factors["^TNX"] = {d: v for d, v in factors["^TNX"].items() if d <= last - timedelta(days=30)}
        self.assertEqual(shocks.analyze(asset, factors)["active"], [])


if __name__ == "__main__":
    unittest.main()
