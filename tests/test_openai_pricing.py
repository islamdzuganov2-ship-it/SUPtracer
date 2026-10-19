import json
import os
from base import DataCase
from pricing import openai_cost_usd, openai_price_for
from codex_usage import read_records


class OpenAIPricing(DataCase):
    def test_standard_input_cache_writes_and_reasoning_in_output(self):
        self.assertAlmostEqual(openai_cost_usd('gpt-6.1-sol', 1_000_000, 1_000_000, 1_000_000, 1_000_000), 14.6)
        self.assertAlmostEqual(openai_cost_usd('gpt-5.5', 1_000_000, 1_000_000, 1_000_000), 35.5)
        self.assertAlmostEqual(openai_cost_usd('gpt-6-astra', 1_000_000, 1_000_000, 1_000_000), 61.)

    def test_long_context_is_per_request_not_cumulative_thread_size(self):
        short = openai_cost_usd('gpt-6.1-sol', 100, 100, request_input=272000)
        long = openai_cost_usd('gpt-6.1-sol', 100, 100, request_input=272001)
        self.assertAlmostEqual(short, .0012)
        self.assertAlmostEqual(long, .0019)

    def test_snapshot_matches_but_unknown_pro_and_internal_aliases_are_not_guessed(self):
        self.assertEqual(openai_price_for('gpt-5.5-2026-04-23'), openai_price_for('gpt-5.5'))
        for model in ('gpt-5.5-pro', 'codex-auto-review', 'future-model'):
            self.assertIsNone(openai_cost_usd(model, 100))

    def test_modern_response_and_legacy_event_are_merged_before_pricing(self):
        rows = [dict(type='session_meta',payload={'id':'s','cwd':'C:/projects/alpha-api'}),
                dict(type='turn_context',payload={'model':'gpt-6.1-sol'})]
        for i in range(1,3):
            totals={'input_tokens':100*i,'cached_input_tokens':20*i,'output_tokens':10*i,'reasoning_output_tokens':5*i}
            stamp='2026-10-14T10:0%d:00Z'%i
            rows.append(dict(type='event_msg',timestamp=stamp,payload={'type':'token_count','info':{'total_token_usage':totals,'last_token_usage':{'input_tokens':100}}}))
            modern=dict(totals,cache_write_input_tokens=30*i)
            rows.append(dict(type='token_usage_record',timestamp=stamp,payload={'thread_token_usage':modern,'usage':{'input_tokens':100,'cache_write_input_tokens':30}}))
        path=os.path.join(self.tmp,'sessions','s.jsonl');os.makedirs(os.path.dirname(path))
        with open(path,'w') as f:
            for row in rows:f.write(json.dumps(row)+'\n')
        records,_,_,_=read_records(self.tmp)
        self.assertEqual(len(records),2)
        self.assertEqual(sum(r['input'] for r in records),100)
        self.assertEqual(sum(r['cache_write'] for r in records),60)
        self.assertEqual(sum(r['output'] for r in records),20)
        self.assertAlmostEqual(sum(r['usd'] for r in records),.000554)
        self.assertEqual(sum(r['unpriced_tokens'] for r in records),0)
