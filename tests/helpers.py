"""테스트 공용 도우미: 앱 모듈 import 경로 설정 + 가짜 시세 데이터 생성."""
from __future__ import annotations

import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")  # urllib3 LibreSSL 경고 등

import common, dividends, portfolio, server, storage, strategy, tax, toss_api, trades, watch, weekly_report  # noqa: E402,F401

FIRST_MONDAY = date(2020, 1, 6)


def weekly_bars(weekly_closes: list[float], start: date = FIRST_MONDAY, adj: list[float] | None = None) -> list[tuple]:
    """주마다 금요일 하루짜리 일봉을 만든다 → 주봉 종가 = 그대로 weekly_closes.
    (월~목 봉은 넣지 않아도 주봉 계산에는 영향 없음 — 주의 마지막 거래일 값만 쓰기 때문)"""
    adj = adj or weekly_closes
    return [(start + timedelta(weeks=i, days=4), float(c), float(a)) for i, (c, a) in enumerate(zip(weekly_closes, adj))]


def hist(weekly_closes: list[float], divs: list[tuple] | None = None, name: str = "TEST", **kw) -> dict:
    return {"bars": weekly_bars(weekly_closes, **kw), "divs": divs or [], "name": name}


def last_friday(bars: list[tuple]) -> date:
    return bars[-1][0]
