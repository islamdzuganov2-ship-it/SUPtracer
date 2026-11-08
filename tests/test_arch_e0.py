# -*- coding: utf-8 -*-
"""TT-ARCH-001, этап E0: флаг модуля, отчёт возможностей только для чтения, закрытый адрес /api/arch/.

TTN-004 (негативный тест неавторизованного доступа), TTN-005/006 (возможности Windows и Docker без секретов
и без запуска Docker), TTN-010 (выключенный флаг модуля). Реальные команды не вызываются: run и which подставные.
"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from base import TOOLS  # noqa: F401
import arch_capabilities as ac
import flags as ff


def fake(answers, calls):
    """run по таблице {первые слова команды: (код, вывод)}; все вызовы пишутся в calls."""
    def run(cmd):
        calls.append(list(cmd))
        for key, val in answers.items():
            if ' '.join(cmd).startswith(key):
                return val
        return 0, ''
    return run


WIN_OK = {
    'powershell -NoProfile -NonInteractive -Command $PSVersionTable': (0, '5.1.26100.1\r\n'),
    'powershell -NoProfile -NonInteractive -Command (Get-Command Get-CimInstance': (0, 'Get-CimInstance\n'),
    'powershell -NoProfile -NonInteractive -Command (Get-Command Get-NetTCPConnection': (0, 'NetTCPIP\n'),
    'netsh': (0, 'Listen on ipv4: 10.0.0.5 secret-host.corp 8080\n'),
    'docker --version': (0, 'Docker version 29.8.0, build abc\n'),
    'docker version': (1, 'Cannot connect to the Docker daemon at npipe:////./pipe/docker_engine\n'),
    'docker context show': (0, 'default\n'),
    'docker compose version': (0, 'v5.5.1\n'),
}


class Capabilities(unittest.TestCase):
    def probe(self, answers=WIN_OK, which=lambda c: 'C:/bin/' + c, **kw):
        calls = []
        r = ac.probe(run=fake(answers, calls), which=which, system=kw.pop('system', 'Windows'),
                     admin=kw.pop('admin', False), **kw)
        return r, calls

    def by(self, r, part):
        return {c['name']: c for c in r[part]}

    def test_daemon_down_is_fact_and_docker_never_started(self):          # TTN-006
        r, calls = self.probe()
        d = self.by(r, 'docker')
        self.assertEqual(d['client']['status'], ac.AVAILABLE); self.assertEqual(d['client']['value'], '29.8.0')
        self.assertEqual(d['daemon']['status'], ac.UNAVAILABLE)
        self.assertIn('Docker не запускался', d['daemon']['reason'])
        self.assertEqual(d['compose']['value'], '5.5.1')
        for cmd in calls:
            self.assertTrue(ac.readonly(cmd), cmd)
            self.assertNotIn('Docker Desktop', ' '.join(cmd))
        self.assertTrue(all(ac.readonly(c) for c, _ in list(ac.WINDOWS.values()) + list(ac.DOCKER.values())))
        self.assertFalse(ac.readonly(['docker', 'compose', 'up']))
        self.assertFalse(ac.readonly(['netsh', 'interface', 'portproxy', 'add', 'v4tov4']))

    def test_windows_capabilities_without_secrets(self):                  # TTN-005
        r, _ = self.probe()
        w = self.by(r, 'windows')
        self.assertEqual(w['powershell']['value'], '5.1.26100.1')
        self.assertEqual(w['nettcpip']['status'], ac.AVAILABLE)
        self.assertEqual(w['elevated']['status'], ac.UNAVAILABLE)
        text = json.dumps(r, ensure_ascii=False)
        # Вывод netsh и адрес демона в отчёт не копируются, имени компьютера и пользователя в нём нет.
        for leak in ('10.0.0.5', 'secret-host', 'npipe', os.environ.get('USERNAME') or '\0', os.environ.get('COMPUTERNAME') or '\0'):
            self.assertNotIn(leak, text)
        self.assertTrue(r['readonly']); self.assertEqual(r['stage'], 'E0')

    def test_unproven_is_unknown_not_available(self):
        answers = dict(WIN_OK)
        answers['docker context show'] = (0, 'странный вывод с пробелами и ; символами\n')
        answers['powershell -NoProfile -NonInteractive -Command $PSVersionTable'] = (None, 'powershell: нет ответа за 20 с')
        r, _ = self.probe(answers)
        self.assertEqual(self.by(r, 'docker')['context']['status'], ac.UNKNOWN)
        self.assertEqual(self.by(r, 'windows')['powershell']['status'], ac.UNKNOWN)
        r, _ = self.probe(admin=None)
        # Подставное None — значит «спросить ОС»; на не-Windows признака прав нет вовсе.
        self.assertIn(self.by(r, 'windows')['elevated']['status'], (ac.AVAILABLE, ac.UNAVAILABLE, ac.UNKNOWN))

    def test_no_docker_client(self):
        r, calls = self.probe(which=lambda c: None if c == 'docker' else 'C:/bin/' + c)
        d = self.by(r, 'docker')
        self.assertTrue(all(x['status'] == ac.UNAVAILABLE for x in d.values()))
        self.assertFalse([c for c in calls if c[0] == 'docker'])

    def test_not_windows(self):
        r, calls = self.probe(system='Linux')
        self.assertEqual(r['windows'][0]['status'], ac.UNAVAILABLE)
        self.assertFalse([c for c in calls if c[0] in ('powershell', 'netsh')])


class Flag(unittest.TestCase):                                            # TTN-010
    def test_flag_registered_off_and_cannot_be_enabled_yet(self):
        self.assertIn('network_architecture_enabled', ff.REGISTRY)
        self.assertFalse(ff.enabled('network_architecture_enabled', {}))
        with self.assertRaisesRegex(ValueError, 'не реализован'):
            ff.apply(dict(network_architecture_enabled=True), {})
        # Запись в обход владельца не включает модуль, пока у него нет реализации; выключение — откат без потерь.
        self.assertFalse(ff.enabled('network_architecture_enabled', dict(flags=dict(network_architecture_enabled=True))))
        self.assertEqual(ff.apply(dict(network_architecture_enabled=False), {}), dict(flags={}))


class ArchApi(unittest.TestCase):                                         # TTN-004
    @classmethod
    def setUpClass(cls):
        import serve
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.data = tempfile.mkdtemp(prefix='tt-arch-')

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def get(self, path, headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers)
        with mock.patch.dict(os.environ, {"TASKTREKER_DATA": self.data}):
            c.request("GET", path, headers=h)
            r = c.getresponse(); body = r.read().decode("utf-8")
        c.close()
        return r.status, body

    def test_topology_read_needs_session_not_just_host(self):
        import session_auth as sa
        import workflow
        good = {"X-TaskTreker": "1", sa.HEADER: sa.TOKEN}
        refused = ({}, {"X-TaskTreker": "1"}, {"X-TaskTreker": "1", sa.HEADER: "0" * 64},
                   dict(good, Origin="http://evil.example"), dict(good, **{"Sec-Fetch-Site": "cross-site"}))
        for h in refused:
            code, body = self.get("/api/arch/graph", h)
            self.assertEqual(code, 403, h); self.assertNotIn('"nodes"', body)
        self.assertEqual(self.get("/api/arch/graph", dict(good, Host="evil.example"))[0], 403)
        with mock.patch.object(workflow, 'state', return_value=dict(settings={})):
            code, body = self.get("/api/arch/graph", good)
        self.assertEqual(code, 409); self.assertTrue(json.loads(body)['disabled'])

    def test_enabled_module_applies_acl(self):                            # TT-133: права на уровне API
        import arch_model as am
        import owner_auth as oa
        import session_auth as sa
        import workflow
        c = am.container('d1', 'abc', 'blog_api', 'exited')
        snap = am.snapshot([c], [am.source('docker:engine', 'available')])
        os.makedirs(os.path.join(self.data, 'arch'), exist_ok=True)
        with open(os.path.join(self.data, 'arch', 'inventory.json'), 'w', encoding='utf-8') as f:
            json.dump(snap, f)
        good = {"X-TaskTreker": "1", sa.HEADER: sa.TOKEN}
        path = "/api/arch/object?id=" + c['id']
        with mock.patch.object(ff, 'enabled', return_value=True), \
             mock.patch.object(workflow, 'state', return_value=dict(settings={})):
            self.assertEqual(self.get(path, good)[0], 404)                           # доска без токена владельца
            with mock.patch.object(oa, 'check_token', return_value=True):
                code, body = self.get(path, good)
                self.assertEqual((code, json.loads(body)['data']['name']), (200, 'blog_api'))
                self.assertEqual(self.get("/api/arch/object?id=container-0000", good)[0], 404)


if __name__ == "__main__":
    unittest.main()
