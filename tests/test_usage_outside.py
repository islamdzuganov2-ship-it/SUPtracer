"""Расход вне портфеля — только по решению владельца, с причиной и датой, и остаётся виден."""
import importlib

from base import DataCase


class UsageOutside(DataCase):
    def setUp(self):
        super().setUp()
        self.portfolio = self.projects()
        import check
        self.check = importlib.reload(check)
        self.jwrite('usage.json', {'projects': {'doc:chatgpt': {'usd': 41.45}, 'scratch-x': {'usd': 4.5}}})

    def run_check(self, outside=None):
        if outside is not None:
            self.portfolio['outside_portfolio'] = outside
        self.jwrite('projects.json', self.portfolio)
        return self.check.t_usage_mapping(), self.check.t_usage_outside()

    def test_unknown_usage_still_fails(self):
        fails, seen = self.run_check()
        self.assertEqual(len(fails), 2)
        self.assertEqual(seen, [])

    def test_named_session_is_excluded_but_visible(self):
        fails, seen = self.run_check({'doc:chatgpt': dict(why='Разовая настройка роутера, не проект', date='2026-11-08')})
        self.assertEqual(len(fails), 1)                       # scratch-x не назван — по-прежнему провал
        self.assertIn('scratch-x', fails[0])
        self.assertEqual(len(seen), 1)
        self.assertIn('$41', seen[0]); self.assertIn('роутера', seen[0])
        self.assertIn(self.check.t_usage_outside, self.check.SOFT)

    def test_entry_needs_reason_and_date(self):
        fails, _ = self.run_check({'doc:chatgpt': dict(why='надо'), 'scratch-x': dict(why='Чат без папки проекта', date='вчера')})
        self.assertEqual(sum('outside_portfolio/' in f for f in fails), 2)
        self.assertTrue(any('doc:chatgpt' in f and 'ни к чему' in f for f in fails))   # плохая запись не исключает
