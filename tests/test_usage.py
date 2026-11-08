"""Учёт провайдеров: реальные форматы событий, повторы и сохранность истории."""
import importlib
import json
import os
from unittest import mock

from base import DataCase


class Usage(DataCase):
    def setUp(self):
        super().setUp()
        import collect_usage
        self.c = importlib.reload(collect_usage)
        self.projects()
        self.codex = os.path.join(self.tmp, "codex")
        self.claude = os.path.join(self.tmp, "claude")
        os.makedirs(self.claude)

    def log(self, path, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def codex_rows(self):
        def event(i, inp, cache, out, reason):
            return {"type": "event_msg", "timestamp": "2026-10-01T10:%02d:00Z" % i,
                    "payload": {"type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": inp, "cached_input_tokens": cache,
                        "output_tokens": out, "reasoning_output_tokens": reason},
                        "last_token_usage": {"input_tokens": 999999}}}}
        return [{"type": "session_meta", "payload": {"id": "s1", "cwd": "C:/projects/alpha-api"}},
                {"type": "turn_context", "payload": {"model": "test-gpt"}},
                event(0, 100, 60, 20, 10), event(1, 100, 60, 20, 10),
                event(2, 160, 90, 30, 15)]

    def collect(self, source="all", previous=None):
        return self.c.collect(source, previous, self.claude, self.codex)

    def test_codex_cumulative_cache_reasoning_and_archive_are_not_double_counted(self):
        rows = self.codex_rows()
        self.log(os.path.join(self.codex, "sessions", "s1.jsonl"), rows)
        self.log(os.path.join(self.codex, "archived_sessions", "s1.jsonl"), rows)
        out = self.collect()
        p = out["projects"]["alpha-api"]
        self.assertEqual((p["input"], p["cache_read"], p["output"], p["thinking"]), (70, 90, 30, 15))
        self.assertEqual(p["billable"], 190)
        self.assertEqual((p["requests"], p["sessions"]), (2, 1))
        self.assertEqual(p["active_seconds"], 120)
        self.assertFalse(p["cost_complete"])
        self.assertEqual(p["unpriced_tokens"], 190)
        self.assertEqual(p["unknown_models"], ["test-gpt"])
        self.assertEqual(out["total"]["tokens"], 190)

    def test_claude_duplicates_keep_existing_costs(self):
        row = {"type": "assistant", "cwd": "C:/projects/alpha-api", "requestId": "request1",
               "sessionId": "claude1", "timestamp": "2026-10-01T10:00:00Z",
               "message": {"model": "claude-opus-5", "usage": {"input_tokens": 1000,
               "output_tokens": 1000, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 1000}}}
        self.log(os.path.join(self.claude, "alpha", "s.jsonl"), [row, row])
        p = self.collect()["projects"]["alpha-api"]
        self.assertEqual(p["billable"], 4000)
        self.assertEqual(p["requests"], 1)
        self.assertTrue(p["cost_complete"])
        self.assertGreater(p["usd"], 0)

    def test_selective_update_preserves_other_provider_and_migrates_legacy(self):
        previous = {"projects": {"alpha-api": {"billable": 100, "input": 100, "usd": 2,
                    "sessions": 1, "output": 0}}, "generated_at": "2026-01-01T00:00:00Z"}
        out = self.collect("codex", previous)
        self.assertEqual(out["providers"]["claude"]["projects"], previous["projects"])
        self.assertEqual(out["projects"]["alpha-api"]["billable"], 100)

    def test_missing_source_preserves_last_good_collection(self):
        self.log(os.path.join(self.codex, "sessions", "s1.jsonl"), self.codex_rows())
        previous = self.collect()
        out = self.c.collect("codex", previous, self.claude, os.path.join(self.tmp, "missing"))
        self.assertEqual(out["total"]["tokens"], previous["total"]["tokens"])
        self.assertTrue(out["providers"]["codex"]["stale"])
        self.assertFalse(out["providers"]["codex"]["available"])

    def test_selective_migration_combines_old_directory_key_and_new_project_key(self):
        self.projects(lambda d: d["projects"]["alpha-api"].update(path="C:/Work/OldFolder"))
        previous = {"projects": {"oldfolder": {"input": 100, "billable": 100,
                    "usd": 2, "sessions": 1, "output": 0}}}
        rows = self.codex_rows()
        rows[0]["payload"]["cwd"] = "C:/Work/OldFolder"
        self.log(os.path.join(self.codex, "sessions", "s1.jsonl"), rows)
        out = self.collect("codex", previous)
        self.assertEqual(set(out["projects"]), {"alpha-api"})
        self.assertEqual(out["projects"]["alpha-api"]["billable"], 290)

    def test_empty_machine_and_malformed_rows(self):
        self.assertEqual(self.collect()["total"]["tokens"], 0)
        self.log(os.path.join(self.codex, "sessions", "s.jsonl"),
                 [[], None, {"type": "event_msg", "payload": {"type": "token_count", "info": None}}])
        self.assertEqual(self.collect()["total"]["tokens"], 0)

    def test_incomplete_cost_does_not_become_complete_estimate(self):
        import estimate
        e = importlib.reload(estimate)
        self.projects(lambda d: [p.update(path="C:/projects/" + k) for k, p in d["projects"].items()])
        self.jwrite("usage.json", {"projects": {"alpha-api": {
            "billable": 1000, "usd": 0, "output": 100, "cost_complete": False}}})
        self.jwrite("repos.json", {"repos": {"alpha-api": {"git": {"commits": 10}}}})
        self.quiet(e.main)
        result = self.jread("estimates.json")
        self.assertFalse(result["projects"]["alpha-api"]["cost_complete"])
        self.assertFalse(result["totals"]["cost_complete"])
        self.assertGreater(result["calibration"]["portfolio_median"]["usd_per_commit"], 0)
