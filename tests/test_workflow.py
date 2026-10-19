import copy
import json
from pathlib import Path
from base import DataCase
import workflow as w

class WorkflowTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api'}]})
        self.task=dict(id='AL-03',title='Проверяемая правка',why='Нужна пользователю',root='request',
            size='S',priority='🟡',risk='low',work_type='implementation',acceptance=['Тест проходит'],depends_on=[])

    def plan(self, tasks=None):
        return w.action(dict(op='plan',project='alpha-api',tasks=tasks or [self.task]))

    def test_invalid_plan_is_atomic(self):
        before=(Path(self.data)/'projects.json').read_bytes()
        second=dict(self.task,id='AL-04',depends_on=['AL-04'])
        with self.assertRaises(ValueError): self.plan([self.task,second])
        self.assertEqual(before,(Path(self.data)/'projects.json').read_bytes())

    def test_unknown_root_and_dependencies(self):
        for patch in ({'root':'missing'},{'depends_on':['missing']},{'size':'XL'},{'risk':'unknown'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError): self.plan([dict(self.task,**patch)])

    def test_routing_does_not_default_to_flagship(self):
        self.assertEqual(w.recommendation(self.task)['model'],'gpt-6-luna')
        self.assertEqual(w.recommendation(dict(self.task,size='M'))['model'],'gpt-6.1-sol')
        self.assertEqual(w.recommendation(dict(self.task,risk='high'))['model'],'gpt-6-astra')

    def test_dependency_blocks_launch(self):
        self.plan([dict(self.task,depends_on=['AL-01'])])
        with self.assertRaises(ValueError): w.action(dict(op='enqueue',ref='alpha-api/AL-03'))
        self.assertFalse(w.action(dict(op='next')))

    def test_lifecycle_and_canonical_status(self):
        self.plan(); ref='alpha-api/AL-03'
        w.action(dict(op='enqueue',ref=ref))
        with self.assertRaises(ValueError): w.action(dict(op='enqueue',ref=ref))
        self.assertEqual(w.action(dict(op='next')),[])
        with self.assertRaises(ValueError): w.action(dict(op='claim',ref=ref,run='RUN-1',worker='test',model='gpt-6-astra'))
        w.action(dict(op='claim',ref=ref,run='RUN-1',worker='test',model='gpt-6-luna'))
        with self.assertRaises(ValueError): w.action(dict(op='update',ref=ref,changes={'risk':'high'}))
        with self.assertRaises(ValueError): w.action(dict(op='finish',ref=ref,run='RUN-1'))
        w.action(dict(op='finish',ref=ref,run='RUN-1',evidence='unit: 12 passed',tokens=450))
        self.assertEqual(w.read('projects.json',{})['projects']['alpha-api']['backlog'][-1]['status'],'done')
        self.assertEqual(w.state()['runs'][0]['tokens'],450)
        with self.assertRaises(ValueError): w.action(dict(op='enqueue',ref=ref))

    def test_focus_and_priority(self):
        self.plan()
        w.action(dict(op='focus',projects=['alpha-api']))
        w.action(dict(op='update',ref='alpha-api/AL-03',changes={'priority':'🔴'}))
        t=w.action(dict(op='next'))[0]
        self.assertEqual(t['rank'][:2],[0,0])
        with self.assertRaises(ValueError): w.action(dict(op='focus',projects=['unknown']))

    def test_packet_bounded_and_policy(self):
        self.plan(); p=w.packet('alpha-api/AL-03')
        self.assertLess(len(json.dumps(p,ensure_ascii=False)),6000)
        self.assertEqual(p['route']['max_attempts'],2)
        w.action(dict(op='update',ref='alpha-api/AL-03',changes={'acceptance':['x'*7000]}))
        with self.assertRaises(ValueError): w.packet('alpha-api/AL-03')

    def test_lock_prevents_parallel_write(self):
        with w.locked(), self.assertRaises(ValueError): self.plan()

    def test_cancel_allows_retry_without_losing_history(self):
        self.plan(); ref='alpha-api/AL-03'
        w.action(dict(op='enqueue',ref=ref))
        w.action(dict(op='cancel',ref=ref,run='RUN-1',evidence='Перепланирование'))
        w.action(dict(op='enqueue',ref=ref))
        self.assertEqual([r['status'] for r in w.state()['runs']],['cancelled','queued'])

    def test_failure_limit_requires_new_plan_or_model(self):
        self.plan(); ref='alpha-api/AL-03'
        for i in (1,2):
            w.action(dict(op='enqueue',ref=ref))
            w.action(dict(op='claim',ref=ref,run='RUN-'+str(i),worker='test',model='gpt-6-luna'))
            w.action(dict(op='fail',ref=ref,run='RUN-'+str(i),evidence='Проверка не прошла',tokens=100))
        with self.assertRaises(ValueError): w.action(dict(op='enqueue',ref=ref))
        w.action(dict(op='update',ref=ref,changes={'model':'gpt-6.1-sol','model_reason':'Две попытки показали сложность интеграции'}))
        w.action(dict(op='enqueue',ref=ref))
        self.assertEqual(w.state()['runs'][-1]['packet']['route']['model'],'gpt-6.1-sol')
        metric=next(m for m in w.action(dict(op='models'))['measurements'] if m['model']=='gpt-6-luna')
        self.assertEqual((metric['completed'],metric['passed'],metric['known_tokens']),(2,0,200))

    def test_mcp_uses_same_workflow(self):
        import mcp_server
        result=mcp_server.call_tool('tt_work',dict(op='plan',project='alpha-api',tasks=[self.task]))
        self.assertFalse(result['isError'])
        result=mcp_server.call_tool('tt_work',dict(op='packet',ref='alpha-api/AL-03'))
        self.assertEqual(json.loads(result['content'][0]['text'])['route']['model'],'gpt-6-luna')

    def test_due_date_and_checklist_validation(self):
        with self.assertRaises(ValueError): self.plan([dict(self.task,due_date='2026-02-30')])
        self.plan(); ref='alpha-api/AL-03'
        for changes in ({'due_date':'tomorrow'}, {'acceptance_done':[0,0]}, {'acceptance_done':[1]}, {'acceptance_done':[True]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): w.action(dict(op='update',ref=ref,changes=changes))
        w.action(dict(op='update',ref=ref,changes={'due_date':'2026-10-20','acceptance_done':[0]}))
        self.assertEqual(w.task_for(ref)['acceptance_done'],[0])
        w.action(dict(op='update',ref=ref,changes={'acceptance':['Новый критерий']}))
        self.assertEqual(w.task_for(ref)['acceptance_done'],[])

    def test_manual_completion_requires_checks_and_evidence(self):
        self.plan(); ref='alpha-api/AL-03'
        with self.assertRaises(ValueError):w.action(dict(op='complete',ref=ref,evidence='Проверено'))
        w.action(dict(op='update',ref=ref,changes={'acceptance_done':[0]}))
        with self.assertRaises(ValueError):w.action(dict(op='complete',ref=ref,evidence=''))
        w.action(dict(op='complete',ref=ref,evidence='Тест воспроизведён, результат соответствует'))
        self.assertEqual(w.task_for(ref)['status'],'done')
        self.assertTrue(w.task_for(ref)['completed_at'])

    def test_manual_completion_cannot_bypass_active_run(self):
        self.plan(); ref='alpha-api/AL-03'
        w.action(dict(op='update',ref=ref,changes={'acceptance_done':[0]}))
        w.action(dict(op='enqueue',ref=ref))
        with self.assertRaises(ValueError):w.action(dict(op='complete',ref=ref,evidence='Проверено'))
