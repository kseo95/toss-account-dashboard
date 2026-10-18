"""이동평균 스크리너(순수 계산): 구성 종목 CSV 파싱·캐시·실패, 주봉 묶기, 기간 검증, 이동평균 대비 위치, 모두/하나라도·위/아래 조건, 부족·실패 분리, 정렬·업종 분포."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

import requests

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import screener as sc

CSV = "Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,Date added,CIK,Founded\n" + "\n".join(
    f"{s},{n},{sec},Sub,City,2000-01-01,1,1900" for s, n, sec in
    [("AAA", "에이 Inc", "Information Technology"), ("BRK.B", "Berkshire", "Financials"), ("BF.B", "Brown-Forman", "Consumer Staples"),
     ("$BAD", "나쁜 코드", "Energy"), ("TOOLONGSYMBOLXYZ", "너무 긴", "Energy"), ("", "빈 코드", "Energy")])


class FakeResp:
    def __init__(self, text="", status=200):
        self.text, self.status = text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))


def daily(start, n, f):
    return [(start + timedelta(days=i), f(i), f(i)) for i in range(n)]


class ConstituentsTest(unittest.TestCase):
    def setUp(self):
        sc.clear_cache()

    def test_parse_normalizes_symbols_and_skips_bad_rows(self):
        rows = sc.parse_constituents(CSV)
        self.assertEqual([r["symbol"] for r in rows], ["AAA", "BRK-B", "BF-B"])                 # 야후 표기(점 → 하이픈), 이상한 코드는 제외
        self.assertEqual(rows[0], {"symbol": "AAA", "name": "에이 Inc", "sector": "Information Technology", "sub_industry": "Sub"})

    def test_fetch_requires_enough_rows_and_is_cached(self):
        big = "Symbol,Security,GICS Sector\n" + "\n".join(f"S{i},N{i},Financials" for i in range(450))
        calls = []

        def get(url, **kw):
            calls.append(url)
            return FakeResp(big)
        first = sc.fetch_sp500(get)
        first.clear()
        self.assertEqual(len(sc.fetch_sp500(get)), 450)
        self.assertEqual(len(calls), 1)

    def test_short_or_failed_downloads_raise_a_safe_error(self):
        with self.assertRaises(sc.ScreenerError):
            sc.fetch_sp500(lambda url, **kw: FakeResp(CSV))                                      # 3개뿐이면 형식이 바뀐 것으로 본다
        def boom(url, **kw):
            raise requests.ConnectionError(f"HTTPSConnectionPool: {url} failed")
        with self.assertRaises(sc.ScreenerError) as ctx:
            sc.fetch_sp500(boom)
        self.assertNotIn("http", str(ctx.exception).lower())
        with self.assertRaises(sc.ScreenerError):
            sc.fetch_sp500(lambda url, **kw: FakeResp(status=503))


class WeeklyTest(unittest.TestCase):
    def test_last_trading_day_of_each_iso_week_is_used(self):
        monday = date(2026, 10, 5)
        bars = [(monday + timedelta(days=i), 100.0 + i, 0) for i in range(12) if (monday + timedelta(days=i)).weekday() < 5]
        weekly = sc.weekly_closes(bars)
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly[0], 104.0)                                                       # 첫 주 금요일 종가
        self.assertEqual(weekly[1], 111.0)                                                       # 둘째 주는 진행 중: 최근 종가
        self.assertEqual(sc.weekly_closes([]), [])
        self.assertEqual(sc.weekly_closes([(monday, None, 0), (monday, 0, 0), (monday, -1, 0)]), [])    # 값이 이상한 날은 건너뜀

    def test_unsorted_bars_are_handled(self):
        bars = [(date(2026, 10, 9), 5.0, 0), (date(2026, 10, 6), 3.0, 0)]
        self.assertEqual(sc.weekly_closes(bars), [5.0])


class PeriodsTest(unittest.TestCase):
    def test_clean_periods(self):
        self.assertEqual(sc.clean_periods([240, 60, 120, 200, 60]), [60, 120, 200, 240])
        self.assertEqual(sc.clean_periods([]), list(sc.DEFAULT_PERIODS))
        self.assertEqual(sc.clean_periods(None), list(sc.DEFAULT_PERIODS))
        self.assertEqual(sc.clean_periods([1, 301, "x", True, 2.5, None]), list(sc.DEFAULT_PERIODS))   # 범위 밖·이상한 값만 있으면 기본값
        self.assertEqual(sc.clean_periods([2, 300]), [2, 300])
        self.assertEqual(len(sc.clean_periods(list(range(10, 30)))), sc.MAX_PERIODS)


class EvaluateTest(unittest.TestCase):
    def test_distance_from_each_moving_average(self):
        closes = [100.0] * 10 + [110.0]
        got = sc.evaluate(closes, [2, 11])
        self.assertAlmostEqual(got["mas"][0]["value"], 105.0)
        self.assertAlmostEqual(got["mas"][0]["dist_pct"], (110 / 105 - 1) * 100)
        self.assertAlmostEqual(got["mas"][1]["value"], (100 * 10 + 110) / 11)
        self.assertEqual(got["close"], 110.0)

    def test_not_enough_weeks_means_no_judgement(self):
        self.assertIsNone(sc.evaluate([1.0] * 5, [6]))
        self.assertIsNone(sc.evaluate([], [2]))
        self.assertIsNotNone(sc.evaluate([1.0] * 6, [6]))


UNIVERSE = [{"symbol": s, "name": f"{s} Corp", "sector": sec} for s, sec in
            (("DOWN", "Information Technology"), ("SLIGHT", "Financials"), ("UP", "Information Technology"), ("MIXED", "Financials"),
             ("NEW", "Energy"), ("FAIL", "Energy"))]


def weekly_for():
    n = 300
    return {
        "DOWN": [200.0 - 0.5 * i for i in range(n)],                                          # 계속 하락: 모든 MA 아래, 가장 깊음
        "SLIGHT": [100.0 + 0.0 * i for i in range(n - 1)] + [99.0],                           # 막판에 살짝 하락: 모든 MA 아래, 얕음
        "UP": [50.0 + 0.5 * i for i in range(n)],                                             # 계속 상승: 모든 MA 위
        "MIXED": [20.0 + 0.5 * i for i in range(240)] + [140.0 - (20 / 59) * j for j in range(60)],   # 오래 오르다 최근 조정: 단기 MA 아래·장기 MA 위
        "NEW": [10.0 + i for i in range(50)],                                                 # 주봉 50개뿐: 판단하지 않음
    }                                                                                           # FAIL은 시세를 못 받음(키 없음)


class ScreenTest(unittest.TestCase):
    def test_below_all_sorted_by_depth_with_counts_and_sectors(self):
        out = sc.screen(UNIVERSE, weekly_for(), [60, 120, 200, 240])
        self.assertEqual([m["symbol"] for m in out["matches"]][:2], ["DOWN", "SLIGHT"])           # 더 깊이 아래인 것이 먼저
        self.assertNotIn("UP", [m["symbol"] for m in out["matches"]])
        self.assertEqual((out["universe_size"], out["evaluated"], out["insufficient"], out["failed"]), (6, 4, 1, 1))
        self.assertEqual(out["periods"], [60, 120, 200, 240])
        down = out["matches"][0]
        self.assertTrue(all(m["dist_pct"] < 0 for m in down["mas"]))
        self.assertEqual(down["hit"], [60, 120, 200, 240])
        counts = {s["sector"]: (s["count"], s["total"]) for s in out["sector_counts"]}
        self.assertEqual(counts["Information Technology"], (1, 2))                                  # 업종별 해당 수/평가한 수

    def test_above_reverses_the_direction_and_order(self):
        out = sc.screen(UNIVERSE, weekly_for(), [60, 120, 200, 240], side="above")
        self.assertEqual([m["symbol"] for m in out["matches"]][:1], ["UP"])
        self.assertNotIn("DOWN", [m["symbol"] for m in out["matches"]])

    def test_any_is_a_superset_of_all(self):
        every = {m["symbol"] for m in sc.screen(UNIVERSE, weekly_for(), [60, 120, 200, 240], match="all")["matches"]}
        some = {m["symbol"] for m in sc.screen(UNIVERSE, weekly_for(), [60, 120, 200, 240], match="any")["matches"]}
        self.assertTrue(every <= some)
        self.assertIn("MIXED", some)                                                                 # 섞인 종목은 하나라도 조건에는 해당
        self.assertNotIn("MIXED", every)                                                              # 모두 조건에는 해당하지 않는다
        mixed = next(m for m in sc.screen(UNIVERSE, weekly_for(), [60, 120, 200, 240], match="any")["matches"] if m["symbol"] == "MIXED")
        self.assertEqual(mixed["hit"], [60, 120])                                                     # 단기 두 개 MA 아래

    def test_sector_filter_and_short_history_by_period(self):
        out = sc.screen(UNIVERSE, weekly_for(), [60], sector="technology")                           # 업종은 부분 일치·대소문자 무시
        self.assertEqual({m["sector"] for m in out["matches"]}, {"Information Technology"})
        self.assertEqual(out["universe_size"], 2)
        short = sc.screen(UNIVERSE, weekly_for(), [40])                                              # 40주면 NEW도 판단할 수 있다
        self.assertEqual(short["insufficient"], 0)

    def test_invalid_parameters_fall_back_to_safe_defaults(self):
        out = sc.screen(UNIVERSE, weekly_for(), None, side="sideways", match="whatever")
        self.assertEqual((out["side"], out["match"], out["periods"]), ("below", "all", list(sc.DEFAULT_PERIODS)))

    def test_empty_inputs(self):
        out = sc.screen([], {}, [60])
        self.assertEqual((out["matches"], out["universe_size"], out["evaluated"]), ([], 0, 0))


if __name__ == "__main__":
    unittest.main()
