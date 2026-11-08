"""Машинные проверки содержания артефактов (07: AG-PO-02…05, PO-09, AG-SA-01…06/09, A5-01, A6-06, AG-ARCH-01/02/04/05,
AG-DES-01…04, AG-MKT-02…04, A4-01)."""
from test_pipeline import PipelineTests, git      # первым: base ставит путь к tools/
import unittest
from pathlib import Path
import artifact_rules as ar
import orchestra as O
import pipeline as P
import workflow as w


class Ctx:
    def __init__(self, acceptance=('Первый критерий', 'Второй критерий'), size='M', api=None):
        self.t = dict(acceptance=list(acceptance), size=size); self.repo = None; self.branch = None; self._api = api

    def artifact(self, spec):
        return self._api


class RuleTests(unittest.TestCase):
    def test_numbers_need_origin(self):
        it = dict(section='Метрик')
        ok, _ = ar.labeled_numbers('# BRD\n## Метрики\n- Конверсия 12% (гипотеза)\n- Время 200 мс (источник: замер)\n', it)
        self.assertTrue(ok)
        ok, why = ar.labeled_numbers('# BRD\n## Метрики\n- Конверсия вырастет на 30%\n', it)
        self.assertFalse(ok); self.assertIn('30%', why)
        ok, why = ar.labeled_numbers('## GTM\n- CAC 40$ (источник: https://x.org)\n', dict(date=True))
        self.assertFalse(ok); self.assertIn('даты', why)                        # источник без даты — не годится для рынка
        self.assertTrue(ar.labeled_numbers('## GTM\n- CAC 40$ (источник: https://x.org, 2026-10)\n', dict(date=True))[0])
        self.assertTrue(ar.labeled_numbers('## Метрики\nСвязано с AC-1 и FR-2\n', it)[0])   # коды — не числа

    def test_nfr_must_be_measurable(self):
        it = dict(section='Нефункциональные')
        self.assertFalse(ar.measurable('## Нефункциональные требования\n- Система должна быть быстрой\n', it)[0])
        self.assertTrue(ar.measurable('## Нефункциональные требования\n- NFR-1: ответ ≤ 300 мс на 95-м перцентиле\n', it)[0])
        self.assertFalse(ar.measurable('## Нефункциональные требования\nтекст без списка\n', it)[0])

    def test_dev_units_reject_monolith(self):
        self.assertFalse(ar.dev_units('# Задачи\n## Сделать всё\n', Ctx(), {})[0])
        self.assertFalse(ar.dev_units('## DU-1 Всё\nразмер: XL, AC-1\n', Ctx(), {})[0])
        self.assertFalse(ar.dev_units('## DU-1 Всё\nAC-1, AC-2\n', Ctx(size='XL'), {})[0])   # XL одним шагом
        self.assertTrue(ar.dev_units('## DU-1 Модель\nAC-1\n## DU-2 API\nAC-2\n', Ctx(size='XL'), {})[0])
        self.assertIn('DU-2 без AC', ar.dev_units('## DU-1 A\nAC-1\n## DU-2 B\nбез критерия\n', Ctx(), {})[1])

    def test_design_matrix_per_ac(self):
        good = '## Позитивные\nAC-1 AC-2\n## Негативные\nAC-1 AC-2\n## Граничные\nAC-1 AC-2\n## Ожидаемый результат\nок\n'
        self.assertTrue(ar.design_matrix(good, Ctx(), {})[0])
        bad = good.replace('## Граничные\nAC-1 AC-2', '## Граничные\nAC-1')
        ok, why = ar.design_matrix(bad, Ctx(), {})
        self.assertFalse(ok); self.assertIn('AC-2', why)
        self.assertFalse(ar.covers_ac('| BR | SR |\n| BR-1 | AC-1 |', Ctx(), {})[0])

    def test_adr_needs_two_options_and_threats_need_measures(self):
        self.assertFalse(ar.adr_options('# ADR-001\n## Решение\nберём X\n', Ctx(), {})[0])
        self.assertTrue(ar.adr_options('# ADR-001\n## Варианты\n- X\n- Y\n## Решение\nX\n', Ctx(), {})[0])
        self.assertFalse(ar.threat_table('| Угроза | Мера |\n|---|---|\n| Инъекция | |\n| XSS | экранирование |\n', Ctx(), {})[0])
        self.assertTrue(ar.threat_table('| Угроза | Мера |\n|---|---|\n| Инъекция | параметры |\n| XSS | экранирование |\n', Ctx(), {})[0])

    def test_ui_fields_must_exist_in_api(self):
        handoff = '## Валидации\n- `email` обязателен\n- `phone` до 15 цифр\n'
        self.assertTrue(ar.fields_in_api(handoff, Ctx(api=None), {})[0])         # контракта нет — сверять не с чем
        ok, why = ar.fields_in_api(handoff, Ctx(api='## Контракт\nemail: string\n'), {})
        self.assertFalse(ok); self.assertIn('phone', why)
        self.assertTrue(ar.api_version(None, Ctx(), {})[0])
        self.assertFalse(ar.api_version('## Контракт\n', Ctx(), {})[0])

    def test_legacy_fields_not_lost(self):                           # AG-DES-07
        import artifact_rules as ar
        self.assertTrue(ar.legacy_fields('## Экраны\nтекст\n', None, {})[0])
        doc = '## Старый интерфейс\n- `name`, `size`, `owner`\n## Новый\n- `name`, `size`\n'
        ok, why = ar.legacy_fields(doc, None, {})
        self.assertFalse(ok); self.assertIn('owner', why)
        self.assertTrue(ar.legacy_fields(doc.replace('`owner`', '`owner` — убрано: дублирует колонку'), None, {})[0])

    def test_states_and_a11y(self):
        it = dict(section='Состояни', words=('ошибк', 'пуст', 'загрузк'))
        self.assertFalse(ar.words_in_section('## Состояния\nобычный вид\n', it)[0])
        self.assertTrue(ar.words_in_section('## Состояния\nошибка, пустой, загрузка\n', it)[0])
        self.assertFalse(ar.heading_any('## Экраны\n', dict(needles=['Доступност']))[0])


class A11yTests(unittest.TestCase):                                                # 07: AG-DES-05…07
    def test_checker_finds_unlabeled_controls(self):
        import a11y_check as a
        bad = a.check_text('<html><button></button><img src=x><input id=q><select></select></html>', 'x.html')
        self.assertEqual(len(bad), 5)                                               # lang, кнопка, img, поле, список
        ok = a.check_text('<html lang="ru"><button aria-label="Закрыть"></button><img alt="" src=x>'
                          '<label for="q">Поиск</label><input id="q"><label>Сессий <select></select></label></html>', 'x.html')
        self.assertEqual(ok, [])

    def test_dashboard_has_no_a11y_problems(self):
        import a11y_check as a
        self.assertEqual(a.run(), [])


class ArtifactFlowTests(PipelineTests):
    def test_hld_paths_must_exist(self):
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        ctx = O.ctx_for(self.task(), self.task()['pipeline'])
        self.assertTrue(ar.refs_exist('Код в `app.py`, новый модуль `export.py`', ctx, {})[0])
        ok, why = ar.refs_exist('Код в `ghost/module.py`', ctx, {})
        self.assertFalse(ok); self.assertIn('ghost/module.py', why)

    def test_brd_change_after_analysis_blocks_development(self):
        self.ref = self.idea()
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        pipe = self.task()['pipeline']
        self.assertTrue(pipe['brd_digest'].startswith('sha256:'))
        wt = Path(pipe['worktree']['path'])
        brd = wt / P.art_path(self.task()['id'], P.ROLES['product_owner']['artifacts'][0]['path'])
        brd.write_text(brd.read_text(encoding='utf-8') + '\nНовое требование задним числом.\n', encoding='utf-8')
        git('add', '-A', cwd=wt); git('commit', '-q', '-m', 'BRD задним числом', cwd=wt)
        res = P.check_dor('development', O.ctx_for(self.task(), pipe), pipe)
        self.assertIn('dor-brd', [it['id'] for it, ok, _ in res if not ok])

    def test_duplicate_idea_is_flagged_and_packet_has_provenance(self):
        self.ref = self.idea()
        r = w.action(dict(op='idea', project='alpha-api', title='Экспорт портфеля в CSV для инвесторов',
                          text='Выгружать портфель в CSV для отчёта инвесторам каждый месяц', source='claude'))
        self.assertIn(self.ref, [d['ref'] for d in r['duplicates']])
        run = next(x for x in w.state()['runs'] if x['ref'] == r['ref'] and x.get('role') == 'product_owner')
        packet = w.action(dict(op='packet', ref=r['ref'], run=run['id']))
        self.assertIn(self.ref, [d['ref'] for d in packet['duplicates']])
        self.assertEqual(packet['provenance']['source'], 'server')
        self.assertEqual(packet['provenance']['schema'], O.SCHEMA_VERSION)
        self.ref = r['ref']
        self.do_role('product_owner', 'idea', acceptance=['Первый критерий приёмки', 'Второй критерий приёмки'],
                     needs=dict(design=False, marketing=False, reason='Только серверная выгрузка, без интерфейса'))
        sa = self.run_of('system_analyst')
        up = w.action(dict(op='packet', ref=self.ref, run=sa['id']))['upstream']
        brd = next(u for u in up if u['path'].endswith('BRD.md'))
        self.assertEqual((brd['trust_level'], brd['project']), ('accepted', 'alpha-api'))
        self.assertTrue(brd['commit'] and brd['timestamp'] and brd['source_id'].endswith('BRD.md'))


for _n in [n for n in dir(PipelineTests) if n.startswith('test_') and n not in ArtifactFlowTests.__dict__]:
    setattr(ArtifactFlowTests, _n, None)
del PipelineTests
