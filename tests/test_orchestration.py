"""Оркестрация: порядок анализа, лимит незавершённого, группы контрактов, очередь диспетчера (TT-85)."""
from base import DataCase
import orchestra as O
import pipeline as P
import workflow as w


def task(ref, stage, roles=None, group=None, needs=None):
    return dict(ref=ref, project=ref.split('/')[0], pipeline=dict(stage=stage, roles=roles or {}, needs=needs or {},
                                                                  contract_group=group))


class OrchestrationTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()

    def test_architect_and_designer_wait_for_analyst(self):
        self.assertEqual(P.waits_for('analysis', 'solution_architect'), 'system_analyst')
        self.assertEqual(P.waits_for('analysis', 'designer', dict(design=True)), 'system_analyst')
        self.assertIsNone(P.waits_for('analysis', 'marketer', dict(marketing=True)))
        s = w.state(); tasks = {'alpha-api/AL-1': task('alpha-api/AL-1', 'analysis')}
        run = dict(ref='alpha-api/AL-1', role='solution_architect', stage='analysis')
        self.assertIn('Системный аналитик', O.hold(s, run, tasks))
        tasks['alpha-api/AL-1']['pipeline']['roles']['analysis/system_analyst'] = dict(state='done')
        self.assertIsNone(O.hold(s, run, tasks))

    def test_wip_limit_stops_product_owner(self):
        s = w.state(); s.setdefault('settings', {})['wip_analysis'] = 2
        tasks = {'alpha-api/AL-%d' % i: task('alpha-api/AL-%d' % i, 'analysis') for i in (1, 2)}
        tasks['alpha-api/AL-9'] = task('alpha-api/AL-9', 'idea')
        tasks['beta-tool/BT-1'] = task('beta-tool/BT-1', 'idea')
        po = dict(ref='alpha-api/AL-9', role='product_owner', stage='idea')
        self.assertIn('лимит 2', O.hold(s, po, tasks))
        self.assertIsNone(O.hold(s, dict(po, ref='beta-tool/BT-1'), tasks))          # лимит — на проект
        tasks['alpha-api/AL-2']['pipeline']['stage'] = 'development'
        self.assertIsNone(O.hold(s, po, tasks))

    def test_contract_group_serializes_analysis(self):
        s = w.state()
        done = dict(state='done')
        tasks = {r: task(r, 'analysis', {'analysis/system_analyst': done}, group='api партнёров') for r in ('alpha-api/AL-1', 'alpha-api/AL-2')}
        s['runs'] = [dict(id='RUN-1', ref='alpha-api/AL-1', role='system_analyst', stage='analysis', status='running',
                          launched_at=w.now().isoformat())]
        run = dict(ref='alpha-api/AL-2', role='system_analyst', stage='analysis')
        self.assertIn('api партнёров', O.hold(s, run, tasks))
        s['runs'][0]['status'] = 'done'
        self.assertIsNone(O.hold(s, run, tasks))

    def test_dispatch_order(self):
        tasks = {}; prio = {'a/1': 1, 'a/2': 0}
        runs = [dict(id='1', ref='a/1', role='product_owner', stage='idea', created_at='1'),
                dict(id='2', ref='a/2', role='system_analyst', stage='analysis', created_at='2'),
                dict(id='3', ref='a/1', role='qa', stage='testing', created_at='3'),
                dict(id='4', ref='a/2', role='resolver', stage='analysis', created_at='4')]
        order = [r['id'] for r in sorted(runs, key=lambda r: O.dispatch_order(r, tasks, prio))]
        self.assertEqual(order, ['4', '3', '2', '1'])                                # Решала, ближе к main, …


class BudgetWatchTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()

    def test_session_cost_and_alert(self):
        import json, os
        from pathlib import Path
        from unittest import mock
        import limits, serve
        home = Path(self.tmp) / 'claude'; (home / 'projects' / 'p').mkdir(parents=True)
        row = lambda rid: json.dumps(dict(type='assistant', requestId=rid, message=dict(
            model='claude-opus-5-5', usage=dict(input_tokens=1000, output_tokens=200000, cache_read_input_tokens=0))))
        (home / 'projects' / 'p' / 'S1.jsonl').write_text('\n'.join([row('a'), row('a'), row('b')]) + '\n', encoding='utf-8')
        run = dict(id='RUN-1', ref='alpha-api/AL-01', status='running', role='developer', agent='claude',
                   launch=dict(agent='claude', session='S1', pid=1), packet=dict(route=dict(budget_tokens=200000)))
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(home)}):
            tok, usd = limits.session_cost(run)
            self.assertEqual(tok, 402000)                                    # дубли requestId не считаются
            from pricing import cost_usd
            want = round(2 * cost_usd('claude-opus-5-5', 1000, 200000, 0, 0, 0), 2)
            self.assertAlmostEqual(usd, want, places=2)
            s = w.state(); s['runs'] = [run]; w.save('workflow.json', s)
            serve._auto_attempts.clear()
            self.assertEqual(serve.budget_once(), ['RUN-1'])
            self.assertEqual(serve.budget_once(), [])                       # одна отметка на запуск
        b = w.state()['runs'][0]['budget_alert']
        self.assertEqual((b['tokens'], b['budget']), (402000, 200000))         # мерило — токены, не доллары
        self.assertTrue(any(n['kind'] == 'budget' for n in w.notifications(w.state(), [])))
        self.assertTrue(any(e['kind'] == 'budget' for e in w.state()['teamlead']))


class FakeCtx:
    def __init__(self, files):
        self.files = files; self.t = dict(id='AL-1'); self.handoff = {}

    def artifact(self, spec):
        return self.files.get(spec['path'].split('/')[-1])


class TraceTests(DataCase):
    def test_trace_finds_dangling_and_untraced(self):
        files = {'BRD.md': '# Требования\nBR-1 выгрузка', 'SRS.md': '## FR\nFR-1 по BR-1\nFR-2 фильтр\nNFR-1 скорость\nсм. API-1',
                 'RTM.md': '| BR-1 | FR-1 | TC-1 |\n| BR-2 | FR-2 | |', 'HLD.md': 'компонент для FR-1, FR-9, ADR-1, ADR-2',
                 'adr': '# ADR-1\nрешение'}
        tr = P.trace(FakeCtx(files))
        self.assertEqual(tr['requirements'], 3)
        self.assertEqual(tr['untraced'], ['NFR-1'])                                 # нет в RTM
        self.assertEqual(set(tr['dangling']), {'BR-2', 'FR-9', 'ADR-2'})   # TC-1 — дело QA; API без api.md — в SRS
        ok, detail = P.machine(dict(check='trace'), 'system_analyst', FakeCtx(files), {})
        self.assertFalse(ok); self.assertIn('NFR-1 нет в RTM', detail)
        ok, detail = P.machine(dict(check='trace'), 'solution_architect', FakeCtx(files), {})
        self.assertFalse(ok); self.assertIn('FR-9', detail); self.assertNotIn('BR-2', detail)   # архитектору — его ссылки
        files.update({'RTM.md': '| BR-1 | FR-1 FR-2 NFR-1 | TC-1 |', 'HLD.md': 'FR-1, ADR-1', 'SRS.md': '## FR\nFR-1 по BR-1\nFR-2\nNFR-1'})
        ok, detail = P.machine(dict(check='trace'), 'system_analyst', FakeCtx(files), {})
        self.assertTrue(ok, detail)
        ok, detail = P.machine(dict(check='trace'), 'system_analyst', FakeCtx({'SRS.md': 'без кодов'}), {})
        self.assertFalse(ok); self.assertIn('нет кодов', detail)


class RecipeMatchTests(DataCase):
    def test_match(self):
        import recipes as R
        m = lambda text, role='developer': (R.match({}, text, role) or {}).get('id')
        self.assertEqual(m('Нет SRS: аналитик ещё не сдал ТЗ', 'solution_architect'), 'Р2')
        self.assertIsNone(m('Нет SRS: аналитик ещё не сдал ТЗ', 'qa'))               # Р2 — только архитектору и дизайнеру
        self.assertEqual(m('В файле ???????? вместо текста'), 'Р4')
        self.assertEqual(m('UnicodeEncodeError: charmap codec'), 'Р4')
        self.assertEqual(m('Список задач обрезан, видно только 10 задач'), 'Р6')
        self.assertIsNone(m('Противоречие в контракте API', 'system_analyst'))      # свой контракт аналитик чинит сам
        self.assertIsNone(m('Неясно, какие поля нужны инвесторам'))                  # содержательное — к Решале
        self.assertEqual([r['id'] for r in R.catalog()], ['Р1', 'Р2', 'Р3', 'Р4', 'Р5', 'Р6'])
        with self.assertRaisesRegex(ValueError, 'keywords'):
            R.learn({}, dict(title='Заголовок рецепта', instructions='x' * 40, keywords=['a']), dict(id='P-1'), w.now())
