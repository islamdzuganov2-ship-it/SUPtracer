"""«▶ Запустить проект»: пакет готовых задач бэклога по объёму бэклога (TT-76)."""
from pathlib import Path
from unittest import mock
from base import DataCase
import serve
import workflow as w


class StartProjectTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        self.base = dict(title='Правка', why='Нужна', root='request', size='S', priority='🟡', risk='low',
                         work_type='implementation', acceptance=['Тест проходит'], depends_on=[])

    def plan(self, *tasks):
        return w.action(dict(op='plan', project='alpha-api', tasks=[dict(self.base, **t) for t in tasks]))

    def start(self, **kw):
        return w.action(dict(dict(op='start', project='alpha-api'), **kw))

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
            out = serve.start_project(dict(project='alpha-api', agents=['claude', 'codex']))
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
