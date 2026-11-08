# -*- coding: utf-8 -*-
"""Трассируемость ТЗ в код (КАЧ-03).

Система меряет у всех проектов портфеля долю кодов требований, найденных в коде.
У неё самой эта цифра должна быть честной, поэтому правило строже простого счёта:
  статус «реализовано» — код требования обязан стоять в коде рядом с реализацией;
  статус «не начато» — кода в коде быть не должно: либо статус устарел, либо код
  поставлен заранее и приписывает системе то, чего она не умеет.
Частичные требования («◐») могут быть и там и там.

Коды ищутся тем же выражением, что у сканера репозиториев (collect_repos.REQ_CODE),
и в тех же файлах кода — чтобы тест и метрика не расходились."""
import glob
import os
import re
import unittest

from base import ROOT, TOOLS  # noqa: F401
import collect_repos

SPEC = os.path.join(ROOT, "docs", "ТЗ_01_Память_Obsidian_Дерево.md")
ROW = re.compile(r"^\| ((?:ПАМ|ПРМ|ОБС|ДРВ|ОБН|ИНТ|КАЧ|БЕЗ)-\d+) \|.*\| (✅|◐|○) \|\s*$", re.M)


def spec_rows():
    with open(SPEC, encoding="utf-8") as f:
        return ROW.findall(f.read())


def codes_in_code():
    found = set()
    for path in glob.glob(os.path.join(ROOT, "tools", "*.py")) + \
            glob.glob(os.path.join(ROOT, "tools", "*.js")) + \
            glob.glob(os.path.join(ROOT, "tests", "*.py")):
        with open(path, encoding="utf-8") as f:
            found |= set(collect_repos.REQ_CODE.findall(f.read()))
    return found


class Traceability(unittest.TestCase):
    def setUp(self):
        self.rows = spec_rows()
        self.code = codes_in_code()

    def test_spec_is_parsed(self):
        self.assertGreaterEqual(len(self.rows), 60)
        self.assertEqual(len({c for c, _ in self.rows}), len(self.rows))    # коды уникальны

    def test_done_requirements_are_in_code(self):
        missing = [c for c, s in self.rows if s == "✅" and c not in self.code]
        self.assertEqual(missing, [], "реализованы по ТЗ, но в коде их кода нет")

    def test_not_started_requirements_are_not_in_code(self):
        early = [c for c, s in self.rows if s == "○" and c in self.code]
        self.assertEqual(early, [], "не начаты по ТЗ, но код уже ссылается на них — "
                                    "обновите статус в ТЗ или уберите ссылку")


if __name__ == "__main__":
    unittest.main()
