# -*- coding: utf-8 -*-
"""Качество раскладки на размеченном наборе (ПРМ-09, ПРМ-02).

Набор — tests/fixtures/routing: вымышленный портфель из шести проектов с пересекающимися
словарями и 42 размеченные заметки. Порог — из ТЗ; тест падает, если правила раскладки
испортились. Порог не двигается ради зелёного теста: чинятся правила."""
import os
import unittest

from base import DataCase, ROOT

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "routing")


class RoutingQuality(DataCase):
    def setUp(self):
        super().setUp()
        import importlib
        import routing_eval
        self.ev = importlib.reload(routing_eval)
        self.res = self.ev.evaluate(FIXTURES)

    def test_set_is_not_trivial(self):
        self.assertGreaterEqual(self.res["cases"], 40)
        kinds = {r["want_kind"] for r in self.res["rows"]}
        self.assertGreaterEqual(len(kinds), 8)                          # все виды представлены
        self.assertIn("портфель", {r["want_project"] for r in self.res["rows"]})

    def test_project_accuracy_meets_threshold(self):                    # ПРМ-09
        self.assertGreaterEqual(self.res["project_accuracy"], self.ev.MIN_PROJECT,
                                self._misses("project_ok"))

    def test_kind_accuracy_meets_threshold(self):                       # ПРМ-02, ПРМ-09
        self.assertGreaterEqual(self.res["kind_accuracy"], self.ev.MIN_KIND, self._misses("kind_ok"))

    def test_nothing_goes_to_a_wrong_project(self):
        # Опасная ошибка — чужой проект: она молча засоряет его память. Неуверенная
        # заметка должна оставаться в «портфеле» на проверку, а не угадываться.
        self.assertEqual(self.res["misrouted"], 0, self._misses("project_ok"))

    def test_evaluation_writes_nothing(self):
        self.assertEqual(os.listdir(self.data), [])

    def _misses(self, field):
        return "\n".join("%s: %s/%s" % (r["file"], r["got_project"], r["got_kind"])
                         for r in self.res["rows"] if r[field] is False)


if __name__ == "__main__":
    unittest.main()
