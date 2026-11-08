"""Поток задач: «сначала довести, потом начать» (FR-014; TT3-080…083).

Учёт незавершённых задач один — журнал спринта `workflow.json` → `delivery_sprints` (sprints.py).
Второго хранилища нет: здесь только правила поверх него.

Режимы (`settings.flow_mode`, меняет только владелец):
  * batch   — действующее правило владельца: спринт из 4 задач, новые ждут main всего состава;
  * rolling — правило FR-014: слот на задачу (`wip_global` на портфель, `wip_per_project`
              на проект), выпуск в main освобождает ровно один слот, готовое к выпуску идёт
              раньше нового (`prefer_finish`).
По умолчанию действует batch, а rolling считается рядом (shadow): решение по каждому приёму
пишется в журнал с вердиктом второго режима, на исполнение это не влияет (AC-14.8).

Слот освобождают только подтверждённый выпуск в main (sprints.in_main) или явная операция
владельца `release_slot` с причиной. Ревью, done без main, пауза, блокировка и сброс лимита —
не освобождают (AC-14.3, AC-14.4). Повторный приём той же задачи второго слота не берёт (AC-14.5).

Исключения владельца (`flow_exception`): critical_incident и owner_override пропускают задачу
мимо лимитов только на приёме; CI, ворота, DoD и права не меняются (AC-14.6). blocked_slot —
временно не считать слот задачи с подтверждённой внешней блокировкой, не больше
`blocked_exception_limit` одновременно. У каждого исключения — причина, срок и журнал.
"""
from datetime import datetime, timedelta, timezone

MODES = ('batch', 'rolling')
DEFAULTS = dict(flow_mode='batch', wip_global=4, wip_per_project=2, prefer_finish=True, blocked_exception_limit=1)
EXCEPTIONS = ('critical_incident', 'owner_override', 'blocked_slot')
EXCEPTION_DAYS = 30
FINISH_STAGES = ('deploy_main', 'testing', 'deploy_test')   # готовое к выпуску: порядок важности
KEEP = 300                                                  # записей в журнале решений


def _now():
    return datetime.now(timezone.utc)


def _parse(v):
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def byref(tasks):
    return {t['ref']: t for t in tasks} if isinstance(tasks, list) else (tasks or {})


def conf(s):
    st = (s or {}).get('settings') or {}
    return {k: st.get(k, v) for k, v in DEFAULTS.items()}


def mode(s):
    m = conf(s)['flow_mode']
    return m if m in MODES else 'batch'


def ledger(s):
    return s.setdefault('delivery_sprints', dict(sequence=0, current=None, history=[], legacy=[]))


def _version(s):
    return ((s.get('policy') or {}).get('version')) or 0


def journal(s, decision, ref, why='', at=None, **kw):
    """Детерминированное решение потока (FR-014 п.8): что, по какой задаче, по какой политике, почему."""
    log = ledger(s).setdefault('decisions', [])
    log.append(dict(at=(at or _now()).isoformat(), decision=decision, ref=ref, mode=mode(s),
                    policy_version=_version(s), why=str(why)[:300], **kw))
    del log[:-KEEP]


# ---------------------------------------------------------------- слоты

def slots(s):
    return ledger(s).setdefault('slots', {})


def closed(s, ref, tasks):
    """Слот задачи закрыт: подтверждённый выпуск в main или явное закрытие владельцем."""
    import sprints
    slot = slots(s).get(ref) or {}
    return bool(slot.get('released_by') == 'owner' or sprints.in_main(s, byref(tasks).get(ref)))


def sync_slots(s, tasks, at=None):
    """Слоты из состава спринта и миграции; выпуск в main закрывает слот ровно один раз."""
    import sprints
    led = ledger(s); sl = slots(s); tasks = byref(tasks); at = at or _now()
    # Составы по проектам (sprints.currents) и прежний общий состав — до его раскладки по проектам.
    groups = [(None, led.get('legacy') or [])] + [(c.get('opened_at'), c.get('refs') or [])
                                                   for c in [led.get('current') or {}] + list((led.get('currents') or {}).values())]
    for opened, refs in groups:
        for r in refs:
            if r not in sl:
                sl[r] = dict(project=r.split('/')[0], admitted_at=opened, admission_id='adm:%s:1' % r, origin='sprint')
    for r, slot in sl.items():
        if slot.get('closed_at'):
            continue
        if sprints.in_main(s, tasks.get(r)):
            pipe = (tasks.get(r) or {}).get('pipeline') or {}
            evidence = (pipe.get('stages', {}).get('deploy_main') or {}).get('passed') or (s.get('main_receipts') or {}).get(r)
            slot.update(closed_at=at.isoformat(), released_by='release', release_evidence=str(evidence)[:200] if evidence else None)
            journal(s, 'release_complete', r, 'выпуск в main подтверждён', at)
    return sl


def open_refs(s, tasks):
    return [r for r, slot in slots(s).items() if not slot.get('closed_at') and not closed(s, r, tasks)]


def take_slot(s, ref, at, origin='agent'):
    """Занять слот (под общей блокировкой). Повтор — тот же слот; повторно открытая задача — новый."""
    sl = slots(s); slot = sl.get(ref)
    if slot and not slot.get('closed_at'):
        return False
    n = int((slot or {}).get('admission_id', 'adm:x:0').rsplit(':', 1)[-1] or 0) + 1
    sl[ref] = dict(project=ref.split('/')[0], admitted_at=at.isoformat(), admission_id='adm:%s:%d' % (ref, n),
                   origin=origin, **({'reopened': True} if slot else {}))
    return True


# ---------------------------------------------------------------- блокировки и исключения

def blocks(s):
    return ledger(s).setdefault('blocked', {})


def exceptions(s):
    return ledger(s).setdefault('exceptions', [])


def active_exceptions(s, at=None):
    at = at or _now()
    return [e for e in exceptions(s) if not e.get('revoked_at') and (_parse(e.get('expires_at')) or at) > at]


def exception_for(s, ref, at=None, kinds=('critical_incident', 'owner_override')):
    return next((e for e in active_exceptions(s, at) if e['ref'] == ref and e['kind'] in kinds), None)


def borrowed(s, at=None):
    """Слоты задач с внешней блокировкой, которые владелец временно разрешил не считать."""
    lim = int(conf(s)['blocked_exception_limit'])
    refs = [e['ref'] for e in active_exceptions(s, at) if e['kind'] == 'blocked_slot' and e['ref'] in blocks(s)]
    return refs[:lim]


def block(s, ref, reason, responsible, recheck_at, at, origin):
    if len((reason or '').strip()) < 5:
        raise ValueError('reason: что блокирует (не короче 5 символов)')
    if not (responsible or '').strip():
        raise ValueError('responsible: кто снимает блокировку')
    when = _parse(recheck_at)
    if not when or when <= at:
        raise ValueError('recheck_at: когда перепроверить (ISO-время в будущем)')
    blocks(s)[ref] = dict(reason=reason.strip()[:300], responsible=responsible.strip()[:80], recheck_at=when.isoformat(),
                          since=(blocks(s).get(ref) or {}).get('since') or at.isoformat(), by=origin)
    journal(s, 'block', ref, reason, at, responsible=responsible)
    return blocks(s)[ref]


def unblock(s, ref, at, origin):
    if ref not in blocks(s):
        raise ValueError('Задача %s не отмечена заблокированной' % ref)
    b = blocks(s).pop(ref)
    for e in exceptions(s):
        if e['ref'] == ref and e['kind'] == 'blocked_slot' and not e.get('revoked_at'):
            e['revoked_at'] = at.isoformat()
    journal(s, 'unblock', ref, 'блокировка снята (%s)' % origin, at, since=b.get('since'))
    return b


def add_exception(s, ref, kind, reason, expires_at, at, origin):
    if kind not in EXCEPTIONS:
        raise ValueError('kind: ' + ', '.join(EXCEPTIONS))
    if len((reason or '').strip()) < 10:
        raise ValueError('reason: обоснование исключения (не короче 10 символов)')
    until = _parse(expires_at)
    if not until or until <= at or until > at + timedelta(days=EXCEPTION_DAYS):
        raise ValueError('expires_at: срок исключения — ISO-время в ближайшие %d дней' % EXCEPTION_DAYS)
    if kind == 'blocked_slot':
        if ref not in blocks(s):
            raise ValueError('blocked_slot — только для задачи с подтверждённой внешней блокировкой (op=flow_block)')
        if len(borrowed(s, at)) >= int(conf(s)['blocked_exception_limit']):
            raise ValueError('Лимит исключений по блокировкам исчерпан (blocked_exception_limit=%s)' % conf(s)['blocked_exception_limit'])
    e = dict(id='FX-%d' % (len(exceptions(s)) + 1), ref=ref, kind=kind, reason=reason.strip()[:300],
             expires_at=until.isoformat(), approved_by=origin, at=at.isoformat())
    exceptions(s).append(e)
    journal(s, 'override', ref, reason, at, kind=kind, exception=e['id'], expires_at=e['expires_at'])
    return e


def release_slot(s, ref, reason, tasks, at, origin):
    """Явное закрытие слота владельцем: отменённая или брошенная задача больше не держит поток."""
    if len((reason or '').strip()) < 5:
        raise ValueError('reason: почему слот закрывается без выпуска (не короче 5 символов)')
    sync_slots(s, tasks, at)
    slot = slots(s).get(ref)
    if not slot or slot.get('closed_at'):
        raise ValueError('У задачи %s нет занятого слота' % ref)
    slot.update(closed_at=at.isoformat(), released_by='owner', release_reason=reason.strip()[:300])
    led = ledger(s)
    led['legacy'] = [r for r in led.get('legacy') or [] if r != ref]
    journal(s, 'release_slot', ref, reason, at, by=origin)
    return slot


# ---------------------------------------------------------------- готовое к выпуску

def finish_candidates(s, tasks, refs=None, at=None):
    """Принятые задачи, у которых есть исполнимая поздняя стадия: запуск роли ждёт ресурса,
    не на паузе и не открыт. Ждущее владельца или идущее сейчас — не кандидат (ресурс не нужен)."""
    tasks = byref(tasks); at = at or _now(); refs = set(refs if refs is not None else open_refs(s, tasks))
    out = []
    for r in s.get('runs') or []:
        t = tasks.get(r.get('ref'))
        if r.get('ref') not in refs or not t or not r.get('role') or r.get('status') != 'queued' or r.get('paused'):
            continue
        pipe = t.get('pipeline') or {}
        if pipe.get('blocked') or r.get('ref') in blocks(s) or r.get('stage') not in FINISH_STAGES or pipe.get('stage') != r.get('stage'):
            continue
        out.append(dict(ref=r['ref'], run=r['id'], role=r['role'], stage=r['stage']))
    out.sort(key=lambda c: (FINISH_STAGES.index(c['stage']), c['ref']))
    seen = set()
    return [c for c in out if not (c['ref'] in seen or seen.add(c['ref']))]


def finish_rank(stage, role=None, rework=False):
    """Порядок диспетчера FR-014 п.3: релиз → тест → ревью → доработка → анализ → новое."""
    order = ['deploy_main', 'testing', 'deploy_test', 'review', 'development', 'analysis', 'idea']
    if role == 'resolver':
        return -1
    i = order.index(stage) if stage in order else order.index('development')
    return i - 0.5 if rework and stage == 'development' else i


# ---------------------------------------------------------------- решение о приёме

def rolling_codes(s, ref, tasks, at=None):
    """[(код, пояснение)] — почему rolling не принял бы задачу ref; [] — принял бы."""
    tasks = byref(tasks); at = at or _now(); c = conf(s)
    sync_slots(s, tasks, at)
    held = open_refs(s, tasks)
    if ref in held or (ref and exception_for(s, ref, at)):
        return []
    lent = set(borrowed(s, at))
    counted = [r for r in held if r not in lent]
    out = []
    if len(counted) >= int(c['wip_global']):
        out.append(('WIP_GLOBAL_LIMIT', 'в потоке %d задач из %d: %s' % (len(counted), c['wip_global'], ', '.join(sorted(counted)))))
    if ref:
        proj = ref.split('/')[0]
        mine = [r for r in counted if r.split('/')[0] == proj]
        if len(mine) >= int(c['wip_per_project']):
            out.append(('PROJECT_WIP_LIMIT', 'в проекте %s %d задач из %d' % (proj, len(mine), c['wip_per_project'])))
    if c['prefer_finish']:
        fin = finish_candidates(s, tasks, held, at)
        if fin:
            out.append(('FINISH_READY_FIRST', 'сначала довести: ' + ', '.join('%s (%s)' % (x['ref'], x['stage']) for x in fin)))
    return out


def rolling_reason(s, ref, tasks, at=None):
    codes = rolling_codes(s, ref, tasks, at)
    return ('Поток (FR-014): ' + '; '.join(t for _, t in codes)) if codes else None


# ---------------------------------------------------------------- снимок потока

def snapshot(s, tasks, at=None, project=None):
    """flow_snapshot (FR-014 п.7). Считается на копии состояния — ничего не меняет.
    project — срез одного проекта: спринт batch у каждого проекта свой (sprints.SCOPE),
    чужие задачи в срезе не показываются и приём не запрещают."""
    import copy
    import sprints
    at = at or _now()
    st = dict(s); st['delivery_sprints'] = copy.deepcopy(s.get('delivery_sprints') or dict(sequence=0, current=None, history=[], legacy=[]))
    tasks = byref(tasks)
    sprints.sync(st, tasks)
    sync_slots(st, tasks, at)
    held = open_refs(st, tasks); c = conf(st); m = mode(st)
    if project:
        held = [r for r in held if sprints.project_of(r) == project]
    buckets = dict(in_work=[], review_waiting=[], test_waiting=[], release_waiting=[], blocked=[])
    rows = []
    for r in sorted(held):
        t = tasks.get(r) or {}; pipe = t.get('pipeline') or {}; stage = pipe.get('stage') or t.get('status') or ''
        blocked = bool(pipe.get('blocked') or r in blocks(st) or t.get('status') == 'blocked')
        b = ('blocked' if blocked else 'release_waiting' if stage == 'deploy_main' else
             'test_waiting' if stage in ('deploy_test', 'testing') else 'review_waiting' if stage in ('review',) or t.get('status') == 'review'
             else 'in_work')
        buckets[b].append(r)
        since = _parse(slots(st)[r].get('admitted_at'))
        paused = any(x.get('ref') == r and x.get('paused') and x.get('status') in ('queued', 'running') for x in st.get('runs') or [])
        rows.append(dict(ref=r, title=t.get('title'), stage=stage, bucket=b, paused=paused, status=t.get('status'),
                         age_days=round((at - since).total_seconds() / 86400, 1) if since else None))
    blocked_rows = []
    for r, b in blocks(st).items():
        since = _parse(b.get('since')); due = _parse(b.get('recheck_at'))
        blocked_rows.append(dict(b, ref=r, age_days=round((at - since).total_seconds() / 86400, 1) if since else None,
                                 overdue=bool(due and due <= at), counted=r not in borrowed(st, at), source='flow_block'))
    # Блокировки конвейера (проблема у Решалы, ждёт владельца) и ручной статус «Заблокировано» считаются в счётчике —
    # значит, и в списке должны быть с причиной и ответственным: иначе «заблокировано 4» и пустая сводка (A11-06).
    for r in buckets['blocked']:
        if r in blocks(st):
            continue
        t = tasks.get(r) or {}; pb = (t.get('pipeline') or {}).get('blocked') or {}
        if pb.get('owner'):
            who = 'владелец'
        elif pb.get('problem'):
            who = 'Решала (%s)' % pb['problem']
        else:
            who = pb.get('role') or 'владелец задачи'
        since = _parse(pb.get('at'))
        blocked_rows.append(dict(ref=r, reason=(pb.get('reason') or t.get('status_note') or 'причина не указана')[:300],
                                 responsible=who, problem=pb.get('problem'), since=pb.get('at'), recheck_at=None,
                                 age_days=round((at - since).total_seconds() / 86400, 1) if since else None,
                                 overdue=False, counted=True, source='pipeline' if pb else 'status'))
    batch_why = sprints.batch_reason(st, None, tasks, project=project)
    batch_by_project = sprints.blocked_projects(st, tasks)
    roll = rolling_codes(st, None, tasks, at)
    allowed = dict(batch=batch_why is None, rolling=not roll)
    nxt = None
    # Общий запрет (весь портфель, готовое к выпуску) — перебирать бэклог незачем.
    shut = batch_why if m == 'batch' else any(k != 'PROJECT_WIP_LIMIT' for k, _ in roll)
    for t in [] if shut else sorted((t for t in tasks.values() if not project or sprints.project_of(t.get('ref')) == project), key=lambda t: ({'🔴': 0, '🟡': 1, '🟢': 2}.get(t.get('priority'), 1), t.get('ref', ''))):
        if t.get('ref') in held or t.get('status') in ('done', 'cancelled') or t.get('needs_plan') or t.get('blocked_by'):
            continue
        why = sprints.reason(st, t['ref'], tasks, at=at)
        if not why:
            nxt = t['ref']; break
    decisions = ledger(st).get('decisions') or []
    return dict(mode=m, shadow='rolling' if m == 'batch' else 'batch', policy=c, policy_version=_version(st),
                project=project, sprint_scope=sprints.SCOPE, batch_by_project=batch_by_project,
                active_wip=len(held), counts={k: len(v) for k, v in buckets.items()}, buckets=buckets, tasks=rows,
                blocked=blocked_rows, exceptions=active_exceptions(st, at),
                new_task_admission_allowed=allowed[m], allowed=allowed,
                reason_codes=[k for k, _ in roll] if m == 'rolling' else (['SPRINT_BATCH'] if batch_why else []),
                reasons=dict(batch=batch_why, rolling=[t for _, t in roll]),
                finish_candidates=finish_candidates(st, tasks, held, at), next_admissible=nxt,
                divergences=[d for d in decisions if d.get('shadow_differs')][-20:], decisions=decisions[-20:])
