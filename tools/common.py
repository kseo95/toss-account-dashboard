"""도구 결과 정리·답변 조립에서 함께 쓰는 작은 도구. agent_tools.py에서 도구별로 나눈 모듈(동작은 같다)."""
from __future__ import annotations

import re



def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _round(value, digits: int = 2):
    value = _number(value)
    return None if value is None else round(value, digits)


def _compact(text: str) -> str:
    """키워드 비교용: 소문자로 바꾸고 공백·문장부호를 없앤다(이번 주 = 이번주, 왜 떨어 = 왜떨어)."""
    return re.sub(r"[\s.,!?~\-_'\"]+", "", str(text or "").lower())


def _origin_tag(item: dict) -> str:
    """보유가 아닌 종목의 표시: 질문에서 이름·코드로 찾아 본 종목은 '조회', 관심종목은 '관심'."""
    return " · 조회" if item.get("extra") else "" if item.get("held") else " · 관심"


def _won(value) -> str:
    return f"{float(value or 0):,.0f}원"


def _pct(value) -> str:
    return f"{float(value or 0):,.2f}%"


def _num(value) -> str:
    return f"{float(value or 0):,.0f}"


def _qty(shares, currency: str | None) -> str:
    """주수 표기. 국내(KRW)는 정수 주, 해외는 소수 둘째 자리. 모르면 '주수 확인 불가'."""
    if shares is None:
        return "주수 확인 불가"
    return f"{shares:,.0f}주" if currency == "KRW" else f"{shares:,.2f}주"


def _short_pct(value) -> str:
    return f"{float(value):.1f}".rstrip("0").rstrip(".") + "%"


def _josa(word: str, with_final: str, without_final: str) -> str:
    """받침 유무에 맞는 조사(이/가, 은/는). 마지막 글자가 한글이 아니면(영문 코드 등) 둘 다 쓴다."""
    last = str(word or "")[-1:]
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 else without_final
    return f"{with_final}({without_final})"


def _signed(value, digits: int = 1) -> str:
    rounded = round(float(value), digits) + 0.0  # -0.0이 "-0.0"으로 보이지 않게
    return f"{rounded:+.{digits}f}"
