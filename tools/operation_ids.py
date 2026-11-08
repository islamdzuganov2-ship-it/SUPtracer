"""Идентификатор операции и дедупликация (TT3-017; ТЗ TT-U-001 §0.5, FR-009, AC-43, TC-12).

Git-коммит и JSON не образуют общей транзакции: процесс может умереть после слияния в main,
но до записи статуса. Поэтому каждое повторяемое тяжёлое действие (слияние, тег, развёртывание)
имеет детерминированный `operation_id` и запись в журнале `workflow.json` → `operations`:

  started → done | failed | reconciliation_required

Повтор с тем же id не выполняет действие второй раз:
  * done          — возвращается записанный результат;
  * started       — действие идёт (или процесс умер): свежая запись — отказ «уже выполняется»;
                    устаревшая — сверка по Git (`applied`): применено — done без повтора;
                    проверить нельзя (развёртывание) — reconciliation_required и отказ до решения владельца;
  * failed        — можно повторить.

Тот же механизм — для запросов tt_work / HTTP с полем `operation_id`: повтор возвращает прежний
ответ, другой запрос под тем же id отклоняется, одновременный второй — отказ.
"""
import hashlib
import json
import re
from datetime import timedelta

import taskstate as ts

KEEP = 600                      # записей в журнале
STALE_MINUTES = 60              # started старше — процесс считается прерванным
RESULT_MAX = 20_000             # байт ответа запроса, который хранится для повтора
ID = re.compile(r'^[A-Za-z0-9_.:\-/]{8,160}$')


def _w():
    import workflow
    return workflow


def _locked():
    return ts.locked(str(_w().data_dir()), wait=10)


def ledger(s):
    return s.setdefault('operations', {})


def _trim(ops):
    if len(ops) > KEEP:
        for k in sorted(ops, key=lambda k: ops[k].get('at') or '')[:len(ops) - KEEP]:
            if ops[k].get('status') in ('done', 'failed'):
                ops.pop(k)


def heavy_id(check, ref, target, sha):
    """Детерминированный id тяжёлого действия: что, по какой задаче, куда, какой коммит."""
    return 'op:%s:%s:%s:%s' % (check, ref or '-', target or '-', (sha or 'nosha')[:12])


def _stale(rec, at):
    t = _w().parse_time(rec.get('at'))
    return t is None or at - t > timedelta(minutes=STALE_MINUTES)


# ---------------------------------------------------------------- тяжёлые действия

def guarded(op_id, act, applied=None, meta=None):
    """Выполнить act() -> (ok, detail) не больше одного раза для op_id.

    applied() -> (bool, detail) — проверка по Git, применено ли уже (для сверки после сбоя).
    Без applied прерванное действие не повторяется вслепую: reconciliation_required."""
    w = _w()
    with _locked():
        s = w.state(); at = w.now(); ops = ledger(s); rec = ops.get(op_id)
        if rec and rec.get('status') == 'done':
            return rec.get('ok', True), 'уже выполнено (%s): %s' % (op_id, rec.get('detail') or '')
        if rec and rec.get('status') == 'reconciliation_required':
            return False, 'операция %s прервана, результат не проверить — нужна сверка владельцем' % op_id
        interrupted = bool(rec and rec.get('status') == 'started')
        if interrupted and not _stale(rec, at):
            return False, 'операция %s уже выполняется с %s' % (op_id, (rec.get('at') or '')[:16])
        if interrupted or applied:
            done, why = applied() if applied else (False, '')
            if done:
                ops[op_id] = dict(rec or {}, status='done', ok=True, at=at.isoformat(), reconciled=True,
                                  detail='сверка по Git: ' + why, **(meta or {}))
                w.save('workflow.json', s)
                return True, 'уже применено, повтор не нужен (%s): %s' % (op_id, why)
            if interrupted and not applied:
                ops[op_id] = dict(rec, status='reconciliation_required', at=at.isoformat(),
                                  detail='процесс прервался во время действия; повтор вслепую опасен')
                w.save('workflow.json', s)
                return False, 'операция %s прервана, результат не проверить — нужна сверка владельцем' % op_id
        ops[op_id] = dict(status='started', at=at.isoformat(), attempt=int((rec or {}).get('attempt') or 0) + 1, **(meta or {}))
        _trim(ops); w.save('workflow.json', s)
    try:
        ok, detail = act()
    except BaseException as e:
        finish(op_id, False, 'сбой: %s' % e)
        raise
    finish(op_id, ok, detail)
    return ok, detail


def finish(op_id, ok, detail):
    w = _w()
    with _locked():
        s = w.state(); ops = ledger(s)
        ops[op_id] = dict(ops.get(op_id) or {}, status='done' if ok else 'failed', ok=bool(ok),
                          finished_at=w.now().isoformat(), detail=str(detail)[:500])
        w.save('workflow.json', s)


def resolve(s, op_id, decision, at, origin):
    """Решение владельца по прерванной операции: retry — разрешить повтор, done — признать выполненной."""
    rec = ledger(s).get(op_id)
    if not rec or rec.get('status') != 'reconciliation_required':
        raise ValueError('Операция %s не ждёт сверки' % op_id)
    if decision not in ('retry', 'done'):
        raise ValueError('decision: retry — повторить, done — уже выполнено (проверено владельцем)')
    rec.update(status='failed' if decision == 'retry' else 'done', ok=decision == 'done', resolved_at=at.isoformat(),
               resolved_by=origin, detail=(rec.get('detail') or '') + '; владелец: ' + decision)
    return rec


def pending(s):
    return [dict(id=k, **v) for k, v in (s.get('operations') or {}).items() if v.get('status') == 'reconciliation_required']


# ---------------------------------------------------------------- запросы tt_work / HTTP

def fingerprint(req, origin):
    body = json.dumps(dict({k: v for k, v in req.items() if k != 'operation_id'}, _origin=origin),
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:24]


def claim_request(op_id, req, origin):
    """(key, cached): cached — прежний ответ, если запрос уже выполнен. Иначе — отметка started."""
    if not isinstance(op_id, str) or not ID.match(op_id):
        raise ValueError('operation_id: 8–160 символов: буквы, цифры, _ . : - /')
    w = _w(); key = 'req:' + op_id; fp = fingerprint(req, origin)
    with _locked():
        s = w.state(); at = w.now(); ops = ledger(s); rec = ops.get(key)
        if rec:
            if rec.get('fingerprint') != fp:
                raise ValueError('operation_id %s уже использован для другого запроса' % op_id)
            if rec.get('status') == 'done':
                return key, rec.get('result') or dict(ok=True, note='результат не сохранён: слишком большой')
            if rec.get('status') == 'started' and not _stale(rec, at):
                raise ValueError('Запрос %s уже выполняется' % op_id)
        ops[key] = dict(status='started', at=at.isoformat(), fingerprint=fp, op=req.get('op'), ref=req.get('ref'), origin=origin)
        _trim(ops); w.save('workflow.json', s)
    return key, None


def done_request(key, result):
    w = _w()
    try:
        body = json.dumps(result, ensure_ascii=False, default=str)
        keep = json.loads(body) if len(body.encode('utf-8')) <= RESULT_MAX else None
    except (TypeError, ValueError):
        keep = None
    with _locked():
        s = w.state(); ops = ledger(s)
        if key in ops:
            ops[key].update(status='done', finished_at=w.now().isoformat(), result=keep)
            w.save('workflow.json', s)


def release_request(key):
    """Запрос упал с ошибкой — id освобождается: исправленный повтор того же запроса возможен."""
    w = _w()
    with _locked():
        s = w.state(); ops = ledger(s)
        if (ops.get(key) or {}).get('status') == 'started':
            ops.pop(key); w.save('workflow.json', s)
