"""Атомарный приём: 10 одновременных заявок на 1 слот (TT-106; ТЗ FR-014: TT3-081, AC-14.5, TC-32)."""
import threading
import time

from base import DataCase
import flow_policy as fp
import taskstate as ts
import workflow as w

REFS = ['alpha-api/AL-%02d' % i for i in range(10, 23)]


class WipRace(DataCase):
    def setUp(self):
        super().setUp()

        def more(d):
            d['projects']['alpha-api']['backlog'] += [dict(id=r.split('/')[1], title='Задача ' + r, size='S', priority='🟡',
                                                           status='queued', why='гонка за слот') for r in REFS]
        self.projects(more)

    def race(self, refs):
        won, lost, errors = [], [], []
        gate = threading.Barrier(len(refs))

        def go(ref):
            gate.wait()
            # Занято — повтор, как у диспетчера; отказ по лимиту — конец. Бюджет — срок, а не число попыток:
            # под параллельным прогоном pm.py check 10 потоков дольше ждут блокировку, атомарность от этого не меняется.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    w.reserve_sprint(ref); won.append(ref); return
                except ts.Busy:
                    time.sleep(0.01)
                except ValueError as e:
                    lost.append((ref, str(e))); return
                except Exception as e:                 # noqa: BLE001 — любая другая ошибка — провал теста
                    errors.append((ref, repr(e))); return
            errors.append((ref, 'блокировка так и не освободилась'))
        threads = [threading.Thread(target=go, args=(r,)) for r in refs]
        for t in threads: t.start()
        for t in threads: t.join(60)
        self.assertFalse(errors, errors)
        return won, lost

    def test_rolling_ten_requests_one_slot(self):
        s = w.state(); s['settings'] = dict(flow_mode='rolling', wip_global=4, wip_per_project=10); w.save('workflow.json', s)
        for ref in REFS[:3]:
            w.reserve_sprint(ref)
        won, lost = self.race(REFS[3:])
        self.assertEqual(len(won), 1, won)
        self.assertEqual(len(lost), 9)
        self.assertTrue(all('в потоке 4 задач из 4' in why for _, why in lost), lost)
        st = w.state()
        self.assertEqual(len(fp.open_refs(st, w.snapshot()['tasks'])), 4)
        self.assertEqual(len([d for d in st['delivery_sprints']['decisions'] if d['decision'] == 'admit']), 4)

    def test_batch_ten_requests_one_place(self):
        for ref in REFS[:3]:
            w.reserve_sprint(ref)
        won, lost = self.race(REFS[3:])
        self.assertEqual(len(won), 1, won)
        self.assertEqual(len(w.state()['delivery_sprints']['currents']['alpha-api']['refs']), 4)

    def test_same_task_ten_times_one_slot(self):
        s = w.state(); s['settings'] = dict(flow_mode='rolling'); w.save('workflow.json', s)
        won, lost = self.race([REFS[0]] * 10)
        self.assertEqual((len(won), lost), (10, []))                               # повтор идемпотентен
        st = w.state()
        self.assertEqual(list(fp.slots(st)), [REFS[0]])
        self.assertEqual(st['delivery_sprints']['currents']['alpha-api']['refs'], [REFS[0]])
