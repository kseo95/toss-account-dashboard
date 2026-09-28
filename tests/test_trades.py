"""체결 내역 테스트: 주문 목록 페이지 넘기기, 체결만 골라내기, 합계, /api/trades."""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import toss_api, trades


def order(oid, sym, side, qty, price, cur="USD", filled_at="2026-03-02T23:30:00+09:00", commission="0.1", tax=None, status="FILLED"):
    return {"orderId": oid, "symbol": sym, "side": side, "currency": cur, "status": status, "orderedAt": filled_at,
            "execution": {"filledQuantity": str(qty), "averageFilledPrice": str(price) if qty else None,
                          "filledAmount": str(qty * price) if qty else None, "commission": commission, "tax": tax,
                          "filledAt": filled_at if qty else None, "settlementDate": "2026-03-04" if qty else None}}


class ClosedOrdersPagingTest(unittest.TestCase):
    def test_follows_cursor_until_no_next(self):
        pages = [{"result": {"orders": [order("1", "A", "BUY", 1, 10)], "hasNext": True, "nextCursor": "c1"}},
                 {"result": {"orders": [order("2", "A", "SELL", 1, 12)], "hasNext": False, "nextCursor": None}}]
        with mock.patch.object(toss_api, "api_get", side_effect=pages) as api:
            orders = toss_api.get_closed_orders("tok", "7", "2026-01-01", "2026-12-31")
        self.assertEqual([o["orderId"] for o in orders], ["1", "2"])
        first, second = api.call_args_list
        self.assertEqual(first.kwargs["params"], {"status": "CLOSED", "from": "2026-01-01", "to": "2026-12-31", "limit": 100})
        self.assertEqual(second.kwargs["params"]["cursor"], "c1")
        self.assertEqual(first.kwargs["account_seq"], "7")

    def test_page_cap(self):
        page = {"result": {"orders": [], "hasNext": True, "nextCursor": "same"}}
        with mock.patch.object(toss_api, "api_get", return_value=page) as api:
            toss_api.get_closed_orders("tok", "7", "2026-01-01", "2026-12-31")
        self.assertEqual(api.call_count, toss_api.ORDERS_MAX_PAGES)


class FillsTest(unittest.TestCase):
    def test_only_filled_and_sorted(self):
        orders = [order("1", "AAPL", "BUY", 2, 150.0, filled_at="2026-01-05T23:40:00+09:00"),
                  order("2", "AAPL", "BUY", 0, 0, status="CANCELED"),
                  order("3", "AAPL", "SELL", 1, 170.0, filled_at="2026-02-10T23:40:00+09:00", tax="0.01", status="PARTIAL_FILLED"),
                  order("4", "005930", "SELL", 3, 60000, cur="KRW", commission="27", tax="360", filled_at="2026-02-01T10:00:00+09:00")]
        fills = trades.fills_from_orders(orders, "7")
        self.assertEqual([f["order_id"] for f in fills], ["3", "4", "1"])
        aapl_sell = fills[0]
        self.assertEqual((aapl_sell["quantity"], aapl_sell["price"], aapl_sell["amount"], aapl_sell["tax"]), (1.0, 170.0, 170.0, 0.01))
        self.assertEqual(aapl_sell["account"], "7")

        s = trades.summarize_fills(fills)
        kinds = {(k["currency"], k["side"]): k for k in s["by_kind"]}
        self.assertEqual(kinds[("KRW", "SELL")]["tax"], 360.0)
        self.assertEqual(kinds[("USD", "BUY")]["amount"], 300.0)
        self.assertEqual(s["first_filled_at"][:10], "2026-01-05")

    def test_missing_amount_falls_back_to_qty_times_price(self):
        o = order("1", "A", "BUY", 2, 5.0)
        o["execution"]["filledAmount"] = None
        self.assertEqual(trades.fills_from_orders([o])[0]["amount"], 10.0)


if __name__ == "__main__":
    unittest.main()


def fill(sym, side, qty, amount, settle, commission=0.0, tax=0.0, cur="USD"):
    return {"symbol": sym, "side": side, "quantity": qty, "amount": amount, "commission": commission, "tax": tax,
            "currency": cur, "settlement_date": settle, "filled_at": settle + "T10:00:00+09:00"}


FX = {"2025-06-02": 1300.0, "2026-01-05": 1400.0, "2026-03-04": 1450.0, "2026-12-31": 1500.0, "2027-01-04": 1500.0}


def fx_on(d):
    return FX.get(d.isoformat())


class RealizedGainsTest(unittest.TestCase):
    def test_fifo_with_fx_and_costs(self):
        fills = [fill("A", "BUY", 10, 1000.0, "2025-06-02", commission=1.0),     # 1,001 × 1300 → 130,130/주
                 fill("A", "BUY", 10, 1200.0, "2026-01-05"),                     # 120 × 1400 = 168,000/주
                 fill("A", "SELL", 15, 2250.0, "2026-03-04", commission=2.0, tax=0.5)]
        r = trades.realized_gains(fills, fx_on, 2026)
        s = r["sells"][0]
        self.assertAlmostEqual(s["proceeds_krw"], (2250 - 2.5) * 1450)
        self.assertAlmostEqual(s["cost_krw"], 1001 * 1300 + 5 * 168_000)       # 먼저 산 10주 전부 + 나중 5주
        self.assertAlmostEqual(r["total_gain_krw"], s["proceeds_krw"] - s["cost_krw"])
        self.assertTrue(r["complete"])

    def test_year_by_settlement_date(self):
        fills = [fill("A", "BUY", 1, 100.0, "2026-01-05"), fill("A", "SELL", 1, 150.0, "2027-01-04")]  # 12/31 체결, 1/4 결제
        self.assertEqual(trades.realized_gains(fills, fx_on, 2026)["sells"], [])
        self.assertEqual(len(trades.realized_gains(fills, fx_on, 2027)["sells"]), 1)

    def test_missing_buys_and_fx_marked_incomplete(self):
        fills = [fill("A", "BUY", 1, 100.0, "2026-01-05"), fill("A", "SELL", 3, 450.0, "2026-03-04"),
                 fill("B", "BUY", 1, 10.0, "2020-01-02"), fill("B", "SELL", 1, 20.0, "2026-03-04")]
        r = trades.realized_gains(fills, fx_on, 2026)
        self.assertFalse(r["complete"])
        self.assertEqual(r["missing_buys"], [{"symbol": "A", "quantity": 2, "date": "2026-03-04"}])
        self.assertEqual(r["missing_fx"], ["2020-01-02"])
        self.assertEqual(r["unknown_count"], 2)
        self.assertEqual(r["total_gain_krw"], 0.0)

    def test_domestic_ignored(self):
        fills = [fill("005930", "BUY", 1, 60000.0, "2026-01-05", cur="KRW"), fill("005930", "SELL", 1, 70000.0, "2026-03-04", cur="KRW")]
        self.assertEqual(trades.realized_gains(fills, fx_on, 2026)["sells"], [])

    def test_fx_lookup_uses_previous_trading_day(self):
        from datetime import date
        on = trades.fx_lookup([(date(2026, 1, 2), 1400.0, 1400.0), (date(2026, 1, 5), 1410.0, 1410.0)])
        self.assertEqual(on(date(2026, 1, 3)), 1400.0)   # 토요일 → 금요일
        self.assertEqual(on(date(2026, 1, 5)), 1410.0)
        self.assertIsNone(on(date(2025, 12, 31)))
