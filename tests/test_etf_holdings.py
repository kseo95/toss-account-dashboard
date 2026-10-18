"""ETF 편입 종목 조회: 표 파싱(이상한 행 건너뛰기), 상태 구분(ok/none/unavailable/error), 하루 캐시. 네트워크는 쓰지 않는다."""
from __future__ import annotations

import unittest

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import etf_holdings


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(self.rows)


def frame(*rows):
    return FakeFrame([(sym, {"Name": name, "Holding Percent": weight}) for sym, name, weight in rows])


class FakeTicker:
    def __init__(self, result):
        self.result = result

    @property
    def funds_data(self):
        if isinstance(self.result, Exception):
            raise self.result
        outer = self

        class FD:
            top_holdings = outer.result
        return FD()


class HoldingsTestCase(unittest.TestCase):
    def setUp(self):
        etf_holdings.clear_cache()


class ParseTest(HoldingsTestCase):
    def test_valid_rows_are_kept_and_bad_rows_are_skipped(self):
        rows = etf_holdings.parse_top_holdings(frame(("rklb", "Rocket Lab", 0.054), ("ASTS", "AST", 0.048), ("bad symbol!", "X", 0.1),
                                                     ("NANW", "nan", float("nan")), ("ZERO", "z", 0), ("NEG", "n", -0.1), ("STR", "s", "abc")))
        self.assertEqual([r["symbol"] for r in rows], ["RKLB", "ASTS"])
        self.assertAlmostEqual(rows[0]["weight"], 0.054)

    def test_percent_values_are_converted_to_fractions(self):
        rows = etf_holdings.parse_top_holdings(frame(("AAA", "a", 40.0), ("BBB", "b", 30.0)))
        self.assertAlmostEqual(sum(r["weight"] for r in rows), 0.7)

    def test_missing_name_falls_back_to_symbol_and_long_names_are_cut(self):
        rows = etf_holdings.parse_top_holdings(frame(("AAA", "", 0.1), ("BBB", "가" * 200, 0.1)))
        self.assertEqual(rows[0]["name"], "AAA")
        self.assertEqual(len(rows[1]["name"]), etf_holdings.NAME_MAX)

    def test_non_frame_input_gives_empty_list(self):
        self.assertEqual(etf_holdings.parse_top_holdings(None), [])
        self.assertEqual(etf_holdings.parse_top_holdings("x"), [])


class FetchTest(HoldingsTestCase):
    def test_ok_none_unavailable_and_error_are_told_apart(self):
        ok = etf_holdings.fetch_top_holdings("UFO", lambda s: FakeTicker(frame(("RKLB", "Rocket Lab", 0.054))))
        self.assertEqual((ok["status"], ok["holdings"][0]["symbol"]), ("ok", "RKLB"))
        empty = etf_holdings.fetch_top_holdings("TLT", lambda s: FakeTicker(frame()))
        self.assertEqual((empty["status"], empty["holdings"]), ("none", []))
        nofund = etf_holdings.fetch_top_holdings("MSTR", lambda s: FakeTicker(RuntimeError("MSTR: No Fund data found.")))
        self.assertEqual(nofund["status"], "none")
        boom = etf_holdings.fetch_top_holdings("XXX", lambda s: FakeTicker(RuntimeError("timeout")))
        self.assertEqual(boom["status"], "error")

        def missing_package(symbol):
            raise ImportError("no yfinance")
        self.assertEqual(etf_holdings.fetch_top_holdings("YYY", missing_package)["status"], "unavailable")

    def test_ok_and_none_are_cached_but_errors_are_retried(self):
        calls = []

        def factory(result):
            def make(symbol):
                calls.append(symbol)
                return FakeTicker(result)
            return make
        good = factory(frame(("AAA", "a", 0.1)))
        etf_holdings.fetch_top_holdings("UFO", good)
        etf_holdings.fetch_top_holdings("ufo", good)                       # 대소문자 무시, 캐시 적중
        self.assertEqual(len(calls), 1)
        bad = factory(RuntimeError("timeout"))
        etf_holdings.fetch_top_holdings("ERR", bad)
        etf_holdings.fetch_top_holdings("ERR", bad)
        self.assertEqual(calls.count("ERR"), 2)

    def test_cache_returns_copies(self):
        factory = lambda s: FakeTicker(frame(("AAA", "a", 0.1)))
        first = etf_holdings.fetch_top_holdings("UFO", factory)
        first["holdings"][0]["symbol"] = "변조"
        self.assertEqual(etf_holdings.fetch_top_holdings("UFO", factory)["holdings"][0]["symbol"], "AAA")


if __name__ == "__main__":
    unittest.main()
