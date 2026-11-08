"""Песочница пользователя ОС для недоверенного кода (07: A2-07; SEC-009 п.2, SEC-TC-16/17).

Проводка проверяется всегда. Настоящая изоляция — только если владелец завёл пользователя песочницы и сохранил
учётные данные `tasktreker-sandbox` в диспетчере Windows; без них SEC-TC-16/17 пропускаются с причиной.
"""
import io
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

import base  # noqa: F401 — путь к tools/
import job_runner as jr

HERE = Path(__file__).resolve().parent
ROOT = jr.sandbox_root(HERE)
FOUND, WHY = jr.sandbox_user(HERE) if ROOT else (None, 'тесты вне каталога рабочих копий')


class Wiring(unittest.TestCase):
    def test_sandbox_root_only_worktree_dirs(self):
        roots = [Path('C:/r/.claude/worktrees'), Path('C:/r.worktrees')]
        self.assertEqual(jr.sandbox_root(Path('C:/r/.claude/worktrees/t1/src'), roots), roots[0].resolve())
        self.assertEqual(jr.sandbox_root(Path('C:/r.worktrees/deploy-x'), roots), roots[1].resolve())
        self.assertIsNone(jr.sandbox_root(Path('C:/r/data'), roots))
        # Чужой «*.worktrees» (временный репозиторий теста в TEMP владельца) — не песочница: туда ACL не выданы.
        self.assertIsNone(jr.sandbox_root(Path('C:/tmp/x/repo.worktrees/deploy-test'), roots))
        main = jr.sandbox_roots()[0]
        self.assertEqual(main.parts[-2:], ('.claude', 'worktrees'))
        self.assertNotIn('.claude', main.parts[:-2])                      # корень основной установки, не рабочей копии

    def test_no_credential_falls_back_to_job_and_says_why(self):
        with mock.patch.object(jr, '_credential', return_value=None, create=True):
            found, why = jr.sandbox_user(HERE)
            self.assertIsNone(found)
            if ROOT and os.name == 'nt':
                self.assertIn(jr.CRED_TARGET, why)
            with mock.patch.dict(os.environ, TASKTREKER_SANDBOX_USER='auto'):
                r = jr.run([sys.executable, '-I', '-c', 'print("ok")'], cwd=str(HERE))
            self.assertEqual((r.returncode, r.stdout.strip()), (0, 'ok'))
            self.assertIn(r.sandbox, ('job', 'rlimit'))

    def test_require_mode_refuses_without_sandbox(self):
        with mock.patch.object(jr, '_credential', return_value=None, create=True), \
                mock.patch.dict(os.environ, TASKTREKER_SANDBOX_USER='require'):
            with self.assertRaises(OSError) as e:
                jr.run([sys.executable, '-c', 'print(1)'], cwd=str(HERE))
        self.assertIn('песочница обязательна', str(e.exception))

    def test_off_mode_never_uses_credentials(self):
        with mock.patch.dict(os.environ, TASKTREKER_SANDBOX_USER='0'):
            self.assertEqual(jr.sandbox_user(HERE)[0], None)

    def test_python_launcher_replaced_with_server_interpreter(self):
        self.assertEqual(jr.sandbox_cmd(['py', '-3', '-m', 'unittest']), [sys.executable, '-m', 'unittest'])
        self.assertEqual(jr.sandbox_cmd(['python', '-c', 'x']), [sys.executable, '-c', 'x'])
        self.assertEqual(jr.sandbox_cmd(['git', 'status']), ['git', 'status'])
        self.assertTrue(jr.sandbox_cmd('py -m pytest').endswith(' -m pytest'))

    def test_output_routed_like_subprocess(self):
        self.assertEqual(jr._emit('привет'.encode(), subprocess.PIPE, True, 'utf-8'), 'привет')
        self.assertEqual(jr._emit(b'x', subprocess.PIPE, False, 'utf-8'), b'x')
        buf = io.BytesIO(); buf.mode = 'wb'
        jr._emit(b'log', buf, False, 'utf-8')
        self.assertEqual(buf.getvalue(), b'log')
        s = io.StringIO()
        jr._emit('лог'.encode(), s, True, 'utf-8')
        self.assertEqual(s.getvalue(), 'лог')


@unittest.skipUnless(FOUND, 'песочница недоступна: %s' % WHY)
class SandboxIsolation(unittest.TestCase):
    """SEC-TC-16/17 на настоящем пользователе песочницы и ACL владельца."""

    def setUp(self):
        self.wd = ROOT / ('.sandbox-test-' + uuid.uuid4().hex[:8])
        self.wd.mkdir()
        self.addCleanup(shutil.rmtree, self.wd, True)

    def py(self, code, *args):
        r = jr.run([sys.executable, '-I', '-c', code, *map(str, args)], cwd=str(self.wd), timeout=120)
        self.assertEqual(r.sandbox, 'user')
        return r

    def test_sec_tc_16_owner_data_not_readable(self):
        repo = ROOT.parent.parent if ROOT.name == 'worktrees' else ROOT.with_name(ROOT.name[:-len('.worktrees')])
        target = repo / 'data' / 'projects.json'
        if not target.is_file():
            self.skipTest('нет %s' % target)
        r = self.py('import sys\ntry:\n open(sys.argv[1],"rb").read(1); print("READ")\nexcept PermissionError: print("DENIED")',
                    target)
        self.assertEqual(r.stdout.strip(), 'DENIED', r.stderr)

    def test_sec_tc_17_write_only_inside_worktree(self):
        outside = Path.home() / ('tt-sandbox-probe-%s.txt' % uuid.uuid4().hex[:8])
        self.addCleanup(lambda: outside.exists() and outside.unlink())
        code = ('import sys,pathlib\ntry:\n pathlib.Path(sys.argv[1]).write_text("x"); print("OUT-WRITTEN")\n'
                'except PermissionError: print("OUT-DENIED")\npathlib.Path("inside.txt").write_text("ok"); print("IN-OK")\n'
                'import os; print(os.environ.get("USERPROFILE"), os.environ.get("TASKTREKER_SANDBOX"))')
        r = self.py(code, outside)
        lines = r.stdout.split()
        self.assertEqual(lines[:2], ['OUT-DENIED', 'IN-OK'], r.stderr)
        self.assertFalse(outside.exists())
        self.assertEqual((self.wd / 'inside.txt').read_text(), 'ok')
        profile = lines[2]
        self.assertNotEqual(Path(profile), Path.home())                      # профиль владельца не передан —
        self.assertIn(jr.IO_DIR, Path(profile).parts)                        # у песочницы свой дом в каталоге обмена
        self.assertEqual(lines[-1], 'user')

    def test_limits_still_apply_under_sandbox(self):
        r = jr.run([sys.executable, '-I', '-c', 'b = bytearray(600 * 1024 * 1024); print("выделено")'],
                   cwd=str(self.wd), mem_mb=200, timeout=120)
        self.assertEqual(r.sandbox, 'user')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('выделено', r.stdout)


if __name__ == '__main__':
    unittest.main()
