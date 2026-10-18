"""섹터 매핑: 검증, 저장 왕복, 노출 계산, 서버 라우트, Agent 도구."""
from __future__ import annotations

import unittest
from unittest import mock

import tempfile
from pathlib import Path

from helpers import portfolio, server, storage, toss_api  # noqa: F401  (helpers가 import 경로를 잡는다)
import sectors
from agent_tools import compose_agent_answer, format_tool_result, plan_agent_query
import test_storage_http as storage_http

TempDataDirMixin = storage_http.TempDataDirMixin


def valid_config():
    return {
        "sectors": [{"label": "반도체", "proxy": "soxx"}, {"label": "빅테크", "proxy": ""}],
        "assignments": [
            {"symbol": "005930", "sector": "반도체", "pct": 100},
            {"symbol": "069500", "sector": "반도체", "pct": 35},
            {"symbol": "069500", "sector": "빅테크", "pct": 10},
        ],
    }


def rows():
    return [
        {"심볼": "005930", "종목": "삼성전자", "평가금액(원)": 600.0},
        {"심볼": "069500", "종목": "KODEX 200", "평가금액(원)": 300.0},
        {"심볼": "AAPL", "종목": "애플", "평가금액(원)": 100.0},
    ]


class ValidateSectorMapTest(unittest.TestCase):
    def test_accepts_valid_config_and_normalizes(self):
        config, error = sectors.validate_sector_map(valid_config())
        self.assertIsNone(error)
        self.assertEqual(config["sectors"][0], {"label": "반도체", "proxy": "SOXX"})  # 대표 티커는 대문자로
        self.assertEqual(len(config["assignments"]), 3)

    def test_empty_config_is_valid(self):
        config, error = sectors.validate_sector_map({"sectors": [], "assignments": []})
        self.assertIsNone(error)
        self.assertEqual(config, {"sectors": [], "assignments": []})

    def test_rejects_bad_shapes(self):
        for raw in (None, [], "x", {"sectors": "a", "assignments": []}, {"sectors": [], "assignments": {}}):
            self.assertIsNotNone(sectors.validate_sector_map(raw)[1], raw)

    def test_rejects_bad_sector_labels(self):
        for label in ("", "   ", "가" * 31):
            _, error = sectors.validate_sector_map({"sectors": [{"label": label}], "assignments": []})
            self.assertIsNotNone(error, label)
        _, error = sectors.validate_sector_map({"sectors": [{"label": "A"}, {"label": "A"}], "assignments": []})
        self.assertIn("중복", error)

    def test_rejects_bad_proxy(self):
        _, error = sectors.validate_sector_map({"sectors": [{"label": "A", "proxy": "bad ticker!"}], "assignments": []})
        self.assertIn("대표 티커", error)
        config, error = sectors.validate_sector_map({"sectors": [{"label": "A", "proxy": "^GSPC"}, {"label": "B", "proxy": "GC=F"}],
                                                     "assignments": []})
        self.assertIsNone(error)  # Yahoo 지수·선물 티커 형태는 허용

    def test_rejects_unknown_sector_and_bad_symbol(self):
        base = {"sectors": [{"label": "A"}]}
        _, error = sectors.validate_sector_map({**base, "assignments": [{"symbol": "AAA", "sector": "B", "pct": 50}]})
        self.assertIn("존재하지 않는 섹터", error)
        _, error = sectors.validate_sector_map({**base, "assignments": [{"symbol": "bad symbol", "sector": "A", "pct": 50}]})
        self.assertIn("종목 코드", error)

    def test_pct_bounds_and_sum(self):
        base = {"sectors": [{"label": "A"}, {"label": "B"}]}
        for pct in (0, -5, 101, "abc", None):
            _, error = sectors.validate_sector_map({**base, "assignments": [{"symbol": "AAA", "sector": "A", "pct": pct}]})
            self.assertIsNotNone(error, pct)
        # 같은 종목의 섹터별 합계가 100을 넘으면 거부, 정확히 100은 통과
        over = [{"symbol": "AAA", "sector": "A", "pct": 60}, {"symbol": "AAA", "sector": "B", "pct": 50}]
        self.assertIn("100을 넘", sectors.validate_sector_map({**base, "assignments": over})[1])
        exact = [{"symbol": "AAA", "sector": "A", "pct": 60}, {"symbol": "AAA", "sector": "B", "pct": 40}]
        self.assertIsNone(sectors.validate_sector_map({**base, "assignments": exact})[1])

    def test_rejects_duplicate_pair_and_limits(self):
        base = {"sectors": [{"label": "A"}]}
        dup = [{"symbol": "AAA", "sector": "A", "pct": 10}, {"symbol": "aaa", "sector": "A", "pct": 10}]
        self.assertIn("두 번", sectors.validate_sector_map({**base, "assignments": dup})[1])
        many_sectors = [{"label": f"S{i}"} for i in range(sectors.SECTOR_MAX + 1)]
        self.assertIsNotNone(sectors.validate_sector_map({"sectors": many_sectors, "assignments": []})[1])
        many_assign = [{"symbol": f"S{i}", "sector": "A", "pct": 1} for i in range(sectors.ASSIGNMENT_MAX + 1)]
        self.assertIsNotNone(sectors.validate_sector_map({**base, "assignments": many_assign})[1])


class StoreTest(TempDataDirMixin, unittest.TestCase):
    def test_missing_file_gives_empty_config(self):
        store = storage.store_for_user("a" * 24)
        self.assertEqual(sectors.load_sector_map(store), {"sectors": [], "assignments": []})

    def test_round_trip_and_corrupted_file(self):
        store = storage.store_for_user("a" * 24)
        config, _ = sectors.validate_sector_map(valid_config())
        sectors.save_sector_map(store, config)
        self.assertEqual(sectors.load_sector_map(store), config)
        store.path(sectors.SECTOR_MAP_FILE).write_text("{깨진", encoding="utf-8")
        self.assertEqual(sectors.load_sector_map(store), {"sectors": [], "assignments": []})

    def test_load_skips_unreadable_entries(self):
        store = storage.store_for_user("a" * 24)
        store.write_json(sectors.SECTOR_MAP_FILE, {
            "sectors": [{"label": "A"}, "x", {"label": ""}, {"label": "A"}],
            "assignments": [{"symbol": "aaa", "sector": "A", "pct": 50}, {"symbol": "BBB", "sector": "없음", "pct": 50},
                            {"symbol": "CCC", "sector": "A", "pct": 0}, 3],
        })
        loaded = sectors.load_sector_map(store)
        self.assertEqual(loaded["sectors"], [{"label": "A", "proxy": ""}])
        self.assertEqual(loaded["assignments"], [{"symbol": "AAA", "sector": "A", "pct": 50}])


class ComputeExposureTest(unittest.TestCase):
    def setUp(self):
        self.config, _ = sectors.validate_sector_map(valid_config())

    def test_amounts_percentages_and_holdings_order(self):
        out = sectors.compute_exposure(rows(), 2000.0, self.config)
        semis, bigtech = out["sectors"]
        # 반도체: 삼성전자 600 × 100% + KODEX 200 300 × 35% = 705
        self.assertAlmostEqual(semis["exposure_krw"], 705.0)
        self.assertAlmostEqual(semis["exposure_pct"], 705 / 2000 * 100)  # 분모는 보유종목 + 예수금
        self.assertEqual([h["symbol"] for h in semis["holdings"]], ["005930", "069500"])
        self.assertAlmostEqual(semis["holdings"][1]["amount_krw"], 105.0)
        self.assertEqual(semis["proxy"], "SOXX")
        self.assertAlmostEqual(bigtech["exposure_krw"], 30.0)  # KODEX 200 300 × 10%

    def test_unmapped_holdings_are_listed(self):
        out = sectors.compute_exposure(rows(), 1000.0, self.config)
        self.assertEqual([u["symbol"] for u in out["unmapped"]], ["AAPL"])
        self.assertAlmostEqual(out["unmapped"][0]["weight_pct"], 10.0)

    def test_sector_without_holdings_still_listed_with_zero(self):
        config = {"sectors": [{"label": "에너지", "proxy": ""}], "assignments": [{"symbol": "XOM", "sector": "에너지", "pct": 100}]}
        out = sectors.compute_exposure(rows(), 1000.0, config)
        self.assertEqual(out["sectors"][0]["exposure_krw"], 0)
        self.assertEqual(out["sectors"][0]["holdings"], [])

    def test_zero_total_does_not_divide_by_zero(self):
        out = sectors.compute_exposure([], 0.0, self.config)
        self.assertTrue(all(s["exposure_pct"] == 0 for s in out["sectors"]))


class SectorHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def snapshot(self):
        items = [
            {"symbol": "005930", "name": "삼성전자", "currency": "KRW", "quantity": 1, "marketValue": {"amount": 600}},
            {"symbol": "AAPL", "name": "애플", "currency": "KRW", "quantity": 1, "marketValue": {"amount": 100}},
        ]
        return {"usd_krw": 1000.0, "cash_krw": 300.0, "cash_usd": 0.0, "items": items}

    def test_requires_login(self):
        for method, path in (("GET", "/api/sector-map"), ("POST", "/api/sector-map"), ("GET", "/api/sector-exposure")):
            self.assertEqual(self.request(method, path, {} if method == "POST" else None)[0], 401, path)

    def test_save_validates_and_checks_symbols_exist(self):
        cookie = self.login("key-a")
        self.assertEqual(self.request("GET", "/api/sector-map", cookie=cookie)[1], {"sectors": [], "assignments": []})
        status, payload, _ = self.request("POST", "/api/sector-map", valid_config(), cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.request("GET", "/api/sector-map", cookie=cookie)[1]["sectors"][0]["proxy"], "SOXX")
        # 검증 실패는 400이고 저장된 값은 그대로
        bad = valid_config()
        bad["assignments"][0]["pct"] = 150
        self.assertEqual(self.request("POST", "/api/sector-map", bad, cookie=cookie)[0], 400)
        self.assertEqual(len(self.request("GET", "/api/sector-map", cookie=cookie)[1]["assignments"]), 3)
        # 토스에 없는 종목은 거부
        with mock.patch.object(server, "get_stock_info", return_value={}):
            status, payload, _ = self.request("POST", "/api/sector-map", valid_config(), cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn("존재하지 않는 종목", payload["error"])

    def test_settings_are_per_user(self):
        a, b = self.login("key-a"), self.login("key-b")
        self.request("POST", "/api/sector-map", valid_config(), cookie=a)
        self.assertEqual(self.request("GET", "/api/sector-map", cookie=b)[1], {"sectors": [], "assignments": []})

    def test_exposure_endpoint(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            empty = self.request("GET", "/api/sector-exposure", cookie=cookie)[1]
            self.assertFalse(empty["configured"])
            self.request("POST", "/api/sector-map", valid_config(), cookie=cookie)
            out = self.request("GET", "/api/sector-exposure", cookie=cookie)[1]
        self.assertTrue(out["configured"])
        semis = out["sectors"][0]
        self.assertEqual(semis["label"], "반도체")
        self.assertAlmostEqual(semis["exposure_krw"], 600.0)  # 069500은 보유하지 않음
        self.assertAlmostEqual(semis["exposure_pct"], 600 / 1000 * 100)  # 분모 = 보유 700 + 예수금 300
        self.assertEqual([u["symbol"] for u in out["unmapped"]], ["AAPL"])

    def test_agent_tool_endpoint_returns_only_allowed_fields(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()), \
                mock.patch.object(portfolio, "get_cached_usd_jpy_quote", return_value=None):
            self.request("POST", "/api/sector-map", valid_config(), cookie=cookie)
            status, payload, _ = self.request("POST", "/api/agent/tool", {"name": "get_sector_exposure"}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["result"]["sectors"][0]["label"], "반도체")
        self.assertNotIn("11100000001", repr(payload))  # 계좌번호가 섞이지 않는다


class SectorAgentToolTest(unittest.TestCase):
    def raw(self):
        config, _ = sectors.validate_sector_map(valid_config())
        out = sectors.compute_exposure(rows(), 1000.0, config)
        return {**out, "configured": True, "token": "must-not-leak"}

    def test_formatter_keeps_only_allowed_fields_and_limitations(self):
        out = format_tool_result("get_sector_exposure", self.raw())
        result = out["result"]
        self.assertTrue(out["read_only"])
        self.assertEqual(result["sectors"][0]["label"], "반도체")
        self.assertEqual(result["unmapped_count"], 1)
        self.assertTrue(any("직접 입력" in x for x in result["limitations"]))
        self.assertNotIn("must-not-leak", repr(out))

    def test_plan_selects_sector_tool(self):
        plan = plan_agent_query("내 섹터 노출 알려줘")
        self.assertEqual(plan["tools"], ["get_sector_exposure"])
        self.assertIn("sector", plan["intents"])

    def test_answer_for_unconfigured_and_configured(self):
        empty = format_tool_result("get_sector_exposure", {"sectors": [], "unmapped": [], "configured": False})["result"]
        answer = compose_agent_answer({"get_sector_exposure": empty})
        self.assertTrue(any("섹터 매핑이 설정되어 있지 않습니다" in b for b in answer["bullets"]))
        full = format_tool_result("get_sector_exposure", self.raw())["result"]
        answer = compose_agent_answer({"get_sector_exposure": full})
        self.assertTrue(any(b.startswith("반도체") and "삼성전자" in b for b in answer["bullets"]))
        self.assertTrue(any("연결하지 않은 보유종목이 1개" in b for b in answer["bullets"]))
        self.assertTrue(answer["limitations"])


def toss_sectors():
    return [
        {"sectorId": "169", "name": "반도체", "depth": 0, "parentSectorId": None},
        {"sectorId": "171", "name": "반도체부품소재", "depth": 1, "parentSectorId": "169"},
        {"sectorId": "1", "name": "운송", "depth": 0, "parentSectorId": None},
        {"sectorId": "711", "name": "반도체 전공정 장비", "depth": 2, "parentSectorId": "171"},
    ]


# (종류, 시장, 섹터ID) → 구성 항목. 없으면 빈 목록.
MEMBERS = {
    ("stocks", "KR", "169"): [{"symbol": "005930", "name": "삼성전자"}, {"symbol": "000660", "name": "SK하이닉스"}],
    ("stocks", "US", "169"): [{"symbol": "NVDA", "name": "엔비디아"}],
    ("stocks", "KR", "1"): [{"symbol": "005930", "name": "삼성전자"}],  # 여러 섹터에 속하는 종목
    ("etfs", "KR", "171"): [{"symbol": "069500", "name": "KODEX 200"}],
    ("etfs", "KR", "711"): [{"symbol": "069500", "name": "KODEX 200"}],
}


def fake_members(sector_id, market, kind):
    return MEMBERS.get((kind, market, sector_id), [])


class TossSectorApiTest(unittest.TestCase):
    def test_get_sectors_reads_list_result(self):
        with mock.patch.object(toss_api, "api_get", return_value={"result": toss_sectors()}) as call:
            out = toss_api.get_sectors("tok")
        self.assertEqual(len(out), 4)
        call.assert_called_once_with("/api/v1/sectors", "tok")

    def test_result_list_tolerates_wrapped_and_bad_shapes(self):
        self.assertEqual(toss_api._result_list({"result": {"sectors": [{"a": 1}, "x"]}}), [{"a": 1}])
        for bad in ({"result": "x"}, {"result": None}, {}, None):
            self.assertEqual(toss_api._result_list(bad), [])

    def test_members_path_and_params(self):
        with mock.patch.object(toss_api, "api_get", return_value={"result": [{"symbol": "A"}]}) as call:
            self.assertEqual(toss_api.get_sector_members("tok", "169", "KR", "etfs"), [{"symbol": "A"}])
        call.assert_called_once_with("/api/v1/sectors/169/etfs", "tok", params={"marketCountry": "KR"})

    def test_members_rejects_unsafe_input_without_calling_api(self):
        with mock.patch.object(toss_api, "api_get") as call:
            for args in (("1/../x", "KR", "stocks"), ("169", "JP", "stocks"), ("169", "KR", "orders"), ("", "KR", "stocks")):
                with self.assertRaises(ValueError):
                    toss_api.get_sector_members("tok", *args)
        call.assert_not_called()


class TossIdConfigTest(unittest.TestCase):
    def test_toss_id_kept_and_validated(self):
        raw = {"sectors": [{"label": "반도체", "proxy": "", "toss_id": "169"}, {"label": "직접만든"}], "assignments": []}
        config, error = sectors.validate_sector_map(raw)
        self.assertIsNone(error)
        self.assertEqual(config["sectors"][0], {"label": "반도체", "proxy": "", "toss_id": "169"})
        self.assertNotIn("toss_id", config["sectors"][1])  # 없으면 키 자체가 없다
        for bad in ("abc", "12345678901", "1/2"):
            _, error = sectors.validate_sector_map({"sectors": [{"label": "A", "toss_id": bad}], "assignments": []})
            self.assertIn("토스 섹터 ID", error, bad)

    def test_toss_id_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.UserStore(Path(tmp))
            config, _ = sectors.validate_sector_map({"sectors": [{"label": "반도체", "toss_id": "169"}], "assignments": []})
            sectors.save_sector_map(store, config)
            self.assertEqual(sectors.load_sector_map(store)["sectors"][0]["toss_id"], "169")


class SectorIndexTest(unittest.TestCase):
    def setUp(self):
        self.index = sectors.new_index(toss_sectors(), now=1000.0)

    def test_new_index_filters_bad_entries(self):
        bad = toss_sectors() + [{"sectorId": "x", "name": "나쁜ID", "depth": 0}, {"sectorId": "9", "name": "", "depth": 0},
                                 {"sectorId": "8", "name": "깊이이상", "depth": 7}, {"sectorId": "169", "name": "중복", "depth": 0}, "x"]
        index = sectors.new_index(bad, now=1.0)
        self.assertEqual([s["id"] for s in index["sectors"]], ["169", "171", "1", "711"])
        self.assertEqual(index["sectors"][1]["parent"], "169")
        self.assertIsNone(index["sectors"][0]["parent"])

    def test_scan_tasks_order_and_markets(self):
        tasks = sectors.scan_tasks(self.index, ["KR"])
        stocks = [t for t in tasks if t[0] == "stocks"]
        etfs = [t for t in tasks if t[0] == "etfs"]
        self.assertEqual([t[2] for t in stocks], ["169", "1"])  # 주식은 대분류만
        self.assertEqual([t[2] for t in etfs], ["1", "169", "171", "711"])  # ETF는 전체 섹터, 얕은 것부터
        self.assertEqual(tasks[:len(stocks)], stocks)  # 주식이 먼저
        self.assertTrue(all(t[1] == "KR" for t in tasks))
        self.assertEqual({t[1] for t in sectors.scan_tasks(self.index, ["KR", "US", "JP"])}, {"KR", "US"})

    def test_run_scan_builds_membership_and_progress(self):
        seen = []
        sectors.run_scan(self.index, ["KR", "US"], fake_members, on_progress=seen.append)
        stocks_kr = self.index["members"]["stocks"]["KR"]
        self.assertEqual(sorted(stocks_kr["005930"]), ["1", "169"])  # 여러 섹터
        self.assertEqual(self.index["members"]["stocks"]["US"]["NVDA"], ["169"])
        self.assertEqual(sorted(self.index["members"]["etfs"]["KR"]["069500"]), ["171", "711"])
        self.assertEqual(seen[-1]["done"], seen[-1]["total"])
        self.assertTrue(seen[-1]["stocks_done"] and seen[-1]["etfs_done"])
        self.assertEqual([p["done"] for p in seen], sorted(p["done"] for p in seen))  # 진행률은 줄어들지 않는다

    def test_run_scan_resumes_without_refetching(self):
        calls = []

        def counting(sid, market, kind):
            calls.append((kind, market, sid))
            return fake_members(sid, market, kind)
        sectors.run_scan(self.index, ["KR"], counting)
        first = len(calls)
        self.assertGreater(first, 0)
        sectors.run_scan(self.index, ["KR"], counting)
        self.assertEqual(len(calls), first, "이미 훑은 작업은 다시 받지 않는다")
        self.assertEqual(len(self.index["members"]["etfs"]["KR"]["069500"]), 2, "다시 돌려도 중복 추가가 없다")

    def test_run_scan_failure_saves_partial_and_can_resume(self):
        saved = []
        state = {"n": 0}

        def flaky(sid, market, kind):
            state["n"] += 1
            if state["n"] == 3:
                raise RuntimeError("boom")
            return fake_members(sid, market, kind)
        with self.assertRaises(RuntimeError):
            sectors.run_scan(self.index, ["KR"], flaky, save=lambda idx: saved.append(len(idx["scanned"])), save_every=100)
        self.assertEqual(saved, [2], "실패해도 그때까지 훑은 만큼은 저장")
        self.assertEqual(len(self.index["scanned"]), 2)
        sectors.run_scan(self.index, ["KR"], fake_members)
        self.assertTrue(sectors.index_progress(self.index, ["KR"])["etfs_done"])

    def test_progress_flags(self):
        self.assertEqual(sectors.index_progress(self.index, ["KR"])["done"], 0)
        self.assertFalse(sectors.index_progress(self.index, ["KR"])["stocks_done"])
        self.assertFalse(sectors.index_progress(sectors.new_index([]), ["KR"])["stocks_done"])  # 섹터가 없으면 "끝남"이 아니다

    def test_freshness(self):
        self.assertTrue(sectors.index_is_fresh(self.index, now=1000.0 + sectors.SECTOR_INDEX_TTL - 1))
        self.assertFalse(sectors.index_is_fresh(self.index, now=1000.0 + sectors.SECTOR_INDEX_TTL + 1))
        self.assertFalse(sectors.index_is_fresh(None))
        self.assertFalse(sectors.index_is_fresh(sectors.new_index([])))

    def test_index_round_trip_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.UserStore(Path(tmp))
            self.assertIsNone(sectors.load_sector_index(store))
            sectors.run_scan(self.index, ["KR"], fake_members)
            sectors.save_sector_index(store, self.index)
            self.assertEqual(sectors.load_sector_index(store), self.index)
            store.write_json(sectors.SECTOR_INDEX_FILE, {"sectors": [], "scanned": [], "members": {"stocks": {}}})
            self.assertIsNone(sectors.load_sector_index(store), "members 형식이 이상하면 버리고 새로 만든다")
            store.path(sectors.SECTOR_INDEX_FILE).write_text("{깨진", encoding="utf-8")
            self.assertIsNone(sectors.load_sector_index(store))

    def held(self):
        return [
            {"심볼": "005930", "종목": "삼성전자", "시장": "KR", "통화": "KRW"},
            {"심볼": "069500", "종목": "KODEX 200", "시장": "KR", "통화": "KRW"},
            {"심볼": "NVDA", "종목": "엔비디아", "시장": "US", "통화": "USD"},
            {"심볼": "TLT", "종목": "TLT", "통화": "USD"},  # 시장 값 없음 → 통화로 US 추정
        ]

    def test_suggestions_when_fully_scanned(self):
        sectors.run_scan(self.index, ["KR", "US"], fake_members)
        by = {s["symbol"]: s for s in sectors.suggest_sectors(self.held(), self.index)}
        self.assertEqual(by["005930"]["kind"], "stock")
        self.assertEqual({c["name"] for c in by["005930"]["candidates"]}, {"반도체", "운송"})
        self.assertEqual(by["069500"]["kind"], "etf")
        self.assertEqual([(c["name"], c["depth"]) for c in by["069500"]["candidates"]], [("반도체부품소재", 1), ("반도체 전공정 장비", 2)])
        self.assertEqual(by["NVDA"]["kind"], "stock")
        self.assertIsNone(by["TLT"]["kind"])
        self.assertFalse(by["TLT"]["pending"], "다 훑었는데 없으면 '분류 없음'이다")
        self.assertEqual(by["TLT"]["market"], "US")

    def test_unmatched_is_pending_until_scan_completes(self):
        by = {s["symbol"]: s for s in sectors.suggest_sectors(self.held(), self.index)}
        self.assertTrue(all(s["pending"] and s["kind"] is None for s in by.values()))
        self.assertEqual(by["005930"]["candidates"], [])

    def test_holding_market_fallback(self):
        self.assertEqual(sectors.holding_market({"시장": "kr"}), "KR")
        self.assertEqual(sectors.holding_market({"시장": "JP", "통화": "KRW"}), "KR")
        self.assertEqual(sectors.holding_market({"통화": "USD"}), "US")


class SectorAutoFillHttpTest(storage_http.HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

        def members(token, sector_id, market, kind):
            self.calls.append((kind, market, sector_id))
            return fake_members(sector_id, market, kind)
        patches = {
            "get_sectors": mock.Mock(side_effect=lambda token: toss_sectors()),
            "get_sector_members": mock.Mock(side_effect=members),
            "_spawn": lambda fn: fn(),  # 백그라운드 작업을 그 자리에서 실행
            "SECTOR_CALL_DELAY": 0,
        }
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server._sector_jobs.clear()

    def snapshot(self, items=None):
        items = items if items is not None else [
            {"symbol": "005930", "name": "삼성전자", "currency": "KRW", "marketCountry": "KR", "quantity": 1, "marketValue": {"amount": 600}},
            {"symbol": "069500", "name": "KODEX 200", "currency": "KRW", "marketCountry": "KR", "quantity": 1, "marketValue": {"amount": 300}},
            {"symbol": "NVDA", "name": "엔비디아", "currency": "USD", "marketCountry": "US", "quantity": 1, "marketValue": {"amount": 1}},
        ]
        return {"usd_krw": 1000.0, "cash_krw": 0.0, "cash_usd": 0.0, "items": items}

    def run_refresh(self, cookie, body=None, snap=None):
        with mock.patch.object(server, "fetch_account_snapshot", return_value=snap or self.snapshot()):
            return self.request("POST", "/api/sector-index/refresh", body or {}, cookie=cookie)

    def test_requires_login(self):
        self.assertEqual(self.request("POST", "/api/sector-index/refresh", {})[0], 401)
        self.assertEqual(self.request("GET", "/api/sector-suggestions")[0], 401)

    def test_refresh_builds_index_and_suggestions(self):
        cookie = self.login("key-a")
        status, payload, _ = self.run_refresh(cookie)
        self.assertEqual(status, 200, payload)
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()):
            status, out, _ = self.request("GET", "/api/sector-suggestions", cookie=cookie)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["status"]["state"], "done")
        self.assertTrue(out["status"]["stocks_done"] and out["status"]["etfs_done"])
        self.assertTrue(out["status"]["fresh"])
        by = {s["symbol"]: s for s in out["suggestions"]}
        self.assertEqual(by["005930"]["kind"], "stock")
        self.assertEqual(by["069500"]["kind"], "etf")
        self.assertEqual(by["NVDA"]["candidates"][0]["name"], "반도체")
        # 보유 시장(KR, US)만 훑는다: 주식 대분류 2개 × 2시장 + ETF 4개 섹터 × 2시장
        self.assertEqual(len(self.calls), 2 * 2 + 4 * 2)

    def test_fresh_index_is_not_rebuilt_but_force_resets(self):
        cookie = self.login("key-a")
        self.run_refresh(cookie)
        first = len(self.calls)
        self.run_refresh(cookie)
        self.assertEqual(len(self.calls), first, "하루 안에는 다시 받지 않는다")
        self.assertEqual(server.get_sectors.call_count, 1)
        self.run_refresh(cookie, {"force": True})
        self.assertEqual(len(self.calls), first * 2)
        self.assertEqual(server.get_sectors.call_count, 2)

    def test_only_held_markets_are_scanned(self):
        cookie = self.login("key-a")
        only_kr = self.snapshot([{"symbol": "005930", "name": "삼성전자", "currency": "KRW", "marketCountry": "KR",
                                  "quantity": 1, "marketValue": {"amount": 600}}])
        self.run_refresh(cookie, snap=only_kr)
        self.assertTrue(all(c[1] == "KR" for c in self.calls))

    def test_no_holdings_is_rejected(self):
        cookie = self.login("key-a")
        status, payload, _ = self.run_refresh(cookie, snap=self.snapshot([]))
        self.assertEqual(status, 400)
        self.assertIn("보유 종목이 없어", payload["error"])
        self.assertEqual(self.calls, [])

    def test_running_job_is_not_started_twice(self):
        cookie = self.login("key-a")
        started = []
        user_id = next(iter(server._sessions.values()))["user_id"]
        server._sector_jobs[user_id] = {"state": "running", "message": ""}
        with mock.patch.object(server, "_spawn", lambda fn: started.append(fn)):
            status, payload, _ = self.run_refresh(cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "running")
        self.assertEqual(started, [])

    def test_failure_becomes_error_state_and_resumes(self):
        cookie = self.login("key-a")
        fail = {"on": True}

        def flaky(token, sector_id, market, kind):
            if fail["on"] and len(self.calls) >= 3:
                resp = mock.Mock(status_code=500)
                raise server.requests.HTTPError(response=resp)
            self.calls.append((kind, market, sector_id))
            return fake_members(sector_id, market, kind)
        with mock.patch.object(server, "get_sector_members", flaky):
            self.run_refresh(cookie)
            with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()):
                out = self.request("GET", "/api/sector-suggestions", cookie=cookie)[1]
            self.assertEqual(out["status"]["state"], "error")
            self.assertIn("이어서", out["status"]["message"])
            self.assertEqual(out["status"]["done"], 3)
            fail["on"] = False
            self.run_refresh(cookie)
            with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()):
                out = self.request("GET", "/api/sector-suggestions", cookie=cookie)[1]
        self.assertEqual(out["status"]["state"], "done")
        self.assertTrue(out["status"]["etfs_done"])
        self.assertEqual(len(self.calls), 12, "이어서 하므로 같은 작업을 두 번 받지 않는다")

    def test_sector_list_failure_leaves_no_stuck_job(self):
        cookie = self.login("key-a")
        with mock.patch.object(server, "get_sectors", mock.Mock(return_value=[])):
            status, payload, _ = self.run_refresh(cookie)
        self.assertEqual(status, 502)
        self.assertEqual(server._sector_jobs, {})
        status, _, _ = self.run_refresh(cookie)
        self.assertEqual(status, 200, "실패 뒤에도 다시 시작할 수 있다")

    def test_index_is_per_user(self):
        a, b = self.login("key-a"), self.login("key-b")
        self.run_refresh(a)
        with mock.patch.object(server, "fetch_account_snapshot", return_value=self.snapshot()):
            out = self.request("GET", "/api/sector-suggestions", cookie=b)[1]
        self.assertEqual(out["suggestions"], [])
        self.assertEqual(out["status"]["state"], "idle")


if __name__ == "__main__":
    unittest.main()
