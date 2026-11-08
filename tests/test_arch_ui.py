"""TT-136: вкладка «Архитектура систем» — проекция для доски, сбор с доски, адреса сервера.

Права те же, что у снимка (arch_acl): владелец видит всё, агент проекта — своё, прочие — ничего; чужое и
несуществующее отвечают одинаково. Сбор и внесение процесса — только владелец и только при включённом флаге.
Реальные команды ОС не вызываются: коллектор подставной, граф кода строится по синтетическому репозиторию.
"""
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from base import TOOLS  # noqa: F401
import arch_acl
import arch_api
import arch_model as am
import arch_static
import flags as ff

OWNER = arch_acl.principal('owner')
SHOP = arch_acl.principal('agent', 'shop')
BLOG = arch_acl.principal('agent', 'blog')
NOBODY = arch_acl.principal('agent')


def repo(root):
    os.makedirs(os.path.join(root, 'app'), exist_ok=True)
    with open(os.path.join(root, 'app', '__init__.py'), 'w', encoding='utf-8') as f:
        f.write('')
    with open(os.path.join(root, 'app', 'core.py'), 'w', encoding='utf-8') as f:
        f.write('def helper():\n    return 1\n\n\ndef work():\n    return helper()\n')
    return root


class Projection(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix='tt-archui-')
        self.src = repo(tempfile.mkdtemp(prefix='tt-archui-src-'))
        self.projects = {'shop': dict(path=self.src)}

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def save_graph(self):
        g = arch_static.scan(self.src, 'shop', self.projects, run=lambda cmd: (1, ''))
        arch_static.save(g, self.data)
        return g

    def test_empty_overview_says_unknown_not_zero(self):
        ov = arch_api.overview(OWNER, base=self.data)
        self.assertIsNone(ov['inventory'])                     # снимка нет — None, а не «0 портов»
        self.assertEqual(ov['code'], [])
        self.assertEqual(ov['processes']['count'], 0)
        self.assertTrue(ov['owner'] and ov['can_scan'])
        self.assertIn('tasktreker_pipeline', ov['seeds'])
        with self.assertRaises(PermissionError):
            arch_api.overview(NOBODY, base=self.data)
        self.assertFalse(arch_api.overview(SHOP, base=self.data)['can_scan'])

    def test_code_graph_visible_only_to_owner_and_own_project(self):
        g = self.save_graph()
        self.assertEqual(g['project']['project'], 'shop')
        full = arch_api.code('shop', OWNER, base=self.data)
        names = {n['qualname'] for n in full['nodes']}
        self.assertTrue({'app.core.helper', 'app.core.work'} <= names)
        self.assertTrue(any(e['type'] == 'calls_static' for e in full['edges']))
        self.assertEqual(arch_api.code('shop', SHOP, base=self.data)['brief']['repo'], 'shop')
        for who in (BLOG, NOBODY):
            with self.assertRaises(LookupError) as a:
                arch_api.code('shop', who, base=self.data)
            with self.assertRaises(LookupError) as b:
                arch_api.code('nope', OWNER, base=self.data)
            self.assertEqual(str(a.exception), str(b.exception))   # чужое = несуществующее
        self.assertEqual([c['repo'] for c in arch_api.overview(BLOG, base=self.data)['code']], [])

    def test_inventory_counts_follow_acl(self):
        c1 = dict(am.container('d1', 'a', 'shop_api', 'running'), project='shop', project_method='compose_label')
        c2 = am.container('d1', 'b', 'stray', 'exited')
        snap = am.snapshot([c1, c2], [am.source('docker:engine', 'available')])
        os.makedirs(os.path.join(self.data, 'arch'), exist_ok=True)
        with open(os.path.join(self.data, 'arch', 'inventory.json'), 'w', encoding='utf-8') as f:
            json.dump(snap, f)
        self.assertEqual(arch_api.overview(OWNER, base=self.data)['inventory']['counts']['container'], 2)
        self.assertEqual(arch_api.overview(SHOP, base=self.data)['inventory']['counts'].get('container'), 1)
        self.assertEqual(arch_api.overview(BLOG, base=self.data)['inventory']['counts'], {})

    def test_seeded_process_is_draft_and_breaks_with_reason(self):
        with self.assertRaises(PermissionError):
            arch_api.seed('tasktreker_pipeline', SHOP, base=self.data)
        with self.assertRaises(ValueError):
            arch_api.seed('../secret', OWNER, base=self.data)
        got = arch_api.seed('tasktreker_pipeline', OWNER, base=self.data)
        self.assertEqual(got['state'], 'draft')                # утверждение — отдельное действие владельца
        self.assertEqual(arch_api.seed('tasktreker_pipeline', OWNER, base=self.data)['id'], got['id'])  # повтор не плодит версий
        out = arch_api.processes(OWNER, base=self.data)['processes']
        self.assertEqual(len(out), 1)
        steps = {s['step']['key']: s for s in out[0]['steps']}
        work = [s for s in steps.values() if s['step']['type'] == 'task']
        self.assertTrue(work)
        for s in work:                                         # привязок нет — разрыв с причиной, путь не дорисован
            self.assertEqual(s['chains'][0]['broken']['reason'], 'no_step_mapping')
            self.assertFalse(s['chains'][0]['complete'])
        self.assertGreater(len(out[0]['paths']['paths']), 0)
        for s in steps.values():                               # начало, конец и развилки — без «разрыва»
            if s['step']['type'] in arch_api.STRUCTURAL:
                self.assertTrue(s['structural']); self.assertEqual(s['chains'], [])
        manual = [s for s in steps.values() if s['step']['type'] == 'manual']
        self.assertEqual(manual[0]['chains'][0]['broken']['reason'], 'not_automated')
        self.assertEqual(arch_api.processes(SHOP, base=self.data)['processes'], [])   # процесс проекта tasktreker

    def test_handle_routes_and_refusals(self):
        self.assertEqual(arch_api.handle('overview', {}, NOBODY, base=self.data)[0], 404)
        self.assertEqual(arch_api.handle('processes', {}, NOBODY, base=self.data)[0], 404)
        self.assertEqual(arch_api.handle('code', {'repo': ['x']}, OWNER, base=self.data)[0], 404)
        self.assertIsNone(arch_api.handle('graph', {}, OWNER, base=self.data))   # снимок отдаёт arch_acl
        self.assertEqual(arch_api.handle('overview', {}, OWNER, base=self.data)[0], 200)

    def test_scan_keys_only_known_projects(self):
        self.assertEqual(arch_api.scan_keys({}, self.projects), ['tasktreker'])
        self.assertEqual(arch_api.scan_keys({'projects': ['shop', 'shop']}, self.projects), ['tasktreker', 'shop'])
        for bad in (['nope'], ['../x'], [1]):
            with self.assertRaises(ValueError):
                arch_api.scan_keys({'projects': bad}, self.projects)

    def test_scan_runs_in_background_once_and_reports_steps(self):
        scan = arch_api.Scan()
        gate = threading.Event()

        def collect(projects=None):
            gate.wait(5)
            return am.snapshot([], [am.source('docker:engine', 'unavailable', 'нет docker')])
        self.assertTrue(scan.start(self.projects, ['shop', 'ghost'], self.data, run=collect))
        self.assertFalse(scan.start(self.projects, ['shop'], self.data, run=collect))   # второй сбор не запускается
        self.assertTrue(scan.snapshot()['running'])
        gate.set()
        for _ in range(100):
            if not scan.snapshot()['running']:
                break
            time.sleep(0.05)
        st = scan.snapshot()
        self.assertFalse(st['running'])
        self.assertFalse(st['ok'])                             # у ghost нет каталога — сбор честно с ошибкой
        names = {s['name']: s['ok'] for s in st['steps']}
        self.assertTrue(names['Снимок ОС и Docker'])
        self.assertTrue(names['Граф кода: shop'])
        self.assertFalse(names['Граф кода: ghost'])
        self.assertTrue(os.path.exists(os.path.join(self.data, 'arch', 'inventory.json')))
        self.assertIn('shop', arch_api.code_graphs(self.data))


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import serve
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.data = tempfile.mkdtemp(prefix='tt-archui-srv-')
        with open(os.path.join(cls.data, 'projects.json'), 'w', encoding='utf-8') as f:
            json.dump({'projects': {}}, f)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.data, ignore_errors=True)

    def call(self, method, path, headers, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers)
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            h["Content-Type"] = "application/json"
        with mock.patch.dict(os.environ, {"TASKTREKER_DATA": self.data}):
            c.request(method, path, body=data, headers=h)
            r = c.getresponse(); out = r.read().decode("utf-8")
        c.close()
        return r.status, json.loads(out) if out.startswith('{') else out

    def good(self):
        import session_auth as sa
        return {"X-TaskTreker": "1", sa.HEADER: sa.TOKEN}

    def test_overview_off_then_owner_only(self):
        import owner_auth as oa
        import workflow
        with mock.patch.object(workflow, 'state', return_value=dict(settings={})):
            code, body = self.call("GET", "/api/arch/overview", self.good())
            self.assertEqual(code, 409); self.assertTrue(body['disabled'])
            self.assertEqual(self.call("POST", "/api/arch/scan", self.good(), {})[0], 403)   # не владелец
            with mock.patch.object(oa, 'check_token', return_value=True):
                self.assertEqual(self.call("POST", "/api/arch/scan", self.good(), {})[0], 409)  # флаг выключен
        on = dict(settings=dict(flags=dict(network_architecture_enabled=True)))
        with mock.patch.object(workflow, 'state', return_value=on):
            self.assertEqual(self.call("GET", "/api/arch/overview", self.good())[0], 404)    # доска без токена владельца
            self.assertEqual(self.call("GET", "/api/arch/overview", {"X-TaskTreker": "1"})[0], 403)
            with mock.patch.object(oa, 'check_token', return_value=True):
                code, body = self.call("GET", "/api/arch/overview", self.good())
                self.assertEqual(code, 200); self.assertIsNone(body['data']['inventory'])
                self.assertEqual(self.call("POST", "/api/arch/seed", self.good(), {"name": "nope"})[0], 400)
                code, body = self.call("POST", "/api/arch/seed", self.good(), {"name": "tasktreker_pipeline"})
                self.assertEqual((code, body['data']['state']), (200, 'draft'))
                code, body = self.call("GET", "/api/arch/processes", self.good())
                self.assertEqual(len(body['data']['processes']), 1)
                self.assertEqual(self.call("POST", "/api/arch/scan", self.good(), {"projects": ["nope"]})[0], 400)


def read(name):
    with open(os.path.join(TOOLS, name), encoding='utf-8') as f:
        return f.read()


class Dashboard(unittest.TestCase):
    def test_tab_is_built_into_dashboard(self):
        shell = read('shell.html')
        self.assertIn('id="tab-arch"', shell); self.assertIn('id="arch-root"', shell)
        build = read('build_dashboard.py')
        self.assertIn('archview.js', build)
        js = read('archview.js')
        self.assertIn('/api/arch/overview', js)
        self.assertNotIn('innerHTML = d.', js)                 # данные идут через esc(), не сырыми
        app = read('app.js')
        self.assertIn("'arch'", app)


if __name__ == '__main__':
    unittest.main()
