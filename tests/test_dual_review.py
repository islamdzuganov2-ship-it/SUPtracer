"""Двойное независимое ревью CRITICAL и реестр разногласий (07: A7-03, A7-04, AG-REV-07, E2E-R05)."""
from test_pipeline import PipelineTests           # первым: base ставит путь к tools/
import adaptive_pipeline as ap
import workflow as w


class DualReviewTests(PipelineTests):
    def to_development(self, risk='high'):
        self.ref = self.idea()
        d = w.read('projects.json', {})
        t = next(x for x in d['projects']['alpha-api']['backlog'] if 'alpha-api/' + x['id'] == self.ref)
        t.update(risk=risk); w.save('projects.json', d)
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        for role in ('system_analyst', 'solution_architect', 'qa'):
            self.do_role(role, 'analysis')
        if 'analysis' not in self.task()['pipeline']['gates']:
            gate = self.claim('teamlead', gate='analysis')
            w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve', notes='Анализ согласован, можно разрабатывать'))

    def to_review(self, risk='high'):
        self.to_development(risk)
        self.do_role('developer', 'development', extra=[('app.py', 'x = 3\n'), ('tests/test_app.py', 'assert True\n')])
        self.assertEqual(self.task()['pipeline']['stage'], 'review')

    def test_critical_code_that_tests_do_not_check_fails_mutation(self):     # 07: A6-08, E2E-R14
        self.to_development()
        r = self.do_role('developer', 'development', extra=[('app.py', 'def fee(x):\n    return x + 1 if x > 0 else 0\n'),
                                                            ('tests/test_app.py', 'assert True\n')])
        self.assertTrue(r['accepted'], r)                                          # тяжёлые пункты — в фоне
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'development')                    # мутационная проверка вернула работу
        fb = t['pipeline']['roles']['development/developer'].get('feedback') or []
        mut = next(f for f in fb if f['id'] == 'dev-mutation')
        self.assertIn('app.py:', mut['detail'])

    def reviewers(self):
        return [r for r in w.state()['runs'] if r['ref'] == self.ref and r.get('role') == 'reviewer']

    def test_critical_task_needs_two_independent_approvals(self):
        self.to_review()
        self.assertEqual((ap.route(self.task()['pipeline']).get('plan') or {}).get('mode'), 'CRITICAL')
        dev = next(r for r in w.state()['runs'] if r['ref'] == self.ref and r.get('role') == 'developer')['agent']
        self.assertTrue(self.do_role('reviewer', 'review', verdict='approve')['accepted'])
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'review')                          # одного одобрения мало
        self.assertIn('второе независимое ревью', t['status_note'])
        first, second = self.reviewers()
        self.assertEqual(second['dual'], dict(second_of=first['id']))
        self.assertNotEqual(second['agent'], dev)                                  # автор не проверяет себя и вторым
        self.assertNotEqual((second['agent'], second['packet']['route']['model']),
                            (first['agent'], first['packet']['route']['model']))   # второе мнение, а не повтор
        r2 = self.do_role('reviewer', 'review', verdict='approve'); self.assertTrue(r2['accepted'], r2)
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'deploy_test')
        dr = t['pipeline']['dual_review']
        self.assertIn(dr['second']['independence'], ('provider', 'model'))

    def test_disagreement_is_registered_and_strict_side_wins(self):
        self.to_review()
        self.do_role('reviewer', 'review', verdict='approve')
        run = self.claim('reviewer'); self.write('reviewer', 'review')
        r = w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                          reason='Нет обработки пустого списка проектов: выгрузка падает с исключением',
                          defects=[dict(title='Пустой портфель роняет выгрузку', severity='major', level='unit', test='test_empty_portfolio')]))
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'development')                     # строгая позиция победила
        self.assertNotIn('dual_review', t['pipeline'])                              # новый код — оба ревью заново
        reg = w.state()['disagreements']
        self.assertEqual(len(reg), 1)
        d = reg[0]
        self.assertEqual((d['ref'], d['first']['verdict'], d['second']['verdict'], d['resolution']), (self.ref, 'approve', 'return', 'return'))
        self.assertIn('пустого списка', d['second']['reason'])
        self.assertIn('disagreement', {e['kind'] for e in t['pipeline']['journal']})

    def test_standard_task_keeps_single_review_and_owner_can_switch_off(self):
        self.to_review(risk='low')
        self.do_role('reviewer', 'review', verdict='approve')
        self.assertEqual(self.task()['pipeline']['stage'], 'deploy_test')
        with self.assertRaises(Exception):
            w.action(dict(op='settings', changes=dict(dual_review='off')))          # агент политику не меняет
        w.action(dict(op='settings', changes=dict(dual_review='off')), origin='owner')
        self.assertEqual(w.settings(w.state())['dual_review'], 'off')


for _n in [n for n in dir(PipelineTests) if n.startswith('test_') and n not in DualReviewTests.__dict__]:
    setattr(DualReviewTests, _n, None)
del PipelineTests
