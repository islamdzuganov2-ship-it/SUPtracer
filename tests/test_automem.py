# -*- coding: utf-8 -*-
"""Импорт авто-памяти ассистента (ИНТ-07)."""
import os
import unittest

from base import DataCase

NOTE = """---
name: {name}
description: "{desc}"
metadata:
  node_type: memory
  type: {type}
  modified: 2026-09-20T10:00:00.000Z
---

{body}
"""


class AutoMemory(DataCase):
    def setUp(self):
        super().setUp()
        self.P = self.projects()["projects"]
        self.root = os.path.join(self.tmp, "claude-projects")
        slug = self.t.automem.slug
        self.alpha = slug(self.P["alpha-api"]["path"])
        self.beta_wt = slug(self.P["beta-tool"]["path"]) + "--claude-worktrees-fix-1a2b3c"
        self.write(self.alpha, "MEMORY.md", "# Memory Index\n\n- [x](x.md) — оглавление\n")
        self.write(self.alpha, "feedback_demo.md", NOTE.format(
            name="feedback-demo", type="feedback", desc="Показывать работу, а не пересказывать её",
            body="Пользователь просит запускать и показывать результат, а не утверждать, что тесты прошли."))
        self.write(self.alpha, "stack.md", NOTE.format(
            name="stack", type="project", desc="Стек Альфы и вход на стенд",
            body="Docker-стек живой. Вход на стенд: password: hunter2-very-secret-value"))
        self.write(self.beta_wt, "user_role.md", NOTE.format(
            name="user-role", type="user", desc="Пользователь сам проверяет отчёты",
            body="Отчёты сверяет вручную перед отправкой."))
        self.write("C--Somewhere-else", "ref.md", NOTE.format(
            name="ref", type="reference", desc="Где лежат макеты",
            body="Макеты — в общей папке дизайна."))

    def write(self, slug, name, text):
        d = os.path.join(self.root, slug, "memory")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)
        return os.path.join(d, name)

    def run_import(self, **kw):
        res, _ = self.quiet(self.t.automem.run, self.root, **kw)
        return res

    def records(self):
        m = self.t.memory
        return {r["title"]: r for r in m.load_all()}

    def test_import_maps_projects_and_kinds(self):                      # ИНТ-07
        res = self.run_import()
        self.assertEqual(res["files"], 4)                              # MEMORY.md — не факт
        recs = self.records()
        demo = recs["Показывать работу, а не пересказывать её"]
        self.assertEqual((demo["project"], demo["kind"]), ("alpha-api", "требование"))
        self.assertEqual(demo["created"], "2026-09-20")
        self.assertEqual(recs["Пользователь сам проверяет отчёты"]["project"], "beta-tool")   # рабочее дерево
        self.assertEqual(recs["Где лежат макеты"]["project"], "портфель")
        group = recs["Авто-память ассистента: Сервис Альфа"]
        self.assertEqual(demo["parent"], group["id"])

    def test_secrets_are_redacted(self):                                # БЕЗ-05
        res = self.run_import()
        body = self.records()["Стек Альфы и вход на стенд"]["body"]
        self.assertNotIn("hunter2-very-secret-value", body)
        self.assertTrue(res["redacted"])

    def test_reimport_does_not_duplicate(self):
        self.run_import()
        before = self.memory_files()
        self.run_import()
        self.assertEqual(self.memory_files(), before)

    def test_vanished_file_retires_its_record(self):                    # ПАМ-09
        self.run_import()
        os.remove(os.path.join(self.root, self.alpha, "memory", "stack.md"))
        res = self.run_import()
        self.assertEqual(res["retired"], 1)
        rec = self.records()["Стек Альфы и вход на стенд"]
        self.assertEqual(rec["status"], "устарела")

    def test_visible_in_project_brief(self):                            # ИНТ-07
        self.run_import()
        b = self.t.memory.brief(budget=1200, project="alpha-api")
        self.assertIn("Показывать работу, а не пересказывать её", b)

    def test_dry_run_writes_nothing(self):
        self.run_import(dry=True)
        self.assertEqual(self.memory_files(), [])

    def test_source_files_are_not_changed(self):
        p = os.path.join(self.root, self.alpha, "memory", "stack.md")
        with open(p, encoding="utf-8") as f:
            before = f.read()
        self.run_import()
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
