"""Модель данных модуля «Архитектура систем» (TT-ARCH-001 §3, §5, ARCH-FR-14; TT-133: TTN-011, TTN-012, TTN-091…093).

Здесь нет ввода-вывода: только схема объектов, устойчивые идентификаторы и правила, которые держат модель
честной. Коллекторы (arch_collect.py) строят объекты этими функциями, права (arch_acl.py) их фильтруют.

Правила, которые проверяются кодом, а не договорённостью:
  * у объекта нет единого поля `status`: жизненный цикл, сокет, конфигурация, доступность, качество
    доказательства и свежесть — независимые оси (§3.1). Объект с полем status validate() отклоняет;
  * идентификатор — хеш канонического ключа, без времени и номера снимка: повторный сбор даёт тот же id
    (NET-AC-032, NET-AC-061). Ключ bind — (host, namespace, protocol, family, address, port) по §3.2;
    процесс — PID + время создания + хост: PID переиспользуется (NET-AC-006);
  * TCP и UDP, IPv4 и IPv6, хост и контейнер — разные ресурсы (NET-AC-004, NET-AC-005);
  * 0.0.0.0 и :: — шаблон привязки, не адрес для браузера; IPv6 в URL — в квадратных скобках (NET-AC-001…003);
  * у каждого факта есть доказательство (evidence) с видом источника и сроком годности; `verified`
    ставят только правила коллектора, ответ LLM — не выше `inferred` (§3.3, ARCH-FR-14.3).
"""
import hashlib
import ipaddress
import json
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1

KINDS = ('host', 'namespace', 'process', 'service', 'container', 'binding', 'connection', 'reservation',
         'endpoint', 'evidence', 'source')

LIFECYCLE = ('running', 'stopped', 'paused', 'exited', 'created', 'restarting', 'unknown')
SOCKET = ('listening_observed', 'bound_observed', 'connection_observed', 'not_observed', 'unknown')
CONFIG = ('configured', 'exposed_declared', 'published', 'declared_docs', 'not_configured', 'unknown')
REACH = ('reachable_verified', 'unreachable_verified', 'not_probed', 'unknown')
QUALITY = ('verified', 'declared', 'inferred', 'unknown')
FRESH = ('current', 'stale', 'unknown')
RESERVATION_TYPES = ('os_excluded_range', 'http_urlacl', 'http_sslcert', 'portproxy', 'project')
EVIDENCE_KINDS = ('observed_socket', 'observed_trace', 'runtime_metadata', 'declared_config', 'declared_docs',
                  'user_confirmed', 'inferred', 'unknown')
SOURCE_STATES = ('available', 'partial', 'unavailable', 'unknown')
PROTOCOLS = ('tcp', 'udp')
FAMILIES = ('ipv4', 'ipv6')

AXES = dict(lifecycle=LIFECYCLE, socket_state=SOCKET, configuration_state=CONFIG, reachability=REACH,
            evidence_quality=QUALITY, freshness=FRESH)
# Какие оси есть у какого объекта. Отсутствующая ось — не «unknown», а «не применимо».
OBJECT_AXES = dict(process=('lifecycle', 'freshness'), service=('lifecycle', 'configuration_state', 'freshness'),
                   container=('lifecycle', 'freshness'),
                   binding=('socket_state', 'configuration_state', 'reachability', 'evidence_quality', 'freshness'),
                   connection=('socket_state', 'evidence_quality', 'freshness'),
                   reservation=('configuration_state', 'evidence_quality', 'freshness'),
                   endpoint=('reachability', 'evidence_quality', 'freshness'))

TTL = dict(observed_socket=timedelta(minutes=30), runtime_metadata=timedelta(hours=1),
           declared_config=timedelta(days=7), declared_docs=timedelta(days=30), user_confirmed=None,
           observed_trace=timedelta(minutes=30), inferred=timedelta(days=1), unknown=timedelta(hours=1))


def now():
    return datetime.now(timezone.utc)


def iso(t):
    return t.isoformat(timespec='seconds') if t else None


def canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def stable_id(kind, *key):
    """kind-<16 hex>: одинаковый ключ — одинаковый id в любом снимке."""
    if kind not in KINDS:
        raise ValueError('Неизвестный вид объекта: %s' % kind)
    return '%s-%s' % (kind, hashlib.sha256(canon([kind] + [str(k).lower() if k is not None else None for k in key]).encode()).hexdigest()[:16])


# ------------------------------------------------------------------ адреса

def family(ip):
    try:
        return 'ipv6' if ipaddress.ip_address(str(ip).strip('[]').split('%')[0]).version == 6 else 'ipv4'
    except ValueError:
        return None


def scope(ip):
    """wildcard | loopback | link_local | private | public | unknown — для показа, не для доступности."""
    try:
        a = ipaddress.ip_address(str(ip).strip('[]').split('%')[0])
    except ValueError:
        return 'unknown'
    if a.is_unspecified:
        return 'wildcard'
    if a.is_loopback:
        return 'loopback'
    if a.is_link_local:
        return 'link_local'
    return 'private' if a.is_private else 'public'


def url(scheme, ip, port, path=''):
    """Адрес для браузера или None. Шаблон привязки (0.0.0.0, ::) адресом не является; схему не угадываем."""
    if scheme not in ('http', 'https') or scope(ip) in ('wildcard', 'unknown') or not port:
        return None
    host = '[%s]' % str(ip).strip('[]') if family(ip) == 'ipv6' else str(ip)
    return '%s://%s:%d%s' % (scheme, host, int(port), path or '')


# ------------------------------------------------------------------ объекты

def evidence(kind, source, collector, observed_at=None, locator=None, ttl=None, quality=None):
    """Доказательство факта. source — откуда (команда, файл), locator — где именно (файл:строка, поле)."""
    if kind not in EVIDENCE_KINDS:
        raise ValueError('Неизвестный вид доказательства: %s' % kind)
    at = observed_at or now()
    life = TTL[kind] if ttl is None else ttl
    q = quality or dict(observed_socket='verified', runtime_metadata='verified', observed_trace='verified',
                        declared_config='declared', declared_docs='declared', user_confirmed='verified',
                        inferred='inferred', unknown='unknown')[kind]
    if kind == 'inferred' and q == 'verified':
        raise ValueError('Вывод (inferred) не бывает verified: нужен проверяемый источник')
    return dict(kind='evidence', id=stable_id('evidence', kind, source, locator, collector),
                evidence_kind=kind, source=source, locator=locator, collector_id=collector,
                observed_at=iso(at), expires_at=iso(at + life) if life else None, quality=q)


def host(host_id, os_name, alias=None):
    return dict(kind='host', id=stable_id('host', host_id), host_id=host_id, os=os_name, alias=alias)


def namespace(host_id, name, kind='host'):
    """Сетевое пространство: сеть самого хоста, VM Docker Desktop, сеть контейнера (§3.2, NET-FR-04.4)."""
    return dict(kind='namespace', id=stable_id('namespace', host_id, kind, name), host_id=host_id, ns_kind=kind, name=name)


def process(host_id, pid, created=None, image=None, lifecycle='running', name=None):
    """Без времени создания PID — не идентичность: id привязан к (хост, PID, время), а не к PID."""
    return dict(kind='process', id=stable_id('process', host_id, pid, created), host_id=host_id, pid=pid,
                created=created, image=image, name=name, identity='verified' if created else 'unknown',
                lifecycle=lifecycle, freshness='current')


def binding(host_id, ns, protocol, ip, port, evidence_ref, socket_state='listening_observed',
            configuration_state='unknown', owner=None, quality='verified', container_port=None):
    protocol = protocol.lower()
    if protocol not in PROTOCOLS:
        raise ValueError('Протокол привязки: tcp или udp, а не %s' % protocol)
    fam = family(ip)
    if fam is None:
        raise ValueError('Неразборный адрес привязки: %r' % (ip,))
    port = int(port)
    if not 0 <= port <= 65535:
        raise ValueError('Порт вне диапазона: %s' % port)
    return dict(kind='binding', id=stable_id('binding', host_id, ns, protocol, fam, str(ip).strip('[]'), port),
                host_id=host_id, namespace_id=ns, protocol=protocol, family=fam, bind_ip=str(ip).strip('[]'),
                port=port, container_port=container_port, scope=scope(ip), owner=owner,
                socket_state=socket_state, configuration_state=configuration_state, reachability='not_probed',
                evidence_quality=quality, freshness='current', evidence_refs=[evidence_ref])


def connection(host_id, ns, protocol, local_ip, local_port, remote_ip, remote_port, evidence_ref, owner=None):
    """Исходящее/установленное соединение — не сервер (NET-AC-014). Хранится как связь с коротким сроком."""
    return dict(kind='connection', id=stable_id('connection', host_id, ns, protocol, local_ip, local_port, remote_ip, remote_port),
                host_id=host_id, namespace_id=ns, protocol=protocol, local=dict(ip=local_ip, port=int(local_port)),
                remote=dict(ip=remote_ip, port=int(remote_port)), owner=owner, socket_state='connection_observed',
                evidence_quality='verified', freshness='current', evidence_refs=[evidence_ref])


def reservation(host_id, rtype, evidence_ref, protocol=None, start=None, end=None, family=None, url_prefix=None,
                listen=None, connect=None, persistent=None, owner_project=None, reason=None, expires=None):
    """Резервация — не сокет: у неё нет socket_state вовсе (NET-AC-009, NET-AC-010, NET-AC-012)."""
    if rtype not in RESERVATION_TYPES:
        raise ValueError('Неизвестный тип резервации: %s' % rtype)
    key = (host_id, rtype, protocol, family, start, end, url_prefix, canon(listen), canon(connect), owner_project)
    r = dict(kind='reservation', id=stable_id('reservation', *key), host_id=host_id, reservation_type=rtype,
             protocol=protocol, family=family, port_start=start, port_end=end, url_prefix=url_prefix,
             listen=listen, connect=connect, persistent=persistent,
             configuration_state='configured', evidence_quality='declared' if rtype == 'project' else 'verified',
             freshness='current', evidence_refs=[evidence_ref])
    if rtype == 'project':
        r.update(owner_project=owner_project, reason=reason, expires=expires)
    return r


def service(host_id, name, lifecycle, pid=None, start_mode=None, evidence_ref=None):
    return dict(kind='service', id=stable_id('service', host_id, name), host_id=host_id, name=name, pid=pid or None,
                start_mode=start_mode, lifecycle=lifecycle, configuration_state='configured', freshness='current',
                evidence_refs=[evidence_ref] if evidence_ref else [])


def container(daemon_id, container_id, name, lifecycle, compose=None, image=None, evidence_ref=None):
    return dict(kind='container', id=stable_id('container', daemon_id, container_id), daemon_id=daemon_id,
                container_id=container_id, name=name, image=image, compose=compose or {}, lifecycle=lifecycle,
                freshness='current', project=None, project_method='unassigned', project_candidates=[],
                evidence_refs=[evidence_ref] if evidence_ref else [])


def source(collector, state, reason=None, observed_at=None, duration_ms=None):
    if state not in SOURCE_STATES:
        raise ValueError('Состояние источника: %s' % state)
    return dict(kind='source', id=stable_id('source', collector), collector_id=collector, state=state,
                reason=reason, observed_at=iso(observed_at or now()), duration_ms=duration_ms)


# ------------------------------------------------------------------ проверки

def validate(obj):
    """Объект соответствует схеме: известный вид, допустимые значения осей, нет склеенного status."""
    if not isinstance(obj, dict) or obj.get('kind') not in KINDS:
        raise ValueError('Не объект модели: %r' % (obj.get('kind') if isinstance(obj, dict) else obj,))
    if 'status' in obj:
        raise ValueError('Поле status запрещено: оси %s независимы (§3.1)' % ', '.join(AXES))
    for axis in OBJECT_AXES.get(obj['kind'], ()):
        if obj.get(axis) not in AXES[axis]:
            raise ValueError('%s %s: ось %s = %r вне %s' % (obj['kind'], obj.get('id'), axis, obj.get(axis), AXES[axis]))
    if obj['kind'] == 'binding' and obj['socket_state'] in ('listening_observed', 'bound_observed') and obj['evidence_quality'] != 'verified':
        raise ValueError('Наблюдаемый сокет без проверенного доказательства: %s' % obj['id'])
    if obj['kind'] == 'binding' and obj['reachability'] == 'reachable_verified' and not obj.get('vantage'):
        raise ValueError('Доступность без точки наблюдения: %s' % obj['id'])
    return obj


def stale(obj, evidences, at=None):
    """Свежесть по самому свежему доказательству: просроченное наблюдение — stale, а не «исчезло»."""
    at = at or now()
    exp = [evidences[e].get('expires_at') for e in obj.get('evidence_refs', []) if e in evidences]
    if not exp:
        return 'unknown'
    if any(x is None for x in exp):
        return 'current'
    return 'current' if max(datetime.fromisoformat(x) for x in exp) > at else 'stale'


def _overlap(a, b):
    if a['bind_ip'] == b['bind_ip']:
        return True
    return scope(a['bind_ip']) == 'wildcard' or scope(b['bind_ip']) == 'wildcard'


OBSERVED = ('listening_observed', 'bound_observed')


def _slot(b):
    """Ресурс привязки. Объявленные в Compose порты публикуются на одном хосте — пространство Compose
    для них не разделяет порт; наблюдаемые и runtime — со своим хостом и пространством (NET-AC-005)."""
    ns = None if b['host_id'] == 'declared' else b['namespace_id']
    return (b['host_id'], ns, b['protocol'], b['family'], b['port'])


def conflicts(bindings):
    """Возможные пересечения портов (NET-FR-03.5). Другой протокол, семейство, хост или пространство —
    не пересечение (NET-AC-004, NET-AC-005). Два сокета, наблюдаемые одновременно, уже доказали, что
    совместимы, — это не конфликт. Объявленные порты разных проектов — conflict_suspected: одновременный
    запуск не доказан; conflict_confirmed этот модуль без такого доказательства не ставит."""
    out = []
    bs = sorted(bindings, key=lambda b: b['id'])
    for i, a in enumerate(bs):
        for b in bs[i + 1:]:
            if _slot(a) != _slot(b) or not _overlap(a, b):
                continue
            if a['socket_state'] in OBSERVED and b['socket_state'] in OBSERVED:
                continue
            if a.get('owner') and a.get('owner') == b.get('owner'):
                continue
            out.append(dict(kind='conflict_suspected', bindings=[a['id'], b['id']], port=a['port'], protocol=a['protocol'],
                            owners=[a.get('owner'), b.get('owner')]))
    return out


def merge_mapping(observed, manual):
    """Ручная привязка к проекту (owner-confirmed) новым сбором не перетирается (§5.3, NET-FR-01.5)."""
    out = dict(observed)
    m = manual.get(observed['id'])
    if m:
        out.update(project=m['project'], project_method='user_confirmed', project_candidates=[m['project']],
                   confirmed_by=m.get('by'), confirmed_at=m.get('at'))
    return out


def snapshot(objects, sources, taken_at=None):
    """Атомарный набор: объекты без повторов по id, состояние каждого источника, partial с причинами."""
    seen = {}
    for o in objects:
        validate(o)
        if o['id'] in seen and o['kind'] == 'evidence':
            continue
        if o['id'] in seen:
            prev = seen[o['id']]
            prev['evidence_refs'] = sorted(set(prev.get('evidence_refs', [])) | set(o.get('evidence_refs', [])))
            continue
        seen[o['id']] = dict(o)
    bad = [s for s in sources if s['state'] != 'available']
    return dict(schema_version=SCHEMA_VERSION, taken_at=iso(taken_at or now()),
                complete=not bad, partial_reasons=['%s: %s' % (s['collector_id'], s.get('reason') or s['state']) for s in bad],
                sources=sources, objects=sorted(seen.values(), key=lambda o: (o['kind'], o['id'])))
