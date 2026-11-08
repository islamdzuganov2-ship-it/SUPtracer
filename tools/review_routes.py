"""Audit ownership changes against the same frozen in-memory accounting events."""
import argparse
import json
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone

import collect_usage as usage
from project_routes import paths, resolve_slug_entries


def audit(before, after, sources):
    result = {'schema':1,'generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),'providers':{}}
    for provider, records in sources.items():
        old_paths, new_paths = paths(before), paths(after)
        old = usage.aggregate(records, provider, {}, '', True, old_paths)
        new = usage.aggregate(records, provider, {}, '', True, new_paths)
        counters = ('input','output','cache_read','cache_write','billable')
        old_totals = {k:sum(p.get(k,0) for p in old['projects'].values()) for k in counters}
        new_totals = {k:sum(p.get(k,0) for p in new['projects'].values()) for k in counters}
        conserved = old_totals == new_totals
        if not conserved:
            raise ValueError('Counters changed during routing: '+provider)
        moved = defaultdict(lambda: {'records':0,'tokens':0})
        for rec in records:
            def key(entries):
                return (usage.project_key(rec['cwd'], entries) if rec['cwd'] else
                        resolve_slug_entries(rec.get('source_slug',''),entries,'unassigned:ambiguous')) or rec.get('fallback_key') or 'unassigned:'+provider
            a,b = key(old_paths),key(new_paths)
            if a != b:
                item = moved[(rec.get('cwd') or rec.get('source_slug',''),a,b)]
                item['records'] += 1
                item['tokens'] += rec.get('input',0)+rec.get('output',0)+rec.get('cache_read',0)+rec.get('cache_write',0)
        result['providers'][provider] = {'conserved':conserved,
            'counters':new_totals,
            'cost_rounding_difference':round(new['total']['usd']-old['total']['usd'],2),
            'moves':[dict(source=s,previous=a,project=b,**v) for (s,a,b),v in sorted(moved.items())]}
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    before=json.loads(Path(args.before).read_text(encoding='utf-8'))['projects']
    after=json.loads((Path(usage.DATA)/'projects.json').read_text(encoding='utf-8'))['projects']
    report=audit(before,after,{'claude':usage.read_claude_records()[0],'codex':usage.read_codex_records()[0]})
    target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    for name,r in report['providers'].items():
        print('%s: counters preserved; %d sources rerouted; cost rounding delta %.2f USD'%(name,len(r['moves']),r['cost_rounding_difference']))
