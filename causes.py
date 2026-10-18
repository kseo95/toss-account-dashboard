"""보도 제목에서 반복되는 주제 모으기: 고점 이후 큰 하락일마다 찾은 보도 제목을 정해 둔 주제 사전(지정학·관세·금리·증시 급락 등)으로 분류해,
어떤 주제가 몇 번(어느 날) 반복됐는지와 하락폭 가중치를 센다. '원인 후보'를 정리하는 재료일 뿐 인과를 판정하지 않는다.

사전은 일반 시장 이슈 분류용이며 종목·날짜에 맞춘 것이 아니다. 제목에 낱말이 들었다는 것만 보므로 틀린 분류가 있을 수 있다.
순수 계산이며 다른 기능 모듈을 부르지 않는다.
"""
from __future__ import annotations

THEMES = {
    "지정학·전쟁": ("이란", "전쟁", "중동", "이스라엘", "우크라", "러시아", "공습", "미사일", "지정학", "하마스", "호르무즈", "북한"),
    "관세·무역 갈등": ("관세", "무역", "트럼프", "보복", "수출규제"),
    "금리·연준(통화정책)": ("금리", "연준", "fomc", "파월", "긴축", "매파", "비둘기", "fed "),
    "달러·환율": ("달러", "환율", "원화", "엔화"),
    "물가·고용 지표": ("물가", "인플레", "cpi", "ppi", "고용", "실업"),
    "증시 급락·투매": ("폭락", "사이드카", "서킷브레이커", "투매", "패닉", "증시 급락", "하락 마감", "매도세", "증발"),
    "반도체·AI": ("반도체", "엔비디아", "하이닉스", "삼성전자", " ai", "ai "),
    "당국 발표·정책 대응": ("재무장관", "기자회견", "긴급", "규제", "당국", "거래소", "금융위", "국민연금", "연기금", "리밸런싱"),
    "차익실현·청산·수급": ("차익실현", "청산", "마진콜", "레버리지", "증거금", "매물", "환매", "자금 유출", "수급"),
    "지수·신용등급 이벤트": ("msci", "강등", "신용등급"),
    "안전자산 동반 매도": ("안전자산", "금까지", "안전자산마저"),
    "원자재·귀금속 연쇄": ("은값", "구리", "원자재", "유가", "은 급락"),
}
MAX_THEMES = 4
MIN_DAYS_FOR_SHARE = 1


def themes_in(titles: list[str]) -> list[str]:
    """제목들에 낱말이 들어 있는 주제(사전 순서). 대소문자는 구분하지 않는다."""
    text = " ".join(str(t or "").lower() for t in titles)
    return [name for name, words in THEMES.items() if any(w in text for w in words)]


def rank_themes(days: list[dict], limit: int = MAX_THEMES) -> list[dict]:
    """days: [{"date", "change_pct", "headlines": [{"title","url","source","published"}], "themes": [...]}]. 큰 하락일 사이에서 자주(많은 날) 나온 주제 순,
    같으면 그 날들의 하락폭 합이 큰 순. 반환: [{"theme", "days": [날짜…], "weight_pct", "example": {...}|None}] — 예시는 가장 크게 빠진 날의 그 주제 보도."""
    out = []
    for name, words in THEMES.items():
        hit = [d for d in days if name in (d.get("themes") or [])]
        if not hit:
            continue
        biggest = min(hit, key=lambda d: d["change_pct"])
        example = next((h for h in (biggest.get("pool") or biggest.get("headlines") or []) if any(w in str(h.get("title") or "").lower() for w in words)), None)
        out.append({"theme": name, "days": [d["date"] for d in hit], "weight_pct": sum(abs(d["change_pct"]) for d in hit), "example": example,
                    "biggest_day": biggest["date"]})
    out.sort(key=lambda t: (-len(t["days"]), -t["weight_pct"]))
    return out[:limit]
