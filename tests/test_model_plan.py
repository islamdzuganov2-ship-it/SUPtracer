"""План модели и усилий для обоих ИИ при формировании задачи (TT-48, TT-49)."""
from pathlib import Path
from base import DataCase
import workflow as w


class ModelPlanTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api'}]})
        self.base=dict(id='AL-03',title='Правка',why='Нужна',root='request',size='S',priority='🟡',risk='low',
                       work_type='implementation',acceptance=['Тест проходит'],depends_on=[])

    def plan(self, **patch):
        w.action(dict(op='plan',project='alpha-api',tasks=[dict(self.base,**patch)]))
        return 'alpha-api/'+self.base['id']

    def test_plan_stores_model_and_effort_for_both_ai(self):
        self.plan()
        raw=w.read('projects.json',{})['projects']['alpha-api']['backlog'][-1]['model_plan']
        self.assertEqual((raw['claude']['model'],raw['claude']['effort']),('claude-haiku-5-5','medium'))
        self.assertEqual((raw['codex']['model'],raw['codex']['effort']),('gpt-6-luna','medium'))
        self.assertTrue(raw['claude']['reason'])

    def test_effort_fits_task(self):
        cases=[(dict(work_type='docs'),'claude','low'), (dict(size='M'),'claude','medium'),
               (dict(size='L',risk='medium'),'claude','high'), (dict(risk='high'),'claude','high'),
               (dict(size='M',risk='high',work_type='architecture'),'claude','xhigh'),
               (dict(size='M',risk='high',work_type='architecture'),'codex','high')]
        for patch,ai,effort in cases:
            with self.subTest(patch=patch,ai=ai):
                self.assertEqual(w.model_plan(dict(self.base,**patch))[ai]['effort'],effort)

    def test_budget_scales_with_effort(self):
        r=w.recommendation(dict(self.base,size='M',risk='high',work_type='architecture'),agent='claude')
        self.assertEqual((r['model'],r['effort'],r['total_token_budget']),('claude-opus-5-5','xhigh',28800))   # высокий риск — ступенью выше
        self.assertEqual(w.recommendation(self.base)['total_token_budget'],6000)  # прежние ориентиры

    def test_manual_plan_needs_reason_and_survives_recalc(self):
        ref=self.plan()
        bad=[{'claude':{'model':'claude-sonnet-5-5','effort':'high','reason':''}},
             {'codex':{'model':'gpt-6-luna','effort':'xhigh','reason':'x'}},
             {'claude':{'model':'gpt-6-luna','effort':'low','reason':'x'}},
             {'gemini':{'model':'x','effort':'low','reason':'x'}}]
        for plan in bad:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                w.action(dict(op='update',ref=ref,changes={'model_plan':plan}))
        w.action(dict(op='update',ref=ref,changes={'model_plan':{'claude':{'model':'claude-sonnet-5-5','effort':'high','reason':'Много файлов'}}}))
        w.action(dict(op='update',ref=ref,changes={'risk':'medium'}))
        plan=w.task_for(ref)['model_plan']
        self.assertEqual((plan['claude']['model'],plan['claude']['source']),('claude-sonnet-5-5','manual'))
        self.assertEqual((plan['codex']['model'],plan['codex']['source']),('gpt-6.1-sol','auto'))  # пересчитан
        self.assertEqual(w.task_for(ref)['routes']['claude']['effort'],'high')
        w.action(dict(op='update',ref=ref,changes={'model_plan':{'claude':None}}))
        self.assertEqual(w.task_for(ref)['model_plan']['claude']['source'],'auto')

    def test_route_for_draft_writes_nothing(self):
        before=w.read('projects.json',{})
        r=w.action(dict(op='route',task=dict(size='L',risk='medium',work_type='implementation')))
        self.assertEqual((r['model_plan']['claude']['model'],r['model_plan']['claude']['effort']),('claude-sonnet-5-5','high'))
        self.assertEqual(before,w.read('projects.json',{}))

    def test_launch_for_selected_ai_is_pinned(self):
        ref=self.plan()
        w.action(dict(op='enqueue',ref=ref,agent='claude'))
        run=w.state()['runs'][0]
        self.assertEqual((run['agent'],run['packet']['route']['model']),('claude','claude-haiku-5-5'))
        with self.assertRaisesRegex(ValueError,'поставлен для ИИ claude'):
            w.action(dict(op='claim',ref=ref,run='RUN-1',worker='codex-1',model='gpt-6-luna'))
        w.action(dict(op='claim',ref=ref,run='RUN-1',worker='claude-1',model='claude-haiku-5-5'))

    def test_launch_for_other_ai_than_assigned_rejected(self):
        ref=self.plan(executor='codex')
        with self.assertRaises(ValueError): w.action(dict(op='enqueue',ref=ref,agent='claude'))

    def test_old_tasks_without_plan_get_routes(self):
        t=w.task_for('alpha-api/AL-01')
        self.assertEqual(set(t['routes']),{'claude','codex'})

    def test_mcp_route(self):
        import json, mcp_server
        r=mcp_server.call_tool('tt_work',dict(op='route',task=dict(size='S',risk='low',work_type='docs',acceptance=['Раздел обновлён'])))
        self.assertEqual(json.loads(r['content'][0]['text'])['model_plan']['claude']['effort'],'low')
