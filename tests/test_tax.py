"""세금 추정 테스트: 종류 추정, 증권거래세, 해외 양도세 합산·공제, 기타 ETF, 설정 검증, 매도 세금 비율."""
from __future__ import annotations

import unittest

from helpers import tax


def pos(sym, name, cur, value, gain):
    return {"심볼": sym, "종목": name, "통화": cur, "평가금액(원)": value, "손익(원)": gain}


def holdings(stocks, cash_stocks=()):
    return {"stocks": list(stocks), "cash": {"stocks": list(cash_stocks)}}


def settings(**kw):
    return tax.normalize_tax_settings(kw)


class GuessClassTest(unittest.TestCase):
    def test_guess(self):
        cases = {
            "삼성전자": "stock", "KODEX 200": "etf_equity", "KODEX 인버스": "etf_equity",
            "PLUS 고배당주위클리커버드콜": "etf_equity", "KODEX 은행": "etf_equity", "TIGER 미디어&엔터테인먼트": "etf_equity",
            "TIGER 미국S&P500": "etf_other", "KODEX 국고채3년": "etf_other", "ACE 골드선물": "etf_other",
            "KODEX 미국달러선물": "etf_other",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(tax.guess_domestic_class(name), expected)


class EstimateTest(unittest.TestCase):
    def test_domestic_transaction_tax_by_market(self):
        h = holdings([pos("005930", "삼성전자", "KRW", 1_000_000, 50_000), pos("247540", "에코프로비엠", "KRW", 500_000, -10_000)])
        est = tax.estimate_taxes(h, settings(), {"005930": {"market": "KOSPI"}, "247540": {"market": "KOSDAQ"}})
        rows = {r["symbol"]: r for r in est["rows"]}
        self.assertAlmostEqual(rows["005930"]["tax_krw"], 2_000)      # 0.20%
        self.assertAlmostEqual(rows["247540"]["tax_krw"], 1_000)      # 손실이어도 거래세는 붙음
        self.assertAlmostEqual(est["summary"]["domestic_tax_if_sell_all_krw"], 3_000)
        self.assertEqual(est["summary"]["gain_tax_if_sell_all_krw"], 0.0)

    def test_overseas_pooled_with_deduction(self):
        h = holdings([pos("AAPL", "Apple", "USD", 10_000_000, 4_000_000), pos("TSLA", "Tesla", "USD", 3_000_000, -1_000_000)],
                     [pos("SGOV", "SGOV", "USD", 2_000_000, 500_000)])
        est = tax.estimate_taxes(h, settings(), {})
        s = est["summary"]
        self.assertAlmostEqual(s["unrealized_pooled_gain_krw"], 3_500_000)
        self.assertAlmostEqual(s["gain_tax_if_sell_all_krw"], (3_500_000 - 2_500_000) * 0.22)
        rows = {r["symbol"]: r for r in est["rows"]}
        self.assertAlmostEqual(rows["AAPL"]["tax_krw"], (4_000_000 - 2_500_000) * 0.22)  # 이 종목만 판다면
        self.assertEqual(rows["TSLA"]["tax_krw"], 0.0)                                    # 손실만 팔면 세금 증가 없음
        self.assertEqual(rows["SGOV"]["tax_krw"], 0.0)                                    # 공제 안쪽

    def test_realized_this_year_uses_up_deduction(self):
        h = holdings([pos("AAPL", "Apple", "USD", 5_000_000, 1_000_000)])
        est = tax.estimate_taxes(h, settings(realized_overseas_gain_ytd_krw=2_000_000), {})
        s = est["summary"]
        self.assertAlmostEqual(s["deduction_left_krw"], 500_000)
        self.assertEqual(s["tax_on_realized_krw"], 0.0)
        self.assertAlmostEqual(s["gain_tax_if_sell_all_krw"], 500_000 * 0.22)
        self.assertAlmostEqual(s["gain_tax_year_total_if_sell_all_krw"], 500_000 * 0.22)

    def test_realized_loss_offsets(self):
        h = holdings([pos("AAPL", "Apple", "USD", 5_000_000, 4_000_000)])
        est = tax.estimate_taxes(h, settings(realized_overseas_gain_ytd_krw=-1_000_000), {})
        self.assertAlmostEqual(est["summary"]["gain_tax_if_sell_all_krw"], (3_000_000 - 2_500_000) * 0.22)

    def test_etf_classes_and_override(self):
        h = holdings([pos("360750", "TIGER 미국S&P500", "KRW", 1_000_000, 200_000),
                      pos("069500", "KODEX 200", "KRW", 1_000_000, 200_000),
                      pos("123456", "KODEX 뭔가", "KRW", 1_000_000, -50_000)])
        est = tax.estimate_taxes(h, settings(domestic_classes={"123456": "etf_other"}), {})
        rows = {r["symbol"]: r for r in est["rows"]}
        self.assertAlmostEqual(rows["360750"]["tax_krw"], 200_000 * 0.154)
        self.assertTrue(rows["360750"]["class_guessed"])
        self.assertEqual(rows["069500"]["tax_krw"], 0.0)               # 국내주식형: 비과세, 거래세 없음
        self.assertEqual(rows["123456"]["tax_krw"], 0.0)               # 기타 ETF라도 손실이면 0
        self.assertFalse(rows["123456"]["class_guessed"])

    def test_major_shareholder_pools_domestic_stock(self):
        h = holdings([pos("005930", "삼성전자", "KRW", 10_000_000, 3_500_000)])
        est = tax.estimate_taxes(h, settings(is_major_shareholder=True), {"005930": {"market": "KOSPI"}})
        row = est["rows"][0]
        self.assertAlmostEqual(row["tax_krw"], 20_000 + (3_500_000 - 2_500_000) * 0.22)

    def test_sell_tax_rate(self):
        h = holdings([pos("AAPL", "Apple", "USD", 10_000_000, 4_000_000)])
        rates = tax.sell_tax_rate_by_symbol(tax.estimate_taxes(h, settings(), {}))
        self.assertAlmostEqual(rates["AAPL"], (1_500_000 * 0.22) / 10_000_000)


class SettingsTest(unittest.TestCase):
    def test_defaults_match_law(self):
        s = settings()
        self.assertEqual(s["transaction_tax_pct"], {"KOSPI": 0.20, "KOSDAQ": 0.20, "KONEX": 0.10})
        self.assertEqual((s["overseas_gain_rate_pct"], s["gain_deduction_krw"]), (22.0, 2_500_000.0))
        self.assertEqual(s["financial_income_threshold_krw"], 20_000_000.0)

    def test_rejections(self):
        for bad, frag in [({"overseas_gain_rate_pct": 120}, "해외 양도세율"), ({"transaction_tax_pct": {"KOSPI": 9}}, "KOSPI"),
                          ({"domestic_classes": {"A": "nope"}}, "종류"), ("x", "형식")]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    tax.normalize_tax_settings(bad)
                self.assertIn(frag, str(ctx.exception))

    def test_partial_market_keeps_defaults(self):
        self.assertEqual(settings(transaction_tax_pct={"KOSDAQ": 0.15})["transaction_tax_pct"]["KOSPI"], 0.20)


if __name__ == "__main__":
    unittest.main()
