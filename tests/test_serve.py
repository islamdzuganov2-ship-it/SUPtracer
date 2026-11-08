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

    def test_workflow_requires_header_and_routes_validated_actions(self):
        import workflow
        self.assertEqual(self.req('POST','/api/work','{}')[0],403)
        with mock.patch.object(workflow,'action',return_value={'ok':True}) as action:
            code,body=self.req('POST','/api/work',json.dumps({'op':'focus','projects':[]}),{'X-TaskTreker':'1'})
            self.assertEqual(code,200)
            action.assert_called_once_with({'op':'focus','projects':[]})
        with mock.patch.object(workflow,'action',side_effect=ValueError('invalid')):
            self.assertEqual(self.req('POST','/api/work','{}',{'X-TaskTreker':'1'})[0],400)

    def test_launch_requires_header_and_reports_errors(self):
        self.assertEqual(self.req('POST','/api/launch','{}')[0],403)
        with mock.patch.object(serve,'launch_session',return_value={'run':'RUN-1','pid':7}) as launch:
            code,body=self.req('POST','/api/launch',json.dumps({'ref':'a/B-1','agent':'claude'}),{'X-TaskTreker':'1'})
        self.assertEqual((code,json.loads(body)['data']['pid']),(200,7))
        launch.assert_called_once_with({'ref':'a/B-1','agent':'claude'})
        with mock.patch.object(serve,'launch_session',side_effect=ValueError('Командная строка не найдена')):
            code,body=self.req('POST','/api/launch','{}',{'X-TaskTreker':'1'})
        self.assertEqual((code,json.loads(body)['error']),(400,'Командная строка не найдена'))

    def test_integrations_status(self):
        with mock.patch.object(serve, "integration_status", return_value={"codex": {"connected": True}}):
            code, body = self.req("GET", "/api/integrations")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["providers"]["codex"]["connected"])

    def test_connect_requires_header_and_known_provider(self):
        self.assertEqual(self.req("POST", "/api/connect", '{}')[0], 403)
        for source in ("unknown", ["codex"], {"codex": True}):
            code, _ = self.req("POST", "/api/connect", json.dumps({"source": source}), {"X-TaskTreker": "1"})
            self.assertEqual(code, 400)
        code, _ = self.req("POST", "/api/connect", json.dumps({"source": "codex"}), {"X-TaskTreker": "1"})
        self.assertEqual(code, 200)
        self.assertEqual(self.calls[-1], ["connect", "--provider", "codex"])

    def test_refresh_source_and_body_limit(self):
        code, _ = self.req("POST", "/api/refresh", json.dumps({"mode": "fast", "source": "codex"}), {"X-TaskTreker": "1"})
        self.assertEqual(code, 200)
        self.assertEqual(self.calls[-1], ["refresh", "--fast", "--source", "codex"])
        with mock.patch.object(serve, "MAX_BODY", 2):
            code, _ = self.req("POST", "/api/refresh", '{"a":1}', {"X-TaskTreker": "1"})
        self.assertEqual(code, 413)

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
