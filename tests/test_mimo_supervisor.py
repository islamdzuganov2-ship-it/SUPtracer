"""Надзор за локальной моделью (07: A9-05, A9-06, A9-07, A3-02, AG-LDEV-08, AG-ARCH-09, AG-DEV-06)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import test_learning as TL                 # первым: base ставит путь к tools/ и данные теста
import local_mimo
import mimo_supervisor as sup
import orchestra
import workflow as w
from test_learning import git


class Units(TL.Base):
    def weights(self, mb):
        f = Path(self.tmp) / 'w.gguf'
        with open(f, 'wb') as h:
            h.truncate(mb * 1048576)
        return str(f)

    def test_resources_refuse_without_memory_or_data(self):
        cfg = dict(weight_path=self.weights(3))
        self.assertFalse(sup.resources(cfg, lambda: 100)[0])                  # мало VRAM — безопасный отказ
        self.assertIn('resources', sup.resources(cfg, lambda: 100)[1])
        self.assertFalse(sup.resources(cfg, lambda: None)[0])                 # без данных GPU — вслепую не грузим
        self.assertFalse(sup.resources({}, lambda: 99999)[0])
        self.assertTrue(sup.resources(cfg, lambda: 8000)[0])

    def test_gpu_lock_is_exclusive_and_recovers_from_dead_holder(self):
        self.assertIsNone(sup.acquire('RUN-1'))
        held = sup.acquire('RUN-2')
        self.assertEqual(held['run'], 'RUN-1')                                # одна загрузка за раз
        sup.release('RUN-2'); self.assertEqual(sup.acquire('RUN-2')['run'], 'RUN-1')   # чужую блокировку не снять
        sup.release('RUN-1'); self.assertIsNone(sup.acquire('RUN-2'))
        p = sup._lock_path(); d = json.loads(p.read_text(encoding='utf-8')); d['pid'] = 999999; p.write_text(json.dumps(d))
        self.assertIsNone(sup.acquire('RUN-3'))                               # держатель умер — блокировка брошена

    def test_ensure_runtime_spawns_on_loopback_and_waits_for_health(self):
        cfg = dict(TL.CFG, weight_path=self.weights(2), runtime_path='C:/rt/llama-server.exe', endpoint='http://127.0.0.1:8089')
        calls, state = [], dict(n=0)

        def health(c):
            state['n'] += 1
            return dict(ok=state['n'] > 2, reason='нет ответа')
        h = sup.ensure_runtime(cfg, health, free_fn=lambda: 8000, popen=lambda cmd, **k: calls.append(cmd), wait=30, sleep=lambda s: None)
        self.assertTrue(h['ok'] and h['spawned'])
        self.assertEqual(calls[0][:3], ['C:/rt/llama-server.exe', '-m', cfg['weight_path']])
        self.assertIn('127.0.0.1', calls[0]); self.assertIn('8089', calls[0])
        h = sup.ensure_runtime(cfg, lambda c: dict(ok=False, reason='нет'), free_fn=lambda: 10, popen=lambda *a, **k: self.fail('без памяти не запускать'))
        self.assertTrue(h['resources'])

    def test_critical_needs_qualified_model(self):
        crit = dict(route=dict(applied='CRITICAL'))
        self.assertTrue(sup.capable(None, dict(route=dict(applied='STANDARD')))[0])
        ok, why = sup.capable(dict(TL.CFG, model_config_id='x'), crit)
        self.assertFalse(ok); self.assertIn('аттестованной', why)


class KnowledgeAfterSwap(TL.Knowledge):
    def test_knowledge_survives_model_swap(self):                                  # 07: A4-08, AG-LDEV-10
        import learning_knowledge as lk
        ev = self.verified_return()
        rec = lk.propose(ev, 'Контракт', 'p', 'a', 's', '', ['app/api/'])
        lk.verify(rec['id'], by='owner', origin='owner')
        task = dict(project='alpha-api', title='Список', scope=['app/api/'])
        a = local_mimo.register(dict(TL.CFG), 'owner'); local_mimo.activate(a['model_config_id'], 'owner', override='первая тестовая модель')
        before = [c['id'] for c in lk.retrieve(task)]
        b = local_mimo.register(dict(TL.CFG, model_id='mimo-next', weight_sha256='sha256:' + 'b' * 64), 'owner')
        local_mimo.activate(b['model_config_id'], 'owner', override='вторая тестовая модель')
        self.assertEqual([c['id'] for c in lk.retrieve(task)], before)              # знание не привязано к весам
        self.assertEqual(before, [rec['id']])
        self.assertTrue(any(e['event_id'] == ev for e in TL.le.read_all()))       # история опыта осталась при новой модели


for _n in [n for n in dir(TL.Knowledge) if n.startswith('test_') and n not in KnowledgeAfterSwap.__dict__]:
    setattr(KnowledgeAfterSwap, _n, None)


class Supervised(TL.Pilot):
    def test_every_edit_is_committed_as_wip(self):
        MockRuntime = TL.MockRuntime
        MockRuntime.script = [dict(tool='replace', args=dict(path='app/ui/hint.js', old='// hint', new='// hint: задачи')),
                              dict(tool='write_file', args=dict(path='app/ui/more.js', content='// ещё\n'))]
        local_mimo.run_agent(self.ref, self.run_['id'], 'mimo-1', max_steps=2)
        wt = w.task_for(self.ref)['pipeline']['worktree']['path']
        log = git('log', '--format=%s', cwd=wt)
        self.assertIn('WIP local_mimo: шаг 1 — replace app/ui/hint.js', log)
        self.assertIn('WIP local_mimo: шаг 2 — write_file app/ui/more.js', log)
        self.assertEqual(git('status', '--porcelain', cwd=wt), '')

    def test_crash_keeps_work_and_pauses(self):
        calls = dict(n=0)

        def client(cfg, messages):
            calls['n'] += 1
            if calls['n'] == 1:
                return json.dumps(dict(tool='write_file', args=dict(path='app/ui/x.js', content='1\n'))), dict(input=1, output=1)
            raise KeyError('обрыв ответа')
        out = local_mimo.run_agent(self.ref, self.run_['id'], 'mimo-1', max_steps=4, client=client)
        self.assertTrue(out['paused'])
        wt = w.task_for(self.ref)['pipeline']['worktree']['path']
        self.assertIn('сбой клиента модели', out['reason'])
        self.assertIn('WIP local_mimo: шаг 1 — write_file app/ui/x.js', git('log', '--format=%s', cwd=wt))
        self.assertTrue((Path(wt) / 'app/ui/x.js').exists())

    def test_session_stays_on_pinned_config_and_owner_can_roll_back(self):
        first = local_mimo.active()['model_config_id']
        with w.locked():
            s = w.state(); run = next(r for r in s['runs'] if r['id'] == self.run_['id'])
            run.setdefault('learning', {})['model_config_id'] = first; w.save('workflow.json', s)
        second = local_mimo.register(dict(TL.CFG, model_id='mimo-next', endpoint=local_mimo.active()['endpoint']), 'owner')
        local_mimo.activate(second['model_config_id'], 'owner', override='проверка закрепления сессии')
        local_mimo.run_agent(self.ref, self.run_['id'], 'mimo-1', max_steps=1)
        run = next(r for r in w.state()['runs'] if r['id'] == self.run_['id'])
        self.assertEqual(run['model'], TL.CFG['model_id'])                    # не перескочил на новую активную
        with self.assertRaises(ValueError):
            local_mimo.rollback('agent')
        local_mimo.rollback('owner')
        self.assertEqual(local_mimo.active()['model_config_id'], first)

    def test_unqualified_model_does_not_get_critical(self):
        s = w.state(); t = w.task_for(self.ref)
        t = dict(t, pipeline=dict(t['pipeline'], route=dict(applied='CRITICAL')))
        agent = orchestra.role_agent(s, t, 'developer')
        self.assertNotEqual(agent, 'local_mimo')
        self.assertIn('аттестованной', s['capability_routes'][-1]['reason'])
        self.assertEqual(orchestra.role_agent(w.state(), w.task_for(self.ref), 'developer'), 'local_mimo')   # обычная — пилот


for _n in [n for n in dir(TL.Pilot) if n.startswith('test_') and n not in Supervised.__dict__]:
    setattr(Supervised, _n, None)

if __name__ == '__main__':
    unittest.main()
