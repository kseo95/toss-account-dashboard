"""매크로 요인 분해: 최소제곱·단계적 선택·동일 시리즈 제외·기여 분해 항등식·상태(ok/weak/no_data). 네트워크 없음."""
from __future__ import annotations

import math
import random
import unittest
from unittest import mock
from datetime import date, timedelta

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import macro

START = date(2025, 1, 1)


def dates(n):
    return [START + timedelta(days=i) for i in range(n)]


def series_from_changes(changes, kind="price", start=100.0):
    """일간 변화로 종가 시리즈를 만든다(price: 로그 수익률, level: 차이)."""
    ds, values = dates(len(changes) + 1), [start]
    for c in changes:
        values.append(values[-1] * math.exp(c) if kind == "price" else values[-1] + c)
    return dict(zip(ds, values))


def noise(n, scale, seed):
    rng = random.Random(seed)
    return [rng.gauss(0, scale) for _ in range(n)]


class BasicsTest(unittest.TestCase):
    def test_factor_candidates_include_semis_korea_crypto_copper_credit_and_5y(self):
        labels = {f[1] for f in macro.FACTORS}
        for needed in ("5년물 금리", "반도체지수(SOX)", "코스피", "비트코인", "구리"):
            self.assertIn(needed, labels)
        self.assertEqual(len(macro.FACTOR_BY_SYMBOL), len(macro.ALL_FACTORS))   # 심볼 중복 없음

    def test_daily_changes_by_kind_and_bad_values(self):
        d = dates(4)
        self.assertEqual(macro.daily_changes({d[0]: 4.0, d[1]: 4.5, d[2]: 4.25}, "level"), {d[1]: 0.5, d[2]: -0.25})
        price = macro.daily_changes({d[0]: 100.0, d[1]: 110.0, d[2]: 0.0, d[3]: 99.0}, "price")
        self.assertAlmostEqual(price[d[1]], math.log(1.1))
        self.assertEqual(set(price), {d[1]})                       # 0이나 음수 가격은 건너뛴다

    def test_window_change(self):
        s = series_from_changes([0.01] * 10)
        self.assertAlmostEqual(macro.window_change(s, "price"), 0.05)
        self.assertAlmostEqual(macro.window_change({d: float(i) for i, d in enumerate(dates(8))}, "level"), 5.0)
        self.assertIsNone(macro.window_change(series_from_changes([0.01] * 3), "price"))

    def test_ols_recovers_known_coefficients(self):
        x = noise(300, 1.0, 1)
        y = [2.0 + 3.0 * v for v in x]
        (intercept, beta), r2 = macro.ols(y, [x])
        self.assertAlmostEqual(beta, 3.0, places=6)
        self.assertAlmostEqual(intercept, 2.0, places=6)
        self.assertAlmostEqual(r2, 1.0, places=6)

    def test_ols_returns_none_for_collinear_columns(self):
        x = noise(100, 1.0, 2)
        self.assertIsNone(macro.ols(noise(100, 1.0, 3), [x, x[:]]))


class FitTest(unittest.TestCase):
    N = 300

    def factors(self):
        return {"^TNX": macro.daily_changes(series_from_changes(noise(self.N, 0.05, 10), "level"), "level"),
                "CL=F": macro.daily_changes(series_from_changes(noise(self.N, 0.02, 11)), "price"),
                "DX-Y.NYB": macro.daily_changes(series_from_changes(noise(self.N, 0.005, 12)), "price")}

    def asset_changes(self, f, weights, scale=0.004, seed=20):
        d = sorted(f["CL=F"])
        eps = noise(len(d), scale, seed)
        return {day: sum(w * f[s][day] for s, w in weights.items()) + e for day, e in zip(d, eps)}

    def test_picks_the_true_driver_and_ignores_the_rest(self):
        f = self.factors()
        fit = macro.fit_factors(self.asset_changes(f, {"^TNX": -0.15}), f)
        self.assertEqual(fit["factors"], ["^TNX"])
        self.assertAlmostEqual(fit["betas"][0], -0.15, delta=0.03)
        self.assertGreater(fit["r2"], 0.7)
        self.assertGreaterEqual(fit["observations"], macro.MIN_OBSERVATIONS)

    def test_two_real_drivers_are_both_selected_strongest_first(self):
        f = self.factors()
        fit = macro.fit_factors(self.asset_changes(f, {"^TNX": -0.15, "CL=F": 0.5}), f)
        self.assertEqual(set(fit["factors"]), {"^TNX", "CL=F"})
        self.assertGreater(fit["r2"], 0.85)

    def test_series_nearly_identical_to_the_asset_is_not_a_factor(self):
        f = self.factors()
        d = sorted(f["CL=F"])
        gold_like = {day: f["DX-Y.NYB"][day] * 5 for day in d}           # 자산과 사실상 같은 움직임
        f["^GSPC"] = gold_like
        asset = {day: gold_like[day] + 0.00001 * i % 0.0003 for i, day in enumerate(d)}
        fit = macro.fit_factors(asset, f)
        self.assertNotIn("^GSPC", fit["factors"])

    def test_too_little_overlap_returns_none(self):
        f = self.factors()
        short = {d: 0.001 for d in sorted(f["CL=F"])[-50:]}
        self.assertIsNone(macro.fit_factors(short, f))

    def test_duplicate_factor_columns_do_not_crash(self):
        f = self.factors()
        f["^TYX"] = dict(f["^TNX"])
        fit = macro.fit_factors(self.asset_changes(f, {"^TNX": -0.15}), f)
        self.assertEqual(len([s for s in fit["factors"] if s in ("^TNX", "^TYX")]), 1)


class AttributionTest(unittest.TestCase):
    N = 300

    def build(self, weights, scale=0.004):
        factor_series = {"^TNX": series_from_changes(noise(self.N, 0.05, 30), "level"),
                         "CL=F": series_from_changes(noise(self.N, 0.02, 31))}
        ch = {s: macro.daily_changes(factor_series[s], macro.FACTOR_BY_SYMBOL[s][2]) for s in factor_series}
        d = sorted(ch["CL=F"])
        eps = noise(len(d), scale, 32)
        asset = {day: sum(w * ch[s][day] for s, w in weights.items()) + e for day, e in zip(d, eps)}
        return series_from_changes([asset[day] for day in d]), factor_series

    def test_contributions_plus_residual_equal_the_weekly_move(self):
        asset_series, factor_series = self.build({"^TNX": -0.15, "CL=F": 0.5})
        out = macro.explain_asset(asset_series, factor_series)
        self.assertEqual(out["status"], "ok")
        att = macro.attribute(asset_series, factor_series, macro.fit_factors(
            macro.daily_changes(asset_series, "price"),
            {s: macro.daily_changes(v, macro.FACTOR_BY_SYMBOL[s][2]) for s, v in factor_series.items()}))
        self.assertAlmostEqual(sum(c["contribution_pct"] for c in att["contributions"]) + att["residual_pct"], att["log_change_pct"], places=9)
        self.assertEqual(out["contributions"], sorted(out["contributions"], key=lambda c: abs(c["contribution_pct"]), reverse=True))

    def test_contribution_signs_and_phrases(self):
        asset_series, factor_series = self.build({"^TNX": -0.15})
        out = macro.explain_asset(asset_series, factor_series)
        c = out["contributions"][0]
        self.assertEqual(c["symbol"], "^TNX")
        self.assertEqual(c["phrase"], "상승" if c["factor_change"] > 0 else "하락")
        # 금리와 자산이 반대로 움직이는 관계(β<0)이므로 금리가 오른 주에는 기여가 음수, 내린 주에는 양수
        self.assertEqual(c["contribution_pct"] < 0, c["factor_change"] > 0)

    def test_weak_when_factors_explain_little(self):
        asset_series = series_from_changes(noise(self.N, 0.01, 40))
        factor_series = {"^TNX": series_from_changes(noise(self.N, 0.05, 41), "level"), "CL=F": series_from_changes(noise(self.N, 0.02, 42))}
        out = macro.explain_asset(asset_series, factor_series)
        self.assertEqual(out["status"], "weak")
        self.assertLess(out["r2"], macro.WEAK_R2)

    def test_no_data_states(self):
        factor_series = {"^TNX": series_from_changes(noise(self.N, 0.05, 50), "level")}
        self.assertEqual(macro.explain_asset(series_from_changes([0.01] * 3), factor_series)["status"], "no_data")   # 자산 시세가 짧다
        self.assertEqual(macro.explain_asset(series_from_changes(noise(self.N, 0.01, 51)), {})["status"], "no_data")      # 요인이 없다
        self.assertEqual(macro.explain_asset(series_from_changes(noise(60, 0.01, 52)), factor_series)["status"], "no_data")  # 겹치는 날이 적다

    def test_weekly_factor_moves_use_bp_for_rates_and_percent_for_prices(self):
        series = {"^TNX": {d: 4.0 + 0.02 * i for i, d in enumerate(dates(8))},                      # 5거래일 +0.10%p = +10bp
                  "CL=F": {d: 100.0 * (1.01 ** i) for i, d in enumerate(dates(8))}}                   # 5거래일 약 +5.1%
        moves = {m["symbol"]: m for m in macro.weekly_factor_moves(series)}
        self.assertAlmostEqual(moves["^TNX"]["change"], 10.0)
        self.assertEqual((moves["^TNX"]["unit"], moves["^TNX"]["phrase"]), ("bp", "상승"))
        self.assertAlmostEqual(moves["CL=F"]["change"], (1.01 ** 5 - 1) * 100, places=6)
        self.assertEqual(moves["CL=F"]["unit"], "%")
        self.assertEqual(macro.weekly_factor_moves({}), [])


class RigorTest(unittest.TestCase):
    """통계적 엄격함: 계수의 t값, 우연한 요인 제거, 검증 구간 설명력, 앞·뒤 절반 부호 안정성."""
    N = 300

    def factors(self):
        return {"^TNX": noise(self.N, 0.05, 71), "CL=F": noise(self.N, 0.02, 72), "^VIX": noise(self.N, 0.03, 73)}

    def test_ols_detail_gives_large_t_for_a_true_driver_and_small_for_noise(self):
        x, z = noise(400, 1.0, 1), noise(400, 1.0, 2)
        y = [2.0 * a + e for a, e in zip(x, noise(400, 0.5, 3))]
        d = macro.ols_detail(y, [x, z])
        self.assertAlmostEqual(d["beta"][1], 2.0, delta=0.1)
        self.assertGreater(abs(d["t"][1]), 20)                    # 진짜 요인
        self.assertLess(abs(d["t"][2]), 4)                        # 무관한 요인은 t가 작다
        self.assertAlmostEqual(d["r2"], macro.ols(y, [x, z])[1])  # ols는 같은 계산의 요약
        self.assertTrue(all(s and s > 0 for s in d["se"]))

    def test_inverse_and_singular_matrices(self):
        inv = macro._inverse([[2.0, 0.0], [0.0, 4.0]])
        self.assertAlmostEqual(inv[0][0], 0.5)
        self.assertAlmostEqual(inv[1][1], 0.25)
        self.assertIsNone(macro._inverse([[1.0, 2.0], [2.0, 4.0]]))
        self.assertIsNone(macro.ols_detail([1.0, 2.0, 3.0, 4.0], [[1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0]]))

    def test_every_kept_factor_has_a_significant_t_and_validation_fields(self):
        f = self.factors()
        y = [-0.15 * a + e for a, e in zip(f["^TNX"], noise(self.N, 0.004, 74))]
        fit = macro.fit_factors(dict(zip(dates(self.N), y)), {s: dict(zip(dates(self.N), v)) for s, v in f.items()})
        self.assertEqual(fit["factors"], ["^TNX"])
        self.assertTrue(all(abs(v) >= macro.MIN_T for v in fit["t"]))
        self.assertGreater(fit["oos_r2"], 0.5)
        self.assertEqual(fit["stable"], [True])

    def test_validation_catches_a_relationship_that_existed_only_in_the_past(self):
        # 앞 2/3 동안만 요인을 따라가다가 뒤 1/3은 완전히 무관해진 자산: 표본 안 설명력은 높아도 검증에서는 못 맞힌다
        n, cut = 300, 200
        x = noise(n, 0.05, 81)
        y = [(-0.15 * x[i] if i < cut else 0.0) + e for i, e in enumerate(noise(n, 0.004, 82))]
        oos, stable = macro.validate([x], y)
        self.assertLess(oos, macro.OOS_MIN_R2)
        good_y = [-0.15 * a + e for a, e in zip(x, noise(n, 0.004, 83))]
        good_oos, good_stable = macro.validate([x], good_y)
        self.assertGreater(good_oos, 0.5)
        self.assertEqual(good_stable, [True])

    def test_sign_flip_between_halves_is_reported_as_unstable(self):
        n = 300
        x = noise(n, 0.05, 84)
        y = [(-0.15 if i < n // 2 else 0.15) * x[i] + e for i, e in enumerate(noise(n, 0.004, 85))]
        self.assertEqual(macro.validate([x], y)[1], [False])

    def test_validation_needs_enough_data(self):
        x = noise(80, 0.05, 86)
        oos, stable = macro.validate([x], [-0.1 * a for a in x])
        self.assertIsNone(oos)                                    # 검증 구간이 30개 미만이면 검증하지 않는다
        self.assertEqual(len(stable), 1)

    def test_explain_asset_marks_unstable_relationships_as_weak_with_a_reason(self):
        n, cut = 300, 200
        fs = {"^TNX": series_from_changes(noise(n, 0.05, 91), "level"), "CL=F": series_from_changes(noise(n, 0.02, 92))}
        tnx = macro.daily_changes(fs["^TNX"], "level")
        days = sorted(tnx)
        eps = noise(len(days), 0.004, 93)
        asset = series_from_changes([(-0.15 * tnx[d] if i < cut else 0.0) + e for i, (d, e) in enumerate(zip(days, eps))])
        out = macro.explain_asset(asset, fs)
        self.assertEqual((out["status"], out["reason"]), ("weak", "unstable"))
        self.assertLess(out["oos_r2"], macro.OOS_MIN_R2)
        stable = macro.explain_asset(series_from_changes([-0.15 * tnx[d] + e for d, e in zip(days, eps)]), fs)
        self.assertEqual((stable["status"], stable["reason"]), ("ok", ""))
        self.assertTrue(all(c["stable"] is True and abs(c["t"]) >= macro.MIN_T for c in stable["contributions"]))


class RankDriversTest(unittest.TestCase):
    """평소 가장 민감한 요인 순위: 창별 상관·일관성·같이 움직이는 요인 묶기·사실상 같은 시리즈 제외·짧은 시세."""
    N = 1500
    WINDOWS = (("짧은", 150), ("가운데", 300), ("긴", 900))

    def build(self, n=None, extra=None):
        n = n or self.N
        f = {"DX-Y.NYB": noise(n, 0.01, 11), "CL=F": noise(n, 0.015, 12), "^GSPC": noise(n, 0.01, 13), "^VIX": noise(n, 0.03, 14)}
        f["^IXIC"] = [0.95 * a + e for a, e in zip(f["^GSPC"], noise(n, 0.002, 15))]                    # S&P500과 거의 같이 움직이는 요인
        y = [-0.8 * d + 0.3 * o + 0.2 * s + e for d, o, s, e in zip(f["DX-Y.NYB"], f["CL=F"], f["^GSPC"], noise(n, 0.006, 16))]
        if extra:
            extra(f, y)
        factors = {s: series_from_changes(v, macro.FACTOR_BY_SYMBOL[s][2]) for s, v in f.items()}
        return series_from_changes(y), factors

    def test_strongest_stable_drivers_come_first_with_correct_signs(self):
        asset, factors = self.build()
        out = macro.rank_drivers(asset, factors, windows=self.WINDOWS)
        self.assertEqual(out["status"], "ok")
        labels = [d["label"] for d in out["drivers"]]
        self.assertEqual(labels[0], "달러")                                              # 가장 센 요인
        self.assertLess(out["drivers"][0]["corr"]["짧은"], -0.5)
        self.assertTrue(out["drivers"][0]["consistent"])
        self.assertEqual(len(out["drivers"]), 3)
        self.assertIn("유가(WTI)", labels)

    def test_factors_that_move_together_are_grouped_under_one_representative(self):
        asset, factors = self.build()
        out = macro.rank_drivers(asset, factors, windows=self.WINDOWS)
        names = {d["label"]: d for d in out["peers"]}                                   # S&P500·나스닥은 '함께 움직이는 자산·시장' 계열
        stock = names.get("S&P500") or names.get("나스닥")
        self.assertIsNotNone(stock)
        self.assertEqual(len(stock["absorbed"]), 1)                                       # 나스닥과 S&P500 중 하나만 대표, 나머지는 묶음
        everything = [d["label"] for d in out["peers"] + out["drivers"] + out["others"]]
        self.assertFalse({"S&P500", "나스닥"} <= set(everything))

    def test_macro_factors_and_similar_assets_are_ranked_separately(self):
        asset, factors = self.build()
        out = macro.rank_drivers(asset, factors, windows=self.WINDOWS)
        self.assertTrue(all(macro.factor_group(d["symbol"]) == "macro" for d in out["drivers"] + out["others"]))
        self.assertTrue(all(macro.factor_group(d["symbol"]) == "peer" for d in out["peers"]))
        self.assertNotIn("S&P500", [d["label"] for d in out["drivers"]])                  # 주가지수가 매크로 순위를 가리지 않는다
        self.assertEqual(out["drivers"][0]["label"], "달러")

    def test_group_table_covers_every_factor_and_precious_metals_are_peers(self):
        self.assertEqual(macro.factor_group("GC=F"), "peer")
        self.assertEqual(macro.factor_group("SI=F"), "peer")
        self.assertEqual(macro.factor_group("HG=F"), "macro")                              # 구리는 산업용 원자재 = 매크로
        self.assertEqual(macro.factor_group("^VIX"), "macro")
        self.assertEqual(macro.factor_group("FRED:DFII10"), "macro")
        self.assertTrue(macro.PEER_SYMBOLS <= set(macro.FACTOR_BY_SYMBOL))                 # 오타로 없는 심볼이 섞이지 않게
        groups = {s: macro.factor_group(s) for s in macro.FACTOR_BY_SYMBOL}
        self.assertGreater(list(groups.values()).count("macro"), 20)
        self.assertGreater(list(groups.values()).count("peer"), 15)

    def test_a_series_that_is_practically_the_asset_is_not_a_factor(self):
        def clone(f, y):
            f["^VIX"] = [a + 1e-6 * i % 1e-4 for i, a in enumerate(y)]
        asset, factors = self.build(extra=clone)
        out = macro.rank_drivers(asset, factors, windows=self.WINDOWS)
        self.assertNotIn("VIX", [d["label"] for d in out["drivers"] + out["others"]])

    def test_a_relationship_that_flips_sign_is_marked_inconsistent_and_penalized(self):
        def flip(f, y):
            n = len(y)
            f["^VIX"] = noise(n, 0.03, 21)
            for i in range(n):
                y[i] += (0.4 if i < 1200 else -0.4) * f["^VIX"][i]                         # 오래된 구간에서는 +, 최근 300일은 −
        asset, factors = self.build(extra=flip)
        out = macro.rank_drivers(asset, factors, top=6, windows=self.WINDOWS)
        vix = next(d for d in out["drivers"] + out["others"] if d["label"] == "VIX")
        self.assertFalse(vix["consistent"])
        self.assertAlmostEqual(vix["score"], vix["raw_score"] * macro.INCONSISTENT_PENALTY)

    def test_windows_without_enough_history_are_left_out(self):
        asset, factors = self.build(n=400)
        out = macro.rank_drivers(asset, factors, windows=macro.RANK_WINDOWS)
        corr = out["drivers"][0]["corr"]
        self.assertIsNotNone(corr["1년"])
        self.assertIsNone(corr["10년"])                                                     # 400일로는 10년 상관을 만들지 않는다
        self.assertIsNone(corr["3년"])

    def test_reaction_is_correlation_times_the_assets_monthly_vol(self):
        asset, factors = self.build()
        out = macro.rank_drivers(asset, factors, windows=self.WINDOWS)
        d = out["drivers"][0]
        vol = math.log(1 + out["asset_vol_1m_pct"] / 100)
        self.assertAlmostEqual(math.log(1 + d["reaction_1m_pct"] / 100), d["corr"]["가운데"] * vol, places=6)
        self.assertLess(d["reaction_1m_pct"], 0)                                           # 달러가 오르면 내리는 쪽

    def test_model_summary_and_no_data(self):
        asset, factors = self.build()
        out = macro.rank_drivers(asset, factors, windows=self.WINDOWS)
        self.assertIn("달러", out["model"]["factors"])
        self.assertGreater(out["model"]["r2"], 0.3)
        self.assertEqual(macro.rank_drivers(series_from_changes(noise(50, 0.01, 1)), {}), {"status": "no_data"})
        self.assertEqual(macro.mid_corr({"a": None, "b": 0.3, "c": 0.2}, (("a", 1), ("b", 2), ("c", 3))), 0.3)
        self.assertEqual(macro.mid_corr({"a": None, "b": None, "c": 0.2}, (("a", 1), ("b", 2), ("c", 3))), 0.2)


class CandidateListTest(unittest.TestCase):
    """요인 후보 목록(야후 40개 + FRED 5개)의 구성과, 후보가 많을 때 우연한 요인이 뽑히지 않게 하는 다중 비교 보정."""

    def test_list_is_large_unique_and_well_formed(self):
        symbols = [f[0] for f in macro.ALL_FACTORS]
        self.assertGreaterEqual(len(macro.FACTORS), 40)
        self.assertEqual(len(symbols), len(set(symbols)))
        labels = [f[1] for f in macro.ALL_FACTORS]
        self.assertEqual(len(labels), len(set(labels)))                                    # 화면에 같은 이름이 둘이면 구분이 안 된다
        for symbol, label, kind, up, down in macro.ALL_FACTORS:
            self.assertIn(kind, ("level", "price"), symbol)
            self.assertTrue(label and up and down, symbol)
        for needed in ("3개월 금리", "달러/위안", "유로/달러", "금", "은", "천연가스", "닛케이225", "러셀2000(미국 중소형주)", "기술 업종(XLK)", "금융 업종(XLF)", "에너지 업종(XLE)",
                       "헬스케어 업종(XLV)", "리츠(부동산, VNQ)", "물가연동채(TIP)", "투자등급 회사채(LQD)", "채권 변동성(MOVE)", "이더리움", "신흥국 주식(EEM)", "2년물 금리"):
            self.assertIn(needed, labels)

    def test_required_t_grows_with_the_number_of_candidates(self):
        self.assertAlmostEqual(macro.required_t(1), 2.0)
        values = [macro.required_t(m) for m in (1, 3, 10, 40, 45)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(macro.required_t(40), 3.0)
        self.assertGreater(macro.required_t(3), macro.MIN_T)

    def test_pure_noise_rarely_produces_a_factor_even_with_forty_candidates(self):
        n = 300
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]

        def picked(seed, candidates):
            cands = {f"F{i}": dict(zip(days, noise(n, 0.01, seed * 100 + i))) for i in range(candidates)}
            y = dict(zip(days, noise(n, 0.01, seed * 100 + 99)))
            return len(macro.fit_factors(y, cands)["factors"]) > 0
        corrected = sum(picked(s, 40) for s in range(1, 41))
        self.assertLessEqual(corrected, 4)                                                  # 유의수준 5%라면 40번 중 2번 안팎
        with mock.patch.object(macro, "required_t", return_value=macro.MIN_T):              # 보정이 없다면 우연한 요인이 훨씬 자주 뽑힌다
            uncorrected = sum(picked(s, 40) for s in range(1, 41))
        self.assertGreater(uncorrected, corrected)

    def test_a_real_driver_survives_the_stricter_threshold_among_many_candidates(self):
        n = 300
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]
        cands = {f"F{i}": dict(zip(days, noise(n, 0.01, 5000 + i))) for i in range(40)}
        driver = noise(n, 0.01, 6000)
        cands["REAL"] = dict(zip(days, driver))
        y = dict(zip(days, [0.5 * d + e for d, e in zip(driver, noise(n, 0.01, 6001))]))
        self.assertEqual(macro.fit_factors(y, cands)["factors"], ["REAL"])


if __name__ == "__main__":
    unittest.main()
