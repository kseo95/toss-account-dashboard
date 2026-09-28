"""주간 리포트 계산 로직 테스트 (Yahoo/토스 호출은 전부 가짜 데이터로 대체)."""
from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from helpers import hist, server, weekly_bars

NY = ZoneInfo("America/New_York")
PERIODS = [60, 120, 200, 240]


class WeeklySeriesTest(unittest.TestCase):
    def test_groups_by_iso_week_and_takes_last_trading_day(self):
        mon = date(2026, 9, 21)
        bars = [(mon, 1, 1), (mon + timedelta(days=4), 5, 5), (mon + timedelta(days=7), 7, 7)]
        self.assertEqual(server._weekly_series(bars, 1), [5, 7])

    def test_year_boundary_uses_iso_week(self):
        # 2025-12-29(월)~2026-01-02(금)은 ISO 기준 같은 주(2026-W01)
        bars = [(date(2025, 12, 29), 1, 1), (date(2026, 1, 2), 2, 2)]
        self.assertEqual(server._weekly_series(bars, 1), [2])


class ReportWeekEndTest(unittest.TestCase):
    def check(self, ny_time: str, expected: str):
        now = datetime.fromisoformat(ny_time).replace(tzinfo=NY)
        self.assertEqual(server.report_week_end(now), date.fromisoformat(expected), ny_time)

    def test_friday_during_session_is_previous_week(self):
        self.check("2026-09-25 15:00", "2026-09-18")

    def test_friday_after_close_is_this_week(self):
        self.check("2026-09-25 17:00", "2026-09-25")

    def test_weekend_is_this_week(self):
        self.check("2026-09-26 10:00", "2026-09-25")
        self.check("2026-09-27 23:59", "2026-09-25")

    def test_weekdays_are_previous_week(self):
        self.check("2026-09-28 09:00", "2026-09-25")
        self.check("2026-10-01 20:00", "2026-09-25")


class ComputeSymbolWeekTest(unittest.TestCase):
    def compute(self, closes, **kw):
        h = hist(closes, **kw)
        return server.compute_symbol_week(h, h["bars"][-1][0], PERIODS)

    def test_uptrend_all_mas_below_price(self):
        m = self.compute(list(range(1, 301)))
        self.assertAlmostEqual(m["change_pct"], (300 / 299 - 1) * 100)
        self.assertEqual(m["broken"], [])
        self.assertEqual(m["ma_order"], [60, 120, 200, 240])
        self.assertEqual(m["ma_alignment"], "up")
        self.assertEqual(m["nearest"]["period"], 60)
        self.assertAlmostEqual(m["nearest"]["dist_pct"], (300 / 270.5 - 1) * 100)
        self.assertAlmostEqual(m["slope_pct"], (270.5 / 266.5 - 1) * 100)
        self.assertAlmostEqual(m["drawdown_52w_pct"], 0.0)
        self.assertAlmostEqual(m["position_52w_pct"], 100.0)
        self.assertTrue(all(ma["cross"] is None for ma in m["mas"]))

    def test_downtrend_all_mas_broken_from_top(self):
        m = self.compute(list(range(300, 0, -1)))
        self.assertEqual(m["ma_order"], [240, 200, 120, 60])
        self.assertEqual(m["broken"], [240, 200, 120, 60])  # 위에서부터 깨고 내려온 순서
        self.assertEqual(m["ma_alignment"], "down")
        self.assertLess(m["drawdown_52w_pct"], 0)

    def test_partial_break_counts_from_top(self):
        # 횡보 → 급락 → 반등: 역배열인데 위쪽 선 2개만 아직 가격 위에 있음
        m = self.compute([100] * 200 + list(range(100, 40, -1)) + list(range(41, 80)))
        self.assertEqual(m["ma_order"], [240, 200, 120, 60])
        self.assertEqual(m["broken"], [240, 200])

    def test_mixed_alignment(self):
        m = self.compute([100] * 200 + list(range(100, 40, -1)) + list(range(41, 110)))
        self.assertIsNone(m["ma_alignment"])
        self.assertEqual(sorted(m["ma_order"]), PERIODS)

    def test_cross_down_this_week(self):
        m = self.compute([100.0] * 299 + [90.0])
        self.assertEqual({ma["period"]: ma["cross"] for ma in m["mas"]}, {60: "down", 120: "down", 200: "down", 240: "down"})

    def test_cross_up_this_week(self):
        m = self.compute([100.0] * 298 + [90.0, 110.0])
        self.assertTrue(all(ma["cross"] == "up" for ma in m["mas"]))

    def test_no_cross_when_staying_below(self):
        m = self.compute([100.0] * 297 + [90.0, 89.0, 88.0])
        self.assertTrue(all(ma["cross"] is None for ma in m["mas"]))

    def test_short_history_has_no_mas(self):
        m = self.compute(list(range(1, 51)))
        self.assertEqual(m["mas"], [])
        self.assertIsNone(m["nearest"])
        self.assertEqual(m["broken"], [])
        self.assertIsNone(m["slope_pct"])
        self.assertEqual(m["weeks"], 50)

    def test_partial_mas_when_history_between_periods(self):
        m = self.compute(list(range(1, 131)))  # 130주 → 60, 120만
        self.assertEqual([ma["period"] for ma in m["mas"]], [60, 120])

    def test_total_return_uses_adjclose(self):
        closes = [100.0] * 10
        adj = [100.0] * 9 + [102.0]
        m = self.compute(closes, adj=adj)
        self.assertAlmostEqual(m["change_pct"], 0.0)
        self.assertAlmostEqual(m["total_return_pct"], 2.0)

    def test_dividend_ttm_only_last_365_days(self):
        closes = [50.0] * 60
        bars = weekly_bars(closes)
        end = bars[-1][0]
        divs = [(end - timedelta(days=10), 1.0), (end - timedelta(days=200), 1.0), (end - timedelta(days=400), 5.0)]
        m = server.compute_symbol_week({"bars": bars, "divs": divs, "name": "D"}, end, PERIODS)
        self.assertAlmostEqual(m["dividend_ttm"], 2.0)
        self.assertAlmostEqual(m["dividend_yield_pct"], 4.0)

    def test_no_dividend_gives_none_yield(self):
        self.assertIsNone(self.compute([10.0] * 5)["dividend_yield_pct"])

    def test_bars_after_end_are_ignored(self):
        h = hist(list(range(1, 11)))
        end = h["bars"][-3][0]
        m = server.compute_symbol_week(h, end, PERIODS)
        self.assertEqual(m["close"], 8)
        self.assertEqual(m["last_date"], end.isoformat())

    def test_stale_when_no_trade_in_report_week(self):
        h = hist(list(range(1, 11)))
        end = h["bars"][-1][0] + timedelta(days=7)
        self.assertTrue(server.compute_symbol_week(h, end, PERIODS)["stale"])

    def test_needs_two_weeks(self):
        with self.assertRaises(ValueError):
            self.compute([10.0])


class NormalizeConfigTest(unittest.TestCase):
    def default(self):
        return copy.deepcopy(server.default_weekly_report_config())

    def assertRejects(self, cfg, fragment):
        with self.assertRaises(ValueError) as cm:
            server._normalize_weekly_report_config(cfg)
        self.assertIn(fragment, str(cm.exception))

    def test_default_is_valid(self):
        cfg = server._normalize_weekly_report_config(self.default())
        self.assertEqual(cfg["ma_periods"], [60, 120, 200, 240])
        self.assertEqual(sum(g["type"] == "my_account" for g in cfg["groups"]), 1)

    def test_old_file_without_new_fields_gets_defaults(self):
        cfg = self.default()
        del cfg["comment_rules"], cfg["fx_symbol"]
        clean = server._normalize_weekly_report_config(cfg)
        self.assertEqual(clean["comment_rules"], server.WEEKLY_REPORT_COMMENT_DEFAULTS)
        self.assertEqual(clean["fx_symbol"], "KRW=X")

    def test_periods_sorted_and_symbols_uppercased(self):
        cfg = self.default()
        cfg["ma_periods"] = [240, 60]
        cfg["groups"][0]["items"][0]["symbol"] = " ief "
        clean = server._normalize_weekly_report_config(cfg)
        self.assertEqual(clean["ma_periods"], [60, 240])
        self.assertEqual(clean["groups"][0]["items"][0]["symbol"], "IEF")

    def test_empty_label_falls_back_to_symbol(self):
        cfg = self.default()
        cfg["groups"][0]["items"][0]["label"] = ""
        self.assertEqual(server._normalize_weekly_report_config(cfg)["groups"][0]["items"][0]["label"],
                         cfg["groups"][0]["items"][0]["symbol"])

    def test_rejections(self):
        cases = []
        c = self.default(); c["ma_periods"] = [60, 60]; cases.append((c, "중복"))
        c = self.default(); c["ma_periods"] = [1]; cases.append((c, "MA 기간"))
        c = self.default(); c["ma_periods"] = [60.5]; cases.append((c, "MA 기간"))
        c = self.default(); c["ma_periods"] = []; cases.append((c, "MA 기간"))
        c = self.default(); c["groups"] = []; cases.append((c, "그룹"))
        c = self.default(); c["groups"][0]["type"] = "zzz"; cases.append((c, "표 종류"))
        c = self.default(); c["groups"][0]["items"][0]["symbol"] = "A B"; cases.append((c, "심볼"))
        c = self.default(); c["groups"][0]["items"][0]["kind"] = "zzz"; cases.append((c, "값 종류"))
        c = self.default(); c["groups"][0]["items"].append(dict(c["groups"][0]["items"][0])); cases.append((c, "중복"))
        c = self.default(); c["groups"][0]["title"] = ""; cases.append((c, "그룹 이름"))
        c = self.default()
        fx = next(g for g in c["groups"] if g["type"] == "fx_account"); fx["items"] = fx["items"][:1]
        cases.append((c, "정확히 2개"))
        c = self.default()
        next(g for g in c["groups"] if g["type"] == "my_account")["items"] = [{"symbol": "AAPL", "label": "", "kind": "price"}]
        cases.append((c, "자동"))
        c = self.default(); c["groups"].append({"title": "또", "type": "my_account", "items": []}); cases.append((c, "하나만"))
        c = self.default(); c["comment_rules"] = {"near_ma_pct": 50}; cases.append((c, "near_ma_pct"))
        c = self.default(); c["comment_rules"] = {"sector_top_n": True}; cases.append((c, "sector_top_n"))
        c = self.default(); c["benchmark"] = ""; cases.append((c, "비교 기준"))
        c = self.default(); c["groups"] = [{"title": f"g{i}", "type": "basic", "items": []} for i in range(13)]
        cases.append((c, "최대"))
        c = self.default()
        c["groups"] = [{"title": f"g{i}", "type": "basic", "items": [{"symbol": f"S{i}X{j}", "label": "", "kind": "price"} for j in range(40)]} for i in range(4)]
        cases.append((c, "전체 종목"))
        for cfg, frag in cases:
            with self.subTest(frag=frag):
                self.assertRejects(cfg, frag)


def row(label, kind="price", **kw):
    """코멘트 테스트용 최소 행."""
    base = {"label": label, "symbol": label, "kind": kind, "rel_pct": None, "spread_pct": None, "change_pct": 0.0,
            "mas": [{"period": p, "cross": None} for p in PERIODS], "broken": [], "ma_alignment": None, "ma_order": PERIODS,
            "nearest": None, "position_52w_pct": None}
    base.update(kw)
    return base


RULES = dict(server.WEEKLY_REPORT_COMMENT_DEFAULTS)


class GroupCommentsTest(unittest.TestCase):
    def test_sector_top_and_bottom(self):
        g = {"type": "sector", "rows": [row("A", rel_pct=3.0), row("B", rel_pct=1.0), row("C", rel_pct=-0.5), row("D", rel_pct=-4.0)]}
        c = server.build_group_comments(g, RULES, "^GSPC", None)
        self.assertIn("^GSPC 대비 강세: A +3.00%p, B +1.00%p", c)
        self.assertIn("^GSPC 대비 약세: D -4.00%p, C -0.50%p", c)

    def test_sector_skips_when_too_few_rows(self):
        g = {"type": "sector", "rows": [row("A", rel_pct=3.0), row("B", rel_pct=-1.0)]}
        self.assertFalse(any("대비" in x for x in server.build_group_comments(g, RULES, "^GSPC", None)))

    def test_dividend_below_rate_count(self):
        g = {"type": "dividend", "rows": [row("A", spread_pct=1.0), row("B", spread_pct=-1.0), row("C", spread_pct=None)]}
        c = server.build_group_comments(g, RULES, "^GSPC", {"symbol": "^TNX", "value": 5.18})
        self.assertIn("2개 중 1개가 ^TNX(5.18%)보다 배당률이 낮음", c)

    def test_fx_opposite_direction(self):
        g = {"type": "fx_account", "account_pct": 0.32,
             "rows": [row("S&P", change_pct=1.21, mas=[]), row("원/달러", kind="level", change_pct=-0.88, mas=[])]}
        c = server.build_group_comments(g, RULES, "^GSPC", None)
        self.assertIn("S&P +1.21%였지만 원/달러 -0.88%로 원화 기준 +0.32%", c)

    def test_fx_same_direction(self):
        g = {"type": "fx_account", "account_pct": 3.02,
             "rows": [row("S&P", change_pct=2.0, mas=[]), row("원/달러", kind="level", change_pct=1.0, mas=[])]}
        c = server.build_group_comments(g, RULES, "^GSPC", None)
        self.assertTrue(any("확대" in x for x in c))

    def test_trend_last_line_and_near(self):
        g = {"type": "basic", "rows": [
            row("DOWN", ma_alignment="down", broken=list(reversed(PERIODS)), ma_order=list(reversed(PERIODS))),
            row("UP", ma_alignment="up"),
            row("LAST", broken=[60, 120, 200], nearest={"period": 240, "dist_pct": 0.5}),
            row("RATE", kind="rate", ma_alignment="up"),  # 금리는 추세 코멘트 대상 아님
        ]}
        c = server.build_group_comments(g, RULES, "^GSPC", None)
        self.assertIn("하락 추세 (역배열 + 모든 선 아래) 1/4: DOWN", c)
        self.assertIn("상승 추세 (정배열 + 모든 선 위) 1/4: UP", c)
        self.assertIn("마지막 선 하나만 남음: LAST — 240주선", c)
        self.assertIn("주봉 MA 시험 중 (±1% 이내): LAST 240주 (+0.5%)", c)

    def test_near_threshold_is_configurable(self):
        g = {"type": "basic", "rows": [row("X", nearest={"period": 60, "dist_pct": 1.4})]}
        self.assertFalse(any("시험 중" in x for x in server.build_group_comments(g, RULES, "^GSPC", None)))
        rules = dict(RULES, near_ma_pct=1.5)
        self.assertTrue(any("시험 중" in x for x in server.build_group_comments(g, rules, "^GSPC", None)))

    def test_error_rows_ignored(self):
        g = {"type": "sector", "rows": [{"label": "BAD", "error": "x"}]}
        self.assertEqual(server.build_group_comments(g, RULES, "^GSPC", None), [])


class SummaryCommentsTest(unittest.TestCase):
    def test_divergence_calm_fear_and_weak_credit(self):
        g = {"type": "risk", "rows": [
            row("VIX", kind="level", symbol="^VIX", position_52w_pct=8.0, mas=[]),
            row("HYG", symbol="HYG", broken=[60, 120, 200]),
            row("ORCL", symbol="ORCL", mas=[{"period": 60, "cross": "down"}, {"period": 120, "cross": None}]),
            row("OK", symbol="OK"),
        ]}
        c = server.build_summary_comments([g], RULES)
        self.assertTrue(c[0].startswith("괴리: 공포지수는 평온(VIX 52주 위치 8%)한데 약세 지표 2개(HYG, ORCL)"), c[0])

    def test_no_divergence_when_fear_not_calm(self):
        g = {"type": "risk", "rows": [row("VIX", kind="level", symbol="^VIX", position_52w_pct=80.0, mas=[]),
                                      row("HYG", symbol="HYG", broken=PERIODS)]}
        self.assertFalse(any(x.startswith("괴리") for x in server.build_summary_comments([g], RULES)))

    def test_single_ma_symbol_not_counted_as_almost_broken(self):
        # MA가 1개뿐인 종목은 "하나 남음"(0/1)으로 치지 않는다
        g = {"type": "risk", "rows": [row("VIX", kind="level", symbol="^VIX", position_52w_pct=5.0, mas=[]),
                                      row("NEW", symbol="NEW", mas=[{"period": 60, "cross": None}], broken=[])]}
        self.assertFalse(any(x.startswith("괴리") for x in server.build_summary_comments([g], RULES)))

    def test_last_line_deduped_across_groups(self):
        r = row("HYG", symbol="HYG", broken=[60, 120, 200])
        c = server.build_summary_comments([{"type": "basic", "rows": [r]}, {"type": "risk", "rows": [r]}], RULES)
        self.assertEqual(c, ["마지막 주봉 MA 하나만 남은 종목: HYG — 240주선"])


def fake_histories():
    up = list(range(1, 301))
    return {
        "^GSPC": hist(up),
        "^TNX": hist([5.0] * 299 + [5.2]),
        "KRW=X": hist([1400.0] * 299 + [1386.0]),
        "A": hist([float(x) * 2 for x in up]),
        "B": hist([100.0] * 299 + [95.0]),
        "D": hist([50.0] * 300, divs=[(weekly_bars([0] * 300)[-1][0] - timedelta(days=30), 3.0)]),
        "^VIX": hist(list(range(300, 0, -1))),
        "HYG": hist(list(range(300, 0, -1))),
    }


def fake_config():
    return server._normalize_weekly_report_config({
        "ma_periods": PERIODS, "benchmark": "^GSPC", "rate_symbol": "^TNX", "fx_symbol": "KRW=X",
        "groups": [
            {"title": "섹터", "type": "sector", "items": [{"symbol": "A", "label": "A"}, {"symbol": "B", "label": "B"},
                                                         {"symbol": "BAD", "label": "없는 종목"}]},
            {"title": "배당", "type": "dividend", "items": [{"symbol": "D", "label": "D"}]},
            {"title": "계좌", "type": "fx_account", "items": [{"symbol": "^GSPC", "label": "S&P"},
                                                             {"symbol": "KRW=X", "label": "원/달러", "kind": "level"}]},
            {"title": "내 계좌", "type": "my_account", "items": []},
            {"title": "위험", "type": "risk", "items": [{"symbol": "^VIX", "label": "VIX", "kind": "level"},
                                                       {"symbol": "HYG", "label": "HYG"}]},
        ],
    })


class BuildWeeklyReportTest(unittest.TestCase):
    def setUp(self):
        server._weekly_report_cache.clear()
        self.hists = fake_histories()
        self.calls = []

        def fake_get(sym, force=False):
            self.calls.append(sym)
            if sym not in self.hists:
                raise ValueError(f"{sym}: 데이터 없음")
            return self.hists[sym]

        patcher = mock.patch.object(server, "get_yahoo_daily_history", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        end = self.hists["^GSPC"]["bars"][-1][0]
        self.now = datetime.combine(end + timedelta(days=1), datetime.min.time()).replace(hour=10, tzinfo=NY)

    def build(self, force=False):
        return server.build_weekly_report(fake_config(), self.now, force=force)

    def test_structure_and_numbers(self):
        r = self.build()
        self.assertEqual(r["week_end"], self.hists["^GSPC"]["bars"][-1][0].isoformat())
        self.assertEqual(r["prev_week_end"], self.hists["^GSPC"]["bars"][-2][0].isoformat())
        self.assertEqual(r["errors"], {"BAD": "BAD: 데이터 없음"})
        sector = r["groups"][0]
        a = next(x for x in sector["rows"] if x["symbol"] == "A")
        self.assertAlmostEqual(a["rel_pct"], a["change_pct"] - r["benchmark"]["change_pct"])
        bad = next(x for x in sector["rows"] if x["symbol"] == "BAD")
        self.assertIn("error", bad)
        d = r["groups"][1]["rows"][0]
        self.assertAlmostEqual(d["spread_pct"], 6.0 - 5.2)
        fx_group = r["groups"][2]
        self.assertAlmostEqual(fx_group["account_pct"], ((1 + 300 / 299 - 1) * (1 - 0.01) - 1) * 100)
        self.assertAlmostEqual(r["fx"]["change_pct"], -1.0)

    def test_my_account_left_empty_for_handler(self):
        g = next(g for g in self.build()["groups"] if g["type"] == "my_account")
        self.assertEqual(g["rows"], [])
        self.assertEqual(g["comments"], [])

    def test_summary(self):
        s = self.build()["summary"]
        # B는 횡보 후 하락이라 모든 선 아래, VIX는 level이라 제외
        self.assertEqual([x["symbol"] for x in s["all_broken"]], ["B", "HYG"])
        self.assertIn(("B", 60, "down"), {(c["symbol"], c["period"], c["dir"]) for c in s["crosses"]})
        self.assertTrue(any(c.startswith("괴리") for c in s["comments"]))

    def test_cache_and_force(self):
        self.build()
        n = len(self.calls)
        self.build()
        self.assertEqual(len(self.calls), n, "두 번째 호출은 캐시")
        self.build(force=True)
        self.assertGreater(len(self.calls), n)

    def test_cache_key_includes_config(self):
        self.build()
        n = len(self.calls)
        cfg = fake_config()
        cfg["ma_periods"] = [60, 120]
        server.build_weekly_report(cfg, self.now)
        self.assertGreater(len(self.calls), n)


class TossToYahooSymbolTest(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(server.toss_to_yahoo_symbol("005930", "KOSPI", "KRW"), "005930.KS")
        self.assertEqual(server.toss_to_yahoo_symbol("247540", "KOSDAQ", "KRW"), "247540.KQ")
        self.assertIsNone(server.toss_to_yahoo_symbol("123456", "KONEX", "KRW"))
        self.assertIsNone(server.toss_to_yahoo_symbol("123456", None, "KRW"))
        self.assertEqual(server.toss_to_yahoo_symbol("BRK.B", "NYSE", "USD"), "BRK-B")
        self.assertEqual(server.toss_to_yahoo_symbol("TLT", None, "USD"), "TLT")


class FillAccountGroupsTest(BuildWeeklyReportTest):
    def holdings(self):
        def st(sym, name, cur, ev):
            return {"심볼": sym, "종목": name, "통화": cur, "평가금액(원)": ev}
        return {
            "stocks": [st("A", "에이", "USD", 3000.0), st("005930", "삼성", "KRW", 2000.0), st("999999", "코넥스", "KRW", 500.0)],
            "cash": {"krw": 500.0, "usd": 1.0, "usd_krw_rate": 1000.0, "stocks": [st("B", "현금성", "USD", 3000.0)]},
            "totals": {"eval_krw": 10000.0, "profit_loss_krw": -100.0, "rate_pct": -1.0},
        }

    def test_fill(self):
        self.hists["005930.KS"] = hist([100.0] * 299 + [110.0])
        report = copy.deepcopy(self.build())
        server.fill_account_groups(report, fake_config(), self.holdings(), {"005930": {"market": "KOSPI"}, "999999": {"market": "KONEX"}})
        g = next(g for g in report["groups"] if g["type"] == "my_account")
        rows = {r["label"]: r for r in g["rows"]}
        fx = report["fx"]["change_pct"]

        a = rows["에이"]
        self.assertAlmostEqual(a["weight_pct"], 30.0)
        self.assertAlmostEqual(a["krw_change_pct"], ((1 + a["total_return_pct"] / 100) * (1 + fx / 100) - 1) * 100)
        self.assertAlmostEqual(a["contribution_pctp"], 0.30 * a["krw_change_pct"])

        samsung = rows["삼성"]
        self.assertEqual(samsung["yahoo_symbol"], "005930.KS")
        self.assertAlmostEqual(samsung["krw_change_pct"], 10.0)  # 국내는 환율 영향 없음

        self.assertIn("error", rows["코넥스"])
        self.assertTrue(rows["현금성"]["is_cash"])
        self.assertAlmostEqual(rows["예수금 (달러)"]["krw_change_pct"], fx)
        self.assertAlmostEqual(rows["예수금 (원화)"]["krw_change_pct"], 0.0)

        # 에러 종목이 있으면 계좌 합계를 "완전하지 않음"으로 표시(None)
        self.assertIsNone(g["account"]["week_krw_pct"])
        self.assertEqual(g["account"]["eval_krw"], 10000.0)

    def test_account_total_is_sum_of_contributions(self):
        self.hists["005930.KS"] = hist([100.0] * 299 + [110.0])
        h = self.holdings()
        h["stocks"] = h["stocks"][:2]
        report = copy.deepcopy(self.build())
        server.fill_account_groups(report, fake_config(), h, {"005930": {"market": "KOSPI"}})
        g = next(g for g in report["groups"] if g["type"] == "my_account")
        self.assertAlmostEqual(g["account"]["week_krw_pct"], sum(r["contribution_pctp"] for r in g["rows"]))
        self.assertTrue(g["comments"][0].startswith("원화 기준 계좌"))
        self.assertIn("계좌를 끌어올린 종목: 삼성", g["comments"][1])
        # 현금 취급 종목(B, 하락 추세 아님이지만)은 추세/MA 코멘트 대상에서 빠진다
        self.assertFalse(any("현금성" in c for c in g["comments"][3:]))

    def test_no_my_account_group_is_noop(self):
        report = copy.deepcopy(self.build())
        report["groups"] = [g for g in report["groups"] if g["type"] != "my_account"]
        before = copy.deepcopy(report)
        server.fill_account_groups(report, fake_config(), self.holdings(), {})
        self.assertEqual(report, before)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class YahooHistoryParseTest(unittest.TestCase):
    def setUp(self):
        server._yahoo_history_cache.clear()

    def payload(self, offset):
        # KRW=X처럼 UTC 전날 23시에 찍히는 봉 + 같은 날 중복 + None 종가
        t1 = int(datetime(2026, 9, 24, 23, 0, tzinfo=ZoneInfo("UTC")).timestamp())
        t2 = int(datetime(2026, 9, 25, 23, 0, tzinfo=ZoneInfo("UTC")).timestamp())
        t3 = t2 + 60
        return {"chart": {"result": [{
            "meta": {"gmtoffset": offset, "longName": "USD/KRW"},
            "timestamp": [t1, t2, t3, t3 + 86400],
            "indicators": {"quote": [{"close": [1370.0, 1360.0, 1361.0, None]}], "adjclose": [{"adjclose": [1370.0, 1360.0, 1361.0, None]}]},
            "events": {"dividends": {str(t1): {"date": t1, "amount": 0.5}}},
        }]}}

    def test_gmtoffset_shifts_to_local_date_and_dedupes(self):
        with mock.patch.object(server.requests, "get", return_value=FakeResponse(self.payload(3600))):
            h = server.get_yahoo_daily_history("KRW=X")
        self.assertEqual([b[0].isoformat() for b in h["bars"]], ["2026-09-25", "2026-09-26"])
        self.assertEqual(h["bars"][1][1], 1361.0)  # 같은 날짜는 마지막 값
        self.assertEqual(h["divs"], [(date(2026, 9, 25), 0.5)])
        self.assertEqual(h["name"], "USD/KRW")

    def test_cache_and_force(self):
        with mock.patch.object(server.requests, "get", return_value=FakeResponse(self.payload(0))) as g:
            server.get_yahoo_daily_history("X")
            server.get_yahoo_daily_history("X")
            self.assertEqual(g.call_count, 1)
            server.get_yahoo_daily_history("X", force=True)
            self.assertEqual(g.call_count, 2)

    def test_empty_result_raises(self):
        with mock.patch.object(server.requests, "get", return_value=FakeResponse({"chart": {"result": None}})):
            with self.assertRaises(ValueError):
                server.get_yahoo_daily_history("NOPE")


class ValidateSymbolsTest(unittest.TestCase):
    def test_only_new_symbols_are_checked(self):
        cfg = fake_config()
        checked = []

        def fake_get(sym, force=False):
            checked.append(sym)
            if sym == "B":
                raise ValueError("x")
            return {}

        with mock.patch.object(server, "get_yahoo_daily_history", side_effect=fake_get):
            failed = server.validate_weekly_report_symbols(cfg, known={"^GSPC", "^TNX", "KRW=X", "A", "D", "^VIX", "HYG", "BAD"})
        self.assertEqual(sorted(checked), ["B"])
        self.assertEqual(failed, ["B"])


if __name__ == "__main__":
    unittest.main()
