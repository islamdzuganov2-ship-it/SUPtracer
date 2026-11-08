"""Адаптивные маршруты FAST/STANDARD/CRITICAL (TT-109; ТЗ FR-001, E2, TC-07/08, AC-49, AC-FR01)."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from base import DataCase
import adaptive_pipeline as ap
import flags as ff
import flow_policy as fp
import mcp_server
import orchestra
import provider_balance as pb
import risk_policy as rp
import workflow as w

FIXTURE = Path(__file__).parent / 'fixtures' / 'routes' / 'tasks.json'


def task(**kw):
    d = dict(ref='alpha-api/AL-07', id='AL-07', project='alpha-api', title='Подсказка у счётчика задач',
             why='Пользователь не понимает, что значит число', size='S', risk='low', work_type='implementation',
             scope=['app/ui/'], acceptance=['Подсказка видна при наведении'])
    d.update(kw)
    return d


def git(*args, cwd):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()


class Classify(DataCase):
    def test_benchmark_no_critical_downgrade(self):                               # AC-FR01, gate E2
        cases = json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']
        rep = rp.evaluate(cases)
        self.assertGreaterEqual(rep['total'], 30)
        self.assertTrue(all(n >= 10 for n in rep['by_mode'].values()), rep['by_mode'])
        self.assertEqual(rep['critical_downgrades'], [])
        self.assertGreaterEqual(rep['accuracy'], 0.9, rep['wrong'])

    def test_security_fix_small_is_critical(self):                                # TC-07
        c = rp.classify(task(title='Убрать пароль из журнала', why='Пароль пользователя попадает в лог'), tier=1)
        self.assertEqual(c['mode'], 'CRITICAL')
        self.assertTrue(any('секрет' in r or 'права' in r for r in c['risk_reasons']), c['risk_reasons'])

    def test_fast_needs_proof(self):                                              # FR-001: FAST только доказанно
        self.assertEqual(rp.classify(task(), tier=1)['mode'], 'FAST')
        for bad, why in ((dict(scope=[]), 'scope'), (dict(acceptance=[]), 'критериев'), (dict(risk=None), 'риск не оценён'),
                         (dict(size='M'), 'размер'), (dict(work_type='architecture'), 'вид работы')):
            c = rp.classify(task(**bad), tier=1)
            self.assertEqual(c['mode'], 'STANDARD', bad)
            self.assertTrue(any(why in r for r in c['risk_reasons']), (bad, c['risk_reasons']))
        c = rp.classify(task(), tests=False, tier=1)
        self.assertEqual(c['mode'], 'STANDARD'); self.assertIn('у проекта нет машинных тестов', c['risk_reasons'])
        self.assertEqual(rp.classify(task(risk='high'), tier=1)['mode'], 'CRITICAL')
        self.assertEqual(rp.classify(task(), tier=3)['mode'], 'CRITICAL')
        doc = rp.classify(task(work_type='docs', scope=['docs/']), files=['docs/a.md'], lines=5, tests=False, tier=1)
        self.assertEqual((doc['mode'], doc['profile']), ('FAST', 'docs_only'))

    def test_diff_reevaluation_only_raises(self):                                 # TC-08, AC-49
        up = rp.reevaluate('FAST', task(), ['app/ui/button.js', 'api/openapi.yaml'], 20, tier=1)
        self.assertEqual((up['mode'], up['raised']), ('CRITICAL', True))
        up = rp.reevaluate('FAST', task(), ['app/ui/x.js', 'tools/secrets.py'], 10, tier=1)
        self.assertTrue(up['raised'])
        out = rp.reevaluate('FAST', task(), ['app/ui/x.js', 'tools/report.py'], 10, tier=1)
        self.assertEqual(out['mode'], 'STANDARD'); self.assertTrue(any('вне scope' in r for r in out['risk_reasons']))
        big = rp.reevaluate('FAST', task(), ['app/ui/%d.js' % i for i in range(9)], 400, tier=1)
        self.assertEqual(big['mode'], 'STANDARD')
        keep = rp.reevaluate('CRITICAL', task(), ['app/ui/x.js'], 3, tier=1)
        self.assertEqual((keep['mode'], keep['lowered_ignored']), ('CRITICAL', True))

    def test_doc_mentioning_migration_is_not_migration(self):
        self.assertEqual(rp.area_hits(paths=['docs/migrations.md']), {})
        self.assertIn('data', rp.area_hits(paths=['db/migrations/0001.sql']))
        self.assertEqual(rp.area_hits('Подписка на рассылку и ключевые показатели'), {})


class Plan(DataCase):
    def test_route_plan_schema(self):                                             # ТЗ §5.1
        for mode, t in (('FAST', task()), ('STANDARD', task(size='M')), ('CRITICAL', task(risk='high'))):
            p = ap.compile_plan(t, rp.classify(t, tier=1))
            for k in ('schema_version', 'task_ref', 'mode', 'risk', 'confidence', 'risk_reasons', 'required_roles',
                      'required_checks', 'optional_roles', 'release_level', 'policy_version', 'inputs_fingerprint'):
                self.assertIn(k, p)
            self.assertEqual(p['mode'], mode)
            self.assertTrue(p['inputs_fingerprint'].startswith('sha256:'))
            self.assertIn('release_policy', p['required_checks'])        # ни один маршрут не обходит релизную политику
            self.assertIn('qa', p['required_roles']); self.assertIn('reviewer', p['required_roles'])
        fast = ap.compile_plan(task(), rp.classify(task(), tier=1))
        self.assertEqual(fast['skipped_stages'], ['idea', 'analysis'])
        self.assertNotIn('system_analyst', fast['required_roles'])
        crit = ap.compile_plan(task(risk='high'), rp.classify(task(risk='high'), tier=1))
        self.assertFalse(crit['server_gates']); self.assertEqual(crit['independent_review'], 'strict')
        self.assertIn('security', crit['required_checks'])
        self.assertNotEqual(fast['inputs_fingerprint'], ap.compile_plan(task(title='Иное'), rp.classify(task(), tier=1))['inputs_fingerprint'])

    def test_applied_mode(self):
        fast = ap.compile_plan(task(), rp.classify(task(), tier=1))
        crit = ap.compile_plan(task(risk='high'), rp.classify(task(risk='high'), tier=1))
        self.assertIsNone(ap.applied_mode(fast, task(), {}))                                       # shadow по умолчанию
        self.assertIsNone(ap.applied_mode(fast, task(), dict(adaptive_mode='enforce')))            # FAST не разрешён
        self.assertEqual(ap.applied_mode(fast, task(), dict(adaptive_mode='enforce', adaptive_fast_allow=['implementation'])), 'FAST')
        self.assertEqual(ap.applied_mode(crit, task(), dict(adaptive_mode='enforce')), 'CRITICAL')
        self.assertIsNone(ap.applied_mode(crit, task(), dict(adaptive_mode='shadow')))


class Conveyor(DataCase):
    """Сквозной сценарий в настоящем git-репозитории."""
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data) / 'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': []})
        self.repo = Path(self.tmp) / 'repos' / 'alpha-api'
        git('init', '-q', '-b', 'main', cwd=self.repo)
        (self.repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
        git('add', '-A', cwd=self.repo); git('commit', '-q', '-m', 'init', cwd=self.repo)
        git('checkout', '-q', '-b', 'разработка', cwd=self.repo)
        d = w.read('projects.json', {}); p = d['projects']['alpha-api']
        p['verify'] = [sys.executable, '-c', 'print(1)']
        p['backlog'] = [b for b in p['backlog'] if b.get('status') == 'done']     # общий спринт не занят чужими задачами
        p['backlog'].append({k: v for k, v in task().items() if k not in ('ref', 'project')} | dict(status='queued', priority='🟡'))
        w.save('projects.json', d)
        os.environ['TASKTREKER_PIPELINE_SYNC'] = '1'
        self.ref = 'alpha-api/AL-07'

    def tearDown(self):
        os.environ.pop('TASKTREKER_PIPELINE_SYNC', None)
        super().tearDown()

    def to_idea(self):
        w.action(dict(op='to_idea', ref=self.ref, dry_run=False), origin='owner')
        return w.task_for(self.ref)

    def test_shadow_records_route_keeps_full_path(self):                          # E2 п.1, NFR-36
        t = self.to_idea()
        self.assertEqual(t['pipeline']['stage'], 'idea')
        r = t['pipeline']['route']
        self.assertEqual((r['plan']['mode'], r['applied'], r['adaptive_mode']), ('FAST', None, 'shadow'))
        rep = w.action(dict(op='adaptive'))
        self.assertIn(self.ref, rep['would_skip']); self.assertEqual(rep['by_mode']['FAST'], 1)
        one = w.action(dict(op='adaptive', ref=self.ref))
        self.assertEqual(one['now']['mode'], 'FAST')
        self.assertFalse(mcp_server.call_tool('tt_work', dict(op='adaptive'))['isError'])

    def test_enforce_fast_skips_analysis_then_diff_reroutes(self):                # E2 п.2–4, TC-08, AC-49
        w.action(dict(op='settings', changes=dict(adaptive_mode='enforce', adaptive_fast_allow=['implementation'])), origin='owner')
        t = self.to_idea(); pipe = t['pipeline']
        self.assertEqual(pipe['stage'], 'development', pipe.get('journal', [])[-3:])
        self.assertEqual({k: v.get('skipped') for k, v in pipe['stages'].items() if v.get('skipped')},
                         {'idea': 'маршрут FAST', 'analysis': 'маршрут FAST'})
        dor = {d['id']: d for d in pipe['stages']['development']['dor']}
        self.assertTrue(all(d['ok'] for d in dor.values()), dor)
        self.assertIn('критерии приёмки', dor['dor-tasks']['detail'])
        run = next(r for r in w.state()['runs'] if r['ref'] == self.ref and r['status'] == 'queued')
        self.assertEqual(run['role'], 'developer')
        packet = w.action(dict(op='packet', ref=self.ref, run=run['id']))
        self.assertEqual(packet['task_route']['mode'], 'FAST'); self.assertIn('BRD/SRS/HLD нет', packet['task_route']['note'])

        # Разработчик вышел за FAST: дифф задел аутентификацию.
        wt = Path(pipe['worktree']['path'])
        (wt / 'app' / 'auth').mkdir(parents=True, exist_ok=True)
        (wt / 'app' / 'auth' / 'session.py').write_text('TTL = 60\n', encoding='utf-8')
        git('add', '-A', cwd=wt); git('commit', '-q', '-m', 'AL-07: подсказка', cwd=wt)
        s = w.state(); fp.take_slot(s, self.ref, w.now())
        before = copy.deepcopy(fp.slots(s).get(self.ref)); self.assertTrue(before)
        p2 = copy.deepcopy(pipe); p2['stage'] = 'review'
        p2['stages']['development'] = dict(dor_ok=True, passed=True)
        orchestra.orchestrate(s, dict(t, pipeline=p2), p2, w.now())
        self.assertEqual(p2['stage'], 'idea')                                       # возврат: анализ теперь нужен
        r = p2['route']
        self.assertEqual((r['plan']['mode'], r['applied'], r['floor']), ('CRITICAL', 'CRITICAL', 'CRITICAL'))
        self.assertTrue(any(h['kind'] == 'reroute' and h['previous'] == 'FAST' for h in p2['history']))
        ret = [j for j in p2['journal'] if j['kind'] == 'returned'][-1]
        self.assertIn('security', ret['text']); self.assertIn('app/auth/session.py', ret['text'])
        self.assertNotIn('skipped', p2['stages'].get('idea', {}))
        self.assertEqual(fp.slots(s).get(self.ref), before)                           # FR-014: слот тот же, второго нет
        # После возврата текст задачи (он прежний, «FAST») маршрут не понижает.
        orchestra.orchestrate(s, dict(t, pipeline=p2), p2, w.now())
        self.assertEqual(p2['route']['plan']['mode'], 'CRITICAL')
        # CRITICAL: ворота только тимлиду, проверка — независимым ИИ.
        self.assertIsNone(orchestra.server_gate(s, dict(t, pipeline=p2), p2, 'analysis'))
        self.assertTrue(pb.strict(dict(t, pipeline=p2)))

    def test_fast_not_applied_mid_analysis(self):
        t = self.to_idea()                                                           # shadow: идёт полный путь
        w.action(dict(op='settings', changes=dict(adaptive_mode='enforce', adaptive_fast_allow=['implementation'])), origin='owner')
        pipe = copy.deepcopy(t['pipeline']); pipe['stage'] = 'analysis'
        orchestra.orchestrate(w.state(), dict(t, pipeline=pipe), pipe, w.now())
        self.assertEqual(pipe['stage'], 'analysis'); self.assertIsNone(pipe['route']['applied'])


class Settings(DataCase):
    def test_owner_only_and_flag(self):
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(dict(op='settings', changes=dict(adaptive_mode='enforce')))
        for bad in (dict(adaptive_mode='fast'), dict(adaptive_fast_allow=['architecture']), dict(adaptive_fast_allow='docs')):
            with self.assertRaises(ValueError):
                w.action(dict(op='settings', changes=bad), origin='owner')
        self.assertEqual(w.settings()['adaptive_mode'], 'shadow')
        self.assertFalse(ff.enabled('tt3_adaptive_pipeline', w.settings()))
        w.action(dict(op='settings', changes=dict(flags=dict(tt3_adaptive_pipeline=True))), origin='owner')
        self.assertEqual(w.settings()['adaptive_mode'], 'enforce')
        draft = w.action(dict(op='route', task=dict(size='S', risk='high', work_type='implementation', title='Права доступа')))
        self.assertEqual(draft['adaptive']['mode'], 'CRITICAL')
