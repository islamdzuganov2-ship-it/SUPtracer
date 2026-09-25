# -*- coding: utf-8 -*-
"""Локальный сервер кнопки «Обновить данные» (ОБН-01, ОБН-02, БЕЗ-01, БЕЗ-02)."""
import http.client
import inspect
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from base import TOOLS  # noqa: F401
import serve


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.page = tempfile.mkstemp(suffix=".html")
        os.write(fd, "<html>дашборд</html>".encode("utf-8"))
        os.close(fd)
        cls.calls = []

        def fake_run(args, timeout=900):
            cls.calls.append(args)
            return True, "собрано", 0.1

        cls.patches = [mock.patch.object(serve, "DASH", cls.page),
                       mock.patch.object(serve, "run_pm", fake_run)]
        for p in cls.patches:
            p.start()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for p in cls.patches:
            p.stop()
        os.remove(cls.page)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        c.close()
        return r.status, data

    def test_serves_only_the_dashboard(self):                           # ОБН-02
        self.assertEqual(self.req("GET", "/"), (200, "<html>дашборд</html>"))
        self.assertEqual(self.req("GET", "/data/projects.json")[0], 404)
        self.assertEqual(self.req("GET", "/../README.md")[0], 404)

    def test_ping(self):
        code, body = self.req("GET", "/api/ping")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["served"])

    def test_refresh_needs_own_header(self):                            # БЕЗ-02
        code, _ = self.req("POST", "/api/refresh", json.dumps({"mode": "fast"}),
                           {"Content-Type": "application/json"})
        self.assertEqual(code, 403)

    def test_refresh_runs_requested_mode(self):                         # ОБН-01
        code, body = self.req("POST", "/api/refresh", json.dumps({"mode": "vault"}),
                              {"Content-Type": "application/json", "X-TaskTreker": "1"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.calls[-1], ["vault", "sync"])

    def test_project_mode_takes_only_known_projects(self):              # ОБН-04
        with mock.patch.object(serve, "project_keys", lambda: {"alpha-api"}):
            code, body = self.req("POST", "/api/refresh",
                                  json.dumps({"mode": "project", "project": "alpha-api"}),
                                  {"X-TaskTreker": "1"})
            self.assertEqual(code, 200)
            self.assertEqual(self.calls[-1], ["refresh", "--project", "alpha-api"])
            for bad in ("beta; rm -rf /", "", None, "../alpha-api"):
                with self.subTest(project=bad):
                    code, _ = self.req("POST", "/api/refresh",
                                       json.dumps({"mode": "project", "project": bad}),
                                       {"X-TaskTreker": "1"})
                    self.assertEqual(code, 400)

    def test_non_object_body_is_ignored(self):
        code, _ = self.req("POST", "/api/refresh", json.dumps([1, 2]), {"X-TaskTreker": "1"})
        self.assertEqual(code, 200)                                     # как пустой запрос: режим fast

    def test_unknown_mode_rejected(self):
        code, _ = self.req("POST", "/api/refresh", json.dumps({"mode": "rm -rf"}),
                           {"X-TaskTreker": "1"})
        self.assertEqual(code, 400)

    def test_foreign_host_rejected(self):                               # БЕЗ-01
        self.assertEqual(self.req("GET", "/", headers={"Host": "evil.example"})[0], 403)

    def test_binds_loopback_only(self):
        self.assertIn('("127.0.0.1", p)', inspect.getsource(serve.main))


if __name__ == "__main__":
    unittest.main()
