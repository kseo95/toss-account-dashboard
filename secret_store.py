"""서버를 다시 켜도 FRED 키가 자동으로 연결되게 하는 보관소: 환경변수 FRED_API_KEY 또는 macOS 키체인. 선택 기능이다.

- 키를 프로젝트 파일(.env·json)에 평문으로 쓰지 않는다. 키체인 항목은 사용자가 화면에서 "키체인에 저장"을 직접 고른 경우에만 만들고, 화면에서 삭제할 수 있다.
- 읽기 우선순위: 환경변수 → 키체인. 화면에서 이번 세션에 입력한 키는 이보다 우선한다(server가 처리).
- 이 키는 서버 전체의 기본값이다(개인 PC에서 혼자 쓰는 용도). 여러 사람이 쓰는 배포에서는 환경변수·키체인을 비워 두고 각자 화면에 입력하게 한다.
- macOS `security` 명령은 비밀번호를 인자로만 받아서 저장하는 순간 같은 사용자의 프로세스 목록에 잠깐 보일 수 있다(읽기는 인자에 키가 없다).
- 키는 로그·예외·반환값(성공 여부 외)에 남기지 않는다. 외부 명령은 셸 없이 인자 목록으로만 실행한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

SERVICE = "toss-portfolio-fred"
ACCOUNT = "api-key"
ENV_NAME = "FRED_API_KEY"
TIMEOUT = 8


def keychain_supported() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _run(args: list[str], runner=subprocess.run):
    return runner(["security", *args], capture_output=True, text=True, timeout=TIMEOUT, check=False)


def read_default_key(is_valid, environ=None, runner=subprocess.run) -> tuple[str | None, str | None]:
    """서버 기본 키와 출처("env" | "keychain"). 형식이 맞지 않거나 없으면 (None, None). 키체인 오류는 없는 것으로 본다."""
    env = (environ if environ is not None else os.environ).get(ENV_NAME, "").strip()
    if env and is_valid(env):
        return env, "env"
    if keychain_supported():
        try:
            out = _run(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"], runner)
        except (OSError, subprocess.SubprocessError):
            return None, None
        key = (out.stdout or "").strip() if out.returncode == 0 else ""
        if key and is_valid(key):
            return key, "keychain"
    return None, None


def save_secret(key: str, runner=subprocess.run) -> bool:
    """키체인에 저장(이미 있으면 덮어씀). 성공 여부만 돌려준다."""
    if not keychain_supported() or not key:
        return False
    try:
        out = _run(["add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT, "-w", key], runner)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def delete_secret(runner=subprocess.run) -> bool:
    """키체인 항목 삭제. 없던 항목이어도 결과적으로 없으니 성공으로 본다."""
    if not keychain_supported():
        return False
    try:
        out = _run(["delete-generic-password", "-s", SERVICE, "-a", ACCOUNT], runner)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode in (0, 44)   # 44: 항목 없음
