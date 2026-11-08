"""Спринт поставки: у каждого проекта свой состав максимум из 4 задач до подтверждённого main.

Решение владельца 2026-11-08: задачи одного проекта не держат спринт другого. Раньше
спринт был общий на портфель, и 39 начатых задач ege запрещали новую работу в tasktreker.
Теперь и «ранее начатые» (legacy), и текущий состав считаются по проекту задачи — префиксу
ссылки `проект/ID`. Внутри проекта правило прежнее: не больше 4 задач, новые ждут main
всего состава, done, ревью, пауза и отмена места не освобождают.

Не путать с временными окнами расхода в metrics.py. Изменения состояния вызываются
только под общей блокировкой workflow; просмотр работает с копией.

Это режим потока batch. Режим rolling (FR-014) и shadow-сравнение — flow_policy.py: журнал
тот же, слоты задач лежат в нём же (`slots`). Задача закрывает своё место выпуском в main
или явным закрытием слота владельцем (op=release_slot) — отменённая задача больше не держит
спринт вечно.
"""
import copy
from datetime import datetime, timezone

import flow_policy as fp

LIMIT = 4
SCOPE = 'project'
RULE = ('В спринте проекта не более 4 задач на всех агентов. Задачи других проектов этот спринт не занимают. '
        'Новые задачи проекта не брать, пока весь его текущий состав не доведён до main. '
        'Ревью, тестирование, done, отмена, пауза и сброс лимита токенов место не освобождают.')


def project_of(ref):
    return str(ref or '').split('/')[0]


def in_main(s, task):
    if not task:
        return False
    pipe = task.get('pipeline')
    if pipe:
        # Эти поля ставит сервер только после всех машинных проверок деплоя и CI.
        return pipe.get('stage') == 'done' and bool((pipe.get('stages', {}).get('deploy_main') or {}).get('passed'))
    return task.get('status') == 'done' and bool((s.get('main_receipts') or {}).get(task['ref']))


def begun(task, runs):
    if task.get('status') in ('done', 'cancelled'):
        return False                    # не затягиваем всю историческую базу в миграцию
    pipe = task.get('pipeline') or {}
    return (task.get('status') in ('in_progress', 'in-progress', 'review') or
            pipe.get('stage') not in (None, 'idea', 'done') or
            any(r.get('ref') == task['ref'] and (r.get('claimed_at') or r.get('launched_at') or
                r.get('status') == 'running') for r in runs))


def currents(ledger):
    """Текущие составы по проектам {проект: {id, project, refs, opened_at}}.
    Общий состав прежней версии (`current`) раскладывается по проектам один раз, номер спринта сохраняется."""
    cur = ledger.setdefault('currents', {})
    old = ledger.get('current')
    if old:
        for r in old.get('refs') or []:
            p = project_of(r)
            c = cur.setdefault(p, dict(id=old.get('id'), project=p, refs=[], opened_at=old.get('opened_at')))
            if r not in c['refs']:
                c['refs'].append(r)
    ledger['current'] = None
    ledger['scope'] = SCOPE
    return cur


def sync(s, tasks):
    tasks = {t['ref']: t for t in tasks} if isinstance(tasks, list) else tasks
    ledger = s.get('delivery_sprints')
    if ledger is None:
        old = sorted(t['ref'] for t in tasks.values() if begun(t, s.get('runs', [])) and not in_main(s, t))
        ledger = s['delivery_sprints'] = dict(sequence=0, current=None, currents={}, history=[], legacy=old)
    ledger['legacy'] = [r for r in ledger.get('legacy', []) if not fp.closed(s, r, tasks)]
    cur = currents(ledger)
    for p, c in list(cur.items()):
        if c['refs'] and all(fp.closed(s, r, tasks) for r in c['refs']):
            ledger.setdefault('history', []).append(dict(c, closed_at=datetime.now(timezone.utc).isoformat()))
            del cur[p]
    fp.sync_slots(s, tasks)
    return ledger


def reason(s, ref, tasks, at=None):
    """Почему задачу ref сейчас не принимать (None — можно) по действующему режиму потока.
    Исключение владельца (critical_incident, owner_override) пропускает мимо лимита в обоих режимах."""
    ledger = sync(s, tasks)
    if fp.exception_for(s, ref, at):
        return None
    if fp.mode(s) == 'rolling':
        return fp.rolling_reason(s, ref, tasks, at)
    return batch_reason(s, ref, tasks, ledger)


def batch_reason(s, ref, tasks, ledger=None, project=None):
    """Правило спринта проекта. ref=None — можно ли принять какую-нибудь новую задачу проекта `project`;
    без проекта ответ «можно»: спринт общий только внутри проекта, решение — по каждой задаче."""
    ledger = ledger or sync(s, tasks)
    proj = project_of(ref) if ref else project
    if not proj:
        return None
    legacy = [r for r in ledger['legacy'] if project_of(r) == proj]
    if legacy:
        if ref in legacy:
            return None
        return 'Сначала довести до main уже начатые задачи проекта %s: %s' % (proj, ', '.join(legacy))
    current = currents(ledger).get(proj)
    if not current or ref in current['refs']:
        return None
    byref = {t['ref']: t for t in tasks} if isinstance(tasks, list) else tasks
    # После первого поставленного результата состав не дополняется даже при <4 задачах.
    if len(current['refs']) >= LIMIT or any(in_main(s, byref.get(r)) for r in current['refs']):
        return 'Спринт %s проекта %s: новые задачи ждут main всего состава (%s)' % (current['id'], proj, ', '.join(current['refs']))
    return None


def blocked_projects(s, tasks, ledger=None):
    """{проект: причина} — проекты, где batch сейчас не принял бы новую задачу."""
    ledger = ledger or sync(s, tasks)
    out = {}
    for p in sorted({project_of(r) for r in ledger['legacy']} | set(currents(ledger))):
        why = batch_reason(s, None, tasks, ledger, project=p)
        if why:
            out[p] = why
    return out


def admit(s, ref, tasks, at=None, origin='agent'):
    """Принять задачу в поток (только под общей блокировкой). Повтор идемпотентен: второй слот
    той же задаче не выдаётся. Решение пишется в журнал вместе с вердиктом второго режима (shadow)."""
    at = at or datetime.now(timezone.utc)
    why = reason(s, ref, tasks, at)
    if why:
        raise ValueError(why)
    fresh = ref not in fp.open_refs(s, tasks)
    if fresh:
        other = fp.rolling_reason(s, ref, tasks, at) if fp.mode(s) == 'batch' else batch_reason(s, ref, tasks)
        exc = fp.exception_for(s, ref, at)
    byref = {t['ref']: t for t in tasks} if isinstance(tasks, list) else tasks
    if byref.get(ref, {}).get('status') != 'done':
        s.get('main_receipts', {}).pop(ref, None)  # повторно открытая задача требует нового подтверждения
    ledger = s['delivery_sprints']
    if fresh and fp.take_slot(s, ref, at, origin):
        fp.journal(s, 'admit', ref, 'исключение %s' % exc['id'] if exc else 'в пределах правила', at,
                   shadow=fp.mode(s) == 'batch' and 'rolling' or 'batch', shadow_verdict=other or 'admit',
                   shadow_differs=bool(other), **({'exception': exc['id']} if exc else {}))
    if ref in ledger['legacy']:
        return
    cur = currents(ledger); p = project_of(ref)
    if p not in cur:
        ledger['sequence'] = ledger.get('sequence', 0) + 1
        cur[p] = dict(id='SPR-%d' % ledger['sequence'], project=p, refs=[], opened_at=datetime.now(timezone.utc).isoformat())
    if ref not in cur[p]['refs']:
        cur[p]['refs'].append(ref)


def view(s, tasks, project=None):
    """Состав спринтов. С project — один проект (current, legacy, pending только его);
    без — сводка: legacy и pending по всем проектам и раскладка `projects`."""
    # История запусков может быть большой; просмотр меняет только журнал спринтов.
    state = dict(s)
    if 'delivery_sprints' in s:
        state['delivery_sprints'] = copy.deepcopy(s['delivery_sprints'])
    ledger = sync(state, tasks)
    byref = {t['ref']: t for t in tasks} if isinstance(tasks, list) else tasks
    cur = currents(ledger)

    def one(p):
        legacy = [r for r in ledger['legacy'] if project_of(r) == p]
        refs = legacy or (cur.get(p) or {}).get('refs', [])
        return dict(current=cur.get(p), legacy=legacy, pending=[r for r in refs if not fp.closed(state, r, byref)],
                    hold=batch_reason(state, None, byref, ledger, project=p))

    base = dict(limit=LIMIT, scope=SCOPE, rule=RULE, mode=fp.mode(state))
    if project:
        return dict(base, project=project, **one(project))
    by = {p: one(p) for p in sorted({project_of(r) for r in ledger['legacy']} | set(cur))}
    return dict(base, project=None, current=None, legacy=ledger['legacy'],
                pending=[r for x in by.values() for r in x['pending']], projects=by)
