"""Аудит 07, пакет Ж: воспроизведение запуска, смена политики, независимость тимлида, перепроверка дефектов,
runbook, совместимость LoRA, журнал нарушений, права CLI ролей, контракт роли поверх разных провайдеров."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import base  # noqa: F401 — путь к tools/
from test_pipeline import PipelineTests, git
import workflow as w
import orchestra as O
import pipeline as P

for _n in [n for n in dir(PipelineTests) if n.startswith('test_')]:
    setattr(PipelineTests, _n, None)


class Pipeline07(PipelineTests):
    def po(self):
        self.ref = self.idea()
        return self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                            needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))

    def test_replay_returns_stored_input_and_detects_tamper(self):          # A1-07
        self.ref = self.idea()
        run = self.claim('product_owner')
        r = w.action(dict(op='replay', ref=self.ref, run=run))
        self.assertTrue(r['input_hash'].startswith('sha256:'))
        self.assertTrue(r['input_intact'])
        self.assertEqual(r['input']['route']['model'], r['model'])
        self.assertEqual(r['identity']['policy_version'], w.POLICY['version'])
        s = w.state()
        x = next(x for x in s['runs'] if x['id'] == run)
        x['packet']['mission'] = 'подменённое задание'
        w.save('workflow.json', s)
        self.assertFalse(w.action(dict(op='replay', ref=self.ref, run=run))['input_intact'])
        self.assertEqual(w.input_hash({'a': 1, 'b': [2]}), w.input_hash({'b': [2], 'a': 1}))   # канонический JSON

    def test_policy_change_during_run_is_journaled(self):                  # A0-05
        self.ref = self.idea()
        run = self.claim('product_owner'); self.write('product_owner', 'idea')
        was = w.POLICY['version']
        try:
            w.POLICY['version'] = was + 1
            r = self.handoff('product_owner', 'idea', run, acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                             needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        finally:
            w.POLICY['version'] = was
        self.assertTrue(r['accepted'], r)
        drift = [e for e in self.task()['pipeline']['journal'] if e['kind'] == 'policy_drift']
        self.assertEqual(len(drift), 1)
        self.assertIn('политика %s → %s' % (was, was + 1), drift[0]['text'])
        self.assertTrue(any(h['kind'] == 'policy_drift' and h['run'] == run for h in self.task()['pipeline']['history']))

    def test_teamlead_gate_not_by_stage_author_when_possible(self):          # LEAD-04
        self.po()
        for role in ('system_analyst', 'solution_architect', 'qa'):
            self.assertTrue(self.do_role(role, 'analysis')['accepted'])
        gate = self.run_of('teamlead', gate='analysis')
        authors = O.stage_authors(self.task())
        self.assertTrue(authors)
        self.assertIn('independent', gate)
        if gate['independent']:
            self.assertNotIn(gate['agent'], authors)
        ev = [e for e in self.task()['pipeline']['journal'] if e['kind'] == 'independence']
        self.assertTrue(ev and 'авторы этапа' in ev[-1]['text'])

    def test_fixed_defect_verified_only_with_existing_test(self):           # QA-08
        self.to_testing()
        run = self.claim('qa'); self.write('qa', 'testing')
        w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                      reason='Пустой портфель роняет выгрузку: исключение вместо пустого файла',
                      defects=[dict(title='Выгрузка падает на пустом портфеле', severity='major', level='unit',
                                    test='tests/test_empty_export.py')]))
        dev = self.claim('developer')
        wt = Path(self.task()['pipeline']['worktree']['path'])
        (wt/'app.py').write_text('x = 5\n', encoding='utf-8'); git('commit', '-qam', 'fix', cwd=wt)
        self.assertTrue(self.handoff('developer', 'development', dev,
                                     fixes={'D-1': dict(method='code_to_spec', note='Пустой список → пустой CSV')})['accepted'])
        self.do_role('reviewer', 'review', verdict='approve')
        self.do_role('devops', 'deploy_test')
        r = self.do_role('qa', 'testing', signoff=True)
        self.assertFalse(r['accepted'])
        f = next(x for x in r['failed'] if x['id'] == 'qa-retest')
        self.assertIn('tests/test_empty_export.py', f['detail'])
        self.assertEqual(self.task()['pipeline']['defects'][0]['status'], 'fixed')       # не VERIFIED по слову QA

    def test_verified_defect_records_runner_and_sha(self):                  # QA-08: успешный путь
        self.to_testing()
        run = self.claim('qa'); self.write('qa', 'testing')
        w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                      reason='Пустой портфель роняет выгрузку: исключение вместо пустого файла',
                      defects=[dict(title='Выгрузка падает на пустом портфеле', severity='major', level='unit', test='TC-2')]))
        dev = self.claim('developer')
        wt = Path(self.task()['pipeline']['worktree']['path'])
        (wt/'app.py').write_text('x = 6\n', encoding='utf-8'); git('commit', '-qam', 'fix', cwd=wt)
        self.handoff('developer', 'development', dev, fixes={'D-1': dict(method='code_to_spec', note='Пустой список → пустой CSV')})
        self.do_role('reviewer', 'review', verdict='approve')
        self.do_role('devops', 'deploy_test')
        self.assertTrue(self.do_role('qa', 'testing', signoff=True)['accepted'])
        d = self.task()['pipeline']['defects'][0]
        self.assertEqual((d['status'], d['verified_by_runner']), ('verified', 'server-ci'))
        self.assertEqual(d['verified_sha'], git('rev-parse', 'test', cwd=self.repo).strip())

    def test_acceptance_survives_context_relay(self):                      # A4-04: «до и после компактизации»
        self.po()
        run = self.claim('system_analyst')
        before = w.action(dict(op='packet', ref=self.ref, run=run))
        w.action(dict(op='pause', reason='context', ref=self.ref, run=run, worker='w-' + run,
                      checkpoint='Разделы SRS 1–3 готовы, осталось НФТ и API; дальше раздел 4'))
        after = w.action(dict(op='packet', ref=self.ref, run=run))
        self.assertEqual(before['acceptance'], after['acceptance'])
        self.assertEqual(len(after['acceptance']), 2)             # паспорт строит сервер из задачи, не из контекста сессии
        self.assertEqual(before['dod'], after['dod'])

    def test_role_contract_same_across_providers(self):                     # A0-02: тестовый адаптер провайдера
        self.ref = self.idea()
        run = self.run_of('product_owner')
        t = self.task()
        t = dict(t, pipeline=O.materialize(w.state(), t) or t['pipeline'])
        packets = {a: O.role_packet(t, dict(run, agent=a), a, toc=False) for a in ('claude', 'codex')}
        stub = dict(packets['claude'])           # «провайдер-заглушка»: тот же контракт, другая модель
        for k in ('dod', 'schema', 'role'):
            self.assertEqual(packets['claude'][k], packets['codex'][k], k)
            self.assertEqual(stub[k], packets['codex'][k], k)
        # Проверка передачи не зависит от провайдера: одна и та же ошибка схемы — одна и та же причина.
        errs = set()
        for a in ('claude', 'codex', 'stub'):
            try:
                O.validate_handoff(dict(summary='Сдача без пунктов DoD', items=None, agent=a))
            except ValueError as e:
                errs.add(str(e))
        self.assertEqual(len(errs), 1)


del PipelineTests


class RunbookRule(unittest.TestCase):                                       # OPS-07
    def test_commands_and_version_required(self):
        import artifact_rules as ar
        ok = '## Развёртывание\n```\ngit merge --no-ff x\n```\n## Откат\n`git revert --no-edit HEAD`\nВерсия: v1.2.0\n'
        self.assertTrue(ar.runbook(ok, None, {})[0])
        ok_sha = ok.replace('v1.2.0', 'коммит a1b2c3d').replace('Версия:', 'Версия выпуска:') + 'version a1b2c3d\n'
        self.assertTrue(ar.runbook(ok_sha, None, {})[0])
        bad, why = ar.runbook('## Развёртывание\nразвернуть как обычно\n## Откат\nоткатить\n', None, {})
        self.assertFalse(bad)
        self.assertIn('отката', why); self.assertIn('версия', why)
        self.assertTrue(any(i['id'] == 'do-runbook-cmd' and i['check'] == 'rule:runbook' for i in P.role_dod('devops', 'deploy_test')))


class LoraGuard(unittest.TestCase):                                          # A10-04
    BASE = 'sha256:' + 'a' * 64

    def cfg(self, **kw):
        return dict(dict(model_id='m', quantization='Q4_K_M', runtime_name='llama.cpp', context_limit=8192,
                         endpoint='http://127.0.0.1:8089', weight_sha256=self.BASE, weight_path='w.gguf',
                         adapter_path='a.gguf', adapter_sha256='sha256:' + 'b' * 64, adapter_base_sha256=self.BASE), **kw)

    def inspect(self, arch_a='qwen2', emb_a=4096, kind='adapter'):
        def fn(path):
            if path == 'a.gguf':
                return dict(architecture=arch_a, meta={'general.type': kind, 'embedding_length': emb_a})
            return dict(architecture='qwen2', meta={'general.type': 'model', 'embedding_length': 4096})
        return fn

    def check(self, cfg, **kw):
        import local_mimo as lm
        return lm.adapter_problem(cfg, inspect_fn=self.inspect(**kw), sha_fn=lambda p: cfg['adapter_sha256'] if p == 'a.gguf' else '')

    def test_compatible_adapter_passes(self):
        self.assertIsNone(self.check(self.cfg()))

    def test_incompatible_rejected_before_load(self):
        self.assertIn('другой основе', self.check(self.cfg(adapter_base_sha256='sha256:' + 'c' * 64)))
        self.assertIn('архитектуры', self.check(self.cfg(), arch_a='llama'))
        self.assertIn('embedding_length', self.check(self.cfg(), emb_a=2048))
        self.assertIn('не адаптер', self.check(self.cfg(), kind='model'))
        import local_mimo as lm
        bad = self.cfg(); self.assertIn('изменился', lm.adapter_problem(bad, inspect_fn=self.inspect(), sha_fn=lambda p: 'sha256:x'))

    def test_spawn_refuses_incompatible_and_adds_lora(self):
        import mimo_supervisor as sup
        cfg = self.cfg(runtime_path='llama-server.exe', adapter_base_sha256='sha256:' + 'c' * 64)
        calls = []
        r = sup.ensure_runtime(cfg, health_fn=lambda c: dict(ok=False, reason='не отвечает'), free_fn=lambda: 99999,
                               popen=lambda *a, **k: calls.append(a), wait=0, sleep=lambda s: None)
        self.assertFalse(r['ok']); self.assertTrue(r.get('adapter')); self.assertEqual(calls, [])
        self.assertIn('--lora', sup.spawn_cmd(self.cfg(runtime_path='x.exe')))
        self.assertNotIn('--lora', sup.spawn_cmd(dict(self.cfg(runtime_path='x.exe'), adapter_path=None)))

    def test_register_requires_base_and_changes_id(self):
        import local_mimo as lm
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get('TASKTREKER_DATA'); os.environ['TASKTREKER_DATA'] = d
            try:
                plain = dict(model_id='m', quantization='Q4_K_M', runtime_name='llama.cpp', context_limit=8192,
                             endpoint='http://127.0.0.1:8089', weight_sha256=self.BASE)
                with self.assertRaisesRegex(ValueError, 'другой основе'):
                    lm.register(dict(plain, adapter_path='a.gguf', adapter_sha256='sha256:' + 'b' * 64,
                                     adapter_base_sha256='sha256:' + 'c' * 64), 'owner')
                with self.assertRaisesRegex(ValueError, 'adapter_base_sha256'):
                    lm.register(dict(plain, adapter_path='a.gguf', adapter_sha256='sha256:' + 'b' * 64), 'owner')
                self.assertNotEqual(lm.config_id(plain), lm.config_id(dict(plain, adapter_sha256='sha256:' + 'b' * 64,
                                                                           adapter_base_sha256=self.BASE)))
            finally:
                if old is None: os.environ.pop('TASKTREKER_DATA', None)
                else: os.environ['TASKTREKER_DATA'] = old


class SecAudit(base.DataCase):                                              # A2-08, E2E-R31
    def test_chain_detects_edit_and_delete(self):
        import sec_audit as sa
        for i in range(3):
            sa.record('zone_violation', 'файл %d' % i, run='RUN-%d' % i, agent='codex', op='handoff', ref='p/T-1')
        ok, d = sa.verify(); self.assertTrue(ok, d)
        lines = sa.path().read_text(encoding='utf-8').splitlines()
        row = json.loads(lines[1]); row['detail'] = 'подчищено'
        sa.path().write_text('\n'.join([lines[0], json.dumps(row, ensure_ascii=False), lines[2]]) + '\n', encoding='utf-8')
        ok, d = sa.verify(); self.assertFalse(ok); self.assertIn('запись 2', d)
        sa.path().write_text('\n'.join([lines[0], lines[2]]) + '\n', encoding='utf-8')
        ok, d = sa.verify(); self.assertFalse(ok); self.assertIn('разрыв', d)

    def test_agent_owner_action_is_audited(self):
        import sec_audit as sa
        self.projects()
        with self.assertRaises(ValueError):
            w.action(dict(op='settings', changes=dict(autonomy='full')))
        rows = sa.read()
        self.assertTrue(rows and rows[-1]['kind'] == 'owner_only_denied')
        self.assertTrue(sa.verify()[0])


class LauncherPolicy(unittest.TestCase):                                     # A2-01, AG-SECAG-03
    def test_read_only_roles_and_deny_rules(self):
        import launcher as L
        tools, deny = L.role_policy('teamlead')
        self.assertNotIn('Edit', tools); self.assertNotIn('Write', tools)
        self.assertIn('Bash(git push:*)', deny); self.assertIn('Read(**/.env)', deny)
        self.assertIn('Edit', L.role_policy('developer')[0])

    def test_codex_read_only_sandbox_without_auto_approve(self):
        import launcher as L
        old = os.environ.pop('TASKTREKER_LEAN', None)
        try:
            extra, _ = L.lean('codex', 'resolver')
            self.assertEqual(extra[-2:], ['-s', 'read-only'])
            cmd = L.headless_command('codex', 'codex.exe', 'gpt', 'high', 'prompt', '.', extra=extra)
            self.assertNotIn('--approve-for-me', cmd)
            res = L.headless_command('codex', 'codex.exe', 'gpt', 'high', 'prompt', '.', session='s1', resume=True, extra=extra)
            self.assertIn('sandbox_mode="read-only"', res); self.assertNotIn('-s', res)
            dev, _ = L.lean('codex', 'developer')
            self.assertIn('--approve-for-me', L.headless_command('codex', 'codex.exe', 'gpt', 'high', 'p', '.', extra=dev))
        finally:
            if old is not None: os.environ['TASKTREKER_LEAN'] = old


if __name__ == '__main__':
    unittest.main()
