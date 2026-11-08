# -*- coding: utf-8 -*-
"""Жизненный цикл записей: изменённые и удалённые заметки, замена записей (ПРМ-11, ПАМ-10)."""
import os
import unittest

from base import DataCase

NOTE = """---
project: alpha-api
---
# Роли партнёров

Нужно проверять роль партнёра на каждом эндпоинте, иначе партнёр увидит чужие данные.

## Решение

Решили проверять роль в общем промежуточном слое, а не в каждом обработчике отдельно.

## Риски

Есть риск утечки чужих данных, если новый эндпоинт забудут подключить к слою ролей.
"""


class Lifecycle(DataCase):
    def setUp(self):
        super().setUp()
        self.projects()
        self.path = self.note("roles.md", NOTE)
        self.first = self.ingest()

    def ingest(self):
        reps, _ = self.quiet(self.t.ingest.run, [self.path], quiet=True)
        return reps[0]

    def rec(self, title):
        return next(r for r in self.first["records"] if r["title"] == title)

    def status(self, rid):
        return self.t.memory.get(rid)["status"]

    def test_removed_section_becomes_stale_file_stays(self):             # ПРМ-11
        risks = self.rec("Риски")["id"]
        self.note("roles.md", NOTE.split("## Риски")[0])
        rep = self.ingest()
        self.assertEqual(rep["retired"], [risks])
        self.assertEqual(self.status(risks), "устарела")
        self.assertEqual(self.t.memory.get(risks)["retired"], "автоматически")
        self.assertTrue(os.path.exists(self.t.memory.record_path(risks)))
        self.assertNotIn("[%s]" % risks, self.t.memory.brief(budget=3000))
        self.assertEqual(self.status(self.rec("Решение")["id"]), "активна")

    def test_section_that_comes_back_is_revived(self):
        risks = self.rec("Риски")["id"]
        self.note("roles.md", NOTE.split("## Риски")[0])
        self.ingest()
        self.note("roles.md", NOTE)
        self.ingest()
        self.assertEqual(self.status(risks), "активна")

    def test_manual_retirement_survives_reingest(self):
        dec = self.rec("Решение")["id"]
        self.quiet(self.t.memory.cmd_forget, [dec, "--why", "передумали"])
        rep = self.ingest()
        self.assertEqual(rep["kept_manual"], [dec])
        self.assertEqual(self.status(dec), "устарела")
        self.assertIn("передумали", self.t.memory.get(dec)["body"])

    def test_supersede_links_both_ways(self):                           # ПАМ-10
        old = self.rec("Решение")["id"]
        m = self.t.memory
        new = m.new_record("Роли проверяет шлюз", "Проверку ролей перенесли в шлюз API.",
                           project="alpha-api", kind="решение")
        m.save(new)
        _, out = self.quiet(m.cmd_forget, [old, "--by", new["id"], "--why", "перенесли в шлюз"])
        self.assertIn("заменена", out)
        o, n = m.get(old), m.get(new["id"])
        self.assertEqual((o["status"], o["superseded_by"]), ("заменена", new["id"]))
        self.assertEqual(n["supersedes"], old)
        self.assertEqual(n["status"], "активна")

    def test_supersede_needs_existing_successor(self):
        old = self.rec("Решение")["id"]
        _, out = self.quiet(self.t.memory.cmd_forget, [old, "--by", "mem-нет-такой"])
        self.assertIn("нет записи-преемника", out)
        self.assertEqual(self.status(old), "активна")

    def test_runtime_path_never_written(self):
        rid = self.rec("Решение")["id"]
        self.t.memory.retire(rid, "проверка")
        with open(self.t.memory.record_path(rid), encoding="utf-8") as f:
            head = f.read().split("---")[1]
        self.assertNotIn("\npath:", head)


class VanishedNote(DataCase):
    def test_deleted_vault_note_retires_its_records(self):              # ПРМ-11
        self.projects()
        vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(vault, ".obsidian"))
        note = os.path.join(vault, "Проекты", "roles.md")
        os.makedirs(os.path.dirname(note))
        with open(note, "w", encoding="utf-8") as f:
            f.write(NOTE)
        self.quiet(self.t.obsidian.cmd_init, [vault])
        self.quiet(self.t.obsidian.cmd_pull, [])
        ids = [r["id"] for r in self.t.memory.load_index()["records"]]
        self.assertEqual(len(ids), 3)
        os.remove(note)
        _, out = self.quiet(self.t.obsidian.cmd_pull, [])
        self.assertIn("исчезла из хранилища, записей устарело: 3", out)
        self.assertTrue(all(self.t.memory.get(i)["status"] == "устарела" for i in ids))
        self.assertEqual(self.jread("vault_state.json")["files"], {})
        self.assertNotIn("исчезло", self.quiet(self.t.obsidian.cmd_pull, [])[1])   # второй раз не снимает


if __name__ == "__main__":
    unittest.main()
