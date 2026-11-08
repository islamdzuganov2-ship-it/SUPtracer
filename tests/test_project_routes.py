import copy
import importlib
from pathlib import Path
from base import DataCase
from project_routes import paths, resolve_path, resolve_slug, conflicts, slug


class ProjectRoutes(DataCase):
    def setUp(self):
        super().setUp()
        self.p = {
            'crm-core': {'path':'C:/Users/u/projects/crm-core'},
            'crm-core_site': {'path':'C:/Users/u/projects/CRM-CORE_site/site-fintech',
                             'usage_paths':['C:/Users/u/projects/CRM-CORE_site']},
            'crm-core_client': {'path':'C:/Users/u/projects/CRM-CORE_for Client',
                              'usage_paths':['C:/Users/u/projects/crm-core_client']},
        }

    def test_shared_routes_keep_three_products_and_worktrees_separate(self):
        import collect_usage
        c=importlib.reload(collect_usage)
        for key, root in [('crm-core','crm-core'),('crm-core_site','CRM-CORE_site'),('crm-core_client','CRM-CORE_for Client')]:
            for cwd in ['C:/Users/u/projects/'+root+'/frontend',
                        'C:/Users/u/projects/'+root+'.worktrees/feature',
                        'C:/Users/u/.codex/worktrees/abc/'+root+'/backend']:
                self.assertEqual(resolve_path(cwd,paths(self.p))['project'],key)
                self.assertEqual(c.project_key(cwd,paths(self.p)),key)
                self.assertEqual(self.t.context.project_for_path(cwd,self.p),key)
            self.assertEqual(resolve_slug(slug('C:/Users/u/projects/'+root),self.p),key)
            self.assertEqual(self.t.automem.project_for_slug(slug('C:/Users/u/projects/'+root)+'--claude-worktrees-fix',self.p),key)
        self.assertIsNone(resolve_slug(slug('C:/Users/u/projects/crm-core_unknown'),self.p))

    def test_conflicting_owners_and_worktree_basenames_are_not_guessed(self):
        p=copy.deepcopy(self.p);p['other']={'path':p['crm-core']['path']}
        self.assertTrue(conflicts(p))
        self.assertIsNone(resolve_path(p['crm-core']['path'],paths(p))['project'])
        p['other']['path']='D:/backup/crm-core'
        self.assertIsNone(resolve_path('C:/Users/u/.codex/worktrees/x/crm-core',paths(p))['project'])

    def test_missing_cwd_uses_exact_source_slug_for_each_product(self):
        import collect_usage
        c=importlib.reload(collect_usage)
        records=[dict(cwd='',source_slug=slug(p['path']),model='test',timestamp='2026-10-14T10:00:00Z',
                      session=key,input=10,output=2,requests=1,usd=.1) for key,p in self.p.items()]
        report=c.aggregate(records,'claude',{},'',True,paths(self.p))
        self.assertEqual(set(report['projects']),set(self.p))
        self.assertEqual(report['total']['tokens'],36)

    def test_colliding_claude_encoding_does_not_fall_back_to_one_owner(self):
        import collect_usage
        c=importlib.reload(collect_usage)
        projects={'alpha':{'path':'C:/Work/crm-core'},'beta':{'path':'C:/Work/crm_core'}}
        rec=dict(cwd='',source_slug=slug('C:/Work/crm-core'),fallback_key='alpha',model='test',
                 timestamp='2026-10-14T10:00:00Z',session='s',input=100,output=2,usd=.1)
        self.assertIsNone(resolve_slug(rec['source_slug'],projects))
        report=c.aggregate([rec],'claude',{},'',True,paths(projects))
        self.assertEqual(set(report['projects']),{'unassigned:ambiguous'})
        self.assertEqual(report['total']['tokens'],102)

    def test_nested_explicit_path_has_priority_and_similar_sibling_is_not_matched(self):
        p=copy.deepcopy(self.p)
        p['crm-core_site']['usage_paths'].append('C:/Users/u/projects/crm-core/docs/public-site')
        self.assertEqual(resolve_path('C:/Users/u/projects/crm-core/docs/public-site/src',paths(p))['project'],'crm-core_site')
        self.assertIsNone(resolve_path('C:/Users/u/projects/crm-core-extra',paths(p))['project'])

    def test_claude_rerouting_preserves_ids_and_manual_text(self):
        doc=self.projects();p=doc['projects'];p['alpha-api']['usage_paths']=['C:/Work/Client']
        root=Path(self.tmp)/'claude';directory=root/slug('C:/Work/Client')/'memory';directory.mkdir(parents=True)
        (directory/'note.md').write_text('---\ndescription: Факт\ntype: project\n---\nИсходное содержание\n',encoding='utf-8')
        self.t.automem.run(str(root),projects=p,quiet=True)
        old=self.t.memory.load_all();record=next(r for r in old if r['kind']!='источник')
        record['body']='Ручное дополнение';self.t.memory.save(record)
        del p['alpha-api']['usage_paths'];p['beta-tool']['usage_paths']=['C:/Work/Client']
        self.t.automem.run(str(root),projects=p,quiet=True)
        after=self.t.memory.get(record['id'])
        self.assertEqual(after['project'],'beta-tool');self.assertEqual(after['body'],'Ручное дополнение')
        self.assertEqual({r['id'] for r in self.t.memory.load_all()}, {r['id'] for r in old})

    def test_gap_registry_keeps_implemented_mechanisms_open_until_acceptance(self):
        from project_activity import document_tasks
        rows=document_tasks('| Q-01 | P0 | implemented catalogue | Внешняя приёмка |\n| R-01 | P0 | verify | Проверить область |\n')
        self.assertEqual([r['status'] for r in rows],['in_progress','blocked'])

    def test_rerouting_conserves_exact_accounting_events(self):
        from review_routes import audit
        old=copy.deepcopy(self.p);old.pop('crm-core_client');old['crm-core']['usage_paths']=['C:/Users/u/projects/CRM-CORE_for Client']
        records=[dict(cwd='C:/Users/u/projects/CRM-CORE_for Client',model='test',session='s',timestamp='2026-10-14T10:00:00Z',input=100,output=20,cache_read=30,usd=.2,requests=1)]
        report=audit(old,self.p,{'codex':records})['providers']['codex']
        self.assertTrue(report['conserved']);self.assertEqual(report['moves'][0]['project'],'crm-core_client')
