"""Флаги TT3 в положении «выкл» (TT-107; ТЗ TT3-008, §11 п.3, NFR-36, IT-24)."""
import unittest

from base import DataCase
import flags as ff
import mcp_server
import sprints as sp
import workflow as w


class FlagRules(unittest.TestCase):
    def test_all_off_by_default(self):
        self.assertTrue(all(not f['enabled'] for f in ff.view({})))
        self.assertEqual({f['name'] for f in ff.view({})}, set(ff.REGISTRY))
        for name in ('tt3_native_runtime', 'tt3_model_registry', 'tt3_local_agent', 'tt3_consensus', 'tt3_machine_oracle',
                     'tt3_failure_triage', 'tt3_learning', 'tt3_training', 'tt3_finish_first'):
            self.assertIn(name, ff.REGISTRY)                                      # список из §11 п.3 ТЗ

    def test_unimplemented_cannot_be_enabled(self):
        with self.assertRaisesRegex(ValueError, 'не реализован'):
            ff.apply(dict(tt3_local_agent=True), {})
        self.assertEqual(ff.apply(dict(tt3_local_agent=False), {}), dict(flags={}))
        with self.assertRaisesRegex(ValueError, 'Неизвестные'):
            ff.apply(dict(tt3_magic=True), {})
        with self.assertRaisesRegex(ValueError, 'true\\|false'):
            ff.apply(dict(tt3_finish_first='yes'), {})
        # Значение, записанное в обход проверки, всё равно не включает нереализованное.
        self.assertFalse(ff.enabled('tt3_training', dict(flags=dict(tt3_training=True))))

    def test_finish_first_is_view_of_flow_mode(self):
        self.assertEqual(ff.apply(dict(tt3_finish_first=True), {}), dict(flow_mode='rolling'))
        self.assertTrue(ff.enabled('tt3_finish_first', dict(flow_mode='rolling')))
        self.assertFalse(ff.enabled('tt3_finish_first', dict(flow_mode='batch', flags=dict(tt3_finish_first=True))))


class FlagsOwnerOnly(DataCase):
    def setUp(self):
        super().setUp(); self.projects()

    def test_agent_reads_owner_changes(self):
        req = dict(op='settings', changes=dict(flags=dict(tt3_finish_first=True)))
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(req)
        self.assertTrue(mcp_server.call_tool('tt_work', req)['isError'])
        r = mcp_server.call_tool('tt_work', dict(op='flags'))
        self.assertFalse(r['isError'], r)
        w.action(req, origin='owner')
        self.assertEqual(w.settings()['flow_mode'], 'rolling')
        self.assertTrue(next(f for f in w.action(dict(op='flags'))['flags'] if f['name'] == 'tt3_finish_first')['enabled'])
        with self.assertRaisesRegex(ValueError, 'не реализован'):
            w.action(dict(op='settings', changes=dict(flags=dict(tt3_consensus=True))), origin='owner')
        with self.assertRaisesRegex(ValueError, 'оставьте одно'):
            w.action(dict(op='settings', changes=dict(flags=dict(tt3_finish_first=False), flow_mode='rolling')), origin='owner')

    def test_flags_off_keeps_legacy_admission(self):
        """IT-24: всё выключено явно — приём задач идёт по прежнему правилу спринта."""
        w.action(dict(op='settings', changes=dict(flags={n: False for n in ff.REGISTRY})), origin='owner')
        self.assertEqual(w.settings()['flow_mode'], 'batch')
        s = {'runs': [], 'settings': w.settings()}
        tasks = [dict(ref='p/T-%d' % i, status='queued') for i in range(6)]
        for t in tasks[:4]:
            sp.admit(s, t['ref'], tasks)
        with self.assertRaisesRegex(ValueError, 'Спринт SPR-1'):
            sp.admit(s, 'p/T-4', tasks)
