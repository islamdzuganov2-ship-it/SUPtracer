# -*- coding: utf-8 -*-
"""Брифинг, проект по каталогу, хук начала сессии (ПАМ-05, ПАМ-06, ПАМ-11, ИНТ-02, ИНТ-03)."""
import json
import os
import subprocess
import sys
import unittest

from base import DataCase, TOOLS


class ProjectForPath(DataCase):
    def setUp(self):
        super().setUp()
        self.P = self.projects()["projects"]
        self.alpha = self.P["alpha-api"]["path"]

    def detect(self, path):
        return self.t.context.project_for_path(path, self.P)

    def test_project_dir_and_nested(self):                              # ИНТ-02
        self.assertEqual(self.detect(self.alpha), "alpha-api")
        self.assertEqual(self.detect(os.path.join(self.alpha, "src", "api")), "alpha-api")

    def test_worktrees_inside_and_beside(self):
        self.assertEqual(self.detect(os.path.join(self.alpha, ".claude", "worktrees", "w1")), "alpha-api")
        self.assertEqual(self.detect(self.alpha + ".worktrees/w2"), "alpha-api")

    def test_backslashes_and_case(self):
        self.assertEqual(self.detect(self.alpha.replace("/", "\\").upper()), "alpha-api")

    def test_prefix_of_other_name_is_not_a_match(self):
        self.assertIsNone(self.detect(self.alpha + "-old"))
        self.assertIsNone(self.detect(self.tmp))

    def test_deepest_project_wins(self):
        def nest(d):
            d["projects"]["inner"] = dict(d["projects"]["beta-tool"],
                                          path=d["projects"]["alpha-api"]["path"] + "/plugins/inner")
        P = self.projects(nest)["projects"]
        self.assertEqual(self.t.context.project_for_path(P["inner"]["path"] + "/x", P), "inner")


class Brief(DataCase):
    def setUp(self):
        super().setUp()
        self.projects()
        m = self.t.memory
        for i in range(60):
            m.save(m.new_record("Решение %d о кеше и ролях партнёров" % i, "Тело записи. " * 40,
                                project="alpha-api", kind="решение"))
        m.save(m.new_record("Отчёты собираются по ночам", "Тело. " * 20, project="beta-tool", kind="факт"))
        m.build_index()

    def test_budget_includes_everything(self):                         # ПАМ-05
        est = self.t.mdparse.est_tokens
        for budget in (300, 800, 1200, 3000):
            out = self.t.context.build(budget=budget)
            self.assertLessEqual(est(out), budget, budget)

    def test_focus_expands_one_project(self):                          # ПАМ-06
        out = self.t.context.build(budget=4000, focus="beta-tool")
        self.assertIn("· Утилита Бета [beta-tool]", out)
        self.assertIn("ОСТАЛЬНОЙ ПОРТФЕЛЬ", out)
        self.assertIn("Сервис Альфа [alpha-api] ф60%", out)            # остальные — одной строкой
        self.assertNotIn("AL-01", out)                                   # чужой бэклог не раскрыт
        self.assertIn("Отчёты собираются по ночам", out)
        self.assertNotIn("Решение 7 о кеше", out)                       # чужая память не лезет

    def test_focus_alerts_only_own(self):
        out = self.t.context.build(budget=4000, focus="beta-tool")
        self.assertNotIn("пароль лежит в конфиге", out)
        full = self.t.context.build(budget=4000)
        self.assertIn("пароль лежит в конфиге", full)

    def test_no_invented_saving_on_empty_data(self):                   # ПАМ-11
        import shutil
        shutil.rmtree(self.data)
        os.makedirs(self.data)
        out = self.t.context.build(budget=4000)
        self.assertIn("Данных в системе пока мало", out)
        self.assertNotRegex(out, r"-\d+% экономии")

    def test_saving_is_reported(self):
        out = self.t.context.build(budget=1500)
        self.assertRegex(out.splitlines()[-1], r"\(\d+% экономии\)")


class Hook(DataCase):
    def run_hook(self, stdin):
        """Хук отвечает JSON с hookSpecificOutput.additionalContext — возвращаем сам текст."""
        env = dict(os.environ, PYTHONIOENCODING="utf-8", TASKTREKER_DATA=self.data)
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "hook.py")], input=stdin,
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return r.returncode, out["hookSpecificOutput"]["additionalContext"], r.stderr

    def test_text_mode_for_humans(self):
        self.projects()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", TASKTREKER_DATA=self.data)
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "hook.py"), "--text"],
                           input=json.dumps({"cwd": self.tmp}), capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=60)
        self.assertTrue(r.stdout.startswith("TaskTreker: каталог вне портфеля"))

    def test_outside_portfolio_one_line(self):                          # ИНТ-03
        self.projects()
        code, out, err = self.run_hook(json.dumps({"cwd": self.tmp}))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("вне портфеля", out)

    def test_inside_project_brief_within_budget(self):
        P = self.projects()["projects"]
        code, out, err = self.run_hook(json.dumps({"cwd": P["alpha-api"]["path"] + "/src",
                                                   "hook_event_name": "SessionStart"}))
        self.assertEqual(code, 0, err)
        self.assertIn("проект alpha-api", out)
        self.assertIn("tt_recall", out)
        sys.path.insert(0, TOOLS)
        import mdparse
        self.assertLessEqual(mdparse.est_tokens(out.strip()), self.t.context.HOOK_BUDGET)

    def test_broken_input_never_fails(self):
        for stdin in ("", "мусор", '{"cwd": 5}', '{"cwd":"C:\\Users\\x"}'):
            code, out, err = self.run_hook(stdin)
            self.assertEqual(code, 0, stdin)
            self.assertNotIn("Traceback", out + err, stdin)

    def test_broken_code_never_fails(self):
        # Сломанная копия модуля брифинга рядом с хуком: хук обязан выжить.
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix="tt-hook-")
        try:
            shutil.copy(os.path.join(TOOLS, "hook.py"), d)
            with open(os.path.join(d, "context.py"), "w", encoding="utf-8") as f:
                f.write("это не питон (\n")
            r = subprocess.run([sys.executable, os.path.join(d, "hook.py")], input="{}",
                               capture_output=True, text=True, encoding="utf-8", timeout=60,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(r.returncode, 0)
            self.assertIn("брифинг не собрался", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
