"""Состояние доказательств выпуска: PASS, STALE, FAILED, NOT_RUN — без подмены зелёным (07: A11-07, E2E-R39)."""
import base  # noqa: F401 — путь к tools/
import unittest
import release_bundle as rb


class EvidenceStateTests(unittest.TestCase):
    def ev(self, status, sha='abcdef1234567'):
        return dict(evidence=[dict(check_id='release_evidence', status=status, commit_sha=sha, source='server',
                                   completed_at='2026-11-08T07:00:00+00:00')])

    def test_states(self):
        self.assertEqual(rb.evidence_state({}, 'abcdef1')['state'], 'NOT_RUN')
        self.assertEqual(rb.evidence_state(self.ev('failed'), 'abcdef1')['state'], 'FAILED')
        self.assertEqual(rb.evidence_state(self.ev('passed'), 'abcdef1234567')['state'], 'PASS')
        st = rb.evidence_state(self.ev('passed'), '9999999000')
        self.assertEqual(st['state'], 'STALE')                       # тестовая ветка ушла вперёд — доказательство устарело
        self.assertEqual((st['source'], st['at']), ('server', '2026-11-08T07:00:00+00:00'))

    def test_digest_covers_every_field(self):
        b = dict(version=rb.VERSION, ref='p/T-1', x=[1, 2]); b['digest'] = rb.digest(b)
        self.assertEqual(rb.digest(b), b['digest'])
        b['x'].append(3)
        self.assertNotEqual(rb.digest(b), b['digest'])


if __name__ == '__main__':
    unittest.main()
