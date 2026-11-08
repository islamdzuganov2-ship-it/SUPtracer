"""Фаза оценки (TT-57), видимый запуск (TT-55) и ручные статусы (TT-56)."""
from datetime import timedelta
from pathlib import Path
from unittest import mock
from base import DataCase
import workflow as w


class FlowTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api','kind':'запрос','status':'активна','title':'Запрос'}]})
        self.dev=dict(id='AL-03',title='Правка',why='Нужна',root='request',size='S',priority='🟡',risk='low',
                      task_type='dev_by_spec',spec_ref='docs/ТЗ.md',acceptance=['Тест проходит'])
        self.ana=dict(id='AL-04',title='Аналитика формата',why='Нужно ТЗ',root='request',size='S',priority='🟡',risk='low',
                      task_type='analysis_spec',question='Какой формат',inputs='Пожелания',deliverable='docs/ТЗ_2.md',acceptance=['ТЗ записано'])

    def plan(self, *tasks):
        return w.action(dict(op='plan',project='alpha-api',tasks=list(tasks)))

    # --- фаза оценки

    def test_analysis_starts_with_assessment_on_model_as_is(self):
        self.plan(self.ana); ref='alpha-api/AL-04'
        t=w.task_for(ref)
        self.assertTrue(t['complexity']['needs_assessment'])
        p=w.packet(ref, agent='claude')
        self.assertEqual(p['phase'],'assess')
        self.assertEqual(p['route']['total_token_budget'],w.ASSESS_BUDGET)
        self.assertIn('op=assess',p['assess']['call'])

    def test_assessment_assigns_model_and_queues_execution(self):
        self.plan(self.ana); ref='alpha-api/AL-04'
        model=w.packet(ref,agent='claude')['route']['model']
        w.action(dict(op='take',ref=ref,model=model,worker='c1'))
        with self.assertRaisesRegex(ValueError,'op=assess'):
            w.action(dict(op='finish',ref=ref,run='RUN-1',worker='c1',evidence='x'))
        with self.assertRaises(ValueError):
            w.action(dict(op='assess',ref=ref,run='RUN-1',worker='other',assessment=dict(complexity=4,uncertainty=4,breadth=3,reasons='r')))
        r=w.action(dict(op='assess',ref=ref,run='RUN-1',worker='c1',tokens=1500,
                        assessment=dict(complexity=4,uncertainty=4,breadth=3,reasons='Много источников и неясный формат')))
        self.assertEqual(r['run'],'RUN-2')
        self.assertTrue(r['switch'])                       # оценка подняла класс
        self.assertNotEqual(r['route']['model'],model)
        self.assertIn('claim',r['prompt'])
        t=w.task_for(ref)
        self.assertEqual((t['status'],t['assessment']['by']['model']),('queued',model))
        self.assertFalse(t['complexity']['needs_assessment'])
        runs=w.state()['runs']
        self.assertEqual([(x['status'],x.get('phase'),x.get('agent')) for x in runs],[('done','assess','claude'),('queued','execute','claude')])
        with self.assertRaises(ValueError):               # чужой ИИ исполнение не примет
            w.action(dict(op='claim',ref=ref,run='RUN-2',worker='g1',model=w.recommendation(t,agent='codex')['model']))
        self.assertEqual(w.packet(ref,agent='claude')['phase'],'execute')
        w.action(dict(op='claim',ref=ref,run='RUN-2',worker='c2',model=r['route']['model']))
        w.action(dict(op='finish',ref=ref,run='RUN-2',worker='c2',evidence='ТЗ записано',tokens=9000))
        self.assertEqual(w.task_for(ref)['status'],'done')

    def test_simple_assessment_lowers_model(self):
        self.plan(dict(self.ana,size='L',risk='medium')); ref='alpha-api/AL-04'
        before=w.packet(ref,agent='codex')['route']['model']
        w.action(dict(op='take',ref=ref,model=before,worker='g1'))
        r=w.action(dict(op='assess',ref=ref,run='RUN-1',worker='g1',assessment=dict(complexity=1,uncertainty=1,breadth=1,reasons='Один вопрос, всё известно')))
        self.assertEqual(r['route']['model'],'gpt-6-luna')

    def test_decomposition_subtasks_get_model_by_their_assessment(self):
        sub=lambda i,c: dict(id='AL-1%d'%i,title='Подзадача %d'%i,why='Из декомпозиции',root='request',size='M',priority='🟡',risk='medium',
                             task_type='direction_task',direction='дизайн',deliverable='Макет',acceptance=['Готово'],
                             assessment=dict(complexity=c,uncertainty=c,breadth=c,reasons='Оценка при декомпозиции'))
        self.plan(sub(1,1),sub(2,5))
        easy,hard=w.task_for('alpha-api/AL-11'),w.task_for('alpha-api/AL-12')
        self.assertEqual(easy['model_plan']['claude']['model'],'claude-haiku-5-5')
        self.assertEqual(hard['model_plan']['claude']['model'],'claude-opus-5-5')
        self.assertFalse(easy['complexity']['needs_assessment'])
        with self.assertRaises(ValueError): self.plan(dict(sub(3,3),assessment=dict(complexity=9,uncertainty=1,breadth=1,reasons='x')))

    def test_decomposition_packet_asks_for_subtask_assessment(self):
        self.plan(dict(self.ana,id='AL-05',task_type='decomposition',work_type='architecture',directions=['дизайн','продажи']))
        self.assertIn('assessment',w.packet('alpha-api/AL-05')['decomposition'])

    # --- видимый запуск

    def test_enqueue_returns_prompt_for_selected_ai(self):
        self.plan(self.dev)
        r=w.action(dict(op='enqueue',ref='alpha-api/AL-03',agent='claude'))
        self.assertEqual((r['run'],r['route']['provider']),('RUN-1','claude'))
        self.assertIn('tt_work op=claim ref=alpha-api/AL-03 run=RUN-1 model=claude-haiku-5-5',r['prompt'])
        self.assertIn('repos',r['prompt'].replace('\\','/'))   # рабочая папка проекта
        self.assertEqual(w.task_for('alpha-api/AL-03')['queued_run'],'RUN-1')

    # --- ручные статусы

    def test_manual_status_transitions(self):
        self.plan(self.dev); ref='alpha-api/AL-03'
        st=lambda s,reason='': w.action(dict(op='status',ref=ref,status=s,reason=reason))
        with self.assertRaisesRegex(ValueError,'причину'): st('blocked')
        st('blocked','Ждём доступ к API')
        t=w.task_for(ref); self.assertEqual((t['status'],t['status_note']),('blocked','Ждём доступ к API'))
        st('queued')
        self.assertNotIn('status_note',w.read('projects.json',{})['projects']['alpha-api']['backlog'][-1])
        st('in_progress'); st('review')
        with self.assertRaises(ValueError): st('done')
        st('cancelled','Не нужна')
        hist=w.task_for(ref)['status_history']
        self.assertEqual([h['to'] for h in hist],['blocked','queued','in_progress','review','cancelled'])
        with self.assertRaises(ValueError): st('cancelled','ещё раз')

    def test_cancel_drops_queued_launch_and_running_needs_cancel(self):
        self.plan(self.dev); ref='alpha-api/AL-03'
        w.action(dict(op='enqueue',ref=ref,agent='claude'))
        w.action(dict(op='status',ref=ref,status='cancelled',reason='Передумали'))
        self.assertEqual(w.state()['runs'][0]['status'],'cancelled')
        w.action(dict(op='status',ref=ref,status='queued'))
        w.action(dict(op='take',ref=ref,model='claude-haiku-5-5',worker='c1'))
        with self.assertRaisesRegex(ValueError,'выполняет claude'):
            w.action(dict(op='status',ref=ref,status='blocked',reason='x'))
        w.action(dict(op='status',ref=ref,status='cancelled',reason='Остановить'))
        self.assertEqual(w.state()['runs'][-1]['status'],'cancelled')
        self.assertNotIn('claimed_by',w.read('projects.json',{})['projects']['alpha-api']['backlog'][-1])

    def test_stale_run_does_not_block_manual_status(self):
        self.plan(self.dev); ref='alpha-api/AL-03'
        w.action(dict(op='take',ref=ref,model='claude-haiku-5-5',worker='c1'))
        with mock.patch.object(w,'now',return_value=w.now()+timedelta(hours=3)):
            w.action(dict(op='status',ref=ref,status='blocked',reason='Исполнитель пропал'))
        self.assertEqual(w.task_for(ref)['status'],'blocked')

    def test_reopen_done_needs_reason(self):
        self.plan(self.dev); ref='alpha-api/AL-03'
        w.action(dict(op='update',ref=ref,changes={'acceptance_done':[0]}))
        w.action(dict(op='complete',ref=ref,evidence='Проверено'))
        with self.assertRaises(ValueError): w.action(dict(op='status',ref=ref,status='queued'))
        with self.assertRaises(ValueError): w.action(dict(op='status',ref=ref,status='blocked',reason='x'))
        w.action(dict(op='status',ref=ref,status='queued',reason='Нашли регресс'))
        self.assertEqual(w.task_for(ref)['status'],'queued')

    def test_legacy_hyphen_status_is_normalized(self):
        d=w.read('projects.json',{}); d['projects']['alpha-api']['backlog'][0]['status']='in-progress'; w.save('projects.json',d)
        self.assertEqual(w.task_for('alpha-api/AL-01')['status'],'in_progress')

    def test_briefing_lists_waiting_launches(self):
        import context
        self.plan(self.dev)
        w.action(dict(op='enqueue',ref='alpha-api/AL-03',agent='claude'))
        out=context.build(budget=4000, focus='alpha-api', hook=True, with_memory=False)
        self.assertIn('ЖДУТ ИСПОЛНИТЕЛЯ',out)
        self.assertIn('tt_work op=claim ref=alpha-api/AL-03 run=RUN-1 model=claude-haiku-5-5',out)
        self.assertNotIn('ЖДУТ ИСПОЛНИТЕЛЯ',context.build(budget=4000, focus='beta-tool', hook=True, with_memory=False))

    def test_mcp_accepts_assess_and_status(self):
        import json, mcp_server
        self.plan(self.dev)
        r=mcp_server.call_tool('tt_work',dict(op='status',ref='alpha-api/AL-03',status='blocked',reason='Ждём'))
        self.assertFalse(r['isError'], r)
        r=mcp_server.call_tool('tt_work',dict(op='assess',ref='alpha-api/AL-03',run='RUN-9',worker='x',assessment=dict(complexity=1,uncertainty=1,breadth=1,reasons='r')))
        self.assertIn('Запуск не найден',r['content'][0]['text'])
