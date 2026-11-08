"""Режим согласия владельца: полная автономия одним согласием или пошаговое (TT-78)."""
import sys
import time
from pathlib import Path
from unittest import mock
from base import DataCase
import launcher
import mcp_server
import serve
import workflow as w


class AutonomyTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        w.action(dict(op='plan', project='alpha-api', tasks=[dict(
            id='AL-03', title='Правка', why='Нужна', root='request', size='S', priority='🟡', risk='low',
            work_type='implementation', acceptance=['Тест проходит'], depends_on=[])]))
        self.ref = 'alpha-api/AL-03'

    def full(self):
        w.action(dict(op='settings', changes=dict(autonomy='full', consent=True)), origin='owner')

    def test_consent_required_and_owner_only(self):
        self.assertEqual(w.settings()['autonomy'], 'step')
        with self.assertRaisesRegex(ValueError, 'согласием'):
            w.action(dict(op='settings', changes=dict(autonomy='full')), origin='owner')
        self.full()
        st = w.settings()
        self.assertEqual((st['autonomy'], st['autonomy_changed']['to']), ('full', 'full'))
        # ИИ через MCP решения владельца не принимает.
        for args in (dict(op='settings', changes=dict(autonomy='step')), dict(op='approve_release', ref=self.ref),
                     dict(op='to_idea', project='alpha-api', dry_run=False), dict(op='settings', changes=dict(max_sessions=12))):
            r = mcp_server.call_tool('tt_work', args)
            self.assertTrue(r['isError'], args); self.assertIn('владельца', r['content'][0]['text'])
        self.assertEqual(w.settings()['autonomy'], 'full')

    def test_rule_in_prompts_only_when_full(self):
        t = w.task_for(self.ref); run = dict(id='RUN-9', phase='execute')
        self.assertNotIn('ПОЛНАЯ АВТОНОМИЯ', w.launch_prompt(t, run, w.recommendation(t), {}))
        self.full()
        self.assertIn('ПОЛНАЯ АВТОНОМИЯ', w.launch_prompt(t, run, w.recommendation(t), {}))

    def test_headless_commands(self):
        cmd = launcher.headless_command('claude', 'claude', 'claude-opus-5-5', 'high', 'p', 'C:/w', session='s1')
        self.assertEqual(cmd[1:6], ['-p', '--session-id', 's1', '--permission-mode', 'auto'])
        cmd = launcher.headless_command('claude', 'claude', 'claude-opus-5-5', 'high', 'p', 'C:/w', session='s1', resume=True)
        self.assertEqual(cmd[1:4], ['-p', '--resume', 's1'])
        with mock.patch.object(launcher, 'git_common_dir', return_value='C:/repo/.git'):
            cmd = launcher.headless_command('codex', 'codex', 'gpt-5.5', 'high', 'p', 'C:/w')
            self.assertEqual(cmd[1], 'exec'); self.assertIn('--approve-for-me', cmd)
            self.assertEqual(cmd[cmd.index('--add-dir') + 1], 'C:/repo/.git')
            cmd = launcher.headless_command('codex', 'codex', 'gpt-5.5', 'high', 'p', 'C:/w', session='x', resume=True)
            self.assertEqual(cmd[1:4], ['exec', 'resume', 'x'])
            self.assertIn('sandbox_mode="workspace-write"', cmd)

    def test_background_process_logs_and_ends(self):
        log = Path(self.tmp)/'logs'/'RUN-1.log'
        pid = launcher.open_background([sys.executable, '-c', 'print("session works")'], self.tmp, log)
        for _ in range(50):
            if not launcher.alive(pid): break
            time.sleep(0.1)
        self.assertFalse(launcher.alive(pid))
        self.assertIn('session works', launcher.tail(log))

    def launched(self, pid=999999):
        w.action(dict(op='enqueue', ref=self.ref, agent='claude'))
        w.mark_launched(self.ref, 'RUN-1', dict(pid=pid, cli='claude', cwd=self.tmp, agent='claude', session='s1', headless=True))
        s = w.state(); s['runs'][0]['launch']['at'] = s['runs'][0]['launched_at'] = '2026-01-01T00:00:00+00:00'; w.save('workflow.json', s)

    def test_ended_session_is_nudged_then_lost(self):
        self.full(); self.launched()
        with mock.patch.object(launcher, 'alive', return_value=False), \
             mock.patch.object(serve, 'open_session', return_value=dict(pid=5, exe='claude.exe', session='s1', resumed=True, headless=True)) as op:
            for _ in range(serve.MAX_NUDGES):
                self.assertEqual(serve.watch_once()['nudged'], ['RUN-1'])
                s = w.state(); s['runs'][0]['last_nudge_at'] = '2026-01-01T00:00:00+00:00'; w.save('workflow.json', s)
            self.assertEqual(op.call_args.kwargs['resume'], 's1')
            self.assertIn('этап не сдан', op.call_args.args[2])
            self.assertEqual(serve.watch_once()['lost'], ['RUN-1'])
        run = w.state()['runs'][0]
        self.assertEqual((run['status'], run['nudges']), ('failed', 2))
        self.assertEqual(w.task_for(self.ref)['status'], 'queued')

    def test_step_mode_only_marks_closed_window(self):
        self.launched()
        with mock.patch.object(launcher, 'alive', return_value=False), mock.patch.object(serve, 'open_session') as op:
            self.assertEqual(serve.watch_once(), dict(nudged=[], lost=[], relayed=[]))
        op.assert_not_called()
        self.assertIn('закрыто', w.state()['runs'][0]['launch_error'])
        self.assertEqual(w.open_sessions(w.state()['runs']), 0)           # закрытое окно место не занимает

    def test_legacy_task_escalates_to_resolver(self):
        w.action(dict(op='enqueue', ref=self.ref, agent='claude'))
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='c1', model='claude-haiku-5-5'))
        r = w.action(dict(op='escalate', ref=self.ref, run='RUN-1', worker='c1', reason='Нет доступа к тестовой базе партнёра'))
        self.assertEqual(w.task_for(self.ref)['status'], 'blocked')
        rs = next(x for x in w.state()['runs'] if x.get('role') == 'resolver')
        self.assertEqual((rs['problem'], rs['agent']), (r['problem'], 'codex'))
        model = w.action(dict(op='packet', ref=self.ref, run=rs['id']))['route']['model']
        w.action(dict(op='claim', ref=self.ref, run=rs['id'], worker='x1', model=model))
        w.action(dict(op='resolve', cause='Причина видна в журнале запуска и паспорте задачи', cause_kind='hypothesis', probe='Перезапуск роли покажет, повторится ли сбой сессии', ref=self.ref, run=rs['id'], worker='x1', decision='continue',
                      options=[dict(title='Поднять локальную копию базы из фикстур'), dict(title='Замокать клиента базы в тестах')],
                      notes='Фикстуры уже есть в репозитории, это быстрее и надёжнее',
                      instructions='Подними локальную копию из tests/fixtures и прогоняй тесты на ней'))
        t = w.task_for(self.ref)
        self.assertEqual(t['status'], 'queued'); self.assertIn('fixtures', t['status_note'])

    def test_bell_items_and_single_system_notification(self):
        w.action(dict(op='enqueue', ref=self.ref, agent='claude'))
        w.mark_launch_error(self.ref, 'RUN-1', 'Командная строка не найдена')
        notes = w.snapshot()['notifications']
        self.assertEqual([(n['kind'], n['run']) for n in notes], [('launch', 'RUN-1')])
        with mock.patch.object(serve, 'toast', return_value=True) as t:
            self.assertEqual(serve.notify_once(), ['l:RUN-1'])
            self.assertEqual(serve.notify_once(), [])                     # повторно не шлётся
        self.assertEqual(t.call_count, 1)
        self.assertIn('сбой запуска', t.call_args.args[1])

    def test_mcp_errors_are_utf8_for_codex(self):
        # Codex читает stderr MCP-сервера как UTF-8: ошибка в cp1251 рвала соединение («Transport closed»).
        import io
        raw = io.BytesIO(); err = io.TextIOWrapper(raw, encoding='cp1251')
        with mock.patch.object(sys, 'stderr', err):
            mcp_server.serve(io.BytesIO(b''), io.BytesIO())
            sys.stderr.write('Ошибка: проверка\n'); sys.stderr.flush()
        self.assertEqual(raw.getvalue().decode('utf-8').strip(), 'Ошибка: проверка')
