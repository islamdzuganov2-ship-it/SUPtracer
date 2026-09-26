# -*- coding: utf-8 -*-
"""MCP-сервер TaskTreker (ИНТ-04, ИНТ-05, БЕЗ-06, ПАМ-08).

Сервер запускается настоящим процессом, как его запускает клиент, и говорит
по stdio построчным JSON-RPC. Проверяются обе эпохи протокола: наследная
(рукопожатие initialize) и современная (версия в _meta каждого запроса)."""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import unittest

from base import DataCase, TOOLS

SERVER = os.path.join(TOOLS, "mcp_server.py")
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientCapabilities": {},
          "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"}}


class McpCase(DataCase):
    def setUp(self):
        super().setUp()
        self.P = self.projects()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", TASKTREKER_DATA=self.data)
        self.proc = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.lines = []                                   # весь stdout — для проверки чистоты
        self.timer = threading.Timer(60, self.proc.kill)  # страховка от зависания
        self.timer.start()
        self.next_id = 0

    def tearDown(self):
        self.timer.cancel()
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        for s in (self.proc.stdout, self.proc.stderr):
            s.close()
        super().tearDown()

    def send(self, obj, expect_reply=True):
        raw = obj if isinstance(obj, (bytes, str)) else json.dumps(obj, ensure_ascii=False)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.proc.stdin.write(raw + b"\n")
        self.proc.stdin.flush()
        if not expect_reply:
            return None
        line = self.proc.stdout.readline()
        if not line:
            # stderr читается только здесь: у живого сервера он открыт и пуст,
            # и чтение «на всякий случай» висело бы до сторожевого таймера.
            self.fail("сервер не ответил; stderr: %s" % self.proc.stderr.read(4000))
        text = line.decode("utf-8")
        self.lines.append(text)
        return json.loads(text)

    def request(self, method, params=None, modern=False):
        self.next_id += 1
        params = dict(params or {})
        if modern:
            params["_meta"] = dict(MODERN, **params.get("_meta", {}))
        return self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})

    def call(self, name, args=None, modern=False):
        r = self.request("tools/call", {"name": name, "arguments": args or {}}, modern)
        self.assertIn("result", r, r)
        return r["result"]

    def text(self, result):
        return result["content"][0]["text"]

    def handshake(self, version="2025-06-18"):
        r = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                        "clientInfo": {"name": "tests", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
        return r


class Legacy(McpCase):
    def test_initialize_echoes_supported_version(self):                 # ИНТ-04
        r = self.handshake("2025-06-18")["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(r["serverInfo"]["name"], "tasktreker")
        self.assertIn("tools", r["capabilities"])
        self.assertNotIn("resultType", r)

    def test_unknown_version_gets_latest_legacy(self):
        r = self.handshake("1999-01-01")["result"]
        self.assertEqual(r["protocolVersion"], "2025-11-25")

    def test_instructions_carry_the_protocol(self):                     # ИНТ-05
        text = self.handshake()["result"]["instructions"]
        self.assertIn("Не копируй данные системы в свою память", text)
        self.assertIn("tt_remember", text)
        self.assertIn("tt_recall", text)

    def test_tools_list(self):
        self.handshake()
        tools = self.request("tools/list")["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), 12)
        for t in tools:
            self.assertRegex(t["name"], r"^[A-Za-z0-9_.-]{1,128}$")
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
            self.assertIn("readOnlyHint", t["annotations"])
        ro = {t["name"] for t in tools if t["annotations"]["readOnlyHint"]}
        self.assertEqual(ro, {"tt_context", "tt_recall", "tt_search", "tt_status", "tt_inbox",
                              "tt_tree", "tt_projects"})

    def test_remember_then_recall_and_search(self):                     # ПАМ-08
        self.handshake()
        r = self.call("tt_remember", {"project": "alpha-api", "title": "Кеш в Redis",
                                      "text": "Решили держать кеш ответов в Redis.", "kind": "решение"})
        self.assertFalse(r["isError"], r)
        rid = re.search(r"(mem-[\w-]+)", self.text(r)).group(1)
        self.assertIn("Решили держать кеш", self.text(self.call("tt_recall", {"query": rid})))
        self.assertIn(rid, self.text(self.call("tt_search", {"query": "кеш redis"})))
        self.assertIn("Кеш в Redis", self.text(self.call("tt_context", {"project": "alpha-api"})))

    def test_forget_with_successor(self):
        self.handshake()
        a = re.search(r"(mem-[\w-]+)", self.text(self.call("tt_remember", {
            "project": "alpha-api", "title": "Старое", "text": "было так"}))).group(1)
        b = re.search(r"(mem-[\w-]+)", self.text(self.call("tt_remember", {
            "project": "alpha-api", "title": "Новое", "text": "стало так"}))).group(1)
        r = self.call("tt_forget", {"id": a, "by": b, "why": "передумали"})
        self.assertFalse(r["isError"], r)
        self.assertIn("заменена", self.text(r))

    def test_context_by_cwd(self):
        self.handshake()
        out = self.text(self.call("tt_context", {"cwd": self.P["projects"]["beta-tool"]["path"] + "/src"}))
        self.assertIn("проект beta-tool", out)

    def test_status_tree_projects(self):
        self.handshake()
        self.assertIn("Сервис Альфа", self.text(self.call("tt_status", {"project": "alpha-api"})))
        self.assertIn("alpha-api — Сервис Альфа", self.text(self.call("tt_projects")))
        self.assertIn("Сервис Альфа", self.text(self.call("tt_tree", {"project": "alpha-api"})))
        self.assertFalse(os.path.exists(os.path.join(self.data, "tree.json")))   # чтение не пишет

    def test_validation_errors_are_tool_errors(self):
        self.handshake()
        for args, needle in (({}, "Не хватает полей: project, title, text"),
                             ({"project": "alpha-api", "title": "x", "text": "y", "kind": "мечта"},
                              "допустимо"),
                             ({"project": "alpha-api", "title": "x", "text": "y", "лишнее": 1},
                              "Неизвестные поля"),
                             ({"project": "нет-такого", "title": "x", "text": "y"}, "Нет проекта")):
            with self.subTest(args=args):
                r = self.call("tt_remember", args)
                self.assertTrue(r["isError"])
                self.assertIn(needle, self.text(r))

    def test_protocol_errors(self):
        self.handshake()
        r = self.request("tools/call", {"name": "tt_nope", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(self.request("resources/list")["error"]["code"], -32601)
        self.assertEqual(self.send(b"{not json")["error"]["code"], -32700)
        self.assertEqual(self.send({"jsonrpc": "2.0", "id": 99})["error"]["code"], -32600)

    def test_secrets_are_redacted_on_remember(self):
        self.handshake()
        key = "sk-" + "ant-" + "api03-" + "Q" * 30
        r = self.call("tt_remember", {"project": "alpha-api", "title": "Ключ", "text": "ключ " + key})
        self.assertIn("Вычищено секретов: 1", self.text(r))
        rid = re.search(r"(mem-[\w-]+)", self.text(r)).group(1)
        self.assertNotIn(key, self.text(self.call("tt_recall", {"query": rid})))

    def test_stdout_is_protocol_only(self):
        self.handshake()
        self.call("tt_status")
        self.call("tt_inbox")
        self.call("tt_ingest", {"path": self.note("x.md", "# Заметка\nНужно сделать кеш.\n"), "dry_run": True})
        for line in self.lines:
            obj = json.loads(line)
            self.assertEqual(obj["jsonrpc"], "2.0")


class Modern(McpCase):
    def test_discover(self):                                            # ИНТ-04, ИНТ-05
        r = self.request("server/discover", modern=True)["result"]
        self.assertEqual(r["resultType"], "complete")
        self.assertIn("2026-07-28", r["supportedVersions"])
        self.assertIn("tools", r["capabilities"])
        self.assertEqual(r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "tasktreker")
        self.assertIn("Не копируй", r["instructions"])

    def test_calls_without_handshake(self):
        tools = self.request("tools/list", modern=True)["result"]
        self.assertEqual(tools["resultType"], "complete")
        r = self.call("tt_projects", modern=True)
        self.assertEqual(r["resultType"], "complete")
        self.assertIn("beta-tool", self.text(r))

    def test_cacheable_results_carry_hints(self):                       # ИНТ-06
        # Ревизия 2026-07-28: без ttlMs и cacheScope клиент отвергает tools/list целиком —
        # сервер подключён, а инструментов в сессии нет. Так и было на живом клиенте.
        for method in ("server/discover", "tools/list"):
            with self.subTest(method=method):
                r = self.request(method, modern=True)["result"]
                self.assertIsInstance(r["ttlMs"], int)
                self.assertGreaterEqual(r["ttlMs"], 0)
                self.assertIn(r["cacheScope"], ("public", "private"))
        call = self.call("tt_projects", modern=True)
        self.assertNotIn("ttlMs", call)                                 # вызов инструмента не кешируется

    def test_legacy_results_have_no_modern_fields(self):
        self.handshake()
        r = self.request("tools/list")["result"]
        for field in ("resultType", "ttlMs", "cacheScope"):
            self.assertNotIn(field, r)

    def test_unsupported_version(self):
        r = self.request("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}},
                         modern=True)
        self.assertEqual(r["error"]["code"], -32022)
        self.assertIn("2026-07-28", r["error"]["data"]["supported"])
        self.assertEqual(r["error"]["data"]["requested"], "1900-01-01")

    def test_missing_client_capabilities(self):
        r = self.request("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})
        self.assertEqual(r["error"]["code"], -32602)


class Safety(McpCase):
    """БЕЗ-06: бэклог меняет только tt_apply."""

    def digest(self):
        with open(os.path.join(self.data, "projects.json"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_only_apply_touches_backlog(self):
        self.handshake()
        before = self.digest()
        note = self.note("n.md", "---\nproject: beta-tool\n---\n# План\n\n## Задачи\n- [ ] Добавить выгрузку в PDF\n")
        self.call("tt_remember", {"project": "beta-tool", "title": "Факт", "text": "Отчёт за 3 с", "kind": "факт"})
        self.call("tt_ingest", {"path": note})
        self.call("tt_reject", {"ids": ["IN-9"], "why": "нет такого"})
        self.call("tt_forget", {"id": "mem-нет", "why": "x"})
        self.assertEqual(self.digest(), before)
        r = self.call("tt_apply", {"ids": ["IN-1"]})
        self.assertFalse(r["isError"], r)
        self.assertNotEqual(self.digest(), before)
        backlog = self.jread("projects.json")["projects"]["beta-tool"]["backlog"]
        self.assertEqual(backlog[-1]["title"], "Добавить выгрузку в PDF")


if __name__ == "__main__":
    unittest.main()
