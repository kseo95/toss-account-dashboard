# ARCHITECTURE

## 현재 구조 (Log 17, 2026-09-28)

```
redo/
├── server.py          # HTTP 계층: 세션, 보안 검사, 라우트 표(@route), 정적 파일
├── common.py          # 공용: ApiError, to_float, 종목 코드 검증, parallel_map
├── toss_api.py        # 토스 Open API + 네이버 검색 + Yahoo USD/JPY, 캐시(종목정보 10분·일봉 5분)
├── storage.py         # 사용자별 저장소(UserStore), user_id, 예전 파일 이전
├── portfolio.py       # 보유종목·예수금, 현금 취급, 목표 비중·리밸런싱 금액 계획, 원/달러 비중
├── watch.py           # 관심종목, MA 감시
├── sectors.py         # 섹터 매핑(직접 입력 + 토스 분류 자동 채우기): 설정 검증, 섹터별 내 노출, 토스 섹터 색인·후보
├── strategy.py        # 분할매수 전략: 템플릿 검증, 평가(래칫·회복·배수·n차 사이클), 매수/매도 금액
├── weekly_report.py   # 주간 리포트: Yahoo 일봉 → 주봉 지표, 자동 코멘트, 내 계좌
├── tax.py             # 세금 추정: 증권거래세·해외 양도세(연간 합산·공제)·기타 ETF, 세율 설정, 법 출처 표
├── trades.py          # 체결 내역(종료된 주문 → 체결), 해외 실현 손익(선입선출·결제일 환율)
├── dividends.py       # 배당 추정(배당락일 보유 수량 × Yahoo 배당 기록, 원천징수)
├── agent_tools.py      # Agent용 읽기 전용 도구 계약 + 비민감 결과 축약
├── claude_agent.py     # AI 모드: Claude API 도구 호출 루프(키는 세션 메모리에만)
├── etf_holdings.py    # ETF 상위 편입 종목·비중(야후 funds_data, 하루 캐시, 상태 구분)
├── etf_breakdown.py   # ETF 등락을 편입 종목 기여(비중×등락)로 분해(순수 계산, 같은 날짜 기준·그 밖 암시 수익률·경고 플래그)
├── long_history.py    # 과거 유사 국면 계산용 긴 대용 시세: 금 월간(1975~, World Bank 월평균 공개 데이터셋)·지수 일봉 대응표(기본값)
├── trend.py           # 추세 사다리·전환 상태: 여러 기간 수익률·이동평균 대비·상태 판정(버퍼·확인 주수는 기본값, 질문 단위 덮어쓰기)·연속 주수(순수 계산)
├── screener.py        # 이동평균 스크리너: S&P500 구성 종목(공개 데이터셋)·주봉 묶기·MA 대비 위치·모두/하나라도 조건(순수 계산)
├── cases.py           # 거시 사례집: 12개 사례 국면별 서사(해석)·시세로 계산한 변화·통념 점검·지금과 닮은 국면(순수 계산)
├── intent_matcher.py  # 문장 유사도(글자 n-gram TF-IDF)로 질문 의도 분류, 비용·모델 없음(순수 계산)
├── intent_examples.py # 의도별 기본 예시 문장(데이터), 사용자가 가르친 문장은 data/users/<id>/agent_intents.json, 켠 경우 놓친 질문은 agent_misses.json
├── secret_store.py    # FRED 키 자동 연결: 환경변수·macOS 키체인 읽기·저장·삭제(셸 없이, 키는 반환값·로그에 남기지 않음)
├── fred.py            # FRED 시계열(일간 5개: 연방기금금리·2년물·실질금리·기대인플레이션·장단기 금리차, 월간·주간 3개: CPI·실업률·연준 총자산은 사례집 전용) 조회, 선택 기능
├── shocks.py          # 과거 매크로 충격 반응: 요인 역사 기준 큰 변동 판정·사례 묶기·부호 검정(본페로니)·이름 붙은 거시 이슈(순수 계산)
├── outlook.py         # 종목별 앞으로의 뷰 재료: 과거 유사 국면 통계(독립 사례 수 포함)·요인 민감도 시나리오·변동성 범위(순수 계산, 예측 아님)
├── macro.py           # 매크로 요인 분해: 일간 수익률 회귀(단계적 선택)로 이번 주 등락의 요인별 기여·설명력 추정(순수 계산)
├── news.py            # 종목별 최근 7일 뉴스 헤드라인(야후 검색 + 구글 뉴스 RSS, 비공식) 수집·정리, 5거래일 등락률
├── dashboard.html     # 마크업만
├── static/css/app.css
├── static/js/         # common → portfolio → sectors → watch → strategy → report → tax → app 순서로 로드
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
- **섹터 자동 채우기**: 토스 섹터 API로 "종목 → 소속 섹터" 색인을 사용자별 `sector_index.json`에 하루 한 번 모은다(백그라운드 스레드, 호출 간격 0.2초, 중단 후 이어하기). 후보는 화면 초안에만 추가되고 저장은 사용자가 따로 한다.
- 외부 데이터: 토스 Open API(조회만), Yahoo Finance 비공식(USD/JPY, 주간 리포트), 네이버 증권 자동완성 비공식(검색).

## 프론트
- 로그인 화면 / 연결 화면(상단 탭 "포트폴리오 / 주간 리포트"). 빌드 도구 없이 일반 `<script>` 여러 개(전역 공유).
- `common.js`: `esc`(API 문자열 이스케이프), 포맷, `getJson`/`postJson`, 저장 버튼 흐름 `runSave`, 종목 검색 자동완성 `attachSymbolSearch`(관심종목·MA·전략 배정이 공유).
- 설정은 전부 화면의 설정 패널에서 편집 → `POST /api/...`로 전체 교체 저장.

## AI Agent 도구 계층 (v1)
- `GET /api/agent/tools`: 허용된 읽기 전용 도구의 이름·설명·입력 스키마를 반환한다.
- `POST /api/agent/tool`: 서버가 기존 계산 함수를 실행한 뒤 `agent_tools.py`가 허용한 필드만 축약해 반환한다.
- 현재 도구는 포트폴리오 요약, 주간 위험 점검, 전량 매도 가정 세금 추정, 섹터 노출, 이번 주 종목 뉴스, 매크로 요인, 앞으로의 상승/하락 뷰, ETF 구성 종목 분해, 과거 매크로 충격 반응, 거시 사례집, 요인 순위, 이동평균 스크리너, 미국 업종 ETF 상태, 고점 대비 낙폭 원인 분석, 요인이 움직일 때 자산 반응, 밸류에이션, 보조지표(RSI·MACD·볼린저), 내 원칙(목표 비중·전략) 대조 18개다. 규칙 기반 모드 답변은 `charts.py`가 만든 그래프 명세(`answer.charts`)를 `static/js/agent_charts.js`가 Chart.js로 그린다. 로컬 모델(`local_model.py`)이 켜져 있으면 모델이 도구를 고르고 결과를 보며 최대 2번 더 생각하며(Log 70) 실패하면 규칙 기반으로 되돌아간다. 규칙 기반 모드는 `agent_loop.py`가 결과를 보고 최대 2개 도구를 더 부른다.
- 질문 실행 API는 API 키가 있으면 모델 도구 호출을 수행하고, 없으면 규칙으로 필요한 도구를 선택해 결과를 조합한다.
- Agent 탭은 예시 질문, 실행 도구 추적, 답변과 계산 한계를 표시한다.
- Anthropic API 키가 설정되면 claude_agent.py가 Claude Messages API(공식 `anthropic` 패키지, 수동 도구 루프)로 질문을 처리한다. 모델은 허용된 도구만 선택하며 서버가 실행한 축약 결과를 받아 최종 답변한다. 패키지는 이 모드에서만 불러오고, 없으면 설치 안내를 보여준다.
- API 키와 모델 설정은 로그인 세션 메모리에만 존재한다. 응답 저장은 비활성화하며 최근 대화 문맥은 브라우저 메모리에서 최대 8개만 전달한다.
- 토스 앱키·시크릿·토큰·계좌번호와 전체 원본 응답은 Agent 결과에 포함하지 않는다.
- 쓰기·설정 변경·주문 도구는 제공하지 않는다. 키가 없을 때만 기존 규칙 기반 라우터를 대체 경로로 사용한다.

## 테스트 (`tests/`)
- `test_toss_api.py`: 일봉→주봉 묶기·페이지·캐시, 계좌 원자료 합치기, 보유 화면 계산.
- `test_weekly_report.py`: 주봉 묶기, 기준일(ET), 종목 주간 지표, 설정 검증, 자동 코멘트, 리포트 조립·캐시, 내 계좌, Yahoo 파싱.
- `test_strategy.py`: 단계 평가, 래칫, 회복 게이팅, 배정 방식, 배수, 다음 단계 %p, n차 사이클(real/ 숫자 경로·무장·배수 초기화·예전 기록), 설정 검증, 자동 배정, 매수/매도 금액.
- `test_portfolio.py`: 리밸런싱·통화 비중, 설정 검증.
- `test_trades.py`: 주문 목록 페이지 넘기기, 체결 골라내기·합계, 실현 손익(선입선출·환율·연도·불완전 표시).
- `test_dividends.py`: 배당락일 보유 수량, 예전 보유분 보정, 원천징수, 연도·환율.
- `test_tax.py`: 국내 종목 종류 추정, 시장별 거래세, 해외 양도세 합산·공제·올해 실현분, 기타 ETF, 대주주, 매도 세금 비율, 설정 검증.
- `test_storage_http.py`: user_id, 저장소 왕복·기본값, 예전 파일 이전, 실제 Handler를 임시 포트로 띄운 HTTP 테스트(로그인, 401/403/413/404, 잘못된 JSON, 정적 파일·경로 조작 차단, 계좌 원자료 캐시, 사용자 분리).
- `test_sectors.py`: 섹터 설정 검증, 저장 왕복·깨진 파일, 노출 계산, 섹터 HTTP 라우트(사용자 분리·종목 실존 확인), Agent 도구 계약, 토스 섹터 호출 검증, 색인 수집(이어하기·실패 저장·진행률)·후보 계산, 자동 채우기 라우트(상태 전이·시장 제한·사용자 분리).
- `test_trade_quantities.py`: 리밸런싱 매수·매도 종목별 주수(설정 저장 → 보유 → 필요한 종목만 시세 조회 → Agent 답변), 시세 조회 실패 처리, 주봉 MA 전략 신호 반영(지금/대기 금액, 전략 제외, 신호 실패, 상태 미저장).
- `test_etf_holdings.py`·`test_etf_breakdown.py`·`test_etf_view.py`·`test_etf_agent.py`: 편입 조회 파싱·캐시, 기여 계산·정렬·경고, 서버 경로(편입 없는 종목 분리·보도 조회어), 표·요약 조합.
- `test_trend.py`·`test_trend_view.py`: 상태 판정·연속 주수, 서버 경로("상태 어떰?" 질문이 업종 ETF 표를 받는지, 내 업종 노출 질문은 노출 도구에 남는지).
- `test_screener.py`·`test_ma_screen_view.py`: 구성 종목 파싱·주봉·조건 판정, 서버 경로(S&P500·내 종목·실패 분리).
- `test_asset_drivers_view.py`: 평소 가장 영향받는 요인 순위의 서버 경로(금 선물 대용·FRED 합류·실패 격리). 순위 계산은 `test_macro.py`.
- `test_intent_matcher.py`·`test_intent_learning.py`: 문장 유사도 분류·새 문장 정확도 기준, 가르치기(사용자별·한도·삭제)와 처리 순서.
- `test_secret_store.py`: 환경변수→키체인 읽기 순서·형식 검사·저장/삭제 인자·오류 격리.
- `test_cases.py`·`test_macro_cases_view.py`·`test_fred.py`: 사례 데이터 무결성·통념 점검·닮은 국면, 서버 경로, FRED 파싱·오류 비노출·설정 경로.
- `test_shocks.py`·`test_macro_history_view.py`·`test_history_agent.py`: 큰 변동 판정·사례·검정·이슈 이름, 서버 경로(금 선물 대용), 표 조합·질문 분류·요인 표의 검증 표시.
- `test_long_history.py`: 금 월간 CSV 파싱·캐시·짧은 응답 거부·대응표.
- `test_outlook.py`·`test_outlook_view.py`: 국면 판정(미래 정보 미사용)·유사 상황 통계·표본/독립 사례 부족 처리·변동성 범위·요인 시나리오, 질문에서 종목 찾기(한글 경계)와 서버 경로.
- `test_macro.py`·`test_macro_view.py`: 최소제곱·요인 선택·동일 시리즈 제외·기여 항등식·상태(ok/weak/no_data), 매크로 도구 서버 경로(조회 실패 격리, 계좌 정보 비노출).
- `test_news.py`·`test_weekly_news.py`: 뉴스 파싱·7일 필터·중복 제거·정렬·링크 안전성·캐시·실패 격리, 이번 주 뉴스 도구(보유+관심종목, 현금 취급 제외, 계좌 정보 비전송).
- `test_claude_agent.py`: Claude Agent 루프(가짜 클라이언트 + 실제 SDK 요청 모양 검사), 오류 변환, 설정 라우트.
- `test_agent_tools.py`: 도구 허용 목록, 자격증명 비노출, 결과 축약, 한계·불완전 데이터 표시.
- 외부 API는 `unittest.mock`으로 대체. 기능 모듈이 `from toss_api import ...`로 가져온 함수는 **쓰는 모듈 쪽**을 patch한다(예: `strategy.fetch_close_history`).
