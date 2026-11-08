"""Экономия в токенах (TT-90): указатель документа, сторож чтений, лёгкий старт ролей, профиль токенов."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from base import DataCase
import launcher
import mcp_server
import pipeline as P
import read_guard
import tokens

DOC = ('# ТЗ\nвступление BR-1\n## Требования\n### FR-1 Вход\nтекст FR-1 и FR-2\n### FR-3 Выход\nFR-3, NFR-1\n'
       '## Тесты\n```\n# не заголовок\n```\nFR-1\n')


class IndexTests(unittest.TestCase):
    def test_index_sections_lines_and_first_named_codes(self):
        idx = P.doc_index(DOC)
        self.assertEqual([(e['n'], e['level'], e['title']) for e in idx],
                         [(1, 1, 'ТЗ'), (2, 2, 'Требования'), (3, 3, 'FR-1 Вход'), (4, 3, 'FR-3 Выход'), (5, 2, 'Тесты')])
        self.assertEqual([e['codes'] for e in idx], [['BR-1'], [], ['FR-1', 'FR-2'], ['FR-3', 'NFR-1'], []])
        self.assertEqual(idx[1]['lines'], [3, 7])                      # раздел с подразделами — для Read offset/limit
        line = P.index_lines(idx)[2]
        self.assertIn('FR-1–2', line); self.assertIn('стр. 4–5', line); self.assertTrue(line.startswith('    3 '))

    def test_section_by_number_code_and_title(self):
        e, text = P.section_by(DOC, 'fr-3')
        self.assertEqual(text, '### FR-3 Выход\nFR-3, NFR-1')       # код — только раздел, где он впервые назван
        self.assertEqual(P.section_by(DOC, '2')[1].count('###'), 2)  # номер — с подразделами
        self.assertTrue(P.section_by(DOC, 'тест')[1].startswith('## Тесты'))
        self.assertEqual(P.section_by(DOC, 'нет')[0], None)

    def test_code_span_and_token_estimate(self):
        self.assertEqual(P.code_span(['FR-3', 'FR-1', 'FR-2', 'FR-7', 'NFR-1']), 'FR-1–3, FR-7, NFR-1')
        self.assertGreater(P.tokens('я' * 250), P.tokens('a' * 250))  # кириллица дороже латиницы
        self.assertEqual(P.tokens(''), 0)


class ReadGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {'TASKTREKER_READS': os.path.join(self.tmp, 'reads'),
                                                'TASKTREKER_OWN': 'docs/tasks/T-1/SRS.md'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.docs = Path(self.tmp) / 'docs' / 'tasks' / 'T-1'; self.docs.mkdir(parents=True)

    def read(self, path, **extra):
        return read_guard.decide(dict(hook_event_name='PreToolUse', tool_name='Read', session_id='S1', cwd=self.tmp,
                                      tool_input=dict(file_path=str(path), **extra)))

    def test_repeat_full_read_of_unchanged_file_is_denied(self):
        small = Path(self.tmp) / 'b.py'; small.write_text('print(1)\n', encoding='utf-8')
        self.assertIsNone(self.read(small)); self.assertIsNone(self.read(small))   # мелкий повтор дешевле отказа
        body = 'x = 1\n' * 2000
        f = Path(self.tmp) / 'a.py'; f.write_text(body + 'print(1)\n', encoding='utf-8')
        self.assertIsNone(self.read(f))
        out = self.read(f)
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('уже прочитан', out['hookSpecificOutput']['permissionDecisionReason'])
        self.assertIsNone(self.read(f, offset=1, limit=5))           # фрагмент — можно
        time.sleep(0.01); f.write_text(body + 'print(2)\n', encoding='utf-8')
        self.assertIsNone(self.read(f))                              # файл изменился — можно
        st = read_guard.load('S1'); self.assertEqual(st['denied'], 1); self.assertGreater(st['saved_tokens'], 0)

    def test_full_read_follows_role_and_complexity_policy(self):
        big = '# BRD\n' + ''.join('## Раздел %d BR-%d\n%s\n' % (i, i, 'текст требования ' * 60) for i in range(1, 9))
        brd = self.docs / 'BRD.md'; brd.write_text(big, encoding='utf-8')
        pol = lambda **kw: mock.patch.dict(os.environ, {'TASKTREKER_READ_POLICY': json.dumps(dict(dict(free=3000, budget=20000, tier=1, primary=[]), **kw))})
        with pol():
            out = self.read(brd)['hookSpecificOutput']['permissionDecisionReason']
            self.assertIn('класс сложности 1', out); self.assertIn('~20 тыс', out)
        with pol(free=8000, tier=3):
            self.assertIsNone(self.read(brd))                        # сложная задача: порог выше — читается целиком
        time.sleep(0.01); brd.write_text(big + '\n', encoding='utf-8')   # изменился — не повтор прочитанного
        with pol(primary=['docs/tasks/T-1/BRD.md']):
            self.assertIsNone(self.read(Path(self.tmp) / 'docs' / 'tasks' / 'T-1' / 'BRD.md'))   # вход этапа роли

    def test_big_foreign_document_gets_index_instead_of_text(self):
        big = '# BRD\n' + ''.join('## Раздел %d BR-%d\n%s\n' % (i, i, 'текст требования ' * 60) for i in range(1, 9))
        brd = self.docs / 'BRD.md'; brd.write_text(big, encoding='utf-8')
        out = self.read(brd)['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn('без причины не читается', out); self.assertIn('2 Раздел 1 BR-1', out); self.assertIn('offset', out)
        self.assertIn('full=true why=', out)                         # путь к полному чтению с обоснованием
        own = self.docs / 'SRS.md'; own.write_text(big, encoding='utf-8')
        self.assertIsNone(self.read(own))                            # свой артефакт роль читает
        small = self.docs / 'HLD.md'; small.write_text('# HLD\nкоротко\n', encoding='utf-8')
        self.assertIsNone(self.read(small))

    def test_compaction_resets_and_errors_allow(self):
        f = Path(self.tmp) / 'a.md'; f.write_text('# A\n', encoding='utf-8')
        self.read(f)
        read_guard.decide(dict(hook_event_name='SessionStart', source='compact', session_id='S1'))
        self.assertIsNone(self.read(f))                              # после сжатия прочитанного в контексте нет
        self.assertIsNone(self.read(Path(self.tmp) / 'нет.md'))
        self.assertIsNone(read_guard.decide(dict(tool_name='Bash', tool_input={})))


class LeanLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.object(launcher, 'LOGS', Path(self.tmp)); p.start(); self.addCleanup(p.stop)

    def test_claude_role_session_starts_lean(self):
        args, env = launcher.lean('claude', 'developer', own=['docs/tasks/T-1/notes.md'])
        self.assertEqual(env['TASKTREKER_OWN'], 'docs/tasks/T-1/notes.md')
        for flag in ('--strict-mcp-config', '--setting-sources', '--append-system-prompt-file', '--exclude-dynamic-system-prompt-sections'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--tools') + 1], 'Bash,Read,Edit,Write,Glob,Grep')
        self.assertEqual(args[args.index('--setting-sources') + 1], 'project,local')
        mcp = json.loads(Path(args[args.index('--mcp-config') + 1]).read_text(encoding='utf-8'))
        self.assertEqual(list(mcp['mcpServers']), ['tasktreker'])
        self.assertEqual(mcp['mcpServers']['tasktreker']['env'], {'TASKTREKER_MCP_ROLE': '1'})
        conf = json.loads(Path(args[args.index('--settings') + 1]).read_text(encoding='utf-8'))
        self.assertIn('read_guard.py', json.dumps(conf['hooks']['PreToolUse']))
        self.assertIn('name: tt-developer', Path(args[args.index('--append-system-prompt-file') + 1]).read_text(encoding='utf-8'))
        cmd = launcher.headless_command('claude', 'claude', 'm', 'high', 'ЗАДАНИЕ', '.', session='S', extra=args)
        self.assertEqual(cmd[-1], 'ЗАДАНИЕ')                          # --tools и --mcp-config не съедают задание
        self.assertLess(cmd.index('--tools'), cmd.index('--model'))

    def test_web_tools_only_for_research_and_switch_off(self):
        tools = lambda role, research=True: (lambda a: a[a.index('--tools') + 1])(launcher.lean('claude', role, research=research)[0])
        self.assertIn('WebSearch', tools('product_owner'))
        self.assertNotIn('WebSearch', tools('product_owner', research=False))
        self.assertNotIn('WebSearch', tools('system_analyst'))
        with mock.patch.dict(os.environ, {'TASKTREKER_LEAN': '0'}):
            self.assertEqual(launcher.lean('claude', 'developer'), ([], {}))

    def test_codex_gets_role_mode_mcp(self):
        args, _ = launcher.lean('codex', 'qa')
        self.assertEqual(args, ['-c', 'mcp_servers.tasktreker.env.TASKTREKER_MCP_ROLE="1"'])
        cmd = launcher.headless_command('codex', 'codex', 'm', 'high', 'ЗАДАНИЕ', '.', extra=args)
        self.assertIn('mcp_servers.tasktreker.env.TASKTREKER_MCP_ROLE="1"', cmd); self.assertEqual(cmd[-1], 'ЗАДАНИЕ')


class McpRoleModeTests(unittest.TestCase):
    def test_role_mode_lists_two_short_tools(self):
        full = len(json.dumps(mcp_server.listed_tools(), ensure_ascii=False))
        with mock.patch.dict(os.environ, {'TASKTREKER_MCP_ROLE': '1'}):
            tools = mcp_server.listed_tools()
            self.assertEqual([t['name'] for t in tools], ['tt_work', 'tt_recall'])
            self.assertLess(len(json.dumps(tools, ensure_ascii=False)) * 3, full)
            self.assertLess(len(mcp_server.instructions()), 400)

    def test_schema_accepts_every_field_the_server_reads(self):
        tool = next(t for t in mcp_server.TOOLS if t['name'] == 'tt_work')
        mcp_server._validate(tool, dict(op='doc', ref='p/T-1', path='docs/x.md', section='3', code='FR-3'))
        mcp_server._validate(tool, dict(op='list', offset=20, limit=20))
        mcp_server._validate(tool, dict(op='handoff', contract_group='экспорт', items={}))
        mcp_server._validate(tool, dict(op='resolve', recipe=dict(title='t'), priority='🔴'))


class ProjectDocsTests(unittest.TestCase):
    def test_large_docs_listed_by_size_without_reading(self):
        repo = Path(tempfile.mkdtemp()); git = lambda *a: subprocess.run(['git', *a], cwd=repo, capture_output=True)
        git('init', '-q', '-b', 'main'); (repo / 'docs').mkdir()
        (repo / 'docs' / 'SPECIFICATION.md').write_text('# Спец\n' + 'текст ' * 2000, encoding='utf-8')
        (repo / 'docs' / 'small.md').write_text('# мало\n', encoding='utf-8')
        git('add', '.'); git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'docs')
        import orchestra
        ctx = type('C', (), dict(repo=str(repo), branch='main'))()
        docs = orchestra.project_docs(ctx)
        self.assertEqual(len(docs), 1); self.assertTrue(docs[0].startswith('docs/SPECIFICATION.md ~'))


class ProfileTests(DataCase):
    def test_profile_counts_start_peak_turns_and_repeats(self):
        home = Path(self.tmp) / 'claude'; (home / 'projects' / 'p').mkdir(parents=True)
        def turn(rid, ctx, read=None):
            content = [dict(type='tool_use', id='t' + rid, name='Read', input=dict(file_path=read))] if read else []
            return json.dumps(dict(type='assistant', requestId=rid, message=dict(content=content, usage=dict(
                input_tokens=1, cache_creation_input_tokens=100, cache_read_input_tokens=ctx, output_tokens=10))))
        rows = [turn('a', 15000, 'x.md'), turn('a', 15000, 'x.md'), turn('b', 20000, 'x.md'), turn('c', 30000)]
        (home / 'projects' / 'p' / 'S1.jsonl').write_text('\n'.join(rows) + '\n', encoding='utf-8')
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(home), 'TASKTREKER_READS': str(Path(self.tmp) / 'r')}):
            p = tokens.profile(dict(launch=dict(agent='claude', session='S1')))
        self.assertEqual((p['start'], p['peak'], p['turns']), (15101, 30101, 3))
        self.assertEqual((p['full_reads'], p['repeat_reads']), (2, 1))
        self.assertEqual(p['total'], 3 * 111 + 65000)
        s = tokens.summary([dict(p, role='qa', agent='claude', lean=True)])
        self.assertEqual((s[0]['start_mode'], s[0]['runs']), ('лёгкий', 1))
        self.assertEqual(p['series'], [15101, 20101, 30101])        # контекст каждого хода — для кривой дашборда
        self.assertEqual(p['read_at'], [0, 1])
        synth = json.dumps(dict(type='assistant', requestId='z', message=dict(model='<synthetic>', content=[], usage=dict(
            input_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=0))))
        with open(home / 'projects' / 'p' / 'S1.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(synth + '\n')
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(home), 'TASKTREKER_READS': str(Path(self.tmp) / 'r')}):
            p = tokens.profile(dict(launch=dict(agent='claude', session='S1')))
        self.assertEqual((p['turns'], p['series'][-1]), (3, 30101))   # служебная запись клиента — не ход


class TokviewTests(unittest.TestCase):
    """Раздел «Токены ролей»: сводки считаются из профилей запусков, без журналов."""
    def rows(self):
        import tokview
        base = dict(project='p', stage='analysis', lean=False, index=False, budget=None, relays=0, justified=0, saved=0,
                    start=20000, turns=10, cache_read=0, peak=0, at='2026-10-20')
        return tokview, [dict(base, run='R1', ref='p/T1', role='system_analyst', week='2026-W43', total=1_000_000, accepted=True),
                         dict(base, run='R2', ref='p/T2', role='system_analyst', week='2026-W43', total=3_000_000, accepted=False),
                         dict(base, run='R3', ref='p/T3', role='system_analyst', week='2026-W44', total=1_000_000, accepted=True,
                              lean=True, start=8000),
                         dict(base, run='R4', ref='p/T4', role='system_analyst', week='2026-W44', total=1_200_000, accepted=True)]

    def test_tokens_per_accepted_handoff_count_failed_runs(self):
        tv, rows = self.rows()
        ph = {(x['role'], x['week']): x for x in tv.per_handoff(rows)}
        self.assertEqual(ph[('system_analyst', '2026-W43')]['per'], 4_000_000)     # 4 млн на одну принятую сдачу
        self.assertEqual(ph[('system_analyst', '')]['per'], 6_200_000 // 3)
        self.assertEqual(tv.summary(rows)['kpi']['per'], 6_200_000 // 3)

    def test_before_after_and_outliers(self):
        tv, rows = self.rows()
        lean = tv.compare(rows)[0]['roles'][0]
        self.assertEqual((lean['before']['n'], lean['after']['n'], lean['after']['start']), (3, 1, 8000))
        self.assertEqual([o['run'] for o in tv.outliers(rows)], ['R2'])                  # 3 млн ≥ 2 × медиана 1,1 млн
        self.assertEqual(tv.matrix(rows)[0]['n'], 4)

    def test_squeeze_keeps_peak_and_marks(self):
        import tokview
        pts, marks, step = tokview.squeeze(list(range(300)), [0, 150, 299], limit=100)
        self.assertEqual((len(pts), step, pts[-1], marks), (100, 3, 299, [0, 50, 99]))


if __name__ == '__main__':
    unittest.main()
