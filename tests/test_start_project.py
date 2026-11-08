"""«▶ Запустить проект»: пакет готовых задач бэклога по объёму бэклога (TT-76)."""
from pathlib import Path
from unittest import mock
from base import DataCase
import serve
import workflow as w


class StartProjectTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        attempt_cache = mock.patch.object(serve, '_auto_attempts', {})
        attempt_cache.start(); self.addCleanup(attempt_cache.stop)
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        self.base = dict(title='Правка', why='Нужна', root='request', size='S', priority='🟡', risk='low',
                         work_type='implementation', acceptance=['Тест проходит'], depends_on=[])

    def plan(self, *tasks):
        return w.action(dict(op='plan', project='alpha-api', tasks=[dict(self.base, **t) for t in tasks]))

    def start(self, **kw):
        return w.action(dict(dict(op='start', project='alpha-api'), **kw), origin='owner')

    def test_order_sessions_and_reasons(self):
        self.plan(dict(id='AL-10', priority='🟢'), dict(id='AL-11', priority='🔴', size='M'),
                  dict(id='AL-12'), dict(id='AL-13', depends_on=['AL-12']), dict(id='AL-14', scope=['api']),
                  dict(id='AL-15', scope=['api/v1']))
        r = self.start(agents=['claude', 'codex'])
        # AL-01 (из фикстуры, без плана) и AL-13 (ждёт AL-12) не готовы; готово 5 задач на 7 очков → 1 сессия на ИИ.
        self.assertEqual((r['backlog']['ready'], r['backlog']['points'], r['per_ai'], r['auto_sessions']), (5, 7, 1, 2))
        self.assertEqual([p['ref'] for p in r['picks']], ['alpha-api/AL-11', 'alpha-api/AL-12'])   # 🔴, затем та, что ждут
        self.assertEqual([p['agent'] for p in r['picks']], ['claude', 'codex'])
        self.assertEqual(r['picks'][1]['waiting'], 1)
        why = {x['ref']: x['why'] for x in r['skipped']}
        self.assertIn('AL-12', why['alpha-api/AL-13'])
        r = self.start(agents=['claude', 'codex'], sessions=5)
        refs = [p['ref'] for p in r['picks']]
        self.assertEqual(len(refs), 4)                                     # AL-15 пересекается областью с AL-14
        self.assertNotIn('alpha-api/AL-15', refs)
        self.assertTrue(r['dry_run'])
        self.assertEqual(w.state()['runs'], [])                             # предпросмотр ничего не пишет

    def test_more_backlog_more_sessions_and_pinned_executor(self):
        self.plan(*[dict(id='AL-%d' % i, size='L') for i in range(20, 26)], dict(id='AL-30', executor='codex'))
        r = self.start(agents=['claude'])
        self.assertEqual((r['backlog']['points'], r['per_ai'], r['sessions']), (48, 3, 3))   # AL-30 назначена Codex — не в счёт
        self.assertIn('назначена ИИ codex', {x['ref']: x['why'] for x in r['skipped']}['alpha-api/AL-30'])

    def test_launch_enqueues_and_opens_sessions(self):
        self.plan(dict(id='AL-10'), dict(id='AL-11'))
        with mock.patch.object(serve, 'launch_session', return_value={}) as launch:
            out = serve.start_project(dict(project='alpha-api', agents=['claude', 'codex']), origin='owner')
            with self.assertRaisesRegex(ValueError, 'владельца'):
                serve.start_project(dict(project='alpha-api', agents=['claude']))          # без токена — агент
        runs = w.state()['runs']
        self.assertEqual(sorted((r['ref'], r['agent']) for r in runs), [('alpha-api/AL-10', 'claude'), ('alpha-api/AL-11', 'codex')])
        self.assertTrue(all(r['batch'] == out['batch'] for r in runs))
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(launch.call_args_list[0].args[0], dict(ref='alpha-api/AL-10', agent='claude', run=runs[0]['id']))
        self.assertEqual((out['opened'], out['failed']), (['RUN-1', 'RUN-2'], []))
        # Повторно те же задачи не запускаются: они уже ждут исполнителя.
        self.assertEqual(self.start(agents=['claude'])['picks'], [])

    def test_empty_or_unready_backlog_explains(self):
        d = w.read('projects.json', {}); d['projects']['alpha-api']['backlog'] = []; w.save('projects.json', d)
        self.assertEqual(self.start()['reason'], 'Бэклог пуст')
        with self.assertRaisesRegex(ValueError, 'Бэклог пуст'):
            self.start(dry_run=False)
        with self.assertRaisesRegex(ValueError, 'agents'):
            self.start(agents=['gpt'])

    def test_dispatcher_respects_session_limit(self):
        self.plan(*[dict(id='AL-%d' % i) for i in range(40, 44)])
        for i in range(40, 44):
            w.action(dict(op='enqueue', ref='alpha-api/AL-%d' % i, agent='claude'))
        s = w.state()
        for r in s['runs']: r['auto_launch'] = True; r['role'] = None
        s['runs'][0].update(launched_at=w.now().isoformat())        # одно окно уже открыто
        w.save('workflow.json', s)
        w.action(dict(op='settings', changes=dict(max_sessions=3, auto_launch_review=True, sessions_per_ai=dict(claude=6))), origin='owner')
        with mock.patch.object(serve, 'launch_session', return_value={}) as launch:
            started = serve.dispatch_once()
        self.assertEqual(len(started), 2)                              # 3 места − 1 открытое
        self.assertEqual(launch.call_count, 2)
        with self.assertRaisesRegex(ValueError, 'max_sessions'):
            w.action(dict(op='settings', changes=dict(max_sessions=50)), origin='owner')

    def test_dispatcher_caps_each_ai_and_puts_resolver_first(self):
        self.plan(*[dict(id='AL-%d' % i) for i in range(50, 54)])
        for i in range(50, 54):
            w.action(dict(op='enqueue', ref='alpha-api/AL-%d' % i, agent='claude' if i < 53 else 'codex'))
        s = w.state()
        for r in s['runs']: r['auto_launch'] = True; r['role'] = None
        s['runs'][-1].update(role='resolver')                         # Решала — последним в очереди
        w.save('workflow.json', s)
        w.action(dict(op='settings', changes=dict(max_sessions=4, auto_launch_review=True, sessions_per_ai=dict(claude=2, codex=1))), origin='owner')
        with mock.patch.object(serve, 'launch_session', return_value={}) as launch:
            serve.dispatch_once()
        opened = [c.args[0] for c in launch.call_args_list]
        self.assertEqual(opened[0]['ref'], 'alpha-api/AL-53')             # Решала открыт первым (А5)
        self.assertEqual(sorted(o['agent'] for o in opened), ['claude', 'claude', 'codex'])   # А7: Claude 2, Codex 1
        with self.assertRaisesRegex(ValueError, 'sessions_per_ai'):
            w.action(dict(op='settings', changes=dict(sessions_per_ai=dict(gpt=1))), origin='owner')

    def test_keep_awake_only_while_work_is_pending(self):
        self.assertFalse(serve.busy_work())
        self.plan(dict(id='AL-60'))
        w.action(dict(op='enqueue', ref='alpha-api/AL-60', agent='claude'))
        s = w.state(); s['runs'][0]['auto_launch'] = True; w.save('workflow.json', s)
        self.assertTrue(serve.busy_work())                                  # запуск ждёт открытия
        w.action(dict(op='settings', changes=dict(auto_pipeline=False)), origin='owner')
        self.assertFalse(serve.busy_work())                                 # автозапуск выключен — спать можно
        with mock.patch.object(serve.os, 'name', 'posix'):
            self.assertFalse(serve.keep_awake(True))                        # не Windows — ничего не делает

    def recovery_queue(self, paused_stages):
        from datetime import timedelta
        stages = ['idea', 'analysis', 'review', 'deploy_test']
        roles = ['product_owner', 'system_analyst', 'reviewer', 'devops']
        self.plan(*[dict(id='AL-%d' % i) for i in range(70, 74)])
        for i in range(70, 74):
            w.action(dict(op='enqueue', ref='alpha-api/AL-%d' % i, agent='codex'))
        s = w.state(); past = w.now() - timedelta(minutes=1)
        for r, stage, role in zip(s['runs'], stages, roles):
            r.update(auto_launch=True, role=role, stage=stage)
            if stage in paused_stages:
                r.update(paused=dict(reason='limit', resume_at=past.isoformat()),
                         launched_at=past.isoformat(), launch=dict(agent='codex'))
        w.note_limit(s, 'codex', past, past - timedelta(hours=5))
        w.save('workflow.json', s)
        w.action(dict(op='settings', changes=dict(max_sessions=2, sessions_per_ai=dict(codex=2))), origin='owner')
        return s['runs']

    def test_recovery_resumes_from_end_with_session_cap_and_no_duplicates(self):
        runs = self.recovery_queue({'idea', 'analysis', 'review', 'deploy_test'})
        with mock.patch.object(serve, 'resume_session', side_effect=lambda r: w.mark_resumed(r['ref'], r['id'], dict(agent='codex'))):
            self.assertEqual(serve.dispatch_once(), [runs[3]['id'], runs[2]['id']])
            self.assertEqual(serve.dispatch_once(), [])
        self.assertEqual(w.open_sessions(w.state()['runs']), 2)

    def test_recovery_does_not_let_early_pause_jump_ahead_of_later_queue(self):
        runs = self.recovery_queue({'idea', 'analysis'})
        with mock.patch.object(serve, 'resume_session') as resume, \
             mock.patch.object(serve, 'launch_session', side_effect=lambda req: w.mark_launched(req['ref'], req['run'], dict(agent='codex'))) as launch:
            self.assertEqual(serve.dispatch_once(resume_only=True), [])
            self.assertEqual(serve.dispatch_once(), [runs[3]['id'], runs[2]['id']])
            resume.assert_not_called()
            self.assertEqual(launch.call_count, 2)

    def test_product_owner_gets_one_notice_per_recovery(self):
        import orchestra
        self.recovery_queue({'idea'})
        self.assertEqual(orchestra.product_owner_recovery(), ['codex'])
        self.assertEqual(orchestra.product_owner_recovery(), [])
        events = [e for e in w.state()['teamlead'] if e['kind'] == 'production_resumed']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['role'], 'product_owner')
        self.assertIn('Деплой в тест → Ревью → Разработка → Анализ → Идея', events[0]['text'])
        from datetime import timedelta
        s = w.state(); at = w.now()
        w.note_limit(s, 'codex', at + timedelta(hours=1), at - timedelta(seconds=1))
        w.save('workflow.json', s)
        self.assertTrue(w.mark_provider_available('codex', at))
        self.assertEqual(orchestra.product_owner_recovery(), ['codex'])
        self.assertEqual(orchestra.product_owner_recovery(), [])
