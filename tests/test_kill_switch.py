"""Аварийная остановка (TT-104; ТЗ TT-U-001: TT3-016, FR-003).

Stop запрещает новые действия (take, claim, enqueue, запуск окон, автозапуск, продолжение пауз,
слияния), сохраняет работу (точка остановки, WIP-коммит), завершает процессы сессий после записи
состояния. Включить может кто угодно — это только сужение прав; снять — только владелец.
"""
import subprocess
from pathlib import Path
from unittest import mock

from base import DataCase
import launcher
import mcp_server
import serve
import workflow as w


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True, encoding='utf-8', check=True).stdout.strip()


class KillSwitchTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data) / 'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        w.action(dict(op='plan', project='alpha-api', tasks=[dict(
            id=i, title='Правка ' + i, why='Нужна', root='request', size='S', priority='🟡', risk='low',
            work_type='implementation', acceptance=['Тест проходит'], depends_on=[]) for i in ('AL-03', 'AL-04')]))
        # Выполняемый запуск с рабочей копией и незакоммиченной правкой.
        self.wt = Path(self.tmp) / 'wt'
        self.wt.mkdir()
        git('init', '-q', '-b', 'ai/claude/al-03', cwd=self.wt)
        (self.wt / 'a.txt').write_text('1\n', encoding='utf-8')
        git('add', '.', cwd=self.wt); git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init', cwd=self.wt)
        (self.wt / 'a.txt').write_text('2 — незакоммиченная работа агента\n', encoding='utf-8')
        model = w.action(dict(op='packet', ref='alpha-api/AL-03', agent='claude'))['route']['model']
        r = w.action(dict(op='take', ref='alpha-api/AL-03', model=model, worker='w1', agent='claude'))
        self.run_id = r.get('run') or next(x['id'] for x in w.state()['runs'] if x['ref'] == 'alpha-api/AL-03')
        s = w.state()
        run = next(x for x in s['runs'] if x['id'] == self.run_id)
        run['launch'] = dict(pid=424242, agent='claude', session='sess-1',
                             worktree=dict(path=str(self.wt), branch='ai/claude/al-03'))
        run['launched_at'] = w.now().isoformat()
        w.save('workflow.json', s)

    def stop(self, **kw):
        with mock.patch.object(launcher, 'terminate', return_value=True) as term:
            r = w.action(dict(dict(op='stop_all', reason='подозрительное поведение агента'), **kw))
        return r, term

    def get_run(self, rid=None):
        return next(x for x in w.state()['runs'] if x['id'] == (rid or self.run_id))

    def test_stop_saves_work_then_terminates_and_blocks_new_actions(self):
        model = self.get_run()['model']
        r, term = self.stop()
        term.assert_called_once_with(424242)                                         # процесс — после записи
        stopped = r['stopped'][0]
        self.assertEqual((stopped['run'], stopped['terminated']), (self.run_id, True))
        p = self.get_run()['paused']
        self.assertEqual(p['reason'], 'kill_switch')
        self.assertIn('подозрительное', p['checkpoint'])
        self.assertTrue(p['wip'])                                                     # правка сохранена коммитом
        self.assertEqual(git('status', '--porcelain', cwd=self.wt), '')
        self.assertIn('незакоммиченная', git('show', 'HEAD:a.txt', cwd=self.wt))
        self.assertEqual(self.get_run()['status'], 'running')                             # метка не потеряна
        for req in (dict(op='take', ref='alpha-api/AL-04', model=model, worker='w2', agent='claude'),
                    dict(op='enqueue', ref='alpha-api/AL-04'),
                    dict(op='heartbeat', ref='alpha-api/AL-03', run=self.run_id, worker='w1'),
                    dict(op='idea', project='alpha-api', title='Новая идея', text='Что-то новое для проекта', source='claude')):
            with self.assertRaisesRegex(ValueError, 'Аварийная остановка', msg=req['op']):
                w.action(req)
        # Чтение и сужение по-прежнему работают.
        self.assertTrue(w.action(dict(op='list'))['tasks'])
        w.action(dict(op='status', ref='alpha-api/AL-04', status='blocked', reason='ждёт снятия остановки'))
        self.assertEqual(w.resume_due(), [])                                          # паузы не продолжаются
        self.assertIn('kill_switch', [n['kind'] for n in w.notifications(w.state(), w.snapshot()['tasks'])])

    def test_server_opens_nothing_while_stopped(self):
        self.stop()
        with mock.patch.object(serve, 'open_session') as opened:
            self.assertEqual(serve.dispatch_once(), [])
            with self.assertRaisesRegex(ValueError, 'Аварийная остановка'):
                serve.launch_session(dict(ref='alpha-api/AL-04', agent='claude'))
        opened.assert_not_called()
        import orchestra
        self.assertEqual(orchestra.tick(), [])

    def test_only_owner_releases_and_sessions_continue(self):
        self.stop()
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(dict(op='resume_all'))
        r = mcp_server.call_tool('tt_work', dict(op='resume_all'))
        self.assertTrue(r['isError'])
        with self.assertRaisesRegex(ValueError, 'Аварийная остановка с'):
            w.action(dict(op='stop_all', reason='второй раз подряд'))
        out = w.action(dict(op='resume_all'), origin='owner')
        self.assertEqual(out['resumed'], [self.run_id])
        self.assertEqual([x['id'] for x in w.resume_due()], [self.run_id])           # та же сессия продолжится
        self.assertEqual(self.get_run()['paused']['session'], 'sess-1')
        w.action(dict(op='enqueue', ref='alpha-api/AL-04'))                           # новые действия снова можно
        log = w.state()['policy']['log']
        self.assertEqual([(e['what'], e['origin']) for e in log[-2:]], [('kill_switch', 'agent'), ('kill_switch', 'owner')])

    def test_any_origin_may_stop_but_needs_reason(self):
        with self.assertRaisesRegex(ValueError, 'reason'):
            w.action(dict(op='stop_all'))
        with mock.patch.object(launcher, 'terminate', return_value=True):             # чужой pid не трогаем
            r = mcp_server.call_tool('tt_work', dict(op='stop_all', reason='агент видит опасность'))
        self.assertFalse(r['isError'], r)
        self.assertEqual(w.state()['policy']['kill_switch']['by'], 'agent')
