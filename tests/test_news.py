"""뉴스 헤드라인 수집: 파싱, 최근 7일 필터, 중복 제거, 정렬, 링크 안전성, 캐시, 실패 격리. 네트워크는 쓰지 않는다."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import requests

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import news

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def ts(days_ago: float) -> float:
    return (NOW - timedelta(days=days_ago)).timestamp()


def rfc822(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S GMT")


def rss(*items) -> str:
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{d}</pubDate><source>{s}</source></item>" for t, l, d, s in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'


class FakeResp:
    def __init__(self, payload=None, text="", status=200):
        self.payload, self.text, self.status = payload, text, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")


class FakeGet:
    """URL에 따라 야후/구글 응답을 돌려주고 호출 내용을 기록한다."""

    def __init__(self, yahoo=None, google="", fail=()):
        self.yahoo, self.google, self.fail, self.calls = yahoo, google, set(fail), []

    def __call__(self, url, params=None, headers=None, timeout=None):
        which = "yahoo" if "yahoo" in url else "google"
        self.calls.append((which, dict(params or {})))
        if which in self.fail:
            raise requests.ConnectionError("down")
        return FakeResp(payload=self.yahoo, text=self.google)


class NewsTestCase(unittest.TestCase):
    def setUp(self):
        news.clear_cache()


class ParsingTest(NewsTestCase):
    def test_yahoo_items_keep_only_safe_complete_entries(self):
        payload = {"news": [
            {"title": "<b>Strategy</b> stock &amp; bitcoin", "publisher": "TIKR", "link": "https://a.example/1",
             "providerPublishTime": ts(1), "relatedTickers": ["mstr", "BTC-USD"]},
            {"title": "나쁜 링크", "publisher": "X", "link": "javascript:alert(1)", "providerPublishTime": ts(1)},
            {"title": "시간 없음", "publisher": "X", "link": "https://a.example/2"},
            {"title": "", "publisher": "X", "link": "https://a.example/3", "providerPublishTime": ts(1)},
            "문자열", None,
        ]}
        items = news.parse_yahoo(payload, "MSTR")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Strategy stock & bitcoin")   # 태그 제거, 엔티티 복원
        self.assertTrue(items[0]["related"])                             # 대소문자 무시 티커 연관
        self.assertEqual(news.parse_yahoo("x", "MSTR"), [])
        self.assertEqual(news.parse_yahoo({"news": "x"}, "MSTR"), [])

    def test_google_rss_strips_source_suffix_and_skips_bad_items(self):
        xml = rss(("삼성전자 주가 급락 이유는 - 연합뉴스", "https://n.example/1", rfc822(1), "연합뉴스"),
                  ("링크 불량 - 매체", "ftp://n.example/2", rfc822(1), "매체"),
                  ("날짜 불량 - 매체", "https://n.example/3", "어제쯤", "매체"))
        items = news.parse_google_rss(xml)
        self.assertEqual([(i["title"], i["source"]) for i in items], [("삼성전자 주가 급락 이유는", "연합뉴스")])
        self.assertEqual(news.parse_google_rss("<rss><깨진"), [])

    def test_google_publisher_suffix_is_removed_even_when_names_differ_or_repeat(self):
        xml = rss(("내다 판 외국인, 국내 ETF는 계속 샀다 - 조선비즈", "https://n.example/1", rfc822(1), "Chosunbiz"),            # 표기가 다름
                  ("폭락장 이긴 ETF투자왕들 '이것' - 머니투데이 - 머니투데이", "https://n.example/2", rfc822(1), "머니투데이"),   # 이름이 두 번
                  ("Fed holds rates - what it means for you - Reuters", "https://n.example/3", rfc822(1), "Reuters"),          # 제목 속 대시는 유지
                  ("매체 표기 없는 제목", "https://n.example/4", rfc822(1), "매체"))
        titles = [i["title"] for i in news.parse_google_rss(xml)]
        self.assertEqual(titles, ["내다 판 외국인, 국내 ETF는 계속 샀다", "폭락장 이긴 ETF투자왕들 '이것'",
                                  "Fed holds rates - what it means for you", "매체 표기 없는 제목"])

    def test_yahoo_titles_are_not_cut_at_dashes(self):
        payload = {"news": [{"title": "Gold - a safe haven - again", "publisher": "P", "link": "https://y.example/1", "providerPublishTime": ts(1)}]}
        self.assertEqual(news.parse_yahoo(payload, "GLDM")[0]["title"], "Gold - a safe haven - again")

    def test_title_cleaning_and_truncation(self):
        self.assertEqual(news.clean_title("a\x00b\n\t c"), "a b c")
        long = news.clean_title("가" * 500)
        self.assertEqual(len(long), news.TITLE_MAX)
        self.assertTrue(long.endswith("…"))

    def test_safe_url_allows_only_http_and_https(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.example", "ftp://x.example", "https://a b.example", "", None):
            self.assertIsNone(news.safe_url(bad), bad)
        self.assertEqual(news.safe_url(" https://ok.example/path?q=1 "), "https://ok.example/path?q=1")


class SelectionTest(NewsTestCase):
    def item(self, title, days, related=False):
        return {"title": title, "source": "S", "url": "https://x.example/" + title, "related": related, "published": NOW - timedelta(days=days)}

    def test_old_future_and_duplicate_titles_are_dropped(self):
        items = [self.item("최근 기사", 1), self.item("8일 전", 8), self.item("미래", -3), self.item("최근-기사!", 2), self.item("다른 기사", 3)]
        titles = [x["title"] for x in news.select_headlines(items, NOW)]
        self.assertEqual(titles, ["최근 기사", "다른 기사"])  # '최근-기사!'는 정규화하면 같은 제목

    def test_relevant_items_come_first_then_newest_and_limit(self):
        # '연관'(티커 연관 또는 이름이 제목에 있음)이 한 묶음으로 앞에 오고, 각 묶음 안에서는 최신순
        items = [self.item("무관 최신", 0.5), self.item("스트래티지 소식", 3), self.item("연관 티커 기사", 5, related=True),
                 self.item("무관 두번째", 1), self.item("무관 세번째", 2)]
        titles = [x["title"] for x in news.select_headlines(items, NOW, name="스트래티지")]
        self.assertEqual(titles, ["스트래티지 소식", "연관 티커 기사", "무관 최신"])
        self.assertEqual(len(titles), news.HEADLINES_PER_SYMBOL)


class HeadlinesForTest(NewsTestCase):
    def yahoo_payload(self):
        return {"news": [{"title": "Strategy falls", "publisher": "TIKR", "link": "https://y.example/1",
                          "providerPublishTime": ts(1), "relatedTickers": ["MSTR"]}]}

    def test_both_sources_are_used_for_a_named_stock(self):
        get = FakeGet(self.yahoo_payload(), rss(("스트래티지 급락 - 매체", "https://g.example/1", rfc822(0.5), "매체")))
        out = news.headlines_for("MSTR", "스트래티지", now=NOW, get=get)
        self.assertFalse(out["error"])
        self.assertEqual({h["title"] for h in out["headlines"]}, {"Strategy falls", "스트래티지 급락"})
        self.assertEqual(sorted(c[0] for c in get.calls), ["google", "yahoo"])
        google_query = next(p["q"] for w, p in get.calls if w == "google")
        self.assertEqual(google_query, "스트래티지 주가 when:7d")           # 조회어는 이름뿐, 계좌 정보 없음
        self.assertEqual(out["headlines"][0]["published"][:4], "2026")

    def test_ticker_like_names_skip_google_and_korean_stocks_can_skip_yahoo(self):
        get = FakeGet(self.yahoo_payload(), rss())
        news.headlines_for("GLDM", "GLDM", now=NOW, get=get)
        self.assertEqual([c[0] for c in get.calls], ["yahoo"])
        get = FakeGet(self.yahoo_payload(), rss())
        news.headlines_for("005930", "삼성전자", use_yahoo=False, now=NOW, get=get)
        self.assertEqual([c[0] for c in get.calls], ["google"])

    def test_error_flag_only_when_every_source_failed(self):
        both_down = FakeGet(fail=("yahoo", "google"))
        self.assertTrue(news.headlines_for("MSTR", "스트래티지", now=NOW, get=both_down)["error"])
        news.clear_cache()
        one_down = FakeGet(self.yahoo_payload(), fail=("google",))
        out = news.headlines_for("MSTR", "스트래티지", now=NOW, get=one_down)
        self.assertFalse(out["error"])
        self.assertEqual(len(out["headlines"]), 1)
        news.clear_cache()
        nothing_to_ask = news.headlines_for("GLDM", "GLDM", use_yahoo=False, now=NOW, get=FakeGet())
        self.assertEqual((nothing_to_ask["headlines"], nothing_to_ask["error"]), ([], False))

    def test_bad_http_status_counts_as_failure(self):
        def get(url, **kw):
            return FakeResp(status=503)
        self.assertTrue(news.headlines_for("MSTR", "GLDM", now=NOW, get=get)["error"])

    def test_results_are_cached_per_source_and_query(self):
        get = FakeGet(self.yahoo_payload(), rss())
        news.headlines_for("MSTR", "스트래티지", now=NOW, get=get)
        first = len(get.calls)
        news.headlines_for("MSTR", "스트래티지", now=NOW, get=get)
        self.assertEqual(len(get.calls), first)
        news.clear_cache()
        news.headlines_for("MSTR", "스트래티지", now=NOW, get=get)
        self.assertEqual(len(get.calls), first * 2)

    def test_cache_returns_copies(self):
        get = FakeGet(self.yahoo_payload())
        a = news.fetch_yahoo_news("MSTR", get)
        a[0]["title"] = "변조"
        self.assertEqual(news.fetch_yahoo_news("MSTR", get)[0]["title"], "Strategy falls")


class ManyAndWeeklyTest(NewsTestCase):
    def test_one_failing_symbol_does_not_affect_others(self):
        def get(url, params=None, headers=None, timeout=None):
            if params and params.get("q") == "BAD":
                raise requests.ConnectionError("down")
            return FakeResp(payload={"news": [{"title": f"{params['q']} 기사", "publisher": "P", "link": "https://y.example/" + params["q"],
                                               "providerPublishTime": ts(1), "relatedTickers": [params["q"]]}]})
        out = news.headlines_for_many([{"symbol": "AAA", "name": "AAA"}, {"symbol": "BAD", "name": "BAD"}, {"symbol": "CCC", "name": "CCC"}],
                                      now=NOW, get=get)
        self.assertEqual(set(out), {"AAA", "BAD", "CCC"})
        self.assertTrue(out["BAD"]["error"])
        self.assertEqual(out["AAA"]["headlines"][0]["title"], "AAA 기사")
        self.assertFalse(out["CCC"]["error"])

    def test_weekly_change_pct(self):
        closes = [100, 101, 102, 103, 104, 105, 110]
        self.assertAlmostEqual(news.weekly_change_pct(closes), (110 / 101 - 1) * 100)   # 5거래일 전 종가 대비
        self.assertIsNone(news.weekly_change_pct([1, 2, 3]))
        self.assertIsNone(news.weekly_change_pct([0, 1, 2, 3, 4, 5]))


if __name__ == "__main__":
    unittest.main()
