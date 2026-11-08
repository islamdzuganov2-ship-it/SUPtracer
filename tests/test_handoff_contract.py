"""Договор сдачи этапа и доказательства ревью (07: A1-03, A1-04, A1-06, A7-01, AG-REV-01..05, E2E-R02, E2E-R15)."""
from test_pipeline import PipelineTests           # первым: base ставит путь к tools/
import orchestra as O
import workflow as w


class HandoffContractTests(PipelineTests):
    def to_development(self):
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        for role in ('system_analyst', 'solution_architect', 'qa'):
            self.do_role(role, 'analysis')
        if 'analysis' not in self.task()['pipeline']['gates']:
            gate = self.claim('teamlead', gate='analysis')
            w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve', notes='Анализ согласован, можно разрабатывать'))

    def to_review(self):
        self.to_development()
        self.do_role('developer', 'development', extra=[('app.py', 'x = 3\n'), ('tests/test_app.py', 'assert True\n')])
        self.assertEqual(self.task()['pipeline']['stage'], 'review')

    def send(self, run, **kw):
        req = dict(op='handoff', ref=self.ref, run=run, worker='w-' + run, summary='Работа роли выполнена по DoD, артефакты в ветке')
        req.update(kw)
        return w.action(req)

    # --- схема договора: ошибка до побочных эффектов

    def test_schema_errors_have_no_side_effects(self):
        self.ref = self.idea()
        run = self.claim('product_owner'); self.write('product_owner', 'idea')
        before = (len(self.task()['pipeline'].get('journal') or []), len(self.task()['pipeline'].get('history') or []))
        items = self.items('product_owner', 'idea')
        with self.assertRaisesRegex(ValueError, 'schema_version'):
            self.send(run, items=items, schema_version='handoff-0')
        with self.assertRaisesRegex(ValueError, 'items: обязательное'):
            self.send(run)                                                       # удалённое обязательное поле
        with self.assertRaisesRegex(ValueError, 'items.po-brd'):
            self.send(run, items=dict(items, **{'po-brd': 'сделано'}))           # неверный тип
        with self.assertRaisesRegex(ValueError, 'assumptions: список строк'):
            self.send(run, items=items, assumptions='одна строка вместо списка')
        t = self.task()
        self.assertEqual(before, (len(t['pipeline'].get('journal') or []), len(t['pipeline'].get('history') or [])))
        self.assertEqual(next(r for r in w.state()['runs'] if r['id'] == run)['status'], 'running')
        packet = w.action(dict(op='packet', ref=self.ref, run=run))
        self.assertEqual(packet['schema']['version'], O.SCHEMA_VERSION)
        self.assertIn('questions', packet['schema']['optional'])
        r = self.send(run, items=items, schema_version=O.SCHEMA_VERSION, assumptions=['Выгрузка нужна только в CSV'],
                      acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                      needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        self.assertTrue(r['accepted'], r)
        sa = self.run_of('system_analyst')
        up = w.action(dict(op='packet', ref=self.ref, run=sa['id']))['upstream_uncertain']
        self.assertIn(dict(source='idea/product_owner', kind='assumptions', text='Выгрузка нужна только в CSV'), up)

    def test_unverified_cannot_be_approved(self):
        self.to_review()
        run = self.claim('reviewer'); self.write('reviewer', 'review')
        with self.assertRaisesRegex(ValueError, 'unverified'):
            self.send(run, items=self.items('reviewer', 'review'), verdict='approve', unverified=['Нагрузочный сценарий не запускал'],
                      **self.review_evidence())

    # --- вопрос и противоречие останавливают этап

    def test_question_stops_stage_and_blocks_development_until_resolved(self):
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        run = self.claim('system_analyst'); self.write('system_analyst', 'analysis')
        r = self.send(run, items=self.items('system_analyst', 'analysis'),
                      conflicts=['BRD требует выгрузку всех проектов, а критерий 2 — только активных'])
        self.assertFalse(r['accepted']); self.assertTrue(r['blocked'])
        t = self.task()
        self.assertEqual(t['status'], 'blocked')
        cf = t['pipeline']['conflicts'][0]
        self.assertEqual((cf['id'], cf['kind'], cf['status'], cf['problem']), ('CF-1', 'conflict', 'open', r['escalated']))
        self.assertNotIn('analysis/system_analyst', {k for k, e in t['pipeline']['roles'].items() if e.get('state') == 'done'})
        rs = self.claim('resolver')
        w.action(dict(op='resolve', cause='Причина видна в журнале запуска и паспорте задачи', cause_kind='fact', ref=self.ref, run=rs, worker='w-' + rs, options=self.OPTS, decision='continue',
                      notes='Критерий 2 уточняет BRD: выгружаются только активные проекты',
                      instructions='Выгружать только активные проекты; поправь SRS и сдай этап заново'))
        t = self.task()
        self.assertEqual(t['pipeline']['conflicts'][0]['status'], 'resolved')
        self.assertIn('активные', t['pipeline']['conflicts'][0]['resolution'])

    def test_open_conflict_blocks_development_dor(self):
        import pipeline as P
        self.to_development()
        pipe = dict(self.task()['pipeline'], conflicts=[dict(id='CF-1', status='open', text='x')])
        res = P.check_dor('development', O.ctx_for(self.task(), pipe), pipe)
        bad = [it['id'] for it, ok, _ in res if not ok]
        self.assertEqual(bad, ['dor-conflicts'])

    # --- ревьюер: матрица по критериям и голова ветки

    def test_reviewer_needs_matrix_by_ac_and_current_head(self):
        self.to_review()
        run = self.claim('reviewer'); self.write('reviewer', 'review')
        items = self.items('reviewer', 'review'); ev = self.review_evidence()
        r = self.send(run, items=items, verdict='approve', reviewed_sha=ev['reviewed_sha'])
        self.assertIn('rv-ac', {f['id'] for f in r['failed']})                   # без матрицы — нет одобрения
        gap = dict(ev['ac_matrix'], **{'AC-2': dict(files=['app.py'], tests=[], verdict='gap')})
        r = self.send(run, items=items, verdict='approve', reviewed_sha=ev['reviewed_sha'], ac_matrix=gap)
        self.assertIn('AC-2: gap', next(f for f in r['failed'] if f['id'] == 'rv-ac')['detail'])
        ghost = dict(ev['ac_matrix'], **{'AC-1': dict(files=['nope.py:12'], tests=[], verdict='ok')})
        r = self.send(run, items=items, verdict='approve', reviewed_sha=ev['reviewed_sha'], ac_matrix=ghost)
        self.assertIn('nope.py', next(f for f in r['failed'] if f['id'] == 'rv-ac')['detail'])
        narrow = {k: dict(files=['tests/test_app.py'], tests=[], verdict='ok') for k in ev['ac_matrix']}
        r = self.send(run, items=items, verdict='approve', reviewed_sha=ev['reviewed_sha'], ac_matrix=narrow)
        self.assertIn('app.py', next(f for f in r['failed'] if f['id'] == 'rv-ac')['detail'])   # изменённый файл без критерия
        r = self.send(run, items=items, verdict='approve', reviewed_sha='0000000', ac_matrix=ev['ac_matrix'])
        self.assertIn('rv-sha', {f['id'] for f in r['failed']})
        r = self.send(run, items=items, verdict='approve', **self.review_evidence())
        self.assertTrue(r['accepted'], r)

    # --- дефекты: вид, ссылка, воспроизведение

    def test_defect_needs_repro_and_existing_file(self):
        self.to_review()
        run = self.claim('reviewer'); self.write('reviewer', 'review')
        ret = dict(op='return', ref=self.ref, run=run, worker='w-' + run, to_stage='development',
                   reason='Нет обработки пустого списка проектов: выгрузка падает с исключением')
        with self.assertRaisesRegex(ValueError, 'NEEDS_REPRO'):
            w.action(dict(ret, defects=[dict(title='Пустой портфель роняет выгрузку', severity='major', level='unit')]))
        with self.assertRaisesRegex(ValueError, 'NEEDS_REPRO.*ghost.py'):
            w.action(dict(ret, defects=[dict(title='Пустой портфель роняет выгрузку', severity='major', level='unit', file='ghost.py:3')]))
        with self.assertRaisesRegex(ValueError, 'kind'):
            w.action(dict(ret, defects=[dict(title='Пустой портфель роняет выгрузку', severity='major', level='unit', file='app.py', kind='bug')]))
        w.action(dict(ret, defects=[dict(title='Тест не проверяет пустой портфель', severity='minor', level='unit', file='tests/test_app.py:1',
                                         kind='test_gap')]))
        d = self.task()['pipeline']['defects'][0]
        self.assertEqual((d['kind'], d['file']), ('test_gap', 'tests/test_app.py:1'))


for _n in [n for n in dir(PipelineTests) if n.startswith('test_') and n not in HandoffContractTests.__dict__]:
    setattr(HandoffContractTests, _n, None)
del PipelineTests
