"""Автопроверка ветки и ревью ИИ: из «Проверка» в «Готово» со слиянием (TT-62, TT-63)."""
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock
from base import DataCase
import launcher
import workflow as w

REF = 'alpha-api/AL-03'


def git(*args, cwd):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()


class ReviewTests(DataCase):
    def setUp(self):
        super().setUp()
        self.verify = None
        def mutate(d):
            if self.verify: d['projects']['alpha-api']['verify'] = self.verify
        self.mutate = mutate
        self.projects(mutate)
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api','kind':'запрос','status':'активна','title':'Запрос'}]})
        self.repo = Path(self.tmp)/'repos'/'alpha-api'
        git('init', '-q', '-b', 'разработка', cwd=self.repo)
        (self.repo/'src').mkdir(); (self.repo/'src'/'app.py').write_text('x = 1\n', encoding='utf-8')
        git('add', '-A', cwd=self.repo); git('commit', '-q', '-m', 'init', cwd=self.repo)
        w.action(dict(op='plan', project='alpha-api', tasks=[dict(id='AL-03', title='Правка', why='Нужна', root='request', size='S',
            priority='🟡', risk='low', task_type='dev_by_spec', spec_ref='docs/ТЗ.md §2', scope=['src'],
            acceptance=['Значение x равно 2', 'Тест проходит'])]))
        os.environ['TASKTREKER_AUTOCHECK_SYNC'] = '1'

    def tearDown(self):
        os.environ.pop('TASKTREKER_AUTOCHECK_SYNC', None)
        super().tearDown()

    def set_verify(self, cmd):
        d = w.read('projects.json', {}); d['projects']['alpha-api']['verify'] = cmd; w.save('projects.json', d)

    def work(self, files=None):
        """Запуск, принятие, коммит в ветке задачи, finish (автопроверка синхронно)."""
        import serve
        with mock.patch.object(launcher, 'find_cli', return_value='C:/bin/claude.exe'), mock.patch.object(launcher, 'open_window', return_value=1):
            r = serve.launch_session(dict(ref=REF, agent='claude'))
        wt = Path(r['worktree']['path']); run = r['run']
        w.action(dict(op='claim', ref=REF, run=run, worker='claude-cli:'+run, model='claude-haiku-5-5'))
        for name, text in (files or {'src/app.py': 'x = 2\n'}).items():
            (wt/name).parent.mkdir(parents=True, exist_ok=True); (wt/name).write_text(text, encoding='utf-8')
        git('add', '-A', cwd=wt); git('commit', '-q', '-m', 'AL-03', cwd=wt)
        w.action(dict(op='finish', ref=REF, run=run, worker='claude-cli:'+run, evidence='Сделано', tokens=100))
        return wt

    def review_run(self):
        return next(r for r in w.state()['runs'] if r.get('phase') == 'review' and r['status'] == 'queued')

    def take_review(self):
        rv = self.review_run()
        w.action(dict(op='claim', ref=REF, run=rv['id'], worker='rev', model=w.packet(REF, agent=rv['agent'])['route']['model']))
        return rv['id']

    def verdict(self, run, verdict, oks=(True, True), **kw):
        return w.action(dict(op='review', ref=REF, run=run, worker='rev', verdict=verdict,
                             criteria=[dict(ok=o, note='проверено') for o in oks], conclusion='Заключение ревью', **kw))

    # --- автопроверка

    def test_passed_autocheck_queues_auto_review(self):
        self.set_verify([sys.executable, '-c', 'print("ok")'])
        self.work()
        t = w.task_for(REF)
        self.assertTrue(t['auto_check']['ok'], t['auto_check'])
        self.assertEqual([c['key'] for c in t['auto_check']['checks']], ['commits', 'scope', 'clean', 'merge', 'verify'])
        rv = self.review_run()
        self.assertEqual((rv['agent'], rv['auto_launch'], t['status']), ('claude', True, 'review'))
        p = w.packet(REF, agent='claude')
        self.assertEqual((p['phase'], p['review']['branch']), ('review', 'ai/claude/al-03'))

    def test_failed_autocheck_returns_task_for_rework(self):
        self.set_verify([sys.executable, '-c', 'import sys; sys.exit(3)'])
        self.work({'src/app.py': 'x = 2\n', 'README.md': 'вне области\n'})
        t = w.task_for(REF)
        failed = {c['key'] for c in t['auto_check']['checks'] if not c['ok']}
        self.assertEqual(failed, {'scope', 'verify'})
        self.assertEqual(t['status'], 'queued')
        self.assertIn('Автопроверка не пройдена', t['status_note'])
        self.assertFalse(any(r.get('phase') == 'review' for r in w.state()['runs']))

    # --- ревью

    def test_approve_merges_branch_and_closes_task(self):
        self.work(); run = self.take_review()
        self.assertEqual(w.task_for(REF)['status'], 'review')   # ревью не переводит в «В работе»
        r = self.verdict(run, 'approve')
        self.assertEqual((r['status'], r['merge']['ok']), ('done', True))
        self.assertEqual((self.repo/'src'/'app.py').read_text(encoding='utf-8'), 'x = 2\n')
        self.assertIn('Слияние ai/claude/al-03', git('log', '-1', '--format=%s', cwd=self.repo))
        t = w.task_for(REF)
        self.assertEqual((t['status'], t['acceptance_done'], t['review']['verdict']), ('done', [0, 1], 'approve'))
        self.assertIn('слито', t['evidence'])

    def test_changes_returns_for_rework_with_feedback(self):
        self.work(); run = self.take_review()
        r = self.verdict(run, 'changes', oks=(True, False))
        t = w.task_for(REF)
        self.assertEqual((r['status'], t['status'], t['acceptance_done']), ('queued', 'queued', [0]))
        self.assertIn('Доработка по ревью', t['status_note'])
        self.assertEqual(w.packet(REF, agent='claude')['review_feedback']['conclusion'], 'Заключение ревью')

    def test_spec_change_records_decision_and_spec_task(self):
        self.work(); run = self.take_review()
        r = self.verdict(run, 'spec_change', spec=dict(deviation='Значение хранится в конфиге, а не в коде', proposal='Описать конфиг в §2'))
        self.assertEqual(r['status'], 'done')
        import memory
        rec = memory.get(r['spec_record'])
        self.assertEqual((rec['kind'], rec['root']), ('решение', 'request'))
        spec_task = w.task_for('alpha-api/' + r['spec_task'])
        self.assertEqual((spec_task['task_type'], spec_task['work_type'], spec_task['deliverable']), ('analysis_spec', 'docs', 'docs/ТЗ.md §2'))
        self.assertIn('Описать конфиг', spec_task['acceptance'][0])

    def test_review_guards(self):
        self.work(); run = self.take_review()
        with self.assertRaisesRegex(ValueError, 'невыполненные критерии'): self.verdict(run, 'approve', oks=(True, False))
        with self.assertRaisesRegex(ValueError, 'criteria'): self.verdict(run, 'approve', oks=(True,))
        with self.assertRaisesRegex(ValueError, 'spec.deviation'): self.verdict(run, 'spec_change')
        with self.assertRaises(ValueError):
            w.action(dict(op='review', ref=REF, run=run, worker='чужой', verdict='approve', criteria=[dict(ok=True)]*2, conclusion='x'))
        d = w.read('projects.json', {}); d['projects']['alpha-api']['backlog'][-1]['auto_check']['ok'] = False; w.save('projects.json', d)
        with self.assertRaisesRegex(ValueError, 'Автопроверка не пройдена'): self.verdict(run, 'approve')

    def test_merge_blocked_by_dirty_base_keeps_review(self):
        self.work(); run = self.take_review()
        (self.repo/'src'/'app.py').write_text('x = 99\n', encoding='utf-8')   # правка владельца в основной копии
        r = self.verdict(run, 'approve')
        t = w.task_for(REF)
        self.assertEqual((r['status'], r['merge']['ok'], t['status']), ('review', False, 'review'))
        self.assertIn('незакоммиченные', t['status_note'])

    def test_reviewer_other_ai(self):
        w.action(dict(op='settings', changes={'reviewer': 'other', 'auto_launch_review': False}))
        self.work()
        rv = self.review_run()
        self.assertEqual((rv['agent'], rv['auto_launch']), ('codex', False))
        with self.assertRaises(ValueError): w.action(dict(op='settings', changes={'reviewer': 'gemini'}))

    # --- автозапуск ревью сервером досок

    def test_dispatcher_launches_review_once_and_records_errors(self):
        import serve
        self.work()
        rv = self.review_run()
        serve._auto_attempts.clear()
        with mock.patch.object(serve, 'launch_session', side_effect=ValueError('Командная строка не найдена')) as ls:
            self.assertEqual(serve.dispatch_once(), [])
            serve.dispatch_once()                       # повтор раньше 5 минут не делается
        ls.assert_called_once_with(dict(ref=REF, agent='claude'))
        self.assertIn('не найдена', next(r for r in w.state()['runs'] if r['id'] == rv['id'])['launch_error'])
        serve._auto_attempts.clear()
        w.action(dict(op='settings', changes={'auto_launch_review': False}))
        with mock.patch.object(serve, 'launch_session') as ls:
            serve.dispatch_once()
        ls.assert_not_called()

    def test_review_session_opens_in_implementer_worktree(self):
        import serve
        wt = self.work()
        rv = self.review_run()
        with mock.patch.object(launcher, 'find_cli', return_value='C:/bin/claude.exe'), mock.patch.object(launcher, 'open_window', return_value=2) as win:
            r = serve.launch_session(dict(ref=REF, agent=rv['agent']))
        self.assertEqual(Path(r['cwd']), wt)
        self.assertIn('РЕВЬЮ', win.call_args[0][0][-1])
        self.assertIn('op=review', win.call_args[0][0][-1])

    def test_stale_code_and_dashboard_detection(self):
        import serve
        with mock.patch.object(serve, 'STARTED', 0):
            self.assertTrue(serve.code_stale())
        with mock.patch.object(serve, 'STARTED', 10**12):
            self.assertFalse(serve.code_stale())
        with mock.patch.object(serve, 'DASH', str(Path(self.tmp)/'нет.html')):
            self.assertTrue(serve.dashboard_stale())

    def test_manual_autocheck_for_task_already_in_review(self):
        self.work()
        rv = self.review_run()
        with self.assertRaisesRegex(ValueError, 'активный запуск'):
            w.action(dict(op='autocheck', ref=REF))
        w.action(dict(op='cancel', ref=REF, run=rv['id'], evidence='Повторим автопроверку'))
        w.action(dict(op='status', ref=REF, status='review'))
        r = w.action(dict(op='autocheck', ref=REF))
        self.assertTrue(r['ok'])
        self.assertTrue(w.task_for(REF)['auto_check']['ok'])
        self.assertEqual(self.review_run()['phase'], 'review')
