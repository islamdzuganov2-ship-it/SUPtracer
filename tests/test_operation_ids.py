"""Идентификатор операции и дедупликация (TT-105; ТЗ TT-U-001: TT3-017, AC-43, TC-12).

Одинаковый operation_id не вызывает двойного действия: ни тяжёлого (слияние, тег, развёртывание),
ни запроса tt_work. Прерванное действие сверяется по Git; не проверяемое — ждёт владельца.
"""
from datetime import timedelta
from pathlib import Path

from base import DataCase
import mcp_server
import operation_ids as oi
import workflow as w


class OperationIdTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data) / 'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        self.calls = 0

    def act(self, ok=True):
        def f():
            self.calls += 1
            return ok, 'сделано %d' % self.calls
        return f

    def age(self, op_id, minutes=120, status='started'):
        s = w.state()
        s['operations'][op_id].update(status=status, at=(w.now() - timedelta(minutes=minutes)).isoformat())
        w.save('workflow.json', s)

    # --- тяжёлые действия

    def test_same_id_runs_once(self):
        op = oi.heavy_id('deploy_main', 'alpha-api/AL-01', 'main', 'a' * 40)
        self.assertEqual(op, 'op:deploy_main:alpha-api/AL-01:main:aaaaaaaaaaaa')
        self.assertEqual(oi.guarded(op, self.act()), (True, 'сделано 1'))
        ok, why = oi.guarded(op, self.act())
        self.assertTrue(ok); self.assertIn('уже выполнено', why)
        self.assertEqual(self.calls, 1)

    def test_failed_may_retry_and_crash_is_recorded(self):
        op = 'op:test:fail:retry'
        self.assertFalse(oi.guarded(op, self.act(ok=False))[0])
        self.assertTrue(oi.guarded(op, self.act())[0])                              # failed — повтор разрешён
        self.assertEqual(self.calls, 2)

        def boom():
            raise RuntimeError('процесс упал')
        with self.assertRaises(RuntimeError):
            oi.guarded('op:test:crash:01', boom)
        self.assertEqual(w.state()['operations']['op:test:crash:01']['status'], 'failed')

    def test_fresh_started_is_not_run_twice(self):
        op = 'op:test:concurrent'
        oi.guarded(op, self.act())
        self.age(op, minutes=1)                                                     # будто идёт прямо сейчас
        ok, why = oi.guarded(op, self.act())
        self.assertFalse(ok); self.assertIn('уже выполняется', why)
        self.assertEqual(self.calls, 1)

    def test_interrupted_reconciled_by_git_without_repeat(self):
        op = 'op:test:merged:01'
        oi.guarded(op, self.act()); self.age(op)                                    # умер после действия
        ok, why = oi.guarded(op, self.act(), applied=lambda: (True, 'коммит уже в main'))
        self.assertTrue(ok); self.assertIn('уже применено', why)
        self.assertEqual(self.calls, 1)
        self.assertTrue(w.state()['operations'][op]['reconciled'])

    def test_interrupted_unverifiable_waits_for_owner(self):
        op = 'op:env_main:alpha-api/AL-01:main:bbbbbbbbbbbb'
        oi.guarded(op, self.act()); self.age(op)
        for _ in range(2):
            ok, why = oi.guarded(op, self.act())
            self.assertFalse(ok); self.assertIn('нужна сверка владельцем', why)
        self.assertEqual(self.calls, 1)
        kinds = [n['kind'] for n in w.notifications(w.state(), w.snapshot()['tasks'])]
        self.assertIn('reconcile', kinds)
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(dict(op='reconcile', operation=op, decision='retry'))
        self.assertTrue(mcp_server.call_tool('tt_work', dict(op='reconcile', operation=op, decision='done'))['isError'])
        w.action(dict(op='reconcile', operation=op, decision='retry'), origin='owner')
        self.assertTrue(oi.guarded(op, self.act())[0])
        self.assertEqual(self.calls, 2)
        with self.assertRaisesRegex(ValueError, 'не ждёт сверки'):
            w.action(dict(op='reconcile', operation=op, decision='retry'), origin='owner')

    # --- запросы tt_work

    def plan(self, op_id, title='Правка'):
        return w.action(dict(op='plan', project='alpha-api', operation_id=op_id, tasks=[dict(
            id='AL-07', title=title, why='Нужна', root='request', size='S', priority='🟡', risk='low',
            work_type='implementation', acceptance=['Тест проходит'], depends_on=[])]))

    def test_request_replay_is_idempotent(self):
        first = self.plan('req-plan-0001')
        again = self.plan('req-plan-0001')                                          # повтор того же запроса
        self.assertTrue(again['replay'])
        self.assertEqual(again['added'], first['added'])
        backlog = w.read('projects.json', {})['projects']['alpha-api']['backlog']
        self.assertEqual(len([t for t in backlog if t['id'] == 'AL-07']), 1)
        with self.assertRaisesRegex(ValueError, 'для другого запроса'):
            self.plan('req-plan-0001', title='Другая правка')
        with self.assertRaisesRegex(ValueError, 'operation_id'):
            self.plan('x')

    def test_failed_request_frees_id_and_concurrent_refused(self):
        with self.assertRaises(ValueError):
            w.action(dict(op='take', ref='alpha-api/НЕТ', model='m', worker='w', operation_id='req-take-0001'))
        self.assertNotIn('req:req-take-0001', w.state().get('operations', {}))
        oi.claim_request('req-busy-0001', dict(op='focus', projects=[]), 'agent')  # другой процесс начал
        with self.assertRaisesRegex(ValueError, 'уже выполняется'):
            w.action(dict(op='focus', projects=[], operation_id='req-busy-0001'))

    def test_owner_check_before_replay_cache(self):
        w.action(dict(op='settings', changes=dict(max_sessions=5), operation_id='req-set-0001'), origin='owner')
        # Агент повторяет id владельца — кэш ответа не даёт обойти проверку прав.
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(dict(op='settings', changes=dict(max_sessions=5), operation_id='req-set-0001'))
        r = mcp_server.call_tool('tt_work', dict(op='focus', projects=[], operation_id='req-mcp-0001'))
        self.assertFalse(r['isError'], r)
