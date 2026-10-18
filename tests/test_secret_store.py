"""FRED 키 자동 연결 보관소: 환경변수 → 키체인 읽기 순서, 형식 검사, 저장·삭제 인자(셸 없이 목록)와 결과, 비지원 환경과 오류 격리. 실제 키체인은 건드리지 않는다."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from helpers import server  # noqa: F401  (helpers가 import 경로를 잡는다)
import secret_store as ss

KEY = "c" * 32
valid = lambda k: len(k) == 32 and k.isalnum()


class Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


class Recorder:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or Result(), error, []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.error:
            raise self.error
        return self.result


class SupportedMixin:
    def setUp(self):
        p = mock.patch.object(ss, "keychain_supported", return_value=True)
        p.start()
        self.addCleanup(p.stop)


class ReadTest(SupportedMixin, unittest.TestCase):
    def test_environment_comes_first_and_invalid_values_are_ignored(self):
        rec = Recorder(Result(KEY + "\n"))
        self.assertEqual(ss.read_default_key(valid, {"FRED_API_KEY": "d" * 32}, rec), ("d" * 32, "env"))
        self.assertEqual(rec.calls, [])                                                       # 환경변수가 있으면 키체인은 부르지 않는다
        self.assertEqual(ss.read_default_key(valid, {"FRED_API_KEY": "짧음"}, rec), (KEY, "keychain"))   # 형식이 틀리면 건너뛰고 키체인으로

    def test_keychain_read_uses_find_without_shell_and_strips_newline(self):
        rec = Recorder(Result(KEY + "\n"))
        self.assertEqual(ss.read_default_key(valid, {}, rec), (KEY, "keychain"))
        args, kw = rec.calls[0]
        self.assertEqual(args[:2], ["security", "find-generic-password"])                      # 셸 문자열이 아니라 인자 목록
        self.assertIn("-w", args)
        self.assertNotIn(KEY, " ".join(args))                                                  # 읽을 때 인자에 키가 없다
        self.assertNotIn("shell", kw)

    def test_missing_or_bad_keychain_entries_and_errors_mean_no_key(self):
        self.assertEqual(ss.read_default_key(valid, {}, Recorder(Result("", 44))), (None, None))        # 항목 없음
        self.assertEqual(ss.read_default_key(valid, {}, Recorder(Result("이상한값\n", 0))), (None, None))   # 형식 불량
        self.assertEqual(ss.read_default_key(valid, {}, Recorder(error=OSError("x"))), (None, None))
        self.assertEqual(ss.read_default_key(valid, {}, Recorder(error=subprocess.TimeoutExpired("security", 8))), (None, None))

    def test_unsupported_platform_never_runs_a_command(self):
        rec = Recorder(Result(KEY))
        with mock.patch.object(ss, "keychain_supported", return_value=False):
            self.assertEqual(ss.read_default_key(valid, {}, rec), (None, None))
            self.assertFalse(ss.save_secret(KEY, rec))
            self.assertFalse(ss.delete_secret(rec))
        self.assertEqual(rec.calls, [])


class SaveDeleteTest(SupportedMixin, unittest.TestCase):
    def test_save_overwrites_existing_item_and_reports_only_success(self):
        rec = Recorder(Result("", 0))
        self.assertTrue(ss.save_secret(KEY, rec))
        args, _ = rec.calls[0]
        self.assertEqual(args[:3], ["security", "add-generic-password", "-U"])                # -U: 이미 있으면 갱신
        self.assertIn(ss.SERVICE, args)
        self.assertFalse(ss.save_secret(KEY, Recorder(Result("", 1))))
        self.assertFalse(ss.save_secret(KEY, Recorder(error=OSError("x"))))
        self.assertFalse(ss.save_secret("", Recorder()))                                       # 빈 키는 저장하지 않는다

    def test_delete_treats_a_missing_item_as_done(self):
        self.assertTrue(ss.delete_secret(Recorder(Result("", 0))))
        self.assertTrue(ss.delete_secret(Recorder(Result("", 44))))
        self.assertFalse(ss.delete_secret(Recorder(Result("", 1))))
        self.assertFalse(ss.delete_secret(Recorder(error=subprocess.SubprocessError("x"))))

    def test_nothing_returns_the_key(self):
        rec = Recorder(Result("", 0))
        for out in (ss.save_secret(KEY, rec), ss.delete_secret(rec)):
            self.assertIsInstance(out, bool)


if __name__ == "__main__":
    unittest.main()
