import importlib
from pathlib import Path
from base import DataCase


class ProjectActivity(DataCase):
    def setUp(self):
        super().setUp()
        import project_activity
        self.a = importlib.reload(project_activity)

    def test_full_plan_partial_status_and_manual_backlog_preserved(self):
        doc = self.projects(lambda d: d['projects']['alpha-api'].update(tracking_documents=['BACKLOG.md']))
        path = Path(doc['projects']['alpha-api']['path'])/'BACKLOG.md'
        path.write_text('### API-01. Реестр\n\n**P0 · Частично · Зависимости: нет.**\n\n### API-02. Импорт\n\n**P1 · План · Зависимости: API-01.**\n', encoding='utf-8')
        before = (Path(self.data)/'projects.json').read_bytes()
        self.jwrite('codex_sessions.json', {'sessions':[{'id':'s','projects':['alpha-api']}]})
        report = self.a.build()['projects']['alpha-api']
        self.assertEqual(report['codex_sessions'],1)
        self.assertEqual([(t['id'],t['status']) for t in report['documents'][0]['tasks']], [('API-01','in_progress'),('API-02','queued')])
        self.assertEqual(before,(Path(self.data)/'projects.json').read_bytes())
        path.write_text('| ID | Задача | Приоритет | Статус |\n| API-01 | Реестр | P0 | Готово |\n',encoding='utf-8')
        self.assertEqual(self.a.build()['projects']['alpha-api']['documents'][0]['tasks'][0]['status'],'done')

    def test_missing_document_does_not_erase_manual_data(self):
        self.projects(lambda d: d['projects']['alpha-api'].update(tracking_documents=['missing.md']))
        report = self.a.build()['projects']['alpha-api']
        self.assertTrue(report['documents'][0]['missing'])

    def test_checked_tasks_are_distinguished(self):
        tasks = self.a.document_tasks('- [x] Сделано\n- [ ] Осталось\n')
        self.assertEqual([t['status'] for t in tasks],['done','queued'])
