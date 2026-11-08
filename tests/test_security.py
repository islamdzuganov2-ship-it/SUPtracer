"""Пакет безопасности SEC-1.0 (TT-120…TT-131): тест-кейсы SEC-TC, которые проверяются кодом.

Каждый тест назван по кейсу. Кейсы, которые здесь не проверяются (нужен отдельный пользователь Windows,
внешний сканер, сетевой стенд), перечислены в docs/БЕЗОПАСНОСТЬ.md со статусом NOT TESTED — честно, а не «зелёные».
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from base import DataCase
import ci
import owner_auth as oa
import path_policy
import pipeline as P
import sandbox_env
import security_scan as ss
import security_triage as st
import session_auth
import trusted_oracle as to
import workflow as w


def git(*args, cwd):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding='utf-8', check=True).stdout.strip()


class Repo(DataCase):
    """Репозиторий проекта: main, тестовая ветка, база разработка и ветка задачи."""
    def setUp(self):
        super().setUp()
        self.repo = Path(self.tmp) / 'repo'; self.repo.mkdir()
        git('init', '-q', '-b', 'main', cwd=self.repo)
        (self.repo / 'tests' / 'acceptance').mkdir(parents=True)
        (self.repo / 'tests' / 'acceptance' / 'test_ok.py').write_text('def test_ok():\n    assert 1 + 1 == 2\n', encoding='utf-8')
        (self.repo / 'app.py').write_text('def f():\n    return 1\n', encoding='utf-8')
        git('add', '.', cwd=self.repo); git('commit', '-qm', 'init', cwd=self.repo)
        git('branch', 'разработка', cwd=self.repo); git('branch', 'тестирование', cwd=self.repo)
        git('checkout', '-q', '-b', 'ai/claude/al-09', 'разработка', cwd=self.repo)
        self.project = dict(path=str(self.repo), branches=dict(base='разработка', test='тестирование', main='main'))

    def commit(self, rel, text, msg='правка'):
        p = self.repo / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding='utf-8')
        git('add', '.', cwd=self.repo); git('commit', '-qm', msg, cwd=self.repo)

    def intact(self, pipe=None):
        return to.intact(pipe or {}, 'alpha-api/AL-09', str(self.repo), 'разработка', 'ai/claude/al-09', self.project)


class OracleTests(Repo):
    def test_sec_tc_05_agent_edits_acceptance_test_is_refused(self):
        self.commit('app.py', 'def f():\n    return 2\n')
        self.assertTrue(self.intact()[0])                                   # своя правка кода — можно
        self.commit('tests/acceptance/test_ok.py', 'def test_ok():\n    assert True\n', 'ослабить тест')
        ok, detail, files = self.intact()
        self.assertFalse(ok)
        self.assertEqual(files, ['tests/acceptance/test_ok.py'])
        self.assertIn('без одобрения владельца', detail)

    def test_sec_tc_05b_owner_signed_change_passes_and_new_change_needs_new_approval(self):
        self.commit('tests/acceptance/test_ok.py', 'def test_ok():\n    assert 2 * 2 == 4\n', 'новое требование')
        files, mb = to.changed(str(self.repo), 'разработка', 'ai/claude/al-09', self.project)
        pipe = dict(approvals=dict(oracle=to.approve_change('alpha-api/AL-09', to.change_digest(str(self.repo), mb, 'ai/claude/al-09', files), w.now())))
        self.assertTrue(self.intact(pipe)[0])
        forged = dict(pipe['approvals']['oracle'], digest='sha256:0')         # подпись не от этого содержимого
        self.assertFalse(self.intact(dict(approvals=dict(oracle=forged)))[0])
        self.commit('tests/acceptance/test_ok.py', 'def test_ok():\n    assert True\n', 'после одобрения')
        self.assertFalse(self.intact(pipe)[0])                              # дифф изменился — нужно новое «да»
        other = dict(approvals=dict(oracle=dict(pipe['approvals']['oracle'], ref='alpha-api/AL-01')))
        self.assertFalse(self.intact(other)[0])

    def test_sec_tc_06_protected_paths_are_owner_configurable(self):
        proj = dict(self.project, oracle_paths=['spec/'])
        self.commit('tests/acceptance/test_ok.py', 'x = 1\n')
        self.assertTrue(to.intact({}, 'r', str(self.repo), 'разработка', 'ai/claude/al-09', proj)[0])
        self.commit('spec/contract.md', '# контракт\n')
        self.assertFalse(to.intact({}, 'r', str(self.repo), 'разработка', 'ai/claude/al-09', proj)[0])

    def test_developer_dod_and_release_dor_contain_trusted_checks(self):
        self.assertIn('dev-oracle', [i['id'] for i in P.role_dod('developer', 'development')])
        self.assertIn('dor-evidence', [i['id'] for i in P.DOR['deploy_main']])
        self.assertTrue(w.needs_owner(dict(op='approve_oracle', ref='x')))


class EvidenceTests(Repo):
    def setUp(self):
        super().setUp()
        git('checkout', '-q', 'тестирование', cwd=self.repo); git('merge', '-q', '--no-ff', '-m', 'в тест', 'ai/claude/al-09', cwd=self.repo)
        self.head = ci.head_sha(str(self.repo), 'refs/heads/тестирование')
        git('checkout', '-q', 'ai/claude/al-09', cwd=self.repo)
        self.ctx = SimpleNamespace(t=dict(ref='alpha-api/AL-09', project='alpha-api'), repo=str(self.repo), branch='ai/claude/al-09',
                                   branches=self.project['branches'], project=self.project)

    def ci_run(self, status='passed', sha=None, steps=True):
        ci.record(dict(id='CI-%s-%s' % (status, os.urandom(2).hex()), project='alpha-api', ref='alpha-api/AL-09', branch='тестирование',
                       scope='test', status=status, sha_full=sha or self.head, sha=(sha or self.head)[:7], started=w.now().isoformat(),
                       steps=[dict(name='t', status='passed', optional=False, seconds=1)] if steps else []))

    def test_sec_tc_02_03_04_26_release_needs_passed_ci_on_exact_test_sha(self):
        pipe = {}
        ok, detail = P.release_evidence(self.ctx, pipe)
        self.assertFalse(ok); self.assertIn('CI', detail)
        self.ci_run(sha='0' * 40)                                           # зелёный, но на другом коммите
        self.assertFalse(P.release_evidence(self.ctx, pipe)[0])
        self.ci_run('not_configured', steps=False)
        self.assertFalse(P.release_evidence(self.ctx, pipe)[0])
        self.ci_run()
        ok, detail = P.release_evidence(self.ctx, pipe)
        self.assertTrue(ok, detail)
        ev = pipe['evidence'][-1]
        for k in ('task_ref', 'run_id', 'commit_sha', 'test_bundle_sha', 'policy_sha', 'runner_version', 'check_id', 'status',
                  'started_at', 'completed_at', 'log_digest', 'source', 'environment_fingerprint', 'waiver_ref'):
            self.assertIn(k, ev)
        self.assertEqual((ev['commit_sha'], ev['status'], ev['source']), (self.head, 'passed', 'server'))

    def test_sec_tc_01_agent_text_cannot_replace_evidence(self):
        self.ci_run()
        pipe = dict(evidence=[dict(commit_sha=self.head, status='passed', source='agent')])   # «доказательство» от агента
        self.commit('tests/acceptance/test_ok.py', 'def test_ok():\n    pass\n', 'ослабить')
        ok, detail = P.release_evidence(self.ctx, pipe)
        self.assertFalse(ok); self.assertIn('эталон', detail)
        self.assertEqual([e['source'] for e in pipe['evidence']], ['server'])          # запись агента вытеснена

    def test_sec_tc_22_27_high_finding_blocks_release_and_only_owner_waives(self):
        self.ci_run()
        self.commit('app.py', 'import subprocess\nsubprocess.run("ls " + x, shell=True)\n', 'опасно')
        git('checkout', '-q', 'тестирование', cwd=self.repo); git('merge', '-q', '--no-ff', '-m', 'в тест', 'ai/claude/al-09', cwd=self.repo)
        head = ci.head_sha(str(self.repo), 'refs/heads/тестирование')
        ss.scan(str(self.repo), 'alpha-api', sha=head, settings={})
        git('checkout', '-q', 'ai/claude/al-09', cwd=self.repo)
        self.ci_run(sha=head)
        ok, detail = P.release_evidence(self.ctx, {})
        self.assertFalse(ok); self.assertIn('PY-SHELL', detail)
        key = next(f['key'] for f in ss.latest('alpha-api')['findings'] if f['rule'] == 'PY-SHELL')
        with self.assertRaises(ValueError):
            st.decide(key, 'false_positive', 'агент считает, что это безопасно', oa.AGENT)
        st.decide(key, 'accepted_risk', 'команда фиксированная, вход проверен', oa.OWNER)
        self.assertTrue(P.release_evidence(self.ctx, {})[0])


class ScanTests(Repo):
    def test_sec_tc_14_builtin_rules_and_secret_masking(self):
        secret = 'sk-' + 'A1b2C3d4E5f6G7h8I9j0K1l2'
        self.commit('cfg.py', 'API_KEY = "%s"\nimport pickle\npickle.loads(b)\nimport requests\nrequests.get(u, verify=False)\n' % secret)
        r = ss.scan(str(self.repo), 'alpha-api', settings={})
        rules = {f['rule'] for f in r['findings']}
        self.assertTrue({'SECRET', 'PY-PICKLE', 'PY-TLS'} <= rules, rules)
        raw = (Path(self.data) / 'security' / 'scans.json').read_text(encoding='utf-8')
        self.assertNotIn(secret, raw)                                       # значение секрета не записано

    def test_sec_tc_23_missing_scanner_is_not_run_and_required_blocks(self):
        with mock.patch('shutil.which', return_value=None):
            r = ss.scan(str(self.repo), 'alpha-api', settings={})
        self.assertEqual(r['scanners']['bandit']['status'], 'not_run')
        self.assertEqual(r['scanners']['builtin']['status'], 'ran')
        why = st.blocking('alpha-api', settings=dict(security=dict(required=['bandit'])))
        self.assertTrue(why and 'bandit' in why[0], why)
        self.assertEqual(st.blocking('alpha-api', settings={}), [])

    def test_sec_tc_29_findings_unique_and_old_debt_scoped(self):
        self.commit('legacy.py', 'import os\nos.system(cmd)\nimport subprocess\nsubprocess.call(c, shell=True)\n')
        r1 = ss.scan(str(self.repo), 'alpha-api', settings={}); r2 = ss.scan(str(self.repo), 'alpha-api', settings={})
        self.assertEqual([f['key'] for f in r1['findings']], [f['key'] for f in r2['findings']])     # повтор — те же ключи
        self.assertEqual(len({f['key'] for f in r1['findings']}), len(r1['findings']))
        self.assertTrue(st.blocking('alpha-api', settings={}))
        self.assertEqual(st.blocking('alpha-api', files=['app.py'], settings={}), [])

    def test_triage_is_owner_only_through_action(self):
        with self.assertRaisesRegex(ValueError, 'владел'):
            w.action(dict(op='security', action='triage', key='k', verdict='false_positive', why='это тестовые данные'))
        self.assertIn('projects', w.action(dict(op='security', action='snapshot')))


class EnvTests(DataCase):
    def test_sec_tc_15_child_env_drops_foreign_secrets(self):
        base = dict(PATH='x', SYSTEMROOT='C:/Windows', AWS_SECRET_ACCESS_KEY='a', GITHUB_TOKEN='g', ANTHROPIC_API_KEY='k',
                    OPENAI_API_KEY='o', TASKTREKER_OWNER_TOKEN='t', TASKTREKER_DATA='d', DATABASE_PASSWORD='p')
        tests = sandbox_env.minimal(base=base)
        self.assertEqual(sorted(tests), ['PATH', 'SYSTEMROOT', 'TASKTREKER_DATA'])
        claude = sandbox_env.minimal(base=base, provider='claude')
        self.assertIn('ANTHROPIC_API_KEY', claude); self.assertNotIn('OPENAI_API_KEY', claude)
        self.assertNotIn('TASKTREKER_OWNER_TOKEN', claude); self.assertNotIn('GITHUB_TOKEN', claude)
        self.assertEqual(sandbox_env.leaked(tests), [])

    def test_ci_and_autocheck_children_use_minimal_env(self):
        with mock.patch.dict(os.environ, dict(SUPER_SECRET_TOKEN='zzz')):
            self.assertNotIn('SUPER_SECRET_TOKEN', ci._child_env())


class PathTests(DataCase):
    def test_sec_tc_10_11_ingest_outside_roots_is_refused(self):
        root = Path(self.tmp) / 'proj'; root.mkdir(); (root / 'note.md').write_text('# x\n', encoding='utf-8')
        projects = dict(p=dict(path=str(root)))
        self.assertTrue(path_policy.check(str(root / 'note.md'), self.data, projects))
        outside = Path(self.tmp) / 'other'; outside.mkdir(); (outside / 'secret.md').write_text('# s\n', encoding='utf-8')
        for bad in (str(outside / 'secret.md'), r'\\server\share\x.md', str(root / '..' / 'other' / 'secret.md'), 'C:/'):
            with self.assertRaises(ValueError, msg=bad):
                path_policy.check(bad, self.data, projects, allow_temp_files=False)
        (root / 'run.exe').write_bytes(b'MZ')
        with self.assertRaises(ValueError):
            path_policy.check(str(root / 'run.exe'), self.data, projects)


class SessionTests(DataCase):
    def test_sec_tc_07_08_cross_site_and_foreign_origin_rejected(self):
        port = 8765
        ok = {'Origin': 'http://127.0.0.1:%d' % port, 'Sec-Fetch-Site': 'same-origin'}
        self.assertIsNone(session_auth.origin_ok(ok, port))
        for h in ({'Origin': 'http://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'http://127.0.0.1:9999'},
                  {'Origin': 'null'}):
            self.assertIsNotNone(session_auth.origin_ok(h, port), h)
        self.assertFalse(session_auth.check(None)); self.assertFalse(session_auth.check('0' * 64))
        self.assertTrue(session_auth.check(session_auth.TOKEN))
        self.assertIn(b'tt-session', session_auth.inject(b'<head></head>'))


class ModelTests(DataCase):
    def gguf(self, name='m.gguf'):
        from test_learning import gguf_bytes
        f = Path(self.tmp) / name; f.write_bytes(gguf_bytes() + b'\0' * 64); return f

    def test_sec_tc_18_corrupt_or_unsafe_gguf_is_refused_without_crash(self):
        import gguf_probe
        f = self.gguf()
        for bad in ('\\server\share\m.gguf', str(f.with_suffix('.bin')), str(Path(self.tmp) / 'absent.gguf')):
            with self.assertRaises(gguf_probe.BadGGUF, msg=bad):
                gguf_probe.inspect(bad)
        with self.assertRaises(gguf_probe.BadGGUF):
            gguf_probe.quarantine(str(f), max_bytes=10)
        f.write_bytes(b'GGUF' + os.urandom(200))                                   # мусор после сигнатуры
        with self.assertRaises(gguf_probe.BadGGUF):
            gguf_probe.inspect(str(f))

    def test_sec_tc_20_weights_or_runtime_changed_after_registration_refuse_activation(self):
        import learn, local_mimo
        f = self.gguf(); rt = Path(self.tmp) / 'llama-server.exe'; rt.write_bytes(b'MZ runtime v1')
        cfg = learn.register_gguf(str(f), 'http://127.0.0.1:8080', 'owner', runtime_path=str(rt))
        cid = cfg['model_config_id']
        self.assertTrue(local_mimo.activate(cid, 'owner', override='тестовая конфигурация без рантайма'))
        rt.write_bytes(b'MZ runtime patched')
        with self.assertRaisesRegex(ValueError, 'рантайм'):
            local_mimo.activate(cid, 'owner')
        rt.write_bytes(b'MZ runtime v1'); f.write_bytes(f.read_bytes() + b'x')
        with self.assertRaisesRegex(ValueError, 'веса'):
            local_mimo.activate(cid, 'owner')

    def test_runtime_pin_reaches_config_through_owner_action(self):
        # Регистрация с доски/CLI (`pm.py learn model-gguf … --runtime`) пинует хеш рантайма, а не только API функции.
        import workflow as w, hashlib
        f = self.gguf(); rt = Path(self.tmp) / 'llama-server.exe'; rt.write_bytes(b'MZ runtime v1')
        cfg = w.action(dict(op='learn', action='register_gguf', path=str(f), endpoint='http://127.0.0.1:8080',
                            runtime_path=str(rt)), origin='owner')
        self.assertEqual(cfg['runtime_sha256'], 'sha256:' + hashlib.sha256(b'MZ runtime v1').hexdigest())


class TrainingTests(DataCase):
    def test_sec_tc_21_unverified_example_goes_to_quarantine(self):
        import learning_gate as lg
        events = [dict(event_id='evt_1', event_type='attempt_rejected', project='alpha-api'),
                  dict(event_id='evt_2', event_type='attempt_accepted', project='alpha-api'),
                  dict(event_id='evt_3', event_type='attempt_accepted', project='beta-tool')]
        out = lg.quarantine([dict(event_id='evt_2', verified=True), dict(event_id='evt_1', verified=True),
                             dict(event_id='evt_x', verified=True), dict(event_id='evt_2', verified=False),
                             dict(event_id='evt_3', verified=True)], project='alpha-api', events=events)
        self.assertEqual([x['event_id'] for x in out['accepted']], ['evt_2'])
        self.assertEqual(len(out['quarantined']), 4)
        self.assertFalse(out['training_started'])


class McpScopeTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        import importlib, memory
        self.mem = memory
        self.a = memory.new_record('Решение альфы о кеше', 'кеш ответов на 5 минут', project='alpha-api', kind='решение')
        self.b = memory.new_record('Решение беты о кеше', 'кеш беты: секретная деталь', project='beta-tool', kind='решение')
        memory.save(self.a); memory.save(self.b); memory.build_index()
        import mcp_server; self.mcp = mcp_server

    def call(self, name, **args):
        return self.mcp.call_tool(name, args)

    def test_sec_tc_12_project_session_cannot_read_or_write_foreign_memory(self):
        with mock.patch.dict(os.environ, dict(TASKTREKER_MCP_PROJECT='alpha-api')):
            self.assertTrue(self.call('tt_recall', query=self.b['id'])['isError'])
            self.assertFalse(self.call('tt_recall', query=self.a['id'])['isError'])
            self.assertTrue(self.call('tt_search', query='кеш', project='beta-tool')['isError'])
            out = self.call('tt_search', query='кеш')['content'][0]['text']
            self.assertNotIn(self.b['id'], out)
            self.assertTrue(self.call('tt_remember', project='beta-tool', title='чужое', text='запись в чужой проект')['isError'])

    def test_sec_tc_13_role_session_tool_list_is_fixed(self):
        with mock.patch.dict(os.environ, dict(TASKTREKER_MCP_ROLE='1', TASKTREKER_MCP_PROJECT='alpha-api')):
            self.assertEqual(sorted(t['name'] for t in self.mcp.listed_tools()), ['tt_recall', 'tt_work'])
            for name, args in (('tt_ingest', dict(path='README.md')), ('tt_remember', dict(project='alpha-api', title='x', text='y')),
                               ('tt_search', dict(query='кеш'))):
                self.assertTrue(self.call(name, **args)['isError'], name)


class FreezeTests(EvidenceTests):
    def test_acceptance_changed_after_dev_start_blocks_release(self):        # 07: A1-02, AG-DEV-02, A5-05
        self.ci_run()
        self.ctx.t['acceptance'] = ['Выгрузка содержит все проекты', 'Файл открывается без ошибок']
        pipe = dict(ac_freeze=dict(digest=P.ac_digest(self.ctx.t['acceptance']), count=2))
        self.assertTrue(P.release_evidence(self.ctx, pipe)[0])
        self.ctx.t['acceptance'] = ['Выгрузка содержит хотя бы один проект']          # ослабили по ходу разработки
        ok, detail = P.release_evidence(self.ctx, pipe)
        self.assertFalse(ok); self.assertIn('критерии приёмки изменены', detail)
