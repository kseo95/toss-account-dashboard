"""못 답하는 질문의 처리 정책(규칙 기반 모드): 못 답하는 이유를 4가지로 나눠 각각 다르게, 정직하게 알린다.

  no_feature  — 도구가 없는 주제(배당 내역·실적·RSI·목표주가·환차손익·예측 등): 없다고 말하고 가까운 대안 질문을 준다
  trade       — 주문 요청: 읽기 전용이라 실행하지 않는다고 말하고 계획 보기를 안내한다
  not_found   — 종목 이름·코드를 찾지 못함: 확인을 부탁한다
  unclear     — 질문을 못 알아들음: 비슷해 보이는 질문 종류를 후보로 되묻는다(없으면 지금 가능한 것을 정리)
엉뚱한 답을 하는 것보다 "여기까지 압니다 / 여기부터는 못 합니다"를 말하는 쪽을 택한다. 순수 함수이며 다른 기능 모듈을 부르지 않는다.
"""
from __future__ import annotations

import re

SAMPLE_NAME = "엔비디아"                       # 보유 종목 이름이 없을 때 예시 질문에 쓰는 이름
TRADE_WORDS = ("사줘", "팔아줘", "매수해줘", "매도해줘", "주문해", "주문넣", "사자", "팔자", "체결해", "담아줘", "대신사", "대신팔")

# (주제 이름, 키워드, 알림, 대안 질문 — {n}은 종목 이름). 키워드는 공백 없는 소문자로.
NO_FEATURE = [
    ("배당", ("배당", "배당률", "배당금", "배당락"), "배당 내역이나 배당률 추이를 보는 기능은 아직 없습니다.", ["{n} PER PBR ROE 알려줘", "{n} 고평가야?"]),
    ("실적·재무제표", ("실적", "어닝", "영업이익", "매출액", "재무제표", "현금흐름", "순이익", "영업현금"), "분기 실적이나 재무제표를 보여 주는 기능은 없습니다. 밸류에이션 지표(PER·PSR·PBR·EPS·BPS·ROE)는 볼 수 있습니다.",
     ["{n} PER PBR ROE 알려줘", "{n} 고평가야?"]),
    ("기술적 보조지표", ("스토캐스틱", "피보나치", "지지선", "저항선", "캔들", "엘리어트", "일목균형", "이치모쿠", "adx", "obv"),
     "스토캐스틱·피보나치·캔들 패턴·지지/저항선 같은 지표는 아직 없습니다. RSI·MACD·볼린저밴드·이동평균 교차는 볼 수 있습니다.", ["{n} RSI MACD 볼린저 보여줘", "{n} 보조지표 어때?"]),
    ("목표주가·애널리스트", ("목표주가", "목표가", "애널리스트", "투자의견", "컨센서스", "리포트"), "증권사 목표주가나 투자의견 데이터는 없습니다. 같은 업종 배수 기준 추정 가격 범위는 볼 수 있습니다(적정 주가가 아닙니다).", ["{n} 고평가야?"]),
    ("환차손익", ("환차익", "환차손", "환차손익", "매입환율", "환율손익"), "매입 시점 환율 기록이 없어 환차손익은 아직 계산하지 못합니다.", ["내 포트폴리오 보여줘"]),
    ("공매도·옵션·신용", ("공매도", "옵션", "신용거래", "대차잔고", "풋콜", "미결제"), "공매도·옵션·신용거래 데이터는 다루지 않습니다. 변동성(VIX) 반응은 볼 수 있습니다.", ["VIX가 급등하면 S&P500은 어떻게 돼?"]),
    ("절세·신고", ("절세", "신고방법", "홈택스", "연말정산", "양도세신고"), "세금 신고 절차나 절세 전략은 안내하지 않습니다. 지금 전량 매도했을 때의 세금 추정만 볼 수 있습니다.", ["전부 팔면 세금은?"]),
    ("가격 예측", ("몇퍼", "내일", "다음주", "언제오를", "언제살", "언제팔", "목표수익", "얼마까지오를"), "정확한 가격이나 시점은 예측하지 않습니다. 과거 통계와 요인 민감도로 만든 조건부 시나리오는 볼 수 있습니다.", ["{n} 앞으로의 상승/하락 뷰"]),
]
GENERAL = ["이번 주 무엇을 확인해야 해?", "{n} 고평가야?", "{n} 고점 대비 왜 빠졌어?", "미국 금리가 오르면 금과 나스닥은 어떻게 돼?", "지금 미국 업종별 etf들 상태 어떰?"]
CAPABILITIES = [
    "내 계좌: 이번 주 점검, 리밸런싱 계획, 전부 팔면 세금, 섹터 노출",
    "종목 분석(보유·관심이 아니어도 이름·코드로 가능): 뉴스, 요인, 앞으로의 뷰, 고점 대비 낙폭 원인, 밸류에이션(고평가·저평가)",
    "시장: 미국 업종 ETF 상태, 이동평균 스크리너, 요인이 움직일 때 자산 반응, 과거 사례집",
]


def _compact(text: str) -> str:
    return re.sub(r"[\s.,!?~\-_'\"]+", "", str(text or "").lower())


def match_limit(question: str) -> dict | None:
    """도구가 없다고 아는 주제나 주문 요청이면 그 정보({kind, topic, message, alternatives}), 아니면 None."""
    compact = _compact(question)
    if any(w in compact for w in TRADE_WORDS):
        return {"kind": "trade", "topic": "주문", "message": "이 대시보드는 읽기 전용이라 주문을 실행하지 않습니다(안전 장치). 얼마를 사고팔아야 하는지 계획은 볼 수 있습니다.",
                "alternatives": ["리밸런싱 계획 보여줘"]}
    for topic, words, message, alternatives in NO_FEATURE:
        if any(w in compact for w in words):
            return {"kind": "no_feature", "topic": topic, "message": message, "alternatives": alternatives}
    return None


def respond(question: str, *, limit: dict | None = None, unknown: list[str] | tuple = (), candidates: list[dict] | tuple = (), names: list[str] | tuple = (),
            subject: list[str] | tuple = ()) -> dict:
    """못 답한 질문의 응답: {kind, headline, bullets, suggestions}. candidates: [{"label", "example"}](비슷해 보이는 질문 종류, 많아야 2개).
    subject: 질문에 적힌 종목 이름(있으면 대안 질문의 {n}에 그 종목을 쓴다), names: 내 종목 이름(subject가 없을 때 쓴다)."""
    name = next((str(n).strip() for n in list(subject) + list(names) if str(n).strip()), SAMPLE_NAME)[:30]
    fill = lambda items: list(dict.fromkeys(x.format(n=name) for x in items))
    if limit:
        head = {"trade": "주문은 실행하지 않습니다", "no_feature": f"{limit['topic']} 기능은 아직 없습니다"}[limit["kind"]]
        return {"kind": limit["kind"], "headline": head, "bullets": [limit["message"], "대신 이런 질문은 바로 답할 수 있습니다."], "suggestions": fill(limit["alternatives"])}
    if unknown:
        listed = ", ".join(f"'{u}'" for u in list(unknown)[:3])
        return {"kind": "not_found", "headline": "종목을 찾지 못했습니다",
                "bullets": [f"{listed}을(를) 종목으로 찾지 못했습니다. 이름이나 코드를 정확히 적어 주세요(토스에서 거래 가능한 한국·미국 종목만 조회됩니다).", "예를 들면 이렇게 물어볼 수 있습니다."],
                "suggestions": fill([f"{SAMPLE_NAME} PER 어때?", "TSLA 고점 대비 왜 빠졌어?", "{n} 뉴스 알려줘"])}
    if candidates:
        return {"kind": "unclear", "headline": "질문을 정확히 이해하지 못했습니다",
                "bullets": ["혹시 이런 질문인가요? 아래에서 고르시거나, 다른 말로 다시 물어봐 주세요."] + [f"· {c['label']}" for c in candidates],
                "suggestions": fill([c["example"] for c in candidates] + GENERAL[:1])}
    return {"kind": "unclear", "headline": "아직 답할 수 없는 질문입니다",
            "bullets": ["질문 뜻을 이해하지 못했거나 해당 기능이 없습니다. 지금 가능한 것은 이렇습니다."] + [f"· {c}" for c in CAPABILITIES], "suggestions": fill(GENERAL)}
