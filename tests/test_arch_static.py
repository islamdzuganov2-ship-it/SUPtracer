# -*- coding: utf-8 -*-
"""Статический граф Python (TT-134: TTN-094…101; NET-AC-069…072, 073, 077…080).

Fixture — tests/fixtures/arch/code, копируется во временный каталог: там же создаются исключаемые каталоги,
файл с ошибкой синтаксиса и ссылки. Git опрашивается подставной функцией, кроме теста SHA.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from base import TOOLS  # noqa: F401
import arch_graph as g
import arch_model as m
import arch_static as s

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'arch', 'code')
SHA = 'a' * 40


def fake_git(sha=SHA, branch='разработка', dirty=''):
    def run(cmd):
        if cmd[-2:] == ['rev-parse', 'HEAD']:
            return 0, sha + '\n'
        if 'abbrev-ref' in cmd:
            return 0, branch + '\n'
        if 'status' in cmd:
            return 0, dirty
        return None, 'нет'
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tt-static-')
        self.root = os.path.join(self.tmp, 'repo')
        shutil.copytree(FIX, self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, repo='shop', **kw):
        run = kw.pop('run', fake_git())
        return s.scan(self.root, repo, run=run, **kw)

    def by(self, gr, kind):
        return {n['qualname']: n for n in gr['nodes'] if n['kind'] == kind}

    def edges(self, gr, etype):
        ids = {n['id']: n for n in gr['nodes']}
        return {(ids[e['source']]['qualname'], ids[e['target']]['qualname']): e for e in gr['edges'] if e['type'] == etype}


class Structure(Base):
    def test_modules_classes_functions_with_location(self):              # TTN-096
        gr = self.scan()
        mods = self.by(gr, 'module')
        self.assertTrue({'app', 'app.core', 'app.web', 'app.dyn', 'app.cli', 'other.pkg.core', 'canary', 'setup'} <= set(mods))
        self.assertIn('app.core.Store', self.by(gr, 'class'))
        fn = self.by(gr, 'function')
        self.assertEqual((fn['app.core.helper']['file'], fn['app.core.helper']['line']), ('app/core.py', 5))
        self.assertTrue(fn['app.core.Store.save']['method'])
        contains = self.edges(gr, 'contains')
        self.assertIn(('app.core', 'app.core.Store'), contains)
        self.assertIn(('app.core.Store', 'app.core.Store.save'), contains)
        self.assertIn(('app', 'app.core'), contains)                       # пакет → модуль
        self.assertEqual(len(mods['app.core']['sha256']), 64)

    def test_same_name_in_other_package_or_repo_not_merged(self):         # NET-AC-072
        gr = self.scan()
        fn = self.by(gr, 'function')
        self.assertNotEqual(fn['app.core.helper']['id'], fn['other.pkg.core.helper']['id'])
        other = self.scan(repo='blog')
        self.assertNotEqual(self.by(other, 'function')['app.core.helper']['id'], fn['app.core.helper']['id'])

    def test_ids_stable_across_rescan_and_line_shift(self):               # TTN-093
        a = self.scan()
        path = os.path.join(self.root, 'app', 'web.py')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n\n' + text)
        b = self.scan()
        self.assertEqual({n['id'] for n in a['nodes']}, {n['id'] for n in b['nodes']})
        self.assertEqual({e['id'] for e in a['edges']}, {e['id'] for e in b['edges']})
        self.assertEqual(self.by(b, 'function')['app.web.items']['line'], self.by(a, 'function')['app.web.items']['line'] + 2)


class ImportsAndCalls(Base):
    def test_import_is_not_call_and_not_network(self):                    # NET-AC-069, 030, 078
        gr = self.scan()
        imp = self.edges(gr, 'imports')
        self.assertIn(('app.dyn', 'app.core'), imp)
        calls = self.edges(gr, 'calls_static')
        self.assertFalse([k for k in calls if k[0].startswith('app.dyn')])       # импорт без вызова — не вызов
        self.assertEqual({e['type'] for e in gr['edges']} - set(g.CODE_EDGES), set())
        self.assertFalse([e for e in gr['edges'] if e['type'] in g.OTHER_LAYERS])
        # requests.get(URL) — внешний вызов, а не сетевое ребро к 198.51.100.7.
        self.assertNotIn('198.51.100.7', json.dumps(gr))
        ext = self.by(gr, 'external_module')
        self.assertTrue(ext['json']['stdlib']); self.assertFalse(ext['requests']['stdlib'])
        self.assertEqual(imp[('app.web', 'requests')]['evidence_quality'], 'inferred')
        self.assertEqual(imp[('app.web', 'json')]['evidence_quality'], 'verified')

    def test_calls_static_with_provenance(self):                          # NET-AC-070, TTN-094
        gr = self.scan()
        calls = self.edges(gr, 'calls_static')
        e = calls[('app.web.items', 'app.core.helper')]
        self.assertEqual(e['resolution'], 'resolved')
        self.assertEqual(len(e['locations']), 2)                          # helper() и core.helper() — одна связь, два места
        for p in e['locations']:
            self.assertEqual((p['repo_id'], p['commit_sha'], p['file'], p['parser']), ('shop', SHA, 'app/web.py', s.PARSER))
            self.assertIsInstance(p['line'], int)
        self.assertIn(('app.web.items', 'app.core.Store'), calls)          # создание экземпляра
        self.assertIn(('app.core.Store.save', 'app.core.Store._write'), calls)   # self.метод того же класса
        self.assertIn(('app.core.Store._write', 'app.core.helper'), calls)

    def test_dynamic_marked_unresolved(self):                             # TTN-097, NET-AC-071
        gr = self.scan()
        dyn = [u for u in gr['unresolved'] if u['location']['file'] == 'app/dyn.py']
        reasons = sorted(u['reason'] for u in dyn)
        self.assertEqual(reasons, ['dynamic_import', 'dynamic_import', 'eval_exec', 'getattr', 'getattr'])
        self.assertTrue(all(u['resolution'] == 'dynamic_unresolved' for u in dyn))
        self.assertFalse([k for k in self.edges(gr, 'calls_static') if k[0] == 'app.dyn.reflect'])
        self.assertEqual(gr['coverage']['unresolved_by_reason']['getattr'] >= 2, True)

    def test_ambiguous_import_is_not_a_choice(self):
        os.makedirs(os.path.join(self.root, 'tools'))
        with open(os.path.join(self.root, 'tools', 'cli.py'), 'w', encoding='utf-8') as f:
            f.write('import core\n\ndef go():\n    core.helper()\n')
        gr = self.scan()
        imp = {k: e for k, e in self.edges(gr, 'imports').items() if k[0] == 'tools.cli'}
        self.assertEqual(set(imp), {('tools.cli', 'app.core'), ('tools.cli', 'other.pkg.core')})
        self.assertTrue(all(e['resolution'] == 'ambiguous' and e['evidence_quality'] == 'inferred' for e in imp.values()))
        self.assertFalse([k for k in self.edges(gr, 'calls_static') if k[0] == 'tools.cli.go'])


class Entrypoints(Base):
    def test_routes_linked_to_handlers(self):                             # TTN-098
        shutil.copy(os.path.join(self.root, 'app', 'web.py'), os.path.join(self.root, 'other', 'web.py'))
        gr = self.scan()
        self.assertIn('other.web:GET /items', self.by(gr, 'route'))      # тот же путь другого приложения — свой узел
        routes = self.by(gr, 'route')
        self.assertEqual({'app.web:' + r for r in ('GET /items', 'GET /orders', 'POST /orders', 'GET /health', 'GET /',
                                                   'GET /index.html', 'GET /api/', 'POST /api/save', 'POST /api/sync')}, {q for q in routes if q.startswith('app.web:')})
        self.assertEqual(routes['app.web:GET /api/']['match'], 'prefix')
        h = self.edges(gr, 'handles_route')
        self.assertIn(('app.web.items', 'app.web:GET /items'), h)
        self.assertIn(('app.web.Handler.send_health', 'app.web:GET /health'), h)            # ветка делегирует методу
        self.assertEqual(h[('app.web.Handler.do_GET', 'app.web:GET /')]['handler_kind'], 'inline')  # reply — общий ответчик
        self.assertEqual(h[('app.web.Handler.do_GET', 'app.web:GET /api/')]['handler_kind'], 'inline')
        self.assertIn(('app.web.Handler.save', 'app.web:POST /api/save'), h)
        # Путь только из списка допустимых — обрабатывает остаток do_POST, а не выдуманная функция.
        self.assertEqual(h[('app.web.Handler.do_POST', 'app.web:POST /api/sync')]['handler_kind'], 'fallthrough')

    def test_cli_commands_and_jobs(self):
        gr = self.scan()
        h = self.edges(gr, 'handles_command')
        self.assertEqual(h[('app.cli.cmd_run', 'app.cli:run')]['resolution'], 'resolved')
        self.assertEqual(h[('app.cli.cmd_serve', 'app.cli:serve')]['resolution'], 'resolved')     # argparse
        lam = h[('app.cli', 'app.cli:sync')]
        self.assertEqual((lam['resolution'], lam['evidence_quality']), ('dynamic_unresolved', 'inferred'))
        hint = [u['hint'] for u in gr['unresolved'] if u['reason'] == 'dispatch_lambda']
        self.assertEqual(hint, ['передаёт в sync.py'])
        jobs = self.edges(gr, 'starts_job')
        self.assertIn(('app.cli.cmd_serve', 'app.cli.cmd_serve->app.cli.worker'), jobs)


class Safety(Base):
    def test_project_code_never_executed(self):                           # TTN-099, NET-AC-079
        gr = self.scan()
        self.assertIn('canary', self.by(gr, 'module'))
        self.assertIn('setup', self.by(gr, 'module'))
        for mark in ('CANARY_RAN', 'SETUP_RAN'):
            self.assertFalse(os.path.exists(os.path.join(self.root, mark)), mark)
        self.assertFalse(gr['coverage']['executed_code'])
        self.assertNotIn('CanaryStatic_8q!', json.dumps(gr, ensure_ascii=False))   # фрагменты кода не хранятся

    def test_excluded_dirs_hidden_and_limits(self):                       # TTN-095, NET-AC-080, ARCH-FR-02.7
        for d in ('.venv/lib', 'node_modules/x', 'dist', 'build', 'tmp', '.hidden', 'proj.egg-info'):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
            with open(os.path.join(self.root, d, 'leak.py'), 'w') as f:
                f.write('def leak():\n    pass\n')
        wt = os.path.join(self.root, 'copy')
        os.makedirs(wt)
        with open(os.path.join(wt, '.git'), 'w') as f:
            f.write('gitdir: elsewhere\n')
        with open(os.path.join(wt, 'w.py'), 'w') as f:
            f.write('x = 1\n')
        with open(os.path.join(self.root, 'broken.py'), 'w') as f:
            f.write('def (:\n')
        with open(os.path.join(self.root, 'big.py'), 'w') as f:
            f.write('#' * 5000)
        gr = self.scan(max_file=4000)
        files = {n['file'] for n in gr['nodes'] if n['kind'] == 'module'}
        self.assertFalse([f for f in files if 'leak' in f or f.startswith('copy/')])
        self.assertEqual(self.by(gr, 'module')['broken']['parse_state'], 'syntax_error')
        self.assertEqual(gr['coverage']['syntax_error'], 1)
        self.assertIn(dict(path='big.py', reason='skipped_size'), gr['refused'])
        few = self.scan(max_files=2)
        self.assertTrue(few['coverage']['truncated'])
        self.assertEqual(few['coverage']['modules'], 2)

    def test_symlink_out_of_root_refused(self):                           # NET-AC-024, 080
        outside = os.path.join(self.tmp, 'outside')
        os.makedirs(outside)
        with open(os.path.join(outside, 'secret.py'), 'w') as f:
            f.write('def secret():\n    pass\n')
        try:
            os.symlink(outside, os.path.join(self.root, 'link'), target_is_directory=True)
            os.symlink(os.path.join(outside, 'secret.py'), os.path.join(self.root, 'flink.py'))
        except (OSError, NotImplementedError):
            self.skipTest('нет права создавать ссылки')
        gr = self.scan()
        self.assertNotIn('secret', json.dumps([n['qualname'] for n in gr['nodes']]))
        self.assertEqual({r['path'] for r in gr['refused']}, {'link', 'flink.py'})


class Versions(Base):
    def test_commit_sha_from_git(self):                                   # TTN-094: проверка SHA
        if not shutil.which('git'):
            self.skipTest('нет git')
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        for cmd in (['init', '-q'], ['add', '.'], ['commit', '-q', '-m', 'fixture']):
            subprocess.run(['git', '-C', self.root] + cmd, check=True, env=env, capture_output=True)
        head = subprocess.run(['git', '-C', self.root, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()

        def run(cmd):
            self.assertTrue(s.ac.readonly(cmd), cmd)
            p = subprocess.run(cmd, capture_output=True, text=True)
            return p.returncode, p.stdout
        gr = self.scan(run=run)
        self.assertEqual(gr['version_ref']['commit_sha'], head)
        self.assertFalse(gr['version_ref']['dirty'])
        self.assertTrue(all(p['commit_sha'] == head for e in gr['edges'] for p in e['locations']))

    def test_no_git_means_unknown_version(self):
        gr = self.scan(run=lambda cmd: (None, 'не найдено: git'))
        self.assertEqual(gr['version_ref'], g.version_ref('shop', None, None, None))
        self.assertEqual(gr['deployment']['state'], 'not_verified')

    def test_design_code_runtime_versions_separate(self):                 # TTN-101, NET-AC-073
        v = g.version_ref('shop', SHA, 'разработка', False)
        self.assertEqual(g.deployment(v)['state'], 'not_verified')
        mis = g.deployment(v, dict(commit_sha='b' * 40))
        self.assertEqual((mis['state'], mis['code_sha'], mis['runtime_sha']), ('mismatch', SHA, 'b' * 40))
        self.assertEqual(g.deployment(dict(v, dirty=True), dict(commit_sha=SHA))['state'], 'not_verified')
        self.assertEqual(g.deployment(v, dict(commit_sha=SHA, evidence='label'))['state'], 'deployed_verified')
        gr = self.scan(run=fake_git(dirty=' M app/web.py\n'))
        self.assertTrue(gr['version_ref']['dirty'])
        self.assertEqual(gr['deployment']['state'], 'not_verified')       # граф кода сам по себе — не развёртывание


class Mapping(Base):
    def test_project_confirmed_ambiguous_unassigned(self):                # TTN-100, ARCH-FR-04.1
        one = dict(shop=dict(path=self.root))
        self.assertEqual(s.map_project(self.root, one)['method'], 'confirmed_path')
        two = dict(shop=dict(path=self.root), shop2=dict(path=self.root.upper()))
        amb = s.map_project(self.root, two)
        self.assertEqual((amb['project'], amb['method'], amb['candidates']), (None, 'ambiguous', ['shop', 'shop2']))
        similar = dict(repo=dict(path=self.root + '-copy'))
        self.assertEqual(s.map_project(self.root, similar)['method'], 'unassigned')
        self.assertEqual(self.scan(projects=one)['project']['project'], 'shop')

    def test_runtime_link_only_when_confirmed(self):                      # TTN-100, NET-AC-077
        gr = self.scan()
        route = self.by(gr, 'route')['app.web:GET /items']
        ev = m.evidence('observed_socket', 'windows:tcp', 'c')
        b = m.binding('local', 'host', 'tcp', '127.0.0.1', 8000, ev['id'])
        c = m.container('d1', 'abc', 'shop_api', 'running')
        inv = m.snapshot([ev, b, c], [m.source('windows:tcp', 'available')])
        self.assertEqual(s.link_runtime(gr, inv, [])['links'], [])         # по имени и порту связи нет
        ok = s.link_runtime(gr, inv, [dict(code=route['id'], runtime=b['id'], by='owner', at='2026-11-08')])
        self.assertEqual(len(ok['links']), 1)
        self.assertEqual((ok['links'][0]['method'], ok['links'][0]['deployment']['state']), ('user_confirmed', 'not_verified'))
        amb = s.link_runtime(gr, inv, [dict(code=route['id'], runtime=b['id']), dict(code=route['id'], runtime=c['id'])])
        self.assertEqual((amb['links'], amb['ambiguous'][0]['candidates']), ([], sorted([b['id'], c['id']])))
        bad = s.link_runtime(gr, inv, [dict(code=route['id'], runtime='binding-0000')])
        self.assertEqual(len(bad['refused']), 1)


class Schema(unittest.TestCase):
    def test_edge_types_enforce_endpoints(self):                          # TTN-092
        mod = g.node('module', 'r', 'a'); fn = g.node('function', 'r', 'a.f'); fn2 = g.node('function', 'r', 'a.g')
        route = g.node('route', 'r', 'GET /x')
        p = g.provenance('r', SHA, 'a.py', 1, s.PARSER)
        self.assertEqual(g.edge('imports', mod, mod, p)['type'], 'imports')
        with self.assertRaisesRegex(ValueError, 'недопустимо'):
            g.edge('imports', fn, fn2, p)                                  # импорт — между модулями
        with self.assertRaisesRegex(ValueError, 'недопустимо'):
            g.edge('handles_route', route, fn, p)                          # направление: код → маршрут
        with self.assertRaisesRegex(ValueError, 'runtime'):
            g.edge('network_connects', fn, fn2, p)
        with self.assertRaisesRegex(ValueError, 'runtime'):
            g.edge('calls_observed', fn, fn2, p)
        with self.assertRaisesRegex(ValueError, 'provenance'):
            g.edge('calls_static', fn, fn2, g.provenance('r', SHA, None, 1, s.PARSER))
        with self.assertRaisesRegex(ValueError, 'verified'):
            g.edge('calls_static', fn, fn2, p, resolution='ambiguous', quality='verified')
        # Импорт и вызов между одними узлами — разные связи (ARCH-FR-01.5).
        self.assertNotEqual(g.edge('calls_static', mod, fn, p)['id'], g.edge('contains', mod, fn, p)['id'])


if __name__ == '__main__':
    unittest.main()
