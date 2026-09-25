# -*- coding: utf-8 -*-
"""Обновление одного проекта и свежесть слоёв (ОБН-04, ОБН-05)."""
import importlib
import unittest
from unittest import mock

from base import DataCase


class RefreshOne(DataCase):
    def setUp(self):
        super().setUp()
        self.projects()
        import pm
        self.pm = importlib.reload(pm)            # пути к данным считаются при импорте

    def refresh(self, *args):
        calls = []
        with mock.patch.object(self.pm, "run", lambda *a: calls.append(a) or 0), \
                mock.patch.object(self.pm, "cmd_snapshot", lambda a: 0):
            rc, out = self.quiet(self.pm.cmd_refresh, list(args))
        return rc, calls

    def test_project_refresh_scans_only_its_repo(self):                 # ОБН-04
        rc, calls = self.refresh("--project", "alpha-api")
        self.assertIn(rc, (0, None))
        self.assertEqual([c for c in calls if c[0] == "collect_repos.py"], [("collect_repos.py", "alpha-api")])
        self.assertIn(("build_dashboard.py",), calls)

    def test_project_refresh_skips_vault_even_when_auto(self):
        self.jwrite("obsidian.json", {"auto": {"pull_on_refresh": True, "push_on_refresh": True}})
        _, calls = self.refresh("--project", "alpha-api")
        self.assertFalse([c for c in calls if c[0] == "obsidian.py"])

    def test_unknown_project_is_refused(self):
        rc, calls = self.refresh("--project", "nope")
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [])

    def test_fast_refresh_scans_nothing(self):
        _, calls = self.refresh("--fast")
        self.assertFalse([c for c in calls if c[0] == "collect_repos.py"])


class Freshness(DataCase):
    def test_layers_and_oldest_repo(self):                              # ОБН-05
        import build_dashboard
        bd = importlib.reload(build_dashboard)
        self.jwrite("usage.json", {"generated_at": "2026-09-25T08:00:00+00:00", "projects": {}})
        self.jwrite("repos.json", {"generated_at": "2026-09-25T09:00:00+00:00", "repos": {
            "alpha-api": {"scanned_at": "2026-09-25T09:00:00+00:00"},
            "beta-tool": {"scanned_at": "2026-09-20T09:00:00+00:00"}}})
        self.jwrite("vault_state.json", {"pulled_at": "2026-09-24T10:00:00+00:00"})
        f = bd.freshness()
        self.assertEqual(f["usage"], "2026-09-25T08:00:00+00:00")
        # Обновление одного проекта не делает свежим весь слой: берётся самый старый срез.
        self.assertEqual(f["repos"], "2026-09-20T09:00:00+00:00")
        self.assertEqual(f["vault_pull"], "2026-09-24T10:00:00+00:00")
        self.assertEqual(f["vault_push"], "")

    def test_repo_scan_is_stamped(self):
        import collect_repos
        P = self.projects()["projects"]
        res = collect_repos.scan_repo("beta-tool", P["beta-tool"])
        self.assertTrue(res["scanned_at"].startswith("20"))


if __name__ == "__main__":
    unittest.main()
