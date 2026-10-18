"""보도 제목 주제 모으기(causes.py): 사전 낱말 분류, 많은 날에 나온 주제 순, 같은 날 수는 하락폭 가중치, 가장 크게 빠진 날의 예시 보도."""
from __future__ import annotations

import unittest

import causes


def day(date, pct, titles):
    heads = [{"title": t, "source": "S", "url": "https://example.com/" + t[:3], "published": date} for t in titles]
    return {"date": date, "change_pct": pct, "headlines": heads, "pool": heads, "themes": causes.themes_in(titles)}


class ThemesInTest(unittest.TestCase):
    def test_keywords_map_to_themes_case_insensitively(self):
        got = causes.themes_in(["코스피 한때 6% 급락…매도 사이드카 발동", "이란 사태 공포", "FOMC 앞두고 긴장", "MSCI 강등 경고"])
        for theme in ("증시 급락·투매", "지정학·전쟁", "금리·연준(통화정책)", "지수·신용등급 이벤트"):
            self.assertIn(theme, got)

    def test_unrelated_titles_give_no_theme(self):
        self.assertEqual(causes.themes_in(["오늘의 날씨", "", None]), [])


class RankThemesTest(unittest.TestCase):
    def setUp(self):
        self.days = [day("2026-01-30", -10.1, ["금값 돌연 급락 재무장관 긴급 기자회견", "은값 폭락"]),
                     day("2026-03-03", -4.4, ["코스피 급락 사이드카 발동", "이란 사태 공포"]),
                     day("2026-03-19", -4.1, ["안전자산마저 급락 이란 전쟁", "금까지 판다"])]

    def test_more_days_first_then_bigger_drops(self):
        ranked = causes.rank_themes(self.days)
        self.assertEqual([r["theme"] for r in ranked[:2]], ["증시 급락·투매", "지정학·전쟁"])         # 둘 다 2일, 하락폭 합이 큰 쪽(01-30 포함)이 먼저
        self.assertEqual(ranked[0]["days"], ["2026-01-30", "2026-03-03"])
        self.assertEqual(ranked[1]["days"], ["2026-03-03", "2026-03-19"])
        self.assertEqual(ranked[1]["biggest_day"], "2026-03-03")
        self.assertIn("이란", ranked[1]["example"]["title"])
        names = [r["theme"] for r in ranked]
        self.assertIn("당국 발표·정책 대응", names)                                                   # 1일뿐이어도 가장 큰 하락일의 주제는 남는다
        self.assertLessEqual(len(ranked), causes.MAX_THEMES)

    def test_example_comes_from_the_biggest_day_and_matches_the_theme(self):
        ranked = {r["theme"]: r for r in causes.rank_themes(self.days, limit=12)}
        self.assertEqual(ranked["증시 급락·투매"]["biggest_day"], "2026-01-30")
        self.assertIn("폭락", ranked["증시 급락·투매"]["example"]["title"])

    def test_no_days_or_no_themes(self):
        self.assertEqual(causes.rank_themes([]), [])
        self.assertEqual(causes.rank_themes([day("2026-01-01", -3.0, ["평범한 제목"])]), [])


if __name__ == "__main__":
    unittest.main()
