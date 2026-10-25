"""Метрики работ по спринтам лимита: окна, git, фичи, документы, качество (TT-74)."""
import os
import subprocess
from unittest import mock
from pathlib import Path
from base import DataCase
import filekinds as fk
import metrics as M

H = '2026-10-20T%02d'


def git(*args, cwd, date=None):
    env = dict(os.environ, GIT_AUTHOR_DATE=date or '', GIT_COMMITTER_DATE=date or '') if date else None
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True, env=env,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()


def usage(claude_hours, codex_hours=(), limits_claude=(), limits_codex=()):
    return dict(providers=dict(
        claude=dict(projects={'alpha-api': dict(by_hour={H % h: [1000, 0.5, 2] for h in claude_hours})}, limits=list(limits_claude)),
        codex=dict(projects={'alpha-api': dict(by_hour={H % h: [300, 0.1, 1] for h in codex_hours})}, limits=list(limits_codex))))


class SprintTests(DataCase):
    def test_limit_window_and_activity_blocks(self):
        u = usage([2, 3, 4, 13], limits_claude=[dict(provider='claude', at='2026-10-20T04:30:00Z',
                                                    resets_at='2026-10-20T06:00:00+00:00', kind='five_hour', project='alpha-api')])
        sp = M.build_sprints(u)
        self.assertEqual([(s['start'], s['end'], s['basis']) for s in sp],
                         [('2026-10-20T01:00+00:00', '2026-10-20T06:00+00:00', 'limit'),
                          ('2026-10-20T13:00+00:00', '2026-10-20T18:00+00:00', 'activity')])
        self.assertEqual(sp[0]['limit_hit'], ['claude'])
        self.assertEqual(sp[1]['limit_hit'], [])

    def test_overlapping_windows_do_not_chain_into_long_sprint(self):
        # Окна двух ИИ сдвинуты: перекрытое окно начинается, где кончилось предыдущее (не склейка на сутки).
        u = usage([2], [5], limits_claude=[dict(provider='claude', at='2026-10-20T04:00:00Z', resets_at='2026-10-20T06:00:00+00:00', kind='five_hour')],
                  limits_codex=[dict(provider='codex', kind='window', resets_at='2026-10-20T09:00:00+00:00', window_minutes=300)])
        sp = M.build_sprints(u)
        self.assertEqual([(s['start'], s['end']) for s in sp],
                         [('2026-10-20T01:00+00:00', '2026-10-20T06:00+00:00'), ('2026-10-20T06:00+00:00', '2026-10-20T09:00+00:00')])
        self.assertEqual(sp[0]['windows'], ['claude', 'codex'])
        # Цепочка сдвинутых окон за сутки — несколько спринтов, каждый не длиннее окна.
        chain = [dict(provider='codex', kind='window', resets_at='2026-10-21T%02d:00:00+00:00' % h, window_minutes=300) for h in (5, 8, 11, 14, 17)]
        sp = M.build_sprints(usage([1], limits_codex=chain))
        self.assertTrue(all(M.stamp(s['end']) - M.stamp(s['start']) <= M.WINDOW for s in sp))
        self.assertGreater(len(sp), 3)

    def test_weekly_limit_does_not_define_window(self):
        u = usage([10], limits_claude=[dict(provider='claude', at='2026-10-20T10:20:00Z', resets_at='2026-10-25T00:00:00+00:00', kind='seven_day')])
        sp = M.build_sprints(u)
        self.assertEqual((sp[0]['basis'], sp[0]['weekly'], sp[0]['limit_hit']), ('activity', True, ['claude']))


class ProjectMetricsTests(DataCase):
    def setUp(self):
        super().setUp(); d = self.projects()
        self.repo = Path(d['projects']['alpha-api']['path'])
        git('init', '-q', '-b', 'main', cwd=self.repo)
        files = {'app.py': 'a\nb\n', 'docs/ТЗ.md': '# ТЗ\n', 'README.md': '# R\n', 'tests/test_app.py': 'x\n', 'tests/e2e/test_ui.py': 'y\n'}
        for name, text in files.items():
            (self.repo/name).parent.mkdir(parents=True, exist_ok=True); (self.repo/name).write_text(text, encoding='utf-8')
        git('add', '-A', cwd=self.repo); git('commit', '-qm', 'AL-01 кеш ответов', cwd=self.repo, date='2026-10-20T02:30:00+00:00')
        (self.repo/'app.py').write_text('a\nc\nd\n', encoding='utf-8')
        git('commit', '-qam', 'правка вне спринта', cwd=self.repo, date='2026-10-20T20:30:00+00:00')

    def test_sprint_rows_split_tokens_code_docs_features(self):
        u = usage([2, 3], [3])
        sp = M.build_sprints(u); idx = M.Index(sp)
        import json
        with open(os.path.join(self.data, 'projects.json'), encoding='utf-8') as f:
            p = json.load(f)['projects']['alpha-api']
        p['backlog'][0]['status'] = 'done'                    # AL-01 закрыта без даты — по коммиту с номером
        out = M.project_metrics('alpha-api', p, u, sp, idx, {'runs': []}, '2026-10-01')
        self.assertEqual(len(out['sprints']), 1)
        s = out['sprints'][0]
        self.assertEqual((s['tokens'], s['requests']), (dict(claude=2000, codex=300), dict(claude=4, codex=1)))
        self.assertEqual((s['commits'], s['code']['added'], s['tests']['files']), (1, 2, 2))
        self.assertEqual({d['path']: d['kind'] for d in s['docs']['files']}, {'docs/ТЗ.md': 'spec', 'README.md': 'doc'})
        self.assertEqual(s['docs']['spec'], 1)
        self.assertEqual([f['id'] for f in s['features']], ['AL-01'])      # AL-02 закрыта без даты и коммита — вне спринтов
        self.assertEqual(out['outside_commits'], 1)
        self.assertEqual(out['tests']['levels'], dict(unit=1, e2e=1))
        # Весь портфель — сумма проектов по тем же спринтам.
        pf = M.portfolio({'alpha-api': out, 'copy': out}, sp)
        self.assertEqual(pf['totals']['tokens'], dict(claude=4000, codex=600))
        self.assertEqual([f['id'] for f in pf['sprints'][0]['features']], ['alpha-api/AL-01', 'copy/AL-01'])
        self.assertEqual((pf['tests']['levels'], pf['outside_commits']), (dict(unit=2, e2e=2), 2))

    def test_quality_from_pipeline_history_and_defects(self):
        p = dict(backlog=[dict(id='AL-09', title='x', status='done', pipeline=dict(
            worktree={}, roles={'analysis/designer': dict(agent='claude'), 'development/developer': dict(agent='codex'),
                                'testing/qa': dict(agent='claude')},
            history=[dict(kind='stage_passed', stage='analysis', at='2026-10-20T02:00:00+00:00'),
                     dict(kind='handoff', role='developer', agent='codex', run='R1', accepted=False, at='2026-10-20T03:00:00+00:00'),
                     dict(kind='handoff', role='developer', agent='codex', run='R1', accepted=True, at='2026-10-20T03:10:00+00:00'),
                     dict(kind='stage_passed', stage='review', at='2026-10-20T04:00:00+00:00'),
                     dict(kind='return', role='qa', stage='testing', to_stage='development', roles=[], defects=['D-1'], at='2026-10-20T04:30:00+00:00'),
                     dict(kind='handoff', role='developer', agent='codex', run='R2', accepted=True, at='2026-10-20T05:00:00+00:00'),
                     dict(kind='return', role='developer', stage='development', to_stage='analysis', roles=['designer'], defects=[], at='2026-10-20T05:20:00+00:00')],
            defects=[dict(id='D-1', title='Падает на пустом', severity='major', level='integration', found_by='qa', found_agent='claude',
                          status='verified', fix=dict(method='test_fix', test_level='unit', agent='codex'))]))])
        q = M.quality('alpha-api', p, {'runs': []}, None)
        self.assertEqual((q['spec_changes']['events'], q['spec_changes']['base']), (1, 1))          # возврат в анализ после его сдачи
        self.assertEqual((q['code_after_testing']['events'], q['code_after_testing']['base']), (1, 1))  # возврат QA
        d = q['defects']
        self.assertEqual((d['found'], d['fixed'], d['verified'], d['by_method'], d['test_fix_levels']),
                         (1, 1, 1, {'test_fix': 1}, {'unit': 1}))
        dev = next(r for r in q['roles'] if r['role'] == 'developer')
        self.assertEqual((dev['handoffs'], dev['first_try'], dev['first_try_pct'], dev['returns']), (3, 1, 50, 1))
        designer = next(r for r in q['roles'] if r['role'] == 'designer')
        self.assertEqual((designer['returns'], designer['clean_pct']), (1, 0))
        self.assertEqual(q['agents']['codex']['defects_fixed'], 1)
        self.assertEqual(q['agents']['claude']['defects_found'], 1)


class FileKindTests(DataCase):
    def test_classify_and_levels(self):
        cases = {'tools/app.py': 'code', 'tests/test_x.py': 'test', 'src/a.test.ts': 'test', 'docs/КОНВЕЙЕР.md': 'doc',
                 'docs/ТЗ.md': 'spec', 'docs/tasks/TT-1/SRS.md': 'spec', 'docs/tasks/TT-1/design/ui-spec.md': 'spec',
                 'docs/tasks/TT-1/review.md': 'doc', 'node_modules/x/a.js': 'skip', 'dashboard.html': 'skip', 'logo.png': 'other'}
        for path, kind in cases.items():
            self.assertEqual(fk.classify(path), kind, path)
        self.assertEqual([fk.test_level(p) for p in ('tests/test_x.py', 'tests/e2e/a.spec.ts', 'tests/integration/test_db.py', 'tests/load/k6.js')],
                         ['unit', 'e2e', 'integration', 'load'])
        self.assertTrue(fk.spec_in_task('docs/tasks/TT-1/SRS.md', 'TT-1'))
        self.assertFalse(fk.spec_in_task('docs/tasks/TT-2/SRS.md', 'TT-1'))


class TeamTests(DataCase):
    def test_agents_done_tokens_upcoming_returns(self):
        p = dict(backlog=[
            dict(id='AL-1', title='a', status='queued', pipeline=dict(stage='development', needs=dict(design=False, marketing=True),
                 roles={'development/developer': dict(agent='codex')}, gates={'analysis': dict(decision='approve')},
                 history=[dict(kind='return', role='reviewer', to_stage='development', roles=[], reason='Нет обработки пустого списка', at='2026-10-20T01:00:00+00:00'),
                          dict(kind='handoff', role='developer', agent='codex', run='R1', accepted=False, failed=['dev-tests'], at='2026-10-20T00:30:00+00:00')])),
            dict(id='AL-2', title='b', status='queued', pipeline=dict(stage='idea', roles={})),
            dict(id='AL-3', title='c', status='queued'),
        ])
        wf = dict(runs=[
            dict(id='R1', ref='alpha-api/AL-1', role='developer', agent='codex', status='done', evidence='Код готов', launch=dict(session='cx-1')),
            dict(id='R2', ref='alpha-api/AL-1', role='product_owner', agent='claude', status='done', evidence='BRD', launch=dict(session='cl-1'),
                 launches=[dict(session='cl-1'), dict(session='cl-2')]),
            dict(id='R3', ref='alpha-api/AL-2', role='product_owner', agent='claude', status='done', evidence='Эскалация Решале: нет данных'),
            dict(id='R4', ref='alpha-api/AL-3', agent='claude', status='done', evidence='готово'),
        ])
        usage = dict(providers=dict(claude=dict(sessions={'cl-1': [1000, 1.5], 'cl-2': [500, 0.5]}), codex=dict(sessions={'cx-1': [300, 0.3]})))
        with mock.patch.object(M, 'codex_sessions', return_value=[]):
            t = M.team('alpha-api', p, wf, usage)
        a = {(x['role'], x['agent']): x for x in t['agents']}
        self.assertEqual((a[('product_owner', 'claude')]['done'], a[('product_owner', 'claude')]['tokens'], a[('product_owner', 'claude')]['usd']), (1, 1500, 2.0))
        self.assertEqual((a[('developer', 'codex')]['done'], a[('developer', 'codex')]['tokens']), (1, 300))
        self.assertEqual(a[(M.EXECUTOR, 'claude')]['done'], 1)                      # задача вне конвейера
        r = t['roles']
        self.assertEqual((r['developer']['returns'], r['developer']['dod_rejects'], r['developer']['dod_top']), (1, 1, [('dev-tests', 1)]))
        self.assertIn('пустого списка', r['developer']['reasons'][0]['reason'])
        # Предстоит: AL-1 — разработка и дальше; AL-2 — всё с идеи, дизайн и маркетинг «возможно».
        self.assertEqual((r['developer']['upcoming'], r['qa']['upcoming'], r['product_owner']['upcoming']), (2, 2, 1))
        self.assertEqual((r['designer']['maybe'], r['marketer']['maybe']), (1, 1))
        self.assertEqual(r[M.EXECUTOR]['upcoming'], 1)
        merged = M.merge_team([t, t])
        self.assertEqual(next(x for x in merged['agents'] if x['role'] == 'developer')['tokens'], 600)
        self.assertEqual(merged['roles']['developer']['returns'], 2)
