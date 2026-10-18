"""고점 이후 큰 하락일(drawdown.py)과 날짜 한정 뉴스 검색(news.headlines_around): 하락일 고르기·연속일 묶기·같은 날 유난히 큰 요인·검색어, 날짜 밖 기사 제거·중복·실패 격리."""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import requests

from helpers import portfolio, server  # noqa: F401  (helpers가 import 경로를 잡는다)
import drawdown
import news


def days_from(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class WorstDaysTest(unittest.TestCase):
    def setUp(self):
        self.days = days_from(date(2026, 1, 5), 30)

    def series(self, drops):
        v, out = 100.0, {}
        for i, d in enumerate(self.days):
            v *= 1 + drops.get(i, 0.001)
            out[d] = v
        return out

    def test_picks_the_biggest_drops_after_the_peak_in_date_order(self):
        s = self.series({5: -0.03, 12: -0.05, 20: -0.02, 25: -0.04})
        got = drawdown.worst_days(s, self.days[2], count=3)
        self.assertEqual([d["date"] for d in got], [self.days[5], self.days[12], self.days[25]])      # -5%, -4%, -3%를 날짜순으로
        self.assertAlmostEqual(got[1]["change_pct"], -5.0, places=3)

    def test_days_before_the_peak_and_small_drops_are_ignored(self):
        s = self.series({1: -0.09, 8: -0.004})
        self.assertEqual(drawdown.worst_days(s, self.days[4]), [])                                    # 고점 이전 하락·1% 미만 하락은 제외

    def test_consecutive_drops_count_as_one_event(self):
        s = self.series({10: -0.05, 11: -0.04, 12: -0.03})
        got = drawdown.worst_days(s, self.days[2])
        self.assertEqual([d["date"] for d in got], [self.days[10]])


class MoversTest(unittest.TestCase):
    def setUp(self):
        self.days = days_from(date(2025, 1, 1), 300)
        self.day = self.days[-1]
        calm = {d: (0.002 if i % 2 else -0.002) for i, d in enumerate(self.days[:-1])}               # 평소 표준편차 약 0.002
        self.meta = {"^SOX": ("반도체지수(SOX)", "price", "상승", "하락"), "^TNX": ("10년물 금리", "level", "상승", "하락"), "DX": ("달러", "price", "강세", "약세")}
        self.changes = {"^SOX": {**calm, self.day: -0.10}, "^TNX": {**calm, self.day: 0.05}, "DX": {**calm, self.day: 0.001}}

    def test_only_unusually_large_moves_and_the_biggest_first(self):
        got = drawdown.day_movers(self.day, self.changes, self.meta)
        self.assertEqual([m["label"] for m in got], ["반도체지수(SOX)", "10년물 금리"])                 # 달러는 평소 수준이라 제외
        self.assertLess(got[0]["change"], -9)                                                          # price는 %
        self.assertAlmostEqual(got[1]["change"], 5.0)                                                  # level은 bp(0.05 → 5bp)
        self.assertEqual(got[0]["phrase"], "하락")

    def test_short_history_or_missing_day_is_skipped(self):
        short = {"^SOX": {d: 0.001 for d in self.days[-10:]}}
        self.assertEqual(drawdown.day_movers(self.day, short, self.meta), [])
        self.assertEqual(drawdown.day_movers(date(2030, 1, 1), self.changes, self.meta), [])

    def test_search_term_names_the_mover_and_direction(self):
        self.assertEqual(drawdown.search_term("반도체지수(SOX)", -6.0), "반도체지수 급락")
        self.assertEqual(drawdown.search_term("VIX", 30.0), "VIX 급등")


class WindowPressureTest(unittest.TestCase):
    """회귀에 안 잡혀도 평소 상관이 있는 요인이 고점 이후 크게 움직였는지(macro.window_pressure)."""

    def build(self, rate_drift, noise=0.01):
        import random
        import macro
        rng = random.Random(7)
        days = days_from(date(2024, 1, 1), 400)
        rate_ch = [rng.gauss(0, 0.04) for _ in days]
        for i in range(len(days) - 40, len(days)):
            rate_ch[i] += rate_drift                                                                  # 마지막 40일 금리 급등
        asset_ch = [-0.2 * r + rng.gauss(0, noise) for r in rate_ch]                                  # 금리와 반대로 움직이는 자산(금리 +1%p당 -0.2%)
        unrelated = [rng.gauss(0, 0.01) for _ in days]

        def path(ch, kind, base):
            v, out = base, {}
            for d, c in zip(days, ch):
                v = v + c if kind == "level" else v * (2.718281828 ** c)
                out[d] = v
            return out
        rate_sym, other_sym = "^TNX", "HG=F"
        factors = {rate_sym: path(rate_ch, "level", 30.0), other_sym: path(unrelated, "price", 100.0)}
        return macro, days, path(asset_ch, "price", 100.0), factors

    def test_a_big_move_in_a_usually_linked_factor_is_a_pressure_in_the_right_direction(self):
        macro, days, asset, factors = self.build(rate_drift=0.05)
        got = macro.window_pressure(asset, factors, since=days[-41])
        self.assertEqual([m["symbol"] for m in got], ["^TNX"])                                        # 상관 없는 구리는 빠진다
        self.assertEqual(got[0]["direction"], "down")                                                 # 금리 급등 × 평소 반대 움직임 → 하락 쪽
        self.assertLess(got[0]["corr"], -0.5)
        self.assertGreater(got[0]["change"], 100)                                                     # 금리는 bp
        self.assertEqual(got[0]["unit"], "bp")

    def test_a_small_move_is_not_reported_and_excluded_symbols_are_skipped(self):
        macro, days, asset, factors = self.build(rate_drift=0.0)
        self.assertEqual(macro.window_pressure(asset, factors, since=days[-41]), [])
        macro, days, asset, factors = self.build(rate_drift=0.05)
        self.assertEqual(macro.window_pressure(asset, factors, since=days[-41], exclude={"^TNX"}), [])


def rss(items):
    body = "".join(f"<item><title>{t} - {s}</title><link>https://example.com/{i}</link><pubDate>{p}</pubDate><source>{s}</source></item>" for i, (t, s, p) in enumerate(items))
    resp = mock.Mock()
    resp.text = f"<rss><channel>{body}</channel></rss>"
    resp.raise_for_status = lambda: None
    return resp


def pub(d, hour=3):
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


class HeadlinesAroundTest(unittest.TestCase):
    def setUp(self):
        news.clear_cache()
        self.day = date(2026, 3, 10)

    def test_query_is_limited_to_the_dates_and_far_articles_are_dropped(self):
        get = mock.Mock(return_value=rss([("그날 급락", "A", pub(self.day)), ("너무 먼 기사", "B", pub(self.day + timedelta(days=9))), ("전날 기사", "C", pub(self.day - timedelta(days=1)))]))
        got = news.headlines_around(["반도체지수 급락"], self.day, get=get)
        self.assertEqual([h["title"] for h in got], ["그날 급락", "전날 기사"])                          # 앞뒤 하루 안만, 가까운 날 먼저
        q = get.call_args.kwargs["params"]["q"]
        self.assertIn("after:2026-03-09", q)
        self.assertIn("before:2026-03-12", q)
        self.assertEqual(got[0]["published"], "2026-03-10")

    def test_duplicates_across_queries_are_removed_and_the_total_is_capped(self):
        get = mock.Mock(return_value=rss([("같은 제목", "A", pub(self.day)), ("다른 제목 하나", "B", pub(self.day)), ("다른 제목 둘", "C", pub(self.day))]))
        got = news.headlines_around(["검색어 하나", "검색어 둘"], self.day, limit=3, get=get)
        self.assertEqual(len({h["title"] for h in got}), len(got))
        self.assertLessEqual(len(got), 3)

    def test_a_failed_search_is_skipped_not_fatal(self):
        def get(url, **kwargs):
            if "나쁜" in kwargs["params"]["q"]:
                raise requests.RequestException("실패")
            return rss([("정상 기사", "A", pub(self.day))])
        got = news.headlines_around(["나쁜 검색어", "좋은 검색어"], self.day, get=get)
        self.assertEqual([h["title"] for h in got], ["정상 기사"])
        self.assertEqual(news.headlines_around(["나쁜 검색어"], self.day, get=get), [])

    def test_ticker_like_names_are_not_used_as_queries(self):
        self.assertIsNone(news.name_query("GLDM", "GLDM"))
        self.assertEqual(news.name_query("SPDR 골드 미니셰어즈", "GLDM"), "SPDR 골드 미니셰어즈 급락")


if __name__ == "__main__":
    unittest.main()
