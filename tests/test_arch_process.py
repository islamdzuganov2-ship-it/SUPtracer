# -*- coding: utf-8 -*-
"""Бизнес-процессы и трассировка (TT-135: TTN-112…123; NET-AC-084…099, NET-AC-129).

Синтетический процесс заказа — на графе кода fixture tests/fixtures/arch/code. Реальный процесс TaskTreker —
конвейер задачи из docs/КОНВЕЙЕР.md (tests/fixtures/arch/process/tasktreker_pipeline.json) на графе кода
самого репозитория.
"""
import json
import os
import shutil
import tempfile
import unittest

from base import TOOLS  # noqa: F401
import arch_acl
import arch_model as m
import arch_process as p
import arch_static as s

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, 'fixtures', 'arch', 'code')
PIPE = os.path.join(HERE, 'fixtures', 'arch', 'process', 'tasktreker_pipeline.json')
SHA = 'a' * 40
OWNER = arch_acl.principal('owner')
AGENT_SHOP = arch_acl.principal('agent', 'shop')
AGENT_BLOG = arch_acl.principal('agent', 'blog')


def git(sha=SHA):
    def run(cmd):
        if cmd[-2:] == ['rev-parse', 'HEAD']:
            return 0, sha
        if 'abbrev-ref' in cmd:
            return 0, 'разработка'
        return 0, ''
    return run


STEPS = [dict(key='start', type='start', name='Заказ пришёл'),
         dict(key='receive', type='task', name='Принять заказ', refs=['FR-1']),
         dict(key='paid', type='gateway_exclusive', name='Оплачен?'),
         dict(key='split', type='gateway_parallel', name='Параллельно'),
         dict(key='reserve', type='task', name='Резерв склада'),
         dict(key='notify', type='message_send', name='Сообщить складу'),
         dict(key='join', type='gateway_parallel', name='Свести'),
         dict(key='ship', type='task', name='Отгрузить', refs=[dict(code='UC-ship', revision='r1')]),
         dict(key='call', type='manual', name='Звонок клиенту', actor='менеджер'),
         dict(key='wait', type='timer', name='Ждать оплату'),
         dict(key='cancel', type='compensation', name='Снять резерв'),
         dict(key='done', type='end', name='Отгружено'),
         dict(key='failed', type='error_end', name='Не оплачен')]
FLOWS = [dict(**{'from': 'start', 'to': 'receive'}), {'from': 'receive', 'to': 'paid'},
         {'from': 'paid', 'to': 'split', 'kind': 'conditional', 'condition': 'оплачено'},
         {'from': 'paid', 'to': 'wait', 'kind': 'conditional', 'condition': 'не оплачено'},
         {'from': 'wait', 'to': 'paid'}, {'from': 'wait', 'to': 'failed', 'kind': 'timeout'},
         {'from': 'split', 'to': 'reserve'}, {'from': 'split', 'to': 'notify'},
         {'from': 'notify', 'to': 'join', 'kind': 'async_message'}, {'from': 'reserve', 'to': 'join'},
         {'from': 'reserve', 'to': 'cancel', 'kind': 'compensation'}, {'from': 'cancel', 'to': 'failed'},
         {'from': 'join', 'to': 'ship'}, {'from': 'ship', 'to': 'call'}, {'from': 'call', 'to': 'done'}]


def order(version=1, src=None, **kw):
    return p.define('shop', 'order', 'Заказ', [dict(x) for x in STEPS], [dict(x) for x in FLOWS],
                    src or p.source('srs', 'docs/SRS.md', 'r1'), version=version, **kw)


class World(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='tt-proc-')
        root = os.path.join(cls.tmp, 'repo')
        shutil.copytree(CODE, root)
        cls.code = s.scan(root, 'shop', run=git())
        cls.n = {n['qualname']: n for n in cls.code['nodes']}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def runtime(self, socket_state='listening_observed', artifact=None, with_link=True, bind=True):
        ev = m.evidence('runtime_metadata', 'docker:engine', 'c')
        c = m.container('d1', 'abc', 'shop_api', 'running', evidence_ref=ev['id'])
        objs = [ev, c]
        if bind:
            quality = 'verified' if socket_state == 'listening_observed' else 'declared'
            objs.append(m.binding('local', 'host', 'tcp', '127.0.0.1', 8000, ev['id'], socket_state=socket_state,
                                  configuration_state='published', owner=c['id'], quality=quality))
        inv = m.snapshot(objs, [m.source('docker:engine', 'available')])
        manual = [dict(code=self.n['app.web.items']['id'], runtime=c['id'], by='owner', at='2026-11-08',
                       artifact=artifact)] if with_link else []
        links = s.link_runtime(self.code, inv, manual)
        return inv, links, c

    def idx(self, **kw):
        inv, links, _ = self.runtime(**kw)
        return p.Index(self.code, inv, links)


class Schema(World):
    def test_stable_ids_and_structure(self):                              # TTN-112, NET-AC-084
        a, b = order(), order()
        self.assertEqual([x['id'] for x in a['steps']], [x['id'] for x in b['steps']])
        self.assertEqual([x['id'] for x in a['flows']], [x['id'] for x in b['flows']])
        self.assertEqual(order(version=2)['id'], a['id'])                 # версия не меняет id
        kinds = {x['type'] for x in a['steps']}
        self.assertTrue({'start', 'end', 'task', 'gateway_exclusive', 'gateway_parallel'} <= kinds)
        self.assertEqual(a['state'], 'draft')

    def test_referential_integrity_rejected(self):
        bad = [
            ([dict(x) for x in STEPS], FLOWS + [{'from': 'ship', 'to': 'nowhere'}], 'несуществующий шаг'),
            ([x for x in STEPS if x['type'] not in ('end', 'error_end')],
             [f for f in FLOWS if f['to'] not in ('done', 'failed')], 'нет конечного'),
            (STEPS + [dict(key='orphan', type='task', name='сирота')], FLOWS, 'недостижим'),
            (STEPS, [f for f in FLOWS if f['to'] != 'wait'] + [{'from': 'paid', 'to': 'wait', 'kind': 'conditional'}],
             'без условия'),
            ([dict(x, refs=['ТЗ-1']) if x['key'] == 'ship' else x for x in STEPS], FLOWS, 'BR/FR'),
            (STEPS, FLOWS + [{'from': 'receive', 'to': 'paid', 'kind': 'async_message'}, {'from': 'start', 'to': 'ship', 'kind': 'async_message'}],
             'не от шага отправки'),
        ]
        for steps, flows, msg in bad:
            with self.assertRaisesRegex(ValueError, msg):
                p.define('shop', 'x', 'x', [dict(x) for x in steps], [dict(f) for f in flows], p.source('doc', 'd', 'r'))

    def test_alternatives_loops_parallel(self):                           # TTN-116, NET-AC-085, 086
        r = order()
        res = p.paths(r)
        self.assertFalse(res['truncated']); self.assertFalse(res['observed'])
        outcomes = {x['outcome'] for x in res['paths']}
        self.assertEqual(outcomes, {'end', 'error_end'})                  # успех и отказ — разные пути
        self.assertGreater(len(res['paths']), 2)
        for x in res['paths']:
            self.assertEqual(len(x['transitions']), len(x['steps']) - 1)
        looped = [x for x in res['paths'] if x['steps'].count('paid') == 2]
        self.assertTrue(looped)                                           # цикл пройден один раз и не бесконечно
        self.assertTrue(all(x['steps'].count('paid') <= 2 for x in res['paths']))
        par = [x for x in res['paths'] if x['parallel']]
        self.assertEqual(par[0]['parallel'][0]['branches'], ['reserve', 'notify'])
        self.assertTrue(p.paths(r, max_paths=1)['truncated'])

    def test_async_and_compensation(self):                                # TTN-117, NET-AC-095
        r = order()
        kinds = {(f['from'], f['to']): f['kind'] for f in r['flows']}
        self.assertEqual(kinds[('notify', 'join')], 'async_message')
        self.assertEqual(kinds[('wait', 'failed')], 'timeout')
        self.assertEqual(kinds[('reserve', 'cancel')], 'compensation')
        job = next(n for q, n in self.n.items() if n['kind'] == 'job' and q.startswith('app.cli.cmd_serve'))
        mp = p.mapping(r, 'notify', job, 'doc', 'docs/SRS.md', SHA)
        chain = p.trace(r, 'notify', [mp], self.idx())[0]
        self.assertEqual([h['type'] for h in chain['hops']], ['realizes_process_step'])   # не handles_route, не HTTP
        self.assertEqual(chain['broken']['reason'], 'no_runtime_mapping')


class Approval(World):
    def test_llm_proposal_never_approved_without_confirmation(self):      # TTN-115, NET-AC-089
        r = order(src=p.source('llm', note='сгенерировано'))
        self.assertEqual(r['state'], 'proposed')
        reg = p.Registry()
        reg.put(r, OWNER)
        with self.assertRaises(PermissionError):
            reg.approve(r['id'], 1, AGENT_SHOP, evidence='я агент')
        with self.assertRaisesRegex(ValueError, 'доказательством'):
            reg.approve(r['id'], 1, OWNER)
        with self.assertRaisesRegex(ValueError, 'без доказательства'):
            p.validate(dict(r, state='approved'))
        ok = reg.approve(r['id'], 1, OWNER, evidence='сверено с SRS r2, 2026-11-08')
        self.assertEqual((ok['state'], ok['approval']['by']), ('approved', 'owner'))
        nodoc = order(src=p.source('srs', 'docs/SRS.md'))
        reg.put(dict(nodoc, version=5, digest=p.digest(dict(nodoc, version=5))), OWNER)
        with self.assertRaisesRegex(ValueError, 'ревизии'):
            reg.approve(r['id'], 5, OWNER)
        with self.assertRaisesRegex(ValueError, 'неутверждённой'):
            reg.put(dict(order(version=9), state='approved'), OWNER)

    def test_versions_not_replaced_silently(self):                        # TTN-113, NET-AC-096
        reg = p.Registry()
        v1 = reg.put(order(), OWNER)
        other = order(goal='другая цель')
        with self.assertRaisesRegex(ValueError, 'новая версия'):
            reg.put(other, OWNER)
        reg.put(order(version=2, goal='другая цель'), OWNER)
        self.assertEqual(reg.get(v1['id'], OWNER)['version'], 2)
        old = reg.get(v1['id'], OWNER, version=1)
        self.assertEqual((old['goal'], old['digest']), (None, v1['digest']))
        self.assertEqual([x['version'] for x in reg.history(v1['id'], OWNER)], [1, 2])
        with self.assertRaisesRegex(ValueError, 'ниже'):
            reg.put(order(version=0, goal='третья'), OWNER)

    def test_acl_foreign_project_like_missing(self):                      # NET-AC-129
        reg = p.Registry()
        r = reg.put(order(), OWNER)
        with self.assertRaises(LookupError) as foreign:
            reg.get(r['id'], AGENT_BLOG)
        with self.assertRaises(LookupError) as missing:
            reg.get('process-0000000000000000', AGENT_BLOG)
        self.assertEqual(str(foreign.exception), str(missing.exception))
        self.assertEqual(reg.current(AGENT_BLOG), [])
        self.assertEqual(reg.get(r['id'], AGENT_SHOP)['id'], r['id'])
        with self.assertRaises(PermissionError):
            reg.put(order(version=3), AGENT_BLOG)
        self.assertEqual(reg.put(order(version=3, goal='x'), AGENT_SHOP)['state'], 'proposed')
        good = p.mapping(r, 'ship', self.n['app.web:GET /items'], 'doc', 'docs/SRS.md', SHA)
        with self.assertRaises(PermissionError):
            reg.map(good, AGENT_SHOP)                                     # подтверждённую ставит не агент
        with self.assertRaises(LookupError):
            reg.map(p.mapping(r, 'ship', self.n['app.web:GET /items'], 'inferred'), AGENT_BLOG)
        reg.map(good, OWNER)
        self.assertEqual(reg.mappings(AGENT_BLOG), [])
        self.assertEqual(len(reg.mappings(AGENT_SHOP)), 1)

    def test_registry_saved_and_loaded(self):
        tmp = tempfile.mkdtemp(prefix='tt-reg-')
        try:
            reg = p.Registry.load(tmp)
            r = reg.put(order(), OWNER); reg.save()
            again = p.Registry.load(tmp)
            self.assertEqual(again.get(r['id'], OWNER)['digest'], r['digest'])
            self.assertTrue(os.path.exists(os.path.join(tmp, 'arch', 'processes.json')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Trace(World):
    def test_manual_step_without_handler(self):                           # TTN-114, NET-AC-087
        r = order()
        with self.assertRaisesRegex(ValueError, 'ручной'):
            p.mapping(r, 'call', self.n['app.web.items'], 'owner')
        chain = p.trace(r, 'call', [], self.idx())[0]
        self.assertEqual((chain['broken']['reason'], chain['hops']), ('not_automated', []))
        self.assertEqual(p.trace(r, 'receive', [], self.idx())[0]['broken']['reason'], 'no_step_mapping')

    def test_full_chain_each_hop_typed(self):                             # TTN-118, 119, NET-AC-090, 092
        r = order()
        mp = p.mapping(r, 'ship', self.n['app.web:GET /items'], 'doc', 'docs/SRS.md', SHA)
        chain = p.trace(r, 'ship', [mp], self.idx())[0]
        self.assertTrue(chain['complete'])
        self.assertEqual([h['type'] for h in chain['hops']],
                         ['realizes_process_step', 'handles_route', 'deployed_as', 'exposes_endpoint'])
        self.assertEqual([h['layer'] for h in chain['hops']], ['logical', 'call', 'deployment', 'network'])
        self.assertEqual((chain['hops'][0]['file'], chain['hops'][1]['frm']), ('app/web.py', self.n['app.web.items']['id']))
        self.assertEqual(chain['hops'][-1]['port'], 8000)
        # Образ запущенного не сверен с SHA — путь полный, но не подтверждённый.
        self.assertFalse(chain['confirmed'])
        self.assertEqual([i['code'] for i in chain['issues']], ['no_runtime_image_identity'])
        ok = p.trace(r, 'ship', [mp], self.idx(artifact=dict(commit_sha=SHA, evidence='метка образа')))[0]
        self.assertTrue(ok['confirmed'])
        self.assertEqual(p.classify(ok), 'planned_process_path')

    def test_breaks_have_reason_codes(self):                              # TTN-120, NET-AC-093, 094
        r = order()
        route = self.n['app.web:GET /items']
        mp = p.mapping(r, 'ship', route, 'doc', 'docs/SRS.md', SHA)
        self.assertEqual(p.trace(r, 'ship', [mp], self.idx(with_link=False))[0]['broken']['reason'], 'no_runtime_mapping')
        self.assertEqual(p.trace(r, 'ship', [mp], self.idx(bind=False))[0]['broken']['reason'], 'no_port_binding')
        dec = p.trace(r, 'ship', [mp], self.idx(socket_state='not_observed'))[0]
        self.assertIn('port_not_observed', [i['code'] for i in dec['issues']])
        self.assertFalse(dec['confirmed'])                                # NET-AC-094
        gone = dict(mp, target='route-0000000000000000')
        self.assertEqual(p.trace(r, 'ship', [gone], self.idx())[0]['broken']['reason'], 'handler_not_in_graph')
        lam = p.mapping(r, 'ship', self.n['app.cli:sync'], 'doc', 'docs/SRS.md', SHA)
        self.assertEqual(p.trace(r, 'ship', [lam], self.idx())[0]['broken']['reason'], 'handler_dynamic_unresolved')
        old = p.mapping(r, 'ship', route, 'doc', 'docs/SRS.md', 'b' * 40)
        self.assertIn('mapping_revision_drift', [i['code'] for i in p.trace(r, 'ship', [old], self.idx())[0]['issues']])
        guess = p.mapping(r, 'ship', route, 'inferred')
        g_chain = p.trace(r, 'ship', [guess], self.idx())[0]
        self.assertEqual(g_chain['hops'][0]['quality'], 'inferred'); self.assertFalse(g_chain['confirmed'])

    def test_alternative_mappings_not_merged(self):
        r = order()
        a = p.mapping(r, 'ship', self.n['app.web:GET /items'], 'doc', 'docs/SRS.md', SHA)
        b = p.mapping(r, 'ship', self.n['app.cli:run'], 'owner')
        chains = p.trace(r, 'ship', [a, b], self.idx())
        self.assertEqual(len(chains), 2)
        self.assertEqual({c['mapping'] for c in chains}, {a['id'], b['id']})

    def test_planned_vs_static_never_observed(self):                      # TTN-121
        idx = self.idx()
        st = p.static_path(idx, self.n['app.web.items']['id'], self.n['app.core.helper']['id'])
        self.assertTrue(st['found']); self.assertFalse(st['observed'])
        self.assertEqual(p.classify(st), 'static_implementation_path')
        dyn = p.static_path(idx, self.n['app.dyn.reflect']['id'], self.n['app.core.helper']['id'])
        self.assertFalse(dyn['found']); self.assertEqual(len(dyn['dynamic_gaps']), 5)
        self.assertNotEqual(p.classify(dict(mode='planned_process_path')), 'observed_execution_path')


class Impact(World):
    def setUp(self):
        self.r = order()
        self.maps = [p.mapping(self.r, 'ship', self.n['app.web:GET /items'], 'doc', 'docs/SRS.md', SHA),
                     p.mapping(self.r, 'receive', self.n['app.core.Store.save'], 'owner')]

    def test_reverse_from_symbol_route_module_port(self):                 # TTN-122, NET-AC-091, 098
        idx = self.idx()
        items = p.impact(self.n['app.web.items']['id'], [self.r], self.maps, idx)
        self.assertEqual((items['verdict'], [x['step_key'] for x in items['direct']]), ('impacted', ['ship']))
        helper = p.impact(self.n['app.core.helper']['id'], [self.r], self.maps, idx)
        self.assertEqual(helper['verdict'], 'possible')
        self.assertEqual(sorted(x['step_key'] for x in helper['possible']), ['receive', 'ship'])
        self.assertTrue(helper['unknown_reasons'])                        # есть вызовы без доказанной цели
        mod = p.impact(self.n['app.core']['id'], [self.r], self.maps, idx)
        self.assertEqual([x['step_key'] for x in mod['direct']], ['receive'])
        _, _, c = self.runtime()
        port = next(o for o in idx.objs.values() if o['kind'] == 'binding')
        self.assertEqual([x['step_key'] for x in p.impact(port['id'], [self.r], self.maps, idx)['direct']], ['ship'])
        back = p.describe(self.n['app.web:GET /items']['id'], [self.r], self.maps)
        self.assertEqual((back['business'], back['steps'][0]['step']), ('mapped', 'ship'))

    def test_no_mapping_is_unknown_not_zero(self):                        # NET-AC-099, NET-AC-088
        idx = self.idx()
        res = p.impact(self.n['app.web.Handler.send_health']['id'], [self.r], self.maps, idx)
        self.assertEqual(res['verdict'], 'unknown')
        self.assertIn(self.n['app.web:GET /health']['id'], res['unmapped_entrypoints'])
        self.assertNotIn(res['verdict'], ('none', 'zero'))
        self.assertEqual(p.impact('binding-0000000000000000', [self.r], self.maps, idx)['verdict'], 'unknown')
        self.assertEqual(p.describe(self.n['app.web:GET /health']['id'], [self.r], self.maps)['business'], 'not_documented')


class Traceability(World):
    def test_requirements_and_drift(self):                                # TTN-123, NET-AC-097
        r = order()
        maps = [p.mapping(r, 'ship', self.n['app.web:GET /items'], 'doc', 'docs/SRS.md', 'b' * 40)]
        t = p.traceability(r, maps, requirements={'FR-1'})
        self.assertEqual((t['refs_by_kind'], t['refs_total']), ({'FR': 1, 'UC': 1}, 2))
        self.assertEqual(t['broken'], [dict(step='ship', ref='UC-ship')])
        self.assertIn('receive', t['steps_without_mapping']); self.assertNotIn('ship', t['steps_without_mapping'])
        self.assertEqual(t['manual_steps'], ['call'])
        d = p.drift(r, maps + [dict(maps[0], target='route-0000000000000000', step_key='x')], self.idx())
        self.assertEqual(d['state'], 'drift')
        self.assertEqual(sorted(i['kind'] for i in d['items']), ['revision_changed', 'target_missing'])


class RealTaskTreker(unittest.TestCase):
    """Конвейер задачи из docs/КОНВЕЙЕР.md на графе кода самого TaskTreker."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(HERE)
        cls.code = s.scan(root, 'tasktreker', run=git('c' * 40))
        cls.n = {n['qualname']: n for n in cls.code['nodes']}
        with open(PIPE, encoding='utf-8') as f:
            d = json.load(f)
        cls.proc = p.define(d['project'], d['key'], d['name'], d['steps'], d['flows'], p.source(**d['source']),
                            goal=d['goal'], owner_role=d['owner_role'], trigger=d['trigger'], outcome=d['outcome'],
                            refs=d['refs'])

    def test_real_process_paths_and_trace(self):
        res = p.paths(self.proc)
        self.assertFalse(res['truncated'])
        self.assertTrue(all(x['steps'][-1] == 'done' for x in res['paths']))
        self.assertTrue(all('owner_yes' in x['steps'] for x in res['paths']))   # main — только через «да» владельца
        self.assertGreaterEqual(len(res['paths']), 4)                         # возвраты анализа, ревью и QA
        idea_cli = p.mapping(self.proc, 'idea', self.n['tools.pm:idea'], 'doc', 'docs/КОНВЕЙЕР.md', 'c' * 40)
        idea_board = p.mapping(self.proc, 'idea', self.n['tools.serve:POST /api/work'], 'inferred')
        chains = {c['mapping']: c for c in p.trace(self.proc, 'idea', [idea_cli, idea_board], p.Index(self.code))}
        cli = chains[idea_cli['id']]
        self.assertEqual([h['type'] for h in cli['hops']], ['realizes_process_step', 'handles_command'])
        self.assertEqual(self.code['nodes'][[n['id'] for n in self.code['nodes']].index(cli['hops'][1]['frm'])]['qualname'],
                         'tools.pm.cmd_idea')
        self.assertEqual(cli['broken']['reason'], 'no_runtime_mapping')          # CLI не служба — честный разрыв
        board = chains[idea_board['id']]
        self.assertEqual([i['code'] for i in board['issues']], ['mapping_not_confirmed'])
        self.assertEqual(board['hops'][1]['handler_kind'], 'inline')
        self.assertEqual(p.trace(self.proc, 'owner_yes', [], p.Index(self.code))[0]['broken']['reason'], 'not_automated')
        imp = p.impact(self.n['tools.pm.cmd_idea']['id'], [self.proc], [idea_cli], p.Index(self.code))
        self.assertEqual([x['step_key'] for x in imp['direct']], ['idea'])
        t = p.traceability(self.proc, [idea_cli])
        self.assertEqual(t['source_revision'], '88540649c00d2d0f213dfb2e111174314caead08')
        self.assertIn('qa', t['steps_without_mapping'])


if __name__ == '__main__':
    unittest.main()
