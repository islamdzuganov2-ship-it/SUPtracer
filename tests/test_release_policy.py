"""Owner-grant на локальный релиз (TT-104; ТЗ TT-U-001: TT3-014, FR-003, AC-FR03).

Слияние в main — только по подписанному решению владельца: одобрению конкретного коммита тестовой
ветки или гранту полной автономии. Негативная матрица: ручная запись одобрения в workflow.json,
одобрение другой задачи, новый коммит после «да», истёкший и подделанный грант, выключенная
автономия, аварийная остановка, вызов владельческих операций из CLI, MCP и без источника.
"""
import json
import os
from datetime import timedelta

from base import TOOLS  # noqa: F401 — пути к tools до импорта модулей системы
import mcp_server
import owner_auth as oa
import release_policy as rp
import test_pipeline
import workflow as w
from test_pipeline import git


class ReleasePolicyTests(test_pipeline.PipelineTests):
    """Использует сценарий конвейера test_pipeline: задача доходит до «Деплой в main»."""

    def to_deploy_main(self):
        self.to_testing()
        self.do_role('qa', 'testing', signoff=True)
        gate = self.claim('teamlead', gate='release')
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='QA Sign-off есть, деплой в тест чистый, риски релиза приняты'))
        self.assertEqual(self.task()['pipeline']['stage'], 'deploy_main')

    def forge(self, approval):
        s = w.state()
        s['tasks'][self.ref]['pipeline'].setdefault('approvals', {})['release'] = approval
        w.save('workflow.json', s)

    def released(self):
        return 'x = 3' in git('show', 'main:app.py', cwd=self.repo)

    def devops_queued(self):
        return any(r['ref'] == self.ref and r.get('role') == 'devops' and r.get('stage') == 'deploy_main'
                   and r['status'] == 'queued' for r in w.state()['runs'])

    def ctx(self):
        import pipeline as P
        return P.Context(self.task(), w.read('projects.json', {})['projects']['alpha-api'])

    def bell(self):
        return [n['kind'] for n in w.notifications(w.state(), w.snapshot()['tasks'])]

    # --- кто может одобрить

    def test_agent_origins_cannot_approve_release(self):
        self.to_deploy_main()
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(dict(op='approve_release', ref=self.ref))                        # без источника — агент
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(dict(op='approve_release', ref=self.ref), origin='agent')
        with self.assertRaises(ValueError):
            w.action(dict(op='approve_release', ref=self.ref), origin='admin')         # выдуманный источник
        req = os.path.join(self.tmp, 'req.json')
        with open(req, 'w', encoding='utf-8') as f:
            json.dump(dict(op='approve_release', ref=self.ref), f)
        self.assertEqual(w.main(['request', req]), 1)                                  # CLI pm.py work request
        r = mcp_server.call_tool('tt_work', dict(op='approve_release', ref=self.ref))
        self.assertTrue(r['isError'])
        self.assertFalse((self.task()['pipeline'].get('approvals') or {}).get('release'))

    # --- подделка и повтор журнала

    def test_hand_written_approval_does_not_release(self):
        self.to_deploy_main()
        self.forge(dict(at=w.now().isoformat(), by='владелец'))                       # как писалось до TT-104
        import orchestra
        orchestra.tick()
        self.assertIn('release', self.bell())                                          # колокольчик всё ещё спрашивает
        self.assertFalse(self.devops_queued())                                         # DevOps не запущен
        import deploy
        ok, why = deploy.run('deploy_main', self.ctx())                                # и само слияние отказывает
        self.assertFalse(ok); self.assertIn('не подписано', why)
        self.assertFalse(self.released())

    def test_forged_signature_and_other_task_approval_rejected(self):
        self.to_deploy_main()
        head = git('rev-parse', 'test', cwd=self.repo)
        good = rp.approval(self.ref, head, w.now())
        pipe = self.task()['pipeline']
        bad_sig = dict(good, sig='0' * 64)
        other = rp.approval('alpha-api/AL-99', head, w.now())                          # перенос с другой задачи
        tampered = dict(good, sha='f' * 40)                                            # подпись не сходится
        for a, why in ((bad_sig, 'не подписано'), (other, 'AL-99'), (tampered, 'не подписано')):
            ok, reason = rp.release_ok(w.state(), dict(pipe, approvals=dict(release=a)), self.ref, 'alpha-api', 'main', head, w.now())
            self.assertFalse(ok, why); self.assertIn(why, reason)
        self.assertTrue(rp.release_ok(w.state(), dict(pipe, approvals=dict(release=good)), self.ref, 'alpha-api', 'main', head, w.now())[0])

    def test_new_commit_after_approval_needs_new_approval(self):
        self.to_deploy_main()
        w.action(dict(op='approve_release', ref=self.ref), origin='owner')
        approved = self.task()['pipeline']['approvals']['release']
        self.assertTrue(oa.verify(approved))
        self.assertEqual(approved['sha'], git('rev-parse', 'test', cwd=self.repo))
        # После «да» в тестовую ветку попал ещё коммит — владелец его не видел.
        wt = str(self.repo) + '.worktrees/deploy-test'
        with open(os.path.join(wt, 'extra.txt'), 'w', encoding='utf-8') as f:
            f.write('после одобрения\n')
        git('add', '.', cwd=wt); git('commit', '-qm', 'после одобрения', cwd=wt)
        self.do_role('devops', 'deploy_main')                                          # сдача принята, слияние — отказ
        self.assertFalse(self.released())
        self.assertFalse((self.task()['pipeline'].get('approvals') or {}).get('release'))   # устаревшее «да» снято
        self.assertIn('release', self.bell())                                          # колокольчик спрашивает снова
        self.assertIn('approval_void', [e['kind'] for e in w.state()['teamlead'] if e['ref'] == self.ref])
        w.action(dict(op='approve_release', ref=self.ref), origin='owner')            # новое «да» — на новом коммите
        dor = {x['id']: x for x in self.task()['pipeline']['stages']['deploy_main']['dor']}
        self.assertFalse(dor['dor-evidence']['ok'])                                    # SEC-011: CI на новом коммите не было
        import ci
        ci.run_pipeline('alpha-api', w.read('projects.json', {})['projects']['alpha-api'], wt, 'test', 'test', ref=self.ref)
        import orchestra; orchestra.tick()
        r = self.do_role('devops', 'deploy_main')
        self.assertTrue(r['accepted'], r)
        self.assertTrue(self.released())

    # --- грант полной автономии

    def full(self):
        w.action(dict(op='settings', changes=dict(autonomy='full', consent=True)), origin='owner')

    def test_full_autonomy_issues_scoped_signed_grant(self):
        self.full()
        g = w.state()['policy']['grant']
        self.assertTrue(oa.verify(g))
        self.assertEqual((g['profile'], g['release_target'], g['external_deploy_allowed'], g['branches']),
                         ('local_release', 'local_main', False, ['main']))
        self.assertGreater(g['expires_at'], w.now().isoformat())
        self.assertFalse(rp.external_push_allowed(w.state(), 'alpha-api', w.now()))   # AC-42
        w.action(dict(op='settings', changes=dict(autonomy='step')), origin='owner')
        self.assertTrue(w.state()['policy']['grant']['revoked_at'])
        self.assertFalse(rp.active_grant(w.state(), 'alpha-api', 'main', w.now())[0])

    def test_expired_or_tampered_grant_does_not_auto_release(self):
        self.full()
        s = w.state()
        s['policy']['grant']['expires_at'] = (w.now() + timedelta(days=3650)).isoformat()   # продлил сам агент
        w.save('workflow.json', s)
        self.to_deploy_main()
        t = self.task()
        self.assertFalse((t['pipeline'].get('approvals') or {}).get('release'))
        self.assertEqual(t['status_note'], 'Ждёт одобрения релиза владельцем')
        self.assertIn('grant', self.bell())
        self.assertFalse(self.devops_queued())
        import deploy
        self.assertFalse(deploy.run('deploy_main', self.ctx())[0])
        self.assertFalse(self.released())

    def test_grant_checks(self):
        at = w.now()
        g = rp.issue_grant(at, projects=['alpha-api'])
        self.assertTrue(rp.grant_check(g, 'alpha-api', 'main', at)[0])
        self.assertIn('не покрывает проект', rp.grant_check(g, 'beta-tool', 'main', at)[1])
        self.assertIn('не покрывает ветку', rp.grant_check(g, 'alpha-api', 'release', at)[1])
        self.assertIn('истёк', rp.grant_check(g, 'alpha-api', 'main', at + timedelta(days=31))[1])
        self.assertIn('отозван', rp.grant_check(dict(g, revoked_at=at.isoformat()), 'alpha-api', 'main', at)[1])
        self.assertIn('не подписан', rp.grant_check(dict(g, external_deploy_allowed=True), 'alpha-api', 'main', at)[1])
        self.assertIn('гранта полной автономии нет', rp.grant_check(None, 'alpha-api', 'main', at)[1])

    def test_kill_switch_blocks_release_even_with_valid_approval(self):
        self.to_deploy_main()
        w.action(dict(op='approve_release', ref=self.ref), origin='owner')
        w.action(dict(op='stop_all', reason='проверка остановки'))
        head = git('rev-parse', 'test', cwd=self.repo)
        ok, why = rp.release_ok(w.state(), self.task()['pipeline'], self.ref, 'alpha-api', 'main', head, w.now())
        self.assertFalse(ok); self.assertIn('Аварийная остановка', why)
        import deploy
        ok, why = deploy.run('deploy_main', self.ctx())
        self.assertFalse(ok); self.assertIn('Аварийная остановка', why)
        self.assertFalse(self.released())

    def test_no_owner_key_means_no_valid_decision(self):
        rec = dict(kind='release_approval', ref='alpha-api/AL-01', sha='a' * 40)
        signed = oa.sign(rec)
        self.assertTrue(oa.verify(signed))
        os.remove(oa.key_path())                                                       # ключ удалён — всё отозвано
        self.assertFalse(oa.verify(signed))
        self.assertIsNone(oa.board_token())
        self.assertFalse(oa.check_token(''))


# Унаследованные тесты конвейера здесь не повторяются: берутся только сценарии и помощники.
for _name in [n for n in dir(test_pipeline.PipelineTests) if n.startswith('test_')]:
    if _name not in ReleasePolicyTests.__dict__:
        setattr(ReleasePolicyTests, _name, None)
