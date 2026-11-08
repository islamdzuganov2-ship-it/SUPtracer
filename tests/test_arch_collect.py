# -*- coding: utf-8 -*-
"""Коллекторы и права «Архитектуры систем» по fixture (TT-133).

Windows: TTN-013…018, NET-AC-006…011, 014. Docker и Compose: NET-AC-015…020, 022, 023. Пути: NET-AC-024, 025.
Права: ARCH-FR-15.2, NET-AC-054…056. Реальные команды не вызываются: run подставной, Docker не нужен.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from base import TOOLS  # noqa: F401
import arch_acl as acl
import arch_collect as c
import arch_model as m
import project_routes as pr

FIX = Path(__file__).resolve().parent / 'fixtures' / 'arch'
CANARIES = ('CanaryPass_9f3a!x', 'CanaryToken_ZZ8q!', 'CanaryArg_4mW!', 'CanaryLabel_7Hx!', 'CanaryCompose_5k!Q', 'CanaryCmd_2z!')


def read(name):
    return (FIX / name).read_text(encoding='utf-8')


def fake_run(table, calls=None):
    def run(cmd):
        if calls is not None:
            calls.append(list(cmd))
        assert c.readonly(cmd), cmd
        for key, val in table.items():
            if ' '.join(cmd).startswith(key):
                return val
        return 0, ''
    return run


def windows_table():
    t = {}
    for k, f in (('tcp', 'tcp.json'), ('udp', 'udp.json'), ('process', 'process.json'), ('service', 'service.json'),
                 ('excluded_tcp4', 'excluded_tcp4.txt'), ('urlacl', 'urlacl.txt'), ('sslcert', 'sslcert.txt'),
                 ('portproxy', 'portproxy.txt')):
        t[' '.join(c.WIN[k])] = (0, read(f))
    return t


class Windows(unittest.TestCase):
    def setUp(self):
        self.procs = c.parse_processes(read('process.json'))

    def test_tcp_listen_vs_connections_and_pid_reuse(self):               # TTN-013, NET-AC-006, 014
        binds, conns = c.parse_tcp(read('tcp.json'), self.procs, 'e')
        listen = {(b['bind_ip'], b['port']): b for b in binds}
        self.assertNotIn(('192.0.2.10', 51234), listen)                    # исходящее — не сервер
        self.assertEqual(len(conns), 1); self.assertEqual(conns[0]['socket_state'], 'connection_observed')
        self.assertEqual(listen[('127.0.0.1', 8787)]['scope'], 'loopback')
        self.assertEqual(listen[('::1', 5432)]['family'], 'ipv6')
        self.assertNotEqual(listen[('0.0.0.0', 8000)]['id'], listen[('::', 8000)]['id'])
        self.assertEqual(listen[('127.0.0.1', 8787)]['owner'], self.procs[4100]['id'])
        # PID 4400 создан в 11:00, сокет — в 07:00: это другой процесс с тем же PID.
        self.assertIsNone(listen[('0.0.0.0', 9000)]['owner'])
        self.assertIsNone(listen[('0.0.0.0', 135)]['owner'])                # PID 1100 в списке процессов нет — unknown

    def test_udp_is_own_class(self):                                      # TTN-014, NET-AC-004
        udp = c.parse_udp(read('udp.json'), self.procs, 'e')
        tcp, _ = c.parse_tcp(read('tcp.json'), self.procs, 'e')
        self.assertTrue(all(u['socket_state'] == 'bound_observed' and u['protocol'] == 'udp' for u in udp))
        u8000 = next(u for u in udp if u['port'] == 8000)
        t8000 = next(t for t in tcp if t['port'] == 8000 and t['family'] == 'ipv4')
        self.assertNotEqual(u8000['id'], t8000['id'])
        self.assertEqual(m.conflicts([u8000, t8000]), [])
        self.assertIsNone(next(u for u in udp if u['port'] == 5353)['owner'])     # у процесса нет времени создания

    def test_stopped_service_not_listening(self):                         # NET-AC-007
        s = {x['name']: x for x in c.parse_services(read('service.json'), 'e')}
        self.assertEqual(s['postgresql-x64-16']['lifecycle'], 'stopped')
        self.assertIsNone(s['postgresql-x64-16']['pid'])
        self.assertNotIn('socket_state', s['postgresql-x64-16'])

    def test_netsh_reservations_localized(self):                          # TTN-016…018, NET-AC-009…011
        ex = c.parse_excluded(read('excluded_tcp4.txt'), 'tcp', 'ipv4', 'e')
        self.assertEqual([(r['port_start'], r['port_end'], r['persistent']) for r in ex],
                         [(1080, 1179, False), (5357, 5357, False), (50000, 50059, True)])
        ua = c.parse_urlacl(read('urlacl.txt'), 'e')
        self.assertEqual([r['url_prefix'] for r in ua], ['http://+:80/Temporary_Listen_Addresses/', 'https://+:5986/wsman/'])
        self.assertNotIn('S-1-5-80', json.dumps(ua))                       # SDDL и учётные записи не берутся
        ssl = c.parse_sslcert(read('sslcert.txt'), 'e')
        self.assertEqual([(r['url_prefix'], r['port_start']) for r in ssl], [('0.0.0.0:44300', 44300), ('dev.example.test:8443', 8443)])
        self.assertNotIn('0123456789abcdef', json.dumps(ssl))
        pp = c.parse_portproxy(read('portproxy.txt'), 'e')
        self.assertEqual([(r['listen'], r['connect'], r['url_prefix']) for r in pp][0],
                         (dict(address='0.0.0.0', port=2222), dict(address='172.20.0.5', port=22), 'v4tov4'))
        for r in ex + ua + ssl + pp:
            self.assertNotIn('socket_state', r); m.validate(r)

    def test_without_admin_rights_partial_not_failure(self):              # NET-FR-02.5, NET-AC-008
        t = windows_table()
        t[' '.join(c.WIN['urlacl'])] = (1, '')
        t[' '.join(c.WIN['excluded_udp6'])] = (None, 'netsh: нет ответа за 30 с')
        objs, srcs = c.collect_windows(fake_run(t))
        st = {s['collector_id']: s for s in srcs}
        self.assertEqual(st['netsh:urlacl']['state'], 'unavailable')
        self.assertEqual(st['windows:tcp']['state'], 'partial')                     # у части сокетов владелец не установлен
        self.assertIn('владелец не установлен', st['windows:tcp']['reason'])
        self.assertEqual(st['netsh:excluded_tcp4']['state'], 'available')
        self.assertTrue(any(o['kind'] == 'binding' for o in objs))

    def test_only_readonly_commands(self):
        for cmd in list(c.WIN.values()) + list(c.DOCKER.values()):
            self.assertTrue(c.readonly(cmd), cmd)
        for bad in (['netsh', 'interface', 'portproxy', 'add', 'v4tov4'], ['docker', 'compose', 'config'],
                    ['docker', 'start', 'x'], ['netsh', 'http', 'delete', 'urlacl']):
            self.assertFalse(c.readonly(bad))
            with self.assertRaises(ValueError):
                c._run(bad)


class Docker(unittest.TestCase):
    def routes(self):
        return pr.paths({'shop': dict(path='C:/work/shop'), 'blog': dict(path='C:/work/blog'),
                         'twin-a': dict(path='C:/work/twin'), 'twin-b': dict(path='C:/work/twin')})

    def parsed(self):
        return c.parse_inspect(read('inspect.json'), 'd1', 'e', self.routes())

    def test_runtime_published_vs_configured_vs_exposed(self):           # NET-AC-015, 017, 018
        cs, bs = self.parsed()
        by = {x['name']: x for x in cs}
        shop = [b for b in bs if b['owner'] == by['shop_api']['id']]
        self.assertEqual({(b['bind_ip'], b['port'], b['configuration_state']) for b in shop},
                         {('0.0.0.0', 49153, 'published'), ('::', 49153, 'published')})   # динамический порт — из runtime
        self.assertEqual(by['shop_api']['exposed'], ['8000/tcp', '9100/tcp'])
        self.assertFalse([b for b in bs if b['container_port'] == 9100])               # EXPOSE без публикации — не порт хоста
        blog = [b for b in bs if b['owner'] == by['blog_api']['id']]
        self.assertEqual([(b['port'], b['configuration_state'], b['socket_state']) for b in blog], [(8001, 'configured', 'not_observed')])
        self.assertEqual(by['blog_api']['lifecycle'], 'exited')
        for b in bs:
            m.validate(b)

    def test_projects_by_compose_label_not_by_name(self):                 # NET-AC-020, 022, NET-FR-07.3
        cs, _ = self.parsed()
        by = {x['name']: x for x in cs}
        self.assertEqual(by['shop_api']['project'], 'shop')
        self.assertEqual(by['blog_api']['project'], 'blog')
        self.assertNotEqual(by['shop_api']['id'], by['blog_api']['id'])                # оба сервиса api, но разные проекты
        self.assertEqual(by['shop-api-copy']['project_method'], 'unassigned')          # похожее имя — не доказательство
        self.assertIsNone(by['shop-api-copy']['project'])
        self.assertEqual((by['twin_backend']['project_method'], by['twin_backend']['project_candidates']),
                         ('ambiguous', ['twin-a', 'twin-b']))

    def test_no_secrets_from_inspect(self):                               # NET-AC-023
        cs, bs = self.parsed()
        text = json.dumps(cs + bs, ensure_ascii=False)
        for canary in CANARIES[:4]:
            self.assertNotIn(canary, text)
        self.assertNotIn('secret.note', text)

    def test_daemon_down_keeps_windows(self):                             # NET-AC-019, NET-AC-065
        t = windows_table()
        t['docker info'] = (1, 'error during connect: npipe')
        t['docker context ls'] = (0, '{"Name":"default","Current":true,"DockerEndpoint":"npipe:////./pipe/docker_engine"}\n')
        calls = []
        snap = c.collect(fake_run(t, calls), which=lambda x: 'C:/bin/' + x, projects={}, system='Windows')
        st = {s['collector_id']: s for s in snap['sources']}
        self.assertEqual(st['docker:engine']['state'], 'unavailable')
        self.assertIn('Docker не запускался', st['docker:engine']['reason'])
        self.assertTrue(any(o['kind'] == 'binding' and o['host_id'] == 'local' for o in snap['objects']))
        self.assertFalse(snap['complete'])
        self.assertFalse([x for x in calls if x[:2] == ['docker', 'ps'] or x[:2] == ['docker', 'inspect']])
        self.assertNotIn('npipe', json.dumps(snap))                                  # endpoint контекста не берётся

    def test_full_engine_and_size_guard(self):
        ids = 'aaaaaaaaaaaa1111111111111111111111111111111111111111111111111111\n; rm -rf /\n'
        t = {'docker context ls': (0, ''), 'docker info': (0, 'ABCD:EFGH\n'), 'docker ps': (0, ids),
             'docker inspect': (0, read('inspect.json'))}
        calls = []
        objs, srcs = c.collect_docker(fake_run(t, calls), which=lambda x: 'docker', routes=self.routes())
        insp = next(x for x in calls if x[:2] == ['docker', 'inspect'])
        self.assertEqual(insp[2:], ['aaaaaaaaaaaa1111111111111111111111111111111111111111111111111111'])   # мусор из вывода не стал аргументом
        self.assertEqual(srcs[-1]['state'], 'available')
        t['docker inspect'] = (0, 'x' * (c.MAX_OUT + 1))
        _, srcs = c.collect_docker(fake_run(t), which=lambda x: 'docker', routes=self.routes())
        self.assertEqual(srcs[-1]['state'], 'unavailable'); self.assertIn('МБ', srcs[-1]['reason'])


class Configs(unittest.TestCase):
    def test_compose_declared_without_secrets_and_variables(self):       # NET-FR-06, NET-AC-013, 016, 023
        info, binds = c.parse_compose(str(FIX / 'shop' / 'docker-compose.yml'), str(FIX / 'shop'), 'shop')
        got = {(b['bind_ip'], b['port'], b['protocol'], b['container_port']) for b in binds}
        self.assertEqual(got, {('127.0.0.1', 8080, 'tcp', 80), ('0.0.0.0', 5000, 'udp', 5000)})
        api = next(s for s in info['services'] if s['service'] == 'api')
        self.assertEqual([u['variables'] for u in api['unresolved_ports']], [[], ['API_PORT']])   # "8000" — порт назначит Docker
        self.assertEqual(api['expose'], ['9100'])
        self.assertTrue(all(b['evidence_quality'] == 'declared' and b['socket_state'] == 'not_observed' for b in binds))
        text = json.dumps(info, ensure_ascii=False) + json.dumps(binds, ensure_ascii=False)
        for canary in CANARIES[4:]:
            self.assertNotIn(canary, text)
        self.assertNotIn('3000', text)                                     # порт из README не разбирается
        self.assertEqual(c.parse_dockerfile(read('shop/Dockerfile')), ['8000/tcp', '9100/tcp'])

    def test_declared_matched_with_runtime(self):                         # NET-AC-016
        routes = pr.paths({'blog': dict(path='C:/work/blog')})
        cs, rt = c.parse_inspect(read('inspect.json'), 'd1', 'e', routes)
        _, decl = c.parse_compose(str(FIX / 'blog' / 'compose.yaml'), str(FIX / 'blog'), 'blog')
        res = {(r['declared_port'] if 'declared_port' in r else None, r['outcome']) for r in c.match_declared(decl, rt, cs)}
        self.assertIn((8001, 'match'), res)
        self.assertIn((None, 'not_observed'), res)                         # 8080:80 не наблюдался

    def test_paths_outside_and_worktrees_refused(self):                   # NET-AC-024, NET-AC-025
        root = tempfile.mkdtemp(prefix='tt-arch-root-'); outside = tempfile.mkdtemp(prefix='tt-arch-out-')
        self.addCleanup(shutil.rmtree, root, True); self.addCleanup(shutil.rmtree, outside, True)
        Path(root, 'compose.yaml').write_text('services: {}\n', encoding='utf-8')
        Path(outside, 'compose.yaml').write_text('services: {}\n', encoding='utf-8')
        for wt in (('.claude', 'worktrees', 'x'), ('copy',)):
            d = Path(root, *wt); d.mkdir(parents=True)
            (d / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
        Path(root, 'copy', '.git').write_text('gitdir: elsewhere\n', encoding='utf-8')   # рабочая копия Git
        linked = False
        try:
            os.symlink(os.path.join(outside, 'compose.yaml'), os.path.join(root, 'docker-compose.yml'))
            os.symlink(outside, os.path.join(root, 'linkdir'), target_is_directory=True)
            linked = True
        except (OSError, NotImplementedError):
            pass                                                           # без прав на ссылки — проверка ниже по realpath
        found, refused = c.config_files(root)
        self.assertEqual([os.path.relpath(f, os.path.realpath(root)) for f in found], ['compose.yaml'])
        if linked:
            self.assertEqual([r['reason'] for r in refused], ['ссылка за пределы проекта'])
        self.assertFalse(c._inside(os.path.join(outside, 'compose.yaml'), os.path.realpath(root)))


class Acl(unittest.TestCase):                                             # ARCH-FR-15.2, NET-AC-054…056
    def snap(self):
        routes = pr.paths({'shop': dict(path='C:/work/shop'), 'blog': dict(path='C:/work/blog')})
        cs, bs = c.parse_inspect(read('inspect.json'), 'd1', 'e1', routes)
        ev = m.evidence('runtime_metadata', 'docker:inspect', 'c')
        procs = c.parse_processes(read('process.json'))
        tcp, _ = c.parse_tcp(read('tcp.json'), procs, ev['id'])
        objs = [ev] + cs + [dict(b, evidence_refs=[ev['id']]) for b in bs] + list(procs.values()) + tcp
        return m.snapshot(objs, [m.source('docker:engine', 'available')])

    def test_agent_sees_only_own_project(self):
        s = self.snap()
        shop = acl.principal('agent', project='shop')
        v = acl.view(s, shop)
        text = json.dumps(v)
        self.assertTrue(v['objects'])
        self.assertIn('shop_api', text)
        self.assertNotIn('blog_api', text); self.assertNotIn('8001', text)            # NET-AC-056
        self.assertNotIn('127.0.0.1', text)                                           # хостовые сокеты без проекта — только владельцу
        self.assertTrue(all(set(o) >= {'kind', 'id'} for o in v['objects']))

    def test_direct_id_of_foreign_object_refused_like_missing(self):     # ARCH-FR-15.2
        s = self.snap()
        blog = next(o for o in s['objects'] if o.get('name') == 'blog_api')
        shop = acl.principal('agent', project='shop')
        with self.assertRaises(LookupError) as a:
            acl.get(s, blog['id'], shop)
        with self.assertRaises(LookupError) as b:
            acl.get(s, 'container-0000000000000000', shop)
        self.assertEqual(str(a.exception), str(b.exception))
        code, body = acl.handle('/api/arch/object', {'id': [blog['id']]}, shop, s)
        self.assertEqual(code, 404)
        code, body = acl.handle('/api/arch/object', {'id': [blog['id']]}, acl.principal('owner'), s)
        self.assertEqual((code, body['data']['name']), (200, 'blog_api'))

    def test_anonymous_and_mvp_limits(self):                             # NET-AC-054, NET-FR-15.2
        s = self.snap()
        anon = acl.principal('agent')                                    # доска без токена владельца и без проекта
        for route in ('summary', 'graph', 'object'):
            self.assertEqual(acl.handle('/api/arch/' + route, {}, anon, s)[0], 404)
        owner = acl.principal('owner')
        self.assertFalse(acl.allowed(owner, 'execute_deployment'))
        self.assertFalse(acl.allowed(owner, 'run_scan_remote'))
        self.assertTrue(acl.allowed(owner, 'run_scan_local'))
        self.assertFalse(acl.allowed(acl.principal('agent', project='shop'), 'manage_project_mapping', 'shop'))
        with self.assertRaises(ValueError):
            acl.allowed(owner, 'sudo')
        self.assertEqual(acl.summary(s, acl.principal('agent', project='blog'))['counts'].get('container'), 1)


if __name__ == '__main__':
    unittest.main()
