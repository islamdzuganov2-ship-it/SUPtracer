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
        self.codex_config = os.path.join(self.tmp, "codex", "config.toml")
        self._codex_env = os.environ.get("TASKTREKER_CODEX_CONFIG")
        os.environ["TASKTREKER_CLAUDE_SETTINGS"] = self.settings
        os.environ["TASKTREKER_CODEX_CONFIG"] = self.codex_config
        import connect
        self.c = importlib.reload(connect)
        self.calls = []

    def tearDown(self):
        if self._env is None:
            os.environ.pop("TASKTREKER_CLAUDE_SETTINGS", None)
        else:
            os.environ["TASKTREKER_CLAUDE_SETTINGS"] = self._env
        if self._codex_env is None:
            os.environ.pop("TASKTREKER_CODEX_CONFIG", None)
        else:
            os.environ["TASKTREKER_CODEX_CONFIG"] = self._codex_env
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

    def write_codex(self, text):
        os.makedirs(os.path.dirname(self.codex_config), exist_ok=True)
        with open(self.codex_config, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read_codex_text(self):
        with open(self.codex_config, encoding="utf-8", newline="") as f:
            return f.read()

    def test_codex_install_uses_absolute_python_and_server(self):
        ok, _ = self.c.install_codex_mcp()
        self.assertTrue(ok)
        data = self.c._parse_codex(self.read_codex_text())
        entry = data["mcp_servers"]["tasktreker"]
        self.assertEqual(entry["command"], os.path.abspath(sys.executable))
        self.assertEqual(entry["args"], [self.c.SERVER])
        self.assertTrue(self.c.codex_mcp_installed())
        self.assertFalse(os.path.exists(self.settings))

    def test_codex_config_honors_codex_home(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": os.path.join(self.tmp, "other home")}, clear=False):
            os.environ.pop("TASKTREKER_CODEX_CONFIG")
            self.assertEqual(self.c.codex_config_path(), os.path.join(self.tmp, "other home", "config.toml"))
            os.environ["TASKTREKER_CODEX_CONFIG"] = self.codex_config

    def test_codex_preserves_foreign_settings_and_backup(self):
        original = ('# preserve comment and CRLF\r\nmodel = "example"\r\n'
                    '[mcp_servers.other]\r\nurl = "https://example.invalid/mcp"\r\n'
                    '[projects."C:/My project"]\r\ntrust_level = "trusted"\r\n')
        self.write_codex(original)
        self.assertTrue(self.c.install_codex_mcp()[0])
        self.assertTrue(self.read_codex_text().startswith(original))
        with open(self.codex_config + ".tasktreker-backup", encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), original)
        data = self.c._parse_codex(self.read_codex_text())
        self.assertEqual(data["mcp_servers"]["other"]["url"], "https://example.invalid/mcp")
        self.assertEqual(data["projects"]["C:/My project"]["trust_level"], "trusted")

    def test_codex_repeat_install_and_absent_remove_are_noops(self):
        self.assertTrue(self.c.remove_codex_mcp()[0])
        self.assertFalse(os.path.exists(self.codex_config))
        self.assertTrue(self.c.install_codex_mcp()[0])
        original = self.read_codex_text()
        before = os.stat(self.codex_config).st_mtime_ns
        with mock.patch.object(self.c, "_write_codex") as write:
            self.assertTrue(self.c.install_codex_mcp()[0])
            write.assert_not_called()
        self.assertEqual(self.read_codex_text(), original)
        self.assertEqual(os.stat(self.codex_config).st_mtime_ns, before)

    def test_codex_conflict_never_overwrites_or_removes_foreign_server(self):
        original = '[mcp_servers.tasktreker]\ncommand = "another-server"\nargs = []\n'
        self.write_codex(original)
        self.assertFalse(self.c.install_codex_mcp()[0])
        self.assertFalse(self.c.remove_codex_mcp()[0])
        self.assertEqual(self.read_codex_text(), original)
        self.assertFalse(os.path.exists(self.codex_config + ".tasktreker-backup"))

    def test_codex_rejects_other_checkout(self):
        original = '[mcp_servers.tasktreker]\ncommand = "python"\nargs = ["C:/other/tools/mcp_server.py"]\n'
        self.write_codex(original)
        self.assertFalse(self.c.install_codex_mcp()[0])
        self.assertFalse(self.c.codex_mcp_installed())
        self.assertEqual(self.read_codex_text(), original)

    def test_codex_broken_toml_is_never_overwritten(self):
        original = '[mcp_servers.tasktreker\n'
        self.write_codex(original)
        self.assertFalse(self.c.install_codex_mcp()[0])
        self.assertFalse(self.c.remove_codex_mcp()[0])
        self.assertEqual(self.read_codex_text(), original)

    def test_codex_update_keeps_custom_options_and_nested_tables(self):
        other = '[mcp_servers.other]\ncommand = "other"\n'
        original = ('[mcp_servers."tasktreker"]\ncommand = "old-python"\nargs = %s\n'
                    'enabled = false\ntool_timeout_sec = 100\n'
                    '[mcp_servers.tasktreker.env]\nEXAMPLE = "keep"\n' % json.dumps([self.c.SERVER]))
        self.write_codex(original + other)
        self.assertFalse(self.c.codex_mcp_installed())
        self.assertTrue(self.c.install_codex_mcp()[0])
        data = self.c._parse_codex(self.read_codex_text())
        entry = data["mcp_servers"]["tasktreker"]
        self.assertEqual(entry["command"], os.path.abspath(sys.executable))
        self.assertEqual(entry["env"], {"EXAMPLE": "keep"})
        self.assertEqual(entry["tool_timeout_sec"], 100)
        self.assertTrue(entry["enabled"])
        self.assertTrue(self.read_codex_text().endswith(other))

    def test_codex_remove_keeps_other_servers_and_settings(self):
        original = '# personal config\nmodel = "example"\n[mcp_servers.other]\ncommand = "other"\n'
        self.write_codex(original)
        self.c.install_codex_mcp()
        self.assertTrue(self.c.remove_codex_mcp()[0])
        self.assertEqual(self.read_codex_text().rstrip(), original.rstrip())
        self.assertFalse(self.c.codex_mcp_installed())

    def test_codex_multiline_lookalike_cannot_damage_other_settings(self):
        original = 'note = """\n[mcp_servers.tasktreker]\ncommand = "example"\n"""\n'
        self.write_codex(original)
        ok, _ = self.c.install_codex_mcp()
        # A conservative refusal is preferable to rewriting the user's string.
        if ok:
            data = self.c._parse_codex(self.read_codex_text())
            self.assertEqual(data["note"], self.c._parse_codex(original)["note"])
        else:
            self.assertEqual(self.read_codex_text(), original)

    def test_codex_inline_foreign_server_config_is_untouched(self):
        original = 'mcp_servers = { other = { command = "example" } }\n'
        self.write_codex(original)
        self.assertFalse(self.c.install_codex_mcp()[0])
        self.assertEqual(self.read_codex_text(), original)

    def test_codex_concurrent_change_is_not_lost(self):
        self.write_codex('model = "new-value"\n')
        with self.assertRaises(self.c.SettingsBroken):
            self.c._write_codex(self.codex_config, 'model = "old-value"\n', 'model = "replacement"\n')
        self.assertEqual(self.read_codex_text(), 'model = "new-value"\n')

    def test_codex_provider_never_calls_claude(self):
        with mock.patch.object(self.c, "install_hook") as hook, \
             mock.patch.object(self.c, "install_mcp") as mcp, \
             mock.patch("builtins.print"):
            self.assertEqual(self.c.cmd_connect([], provider="codex"), 0)
            self.assertEqual(self.c.cmd_status("codex"), 0)
            self.assertEqual(self.c.cmd_disconnect(["--provider", "codex"]), 0)
            hook.assert_not_called()
            mcp.assert_not_called()

    def test_all_provider_reports_partial_failure(self):
        with mock.patch.object(self.c, "install_hook", return_value="установлен"), \
             mock.patch.object(self.c, "install_mcp", return_value=(False, "нет CLI")), \
             mock.patch("builtins.print"):
            self.assertEqual(self.c.cmd_connect(["--provider=all"]), 1)
        self.assertTrue(self.c.codex_mcp_installed())

    def test_invalid_provider_and_codex_worktree_never_change_settings(self):
        with mock.patch("builtins.print"):
            self.assertEqual(self.c.cmd_connect(["--provider"]), 1)
            self.assertEqual(self.c.cmd_connect([], provider="other"), 1)
            with mock.patch.object(self.c, "TOOLS", "C:/Users/me/.codex/worktrees/123/tools"):
                self.assertEqual(self.c.cmd_connect([], provider="codex"), 1)
        self.assertFalse(os.path.exists(self.codex_config))
        self.assertFalse(os.path.exists(self.settings))


if __name__ == "__main__":
    unittest.main()
