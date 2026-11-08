"""Права на сведения «Архитектуры систем» (TT-ARCH-001 NET-FR-15.2, ARCH-FR-15.1–2; TT-133, NET-AC-054…056).

Топология — чувствительные метаданные. Доступ решается на уровне API по проекту объекта, а не скрытием в UI:
прямой id чужого объекта даёт тот же ответ, что несуществующий (не подсказывает, что объект есть).

Права (NET-FR-15.2). `run_scan_remote` и `execute_deployment` в MVP не выдаются никому:
  * владелец (доска с токеном владельца) — view_summary, view_sensitive_topology, run_scan_local,
    manage_project_mapping, manage_project_reservations, manage_probe_policy, view_commands — по всем проектам;
  * агент проекта P — view_summary и view_sensitive_topology только по объектам P;
  * все остальные — ничего.
Объект без доказанного проекта (процесс без привязки, неразобранный контейнер, хост, резервации ОС) видит
только владелец: агент проекта не получает чужие адреса через «общие» объекты (NET-AC-056).
"""
OWNER_PERMS = frozenset(('view_summary', 'view_sensitive_topology', 'run_scan_local', 'manage_project_mapping',
                         'manage_project_reservations', 'manage_probe_policy', 'view_commands'))
AGENT_PERMS = frozenset(('view_summary', 'view_sensitive_topology'))
NEVER_IN_MVP = frozenset(('run_scan_remote', 'execute_deployment'))
ALL_PERMS = OWNER_PERMS | NEVER_IN_MVP
NOT_FOUND = 'нет такого объекта или нет прав на него'


def principal(origin, project=None, role=None):
    if origin == 'owner':
        return dict(origin='owner', project=None, role=role, perms=OWNER_PERMS)
    if origin == 'agent' and project:
        return dict(origin='agent', project=project, role=role, perms=AGENT_PERMS)
    return dict(origin=origin or 'anonymous', project=None, role=role, perms=frozenset())


def allowed(p, perm, project=None):
    """Есть ли у субъекта право perm на проект project (None — объект без проекта)."""
    if perm not in ALL_PERMS:
        raise ValueError('Неизвестное право: %s' % perm)
    if perm in NEVER_IN_MVP or perm not in p['perms']:
        return False
    if p['origin'] == 'owner':
        return True
    return project is not None and project == p['project']


def projects_of(obj, index):
    """Проекты объекта — только доказанные: метка Compose, ручная привязка, объявление в файле проекта."""
    k = obj.get('kind')
    if k == 'container':
        return {obj['project']} if obj.get('project') else set()
    if k == 'binding':
        if obj.get('host_id') == 'declared':
            return {obj['owner']} if obj.get('owner') else set()
        owner = index.get(obj.get('owner')) if obj.get('owner') else None
        return projects_of(owner, index) if owner and owner is not obj else set()
    if k == 'reservation':
        return {obj['owner_project']} if obj.get('owner_project') else set()
    if k == 'process' or k == 'service':
        return {obj['project']} if obj.get('project') else set()
    return set()


def visible(obj, p, index):
    if 'view_sensitive_topology' not in p['perms']:
        return False
    if p['origin'] == 'owner':
        return True
    if obj.get('kind') == 'evidence':
        return False                               # доказательство — через объект, который на него ссылается
    return p['project'] in projects_of(obj, index)


def view(snapshot, p):
    """Срез снимка для субъекта: только видимые объекты и их доказательства; источники — без причин отказа
    для агента (в причинах бывают пути и имена команд)."""
    if not p['perms']:
        raise PermissionError(NOT_FOUND)
    index = {o['id']: o for o in snapshot.get('objects', [])}
    objs = [o for o in snapshot.get('objects', []) if visible(o, p, index)]
    if p['origin'] != 'owner':
        refs = {r for o in objs for r in o.get('evidence_refs', [])}
        objs += [index[r] for r in sorted(refs) if r in index]
    out = dict(schema_version=snapshot.get('schema_version'), taken_at=snapshot.get('taken_at'),
               complete=snapshot.get('complete'), objects=objs)
    if p['origin'] == 'owner':
        out.update(sources=snapshot.get('sources'), partial_reasons=snapshot.get('partial_reasons'),
                   configs=snapshot.get('configs'), refused=snapshot.get('refused'), conflicts=snapshot.get('conflicts'))
    else:
        out.update(sources=[dict(collector_id=s['collector_id'], state=s['state']) for s in snapshot.get('sources', [])],
                   configs=[c for c in snapshot.get('configs', []) if c.get('project') == p['project']])
    return out


def get(snapshot, object_id, p):
    """Объект по прямому id. Чужой и несуществующий — одна и та же ошибка (ARCH-FR-15.2)."""
    index = {o['id']: o for o in snapshot.get('objects', [])}
    obj = index.get(object_id)
    if obj is None or not p['perms'] or not visible(obj, p, index):
        raise LookupError(NOT_FOUND)
    return obj


def summary(snapshot, p):
    """Счётчики по видам — право view_summary; для агента — только по его проекту."""
    if 'view_summary' not in p['perms']:
        raise PermissionError(NOT_FOUND)
    counts = {}
    for o in view(snapshot, p)['objects']:
        counts[o['kind']] = counts.get(o['kind'], 0) + 1
    return dict(project=p['project'], complete=snapshot.get('complete'), counts=counts)


def handle(path, query, p, snapshot):
    """Маршруты /api/arch/* поверх прав: (код, тело). Вызывается сервером досок после проверки сессии."""
    route = path[len('/api/arch/'):] if path.startswith('/api/arch/') else ''
    try:
        if snapshot is None:
            return 404, dict(ok=False, error='снимка ещё нет: py tools/arch_collect.py --save в терминале владельца')
        if route == 'summary':
            return 200, dict(ok=True, data=summary(snapshot, p))
        if route == 'graph':
            return 200, dict(ok=True, data=view(snapshot, p))
        if route == 'object':
            return 200, dict(ok=True, data=get(snapshot, (query.get('id') or [''])[0], p))
    except (PermissionError, LookupError):
        return 404, dict(ok=False, error=NOT_FOUND)
    return 404, dict(ok=False, error='нет такого адреса')
