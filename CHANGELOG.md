# CHANGELOG

최신 로그가 위에 오도록 기록한다.

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
