"""Панель не выдаёт аренду, завершённый процесс или старый журнал за работу."""
import copy
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from base import DataCase
import activity


class ActivityTests(DataCase):
    def setUp(self):
        super().setUp()
        self.at = datetime.now(timezone.utc)
        self.run = dict(id='RUN-1', ref='ege/EGE-20', status='running', agent='codex',
                        launched_at=self.at.isoformat(), launch=dict(pid=123, agent='codex'))

    def sample(self, alive=None, log=None, run=None):
        with mock.patch.object(activity, 'process_alive', return_value=alive), \
             mock.patch.object(activity, 'log_signal', return_value=log):
            return activity.snapshot([run or self.run], [], self.at)['rows'][0]

    def test_lease_is_not_activity(self):
        self.run['lease_until'] = (self.at + timedelta(hours=2)).isoformat()
        self.assertEqual(self.sample()['state'], 'unknown')
        self.assertEqual(self.sample(alive=True)['state'], 'quiet')

    def test_log_and_heartbeat_are_separate_evidence(self):
        row = self.sample(alive=True, log=self.at.timestamp()-3)
        self.assertEqual((row['state'], row['signal_age_seconds']), ('recent', 3))
        self.assertIsNotNone(row['log_at'])
        self.assertIsNone(row['heartbeat_at'])
        self.run['heartbeat_at'] = (self.at-timedelta(seconds=20)).isoformat()
        self.assertEqual(self.sample()['state'], 'recent')
        self.assertIsNone(self.sample()['process_alive'])

    def test_exited_process_and_pause_override_fresh_log(self):
        self.assertEqual(self.sample(alive=False, log=self.at.timestamp())['state'], 'exited')
        self.run['paused'] = dict(reason='limit', resume_at=self.at.isoformat())
        self.assertEqual(self.sample(alive=True, log=self.at.timestamp())['state'], 'paused')

    def test_old_and_future_signals_are_not_current_activity(self):
        for seconds in (-181, 3600):
            self.assertEqual(self.sample(alive=True, log=self.at.timestamp()+seconds)['state'], 'quiet')

    def test_only_open_runs_appear_and_waiting_is_counted_without_probes(self):
        waiting = dict(id='R2', ref='ege/EGE-23', status='queued')
        done = dict(self.run, status='done')
        with mock.patch.object(activity, 'process_alive') as probe, mock.patch.object(activity, 'log_signal') as log:
            out = activity.snapshot([waiting, done], [], self.at)
            probe.assert_not_called(); log.assert_not_called()
        self.assertEqual(out['rows'], [])
        self.assertEqual(out['queued'], {'ege': 1})

    def test_log_observation_is_read_only_and_never_guesses_other_session(self):
        path = Path(self.tmp)/'output.log'
        path.write_text('event', encoding='utf-8')
        os.utime(path, (self.at.timestamp()-5, self.at.timestamp()-5))
        self.run['launch']['log'] = str(path)
        before = copy.deepcopy(self.run)
        with mock.patch.object(activity.limits, 'session_of') as session:
            self.assertAlmostEqual(activity.log_signal(self.run), self.at.timestamp()-5, places=3)
            session.assert_not_called()
        self.assertEqual(self.run, before)
        path.unlink()
        self.assertIsNone(activity.log_signal(self.run))

    def test_missing_pid_is_unknown(self):
        self.assertIsNone(activity.process_alive(None))

    def test_all_active_runs_are_included_even_outside_last_50(self):
        runs = [self.run] + [dict(id='R%d' % n, ref='ege/EGE-23', status='done') for n in range(100)]
        with mock.patch.object(activity, 'process_alive', return_value=None), mock.patch.object(activity, 'log_signal', return_value=None):
            self.assertEqual(activity.snapshot(runs, [], self.at)['rows'][0]['run'], 'RUN-1')

    def test_workload_counts_live_processes_not_claims_or_pauses(self):
        self.run['model'] = 'gpt-6.1-sol'
        paused = dict(self.run, id='R2', paused=dict(reason='limit'))
        waiting = dict(id='R3', ref='ege/EGE-23', agent='codex', status='queued', packet=dict(route=dict(model='gpt-6.1-sol')))
        with mock.patch.object(activity, 'process_alive', return_value=True), mock.patch.object(activity, 'log_signal', return_value=None):
            out = activity.snapshot([self.run, paused, waiting], [], self.at,
                                    settings=dict(max_sessions=4, sessions_per_ai=dict(codex=2, claude=0)))
        group = out['workloads'][0]
        self.assertEqual((group['busy'], group['signals'], group['queued'], group['paused'], group['tasks']), (1, 0, 1, 1, 2))
        self.assertEqual(out['capacities'], dict(codex=2, claude=0))

    def test_models_share_provider_capacity_bounded_by_global_limit(self):
        self.run['model'] = 'model-A'
        other = dict(self.run, id='R2', ref='ege/EGE-46', model='model-B')
        with mock.patch.object(activity, 'process_alive', return_value=True), mock.patch.object(activity, 'log_signal', return_value=None):
            out = activity.snapshot([self.run, other], [], self.at,
                                    settings=dict(max_sessions=2, sessions_per_ai=dict(codex=6)))
        self.assertEqual(out['capacities']['codex'], 2)
        self.assertEqual([g['busy'] for g in out['workloads']], [1, 1])
