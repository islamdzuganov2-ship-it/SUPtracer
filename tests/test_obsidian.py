# -*- coding: utf-8 -*-
"""Обмен с хранилищем Obsidian (ОБС-01 … ОБС-06)."""
import os
import unittest

from base import DataCase
from test_ingest import NOTE_API


class Vault(DataCase):
    def setUp(self):
        super().setUp()
        self.projects()
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, ".obsidian"))
        self.write("Проекты/api-kesh.md", NOTE_API)
        self.write("Входящие/отчёты.md", "# Ускорить отчёты\n\nНужно ускорить конвертер отчётов CSV в Excel.\n")
        self.write("Личное/дневник.md", "# Дневник\nничего про проекты\n")
        self.quiet(self.t.obsidian.cmd_init, [self.vault])

    def write(self, rel, text):
        p = os.path.join(self.vault, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def read(self, rel):
        with open(os.path.join(self.vault, rel), encoding="utf-8") as f:
            return f.read()

    def pull(self, *args):
        return self.quiet(self.t.obsidian.cmd_pull, list(args))[1]

    def push(self, *args):
        return self.quiet(self.t.obsidian.cmd_push, list(args))[1]

    def test_init_records_vault(self):                                  # ОБС-01
        conf = self.jread("obsidian.json")
        self.assertEqual(conf["vault"], self.vault.replace("\\", "/"))
        self.assertEqual(conf["export"]["folder"], "TaskTreker")

    def test_scan_respects_folders(self):
        c = self.t.obsidian.conf()
        c["import"]["folders"] = ["Проекты", "Входящие"]
        self.jwrite("obsidian.json", c)
        found = sorted(self.t.obsidian.scan(self.t.obsidian.conf()))
        self.assertEqual(found, ["Входящие/отчёты.md", "Проекты/api-kesh.md"])

    def test_pull_is_incremental(self):                                 # ОБС-02
        out = self.pull()
        self.assertIn("изменилось: 3", out)
        self.assertIn("изменилось: 0", self.pull())
        self.write("Входящие/отчёты.md", "# Ускорить отчёты\n\nНужно ускорить конвертер ещё сильнее.\n")
        self.assertIn("изменилось: 1", self.pull())

    def test_push_writes_linked_pages(self):                            # ОБС-03
        self.pull()
        # Документ не из хранилища: его запись выгружается заметкой памяти.
        doc = self.note("решение.md", "---\nproject: alpha-api\n---\n# Кеш в Redis\n\n"
                                      "Решили держать кеш ответов в Redis, а не в памяти процесса.\n\n"
                                      "## Сроки\n\nКеш должен жить не дольше пяти минут, иначе роли "
                                      "партнёров устаревают.\n")
        self.quiet(self.t.ingest.run, [doc], quiet=True)
        self.quiet(self.t.tree.build)
        self.push()
        page = self.read("TaskTreker/Проекты/Сервис Альфа.md")
        self.assertIn("tasktreker: generated", page)
        self.assertIn("[[Портфель TaskTreker]]", page)
        self.assertIn("- [ ] **AL-01**", page)
        self.assertIn("- [x] **AL-02**", page)
        # Запись из хранилища ведёт на исходную заметку, а не на свою копию.
        self.assertIn("[[Проекты/api-kesh|Вынести кеш эндпоинтов из приложения]]", page)
        self.assertIn("[[решение — Кеш в Redis]]", page)
        notes = os.listdir(os.path.join(self.vault, "TaskTreker", "Память"))
        self.assertFalse(any(n.startswith("запрос — Вынести кеш") for n in notes), notes)
        linked = self.read("TaskTreker/Память/требование — Сроки.md")
        self.assertIn("[[Сервис Альфа]]", linked)
        self.assertIn("Выросло из: [[решение — Кеш в Redis]]", linked)
        tree = self.read("TaskTreker/Дерево работ.md")
        self.assertIn("[[Проекты/api-kesh#Требования|Требования]]", tree)   # раздел исходной заметки

    def test_push_is_idempotent(self):                                  # ОБС-06
        self.push()
        self.assertIn("всё уже актуально", self.push())
        self.assertTrue(self.jread("vault_state.json")["pushed_at"])    # ОБН-05: свежесть выгрузки

    def test_generated_notes_are_never_read_back(self):                 # ОБС-04
        self.pull()
        self.push()
        before = self.memory_files()
        self.assertIn("изменилось: 0", self.pull())
        self.assertEqual(self.memory_files(), before)
        self.assertNotIn("TaskTreker/", "".join(self.t.obsidian.scan(self.t.obsidian.conf())))

    def test_checkbox_changes_task_status_only(self):                   # ОБС-05
        self.push()
        page = os.path.join(self.vault, "TaskTreker", "Проекты", "Сервис Альфа.md")
        with open(page, encoding="utf-8") as f:
            text = f.read().replace("- [ ] **AL-01**", "- [x] **AL-01**")
        with open(page, "w", encoding="utf-8") as f:
            f.write(text)
        out = self.pull()
        self.assertIn("alpha-api/AL-01: queued -> done", out)
        backlog = self.jread("projects.json")["projects"]["alpha-api"]["backlog"]
        self.assertEqual([(b["id"], b["status"]) for b in backlog], [("AL-01", "done"), ("AL-02", "done")])

    PAGE = "TaskTreker/Проекты/Сервис Альфа.md"

    def edit_page(self, fn):
        p = os.path.join(self.vault, *self.PAGE.split("/"))
        with open(p, encoding="utf-8") as f:
            text = fn(f.read())
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def copies(self):
        d = os.path.join(self.vault, "TaskTreker", "Проекты")
        return [n for n in os.listdir(d) if "— правка" in n]

    def test_human_edit_survives_push_as_copy(self):                    # ОБС-07
        self.push()
        self.edit_page(lambda t: t + "\nМоя мысль про кеш, которую нельзя терять.\n")
        # Бэклог изменился — страница будет перерисована.
        def m(d):
            d["projects"]["alpha-api"]["backlog"][0]["title"] = "Добавить кеш ответов и роли"
        self.projects(m)
        out = self.push()
        self.assertIn("ваш текст сохранён копиями", out)
        [copy] = self.copies()
        with open(os.path.join(self.vault, "TaskTreker", "Проекты", copy), encoding="utf-8") as f:
            kept = f.read()
        self.assertIn("Моя мысль про кеш", kept)
        self.assertNotIn("tasktreker: generated", kept)                # копия — уже заметка человека
        self.assertNotIn("Моя мысль про кеш", self.read(self.PAGE))     # страница перерисована

    def test_ticks_are_not_a_text_edit_and_are_not_lost(self):          # ОБС-07, ОБС-05
        self.push()
        self.edit_page(lambda t: t.replace("- [ ] **AL-01**", "- [x] **AL-01**"))
        out = self.push()                                               # без pull
        self.assertEqual(self.copies(), [])
        self.assertIn("alpha-api/AL-01: queued -> done", out)
        self.assertIn("- [x] **AL-01**", self.read(self.PAGE))

    def test_system_side_status_change_is_not_rolled_back(self):        # ОБС-05
        # Страница выгружена с AL-01 «в работе»; потом задачу закрыли в системе.
        # Отметку «[/]» на странице записала система — это не ввод человека.
        def wip(d):
            d["projects"]["alpha-api"]["backlog"][0]["status"] = "in-progress"
        self.projects(wip)
        self.push()
        self.assertIn("- [/] **AL-01**", self.read(self.PAGE))
        def done(d):
            d["projects"]["alpha-api"]["backlog"][0]["status"] = "done"
        self.projects(done)
        out = self.push() + self.pull()
        self.assertNotIn("AL-01: done ->", out)
        status = {b["id"]: b["status"] for b in self.jread("projects.json")["projects"]["alpha-api"]["backlog"]}
        self.assertEqual(status["AL-01"], "done")
        self.assertIn("- [x] **AL-01**", self.read(self.PAGE))

    def test_tick_cannot_reopen_a_closed_task(self):                    # ОБС-05
        self.push()
        self.edit_page(lambda t: t.replace("- [x] **AL-02**", "- [/] **AL-02**"))
        self.pull()
        status = {b["id"]: b["status"] for b in self.jread("projects.json")["projects"]["alpha-api"]["backlog"]}
        self.assertEqual(status["AL-02"], "done")

    def test_repeated_push_makes_no_copies(self):                       # ОБС-07
        self.push()
        self.push()
        self.push()
        self.assertEqual(self.copies(), [])

    def test_user_notes_are_never_changed(self):                        # ОБС-08
        def snapshot():
            out = {}
            for d, _, files in os.walk(self.vault):
                for f in files:
                    p = os.path.join(d, f)
                    rel = os.path.relpath(p, self.vault).replace("\\", "/")
                    if not rel.startswith("TaskTreker/"):
                        with open(p, "rb") as fh:
                            out[rel] = fh.read()
            return out
        before = snapshot()
        self.pull()
        self.quiet(self.t.tree.build)
        self.push()
        self.pull()
        self.assertEqual(snapshot(), before)
        self.assertTrue(os.listdir(os.path.join(self.vault, "TaskTreker")))
        with self.assertRaises(ValueError):
            self.t.obsidian.write_note(os.path.join(self.vault, "Личное", "x.md"), {}, "текст", [])

    def test_dry_run_changes_nothing(self):
        out = self.pull("--dry-run")
        self.assertIn("Пробный прогон", out)
        self.assertEqual(self.memory_files(), [])
        self.assertIsNone(self.jread("vault_state.json"))


if __name__ == "__main__":
    unittest.main()
