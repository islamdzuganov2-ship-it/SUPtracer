"""Запуск сессии ИИ с доски (TT-60) и рабочая копия задачи (TT-44)."""
import os
import subprocess
from pathlib import Path
from unittest import mock
from base import DataCase
import launcher
import workflow as w


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


class LauncherTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api','kind':'запрос','status':'активна','title':'Запрос'}]})
        self.repo=Path(self.tmp)/'repos'/'alpha-api'
        w.action(dict(op='plan',project='alpha-api',tasks=[dict(id='AL-03',title='Правка «в кавычках»',why='Нужна',root='request',size='S',
            priority='🟡',risk='low',task_type='dev_by_spec',spec_ref='docs/ТЗ.md',acceptance=['Тест проходит'])]))
        self._saved_env={k:os.environ.get(k) for k in ('APPDATA','LOCALAPPDATA',*launcher.ENV_BIN.values())}
        for k in launcher.ENV_BIN.values(): os.environ.pop(k,None)

    def tearDown(self):
        for k,v in self._saved_env.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v
        super().tearDown()

    def make_git(self):
        git('init','-q','-b','разработка',cwd=self.repo)
        git('-c','user.name=t','-c','user.email=t@t','commit','-q','--allow-empty','-m','init',cwd=self.repo)

    # --- поиск CLI

    def test_finds_newest_bundled_cli_and_env_override(self):
        app=Path(self.tmp)/'appdata'
        for ver in ('2.1.281','2.1.295'):
            exe=app/'Claude'/'claude-code'/ver/'abc'/launcher.NAMES['claude']; exe.parent.mkdir(parents=True); exe.write_text('')
        os.environ['APPDATA']=str(app)
        with mock.patch.object(launcher.shutil,'which',return_value=None):
            self.assertIn('2.1.295',launcher.find_cli('claude'))
            os.environ['TASKTREKER_CLAUDE_BIN']=str(Path(self.tmp)/'нет.exe')
            self.assertIsNone(launcher.find_cli('claude'))
            os.environ['LOCALAPPDATA']=str(Path(self.tmp)/'пусто')
            self.assertFalse(launcher.status()['codex']['available'])

    def test_command_is_argument_list_with_model_effort_and_prompt(self):
        c=launcher.command('claude','claude.exe','claude-haiku-5-5','medium','задание "с кавычками"','C:/p')
        self.assertEqual(c,['claude.exe','--model','claude-haiku-5-5','--effort','medium','задание "с кавычками"'])
        c=launcher.command('codex','codex.exe','gpt-6-luna','low','задание','C:/p')
        self.assertEqual(c,['codex.exe','-m','gpt-6-luna','-c','model_reasoning_effort="low"','-C','C:/p','задание'])

    # --- рабочая копия задачи

    def test_worktree_created_once_on_task_branch(self):
        self.assertIsNone(launcher.ensure_worktree(self.repo,'claude','AL-03'))   # не git
        self.make_git()
        a=launcher.ensure_worktree(self.repo,'claude','AL-03')
        self.assertEqual((a['branch'],a['base'],a['created']),('ai/claude/al-03','разработка',True))
        self.assertTrue(Path(a['path'],'.git').exists())
        self.assertFalse(Path(a['path']).is_relative_to(self.repo))
        b=launcher.ensure_worktree(self.repo,'claude','AL-03')
        self.assertEqual((b['path'],b['created']),(a['path'],False))
        other=launcher.ensure_worktree(self.repo,'codex','AL-03')
        self.assertNotEqual(other['path'],a['path'])

    # --- запуск через сервер

    def test_launch_session_opens_window_and_marks_run(self):
        import serve
        self.make_git()
        with mock.patch.object(launcher,'find_cli',return_value='C:/bin/claude.exe'), \
             mock.patch.object(launcher,'open_window',return_value=4242) as win:
            r=serve.launch_session(dict(ref='alpha-api/AL-03',agent='claude'))
        cmd,cwd,env=win.call_args[0][0],win.call_args[0][1],win.call_args[0][2]
        self.assertEqual(cmd[:5],['C:/bin/claude.exe','--model','claude-haiku-5-5','--effort','medium'])
        self.assertIn('tt_work op=claim ref=alpha-api/AL-03 run=RUN-1 model=claude-haiku-5-5 worker=claude-cli:RUN-1',cmd[-1])
        self.assertIn('ai/claude/al-03',cmd[-1])
        self.assertEqual(cwd,r['worktree']['path'])
        self.assertEqual(env['TASKTREKER_DATA'],w.data_dir().as_posix())
        run=w.state()['runs'][0]
        self.assertEqual((run['status'],run['launch']['pid'],run['agent']),('queued',4242,'claude'))
        self.assertEqual(w.packet('alpha-api/AL-03',agent='claude')['workspace']['worktree']['branch'],'ai/claude/al-03')
        # Повторный запуск — та же очередь и та же рабочая копия, история попыток.
        with mock.patch.object(launcher,'find_cli',return_value='C:/bin/claude.exe'), mock.patch.object(launcher,'open_window',return_value=4343):
            serve.launch_session(dict(ref='alpha-api/AL-03',agent='claude'))
        run=w.state()['runs'][0]
        self.assertEqual((len(w.state()['runs']),len(run['launches'])),(1,2))
        # Принятие делает сам ИИ.
        w.action(dict(op='claim',ref='alpha-api/AL-03',run='RUN-1',worker='claude-cli:RUN-1',model='claude-haiku-5-5'))
        self.assertEqual(w.task_for('alpha-api/AL-03')['claim']['worker'],'claude-cli:RUN-1')

    def test_owner_choice_repins_waiting_launch(self):
        import serve
        w.action(dict(op='enqueue',ref='alpha-api/AL-03',agent='codex'))
        with mock.patch.object(launcher,'find_cli',return_value='C:/bin/x.exe'), mock.patch.object(launcher,'open_window',return_value=5) as win:
            r=serve.launch_session(dict(ref='alpha-api/AL-03',agent='claude',isolate=False))
        run=w.state()['runs'][0]
        self.assertEqual((len(w.state()['runs']),run['agent'],r['route']['model']),(1,'claude','claude-haiku-5-5'))
        self.assertEqual(run['repins'][0]['from_agent'],'codex')
        self.assertEqual(win.call_args[0][0][1:3],['--model','claude-haiku-5-5'])   # окно — именно Claude
        # Без явного выбора сервер держится закрепления запуска, а не ИИ по умолчанию.
        w.action(dict(op='settings',changes={'default_executor':'codex'}))
        with mock.patch.object(launcher,'find_cli',return_value='C:/bin/x.exe'), mock.patch.object(launcher,'open_window',return_value=6):
            self.assertEqual(serve.launch_session(dict(ref='alpha-api/AL-03',isolate=False))['agent'],'claude')

    def test_repin_rules(self):
        import serve
        w.action(dict(op='update',ref='alpha-api/AL-03',changes={'executor':'claude'}))
        w.action(dict(op='enqueue',ref='alpha-api/AL-03',agent='claude'))
        with self.assertRaisesRegex(ValueError,'назначена ИИ Claude'):
            w.action(dict(op='repin',ref='alpha-api/AL-03',run='RUN-1',agent='codex'))
        w.action(dict(op='claim',ref='alpha-api/AL-03',run='RUN-1',worker='c',model='claude-haiku-5-5'))
        with self.assertRaisesRegex(ValueError,'ждущего запуска'):
            w.action(dict(op='repin',ref='alpha-api/AL-03',run='RUN-1',agent='claude'))

    def test_launch_refuses_missing_cli(self):
        import serve
        w.action(dict(op='enqueue',ref='alpha-api/AL-03',agent='codex'))
        with mock.patch.object(launcher,'find_cli',return_value=None), mock.patch.object(launcher,'open_window') as win:
            with self.assertRaises(ValueError):
                serve.launch_session(dict(ref='alpha-api/AL-03',agent='codex',isolate=False))
            win.assert_not_called()
        self.assertNotIn('launched_at',w.state()['runs'][0])

    def test_diagnose_codex_session_error(self):
        import json as _json
        home=Path(self.tmp)/'codex'; day=home/'sessions'/'2026'/'10'/'19'; day.mkdir(parents=True)
        cwd=str(Path(self.tmp)/'wt')
        rows=[dict(type='session_meta',payload=dict(cwd=cwd)),
              dict(type='event_msg',payload=dict(type='task_complete',error=dict(message='You’ve hit your usage limit. Try again at Oct 24th',codex_error_info='usage_limit_exceeded')))]
        (day/'rollout-x.jsonl').write_text('\n'.join(_json.dumps(r) for r in rows),encoding='utf-8')
        other=dict(rows[0],payload=dict(cwd=str(Path(self.tmp)/'другая')))
        (day/'rollout-y.jsonl').write_text(_json.dumps(other),encoding='utf-8')
        old=os.environ.get('CODEX_HOME'); os.environ['CODEX_HOME']=str(home)
        try:
            run=dict(launched_at=w.now().isoformat(),launch=dict(agent='codex',cwd=cwd))
            d=launcher.diagnose(run)
            self.assertEqual(d['code'],'usage_limit_exceeded'); self.assertIn('Oct 24th',d['message'])
            self.assertIsNone(launcher.diagnose(dict(run,launch=dict(agent='codex',cwd=str(Path(self.tmp)/'другая')))))
            self.assertIsNone(launcher.diagnose(dict(run,launch=dict(agent='claude',cwd=cwd))))
        finally:
            if old is None: os.environ.pop('CODEX_HOME',None)
            else: os.environ['CODEX_HOME']=old

    def test_finish_in_task_branch_goes_to_review(self):
        import serve
        self.make_git()
        with mock.patch.object(launcher,'find_cli',return_value='C:/bin/claude.exe'), mock.patch.object(launcher,'open_window',return_value=1):
            serve.launch_session(dict(ref='alpha-api/AL-03',agent='claude'))
        w.action(dict(op='claim',ref='alpha-api/AL-03',run='RUN-1',worker='claude-cli:RUN-1',model='claude-haiku-5-5'))
        with mock.patch.object(w,'spawn_autocheck') as spawn:
            r=w.action(dict(op='finish',ref='alpha-api/AL-03',run='RUN-1',worker='claude-cli:RUN-1',evidence='Тесты прошли, коммит abc'))
        spawn.assert_called_once_with('alpha-api/AL-03','RUN-1')
        self.assertEqual((r['status'],r['branch']),('review','ai/claude/al-03'))
        t=w.task_for('alpha-api/AL-03')
        self.assertEqual(t['status'],'review'); self.assertIn('ai/claude/al-03',t['status_note'])
        # Владелец слил ветку и закрывает задачу проверкой критериев.
        w.action(dict(op='update',ref='alpha-api/AL-03',changes={'acceptance_done':[0]}))
        w.action(dict(op='complete',ref='alpha-api/AL-03',evidence='Ветка слита в разработку, гейт пройден'))
        self.assertEqual(w.task_for('alpha-api/AL-03')['status'],'done')
        self.assertNotIn('status_note',w.read('projects.json',{})['projects']['alpha-api']['backlog'][-1])

    # --- TT-66: настоящий MCP и чужая копия кода

    def test_tt_work_callable_over_json_rpc(self):
        import json as _json, sys as _sys
        server=Path(launcher.__file__).with_name('mcp_server.py')
        msgs=[dict(jsonrpc='2.0',id=1,method='initialize',params=dict(protocolVersion='2025-06-18',capabilities={},clientInfo=dict(name='t',version='1'))),
              dict(jsonrpc='2.0',method='notifications/initialized'),
              dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='tt_work',arguments=dict(op='packet',ref='alpha-api/AL-03',agent='claude')))]
        out=subprocess.run([_sys.executable,str(server)],input='\n'.join(_json.dumps(m) for m in msgs)+'\n',capture_output=True,text=True,
                           encoding='utf-8',timeout=60,env=dict(os.environ,TASKTREKER_DATA=str(self.data),PYTHONIOENCODING='utf-8')).stdout
        reply=next(_json.loads(l) for l in out.splitlines() if l.strip() and _json.loads(l).get('id')==2)
        self.assertNotIn('error',reply,reply)
        self.assertEqual(_json.loads(reply['result']['content'][0]['text'])['route']['model'],'claude-haiku-5-5')

    def test_task_copy_refuses_to_write_shared_data(self):
        import pm
        os.environ['TASKTREKER_SYSTEM']=str(Path(self.tmp)/'system'/'tools')
        try:
            self.assertIn('рабочая копия задачи',w.foreign_copy())
            self.assertEqual(w.main(['list']),2)
            with mock.patch.object(pm.sys,'argv',['pm.py','work','list']):
                self.assertEqual(pm.main(),2)
            os.environ['TASKTREKER_SYSTEM']=str(Path(w.__file__).parent)
            self.assertIsNone(w.foreign_copy())
        finally:
            os.environ.pop('TASKTREKER_SYSTEM',None)

    def test_prompt_requires_mcp_and_names_system_cli(self):
        r=w.action(dict(op='enqueue',ref='alpha-api/AL-03',agent='claude'))
        self.assertIn('только через MCP-инструмент tt_work',r['prompt'])
        self.assertIn(str(w.ROOT/'tools'/'pm.py'),r['prompt'])
