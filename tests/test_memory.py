# -*- coding: utf-8 -*-
"""Хранилище памяти (ПАМ-01 … ПАМ-11)."""
import json
import os
import unittest

from base import DataCase


class Memory(DataCase):
    def rec(self, title, body="Тело записи достаточной длины для дайджеста.", **kw):
        kw.setdefault("project", "alpha-api")
        kw.setdefault("kind", "решение")
        r = self.t.memory.new_record(title, body, **kw)
        self.t.memory.save(r)
        return r

    def test_id_is_stable_for_same_title_project_source(self):          # ПАМ-02
        m = self.t.memory
        self.assertEqual(m.make_id("Кеш на диске", "alpha-api", "a.md"),
                         m.make_id("Кеш на диске", "alpha-api", "a.md"))
        self.assertNotEqual(m.make_id("Кеш на диске", "alpha-api", "a.md"),
                            m.make_id("Кеш на диске", "beta-tool", "a.md"))

    def test_saved_record_reads_back_with_all_fields(self):             # ПАМ-01
        r = self.rec("Роли на эндпоинтах", "Проверять роль на каждом эндпоинте.",
                     tags=["роли"], links=["Сервис Альфа"], parent="mem-p", root="mem-r", pin=True)
        back = self.t.memory.read(self.t.memory.record_path(r["id"]))
        for k in ("id", "kind", "project", "title", "parent", "root", "pin"):
            self.assertEqual(back[k], r[k], k)
        self.assertEqual(back["tags"], ["роли"])
        self.assertIn("Сервис Альфа", back["links"])
        self.assertEqual(back["body"], "Проверять роль на каждом эндпоинте.")

    def test_unknown_kind_becomes_note(self):                            # ПАМ-03
        r = self.t.memory.new_record("x", "y", kind="неведомое")
        self.assertEqual(r["kind"], "заметка")

    def test_index_grows_with_count_not_with_body_size(self):           # ПАМ-04
        short = self.rec("Запись", "Короткое тело записи про кеш.")
        small = len(json.dumps(self.t.memory.build_index()["records"], ensure_ascii=False))
        long_body = " ".join("слово%d" % i for i in range(3000))
        self.t.memory.save(self.t.memory.new_record(short["title"], long_body,
                                                    project="alpha-api", kind="решение"))
        idx = self.t.memory.build_index()
        big = len(json.dumps(idx["records"], ensure_ascii=False))
        self.assertLessEqual(len(idx["records"][0]["digest"]), 220)
        self.assertLess(big - small, 600)          # тело выросло в сотни раз, индекс — нет
        self.assertGreater(idx["totals"]["stored_tokens"], 3000)

    def test_search_puts_title_match_first(self):                        # ПАМ-07
        self.rec("Кеш ответов вынесен на диск")
        self.rec("Роли партнёров")
        self.rec("Отчёты в Excel", project="beta-tool")
        self.t.memory.build_index()
        hits = self.t.memory.search("кеш диск")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["title"], "Кеш ответов вынесен на диск")

    def test_brief_respects_budget_and_hides_bodies(self):              # ПАМ-05
        for i in range(40):
            self.rec("Решение номер %d про кеш и роли" % i, "Тело %d " % i * 30)
        self.t.memory.build_index()
        b = self.t.memory.brief(budget=300)
        self.assertLessEqual(self.t.mdparse.est_tokens(b), 420)   # заголовок + строки в бюджете
        self.assertNotIn("Тело 5 Тело 5", b)
        self.assertIn("Не поместилось", b)

    def test_forget_keeps_file_and_hides_from_brief(self):              # ПАМ-09
        r = self.rec("Устаревшее решение")
        self.t.memory.build_index()
        _, out = self.quiet(self.t.memory.cmd_forget, [r["id"], "--why", "заменили"])
        self.assertTrue(os.path.exists(self.t.memory.record_path(r["id"])))
        back = self.t.memory.get(r["id"])
        self.assertEqual(back["status"], "устарела")
        self.assertIn("заменили", back["body"])
        self.assertNotIn("Устаревшее решение", self.t.memory.brief(budget=2000))

    def test_resave_without_changes_keeps_updated_date(self):
        r = self.rec("Неизменная запись")
        path = self.t.memory.record_path(r["id"])
        with open(path, encoding="utf-8") as f:
            text = f.read().replace("updated: " + r["updated"], "updated: 2020-01-01")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        again = self.t.memory.new_record(r["title"], r["body"], project=r["project"], kind=r["kind"])
        self.t.memory.save(again)
        self.assertEqual(self.t.memory.get(r["id"])["updated"], "2020-01-01")


if __name__ == "__main__":
    unittest.main()
