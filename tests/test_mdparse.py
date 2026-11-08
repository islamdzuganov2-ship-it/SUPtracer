# -*- coding: utf-8 -*-
"""Разбор Markdown и разметки Obsidian (ПРМ-01)."""
import unittest

from base import TOOLS  # noqa: F401 — добавляет tools в sys.path
import mdparse as md


class Frontmatter(unittest.TestCase):
    def test_roundtrip_keeps_types(self):
        src = {"id": "mem-x", "kind": "решение", "pin": True, "n": 3,
               "tags": ["кеш", "память"], "title": "Итог: вынесли кеш", "links": ["A", "B"]}
        fm, body = md.parse_frontmatter(md.dump_frontmatter(src) + "\nтело")
        self.assertEqual(fm, src)
        self.assertEqual(body.strip(), "тело")

    def test_inline_and_block_lists(self):
        fm, _ = md.parse_frontmatter("---\na: [x, 'y', \"z\"]\nb:\n  - один\n  - два\n---\n")
        self.assertEqual(fm["a"], ["x", "y", "z"])
        self.assertEqual(fm["b"], ["один", "два"])

    def test_without_frontmatter_text_is_untouched(self):
        fm, body = md.parse_frontmatter("# Заголовок\nтекст")
        self.assertEqual(fm, {})
        self.assertEqual(body, "# Заголовок\nтекст")

    def test_empty_values_are_not_dumped(self):
        out = md.dump_frontmatter({"a": "", "b": None, "c": [], "d": "x"})
        self.assertNotIn("a:", out)
        self.assertNotIn("b:", out)
        self.assertNotIn("c:", out)
        self.assertIn("d: x", out)


class Structure(unittest.TestCase):
    def test_sections_by_heading_levels(self):
        secs = md.sections("вступление\n\n# Один\nтекст один\n## Два\nтекст два\n#### Четыре\nглубоко")
        titles = [s["title"] for s in secs]
        self.assertEqual(titles, ["", "Один", "Два"])
        self.assertIn("#### Четыре", secs[2]["body"])     # 4 уровень не режет раздел

    def test_heading_inside_code_fence_is_not_a_section(self):
        secs = md.sections("# Настоящий\n```\n# не заголовок\n```\nпосле")
        self.assertEqual([s["title"] for s in secs], ["Настоящий"])

    def test_task_states(self):
        t = md.tasks("- [ ] открыта\n- [x] закрыта\n- [/] в работе\n- [-] отменена\n* [X] тоже закрыта")
        self.assertEqual([(x["done"], x["wip"], x["dropped"]) for x in t],
                         [(False, False, False), (True, False, False), (False, True, False),
                          (False, False, True), (True, False, False)])

    def test_tags_skip_code_and_numbers_and_merge_frontmatter(self):
        body = "текст #кеш и #2026\n```\n#внутри_кода\n```\nещё (#память)"
        self.assertEqual(md.tags(body, {"tags": ["Obsidian"]}), ["obsidian", "кеш", "память"])

    def test_wikilinks_drop_alias_and_heading(self):
        self.assertEqual(md.wikilinks("[[Заметка|подпись]] и [[Другая#Раздел]] и [[Третья]]"),
                         ["Заметка", "Другая", "Третья"])

    def test_first_paragraph_skips_heading(self):
        self.assertEqual(md.first_paragraph("# Заголовок\n\nСуть текста здесь, а не в заголовке."),
                         "Суть текста здесь, а не в заголовке.")


class Measures(unittest.TestCase):
    def test_cyrillic_costs_more_than_latin(self):
        self.assertGreater(md.est_tokens("а" * 400), md.est_tokens("a" * 400))

    def test_empty_costs_nothing(self):
        self.assertEqual(md.est_tokens(""), 0)

    def test_slugify_transliterates(self):
        self.assertEqual(md.slugify("Запись кеша в TaskTreker"), "zapis-kesha-v-tasktreker")
        self.assertEqual(md.slugify("!!!"), "zapis")

    def test_stem_joins_word_forms(self):
        self.assertEqual(md.stem("дашборда"), md.stem("дашборд"))
        self.assertIn(md.stem("проекта"), md.word_set("Раскладка по проектам проекта"))


if __name__ == "__main__":
    unittest.main()
