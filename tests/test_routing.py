# -*- coding: utf-8 -*-
"""Раскладка по пути и псевдонимам, нумерация задач, деление крупных разделов
(ПРМ-03, ПРМ-07, ПРМ-08, ПРМ-10)."""
import os
import unittest

from base import DataCase


class PathRouting(DataCase):
    def setUp(self):
        super().setUp()
        self.projects()

    def ingest(self, rel, text, **kw):
        path = self.note(rel, text)
        reps, _ = self.quiet(self.t.ingest.run, [path], quiet=True, rel_hint=rel, **kw)
        return reps[0]

    PLAIN = "# Заметка\n\nТекст без единого признака проекта: погода, настроение, планы на выходные.\n"

    def test_project_folder(self):                                      # ПРМ-08
        rep = self.ingest("Проекты/Сервис Альфа/итоги.md", self.PLAIN)
        self.assertEqual((rep["project"], rep["confidence"]), ("alpha-api", 0.95))
        self.assertIn("папка «Сервис Альфа»", rep["why"])

    def test_alias_folder(self):
        self.assertEqual(self.ingest("Проекты/Gamma Voice/x.md", self.PLAIN)["project"], "gamma-voice")

    def test_filename_prefix(self):
        rep = self.ingest("Разное/Гамма — план записи.md", self.PLAIN)
        self.assertEqual((rep["project"], rep["confidence"]), ("gamma-voice", 0.9))

    def test_deepest_folder_wins(self):
        self.assertEqual(self.ingest("Утилита Бета/Сервис Альфа/x.md", self.PLAIN)["project"], "alpha-api")

    def test_frontmatter_beats_path(self):
        rep = self.ingest("Проекты/Сервис Альфа/x.md", "---\nproject: beta-tool\n---\n" + self.PLAIN)
        self.assertEqual(rep["project"], "beta-tool")

    def test_path_beats_tag(self):
        rep = self.ingest("Проекты/Сервис Альфа/x.md", "# Заметка\n\n#beta-tool про отчёты\n")
        self.assertEqual(rep["project"], "alpha-api")

    def test_word_prefix_only(self):
        # «Гаммалучи» начинается с «гамма», но это другое слово — не проект «Гамма».
        rep = self.ingest("Разное/Гаммалучи.md", self.PLAIN)
        self.assertEqual(rep["project"], "портфель")

    def test_sections_inherit_folder_project(self):
        rep = self.ingest("Проекты/Сервис Альфа/план.md",
                          self.PLAIN + "\n## Отчёты\n\nКонвертер отчётов CSV в Excel тоже упомянут, "
                                       "но заметка лежит в папке Альфы.\n")
        self.assertTrue(all(r["project"] == "alpha-api" for r in rep["records"]))

    def test_vault_pull_uses_folders(self):
        vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(vault, ".obsidian"))
        p = os.path.join(vault, "Проекты", "Сервис Альфа", "x.md")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.PLAIN)
        self.quiet(self.t.obsidian.cmd_init, [vault])
        _, out = self.quiet(self.t.obsidian.cmd_pull, [])
        self.assertIn("-> alpha-api (95%)", out)


class ForeignAndMentions(DataCase):
    """Калибровка на реальном хранилище: заметки проектов, которых нет в портфеле,
    упоминания нескольких проектов, разделы, случайные совпадения со словарём."""

    # Текст в словах проекта Альфа — без папки и имени он ушёл бы в Альфу.
    ALPHA_WORDS = "# Обзор\n\nAPI партнёров: роли, кеш ответов, эндпоинты чтения.\n"

    def setUp(self):
        super().setUp()
        self.P = self.projects()["projects"]

    def ingest(self, rel, text, **kw):
        path = self.note(rel, text)
        reps, _ = self.quiet(self.t.ingest.run, [path], quiet=True, rel_hint=rel, **kw)
        return reps[0]

    def route(self, text, links=()):
        vocab, weight = self.t.ingest.build_vocab(self.P)
        return self.t.ingest.route(text, {}, [], list(links), self.P, vocab, weight)

    def test_foreign_project_folder(self):                              # ПРМ-08
        rep = self.ingest("Проекты/Облачный склад/обзор.md",
                          self.ALPHA_WORDS + "\n## Состав\n\nКеш ответов и роли партнёров, "
                                             "как у всех, плюс свой склад и доставка.\n")
        self.assertEqual((rep["project"], rep["confidence"]), ("портфель", 0.9))
        self.assertEqual(rep["foreign_project"], "Облачный склад")
        self.assertTrue(all(r["project"] == "портфель" for r in rep["records"]))
        self.assertTrue(all(r.get("foreign_project") == "Облачный склад" for r in rep["records"]))

    def test_foreign_folder_beats_named_project(self):
        rep = self.ingest("Проекты/Облачный склад/x.md", "# Интеграция\n\nОтдаём остатки в Сервис Альфа.\n")
        self.assertEqual(rep["project"], "портфель")

    def test_configured_project_folder(self):
        self.jwrite("obsidian.json", {"import": {"project_folders": ["Проекты/*", "Лаборатория"]}})
        rep = self.ingest("Лаборатория/опыт.md", self.ALPHA_WORDS)
        self.assertEqual((rep["project"], rep["foreign_project"]), ("портфель", "Лаборатория"))
        # Без настройки «Лаборатория» — обычная папка, и текст решает сам.
        self.jwrite("obsidian.json", {"import": {}})
        self.assertEqual(self.ingest("Лаборатория/опыт.md", self.ALPHA_WORDS)["project"], "alpha-api")

    def test_project_folder_patterns(self):
        pf = self.t.ingest.project_folder
        self.assertEqual(pf(["Проекты", "Склад", "x.md"]), "Склад")
        self.assertEqual(pf(["проекты", "Склад", "Итоги", "x.md"]), "Склад")      # регистр и глубина
        self.assertIsNone(pf(["Проекты", "x.md"]))                                 # сам файл — не папка
        self.assertIsNone(pf(["Работа", "LLM", "x.md"], ["LLM"]))                 # папка не в корне
        self.assertEqual(pf(["LLM", "sub", "x.md"], ["LLM"]), "LLM")

    def test_one_named_project(self):
        self.assertEqual(self.route("Обсудили, что Утилита Бета теряет строки.")[:2], ("beta-tool", 0.8))

    def test_several_named_projects_is_portfolio(self):
        key, conf, why = self.route("Веду Сервис Альфа и Утилита Бета, остальное потом.")
        self.assertEqual((key, conf), ("портфель", 0.6))
        self.assertIn("несколько проектов", why)

    def test_dominant_named_project(self):
        text = ("Сервис Альфа отдаёт данные. Сервис Альфа проверяет роли. "
                "Сервис Альфа кеширует ответы. Отчёты как в Утилита Бета.")
        self.assertEqual(self.route(text)[0], "alpha-api")

    def test_links_to_several_projects_is_portfolio(self):
        self.assertEqual(self.route("Связь между проектами", links=["Сервис Альфа", "Утилита Бета"])[0],
                         "портфель")

    def test_named_is_whole_word(self):
        self.assertEqual(self.t.ingest.named_projects("Гаммалучи и гамма-распад", self.P), {})
        self.assertEqual(self.t.ingest.named_projects("проект Гамма, снова Гамма.", self.P),
                         {"gamma-voice": 2})

    def test_section_keeps_file_project_despite_mention(self):          # ПРМ-13
        rep = self.ingest("Проекты/Сервис Альфа/план.md",
                          "# План\n\nЧто делаем дальше.\n\n## Сравнение\n\n"
                          "Утилита Бета собирает отчёты похожим способом, берём у неё идею выгрузки.\n")
        self.assertEqual({r["project"] for r in rep["records"]}, {"alpha-api"})

    def test_section_tag_overrides(self):
        rep = self.ingest("Проекты/Сервис Альфа/план.md",
                          "# План\n\nЧто делаем дальше.\n\n## Отчёты\n\n"
                          "#beta-tool Выгрузку отчётов переносим в утилиту, в сервисе её не будет.\n")
        sec = [r for r in rep["records"] if r["title"] == "Отчёты"][0]
        self.assertEqual(sec["project"], "beta-tool")

    def test_weekly_review_sections_follow_their_projects(self):        # ПРМ-13
        rep = self.ingest("Разное/неделя.md",
                          "# Итоги недели\n\nЧто сдвинулось по разным проектам.\n\n"
                          "## Сервис Альфа\n\nСервис Альфа: закрыли кеш ответов, осталось проверить роли.\n\n"
                          "- [ ] Проверить роли на эндпоинтах записи\n\n"
                          "## Утилита Бета\n\nУтилита Бета научилась читать большие файлы CSV без падений "
                          "и лишней памяти.\n")
        self.assertEqual(rep["project"], "портфель")
        by_title = {r["title"]: r["project"] for r in rep["records"]}
        self.assertEqual(by_title["Сервис Альфа"], "alpha-api")
        self.assertEqual(by_title["Утилита Бета"], "beta-tool")
        self.assertEqual([p["project"] for p in rep["proposals"]], ["alpha-api"])   # задача — в проект раздела

    def test_chance_matches_do_not_route(self):                         # ПРМ-03
        # Три слова из словаря Альфы в короткой заметке — признак, в длинной — случайность.
        short = "Кеш ответов и роли партнёров."
        self.assertEqual(self.route(short)[0], "alpha-api")
        filler = " ".join("пункт%d" % i for i in range(400))
        key, _, why = self.route(filler + " " + short)
        self.assertEqual(key, "портфель")
        self.assertIn("случайность", why)

    def test_vocab_weights_by_full_description(self):
        # Слово из длинного описания двух проектов весит вдвое меньше уникального.
        def m(d):
            d["projects"]["beta-tool"]["goal_long"] = "Партнёры получают отчёты без ручной сборки."
        P = self.projects(m)["projects"]
        _, weight = self.t.ingest.build_vocab(P)
        self.assertEqual(weight["партнёр"], 0.5)
        self.assertEqual(weight["кеш"], 1.0)

    def test_vault_pull_reports_foreign_projects(self):
        vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(vault, ".obsidian"))
        for rel in ("Проекты/Облачный склад/a.md", "Проекты/Облачный склад/b.md", "Проекты/Сервис Альфа/c.md"):
            p = os.path.join(vault, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.ALPHA_WORDS)
        self.quiet(self.t.obsidian.cmd_init, [vault])
        _, out = self.quiet(self.t.obsidian.cmd_pull, [])
        self.assertIn("Облачный склад — 2", out)
        self.assertIn("-> alpha-api (95%)", out)


class Numbering(DataCase):
    def nid(self, key, mutate=None, trusted=()):
        P = self.projects(mutate)["projects"]
        return self.t.inbox.next_task_id(key, P[key].get("backlog", []), P[key], trusted)

    def test_continues_backlog(self):
        self.assertEqual(self.nid("alpha-api"), "AL-03")

    def test_empty_backlog_continues_closed_series(self):               # ПРМ-07
        def m(d):
            d["projects"]["beta-tool"]["readiness"]["basis"] = (
                # Коды вымышленной серии: настоящие коды ТЗ здесь исказили бы трассируемость.
                "Закрыты TT-01, TT-02 и TT-03. Требования ЗАЩ-01, ЗАЩ-02 и ЗАЩ-05 выполнены, ХРН-08 нет.")
        self.assertEqual(self.nid("beta-tool", m), "TT-04")

    def test_explicit_prefix(self):
        def m(d):
            d["projects"]["beta-tool"]["id_prefix"] = "bt"
        self.assertEqual(self.nid("beta-tool", m), "BT-01")

    def test_trusted_moved_to(self):
        self.assertEqual(self.nid("gamma-voice", trusted=["gamma-voice/GV-07"]), "GV-08")

    def test_fallback_from_key(self):
        self.assertEqual(self.nid("gamma-voice"), "GA-01")

    def test_apply_uses_comments_history(self):
        self.projects()
        self.jwrite("comments.json", {"comments": [
            {"id": 1, "project": "beta-tool", "status": "processed", "moved_to": "beta-tool/UB-05"}]})
        self.note("x.md", "---\nproject: beta-tool\n---\n# Отчёты\n\n## Задачи\n- [ ] Добавить выгрузку в PDF\n")
        self.quiet(self.t.ingest.run, [os.path.join(self.tmp, "notes", "x.md")], quiet=True)
        self.quiet(self.t.inbox.cmd_apply, ["IN-1"])
        backlog = self.jread("projects.json")["projects"]["beta-tool"]["backlog"]
        self.assertEqual(backlog[-1]["id"], "UB-06")


class Splitting(DataCase):
    def setUp(self):
        super().setUp()
        self.projects()
        self.est = self.t.mdparse.est_tokens
        self.limit = self.t.ingest.MAX_RECORD_TOKENS

    def ingest(self, text):
        p = self.note("big.md", "---\nproject: alpha-api\n---\n" + text)
        reps, _ = self.quiet(self.t.ingest.run, [p], quiet=True)
        return reps[0]

    def paragraph(self, i, words=60):
        return " ".join("слово%d_%d" % (i, j) for j in range(words))

    def test_long_section_split_by_paragraphs(self):                    # ПРМ-10
        body = "\n\n".join(self.paragraph(i) for i in range(80))
        rep = self.ingest("# Документ\n\nВступление.\n\n## Большой раздел\n\n" + body)
        parts = [r for r in rep["records"] if r["title"].startswith("Большой раздел")]
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(self.est(r["body"]) <= self.limit for r in parts))
        self.assertEqual(len({r["parent"] for r in parts}), 1)          # общий родитель
        self.assertEqual(parts[0]["title"], "Большой раздел (часть 1 из %d)" % len(parts))
        joined = " ".join(r["body"] for r in parts)
        self.assertIn("слово79_59", joined)                              # ничего не потеряно

    def test_split_by_fourth_level_headings(self):
        a = "\n\n".join(self.paragraph(i, 40) for i in range(30))
        b = "\n\n".join(self.paragraph(100 + i, 40) for i in range(30))
        rep = self.ingest("# Д\n\nВступление.\n\n## Раздел\n\n#### Первое\n\n" + a + "\n\n#### Второе\n\n" + b)
        titles = [r["title"] for r in rep["records"] if r["title"].startswith("Раздел")]
        self.assertTrue(any(t.startswith("Раздел — Первое") for t in titles), titles)
        self.assertTrue(any(t.startswith("Раздел — Второе") for t in titles), titles)

    def test_long_table_split_by_lines(self):
        rows = "\n".join("| %d | %s |" % (i, self.paragraph(i, 25)) for i in range(120))
        rep = self.ingest("# Д\n\nВступление.\n\n## Таблица\n\n| № | Текст |\n| --- | --- |\n" + rows)
        parts = [r for r in rep["records"] if r["title"].startswith("Таблица")]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(self.est(r["body"]) <= self.limit for r in parts))

    def test_code_block_is_indivisible(self):
        code = "```\n" + "\n\n".join(self.paragraph(i) for i in range(60)) + "\n```"
        rep = self.ingest("# Д\n\nВступление.\n\n## Код\n\n" + code)
        bodies = [r["body"] for r in rep["records"] if r["title"].startswith("Код")]
        self.assertTrue(any(b.startswith("```") and b.rstrip().endswith("```") for b in bodies))

    def test_long_intro_is_continued_not_cut(self):
        intro = "\n\n".join(self.paragraph(i) for i in range(60))
        rep = self.ingest("# Большое вступление\n\n" + intro + "\n\n## Итог\n\nРешили вынести кеш на диск.\n")
        root = rep["records"][0]
        self.assertLessEqual(self.est(root["body"]), self.limit)
        cont = [r for r in rep["records"] if "вступление (часть" in r["title"]]
        self.assertTrue(cont)
        self.assertIn("слово59_59", " ".join(r["body"] for r in [root] + cont))


if __name__ == "__main__":
    unittest.main()
