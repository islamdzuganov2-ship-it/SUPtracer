# -*- coding: utf-8 -*-
"""Модель «Архитектуры систем» (TT-133: TTN-011, TTN-012, TTN-091…093; NET-AC-001…005, 009, 010, 012, 032, 061)."""
import unittest
from datetime import timedelta

from base import TOOLS  # noqa: F401
import arch_model as m


class Identity(unittest.TestCase):
    def test_stable_ids_survive_rescan(self):                             # NET-AC-032, TTN-093
        ev1 = m.evidence('observed_socket', 'windows:tcp', 'c')
        ev2 = m.evidence('observed_socket', 'windows:tcp', 'c', observed_at=m.now() + timedelta(hours=3))
        self.assertEqual(ev1['id'], ev2['id'])
        a = m.binding('local', 'host', 'tcp', '127.0.0.1', 8080, ev1['id'])
        b = m.binding('local', 'host', 'TCP', '127.0.0.1', '8080', ev2['id'])
        self.assertEqual(a['id'], b['id'])
        self.assertTrue(a['id'].startswith('binding-'))

    def test_bind_key_separates_protocol_family_host_and_namespace(self):  # NET-AC-004, 005, §3.2
        ev = 'e'
        ids = {m.binding('local', 'host', 'tcp', '127.0.0.1', 8080, ev)['id'],
               m.binding('local', 'host', 'udp', '127.0.0.1', 8080, ev)['id'],
               m.binding('local', 'host', 'tcp', '::1', 8080, ev)['id'],
               m.binding('local', 'docker:d1', 'tcp', '127.0.0.1', 8080, ev)['id'],
               m.binding('vps', 'host', 'tcp', '127.0.0.1', 8080, ev)['id']}
        self.assertEqual(len(ids), 5)

    def test_pid_without_start_time_is_not_identity(self):                # NET-AC-006
        a = m.process('local', 4100, created='2026-11-08T10:00:00Z')
        b = m.process('local', 4100, created='2026-11-08T12:00:00Z')
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(m.process('local', 4100)['identity'], 'unknown')

    def test_snapshot_dedupes_and_reports_partial(self):                  # NET-AC-061, NET-AC-060
        ev = m.evidence('observed_socket', 'windows:tcp', 'c')
        b = m.binding('local', 'host', 'tcp', '127.0.0.1', 1, ev['id'])
        snap = m.snapshot([ev, b, dict(b), ev], [m.source('windows:tcp', 'available'),
                                                  m.source('docker:engine', 'unavailable', 'демон не запущен')])
        self.assertEqual(len(snap['objects']), 2)
        self.assertFalse(snap['complete'])
        self.assertIn('docker:engine: демон не запущен', snap['partial_reasons'])


class Axes(unittest.TestCase):
    def test_no_single_status_field(self):                                # TTN-012, §3.1
        b = m.binding('local', 'host', 'tcp', '0.0.0.0', 80, 'e')
        for axis in ('socket_state', 'configuration_state', 'reachability', 'evidence_quality', 'freshness'):
            self.assertIn(axis, b)
        with self.assertRaisesRegex(ValueError, 'status запрещено'):
            m.validate(dict(b, status='up'))
        with self.assertRaisesRegex(ValueError, 'ось'):
            m.validate(dict(b, socket_state='open'))

    def test_observed_socket_needs_verified_evidence_and_reach_needs_vantage(self):
        b = m.binding('local', 'host', 'tcp', '0.0.0.0', 80, 'e', quality='declared')
        with self.assertRaisesRegex(ValueError, 'проверенного'):
            m.validate(b)
        with self.assertRaisesRegex(ValueError, 'точки наблюдения'):
            m.validate(dict(m.binding('local', 'host', 'tcp', '0.0.0.0', 80, 'e'), reachability='reachable_verified'))

    def test_llm_inference_is_never_verified(self):                       # ARCH-FR-14.3
        self.assertEqual(m.evidence('inferred', 'llm', 'c')['quality'], 'inferred')
        with self.assertRaisesRegex(ValueError, 'inferred'):
            m.evidence('inferred', 'llm', 'c', quality='verified')

    def test_reservation_is_not_a_socket(self):                           # NET-AC-009, 010, 012
        for rtype in ('os_excluded_range', 'http_urlacl', 'http_sslcert', 'portproxy'):
            r = m.validate(m.reservation('local', rtype, 'e', protocol='tcp', start=80, end=80))
            self.assertNotIn('socket_state', r)
        p = m.reservation('local', 'project', 'e', protocol='tcp', start=8000, end=8010, owner_project='shop',
                          reason='план', expires='2026-12-01')
        self.assertEqual(p['evidence_quality'], 'declared')
        self.assertEqual(p['reservation_type'], 'project')            # не os_excluded_range

    def test_freshness_by_evidence_expiry(self):                          # ARCH-FR-14.5
        old = m.evidence('observed_socket', 's', 'c', observed_at=m.now() - timedelta(hours=2))
        b = m.binding('local', 'host', 'tcp', '127.0.0.1', 1, old['id'])
        self.assertEqual(m.stale(b, {old['id']: old}), 'stale')
        manual = m.evidence('user_confirmed', 'owner', 'c', observed_at=m.now() - timedelta(days=400))
        self.assertEqual(m.stale(dict(b, evidence_refs=[manual['id']]), {manual['id']: manual}), 'current')


class Addresses(unittest.TestCase):
    def test_loopback_wildcard_ipv6(self):                                # NET-AC-001…003
        self.assertEqual(m.scope('127.0.0.1'), 'loopback')
        self.assertEqual(m.scope('::'), 'wildcard'); self.assertEqual(m.scope('0.0.0.0'), 'wildcard')
        self.assertIsNone(m.url('http', '0.0.0.0', 8000))
        self.assertIsNone(m.url('http', '::', 8000))
        self.assertEqual(m.url('http', '::1', 8000, '/x'), 'http://[::1]:8000/x')
        self.assertEqual(m.url('http', '127.0.0.1', 8787), 'http://127.0.0.1:8787')
        self.assertIsNone(m.url('tcp', '127.0.0.1', 5432))                       # схему не угадываем

    def test_conflicts(self):                                             # NET-AC-004, NET-FR-03.5
        tcp = m.binding('declared', 'compose:a/api', 'tcp', '0.0.0.0', 8080, 'e', socket_state='not_observed',
                        configuration_state='configured', quality='declared', owner='a')
        udp = dict(m.binding('declared', 'compose:b/api', 'udp', '0.0.0.0', 8080, 'e', socket_state='not_observed',
                             configuration_state='configured', quality='declared', owner='b'))
        other = m.binding('declared', 'compose:b/web', 'tcp', '127.0.0.1', 8080, 'e', socket_state='not_observed',
                          configuration_state='configured', quality='declared', owner='b')
        self.assertEqual(m.conflicts([tcp, udp]), [])
        c = m.conflicts([tcp, udp, other])
        self.assertEqual(len(c), 1); self.assertEqual(c[0]['kind'], 'conflict_suspected')
        # Два одновременно слушающих сокета уже совместимы — конфликта нет.
        l1 = m.binding('local', 'host', 'tcp', '0.0.0.0', 445, 'e', owner='p1')
        l2 = m.binding('local', 'host', 'tcp', '192.0.2.1', 445, 'e', owner='p2')
        self.assertEqual(m.conflicts([l1, l2]), [])

    def test_manual_mapping_survives_new_scan(self):                      # NET-FR-01.5, §5.3
        c = m.container('d1', 'abc', 'x', 'running')
        out = m.merge_mapping(c, {c['id']: dict(project='shop', by='owner', at='2026-11-08')})
        self.assertEqual((out['project'], out['project_method']), ('shop', 'user_confirmed'))
        self.assertEqual(m.merge_mapping(c, {})['project_method'], 'unassigned')


if __name__ == '__main__':
    unittest.main()
