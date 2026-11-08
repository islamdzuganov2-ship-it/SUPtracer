import copy
import importlib
from base import DataCase


class ReadinessReview(DataCase):
    def setUp(self):
        super().setUp()
        self.portfolio = self.projects()
        import check
        self.check = importlib.reload(check)

    def reviewed(self):
        axis = {'confirmed': ['Локальный запуск'],
                'pending': ['Поставка', 'Восстановление', 'Приёмка'],
                'evidence': 'README.md; локальная проверка 15.10.2026'}
        dims = {k: copy.deepcopy(axis) for k in ('functional', 'production', 'docs', 'tests', 'security')}
        r = {k: 25 for k in dims}
        r.update(basis='Ручная ревизия', assessed=True,
                 review={'date': '2026-10-15', 'method': 'checkpoints-v1', 'dimensions': dims})
        self.portfolio['projects']['alpha-api']['readiness'] = r
        return r

    def validate(self):
        self.jwrite('projects.json', self.portfolio)
        return self.check.t_readiness()

    def test_legacy_and_evidence_based_reviews_pass(self):
        self.assertEqual(self.validate(), [])
        self.reviewed()
        self.assertEqual(self.validate(), [])

    def test_numbers_cannot_be_raised_without_confirmed_points(self):
        r = self.reviewed(); r['functional'] = 75
        self.assertTrue(any('число расходится' in s for s in self.validate()))

    def test_empty_evidence_missing_points_and_duplicate_points_fail(self):
        axis = self.reviewed()['review']['dimensions']['tests']
        axis['evidence'] = ''
        axis['pending'] = ['Локальный запуск']
        errors = self.validate()
        self.assertTrue(any('четыре' in s for s in errors))
        self.assertTrue(any('повторяющиеся' in s for s in errors))
        self.assertTrue(any('основания' in s for s in errors))

    def test_review_needs_date_and_explicit_assessed(self):
        r = self.reviewed(); r['review']['date'] = 'later'; r['assessed'] = False
        self.assertGreaterEqual(len(self.validate()), 2)

    def test_malformed_evidence_reports_failure_without_crashing(self):
        dims = self.reviewed()['review']['dimensions']
        dims['tests'] = None
        dims['docs']['confirmed'] = [{'not': 'a checkpoint'}]
        self.assertGreaterEqual(len(self.validate()), 2)

    def test_project_route_check_is_part_of_merge_gate(self):
        self.assertIn(self.check.t_project_routes, self.check.TESTS)
        self.jwrite('usage.json', {'providers': {'codex': {'routing_stale': True}}})
        self.assertTrue(any('codex' in s for s in self.check.t_project_routes()))
