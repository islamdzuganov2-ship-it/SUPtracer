"""CI/CD: конфигурация, прогоны по шагам, тег релиза, откат и экспорт в GitHub Actions (TT-84)."""
import json
import subprocess
import sys
from pathlib import Path
from base import DataCase
import ci

PY = sys.executable


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True, encoding='utf-8',
                          check=True).stdout.strip()


class CITests(DataCase):
    def make_repo(self):
        repo = Path(self.tmp) / 'repo'
        repo.mkdir()
        git('init', '-q', '-b', 'main', cwd=repo)
        git('config', 'user.email', 't@t', cwd=repo); git('config', 'user.name', 't', cwd=repo)
        (repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
        git('add', '.', cwd=repo); git('commit', '-qm', 'init', cwd=repo)
        return repo

    def test_config_sources(self):
        repo = self.make_repo()
        steps = [dict(name='Тесты', kind='test', cmd=[PY, '-c', 'print(1)'])]
        self.assertEqual(ci.config(dict(path=str(repo), ci=dict(steps=steps)))['source'], 'projects.json')
        self.assertEqual(ci.config(dict(path=str(repo), verify=[PY, '-c', '1']))['source'], 'verify')
        self.assertEqual(ci.config(dict(path=str(repo)))['source'], 'none')
        (repo / 'requirements.txt').write_text('', encoding='utf-8'); (repo / 'tests').mkdir()
        (repo / 'package.json').write_text(json.dumps(dict(scripts=dict(build='x', test='y'))), encoding='utf-8')
        auto = ci.config(dict(path=str(repo)))
        self.assertEqual(auto['source'], 'auto')
        self.assertEqual([s['name'] for s in auto['steps']], ['Тесты Python', 'npm run build', 'npm run test'])
        (repo / 'scripts').mkdir(); (repo / 'scripts' / 'check.ps1').write_text('', encoding='utf-8')
        self.assertIn('check.ps1', ci.config(dict(path=str(repo)))['steps'][0]['name'])   # скрипт проекта главнее

    def test_validate(self):
        self.assertIsNone(ci.validate(dict(steps=[dict(name='a', cmd=['x'], on=['task'])])))
        self.assertIn('cmd', ci.validate(dict(steps=[dict(name='a', cmd='x')])))
        self.assertIn('kind', ci.validate(dict(steps=[dict(name='a', cmd=['x'], kind='magic')])))
        self.assertIn('on', ci.validate(dict(steps=[dict(name='a', cmd=['x'], on=['prod'])])))
        self.assertIn('deploy', ci.validate(dict(steps=[], deploy=dict(prod={}))))

    def test_run_by_scope_stops_on_failure_and_keeps_log(self):
        repo = self.make_repo()
        p = dict(path=str(repo), ci=dict(steps=[
            dict(name='Только задача', kind='lint', cmd=[PY, '-c', 'print("{branch} {base}")'], on=['task']),
            dict(name='Необязательный', kind='security', cmd=[PY, '-c', 'raise SystemExit(3)'], optional=True),
            dict(name='Падает', kind='test', cmd=[PY, '-c', 'raise SystemExit(2)']),
            dict(name='Не дойдёт', kind='test', cmd=[PY, '-c', 'print(1)']),
        ]))
        r = ci.run_pipeline('alpha-api', p, repo, 'main', 'task', ref='alpha-api/AL-01', branches=dict(base='dev'))
        self.assertEqual(r['status'], 'failed')
        self.assertEqual([s['name'] for s in r['steps']], ['Только задача', 'Необязательный', 'Падает'])
        self.assertIn('main dev', Path(r['log']).read_text(encoding='utf-8'))          # плейсхолдеры подставлены
        self.assertIn('«Падает»', ci.summary(r))
        self.assertTrue(Path(r['log']).is_relative_to(Path(self.tmp)))                 # журнал у данных теста
        r2 = ci.run_pipeline('alpha-api', p, repo, 'main', 'main', ref='alpha-api/AL-01')
        self.assertNotIn('Только задача', [s['name'] for s in r2['steps']])
        self.assertNotEqual(r['id'], r2['id'])                                       # в одну секунду — разные прогоны
        self.assertEqual(ci.last(ref='alpha-api/AL-01', scope='task')['id'], r['id'])
        self.assertEqual(len(ci.runs(ref='alpha-api/AL-01')), 2)
        skipped = ci.run_pipeline('alpha-api', dict(path=str(repo)), repo, 'main', 'test')
        self.assertEqual(skipped['status'], 'not_configured')          # нет шагов — не «пропущено»

    def test_deploy_tag_and_rollback(self):
        repo = self.make_repo()
        self.assertIsNone(ci.deploy(dict(path=str(repo)), 'test', repo)[0])            # нечего разворачивать
        ok, msg = ci.deploy(dict(path=str(repo), ci=dict(steps=[], deploy=dict(test=dict(
            cmd=[PY, '-c', 'print(1)'], health=[PY, '-c', 'raise SystemExit(1)'])))), 'test', repo)
        self.assertFalse(ok); self.assertIn('работоспособности', msg)
        git('checkout', '-qb', 'feature', cwd=repo)
        (repo / 'app.py').write_text('x = 2\n', encoding='utf-8'); git('commit', '-qam', 'AL-01 x', cwd=repo)
        git('checkout', '-q', 'main', cwd=repo); git('merge', '-q', '--no-ff', 'feature', '-m', 'merge AL-01', cwd=repo)
        ok, name = ci.tag_release(repo, 'AL-01', 'Кеш')
        self.assertTrue(ok); self.assertTrue(name.startswith('release/') and name.endswith('-al-01'))
        self.assertIn(name, git('tag', '--list', cwd=repo))
        ok, msg = ci.rollback(repo, git('rev-parse', 'HEAD', cwd=repo))
        self.assertTrue(ok, msg)
        self.assertEqual((repo / 'app.py').read_text(encoding='utf-8'), 'x = 1\n')

    def test_export_github_actions(self):
        repo = self.make_repo()
        p = dict(path=str(repo), ci=dict(steps=[
            dict(name='Политика', cmd=['{repo}/.venv/Scripts/python.exe', 'check.py', '--base', '{base}'], on=['task']),
            dict(name='Flutter', cmd=['flutter', 'test'], cwd='app', optional=True)]))
        dest = ci.export_github(p, dict(base='разработка', test='тестирование', main='main'))
        text = dest.read_text(encoding='utf-8')
        self.assertIn('tasktreker: generated', text)
        self.assertIn('runs-on: windows-latest', text)
        self.assertIn('--base разработка', text)
        self.assertIn('working-directory: app', text)
        self.assertIn('continue-on-error: true', text)
