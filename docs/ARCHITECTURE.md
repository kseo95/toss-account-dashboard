# ARCHITECTURE

## 현재 구조 (Log 13, 2026-09-28)

```
redo/
├── server.py           # 백엔드 전부 (표준 라이브러리 http.server + requests)
├── dashboard.html      # 프론트 전부 (HTML/CSS/JS 단일 파일, Chart.js CDN)
├── tests/              # unittest (python3 -m unittest discover -s tests)
├── data/               # .gitignore — 실행 중 생김
│   ├── server_secret   # user_id HMAC용 비밀값 (권한 600)
│   ├── legacy_migrated # 예전 설정 파일을 옮겼다는 표시
│   └── users/<user_id>/*.json   # 사용자별 설정·기록
├── README.md, CLAUDE.md, CHANGELOG.md
└── docs/ARCHITECTURE.md
```

## 서버 (`server.py`)
- `127.0.0.1:8767` 전용. `Host`/`Origin` 헤더 검사, 세션 쿠키는 `HttpOnly; SameSite=Strict`.
- 로그인: 대시보드에서 앱키/시크릿 입력 → 토스 OAuth 토큰 발급 → 세션(메모리)에 보관. 디스크에는 저장하지 않아 서버 재시작 시 재로그인.
- **사용자 구분**: 로그인할 때 토스 계좌 목록에서 가장 작은 `accountSeq` 계좌의 계좌번호를 `data/server_secret`으로 HMAC-SHA256 → 앞 24자리 = `user_id`. 앱키를 재발급해도 같은 사용자로 인식되고, 폴더 이름만으로는 계좌번호를 알 수 없음.
- **사용자별 저장소**: `UserStore(data/users/<user_id>)`. 모든 `load_*`/`save_*` 함수가 store를 첫 인자로 받음. 파일: `cash_symbols.json`, `rebalance.json`, `watchlist.json`, `ma_rules.json`, `strategy_templates.json`, `strategy_state.json`, `weekly_report.json`. 파일이 없거나 깨지면 기본값. 저장은 `.tmp`에 쓰고 교체(원자적).
- 예전(`redo/` 바로 아래) 설정 파일은 **처음 로그인한 사용자 한 명에게만** 옮기고 `data/legacy_migrated`를 남김.
- 외부 데이터: 토스 Open API(계좌·시세, 조회만), Yahoo Finance 비공식(USD/JPY, 주간 리포트 일봉), 네이버 증권 자동완성 비공식(종목 검색).
- 공용(사용자 무관) 캐시: Yahoo 일봉 30분, 주간 리포트 시장 부분 30분(설정+기준일이 캐시 키라 사용자별 설정이 달라도 섞이지 않음), 토스 종목 기본정보 10분.

## 프론트 (`dashboard.html`)
- 로그인 화면 / 연결 화면. 연결 화면은 상단 탭 "포트폴리오 / 주간 리포트".
- 설정은 전부 화면의 설정 패널에서 편집 → `POST /api/...`로 전체 교체 저장.

## 테스트 (`tests/`)
- `test_weekly_report.py`: 주봉 묶기, 기준일(ET), 종목 주간 지표, 설정 검증, 자동 코멘트, 리포트 조립·캐시, 내 계좌 채우기, Yahoo 응답 파싱.
- `test_strategy.py`: 단계 평가(MA/RSI/등락률), 래칫, 회복 게이팅, 배정 방식, 배수 규칙, 다음 단계 %p, 템플릿·목표 비중·기본 전략 검증, 자동 배정, 매수 금액 계산.
- `test_portfolio.py`: 리밸런싱·통화 비중 계산, 설정 검증.
- `test_storage_http.py`: user_id, 저장소 왕복·기본값, 예전 파일 이전, 실제 Handler를 임시 포트로 띄운 HTTP 테스트(로그인, 401/403/413, 사용자 간 분리, 앱키 재발급 시 같은 사용자).
- 외부 API는 전부 `unittest.mock`으로 대체 — 네트워크 없이 돈다.
