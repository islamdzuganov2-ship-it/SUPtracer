# -*- coding: utf-8 -*-
"""Входящие и дерево работ (ПРМ-05, ДРВ-01 … ДРВ-07, КАЧ-05)."""
import os
import subprocess
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


class Growth(DataCase):
    """Реализация в дереве: коммиты под задачами и даты сделанного (ДРВ-05, ДРВ-06)."""
    find = Tree.find

    def git(self, repo, *args, date=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        if date:
            env.update(GIT_AUTHOR_DATE=date + "T12:00:00", GIT_COMMITTER_DATE=date + "T12:00:00")
        subprocess.run(["git"] + list(args), cwd=repo, env=env, check=True, capture_output=True)

    def commit(self, repo, msg, date):
        with open(os.path.join(repo, "f.txt"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        self.git(repo, "add", "f.txt")
        self.git(repo, "commit", "-q", "-m", msg, date=date)

    def test_commits_with_task_number_become_leaves(self):             # ДРВ-05
        import collect_repos
        def m(d):
            d["projects"]["alpha-api"]["backlog"][0]["refs"] = ["Т2"]
        P = self.projects(m)["projects"]
        repo = P["alpha-api"]["path"]
        self.git(repo, "init", "-q")
        self.commit(repo, "Каркас сервиса", "2026-03-01")
        self.commit(repo, "AL-01: кеш ответов в Redis", "2026-03-10")
        self.commit(repo, "Т2: инвалидация кеша; Т14 тут ни при чём", "2026-03-12")
        self.commit(repo, "Т14: другое", "2026-03-15")
        found = collect_repos.task_commits(repo, collect_repos.task_tokens(P["alpha-api"]))
        self.assertEqual([c["date"] for c in found["AL-01"]], ["2026-03-10", "2026-03-12"])
        self.assertNotIn("AL-02", found)
        self.jwrite("repos.json", {"repos": {"alpha-api": {"git": {"task_commits": found}}}})
        (out, _), _ = self.quiet(self.t.tree.build)
        task = self.find(out["root"], lambda n: n.get("task") == "AL-01")
        self.assertEqual([c["kind"] for c in task["children"]], ["коммит", "коммит"])
        self.assertEqual(task["date"], "2026-03-10")                     # дата первого коммита
        self.assertIn("AL-01: кеш ответов", task["children"][0]["label"])

    def test_done_dates_are_estimated_in_order(self):                  # ДРВ-06
        def m(d):
            d["projects"]["alpha-api"]["done"] = ["Первое", "Второе", "Третье 2026-02-20", "Четвёртое"]
        self.projects(m)
        self.jwrite("repos.json", {"repos": {"alpha-api": {"git": {
            "first_commit": "2026-01-01", "last_commit": "2026-05-01"}}}})
        (out, _), _ = self.quiet(self.t.tree.build)
        found = self.find(out["root"], lambda n: n["id"] == "f:alpha-api")
        dates = [(c["date"], c["date_estimated"]) for c in found["children"]]
        self.assertEqual(dates[2], ("2026-02-20", False))                # своя дата в тексте
        self.assertTrue(dates[0][1] and dates[1][1] and dates[3][1])
        self.assertLess(dates[0][0], dates[1][0])                        # фундамент растёт по порядку
        self.assertEqual(dates[3][0], "2026-05-01")


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
