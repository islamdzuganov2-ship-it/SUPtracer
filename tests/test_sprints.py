"""Состав спринта общий для агентов внутри проекта, освобождается только после main."""
import copy
import json
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
from base import DataCase
import sprints as sp
import workflow as w


class SprintRules(unittest.TestCase):
    def setUp(self):
        self.s = {'runs': []}
        self.tasks = [dict(ref='p/T-%d' % i, status='queued') for i in range(6)]

    def merge(self, index):
        self.tasks[index]['pipeline'] = dict(stage='done', stages={'deploy_main': {'passed': True}})

    def test_four_tasks_not_four_sessions_and_no_refill(self):
        for t in self.tasks[:4]: sp.admit(self.s, t['ref'], self.tasks)
        sp.admit(self.s, 'p/T-0', self.tasks)  # следующая роль той же задачи
        with self.assertRaises(ValueError): sp.admit(self.s, 'p/T-4', self.tasks)
        self.merge(0)
        with self.assertRaises(ValueError): sp.admit(self.s, 'p/T-4', self.tasks)
        for i in range(1, 4): self.merge(i)
        sp.admit(self.s, 'p/T-4', self.tasks)
        self.assertEqual(self.s['delivery_sprints']['currents']['p']['refs'], ['p/T-4'])
        self.assertEqual(len(self.s['delivery_sprints']['history']), 1)

    def test_partial_sprint_seals_after_first_merge(self):
        for t in self.tasks[:2]: sp.admit(self.s, t['ref'], self.tasks)
        self.merge(0)
        self.assertIsNotNone(sp.reason(self.s, 'p/T-2', self.tasks))

    def test_done_cancel_pause_or_missing_task_do_not_release(self):
        for t in self.tasks[:4]: sp.admit(self.s, t['ref'], self.tasks)
        self.tasks[0]['status'] = 'done'
        self.tasks[1]['status'] = 'cancelled'
        self.tasks[2]['paused'] = True
        self.tasks.pop(3)
        restored = json.loads(json.dumps(self.s))
        self.assertIsNotNone(sp.reason(restored, 'p/T-4', self.tasks))
        self.assertEqual(len(sp.view(restored, self.tasks)['pending']), 4)

    def test_pipeline_done_requires_main_gate(self):
        t = self.tasks[0]
        for pipe in ({'stage': 'testing'}, {'stage': 'done'},
                     {'stage': 'done', 'stages': {'deploy_main': {'passed': False}}}):
            t['pipeline'] = pipe
            self.assertFalse(sp.in_main(self.s, t))
        self.merge(0)
        self.assertTrue(sp.in_main(self.s, t))

    def test_legacy_drain_allows_rework_but_no_new_admission(self):
        for t in self.tasks[:5]: t['status'] = 'in_progress'
        sp.sync(self.s, self.tasks)
        for t in self.tasks[:5]: sp.admit(self.s, t['ref'], self.tasks)
        self.assertIsNotNone(sp.reason(self.s, 'p/T-5', self.tasks))
        for i in range(5): self.merge(i)
        sp.admit(self.s, 'p/T-5', self.tasks)
        self.assertEqual(self.s['delivery_sprints']['currents']['p']['refs'], ['p/T-5'])

    def test_preview_does_not_mutate(self):
        before = copy.deepcopy(self.s)
        sp.view(self.s, self.tasks)
        self.assertEqual(self.s, before)

    def test_legacy_of_other_project_does_not_block(self):
        """39 начатых задач ege не должны запрещать новую задачу tasktreker."""
        tasks = [dict(ref='ege/EGE-%d' % i, status='in_progress') for i in range(5)] +                 [dict(ref='tasktreker/TT-1', status='in_progress'), dict(ref='tasktreker/TT-2', status='queued')]
        sp.sync(self.s, tasks)
        self.assertIsNone(sp.reason(self.s, 'other/X-1', tasks))
        why = sp.reason(self.s, 'tasktreker/TT-2', tasks)
        self.assertIn('tasktreker/TT-1', why)
        self.assertNotIn('ege/', why)
        tasks[5]['pipeline'] = dict(stage='done', stages={'deploy_main': {'passed': True}})
        sp.admit(self.s, 'tasktreker/TT-2', tasks)
        self.assertEqual(self.s['delivery_sprints']['currents']['tasktreker']['refs'], ['tasktreker/TT-2'])
        self.assertIsNotNone(sp.reason(self.s, 'ege/EGE-99', tasks))     # у ege свои начатые задачи

    def test_each_project_has_own_four(self):
        tasks = [dict(ref='a/T-%d' % i, status='queued') for i in range(5)] + [dict(ref='b/T-%d' % i, status='queued') for i in range(5)]
        for i in range(4):
            sp.admit(self.s, 'a/T-%d' % i, tasks); sp.admit(self.s, 'b/T-%d' % i, tasks)
        for p in 'ab':
            with self.assertRaisesRegex(ValueError, 'проекта %s' % p): sp.admit(self.s, '%s/T-4' % p, tasks)
        cur = self.s['delivery_sprints']['currents']
        self.assertNotEqual(cur['a']['id'], cur['b']['id'])
        v = sp.view(self.s, tasks, project='a')
        self.assertEqual(v['pending'], ['a/T-%d' % i for i in range(4)])
        self.assertEqual(sorted(sp.view(self.s, tasks)['projects']), ['a', 'b'])

    def test_old_portfolio_sprint_migrates_per_project(self):
        self.s['delivery_sprints'] = dict(sequence=3, current=dict(id='SPR-3', refs=['a/T-0', 'b/T-0'], opened_at='x'),
                                          history=[], legacy=[])
        sp.sync(self.s, self.tasks)
        led = self.s['delivery_sprints']
        self.assertIsNone(led['current'])
        self.assertEqual({p: c['refs'] for p, c in led['currents'].items()}, {'a': ['a/T-0'], 'b': ['b/T-0']})
        self.assertEqual(led['currents']['a']['id'], 'SPR-3')
        import flow_policy as fp
        self.assertEqual(sorted(fp.slots(self.s)), ['a/T-0', 'b/T-0'])   # слоты состава не теряются при раскладке

    def test_reopened_task_needs_new_receipt(self):
        self.s['main_receipts'] = {'p/T-0': {'commit': 'old'}}
        sp.admit(self.s, 'p/T-0', self.tasks)
        self.tasks[0]['status'] = 'done'
        self.assertFalse(sp.in_main(self.s, self.tasks[0]))


class SprintIntegration(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data) / 'memory').mkdir(exist_ok=True)
        self.tasks = [dict(ref='alpha-api/T-%d' % i, status='queued') for i in range(5)]

    def test_reservation_survives_process_restart_and_blocks_fifth(self):
        with patch.object(w, 'snapshot', return_value={'tasks': self.tasks}):
            for t in self.tasks[:4]: w.reserve_sprint(t['ref'])
            with self.assertRaises(ValueError): w.reserve_sprint(self.tasks[4]['ref'])
        self.assertEqual(len(w.state()['delivery_sprints']['currents']['alpha-api']['refs']), 4)

    def test_all_admission_paths_block_fifth_without_mutation(self):
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        w.action(dict(op='plan', project='alpha-api', tasks=[
            dict(id='AL-%d' % i, title='Правка', why='Нужна', root='request', size='S', risk='low', priority='🟡',
                 work_type='implementation', acceptance=['Проверка проходит']) for i in range(10, 15)]))
        for i in range(10, 14): w.action(dict(op='enqueue', ref='alpha-api/AL-%d' % i))
        before = w.state()
        ref = 'alpha-api/AL-14'
        for req in (dict(op='enqueue', ref=ref), dict(op='take', ref=ref, worker='fifth', model='gpt-6-luna'),
                    dict(op='status', ref=ref, status='in_progress')):
            with self.subTest(op=req['op']), self.assertRaises(ValueError): w.action(req)
            self.assertEqual(w.state(), before)
        self.assertEqual(w.action(dict(op='next')), [])

    def test_batch_preview_limits_four_without_writing(self):
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        w.action(dict(op='plan', project='alpha-api', tasks=[
            dict(id='AL-%d' % i, title='Правка', why='Нужна', root='request', size='S', risk='low', priority='🟡',
                 work_type='implementation', acceptance=['Проверка проходит']) for i in range(10, 16)]))
        before = w.state()
        out = w.action(dict(op='start', project='alpha-api', agents=['codex'], sessions=6))
        self.assertEqual(len(out['picks']), 4)
        self.assertEqual(w.state(), before)

    def test_dispatcher_holds_fifth_of_same_project_only(self):
        """Решение владельца 2026-11-08: спринт у каждого проекта свой — чужой полный состав не держит."""
        import orchestra
        s = {'runs': []}
        for t in self.tasks[:4]: sp.admit(s, t['ref'], self.tasks)
        other = dict(ref='beta-tool/BT-1', status='queued')
        tasks = {t['ref']: t for t in self.tasks + [other]}
        self.assertIn('Спринт', orchestra.hold(s, dict(ref=self.tasks[4]['ref']), tasks))
        self.assertIsNone(orchestra.hold(s, dict(ref=other['ref']), tasks))

    def test_sprint_command_persists_migration(self):
        self.tasks[0]['status'] = 'in_progress'
        with patch.object(w, 'snapshot', return_value={'tasks': self.tasks}):
            out = w.action({'op': 'sprint'})
        self.assertEqual(out['legacy'], [self.tasks[0]['ref']])
        self.assertEqual(w.state()['delivery_sprints']['legacy'], out['legacy'])

    def test_role_launch_is_blocked_before_opening_session(self):
        import serve
        with patch.object(w, 'snapshot', return_value={'tasks': self.tasks}):
            for t in self.tasks[:4]: w.reserve_sprint(t['ref'])
            with patch.object(serve, 'open_session') as opened, self.assertRaises(ValueError):
                serve.launch_role(self.tasks[4], {'agent': 'codex'}, {})
            opened.assert_not_called()

    def test_confirm_main_checks_actual_git_ancestry(self):
        repo = Path(self.tmp) / 'repos' / 'alpha-api'
        def git(*args):
            return subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=t@t', *args],
                                  cwd=repo, capture_output=True, check=True)
        git('init', '-q', '-b', 'main')
        (repo / 'a.txt').write_text('base')
        git('add', '.'); git('commit', '-qm', 'base'); git('checkout', '-qb', 'task')
        (repo / 'a.txt').write_text('change')
        git('commit', '-qam', 'task change')
        t = dict(ref='alpha-api/T-0', project='alpha-api', status='done')
        s = {'runs': [dict(ref=t['ref'], launch={'worktree': {'path': str(repo), 'branch': 'task'}})]}
        with self.assertRaises(ValueError): w.confirm_main(s, t)
        git('checkout', '-q', 'main'); git('merge', '--ff-only', 'task')
        receipt = w.confirm_main(s, t)
        self.assertEqual(receipt['commit'], receipt['main_commit'])
        self.assertTrue(sp.in_main(s, t))
