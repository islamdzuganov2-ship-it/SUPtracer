"""Выбор ИИ-исполнителя и параллельная работа двух ИИ в одном проекте (TT-37…TT-39)."""
import os
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock
from base import DataCase
import workflow as w


class ParallelAITests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api'}]})
        self.base=dict(title='Правка',why='Нужна',root='request',size='S',priority='🟡',risk='low',
                       work_type='implementation',acceptance=['Тест проходит'],depends_on=[])

    def plan(self, *tasks):
        return w.action(dict(op='plan',project='alpha-api',tasks=[dict(self.base,**t) for t in tasks]))

    def take(self, tid, model, worker):
        return w.action(dict(op='take',ref='alpha-api/'+tid,model=model,worker=worker))

    # --- выбор ИИ (TT-37)

    def test_route_follows_selected_ai(self):
        self.assertEqual(w.recommendation(self.base, agent='claude')['model'],'claude-haiku-5-5')
        self.assertEqual(w.recommendation(dict(self.base,size='M'), agent='claude')['model'],'claude-sonnet-5-5')
        self.assertEqual(w.recommendation(dict(self.base,risk='high'), agent='claude')['model'],'claude-opus-5-5')
        self.assertEqual(w.recommendation(dict(self.base,executor='claude'), agent='codex')['provider'],'claude')
        self.assertEqual(w.recommendation(self.base)['provider'],'codex')  # прежнее поведение по умолчанию

    def test_flagship_fable_never_chosen_automatically(self):
        for t in (self.base, dict(self.base,risk='high'), dict(self.base,work_type='research',size='L')):
            self.assertNotEqual(w.recommendation(t, agent='claude')['model'],'claude-fable-5-1')

    def test_model_of_other_ai_rejected(self):
        self.plan(dict(id='AL-03',executor='claude'))
        with self.assertRaises(ValueError):
            w.action(dict(op='update',ref='alpha-api/AL-03',changes={'model':'gpt-6-astra','model_reason':'проба'}))
        with self.assertRaises(ValueError): self.plan(dict(id='AL-04',executor='gemini'))

    def test_explicit_model_implies_ai(self):
        self.plan(dict(id='AL-03'))
        w.action(dict(op='update',ref='alpha-api/AL-03',changes={'model':'claude-opus-5-5','model_reason':'Нужна архитектура'}))
        self.assertEqual(w.task_for('alpha-api/AL-03')['route']['provider'],'claude')

    def test_default_executor_setting(self):
        self.plan(dict(id='AL-03'))
        w.action(dict(op='settings',changes={'default_executor':'claude'}))
        self.assertEqual(w.task_for('alpha-api/AL-03')['route']['model'],'claude-haiku-5-5')
        with self.assertRaises(ValueError): w.action(dict(op='settings',changes={'default_executor':'x'}))

    def test_unassigned_task_shows_both_alternatives(self):
        self.plan(dict(id='AL-03'))
        alt=w.task_for('alpha-api/AL-03')['alternatives']
        self.assertEqual(alt,{'codex':'gpt-6-luna','claude':'claude-haiku-5-5'})

    def test_inconsistent_manual_edit_does_not_break_boards(self):
        self.plan(dict(id='AL-03'))
        d=w.read('projects.json',{}); d['projects']['alpha-api']['backlog'][-1].update(executor='claude',model='gpt-6-luna'); w.save('projects.json',d)
        self.assertIn('error',w.task_for('alpha-api/AL-03')['route'])

    # --- метка занятости (TT-38)

    def test_take_marks_task_for_other_ai(self):
        self.plan(dict(id='AL-03'))
        self.take('AL-03','claude-haiku-5-5','claude-chat-1')
        t=w.task_for('alpha-api/AL-03')
        self.assertEqual(t['status'],'in_progress')
        self.assertEqual(t['claimed_by']['agent'],'claude')
        self.assertEqual(t['claim']['worker'],'claude-chat-1')
        with self.assertRaisesRegex(ValueError,'Занято: claude'): self.take('AL-03','gpt-6-luna','codex-1')
        self.assertEqual(w.action(dict(op='next',agent='codex')),[])

    def test_assigned_task_not_taken_by_other_ai(self):
        self.plan(dict(id='AL-03',executor='claude'))
        with self.assertRaisesRegex(ValueError,'назначена ИИ claude'): self.take('AL-03','gpt-6-luna','codex-1')
        self.assertEqual(w.action(dict(op='next',agent='codex')),[])
        self.assertEqual([t['ref'] for t in w.action(dict(op='next',agent='claude'))],['alpha-api/AL-03'])

    def test_model_must_match_route_of_ai(self):
        self.plan(dict(id='AL-03'))
        with self.assertRaises(ValueError): self.take('AL-03','claude-opus-5-5','claude-1')
        with self.assertRaises(ValueError): self.take('AL-03','unknown-model','x')

    def test_expired_lease_is_taken_over_with_history(self):
        self.plan(dict(id='AL-03'))
        self.take('AL-03','claude-haiku-5-5','claude-1')
        later=w.now()+timedelta(minutes=w.LEASE_MINUTES+1)
        with mock.patch.object(w,'now',return_value=later):
            self.assertTrue(w.task_for('alpha-api/AL-03')['claim']['stale'])
            self.assertEqual([t['ref'] for t in w.action(dict(op='next',agent='codex'))],['alpha-api/AL-03'])
            self.take('AL-03','gpt-6-luna','codex-1')
        runs=w.state()['runs']
        self.assertEqual([r['status'] for r in runs],['expired','running'])
        self.assertIn('codex-1',runs[0]['evidence'])
        self.assertEqual(w.task_for('alpha-api/AL-03')['claimed_by']['agent'],'codex')

    def test_heartbeat_extends_lease_only_for_owner(self):
        self.plan(dict(id='AL-03'))
        self.take('AL-03','claude-haiku-5-5','claude-1')
        with self.assertRaises(ValueError):
            w.action(dict(op='heartbeat',ref='alpha-api/AL-03',run='RUN-1',worker='codex-1'))
        later=w.now()+timedelta(minutes=40)
        with mock.patch.object(w,'now',return_value=later):
            w.action(dict(op='heartbeat',ref='alpha-api/AL-03',run='RUN-1',worker='claude-1'))
        with mock.patch.object(w,'now',return_value=later+timedelta(minutes=30)):
            self.assertFalse(w.task_for('alpha-api/AL-03')['claim']['stale'])

    def test_finish_clears_mark(self):
        self.plan(dict(id='AL-03'))
        self.take('AL-03','claude-haiku-5-5','claude-1')
        with self.assertRaises(ValueError):
            w.action(dict(op='finish',ref='alpha-api/AL-03',run='RUN-1',worker='codex-1',evidence='чужой'))
        w.action(dict(op='finish',ref='alpha-api/AL-03',run='RUN-1',worker='claude-1',evidence='unit ok',tokens=900))
        raw=w.read('projects.json',{})['projects']['alpha-api']['backlog'][-1]
        self.assertEqual(raw['status'],'done'); self.assertNotIn('claimed_by',raw)
        self.assertEqual(w.state()['runs'][0]['agent'],'claude')

    def test_board_queue_claimed_by_any_ai(self):
        self.plan(dict(id='AL-03'))
        w.action(dict(op='enqueue',ref='alpha-api/AL-03'))
        self.assertEqual(len(w.action(dict(op='queue',agent='claude'))),1)
        w.action(dict(op='claim',ref='alpha-api/AL-03',run='RUN-1',worker='claude-1',model='claude-haiku-5-5'))
        self.assertEqual(w.state()['runs'][0]['packet']['route']['provider'],'claude')

    def test_scope_conflict_blocks_parallel_edit_of_same_files(self):
        self.plan(dict(id='AL-03',scope=['tools/workflow.py']), dict(id='AL-04',scope=['tools']),
                  dict(id='AL-05',scope=['tests/']))
        self.take('AL-03','claude-haiku-5-5','claude-1')
        with self.assertRaisesRegex(ValueError,'пересекается'): self.take('AL-04','gpt-6-luna','codex-1')
        self.assertEqual([t['ref'] for t in w.action(dict(op='next',agent='codex'))],['alpha-api/AL-05'])
        self.take('AL-05','gpt-6-luna','codex-1')
        p=w.packet('alpha-api/AL-05', agent='codex')
        self.assertEqual(p['coordination']['parallel'][0]['agent'],'claude')
        self.assertEqual(p['coordination']['branch'],'ai/codex/al-05')

    def test_scope_validation_and_overlap(self):
        self.assertTrue(w.scopes_overlap(['./Tools/'],['tools/app.js']))
        self.assertFalse(w.scopes_overlap(['tools/app'],['tools/app.js']))
        self.assertTrue(w.scopes_overlap(['.'],['docs']))
        for bad in (['../etc'], 'tools', ['']):
            with self.subTest(bad=bad), self.assertRaises(ValueError): self.plan(dict(id='AL-09',scope=bad))

    def test_stale_lock_file_is_recovered(self):
        lock=Path(self.data)/'.workflow.lock'; lock.write_text('')
        with self.assertRaises(ValueError): self.plan(dict(id='AL-03'))
        old=time.time()-w.LOCK_STALE_SECONDS-5; os.utime(lock,(old,old))
        self.plan(dict(id='AL-03'))
        self.assertFalse(lock.exists())

    # --- распределение (TT-39)

    def test_split_balances_and_keeps_chains_together(self):
        self.plan(dict(id='A1',size='L'), dict(id='A2',depends_on=['A1']), dict(id='B1',size='M'),
                  dict(id='B2',size='M',scope=['docs']), dict(id='B3',scope=['docs/x.md']), dict(id='C1',executor='claude',size='M'))
        before=w.read('projects.json',{})
        r=w.action(dict(op='split',project='alpha-api',agents=['claude','codex']))
        self.assertTrue(r['dry_run']); self.assertEqual(before,w.read('projects.json',{}))
        a=r['assign']
        self.assertEqual(a['alpha-api/A1'],a['alpha-api/A2'])
        self.assertEqual(a['alpha-api/B2'],a['alpha-api/B3'])
        self.assertNotIn('alpha-api/C1',a)
        self.assertLessEqual(abs(r['load']['claude']-r['load']['codex']),8)
        w.action(dict(op='split',project='alpha-api',agents=['claude','codex'],dry_run=False))
        self.assertEqual(w.task_for('alpha-api/A2')['executor'],a['alpha-api/A2'])

    def test_split_counts_work_already_held(self):
        self.plan(dict(id='A1',size='L'), dict(id='B1'))
        self.take('A1','claude-sonnet-5-5','claude-1')
        r=w.action(dict(op='split',project='alpha-api',agents=['claude','codex']))
        self.assertEqual(r['load']['claude'],8)   # держит A1, хоть она и не назначена
        self.assertEqual(r['assign']['alpha-api/B1'],'codex')
        self.assertNotIn('alpha-api/A1',r['assign'])

    def test_split_follows_pinned_dependency(self):
        self.plan(dict(id='A1',size='L'), dict(id='A2',depends_on=['A1'],executor='codex'))
        r=w.action(dict(op='split',project='alpha-api',agents=['claude','codex']))
        self.assertEqual(r['assign']['alpha-api/A1'],'codex')

    def test_split_requires_two_known_ai(self):
        for agents in (['claude'], ['claude','claude'], ['claude','gemini']):
            with self.subTest(agents=agents), self.assertRaises(ValueError):
                w.action(dict(op='split',project='alpha-api',agents=agents))

    def test_mcp_schema_accepts_new_fields(self):
        import mcp_server, json
        self.plan(dict(id='AL-03'))
        r=mcp_server.call_tool('tt_work',dict(op='take',ref='alpha-api/AL-03',model='claude-haiku-5-5',worker='c1',agent='claude'))
        self.assertFalse(r['isError'], r)
        r=mcp_server.call_tool('tt_work',dict(op='heartbeat',ref='alpha-api/AL-03',run='RUN-1',worker='c1',minutes=60))
        self.assertFalse(r['isError'], r)
        r=mcp_server.call_tool('tt_work',dict(op='split',project='alpha-api',agents=['claude','codex'],dry_run=True))
        self.assertTrue(json.loads(r['content'][0]['text'])['dry_run'])
