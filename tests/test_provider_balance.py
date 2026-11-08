"""Баланс Claude/Codex и mono-failover (TT-108; ТЗ FR-015/016, TTU-004…010, TC-36…54)."""
from datetime import timedelta
from pathlib import Path
from unittest import mock

from base import DataCase
import flags as ff
import mcp_server
import orchestra
import provider_balance as pb
import workflow as w

ANALYSIS = ['system_analyst', 'product_owner', 'solution_architect', 'system_analyst', 'developer']


class Base(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        self.at = w.now() - timedelta(days=2)
        self.t = dict(ref='alpha-api/AL-01', project='alpha-api', id='AL-01', size='S', risk='low', work_type='implementation',
                      pipeline=dict(roles={}))

    def state(self, **settings):
        return dict(runs=[], settings=settings, provider_limits={})

    def done(self, s, agent, role, at, ref='alpha-api/AL-01', accepted=True, status='done'):
        r = dict(id='RUN-%d' % (len(s['runs']) + 1), ref=ref, role=role, agent=agent, status=status, finished_at=at.isoformat())
        if accepted:
            r['handoff'] = dict(summary='ok')
        s['runs'].append(r)
        return r

    def limit(self, s, agent, until, at=None):
        w.note_limit(s, agent, until, at or self.at)


class Health(Base):
    def test_modes(self):                                                          # TC-45…47
        s = self.state(); at = self.at
        self.assertEqual(pb.mode(s, at)[0], 'balanced')
        self.limit(s, 'claude', at + timedelta(hours=1))
        self.assertEqual(pb.mode(s, at)[0], 'mono_codex')
        self.limit(s, 'codex', at + timedelta(hours=1))
        self.assertEqual(pb.mode(s, at)[0], 'paused_all')
        s2 = self.state(); self.limit(s2, 'codex', at + timedelta(hours=1))
        self.assertEqual(pb.mode(s2, at)[0], 'mono_claude')
        self.assertEqual(pb.mode(self.state(), at, cli=lambda a: a == 'codex')[0], 'mono_codex')   # CLI не найден

    def test_unknown_reset_is_not_ready(self):                                     # TC-53
        s = self.state(); self.limit(s, 'claude', self.at + timedelta(hours=5))
        h = pb.health(s, 'claude', self.at)
        self.assertEqual((h['state'], h['confidence']), ('rate_limited', 'estimated'))

    def test_recovery_needs_two_successes_and_cooldown(self):                      # TC-50, TC-51
        s = self.state(); at = self.at; until = at + timedelta(minutes=10)
        self.limit(s, 'claude', until)
        later = until + timedelta(seconds=30)
        self.assertEqual(pb.health(s, 'claude', later)['state'], 'degraded')
        self.assertEqual(pb.mode(s, later)[0], 'mono_codex')
        self.done(s, 'claude', 'developer', until + timedelta(seconds=20))
        self.assertEqual(pb.health(s, 'claude', until + timedelta(seconds=40))['state'], 'degraded')     # один успех
        self.done(s, 'claude', 'qa', until + timedelta(seconds=50))
        self.assertEqual(pb.health(s, 'claude', until + timedelta(minutes=5))['state'], 'degraded')       # разрыв < 60 с
        self.done(s, 'claude', 'qa', until + timedelta(seconds=130))
        self.assertEqual(pb.health(s, 'claude', until + timedelta(seconds=150))['state'], 'degraded')     # пауза < 180 с
        self.assertEqual(pb.health(s, 'claude', until + timedelta(seconds=200))['state'], 'ready')
        self.assertTrue(pb.sync(s, until + timedelta(seconds=200)))
        self.assertNotIn('claude', s['provider_limits'])                          # подтверждено — запись снята
        self.assertEqual(s['provider_modes'][-1]['mode'], 'balanced')
        self.assertFalse(pb.sync(s, until + timedelta(seconds=260)))              # без смены режима — без записи

    def test_success_then_new_limit_no_ping_pong(self):                            # TC-50
        s = self.state(); until = self.at + timedelta(minutes=10)
        self.limit(s, 'claude', until)
        self.done(s, 'claude', 'qa', until + timedelta(seconds=10))
        self.limit(s, 'claude', until + timedelta(hours=1), until + timedelta(seconds=20))
        self.assertEqual(pb.health(s, 'claude', until + timedelta(seconds=30))['state'], 'rate_limited')

    def test_probe_session_bounded_in_balanced_mode(self):                         # TC-53
        s = self.state(balance_mode='balanced'); until = self.at + timedelta(minutes=1)
        self.limit(s, 'claude', until)
        at = until + timedelta(minutes=1)
        self.assertEqual(pb.eligible(s, self.t, 'qa', at)[0], ['claude', 'codex'])
        s['runs'].append(dict(id='RUN-P', ref='alpha-api/AL-02', agent='claude', status='running', launched_at=at.isoformat()))
        cands, excluded, _ = pb.eligible(s, self.t, 'qa', at)
        self.assertEqual(cands, ['codex']); self.assertIn('пробная', excluded['claude'])

    def test_success_marks_once_and_keeps_record(self):                           # TT-100, TC-49
        at = w.now(); s = w.state()
        w.note_limit(s, 'codex', at + timedelta(hours=1), at - timedelta(minutes=5)); w.save('workflow.json', s)
        ok = at - timedelta(minutes=1)
        self.assertTrue(w.mark_provider_available('codex', ok))
        self.assertFalse(w.mark_provider_available('codex', ok))                  # тот же успех повторно
        self.assertFalse(w.limited(None, 'codex'))
        self.assertEqual(pb.health(w.state(), 'codex', at)['state'], 'degraded')


class Units(Base):
    def test_only_accepted_balanced_work_counts(self):                            # TC-41, TC-42, TC-43
        s = self.state(); at = self.at
        pb.sync(s, at - timedelta(minutes=1))
        self.done(s, 'claude', 'developer', at)
        self.done(s, 'claude', 'developer', at, accepted=False, status='running')   # отказанный handoff
        self.done(s, 'codex', 'qa', at, accepted=False, status='cancelled')         # передача по лимиту
        self.done(s, 'codex', 'qa', at)
        s['runs'].append(dict(id='RUN-T', ref='alpha-api/AL-01', role='teamlead', agent='codex', status='done', finished_at=at.isoformat()))
        tasks = pb.backlog()
        rep = pb.units(s, tasks, at + timedelta(minutes=1), 30)
        self.assertEqual(rep['events'], 2); self.assertEqual(rep['status'], 'insufficient_sample')
        self.assertEqual(rep['providers']['claude']['share'], 0.5)
        s['provider_modes'].append(dict(mode='mono_codex', at=(at + timedelta(minutes=2)).isoformat()))
        self.done(s, 'codex', 'qa', at + timedelta(minutes=3))
        rep = pb.units(s, tasks, at + timedelta(minutes=4), 30)
        self.assertEqual((rep['events'], rep['skipped_mono']), (2, 1))           # период mono не начисляется

    def corpus(self, mode, n=60):
        s = self.state(balance_mode=mode); at = self.at
        pb.sync(s, at - timedelta(minutes=1))
        with mock.patch.object(w, 'state', return_value=s):
            for i in range(n):
                role = ANALYSIS[i % len(ANALYSIS)]
                d = pb.choose(s, self.t, role, at)
                self.done(s, d['selected'], role, at)
                at += timedelta(minutes=10)
        return s, pb.units(s, pb.backlog(), at, 30)

    def test_balanced_corpus_45_55(self):                                          # TC-36, TTU-007
        s, rep = self.corpus('balanced')
        self.assertEqual(rep['status'], 'in_tolerance', rep)
        for a in pb.PROVIDERS:
            self.assertGreaterEqual(rep['providers'][a]['share'], 0.45); self.assertLessEqual(rep['providers'][a]['share'], 0.55)
        self.assertTrue(all(d['balance_mode'] == 'balanced' for d in s['provider_decisions']))

    def test_prefer_mode_keeps_legacy_and_shadows(self):                           # AC-14.8-подобно, NFR-36
        s, rep = self.corpus('prefer')
        self.assertEqual(rep['status'], 'skewed')
        self.assertAlmostEqual(rep['providers']['claude']['share'], 0.8, delta=0.02)   # 4 из 5 ролей — анализ у Claude
        self.assertTrue(any(d['shadow_selected'] != d['selected'] for d in s['provider_decisions']))


class Choice(Base):
    def test_affinity_and_pin(self):                                               # TC-37, TC-38, TC-39
        s = self.state(balance_mode='balanced', role_affinity={'system_analyst': {'eligible': ['claude']}}, roles={'developer': 'claude'})
        for _ in range(3):
            self.done(s, 'claude', 'system_analyst', self.at)                     # Claude уже впереди
        d = pb.choose(s, self.t, 'system_analyst', self.at)
        self.assertEqual(d['selected'], 'claude'); self.assertIn('eligible', d['excluded_reasons']['codex'])
        d = pb.choose(s, self.t, 'developer', self.at)
        self.assertEqual(d['selected'], 'claude'); self.assertTrue(d['policy_constrained'])
        self.limit(s, 'claude', self.at + timedelta(hours=1))                     # закреплённый на лимите — роль второму
        self.assertEqual(pb.choose(s, self.t, 'developer', self.at)['selected'], 'codex')

    def test_independent_review_for_critical(self):                                # TC-40, TC-52, TTU-010
        hard = dict(self.t, size='XL', risk='high', work_type='architecture',
                    pipeline=dict(roles={'development/developer': dict(agent='claude')}))
        s = self.state(balance_mode='balanced')
        self.assertTrue(pb.strict(hard))
        for _ in range(4):
            self.done(s, 'codex', 'qa', self.at)                                   # долг баланса тянет к Claude
        d = pb.choose(s, hard, 'reviewer', self.at)
        self.assertEqual(d['selected'], 'codex'); self.assertIn('независимость', d['excluded_reasons']['claude'])
        self.limit(s, 'codex', self.at + timedelta(hours=1))
        d = pb.choose(s, hard, 'reviewer', self.at)
        self.assertEqual((d['selected'], d['eligible']), ('codex', []))           # ждёт независимого, не уходит автору
        self.assertIn('ждёт', d['waiting'])
        run = dict(ref=hard['ref'], role='reviewer', stage='review', agent='claude')
        self.assertIn('независимого', orchestra.hold(s, run, {hard['ref']: hard}))
        easy = dict(self.t, pipeline=dict(roles={'development/developer': dict(agent='claude')}))
        self.assertEqual(pb.choose(s, easy, 'reviewer', self.at)['selected'], 'claude')   # простая — как раньше

    def test_pinned_author_never_reviews_own_hard_task(self):                     # 07: AG-REV-01, A3-05, E2E-R06
        hard = dict(self.t, size='XL', risk='high', work_type='architecture',
                    pipeline=dict(roles={'development/developer': dict(agent='claude')}))
        for mode in ('balanced', 'prefer'):
            s = self.state(balance_mode=mode, roles=dict(reviewer='claude'))           # владелец закрепил ревьюера за автором
            d = pb.choose(s, hard, 'reviewer', self.at)
            self.assertEqual(d['selected'], 'codex', mode)
            self.assertIn('закрепление', d['excluded_reasons']['claude'])
            with mock.patch.object(pb, 'choose', return_value=dict(d, selected=None)):
                self.assertEqual(orchestra.role_agent(s, hard, 'reviewer'), 'codex')    # и запасной путь не уходит автору

    def test_decision_fields(self):
        s = self.state()
        d = pb.choose(s, self.t, 'qa', self.at, run_id='RUN-9')
        for k in ('task_ref', 'role', 'eligible', 'excluded_reasons', 'selected', 'reason', 'mode', 'policy_version',
                  'balanced_share_before', 'at', 'run_id', 'quota_state', 'debt'):
            self.assertIn(k, d)
        self.assertIsNone(d['quality_estimate']); self.assertIsNone(d['cost_estimate'])        # неизвестное — null
        self.assertEqual(s['provider_decisions'][-1]['run_id'], 'RUN-9')


class Handover(Base):
    def setUp(self):
        super().setUp()
        (Path(self.data) / 'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': [{'id': 'request', 'project': 'alpha-api'}]})
        w.action(dict(op='plan', project='alpha-api', tasks=[dict(
            id='AL-03', title='Правка', why='Нужна', root='request', size='S', priority='🟡', risk='low',
            work_type='implementation', acceptance=['Тест проходит'], depends_on=[])]))
        w.action(dict(op='settings', changes=dict(failover=True)), origin='owner')

    def paused_run(self, pid=None):
        s = w.state(); at = w.now()
        run = w.new_run(s, 'alpha-api/AL-03', None, at)
        run.update(agent='claude', status='running', paused=dict(reason='limit', checkpoint='сделано половина'),
                   launch=dict(agent='claude', pid=pid) if pid else {})
        w.note_limit(s, 'claude', at + timedelta(hours=1), at); w.save('workflow.json', s)
        return run['id']

    def test_repeat_event_one_handover(self):                                      # TC-49, TC-54
        rid = self.paused_run()
        with mock.patch.object(w, 'cli_ok', return_value=True):
            first = w.failover_run('alpha-api/AL-03', rid)
            again = w.failover_run('alpha-api/AL-03', rid)
        self.assertEqual(first['agent'], 'codex'); self.assertIsNone(again)
        runs = [r for r in w.state()['runs'] if r['ref'] == 'alpha-api/AL-03' and r['status'] in ('queued', 'running')]
        self.assertEqual(len(runs), 1)
        s = w.state(); old = next(r for r in s['runs'] if r['id'] == rid)
        self.assertIsNone(w.failover(s, w.task_for('alpha-api/AL-03'), dict(old, status='running'), w.now()))   # дубль события

    def test_single_writer_handover(self):                                         # TC-48, TTU-009
        rid = self.paused_run(pid=4242)
        with mock.patch.object(w, 'cli_ok', return_value=True), \
                mock.patch.object(orchestra, 'autocommit', return_value='abc1234') as ac, \
                mock.patch('launcher.alive', return_value=True), mock.patch('launcher.terminate') as term:
            out = w.failover_run('alpha-api/AL-03', rid)
        self.assertEqual(out['wip'], 'abc1234'); ac.assert_called_once()
        term.assert_called_once_with(4242)

    def test_both_limited_no_handover(self):                                       # TC-47
        rid = self.paused_run()
        s = w.state(); w.note_limit(s, 'codex', w.now() + timedelta(hours=1), w.now()); w.save('workflow.json', s)
        with mock.patch.object(w, 'cli_ok', return_value=True):
            self.assertIsNone(w.failover_run('alpha-api/AL-03', rid))
        self.assertEqual(w.provider_sync(), 'paused_all')
        r = next(r for r in w.state()['runs'] if r['id'] == rid)
        self.assertEqual(r['status'], 'running'); self.assertTrue(r['paused'])     # работа не отменена


class OwnerPolicy(Base):
    def test_settings_owner_only_and_flag(self):
        req = dict(op='settings', changes=dict(balance_mode='balanced'))
        with self.assertRaisesRegex(ValueError, 'владельца'):
            w.action(req)
        self.assertTrue(mcp_server.call_tool('tt_work', dict(op='settings', changes=dict(target_shares={'claude': 0.6, 'codex': 0.4})))['isError'])
        for bad in (dict(balance_mode='fair'), dict(target_shares={'claude': 0.7, 'codex': 0.4}), dict(target_shares={'claude': 1}),
                    dict(affinity_bonus=0.5), dict(role_affinity={'developer': {'eligible': ['gemini']}})):
            with self.assertRaises(ValueError):
                w.action(dict(op='settings', changes=bad), origin='owner')
        w.action(dict(op='settings', changes=dict(target_shares={'claude': 0.6, 'codex': 0.4})), origin='owner')
        w.action(dict(op='settings', changes=dict(flags=dict(tt3_provider_balance=True))), origin='owner')
        self.assertEqual(w.settings()['balance_mode'], 'balanced')
        self.assertTrue(ff.enabled('tt3_provider_balance', w.settings()))
        r = mcp_server.call_tool('tt_work', dict(op='balance'))
        self.assertFalse(r['isError'], r)
        rep = w.action(dict(op='balance'))
        self.assertEqual(rep['target_shares'], {'claude': 0.6, 'codex': 0.4})
        self.assertEqual(set(rep['windows']), {7, 30})
