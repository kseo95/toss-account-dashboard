"""분할매수 전략 테스트: 단계 평가, 래칫, 회복 게이팅(Log 10 버그 회귀 방지), 배정 방식, 배수 규칙."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

from helpers import server


def ma_step(sid, period, buy_pct=None):
    s = {"id": sid, "indicator": "MA", "params": {"interval": "week", "period": period}}
    if buy_pct is not None:
        s["buy_pct"] = buy_pct
    return s


def template(assign_mode="fixed", recovery_pct=30.0, multiplier_steps=None, multiplier_factor=1.0):
    return {
        "label": "테스트",
        "assign_mode": assign_mode,
        "steps": [ma_step("s1", 3, 10.0), ma_step("s2", 5, 20.0)],
        "recovery_steps": [ma_step("r1", 2)],
        "recovery_pct": recovery_pct,
        "multiplier_steps": multiplier_steps or [],
        "multiplier_factor": multiplier_factor,
    }


class EvalStepTest(unittest.TestCase):
    def test_ma_triggered_below_and_not_above(self):
        step = ma_step("s", 3)
        data = {"price": 9.0, "weekly_closes": [10.0, 10.0, 10.0]}
        self.assertTrue(server.eval_step(step, data)["triggered"])
        data["price"] = 11.0
        self.assertFalse(server.eval_step(step, data)["triggered"])

    def test_ma_touch_counts_as_triggered(self):
        self.assertTrue(server.eval_step(ma_step("s", 2), {"price": 10.0, "weekly_closes": [10.0, 10.0]})["triggered"])

    def test_ma_uses_latest_first_closes(self):
        # closes[0]이 최신 — 앞 3개 평균만 써야 함
        r = server.eval_step(ma_step("s", 3), {"price": 10.0, "weekly_closes": [9.0, 9.0, 9.0, 1000.0]})
        self.assertFalse(r["triggered"])

    def test_ma_insufficient_data(self):
        self.assertFalse(server.eval_step(ma_step("s", 3), {"price": 10.0, "weekly_closes": [1.0]})["ok"])
        self.assertFalse(server.eval_step(ma_step("s", 1), {"price": 0.0, "weekly_closes": [1.0]})["ok"])

    def test_rsi(self):
        step = {"id": "r", "indicator": "RSI", "params": {"period": 3, "threshold": 30}}
        rising = {"price": 13.0, "daily_closes": [13.0, 12.0, 11.0, 10.0]}
        r = server.eval_step(step, rising)
        self.assertEqual(r["label"], "RSI(3) 100.0")
        self.assertFalse(r["triggered"])
        falling = {"price": 10.0, "daily_closes": [10.0, 11.0, 12.0, 13.0]}
        self.assertTrue(server.eval_step(step, falling)["triggered"])

    def test_change(self):
        step = {"id": "c", "indicator": "CHANGE", "params": {"threshold_pct": -5.0}}
        self.assertTrue(server.eval_step(step, {"price": 90.0, "daily_closes": [90.0, 100.0]})["triggered"])
        self.assertFalse(server.eval_step(step, {"price": 97.0, "daily_closes": [97.0, 100.0]})["triggered"])


class ComputeStrategyRowsTest(unittest.TestCase):
    """토스 API 호출(lookup_stocks/get_prices/캔들)은 가짜로 바꾼다."""

    def run_rows(self, tpl, price, weekly, state=None, daily=None, symbols=("AAA",), assignments=None, excluded=()):
        state = {} if state is None else state
        with mock.patch.object(server, "lookup_stocks", return_value={}), \
                mock.patch.object(server, "get_prices", return_value={s: price for s in symbols}), \
                mock.patch.object(server, "fetch_weekly_closes", return_value=weekly), \
                mock.patch.object(server, "fetch_daily_closes", return_value=daily or []):
            rows, changed = server.compute_strategy_rows(
                "token", {"t": tpl}, assignments or {s: "t" for s in symbols}, set(excluded), state, list(symbols))
        return rows, changed, state

    def test_all_steps_broken_then_recovery(self):
        # MA2=10, MA3≈13.3, MA5=16 → 가격 11은 MA3/MA5 아래(2단계 모두 돌파), MA2 위(회복)
        rows, changed, state = self.run_rows(template(), 11.0, [10.0, 10.0, 20.0, 20.0, 20.0])
        r = rows[0]
        self.assertEqual(r["stage_count"], 2)
        self.assertTrue(r["recovered"])
        self.assertAlmostEqual(r["entry_pct"], 10 + 20 + 30)
        self.assertTrue(changed)
        self.assertEqual(state["AAA"]["broken_step_ids"], ["s1", "s2"])

    def test_recovery_gated_until_all_steps_broken(self):
        # Log 10 버그 회귀 방지: 1단계만 돌파(MA5 아래, MA3 위)인데 MA2 위에 있어도 회복이 뜨면 안 됨
        rows, _, state = self.run_rows(template(), 12.0, [10.0, 10.0, 10.0, 30.0, 30.0])
        r = rows[0]
        self.assertEqual(r["stage_count"], 1)
        self.assertFalse(r["recovered"])
        self.assertEqual(r["recovery_steps"], [])
        self.assertAlmostEqual(r["entry_pct"], 20.0)  # fixed: s2의 20%p만
        self.assertFalse(state["AAA"]["recovered"])

    def test_ratchet_keeps_broken_steps_after_price_recovers(self):
        state = {"AAA": {"broken_step_ids": ["s1"], "recovered": False, "multiplier_triggered": False}}
        rows, changed, state = self.run_rows(template(), 100.0, [10.0] * 5, state=state)
        self.assertEqual(rows[0]["stage_count"], 1)
        self.assertAlmostEqual(rows[0]["entry_pct"], 10.0)
        self.assertFalse(changed)

    def test_recovered_state_is_sticky(self):
        state = {"AAA": {"broken_step_ids": ["s1", "s2"], "recovered": True, "multiplier_triggered": False}}
        rows, changed, _ = self.run_rows(template(), 1.0, [10.0] * 5, state=state)
        self.assertTrue(rows[0]["recovered"])
        self.assertAlmostEqual(rows[0]["entry_pct"], 60.0)
        self.assertFalse(changed)

    def test_order_mode_fills_from_first_step(self):
        # s2만 돌파돼도 order 방식은 "1개 돌파" = 첫 단계 비율(10%p)
        rows, _, _ = self.run_rows(template(assign_mode="order"), 12.0, [10.0, 10.0, 10.0, 30.0, 30.0])
        self.assertAlmostEqual(rows[0]["entry_pct"], 10.0)

    def test_multiplier_triggers_without_gating_and_multiplies_steps(self):
        mult = [{"id": "m1", "indicator": "CHANGE", "params": {"threshold_pct": -5.0}}]
        tpl = template(multiplier_steps=mult, multiplier_factor=2.0)
        rows, _, state = self.run_rows(tpl, 12.0, [10.0, 10.0, 10.0, 30.0, 30.0], daily=[12.0, 20.0])
        r = rows[0]
        self.assertTrue(r["multiplier_triggered"])
        self.assertAlmostEqual(r["entry_pct"], 20.0 * 2)
        self.assertTrue(state["AAA"]["multiplier_triggered"])

    def test_entry_capped_at_100(self):
        tpl = template(recovery_pct=70.0, multiplier_steps=[{"id": "m1", "indicator": "CHANGE", "params": {"threshold_pct": 0}}],
                       multiplier_factor=3.0)
        rows, _, _ = self.run_rows(tpl, 11.0, [10.0, 10.0, 20.0, 20.0, 20.0], daily=[11.0, 20.0])
        self.assertEqual(rows[0]["entry_pct"], 100.0)

    def test_unassigned_and_excluded_are_skipped(self):
        rows, changed, _ = self.run_rows(template(), 11.0, [10.0] * 5, symbols=("AAA", "BBB", "CCC"),
                                         assignments={"AAA": "t", "BBB": "t"}, excluded=("BBB",))
        self.assertEqual([r["symbol"] for r in rows], ["AAA"])

    def test_next_buy_pct(self):
        # fixed: s2(20%p)만 깨짐 → 다음은 안 깨진 s1의 10%p
        rows, _, _ = self.run_rows(template(), 12.0, [10.0, 10.0, 10.0, 30.0, 30.0])
        self.assertAlmostEqual(rows[0]["next_buy_pct"], 10.0)
        # order: 1개 깨짐 → 다음은 두 번째 단계 20%p
        rows, _, _ = self.run_rows(template(assign_mode="order"), 12.0, [10.0, 10.0, 10.0, 30.0, 30.0])
        self.assertAlmostEqual(rows[0]["next_buy_pct"], 20.0)
        # 전 단계 돌파, 회복 전 → 다음은 회복 매수분
        state = {"AAA": {"broken_step_ids": ["s1", "s2"], "recovered": False, "multiplier_triggered": False}}
        rows, _, _ = self.run_rows(template(), 1.0, [10.0] * 5, state=state)
        self.assertAlmostEqual(rows[0]["next_buy_pct"], 30.0)
        # 회복까지 끝 → 0
        state = {"AAA": {"broken_step_ids": ["s1", "s2"], "recovered": True, "multiplier_triggered": False}}
        rows, _, _ = self.run_rows(template(), 1.0, [10.0] * 5, state=state)
        self.assertEqual(rows[0]["next_buy_pct"], 0.0)

    def test_next_buy_pct_with_multiplier_is_capped(self):
        mult = [{"id": "m1", "indicator": "CHANGE", "params": {"threshold_pct": -5.0}}]
        tpl = template(multiplier_steps=mult, multiplier_factor=2.0)
        rows, _, _ = self.run_rows(tpl, 12.0, [10.0, 10.0, 10.0, 30.0, 30.0], daily=[12.0, 20.0])
        self.assertAlmostEqual(rows[0]["next_buy_pct"], 20.0)  # s1 10%p × 2
        tpl = template(multiplier_steps=mult, multiplier_factor=5.0)
        rows, _, _ = self.run_rows(tpl, 12.0, [10.0, 10.0, 10.0, 30.0, 30.0], daily=[12.0, 20.0])
        self.assertAlmostEqual(rows[0]["entry_pct"], 100.0)
        self.assertEqual(rows[0]["next_buy_pct"], 0.0)  # 이미 100%라 더 없음

    def test_missing_price(self):
        rows, changed, _ = self.run_rows(template(), 0.0, [10.0] * 5)
        self.assertFalse(rows[0]["ok"])
        self.assertFalse(changed)


class ValidateTemplatesTest(unittest.TestCase):
    def base(self):
        return {"templates": {"t": template()}, "assignments": {"aaa": "t"}, "excluded_symbols": [" bbb ", "BBB"]}

    def test_valid_and_normalized(self):
        clean, err = server.validate_strategy_templates(self.base())
        self.assertIsNone(err)
        self.assertEqual(clean["assignments"], {"AAA": "t"})
        self.assertEqual(clean["excluded_symbols"], ["BBB"])

    def test_default_templates_are_valid(self):
        _, err = server.validate_strategy_templates(copy.deepcopy(server.STRATEGY_DEFAULT_TEMPLATES))
        self.assertIsNone(err)

    def test_rejections(self):
        cases = []
        d = self.base(); d["templates"]["t"]["recovery_pct"] = 80.0; cases.append((d, "100%를 넘습니다"))
        d = self.base(); d["templates"]["t"]["recovery_pct"] = 0.0; cases.append((d, "회복 매수 비율도"))
        d = self.base(); d["assignments"] = {"AAA": "없음"}; cases.append((d, "존재하지 않습니다"))
        d = self.base(); d["templates"]["t"]["steps"] = []; cases.append((d, "최소 1개"))
        d = self.base(); d["templates"]["t"]["steps"][0]["buy_pct"] = 0; cases.append((d, "매수 비율"))
        d = self.base(); d["templates"]["t"]["steps"][0]["params"]["period"] = 301; cases.append((d, "MA 기간"))
        d = self.base(); d["templates"]["t"]["steps"][0]["indicator"] = "MACD"; cases.append((d, "지원하지 않는 지표"))
        d = self.base(); d["templates"]["t"]["multiplier_steps"] = [ma_step("m", 3)]; cases.append((d, "배수는 1보다"))
        d = self.base(); d["templates"]["t"]["assign_mode"] = "random"; cases.append((d, "배정 방식"))
        d = self.base(); d["templates"] = {}; cases.append((d, "최소 1개"))
        for data, frag in cases:
            with self.subTest(frag=frag):
                _, err = server.validate_strategy_templates(data)
                self.assertIsNotNone(err)
                self.assertIn(frag, err)

    def test_position_pct(self):
        d = self.base()
        d["position_pct"] = {"aaa": "12.5", "ZZZ": 5, "a b": 1}  # 기본 전략 대상일 수 있는 ZZZ는 유지, 잘못된 코드는 버림
        clean, err = server.validate_strategy_templates(d)
        self.assertIsNone(err)
        self.assertEqual(clean["position_pct"], {"AAA": 12.5, "ZZZ": 5.0})
        for bad, frag in [({"AAA": 0}, "0보다"), ({"AAA": 101}, "0보다"), ("x", "형식")]:
            with self.subTest(bad=bad):
                d = self.base(); d["position_pct"] = bad
                _, err = server.validate_strategy_templates(d)
                self.assertIn(frag, err)
        d = self.base(); d["position_pct"] = {"AAA": 60, "CCC": 50}
        _, err = server.validate_strategy_templates(d)
        self.assertIn("합계", err)

    def test_default_template(self):
        d = self.base()
        clean, _ = server.validate_strategy_templates(d)
        self.assertEqual(clean["default_template"], "")  # 키 없음 + ma-default 템플릿 없음 → 자동 적용 안 함
        d["default_template"] = "t"
        clean, err = server.validate_strategy_templates(d)
        self.assertIsNone(err)
        self.assertEqual(clean["default_template"], "t")
        d["default_template"] = "없음"
        _, err = server.validate_strategy_templates(d)
        self.assertIn("기본 전략", err)
        # 예전 파일(키 없음)이라도 ma-default가 있으면 보유종목 전체 자동 적용이 기본
        legacy = copy.deepcopy(server.STRATEGY_DEFAULT_TEMPLATES)
        del legacy["default_template"]
        clean, _ = server.validate_strategy_templates(legacy)
        self.assertEqual(clean["default_template"], "ma-default")

    def test_position_pct_for_excluded_is_dropped(self):
        d = self.base(); d["position_pct"] = {"BBB": 5, "CCC": 3}  # BBB는 제외 종목, CCC는 미배정 보유종목일 수 있음
        clean, err = server.validate_strategy_templates(d)
        self.assertIsNone(err)
        self.assertEqual(clean["position_pct"], {"CCC": 3.0})

    def test_duplicate_step_ids_are_regenerated(self):
        d = self.base()
        d["templates"]["t"]["steps"][1]["id"] = "s1"
        clean, err = server.validate_strategy_templates(d)
        self.assertIsNone(err)
        ids = [s["id"] for s in clean["templates"]["t"]["steps"]]
        self.assertEqual(len(set(ids)), 2)


class EffectiveAssignmentsTest(unittest.TestCase):
    def test_held_get_default_except_excluded_cash_and_explicit(self):
        data = {"assignments": {"AAA": "other", "WATCH": "t"}, "default_template": "t", "excluded_symbols": ["TLT"]}
        assignments, auto = server.effective_assignments(data, {"AAA", "BBB", "TLT", "SGOV"}, {"SGOV"})
        self.assertEqual(assignments, {"AAA": "other", "WATCH": "t", "BBB": "t"})
        self.assertEqual(auto, ["BBB"])

    def test_no_default(self):
        data = {"assignments": {"AAA": "t"}, "default_template": "", "excluded_symbols": []}
        assignments, auto = server.effective_assignments(data, {"AAA", "BBB"}, set())
        self.assertEqual(assignments, {"AAA": "t"})
        self.assertEqual(auto, [])


class StrategySizingTest(unittest.TestCase):
    def holdings(self):
        return {"totals": {"eval_krw": 10_000_000.0}, "usd_krw": 1000.0,
                "stocks": [{"심볼": "AAA", "통화": "USD", "평가금액(원)": 100_000.0}],
                "cash": {"stocks": [{"심볼": "SGOV", "통화": "USD", "평가금액(원)": 3_000_000.0}]}}

    def row(self, sym="AAA", entry=30.0, nxt=15.0, price=50.0, cur="USD"):
        return {"symbol": sym, "ok": True, "currency": cur, "price": price, "entry_pct": entry, "next_buy_pct": nxt}

    def test_buy_now_and_next(self):
        rows = [self.row()]
        server.apply_strategy_sizing(rows, {"AAA": 10.0}, self.holdings())
        z = rows[0]["sizing"]
        self.assertAlmostEqual(z["full_krw"], 1_000_000)        # 1천만 × 10%
        self.assertAlmostEqual(z["target_now_pct"], 3.0)        # 10% × 30%
        self.assertAlmostEqual(z["current_pct"], 1.0)
        self.assertAlmostEqual(z["buy_now_krw"], 200_000)       # 30만 − 10만
        self.assertEqual(z["buy_now_shares"], 4)                # $50 × 1000원 = 5만원/주
        self.assertAlmostEqual(z["next_buy_krw"], 150_000)      # 100만 × 15%p
        self.assertEqual(z["next_buy_shares"], 3)
        self.assertAlmostEqual(z["next_buy_pct_of_total"], 1.5)

    def test_over_target_and_unheld_krw(self):
        rows = [self.row(entry=5.0), self.row(sym="005930", entry=20.0, price=70_000.0, cur="KRW"), self.row(sym="NOPE")]
        server.apply_strategy_sizing(rows, {"AAA": 10.0, "005930": 5.0}, self.holdings())
        a = rows[0]["sizing"]
        self.assertEqual(a["buy_now_krw"], 0.0)
        self.assertAlmostEqual(a["over_krw"], 50_000)           # 목표 5만, 보유 10만
        k = rows[1]["sizing"]
        self.assertAlmostEqual(k["buy_now_krw"], 100_000)       # 50만 × 20%, 미보유
        self.assertEqual(k["buy_now_shares"], 1)
        self.assertNotIn("sizing", rows[2])                     # 목표 비중 없음

    def test_skips_failed_rows(self):
        rows = [{"symbol": "AAA", "ok": False}]
        server.apply_strategy_sizing(rows, {"AAA": 10.0}, self.holdings())
        self.assertNotIn("sizing", rows[0])


if __name__ == "__main__":
    unittest.main()
