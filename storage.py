"""사용자별 저장소.

웹 서비스로 배포할 것을 염두에 두고, 설정·기록 파일을 서버 하나에 공용으로 두지 않고 사용자마다
data/users/<user_id>/ 아래에 따로 둔다. user_id는 토스 계좌번호(가장 작은 accountSeq 계좌)를 서버
비밀값으로 HMAC한 값 — 앱키는 재발급하면 바뀌지만 계좌번호는 그대로이고, 폴더 이름만 봐서는 계좌번호를
알 수 없다. data/는 통째로 .gitignore.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from common import to_float

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"
SERVER_SECRET_FILE = DATA_DIR / "server_secret"
LEGACY_MIGRATED_MARKER = DATA_DIR / "legacy_migrated"
USER_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")

# 기능 모듈이 쓰는 파일 이름들. 사용자별 저장소 도입 전(redo/ 바로 아래)에 있던 파일을 옮길 때도 이 목록을 쓴다.
USER_DATA_FILES = ["cash_symbols.json", "rebalance.json", "watchlist.json", "ma_rules.json",
                   "strategy_templates.json", "strategy_state.json", "weekly_report.json"]

_secret_lock = threading.Lock()
_migration_lock = threading.Lock()
_file_locks: dict[Path, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _file_locks_guard:
        return _file_locks.setdefault(path, threading.Lock())


class UserStore:
    """사용자 한 명의 파일 위치(data/users/<user_id>/). 파일은 저장할 때 처음 생긴다."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, name: str) -> Path:
        return self.root / name

    def read_json(self, name: str):
        """파일이 없거나 JSON이 깨졌으면 None — 호출하는 쪽이 기본값을 쓴다(직전 상태를 지우지 않음)."""
        try:
            return json.loads(self.path(name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def write_json(self, name: str, data) -> None:
        """임시 파일에 쓰고 교체(원자적). 같은 파일 동시 저장은 파일별 잠금으로 막는다."""
        path = self.path(name)
        with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)


def _server_secret() -> bytes:
    """user_id를 만들 때 쓰는 서버 비밀값. 처음 한 번 만들어 data/server_secret(권한 600)에 둔다."""
    with _secret_lock:
        try:
            return bytes.fromhex(SERVER_SECRET_FILE.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_bytes(32)
            SERVER_SECRET_FILE.write_text(secret.hex(), encoding="utf-8")
            os.chmod(SERVER_SECRET_FILE, 0o600)
            return secret


def user_id_for_accounts(accounts: list[dict]) -> str:
    """계좌 목록 → user_id. 계좌번호 자체는 저장하지 않고 HMAC 값만 폴더 이름으로 쓴다."""
    valid = [a for a in accounts if str(a.get("accountNo") or "").strip()]
    if not valid:
        raise ValueError("계좌번호를 찾을 수 없습니다.")
    primary = min(valid, key=lambda a: to_float(a.get("accountSeq"), float("inf")))
    return hmac.new(_server_secret(), str(primary["accountNo"]).strip().encode(), hashlib.sha256).hexdigest()[:24]


def store_for_user(user_id: str) -> UserStore:
    if not USER_ID_PATTERN.fullmatch(user_id):  # 경로 조작 방지(세션 값은 서버가 만들지만 방어적으로)
        raise ValueError("잘못된 user_id")
    return UserStore(USERS_DIR / user_id)


def store_for(session: dict) -> UserStore:
    return store_for_user(session["user_id"])


def migrate_legacy_files(store: UserStore) -> list[str]:
    """사용자별 저장소 도입 전(redo/ 바로 아래)의 설정 파일을 **처음 로그인한 사용자 한 명에게만** 옮긴다.
    그 뒤 로그인하는 다른 사용자는 빈 설정으로 시작. 한 번 옮기면 표시 파일을 남겨 다시 하지 않는다."""
    with _migration_lock:
        if LEGACY_MIGRATED_MARKER.exists():
            return []
        store.root.mkdir(parents=True, exist_ok=True)
        moved = []
        for name in USER_DATA_FILES:
            src, target = BASE_DIR / name, store.path(name)
            if src.exists() and not target.exists():
                src.replace(target)
                moved.append(name)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LEGACY_MIGRATED_MARKER.write_text(json.dumps({"moved": moved, "at": time.time()}), encoding="utf-8")
        return moved
