# CHANGELOG

최신 로그가 위에 오도록 기록한다.

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
