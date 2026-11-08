"""Схема графа кода модуля «Архитектура систем» (TT-ARCH-001 ARCH-FR-01, ARCH-FR-02; TT-134: TTN-091…094, TTN-101).

Слой «статическая реализация» (§3 ARCH): узлы — репозиторий, пакеты, модули, классы, функции, маршруты, команды
CLI, фоновые задания; связи — contains, imports, calls_static, handles_route, handles_command, starts_job.
Ввода-вывода здесь нет: коллектор (arch_static.py) строит объекты этими функциями.

Правила, которые проверяются кодом:
  * тип связи задаёт допустимые виды концов (TTN-092). imports — только между модулями, calls_static — от кода
    к коду, handles_route — от кода к маршруту. Связь с концами не того вида validate_edge() отклоняет;
  * связи с разной семантикой не склеиваются (ARCH-FR-01.5): id связи — хеш (тип, начало, конец). Импорт модуля
    и вызов его функции — две разные связи. Сетевой связи из кода этот слой не строит вовсе (NET-AC-030, 078):
    типа «сетевое соединение» в CODE_EDGES нет;
  * id узла — хеш (вид, repo_id, полное имя). Номера строк и SHA в id не входят: повторный скан неизменного
    кода даёт те же id (TTN-093). Одинаковое имя в другом пакете или репозитории — другой узел (NET-AC-072);
  * у каждой связи есть provenance: repo_id, commit_sha, файл, строка, версия разборщика (TTN-094, NET-AC-070);
  * динамический импорт, getattr, eval — не связь, а запись unresolved с местом и причиной (ARCH-FR-02.5);
  * версия кода, версия проекта и запущенное — разные сведения (TTN-101, ARCH-FR-04.2): снимок кода хранит
    свой commit_sha, а deployment_state без доказательства идентичности артефакта — not_verified.
"""
import hashlib

import arch_model as m

GRAPH_VERSION = 1

NODE_KINDS = ('repo', 'package', 'module', 'class', 'function', 'route', 'cli_command', 'job', 'external_module')
CODE = ('module', 'class', 'function')
# Допустимые концы каждого типа связи: (виды начала, виды конца).
CODE_EDGES = {
    'contains': (('repo', 'package', 'module', 'class', 'function'), ('package', 'module', 'class', 'function')),
    'imports': (('module',), ('module', 'external_module')),
    'calls_static': (CODE, ('class', 'function')),
    'handles_route': (CODE, ('route',)),
    'handles_command': (CODE, ('cli_command',)),
    'starts_job': (CODE, ('job',)),
}
# Типы связей единого графа (ARCH-FR-01.3), которых этот слой не строит: они требуют runtime, конфигурации
# или процесса. Код, который пытается их построить из AST, ошибочен.
OTHER_LAYERS = ('deployed_as', 'runs_on', 'calls_observed', 'publishes_event', 'consumes_event', 'reads_data',
                'writes_data', 'uses_database', 'uses_ipc', 'exposes_endpoint', 'flows_to', 'realizes_process_step',
                'initiates', 'depends_on', 'implements', 'network_connects')
RESOLUTION = ('resolved', 'external', 'ambiguous', 'dynamic_unresolved')
UNRESOLVED_REASONS = ('dynamic_import', 'getattr', 'eval_exec', 'dispatch_lambda', 'dispatch_unknown', 'star_import')
PARSE = ('parsed', 'syntax_error', 'skipped_size', 'skipped_encoding', 'unsupported')
DEPLOYMENT = ('deployed_verified', 'not_verified', 'mismatch')


def gid(kind, *key):
    return '%s-%s' % (kind, hashlib.sha256(m.canon([kind] + [str(k) if k is not None else None for k in key]).encode()).hexdigest()[:16])


def node(kind, repo_id, qualname, file=None, line=None, **extra):
    if kind not in NODE_KINDS:
        raise ValueError('Неизвестный вид узла кода: %s' % kind)
    return dict(kind=kind, id=gid(kind, repo_id, qualname), repo_id=repo_id, qualname=qualname, file=file, line=line, **extra)


def provenance(repo_id, commit_sha, file, line, parser, col=None):
    return dict(repo_id=repo_id, commit_sha=commit_sha, file=file, line=line, col=col, parser=parser)


def edge(etype, src, dst, prov, resolution='resolved', quality=None, **extra):
    """Связь между двумя узлами. id не зависит от места: все места одной связи — в locations.
    verified — только для разрешённой связи; ambiguous и dynamic_unresolved — не выше inferred."""
    q = quality or ('verified' if resolution == 'resolved' else 'inferred')
    if q == 'verified' and resolution not in ('resolved', 'external'):
        raise ValueError('Связь с разрешением %s не может быть verified' % resolution)
    e = dict(kind='edge', type=etype, id=gid('edge', etype, src['id'], dst['id']), source=src['id'], target=dst['id'],
             source_kind=src['kind'], target_kind=dst['kind'], resolution=resolution, evidence_quality=q,
             locations=[prov], **extra)
    return validate_edge(e)


def unresolved(src, reason, prov, hint=None):
    if reason not in UNRESOLVED_REASONS:
        raise ValueError('Причина неразрешённого места: %s' % reason)
    return dict(kind='unresolved', id=gid('unresolved', src['id'], reason, prov['file'], prov['line'], prov.get('col')),
                source=src['id'], reason=reason, resolution='dynamic_unresolved', hint=hint, location=prov)


def validate_edge(e):
    t = e.get('type')
    if t in OTHER_LAYERS:
        raise ValueError('Связь %s не строится из статического кода: нужен runtime или конфигурация (ARCH §3)' % t)
    if t not in CODE_EDGES:
        raise ValueError('Неизвестный тип связи: %r' % (t,))
    srcs, dsts = CODE_EDGES[t]
    if e.get('source_kind') not in srcs or e.get('target_kind') not in dsts:
        raise ValueError('%s: %s → %s недопустимо, нужно %s → %s' % (t, e.get('source_kind'), e.get('target_kind'), srcs, dsts))
    if e.get('resolution') not in RESOLUTION:
        raise ValueError('Разрешение связи: %r' % (e.get('resolution'),))
    if not e.get('locations') or any(not p.get('file') or not p.get('repo_id') for p in e['locations']):
        raise ValueError('Связь %s без provenance (файл, repo_id)' % e.get('id'))
    return e


def version_ref(repo_id, commit_sha, branch=None, dirty=None):
    """Версия, на которой собран граф кода. Это не версия «запущенного»: deployment_state отдельно."""
    return dict(repo_id=repo_id, commit_sha=commit_sha, branch=branch, dirty=dirty)


def deployment(version, runtime=None):
    """Связь кода с запущенным (ARCH-FR-04.4, 04.5, TTN-101). deployed_verified — только если runtime
    доказал тот же полный commit_sha артефакта (метка образа, сборка) и рабочее дерево графа не грязное.
    Другой SHA — mismatch с обеими версиями. Нет доказательства — not_verified."""
    rt = runtime or {}
    sha = rt.get('commit_sha')
    if not sha or not version.get('commit_sha'):
        return dict(state='not_verified', code_sha=version.get('commit_sha'), runtime_sha=sha,
                    reason='нет доказательства версии запущенного артефакта')
    if sha != version['commit_sha']:
        return dict(state='mismatch', code_sha=version['commit_sha'], runtime_sha=sha, reason='SHA кода и артефакта разные')
    if version.get('dirty'):
        return dict(state='not_verified', code_sha=version['commit_sha'], runtime_sha=sha,
                    reason='граф собран с незакоммиченными правками')
    return dict(state='deployed_verified', code_sha=sha, runtime_sha=sha, evidence=rt.get('evidence'))


def graph(version, nodes, edges, unresolved_items, coverage, mapping):
    """Снимок графа кода: узлы и связи без повторов, места одной связи слиты."""
    seen_n, seen_e = {}, {}
    for n in nodes:
        seen_n.setdefault(n['id'], n)
    for e in edges:
        validate_edge(e)
        if e['source'] not in seen_n or e['target'] not in seen_n:
            raise ValueError('Связь %s указывает на узел вне графа' % e['id'])
        if e['id'] in seen_e:
            have = seen_e[e['id']]['locations']
            have.extend(p for p in e['locations'] if p not in have)
        else:
            seen_e[e['id']] = dict(e, locations=list(e['locations']))
    un = {u['id']: u for u in unresolved_items}
    return dict(graph_version=GRAPH_VERSION, layer='static', version_ref=version, deployment=deployment(version),
                project=mapping, coverage=coverage,
                nodes=sorted(seen_n.values(), key=lambda n: n['id']),
                edges=sorted(seen_e.values(), key=lambda e: e['id']),
                unresolved=sorted(un.values(), key=lambda u: u['id']))
