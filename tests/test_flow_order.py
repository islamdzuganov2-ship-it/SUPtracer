"""Готовое к выпуску — раньше нового (TT-106; ТЗ FR-014 п.3–4: TT3-082, AC-14.2, TC-27)."""
import unittest
from datetime import datetime, timezone

from base import TOOLS  # noqa: F401 — пути к tools до импорта модулей системы
import flow_policy as fp
import orchestra as O
import sprints as sp

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


class FinishFirst(unittest.TestCase):
    def setUp(self):
        self.s = dict(runs=[], settings=dict(flow_mode='rolling'),     # без миграции: начатое — в слотах
                      delivery_sprints=dict(sequence=0, current=None, history=[], legacy=[]))
        self.tasks = [dict(ref='p/T-0', status='in_progress', pipeline=dict(stage='testing')),
                      dict(ref='q/Q-0', status='queued'), dict(ref='q/Q-1', status='queued')]
        sp.admit(self.s, 'p/T-0', self.tasks, at=NOW)
        self.run = dict(id='RUN-1', ref='p/T-0', role='qa', stage='testing', status='queued')
        self.s['runs'].append(self.run)

    def test_release_ready_blocks_new_admission(self):                           # TC-27
        why = sp.reason(self.s, 'q/Q-0', self.tasks, at=NOW)
        self.assertIn('сначала довести: p/T-0 (testing)', why)
        self.assertEqual(fp.finish_candidates(self.s, self.tasks, at=NOW)[0]['run'], 'RUN-1')

    def test_not_actionable_does_not_block(self):
        """Идёт прямо сейчас, на паузе по квоте или ждёт владельца — ресурс не нужен, новое можно."""
        for change in (dict(status='running'), dict(paused=dict(reason='limit'))):
            self.run.update(status='queued'); self.run.pop('paused', None); self.run.update(change)
            self.assertIsNone(sp.reason(self.s, 'q/Q-0', self.tasks, at=NOW), change)
        self.run.update(status='done')
        self.assertIsNone(sp.reason(self.s, 'q/Q-0', self.tasks, at=NOW))

    def test_prefer_finish_off(self):
        self.s['settings']['prefer_finish'] = False
        self.assertIsNone(sp.reason(self.s, 'q/Q-0', self.tasks, at=NOW))

    def test_batch_mode_only_shadows_finish_first(self):
        self.s['settings']['flow_mode'] = 'batch'
        sp.admit(self.s, 'q/Q-0', self.tasks, at=NOW)                             # batch пустил
        d = self.s['delivery_sprints']['decisions'][-1]
        self.assertTrue(d['shadow_differs']); self.assertIn('сначала довести', d['shadow_verdict'])

    def test_dispatch_order(self):
        """Релиз → тест → деплой в тест → ревью → доработка после возврата → разработка → анализ → идея."""
        tasks = {'a/R': dict(pipeline=dict(roles={'development/developer': dict(run='5', feedback='QA: падает экспорт')}))}
        runs = [dict(id='1', ref='a/N', role='product_owner', stage='idea'),
                dict(id='2', ref='a/A', role='system_analyst', stage='analysis'),
                dict(id='3', ref='a/D', role='developer', stage='development'),
                dict(id='4', ref='a/V', role='reviewer', stage='review'),
                dict(id='5', ref='a/R', role='developer', stage='development'),
                dict(id='6', ref='a/T', role='qa', stage='testing'),
                dict(id='7', ref='a/M', role='devops', stage='deploy_main'),
                dict(id='8', ref='a/X', role='devops', stage='deploy_test')]
        order = [r['id'] for r in sorted(runs, key=lambda r: O.dispatch_order(r, tasks, {}))]
        self.assertEqual(order, ['7', '6', '8', '4', '5', '3', '2', '1'])
