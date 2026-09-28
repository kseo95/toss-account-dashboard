# ARCHITECTURE

## 현재 구조 (Log 17, 2026-09-28)

```
redo/
├── server.py          # HTTP 계층: 세션, 보안 검사, 라우트 표(@route), 정적 파일
├── common.py          # 공용: ApiError, to_float, 종목 코드 검증, parallel_map
├── toss_api.py        # 토스 Open API + 네이버 검색 + Yahoo USD/JPY, 캐시(종목정보 10분·일봉 5분)
├── storage.py         # 사용자별 저장소(UserStore), user_id, 예전 파일 이전
├── portfolio.py       # 보유종목·예수금 계산, 현금 취급 종목, 리밸런싱, 원/달러 비중
├── watch.py           # 관심종목, MA 감시
├── strategy.py        # 분할매수 전략: 템플릿 검증, 평가(래칫·회복·배수·n차 사이클), 매수/매도 금액
├── weekly_report.py   # 주간 리포트: Yahoo 일봉 → 주봉 지표, 자동 코멘트, 내 계좌
├── tax.py             # 세금 추정: 증권거래세·해외 양도세(연간 합산·공제)·기타 ETF, 세율 설정, 법 출처 표
├── trades.py          # 체결 내역(종료된 주문 → 체결), 해외 실현 손익(선입선출·결제일 환율)
├── dividends.py       # 배당 추정(배당락일 보유 수량 × Yahoo 배당 기록, 원천징수)
├── dashboard.html     # 마크업만
├── static/css/app.css
├── static/js/         # common → portfolio → watch → strategy → report → tax → app 순서로 로드
├── tests/             # unittest
└── data/              # .gitignore — server_secret, legacy_migrated, users/<user_id>/*.json
```

의존 방향: `server` → 기능 모듈 → `toss_api`/`storage` → `common`. 기능 모듈끼리는 서로 부르지 않는다.

## 서버
- `127.0.0.1:8767` 전용. `Host`/`Origin` 헤더 검사, 세션 쿠키 `HttpOnly; SameSite=Strict`, 요청 크기 제한(기본 8KB, 주간 리포트 설정 64KB).
- 라우트는 `@route(method, path)`로 등록. 핸들러는 `Request(session, query, body)`를 받아 dict를 돌려주고, 에러는 `ApiError`(메시지 그대로) / 토스 HTTP 오류 / 네트워크 오류 / 그 외 500으로 `_dispatch`가 한곳에서 JSON으로 바꾼다.
- 로그인: 앱키/시크릿 → 토스 토큰 → 계좌 목록 → `user_id` → 세션(메모리). 계좌 목록은 세션에 두고 재사용. 토큰이 401이면 한 번만 재발급(`call_with_reauth`).
- **계좌 원자료 캐시**: 환율·예수금·보유종목(`fetch_account_snapshot`, 요청 병렬)을 세션에 15초 보관. 첫 화면에서 보유·전략·리포트가 같이 쓰고, 동시에 온 요청은 세션 잠금으로 한 번만 받는다. 계산(`build_holdings`)은 매번 새로 해서 설정 변경은 바로 반영.
- **일봉 캐시**: `fetch_close_history`가 일봉 페이지를 한 번 받아 일봉·주봉(ISO 주) 종가를 같이 만든다. 5분 캐시라 MA 감시·분할매수·관심종목이 같은 종목 캔들을 다시 받지 않는다. 종목별 조회는 4개씩 병렬.
- 공용 캐시: Yahoo 일봉 30분, 주간 리포트 시장 부분 30분(설정+기준일이 키), 토스 종목 기본정보 10분.
- 사용자 구분: 가장 작은 `accountSeq` 계좌번호를 `data/server_secret`으로 HMAC-SHA256 → 앞 24자리. 앱키를 재발급해도 같은 사용자, 폴더 이름으로 계좌번호를 알 수 없음. 예전(`redo/` 바로 아래) 설정 파일은 처음 로그인한 사용자 한 명에게만 옮김.
- 외부 데이터: 토스 Open API(조회만), Yahoo Finance 비공식(USD/JPY, 주간 리포트), 네이버 증권 자동완성 비공식(검색).

## 프론트
- 로그인 화면 / 연결 화면(상단 탭 "포트폴리오 / 주간 리포트"). 빌드 도구 없이 일반 `<script>` 여러 개(전역 공유).
- `common.js`: `esc`(API 문자열 이스케이프), 포맷, `getJson`/`postJson`, 저장 버튼 흐름 `runSave`, 종목 검색 자동완성 `attachSymbolSearch`(관심종목·MA·전략 배정이 공유).
- 설정은 전부 화면의 설정 패널에서 편집 → `POST /api/...`로 전체 교체 저장.

## 테스트 (`tests/`)
- `test_toss_api.py`: 일봉→주봉 묶기·페이지·캐시, 계좌 원자료 합치기, 보유 화면 계산.
- `test_weekly_report.py`: 주봉 묶기, 기준일(ET), 종목 주간 지표, 설정 검증, 자동 코멘트, 리포트 조립·캐시, 내 계좌, Yahoo 파싱.
- `test_strategy.py`: 단계 평가, 래칫, 회복 게이팅, 배정 방식, 배수, 다음 단계 %p, n차 사이클(real/ 숫자 경로·무장·배수 초기화·예전 기록), 설정 검증, 자동 배정, 매수/매도 금액.
- `test_portfolio.py`: 리밸런싱·통화 비중, 설정 검증.
- `test_trades.py`: 주문 목록 페이지 넘기기, 체결 골라내기·합계, 실현 손익(선입선출·환율·연도·불완전 표시).
- `test_dividends.py`: 배당락일 보유 수량, 예전 보유분 보정, 원천징수, 연도·환율.
- `test_tax.py`: 국내 종목 종류 추정, 시장별 거래세, 해외 양도세 합산·공제·올해 실현분, 기타 ETF, 대주주, 매도 세금 비율, 설정 검증.
- `test_storage_http.py`: user_id, 저장소 왕복·기본값, 예전 파일 이전, 실제 Handler를 임시 포트로 띄운 HTTP 테스트(로그인, 401/403/413/404, 잘못된 JSON, 정적 파일·경로 조작 차단, 계좌 원자료 캐시, 사용자 분리).
- 외부 API는 `unittest.mock`으로 대체. 기능 모듈이 `from toss_api import ...`로 가져온 함수는 **쓰는 모듈 쪽**을 patch한다(예: `strategy.fetch_close_history`).
