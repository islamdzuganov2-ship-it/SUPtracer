"""Два независимых анализа CRITICAL разными ИИ (07: E2E-R05): обе версии сохранены, расхождения не скрыты,
ворота анализа без решения по каждому расхождению не закрываются — молчаливого компромисса нет."""
from test_pipeline import PipelineTests           # первым: base ставит путь к tools/
import orchestra as O
import pipeline as P
import workflow as w

ALT_TEXT = ('# Второй анализ\n\n## Требования\nREQ-1 выгрузка CSV; REQ-2 кодировка UTF-8 с BOM для Excel.\n\n'
            '## Решения\nПоток строк без загрузки всего портфеля в память.\n\n## Риски\nБольшой портфель — таймаут ответа.\n'
            + 'Подробности сравнения с первым анализом и обоснование расхождений. ' * 6)


class DualAnalysisTests(PipelineTests):
    def critical_analysis(self, mode='critical'):
        self.ref = self.idea()
        d = w.read('projects.json', {})
        t = next(x for x in d['projects']['alpha-api']['backlog'] if 'alpha-api/' + x['id'] == self.ref)
        t.update(risk='high'); w.save('projects.json', d)
        if mode:
            w.action(dict(op='settings', changes=dict(dual_analysis=mode)), origin='owner')
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        for role in ('system_analyst', 'solution_architect', 'qa'):
            self.assertTrue(self.do_role(role, 'analysis')['accepted'])

    def analysts(self):
        return [r for r in w.state()['runs'] if r['ref'] == self.ref and r.get('role') == 'system_analyst']

    def commit(self, files):
        """Второй аналитик пишет только свои файлы (помощник write переписал бы все артефакты роли)."""
        from pathlib import Path
        import subprocess
        wt = Path(self.task()['pipeline']['worktree']['path'])
        for name, text in files:
            (wt / name).parent.mkdir(parents=True, exist_ok=True); (wt / name).write_text(text, encoding='utf-8')
        subprocess.run(['git', 'add', '-A'], cwd=wt, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'второй анализ'], cwd=wt, check=True)

    def alt(self):
        return P.art_path(self.task()['id'], P.ART + '/' + O.ALT_ANALYSIS)

    def test_second_independent_analysis_and_conflict_needs_explicit_decision(self):
        self.critical_analysis()
        t = self.task()
        self.assertEqual(t['pipeline']['stage'], 'analysis')
        self.assertIn('второй независимый анализ', t['status_note'])
        first, second = self.analysts()
        self.assertEqual(second['dual'], dict(second_of=first['id']))
        self.assertNotEqual(second['agent'], first['agent'])                        # другой ИИ, а не повтор

        # Паспорт второго аналитика объясняет задачу: свой документ, сравнение, артефакты первого не трогать.
        run = self.claim('system_analyst')
        pk = w.action(dict(op='packet', ref=self.ref, run=run))
        self.assertEqual(pk['dual_analysis']['first_run'], first['id'])
        self.assertIn('comparison=', pk['handoff'])
        # Без своего документа и сравнения — не принимается.
        r = self.handoff('system_analyst', 'analysis', run)
        self.assertFalse(r['accepted'])
        self.assertTrue({'dual-own', 'dual-compare'} <= {f['id'] for f in r['failed']})

        # Правка артефакта первого аналитика — не принимается: обе версии должны сохраниться.
        own = P.art_path(self.task()['id'], P.ROLES['system_analyst']['artifacts'][0]['path'])
        self.commit([(self.alt(), ALT_TEXT), (own, '# переписано вторым аналитиком\n')])
        diff = [dict(topic='Кодировка CSV', first='UTF-8 без BOM', second='UTF-8 с BOM — иначе Excel ломает кириллицу')]
        r = self.handoff('system_analyst', 'analysis', run, comparison=dict(agree=False, differences=diff))
        self.assertIn('dual-independent', {f['id'] for f in r['failed']})

        import subprocess
        wt = self.task()['pipeline']['worktree']['path']
        subprocess.run(['git', 'checkout', 'HEAD~1', '--', own], cwd=wt, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-am', 'вернул артефакт первого'],
                       cwd=wt, check=True)
        r = self.handoff('system_analyst', 'analysis', run, comparison=dict(agree=False, differences=diff))
        self.assertTrue(r['accepted'], r)
        t = self.task()
        da = t['pipeline']['dual_analysis']
        (dis,) = da['second']['disagreements']
        rec = next(d for d in w.state()['disagreements'] if d['id'] == dis)
        self.assertEqual((rec['stage'], rec['resolution'], rec['topic']), ('analysis', 'open', 'Кодировка CSV'))
        self.assertIn('BOM', rec['second']['position'])
        self.assertEqual(da['second']['document'], self.alt())
        self.assertNotIn('analysis', t['pipeline']['gates'])                       # сервер ворота не закрыл

        gate = self.claim('teamlead', gate='analysis')
        pk = w.action(dict(op='packet', ref=self.ref, run=gate))
        self.assertEqual([d['id'] for d in pk['disagreements']], [dis])               # тимлид видит обе позиции
        with self.assertRaisesRegex(ValueError, dis):
            w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                          notes='Анализ согласован, можно разрабатывать по контракту'))
        w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                      notes='%s: принята позиция второго анализа — UTF-8 с BOM, Excel владельца иначе ломает кириллицу' % dis))
        rec = next(d for d in w.state()['disagreements'] if d['id'] == dis)
        self.assertEqual(rec['resolution'], 'decided')
        self.assertIn('BOM', rec['decision'])
        self.assertEqual(self.task()['pipeline']['stage'], 'development')

    def test_agreeing_second_analysis_records_both(self):
        self.critical_analysis()
        run = self.claim('system_analyst')
        self.commit([(self.alt(), ALT_TEXT)])
        r = self.handoff('system_analyst', 'analysis', run, comparison=dict(agree=True, differences=[]))
        self.assertTrue(r['accepted'], r)
        da = self.task()['pipeline']['dual_analysis']
        self.assertTrue(da['second']['agree'])
        self.assertTrue(da['first']['run'] and da['second']['run'] != da['first']['run'])

    def test_off_by_default_and_only_owner_switches(self):
        self.critical_analysis(mode=None)
        self.assertEqual(len(self.analysts()), 1)                                   # по умолчанию — один анализ
        with self.assertRaises(Exception):
            w.action(dict(op='settings', changes=dict(dual_analysis='critical')))  # агент политику не меняет
        with self.assertRaises(ValueError):
            w.action(dict(op='settings', changes=dict(dual_analysis='always')), origin='owner')


for _n in [n for n in dir(PipelineTests) if n.startswith('test_') and n not in DualAnalysisTests.__dict__]:
    setattr(DualAnalysisTests, _n, None)
del PipelineTests
