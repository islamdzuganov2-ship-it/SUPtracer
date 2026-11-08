"""Бенчмарк допуска локальной модели (07: A9-04, A10-05, AG-LDEV-01/06, E2E-R28).

Конфигурация local_mimo становится рабочей только после прохода квалификации: фиксированный набор
маленьких задач с тестами, решаемых тем же агентным циклом и той же песочницей, что и в работе
(`local_mimo.Sandbox`, `parse_call`, `SYSTEM_PROMPT`). Итог считает не модель: после её finish тесты
задачи прогоняет бенчмарк сам, а файл теста сверяется с эталоном — правка теста = провал задачи.

Результат — в data/learning/bench.json (личное, вне git): по каждой задаче итог, шаги, отказы
инструментов, токены и время; по конфигурации — доля решённых и доля корректных вызовов.
`local_mimo.activate` отклоняет конфигурацию без пройденного бенчмарка на её весах и рантайме.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import learning_events as le
import local_mimo as lm

THRESHOLD = 0.6            # доля решённых задач для допуска
PROTOCOL_MIN = 0.8         # доля корректных вызовов инструментов
MAX_STEPS = 16
REPEATS = 3          # повторов набора: допуск по худшему
VERSION = 'bench-1'


def _case(cid, title, files, test, acceptance):
    return dict(id=cid, title=title, files=files, test=test, acceptance=acceptance)


CASES = [
    _case('B1-fix-sign', 'Исправить ошибку знака в add', {'calc.py': 'def add(a, b):\n    return a - b\n'},
          ('test_calc.py', 'import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n'
                           '    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n        self.assertEqual(add(-1, 1), 0)\n'),
          ['add(a, b) возвращает сумму']),
    _case('B2-empty-list', 'Средним пустого списка должен быть 0, а не исключение',
          {'stats.py': 'def mean(xs):\n    return sum(xs) / len(xs)\n'},
          ('test_stats.py', 'import unittest\nfrom stats import mean\n\n\nclass T(unittest.TestCase):\n'
                            '    def test_mean(self):\n        self.assertEqual(mean([1, 2, 3]), 2)\n'
                            '    def test_empty(self):\n        self.assertEqual(mean([]), 0)\n'),
          ['mean([]) возвращает 0', 'для непустого списка — среднее']),
    _case('B3-new-func', 'Добавить функцию slug(text)', {'text.py': '"""Утилиты строк."""\n'},
          ('test_text.py', 'import unittest\nfrom text import slug\n\n\nclass T(unittest.TestCase):\n'
                           '    def test_slug(self):\n        self.assertEqual(slug("Hello  World"), "hello-world")\n'
                           '        self.assertEqual(slug(" A-b "), "a-b")\n'),
          ['slug: нижний регистр, пробелы по краям убраны, пробелы между словами — один дефис']),
    _case('B4-boundary', 'Граница: скидка действует с 100 включительно',
          {'price.py': 'def discount(total):\n    if total > 100:\n        return total * 0.9\n    return total\n'},
          ('test_price.py', 'import unittest\nfrom price import discount\n\n\nclass T(unittest.TestCase):\n'
                            '    def test_boundary(self):\n        self.assertEqual(discount(100), 90)\n'
                            '        self.assertEqual(discount(99), 99)\n        self.assertEqual(discount(200), 180)\n'),
          ['скидка 10% при сумме от 100 включительно']),
    _case('B5-two-files', 'Перевести формат даты в отчёте на ISO',
          {'fmt.py': 'def fmt_date(y, m, d):\n    return "%02d.%02d.%04d" % (d, m, y)\n',
           'report.py': 'from fmt import fmt_date\n\n\ndef line(name, y, m, d):\n    return name + ": " + fmt_date(y, m, d)\n'},
          ('test_report.py', 'import unittest\nfrom report import line\n\n\nclass T(unittest.TestCase):\n'
                             '    def test_iso(self):\n        self.assertEqual(line("релиз", 2026, 3, 7), "релиз: 2026-03-07")\n'),
          ['даты в отчёте — ГГГГ-ММ-ДД']),
]


def _store():
    return le.root() / 'bench.json'


def load():
    p = _store()
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(runs=[])


def _save(doc):
    p = _store(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def provenance(cfg):
    """Что именно квалифицировано: конфигурация, веса и рантайм по хешам, версия набора."""
    return dict(model_config_id=cfg.get('model_config_id') or lm.config_id(cfg), weight_sha256=cfg.get('weight_sha256'),
                runtime_sha256=cfg.get('runtime_sha256'), bench=VERSION)


def _trusted_tests(root):
    out = subprocess.run([sys.executable, '-I', '-m', 'unittest', '-q'], cwd=root, capture_output=True, text=True,
                         encoding='utf-8', errors='replace', timeout=120, stdin=subprocess.DEVNULL, env=_env())
    return out.returncode == 0, (out.stdout + out.stderr)[-800:]


def _env():
    try:
        import sandbox_env
        return sandbox_env.minimal()
    except Exception:  # noqa: BLE001
        return None


def run_case(cfg, case, chat_fn, max_steps=MAX_STEPS):
    with tempfile.TemporaryDirectory(prefix='tt-bench-') as d:
        root = Path(d)
        for rel, text in dict(case['files'], **{case['test'][0]: case['test'][1]}).items():
            (root / rel).write_text(text, encoding='utf-8')
        oracle = hashlib.sha256(case['test'][1].encode('utf-8')).hexdigest()
        box = lm.Sandbox(root, None, {'verify': [sys.executable, '-I', '-m', 'unittest', '-q']})
        brief = json.dumps(dict(task=case['id'], title=case['title'], acceptance=case['acceptance'], scope=[],
                                dod=[dict(id='dev-verify', text='Тесты проходят (run verify)')],
                                note='Тест %s — эталон приёмки: его не менять.' % case['test'][0]), ensure_ascii=False)
        messages = [dict(role='system', content=lm.SYSTEM_PROMPT), dict(role='user', content=brief)]
        calls = dict(ok=0, refused=0, bad_json=0); tokens = dict(input=0, output=0); finished = False
        started = time.time(); err = None
        for step in range(max_steps):
            try:
                text, usage = chat_fn(cfg, messages)
            except lm.RuntimeUnavailable as e:
                err = 'runtime: %s' % e; break
            for k in tokens:
                tokens[k] += (usage or {}).get(k) or 0
            messages.append(dict(role='assistant', content=text[:4000]))
            tool = '?'
            try:
                tool, args = lm.parse_call(text)
                if tool == 'finish':
                    finished = True; break
                result = box.call(tool, args); calls['ok'] += 1
            except lm.ToolError as e:
                result = 'ОТКАЗ: %s' % e
                calls['bad_json' if tool == '?' else 'refused'] += 1
            messages.append(dict(role='user', content='Результат %s:\n%s' % (tool, str(result)[:lm.TOOL_OUTPUT])))
            if len(messages) > 24:
                messages = messages[:2] + messages[-20:]
        test_now = (root / case['test'][0]).read_text(encoding='utf-8') if (root / case['test'][0]).exists() else ''
        tampered = hashlib.sha256(test_now.encode('utf-8')).hexdigest() != oracle
        ok, log = (False, 'эталон изменён') if tampered else _trusted_tests(root)
        return dict(id=case['id'], passed=bool(ok and not err), finished=finished, tampered=tampered, steps=step + 1,
                    calls=calls, tokens=tokens, seconds=round(time.time() - started, 1), error=err, log=log[-300:])


def run(cfg=None, chat_fn=None, cases=None, origin='server', repeats=REPEATS):
    """Прогнать набор на конфигурации `repeats` раз. Допуск — только если каждый повтор проходит пороги:
    модель, решающая набор через раз, к работе не готова (стабильность — часть квалификации)."""
    cfg = cfg or lm.active()
    if not cfg:
        raise ValueError('Нет конфигурации: зарегистрируйте модель (pm.py learn model-gguf …)')
    if chat_fn is None:
        h = lm.health(cfg)
        if not h['ok']:
            raise ValueError('Рантайм модели не отвечает: %s — запустите ops/mimo-server.cmd' % h['reason'])
    reps = []
    for _ in range(max(1, int(repeats))):
        results = [run_case(cfg, c, chat_fn or lm.chat) for c in (cases or CASES)]
        n = len(results); solved = sum(r['passed'] for r in results)
        total_calls = sum(sum(r['calls'].values()) for r in results)
        protocol = (sum(r['calls']['ok'] + r['calls']['refused'] for r in results) / total_calls) if total_calls else 0.0
        reps.append(dict(cases=results, solved=solved, total=n, score=round(solved / n, 3) if n else 0.0,
                         protocol=round(protocol, 3), tampered=any(r['tampered'] for r in results)))
    rec = dict(provenance(cfg), at=le.iso(), origin=origin, repeats=reps, cases=reps[-1]['cases'],
               solved=min(r['solved'] for r in reps), total=reps[0]['total'],
               score=min(r['score'] for r in reps), protocol=min(r['protocol'] for r in reps),
               scores=[r['score'] for r in reps], threshold=THRESHOLD, protocol_min=PROTOCOL_MIN)
    rec['passed'] = all(r['score'] >= THRESHOLD and r['protocol'] >= PROTOCOL_MIN and not r['tampered'] for r in reps)
    doc = load(); doc['runs'] = (doc.get('runs') or [])[-49:] + [rec]; _save(doc)
    le.record('model_benchmark', dict(component='learn', bench=VERSION, config=rec['model_config_id'], at=rec['at']), origin=origin,
              model_config_id=rec['model_config_id'], result='passed' if rec['passed'] else 'failed',
              reason='повторов %d, решено по худшему %d из %d, протокол по худшему %.0f%%' % (
                  len(reps), rec['solved'], rec['total'], rec['protocol'] * 100))
    return rec


def qualified(cfg):
    """(ok, причина). Допуск — последний прогон набора на тех же весах и рантайме."""
    want = provenance(cfg)
    runs = [r for r in load().get('runs') or [] if all(r.get(k) == want[k] for k in want)]
    if not runs:
        return False, 'бенчмарк %s на этих весах и рантайме не прогонялся: py tools/pm.py learn bench' % VERSION
    last = runs[-1]
    if not last.get('passed'):
        return False, 'бенчмарк не пройден: решено %d из %d (порог %.0f%%), протокол %.0f%%' % (
            last['solved'], last['total'], THRESHOLD * 100, last['protocol'] * 100)
    return True, 'бенчмарк пройден %s: %d из %d' % (last['at'][:10], last['solved'], last['total'])
