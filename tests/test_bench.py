"""Бенчмарк допуска локальной модели (07: A9-04, A10-05, E2E-R28)."""
import json

from base import DataCase
import learn_bench as lb
import local_mimo as lm


def scripted(*answers):
    """Модель-заглушка: по очереди отдаёт заготовленные ответы, дальше — finish."""
    seq = list(answers)

    def chat(cfg, messages):
        text = seq.pop(0) if seq else json.dumps(dict(tool='finish', args=dict(summary='готово', items={})))
        return text, dict(input=10, output=5)
    return chat


def call(tool, **args):
    return json.dumps(dict(tool=tool, args=args), ensure_ascii=False)


def per_case(answers_by_try):
    """Модель-заглушка для повторов: в начале каждой задачи (паспорт — второе сообщение) берёт ответы следующей попытки."""
    tries = iter(answers_by_try); cur = []

    def chat(cfg, messages):
        nonlocal cur
        if len(messages) == 2:
            cur = list(next(tries, []))
        text = cur.pop(0) if cur else json.dumps(dict(tool='finish', args=dict(summary='готово', items={})))
        return text, dict(input=10, output=5)
    return chat


B1 = lb.CASES[0]
FIX = call('replace', path='calc.py', old='return a - b', new='return a + b')
TAMPER = call('write_file', path='test_calc.py', content='import unittest\n')


class BenchTests(DataCase):
    def cfg(self):
        return lm.register(dict(model_id='mimo-test', quantization='Q4_K_M', runtime_name='llama.cpp', context_limit=4096,
                                endpoint='http://127.0.0.1:8089'), 'owner')

    def test_solution_passes_by_trusted_tests_not_by_model_claim(self):
        ok = lb.run_case({}, B1, scripted(call('read_file', path='calc.py'), FIX))
        self.assertTrue(ok['passed']); self.assertTrue(ok['finished']); self.assertFalse(ok['tampered'])
        claim = lb.run_case({}, B1, scripted())                            # сразу finish без правки — тесты красные
        self.assertFalse(claim['passed']); self.assertTrue(claim['finished'])

    def test_editing_oracle_test_fails_case(self):
        r = lb.run_case({}, B1, scripted(TAMPER, FIX))
        self.assertTrue(r['tampered']); self.assertFalse(r['passed'])

    def test_activation_requires_passed_bench_on_same_config(self):
        cfg = self.cfg(); cid = cfg['model_config_id']
        with self.assertRaisesRegex(ValueError, 'бенчмарк'):
            lm.activate(cid, 'owner')                                       # без квалификации — отказ (E2E-R28)
        bad = lb.run(cfg, scripted(*['не json'] * 60), cases=[B1])
        self.assertFalse(bad['passed']); self.assertLess(bad['protocol'], lb.PROTOCOL_MIN)
        with self.assertRaisesRegex(ValueError, 'не пройден'):
            lm.activate(cid, 'owner')
        flaky = lb.run(cfg, per_case([[FIX], [], [FIX]]), cases=[B1])        # решает через раз — к работе не готова
        self.assertFalse(flaky['passed']); self.assertEqual(flaky['scores'], [1.0, 0.0, 1.0])
        with self.assertRaisesRegex(ValueError, 'не пройден'):
            lm.activate(cid, 'owner')
        good = lb.run(cfg, per_case([[FIX]] * 3), cases=[B1])
        self.assertTrue(good['passed'], good)
        self.assertTrue(lm.activate(cid, 'owner'))
        other = lm.register(dict(model_id='mimo-other', quantization='Q8_0', runtime_name='llama.cpp', context_limit=4096,
                                 endpoint='http://127.0.0.1:8089'), 'owner')
        ok, why = lb.qualified(other)                                       # чужой прогон не переносится
        self.assertFalse(ok); self.assertIn('не прогонялся', why)
        self.assertTrue(lm.activate(other['model_config_id'], 'owner', override='осознанно без квалификации: пробный запуск'))

    def test_builtin_cases_are_unsolved_as_given(self):
        # Каждая задача набора без правки модели красная: бенчмарк не засчитывает «ничего не делать».
        for case in lb.CASES:
            self.assertFalse(lb.run_case({}, case, scripted())['passed'], case['id'])



class ParseRepairTests(DataCase):
    def test_qwen_tail_and_missing_brace_repaired_but_nothing_else(self):
        # Реальный ответ MiMo на B3: хвост </function></tool_call> и потерянная закрывающая скобка args.
        raw = (r'{"tool": "write_file", "args": {"path": "text.py", "content": "def slug(text):\n    return \"-\".join('
               r'part for part in text.lower().split() if part)  # {}\n"}</function></tool_call>')
        tool, args = lm.parse_call(raw)
        self.assertEqual((tool, args['path']), ('write_file', 'text.py'))
        self.assertIn('return "-".join', args['content'])
        for bad in ('{"tool": "x", "args": {"a": "незакрытая строка', 'просто текст', '{"tool": "x"}}}',
                    '{"tool": "x", "args": {"a": [1, 2}}'):
            with self.assertRaises(lm.ToolError, msg=bad):
                lm.parse_call(bad)
