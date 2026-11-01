"""Факт по задачам для дашборда (TT-95): расчёт из записанных данных, без ИИ."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from base import DataCase
import taskview

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
iso = lambda **kw: (NOW - timedelta(**kw)).isoformat()


def task(ref, status='queued', stage=None, size='M', **extra):
    t = dict(ref=ref, project=ref.split('/')[0], title='Задача ' + ref, status=status, size=size, **extra)
    if stage:
        t.setdefault('pipeline', dict(stage=stage, roles={}, history=[], stages={}))
    return t


class TaskViewTests(DataCase):
    def build(self, tasks, runs, **kw):
        snap = dict(tasks=tasks, projects={'p': dict(name='Проект П'), 'q': dict(name='Проект Q')})
        with mock.patch.object(taskview.w, 'snapshot', return_value=snap), \
                mock.patch.object(taskview.w, 'state', return_value=dict(runs=runs)):
            return taskview.build(now=NOW, with_tokens=False, **kw)

    def sample(self):
        dev = task('p/P-2', stage='development', size='L')
        dev['pipeline']['stages'] = {
            'analysis': dict(started=iso(hours=30), passed_at=iso(hours=10)),
            'development': dict(started=iso(hours=10))}
        dev['pipeline']['roles'] = {'development/developer': dict(state='done')}
        dev['pipeline']['history'] = [dict(kind='stage_passed', stage='analysis', at=iso(hours=10)),
                                      dict(kind='return', stage='review', to_stage='development', at=iso(hours=2)),
                                      dict(kind='to_idea', at=iso(hours=40))]
        tasks = [task('p/P-1', status='done', size='S', completed_at=iso(hours=3)), dev,
                 task('p/P-3', status='blocked', stage='analysis', size='XL'), task('q/Q-1', stage='idea'),
                 task('q/Q-2'), task('q/Q-3', status='cancelled')]
        runs = [dict(id='R1', ref='p/P-2', role='system_analyst', stage='analysis', status='done',
                     launched_at=iso(hours=28), finished_at=iso(hours=24)),
                dict(id='R2', ref='p/P-2', role='developer', stage='development', status='running', launched_at=iso(hours=1)),
                dict(id='R3', ref='q/Q-1', role='product_owner', stage='idea', status='queued')]
        return tasks, runs

    def test_kpi_columns_and_points(self):
        d = self.build(*self.sample())
        k = d['kpi']
        self.assertEqual((k['total'], k['done'], k['blocked'], k['cancelled'], k['pipeline']), (5, 1, 1, 1, 3))
        self.assertEqual((k['points_total'], k['points_done']), (1 + 8 + 20 + 3 + 3, 1))
        self.assertEqual((k['working'], k['waiting']), (1, 1))      # R2 открыт сессией, R3 ждёт открытия
        p = next(r for r in d['matrix'] if r['key'] == 'p')
        self.assertEqual((p['counts']['done'], p['counts']['development'], p['counts']['analysis']), (1, 1, 1))
        self.assertEqual(p['blocked']['analysis'], 1)
        self.assertEqual((p['points']['done'], p['points']['build'], p['points']['blocked']), (1, 8, 20))
        q = next(r for r in d['matrix'] if r['key'] == 'q')
        self.assertEqual((q['counts']['idea'], q['counts']['backlog']), (1, 1))

    def test_flow_and_stage_time(self):
        d = self.build(*self.sample())
        self.assertEqual(sum(d['flow']['stage_passed']), 1)
        self.assertEqual(sum(d['flow']['return']), 1)
        self.assertEqual(sum(d['flow']['completed']), 1)
        a = next(s for s in d['stage_time'] if s['stage'] == 'analysis')
        self.assertEqual((a['passed'], a['work_h'], a['wait_h']), (1, 4.0, 16.0))   # 20 ч на этапе, 4 ч сессии
        dv = next(s for s in d['stage_time'] if s['stage'] == 'development')
        self.assertEqual((dv['open'], dv['open_age_h']), (1, 10.0))

    def test_window_runs_events_and_progress(self):
        d = self.build(*self.sample(), hours=5)
        win = d['window']
        self.assertEqual([t['ref'] for t in win['tasks']], ['p/P-2', 'p/P-1'])
        dev = win['tasks'][0]
        self.assertEqual([r['id'] for r in dev['runs']], ['R2'])     # R1 закончился до окна
        self.assertTrue(dev['runs'][0]['live'])
        self.assertEqual([e['kind'] for e in dev['events']], ['return'])
        self.assertEqual(dev['progress'], round(100 * (2 + 1) / 7))  # два этапа пройдено, единственная роль сдана
        self.assertEqual(win['tasks'][1]['events'][0]['kind'], 'completed')
        s = win['summary']
        self.assertEqual((s['tasks'], s['runs'], s['returns'], s['completed']), (2, 1, 1, 1))

    def test_project_filter(self):
        d = self.build(*self.sample(), project='q')
        self.assertEqual([r['key'] for r in d['matrix']], ['q'])
        self.assertEqual(d['kpi']['total'], 2)


if __name__ == '__main__':
    unittest.main()
