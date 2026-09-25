# -*- coding: utf-8 -*-
"""Подключение Клода на уровне пользователя: хук и MCP-сервер (ИНТ-06).

Настоящие ~/.claude не трогаются: путь к settings.json подменяется переменной
окружения, CLI Claude Code имитируется."""
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from base import TOOLS  # noqa: F401

OTHER_HOOK = {"hooks": [{"type": "command", "command": "echo чужой хук"}]}


class Connect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tt-connect-")
        self.settings = os.path.join(self.tmp, "settings.json")
        self._env = os.environ.get("TASKTREKER_CLAUDE_SETTINGS")
        os.environ["TASKTREKER_CLAUDE_SETTINGS"] = self.settings
        import connect
        self.c = importlib.reload(connect)
        self.calls = []

    def tearDown(self):
        if self._env is None:
            os.environ.pop("TASKTREKER_CLAUDE_SETTINGS", None)
        else:
            os.environ["TASKTREKER_CLAUDE_SETTINGS"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, obj):
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def read(self):
        with open(self.settings, encoding="utf-8") as f:
            return json.load(f)

    def fake_cli(self, args, cli=None):
        self.calls.append(args)
        return 0, "tasktreker: py -3 %s" % self.c.SERVER

    def test_hook_merges_and_keeps_foreign_settings(self):
        self.write({"autoMode": {"allow": ["$defaults"]},
                    "hooks": {"SessionStart": [OTHER_HOOK], "PreToolUse": [OTHER_HOOK]}})
        self.assertEqual(self.c.install_hook(), "установлен")
        d = self.read()
        self.assertEqual(d["autoMode"], {"allow": ["$defaults"]})
        self.assertEqual(d["hooks"]["PreToolUse"], [OTHER_HOOK])
        self.assertEqual(d["hooks"]["SessionStart"][0], OTHER_HOOK)
        ours = d["hooks"]["SessionStart"][1]["hooks"][0]
        self.assertEqual((ours["type"], ours["command"]), ("command", "py"))
        self.assertEqual(ours["args"], ["-3", self.c.HOOK])
        self.assertTrue(os.path.exists(self.settings + ".tasktreker-backup"))

    def test_reinstall_does_not_duplicate(self):
        self.write({})
        self.c.install_hook()
        self.assertEqual(self.c.install_hook(), "обновлён")
        self.assertEqual(len(self.read()["hooks"]["SessionStart"]), 1)

    def test_missing_settings_file_is_created(self):
        self.assertFalse(os.path.exists(self.settings))
        self.c.install_hook()
        self.assertTrue(self.c.hook_installed())

    def test_remove_takes_only_ours(self):
        self.write({"hooks": {"SessionStart": [OTHER_HOOK]}})
        self.c.install_hook()
        self.assertTrue(self.c.remove_hook())
        self.assertEqual(self.read()["hooks"]["SessionStart"], [OTHER_HOOK])
        self.assertFalse(self.c.remove_hook())

    def test_remove_cleans_empty_sections(self):
        self.write({"autoMode": {}})
        self.c.install_hook()
        self.c.remove_hook()
        self.assertEqual(self.read(), {"autoMode": {}})

    def test_broken_settings_are_not_overwritten(self):
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write("{ это не json")
        with self.assertRaises(self.c.SettingsBroken):
            self.c.install_hook()
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ это не json")

    def test_mcp_registration_uses_user_scope(self):
        with mock.patch.object(self.c, "_cli", self.fake_cli), \
             mock.patch.object(self.c, "find_cli", lambda: "claude"):
            ok, _ = self.c.install_mcp()
        self.assertTrue(ok)
        self.assertEqual(self.calls[0], ["mcp", "remove", "--scope", "user", "tasktreker"])
        self.assertEqual(self.calls[1], ["mcp", "add", "--scope", "user", "tasktreker", "--",
                                         "py", "-3", self.c.SERVER])

    def test_no_cli_explains_manual_step(self):
        with mock.patch.object(self.c, "find_cli", lambda: None):
            ok, msg = self.c.install_mcp()
        self.assertFalse(ok)
        self.assertIn("claude mcp add --scope user tasktreker", msg)

    def test_worktree_copy_refuses_without_force(self):
        with mock.patch.object(self.c, "TOOLS", "C:/repo/.claude/worktrees/w1/tools"), \
             mock.patch.object(self.c, "_cli", self.fake_cli):
            buf = []
            with mock.patch("builtins.print", lambda *a, **k: buf.append(" ".join(map(str, a)))):
                code = self.c.cmd_connect([])
        self.assertEqual(code, 1)
        self.assertIn("временное рабочее дерево", " ".join(buf))
        self.assertFalse(os.path.exists(self.settings))


if __name__ == "__main__":
    unittest.main()
