"""토스 원자료 가공 테스트: 일봉→주봉 묶음·페이지·캐시, 계좌 원자료 합치기, 보유 화면 계산."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from helpers import portfolio, storage, toss_api


def candle(d: date, close: float) -> dict:
    return {"timestamp": d.isoformat() + "T00:00:00+09:00", "closePrice": close}


def pages_of(days: list[date], per_page: int) -> list[dict]:
    """최신순 날짜 목록 → get_daily_candles가 돌려주는 페이지들(nextBefore로 이어짐)."""
    pages = []
    for i in range(0, len(days), per_page):
        chunk = days[i:i + per_page]
        more = i + per_page < len(days)
        pages.append({"candles": [candle(d, float(d.toordinal())) for d in chunk], "nextBefore": f"p{i + per_page}" if more else None})
    return pages


class CloseHistoryTest(unittest.TestCase):
    def setUp(self):
        toss_api._candle_cache.clear()
        # 2026-09-25(금)부터 거꾸로 평일 30일
        d, self.days = date(2026, 9, 25), []
        while len(self.days) < 30:
            if d.weekday() < 5:
                self.days.append(d)
            d -= timedelta(days=1)

    def run_fetch(self, pages, **kw):
        with mock.patch.object(toss_api, "get_daily_candles", side_effect=pages) as g:
            result = toss_api.fetch_close_history("tok", "AAA", **kw)
        return result, g.call_count

    def test_weekly_takes_last_trading_day_of_each_week(self):
        (daily, weekly), _ = self.run_fetch(pages_of(self.days, 200), weeks=3)
        self.assertEqual(daily[0], float(date(2026, 9, 25).toordinal()))
        self.assertEqual(weekly[:3], [float(date(2026, 9, d).toordinal()) for d in (25, 18, 11)])

    def test_stops_paging_when_enough(self):
        _, calls = self.run_fetch(pages_of(self.days, 10), days=5)
        self.assertEqual(calls, 1)
        toss_api._candle_cache.clear()
        _, calls = self.run_fetch(pages_of(self.days, 10), weeks=4)  # 한 페이지(10일)=2주 → 2페이지 필요
        self.assertEqual(calls, 2)

    def test_cache_reused_until_more_is_needed(self):
        pages = pages_of(self.days, 10)
        with mock.patch.object(toss_api, "get_daily_candles", side_effect=pages * 3) as g:
            toss_api.fetch_close_history("tok", "AAA", days=5)
            toss_api.fetch_close_history("tok", "AAA", days=8)  # 캐시(10일)로 충분
            self.assertEqual(g.call_count, 1)
            toss_api.fetch_close_history("tok", "AAA", days=25)  # 부족 → 다시 받음
            self.assertGreater(g.call_count, 1)

    def test_exhausted_history_is_cached_too(self):
        pages = pages_of(self.days[:5], 200)  # 상장 직후처럼 기록이 5일뿐
        with mock.patch.object(toss_api, "get_daily_candles", side_effect=pages * 2) as g:
            daily, _ = toss_api.fetch_close_history("tok", "NEW", days=100)
            toss_api.fetch_close_history("tok", "NEW", days=100)
        self.assertEqual(len(daily), 5)
        self.assertEqual(g.call_count, 1)

    def test_bad_timestamp_kept_for_daily_but_not_weekly(self):
        page = {"candles": [{"timestamp": "", "closePrice": 1.0}, candle(date(2026, 9, 24), 2.0)], "nextBefore": None}
        (daily, weekly), _ = self.run_fetch([page], days=2)
        self.assertEqual(daily, [1.0, 2.0])
        self.assertEqual(weekly, [2.0])


class AccountSnapshotTest(unittest.TestCase):
    def test_sums_accounts(self):
        accounts = [{"accountSeq": "1"}, {"accountSeq": "2"}]
        with mock.patch.object(toss_api, "get_usd_krw_rate", return_value=1400.0), \
                mock.patch.object(toss_api, "get_buying_power", side_effect=lambda t, seq, cur: {"KRW": 100.0, "USD": 1.0}[cur] * int(seq)), \
                mock.patch.object(toss_api, "get_holdings", side_effect=lambda t, seq: {"items": [{"symbol": f"S{seq}"}]}):
            snap = toss_api.fetch_account_snapshot("tok", accounts)
        self.assertEqual(snap["usd_krw"], 1400.0)
        self.assertEqual(snap["cash_krw"], 300.0)
        self.assertEqual(snap["cash_usd"], 3.0)
        self.assertEqual(sorted(it["symbol"] for it in snap["items"]), ["S1", "S2"])


def item(sym, cur, amount, pl, qty=1.0, name=None):
    return {"symbol": sym, "name": name or sym, "currency": cur, "quantity": qty, "marketCountry": "KR" if cur == "KRW" else "US",
            "marketValue": {"amount": amount}, "profitLoss": {"amountAfterCost": pl, "rateAfterCost": 0.1}}


class BuildHoldingsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = storage.UserStore(Path(tmp.name))
        p = mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_totals_cash_split_and_zero_quantity(self):
        portfolio.save_cash_symbols(self.store, ["SGOV"])
        snap = {"usd_krw": 1000.0, "cash_krw": 500.0, "cash_usd": 0.5,
                "items": [item("AAPL", "USD", 3.0, 1.0), item("005930", "KRW", 4000.0, -200.0), item("SGOV", "USD", 2.0, 0.0),
                          item("GONE", "USD", 9.0, 9.0, qty=0)]}
        h = portfolio.build_holdings(snap, self.store)
        self.assertEqual([r["심볼"] for r in h["stocks"]], ["005930", "AAPL"])  # 평가금액 내림차순, 수량 0 제외
        self.assertEqual([r["심볼"] for r in h["cash"]["stocks"]], ["SGOV"])
        self.assertAlmostEqual(h["totals"]["eval_krw"], 3000 + 4000 + 2000 + 500 + 500)
        self.assertAlmostEqual(h["totals"]["profit_loss_krw"], 1000 - 200)
        self.assertAlmostEqual(h["cash"]["total_krw"], 500 + 500 + 2000)
        self.assertAlmostEqual(h["stocks"][1]["수익률(%)"], 10.0)
        self.assertAlmostEqual(sum(r["current_pct"] for r in h["rebalance"]), 100.0)


if __name__ == "__main__":
    unittest.main()
