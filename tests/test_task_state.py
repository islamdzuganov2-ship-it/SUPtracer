"""Оперативное состояние задач отдельно от ручного projects.json (TT-45, ревью A6 и I4)."""
import os
import threading
import time
from pathlib import Path
from base import DataCase
import taskstate as ts
import workflow as w

REF = 'alpha-api/AL-03'


class StateLayerTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api','kind':'запрос','status':'активна','title':'Запрос'}]})
        w.action(dict(op='plan', project='alpha-api', tasks=[dict(id='AL-03', title='Правка', why='Нужна', root='request', size='S',
            priority='🟡', risk='low', work_type='implementation', acceptance=['Тест проходит', 'Документ обновлён'])]))

    def manual(self):
        return next(b for b in self.jread('projects.json')['projects']['alpha-api']['backlog'] if b['id'] == 'AL-03')

    def edit_manual(self, **fields):
        d = self.jread('projects.json')
        next(b for b in d['projects']['alpha-api']['backlog'] if b['id'] == 'AL-03').update(fields)
        self.jwrite('projects.json', d)

    def raw(self):
        return (Path(self.data)/'projects.json').read_bytes()

    def layer(self):
        return w.state()['tasks'].get(REF, {})

    def take(self, worker='c1'):
        w.action(dict(op='take', ref=REF, model='claude-haiku-5-5', worker=worker))

    # --- запись по слоям

    def test_execution_does_not_touch_manual_layer(self):
        before = self.raw()
        self.take()
        w.action(dict(op='heartbeat', ref=REF, run='RUN-1', worker='c1'))
        w.action(dict(op='fail', ref=REF, run='RUN-1', worker='c1', evidence='Тест упал'))
        w.action(dict(op='update', ref=REF, changes={'acceptance_done': [0]}))
        w.action(dict(op='status', ref=REF, status='blocked', reason='Ждём доступ'))
        self.assertEqual(before, self.raw())                       # ручной файл не переписывался
        t = w.task_for(REF)
        self.assertEqual((t['status'], t['acceptance_done'], t['status_note']), ('blocked', [0], 'Ждём доступ'))
        self.assertEqual((self.layer()['status'], self.layer()['status_base']), ('blocked', 'queued'))

    def test_claim_is_written_with_the_run(self):
        self.take()
        layer, run = self.layer(), w.state()['runs'][0]
        self.assertEqual((layer['status'], layer['claimed_by']['run']), ('in_progress', run['id']))
        self.assertNotIn('claimed_by', self.manual())
        self.assertEqual(self.manual()['status'], 'queued')

    def test_outcome_goes_to_manual_layer(self):
        self.take()
        w.action(dict(op='finish', ref=REF, run='RUN-1', worker='c1', evidence='unit: 12 passed', tokens=10))
        m = self.manual()
        self.assertEqual((m['status'], m['evidence']), ('done', 'unit: 12 passed'))
        self.assertIn('completed_at', m)
        for key in ('claimed_by', 'status_note', 'status_history', 'acceptance_done'):
            self.assertNotIn(key, m)
        self.assertNotIn('status', self.layer())                   # итог — только в ручном слое
        self.assertEqual(w.task_for(REF)['status'], 'done')

    def test_cancel_and_reopen_are_outcome(self):
        st = lambda s, reason='': w.action(dict(op='status', ref=REF, status=s, reason=reason))
        st('review')
        self.assertEqual(self.manual()['status'], 'queued')
        st('cancelled', 'Не нужна')
        self.assertEqual(self.manual()['status'], 'cancelled')
        st('queued', 'Снова нужна')
        self.assertEqual(self.manual()['status'], 'queued')
        self.assertEqual([h['to'] for h in w.task_for(REF)['status_history']], ['review', 'cancelled', 'queued'])
        self.assertNotIn('status_history', self.manual())

    def test_work_evidence_becomes_outcome_on_cancel(self):
        self.take()
        s = w.state(); s['tasks'][REF]['evidence'] = 'Сделано в ветке'; w.save('workflow.json', s)
        w.action(dict(op='status', ref=REF, status='cancelled', reason='Остановить'))
        self.assertEqual((self.manual()['status'], self.manual()['evidence']), ('cancelled', 'Сделано в ветке'))

    # --- ручная правка новее слоя

    def test_manual_edit_newer_than_layer_wins(self):
        self.take()
        self.assertEqual(w.task_for(REF)['status'], 'in_progress')
        self.edit_manual(status='blocked')                          # регламент, редактор, Obsidian
        self.assertEqual(w.task_for(REF)['status'], 'blocked')
        self.assertEqual(self.t.context.collect('alpha-api')[0]['alpha-api']['backlog'][-1]['status'], 'blocked')
        w.action(dict(op='cancel', ref=REF, run='RUN-1', evidence='Снято'))
        self.assertEqual(w.task_for(REF)['status'], 'queued')       # операция после правки — снова слой
        self.assertEqual(self.layer()['status_base'], 'blocked')

    def test_layer_written_before_tt45_still_wins(self):
        s = w.state(); s['tasks'][REF] = dict(status='review'); w.save('workflow.json', s)
        self.assertEqual(w.task_for(REF)['status'], 'review')

    # --- потребители читают действующий статус

    def find(self, node, nid):
        if node.get('id') == nid: return node
        for child in node.get('children', []):
            hit = self.find(child, nid)
            if hit: return hit

    def test_tree_estimate_and_briefing_read_the_layer(self):
        self.take()
        self.quiet(self.t.estimate.main)
        items = {i['id']: i for i in self.jread('estimates.json')['projects']['alpha-api']['items']}
        self.assertEqual(items['AL-03']['status'], 'in_progress')
        tree, _ = self.t.tree.build(False)
        self.assertEqual(self.find(tree['root'], 't:alpha-api:AL-03')['status'], 'in_progress')
        P = self.t.context.collect('alpha-api')[0]
        self.assertEqual(P['alpha-api']['backlog'][-1]['claimed_by']['worker'], 'c1')
        w.action(dict(op='finish', ref=REF, run='RUN-1', worker='c1', evidence='ok', tokens=1))
        self.quiet(self.t.estimate.main)
        items = {i['id']: i for i in self.jread('estimates.json')['projects']['alpha-api']['items']}
        self.assertTrue(items['AL-03']['closed'])

    def test_apply_leaves_source_untouched(self):
        self.take()
        doc = self.jread('projects.json')
        merged = ts.apply(doc, self.data)
        self.assertEqual(merged['projects']['alpha-api']['backlog'][-1]['status'], 'in_progress')
        self.assertEqual(doc['projects']['alpha-api']['backlog'][-1]['status'], 'queued')
        self.assertEqual(merged['size_calibration'], doc['size_calibration'])

    # --- поля до TT-45 в ручном слое

    def test_legacy_fields_move_to_layer_without_loss(self):
        legacy = dict(claimed_by=dict(agent='codex', worker='old'), acceptance_done=[1], status_note='Старая заметка',
                      auto_check=dict(ok=True, run='RUN-0'))
        self.edit_manual(**legacy)
        t = w.task_for(REF)
        self.assertEqual({k: t[k] for k in legacy}, legacy)        # до переезда — читаются из ручного слоя
        w.action(dict(op='update', ref=REF, changes={'priority': '🔴'}))
        m = self.manual()
        self.assertEqual(m['priority'], '🔴')
        self.assertFalse(set(legacy) & set(m))                      # переехали из ручного слоя
        t = w.task_for(REF)
        self.assertEqual({k: t[k] for k in legacy}, legacy)        # и ничего не потеряли

    def test_cleared_field_hides_legacy_value(self):
        self.edit_manual(status_note='Старая заметка', claimed_by=dict(agent='codex', worker='old'))
        before = self.raw()
        w.action(dict(op='status', ref=REF, status='blocked', reason='Ждём'))
        w.action(dict(op='status', ref=REF, status='queued'))
        self.assertEqual(before, self.raw())
        t = w.task_for(REF)
        self.assertNotIn('status_note', t); self.assertNotIn('claimed_by', t)

    # --- I4: проверки op=update — таблица

    def test_update_fields_table(self):
        self.assertEqual(set(w.UPDATE_FIELDS), {'priority', 'model', 'model_reason', 'model_plan', 'acceptance', 'acceptance_done',
            'risk', 'work_type', 'due_date', 'root', 'title', 'why', 'executor', 'scope', 'task_type', 'new_root', 'assessment', *w.TYPE_FIELDS})
        for changes, message in (({'title': ' '}, 'Заполните title'), ({'why': 5}, 'Заполните why'),
                                 ({'priority': 'x'}, 'Неверный приоритет'), ({'risk': 'x'}, 'Неверный риск'),
                                 ({'work_type': 'x'}, 'Неверный вид работы'), ({'acceptance': []}, 'Нужны критерии'),
                                 ({'root': 'missing'}, 'Корень должен принадлежать'), ({'model_plan': {'gpt': None}}, 'model_plan: только'),
                                 ({'new_root': {'title': 'x'}}, 'текст запроса'), ({'assessment': {'complexity': 9}}, ''),
                                 ({'acceptance_done': [5]}, 'Неверные отметки'), ({'due_date': '19.10.2026'}, 'ГГГГ-ММ-ДД'),
                                 ({'unknown': 1}, 'Недопустимые поля'), ({'model': 'gpt-6-luna'}, 'Обоснуйте выбор модели'),
                                 ({'root': 'request', 'new_root': {'title': 'x', 'text': 'y'}}, 'либо существующий')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                w.action(dict(op='update', ref=REF, changes=changes))
        self.assertEqual(w.state()['tasks'], {})                     # отказ ничего не записал

    def test_reworded_criteria_reset_marks_in_layer(self):
        w.action(dict(op='update', ref=REF, changes={'acceptance_done': [0, 1]}))
        w.action(dict(op='update', ref=REF, changes={'acceptance': ['Тест проходит', 'Документ и пример обновлены']}))
        self.assertEqual(self.manual()['acceptance'][1], 'Документ и пример обновлены')
        self.assertEqual(w.task_for(REF)['acceptance_done'], [])

    # --- общая блокировка

    def test_lock_waits_for_short_operation(self):
        lock = Path(self.data)/'.workflow.lock'; lock.write_text('')
        with self.assertRaises(ValueError), ts.locked(self.data): pass
        threading.Timer(0.3, lock.unlink).start()
        started = time.time()
        with ts.locked(self.data, wait=5): pass
        self.assertLess(time.time() - started, 4)
        self.assertFalse(lock.exists())


class VaultLayerTests(DataCase):
    PAGE = os.path.join("TaskTreker", "Проекты", "Сервис Альфа.md")

    def setUp(self):
        super().setUp(); self.projects()
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, ".obsidian"))
        self.quiet(self.t.obsidian.cmd_init, [self.vault])
        # AL-01 ведёт исполнитель: в ручном слое «к выполнению», в оперативном — «в работе».
        self.jwrite("workflow.json", dict(schema=1, focus=[], runs=[], tasks={
            "alpha-api/AL-01": dict(status="in_progress", status_base="queued", claimed_by=dict(agent="claude", worker="c1"))}))

    def page(self):
        with open(os.path.join(self.vault, self.PAGE), encoding="utf-8") as f:
            return f.read()

    def tick(self, old, new):
        text = self.page().replace(old, new)
        with open(os.path.join(self.vault, self.PAGE), "w", encoding="utf-8") as f:
            f.write(text)

    def status(self):
        return {b["id"]: b["status"] for b in self.jread("projects.json")["projects"]["alpha-api"]["backlog"]}

    def test_push_shows_layer_status(self):
        self.quiet(self.t.obsidian.cmd_push, [])
        self.assertIn("- [/] **AL-01**", self.page())

    def test_untouched_page_does_not_write_manual_layer(self):
        self.quiet(self.t.obsidian.cmd_push, [])
        before = self.status()
        out = self.quiet(self.t.obsidian.cmd_pull, [])[1]
        self.assertNotIn("AL-01:", out)
        self.assertEqual(before, self.status())

    def test_tick_beats_layer_and_is_written_atomically(self):
        self.quiet(self.t.obsidian.cmd_push, [])
        self.tick("- [/] **AL-01**", "- [x] **AL-01**")
        out = self.quiet(self.t.obsidian.cmd_pull, [])[1]
        self.assertIn("alpha-api/AL-01: in_progress -> done", out)
        self.assertEqual(self.status()["AL-01"], "done")
        merged = ts.apply(self.jread("projects.json"), self.data)
        self.assertEqual(merged["projects"]["alpha-api"]["backlog"][0]["status"], "done")
        self.assertFalse([n for n in os.listdir(self.data) if n.endswith(".tmp") or n == ".workflow.lock"])
