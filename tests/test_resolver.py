"""Решала: доказательная база проблемы, маршрут по классу сбоя, причина решения (07: AG-RES-02…06, E2E-R18)."""
from test_pipeline import PipelineTests           # первым: base ставит путь к tools/
import orchestra as O
import workflow as w


class ResolverTests(PipelineTests):
    def test_route_hint_by_failure_class(self):
        env = lambda cls, cid='x': dict(cls=cls, checks=[dict(id=cid, cls=cls)])
        self.assertEqual(O.route_hint(env('test'), 'reviewer')['target'], 'developer')
        self.assertEqual(O.route_hint(env('environment'), 'developer')['target'], 'devops')
        self.assertEqual(O.route_hint(env('policy'), 'developer')['target'], 'owner')
        self.assertEqual(O.route_hint(env('artifact', 'sa-srs'), 'developer')['target'], 'system_analyst')
        self.assertEqual(O.route_hint(env('artifact', 'po-brd'), 'qa')['target'], 'product_owner')
        unknown = O.route_hint(None, 'developer')
        self.assertEqual((unknown['cls'], unknown['target']), ('unknown', 'resolver'))
        self.assertIn('UNKNOWN', unknown['why'])

    def stuck_po(self):
        """Три одинаковых отказа сдачи PO без BRD — сервер сам зовёт Решалу."""
        self.ref = self.idea()
        run = self.claim('product_owner')
        for _ in range(O.SAME_REJECTS):
            r = self.handoff('product_owner', 'idea', run)
        self.assertTrue(r.get('escalated'), r)
        return next(p for p in w.state()['problems'] if p['id'] == r['escalated'])

    def test_problem_carries_envelope_history_sha_and_route(self):
        prob = self.stuck_po()
        ev = prob['evidence']
        self.assertEqual(ev['basis'], 'envelope')
        self.assertEqual(ev['envelope']['cls'], 'artifact')
        self.assertEqual(len(ev['prior']), O.SAME_REJECTS - 1)                   # прежние конверты — не только последний
        self.assertEqual(len({e['fingerprint'] for e in ev['prior'] + [ev['envelope']]}), 1)
        self.assertRegex(ev['sha'], r'^[0-9a-f]{40}$')
        self.assertEqual(prob['route_hint']['target'], 'product_owner')
        rs = self.claim('resolver')
        packet = w.action(dict(op='packet', ref=self.ref, run=rs))
        self.assertEqual(packet['problem']['route_hint']['target'], 'product_owner')
        self.assertIn('UNKNOWN', packet['rules'])

    def test_resolution_needs_cause_and_probe_for_hypothesis(self):
        self.stuck_po()
        rs = self.claim('resolver')
        base = dict(op='resolve', ref=self.ref, run=rs, worker='w-' + rs, options=self.OPTS, decision='continue',
                    notes='BRD не записан: агент сдавал этап без артефакта три раза подряд',
                    instructions='Сначала напиши BRD в ветку задачи по паспорту, затем сдай этап заново')
        with self.assertRaisesRegex(ValueError, 'cause_kind'):
            w.action(base)
        with self.assertRaisesRegex(ValueError, 'probe'):
            w.action(dict(base, cause_kind='hypothesis', cause='Агент не видит путь к BRD в паспорте'))
        w.action(dict(base, cause_kind='fact', cause='В ветке задачи нет файла BRD: po-brd провален три раза'))
        prob = w.state()['problems'][-1]
        self.assertEqual(prob['cause']['kind'], 'fact')
        self.assertEqual(prob['status'], 'resolved')

    def test_trivial_problem_takes_short_route(self):
        self.ref = self.idea()
        s = w.state(); t = w.task_for(self.ref)
        p = O.open_problem(s, t, w.now(), 'лимит токенов исчерпан', 'product_owner', 'claude', 'idea')
        self.assertEqual(p['evidence']['basis'], 'UNKNOWN')
        p['route_hint'] = O.route_hint(dict(cls='quota', checks=[]), 'product_owner')
        self.assertEqual(p['route_hint']['target'], 'pause')
        self.assertIn(p['route_hint']['cls'], O.TRIVIAL)
        prob = self.stuck_po()
        with w.locked():
            s = w.state(); x = next(q for q in s['problems'] if q['id'] == prob['id'])
            x['route_hint'] = O.route_hint(dict(cls='quota', checks=[]), 'product_owner'); w.save('workflow.json', s)
        rs = self.claim('resolver')
        r = w.action(dict(op='resolve', ref=self.ref, run=rs, worker='w-' + rs, options=self.OPTS[:1], decision='continue',
                          cause_kind='fact', cause='Лимит токенов исчерпан на середине сдачи этапа',
                          notes='Простая проблема: дождаться сброса лимита и сдать этап',
                          instructions='После сброса лимита продолжи с точки остановки и сдай этап'))
        self.assertEqual(r['decision'], 'continue')                                # один путь — простая проблема


for _n in [n for n in dir(PipelineTests) if n.startswith('test_') and n not in ResolverTests.__dict__]:
    setattr(ResolverTests, _n, None)
del PipelineTests
