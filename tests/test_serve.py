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
        import owner_auth                       # ключ владельца теста, не настоящий (TT-104)
        cls.keydir = tempfile.mkdtemp(prefix="tt-owner-")
        owner_auth._path = os.path.join(cls.keydir, "owner.key")
        cls.owner = {"X-TaskTreker": "1", owner_auth.HEADER: owner_auth.board_token(create=True)}

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
        import owner_auth, shutil
        owner_auth._path = None
        shutil.rmtree(cls.keydir, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, session=True):
        import session_auth
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        if session and (headers or {}).get("X-TaskTreker"):
            h[session_auth.HEADER] = session_auth.TOKEN        # страница сервера несёт токен сессии (SEC-006)
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        c.close()
        return r.status, data

    def test_serves_only_the_dashboard(self):                           # ОБН-02
        code, body = self.req("GET", "/")
        import session_auth
        self.assertEqual(code, 200); self.assertTrue(body.endswith("<html>дашборд</html>"))
        self.assertIn('<meta name="tt-session" content="%s">' % session_auth.TOKEN, body)
        self.assertEqual(self.req("GET", "/data/projects.json")[0], 404)
        self.assertEqual(self.req("GET", "/../README.md")[0], 404)

    def test_write_needs_session_and_same_origin(self):                  # SEC-006/007, SEC-TC-07/08
        import session_auth
        body = json.dumps({"op": "focus", "projects": []})
        with mock.patch.dict(os.environ, {"TASKTREKER_DATA": tempfile.mkdtemp()}):
            code, text = self.req("POST", "/api/work", body, {"X-TaskTreker": "1"}, session=False)
            self.assertEqual(code, 403); self.assertIn("токена сессии", text)
            code, _ = self.req("POST", "/api/work", body, {"X-TaskTreker": "1", session_auth.HEADER: "0" * 64}, session=False)
            self.assertEqual(code, 403)
            for bad in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "http://127.0.0.1:1"}):
                code, text = self.req("POST", "/api/work", body, dict({"X-TaskTreker": "1"}, **bad))
                self.assertEqual(code, 403, bad)
            ok = {"X-TaskTreker": "1", "Origin": "http://127.0.0.1:%d" % self.port, "Sec-Fetch-Site": "same-origin"}
            self.assertNotEqual(self.req("POST", "/api/work", "{}", ok)[0], 403)
            log = os.path.join(os.environ["TASKTREKER_DATA"], "security", "http_audit.jsonl")
            rows = [json.loads(l) for l in open(log, encoding="utf-8")]
            self.assertGreaterEqual(len(rows), 6)
            self.assertNotIn(session_auth.TOKEN, open(log, encoding="utf-8").read())

    def test_ping(self):
        code, body = self.req("GET", "/api/ping")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["served"])

    def test_activity_uses_existing_work_response(self):
        import workflow, launcher
        data = dict(runs=[], tasks=[], settings={})
        with mock.patch.object(workflow, 'snapshot', return_value=data), \
             mock.patch.object(workflow, 'state', return_value=dict(runs=[])), \
             mock.patch.object(launcher, 'status', return_value={}):
            code, body = self.req('GET', '/api/work')
        self.assertEqual(code, 200)
        measured = json.loads(body)['data']['activity']
        self.assertEqual(measured['rows'], [])
        self.assertIn('observed_at', measured)

    def test_workflow_requires_header_and_routes_validated_actions(self):
        import workflow
        self.assertEqual(self.req('POST','/api/work','{}')[0],403)
        with mock.patch.object(workflow,'action',return_value={'ok':True}) as action:
            code,body=self.req('POST','/api/work',json.dumps({'op':'focus','projects':[]}),{'X-TaskTreker':'1'})
            self.assertEqual(code,200)
            action.assert_called_once_with({'op':'focus','projects':[]}, origin='agent')   # без токена — агент
            self.assertEqual(self.req('POST','/api/work',json.dumps({'op':'focus'}),self.owner)[0],200)
            self.assertEqual(action.call_args.kwargs, {'origin':'owner'})
        with mock.patch.object(workflow,'action',side_effect=ValueError('invalid')):
            self.assertEqual(self.req('POST','/api/work','{}',{'X-TaskTreker':'1'})[0],400)

    def test_launch_requires_header_and_reports_errors(self):
        self.assertEqual(self.req('POST','/api/launch','{}')[0],403)
        self.assertEqual(self.req('POST','/api/launch','{}',{'X-TaskTreker':'1'})[0],403)     # заголовок без токена владельца
        with mock.patch.object(serve,'launch_session',return_value={'run':'RUN-1','pid':7}) as launch:
            code,body=self.req('POST','/api/launch',json.dumps({'ref':'a/B-1','agent':'claude'}),self.owner)
        self.assertEqual((code,json.loads(body)['data']['pid']),(200,7))
        launch.assert_called_once_with({'ref':'a/B-1','agent':'claude'})
        with mock.patch.object(serve,'launch_session',side_effect=ValueError('Командная строка не найдена')):
            code,body=self.req('POST','/api/launch','{}',self.owner)
        self.assertEqual((code,json.loads(body)['error']),(400,'Командная строка не найдена'))

    def test_integrations_status(self):
        with mock.patch.object(serve, "integration_status", return_value={"codex": {"connected": True}}):
            code, body = self.req("GET", "/api/integrations")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["providers"]["codex"]["connected"])

    def test_connect_requires_header_and_known_provider(self):
        self.assertEqual(self.req("POST", "/api/connect", '{}')[0], 403)
        self.assertEqual(self.req("POST", "/api/connect", json.dumps({"source": "codex"}), {"X-TaskTreker": "1"})[0], 403)
        for source in ("unknown", ["codex"], {"codex": True}):
            code, _ = self.req("POST", "/api/connect", json.dumps({"source": source}), self.owner)
            self.assertEqual(code, 400)
        code, _ = self.req("POST", "/api/connect", json.dumps({"source": "codex"}), self.owner)
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
