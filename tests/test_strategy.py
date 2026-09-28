"""분할매수 전략 테스트: 단계 평가, 래칫, 회복 게이팅(Log 10 버그 회귀 방지), 배정 방식, 배수 규칙."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

from helpers import strategy


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
        self.assertTrue(strategy.eval_step(step, data)["triggered"])
        data["price"] = 11.0
        self.assertFalse(strategy.eval_step(step, data)["triggered"])

    def test_ma_touch_counts_as_triggered(self):
        self.assertTrue(strategy.eval_step(ma_step("s", 2), {"price": 10.0, "weekly_closes": [10.0, 10.0]})["triggered"])

    def test_ma_uses_latest_first_closes(self):
        # closes[0]이 최신 — 앞 3개 평균만 써야 함
        r = strategy.eval_step(ma_step("s", 3), {"price": 10.0, "weekly_closes": [9.0, 9.0, 9.0, 1000.0]})
        self.assertFalse(r["triggered"])

    def test_ma_insufficient_data(self):
        self.assertFalse(strategy.eval_step(ma_step("s", 3), {"price": 10.0, "weekly_closes": [1.0]})["ok"])
        self.assertFalse(strategy.eval_step(ma_step("s", 1), {"price": 0.0, "weekly_closes": [1.0]})["ok"])

    def test_rsi(self):
        step = {"id": "r", "indicator": "RSI", "params": {"period": 3, "threshold": 30}}
        rising = {"price": 13.0, "daily_closes": [13.0, 12.0, 11.0, 10.0]}
        r = strategy.eval_step(step, rising)
        self.assertEqual(r["label"], "RSI(3) 100.0")
        self.assertFalse(r["triggered"])
        falling = {"price": 10.0, "daily_closes": [10.0, 11.0, 12.0, 13.0]}
        self.assertTrue(strategy.eval_step(step, falling)["triggered"])

    def test_change(self):
        step = {"id": "c", "indicator": "CHANGE", "params": {"threshold_pct": -5.0}}
        self.assertTrue(strategy.eval_step(step, {"price": 90.0, "daily_closes": [90.0, 100.0]})["triggered"])
        self.assertFalse(strategy.eval_step(step, {"price": 97.0, "daily_closes": [97.0, 100.0]})["triggered"])


class ComputeStrategyRowsTest(unittest.TestCase):
    """토스 API 호출(lookup_stocks/get_prices/캔들)은 가짜로 바꾼다."""

    def run_rows(self, tpl, price, weekly, state=None, daily=None, symbols=("AAA",), assignments=None, excluded=()):
        state = {} if state is None else state
        with mock.patch.object(strategy, "lookup_stocks", return_value={}), \
                mock.patch.object(strategy, "get_prices", return_value={s: price for s in symbols}), \
                mock.patch.object(strategy, "fetch_close_history", return_value=(daily or [], weekly)):
            rows, changed = strategy.compute_strategy_rows(
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
        clean, err = strategy.validate_strategy_templates(self.base())
        self.assertIsNone(err)
        self.assertEqual(clean["assignments"], {"AAA": "t"})
        self.assertEqual(clean["excluded_symbols"], ["BBB"])

    def test_default_templates_are_valid(self):
        _, err = strategy.validate_strategy_templates(copy.deepcopy(strategy.STRATEGY_DEFAULT_TEMPLATES))
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
                _, err = strategy.validate_strategy_templates(data)
                self.assertIsNotNone(err)
                self.assertIn(frag, err)

    def test_position_pct(self):
        d = self.base()
        d["position_pct"] = {"aaa": "12.5", "ZZZ": 5, "a b": 1}  # 기본 전략 대상일 수 있는 ZZZ는 유지, 잘못된 코드는 버림
        clean, err = strategy.validate_strategy_templates(d)
        self.assertIsNone(err)
        self.assertEqual(clean["position_pct"], {"AAA": 12.5, "ZZZ": 5.0})
        for bad, frag in [({"AAA": 0}, "0보다"), ({"AAA": 101}, "0보다"), ("x", "형식")]:
            with self.subTest(bad=bad):
                d = self.base(); d["position_pct"] = bad
                _, err = strategy.validate_strategy_templates(d)
                self.assertIn(frag, err)
        d = self.base(); d["position_pct"] = {"AAA": 60, "CCC": 50}
        _, err = strategy.validate_strategy_templates(d)
        self.assertIn("합계", err)

    def test_default_template(self):
        d = self.base()
        clean, _ = strategy.validate_strategy_templates(d)
        self.assertEqual(clean["default_template"], "")  # 키 없음 + ma-default 템플릿 없음 → 자동 적용 안 함
        d["default_template"] = "t"
        clean, err = strategy.validate_strategy_templates(d)
        self.assertIsNone(err)
        self.assertEqual(clean["default_template"], "t")
        d["default_template"] = "없음"
        _, err = strategy.validate_strategy_templates(d)
        self.assertIn("기본 전략", err)
        # 예전 파일(키 없음)이라도 ma-default가 있으면 보유종목 전체 자동 적용이 기본
        legacy = copy.deepcopy(strategy.STRATEGY_DEFAULT_TEMPLATES)
        del legacy["default_template"]
        clean, _ = strategy.validate_strategy_templates(legacy)
        self.assertEqual(clean["default_template"], "ma-default")

    def test_position_pct_for_excluded_is_dropped(self):
        d = self.base(); d["position_pct"] = {"BBB": 5, "CCC": 3}  # BBB는 제외 종목, CCC는 미배정 보유종목일 수 있음
        clean, err = strategy.validate_strategy_templates(d)
        self.assertIsNone(err)
        self.assertEqual(clean["position_pct"], {"CCC": 3.0})

    def test_duplicate_step_ids_are_regenerated(self):
        d = self.base()
        d["templates"]["t"]["steps"][1]["id"] = "s1"
        clean, err = strategy.validate_strategy_templates(d)
        self.assertIsNone(err)
        ids = [s["id"] for s in clean["templates"]["t"]["steps"]]
        self.assertEqual(len(set(ids)), 2)


def cycle_template(**kw):
    """real/ 설정값과 같은 모양: 매수 4단계(order) + 회복 1개 + 2차 이후 사이클 표."""
    tpl = {
        "label": "사이클", "assign_mode": "order",
        "steps": [{"id": f"s{i}", "indicator": "MA", "params": {"interval": "week", "period": 10 * i}, "buy_pct": b}
                  for i, b in enumerate([5.0, 10.0, 15.0, 20.0], 1)],
        "recovery_steps": [{"id": "r1", "indicator": "MA", "params": {"interval": "week", "period": 5}}],
        "recovery_pct": 50.0, "multiplier_steps": [], "multiplier_factor": 1.0,
        "cycle_steps": [{"sell_pct": 40.0, "buy_pct": 5.0}, {"sell_pct": 30.0, "buy_pct": 10.0},
                        {"sell_pct": 20.0, "buy_pct": 15.0}, {"sell_pct": 10.0, "buy_pct": 20.0}],
    }
    tpl.update(kw)
    return tpl


class CycleTest(unittest.TestCase):
    """eval_step을 가짜로 바꿔 "어느 선 아래에 있나"만 정해 주고 사이클 진행을 따라간다."""

    def setUp(self):
        p = mock.patch.object(strategy, "eval_step",
                              side_effect=lambda step, md: {"ok": True, "triggered": step["id"] in md["below"], "label": step["id"]})
        p.start()
        self.addCleanup(p.stop)
        self.tpl = cycle_template()
        self.state = dict(strategy.EMPTY_STATE)

    def step(self, *below):
        result, self.state = strategy.evaluate_template(self.tpl, self.state, {"below": set(below), "price": 1.0})
        return result

    def test_full_cycles_follow_real_numbers(self):
        self.assertEqual(self.step("s1", "r1")["entry_pct"], 5.0)
        self.assertEqual(self.step("s1", "s2", "s3", "s4", "r1")["entry_pct"], 50.0)
        r = self.step("s1", "s2", "s3", "s4")                     # 회복선 위로 → 1차 사이클 완성
        self.assertEqual((r["cycle"], r["entry_pct"], r["recovered"]), (1, 100.0, True))
        # 회복 직후 아직 선 아래인 단계는 "새로 뚫은 것"이 아니라 무시
        self.assertEqual(self.step("s1", "s2", "s3", "s4")["cycle"], 1)
        self.step("s2", "s3", "s4")                               # s1 위로 올라감 → 무장
        r = self.step("s1", "s2", "s3", "s4")                     # s1 다시 하방 돌파 → 2차 사이클
        self.assertEqual((r["cycle"], r["entry_pct"], r["stage_count"]), (2, 65.0, 1))
        self.assertEqual(self.state["base_pct"], 100.0)
        path = [self.step("s1", "s2", "r1")["entry_pct"], self.step("s1", "s2", "s3", "r1")["entry_pct"],
                self.step("s1", "s2", "s3", "s4", "r1")["entry_pct"]]
        self.assertEqual(path, [45.0, 40.0, 50.0])
        r = self.step("s1", "s2", "s3", "s4")                     # 2차 회복 → 100%
        self.assertEqual((r["cycle"], r["entry_pct"]), (2, 100.0))
        self.step()                                               # 전부 위로 → 전부 무장
        r = self.step("s3")                                       # 어느 단계든 다시 뚫으면 3차 사이클
        self.assertEqual((r["cycle"], r["entry_pct"]), (3, 65.0))

    def test_recovery_gated_in_later_cycles(self):
        self.state = {**strategy.EMPTY_STATE, "cycle": 2, "base_pct": 100.0, "broken_step_ids": ["s1"]}
        r = self.step("s1")                                       # r1 위지만 4단계를 다 안 뚫음 → 회복 아님
        self.assertFalse(r["recovered"])
        self.assertEqual(r["entry_pct"], 65.0)

    def test_next_move_in_second_cycle(self):
        self.state = {**strategy.EMPTY_STATE, "cycle": 2, "base_pct": 100.0, "broken_step_ids": ["s1"]}
        r = self.step("s1", "r1")
        self.assertEqual((r["next_sell_pct"], r["next_buy_pct"], r["next_change_pct"]), (30.0, 10.0, -20.0))

    def test_without_cycle_table_stays_complete(self):
        self.tpl = cycle_template(cycle_steps=[])
        self.step("s1", "s2", "s3", "s4", "r1")
        self.step("s1", "s2", "s3", "s4")
        self.step()
        r = self.step("s1")
        self.assertEqual((r["cycle"], r["entry_pct"]), (1, 100.0))

    def test_multiplier_resets_on_new_cycle(self):
        self.state = {**strategy.EMPTY_STATE, "broken_step_ids": ["s1", "s2", "s3", "s4"], "recovered": True,
                      "multiplier_triggered": True, "armed_step_ids": ["s1"]}
        self.step("s1")
        self.assertEqual(self.state["cycle"], 2)
        self.assertFalse(self.state["multiplier_triggered"])

    def test_old_state_without_cycle_fields(self):
        r, st = strategy.evaluate_template(self.tpl, {"broken_step_ids": ["s1"], "recovered": False, "multiplier_triggered": False},
                                           {"below": {"s1", "r1"}, "price": 1.0})
        self.assertEqual((r["cycle"], r["entry_pct"], st["base_pct"]), (1, 5.0, 0.0))


class CycleValidationTest(unittest.TestCase):
    def check(self, tpl, frag):
        _, err = strategy.validate_strategy_templates({"templates": {"t": tpl}})
        self.assertIsNotNone(err)
        self.assertIn(frag, err)

    def test_valid_and_default(self):
        clean, err = strategy.validate_strategy_templates({"templates": {"t": cycle_template()}})
        self.assertIsNone(err)
        self.assertEqual(len(clean["templates"]["t"]["cycle_steps"]), 4)
        self.assertEqual(len(strategy.STRATEGY_DEFAULT_TEMPLATES["templates"]["ma-default"]["cycle_steps"]), 4)
        clean, _ = strategy.validate_strategy_templates({"templates": {"t": cycle_template(cycle_steps=None) | {"cycle_steps": []}}})
        self.assertEqual(clean["templates"]["t"]["cycle_steps"], [])

    def test_rejections(self):
        rows = cycle_template()["cycle_steps"]
        self.check(cycle_template(cycle_steps=rows[:3]), "매수 단계 수(4개)")
        self.check(cycle_template(recovery_steps=[], recovery_pct=0.0), "회복 판정 단계가 있어야")
        self.check(cycle_template(cycle_steps=[{"sell_pct": 90.0, "buy_pct": 0.0}, {"sell_pct": 20.0, "buy_pct": 0.0}] + rows[2:]),
                   "2번째에서 비율이 0% 아래")
        self.check(cycle_template(cycle_steps=[{"sell_pct": -1, "buy_pct": 5}] + rows[1:]), "0~100")
        self.check(cycle_template(cycle_steps="x"), "형식")


class EffectiveAssignmentsTest(unittest.TestCase):
    def test_held_get_default_except_excluded_cash_and_explicit(self):
        data = {"assignments": {"AAA": "other", "WATCH": "t"}, "default_template": "t", "excluded_symbols": ["TLT"]}
        assignments, auto = strategy.effective_assignments(data, {"AAA", "BBB", "TLT", "SGOV"}, {"SGOV"})
        self.assertEqual(assignments, {"AAA": "other", "WATCH": "t", "BBB": "t"})
        self.assertEqual(auto, ["BBB"])

    def test_no_default(self):
        data = {"assignments": {"AAA": "t"}, "default_template": "", "excluded_symbols": []}
        assignments, auto = strategy.effective_assignments(data, {"AAA", "BBB"}, set())
        self.assertEqual(assignments, {"AAA": "t"})
        self.assertEqual(auto, [])


class StrategySizingTest(unittest.TestCase):
    def holdings(self):
        return {"totals": {"eval_krw": 10_000_000.0}, "usd_krw": 1000.0,
                "stocks": [{"심볼": "AAA", "통화": "USD", "평가금액(원)": 100_000.0}],
                "cash": {"stocks": [{"심볼": "SGOV", "통화": "USD", "평가금액(원)": 3_000_000.0}]}}

    def row(self, sym="AAA", entry=30.0, nxt=15.0, price=50.0, cur="USD"):
        return {"symbol": sym, "ok": True, "currency": cur, "price": price, "entry_pct": entry, "next_buy_pct": nxt,
                "next_change_pct": nxt}

    def test_buy_now_and_next(self):
        rows = [self.row()]
        strategy.apply_strategy_sizing(rows, {"AAA": 10.0}, self.holdings())
        z = rows[0]["sizing"]
        self.assertAlmostEqual(z["full_krw"], 1_000_000)        # 1천만 × 10%
        self.assertAlmostEqual(z["target_now_pct"], 3.0)        # 10% × 30%
        self.assertAlmostEqual(z["current_pct"], 1.0)
        self.assertAlmostEqual(z["buy_now_krw"], 200_000)       # 30만 − 10만
        self.assertEqual(z["buy_now_shares"], 4)                # $50 × 1000원 = 5만원/주
        self.assertAlmostEqual(z["next_change_krw"], 150_000)   # 100만 × 15%p
        self.assertEqual(z["next_change_shares"], 3)
        self.assertAlmostEqual(z["next_change_pct_of_total"], 1.5)

    def test_sell_side(self):
        # 2차 사이클: 다음 단계가 순매도(-35%p), 지금은 목표보다 많이 들고 있음
        rows = [self.row(entry=5.0, nxt=-35.0)]
        strategy.apply_strategy_sizing(rows, {"AAA": 10.0}, self.holdings())
        z = rows[0]["sizing"]
        self.assertAlmostEqual(z["over_krw"], 50_000)
        self.assertEqual(z["over_shares"], 1)
        self.assertAlmostEqual(z["next_change_krw"], -350_000)
        self.assertEqual(z["next_change_shares"], 7)

    def test_over_target_and_unheld_krw(self):
        rows = [self.row(entry=5.0), self.row(sym="005930", entry=20.0, price=70_000.0, cur="KRW"), self.row(sym="NOPE")]
        strategy.apply_strategy_sizing(rows, {"AAA": 10.0, "005930": 5.0}, self.holdings())
        a = rows[0]["sizing"]
        self.assertEqual(a["buy_now_krw"], 0.0)
        self.assertAlmostEqual(a["over_krw"], 50_000)           # 목표 5만, 보유 10만
        k = rows[1]["sizing"]
        self.assertAlmostEqual(k["buy_now_krw"], 100_000)       # 50만 × 20%, 미보유
        self.assertEqual(k["buy_now_shares"], 1)
        self.assertNotIn("sizing", rows[2])                     # 목표 비중 없음

    def test_skips_failed_rows(self):
        rows = [{"symbol": "AAA", "ok": False}]
        strategy.apply_strategy_sizing(rows, {"AAA": 10.0}, self.holdings())
        self.assertNotIn("sizing", rows[0])


if __name__ == "__main__":
    unittest.main()
