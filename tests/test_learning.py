"""Механизм развития локального разработчика (TT-110…TT-118; ТЗ LEARN-01…09, приёмочные AT-01…AT-20).

Локальная модель подменена mock-сервером OpenAI-совместимого API: тесты не требуют GPU (§14.1).
Синтетические задачи доказывают работу механизма, а не экономию или качество модели.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from base import DataCase, ROOT
import learn
import learning_causes as lc
import learning_events as le
import learning_gate as lg
import learning_knowledge as lk
import learning_metrics as lm
import local_mimo
import memory
import orchestra
import workflow as w

T0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
CFG = dict(model_id='mimo-v2.6-9b', quantization='Q4_K_M', runtime_name='llama.cpp', runtime_version='b5000',
           context_limit=8192, endpoint='http://127.0.0.1:18080', weight_sha256='sha256:' + 'a' * 64)


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def ptask(i, returns=(), done=True, project='alpha-api', start=0, spec_change=False, work_type='implementation'):
    """Задача с историей конвейера: сдачи разработчика, возвраты, приёмка."""
    ref = '%s/LT-%02d' % (project, i)
    hist, runs, m = [], [], start
    n = len(returns) + 1
    for k in range(n):
        run = 'RUN-%s-%d' % (i, k)
        runs.append(dict(id=run, ref=ref, role='developer', agent='local_mimo', model='mimo-v2.6-9b', status='done',
                         claimed_at=at(m), finished_at=at(m + 30),
                         learning=dict(model_config_id='cfg-A', spec_revision='spec-%d' % (k if spec_change else 0)),
                         tokens=dict(input=1000, output=200)))
        m += 30
        hist.append(dict(kind='handoff', role='developer', run=run, at=at(m), accepted=True, agent='local_mimo', model='mimo-v2.6-9b'))
        if k < len(returns):
            m += 5
            hist.append(dict(kind='return', stage='review', role='reviewer', to_stage='development', at=at(m),
                             reason=returns[k], defects=['D-%d' % k], run='RUN-R-%s-%d' % (i, k)))
    t = dict(ref=ref, id='LT-%02d' % i, project=project, title='Задача %d' % i, work_type=work_type, size='S',
             scope=['app/'], status='done' if done else 'in_progress',
             pipeline=dict(stage='done' if done else 'development', history=hist), completed_at=at(m + 10) if done else None)
    return t, runs


def corpus(specs):
    tasks, runs = [], []
    for i, kw in enumerate(specs):
        t, r = ptask(i, **kw)
        tasks.append(t); runs += r
    return tasks, dict(runs=runs)


def rejections(ref=None):
    return [e for e in le.read_all() if e['event_type'] == 'attempt_rejected' and (not ref or e['task_ref'] == ref)]


class Base(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data) / 'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': []})

    def sync(self, specs):
        tasks, state = corpus(specs)
        le.sync(tasks, state)
        return tasks, state

    def report(self, tasks, state, **kw):
        return lm.compute(events=le.read_all(), tasks=tasks, state=state, **kw)


class Events(Base):
    def test_at01_single_accepted_attempt(self):
        tasks, state = self.sync([dict()])
        rep = self.report(tasks, state)
        self.assertEqual((rep['accepted'], rep['open']), (1, 0))
        self.assertEqual((rep['metrics']['M01']['numerator'], rep['metrics']['M01']['denominator']), (1, 1))
        self.assertEqual(rep['metrics']['M03']['value'], 1.0)
        self.assertEqual(len([e for e in le.read_all() if e['event_type'] == 'attempt_submitted']), 1)

    def test_at02_return_and_resubmit(self):
        tasks, state = self.sync([dict(returns=['Ревьюер: не обработан пустой список'])])
        rep = self.report(tasks, state)
        self.assertEqual(rep['metrics']['M01']['numerator'], 0)
        self.assertEqual(rep['metrics']['M03']['value'], 2.0)
        self.assertEqual(rep['metrics']['M02']['numerator'], 1)
        rej = rejections()
        self.assertEqual(len(rej), 1)
        self.assertTrue(any(x.startswith('pipeline:') for x in rej[0]['evidence_refs']))
        self.assertIn('defect:alpha-api/LT-00/D-0', rej[0]['evidence_refs'])
        self.assertEqual(rej[0]['attempt_no'], 1)
        insp = lm.inspect('alpha-api/LT-00')
        self.assertEqual(len(insp['attempts']), 2); self.assertEqual(len(insp['attempts'][0]['rejections']), 1)

    def test_at05_idempotent(self):
        tasks, state = self.sync([dict(returns=['x'])])
        n = len(le.read_all())
        self.assertEqual(le.sync(tasks, state)['added'], 0)
        self.assertEqual(len(le.read_all()), n)
        e1 = le.record('tool_call', dict(component='t', id=1)); e2 = le.record('tool_call', dict(component='t', id=1))
        self.assertFalse(e1['duplicate']); self.assertTrue(e2['duplicate'])
        v = le.verify(); self.assertTrue(v['ok']); self.assertEqual(v['duplicates'], [])
        self.assertEqual(self.report(tasks, state)['metrics']['M03']['value'], 2.0)

    def test_corrupt_line_does_not_break_reading(self):                          # REQ-DATA-05
        self.sync([dict()])
        f = next(le.events_dir().glob('*.jsonl'))
        with open(f, 'a', encoding='utf-8') as fh:
            fh.write('{битая строка\n')
        self.assertEqual(len(le.read_all()), 2)
        v = le.verify(); self.assertFalse(v['ok']); self.assertEqual(len(v['problems']), 1)

    def test_at11_secret_never_stored(self):
        key = 'sk-' + 'A1b2C3d4' * 4
        self.sync([dict(returns=['упало: api_key=%s в журнале' % key])])
        raw = ''.join(p.read_text(encoding='utf-8') for p in le.events_dir().glob('*.jsonl'))
        self.assertNotIn(key, raw)
        rej = rejections()[0]
        self.assertTrue(rej.get('redacted')); self.assertIn('секрет удалён', rej['reason'])
        # Каталог журнала вне git (REQ-DATA-04).
        self.assertIn('data/learning/', (Path(ROOT) / '.gitignore').read_text(encoding='utf-8'))
        out = subprocess.run(['git', 'check-ignore', '-q', 'data/learning/events/2026-06.jsonl'], cwd=ROOT)
        self.assertEqual(out.returncode, 0)
        # Карточка с секретом не сохраняется.
        ev = rej['event_id']
        lc.classify(ev, 'missing_context', ['test:t1'], by='server', origin='server')
        with self.assertRaisesRegex(ValueError, 'секрет'):
            lk.propose(ev, 'Ключ', 'p', 'a', 'используй token=%s' % key, '', ['app/'])

    def test_at16_existing_data_untouched(self):
        before = {n: hashlib.sha256((Path(self.data) / n).read_bytes()).hexdigest() for n in ('projects.json',)}
        wf = Path(self.data) / 'workflow.json'
        wf_before = wf.read_bytes() if wf.exists() else None
        self.sync([dict(returns=['x']), dict()])
        lc.suggest_all(); learn.snapshot()
        self.assertEqual(before, {n: hashlib.sha256((Path(self.data) / n).read_bytes()).hexdigest() for n in before})
        self.assertEqual(wf_before, wf.read_bytes() if wf.exists() else None)

    def test_disabled_flag_stops_collection(self):                                  # §15.5
        w.action(dict(op='settings', changes=dict(learning=dict(enabled=False))), origin='owner')
        tasks, state = corpus([dict()])
        self.assertTrue(le.sync(tasks, state).get('disabled'))
        self.assertEqual(le.read_all(), [])


class Causes(Base):
    def test_at03_environment_is_not_model(self):
        tasks, state = self.sync([dict(returns=['CI: ModuleNotFoundError: No module named pytest'])])
        lc.suggest_all()
        ev = rejections()[0]['event_id']
        self.assertEqual(lc.current(le.read_all(), ev)['category'], 'environment_failure')
        self.assertEqual(lc.current(le.read_all(), ev)['confidence'], 'unverified')        # предложение — не подтверждение
        lc.classify(ev, 'environment_failure', ['ci:CI-7'], by='server', origin='server')
        c = lc.current(le.read_all(), ev)
        self.assertEqual((c['category'], c['confidence']), ('environment_failure', 'verified'))
        rep = self.report(tasks, state)
        self.assertEqual(rep['metrics']['M04']['numerator'], 0)
        self.assertEqual(rep['metrics']['M07']['value'], 1.0)

    def test_unknown_is_not_model_and_self_claim_does_not_verify(self):
        tasks, state = self.sync([dict(returns=['что-то не так'])])
        ev = rejections()[0]['event_id']
        e = lc.classify(ev, 'model_reasoning_failure', [], by='local_mimo', origin='agent')
        self.assertEqual(e['cause']['confidence'], 'unverified')
        e = lc.classify(ev, 'model_reasoning_failure', ['test:t-empty'], by='reviewer', origin='agent')
        self.assertEqual(e['cause']['confidence'], 'unverified')                 # нет предпосылок REQ-RC-04
        self.assertIn('spec_sufficient', ' '.join(e['cause']['why_unverified']))
        self.assertEqual(self.report(tasks, state)['metrics']['M04']['numerator'], 0)
        pre = dict(spec_sufficient=True, context_available=True, tools_working=True, reproducible=True)
        e = lc.classify(ev, 'model_reasoning_failure', ['test:t-empty'], by='reviewer', origin='agent', preconditions=pre)
        self.assertEqual(e['cause']['confidence'], 'verified')
        self.assertEqual(e['cause']['previous'], 'model_reasoning_failure')
        self.assertEqual(len(lc.history(le.read_all(), ev)), 3)                   # переклассификация хранит историю
        self.assertEqual(self.report(tasks, state)['metrics']['M04']['numerator'], 1)

    def test_at04_requirement_change(self):
        tasks, state = self.sync([dict(returns=['владелец добавил новое требование'], spec_change=True)])
        self.assertEqual(len(lc.requirement_changes(le.read_all())), 1)
        rep = self.report(tasks, state)
        self.assertEqual(rep['metrics']['M02']['numerator'], 1)
        self.assertEqual(rep['metrics']['M02']['excluding_requirement_change'], 0.0)
        lc.suggest_all()
        self.assertEqual(lc.current(le.read_all(), rejections()[0]['event_id'])['category'], 'requirement_change')

    def test_agent_cannot_verify_by_word_only(self):
        self.sync([dict(returns=['x'])])
        ev = rejections()[0]['event_id']
        out = w.action(dict(op='learn', action='classify', event=ev, cause='spec_ambiguity', by='owner'))   # источник agent
        self.assertEqual(out['cause']['confidence'], 'unverified')
        out = w.action(dict(op='learn', action='classify', event=ev, cause='spec_ambiguity'), origin='owner')
        self.assertEqual(out['cause']['confidence'], 'verified')


class Metrics(Base):
    def test_at06_missing_ci_is_unknown(self):
        tasks, state = self.sync([dict()])
        m06 = self.report(tasks, state)['metrics']['M06']
        self.assertEqual((m06['numerator'], m06['denominator']), (0, 1))
        self.assertEqual(lm.metric('X', 'x', 0, 0)['value'], None)               # нет знаменателя — нет доли
        self.assertEqual(lm.metric('X', 'x', 0, 0)['note'], 'нет данных')

    def test_at17_open_tasks_separate(self):
        tasks, state = self.sync([dict() for _ in range(50)] + [dict(done=False) for _ in range(10)])
        rep = self.report(tasks, state)
        self.assertEqual((rep['accepted'], rep['open']), (50, 10))
        self.assertEqual(rep['metrics']['M01']['denominator'], 50)
        self.assertEqual(rep['metrics']['M01']['value'], 1.0)
        self.assertEqual(rep['status'], 'analysable')

    def test_at18_completeness_fields(self):
        tasks, state = self.sync([dict(returns=['x']), dict()])
        rep = self.report(tasks, state)
        for mid, m in rep['metrics'].items():
            for k in ('value', 'numerator', 'denominator', 'n', 'unknown_count', 'coverage', 'sources', 'note'):
                self.assertIn(k, m, (mid, k))
        self.assertEqual(rep['metrics']['M04']['unknown_count'], 1)                # возврат без диагностики
        self.assertEqual(rep['status'], 'insufficient_data')
        self.assertEqual(rep['metrics']['M09']['denominator'], 2)
        self.assertEqual(rep['metrics']['M11']['local_tokens'], 3600)

    def test_at10_config_versions_not_mixed(self):
        a = local_mimo.config_id(dict(CFG)); b = local_mimo.config_id(dict(CFG, quantization='Q5_K_M'))
        c = local_mimo.config_id(dict(CFG, system_prompt_revision='p2'))
        self.assertEqual(len({a, b, c}), 3)
        with self.assertRaisesRegex(ValueError, 'владелец'):
            local_mimo.register(CFG, 'agent')
        with self.assertRaisesRegex(ValueError, '127.0.0.1'):
            local_mimo.register(dict(CFG, endpoint='http://10.0.0.5:8080'), 'owner')
        one = local_mimo.register(CFG, 'owner'); two = local_mimo.register(dict(CFG, quantization='Q5_K_M'), 'owner')
        self.assertEqual(local_mimo.register(CFG, 'owner')['model_config_id'], one['model_config_id'])
        with self.assertRaisesRegex(ValueError, 'владелец'):
            local_mimo.activate(one['model_config_id'], 'agent')
        local_mimo.activate(one['model_config_id'], 'owner', override='тестовая конфигурация без рантайма')
        self.assertEqual(local_mimo.active()['model_config_id'], one['model_config_id'])
        tasks, state = corpus([dict(), dict()])
        state['runs'][1]['learning']['model_config_id'] = two['model_config_id']
        state['runs'][0]['learning']['model_config_id'] = one['model_config_id']
        le.sync(tasks, state)
        r1 = self.report(tasks, state, model_config_id=one['model_config_id'])
        r2 = self.report(tasks, state, model_config_id=two['model_config_id'])
        self.assertEqual((r1['accepted'], r2['accepted']), (1, 1))


class Knowledge(Base):
    def verified_return(self):
        self.sync([dict(returns=['не учтён контракт ответа'])])
        ev = rejections()[0]['event_id']
        lc.classify(ev, 'missing_context', ['test:test_contract'], by='server', origin='server')
        return ev

    def test_at07_candidate_not_in_context(self):
        ev = self.verified_return()
        rec = lk.propose(ev, 'Контракт ответа API', 'Ответ без поля total', 'Эндпоинты списков app/api',
                         'Всегда отдавать total рядом с items', 'не для потоковых ответов', ['app/api/'])
        self.assertEqual(rec['learning'], 'candidate')
        task = dict(project='alpha-api', title='Список партнёров', scope=['app/api/partners.py'])
        self.assertEqual(lk.retrieve(task), [])
        with self.assertRaisesRegex(ValueError, 'Исполнитель'):
            lk.verify(rec['id'], by='local_mimo', origin='agent', evidence=['test:x'])
        with self.assertRaisesRegex(ValueError, 'владел'):
            w.action(dict(op='learn', action='verify_knowledge', id=rec['id']))
        self.assertTrue(w.needs_owner(dict(op='learn', action='verify_knowledge')))
        w.action(dict(op='learn', action='verify_knowledge', id=rec['id']), origin='owner')
        got = lk.retrieve(task)
        self.assertEqual([g['id'] for g in got], [rec['id']])
        self.assertEqual(lk.retrieve(dict(project='alpha-api', title='x', scope=['app/ui/'])), [])     # чужой scope
        self.assertEqual(lk.retrieve(dict(project='beta-tool', title='x', scope=['app/api/'])), [])    # чужой проект
        self.assertEqual(lk.retrieve(dict(project='alpha-api', title='Потоковых ответов эндпоинт', why='для потоковых ответов',
                                          scope=['app/api/'])), [])                                    # not_when

    def test_at08_retired_card_excluded(self):
        ev = self.verified_return()
        rec = lk.propose(ev, 'Контракт', 'p', 'a', 's', '', ['app/api/'])
        lk.verify(rec['id'], by='owner', origin='owner')
        task = dict(project='alpha-api', title='Список', scope=['app/api/'])
        self.assertEqual(len(lk.retrieve(task)), 1)
        w.action(dict(op='learn', action='reject_knowledge', id=rec['id'], why='контракт сменился'), origin='owner')
        self.assertEqual(lk.retrieve(task), [])
        self.assertEqual(memory.get(rec['id'])['status'], 'устарела')              # не удалена

    def test_propose_needs_verified_cause(self):
        self.sync([dict(returns=['x'])])
        with self.assertRaisesRegex(ValueError, 'подтверждённой'):
            lk.propose(rejections()[0]['event_id'], 't', 'p', 'a', 's', '', ['app/'])

    def test_end_to_end_story(self):                                                # DoD: возврат → карточка → использование
        ev = self.verified_return()
        rec = lk.propose(ev, 'Контракт', 'p', 'a', 's', '', ['app/api/'])
        lk.verify(rec['id'], by='reviewer', origin='agent', evidence=['test:test_contract'])
        cards = lk.retrieve(dict(project='alpha-api', title='Новый список', scope=['app/api/']))
        lk.note_use('RUN-9', 'alpha-api/LT-00', cards, applied=[rec['id']])
        ku = [e for e in le.read_all() if e['event_type'] == 'knowledge_used']
        self.assertEqual(ku[0]['knowledge_refs'][0]['id'], rec['id'])
        self.assertNotIn('text', json.dumps(ku[0]['knowledge_refs']))              # только id и версия


class Gate(Base):
    def verify_all(self, category, pre=None, at=None):
        for r in rejections():
            lc.classify(r['event_id'], category, ['test:t-%s' % r['task_ref']], by='server', origin='server', preconditions=pre, at=at)

    def test_at12_train_request_only_recommendation(self):
        self.sync([dict(returns=['ошибка'])])
        out = w.action(dict(op='learn', action='request_training'))
        self.assertFalse(out['training_started']); self.assertEqual(out['status'], 'collecting_data')
        self.assertFalse(hasattr(lg, 'train')); self.assertFalse(hasattr(learn, 'train'))

    def test_at13_context_errors_recommend_retrieval(self):
        self.sync([dict(returns=['не передан контекст'])] * 50)
        self.verify_all('missing_context')
        rec = lg.recommend()
        self.assertEqual(rec['status'], 'process_fix_first')
        self.assertEqual(rec['alternatives'][0]['layer'], 'context')
        self.assertFalse(rec['training_started'])

    def test_at14_unreproducible_model_errors_not_candidate(self):
        self.sync([dict(returns=['логика'])] * 15 + [dict()] * 35)
        self.verify_all('model_reasoning_failure', pre=dict(spec_sufficient=True, context_available=True, tools_working=True))
        rec = lg.recommend()
        self.assertNotEqual(rec['status'], 'candidate')
        self.assertEqual(rec['sample']['unverified'], 15)

    def test_at15_candidate_needs_owner(self):
        self.sync([dict(returns=['логика'])] * 15 + [dict(returns=['инструмент'])] + [dict()] * 34)
        pre = dict(spec_sufficient=True, context_available=True, tools_working=True, reproducible=True)
        rej = rejections()
        tool_ev = [r for r in rej if r['reason'] == 'инструмент'][0]['event_id']
        lc.classify(tool_ev, 'model_tool_use_failure', ['test:t'], by='server', origin='server', corrective_action='instructions_updated',
                    preconditions=pre, at=T0 - timedelta(days=30))
        for r in rej:
            if r['event_id'] != tool_ev:
                lc.classify(r['event_id'], 'model_reasoning_failure', ['test:t-%s' % r['task_ref']], by='server', origin='server',
                            preconditions=pre)
        local_mimo.register(CFG, 'owner'); cid = local_mimo.register(CFG, 'owner')['model_config_id']
        local_mimo.activate(cid, 'owner', override='тестовая конфигурация без рантайма')
        rec = lg.recommend()
        self.assertEqual(rec['status'], 'candidate', rec['reasons'])
        self.assertEqual(rec['approval'], 'ожидает владельца')
        with self.assertRaisesRegex(ValueError, 'владел'):
            w.action(dict(op='learn', action='decision_status', decision=rec['decision_id'], status='approved_for_experiment'))
        out = w.action(dict(op='learn', action='decision_status', decision=rec['decision_id'], status='approved_for_experiment'),
                       origin='owner')
        self.assertFalse(out['data']['training_started'])
        self.assertEqual(local_mimo.active()['model_config_id'], cid)                # веса и конфигурация не изменены

    def test_at20_split_leak_rejected(self):
        items = [dict(task_ref='a/1', split='train', verified=True, license_ok=True),
                 dict(task_ref='a/1', split='holdout', verified=True, license_ok=True),
                 dict(task_ref='a/2', split='train', verified=False, license_ok=True),
                 dict(task_ref='a/3', split='validation', verified=True, license_ok=None, note='password=hunter2hunter2')]
        out = lg.check_split(items)
        self.assertFalse(out['ok']); self.assertFalse(out['exported']); self.assertEqual(out['leak'], ['a/1'])
        text = ' '.join(out['problems'])
        for word in ('утечка', 'не подтверждён', 'разрешения', 'секрет'):
            self.assertIn(word, text)
        ok = lg.check_split([dict(task_ref='a/1', split='train', verified=True, license_ok=True),
                             dict(task_ref='a/2', split='holdout', verified=True, license_ok=True)])
        self.assertTrue(ok['ok'])


class Sandbox(Base):
    def test_paths_and_commands(self):
        root = Path(self.tmp) / 'wt'; (root / 'app').mkdir(parents=True); (root / '.git').mkdir()
        (root / 'app' / 'a.py').write_text('x = 1\n', encoding='utf-8')
        box = local_mimo.Sandbox(root, ['app/'], {'verify': [sys.executable, '-c', 'print(42)']})
        self.assertIn('a.py', box.call('list_dir', dict(path='app')))
        self.assertEqual(box.call('read_file', dict(path='app/a.py')), 'x = 1\n')
        box.call('replace', dict(path='app/a.py', old='x = 1', new='x = 2'))
        self.assertIn('exit=0', box.call('run', dict(command='verify')))
        for tool, args in (('read_file', dict(path='../secret.txt')), ('write_file', dict(path='README.md', content='x')),
                           ('write_file', dict(path='appx/b.py', content='x')), ('read_file', dict(path='.git/config')),
                           ('run', dict(command='powershell')), ('delete', dict(path='app/a.py'))):
            with self.assertRaises(local_mimo.ToolError, msg=(tool, args)):
                box.call(tool, args)
        self.assertEqual(local_mimo.parse_call('```json\n{"tool": "read_file", "args": {"path": "a"}}\n```'), ('read_file', {'path': 'a'}))
        with self.assertRaises(local_mimo.ToolError):
            local_mimo.parse_call('я думаю, что...')


class MockRuntime(BaseHTTPRequestHandler):
    """OpenAI-совместимый локальный сервер: по сценарию отвечает вызовами инструментов или ошибкой."""
    script = []
    fail = None

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        self.reply(200, dict(data=[dict(id=CFG['model_id'])]))

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if MockRuntime.fail:
            return self.reply(500, dict(error=MockRuntime.fail))
        call = MockRuntime.script.pop(0) if MockRuntime.script else dict(tool='finish', args=dict(summary='готово'))
        self.reply(200, dict(choices=[dict(message=dict(content=json.dumps(call, ensure_ascii=False)))],
                             usage=dict(prompt_tokens=100, completion_tokens=20)))


def git(*args, cwd):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()


class Pilot(Base):
    """Сквозной сценарий: пилотный проект, разработчик — local_mimo через mock-сервер, без failover."""
    def setUp(self):
        super().setUp()
        self.srv = HTTPServer(('127.0.0.1', 0), MockRuntime)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        MockRuntime.script, MockRuntime.fail = [], None
        self.repo = Path(self.tmp) / 'repos' / 'alpha-api'
        git('init', '-q', '-b', 'main', cwd=self.repo)
        (self.repo / 'app' / 'ui').mkdir(parents=True)
        (self.repo / 'app' / 'ui' / 'hint.js').write_text('// hint\n', encoding='utf-8')
        git('add', '-A', cwd=self.repo); git('commit', '-q', '-m', 'init', cwd=self.repo)
        git('checkout', '-q', '-b', 'разработка', cwd=self.repo)
        d = w.read('projects.json', {}); p = d['projects']['alpha-api']
        p['verify'] = [sys.executable, '-c', 'print(1)']
        p['backlog'] = [b for b in p['backlog'] if b.get('status') == 'done']
        p['backlog'].append(dict(id='AL-07', title='Подсказка у счётчика задач', why='Пользователь не понимает число', size='S',
                                 risk='low', work_type='implementation', scope=['app/ui/'], acceptance=['Подсказка видна'],
                                 status='queued', priority='🟡'))
        w.save('projects.json', d)
        os.environ['TASKTREKER_PIPELINE_SYNC'] = '1'
        self.ref = 'alpha-api/AL-07'
        cfg = local_mimo.register(dict(CFG, endpoint='http://127.0.0.1:%d' % self.srv.server_port), 'owner')
        local_mimo.activate(cfg['model_config_id'], 'owner', override='тестовая конфигурация без рантайма')
        w.action(dict(op='settings', changes=dict(learning=dict(enabled=True, pilot_projects=['alpha-api']),
                                                  adaptive_mode='enforce', adaptive_fast_allow=['implementation'])), origin='owner')
        w.action(dict(op='to_idea', ref=self.ref, dry_run=False), origin='owner')
        self.run_ = next(r for r in w.state()['runs'] if r['ref'] == self.ref and r['status'] == 'queued')

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        os.environ.pop('TASKTREKER_PIPELINE_SYNC', None)
        super().tearDown()

    def test_developer_is_local_mimo_without_failover(self):
        self.assertEqual((self.run_['role'], self.run_['agent']), ('developer', 'local_mimo'))
        s = w.state(); run = next(r for r in s['runs'] if r['id'] == self.run_['id'])
        self.assertIsNone(w.failover(s, w.task_for(self.ref), run, w.now()))
        route = orchestra.role_route(w.task_for(self.ref), 'developer', 'local_mimo')
        self.assertEqual(route['provider'], 'local_mimo')

    def test_at09_runtime_down_pauses_task(self):
        MockRuntime.fail = 'CUDA error: out of memory'
        out = local_mimo.run_agent(self.ref, self.run_['id'], 'mimo-1', max_steps=3)
        self.assertTrue(out['paused']); self.assertFalse(out['failover'])
        run = next(r for r in w.state()['runs'] if r['id'] == self.run_['id'])
        self.assertEqual(run['paused']['reason'], 'runtime'); self.assertEqual(run['agent'], 'local_mimo')
        self.assertNotEqual(run['status'], 'done')
        ev = [e for e in le.read_all() if e['event_type'] == 'runtime_unavailable']
        self.assertEqual(len(ev), 1); self.assertEqual(ev[0]['cause']['category'], 'environment_failure')
        self.assertFalse(any(r['ref'] == self.ref and r.get('agent') in ('claude', 'codex') and r['role'] == 'developer'
                             for r in w.state()['runs']))

    def test_agent_loop_submits_attempt(self):
        packet = w.action(dict(op='packet', ref=self.ref, run=self.run_['id']))
        items = {d['id']: dict(status='done', evidence='проверено: verify exit=0, файл app/ui/hint.js') for d in packet.get('dod') or []}
        MockRuntime.script = [dict(tool='read_file', args=dict(path='app/ui/hint.js')),
                              dict(tool='write_file', args=dict(path='../escape.txt', content='x')),       # отказ песочницы
                              dict(tool='replace', args=dict(path='app/ui/hint.js', old='// hint', new='// hint: число задач')),
                              dict(tool='run', args=dict(command='verify')),
                              dict(tool='finish', args=dict(summary='Подсказка у счётчика задач добавлена', items=items))]
        local_mimo.run_agent(self.ref, self.run_['id'], 'mimo-1', max_steps=8)
        run = next(r for r in w.state()['runs'] if r['id'] == self.run_['id'])
        self.assertEqual(run['learning']['tool_calls'], dict(ok=3, refused=1))
        self.assertEqual(run['learning']['local_tokens'], dict(input=500, output=100))
        self.assertTrue(run['learning'].get('model_config_id'))
        self.assertFalse((Path(self.tmp) / 'repos' / 'escape.txt').exists())
        learn.refresh()
        subm = [e for e in le.read_all() if e['event_type'] == 'attempt_submitted' and e['task_ref'] == self.ref]
        self.assertEqual(len(subm), 1)
        self.assertEqual(subm[0]['model_config_id'], local_mimo.active()['model_config_id'])
        card = w.action(dict(op='learn', action='card'))
        self.assertEqual(card['config']['model'], CFG['model_id'])


class Surface(Base):
    def test_at19_cli_mcp_dashboard(self):
        import build_dashboard, mcp_server
        self.sync([dict(returns=['x']), dict()])
        snap = build_dashboard.learnview_snapshot()
        self.assertNotIn('error', snap); self.assertEqual(snap['accepted'], 2); self.assertEqual(len(snap['returns']), 1)
        self.assertIn('id="learnview"', (Path(ROOT) / 'tools' / 'shell.html').read_text(encoding='utf-8'))
        self.assertIn('learnview.js', (Path(ROOT) / 'tools' / 'build_dashboard.py').read_text(encoding='utf-8'))
        self.assertFalse(mcp_server.call_tool('tt_work', dict(op='learn', action='status'))['isError'])
        self.assertTrue(mcp_server.call_tool('tt_work', dict(op='learn', action='activate_model', id='x'))['isError'])
        self.assertEqual(learn.main(['help']), 0)
        self.assertIn('learning', __import__('backup').DIRS)


def gguf_bytes(magic=b'GGUF', version=3, kv=None, strlen=None):
    import struct
    def s(x):
        b = x.encode('utf-8'); return struct.pack('<Q', strlen if strlen is not None else len(b)) + b
    kv = kv if kv is not None else [('general.architecture', 8, 'qwen35'), ('general.name', 8, 'Test 1B'),
                                     ('general.file_type', 4, 15), ('qwen35.context_length', 4, 32768)]
    out = magic + struct.pack('<IQQ', version, 0, len(kv))
    for k, t, v in kv:
        out += s(k) + struct.pack('<I', t) + (s(v) if t == 8 else struct.pack('<I', v))
    return out


class GGUF(Base):
    def test_probe_and_register(self):
        import gguf_probe
        f = Path(self.tmp) / 'm.gguf'; f.write_bytes(gguf_bytes() + b'\0' * 64)
        info = gguf_probe.inspect(str(f), sha=True)
        self.assertEqual((info['architecture'], info['quantization'], info['context_length']), ('qwen35', 'Q4_K_M', 32768))
        self.assertTrue(info['weight_sha256'].startswith('sha256:'))
        for bad in (gguf_bytes(magic=b'GGML'), gguf_bytes(version=9), gguf_bytes(strlen=1 << 40), gguf_bytes()[:20]):
            f.write_bytes(bad)
            with self.assertRaises(gguf_probe.BadGGUF):
                gguf_probe.inspect(str(f))
        f.write_bytes(gguf_bytes())
        with self.assertRaisesRegex(ValueError, 'владел'):
            w.action(dict(op='learn', action='register_gguf', path=str(f)))
        cfg = w.action(dict(op='learn', action='register_gguf', path=str(f), endpoint='http://127.0.0.1:8080'), origin='owner')
        self.assertEqual(cfg['quantization'], 'Q4_K_M'); self.assertEqual(cfg['weight_sha256'], gguf_probe.inspect(str(f), sha=True)['weight_sha256'])
        with self.assertRaisesRegex(ValueError, 'контекста'):
            learn.register_gguf(str(f), 'http://127.0.0.1:8080', 'owner', context_limit=65536)
