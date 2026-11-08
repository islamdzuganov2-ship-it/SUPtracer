"""Owner-only политика с предпросмотром и версией (TT-104; ТЗ TT-U-001: TTU-018, AC-FR03).

MCP-агент не может повысить свои права: ни через settings, ни через CLI pm.py work, ни через HTTP
без токена владельца, ни правкой журнала. Предпросмотр доступен всем и ничего не меняет;
каждое решение владельца — новая версия политики с записью в журнале.
"""
import json
import os
from pathlib import Path

from base import DataCase
import mcp_server
import owner_auth as oa
import workflow as w

ESCALATIONS = (dict(autonomy='full', consent=True), dict(max_sessions=12), dict(auto_merge=True),
               dict(failover=False), dict(sessions_per_ai=dict(claude=12)), dict(wip_analysis=50),
               dict(roles={'developer': 'codex'}), dict(default_executor='codex'), dict(lease_minutes=480),
               dict(reviewer='same'), dict(auto_pipeline=True), dict(context_ceiling=0))


class OwnerPolicyTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data) / 'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records': []})

    def test_no_setting_changes_from_agent_origins(self):
        before = w.settings()
        req = os.path.join(self.tmp, 'req.json')
        for ch in ESCALATIONS:
            with self.subTest(ch=ch):
                with self.assertRaisesRegex(ValueError, 'владельца'):
                    w.action(dict(op='settings', changes=ch))
                r = mcp_server.call_tool('tt_work', dict(op='settings', changes=ch))
                self.assertTrue(r['isError']); self.assertIn('владельца', r['content'][0]['text'])
                with open(req, 'w', encoding='utf-8') as f:
                    json.dump(dict(op='settings', changes=ch), f)
                self.assertEqual(w.main(['request', req]), 1)                          # CLI pm.py work request
        self.assertEqual(w.settings(), before)
        self.assertNotIn('policy', w.state())

    def test_other_owner_decisions_refused_for_agent(self):
        for req in (dict(op='answer', ref='alpha-api/AL-01', problem='P-1', option=1), dict(op='resume_all'),
                    dict(op='to_idea', project='alpha-api', dry_run=False),
                    dict(op='start', project='alpha-api', agents=['claude'], dry_run=False)):
            with self.subTest(op=req['op']):
                with self.assertRaisesRegex(ValueError, 'владельца'):
                    w.action(req)
                self.assertTrue(mcp_server.call_tool('tt_work', req)['isError'])
        # Предпросмотры безопасны и доступны агенту.
        self.assertTrue(w.action(dict(op='start', project='alpha-api', agents=['claude']))['dry_run'])

    def test_preview_shows_diff_without_applying(self):
        r = json.loads(mcp_server.call_tool('tt_work', dict(op='settings', preview=True,
                                                            changes=dict(max_sessions=6)))['content'][0]['text'])
        self.assertFalse(r['applied'])
        self.assertEqual(r['preview'], [dict(key='max_sessions', before=w.settings()['max_sessions'], after=6)])
        self.assertNotEqual(w.settings()['max_sessions'], 6)
        with self.assertRaisesRegex(ValueError, 'от 1 до 12'):                        # предпросмотр проверяет значения
            w.action(dict(op='settings', preview=True, changes=dict(max_sessions=99)))

    def test_owner_change_is_versioned(self):
        r1 = w.action(dict(op='settings', changes=dict(max_sessions=6)), origin='owner')
        r2 = w.action(dict(op='settings', changes=dict(autonomy='full', consent=True)), origin='owner')
        self.assertEqual((r1['policy_version'], r2['policy_version']), (1, 2))
        p = w.state()['policy']
        self.assertEqual([e['version'] for e in p['log']], [1, 2])
        self.assertEqual(p['log'][0]['diff'], [dict(key='max_sessions', before=4, after=6)])
        self.assertTrue(all(e['origin'] == 'owner' and len(e['hash']) == 16 for e in p['log']))
        self.assertNotEqual(p['log'][0]['hash'], p['log'][1]['hash'])
        self.assertTrue(oa.verify(p['grant']))
        # by из запроса не подменяет автора решения.
        w.action(dict(op='settings', changes=dict(autonomy='step'), by='злоумышленник'), origin='owner')
        self.assertEqual(w.settings()['autonomy_changed']['by'], 'владелец')

    def test_owner_token(self):
        self.assertIsNone(oa.board_token())                                         # ключа ещё нет — токена нет
        self.assertFalse(oa.check_token('x' * 64))
        tok = oa.board_token(create=True)
        self.assertTrue(oa.check_token(tok))
        self.assertFalse(oa.check_token(tok[:-1] + ('0' if tok[-1] != '0' else '1')))
        self.assertFalse(oa.check_token(None))
        self.assertEqual(oa.board_token(create=True), tok)                           # ключ не пересоздаётся
        self.assertTrue(str(oa.key_path()).startswith(self.tmp))                     # в тестах — не настоящий

    def test_key_lives_outside_repo_and_data(self):
        saved = oa._path
        oa._path = None
        try:
            p = oa.key_path().resolve()
        finally:
            oa._path = saved
        root = Path(__file__).resolve().parent.parent
        self.assertFalse(str(p).startswith(str(root)))
        self.assertFalse(str(p).startswith(str(Path(self.data).resolve())))
