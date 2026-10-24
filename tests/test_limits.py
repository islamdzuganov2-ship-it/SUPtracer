"""Пауза по исчерпанию лимита токенов и возобновление сессии с места остановки (TT-72)."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from base import DataCase
import launcher
import limits
import serve
import workflow as w

RESET = 1792544295   # 2026-10-21T00:58:15Z


def iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


class LimitTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        self.home = Path(self.tmp)/'home'
        self.env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home/'claude'), 'CODEX_HOME': str(self.home/'codex')})
        self.env.start()
        w.action(dict(op='plan', project='alpha-api', tasks=[dict(
            id='AL-03', title='Правка', why='Нужна', root='request', size='S', priority='🟡', risk='low',
            work_type='implementation', acceptance=['Тест проходит'], depends_on=[])]))
        self.ref = 'alpha-api/AL-03'

    def tearDown(self):
        self.env.stop(); super().tearDown()

    def claude_log(self, sid, rows):
        path = self.home/'claude'/'projects'/'C--work'/(sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            for r in rows: f.write(json.dumps(r) + '\n')
        return path

    def limit_row(self, at, resets=RESET):
        return dict(type='assistant', timestamp=iso(at), sessionId='s', error='rate_limit', isApiErrorMessage=True,
                    quotaLimits=dict(status='rejected', resetsAt=resets, rateLimitType='five_hour'),
                    message=dict(model='<synthetic>', content=[dict(type='text', text="You've hit your session limit · resets 1am")],
                                 usage=dict(input_tokens=0, output_tokens=0)))

    def launched_run(self, agent='claude', session='sess-1'):
        w.action(dict(op='enqueue', ref=self.ref, agent=agent))
        run = w.state()['runs'][0]
        w.mark_launched(self.ref, run['id'], dict(pid=1, cli=agent, cwd=str(self.home/'work'), agent=agent, session=session if agent == 'claude' else None))
        return next(r for r in w.state()['runs'] if r['id'] == run['id'])

    # --- обнаружение по журналам

    def test_claude_limit_found_in_session_log_and_cleared_by_later_work(self):
        run = self.launched_run()
        self.claude_log('sess-1', [self.limit_row(datetime.now(timezone.utc))])
        e = limits.detect(run)
        self.assertEqual((e['provider'], e['kind'], e['resets_at']), ('claude', 'five_hour', '2026-10-21T00:58:15+00:00'))
        # Сессия снова работает — лимита больше нет.
        self.claude_log('sess-1', [dict(type='assistant', timestamp=iso(datetime.now(timezone.utc)),
                                        message=dict(model='claude-opus-5-5', usage=dict(input_tokens=5, output_tokens=40)))])
        self.assertIsNone(limits.detect(run))

    def test_old_limit_before_launch_is_ignored(self):
        run = self.launched_run()
        self.claude_log('sess-1', [self.limit_row(datetime.now(timezone.utc) - timedelta(hours=3))])
        self.assertIsNone(limits.detect(run))

    def test_codex_limit_found_with_reset_from_rate_limits(self):
        run = self.launched_run('codex')
        day = self.home/'codex'/'sessions'/'2026'/'10'/'24'; day.mkdir(parents=True)
        now = datetime.now(timezone.utc)
        rows = [dict(type='session_meta', timestamp=iso(now), payload=dict(id='cx-1', cwd=str(self.home/'work'), timestamp=iso(now))),
                dict(type='event_msg', timestamp=iso(now), payload=dict(type='token_count', rate_limits=dict(
                    primary=dict(used_percent=100.0, window_minutes=300, resets_at=RESET),
                    secondary=dict(used_percent=40.0, window_minutes=10080, resets_at=RESET + 86400)))),
                dict(type='event_msg', timestamp=iso(now), payload=dict(type='task_complete', error=dict(
                    message='You’ve hit your usage limit. Try again at 10:56 PM.', codex_error_info='usage_limit_exceeded')))]
        (day/'rollout-2026-10-24T08-00-00-cx-1.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        e = limits.detect(run)
        self.assertEqual((e['provider'], e['session'], e['kind'], e['resets_at']), ('codex', 'cx-1', 'five_hour', '2026-10-21T00:58:15+00:00'))

    def test_resume_time_is_reset_or_five_hours(self):
        at = datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(limits.resume_at({}, at), at + timedelta(hours=5))
        self.assertEqual(limits.resume_at(dict(resets_at='2026-10-24T10:00:00+00:00'), at), datetime(2026, 10, 24, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(limits.resume_at(dict(resets_at='2026-10-24T07:00:00+00:00'), at), at + limits.MARGIN)

    # --- пауза агентом и сервером, возобновление

    def test_agent_pause_keeps_claim_and_lease_until_resume(self):
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        with self.assertRaises(ValueError):   # без точки остановки пауза не ставится
            w.action(dict(op='pause', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', checkpoint='мало'))
        r = w.action(dict(op='pause', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1',
                          checkpoint='Сделан парсер, осталось подключить кеш; следующий шаг — тест кеша'))
        run = w.state()['runs'][0]
        self.assertEqual((run['status'], run['paused']['by'], run['paused']['session']), ('running', 'agent', 'sess-1'))
        resume = datetime.fromisoformat(r['paused']['resume_at'])
        self.assertGreater(datetime.fromisoformat(run['lease_until']), resume)
        self.assertTrue(w.lease_alive(run, resume - timedelta(minutes=1)))
        t = w.task_for(self.ref)
        self.assertIn('Пауза', t['status_note']); self.assertEqual(t['claimed_by']['paused_until'], r['paused']['resume_at'])
        # Другой ИИ задачу не перехватит.
        with self.assertRaises(ValueError):
            w.action(dict(op='take', ref=self.ref, model='gpt-5.4-mini', worker='codex-2'))
        self.assertIn('с места остановки', w.resume_prompt(t, run))
        self.assertIn('Сделан парсер', w.resume_prompt(t, run))

    def test_server_pauses_on_log_and_resumes_same_session(self):
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        self.claude_log('sess-1', [self.limit_row(datetime.now(timezone.utc), resets=int(datetime.now(timezone.utc).timestamp()) + 3600)])
        self.assertEqual(serve.limits_once(), dict(paused=['RUN-1'], resumed=[]))
        run = w.state()['runs'][0]
        self.assertEqual(run['paused']['by'], 'server')
        self.assertEqual(w.resume_due(), [])
        # Срок паузы вышел — та же сессия продолжается.
        later = datetime.fromisoformat(run['paused']['resume_at']) + timedelta(minutes=1)
        with mock.patch.object(w, 'now', return_value=later), \
             mock.patch.object(launcher, 'launch', return_value=dict(pid=9, exe='claude.exe', session='sess-1', resumed=True)) as launch, \
             mock.patch.object(limits, 'detect', return_value=None):
            self.assertEqual(serve.limits_once(), dict(paused=[], resumed=['RUN-1']))
        self.assertEqual(launch.call_args.kwargs['resume'], 'sess-1')
        self.assertIn('claim не нужен', launch.call_args.args[3])
        run = w.state()['runs'][0]
        self.assertNotIn('paused', run)
        self.assertEqual((run['pauses'][-1]['resumed_by'], run['pauses'][-1]['resumed_session']), ('server', True))
        self.assertGreater(datetime.fromisoformat(run['lease_until']), later)

    def test_heartbeat_after_manual_continue_clears_pause(self):
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        w.mark_paused(self.ref, 'RUN-1', dict(resets_at=None))
        w.action(dict(op='heartbeat', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1'))
        run = w.state()['runs'][0]
        self.assertNotIn('paused', run); self.assertEqual(run['pauses'][-1]['resumed_by'], 'agent')

    def test_commands_open_and_resume_sessions(self):
        cmd = launcher.command('claude', 'claude', 'claude-opus-5-5', 'high', 'p', 'C:/w', edits=True, session='abc')
        self.assertEqual(cmd[1:3], ['--session-id', 'abc'])
        cmd = launcher.command('claude', 'claude', 'claude-opus-5-5', 'high', 'p', 'C:/w', session='abc', resume=True)
        self.assertEqual(cmd[1:3], ['--resume', 'abc'])
        cmd = launcher.command('codex', 'codex', 'gpt-5.5', 'high', 'p', 'C:/w', edits=True, session='cx', resume=True)
        self.assertEqual(cmd[1:3], ['resume', 'cx']); self.assertIn('workspace-write', cmd)
        self.assertEqual(cmd[-1], 'p')

    def test_limit_rule_in_prompts_and_skills(self):
        import skills
        import pipeline as P
        for role in P.ROLES:
            self.assertIn('op=pause', skills.render(role))
        t = w.task_for(self.ref)
        run = dict(id='RUN-9', phase='execute')
        self.assertIn('op=pause', w.launch_prompt(t, run, w.recommendation(t), {}))
