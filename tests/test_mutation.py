"""Мутационная проверка тестов (07: A6-08, AG-QA-06, E2E-R14)."""
import sys
import tempfile
import unittest
from pathlib import Path

import base  # noqa: F401 — путь к tools/
import mutation

CODE = ('def discount(total):\n    if total >= 100:\n        return total * 9 // 10\n    return total\n\n\n'
        'def allowed(user, admin):\n    return user and not admin\n')
STRONG = ('import unittest\nfrom shop import discount, allowed\n\n\nclass T(unittest.TestCase):\n'
          '    def test_discount(self):\n        self.assertEqual(discount(100), 90)\n        self.assertEqual(discount(99), 99)\n'
          '        self.assertEqual(discount(200), 180)\n'
          '    def test_allowed(self):\n        self.assertTrue(allowed(True, False))\n        self.assertFalse(allowed(False, False))\n'
          '        self.assertFalse(allowed(True, True))\n')
WEAK = ('import unittest\nfrom shop import discount, allowed\n\n\nclass T(unittest.TestCase):\n'
        '    def test_runs(self):\n        discount(500); allowed(True, False)\n')
CMD = [sys.executable, '-I', '-m', 'unittest', '-q']


class MutationTests(unittest.TestCase):
    def project(self, test):
        d = Path(tempfile.mkdtemp(prefix='tt-mut-src-'))
        (d / 'shop.py').write_text(CODE, encoding='utf-8')
        (d / 'test_shop.py').write_text(test, encoding='utf-8')
        return d

    def test_mutants_are_deterministic_and_change_semantics(self):
        a, b = mutation.mutants(CODE), mutation.mutants(CODE)
        self.assertEqual(a, b)
        self.assertTrue(a)
        descs = {d for _, d, _ in a}
        self.assertIn('>= → >', descs); self.assertIn('and → or', descs)
        self.assertTrue(all(src != CODE for _, _, src in a))
        self.assertEqual(mutation.mutants('x = (\n'), [])                            # синтаксическая ошибка — без мутантов

    def test_strong_tests_kill_mutants_weak_tests_do_not(self):
        strong = mutation.run(self.project(STRONG), ['shop.py', 'test_shop.py'], CMD)
        self.assertEqual(strong['status'], 'passed', strong)
        self.assertGreaterEqual(strong['score'], mutation.THRESHOLD)
        weak = mutation.run(self.project(WEAK), ['shop.py'], CMD)
        self.assertEqual(weak['status'], 'failed')
        self.assertTrue(weak['survivors'])
        self.assertIn('shop.py:', mutation.summary(weak))                            # выживший назван по месту

    def test_not_run_and_fail_closed_cases(self):
        src = self.project(STRONG)
        self.assertEqual(mutation.run(src, ['README.md', 'test_shop.py'], CMD)['status'], 'not_run')
        self.assertEqual(mutation.run(src, ['shop.py'], None)['status'], 'failed')   # нечем проверять — не зелёный
        (src / 'test_shop.py').write_text(STRONG.replace('90)', '91)'), encoding='utf-8')
        red = mutation.run(src, ['shop.py'], CMD)
        self.assertEqual(red['status'], 'failed'); self.assertIn('красные', red['reason'])
        self.assertEqual((src / 'shop.py').read_text(encoding='utf-8'), CODE)          # исходник не тронут


if __name__ == '__main__':
    unittest.main()
