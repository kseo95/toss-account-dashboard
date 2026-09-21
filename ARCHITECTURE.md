# ARCHITECTURE

## 현재 구조 (Log 1, 2026-09-21)

```
redo/
├── dashboard.html      # 프론트: HTML + CSS 단일 파일 (헤더만 있음)
├── README.md           # 비어 있음
├── CLAUDE.md
├── CHANGELOG.md
└── docs/
    ├── ARCHITECTURE.md
    └── STRATEGY.md     # 비어 있음
```

## 프론트엔드
- 순수 HTML/CSS 단일 파일. JS, 프레임워크, 빌드 도구, 외부 라이브러리 없음.
- 다크 테마는 CSS 변수(`--bg`, `--border`, `--text`)로 관리한다.

## 미정
- 백엔드/서버 유무, 데이터 소스, 계산 로직의 위치는 아직 정해지지 않았다.
- 결정되면 이 문서에 추가하고 `CHANGELOG.md`에 기록한다.
