import importlib
import json
from pathlib import Path

from base import DataCase


class CodexMemory(DataCase):
    def setUp(self):
        super().setUp()
        self.p = self.projects()["projects"]
        import collect_usage
        importlib.reload(collect_usage)
        import codex_memory
        self.cm = importlib.reload(codex_memory)
        self.home = Path(self.tmp) / "codex"

    def log(self, sid="s1", cwd=None, messages=None, parent="", directory="sessions"):
        cwd = cwd or self.p["alpha-api"]["path"]
        rows = [{"type": "session_meta", "timestamp": "2026-10-04T10:00:00Z", "payload": {
            "id": sid, "cwd": cwd, "timestamp": "2026-10-04T10:00:00Z",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"}},
            {"type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-test"}}]
        for i, (role, text, phase) in enumerate(messages or [("user", "Добавь поиск", None), ("assistant", "Поиск добавлен", "final_answer")]):
            rows.append({"type": "response_item", "timestamp": "2026-10-04T10:01:%02dZ" % i,
                "payload": {"type": "message", "id": sid + str(i), "role": role, "phase": phase,
                            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}})
        path = self.home / directory / (sid + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return path, rows

    def test_requests_answers_are_nested_and_repeated_refresh_is_idempotent(self):
        path, _ = self.log()
        self.cm.import_sessions(self.home)
        before = {r["id"]: r for r in self.t.memory.load_all()}
        self.cm.import_sessions(self.home)
        self.assertEqual(len(self.t.memory.load_all()), 3)
        user = next(r for r in before.values() if r.get("role") == "user")
        answer = next(r for r in before.values() if r.get("role") == "assistant")
        self.assertEqual(answer["parent"], user["id"])
        self.t.memory.build_index()
        tree, _ = self.t.tree.build(False)
        project = next(n for n in tree["root"]["children"] if n["project"] == "alpha-api")
        session = next(n for n in project["children"] if n.get("session"))
        self.assertEqual(session["children"][0]["children"][0]["role"], "assistant")
        archive = self.home / "archived_sessions" / "copy.jsonl"
        archive.parent.mkdir()
        archive.write_bytes(path.read_bytes())
        out, _ = self.cm.import_sessions(self.home)
        self.assertEqual(out["requests"], 1)
        self.assertEqual(len(self.memory_files()), 3)

    def test_unknown_project_is_visible_without_rewriting_manual_portfolio(self):
        before = (Path(self.data) / "projects.json").read_bytes()
        self.log(cwd="C:/Users/test/projects/new_project")
        self.cm.import_sessions(self.home)
        self.t.memory.build_index()
        tree, _ = self.t.tree.build(False)
        branch = next(n for n in tree["root"]["children"] if n["project"] == "new_project")
        self.assertTrue(branch["children"])
        self.assertTrue(branch["discovered"])
        self.assertEqual(before, (Path(self.data) / "projects.json").read_bytes())
        self.assertTrue(all(r["project"] == "портфель" for r in self.t.memory.load_all()))

    def test_environment_commentary_and_reasoning_do_not_enter_memory(self):
        self.log(messages=[("user", "# AGENTS.md instructions for C:/Work\nPrivate rules", None),
            ("user", "<environment_context>Private context</environment_context>", None),
            ("user", "Сделай экспорт", None), ("assistant", "Смотрю файлы", "commentary"),
            ("assistant", "Экспорт готов", "final_answer")])
        out, _ = self.cm.import_sessions(self.home)
        self.assertEqual((out["requests"], out["answers"]), (1, 1))
        text = " ".join(r["body"] for r in self.t.memory.load_all())
        self.assertNotIn("Private", text)
        self.assertNotIn("Смотрю", text)

    def test_ambient_browser_snapshots_are_not_tasks_but_attached_requests_survive(self):
        self.log(messages=[('user','<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>',None),
            ('user','<in-app-browser-context source="ambient-ui-state">Page metadata</in-app-browser-context>\n\n## My request:\nДобавь экспорт',None)])
        report,_ = self.cm.import_sessions(self.home)
        self.assertEqual(report['requests'],1)
        user = next(r for r in self.t.memory.load_all() if r.get('role')=='user')
        self.assertEqual(user['body'],'Добавь экспорт')

    def test_manual_edits_retirement_and_missing_logs_are_preserved(self):
        path, _ = self.log()
        self.cm.import_sessions(self.home)
        r = next(r for r in self.t.memory.load_all() if r.get("role") == "user")
        r["body"] = "Ручное уточнение"
        self.t.memory.save(r)
        answer = next(r for r in self.t.memory.load_all() if r.get("role") == "assistant")
        answer["status"] = "устарела"
        self.t.memory.save(answer)
        self.cm.import_sessions(self.home)
        self.assertEqual(self.t.memory.get(r["id"])["body"], "Ручное уточнение")
        self.assertEqual(self.t.memory.get(answer["id"])["status"], "устарела")
        path.unlink()
        out, _ = self.cm.import_sessions(self.home)
        self.assertEqual(out["requests"], 1)
        self.assertEqual(len(self.memory_files()), 3)

    def test_manual_relationship_and_title_changes_are_preserved(self):
        self.log()
        self.cm.import_sessions(self.home)
        r = next(r for r in self.t.memory.load_all() if r.get("role") == "user")
        r["parent"] = "manual-parent"
        r["title"] = "Уточнённый вручную запрос"
        self.t.memory.save(r)
        self.cm.import_sessions(self.home)
        after = self.t.memory.get(r["id"])
        self.assertEqual(after["title"], r["title"])
        self.assertEqual(after["parent"], "manual-parent")

    def test_fork_history_does_not_reassign_owner_or_duplicate_parent_requests(self):
        _, parent = self.log()
        child, rows = self.log("child", parent="s1")
        inherited = json.loads(json.dumps(parent))
        for row in inherited:
            row["timestamp"] = "2026-10-03T10:00:00Z"
        child.write_text("\n".join(json.dumps(r) for r in rows[:1] + inherited + rows[1:]), encoding="utf-8")
        out, _ = self.cm.import_sessions(self.home)
        self.assertEqual(out["requests"], 2)
        records = self.t.memory.load_all()
        root = next(r for r in records if r.get("session") == "child" and r["kind"] == "источник")
        self.assertEqual(self.t.memory.get(root["parent"])["session"], "s1")

    def test_subagent_in_another_project_does_not_reference_a_nonexistent_parent_root(self):
        self.log()
        self.log('child',cwd=self.p['beta-tool']['path'],parent='s1')
        self.cm.import_sessions(self.home)
        records=self.t.memory.load_all()
        child=next(r for r in records if r.get('session')=='child' and r['kind']=='источник')
        self.assertEqual(child['parent'],'')
        known={r['id'] for r in records}
        self.assertTrue(all(not r.get('parent') or r['parent'] in known for r in records))

    def test_dry_run_and_incomplete_tail(self):
        path, _ = self.log()
        with path.open("a") as f:
            f.write('\n[]\nnull\n{"type":')
        out, _ = self.cm.import_sessions(self.home, dry_run=True)
        self.assertEqual(out["scan"]["malformed_rows"], 1)
        self.assertEqual(self.memory_files(), [])
        self.assertFalse(Path(self.t.memory.MEM).exists())
        self.assertFalse((Path(self.data) / "codex_sessions.json").exists())

    def test_secret_redaction_and_continuation_create_a_new_request_branch(self):
        token = "sk-" + "A" * 48
        self.log(messages=[("user", "Добавь ключ " + token, None),
            ("assistant", "Ключ " + token, "final_answer"),
            ("user", "Теперь добавь экспорт", None),
            ("assistant", "Экспорт добавлен", "final_answer")])
        out, _ = self.cm.import_sessions(self.home)
        self.assertEqual((out["requests"], out["answers"]), (2, 2))
        records = self.t.memory.load_all()
        self.assertNotIn(token, " ".join(r["title"] + r["body"] for r in records))
        parents = {r["parent"] for r in records if r.get("role") == "assistant"}
        self.assertEqual(parents, {r["id"] for r in records if r.get("role") == "user"})

    def test_codex_managed_worktree_uses_exact_registered_directory(self):
        from collect_usage import project_key, usage_overrides
        self.assertEqual(project_key("C:/Users/test/.codex/worktrees/abc/alpha-api/src", usage_overrides()), "alpha-api")
        self.assertEqual(project_key("C:/Users/test/.codex/worktrees/abc/alpha-api_site", usage_overrides()), "alpha-api_site")

    def test_usage_only_unknown_project_also_has_a_branch(self):
        self.jwrite("usage.json", {"projects": {"usage-only": {"billable": 123}}})
        tree, _ = self.t.tree.build(False)
        branch = next(n for n in tree["root"]["children"] if n["project"] == "usage-only")
        self.assertEqual(branch["spent_tokens"], 123)

    def test_rerouting_preserves_message_ids_and_retires_empty_old_session_root(self):
        doc=self.jread('projects.json');doc['projects']['alpha-api']['usage_paths']=['C:/Work/Qatar']
        self.jwrite('projects.json',doc);self.log(cwd='C:/Work/Qatar')
        self.cm.import_sessions(self.home)
        before={r['id'] for r in self.t.memory.load_all() if r.get('role')}
        del doc['projects']['alpha-api']['usage_paths'];doc['projects']['beta-tool']['usage_paths']=['C:/Work/Qatar']
        self.jwrite('projects.json',doc)
        report,_=self.cm.import_sessions(self.home)
        active=self.t.memory.load_all(False)
        self.assertEqual({r['id'] for r in active if r.get('role')},before)
        self.assertTrue(all(r['project']=='beta-tool' for r in active))
        self.assertEqual(report['sessions'][0]['projects'],['beta-tool'])
        self.cm.import_sessions(self.home)
        self.assertEqual(len(self.t.memory.load_all(False)),3)
