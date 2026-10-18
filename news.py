"""종목별 최근 7일 뉴스 헤드라인 수집(야후 파이낸스 검색 + 구글 뉴스 RSS). 제목·출처·날짜·링크만 다룬다.

둘 다 공식 API가 아니라 공개 검색 결과를 읽는 비공식 방식이다 — 형식이 바뀌거나 막히면 그 종목의 뉴스만 "불러오지 못함"으로
표시하고 나머지는 계속한다. 조회어에는 종목 이름·코드만 쓰고 계좌 정보(수량·금액·계좌번호)는 절대 넣지 않는다.
보도 제목은 참고용이며 가격 변동의 원인으로 확정하지 않는다(그런 문장은 쓰지 않는다).
"""
from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from common import parallel_map

YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
USER_AGENT = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT = 10
NEWS_CACHE_TTL = 30 * 60
MAX_AGE_DAYS = 7
HEADLINES_PER_SYMBOL = 3
FETCH_PER_SOURCE = 8
TITLE_MAX = 140
SOURCE_MAX = 30
NEWS_WORKERS = 4

_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def safe_url(url) -> str | None:
    """링크는 http(s)만 허용한다(javascript: 등으로 화면이 오염되지 않게)."""
    url = str(url or "").strip()
    return url if re.match(r"^https?://[^\s<>\"']+$", url, re.I) else None


def clean_title(title, source: str = "", strip_publisher: bool = False) -> str:
    """태그·제어문자 제거, 길이 제한. 구글 뉴스 제목은 항상 끝에 " - 매체"가 붙으므로(strip_publisher) 마지막 조각을 떼고,
    매체가 제목에 같은 이름을 또 붙인 경우도 한 번 더 뗀다(RSS 출처 필드와 표기가 달라도 동작: Chosunbiz ↔ 조선비즈)."""
    text = html.unescape(re.sub(r"<[^>]+>", "", str(title or "")))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if strip_publisher and " - " in text:
        head, tail = text.rsplit(" - ", 1)
        if head.strip() and len(tail) <= SOURCE_MAX + 10:
            text = head.strip()
        if source and text.endswith(f" - {source}"):
            text = text[: -len(source) - 3].rstrip()
    elif source and text.endswith(f" - {source}"):
        text = text[: -len(source) - 3].rstrip()
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1].rstrip() + "…"


def _normalized(title: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", title.lower())


def parse_yahoo(payload, symbol: str) -> list[dict]:
    items = []
    for n in (payload or {}).get("news", []) if isinstance(payload, dict) else []:
        if not isinstance(n, dict) or not n.get("providerPublishTime"):
            continue
        url, source = safe_url(n.get("link")), str(n.get("publisher") or "")[:SOURCE_MAX]
        title = clean_title(n.get("title"), source)
        if not url or not title:
            continue
        related = symbol.upper() in {str(x).upper() for x in (n.get("relatedTickers") or [])}
        items.append({"title": title, "source": source, "url": url, "related": related,
                      "published": datetime.fromtimestamp(float(n["providerPublishTime"]), timezone.utc)})
    return items


def parse_google_rss(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for it in root.findall(".//item"):
        source = (it.findtext("source") or "")[:SOURCE_MAX]
        url, title = safe_url(it.findtext("link")), clean_title(it.findtext("title"), source, strip_publisher=True)
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if url and title:
            items.append({"title": title, "source": source, "url": url, "related": False, "published": published})
    return items


def _cached(key: tuple[str, str], fetch):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < NEWS_CACHE_TTL:
            return [dict(x) for x in hit[1]]
    items = fetch()
    with _cache_lock:
        _cache[key] = (now, items)
    return [dict(x) for x in items]


def fetch_yahoo_news(symbol: str, get=requests.get) -> list[dict]:
    def fetch():
        resp = get(YAHOO_SEARCH_URL, params={"q": symbol, "newsCount": FETCH_PER_SOURCE, "quotesCount": 0},
                   headers=USER_AGENT, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return parse_yahoo(resp.json(), symbol)
    return _cached(("yahoo", symbol.upper()), fetch)


def fetch_google_news(query: str, get=requests.get) -> list[dict]:
    def fetch():
        resp = get(GOOGLE_NEWS_RSS_URL, params={"q": f"{query} when:{MAX_AGE_DAYS}d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
                   headers=USER_AGENT, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return parse_google_rss(resp.text)[:FETCH_PER_SOURCE]
    return _cached(("google", query), fetch)


def _looks_like_ticker(name: str, symbol: str) -> bool:
    """이름이 사실상 티커(예: GLDM, JEPQ)면 구글 검색어로 쓰지 않는다 — 무관한 기사가 섞인다."""
    return not name or name.upper() == symbol.upper() or bool(re.fullmatch(r"[A-Za-z0-9.\-]{1,6}", name))


def select_headlines(items: list[dict], now: datetime, name: str = "") -> list[dict]:
    """최근 7일 기사만, 제목 중복 제거, 종목 연관 기사와 이름이 제목에 든 기사를 앞에 두고 최신순, 종목당 3건."""
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    seen, picked = set(), []
    for item in items:
        key = _normalized(item["title"])
        if item["published"] < cutoff or item["published"] > now + timedelta(days=1) or not key or key in seen:
            continue
        seen.add(key)
        picked.append(item)
    needle = _normalized(name)
    picked.sort(key=lambda x: (not (x["related"] or (needle and needle in _normalized(x["title"]))), -x["published"].timestamp()))
    return picked[:HEADLINES_PER_SYMBOL]


def headlines_for(symbol: str, name: str, *, use_yahoo: bool = True, now: datetime | None = None, get=requests.get) -> dict:
    """한 종목의 헤드라인. 모든 소스가 실패했을 때만 error=True(일부만 실패하면 받은 것만 보여 준다)."""
    now = now or datetime.now(timezone.utc)
    sources = []
    if use_yahoo:
        sources.append(lambda: fetch_yahoo_news(symbol, get))
    if not _looks_like_ticker(name, symbol):
        sources.append(lambda: fetch_google_news(f"{name} 주가", get))
    items, failures = [], 0
    for fetch in sources:
        try:
            items += fetch()
        except (requests.RequestException, ValueError):
            failures += 1
    chosen = select_headlines(items, now, name)
    return {"headlines": [{"title": x["title"], "source": x["source"], "url": x["url"],
                           "published": x["published"].astimezone(timezone.utc).strftime("%Y-%m-%d")} for x in chosen],
            "error": bool(sources) and failures == len(sources)}


def headlines_for_many(entries: list[dict], now: datetime | None = None, get=requests.get) -> dict[str, dict]:
    """entries: [{"symbol", "name", "use_yahoo"}]. 종목마다 독립적으로 조회한다(한 종목의 실패가 다른 종목에 번지지 않는다)."""
    def one(entry):
        return entry["symbol"], headlines_for(entry["symbol"], entry.get("name") or "", use_yahoo=entry.get("use_yahoo", True),
                                              now=now, get=get)
    return dict(parallel_map(one, entries, NEWS_WORKERS))


RANGE_PAD_DAYS = 1           # 날짜 검색은 그날의 앞뒤로 이만큼 넓혀 본다(시간대 차이와 다음 날 보도)
RANGE_HEADLINES = 2          # 검색어 하나당 가져오는 기사 수


def fetch_google_news_around(query: str, day: date, get=requests.get) -> list[dict]:
    """구글 뉴스 검색을 day 앞뒤 며칠로 한정해 가져온다(그 밖 날짜 기사는 버린다). 결과는 가까운 날짜 순."""
    start, end = day - timedelta(days=RANGE_PAD_DAYS), day + timedelta(days=RANGE_PAD_DAYS)

    def fetch():
        resp = get(GOOGLE_NEWS_RSS_URL, params={"q": f"{query} after:{start.isoformat()} before:{(end + timedelta(days=1)).isoformat()}",
                                                "hl": "ko", "gl": "KR", "ceid": "KR:ko"}, headers=USER_AGENT, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return parse_google_rss(resp.text)[:FETCH_PER_SOURCE]
    items = [x for x in _cached(("google_range", f"{query}|{start}|{end}"), fetch) if start <= x["published"].astimezone(timezone.utc).date() <= end]
    return sorted(items, key=lambda x: abs((x["published"].astimezone(timezone.utc).date() - day).days))


def name_query(name: str, symbol: str, suffix: str = "급락") -> str | None:
    """종목 이름으로 만든 검색어. 이름이 사실상 티커면 None(무관한 기사가 섞이므로 쓰지 않는다)."""
    return None if _looks_like_ticker(name, symbol) else f"{name} {suffix}"


def headlines_around(queries: list[str], day: date, limit: int = HEADLINES_PER_SYMBOL, get=requests.get, per_query: int = RANGE_HEADLINES) -> list[dict]:
    """그 날짜 앞뒤의 보도: 검색어마다 가까운 기사를 골라 제목 중복 없이 모은다. 검색이 전부 실패하면 빈 목록(호출하는 쪽이 '못 찾음'으로 알린다).
    반환: [{"title","source","url","published"(YYYY-MM-DD),"query"}]"""
    picked, seen = [], set()
    for query in queries:
        try:
            items = fetch_google_news_around(query, day, get)
        except (requests.RequestException, ValueError):
            continue
        for item in items[:per_query]:
            key = _normalized(item["title"])
            if key and key not in seen and len(picked) < limit:
                seen.add(key)
                picked.append({"title": item["title"], "source": item["source"], "url": item["url"],
                               "published": item["published"].astimezone(timezone.utc).strftime("%Y-%m-%d"), "query": query})
    return picked


def weekly_change_pct(closes: list[float], trading_days: int = 5) -> float | None:
    """최근 N거래일 등락률(%). closes는 오래된 것 → 최신 순. 데이터가 모자라면 None."""
    if len(closes) <= trading_days or not closes[-trading_days - 1]:
        return None
    return (closes[-1] / closes[-trading_days - 1] - 1) * 100
