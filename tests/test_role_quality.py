"""Качество ролей из истории и контрольный набор ревью (07: A7-05, A11-02, A11-03, AG-*-10, A0-06)."""
import json
import unittest

from base import DataCase
import review_eval as re_
import role_quality as rq


def task(defects=(), hist=(), dual=False):
    return dict(pipeline=dict(defects=list(defects), history=list(hist), dual_review=dict(second=dict(run='R')) if dual else None))


PASSED_REVIEW = dict(kind='stage_passed', stage='review')
REV_OK = dict(kind='handoff', role='reviewer', accepted=True, agent='codex')


class HistoryMetrics(unittest.TestCase):
    def test_reviewer_recall_counts_escaped_defects(self):
        tasks = [task([dict(found_by='reviewer', found_agent='codex', found_stage='review')], [REV_OK, PASSED_REVIEW]),
                 task([dict(found_by='qa', found_stage='testing')], [REV_OK, PASSED_REVIEW]),            # пропущен ревьюером
                 task([dict(found_by='reviewer', found_stage='review', fix=dict(method='not_a_defect'))], [REV_OK, PASSED_REVIEW])]
        f = rq.finders(tasks)['reviewer']
        self.assertEqual((f['found'], f['escaped'], f['false_positive']), (2, 1, 1))
        self.assertEqual(f['recall']['value'], round(2 / 3, 3))
        self.assertEqual(f['false_positive_rate']['value'], 0.5)
        self.assertTrue(f['recall']['small'])                                    # мало данных — помечено
        self.assertEqual(f['by_agent']['codex']['escaped'], 1)

    def test_wrong_gate_and_resolver_accuracy(self):
        tasks = [task(hist=[dict(kind='gate', gate='analysis', decision='approve', at='1'),
                            dict(kind='return', to_stage='analysis', at='2')]),
                 task(hist=[dict(kind='gate', gate='analysis', decision='approve', at='1')])]
        tl = rq.teamlead(tasks)
        self.assertEqual((tl['approved'], tl['wrong']), (2, 1))
        probs = [dict(ref='p/1', role='developer', decision='continue', decided_at='2026-01-01', at='2026-01-01', cause=dict(kind='fact')),
                 dict(ref='p/1', role='developer', at='2026-01-02'),
                 dict(ref='p/2', role='qa', decision='return', decided_at='2026-01-01', at='2026-01-01', cause=dict(kind='hypothesis'))]
        r = rq.resolver(probs)
        self.assertEqual((r['decided'], r['repeated'], r['routing_accuracy']['value']), (2, 1, 0.5))
        self.assertEqual(r['causes'], dict(fact=1, hypothesis=1))

    def test_ablation_is_observational(self):
        tasks = [task([dict(found_stage='testing')], [PASSED_REVIEW]), task([], [PASSED_REVIEW], dual=True)]
        a = rq.ablation(tasks)
        self.assertEqual((a['single']['per_task'], a['dual']['per_task']), (1.0, 0.0))
        self.assertEqual(a['kind'], 'observational')

    def test_gate_ablation_by_task_size(self):                       # AG-LEAD-10
        srv = dict(kind='gate', gate='release', decision='approve', by='server')
        tl = dict(kind='gate', gate='release', decision='approve', by='teamlead')
        back = dict(kind='return', to_stage='development')
        tasks = [dict(size='S', pipeline=dict(history=[srv])), dict(size='S', pipeline=dict(history=[srv, back])),
                 dict(size='L', pipeline=dict(history=[dict(tl, decision='return'), tl]))]
        a = rq.gate_ablation(tasks)
        self.assertEqual(a['by_size']['S']['server']['value'], 0.5)
        self.assertEqual((a['by_size']['L']['teamlead']['value'], a['by_size']['L']['teamlead_caught']), (0.0, 1))
        self.assertEqual(a['kind'], 'observational')
        self.assertIn('gate_ablation', rq.collect(tasks, []))

    def test_role_overlap_report(self):
        o = rq.overlap()
        self.assertIn('docs/tasks/{id}/RTM.md', o['shared_artifacts'])
        self.assertEqual(o['shared_artifacts']['docs/tasks/{id}/RTM.md'], ['qa', 'system_analyst'])


class ControlSet(DataCase):
    def test_review_eval_counts_recall_and_false_positives(self):
        def oracle(cfg, msgs):                                                      # ревьюер, возвращающий всё подряд
            return json.dumps(dict(verdict='return', defect='x', line=1)), {}
        rec = re_.run(dict(model_id='stub'), chat_fn=oracle, repeats=1)
        self.assertEqual((rec['recall'], rec['false_positive_rate']), (1.0, 1.0))  # recall без FPR ничего не значит

        def perfect(cfg, msgs):
            bad = any(c['diff'] in msgs[-1]['content'] for c in re_.CASES if c['label'] == 'defect')
            return '{"verdict": "%s", "defect": "", "line": 0}' % ('return' if bad else 'approve'), {}
        rec = re_.run(dict(model_id='stub'), chat_fn=perfect, repeats=2)
        self.assertEqual((rec['recall'], rec['false_positive_rate'], rec['repeats']), (1.0, 0.0, 2))
        self.assertEqual(re_.latest()['model_id'], 'stub')
        self.assertEqual(rec['defects'], rec['clean'])                              # набор сбалансирован

    def test_verdict_parsing(self):
        self.assertEqual(re_.verdict_of('<think>…</think>{"verdict": "approve"}'), 'approve')
        self.assertEqual(re_.verdict_of('мусор'), None)


if __name__ == '__main__':
    unittest.main()
