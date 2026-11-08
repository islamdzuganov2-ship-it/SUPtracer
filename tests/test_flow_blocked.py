"""Внешняя блокировка ≠ завершённая задача (TT-106; ТЗ FR-014 п.5: TT3-083, AC-14.4, TC-31)."""
from datetime import timedelta

from base import DataCase
import flow_policy as fp
import mcp_server
import workflow as w


class Blocked(DataCase):
    def setUp(self):
        super().setUp()

        def more(d):
            d['projects']['alpha-api']['backlog'] += [dict(id='AL-%02d' % i, title='Задача %d' % i, size='S', priority='🟡',
                                                           status='queued', why='для теста потока') for i in range(10, 13)]
        self.projects(more)
        s = w.state(); s['settings'] = dict(flow_mode='rolling', wip_global=2, wip_per_project=5); w.save('workflow.json', s)
        for ref in ('alpha-api/AL-10', 'alpha-api/AL-11'):
            w.reserve_sprint(ref)

    def block(self, ref='alpha-api/AL-10', hours=24, **kw):
        return w.action(dict(dict(op='flow_block', ref=ref, reason='Ждём ключ API от партнёра', responsible='владелец',
                                  recheck_at=(w.now() + timedelta(hours=hours)).isoformat()), **kw))

    def test_blocked_stays_counted_and_visible(self):                            # TC-31
        self.block()
        snap = w.action(dict(op='flow'))
        self.assertEqual(snap['active_wip'], 2)
        self.assertEqual(snap['counts']['blocked'], 1)
        self.assertFalse(snap['new_task_admission_allowed'])
        self.assertIn('WIP_GLOBAL_LIMIT', snap['reason_codes'])
        self.assertTrue(snap['blocked'][0]['counted'])
        with self.assertRaisesRegex(ValueError, 'Поток'):
            w.reserve_sprint('alpha-api/AL-12')

    def test_block_needs_details_and_slot(self):
        with self.assertRaisesRegex(ValueError, 'responsible'):
            self.block(responsible='')
        with self.assertRaisesRegex(ValueError, 'recheck_at'):
            self.block(hours=-1)
        with self.assertRaisesRegex(ValueError, 'не в потоке'):
            self.block(ref='alpha-api/AL-12')
        r = mcp_server.call_tool('tt_work', dict(op='flow_block', ref='alpha-api/AL-11', reason='Нет стенда для теста',
                                                 responsible='devops', recheck_at=(w.now() + timedelta(hours=1)).isoformat()))
        self.assertFalse(r['isError'], r)                                         # агент может отметить: это ничего не освобождает

    def test_overdue_recheck_goes_to_bell(self):
        self.block(hours=1)
        s = w.state(); b = s['delivery_sprints']['blocked']['alpha-api/AL-10']
        b['recheck_at'] = (w.now() - timedelta(minutes=5)).isoformat(); w.save('workflow.json', s)
        bell = [n for n in w.notifications(w.state(), w.snapshot()['tasks']) if n['kind'] == 'flow_block']
        self.assertEqual(len(bell), 1); self.assertIn('слот задачи по-прежнему занят', bell[0]['text'])
        self.assertTrue(w.action(dict(op='flow'))['blocked'][0]['overdue'])

    def test_owner_may_lend_blocked_slot_within_limit(self):
        self.block(); self.block('alpha-api/AL-11')
        until = (w.now() + timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(dict(op='flow_exception', ref='alpha-api/AL-10', kind='blocked_slot', reason='Партнёр молчит неделю', expires_at=until))
        w.action(dict(op='flow_exception', ref='alpha-api/AL-10', kind='blocked_slot', reason='Партнёр молчит неделю', expires_at=until), origin='owner')
        with self.assertRaisesRegex(ValueError, 'Лимит исключений'):
            w.action(dict(op='flow_exception', ref='alpha-api/AL-11', kind='blocked_slot', reason='Стенд чинят неделю', expires_at=until), origin='owner')
        w.reserve_sprint('alpha-api/AL-12')                                       # одолженный слот
        snap = w.action(dict(op='flow'))
        self.assertEqual(snap['active_wip'], 3)                                   # блокированная всё ещё в потоке
        self.assertFalse(snap['blocked'][0]['counted'])
        w.action(dict(op='flow_unblock', ref='alpha-api/AL-10'))
        self.assertFalse(fp.borrowed(w.state()))                                  # снятие блокировки отзывает исключение
        self.assertEqual(w.action(dict(op='flow'))['active_wip'], 3)


class BlockedVisible(Blocked):
    """A11-06/DES-04: всё, что в счётчике «заблокировано», есть в сводке с причиной и ответственным."""

    def test_status_blocked_task_listed_with_reason_and_owner(self):
        w.action(dict(op='status', ref='alpha-api/AL-11', status='blocked', reason='Ждём доступ к стенду заказчика'))
        snap = w.action(dict(op='flow'))
        self.assertEqual(snap['counts']['blocked'], len(snap['blocked']))
        row = next(b for b in snap['blocked'] if b['ref'] == 'alpha-api/AL-11')
        self.assertIn('стенду', row['reason'])
        self.assertTrue(row['responsible'])
        self.assertEqual(row['source'], 'status')

    def test_pipeline_problem_names_resolver_or_owner(self):
        s = w.state()
        tasks = {t['ref']: t for t in w.snapshot()['tasks']}
        t = dict(tasks['alpha-api/AL-10'], pipeline=dict(stage='analysis', blocked=dict(reason='Нет доступа к .git', problem='P-64', owner=True, at=w.now().isoformat())))
        t2 = dict(tasks['alpha-api/AL-11'], pipeline=dict(stage='analysis', blocked=dict(reason='Сервер не сохранил ворота', problem='P-73', role='teamlead')))
        snap = fp.snapshot(s, [t, t2], at=w.now())
        rows = {b['ref']: b for b in snap['blocked']}
        self.assertEqual((rows['alpha-api/AL-10']['responsible'], rows['alpha-api/AL-10']['source']), ('владелец', 'pipeline'))
        self.assertEqual(rows['alpha-api/AL-11']['responsible'], 'Решала (P-73)')
        self.assertEqual(snap['counts']['blocked'], len(snap['blocked']))

    def test_board_gets_lists_even_if_hand_edited_as_strings(self):
        def hand(d):
            d['projects']['alpha-api']['backlog'][0].update(acceptance='Выгрузка CSV открывается в Excel', depends_on='AL-10; AL-11')
        self.projects(hand)
        t = w.snapshot()['tasks'][0]
        self.assertEqual(t['acceptance'], ['Выгрузка CSV открывается в Excel'])     # паспорт делает .map — строка его роняла
        self.assertEqual(t['depends_on'], ['AL-10', 'AL-11'])
