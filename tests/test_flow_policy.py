"""Слот на задачу и политика владельца (TT-106; ТЗ FR-014: TT3-080, AC-14.1/3/6/8, TC-26, TC-28…30, TC-33…35).

Слот ИИ-сессии ≠ слот задачи: свободная сессия не даёт права начать новую задачу.
"""
import unittest
from datetime import datetime, timedelta, timezone

from base import DataCase
import flow_policy as fp
import mcp_server
import sprints as sp
import workflow as w

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def rolling(**kw):
    return dict(runs=[], settings=dict(flow_mode='rolling', **kw))


class RollingSlots(unittest.TestCase):
    def setUp(self):
        self.s = rolling()
        self.tasks = [dict(ref='p/T-%d' % i, status='queued') for i in range(4)] + \
                     [dict(ref='q/Q-%d' % i, status='queued') for i in range(4)]

    def admit(self, ref):
        sp.admit(self.s, ref, self.tasks, at=NOW)

    def merge(self, ref):
        t = next(t for t in self.tasks if t['ref'] == ref)
        t['pipeline'] = dict(stage='done', stages={'deploy_main': {'passed': 'sha abc1234'}})

    def test_project_and_global_limits(self):                                    # TC-26
        self.admit('p/T-0'); self.admit('p/T-1')
        why = sp.reason(self.s, 'p/T-2', self.tasks, at=NOW)
        self.assertIn('в проекте p 2 задач из 2', why)
        self.admit('q/Q-0'); self.admit('q/Q-1')
        codes = [c for c, _ in fp.rolling_codes(self.s, 'q/Q-2', self.tasks, NOW)]
        self.assertEqual(codes, ['WIP_GLOBAL_LIMIT', 'PROJECT_WIP_LIMIT'])
        with self.assertRaisesRegex(ValueError, 'Поток'):
            self.admit('q/Q-2')

    def test_repeat_admission_takes_no_second_slot(self):                        # AC-14.5, TC-34
        for _ in range(3):
            self.admit('p/T-0')
        self.assertEqual(list(fp.slots(self.s)), ['p/T-0'])
        self.assertEqual(len([d for d in self.s['delivery_sprints']['decisions'] if d['decision'] == 'admit']), 1)

    def test_release_frees_exactly_one_slot(self):                               # AC-14.3, TC-29
        for r in ('p/T-0', 'p/T-1', 'q/Q-0', 'q/Q-1'):
            self.admit(r)
        self.merge('p/T-0')
        self.admit('p/T-2')
        with self.assertRaises(ValueError):
            self.admit('p/T-3')
        rel = [d for d in self.s['delivery_sprints']['decisions'] if d['decision'] == 'release_complete']
        self.assertEqual([d['ref'] for d in rel], ['p/T-0'])
        self.assertEqual(fp.slots(self.s)['p/T-0']['released_by'], 'release')
        sp.sync(self.s, self.tasks)                                               # повторный проход — не второе освобождение
        self.assertEqual(len([d for d in self.s['delivery_sprints']['decisions'] if d['decision'] == 'release_complete']), 1)

    def test_review_done_pause_quota_do_not_release(self):                       # TC-28, TC-30, AC-14.4
        for r in ('p/T-0', 'p/T-1', 'q/Q-0', 'q/Q-1'):
            self.admit(r)
        self.tasks[0]['status'] = 'done'                                          # done без main
        self.tasks[1]['status'] = 'review'
        self.tasks[1]['pipeline'] = dict(stage='review')
        self.s['runs'] = [dict(id='RUN-1', ref='q/Q-0', role='developer', stage='development', status='queued',
                               paused=dict(reason='limit'))]
        self.assertEqual(len(fp.open_refs(self.s, self.tasks)), 4)
        self.assertIn('в потоке 4 задач из 4', sp.reason(self.s, 'q/Q-2', self.tasks, at=NOW))

    def test_owner_release_slot_and_reopen(self):
        self.admit('p/T-0'); self.admit('p/T-1')
        with self.assertRaisesRegex(ValueError, 'reason'):
            fp.release_slot(self.s, 'p/T-0', '', self.tasks, NOW, 'owner')
        fp.release_slot(self.s, 'p/T-0', 'Задача отменена владельцем', self.tasks, NOW, 'owner')
        self.admit('p/T-2')
        with self.assertRaisesRegex(ValueError, 'нет занятого слота'):
            fp.release_slot(self.s, 'p/T-0', 'ещё раз закрыть', self.tasks, NOW, 'owner')
        # Повторно открытая задача — новая заявка, номер растёт, старый слот не воскресает.
        fp.release_slot(self.s, 'p/T-2', 'Тоже отменена', self.tasks, NOW, 'owner')
        self.tasks[0]['status'] = 'queued'
        slot = fp.slots(self.s)['p/T-0']
        self.assertTrue(slot['closed_at'])
        fp.slots(self.s)['p/T-0'].pop('released_by')                              # закрыт и без отметки — всё равно не открыт
        self.assertNotIn('p/T-0', fp.open_refs(self.s, self.tasks))

    def test_exception_passes_limit_but_expires(self):                           # AC-14.6, TC-33
        for r in ('p/T-0', 'p/T-1'):
            self.admit(r)
        with self.assertRaisesRegex(ValueError, 'expires_at'):
            fp.add_exception(self.s, 'p/T-2', 'critical_incident', 'Падает прод у клиента', (NOW + timedelta(days=31)).isoformat(), NOW, 'owner')
        with self.assertRaisesRegex(ValueError, 'reason'):
            fp.add_exception(self.s, 'p/T-2', 'critical_incident', 'срочно', (NOW + timedelta(hours=2)).isoformat(), NOW, 'owner')
        e = fp.add_exception(self.s, 'p/T-2', 'critical_incident', 'Падает прод у клиента', (NOW + timedelta(hours=2)).isoformat(), NOW, 'owner')
        self.admit('p/T-2')
        admit = [d for d in self.s['delivery_sprints']['decisions'] if d['decision'] == 'admit' and d['ref'] == 'p/T-2'][0]
        self.assertEqual(admit['exception'], e['id'])
        self.assertIn('p/T-2', fp.open_refs(self.s, self.tasks))                  # слот занят: исключение не бесплатно
        fp.add_exception(self.s, 'p/T-3', 'owner_override', 'Нужно до демо в пятницу', (NOW + timedelta(hours=1)).isoformat(), NOW, 'owner')
        later = NOW + timedelta(hours=3)
        self.assertIsNotNone(sp.reason(self.s, 'p/T-3', self.tasks, at=later))    # срок вышел — лимит снова действует


class BatchShadow(unittest.TestCase):
    """Режим по умолчанию — batch; rolling считается рядом и только пишет отличия (AC-14.8, TC-35)."""

    def test_default_is_batch_and_shadow_logs_difference(self):
        s = {'runs': []}
        tasks = [dict(ref='p/T-%d' % i, status='queued') for i in range(4)]
        self.assertEqual(fp.mode(s), 'batch')
        for t in tasks:
            sp.admit(s, t['ref'], tasks, at=NOW)                                  # batch: 4 задачи одного проекта можно
        self.assertEqual(s['delivery_sprints']['currents']['p']['refs'], [t['ref'] for t in tasks])
        dec = [d for d in s['delivery_sprints']['decisions'] if d['decision'] == 'admit']
        self.assertEqual([d['shadow_differs'] for d in dec], [False, False, True, True])
        self.assertIn('в проекте p 2 задач из 2', dec[2]['shadow_verdict'])
        self.assertTrue(all(d['mode'] == 'batch' and d['shadow'] == 'rolling' for d in dec))

    def test_batch_unchanged_by_flow_code(self):
        s = {'runs': []}
        tasks = [dict(ref='p/T-%d' % i, status='queued') for i in range(6)]
        for t in tasks[:4]:
            sp.admit(s, t['ref'], tasks)
        with self.assertRaisesRegex(ValueError, 'Спринт SPR-1'):
            sp.admit(s, 'p/T-4', tasks)

    def test_owner_release_unsticks_batch_sprint(self):
        """Отменённая задача держала спринт вечно: теперь владелец закрывает её слот явно."""
        s = {'runs': []}
        tasks = [dict(ref='p/T-%d' % i, status='queued') for i in range(5)]
        for t in tasks[:2]:
            sp.admit(s, t['ref'], tasks)
        tasks[0]['pipeline'] = dict(stage='done', stages={'deploy_main': {'passed': True}})
        tasks[1]['status'] = 'cancelled'
        self.assertIsNotNone(sp.reason(s, 'p/T-2', tasks))
        fp.release_slot(s, 'p/T-1', 'Задача отменена, в main не пойдёт', tasks, NOW, 'owner')
        sp.admit(s, 'p/T-2', tasks)
        self.assertEqual(s['delivery_sprints']['currents']['p']['refs'], ['p/T-2'])
        self.assertEqual(s['delivery_sprints']['history'][-1]['refs'], ['p/T-0', 'p/T-1'])


class ProjectScopedSnapshot(unittest.TestCase):
    def test_other_project_legacy_not_in_project_slice(self):
        """Срез проекта: чужие начатые задачи не видны и приём не запрещают (решение владельца 2026-11-08)."""
        s = {'runs': []}
        tasks = [dict(ref='ege/EGE-%d' % i, status='in_progress', title='e') for i in range(6)] +                 [dict(ref='tasktreker/TT-1', status='queued', title='t', priority='🔴')]
        sp.sync(s, tasks)
        for t in tasks[:6]:
            sp.admit(s, t['ref'], tasks)
        snap = fp.snapshot(s, tasks, project='tasktreker')
        self.assertEqual(snap['active_wip'], 0)
        self.assertTrue(snap['allowed']['batch'])
        self.assertEqual(snap['next_admissible'], 'tasktreker/TT-1')
        self.assertIn('ege', snap['batch_by_project'])
        self.assertFalse(fp.snapshot(s, tasks, project='ege')['allowed']['batch'])


class OwnerPolicy(DataCase):
    """Режим, лимиты, закрытие слота и исключения меняет только владелец (FR-014 п.8)."""

    def setUp(self):
        super().setUp(); self.projects()

    def test_agent_can_only_look(self):
        for req in (dict(op='settings', changes=dict(flow_mode='rolling')),
                    dict(op='release_slot', ref='alpha-api/AL-01', reason='не нужна больше'),
                    dict(op='flow_exception', ref='alpha-api/AL-01', kind='owner_override', reason='очень надо сейчас',
                         expires_at=(w.now() + timedelta(hours=1)).isoformat())):
            with self.assertRaisesRegex(ValueError, 'владельца'):
                w.action(req)
            self.assertTrue(mcp_server.call_tool('tt_work', req)['isError'])
        r = mcp_server.call_tool('tt_work', dict(op='settings', changes=dict(flow_mode='rolling', wip_global=3), preview=True))
        self.assertFalse(r['isError'], r)
        self.assertEqual(w.settings()['flow_mode'], 'batch')

    def test_owner_switches_mode_with_validation(self):
        for bad in (dict(flow_mode='fast'), dict(wip_global=0), dict(wip_per_project=11), dict(prefer_finish='yes')):
            with self.assertRaises(ValueError):
                w.action(dict(op='settings', changes=bad), origin='owner')
        out = w.action(dict(op='settings', changes=dict(flow_mode='rolling', wip_global=3)), origin='owner')
        self.assertTrue(out['policy_version'])
        self.assertEqual((w.settings()['flow_mode'], w.settings()['wip_global']), ('rolling', 3))
        snap = w.action(dict(op='flow'))
        self.assertEqual(snap['mode'], 'rolling'); self.assertEqual(snap['policy']['wip_global'], 3)

    def test_flow_snapshot_is_read_only(self):
        w.reserve_sprint('alpha-api/AL-01')
        before = w.read('workflow.json', {})
        snap = mcp_server.call_tool('tt_work', dict(op='flow'))
        self.assertFalse(snap['isError'], snap)
        self.assertEqual(w.read('workflow.json', {}), before)
        s = w.action(dict(op='flow'))
        self.assertEqual(s['active_wip'], 1)
        self.assertEqual(s['counts']['in_work'], 1)
        self.assertEqual(s['mode'], 'batch'); self.assertEqual(s['shadow'], 'rolling')
        self.assertEqual(w.snapshot()['flow']['active_wip'], 1)                      # доска видит тот же снимок
