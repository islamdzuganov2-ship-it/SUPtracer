import importlib
import json
from pathlib import Path
from base import DataCase


class ProviderProjects(DataCase):
    def test_gpt_only_forecast_uses_comparable_provider_history(self):
        self.projects()
        import estimate
        e=importlib.reload(estimate)
        def usage(tokens, usd, provider):
            return dict(billable=tokens,usd=usd,output=1000,by_provider={provider:{'billable':tokens}})
        self.jwrite('usage.json',{'projects':{'alpha-api':usage(1_000_000,10,'codex'),
            'beta-tool':usage(100_000_000,1000,'claude'),'gamma-voice':usage(1000,.01,'codex')}})
        self.jwrite('repos.json',{'repos':{'alpha-api':{'git':{'commits':10}},'beta-tool':{'git':{'commits':10}}}})
        self.quiet(e.main)
        out=self.jread('estimates.json')['projects']['gamma-voice']
        self.assertEqual(out['tokens_per_commit'],100000)
        self.assertEqual(out['usd_per_commit'],1.)
        self.assertIn('GPT / Codex',out['rate_basis'])

    def test_authored_static_sources_opt_in_does_not_include_bundles_elsewhere(self):
        p=self.projects()['projects']['alpha-api']
        root=Path(p['path']);(root/'dist').mkdir();(root/'node_modules').mkdir()
        (root/'dist'/'index.html').write_text('<html>\n<body>\n</body>\n</html>\n')
        (root/'dist'/'app.mjs').write_text('export const x = 1;\n')
        (root/'node_modules'/'bundle.js').write_text('ignore me\n')
        import collect_repos
        cr=importlib.reload(collect_repos)
        normal=cr.scan_repo('alpha-api',p)
        authored=cr.scan_repo('alpha-api',dict(p,web_sources=True,scan_include_dirs=['dist']))
        self.assertEqual(normal['code']['loc_total'],0)
        self.assertEqual(authored['code']['loc_total'],7)
        self.assertEqual(authored['code']['files_total'],2)

    def test_modern_javascript_tests_at_repository_root_are_counted(self):
        p=self.projects()['projects']['alpha-api']
        tests=Path(p['path'])/'tests';tests.mkdir()
        (tests/'chronology.test.mjs').write_text('import {test} from "node:test";\n')
        import collect_repos
        cr=importlib.reload(collect_repos)
        self.assertEqual(cr.scan_repo('alpha-api',p)['code']['test_files'],1)
