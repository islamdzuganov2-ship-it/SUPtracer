"""Конвейер от идеи до main: жёсткие DoR/DoD, тимлид, ворота, одобрение владельца (TT-68, TT-69)."""
import os
import subprocess
import sys
from pathlib import Path
from base import DataCase
import pipeline as P
import workflow as w


def git(*args, cwd):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()


def doc_text(spec, role):
    """Документ, проходящий проверку раздела, размера и машинных пунктов роли."""
    body = ['# %s' % spec['path'].split('/')[-1]]
    for sec in spec['sections'] or ['Содержание']:
        body += ['', '## %s' % sec, 'Подробное описание раздела «%s» для задачи. ' % sec * 3]
    if role == 'product_owner':
        body += ['', '## Источники'] + ['- https://example.org/source-%d' % i for i in range(3)]
    body += ['', '```mermaid', 'sequenceDiagram', 'A->>B: запрос', '```',
             '', '| БТ | СТ | Тест |', '|---|---|---|', '| BR-1 | SR-1 | TC-1 |', '| BR-2 | SR-2 | TC-2 |']
    text = '\n'.join(body) + '\n'
    while len(text.encode('utf-8')) < spec['min_bytes']:
        text += 'Дополнительное пояснение, чтобы документ был содержательным.\n'
    return text


class PipelineTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': []})
        self.repo = Path(self.tmp)/'repos'/'alpha-api'
        git('init', '-q', '-b', 'main', cwd=self.repo)
        (self.repo/'app.py').write_text('x = 1\n', encoding='utf-8')
        git('add', '-A', cwd=self.repo); git('commit', '-q', '-m', 'init', cwd=self.repo)
        git('checkout', '-q', '-b', 'разработка', cwd=self.repo)
        d = w.read('projects.json', {}); d['projects']['alpha-api']['verify'] = [sys.executable, '-c', 'print(1)']
        w.save('projects.json', d)
        os.environ['TASKTREKER_PIPELINE_SYNC'] = '1'

    def tearDown(self):
        os.environ.pop('TASKTREKER_PIPELINE_SYNC', None)
        super().tearDown()

    # --- помощники «агентов»

    def idea(self):
        r = w.action(dict(op='idea', project='alpha-api', title='Экспорт портфеля в CSV',
                          text='Владелец хочет выгружать портфель в CSV для отчёта инвесторам', source='claude'))
        return r['ref']

    def task(self):
        return w.task_for(self.ref)

    def run_of(self, role, gate=None):
        return next(r for r in w.state()['runs'] if r['ref'] == self.ref and r.get('role') == role
                    and r['status'] == 'queued' and (gate is None or r.get('gate') == gate))

    def claim(self, role, gate=None):
        run = self.run_of(role, gate)
        model = w.action(dict(op='packet', ref=self.ref, run=run['id']))['route']['model']
        w.action(dict(op='claim', ref=self.ref, run=run['id'], worker='w-' + run['id'], model=model))
        return run['id']

    def write(self, role, stage, extra=()):
        wt = Path(self.task()['pipeline']['worktree']['path'])
        for spec in P.ROLES[role]['artifacts']:
            if spec.get('optional'): continue
            path = wt / P.art_path(self.task()['id'], spec['path'])
            if spec.get('dir'):
                path = path / 'ADR-001.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(doc_text(spec, role), encoding='utf-8')
        for name, text in extra:
            (wt/name).parent.mkdir(parents=True, exist_ok=True); (wt/name).write_text(text, encoding='utf-8')
        git('add', '-A', cwd=wt); git('commit', '-q', '--allow-empty', '-m', '%s: %s' % (role, stage), cwd=wt)

    def items(self, role, stage):
        out = {}
        for it in P.role_dod(role, stage):
            out[it['id']] = dict(status='na', evidence='Неприменимо для этой задачи: нет такого контура') if it.get('allow_na') \
                else dict(status='done', evidence='Сделано и проверено: подробности в артефактах задачи')
        return out

    def handoff(self, role, stage, run, **kw):
        return w.action(dict(op='handoff', ref=self.ref, run=run, worker='w-' + run, summary='Работа роли выполнена по DoD, артефакты в ветке',
                             items=self.items(role, stage), **kw))

    def do_role(self, role, stage, extra=(), **kw):
        run = self.claim(role); self.write(role, stage, extra)
        return self.handoff(role, stage, run, **kw)

    # --- сценарии

    def test_full_pipeline_idea_to_main(self):
        self.ref = self.idea()
        t = self.task()
        self.assertEqual((t['pipeline']['stage'], t['status']), ('idea', 'queued'))
        self.assertTrue(t['pipeline']['worktree']['branch'].startswith('task/'))
        po = self.run_of('product_owner')
        self.assertTrue(po['auto_launch'])
        # Без BRD передача не принимается: DoD железный.
        run = self.claim('product_owner')
        r = self.handoff('product_owner', 'idea', run)
        self.assertFalse(r['accepted'])
        self.assertIn('po-brd', {f['id'] for f in r['failed']})
        self.write('product_owner', 'idea')
        r = self.handoff('product_owner', 'idea', run, acceptance=['CSV содержит все проекты портфеля', 'Файл открывается в Excel без ошибок'],
                         needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса и запуска'))
        self.assertTrue(r['accepted'], r)
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'analysis')
        self.assertEqual(len(t['acceptance']), 2)
        queued = {x['role'] for x in w.state()['runs'] if x['ref'] == self.ref and x['status'] == 'queued'}
        self.assertEqual(queued, {'system_analyst', 'solution_architect'})          # дизайн и маркетинг не нужны
        for role in ('system_analyst', 'solution_architect'):
            self.assertTrue(self.do_role(role, 'analysis')['accepted'])
        self.assertEqual(self.task()['pipeline']['stage'], 'analysis')             # ждёт ворот тимлида
        gate = self.claim('teamlead', gate='analysis')
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='SRS, HLD и ADR согласованы с BRD, разработка возможна без трактовок'))
        self.assertEqual(self.task()['pipeline']['stage'], 'development')
        r = self.do_role('developer', 'development', extra=[('app.py', 'x = 2\n'), ('tests/test_app.py', 'assert True\n')])
        self.assertTrue(r['accepted'], r)
        self.assertEqual(self.task()['pipeline']['stage'], 'review')
        r = self.do_role('reviewer', 'review', verdict='approve')
        self.assertTrue(r['accepted'], r)
        self.assertEqual(self.task()['pipeline']['stage'], 'testing')
        self.assertEqual((self.repo/'app.py').read_text(encoding='utf-8'), 'x = 2\n')     # слито в разработку
        r = self.do_role('qa', 'testing', signoff=True)
        self.assertTrue(r['accepted'], r)
        self.assertEqual(self.task()['pipeline']['stage'], 'deploy_test')
        r = self.do_role('devops', 'deploy_test')
        self.assertTrue(r['accepted'], r)
        self.assertIn('x = 2', git('show', 'test:app.py', cwd=self.repo))          # тестовая ветка
        gate = self.claim('teamlead', gate='release')
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='QA Sign-off есть, деплой в тест чистый, риски релиза приняты'))
        t = self.task()
        self.assertEqual((t['pipeline']['stage'], t['status_note']), ('deploy_main', 'Ждёт одобрения релиза владельцем'))
        self.assertFalse(any(x['ref'] == self.ref and x['status'] == 'queued' for x in w.state()['runs']))
        w.action(dict(op='approve_release', ref=self.ref))
        r = self.do_role('devops', 'deploy_main')
        self.assertTrue(r['accepted'], r)
        t = self.task()
        self.assertEqual((t['pipeline']['stage'], t['status']), ('done', 'done'))
        self.assertIn('x = 2', git('show', 'main:app.py', cwd=self.repo))
        kinds = [e['kind'] for e in w.state()['teamlead'] if e['ref'] == self.ref]
        for k in ('idea', 'handoff', 'handoff_rejected', 'gate', 'stage_passed', 'approved', 'done'):
            self.assertIn(k, kinds)

    def test_reviewer_returns_to_developer_with_review(self):
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=True, marketing=False, reason='Нужен экран выгрузки, запуск не нужен'))
        roles = {x['role'] for x in w.state()['runs'] if x['ref'] == self.ref and x['status'] == 'queued'}
        self.assertIn('designer', roles)
        for role in ('system_analyst', 'solution_architect', 'designer'):
            self.do_role(role, 'analysis')
        gate = self.claim('teamlead', gate='analysis')
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve', notes='Анализ согласован, можно разрабатывать'))
        self.do_role('developer', 'development', extra=[('app.py', 'x = 3\n'), ('tests/test_app.py', 'assert True\n')])
        run = self.claim('reviewer')
        with self.assertRaisesRegex(ValueError, 'review.md'):
            w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                          reason='Нет обработки пустого портфеля: функция падает на пустом списке'))
        self.write('reviewer', 'review')
        with self.assertRaisesRegex(ValueError, 'возвращает только'):
            w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='idea', reason='x' * 40))
        w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                      reason='Нет обработки пустого портфеля: функция падает на пустом списке'))
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'development')
        dev = self.run_of('developer')
        packet = w.action(dict(op='packet', ref=self.ref, run=dev['id']))
        self.assertIn('пустого портфеля', packet['feedback'][0]['detail'])

    def test_rules_enforced(self):
        self.ref = self.idea()
        run = self.claim('product_owner'); self.write('product_owner', 'idea')
        items = self.items('product_owner', 'idea')
        items['po-brd'] = dict(status='na', evidence='Неприменимо по мнению агента')        # BRD «неприменимо» нельзя
        r = w.action(dict(op='handoff', ref=self.ref, run=run, worker='w-' + run, summary='Сдаю без BRD — так нельзя',
                          items=items, acceptance=['а' * 12, 'б' * 12], needs=dict(design=False, marketing=False, reason='не нужно совсем')))
        self.assertIn('po-brd', {f['id'] for f in r['failed']})
        with self.assertRaisesRegex(ValueError, 'конвейеру'):
            w.action(dict(op='complete', ref=self.ref, evidence='x'))
        with self.assertRaisesRegex(ValueError, 'конвейеру'):
            w.action(dict(op='status', ref=self.ref, status='review'))
        with self.assertRaisesRegex(ValueError, 'принявший'):
            w.action(dict(op='handoff', ref=self.ref, run=run, worker='чужой', summary='Сдаю за другого агента, так нельзя', items={}))
        with self.assertRaisesRegex(ValueError, 'Одобрение'):
            w.action(dict(op='approve_release', ref=self.ref))

    def test_escalation_blocks_and_teamlead_unblocks(self):
        self.ref = self.idea()
        run = self.claim('product_owner')
        w.action(dict(op='escalate', ref=self.ref, run=run, worker='w-' + run,
                      reason='Неясно, для каких инвесторов выгрузка: нужен ответ владельца'))
        t = self.task()
        self.assertEqual(t['status'], 'blocked')
        gate = self.claim('teamlead', gate='escalation')
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='Владелец уточнил: инвесторы фонда, формат как в прошлом отчёте'))
        t = self.task()
        self.assertEqual(t['status'], 'queued')
        self.assertTrue(self.run_of('product_owner'))

    def test_idea_validation_and_role_model_floor(self):
        with self.assertRaisesRegex(ValueError, 'проект'):
            w.action(dict(op='idea', project='нет', title='Идея', text='x' * 30))
        with self.assertRaises(ValueError):
            w.action(dict(op='idea', project='alpha-api', title='Идея', text='коротко'))
        self.ref = self.idea()
        po = self.run_of('product_owner')
        self.assertEqual(w.model_entry(po['packet']['route']['model'])['tier'], 3)       # PO — старший класс

    def test_spec_consistency(self):
        for role, spec in P.ROLES.items():
            stages = spec['stage'] if isinstance(spec['stage'], tuple) else (spec['stage'],)
            for st in stages:
                if st is None: continue
                for it in P.role_dod(role, st):
                    if it['kind'] == 'artifact' or it.get('art') is not None and isinstance(it.get('art'), int):
                        self.assertLess(it['art'], len(spec['artifacts']), (role, it['id']))
        ids = [it['id'] for r in P.ROLES for st in P.ORDER for it in (P.role_dod(r, st) if r in P.STAGE[st]['roles'] else [])]
        self.assertEqual(len(ids), len(set(ids)))
