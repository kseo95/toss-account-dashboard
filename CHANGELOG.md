# CHANGELOG

최신 로그가 위에 오도록 기록한다.

## Log 5 — 2026-09-22 (리밸런싱 도넛차트, 원/달러 비율 도넛차트)

### 구현 내용
- `server.py`
  - `load_rebalance_config()`: `rebalance.json`을 읽어 카테고리(`label`/`symbols`/`target_pct`) + `rest_pct`를 반환. 파일이 없으면 카테고리 없이 "나머지 100%"로 취급.
  - `compute_rebalance()`: 카테고리별 현재%(보유종목 평가금액 합 ÷ 총자산) vs 목표%. 어느 카테고리에도 안 걸린 나머지 종목+예수금은 "나머지" 버킷(전부 현금 취급 종목이면 "현금"으로 자동 표시) — `real/portfolio.py`의 관례와 동일.
  - `compute_currency_split()`: 전체 자산(예수금 포함) 중 원화/달러 비중.
  - `GET /api/holdings` 응답에 `rebalance`, `currency_split` 필드 추가.
- `dashboard.html`
  - Chart.js 4 + chartjs-plugin-datalabels 2 (jsdelivr CDN, `real/dashboard.html`과 동일한 라이브러리/버전) 추가.
  - "리밸런싱 구성" 도넛(범례에 "현재% / 목표%", 0% 카테고리는 도넛엔 안 나오고 범례에만 회색 유지)과 "원/달러 비율" 도넛을 현금 표 위에 나란히 배치.
  - 카테고리 색은 `real/`과 동일한 dataviz 스킬 검증 팔레트(`CAT_PALETTE`, 11색) 재사용, "나머지"/"현금"은 중립 회색.

### 의도적으로 미룬 것
- **리밸런싱 목표를 화면에서 편집하는 UI는 아직 없음.** 지금은 `rebalance.json`을 직접 작성/수정해야 함. `real/`도 처음엔 파일만 만들고 그 위에 웹 편집 UI를 나중에 얹은 이력이 있어(설정 로그 참고), 같은 순서로 간다. **다음 단계 후보로 이 편집 UI를 최우선으로 고려할 것** — 사용자가 "코드 수정 없이 화면에서 설정을 바꿀 수 있어야 한다"는 원칙을 명확히 요청함(배포 예정이라 다른 사람도 자기 설정으로 쓸 수 있어야 함).
- 합계 100% 검증, 종목 중복(이중 집계) 검증은 아직 없음(파일을 직접 쓰는 지금 단계에서는 실수해도 걸러주는 게 없으니 주의).
- `rebalance.json`은 개인 계좌 구성을 담으므로 `cash_symbols.json`과 함께 `.gitignore` 처리함.

### 확인한 것
- 가짜 데이터로 `compute_rebalance`/`compute_currency_split` 단위 테스트: 카테고리 매칭, 나머지→현금 자동 전환, 비율 합계 100% 확인.
- 서버 문법 체크, JS 문법 체크(jsc) 통과.

## Log 4 — 2026-09-22 (보유종목 조회·표시, 현금 취급 종목 설정)

### 구현 내용
- `server.py`
  - `fetch_holdings_data`: 계좌 전체의 보유종목(국내/해외 구분) + 예수금(원화/달러) + 총계(총 평가금액·총 손익·총 수익률)를 계산. `real/portfolio.py`의 `fetch_rows`/`totals_of`와 같은 방식(지분율·수익률 분모는 "보유종목 + 예수금"으로 통일).
  - `GET /api/holdings`: 위 데이터를 반환하는 새 엔드포인트.
  - **현금 취급 종목 설정** (`cash_symbols.json`, `GET`/`POST /api/cash-symbols`): 어떤 심볼을 "현금"으로 취급할지 사용자가 고를 수 있게 함. 설정은 보유 여부와 독립적으로 파일에 저장되므로, 전량 매도해도 설정이 사라지지 않고(화면엔 "미보유 · 설정만 남음"으로 표시) 나중에 다시 사면 자동으로 다시 현금 취급됨.
  - 손익 필드는 `profitLoss.amount`/`rate`(수수료·세금 반영 전) 대신 **`amountAfterCost`/`rateAfterCost`(반영 후)를 사용하기로 결정** — 토스 앱/웹 화면에 표시되는 숫자(반영 전 기준으로 추정)와 최대 몇 달러/1%p 수준의 작은 오차가 날 수 있음을 확인했고(스크린샷으로 실측 비교), 실제 손에 남는 손익에 더 가까운 값을 우선하기로 함. **다시 반영 전으로 되돌리지 말 것** (이미 논의하고 결정한 사항).
- `dashboard.html`
  - 로그인 후 화면: 상단 요약 카드(총 평가금액/총 손익/총 수익률), 국내 종목 표, 해외 종목 표, 현금 표(원화 예수금+달러 예수금+현금 취급 종목+합계), 현금 취급 종목 설정 패널(접이식).
  - 해외 종목 표: 평가금액/손익을 **원/$ 토글 버튼**으로 전환해서 보여줌(재조회 없이 캐시된 데이터로 다시 그림). 수익률(%)·지분율(%)은 통화와 무관한 비율이라 토글 대상에서 제외(환율이 분자·분모에 동일하게 곱해져 상쇄되므로 원/$ 값이 항상 같음 — 단, 매입 시점 환율을 반영한 환차손익까지는 API 한계로 계산 안 함, 아래 "미해결" 참고).

### 겪은 문제와 수정
- **429 (요청 제한)**: 로그인 직후 `/api/holdings`가 토큰 검증용 `get_accounts` 호출 + 실제 작업용 `get_accounts` 호출을 중복으로 하고 있었고, 계좌당 예수금(KRW/USD)+보유종목 조회가 한꺼번에 나가면서 토스의 초당 요청 제한에 걸림. `call_with_reauth(session, fn)`로 바꿔서 "일단 실행해보고 401일 때만 재발급"하는 `real/portfolio.py`와 같은 방식으로 변경해 중복 호출을 없앴고, `api_get`에 429 재시도(백오프)를 추가함.
- **401이 아닌 실패를 로그아웃으로 오판**: `/api/holdings` 실패 시 프론트가 무조건 로그인 화면으로 돌아가서 에러 원인이 안 보였음(에러가 조용히 삼켜짐). 401만 로그아웃 처리하고, 그 외 실패는 화면에 에러 메시지를 띄우도록 수정 + 서버 쪽에 에러 로그(스택트레이스, 앱키/시크릿 제외) 추가.

### 코드 정리 (동작 변경 없음)
- 세션 쿠키에서 세션 ID를 뽑는 로직이 `_get_session`과 `/api/logout`에 중복돼 있던 것을 `_session_id_from_cookie` 헬퍼로 통합.
- `fetch_holdings_data`에서 `rows`를 두 번 순회해 `stock_rows`/`cash_stock_rows`를 만들던 것을 한 번의 순회로 통합.
- `import traceback`을 함수 내부에서 파일 상단으로 이동.
- `dashboard.html`: 국내/해외 종목 표에서 중복되던 "종목명(심볼)"/수량/수익률/지분율 셀 마크업을 `symbolQtyCells`/`rateShareCells` 헬퍼로 통합.
- (참고로 이 정리는 성능 최적화가 아니라 가독성/중복 제거 목적. 계좌별 API 호출을 병렬화하는 건 429 재발 위험 때문에 일부러 안 함.)

### 확인한 것 (실제 계좌로 테스트)
- 로그인 → 계좌/보유종목/예수금 조회 정상 동작.
- 현금 취급 종목 체크 → 저장 → 현금 표로 이동 확인.
- 해외 종목 원/$ 토글 동작 확인.
- 대시보드 수치와 토스 앱 화면을 스크린샷으로 실측 비교(해외 종목 7개 중 4개는 소수점까지 완전 일치, 3개는 수수료 반영 차이로 추정되는 소액 오차 — 위 "손익 필드" 결정 참고).

### 아직 없는 것 / 미해결
- **환차손익(매입 시점 환율 반영)은 API 한계로 미구현**: 토스 Open API에 매입 시점 적용 환율이나 환전 내역 조회가 없음(확인함, `GET /api/v1/exchange-rate`는 참고용 현재 환율만 제공). `GET /api/v1/orders`(주문/체결 내역)로 체결 시각(`filledAt`)은 알 수 있어서, 그 날짜의 과거 시장 환율(외부 소스)을 매칭하면 근사치는 만들 수 있으나, 매수/매도가 섞였을 때 잔여 물량의 원가를 재구성해야 해서 작업량이 꽤 큼 — 아직 손 안 댐.
- 주봉 이동평균선(MA), 관심종목, 리밸런싱, 분할매수 전략은 아직 없음(다음 단계).

## Log 3 — 2026-09-22 (README: 앱키/시크릿 발급 방법 문서화)

### 상태
- `README.md`를 비워둔 상태에서 채웠다. 토스증권 공식 Open API 문서(`developers.tossinvest.com`, `openapi.tossinvest.com/openapi-docs/*.md`)를 확인해서 앱키(client_id)/시크릿(client_secret) 발급 절차를 정리했다.

### 확인한 공식 정보 (출처: openapi.tossinvest.com/openapi-docs/overview.md, faq.md)
- 발급 위치: 토스증권 WTS 로그인 → **설정 > Open API** 메뉴에서 `client_id`/`client_secret` 발급.
- 발급 후 같은 메뉴 하단 **"허용 IP 관리"에 호출 IP를 등록해야 함** — 미등록 IP의 호출은 403으로 차단됨. 유동 IP 환경(가정용 인터넷 등)이면 IP가 바뀔 때마다 재등록이 필요할 수 있음.
- OAuth 토큰 발급: `POST https://openapi.tossinvest.com/oauth2/token` (`grant_type=client_credentials`, `client_id`, `client_secret`) — `server.py`의 기존 구현과 동일함을 재확인.
- 클라이언트(앱키)당 유효 토큰은 동시에 1개뿐(새로 발급하면 이전 토큰 즉시 무효화) — `../real/`과 같은 앱키를 동시에 쓰면 토큰이 서로 무효화될 수 있음(`real/CLAUDE.md`에 있던 내용과 일치).
- 계좌 보유 여부 등 신청 사전조건은 공식 문서에 명시되어 있지 않음(확인 안 됨으로 문서화하지 않음).

### README에 반영한 것
- 앱키/시크릿 발급 절차 4단계(WTS 로그인 → 메뉴 진입 → 발급 → 허용 IP 등록) + 공인 IP 확인 명령(`curl -s ifconfig.me`) 안내.
- `.env`가 필요 없고 로그인 화면에 바로 입력하면 된다는 점 명시(redo만의 방식).
- 설치/실행 방법, 파일 구성, 주의사항(앱키/시크릿 민감정보 취급, 아직 계좌 목록 조회까지만 구현됨).

## Log 2 — 2026-09-22 (토스 계좌 API 연동, 로그인 방식)

### 상태
- `server.py`(파이썬 표준 라이브러리 `http.server` + `requests`)와 `dashboard.html`의 로그인 화면으로 토스증권 Open API 연동을 구현했다.
- `.env`에 앱키/시크릿을 미리 넣어두는 방식이 아니라, 대시보드 화면에서 앱키/시크릿을 입력받아 그 자리에서 토큰을 발급받는 "로그인 방식"으로 만들었다(사용자 요청).

### 구현 내용
- `server.py`
  - `API_BASE = "https://openapi.tossinvest.com"`, `POST /oauth2/token`(`grant_type=client_credentials`)으로 access_token 발급 (`../real/portfolio.py`의 기존 구현과 동일한 엔드포인트/방식 재사용).
  - `POST /api/login`: 앱키/시크릿을 받아 토큰 발급 시도 → 성공 시 세션ID(`secrets.token_urlsafe`)를 발급하고 `HttpOnly` 쿠키로 내려줌. 앱키/시크릿/토큰은 **디스크에 저장하지 않고 서버 프로세스 메모리에만** 세션 단위로 보관(서버 재시작 시 소실 → 재로그인 필요).
  - `GET /api/session`: 로그인 여부 확인.
  - `GET /api/accounts`: 세션 쿠키 확인 후 `GET /api/v1/accounts` 호출 결과 반환. 401이면 저장된 앱키/시크릿으로 토큰 재발급 후 재시도.
  - `POST /api/logout`: 세션 삭제 + 쿠키 만료.
  - `real/web_server.py`와 동일한 보안 패턴 적용: Host 헤더 검사(모든 요청), POST는 Origin도 추가 검사, localhost(127.0.0.1) 전용, 포트 8767(`real`=8765, `demo`=8766과 겹치지 않게).
- `dashboard.html`
  - 로그인 카드: 앱키/시크릿 입력(비밀번호 타입), 로그인 버튼, 에러 메시지 영역.
  - 로그인 성공 시 로그인 카드를 숨기고 "연결됨" 화면으로 전환, 조회된 계좌 목록을 표시. 헤더에 로그아웃 버튼 추가.
  - 페이지 로드 시 `/api/session`으로 기존 세션 확인 후 화면 상태 결정.

### 확인한 것
- `python3 -c "import ast; ast.parse(...)"`로 `server.py` 문법 확인.
- 서버 기동 후 `curl`로 확인: `GET /` 200, `GET /api/session` `{"logged_in": false}`, 잘못된 Host 헤더로 `POST /api/login` → 403, 빈 값으로 `POST /api/login` → 400, **더미(무효) 앱키/시크릿으로 `POST /api/login` → 실제 토스 API가 401을 반환하고 서버가 "로그인 실패: 앱키/시크릿을 확인하세요."로 응답**하는 것까지 확인(엔드포인트가 실제로 살아있고 에러 처리가 동작함을 검증. 유효한 앱키/시크릿으로 로그인 성공 → 계좌 목록 조회까지는 아직 확인 안 됨, 실제 키가 필요).

### 아직 없는 것
- 유효한 앱키/시크릿으로의 실제 로그인 성공 확인(사용자가 직접 발급받은 키로 테스트 필요).
- 보유 종목/평가금액 등 포트폴리오 데이터 조회·표시 (다음 단계).
- 세션 만료 시 프론트엔드 안내(현재는 `/api/accounts` 401이면 조용히 로그인 화면으로 돌아감).

## Log 1 — 2026-09-21 (초기 골격)

### 상태
- 실질적인 구현은 없고, 파일 골격과 `dashboard.html` 헤더만 존재한다.

### 파일 현황
| 파일 | 상태 |
|---|---|
| `dashboard.html` | 34줄. 다크 테마 + 헤더(`<h1>토스증권 포트폴리오</h1>`)만 있음 |
| `CHANGELOG.md` | 이 로그부터 기록 시작 |
| `CLAUDE.md` | 현재 상태 요약 (Log 1 시점) |
| `docs/ARCHITECTURE.md` | 현재 구조만 기록 (설계는 미정) |
| `docs/STRATEGY.md` | 비어 있음 (정해진 전략 없음) |
| `README.md` | 비어 있음 (실행 가능한 결과물이 생기면 작성) |

### `dashboard.html` 구현 내용
- 순수 HTML + CSS 단일 파일 (JS, 프레임워크, 빌드 도구, 외부 라이브러리 없음)
- `lang="ko"`, UTF-8, viewport 메타, 제목 "토스증권 포트폴리오 대시보드"
- CSS 변수: `--bg: #0d1117`, `--border: #30363d`, `--text: #e6edf3`
- 전역 `box-sizing: border-box`
- body: 마진 0, `min-height: 100vh`, 폰트 `-apple-system → Malgun Gothic → Apple SD Gothic Neo → sans-serif`, 14px
- header: 패딩 `14px 20px`, 하단 1px 테두리, h1 18px

### 아직 없는 것
- JavaScript, 데이터 로딩, 차트/표
- 서버, API 연동
- 포트폴리오 계산 로직, 리밸런싱 로직

### 참고
- 같은 위치의 `../demo/`, `../real/`에 기존 구현(`portfolio.py`, `web_server.py`, `dashboard.html` 등)이 있다. `redo`가 이를 다시 만들기 위한 폴더인지는 아직 확인되지 않았다.
- git 저장소가 아니어서 커밋 이력은 없다. 변경 내역은 이 파일로 관리한다.
