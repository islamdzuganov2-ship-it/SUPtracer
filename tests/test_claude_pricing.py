import unittest
import base  # noqa: F401  путь к tools
from pricing import price_for, cost_usd


class ClaudePricingTests(unittest.TestCase):
    def test_current_generation_not_billed_as_predecessor(self):
        self.assertEqual(price_for('claude-opus-5-5')['in'], 4.0)
        self.assertEqual(price_for('claude-opus-5')['in'], 5.0)
        self.assertEqual(price_for('claude-haiku-5-5')['out'], 0.5)
        self.assertEqual(price_for('claude-sonnet-5-5')['in'], 2.0)

    def test_longest_prefix_wins_for_suffixed_ids(self):
        self.assertIs(price_for('claude-opus-5-5[1m]'), price_for('claude-opus-5-5'))

    def test_opus_55_cache_read(self):
        self.assertAlmostEqual(cost_usd('claude-opus-5-5', cread=1_000_000), 0.20)


if __name__ == '__main__':
    unittest.main()
