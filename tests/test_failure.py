"""Конверт сбоя и отпечаток одинаковой ошибки (07: A8-02, A8-05, AG-RES-02, E2E-R17)."""
import unittest

from test_pipeline import PipelineTests      # первым: base ставит путь к tools/
import failure
import workflow as w


class EnvelopeTests(unittest.TestCase):
    def test_fingerprint_ignores_numbers_hashes_paths_and_time(self):
        a = [dict(id='dev-verify', detail='3 tests failed in 12.4s at 2026-11-08T10:00:00Z, commit 1a2b3c4d, C:\\tmp\\x1\\log.txt')]
        b = [dict(id='dev-verify', detail='7 tests failed in 3.1s at 2026-11-09T11:30:00Z, commit 9f8e7d6c, C:\\tmp\\y2\\log.txt')]
        c = [dict(id='dev-tests', detail='тесты не менялись')]
        self.assertEqual(failure.fingerprint(a), failure.fingerprint(b))
        self.assertNotEqual(failure.fingerprint(a), failure.fingerprint(c))
        self.assertEqual(failure.fingerprint(a + c), failure.fingerprint(c + b))     # порядок провалов не важен

    def test_classes_are_chosen_by_rule(self):
        cls = lambda **f: failure.classify(f)
        self.assertEqual(cls(id='dm-merge-main', detail='слияние запрещён политикой: нет одобрения владельца'), 'policy')
        self.assertEqual(cls(id='dev-oracle', detail='изменён эталон приёмки'), 'security')
        self.assertEqual(cls(id='qa-ci', detail='CI не настроен для проекта'), 'environment')
        self.assertEqual(cls(id='dev-verify', detail='2 tests failed'), 'test')
        self.assertEqual(cls(id='zone', detail='вне зоны роли изменены: app.py'), 'scope')
        self.assertEqual(cls(id='po-brd', detail='нет раздела «Проблема» в BRD.md'), 'artifact')
        self.assertEqual(cls(id='x', detail='что-то непонятное'), 'unknown')
        env = failure.envelope([dict(id='po-brd', detail='нет раздела'), dict(id='dm-merge-main', detail='запрещён политикой')],
                               'devops', 'deploy_main', run='RUN-1')
        self.assertEqual(env['cls'], 'policy')                                       # самый тяжёлый класс набора
        self.assertEqual({c['id'] for c in env['checks']}, {'po-brd', 'dm-merge-main'})
        self.assertEqual(failure.repeated([dict(fingerprint='a'), dict(fingerprint='b'), dict(fingerprint='b')], 'b'), 2)


class RepeatedRejectTests(PipelineTests):
    def test_same_rejection_three_times_goes_to_resolver(self):
        self.ref = self.idea()
        run = self.claim('product_owner')
        r1 = self.handoff('product_owner', 'idea', run)
        self.assertFalse(r1['accepted']); self.assertEqual(r1['failure']['cls'], 'artifact')
        self.assertNotIn('escalated', r1)
        r2 = self.handoff('product_owner', 'idea', run)
        self.assertIn('Решале', r2['hint'])                                          # предупреждение до эскалации
        self.assertEqual(r2['failure']['fingerprint'], r1['failure']['fingerprint'])
        r3 = self.handoff('product_owner', 'idea', run)
        self.assertTrue(r3.get('escalated'), r3)
        t = self.task()
        self.assertEqual(t['status'], 'blocked')
        prob = next(p for p in w.state()['problems'] if p['id'] == r3['escalated'])
        self.assertIn(r1['failure']['fingerprint'], prob['reason'])
        self.assertTrue(any(r['role'] == 'resolver' for r in w.state()['runs'] if r['ref'] == self.ref))
        self.assertEqual(t['pipeline']['failures'][-1]['cls'], 'artifact')          # конверт в истории задачи

    def test_different_errors_do_not_escalate_early(self):
        self.ref = self.idea()
        run = self.claim('product_owner')
        self.handoff('product_owner', 'idea', run); self.handoff('product_owner', 'idea', run)
        self.write('product_owner', 'idea')                                          # ошибка сменилась: BRD есть, нет критериев
        r = self.handoff('product_owner', 'idea', run)
        self.assertFalse(r['accepted']); self.assertNotIn('escalated', r)
        r = self.handoff('product_owner', 'idea', run, acceptance=['CSV содержит все проекты портфеля', 'Файл открывается в Excel без ошибок'],
                         needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса и запуска'))
        self.assertTrue(r['accepted'], r)
        self.assertNotIn('failures', self.task()['pipeline']['roles']['idea/product_owner'])   # сдал — история сброшена


# Сценарии конвейера унаследованы ради помощников, но гоняются в test_pipeline, а не второй раз здесь.
for _n in [n for n in dir(PipelineTests) if n.startswith('test_') and n not in RepeatedRejectTests.__dict__]:
    setattr(RepeatedRejectTests, _n, None)
del PipelineTests
