"""웹 검색과 페이지 읽기(에이전트의 `web_search`·`read_page` 도구). 읽기 전용이고 계좌 정보는 보내지 않는다.

- 검색어는 종목·주제 낱말만 남긴다(금액·수량·긴 숫자·이메일·주소 제거, 길이 제한). 소스는 DuckDuckGo HTML, 실패하면 구글 뉴스 RSS.
- 페이지는 검색 결과로 나온 주소만 읽는다(모델이 임의 주소로 정보를 보내지 못하게). http/https만, 사설·루프백·링크로컬 주소와
  내부 이름은 막고 리다이렉트마다 다시 검사한다. 크기·시간을 제한하고 글자만 뽑는다(script·style 무시).
- 가져온 글은 참고용 외부 글이다. 그 안의 지시는 따르지 않는다(도구 결과는 데이터).
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

import requests

DDG_URL = "https://html.duckduckgo.com/html/"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
USER_AGENT = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = (5, 10)
MAX_BYTES = 400_000
MAX_REDIRECTS = 3
MAX_RESULTS = 6
QUERY_MAX = 80
QUERY_WORDS_MAX = 10
TEXT_MAX = 2500
SNIPPET_MAX = 200
TITLE_MAX = 120
SEEN_TTL = 3600
SEEN_MAX = 200
PERSONAL_WORDS = {"내", "제", "나의", "우리", "계좌", "보유", "잔고", "비밀번호", "토큰", "앱키", "시크릿"}
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")

_seen: dict[str, float] = {}
_lock = threading.Lock()


class WebError(Exception):
    """사용자에게 보여 줄 수 있는 짧은 실패 이유."""


def clear_seen() -> None:
    with _lock:
        _seen.clear()


# ---------- 검색어 정화 ----------
def sanitize_query(text) -> str:
    """종목명·주제 낱말만 남긴다: 이메일·주소·금액(단위 붙은 숫자, 쉼표 숫자)·긴 숫자·개인 낱말 제거. 연도(1990~2039)는 남긴다."""
    t = str(text or "")
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)
    t = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", " ", t)
    t = re.sub(r"[$₩¥€]\s*[\d,.]+", " ", t)
    t = re.sub(r"\d[\d,.]*\s*(?:원|달러|불|엔|만|억|조|천|주|개|%|퍼센트|usd|krw|jpy|shares?)(?![가-힣A-Za-z])", " ", t, flags=re.I)  # 뒤에 글자가 이어지면 단위가 아니라 낱말의 일부(11100000001 엔비디아)
    t = re.sub(r"\d{1,3}(?:,\d{3})+", " ", t)
    t = re.sub(r"\b(?!(?:19[9]\d|20[0-3]\d)\b)\d{3,}\b", " ", t)
    t = re.sub(r"[^\w\s&.\-]", " ", t, flags=re.U)
    words = [w for w in t.split() if w not in PERSONAL_WORDS]
    return " ".join(words[:QUERY_WORDS_MAX])[:QUERY_MAX].strip()


# ---------- 주소 검사 ----------
def _blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return True
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not addr.is_global or addr.is_multicast


def check_url(url: str) -> str:
    """읽어도 되는 주소인지 검사해 정리된 주소를 돌려준다(아니면 WebError). 호스트 이름이 가리키는 모든 IP를 확인한다."""
    url = str(url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebError("http/https 주소만 읽을 수 있습니다.")
    if parts.username or parts.password:
        raise WebError("계정 정보가 들어간 주소는 읽지 않습니다.")
    host = parts.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES) or ("." not in host and ":" not in host):
        raise WebError("내부 주소는 읽지 않습니다.")
    try:
        port = parts.port
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError, OSError):
        raise WebError("주소를 찾지 못했습니다.")
    if not infos or any(_blocked_ip(i[4][0]) for i in infos):
        raise WebError("내부·사설 주소는 읽지 않습니다.")
    return url


def _remember(urls) -> None:
    now = time.time()
    with _lock:
        for u in urls:
            _seen[u] = now
        for old in [k for k, v in _seen.items() if now - v > SEEN_TTL]:
            del _seen[old]
        while len(_seen) > SEEN_MAX:
            del _seen[min(_seen, key=_seen.get)]


def _was_seen(url: str) -> bool:
    with _lock:
        t = _seen.get(url)
    return t is not None and time.time() - t <= SEEN_TTL


# ---------- 검색 ----------
def _clean(text, limit: int) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _public_http(url) -> str | None:
    url = str(url or "").strip()
    return url if re.match(r"^https?://[^\s<>\"']+$", url, re.I) else None


def _ddg_target(href: str) -> str:
    """DuckDuckGo 결과의 //duckduckgo.com/l/?uddg=<원주소> 형태에서 원주소를 꺼낸다."""
    if "uddg=" in href:
        q = parse_qs(urlsplit(href if "//" in href else "//x" + href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href


class _DdgParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._field = None
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._cur = {"title": "", "snippet": "", "url": _ddg_target(a.get("href") or "")}
            self.results.append(self._cur)
            self._field = "title"
        elif tag in ("a", "div") and "result__snippet" in cls and self._cur is not None:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "div") and self._field:
            self._field = None

    def handle_data(self, data):
        if self._field and self._cur is not None:
            self._cur[self._field] += data


def _search_ddg(query: str, get) -> list[dict]:
    resp = get(DDG_URL, params={"q": query}, headers=USER_AGENT, timeout=TIMEOUT)
    resp.raise_for_status()
    p = _DdgParser()
    p.feed(resp.text[:MAX_BYTES])
    return [{"title": r["title"], "snippet": r["snippet"], "url": r["url"], "source": urlsplit(r["url"]).hostname or ""} for r in p.results]


def _search_news(query: str, get) -> list[dict]:
    resp = get(GOOGLE_NEWS_RSS_URL, params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}, headers=USER_AGENT, timeout=TIMEOUT)
    resp.raise_for_status()
    out = []
    for item in ET.fromstring(resp.content[:MAX_BYTES]).iter("item"):
        src = item.find("source")
        out.append({"title": item.findtext("title") or "", "snippet": "", "url": item.findtext("link") or "", "source": (src.text if src is not None else "") or ""})
    return out


def search(query, get=None) -> dict:
    """검색 결과 {query, results:[{title,snippet,url,source}], error}. 네트워크 실패는 예외 대신 error로 알린다. 나온 주소는 read_page가 읽을 수 있게 기억한다."""
    q = sanitize_query(query)
    if not q:
        return {"query": "", "results": [], "error": "검색어에서 종목·주제 낱말을 찾지 못했습니다(금액·수량·계좌 정보는 검색어에 넣지 않습니다)."}
    get = get or requests.get
    results, failed = [], 0
    for fetch in (_search_ddg, _search_news):
        try:
            results = fetch(q, get)
        except (requests.RequestException, ET.ParseError, ValueError):
            failed += 1
            continue
        results = [{"title": _clean(r["title"], TITLE_MAX), "snippet": _clean(r["snippet"], SNIPPET_MAX), "url": u, "source": _clean(r["source"], 40)}
                   for r in results if (u := _public_http(r["url"])) and _clean(r["title"], TITLE_MAX)][:MAX_RESULTS]
        if results:
            break
    if not results:
        return {"query": q, "results": [], "error": "검색에 실패했습니다." if failed == 2 else None}
    _remember(r["url"] for r in results)
    return {"query": q, "results": results, "error": None}


# ---------- 페이지 읽기 ----------
class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "head", "svg", "iframe", "form"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def extract_text(markup: str) -> tuple[str, str]:
    """HTML → (제목, 본문 글자). script·style·head 등은 버리고 공백을 정리한다."""
    p = _TextParser()
    p.feed(markup)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in "".join(p.parts).split("\n")]
    text = "\n".join(ln for ln in lines if len(ln) >= 2)
    return _clean(p.title, TITLE_MAX), re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)[:TEXT_MAX].strip()


def read_page(url, get=None) -> dict:
    """검색 결과로 나온 주소의 본문 글자 {url, title, text, error}. 리다이렉트마다 주소를 다시 검사한다."""
    get = get or requests.get
    url = str(url or "").strip()
    if not _was_seen(url):
        return {"url": url[:300], "title": "", "text": "", "error": "검색 결과에 나온 주소만 읽을 수 있습니다. 먼저 web_search로 찾아 주세요."}
    try:
        current = check_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            resp = get(current, headers=USER_AGENT, timeout=TIMEOUT, allow_redirects=False, stream=True)
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    current = check_url(urljoin(current, resp.headers["Location"]))
                    continue
                resp.raise_for_status()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and not any(k in ctype for k in ("text/html", "text/plain", "application/xhtml")):
                    raise WebError("글자로 읽을 수 없는 형식입니다.")
                raw = b""
                for chunk in resp.iter_content(16384):
                    raw += chunk
                    if len(raw) >= MAX_BYTES:
                        break
            finally:
                resp.close()
            encoding = resp.encoding if resp.encoding and "charset" in ctype else "utf-8"
            markup = raw[:MAX_BYTES].decode(encoding or "utf-8", errors="replace")
            title, text = extract_text(markup) if "text/plain" not in ctype else ("", _clean(markup, TEXT_MAX))
            if not text:
                raise WebError("읽을 수 있는 글이 없습니다.")
            return {"url": url[:300], "title": title, "text": text, "error": None}
        raise WebError("리다이렉트가 너무 많습니다.")
    except WebError as e:
        return {"url": url[:300], "title": "", "text": "", "error": str(e)}
    except (requests.RequestException, ValueError, LookupError):
        return {"url": url[:300], "title": "", "text": "", "error": "페이지를 불러오지 못했습니다."}
