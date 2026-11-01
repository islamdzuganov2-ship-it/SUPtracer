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
        DataCase.setUp(self); self.projects()   # общий с RelayTests
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        self.home = Path(self.tmp)/'home'
        self.env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home/'claude'), 'CODEX_HOME': str(self.home/'codex')})
        self.env.start()
        w.action(dict(op='plan', project='alpha-api', tasks=[dict(
            id='AL-03', title='Правка', why='Нужна', root='request', size='S', priority='🟡', risk='low',
            work_type='implementation', acceptance=['Тест проходит'], depends_on=[])]))
        self.ref = 'alpha-api/AL-03'
        # Пауза и возобновление проверяются без передачи другому ИИ; передача — в своих тестах ниже.
        w.action(dict(op='settings', changes=dict(failover=False)))

    def tearDown(self):
        self.env.stop(); DataCase.tearDown(self)

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
        self.assertEqual(serve.limits_once(), dict(paused=['RUN-1'], resumed=[], handed=[]))
        run = w.state()['runs'][0]
        self.assertEqual(run['paused']['by'], 'server')
        self.assertEqual(w.resume_due(), [])
        # Срок паузы вышел — та же сессия продолжается.
        later = datetime.fromisoformat(run['paused']['resume_at']) + timedelta(minutes=1)
        with mock.patch.object(w, 'now', return_value=later), \
             mock.patch.object(launcher, 'launch', return_value=dict(pid=9, exe='claude.exe', session='sess-1', resumed=True)) as launch, \
             mock.patch.object(limits, 'detect', return_value=None):
            self.assertEqual(serve.limits_once(), dict(paused=[], resumed=['RUN-1'], handed=[]))
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

    # --- передача другому ИИ по лимиту (TT-79)

    def test_failover_hands_task_to_free_ai_with_its_route(self):
        w.action(dict(op='settings', changes=dict(failover=True)))
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        with mock.patch.object(w, 'cli_ok', return_value=True):
            r = w.action(dict(op='pause', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1',
                              checkpoint='Сделан парсер, осталось подключить кеш и тест кеша'))
        moved = r['handed_to']
        self.assertEqual(moved['agent'], 'codex')
        runs = {x['id']: x for x in w.state()['runs']}
        self.assertEqual(runs['RUN-1']['status'], 'cancelled')
        new = runs[moved['run']]
        self.assertEqual((new['agent'], new['status'], new['auto_launch']), ('codex', 'queued', True))
        self.assertEqual(new['packet']['route']['provider'], 'codex')
        self.assertIn('Сделан парсер', new['failover']['handover'])
        self.assertTrue(w.limited(None, 'claude'))
        t = w.task_for(self.ref)
        self.assertIn('Передано', t['status_note'])
        prompt = w.launch_prompt(t, new, new['packet']['route'], {})
        self.assertIn('Передача от Claude', prompt)
        # Задача была назначена Claude, но принявший передачу Codex её берёт.
        d = w.read('projects.json', {}); d['projects']['alpha-api']['backlog'][-1]['executor'] = 'claude'; w.save('projects.json', d)
        w.action(dict(op='claim', ref=self.ref, run=new['id'], worker='codex-cli:' + new['id'], model=new['packet']['route']['model']))
        self.assertEqual(w.task_for(self.ref)['claimed_by']['agent'], 'codex')

    def test_no_free_ai_means_waiting_for_reset(self):
        w.action(dict(op='settings', changes=dict(failover=True)))
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        s = w.state(); w.note_limit(s, 'codex', datetime.now(timezone.utc) + timedelta(hours=2), datetime.now(timezone.utc)); w.save('workflow.json', s)
        r = w.action(dict(op='pause', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1',
                          checkpoint='Сделан парсер, осталось подключить кеш и тест кеша'))
        self.assertIn('paused', r)
        self.assertEqual(w.state()['runs'][0]['status'], 'running')

    def test_server_detected_limit_hands_over(self):
        w.action(dict(op='settings', changes=dict(failover=True)))
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        self.claude_log('sess-1', [self.limit_row(datetime.now(timezone.utc), resets=int(datetime.now(timezone.utc).timestamp()) + 3600)])
        with mock.patch.object(w, 'cli_ok', return_value=True):
            out = serve.limits_once()
        self.assertEqual(out['paused'], ['RUN-1'])
        self.assertEqual(out['handed'][0]['agent'], 'codex')
        # Ждущий запуск ИИ на лимите автозапуск не открывает, а переводит.
        self.assertTrue(w.limited(None, 'claude'))

    def test_unlaunched_run_switches_ai_in_place(self):
        # Неоткрытый запуск ИИ на лимите меняет ИИ сам, без новых запусков (раньше — сотни передач).
        w.action(dict(op='settings', changes=dict(failover=True)))
        w.action(dict(op='enqueue', ref=self.ref, agent='claude'))
        s = w.state(); w.note_limit(s, 'claude', datetime.now(timezone.utc) + timedelta(hours=1), datetime.now(timezone.utc)); w.save('workflow.json', s)
        with mock.patch.object(w, 'cli_ok', return_value=True):
            self.assertEqual(w.reroute_queued(self.ref, 'RUN-1'), 'codex')
            self.assertEqual(w.reroute_queued(self.ref, 'RUN-1'), 'codex')        # повтор ничего не плодит
        runs = w.state()['runs']
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]['agent'], runs[0]['status'], runs[0]['packet']['route']['provider']), ('codex', 'queued', 'codex'))
        self.assertEqual(runs[0]['repins'][-1]['by'], 'лимит токенов')
        self.assertNotIn('Передача', w.launch_prompt(w.task_for(self.ref), runs[0], runs[0]['packet']['route'], {}))
        # Назначение задачи прежнему ИИ не мешает принять запуск.
        d = w.read('projects.json', {}); d['projects']['alpha-api']['backlog'][-1]['executor'] = 'claude'; w.save('projects.json', d)
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='codex-1', model=runs[0]['packet']['route']['model']))

    def test_watch_keeps_session_that_still_writes(self):
        w.action(dict(op='settings', changes=dict(autonomy='full', consent=True)))
        self.launched_run()
        path = self.claude_log('sess-1', [dict(type='user', timestamp=iso(datetime.now(timezone.utc)))])
        s = w.state(); s['runs'][0]['launch']['pid'] = 999999; s['runs'][0]['launch']['at'] = s['runs'][0]['launched_at'] = '2026-01-01T00:00:00+00:00'; w.save('workflow.json', s)
        with mock.patch.object(launcher, 'alive', return_value=False), mock.patch.object(serve, 'open_session') as op:
            self.assertEqual(serve.watch_once(), dict(nudged=[], lost=[], relayed=[]))   # журнал свежий — сессия жива
        op.assert_not_called()


class RelayTests(DataCase):
    """Эстафета (TT-91): контекст сессии роли перерос потолок — свежая сессия с точки остановки."""
    claude_log = LimitTests.claude_log
    launched_run = LimitTests.launched_run
    tearDown = LimitTests.tearDown

    def setUp(self):
        LimitTests.setUp(self)
        serve._auto_attempts.clear()            # отметки «уже пробовали» живут в модуле между тестами

    def usage_row(self, ctx, text='Сделан раздел FR, дальше API'):
        return dict(type='assistant', requestId='r%d' % ctx, timestamp=iso(datetime.now(timezone.utc)),
                    message=dict(model='claude-opus-5-5', content=[dict(type='text', text=text)],
                                 usage=dict(input_tokens=1, cache_creation_input_tokens=100, cache_read_input_tokens=ctx, output_tokens=5)))

    def test_agent_relay_opens_fresh_session_with_checkpoint(self):
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        r = w.action(dict(op='pause', reason='context', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1',
                          checkpoint='SRS: разделы 1–4 готовы, осталось API; читать docs/tasks/AL-03/BRD.md раздел 3'))
        self.assertTrue(r['relay']['relay']); self.assertIsNone(r['relay']['session'])
        self.assertEqual([x['id'] for x in w.resume_due()], ['RUN-1'])                  # сразу, без ожидания
        with mock.patch.object(launcher, 'launch', return_value=dict(pid=9, exe='claude.exe', session='sess-2')) as launch,              mock.patch.object(limits, 'detect', return_value=None):
            self.assertEqual(serve.limits_once()['resumed'], ['RUN-1'])
        self.assertIsNone(launch.call_args.kwargs['resume'])                            # новая сессия, не --resume
        prompt = launch.call_args.args[3]
        self.assertIn('Эстафета', prompt); self.assertIn('BRD.md раздел 3', prompt); self.assertIn('claim не нужен', prompt)
        run = w.state()['runs'][0]
        self.assertTrue(run['pauses'][-1]['relay']); self.assertEqual(w.relays(run), 1)
        self.assertIn('Эстафета', w.task_for(self.ref)['status_note'])

    def test_relays_are_limited_per_run(self):
        self.launched_run()
        w.action(dict(op='claim', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1', model='claude-haiku-5-5'))
        s = w.state(); s['runs'][0]['pauses'] = [dict(relay=True)] * w.MAX_RELAYS; w.save('workflow.json', s)
        with self.assertRaisesRegex(ValueError, 'Эстафет'):
            w.action(dict(op='pause', reason='context', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1',
                          checkpoint='Сделано много, осталось немного, следующий шаг — тест'))
        with self.assertRaisesRegex(ValueError, 'reason'):
            w.action(dict(op='pause', reason='устал', ref=self.ref, run='RUN-1', worker='claude-cli:RUN-1',
                          checkpoint='Сделано много, осталось немного, следующий шаг — тест'))

    def test_server_relays_ended_session_above_ceiling(self):
        w.action(dict(op='settings', changes=dict(autonomy='full', consent=True)))
        self.launched_run()
        path = self.claude_log('sess-1', [self.usage_row(20000), self.usage_row(120000, 'Готовы FR-1…FR-9, осталось API')])
        os.utime(path, (1, 1))                                                          # журнал давно не пишется
        s = w.state(); r0 = s['runs'][0]
        r0.update(role='system_analyst', stage='analysis'); r0['launch'].update(pid=999999, at='2026-01-01T00:00:00+00:00')
        r0['launched_at'] = '2026-01-01T00:00:00+00:00'; w.save('workflow.json', s)
        with mock.patch.object(launcher, 'alive', return_value=False), mock.patch.object(serve, 'open_session') as op:
            self.assertEqual(serve.watch_once()['relayed'], ['RUN-1'])
        op.assert_not_called()
        pz = w.state()['runs'][0]['paused']
        self.assertEqual((pz['by'], pz['relay']), ('server', True)); self.assertIn('осталось API', pz['checkpoint'])
        w.action(dict(op='settings', changes=dict(context_ceiling=0)))                  # выключено — эстафет нет
        self.assertFalse(serve.relay_due(dict(w.state()['runs'][0], paused=None)))

    def test_ceiling_setting_is_validated(self):
        w.action(dict(op='settings', changes=dict(context_ceiling=120000)))
        self.assertEqual(w.settings()['context_ceiling'], 120000)
        with self.assertRaises(ValueError):
            w.action(dict(op='settings', changes=dict(context_ceiling=1000)))

    def test_hook_reminds_once_per_level(self):
        import read_guard
        path = self.claude_log('sess-h', [self.usage_row(95000)])
        env = {'TASKTREKER_CEILING': '90000', 'TASKTREKER_READS': str(Path(self.tmp) / 'reads'),
               'TASKTREKER_RELAY': 'tt_work op=pause reason=context ref=a run=b worker=c checkpoint="…"'}
        hit = lambda: read_guard.decide(dict(hook_event_name='PostToolUse', session_id='H', transcript_path=str(path)))
        with mock.patch.dict(os.environ, env):
            note = hit()['hookSpecificOutput']
            self.assertEqual(note['hookEventName'], 'PostToolUse')
            self.assertIn('95 тыс', note['additionalContext']); self.assertIn('reason=context ref=a', note['additionalContext'])
            self.assertIsNone(hit())                                                    # второй раз — молчит
            self.claude_log('sess-h', [self.usage_row(130000)])
            self.assertIsNotNone(hit())                                                 # 130% — напоминает снова
        self.assertIsNone(hit())                                                        # без потолка — молчит

    def test_hook_warns_about_run_budget_inside_session(self):
        import read_guard
        path = self.claude_log('sess-b', [self.usage_row(40000), self.usage_row(40000), self.usage_row(50000)])
        env = {'TASKTREKER_BUDGET': '100000', 'TASKTREKER_READS': str(Path(self.tmp) / 'reads')}
        hit = lambda: read_guard.decide(dict(hook_event_name='PostToolUse', session_id='B', transcript_path=str(path)))
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(hit())                                  # r40000 повтором не считается: 90 тыс. < 100 тыс.
            self.claude_log('sess-b', [self.usage_row(20000)])
            self.assertIn('ориентира роли', hit()['hookSpecificOutput']['additionalContext'])
            self.assertIsNone(hit())
            self.claude_log('sess-b', [self.usage_row(60000)])
            self.assertIn('op=escalate', hit()['hookSpecificOutput']['additionalContext'])   # 150%
            self.assertEqual(read_guard.load('B')['b_total'], 170424)   # 4 разных ответа: контекст + 106

    def test_profile_sums_sessions_of_a_run(self):
        import tokens
        self.launched_run()
        self.claude_log('sess-1', [self.usage_row(15000), self.usage_row(95000)])
        self.claude_log('sess-2', [self.usage_row(16000)])
        s = w.state(); r0 = s['runs'][0]
        r0['launches'] = [dict(agent='claude', session='sess-1', at=r0['launched_at']), dict(agent='claude', session='sess-2')]
        r0['launch']['session'] = 'sess-2'; w.save('workflow.json', s)
        p = tokens.profile(w.state()['runs'][0])
        self.assertEqual((p['sessions'], p['turns'], p['start'], p['peak']), (2, 3, 15101, 95101))

