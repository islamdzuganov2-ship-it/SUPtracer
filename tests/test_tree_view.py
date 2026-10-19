# -*- coding: utf-8 -*-
"""Древо на дашборде: цвет статусов и раскладка ветвей (ДРВ-07, ДРВ-09).

Картинку глазами тестом не проверить, но её правила проверяются: каждый статус бэклога
попадает ровно в одну из шести категорий цвета, корни растут под землёй, крона — над ней,
у каждого узла есть путь до ствола, а узел с сотней детей ветвится развилками, а не метлой
из одной точки. Раскладка считается тем же кодом, что на дашборде, — в Node.js;
без Node.js тест пропускается."""
import json
import os
import shutil
import subprocess
import unittest

from base import TOOLS

NODE = shutil.which("node")

HARNESS = r"""
globalThis.Path2D = class { moveTo() {} lineTo() {} closePath() {} ellipse() {} };
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8') + '\nglobalThis.TREEVIEW = TREEVIEW;');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const T = TREEVIEW._test, out = {status: {}};
for (const s of input.statuses) out.status[s] = T.workStatus(s);
const M = T.layoutTree(input.tree);
const same = (a, b) => Math.abs(a - b) < 1e-6;
let fanMax = 0, fanNode = '';
for (const n of M.nodes) {
  if (n._x == null) continue;
  const k = M.edges.filter(e => same(e.x0, n._x) && same(e.y0, n._y)).length;
  if (k > fanMax) { fanMax = k; fanNode = n.id; }
}
out.finite = M.nodes.every(n => Number.isFinite(n._x) && Number.isFinite(n._y));
out.rootsBelow = M.tips.every(n => n._y > 0);
out.crownAbove = M.leaves.concat(M.fruits).every(n => n._y < 0);
out.noPath = M.nodes.filter(n => n !== M.root && !(n._chain && n._chain.length)).map(n => n.id);
out.fanMax = fanMax; out.fanNode = fanNode;
out.tips = M.tips.filter(n => n.kind === 'сделано').length;
out.fruits = M.fruits.length;
out.blossoms = M.leaves.filter(n => n._work).length;
out.search = T.searchNodes(M.nodes, 'альфа задача').map(n => n.id);
out.exactSearch = T.searchNodes([{id:'a', label:'Учёт расходов', detail:'Codex'},
  {id:'b', label:'Расходы', detail:'учёт Codex'}], 'учет расходов').map(n => n.id);
out.page = T.childPage({children: Array.from({length: 40}, (_, i) => ({id:i, _vis:i < 35}))}, 28);
process.stdout.write(JSON.stringify(out));
"""


def node(nid, kind, label, children=(), **kw):
    n = {"id": nid, "kind": kind, "label": label, "children": list(children), "date": "2026-09-01"}
    n.update(kw)
    return n


def tree():
    notes = [node("m:n%d" % i, "заметка", "Заметка %d" % i, subtree_weight=100 + i) for i in range(100)]
    tasks = [node("t:a:%d" % i, "задача", "Задача %d" % i, status=s, subtree_weight=5000,
                  children=[node("c:a:%d:1" % i, "коммит", "коммит")] if i == 0 else [])
             for i, s in enumerate(["done", "queued", "в реализации 59%", "частично", "idea", "blocked"])]
    alpha = node("p:alpha", "проект", "Альфа", project="alpha", subtree_weight=900000, children=[
        node("f:alpha", "фундамент", "Фундамент", children=[
            node("d:alpha:%d" % i, "сделано", "Сделано %d" % i, subtree_weight=1000) for i in range(5)]),
        node("m:q", "запрос", "Большой запрос", subtree_weight=20000, children=notes),
        node("b:alpha", "бэклог", "Ветви", children=tasks),
    ])
    beta = node("p:beta", "проект", "Бета", project="beta", subtree_weight=3000, children=[
        node("m:b1", "решение", "Решение Беты", subtree_weight=300)])
    return {"root": node("портфель", "портфель", "Портфель", [alpha, beta], subtree_weight=1000000),
            "stats": {"months": ["2026-09"]}}


STATUSES = {
    "done": "done", "готово": "done", "queued": "backlog", "": "backlog", "открыт": "backlog",
    "in-progress": "progress", "в реализации 59%": "progress", "идёт": "progress",
    "частично": "partial", "промпты готовы, генерация не начата": "partial",
    "idea": "later", "не начато": "later", "отложено": "later",
    "blocked": "blocked", "ждёт решения владельца стенда": "blocked",
    "in_progress": "progress", "готово частично": "partial", "completed": "done",
}


@unittest.skipUnless(NODE, "нужен Node.js")
class TreeView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        payload = json.dumps({"statuses": list(STATUSES), "tree": tree()}, ensure_ascii=False)
        r = subprocess.run([NODE, "-e", HARNESS, os.path.join(TOOLS, "tree.js")], input=payload,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_every_status_has_one_colour(self):                         # ДРВ-09
        self.assertEqual(self.out["status"], STATUSES)
        self.assertEqual(len(set(STATUSES.values())), 6)

    def test_roots_below_crown_above(self):                             # ДРВ-09
        self.assertTrue(self.out["finite"])
        self.assertTrue(self.out["rootsBelow"])
        self.assertTrue(self.out["crownAbove"])
        self.assertEqual(self.out["tips"], 5)
        self.assertEqual(self.out["blossoms"], 6)                        # задачи — цветы
        self.assertEqual(self.out["fruits"], 1)                          # коммит — плод под задачей

    def test_every_node_has_a_path_to_the_trunk(self):                  # ДРВ-08: подсветка пути
        self.assertEqual(self.out["noPath"], [])

    def test_wide_node_forks_instead_of_a_broom(self):                  # ДРВ-07
        self.assertLessEqual(self.out["fanMax"], 3, self.out["fanNode"])

    def test_search_uses_project_words_and_normalizes_yo(self):
        self.assertEqual(len(self.out["search"]), 6)
        self.assertEqual(self.out["exactSearch"][0], 'a')

    def test_wide_children_can_be_paged_without_hidden_children_in_count(self):
        self.assertEqual(len(self.out["page"]["items"]), 28)
        self.assertEqual(self.out["page"]["remaining"], 7)
        self.assertEqual(self.out["page"]["hidden"], 5)


if __name__ == "__main__":
    unittest.main()
