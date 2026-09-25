# -*- coding: utf-8 -*-
"""Входящие и дерево работ (ПРМ-05, ДРВ-01 … ДРВ-04, КАЧ-05)."""
import unittest

from base import DataCase
from test_ingest import NOTE_API


class Inbox(DataCase):
    def setUp(self):
        super().setUp()
        self.projects()
        self.quiet(self.t.ingest.run, [self.note("api.md", NOTE_API)], quiet=True)

    def backlog(self, key="alpha-api"):
        return self.jread("projects.json")["projects"][key]["backlog"]

    def test_apply_continues_numbering_and_keeps_root(self):            # ДРВ-02
        _, out = self.quiet(self.t.inbox.cmd_apply, ["IN-1"])
        task = self.backlog()[-1]
        self.assertEqual(task["id"], "AL-03")
        self.assertEqual(task["title"], "Вынести кеш эндпоинтов в отдельное хранилище")
        self.assertEqual(task["status"], "queued")
        self.assertTrue(task["root"].startswith("mem-"))
        self.assertIn("api.md", task["source"])
        item = self.jread("inbox.json")["items"][0]
        self.assertEqual((item["status"], item["applied_to"]), ("applied", "alpha-api/AL-03"))

    def test_apply_all_for_project(self):
        self.quiet(self.t.inbox.cmd_apply, ["--all", "--project", "alpha-api"])
        self.assertEqual([b["id"] for b in self.backlog()], ["AL-01", "AL-02", "AL-03", "AL-04", "AL-05"])

    def test_apply_twice_does_nothing_second_time(self):
        self.quiet(self.t.inbox.cmd_apply, ["IN-2"])
        _, out = self.quiet(self.t.inbox.cmd_apply, ["IN-2"])
        self.assertIn("уже applied", out)
        self.assertEqual(len(self.backlog()), 3)

    def test_reject_keeps_record_with_reason(self):
        self.quiet(self.t.inbox.cmd_reject, ["IN-3", "--why", "не задача"])
        item = self.jread("inbox.json")["items"][2]
        self.assertEqual((item["status"], item["why_rejected"]), ("rejected", "не задача"))

    def test_list_shows_only_new(self):
        self.quiet(self.t.inbox.cmd_apply, ["IN-1"])
        _, out = self.quiet(self.t.inbox.cmd_list, [])
        self.assertNotIn("IN-1 ", out)
        self.assertIn("IN-2", out)


class Tree(DataCase):
    def build(self):
        (out, _), _ = self.quiet(self.t.tree.build)
        return out

    def find(self, node, pred):
        if pred(node):
            return node
        for c in node["children"]:
            f = self.find(c, pred)
            if f:
                return f
        return None

    def test_structure_of_a_project(self):                              # ДРВ-01
        self.projects()
        self.quiet(self.t.ingest.run, [self.note("api.md", NOTE_API)], quiet=True)
        self.quiet(self.t.inbox.cmd_apply, ["IN-1"])
        out = self.build()
        alpha = self.find(out["root"], lambda n: n["id"] == "p:alpha-api")
        kinds = [c["kind"] for c in alpha["children"]]
        self.assertEqual(kinds, ["фундамент", "запрос", "бэклог", "почки"])
        request = alpha["children"][1]
        self.assertEqual(request["label"], "Вынести кеш эндпоинтов из приложения")
        under = [c["kind"] for c in request["children"]]
        self.assertIn("требование", under)
        self.assertIn("задача", under)                                  # ДРВ-02: принятая задача под запросом
        loose = alpha["children"][2]
        self.assertEqual([c["task"] for c in loose["children"]], ["AL-01", "AL-02"])

    def test_weights_roll_up(self):                                     # ДРВ-03
        self.projects()
        self.jwrite("estimates.json", {"projects": {"alpha-api": {"items": [
            {"id": "AL-01", "est_tokens": 3000}, {"id": "AL-02", "est_tokens": 1000}]}}})
        out = self.build()
        alpha = self.find(out["root"], lambda n: n["id"] == "p:alpha-api")
        self.assertEqual(alpha["subtree_weight"], 4000)
        self.assertEqual(out["root"]["subtree_weight"], 4000)

    def test_branch_date_is_earliest(self):                             # ДРВ-04
        self.projects()
        m = self.t.memory
        root = m.new_record("Старый запрос", "текст", project="beta-tool", kind="запрос",
                            created="2026-03-05")
        m.save(root)
        m.save(m.new_record("Новое решение", "текст", project="beta-tool", kind="решение",
                            created="2026-08-01", parent=root["id"], root=root["id"]))
        m.build_index()
        out = self.build()
        node = self.find(out["root"], lambda n: n.get("memory") == root["id"])
        self.assertEqual(node["first_date"], "2026-03-05")
        self.assertIn("2026-03", out["stats"]["months"])
        self.assertIn("2026-08", out["stats"]["months"])

    def test_memory_outside_known_projects_is_kept(self):
        self.projects()
        self.t.memory.save(self.t.memory.new_record("Личное", "текст", project="портфель", kind="заметка"))
        self.t.memory.build_index()
        out = self.build()
        self.assertTrue(self.find(out["root"], lambda n: n["id"] == "p:портфель"))

    def test_foreign_project_is_its_own_branch(self):                   # ПРМ-08
        self.projects()
        m = self.t.memory
        rec = m.new_record("Устав проекта", "текст", project="портфель", kind="запрос")
        rec["foreign_project"] = "Облачный склад"
        m.save(rec)
        m.save(m.new_record("Личное", "текст", project="портфель", kind="заметка"))
        idx = m.build_index()
        self.assertEqual([r.get("foreign_project") for r in idx["records"] if r["title"] == "Устав проекта"],
                         ["Облачный склад"])
        out = self.build()
        branch = self.find(out["root"], lambda n: n["id"] == "p:Облачный склад")
        self.assertEqual(branch["label"], "Облачный склад — вне портфеля")
        self.assertEqual([c["label"] for c in branch["children"]], ["Устав проекта"])
        rest = self.find(out["root"], lambda n: n["id"] == "p:портфель")
        self.assertEqual([c["label"] for c in rest["children"]], ["Личное"])

    def test_wide_node_is_capped_in_text(self):                         # ДРВ-07
        self.projects()
        m = self.t.memory
        for i in range(40):
            m.save(m.new_record("Запись %02d" % i, "текст", project="beta-tool", kind="заметка",
                                created="2026-09-%02d" % (i % 28 + 1)))
        m.build_index()
        text = self.t.tree.render_text("beta-tool", depth=2, max_lines=500)
        self.assertEqual(text.count("Запись "), self.t.tree.WIDE)
        self.assertIn("… ещё 15 записей", text)
        self.assertIn("Фундамент", text)                                  # служебные ветви не прячутся


class ForeignTasks(DataCase):
    def test_foreign_note_tasks_stay_out_of_inbox(self):                # ПРМ-08
        self.projects()
        p = self.note("Проекты/Облачный склад/план.md",
                      "# План\n\nЧто делаем.\n\n## Задачи\n- [ ] Подключить оплату картой\n"
                      "- [ ] Добавить остатки по ячейкам\n")
        reps, _ = self.quiet(self.t.ingest.run, [p], quiet=True, rel_hint="Проекты/Облачный склад/план.md")
        self.assertEqual((reps[0]["foreign_project"], reps[0]["foreign_tasks"]), ("Облачный склад", 2))
        self.assertEqual(reps[0]["proposals"], [])
        self.assertEqual(self.jread("inbox.json")["items"], [])


class Estimate(DataCase):
    def test_closed_tasks_do_not_count_in_remaining(self):             # КАЧ-05
        self.projects()
        self.quiet(self.t.estimate.main)
        est = self.jread("estimates.json")["projects"]["alpha-api"]
        per = est["tokens_per_commit"]
        self.assertEqual(len(est["items"]), 2)                          # список полный — для дерева
        self.assertEqual(est["remaining_tokens"], per * 3)              # только открытая M-задача
        done = [i for i in est["items"] if i["id"] == "AL-02"][0]
        self.assertTrue(done["closed"])


if __name__ == "__main__":
    unittest.main()
