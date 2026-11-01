"""Конвейер от идеи до main: жёсткие DoR/DoD, тимлид, ворота, одобрение владельца (TT-68, TT-69)."""
import os
import subprocess
import sys
from pathlib import Path
from base import DataCase
import ci
import pipeline as P
import workflow as w


def git(*args, cwd):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()


def doc_text(spec, role):
    """Документ, проходящий проверку раздела, размера и машинных пунктов роли."""
    body = ['# %s' % spec['path'].split('/')[-1]]
    for sec in (spec['sections'] + spec.get('market', [])) or ['Содержание']:
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
            out[it['id']] = dict(status='na', evidence='Неприменимо для этой задачи: нет такого контура') if it.get('allow_na') and not it.get('na_when') \
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
        self.assertNotIn('worktree', t['pipeline'])          # рабочая копия — когда роль берёт задачу
        po = self.run_of('product_owner')
        self.assertTrue(po['auto_launch'])
        # Без BRD передача не принимается: DoD железный.
        run = self.claim('product_owner')
        self.assertTrue(self.task()['pipeline']['worktree']['branch'].startswith('task/'))
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
        self.assertEqual(queued, {'system_analyst'})          # дизайн и маркетинг не нужны; архитектор — после ТЗ (А1)
        self.assertIn('после: Системный аналитик', self.task()['status_note'])
        for role in ('system_analyst', 'solution_architect'):
            self.assertTrue(self.do_role(role, 'analysis')['accepted'])
        self.assertEqual(self.task()['pipeline']['stage'], 'analysis')             # ждёт ворот тимлида
        gate = self.claim('teamlead', gate='analysis')
        dg = w.action(dict(op='packet', ref=self.ref, run=gate))['digest']          # сводка вместо чтения всего (Б6)
        self.assertGreater(dg['trace']['requirements'], 0); self.assertEqual(dg['trace']['dangling'], {})
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='SRS, HLD и ADR согласованы с BRD, разработка возможна без трактовок'))
        self.assertEqual(self.task()['pipeline']['stage'], 'development')
        r = self.do_role('developer', 'development', extra=[('app.py', 'x = 2\n'), ('tests/test_app.py', 'assert True\n')])
        self.assertTrue(r['accepted'], r)
        self.assertEqual(self.task()['pipeline']['stage'], 'review')
        r = self.do_role('reviewer', 'review', verdict='approve')
        self.assertTrue(r['accepted'], r)
        self.assertEqual(self.task()['pipeline']['stage'], 'deploy_test')            # сначала деплой в тест
        self.assertEqual((self.repo/'app.py').read_text(encoding='utf-8'), 'x = 2\n')     # слито в разработку
        self.assertEqual(ci.last(ref=self.ref, scope='task')['status'], 'passed')     # CI ветки задачи до слияния
        r = self.do_role('devops', 'deploy_test')
        self.assertTrue(r['accepted'], r)
        self.assertIn('x = 2', git('show', 'test:app.py', cwd=self.repo))          # тестовая ветка
        self.assertEqual(self.task()['pipeline']['stage'], 'testing')                # потом тестирование
        self.assertEqual(ci.last(ref=self.ref, scope='test')['status'], 'passed')
        qa = self.run_of('qa')
        env = w.action(dict(op='packet', ref=self.ref, run=qa['id']))['test_env']
        self.assertEqual(env['branch'], 'test'); self.assertIn('пройден', env['ci'])
        r = self.do_role('qa', 'testing', signoff=True)
        self.assertTrue(r['accepted'], r)
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
        self.assertTrue(git('tag', '--list', 'release/*', cwd=self.repo))            # тег релиза
        self.assertEqual(ci.last(ref=self.ref, scope='main')['status'], 'passed')
        kinds = [e['kind'] for e in w.state()['teamlead'] if e['ref'] == self.ref]
        for k in ('idea', 'handoff', 'handoff_rejected', 'gate', 'stage_passed', 'approved', 'done'):
            self.assertIn(k, kinds)

    def test_task_index_committed_and_used_by_packet(self):
        self.ref = self.idea()
        r = self.do_role('product_owner', 'idea', acceptance=['CSV содержит все проекты', 'Файл открывается в Excel'],
                                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка без интерфейса')); self.assertTrue(r['accepted'], r)
        self.assertTrue(self.do_role('system_analyst', 'analysis')['accepted'])
        t = self.task(); wt = t['pipeline']['worktree']; rel = P.index_path(t['id'])
        text = git('show', '%s:%s' % (wt['branch'], rel), cwd=self.repo)
        self.assertIn('## Реестр требований', text)
        self.assertRegex(text, r'\| BR-1 \| .*BR-1.* \| BRD\.md §\d+, стр\. \d+ \|')
        self.assertRegex(text, r'\| SR-1 \| .* \| SRS\.md §\d+')
        self.assertIn('### %s' % P.art_path(t['id'], P.ROLES['system_analyst']['artifacts'][0]['path']), text)
        self.assertEqual(git('status', '--porcelain', cwd=wt['path']).strip(), '')           # закоммичен
        packet = w.action(dict(op='packet', ref=self.ref, run=self.run_of('solution_architect')['id']))
        self.assertEqual(packet['task_index'], rel)
        self.assertTrue(all('index' not in u for u in packet['upstream']))                          # указатель — в INDEX.md
        sec = w.action(dict(op='doc', ref=self.ref, code='SR-1'))                                   # код без пути
        self.assertTrue(sec['path'].endswith('SRS.md')); self.assertIn('SR-1', sec['text'])

    def test_reviewer_returns_to_developer_with_review(self):
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=True, marketing=False, reason='Нужен экран выгрузки, запуск не нужен'))
        self.do_role('system_analyst', 'analysis')
        roles = {x['role'] for x in w.state()['runs'] if x['ref'] == self.ref and x['status'] == 'queued'}
        self.assertEqual(roles, {'solution_architect', 'designer'})                   # оба — после ТЗ
        for role in ('solution_architect', 'designer'):
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

    # --- Решала (TT-80)

    OPTS = [dict(title='Уточнить у PO состав выгрузки по прошлому отчёту', pros='быстро', cons='риск неточности'),
            dict(title='Взять полный набор полей и отметить допущение', pros='без ожидания', cons='лишние поля')]

    def escalate_po(self):
        run = self.claim('product_owner')
        r = w.action(dict(op='escalate', ref=self.ref, run=run, worker='w-' + run,
                          reason='Неясно, для каких инвесторов выгрузка и какие поля нужны',
                          tried='Прочитал запрос и прошлые записи памяти', need='Состав полей CSV'))
        return r['problem']

    def test_escalation_goes_to_resolver_and_continues(self):
        self.ref = self.idea()
        pid = self.escalate_po()
        t = self.task()
        self.assertEqual(t['status'], 'blocked')
        prob = next(p for p in w.state()['problems'] if p['id'] == pid)
        self.assertEqual((prob['status'], prob['role'], prob['source']), ('resolving', 'product_owner', 'agent'))
        rs = self.run_of('resolver')
        self.assertEqual(rs['problem'], pid)
        po_agent = next(r for r in w.state()['runs'] if r.get('role') == 'product_owner')['agent']
        self.assertNotEqual(rs['agent'], po_agent)                                  # свежий взгляд — другой ИИ
        self.assertFalse(any(r.get('role') == 'teamlead' for r in w.state()['runs']))
        run = self.claim('resolver')
        packet = w.action(dict(op='packet', ref=self.ref, run=run))
        self.assertEqual(packet['problem']['id'], pid)
        self.assertIn('ask', packet['second_opinion'])
        with self.assertRaisesRegex(ValueError, 'options'):
            w.action(dict(op='resolve', ref=self.ref, run=run, worker='w-' + run, options=self.OPTS[:1], decision='continue',
                          notes='Достаточно допущения, владелец не нужен для этого', instructions='Взять полный набор полей'))
        w.action(dict(op='resolve', ref=self.ref, run=run, worker='w-' + run, options=self.OPTS, decision='continue',
                      notes='Полный набор полей покрывает отчёт, лишнее можно убрать позже',
                      instructions='Возьми полный набор полей портфеля и запиши допущение в BRD, раздел «Допущения»'))
        t = self.task()
        self.assertEqual(t['status'], 'queued')
        po = self.run_of('product_owner')
        fb = w.action(dict(op='packet', ref=self.ref, run=po['id']))['feedback']
        self.assertIn('полный набор полей', fb[0]['detail'])
        self.assertEqual(next(p for p in w.state()['problems'] if p['id'] == pid)['status'], 'resolved')

    def test_resolver_asks_owner_through_bell(self):
        import mcp_server
        self.ref = self.idea()
        pid = self.escalate_po()
        run = self.claim('resolver')
        with self.assertRaisesRegex(ValueError, 'question'):
            w.action(dict(op='resolve', ref=self.ref, run=run, worker='w-' + run, options=self.OPTS, decision='owner',
                          notes='Состав полей — решение владельца продукта', question='?'))
        w.action(dict(op='resolve', ref=self.ref, run=run, worker='w-' + run, options=self.OPTS, decision='owner',
                      notes='Состав полей — продуктовое решение владельца, агент его не принимает',
                      question='Какие поля включить в CSV для инвесторов?', why_owner='Выбор полей — продуктовое решение владельца'))
        notes = w.snapshot()['notifications']
        q = next(n for n in notes if n['kind'] == 'question')
        self.assertEqual((q['problem'], q['ref'], len(q['options'])), (pid, self.ref, 2))
        self.assertIn('👤', self.task()['status_note'])
        r = mcp_server.call_tool('tt_work', dict(op='answer', ref=self.ref, problem=pid, option=1))
        self.assertTrue(r['isError'])                                               # отвечает только владелец
        w.action(dict(op='answer', ref=self.ref, problem=pid, option=1))
        self.assertFalse([n for n in w.snapshot()['notifications'] if n['kind'] == 'question'])
        po = self.run_of('product_owner')
        fb = w.action(dict(op='packet', ref=self.ref, run=po['id']))['feedback']
        self.assertIn('Взять полный набор полей', fb[0]['detail'])
        self.assertEqual(self.task()['status'], 'queued')

    def test_lost_resolver_session_goes_to_owner(self):
        self.ref = self.idea()
        pid = self.escalate_po()
        rs = self.run_of('resolver')
        w.action(dict(op='claim', ref=self.ref, run=rs['id'], worker='w-' + rs['id'],
                      model=w.action(dict(op='packet', ref=self.ref, run=rs['id']))['route']['model']))
        w.session_lost(self.ref, rs['id'], 'сессия 3 раза заканчивалась без сдачи этапа')
        prob = next(p for p in w.state()['problems'] if p['id'] == pid)
        self.assertEqual(prob['status'], 'owner')
        self.assertTrue(any(n['kind'] == 'question' for n in w.snapshot()['notifications']))

    def test_idea_validation_and_role_model_floor(self):
        with self.assertRaisesRegex(ValueError, 'проект'):
            w.action(dict(op='idea', project='нет', title='Идея', text='x' * 30))
        with self.assertRaises(ValueError):
            w.action(dict(op='idea', project='alpha-api', title='Идея', text='коротко'))
        self.ref = self.idea()
        po = self.run_of('product_owner')
        self.assertEqual(w.model_entry(po['packet']['route']['model'])['tier'], 2)       # PO средней задачи — Sonnet (Б2)

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

    # --- реестр дефектов (TT-73)

    def to_testing(self, fresh=True):
        if fresh:
            self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        for role in ('system_analyst', 'solution_architect'):
            self.do_role(role, 'analysis')
        if 'analysis' not in self.task()['pipeline']['gates']:
            gate = self.claim('teamlead', gate='analysis')
            w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve', notes='Анализ согласован, можно разрабатывать'))
        self.do_role('developer', 'development', extra=[('app.py', 'x = 3\n'), ('tests/test_app.py', 'assert True\n')])
        self.do_role('reviewer', 'review', verdict='approve')
        self.do_role('devops', 'deploy_test')
        self.assertEqual(self.task()['pipeline']['stage'], 'testing')

    def test_simple_task_gates_closed_by_server(self):
        self.ref = self.idea()
        d = w.read('projects.json', {})
        t = next(x for x in d['projects']['alpha-api']['backlog'] if 'alpha-api/' + x['id'] == self.ref)
        t.update(size='S', risk='low', work_type='implementation'); w.save('projects.json', d)
        self.to_testing(fresh=False)
        self.assertFalse([r for r in w.state()['runs'] if r['ref'] == self.ref and r.get('role') == 'teamlead'])
        g = self.task()['pipeline']['gates']['analysis']
        self.assertEqual((g['decision'], g['by']), ('approve', 'сервер'))
        self.assertIn('коды требований сходятся', g['notes'])
        self.do_role('qa', 'testing', signoff=True)
        t = self.task()
        self.assertEqual(t['pipeline']['gates']['release']['by'], 'сервер')
        self.assertEqual(t['status_note'], 'Ждёт одобрения релиза владельцем')     # «да» владельца сервер не заменяет

    def test_technical_task_skips_market_research_and_doc_reads_sections(self):
        self.ref = self.idea()
        run = self.claim('product_owner')
        spec = P.ROLES['product_owner']['artifacts'][0]
        rel = P.art_path(self.task()['id'], spec['path'])
        wt = Path(self.task()['pipeline']['worktree']['path'])
        lean = dict(spec, market=[])                                   # BRD технической задачи: без разделов о рынке
        (wt / rel).parent.mkdir(parents=True, exist_ok=True); (wt / rel).write_text(doc_text(lean, 'designer'), encoding='utf-8')
        git('add', '-A', cwd=wt); git('commit', '-q', '-m', 'BRD', cwd=wt)
        items = self.items('product_owner', 'idea')
        items['po-market'] = dict(status='na', evidence='Техническая задача: рынок не исследуется')
        acc = ['Первый критерий приёмки', 'Второй критерий приёмки']
        send = lambda **kw: w.action(dict(op='handoff', ref=self.ref, run=run, worker='w-' + run, items=items,
                                          summary='Работа роли выполнена по DoD, артефакты в ветке', acceptance=acc, **kw))
        r = send(needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        self.assertFalse(r['accepted'])                                # продуктовой по умолчанию рынок нужен
        self.assertEqual({f['id'] for f in r['failed']}, {'po-brd', 'po-market'})
        r = send(contract_group='Экспорт',
                 needs=dict(design=False, marketing=False, research=False, reason='Техническая задача: рефакторинг выгрузки'))
        self.assertTrue(r['accepted'], r)
        self.assertEqual(self.task()['pipeline']['contract_group'], 'экспорт')
        toc = w.action(dict(op='doc', ref=self.ref, path=rel))
        self.assertTrue(any(' Риск' in line and 'стр.' in line for line in toc['index']), toc)
        sec = w.action(dict(op='doc', ref=self.ref, path=rel, section='риск'))
        self.assertTrue(sec['text'].startswith('## Риск'))
        num = next(line.split()[0] for line in toc['index'] if ' Риск' in line)
        self.assertEqual(w.action(dict(op='doc', ref=self.ref, path=rel, section=num))['text'], sec['text'])   # по номеру указателя
        self.assertNotIn('## Проблема', sec['text'])
        with self.assertRaisesRegex(ValueError, 'Есть:'):
            w.action(dict(op='doc', ref=self.ref, path=rel, section='нет такого'))
        with self.assertRaisesRegex(ValueError, 'path'):
            w.action(dict(op='doc', ref=self.ref, path='../secret.txt'))
        sa = self.run_of('system_analyst')
        pk = w.action(dict(op='packet', ref=self.ref, run=sa['id']))
        brd = next(u for u in pk['upstream'] if u['path'] == rel)
        self.assertNotIn('index', brd); self.assertGreater(brd['tokens'], 100)      # указатель — в INDEX.md, не в паспорте
        index = (Path(self.task()['pipeline']['worktree']['path']) / pk['task_index']).read_text(encoding='utf-8')
        self.assertIn(rel, index); self.assertIn(' Риск', index)

    # --- рецепты Решалы (TT-88)

    def esc(self, run, reason, tried='Пробовал дважды', need='Продолжить работу'):
        return w.action(dict(op='escalate', ref=self.ref, run=run, worker='w-' + run, reason=reason, tried=tried, need=need))

    def test_recipes_close_mechanical_problems_without_resolver(self):
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        sa = self.claim('system_analyst')
        r = self.esc(sa, 'git commit не проходит: index.lock Permission denied в песочнице')
        self.assertEqual((r['escalated'], r['recipe']), (False, 'Р1'))
        self.assertEqual(next(x for x in w.state()['runs'] if x['id'] == sa)['status'], 'running')   # работа продолжается
        self.assertEqual(w.state()['problems'][-1]['status'], 'resolved')
        r = self.esc(sa, 'git commit всё равно не проходит: Permission denied')
        self.assertTrue(r['escalated'])                                                # рецепт не помог — к Решале
        self.assertTrue(self.run_of('resolver'))
        # Решала решает и пополняет базу решений
        res = self.claim('resolver')
        w.action(dict(op='resolve', ref=self.ref, run=res, worker='w-' + res, options=self.OPTS, decision='continue',
                      notes='Каталог .git только для чтения: коммитит сервер, роли — только файлы',
                      instructions='Не коммить сам: сохрани файлы и сдай handoff, сервер закоммитит их',
                      recipe=dict(title='Каталог .git только для чтения', keywords=['только для чтения', 'git'],
                                  instructions='Каталог .git в песочнице только для чтения: сохрани файлы и сдай handoff, сервер закоммитит.')))
        self.assertEqual(w.state()['playbook'][0]['id'], 'К-1')
        sa = self.claim('system_analyst')
        r = self.esc(sa, 'Каталог git открыт только для чтения, не сохранить историю')
        self.assertEqual(r['recipe'], 'К-1')
        self.assertEqual(w.state()['playbook'][0]['uses'], 1)
        # Р4: «????» в артефакте не принимается
        self.write('system_analyst', 'analysis')
        wt = Path(self.task()['pipeline']['worktree']['path'])
        srs = wt / P.art_path(self.task()['id'], P.ROLES['system_analyst']['artifacts'][0]['path'])
        srs.write_text(srs.read_text(encoding='utf-8') + chr(10) + '????????? ?????' + chr(10), encoding='utf-8')
        git('add', '-A', cwd=wt); git('commit', '-q', '-m', 'garbled', cwd=wt)
        r = self.handoff('system_analyst', 'analysis', sa)
        self.assertIn('Р4', str(r['failed']))
        srs.write_text(srs.read_text(encoding='utf-8').replace('????????? ?????', 'Пояснение'), encoding='utf-8')
        git('add', '-A', cwd=wt); git('commit', '-q', '-m', 'utf8', cwd=wt)
        self.assertTrue(self.handoff('system_analyst', 'analysis', sa)['accepted'])
        # Р5: противоречие контрактов — к системному аналитику с матрицей кодов
        arch = self.claim('solution_architect')
        r = self.esc(arch, 'Противоречие между SRS и HLD: контракт API выгрузки расходится по полям')
        self.assertEqual((r['recipe'], r['returned']), ('Р5', 'analysis'))
        fb = w.action(dict(op='packet', ref=self.ref, run=self.run_of('system_analyst')['id']))['feedback']
        self.assertIn('Матрица кодов', fb[0]['detail'])
        self.assertFalse([x for x in w.state()['runs'] if x['ref'] == self.ref and x.get('role') == 'solution_architect'
                          and x['status'] == 'queued'])                              # архитектор — снова после аналитика

    def test_session_recipe_and_list_pages(self):
        self.ref = self.idea()
        po = self.run_of('product_owner')
        w.session_lost(self.ref, po['id'], 'сессия 3 раза заканчивалась без сдачи этапа')
        new = self.run_of('product_owner')                                            # Р3: роль снова в работе
        fb = w.action(dict(op='packet', ref=self.ref, run=new['id']))['feedback']
        self.assertEqual(fb[0]['id'], 'recipe-Р3'); self.assertIn('BRD.md', fb[0]['detail'])
        self.assertEqual(self.task()['status'], 'queued')
        w.session_lost(self.ref, new['id'], 'сессия 3 раза заканчивалась без сдачи этапа')
        self.assertEqual(self.task()['status'], 'blocked')                            # второй раз — к Решале
        self.assertTrue(self.run_of('resolver'))
        page = w.action(dict(op='list', project='alpha-api', limit=1))
        self.assertEqual((len(page['tasks']), page['next_offset']), (1, 1))
        last = w.action(dict(op='list', project='alpha-api', offset=page['total'] - 1, limit=5))
        self.assertIsNone(last['next_offset'])

    def test_red_ci_on_main_rolls_back_release(self):
        d = w.read('projects.json', {}); p = d['projects']['alpha-api']
        p['ci'] = dict(steps=[dict(name='Тесты', kind='test', cmd=[sys.executable, '-c', 'print(1)']),
                              dict(name='Дым на main', kind='e2e', cmd=[sys.executable, '-c', 'raise SystemExit(1)'], on=['main'])])
        w.save('projects.json', d)
        self.to_testing()
        self.do_role('qa', 'testing', signoff=True)
        gate = self.claim('teamlead', gate='release')
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='QA Sign-off есть, деплой в тест чистый, риски релиза приняты'))
        w.action(dict(op='approve_release', ref=self.ref))
        self.do_role('devops', 'deploy_main')
        t = self.task()
        self.assertEqual(git('show', 'main:app.py', cwd=self.repo), 'x = 1')          # main вернулся к прежнему
        self.assertEqual(ci.last(ref=self.ref, scope='main')['status'], 'failed')
        self.assertEqual(t['pipeline']['stage'], 'development')                       # и задача — к разработчику
        d = t['pipeline']['defects'][-1]
        self.assertEqual((d['severity'], d['status'], d['found_stage']), ('critical', 'open', 'deploy_main'))
        self.assertIn('откачено', t['pipeline']['history'][-1]['reason'])

    def test_defects_registered_and_fix_method_checked_by_commits(self):
        self.to_testing()
        run = self.claim('qa'); self.write('qa', 'testing')
        with self.assertRaisesRegex(ValueError, 'defects'):
            w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                          reason='Пустой портфель роняет выгрузку: исключение вместо пустого файла'))
        r = w.action(dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                          reason='Пустой портфель роняет выгрузку: исключение вместо пустого файла',
                          defects=[dict(title='Выгрузка падает на пустом портфеле', severity='major', level='integration', test='TC-2')]))
        self.assertEqual(r['defects'], ['D-1'])
        d = self.task()['pipeline']['defects'][0]
        self.assertEqual((d['status'], d['found_by'], d['level']), ('open', 'qa', 'integration'))
        self.assertTrue(d['base'])
        dev = self.claim('developer')
        self.assertEqual(w.action(dict(op='packet', ref=self.ref, run=dev))['defects'][0]['id'], 'D-1')
        wt = Path(self.task()['pipeline']['worktree']['path'])
        (wt/'app.py').write_text('x = 4\n', encoding='utf-8'); git('commit', '-qam', 'fix', cwd=wt)
        r = self.handoff('developer', 'development', dev)
        self.assertIn('defect:D-1', {f['id'] for f in r['failed']})                       # без способа — не сдать
        r = self.handoff('developer', 'development', dev, fixes={'D-1': dict(method='test_fix', test_level='unit')})
        self.assertIn('тестов', next(f for f in r['failed'] if f['id'] == 'defect:D-1')['detail'])   # тесты не правились
        r = self.handoff('developer', 'development', dev, fixes={'D-1': dict(method='spec_change')})
        self.assertFalse(r['accepted'])                                                  # ТЗ не правилось
        r = self.handoff('developer', 'development', dev, fixes={'D-1': dict(method='code_to_spec', note='Пустой список → пустой CSV')})
        self.assertTrue(r['accepted'], r)
        d = self.task()['pipeline']['defects'][0]
        self.assertEqual((d['status'], d['fix']['method'], d['fix']['files']), ('fixed', 'code_to_spec', ['app.py']))
        self.do_role('reviewer', 'review', verdict='approve')
        self.do_role('devops', 'deploy_test')
        self.do_role('qa', 'testing', signoff=True)
        self.assertEqual(self.task()['pipeline']['defects'][0]['status'], 'verified')
        hist = self.task()['pipeline']['history']
        dev_handoffs = [h['accepted'] for h in hist if h['kind'] == 'handoff' and h['role'] == 'developer']
        self.assertEqual(dev_handoffs, [True, False, False, False, True])
        self.assertTrue(any(h['kind'] == 'return' and h['role'] == 'qa' and h['defects'] == ['D-1'] for h in hist))

    # --- перенос в «Идея» (TT-77)

    def test_owner_moves_tasks_to_idea(self):
        # Задача вне конвейера «в работе» и задача конвейера на анализе — обе в «Идея».
        d = w.read('projects.json', {}); b = d['projects']['alpha-api']['backlog']
        b[0]['status'] = 'in_progress'; b.append(dict(id='AL-05', title='Без запроса', why='Описание задачи без корня', size='S', status='queued'))
        w.save('projects.json', d)
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        self.assertEqual(self.task()['pipeline']['stage'], 'analysis')
        prev = self.run_of('system_analyst')
        r = w.action(dict(op='to_idea', project='alpha-api'))
        self.assertTrue(r['dry_run'])
        self.assertEqual(sorted(x['ref'] for x in r['items']), ['alpha-api/AL-01', 'alpha-api/AL-05', self.ref])
        self.assertEqual({x['why'] for x in r['skipped_items']}, {'закрыта'})                 # AL-02 готова
        self.assertEqual(self.task()['pipeline']['stage'], 'analysis')                          # предпросмотр ничего не меняет
        w.action(dict(op='to_idea', project='alpha-api', dry_run=False))
        for ref in ('alpha-api/AL-01', 'alpha-api/AL-05', self.ref):
            t = w.task_for(ref)
            self.assertEqual((t['pipeline']['stage'], t['status']), ('idea', 'queued'), ref)
            po = next(x for x in w.state()['runs'] if x['ref'] == ref and x.get('role') == 'product_owner' and x['status'] == 'queued')
            self.assertFalse(po['auto_launch'])                                                # окна сами не открываются
        self.assertTrue(w.task_for('alpha-api/AL-05')['root'])                                  # запрос создан из задачи
        self.assertEqual(next(x for x in w.state()['runs'] if x['id'] == prev['id'])['status'], 'cancelled')
        self.assertEqual(w.task_for('alpha-api/AL-02')['status'], 'done')
        # «Запустить проект» берёт роли конвейера в пределах лимита сессий.
        plan = w.action(dict(op='start', project='alpha-api', agents=['claude', 'codex']))
        self.assertEqual({p['kind'] for p in plan['picks']}, {'role'})
        self.assertLessEqual(plan['sessions'], 4)

    def test_full_autonomy_releases_and_project_runs_itself(self):
        w.action(dict(op='settings', changes=dict(autonomy='full', consent=True)))
        self.to_testing()
        self.do_role('qa', 'testing', signoff=True)
        gate = self.claim('teamlead', gate='release')
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='QA Sign-off есть, деплой в тест чистый, риски релиза приняты'))
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'deploy_main')
        self.assertIn('полная автономия', t['pipeline']['approvals']['release']['by'])   # без отдельного да владельца
        self.assertTrue(self.run_of('devops'))
        # Запуск проекта: все ждущие роли проекта открываются сервером по мере мест; остановка снимает это.
        w.action(dict(op='to_idea', project='alpha-api', dry_run=False))
        w.action(dict(op='start', project='alpha-api', agents=['claude'], sessions=1, dry_run=False))
        pos = [r for r in w.state()['runs'] if r.get('role') == 'product_owner' and r['status'] == 'queued']
        self.assertTrue(pos and all(r['auto_launch'] for r in pos))
        self.assertIn('alpha-api', w.snapshot()['active_projects'])
        r = w.action(dict(op='stop_project', project='alpha-api'))
        self.assertEqual(r['held'], len(pos))
        self.assertNotIn('alpha-api', w.snapshot()['active_projects'])

    # --- ИИ, модель и усилия по роли (TT-79)

    def test_role_routing_by_role_and_complexity(self):
        import orchestra
        self.ref = self.idea()
        s = w.state(); t = self.task()
        po = orchestra.role_route(t, 'product_owner', orchestra.role_agent(s, t, 'product_owner'))
        self.assertEqual((po['provider'], w.model_entry(po['model'])['tier'], po['source']), ('claude', 2, 'role'))
        hard = dict(t, size='XL', risk='high', work_type='architecture')
        for role in ('product_owner', 'solution_architect', 'teamlead', 'resolver'):          # старший класс — только сложным
            r = orchestra.role_route(hard, role, 'claude')
            self.assertEqual((w.model_entry(r['model'])['tier'], r['effort']), (3, 'xhigh'), role)
        self.assertGreater(orchestra.role_route(hard, 'product_owner', 'claude')['budget_tokens'], po['budget_tokens'])
        self.assertIn('Product Owner', po['reason'])
        dev = orchestra.role_route(t, 'developer', orchestra.role_agent(s, t, 'developer'))
        self.assertEqual(dev['provider'], 'codex')
        self.assertIn(dev['effort'], w.EFFORTS['codex'])                       # у Codex выше high не бывает
        # Ревьюер — другой ИИ, чем разработчик этого конвейера.
        t2 = dict(t, pipeline=dict(t['pipeline'], roles={'development/developer': dict(agent='claude')}))
        self.assertEqual(orchestra.role_agent(s, t2, 'reviewer'), 'codex')
        t3 = dict(t, pipeline=dict(t['pipeline'], roles={'development/developer': dict(agent='codex')}))
        self.assertEqual(orchestra.role_agent(s, t3, 'reviewer'), 'claude')
        # Закрепление владельцем сильнее таблицы; ИИ на лимите пропускается.
        w.action(dict(op='settings', changes=dict(roles={'developer': 'claude'})))
        self.assertEqual(orchestra.role_agent(w.state(), t, 'developer'), 'claude')
        s = w.state(); w.note_limit(s, 'claude', w.now() + __import__('datetime').timedelta(hours=1), w.now()); w.save('workflow.json', s)
        self.assertEqual(orchestra.role_agent(w.state(), t, 'developer'), 'codex')
        # Сложная задача поднимает класс и усилия аналитика.
        hard = dict(t, size='L', risk='high', work_type='architecture')
        easy = dict(t, size='S', risk='low', work_type='docs')
        r_hard, r_easy = orchestra.role_route(hard, 'system_analyst', 'claude'), orchestra.role_route(easy, 'system_analyst', 'claude')
        self.assertGreaterEqual(w.model_entry(r_hard['model'])['tier'], w.model_entry(r_easy['model'])['tier'])
        self.assertGreaterEqual(w.EFFORTS['claude'].index(r_hard['effort']), w.EFFORTS['claude'].index(r_easy['effort']))

    def test_server_commits_when_agent_cannot(self):
        # Песочница Codex не даёт писать в .git: файлы роли коммитит сервер при сдаче.
        self.ref = self.idea()
        run = self.claim('product_owner')
        wt = Path(self.task()['pipeline']['worktree']['path'])
        for spec in P.ROLES['product_owner']['artifacts']:
            if spec.get('optional'): continue
            path = wt / P.art_path(self.task()['id'], spec['path'])
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(doc_text(spec, 'product_owner'), encoding='utf-8')
        r = self.handoff('product_owner', 'idea', run, acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                         needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        self.assertTrue(r['accepted'], r)
        self.assertEqual(git('status', '--porcelain', cwd=wt), '')
        self.assertIn('Автокоммит сервера', git('log', '-2', '--format=%B', cwd=wt))   # последним — INDEX.md
        self.assertTrue(any(e['kind'] == 'autocommit' for e in self.task()['pipeline']['journal']))
