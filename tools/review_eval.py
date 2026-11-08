"""Размеченный контрольный набор ревью (07: A7-05, A11-03, AG-REV-10).

Качество ревьюера меряется не долей approve, а на заранее известных образцах: патчи с внесённым дефектом
(должен вернуть) и чистые патчи (должен одобрить). Recall = найденные дефекты / все дефектные образцы;
false-positive rate = возвраты чистых / все чистые. Каждый образец прогоняется `repeats` раз; итог — по худшему.

    py tools/review_eval.py [--endpoint http://127.0.0.1:8089] [--model <id>] [--repeats 2]

Результат пишется в data/learning/review_eval.json (личное, вне git) с происхождением модели.
"""
import json
import re
import sys
import time

import learning_events as le
import local_mimo as lm

VERSION = 'review-eval-1'
SYSTEM = ('Ты ревьюер кода. Тебе дают критерии приёмки и дифф. Найди дефект, если он есть: ошибку логики, граничный случай, '
          'уязвимость, секрет в коде, нарушение критерия. Не придирайся к стилю. Ответь ТОЛЬКО JSON одной строкой: '
          '{"verdict": "approve" или "return", "defect": "суть дефекта или пусто", "line": номер строки дифа или 0}.')

CASES = [
    dict(id='R1', label='defect', kind='граница', ac='Скидка 10% начисляется при сумме от 1000 включительно',
         diff='+def discount(total):\n+    if total > 1000:\n+        return total * 0.9\n+    return total'),
    dict(id='R2', label='defect', kind='инъекция', ac='Поиск пользователя по имени безопасен для любых строк',
         diff='+def find(db, name):\n+    return db.execute("SELECT * FROM users WHERE name = \'" + name + "\'").fetchall()'),
    dict(id='R3', label='defect', kind='секрет', ac='Ключ API берётся из окружения',
         diff='+import os\n+API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"\n+def headers():\n+    return {"Authorization": API_KEY}'),
    dict(id='R4', label='defect', kind='пустой ввод', ac='Среднее пустого списка равно 0',
         diff='+def mean(xs):\n+    return sum(xs) / len(xs)'),
    dict(id='R5', label='defect', kind='логика', ac='Функция возвращает True, если пользователь — администратор ИЛИ владелец',
         diff='+def can_edit(user, doc):\n+    return user.is_admin and doc.owner_id == user.id'),
    dict(id='R6', label='defect', kind='индекс', ac='last(xs) возвращает последний элемент непустого списка',
         diff='+def last(xs):\n+    return xs[len(xs)]'),
    dict(id='C1', label='clean', kind='чистый', ac='Скидка 10% начисляется при сумме от 1000 включительно',
         diff='+def discount(total):\n+    if total >= 1000:\n+        return total * 0.9\n+    return total'),
    dict(id='C2', label='clean', kind='чистый', ac='Поиск пользователя по имени безопасен для любых строк',
         diff='+def find(db, name):\n+    return db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchall()'),
    dict(id='C3', label='clean', kind='чистый', ac='Среднее пустого списка равно 0',
         diff='+def mean(xs):\n+    if not xs:\n+        return 0\n+    return sum(xs) / len(xs)'),
    dict(id='C4', label='clean', kind='чистый', ac='Ключ API берётся из окружения',
         diff='+import os\n+def headers():\n+    return {"Authorization": os.environ["API_KEY"]}'),
    dict(id='C5', label='clean', kind='чистый', ac='last(xs) возвращает последний элемент непустого списка',
         diff='+def last(xs):\n+    return xs[-1]'),
    dict(id='C6', label='clean', kind='чистый', ac='Функция возвращает True, если пользователь — администратор ИЛИ владелец',
         diff='+def can_edit(user, doc):\n+    return user.is_admin or doc.owner_id == user.id'),
]


def verdict_of(text):
    m = re.search(r'\{.*\}', text or '', re.S)
    if m:
        try:
            v = json.loads(m.group(0)).get('verdict')
            if v in ('approve', 'return'):
                return v
        except ValueError:
            pass
    low = (text or '').lower()
    return 'return' if '"return"' in low or 'verdict: return' in low else ('approve' if 'approve' in low else None)


def run_once(cfg, chat_fn, cases):
    out = []
    for c in cases:
        msgs = [dict(role='system', content=SYSTEM),
                dict(role='user', content='Критерий приёмки: %s\nДифф:\n%s' % (c['ac'], c['diff']))]
        t0 = time.time()
        try:
            text, _ = chat_fn(cfg, msgs)
        except lm.RuntimeUnavailable as e:
            raise ValueError('рантайм недоступен: %s' % e)
        v = verdict_of(text)
        out.append(dict(id=c['id'], label=c['label'], verdict=v, ok=(v == 'return') == (c['label'] == 'defect'),
                        seconds=round(time.time() - t0, 1), answer=(text or '')[:300]))
    d = [r for r in out if r['label'] == 'defect']; cl = [r for r in out if r['label'] == 'clean']
    tp = sum(r['verdict'] == 'return' for r in d); fp = sum(r['verdict'] == 'return' for r in cl)
    return dict(cases=out, recall=round(tp / len(d), 3) if d else None, false_positive_rate=round(fp / len(cl), 3) if cl else None,
                tp=tp, fn=len(d) - tp, fp=fp, tn=len(cl) - fp, unparsed=sum(r['verdict'] is None for r in out))


def run(cfg, chat_fn=None, repeats=2, cases=None):
    reps = [run_once(cfg, chat_fn or lm.chat, cases or CASES) for _ in range(max(1, repeats))]
    rec = dict(version=VERSION, at=le.iso(), model_id=cfg.get('model_id'), model_config_id=cfg.get('model_config_id'),
               weight_sha256=cfg.get('weight_sha256'), endpoint=cfg.get('endpoint'), repeats=len(reps),
               recall=min(r['recall'] for r in reps), false_positive_rate=max(r['false_positive_rate'] for r in reps),
               recalls=[r['recall'] for r in reps], fprs=[r['false_positive_rate'] for r in reps],
               defects=sum(c['label'] == 'defect' for c in cases or CASES), clean=sum(c['label'] == 'clean' for c in cases or CASES),
               last=reps[-1])
    p = le.root() / 'review_eval.json'
    try:
        doc = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        doc = dict(runs=[])
    doc['runs'] = doc['runs'][-29:] + [rec]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
    return rec


def latest():
    try:
        return (json.loads((le.root() / 'review_eval.json').read_text(encoding='utf-8')).get('runs') or [None])[-1]
    except (OSError, ValueError):
        return None


def main(argv):
    o = dict(zip(argv[::2], argv[1::2]))
    cfg = lm.active() or {}
    if '--endpoint' in o:
        cfg = dict(cfg, endpoint=o['--endpoint'], model_id=o.get('--model') or cfg.get('model_id') or 'local')
    if not cfg.get('endpoint'):
        print('нет конфигурации: --endpoint http://127.0.0.1:8089 или активная модель'); return 2
    rec = run(cfg, repeats=int(o.get('--repeats', 2)))
    print('recall %.2f · false-positive rate %.2f · дефектных %d, чистых %d · повторов %d' % (
        rec['recall'], rec['false_positive_rate'], rec['defects'], rec['clean'], rec['repeats']))
    for c in rec['last']['cases']:
        print('  %s %-6s → %-7s %s' % ('✓' if c['ok'] else '✗', c['label'], c['verdict'], c['answer'][:90].replace('\n', ' ')))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
