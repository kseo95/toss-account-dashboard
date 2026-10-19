"""리밸런싱·통화 비중·설정 검증 테스트."""
from __future__ import annotations

import unittest
import unittest.mock

from helpers import common, portfolio, watch


def stock(sym, name, cur, ev):
    return {"심볼": sym, "종목": name, "통화": cur, "평가금액(원)": ev}


class ComputeRebalanceTest(unittest.TestCase):
    def setUp(self):
        self.rows = [stock("AAA", "에이", "USD", 400.0), stock("BBB", "비", "USD", 200.0),
                     stock("CCC", "씨", "KRW", 100.0), stock("SGOV", "SGOV", "USD", 200.0)]
        self.config = {"targets": [{"label": "묶음", "symbols": ["AAA", "BBB"], "target_pct": 50.0}],
                       "default_rest_target_pct": 5.0, "krw_target_pct": 30.0}

    def test_categories_rest_and_cash_merge(self):
        result = {r["label"]: r for r in portfolio.compute_rebalance(self.rows, 100.0, {"SGOV"}, 1000.0, self.config)}
        self.assertAlmostEqual(result["묶음"]["current_pct"], 60.0)
        self.assertEqual(result["묶음"]["target_pct"], 50.0)
        self.assertAlmostEqual(result["씨"]["current_pct"], 10.0)
        self.assertEqual(result["씨"]["target_pct"], 5.0)
        # 현금 취급 종목(SGOV 20%) + 예수금(10%) → "현금" 하나로 합쳐짐
        self.assertAlmostEqual(result["현금"]["current_pct"], 30.0)
        self.assertEqual(len(result), 3)

    def test_cash_target_turns_cash_into_a_calculated_category(self):
        config = {**self.config, "cash_target_pct": 20.0}
        result = {r["label"]: r for r in portfolio.compute_rebalance(self.rows, 100.0, {"SGOV"}, 1000.0, config)}
        self.assertEqual((result["현금"]["category"], result["현금"]["target_pct"]), (True, 20.0))
        self.assertAlmostEqual(result["현금"]["current_pct"], 30.0)
        plan = portfolio.compute_rebalance_plan(list(result.values()), 1000.0)
        cash = next(r for r in plan["rows"] if r["label"] == "현금")
        self.assertEqual((cash["action"], round(cash["difference_krw"])), ("sell", -100))   # 현금 30% → 목표 20%
        self.assertEqual(plan["excluded"]["cash_pct"], 0)

    def test_cash_target_row_exists_even_without_cash(self):
        config = {**self.config, "cash_target_pct": 10.0}
        result = {r["label"]: r for r in portfolio.compute_rebalance(self.rows[:3], 0.0, set(), 700.0, config)}
        self.assertEqual((result["현금"]["current_pct"], result["현금"]["category"]), (0.0, True))

    def test_without_cash_target_cash_stays_excluded(self):
        result = {r["label"]: r for r in portfolio.compute_rebalance(self.rows, 100.0, {"SGOV"}, 1000.0, {**self.config, "cash_target_pct": None})}
        self.assertFalse(result["현금"]["category"])

    def test_zero_total_does_not_divide_by_zero(self):
        for r in portfolio.compute_rebalance(self.rows, 0.0, set(), 0.0, self.config):
            self.assertEqual(r["current_pct"], 0.0)

    def test_plan_balances_when_targets_sum_to_100(self):
        plan = portfolio.compute_rebalance_plan([
            {"label": "주식", "current_pct": 70, "target_pct": 60},
            {"label": "현금", "current_pct": 30, "target_pct": 40},
        ], 1_000_000)
        self.assertTrue(plan["ready"])
        self.assertAlmostEqual(plan["sell_total_krw"], 100_000)
        self.assertAlmostEqual(plan["buy_total_krw"], 100_000)
        rows = {r["label"]: r for r in plan["rows"]}
        self.assertEqual(rows["주식"]["action"], "sell")
        self.assertEqual(rows["현금"]["action"], "buy")

    def test_plan_warns_when_targets_do_not_sum_to_100(self):
        plan = portfolio.compute_rebalance_plan([
            {"label": "주식", "current_pct": 70, "target_pct": 50},
            {"label": "현금", "current_pct": 30, "target_pct": 20},
        ], 1_000_000)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["target_total_pct"], 70)
        self.assertTrue(any("100%" in x for x in plan["warnings"]))


class RebalancePlanScopeTest(unittest.TestCase):
    """목표를 설정한 카테고리만 계산한다 — 안 묶인 보유종목과 현금은 계산에서 빠지고 현재 규모만 알려준다."""

    def items(self):
        return [
            {"label": "채권", "current_pct": 10, "target_pct": 40, "category": True},
            {"label": "주식", "current_pct": 50, "target_pct": 60, "category": True},
            {"label": "정리할종목", "current_pct": 15, "target_pct": 5, "category": False},
            {"label": "다른종목", "current_pct": 5, "target_pct": 5, "category": False},
            {"label": "현금", "current_pct": 20, "target_pct": 5, "category": False},
        ]

    def test_compute_rebalance_marks_categories_and_leftovers(self):
        rows = [stock("AAA", "에이", "USD", 400.0), stock("CCC", "씨", "KRW", 100.0), stock("SGOV", "SGOV", "USD", 200.0)]
        config = {"targets": [{"label": "묶음", "symbols": ["AAA"], "target_pct": 100.0}],
                  "default_rest_target_pct": 5.0, "krw_target_pct": 30.0}
        result = {r["label"]: r for r in portfolio.compute_rebalance(rows, 100.0, {"SGOV"}, 1000.0, config)}
        self.assertTrue(result["묶음"]["category"])
        self.assertFalse(result["씨"]["category"])
        self.assertFalse(result["현금"]["category"])

    def test_category_targets_at_100_are_ready_even_with_leftovers(self):
        # 설정 패널의 카테고리 합계가 100%면, 미분류 종목에 기본 목표%가 있어도 계획은 확정된다.
        plan = portfolio.compute_rebalance_plan(self.items(), 1_000_000)
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["target_total_pct"], 100)
        self.assertEqual([r["label"] for r in plan["rows"]], ["채권", "주식"])  # 안 묶인 항목은 행에 없다
        rows = {r["label"]: r for r in plan["rows"]}
        self.assertAlmostEqual(rows["채권"]["difference_krw"], 300_000)
        self.assertAlmostEqual(rows["주식"]["difference_krw"], 100_000)
        self.assertAlmostEqual(plan["buy_total_krw"], 400_000)
        self.assertAlmostEqual(plan["sell_total_krw"], 0)
        # 자금 차이 = 제외 항목(정리할 종목 + 현금 40%)로 채워야 하는 금액
        self.assertAlmostEqual(plan["funding_gap_krw"], 400_000)

    def test_excluded_summary_and_message(self):
        plan = portfolio.compute_rebalance_plan(self.items(), 1_000_000)
        ex = plan["excluded"]
        self.assertEqual(ex["holdings_count"], 2)
        self.assertAlmostEqual(ex["holdings_pct"], 20)
        self.assertAlmostEqual(ex["cash_pct"], 20)
        self.assertAlmostEqual(ex["holdings_krw"], 200_000)
        note = next(w for w in plan["warnings"] if "제외" in w)
        self.assertIn("보유종목 2개(현재 20.0%)", note)
        self.assertIn("현금(현재 20.0%)은 계산에서 제외", note)
        self.assertFalse(any("합계가" in w for w in plan["warnings"]), "합계가 100이면 합계 경고가 없다")

    def test_not_ready_message_uses_category_sum_only(self):
        items = self.items()
        items[0]["target_pct"] = 30  # 카테고리 합계 90
        plan = portfolio.compute_rebalance_plan(items, 1_000_000)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["target_total_pct"], 90)
        self.assertTrue(any("카테고리 목표 비중 합계가 90%" in w for w in plan["warnings"]))

    def test_no_categories_is_not_ready_and_says_so(self):
        plan = portfolio.compute_rebalance_plan([i for i in self.items() if not i["category"]], 1_000_000)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["rows"], [])
        self.assertTrue(any("목표를 설정한 카테고리가 없어" in w for w in plan["warnings"]))
        self.assertEqual(plan["excluded"]["holdings_count"], 2)

    def test_only_cash_excluded_uses_correct_wording(self):
        items = [i for i in self.items() if i["label"] in ("채권", "주식", "현금")]
        note = next(w for w in portfolio.compute_rebalance_plan(items, 1_000_000)["warnings"] if "제외" in w)
        self.assertTrue(note.startswith("현금(현재 20.0%)은 계산에서 제외"), note)

    def test_nothing_excluded_has_no_exclusion_note(self):
        items = [i for i in self.items() if i["category"]]
        plan = portfolio.compute_rebalance_plan(items, 1_000_000)
        self.assertFalse(any("제외" in w for w in plan["warnings"]))
        self.assertEqual(plan["excluded"]["holdings_count"], 0)

    def test_build_holdings_plan_ignores_unclassified_default_target(self):
        import tempfile
        from pathlib import Path
        from helpers import storage
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.UserStore(Path(tmp))
            store.write_json("rebalance.json", {"targets": [{"label": "주식", "symbols": ["AAA"], "target_pct": 100.0}],
                                                "default_rest_target_pct": 5.0, "krw_target_pct": 50.0})
            snapshot = {"usd_krw": 1000.0, "cash_krw": 100.0, "cash_usd": 0.0, "items": [
                {"symbol": "AAA", "name": "에이", "currency": "KRW", "quantity": 1, "marketValue": {"amount": 600}},
                {"symbol": "ZZZ", "name": "정리할것", "currency": "KRW", "quantity": 1, "marketValue": {"amount": 300}},
            ]}
            with unittest.mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
                data = portfolio.build_holdings(snapshot, store)
        plan = data["rebalance_plan"]
        self.assertTrue(plan["ready"], plan["warnings"])
        self.assertEqual([r["label"] for r in plan["rows"]], ["주식"])
        self.assertAlmostEqual(plan["rows"][0]["target_krw"], 1000.0)  # 총자산(600+300+현금 100) 기준
        self.assertEqual(plan["excluded"]["holdings_count"], 1)
        self.assertAlmostEqual(plan["excluded"]["cash_pct"], 10.0)


def held(sym, name, cur, value, qty):
    return {"심볼": sym, "종목": name, "통화": cur, "평가금액(원)": value, "수량": qty}


class TradeSharesTest(unittest.TestCase):
    """카테고리 금액 → 종목별 금액·주수. 보유 종목이 있으면 보유 비율, 없으면 첫 번째 종목."""

    def setUp(self):
        self.categories = [{"label": "우주", "symbols": ["UFO", "NASA"]}, {"label": "S&P", "symbols": ["SPYM", "SH", "QNDX"]},
                           {"label": "코스피", "symbols": ["069500", "114800"]}, {"label": "금", "symbols": ["GLDM"]}]
        self.held = [held("UFO", "UFO", "USD", 100_000.0, 4.0),     # 단가 25,000원
                     held("NASA", "NASA", "USD", 300_000.0, 10.0),  # 단가 30,000원
                     held("GLDM", "GLDM", "USD", 50_000.0, 1.0)]

    def plan(self, **diffs):
        return [{"label": k, "difference_krw": v, "action": "buy" if v > 0 else "sell" if v < 0 else "hold"}
                for k, v in diffs.items()]

    def test_sell_is_split_by_holding_value_and_floored_to_two_decimals(self):
        out = portfolio.allocate_trade_shares(self.plan(우주=-100_000.0), self.categories, self.held, {})
        parts = {p["symbol"]: p for p in out["allocations"]["우주"]}
        self.assertAlmostEqual(parts["UFO"]["amount_krw"], 25_000.0)   # 100k:300k = 1:3
        self.assertAlmostEqual(parts["NASA"]["amount_krw"], 75_000.0)
        self.assertEqual(parts["UFO"]["shares"], 1.0)                   # 25,000 / 25,000
        self.assertEqual(parts["NASA"]["shares"], 2.5)                  # 75,000 / 30,000
        self.assertEqual(out["missing"], [])

    def test_us_shares_floor_not_round(self):
        out = portfolio.allocate_trade_shares(self.plan(금=10_000.0), self.categories, self.held, {})
        # 10,000원 / 50,000원 = 0.2주
        self.assertEqual(out["allocations"]["금"][0]["shares"], 0.2)
        out = portfolio.allocate_trade_shares(self.plan(금=33_333.0), self.categories, self.held, {})
        self.assertEqual(out["allocations"]["금"][0]["shares"], 0.66)   # 0.66666… → 내림

    def test_buy_without_holdings_goes_to_first_symbol_with_quote(self):
        quotes = {"SPYM": {"name": "SPDR 포트폴리오 S&P 500", "currency": "USD", "price_krw": 90_000.0},
                  "069500": {"name": "KODEX 200", "currency": "KRW", "price_krw": 88_750.0}}
        out = portfolio.allocate_trade_shares(self.plan(**{"S&P": 555_476.0, "코스피": 277_738.0}), self.categories, self.held, quotes)
        sp = out["allocations"]["S&P"]
        self.assertEqual([p["symbol"] for p in sp], ["SPYM"])           # SH(인버스)·QNDX가 아니라 첫 종목
        self.assertEqual(sp[0]["shares"], 6.17)                         # 555,476 / 90,000 = 6.1719…
        kospi = out["allocations"]["코스피"]
        self.assertEqual(kospi[0]["symbol"], "069500")
        self.assertEqual(kospi[0]["shares"], 3.0)                       # 국내는 정수 주(내림): 277,738 / 88,750 = 3.12

    def test_buy_with_holdings_follows_holding_ratio(self):
        out = portfolio.allocate_trade_shares(self.plan(우주=400_000.0), self.categories, self.held, {})
        self.assertEqual({p["symbol"]: round(p["amount_krw"]) for p in out["allocations"]["우주"]}, {"UFO": 100_000, "NASA": 300_000})

    def test_missing_quote_is_reported_not_guessed(self):
        out = portfolio.allocate_trade_shares(self.plan(**{"S&P": 100_000.0}), self.categories, self.held, {})
        self.assertIsNone(out["allocations"]["S&P"][0]["shares"])
        self.assertEqual(out["missing"], ["SPYM"])

    def test_hold_rows_and_sell_without_holdings_get_nothing(self):
        out = portfolio.allocate_trade_shares(self.plan(금=0.0, 코스피=-1000.0), self.categories, self.held, {})
        self.assertNotIn("금", out["allocations"])
        self.assertEqual(out["allocations"]["코스피"], [])              # 보유가 없는 카테고리는 팔 수 없다

    def test_symbols_needing_quotes_only_for_unheld_buys(self):
        rows = self.plan(우주=1000.0, **{"S&P": 1000.0, "코스피": -1000.0, "금": 0.0})
        self.assertEqual(portfolio.symbols_needing_quotes(rows, self.categories, self.held), ["SPYM"])

    def test_category_composition_is_exposed_in_holdings(self):
        import tempfile
        from pathlib import Path
        from helpers import storage
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.UserStore(Path(tmp))
            store.write_json("rebalance.json", {"targets": [{"label": "S&P", "symbols": ["SPYM", "SH"], "target_pct": 100.0}],
                                                "default_rest_target_pct": 0.0, "krw_target_pct": 50.0})
            snap = {"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": []}
            with unittest.mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
                data = portfolio.build_holdings(snap, store)
        self.assertEqual(data["rebalance_categories"], [{"label": "S&P", "symbols": ["SPYM", "SH"]}])


class EntryScalingTest(unittest.TestCase):
    def rows(self):
        return [
            {"label": "S&P", "action": "buy", "difference_krw": 300.0, "allocations": [
                {"symbol": "SPYM", "amount_krw": 200.0, "price_krw": 50.0, "currency": "USD"},
                {"symbol": "QNDX", "amount_krw": 100.0, "price_krw": 20.0, "currency": "USD"}]},
            {"label": "코스피", "action": "buy", "difference_krw": 100.0, "allocations": [
                {"symbol": "069500", "amount_krw": 100.0, "price_krw": 10.0, "currency": "KRW"}]},
            {"label": "우주", "action": "sell", "difference_krw": -50.0, "allocations": [
                {"symbol": "UFO", "amount_krw": 50.0, "price_krw": 5.0, "currency": "USD"}]},
        ]

    def test_scales_only_ok_signals_and_leaves_the_rest_full(self):
        rows = self.rows()
        out = portfolio.apply_entry_scaling(rows, {"SPYM": {"status": "ok", "entry_pct": 25.0},
                                                   "QNDX": {"status": "ok", "entry_pct": 0.0},
                                                   "069500": {"status": "excluded"}})
        sp = rows[0]
        self.assertAlmostEqual(sp["allocations"][0]["buy_now_krw"], 50.0)    # 200 × 25%
        self.assertAlmostEqual(sp["allocations"][0]["wait_krw"], 150.0)
        self.assertEqual(sp["allocations"][0]["shares_now"], 1.0)           # 50 / 50
        self.assertEqual(sp["allocations"][1]["buy_now_krw"], 0.0)          # 진입 0% → 지금 0
        self.assertEqual(sp["allocations"][1]["shares_now"], 0.0)
        self.assertAlmostEqual(sp["buy_now_krw"], 50.0)
        self.assertAlmostEqual(sp["wait_krw"], 250.0)
        kospi = rows[1]["allocations"][0]
        self.assertEqual((kospi["buy_now_krw"], kospi["wait_krw"], kospi["signal_status"]), (100.0, 0.0, "excluded"))
        self.assertNotIn("buy_now_krw", rows[2])                            # 매도 행은 그대로
        self.assertTrue(out["scaled"])
        self.assertAlmostEqual(out["buy_now_total_krw"], 150.0)
        self.assertAlmostEqual(out["wait_total_krw"], 250.0)

    def test_entry_is_clamped_to_0_100(self):
        rows = self.rows()[:1]
        portfolio.apply_entry_scaling(rows, {"SPYM": {"status": "ok", "entry_pct": 250.0}, "QNDX": {"status": "ok", "entry_pct": -5.0}})
        self.assertEqual(rows[0]["allocations"][0]["buy_now_krw"], 200.0)
        self.assertEqual(rows[0]["allocations"][1]["buy_now_krw"], 0.0)

    def test_nothing_scaled_without_signals(self):
        rows = self.rows()
        out = portfolio.apply_entry_scaling(rows, {})
        self.assertFalse(out["scaled"])
        self.assertAlmostEqual(out["buy_now_total_krw"], 400.0)
        self.assertEqual(out["wait_total_krw"], 0.0)
        self.assertEqual(rows[0]["allocations"][0]["signal_status"], "none")


class CurrencySplitTest(unittest.TestCase):
    def test_split_includes_cash(self):
        rows = [stock("A", "A", "USD", 600.0), stock("K", "K", "KRW", 200.0)]
        s = portfolio.compute_currency_split(rows, cash_krw_amt=100.0, cash_usd_amt=0.1, usd_krw=1000.0, total_eval=1000.0, krw_target_pct=30.0)
        self.assertAlmostEqual(s["krw_pct"], 30.0)
        self.assertAlmostEqual(s["usd_pct"], 70.0)
        self.assertEqual(s["usd_target_pct"], 70.0)


class ValidateRebalanceTargetsTest(unittest.TestCase):
    def test_valid(self):
        clean, err = portfolio.validate_rebalance_targets([{"label": " 묶음 ", "symbols": ["aaa", "AAA", "bbb"], "target_pct": 10}])
        self.assertIsNone(err)
        self.assertEqual(clean, [{"label": "묶음", "symbols": ["AAA", "BBB"], "target_pct": 10.0}])

    def test_rejections(self):
        cases = [
            ([{"label": "현금", "symbols": ["A"], "target_pct": 1}], "예약된 이름"),
            ([{"label": "x", "symbols": ["A"], "target_pct": 1}, {"label": "x", "symbols": ["B"], "target_pct": 1}], "중복"),
            ([{"label": "x", "symbols": ["A"], "target_pct": 1}, {"label": "y", "symbols": ["A"], "target_pct": 1}], "두 카테고리"),
            ([{"label": "x", "symbols": [], "target_pct": 1}], "하나 이상"),
            ([{"label": "x", "symbols": ["A"], "target_pct": 101}], "목표%"),
            ("nope", "배열"),
        ]
        for targets, frag in cases:
            with self.subTest(frag=frag):
                _, err = portfolio.validate_rebalance_targets(targets)
                self.assertIn(frag, err)


class ValidateMaRulesTest(unittest.TestCase):
    def test_valid(self):
        clean, err = watch.validate_ma_rules([{"symbol": "tlt", "interval": "week", "period": 60, "proximity_pct": 2}])
        self.assertIsNone(err)
        self.assertEqual(clean[0]["symbol"], "TLT")

    def test_rejections(self):
        cases = [
            ({"symbol": "TLT", "interval": "month", "period": 60, "proximity_pct": 2}, "봉 종류"),
            ({"symbol": "TLT", "interval": "day", "period": 201, "proximity_pct": 2}, "기간"),
            ({"symbol": "TLT", "interval": "week", "period": 60.5, "proximity_pct": 2}, "기간"),
            ({"symbol": "T L", "interval": "week", "period": 60, "proximity_pct": 2}, "종목 코드"),
            ({"symbol": "TLT", "interval": "week", "period": 60, "proximity_pct": -1}, "근접"),
        ]
        for rule, frag in cases:
            with self.subTest(frag=frag):
                _, err = watch.validate_ma_rules([rule])
                self.assertIn(frag, err)


class ToFloatTest(unittest.TestCase):
    def test_to_float(self):
        self.assertEqual(common.to_float("1.5"), 1.5)
        self.assertEqual(common.to_float(None), 0.0)
        self.assertEqual(common.to_float("x", -1), -1)


if __name__ == "__main__":
    unittest.main()
