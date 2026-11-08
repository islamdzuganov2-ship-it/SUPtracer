"""CI fail-closed (TT-102; ТЗ TT-U-001: TT3-009…013, TT3-068, FR-002).

Негативная матрица: ни один путь, кроме реально пройденного обязательного шага на текущем SHA,
не даёт зелёного CI. Пропуск, отсутствие шагов, текст агента, устаревший коммит, незапустившийся
шаг и старые записи «skipped» — всё отказ.
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from base import DataCase
import ci
import deploy
import pipeline as P

PY = sys.executable


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True, encoding='utf-8',
                          check=True).stdout.strip()


def ok_step(name='Тесты', **kw):
    return dict(name=name, kind='test', cmd=[PY, '-c', 'print(1)'], **kw)


class FailClosedTests(DataCase):
    def setUp(self):
        super().setUp()
        self.repo = Path(self.tmp) / 'repo'
        self.repo.mkdir()
        git('init', '-q', '-b', 'main', cwd=self.repo)
        git('config', 'user.email', 't@t', cwd=self.repo); git('config', 'user.name', 't', cwd=self.repo)
        (self.repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
        git('add', '.', cwd=self.repo); git('commit', '-qm', 'init', cwd=self.repo)

    def commit(self, text):
        (self.repo / 'app.py').write_text(text, encoding='utf-8')
        git('commit', '-qam', 'change', cwd=self.repo)
        return git('rev-parse', 'HEAD', cwd=self.repo)

    def run_ci(self, steps, scope='task'):
        p = dict(path=str(self.repo), ci=dict(steps=steps)) if steps is not None else dict(path=str(self.repo))
        return ci.run_pipeline('alpha-api', p, self.repo, 'main', scope, ref='alpha-api/AL-01')

    # --- статусы прогона (TT3-009)

    def test_no_steps_is_not_configured_and_not_green(self):
        r = self.run_ci(None)
        self.assertEqual(r['status'], 'not_configured')
        self.assertFalse(ci.green(r)[0])
        self.assertIn('ci', ci.summary(r))                                   # подсказка, что настроить

    def test_only_optional_steps_is_not_configured(self):
        r = self.run_ci([ok_step(optional=True)])
        self.assertEqual(r['status'], 'not_configured')
        self.assertFalse(ci.green(r)[0])

    def test_steps_of_other_scope_only_is_not_configured(self):
        self.assertEqual(self.run_ci([ok_step(on=['main'])], scope='task')['status'], 'not_configured')

    def test_step_that_cannot_start_is_error(self):
        r = self.run_ci([dict(name='Нет программы', kind='test', cmd=[str(self.repo / 'нет-такого.exe')])])
        self.assertEqual(r['status'], 'error')
        ok, why = ci.green(r)
        self.assertFalse(ok); self.assertIn('не запустился', why)

    def test_passed_records_full_sha_and_is_green_on_same_head(self):
        r = self.run_ci([ok_step()])
        head = git('rev-parse', 'HEAD', cwd=self.repo)
        self.assertEqual(r['status'], 'passed')
        self.assertEqual(r['sha_full'], head)
        self.assertEqual(r['sha'], head[:7])                                  # короткий — для показа, как раньше
        self.assertTrue(ci.green(r)[0])
        self.assertTrue(ci.green(r, head)[0])

    def test_optional_failure_does_not_block_but_needs_required_pass(self):
        r = self.run_ci([dict(name='Линтер', kind='lint', cmd=[PY, '-c', 'raise SystemExit(1)'], optional=True), ok_step()])
        self.assertTrue(ci.green(r)[0])

    # --- устаревший SHA (TT3-013, TT3-068)

    def test_old_pass_does_not_allow_new_head(self):
        r = self.run_ci([ok_step()])
        new = self.commit('x = 2\n')
        ok, why = ci.green(r, new)
        self.assertFalse(ok)
        self.assertIn(new[:7], why)

    def test_commit_during_run_marks_stale(self):
        # Шаг сам коммитит в рабочую копию: проверено не то, что окажется в ветке.
        script = ("import subprocess,pathlib;pathlib.Path('app.py').write_text('x = 3\\n');"
                  "subprocess.run(['git','commit','-qam','во время CI'],check=True)")
        r = self.run_ci([dict(name='Меняет ветку', kind='test', cmd=[PY, '-c', script])])
        self.assertEqual(r['status'], 'stale')
        self.assertFalse(ci.green(r)[0])

    # --- старые записи ci.json

    def test_legacy_skipped_record_is_not_green(self):
        legacy = dict(id='CI-old', status='skipped', sha='abc1234', steps=[], note='шагов CI для «test» нет')
        self.assertFalse(ci.green(legacy)[0])
        self.assertFalse(ci.green(legacy, 'abc1234' + '0' * 33)[0])

    def test_legacy_short_sha_is_compared_by_prefix(self):
        legacy = dict(id='CI-old', status='passed', sha='abc1234', branch='t',
                      steps=[dict(name='a', status='passed', note='', seconds=1)])
        self.assertTrue(ci.green(legacy, 'abc1234' + 'f' * 33)[0])
        self.assertFalse(ci.green(legacy, 'def5678' + 'f' * 33)[0])

    def test_passed_without_steps_is_not_green(self):
        forged = dict(id='CI-x', status='passed', sha='abc1234', sha_full='abc1234' + '0' * 33, steps=[])
        self.assertFalse(ci.green(forged)[0])

    def test_unknown_or_running_status_is_not_green(self):
        for st in ('running', 'magic', None):
            self.assertFalse(ci.green(dict(id='CI-x', status=st, steps=[]))[0], st)
        self.assertFalse(ci.green(None)[0])

    # --- текст агента не заменяет CI (TT3-010)

    def ctx(self, project):
        t = dict(id='AL-01', ref='alpha-api/AL-01', title='Задача', project='alpha-api')
        return SimpleNamespace(repo=str(self.repo), branches=dict(base='main', test='main', main='main'), t=t,
                               project=project, worktree=dict(path=str(self.repo)), branch='main')

    def test_evidence_text_does_not_replace_ci(self):
        ctx = self.ctx(dict(path=str(self.repo)))                            # шагов CI нет
        ok, why = deploy.run('ci_task', ctx, evidence='Все тесты прошли локально, проверено вручную и очень тщательно')
        self.assertFalse(ok)
        self.assertIn('не настроен', why)

    def test_deploy_ci_passes_with_real_step(self):
        ok, why = deploy.run('ci_task', self.ctx(dict(path=str(self.repo), ci=dict(steps=[ok_step()]))))
        self.assertTrue(ok, why)

    # --- серверная проверка тестовой ветки (TT3-011/013)

    def test_ci_test_green_rejects_stale_test_branch(self):
        project = dict(path=str(self.repo), ci=dict(steps=[ok_step()]), branches=dict(base='main', test='main'))
        ci.run_pipeline('alpha-api', project, self.repo, 'main', 'test', ref='alpha-api/AL-01')
        ctx = P.Context(dict(id='AL-01', ref='alpha-api/AL-01', project='alpha-api'), project)
        item = dict(id='ci', check='ci_test_green')
        ok, _ = P.machine(item, 'qa', ctx, {})
        self.assertTrue(ok)
        self.commit('x = 9\n')
        ok, why = P.machine(item, 'qa', ctx, {})
        self.assertFalse(ok)
        self.assertIn('новый прогон', why)

    def test_ci_test_green_rejects_not_configured(self):
        project = dict(path=str(self.repo), branches=dict(base='main', test='main'))
        ci.run_pipeline('alpha-api', project, self.repo, 'main', 'test', ref='alpha-api/AL-01')
        ctx = P.Context(dict(id='AL-01', ref='alpha-api/AL-01', project='alpha-api'), project)
        self.assertFalse(P.machine(dict(id='ci', check='ci_test_green'), 'qa', ctx, {})[0])

    # --- автоодобрение релиза тимлида сервером (TT3-012)

    def test_server_release_gate_rejects_stale_and_not_configured(self):
        import orchestra as O
        project = dict(path=str(self.repo), ci=dict(steps=[ok_step()]), branches=dict(base='main', test='main'))
        t = dict(id='AL-01', ref='alpha-api/AL-01', project='alpha-api', title='Задача')
        pipe = dict(roles={O.key('testing', 'qa'): dict(handoff=dict(signoff=True))})
        saved = (O.project_of, O.w.cx.assess)
        O.project_of = lambda _t: project
        O.w.cx.assess = lambda _t: dict(tier=1)
        try:
            self.assertIsNone(O.server_gate({}, t, pipe, 'release'))                      # прогона нет
            ci.run_pipeline('alpha-api', dict(path=str(self.repo)), self.repo, 'main', 'test', ref=t['ref'])
            self.assertIsNone(O.server_gate({}, t, pipe, 'release'))                      # not_configured
            ci.run_pipeline('alpha-api', project, self.repo, 'main', 'test', ref=t['ref'])
            self.assertIn('подпись QA', O.server_gate({}, t, pipe, 'release'))            # зелёный на голове
            self.commit('x = 5\n')
            self.assertIsNone(O.server_gate({}, t, pipe, 'release'))                      # ветка ушла вперёд
            self.assertIsNone(O.server_gate({}, t, dict(roles={}), 'release'))            # без подписи QA
        finally:
            O.project_of, O.w.cx.assess = saved
