"""거시 사례집: 사례 데이터 무결성, 국면 변화 계산(금 일봉/월평균 선택), 통념 점검, 지금과 닮은 국면(σ 표준화·길이 보정·금 제외), 사례 선택. 네트워크 없음."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import cases
import test_macro as tm


def daily(start, n, f):
    return {start + timedelta(days=i): f(i) for i in range(n)}


class CaseDataTest(unittest.TestCase):
    def test_every_case_is_well_formed(self):
        ids = [c["id"] for c in cases.CASES]
        self.assertEqual(len(ids), len(set(ids)))
        for c in cases.CASES:
            self.assertTrue(c["title"] and c["summary"] and c["keywords"], c["id"])
            self.assertTrue(all(k == k.lower() and " " not in k for k in c["keywords"]), c["id"])      # 질문은 소문자·공백 없이 비교한다
            last = None
            for p in c["phases"]:
                self.assertLess(p["start"], p["end"], (c["id"], p["name"]))
                self.assertTrue(p["story"] and p["name"])
                if last:
                    self.assertGreaterEqual(p["start"], last, (c["id"], p["name"]))                    # 국면은 시간순이고 겹치지 않는다
                last = p["end"]

    def test_reference_keys_are_consistent(self):
        self.assertEqual(set(cases.LABELS), {k for k, _l, _kind in cases.REFERENCE})
        self.assertEqual(set(cases.KINDS.values()), {"level", "price"})
        self.assertIn("gold", cases.MATCH_EXCLUDE)


class MoveTest(unittest.TestCase):
    def test_level_and_price_changes_and_missing_data(self):
        s = {date(2020, 1, 1): 4.0, date(2020, 1, 31): 4.6}
        p = cases.prep(s)
        self.assertAlmostEqual(cases.change(p, "level", date(2020, 1, 1), date(2020, 1, 31)), 0.6)
        self.assertAlmostEqual(cases.change(p, "price", date(2020, 1, 1), date(2020, 1, 31)), math.log(4.6 / 4.0))
        self.assertIsNone(cases.change(p, "level", date(2019, 1, 1), date(2020, 1, 31)))          # 시세가 시작일보다 늦게 시작
        self.assertIsNone(cases.change(p, "level", date(2020, 1, 1), date(2020, 6, 1)))           # 끝 근처에서 시세가 끊김(stale)
        self.assertIsNone(cases.change(None, "level", date(2020, 1, 1), date(2020, 1, 31)))
        self.assertEqual(cases.display(0.6, "level"), (60.0, "bp"))
        self.assertAlmostEqual(cases.display(math.log(1.1), "price")[0], 10.0)

    def test_gold_uses_daily_after_2000_and_monthly_before(self):
        d = cases.prep(daily(date(2001, 1, 1), 400, lambda i: 100.0 * 1.001 ** i))
        m = cases.prep({date(1980, 1, 1): 675.0, date(1980, 2, 1): 665.0, date(1982, 8, 1): 340.0})
        new = cases.gold_change(d, m, date(2001, 3, 1), date(2001, 9, 1))
        self.assertAlmostEqual(new, math.log(1.001 ** (date(2001, 9, 1) - date(2001, 1, 1)).days / 1.001 ** (date(2001, 3, 1) - date(2001, 1, 1)).days))
        old = cases.gold_change(d, m, date(1980, 1, 31), date(1982, 8, 12))
        self.assertAlmostEqual(old, math.log(340.0 / 675.0), places=6)                        # 1980년대는 월평균(전월 값)
        self.assertIsNone(cases.gold_change(None, None, date(1980, 1, 31), date(1982, 8, 12)))

    def test_phase_moves_skip_missing_series_and_forward_change(self):
        refs = {"tnx": cases.prep({date(2008, 1, 1): 4.0, date(2008, 12, 1): 3.0, date(2009, 3, 2): 3.5, date(2009, 3, 5): 3.6})}
        moves = cases.phase_moves(refs, None, None, date(2008, 1, 2), date(2008, 12, 1))
        self.assertEqual([m["key"] for m in moves], ["tnx"])
        self.assertAlmostEqual(moves[0]["value"], -100.0)
        self.assertEqual(moves[0]["unit"], "bp")
        self.assertAlmostEqual(cases.forward_change(refs["tnx"], "level", date(2008, 12, 1)), 50.0)   # 약 3개월 뒤
        self.assertIsNone(cases.forward_change(refs["tnx"], "level", date(2009, 1, 1)))               # 그만큼의 시세가 아직 없다


class ConventionTest(unittest.TestCase):
    def moves(self, **kw):
        units = {"tnx": "bp", "irx": "bp", "FRED:DFII10": "bp"}
        return [{"key": k, "label": k, "value": v, "unit": units.get(k, "%")} for k, v in kw.items()]

    def test_rate_rule_flags_the_opposite_case_and_ignores_small_moves(self):
        out = cases.conventions(self.moves(gold=49.0, tnx=58.0))
        self.assertEqual([(c["held"]) for c in out], [False])                                  # 금리 +58bp인데 금 +49%: 통념과 달랐음
        self.assertIn("금 +49%", out[0]["detail"])
        self.assertTrue(cases.conventions(self.moves(gold=-8.0, tnx=40.0))[0]["held"])
        self.assertEqual(cases.conventions(self.moves(gold=20.0, tnx=10.0)), [])               # 금리 변화가 작으면 판정하지 않는다
        self.assertIsNone(cases.conventions(self.moves(gold=1.0, tnx=60.0))[0]["held"])        # 금 움직임이 뚜렷하지 않으면 보류

    def test_dollar_and_risk_off_rules(self):
        out = {c["rule"][:4]: c["held"] for c in cases.conventions(self.moves(gold=-25.0, dxy=23.0, spx=-41.0, vix=151.0))}
        self.assertTrue(out["달러가 "])                                                         # 달러 강세 → 금 약세: 맞음
        self.assertFalse(out["위험을 "])                                                        # 증시 폭락인데 금도 하락: 안전자산 통념과 달랐음
        calm = cases.conventions(self.moves(gold=15.0, spx=18.0, vix=65.0))
        self.assertEqual(calm, [])                                                              # 증시가 오르는 중의 VIX 상승은 위험 회피가 아니다

    def test_real_yield_rule_needs_fred_and_no_gold_means_nothing(self):
        out = cases.conventions(self.moves(gold=10.0, **{"FRED:DFII10": -60.0}))
        self.assertTrue(out[0]["held"])
        self.assertIn("실질금리", out[0]["rule"])
        self.assertEqual(cases.conventions(self.moves(tnx=60.0)), [])


class SimilarityTest(unittest.TestCase):
    """합성 시세: 최근 6개월 움직임이 특정 과거 국면과 같도록 만든 뒤 그 국면이 1위인지 본다."""

    def build(self):
        n = 252 * 12
        start = date(2005, 1, 3)
        days = [start + timedelta(days=i) for i in range(int(n * 365 / 252))]
        days = [d for d in days if d.weekday() < 5][:n]
        refs = {}
        for i, (key, _l, kind) in enumerate(cases.REFERENCE):
            if key == "gold" or key.startswith("FRED:"):
                continue
            noise = tm.noise(n, 0.05 if kind == "level" else 0.01, 40 + i)
            vals, v = [], (4.0 if kind == "level" else 100.0)
            for e in noise:
                v = v + e if kind == "level" else v * math.exp(e)
                vals.append(v)
            refs[key] = dict(zip(days, vals))
        return days, refs

    def test_similar_phase_ranks_first_and_gold_is_excluded_from_matching(self):
        days, refs = self.build()
        # 가짜 사례: 2008-01-02 ~ 2008-07-01 구간(약 126거래일)의 요인 움직임을 마지막 6개월에 그대로 복사
        ph_start, ph_end = date(2008, 1, 2), date(2008, 7, 1)
        target = {c["id"]: c for c in cases.CASES}
        fake = [{"id": "fake", "title": "가짜 사례", "keywords": ("가짜",), "summary": "s",
                 "phases": [{"name": "복사된 국면", "start": ph_start, "end": ph_end, "story": "x"},
                            {"name": "반대 국면", "start": date(2010, 1, 4), "end": date(2010, 7, 1), "story": "y"}]}]
        for key, series in refs.items():
            ds = sorted(series)
            src = [d for d in ds if ph_start <= d <= ph_end]
            tail = ds[-len(src):]
            kind = cases.KINDS[key]
            base = series[tail[0]]
            for s_d, t_d in zip(src, tail):
                delta = series[s_d] - series[src[0]] if kind == "level" else series[s_d] / series[src[0]]
                series[t_d] = base + delta if kind == "level" else base * delta
        gold = {d: 100.0 * 1.0005 ** i for i, d in enumerate(days)}
        out = cases.similar_phases({k: cases.prep(v) for k, v in {**refs, "gold": gold}.items()}, cases.prep(gold), None, cases=fake, top=2)
        self.assertEqual(out[0]["phase"], "복사된 국면")
        self.assertLess(out[0]["distance"], out[1]["distance"])
        self.assertNotIn("gold", [c["key"] for c in out[0]["compare"]])                      # 결과로 보는 금은 닮은 정도에서 뺀다
        self.assertGreaterEqual(out[0]["shared"], cases.MIN_SHARED)
        self.assertTrue(all(c["close"] for c in out[0]["compare"]))

    def test_too_few_factors_gives_no_match(self):
        days, refs = self.build()
        few = {k: cases.prep(v) for k, v in list(refs.items())[:2]}
        self.assertEqual(cases.similar_phases(few, None, None), [])

    def test_short_phases_are_skipped(self):
        days, refs = self.build()
        fake = [{"id": "f", "title": "t", "keywords": ("f",), "summary": "s",
                 "phases": [{"name": "너무 짧음", "start": date(2008, 1, 2), "end": date(2008, 1, 10), "story": "x"}]}]
        self.assertEqual(cases.similar_phases({k: cases.prep(v) for k, v in refs.items()}, None, None, cases=fake), [])


class SlowIndicatorTest(unittest.TestCase):
    """월간·주간 지표(CPI 전년비·실업률·연준 총자산)는 국면별 변화에만 쓰고 닮은 정도 계산에서는 뺀다."""

    def test_new_reference_keys_exist_and_slow_ones_are_marked(self):
        keys = {k for k, _l, _kind in cases.REFERENCE}
        for needed in ("xlf", "FRED:DGS2", "FRED:CPIYOY", "FRED:UNRATE", "FRED:WALCL"):
            self.assertIn(needed, keys)
        self.assertTrue(cases.SLOW_KEYS <= keys)
        self.assertTrue(cases.SLOW_KEYS <= cases.MATCH_EXCLUDE)
        self.assertEqual(cases.KINDS["FRED:WALCL"], "price")

    def test_monthly_values_are_read_with_a_longer_stale_limit(self):
        refs = {"FRED:CPIYOY": cases.prep({date(2004, 6, 1): 3.3, date(2006, 6, 1): 4.3}),
                "FRED:UNRATE": cases.prep({date(2004, 6, 1): 5.6, date(2006, 6, 1): 4.6}),
                "tnx": cases.prep({date(2004, 6, 1): 4.0, date(2006, 6, 1): 5.0})}
        moves = {m["key"]: m for m in cases.phase_moves(refs, None, None, date(2004, 6, 30), date(2006, 6, 29))}
        self.assertAlmostEqual(moves["FRED:CPIYOY"]["value"], 100.0)                             # 월간 지표는 한 달 가까이 지난 값도 쓴다
        self.assertAlmostEqual(moves["FRED:UNRATE"]["value"], -100.0)
        self.assertNotIn("tnx", moves)                                                            # 일간 지표는 한 달 전 값이면 쓰지 않는다

    def test_slow_series_never_enter_the_similarity_computation(self):
        days = [date(2005, 1, 3) + timedelta(days=i) for i in range(3000)]
        slow = cases.prep({d: 1.0 + 0.001 * i for i, d in enumerate(days)})
        refs = {k: slow for k in cases.SLOW_KEYS}
        self.assertEqual(cases.current_changes(refs), {})


class MoveLinesTest(unittest.TestCase):
    def test_every_reference_key_is_shown_somewhere_in_the_numbers(self):
        import agent_tools
        shown = {k for group in agent_tools.CASE_MOVE_ROWS for k in group}
        for key, _label, _kind in cases.REFERENCE:
            self.assertIn(key, shown, key)                                                        # 새 지표를 REFERENCE에 넣고 화면 묶음에 빼먹지 않게


class SelectAndBuildTest(unittest.TestCase):
    def test_select_cases_by_year_event_and_spacing(self):
        pick = lambda q: [c["id"] for c in cases.select_cases(q)]
        self.assertEqual(pick("2008년 금융위기 때 금은?"), ["gfc-2008"])
        self.assertIn("covid-2020", pick("코로나 때"))
        self.assertEqual(pick("V O L C K E R"), [])
        self.assertIn("volcker-1979", pick("볼커 긴축"))
        self.assertEqual(pick("안녕"), [])
        self.assertEqual(pick(""), [])
        self.assertLessEqual(len(pick("2008 2013 2015 2018 2020")), 3)                         # 최대 3개

    def test_build_returns_named_cases_with_conventions_and_asset_columns(self):
        refs = {"tnx": daily(date(2004, 1, 1), 3000, lambda i: 4.0 + 0.001 * i), "dxy": daily(date(2004, 1, 1), 3000, lambda i: 90.0 - 0.001 * i)}
        gold = daily(date(2004, 1, 1), 3000, lambda i: 400.0 * 1.0003 ** i)
        asset = {"name": "TLT", "series": daily(date(2004, 1, 1), 3000, lambda i: 80.0 * 1.0001 ** i)}
        out = cases.build(refs, gold, None, "2008년 금융위기", asset)
        self.assertTrue(out["named"])
        self.assertEqual(out["asset"], "TLT")
        case = out["cases"][0]
        self.assertEqual(case["id"], "gfc-2008")
        self.assertEqual(len(case["phases"]), 5)
        first = case["phases"][0]
        self.assertTrue(first["story"])
        self.assertIsNotNone(first["asset_move_pct"])
        self.assertTrue(any(c["rule"].startswith("금리가") for c in first["conventions"]))     # 금리 상승·금 상승이므로 통념과 달랐음이 잡힌다
        self.assertFalse(first["conventions"][0]["held"])
        self.assertEqual([a["id"] for a in out["available"]], [c["id"] for c in cases.CASES])
        none = cases.build(refs, gold, None, "안녕")
        self.assertFalse(none["named"])
        self.assertEqual(none["cases"], [])


if __name__ == "__main__":
    unittest.main()
