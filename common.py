"""여러 모듈이 같이 쓰는 작은 도우미와 상수."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,10}$")  # 토스 종목 코드
MA_INTERVALS = {"day", "week"}
MA_MAX_PERIOD = {"day": 200, "week": 300}  # 캔들 페이지네이션으로 무리없이 받아올 수 있는 상한


class ApiError(Exception):
    """사용자에게 그대로 보여줄 에러(상태 코드 + 메시지). HTTP 계층에서 JSON 응답으로 바꾼다."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_as_of(ts: float) -> str:
    """기준 시각 표기: 오늘이면 HH:MM, 아니면 MM-DD HH:MM (로컬 시간). real/portfolio.py와 동일."""
    t = time.localtime(ts)
    same_day = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M" if same_day else "%m-%d %H:%M", t)


def parallel_map(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """순서를 유지하는 병렬 map. 항목이 하나 이하면 스레드를 만들지 않는다."""
    items = list(items)
    if len(items) <= 1 or workers <= 1:
        return [fn(it) for it in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


def normalize_symbols(raw, max_count: int, what: str, pattern: re.Pattern = SYMBOL_PATTERN) -> list[str]:
    """종목 코드 목록 검증: 대문자·공백 제거·중복 제거(순서 유지). 잘못되면 ApiError(400)."""
    if not isinstance(raw, list):
        raise ApiError(400, "symbols는 배열이어야 합니다.")
    if len(raw) > max_count:
        raise ApiError(400, f"{what}은 최대 {max_count}개까지 등록할 수 있습니다.")
    symbols: list[str] = []
    for s in raw:
        if not isinstance(s, str):
            raise ApiError(400, "종목 코드는 문자열이어야 합니다.")
        sym = s.strip().upper()
        if not pattern.fullmatch(sym):
            raise ApiError(400, f"잘못된 종목 코드입니다: {s!r}")
        if sym not in symbols:
            symbols.append(sym)
    return symbols
