# -*- coding: utf-8 -*-
"""Приём заметок: вид, проект, структура, предложения (ПРМ-01 … ПРМ-06, ПРМ-12)."""
import os
import sys
import unittest
from unittest import mock

from base import DataCase

NOTE_API = """---
tags: [разработка]
created: 2026-09-10
---
# Вынести кеш эндпоинтов из приложения

Нужно реализовать внешнее хранилище кеша для API партнёров: сейчас каждый запрос
собирает ответ заново, и это дорого.

## Требования

Сервис должен отдавать ответ из кеша не дольше 50 мс. Обязательно сохранять роли:
партнёр не должен видеть чужие данные. Это критерий приёмки.

## Задачи

- [ ] Вынести кеш эндпоинтов в отдельное хранилище
- [ ] Поправить опечатку в описании API
- [x] Померить время ответа
- Спроектировать сквозную схему инвалидации кеша
- просто заметка без глагола
"""


class Classify(DataCase):
    def test_explicit_kind_wins(self):                                  # ПРМ-02
        kind, conf, _ = self.t.ingest.classify("Что угодно", "нужно сделать", {"kind": "риск"})
        self.assertEqual((kind, conf), ("риск", 1.0))

    def test_tag_kind(self):
        self.assertEqual(self.t.ingest.classify("x", "y", None, ["решение"])[0], "решение")

    def test_markers(self):
        c = self.t.ingest.classify
        self.assertEqual(c("План", "Нужно реализовать выгрузку и добавить кнопку")[0], "запрос")
        self.assertEqual(c("Выбор хранилища", "Решили использовать файлы вместо базы")[0], "решение")
        self.assertEqual(c("Утечка", "Есть риск утечки ключа доступа")[0], "риск")
        self.assertEqual(c("Сделали", "Итог: релиз выкачен, всё готово")[0], "итог")

    def test_nothing_recognisable_is_a_note(self):
        self.assertEqual(self.t.ingest.classify("Разное", "просто текст про погоду")[0], "заметка")

    # Раздел руководства: описание экрана, где слова «добавить» и «нужно» — часть текста.
    MANUAL = ("Окно открывается из реестра. " + "Поле заполняется вручную, значение сохраняется сразу. " * 12 +
              "Если нужно, можно добавить описание. " +
              "Сохранённая запись появляется в списке и доступна всем ролям. " * 12)

    def test_section_is_a_request_only_with_marker_in_title(self):     # ПРМ-02
        c = self.t.ingest.classify
        self.assertNotEqual(c("Карточка системы", self.MANUAL, section=True)[0], "запрос")
        self.assertEqual(c("Что нужно от заказчика", "Доступ к стенду и выгрузка метрик.", section=True)[0],
                         "запрос")

    def test_long_text_is_judged_by_density(self):                     # ПРМ-02
        # Одно слово «риск» на длинный раздел — упоминание, а не вид записи.
        text = "Экран показывает список систем и их метрики за период. " * 30 + "Риск тут не главное."
        self.assertEqual(self.t.ingest.classify("Экран систем", text)[0], "заметка")
        dense = "Риск утечки ключа. Уязвимость в ролях. Утечка токена опасна. " * 5
        self.assertEqual(self.t.ingest.classify("Разбор", dense)[0], "риск")


class Route(DataCase):
    def setUp(self):
        super().setUp()
        self.P = self.projects()["projects"]
        self.vocab, self.weight = self.t.ingest.build_vocab(self.P)

    def route(self, text, fm=None, tags=(), links=(), default=None):
        return self.t.ingest.route(text, fm, list(tags), list(links), self.P,
                                   self.vocab, self.weight, default)

    def test_frontmatter_project_is_certain(self):                     # ПРМ-03
        self.assertEqual(self.route("любой текст", {"project": "beta-tool"})[:2], ("beta-tool", 1.0))

    def test_frontmatter_by_project_name(self):
        self.assertEqual(self.route("текст", {"project": "Утилита Бета"})[0], "beta-tool")

    def test_tag_and_wikilink(self):
        self.assertEqual(self.route("текст", tags=["alpha-api"])[0], "alpha-api")
        self.assertEqual(self.route("текст", links=["Утилита Бета"])[0], "beta-tool")

    def test_vocabulary(self):
        key, conf, _ = self.route("Кеш ответов эндпоинтов API и роли партнёров")
        self.assertEqual(key, "alpha-api")
        self.assertGreaterEqual(conf, 0.5)

    def test_argument_beats_everything(self):
        self.assertEqual(self.route("API партнёров", {"project": "alpha-api"}, default="beta-tool")[0],
                         "beta-tool")

    def test_unknown_goes_to_portfolio(self):
        key, conf, _ = self.route("рецепт борща со сметаной")
        self.assertEqual(key, "портфель")
        self.assertLess(conf, 0.5)


class IngestFile(DataCase):
    def setUp(self):
        super().setUp()
        self.P = self.projects()
        self.path = self.note("api-kesh.md", NOTE_API)

    def run_ingest(self, *paths, **kw):
        res, _ = self.quiet(self.t.ingest.run, list(paths) or [self.path], quiet=True, **kw)
        return res

    def test_structure_root_and_children(self):                         # ПРМ-04
        rep = self.run_ingest()[0]
        self.assertEqual(rep["project"], "alpha-api")
        root = rep["records"][0]
        self.assertEqual(root["title"], "Вынести кеш эндпоинтов из приложения")
        self.assertEqual(root["kind"], "запрос")
        self.assertEqual(root["created"], "2026-09-10")
        kids = rep["records"][1:]
        self.assertEqual([k["title"] for k in kids], ["Требования"])   # «Задачи» — список, не запись
        self.assertEqual(kids[0]["parent"], root["id"])
        self.assertEqual(kids[0]["root"], root["id"])
        self.assertEqual(kids[0]["kind"], "требование")

    def test_attach_to_existing_request(self):                          # ПРМ-04
        rep = self.run_ingest(attach_root="mem-koren")[0]
        root, kid = rep["records"][0], rep["records"][1]
        self.assertEqual((root["parent"], root["root"]), ("mem-koren", "mem-koren"))
        self.assertEqual((kid["parent"], kid["root"]), (root["id"], "mem-koren"))
        self.assertTrue(all(p["root"] == "mem-koren" for p in rep["proposals"]))

    def test_tasks_become_proposals_not_backlog(self):                  # ПРМ-05
        before = self.jread("projects.json")
        rep = self.run_ingest()[0]
        titles = [p["title"] for p in rep["proposals"]]
        self.assertEqual(titles, ["Вынести кеш эндпоинтов в отдельное хранилище",
                                  "Поправить опечатку в описании API",
                                  "Спроектировать сквозную схему инвалидации кеша"])
        self.assertEqual(self.jread("projects.json"), before)
        box = self.jread("inbox.json")
        self.assertEqual([i["id"] for i in box["items"]], ["IN-1", "IN-2", "IN-3"])
        self.assertTrue(all(i["status"] == "new" for i in box["items"]))
        sizes = {p["title"]: p["size"] for p in rep["proposals"]}
        self.assertEqual(sizes["Поправить опечатку в описании API"], "S")

    def test_reingest_creates_nothing_new(self):                        # ПРМ-02, ПРМ-06
        self.run_ingest()
        files = self.memory_files()
        rep = self.run_ingest()[0]
        self.assertEqual(rep["proposals"], [])
        self.assertEqual(self.memory_files(), files)
        self.assertEqual(len(self.jread("inbox.json")["items"]), 3)

    def test_existing_backlog_title_is_not_proposed(self):
        self.note("dup.md", "# Кеш\n\n## Задачи\n- [ ] Добавить кеш ответов эндпоинтов\n")
        rep = self.run_ingest(os.path.join(self.tmp, "notes", "dup.md"), default_project="alpha-api")[0]
        self.assertEqual(rep["proposals"], [])

    def test_dry_run_writes_nothing(self):
        self.run_ingest(dry=True)
        self.assertEqual(self.memory_files(), [])
        self.assertIsNone(self.jread("inbox.json"))

    def test_directory_skips_hidden_and_obsidian(self):
        self.note(".obsidian/x.md", "# не читать\nнужно сделать")
        self.note("sub/y.md", "# Утилита отчётов\nКонвертер отчётов CSV в Excel нужно ускорить.")
        reps = self.run_ingest(os.path.join(self.tmp, "notes"))
        names = sorted(os.path.basename(r["file"]) for r in reps)
        self.assertEqual(names, ["api-kesh.md", "y.md"])

    def test_uncertain_file_is_reported(self):                          # ПРМ-12
        p = self.note("borsch.md", "# Рецепт\nСвёкла, капуста, сметана.")
        with mock.patch.object(sys, "argv", ["ingest.py", p]):
            _, out = self.quiet(self.t.ingest.main)
        self.assertIn("неуверенно", out)
        self.assertIn("borsch.md", out)


if __name__ == "__main__":
    unittest.main()
