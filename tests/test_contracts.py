"""Инвалидация по контрактам группы (07: A5-06, AG-SA-08, E2E-R30)."""
from pathlib import Path

from test_pipeline import PipelineTests, git      # первым: base ставит путь к tools/
import pipeline as P
import workflow as w


def api_doc(version):
    text = '# api.md\n\n## Контракт\nGET /export?format=csv — версия %s, поля: id, title, status.\n\n## Ошибки\n' % version
    text += '400 — неизвестный формат; 404 — нет проекта; ответы JSON {code, message}.\n'
    text += '\n## Версия\nКонтракт %s.\n\n## Совместимость\nНовые поля только добавляются, старые клиенты работают.\n' % version
    return text + 'Подробности контракта и примеры запросов и ответов для интеграции.\n' * 6


class ContractTests(PipelineTests):
    def analyse(self, version):
        """Идея и анализ задачи группы «export» с контрактом указанной версии, до ворот анализа включительно."""
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'),
                     contract_group='export')
        api = P.art_path(self.task()['id'], P.ART + '/api.md')
        self.do_role('system_analyst', 'analysis', extra=[(api, api_doc(version))])
        for role in ('solution_architect', 'qa'):
            self.do_role(role, 'analysis')
        if 'analysis' not in self.task()['pipeline']['gates']:
            gate = self.claim('teamlead', gate='analysis')
            w.action(dict(op='gate', ref=self.ref, run=gate, worker='w-' + gate, decision='approve',
                          notes='Анализ согласован, можно разрабатывать по контракту'))
        return self.ref

    def test_changed_contract_stops_dependent_task_until_reanalysis(self):
        a = self.analyse('v1')
        ta = w.task_for(a)
        self.assertEqual(ta['pipeline']['stage'], 'development')
        seen = ta['pipeline']['contract_seen']['digest']
        b = self.analyse('v2')                                               # другая задача группы меняет контракт
        self.assertNotEqual(w.task_for(b)['pipeline']['contract_seen']['digest'], seen)
        st = w.task_for(a)['pipeline']['contract_stale']
        self.assertEqual((st['by'], st['seen']), (b, seen))
        self.assertIn('contract_stale', {e['kind'] for e in w.task_for(a)['pipeline']['journal']})
        # Разработчик задачи A не сдаст этап по старому контракту — причина и выход названы.
        self.ref = a
        r = self.do_role('developer', 'development', extra=[('app.py', 'x = 3\n'), ('tests/test_app.py', 'assert True\n')])
        self.assertFalse(r['accepted'])
        f = next(x for x in r['failed'] if x['id'] == 'dev-contract')
        self.assertIn(b, f['detail']); self.assertIn('to_stage=analysis', f['detail'])

    def test_same_contract_does_not_invalidate(self):
        a = self.analyse('v1')
        self.analyse('v1')                                                   # тот же контракт — ничего не устаревает
        self.assertNotIn('contract_stale', w.task_for(a)['pipeline'])
        self.assertEqual(P.contract_problem(w.task_for(a)['pipeline']), None)


for _n in [n for n in dir(PipelineTests) if n.startswith('test_') and n not in ContractTests.__dict__]:
    setattr(ContractTests, _n, None)
del PipelineTests
