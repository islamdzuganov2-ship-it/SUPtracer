"""Многофакторная оценка сложности (TT-57): свойства, а не таблица ответов."""
import unittest
import base  # noqa: F401  путь к tools
import complexity as cx

BASE = dict(size='M', risk='medium', work_type='implementation', task_type='dev_by_spec',
            spec_ref='docs/ТЗ.md', acceptance=['a', 'b'])
ALL = ('low', 'medium', 'high', 'xhigh', 'max')


class ComplexityTests(unittest.TestCase):
    def score(self, **patch):
        return cx.assess(dict(BASE, **patch))['score']

    def test_monotonic_in_each_factor(self):
        for key, ladder in (('size', 'SML'), ('risk', ('low', 'medium', 'high')),
                            ('work_type', ('docs', 'test', 'implementation', 'research', 'architecture'))):
            scores = [self.score(**{key: v}) for v in ladder]
            with self.subTest(key=key):
                self.assertEqual(scores, sorted(scores))
        self.assertLess(self.score(acceptance=['a']), self.score(acceptance=list('abcdefg')))
        self.assertLess(self.score(scope=['src/a.py']), self.score(scope=['.']))
        self.assertLess(self.score(), self.score(depends_on=['X-1', 'X-2']))

    def test_not_one_rule_analysis_is_not_always_top_model(self):
        small = cx.assess(dict(size='S', risk='low', work_type='research', task_type='analysis_spec',
                               acceptance=['a'], question='Какой формат', inputs='x', deliverable='y'))
        self.assertLess(small['tier'], 3)
        big = cx.assess(dict(size='L', risk='high', work_type='architecture', task_type='analysis_spec',
                             acceptance=list('abcdef'), question='Миграция архитектуры и безопасность'))
        self.assertEqual(big['tier'], 3)

    def test_text_signals_move_score_both_ways(self):
        plain = self.score(title='Экспорт')
        self.assertGreater(self.score(title='Миграция и интеграция с шифрованием'), plain)
        self.assertLess(self.score(title='Опечатки и оформление README'), plain)

    def test_every_factor_explained(self):
        c = cx.assess(BASE)
        self.assertAlmostEqual(sum(f['contribution'] for f in c['factors']), c['heuristic'], places=2)
        self.assertEqual(round(sum(f['weight'] for f in c['factors']), 6), 1)
        self.assertIn('балл', cx.summary(c))

    def test_confidence_drops_on_unknown_and_conflicts(self):
        clear = cx.assess(BASE)['confidence']
        self.assertLess(cx.assess(dict(BASE, risk=None, acceptance=[]))['confidence'], clear)
        self.assertLess(cx.assess(dict(BASE, size='S', risk='high'))['confidence'], clear)
        self.assertIn('размер и риск расходятся', cx.assess(dict(BASE, size='S', risk='high'))['conflicts'])

    def test_who_needs_ai_assessment(self):
        self.assertFalse(cx.assess(BASE)['needs_assessment'])
        self.assertTrue(cx.assess(dict(BASE, task_type='analysis_spec'))['needs_assessment'])
        self.assertTrue(cx.assess(dict(BASE, task_type='decomposition'))['needs_assessment'])
        vague = dict(size=None, risk=None, work_type=None, acceptance=[])
        self.assertTrue(cx.assess(vague)['needs_assessment'])
        done = dict(BASE, task_type='analysis_spec', assessment=dict(complexity=2, uncertainty=2, breadth=2, reasons='r'))
        self.assertFalse(cx.assess(done)['needs_assessment'])

    def test_ai_assessment_outweighs_heuristic(self):
        task = dict(size='L', risk='medium', work_type='research', task_type='analysis_spec', acceptance=list('abcd'))
        low = cx.assess(dict(task, assessment=dict(complexity=1, uncertainty=1, breadth=1, reasons='r')))
        high = cx.assess(dict(task, assessment=dict(complexity=5, uncertainty=5, breadth=5, reasons='r')))
        self.assertEqual((low['tier'], high['tier']), (1, 3))

    def test_effort_respects_tier_cap_and_provider(self):
        task = dict(BASE, size='S', risk='low', assessment=dict(complexity=1, uncertainty=5, breadth=1, reasons='r'))
        c = cx.assess(task)
        self.assertEqual(c['tier'], 1)
        self.assertEqual(cx.effort_for(c, ALL, task)[0], 'medium')   # не выше потолка класса
        top = dict(BASE, size='L', risk='high', work_type='architecture', assessment=dict(complexity=5, uncertainty=5, breadth=5, reasons='r'))
        c = cx.assess(top)
        self.assertEqual(cx.effort_for(c, ALL, top)[0], 'max')
        self.assertEqual(cx.effort_for(c, ('low', 'medium', 'high'), top)[0], 'high')

    def test_high_risk_never_bottom_tier_without_ai(self):
        c = cx.assess(dict(size='S', risk='high', work_type='docs', acceptance=['a']))
        self.assertGreaterEqual(c['tier'], 2)

    def test_assessment_validation(self):
        for bad in (None, dict(complexity=0, uncertainty=1, breadth=1, reasons='r'),
                    dict(complexity=2, uncertainty=2, breadth=2, reasons=''), dict(complexity='3', uncertainty=2, breadth=2, reasons='r')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cx.validate_assessment(bad)


if __name__ == '__main__':
    unittest.main()
