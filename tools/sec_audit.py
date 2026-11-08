"""Журнал нарушений и привилегий с цепочкой хешей (07: A2-08, E2E-R31).

Каждая запись несёт хеш предыдущей: правка, удаление или вставка задним числом рвут цепочку, и `verify`
показывает, с какой записи журнал недостоверен. Пишет только сервер; агент формирует запись лишь косвенно —
своим нарушением. Корреляция: run, agent, op, ref.

События: попытка агента выполнить действие владельца, правка вне зоны роли, изменение эталона приёмки,
отказ HTTP (session_auth), запрещённый путь (path_policy).

    py tools/sec_audit.py verify
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

GENESIS = 'sha256:' + '0' * 64


def path():
    base = os.environ.get('TASKTREKER_DATA') or str(Path(__file__).resolve().parent.parent / 'data')
    return Path(base) / 'security' / 'audit.jsonl'


def _hash(row):
    body = {k: v for k, v in row.items() if k != 'hash'}
    return 'sha256:' + hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _last_hash(p):
    try:
        with open(p, 'rb') as fh:
            fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 8192))
            lines = [l for l in fh.read().decode('utf-8', 'replace').splitlines() if l.strip()]
        return json.loads(lines[-1])['hash'] if lines else GENESIS
    except (OSError, ValueError, KeyError, IndexError):
        return GENESIS


def record(kind, detail='', run=None, agent=None, op=None, ref=None, origin='agent'):
    """Добавить запись. Сбой записи не меняет решение (отказ остаётся отказом), но печатается в stderr."""
    try:
        p = path(); p.parent.mkdir(parents=True, exist_ok=True)
        row = dict(at=datetime.now(timezone.utc).isoformat(timespec='seconds'), kind=kind, detail=str(detail)[:400],
                   run=run, agent=agent, op=op, ref=ref, origin=origin, prev=_last_hash(p))
        row['hash'] = _hash(row)
        with open(p, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        return row
    except OSError as e:
        print('sec_audit: запись не удалась: %s' % e, file=sys.stderr)
        return None


def read():
    try:
        return [json.loads(l) for l in path().read_text(encoding='utf-8').splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def verify(rows=None):
    """(ok, detail): цепочка цела от первой записи до последней."""
    rows = read() if rows is None else rows
    prev = GENESIS
    for i, r in enumerate(rows, 1):
        if r.get('prev') != prev:
            return False, 'запись %d: разрыв цепочки (удалена или вставлена запись перед ней)' % i
        if _hash(r) != r.get('hash'):
            return False, 'запись %d: содержимое изменено после записи' % i
        prev = r['hash']
    return True, 'записей %d, цепочка цела' % len(rows)


if __name__ == '__main__':
    if sys.argv[1:2] == ['verify']:
        ok, d = verify(); print(('OK ' if ok else 'FAIL ') + d); sys.exit(0 if ok else 1)
    print('py tools/sec_audit.py verify'); sys.exit(2)
