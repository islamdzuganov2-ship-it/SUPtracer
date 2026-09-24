# -*- coding: utf-8 -*-
"""Первый запуск на пустых данных не падает ни в одной команде (КАЧ-04).

Команды запускаются настоящими процессами — так, как их запускает человек,
с пустым каталогом данных. Сбор токенов и репозиториев сюда не входит: он читает
журналы и диски машины и проверяется на живых данных гейтом."""
import os
import subprocess
import sys
import unittest

from base import DataCase, TOOLS

PM = os.path.join(TOOLS, "pm.py")


class EmptyData(DataCase):
    def run_cmd(self, *args, script=PM):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", TASKTREKER_DATA=self.data)
        r = subprocess.run([sys.executable, script] + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=120,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def assert_survives(self, *args, script=PM):
        code, out = self.run_cmd(*args, script=script)
        self.assertNotIn("Traceback", out, "%s: %s" % (" ".join(args), out[-600:]))
        self.assertIn(code, (0, 1), " ".join(args))
        return out

    def test_commands_on_empty_data(self):
        note = self.note("n.md", "# Заметка\nНужно что-то сделать.\n\n- [ ] первая задача\n")
        for args in (["context"], ["context", "--budget", "300"], ["status"], ["comments"],
                     ["inbox"], ["tree"], ["recall", "кеш"], ["forget", "mem-нет"],
                     ["vault", "status"], ["vault", "pull"], ["vault", "push"],
                     ["ingest", note], ["apply", "IN-1"], ["reject", "IN-9"]):
            with self.subTest(args=args):
                self.assert_survives(*args)

    def test_dashboard_builds_on_empty_data(self):
        dest = os.path.join(self.tmp, "d.html")
        self.assert_survives(dest, script=os.path.join(TOOLS, "build_dashboard.py"))
        self.assertGreater(os.path.getsize(dest), 10000)

    def test_hook_on_empty_data(self):
        out = self.assert_survives(script=os.path.join(TOOLS, "hook.py"))
        self.assertTrue(out.strip())


if __name__ == "__main__":
    unittest.main()
