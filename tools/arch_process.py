"""Бизнес-процессы и трассировка шаг → код → сервис → порт (TT-ARCH-001 ARCH-FR-05, ARCH-FR-07, §6A.1;
TT-135: TTN-112…123, NET-AC-084…099, NET-AC-129).

Процесс — определение из источника (BRD, SRS, BPMN, документ, тест) или от владельца. Предложение ИИ всегда
`proposed`, утверждает его только владелец с доказательством (TTN-115, NET-AC-089). Код здесь процессов не
выводит: технический endpoint без привязки остаётся `not_documented` (ARCH-FR-05.6, NET-AC-088).

Трассировка (ARCH-FR-07) идёт по переходам, и каждый переход проверяется отдельно:
  шаг —realizes_process_step→ маршрут/команда/функция —handles_route→ символ —deployed_as→ объект runtime
  —exposes_endpoint→ порт.
Где переход не доказан, путь обрывается с кодом причины (REASONS), а не достраивается (TTN-120, NET-AC-093).
Режимы §6A.1 не смешиваются: путь по определению процесса — planned_process_path, по графу кода —
static_implementation_path; наблюдаемого исполнения этот модуль не утверждает никогда (TTN-121).

Обратный поиск (TTN-122, NET-AC-098, 099) отвечает «затронуто», «возможно затронуто» или «неизвестно».
Ответа «ничего не затронуто» нет: отсутствие привязки — это неизвестность, а не ноль.
"""
import hashlib
import json
import os
import re
from pathlib import Path

import arch_acl
import arch_graph as g
import arch_model as m

STEP_TYPES = ('start', 'end', 'task', 'manual', 'gateway_exclusive', 'gateway_inclusive', 'gateway_parallel',
              'timer', 'message_send', 'message_receive', 'external', 'error_end', 'compensation', 'subprocess')
GATEWAYS = ('gateway_exclusive', 'gateway_inclusive', 'gateway_parallel')
FLOW_KINDS = ('sequence', 'conditional', 'default', 'async_message', 'error', 'timeout', 'compensation')
STATES = ('draft', 'proposed', 'approved', 'deprecated', 'observed_partial')
SOURCE_KINDS = ('brd', 'srs', 'bpmn', 'doc', 'test', 'owner', 'llm')
DOCUMENTED = ('brd', 'srs', 'bpmn', 'doc', 'test')
MAP_SOURCES = ('doc', 'test', 'owner', 'inferred', 'llm')
CONFIRMED_MAP = ('doc', 'test', 'owner')
REQ = re.compile(r'^(?:[A-Z0-9]+-)*(BR|FR|NFR|UC|TC)(?:-[\w.]+)?$')
REASONS = {
    'no_step_mapping': 'у шага нет привязки к коду',
    'not_automated': 'ручной шаг: техническая реализация не предусмотрена',
    'handler_not_in_graph': 'объекта привязки нет в графе кода этой версии',
    'no_route_handler': 'у маршрута или команды нет обработчика в графе',
    'handler_dynamic_unresolved': 'обработчик определяется только при исполнении',
    'no_runtime_mapping': 'нет подтверждённой связи кода с объектом runtime',
    'no_port_binding': 'у объекта runtime нет доказанной привязки к порту',
}
ISSUES = {
    'mapping_not_confirmed': 'привязка шага не подтверждена источником или владельцем',
    'mapping_revision_drift': 'привязка подтверждена на другой версии кода',
    'no_runtime_image_identity': 'версия запущенного артефакта не доказана',
    'port_not_observed': 'порт объявлен, но не наблюдался',
}
MAX_PATHS = 64
MAX_DEPTH = 8


def gid(kind, *key):
    return '%s-%s' % (kind, hashlib.sha256(m.canon([kind] + [str(k) if k is not None else None for k in key]).encode()).hexdigest()[:16])


# ------------------------------------------------------------------ определение процесса

def source(kind, ref=None, revision=None, note=None):
    if kind not in SOURCE_KINDS:
        raise ValueError('Источник процесса: %s' % kind)
    return dict(kind=kind, ref=ref, revision=revision, note=note)


def define(project, key, name, steps, flows, src, version=1, goal=None, owner_role=None, trigger=None,
           inputs=None, outcome=None, criticality='normal', refs=None):
    """Процесс с устойчивыми id: процесс — (проект, ключ), шаг — (процесс, ключ шага), поток — (процесс,
    начало, конец, вид). Версия — отдельное поле: новая версия не меняет id процесса и шагов."""
    pid = gid('process', project, key)
    sid = {s['key']: gid('step', pid, s['key']) for s in steps}
    out_steps = [dict(s, id=sid[s['key']], process_id=pid, refs=list(s.get('refs') or [])) for s in steps]
    out_flows = []
    for f in flows:
        kind = f.get('kind', 'sequence')
        out_flows.append(dict(f, kind=kind, process_id=pid, id=gid('flow', pid, f['from'], f['to'], kind),
                              source=sid.get(f['from']), target=sid.get(f['to'])))
    proc = dict(kind='business_process', id=pid, project_id=project, key=key, name=name, version=int(version),
                goal=goal, owner_role=owner_role, trigger=trigger, inputs=inputs, outcome=outcome,
                criticality=criticality, source=src, refs=list(refs or []),
                state='proposed' if src['kind'] == 'llm' else 'draft', approval=None,
                steps=out_steps, flows=out_flows)
    proc['digest'] = digest(proc)
    return validate(proc)


def digest(proc):
    """Отпечаток содержания версии: состояние и согласование в него не входят."""
    body = {k: proc[k] for k in ('key', 'name', 'version', 'goal', 'trigger', 'inputs', 'outcome', 'steps', 'flows', 'refs')}
    return hashlib.sha256(m.canon(body).encode()).hexdigest()


def _ref_code(r):
    return r['code'] if isinstance(r, dict) else r


def validate(proc):
    """Ссылочная целостность и структура (TTN-112, NET-AC-084). Ошибки собираются все сразу."""
    errs = []
    keys = [s['key'] for s in proc['steps']]
    if len(keys) != len(set(keys)):
        errs.append('ключи шагов повторяются')
    by = {s['key']: s for s in proc['steps']}
    for s in proc['steps']:
        if s.get('type') not in STEP_TYPES:
            errs.append('шаг %s: тип %r' % (s['key'], s.get('type')))
    if proc['state'] not in STATES:
        errs.append('состояние %r' % proc['state'])
    if proc['source']['kind'] == 'llm' and proc['state'] == 'approved' and not (proc.get('approval') or {}).get('evidence'):
        errs.append('предложение ИИ не утверждается без доказательства')
    out, inc = {k: [] for k in by}, {k: [] for k in by}
    for f in proc['flows']:
        if f['kind'] not in FLOW_KINDS:
            errs.append('поток %s→%s: вид %r' % (f['from'], f['to'], f['kind']))
        if f['from'] not in by or f['to'] not in by:
            errs.append('поток %s→%s ссылается на несуществующий шаг' % (f['from'], f['to']))
            continue
        out[f['from']].append(f); inc[f['to']].append(f)
        if f['kind'] == 'conditional' and not f.get('condition'):
            errs.append('условный поток %s→%s без условия' % (f['from'], f['to']))
        if f['kind'] == 'async_message' and by[f['from']]['type'] not in ('message_send', 'task', 'external'):
            errs.append('асинхронное сообщение %s→%s не от шага отправки' % (f['from'], f['to']))
    starts = [k for k, s in by.items() if s['type'] == 'start']
    ends = [k for k, s in by.items() if s['type'] in ('end', 'error_end')]
    if not starts:
        errs.append('нет начального события')
    if not ends:
        errs.append('нет конечного события')
    for k in starts:
        if inc[k]:
            errs.append('во начальное событие %s входит поток' % k)
    for k in ends:
        if out[k]:
            errs.append('из конечного события %s выходит поток' % k)
    for k, s in by.items():
        if s['type'] in GATEWAYS and len(out[k]) < 2 and len(inc[k]) < 2:
            errs.append('шлюз %s не ветвит и не сводит' % k)
    seen, stack = set(starts), list(starts)
    while stack:
        k = stack.pop()
        for f in out.get(k, []):
            if f['to'] not in seen:
                seen.add(f['to']); stack.append(f['to'])
    for k in by:
        if k not in seen:
            errs.append('шаг %s недостижим от начала' % k)
    for r in proc.get('refs', []) + [r for s in proc['steps'] for r in s.get('refs', [])]:
        if not REQ.match(_ref_code(r) or ''):
            errs.append('ссылка на требование %r не в формате BR/FR/NFR/UC/TC' % (r,))
    if errs:
        raise ValueError('; '.join(errs))
    return proc


def paths(proc, max_paths=MAX_PATHS, max_visits=2):
    """Все альтернативные пути от начала до конца (NET-AC-085). Шаг посещается не больше max_visits раз
    за путь: возврат на доработку проходится один раз, и поиск конечен (NET-AC-086). Параллельный шлюз
    отмечается в пути: его ветви исполняются вместе, путь показывает одну из них и список остальных."""
    by = {s['key']: s for s in proc['steps']}
    out = {}
    for f in proc['flows']:
        out.setdefault(f['from'], []).append(f)
    found, truncated = [], False
    stack = [([k], []) for k, s in by.items() if s['type'] == 'start']
    while stack:
        steps, flows = stack.pop()
        if len(found) >= max_paths:
            truncated = True
            break
        k = steps[-1]
        if by[k]['type'] in ('end', 'error_end'):
            par = [dict(gateway=s, branches=[f['to'] for f in out.get(s, [])]) for s in steps
                   if by[s]['type'] == 'gateway_parallel' and len(out.get(s, [])) > 1]
            found.append(dict(steps=steps, transitions=flows, parallel=par,
                              gateways=[s for s in steps if by[s]['type'] in GATEWAYS],
                              outcome=by[k]['type']))
            continue
        for f in reversed(out.get(k, [])):
            if steps.count(f['to']) >= max_visits:
                continue
            stack.append((steps + [f['to']], flows + [dict(kind=f['kind'], condition=f.get('condition'))]))
    return dict(mode='planned_process_path', observed=False, paths=found, truncated=truncated)


# ------------------------------------------------------------------ реестр (TTN-113, NET-AC-129)

def _owner(who):
    if who.get('origin') != 'owner':
        raise PermissionError('Менять утверждённый реестр процессов может только владелец')


def _visible(proc, who):
    return who.get('origin') == 'owner' or (who.get('origin') == 'agent' and who.get('project') == proc['project_id'])


class Registry:
    """Реестр процессов: версии не перезаписываются (NET-AC-096), чтение — по правам arch_acl."""

    def __init__(self, data=None, path=None):
        self.path = path
        self.data = data or dict(processes={}, mappings={})

    @classmethod
    def load(cls, data_dir=None):
        base = Path(data_dir or os.environ.get('TASKTREKER_DATA') or Path(__file__).resolve().parent.parent / 'data') / 'arch'
        p = base / 'processes.json'
        data = json.loads(p.read_text(encoding='utf-8')) if p.exists() else None
        return cls(data, p)

    def save(self):
        import taskstate
        taskstate.save_json(str(self.path.parent), self.path.name, self.data)

    def put(self, proc, who):
        """Новая версия. Агент может только предложить (proposed) в своём проекте; владелец — любую.
        Та же версия с другим содержанием отклоняется: история не заменяется молча."""
        validate(proc)
        if who.get('origin') != 'owner':
            if who.get('origin') != 'agent' or who.get('project') != proc['project_id']:
                raise PermissionError(arch_acl.NOT_FOUND)
            proc = dict(proc, state='proposed')
        if proc['state'] == 'approved':
            raise ValueError('Версия входит в реестр неутверждённой: approve — отдельное действие')
        rec = self.data['processes'].setdefault(proc['id'], dict(versions={}, current=None))
        v = str(proc['version'])
        have = rec['versions'].get(v)
        if have is not None:
            if have['digest'] != proc['digest']:
                raise ValueError('Версия %s процесса %s уже есть с другим содержанием: нужна новая версия' % (v, proc['key']))
            return have
        if rec['current'] is not None and proc['version'] < rec['current']:
            raise ValueError('Версия %s ниже текущей %s' % (v, rec['current']))
        rec['versions'][v] = proc
        rec['current'] = proc['version']
        return proc

    def approve(self, pid, version, who, evidence=None, at=None):
        """Утверждение (TTN-115). Документированный источник — с ref и revision. Предложение ИИ и процесс
        от владельца без документа — только с доказательством подтверждения."""
        _owner(who)
        proc = self.data['processes'][pid]['versions'][str(version)]
        src = proc['source']
        if src['kind'] in DOCUMENTED and not (src.get('ref') and src.get('revision')):
            raise ValueError('Источник %s без ссылки и ревизии — утверждать не на чем' % src['kind'])
        if src['kind'] in ('llm', 'owner') and not evidence:
            raise ValueError('Процесс из источника %s утверждается только с доказательством подтверждения' % src['kind'])
        proc['state'] = 'approved'
        proc['approval'] = dict(by='owner', at=at or m.iso(m.now()), evidence=evidence)
        return validate(proc)

    def deprecate(self, pid, version, who, why):
        _owner(who)
        proc = self.data['processes'][pid]['versions'][str(version)]
        proc['state'] = 'deprecated'
        proc['deprecated'] = dict(why=why, at=m.iso(m.now()))
        return proc

    def get(self, pid, who, version=None):
        rec = self.data['processes'].get(pid)
        if rec is None:
            raise LookupError(arch_acl.NOT_FOUND)
        proc = rec['versions'][str(version or rec['current'])] if str(version or rec['current']) in rec['versions'] else None
        if proc is None or not _visible(proc, who):
            raise LookupError(arch_acl.NOT_FOUND)                     # чужой и несуществующий — одинаково
        return proc

    def history(self, pid, who):
        cur = self.get(pid, who)
        return [self.data['processes'][pid]['versions'][v] for v in sorted(self.data['processes'][pid]['versions'], key=int)
                if cur]

    def current(self, who):
        out = []
        for pid, rec in sorted(self.data['processes'].items()):
            proc = rec['versions'][str(rec['current'])]
            if _visible(proc, who):
                out.append(proc)
        return out

    def map(self, mapping, who):
        proc = self.get(mapping['process_id'], who)
        if who.get('origin') != 'owner' and mapping['source']['kind'] in CONFIRMED_MAP:
            raise PermissionError('Подтверждённую привязку ставит владелец или документ; агент — только предложение')
        step = next((s for s in proc['steps'] if s['id'] == mapping['step_id']), None)
        if step is None:
            raise LookupError(arch_acl.NOT_FOUND)
        self.data['mappings'][mapping['id']] = mapping
        return mapping

    def mappings(self, who):
        return [x for x in self.data['mappings'].values()
                if x['process_id'] in self.data['processes'] and _visible(
                    self.data['processes'][x['process_id']]['versions'][str(self.data['processes'][x['process_id']]['current'])], who)]


# ------------------------------------------------------------------ привязки шагов (TTN-118)

def mapping(proc, step_key, target, src_kind, ref=None, revision=None, by=None):
    """Шаг → маршрут, команда, функция или задание графа кода. Ручной шаг привязать нельзя (NET-AC-087)."""
    step = next((s for s in proc['steps'] if s['key'] == step_key), None)
    if step is None:
        raise ValueError('Нет шага %s' % step_key)
    if step['type'] == 'manual':
        raise ValueError('Шаг %s ручной: технический обработчик ему не назначается' % step_key)
    if src_kind not in MAP_SOURCES:
        raise ValueError('Источник привязки: %s' % src_kind)
    if target['kind'] not in ('route', 'cli_command', 'function', 'class', 'job', 'module'):
        raise ValueError('Шаг привязывается к узлу кода, а не к %s' % target['kind'])
    confirmed = src_kind in CONFIRMED_MAP and (src_kind == 'owner' or bool(ref and revision))
    return dict(kind='step_mapping', id=gid('step_map', step['id'], target['id']), process_id=proc['id'],
                process_version=proc['version'], step_id=step['id'], step_key=step_key, target=target['id'],
                target_kind=target['kind'], target_qualname=target['qualname'], target_file=target.get('file'),
                target_line=target.get('line'), source=dict(kind=src_kind, ref=ref, revision=revision, by=by),
                confirmed=confirmed)


# ------------------------------------------------------------------ трассировка (TTN-119…121)

class Index:
    """Индекс графа кода, ручных связей с runtime и инвентаря для обхода."""

    def __init__(self, code, inventory=None, links=None):
        self.code = code
        self.nodes = {n['id']: n for n in code['nodes']}
        self.handler = {}
        self.callers = {}
        for e in code['edges']:
            if e['type'] in ('handles_route', 'handles_command'):
                self.handler.setdefault(e['target'], []).append(e)
            if e['type'] == 'calls_static':
                self.callers.setdefault(e['target'], []).append(e['source'])
        self.objs = {o['id']: o for o in (inventory or {}).get('objects', [])}
        self.links = {}
        for ln in (links or {}).get('links', []) if isinstance(links, dict) else (links or []):
            self.links.setdefault(ln['code'], []).append(ln)
        self.sha = code['version_ref'].get('commit_sha')

    def ports(self, obj):
        """Привязки объекта runtime: сама привязка, привязки контейнера или процесса службы."""
        if obj['kind'] == 'binding':
            return [obj]
        owners = {obj['id']}
        if obj['kind'] == 'service' and obj.get('pid'):
            owners |= {o['id'] for o in self.objs.values() if o['kind'] == 'process' and o.get('pid') == obj['pid']
                       and o.get('identity') == 'verified'}
        return [o for o in self.objs.values() if o['kind'] == 'binding' and o.get('owner') in owners]


def _hop(frm, to, etype, layer, quality, **extra):
    return dict(frm=frm, to=to, type=etype, layer=layer, quality=quality, **extra)


def trace(proc, step_key, mappings, idx):
    """Цепочки шага: по одной на каждую привязку, альтернативы не склеиваются (ARCH-FR-07.2)."""
    step = next((s for s in proc['steps'] if s['key'] == step_key), None)
    if step is None:
        raise LookupError('Нет шага %s' % step_key)
    mine = [x for x in mappings if x['step_id'] == step['id']]
    base = dict(process=proc['id'], process_version=proc['version'], step=step['id'], step_key=step_key,
                mode='planned_process_path', observed=False, code_sha=idx.sha)
    if not mine:
        reason = 'not_automated' if step['type'] == 'manual' else 'no_step_mapping'
        return [dict(base, mapping=None, hops=[], complete=False, confirmed=False, issues=[],
                     broken=dict(reason=reason, text=REASONS[reason], at=step['id']))]
    return [_chain(base, step, x, idx) for x in sorted(mine, key=lambda x: x['id'])]


def _chain(base, step, mp, idx):
    hops, issues = [], []

    def done(reason=None, at=None):
        broken = dict(reason=reason, text=REASONS[reason], at=at) if reason else None
        complete = broken is None
        return dict(base, mapping=mp['id'], hops=hops, issues=issues, broken=broken, complete=complete,
                    confirmed=complete and not issues and all(h['quality'] == 'verified' for h in hops))

    if not mp['confirmed']:
        issues.append(dict(code='mapping_not_confirmed', text=ISSUES['mapping_not_confirmed']))
    if mp['source'].get('revision') and idx.sha and mp['source']['revision'] != idx.sha:
        issues.append(dict(code='mapping_revision_drift', text=ISSUES['mapping_revision_drift'],
                           mapped_at=mp['source']['revision'], code_sha=idx.sha))
    target = idx.nodes.get(mp['target'])
    if target is None:
        return done('handler_not_in_graph', mp['target'])
    hops.append(_hop(step['id'], target['id'], 'realizes_process_step', 'logical',
                     'verified' if mp['confirmed'] else 'inferred', source=mp['source'],
                     file=target.get('file'), line=target.get('line')))
    symbol = target
    if target['kind'] in ('route', 'cli_command'):
        hs = idx.handler.get(target['id'], [])
        if not hs:
            return done('no_route_handler', target['id'])
        if len(hs) > 1:
            issues.append(dict(code='mapping_not_confirmed', text='у маршрута несколько обработчиков'))
        e = hs[0]
        if e['resolution'] == 'dynamic_unresolved':
            return done('handler_dynamic_unresolved', target['id'])
        symbol = idx.nodes[e['source']]
        hops.append(_hop(symbol['id'], target['id'], e['type'], 'call', e['evidence_quality'],
                         handler_kind=e.get('handler_kind'), locations=e['locations']))
    links = idx.links.get(symbol['id'], []) + (idx.links.get(target['id'], []) if target is not symbol else [])
    if not links:
        return done('no_runtime_mapping', symbol['id'])
    ln = links[0]
    dep = ln.get('deployment') or {}
    if dep.get('state') != 'deployed_verified':
        issues.append(dict(code='no_runtime_image_identity', text=ISSUES['no_runtime_image_identity'],
                           deployment=dep.get('state')))
    obj = idx.objs.get(ln['runtime'])
    if obj is None:
        return done('no_runtime_mapping', symbol['id'])
    hops.append(_hop(ln['code'], obj['id'], 'deployed_as', 'deployment',
                     'verified' if dep.get('state') == 'deployed_verified' else 'declared', method=ln.get('method')))
    ports = idx.ports(obj)
    if not ports:
        return done('no_port_binding', obj['id'])
    for b in sorted(ports, key=lambda b: b['id']):
        if b['socket_state'] not in ('listening_observed', 'bound_observed'):
            issues.append(dict(code='port_not_observed', text=ISSUES['port_not_observed'], binding=b['id']))
        hops.append(_hop(obj['id'], b['id'], 'exposes_endpoint', 'network', b['evidence_quality'],
                         protocol=b['protocol'], port=b['port'], bind_ip=b['bind_ip']))
    return done()


def static_path(idx, src_id, dst_id, max_depth=MAX_DEPTH):
    """Возможный путь вызовов по графу кода (§6A.1 static_implementation_path). Это не исполнение:
    observed=False всегда; динамические места у пройденных узлов перечисляются как разрывы знания."""
    out = {}
    for e in idx.code['edges']:
        if e['type'] == 'calls_static':
            out.setdefault(e['source'], []).append(e['target'])
    prev, frontier, depth = {src_id: None}, [src_id], 0
    while frontier and dst_id not in prev and depth < max_depth:
        nxt = []
        for n in frontier:
            for t in out.get(n, []):
                if t not in prev:
                    prev[t] = n; nxt.append(t)
        frontier, depth = nxt, depth + 1
    gaps = sorted({u['id'] for u in idx.code.get('unresolved', []) if u['source'] in prev})
    if dst_id not in prev:
        return dict(mode='static_implementation_path', observed=False, found=False, path=[], dynamic_gaps=gaps)
    path, k = [], dst_id
    while k is not None:
        path.append(k); k = prev[k]
    return dict(mode='static_implementation_path', observed=False, found=True, path=path[::-1], dynamic_gaps=gaps)


def classify(result):
    """Режим пути по его доказательствам. observed_execution_path без трассы исполнения не выдаётся."""
    if result.get('trace_evidence'):
        return 'observed_execution_path'
    if result.get('mode') == 'static_implementation_path':
        return 'static_implementation_path'
    return 'planned_process_path'


# ------------------------------------------------------------------ обратный поиск (TTN-122, NET-AC-091, 098, 099)

def _code_targets(target_id, idx):
    """Узлы кода, которые меняются вместе с целью: сама цель, символы модуля, код, связанный с портом."""
    n = idx.nodes.get(target_id)
    if n is not None and n['kind'] == 'module':
        prefix = n['qualname'] + '.'
        return [target_id] + [k for k, x in idx.nodes.items() if x['kind'] in ('function', 'class')
                              and x['repo_id'] == n['repo_id'] and x['qualname'].startswith(prefix)]
    if n is not None:
        return [target_id]
    obj = idx.objs.get(target_id)
    if obj is None:
        return []
    related = {target_id}
    if obj['kind'] == 'binding':
        related.add(obj.get('owner'))
        for o in idx.objs.values():
            if o['kind'] == 'service' and o.get('pid') and any(
                    p['id'] == obj.get('owner') and p.get('pid') == o['pid'] for p in idx.objs.values() if p['kind'] == 'process'):
                related.add(o['id'])
    return sorted({ln['code'] for lns in idx.links.values() for ln in lns if ln['runtime'] in related})


def impact(target_id, procs, mappings, idx, max_depth=MAX_DEPTH):
    """Какие процессы затронет изменение функции, маршрута, модуля или порта (ARCH-FR-07.6).
    direct — шаг привязан к цели или к маршруту/команде, которую она обрабатывает; possible — шаг привязан
    к коду, из которого цель достижима вызовами; unknown — есть то, что связь могло бы скрыть."""
    steps = {s['id']: (p, s) for p in procs for s in p['steps']}
    by_target = {}
    for x in mappings:
        if x['step_id'] in steps:
            by_target.setdefault(x['target'], []).append(x)
    seeds = _code_targets(target_id, idx)
    direct, possible, reasons = {}, {}, []
    if not seeds:
        reasons.append('цель не найдена в графе кода и не связана с кодом')

    def hit(where, x, via):
        p, s = steps[x['step_id']]
        where.setdefault(x['step_id'], dict(process=p['id'], process_key=p['key'], process_version=p['version'],
                                             step=s['id'], step_key=s['key'], via=via, mapping=x['id'],
                                             confirmed=x['confirmed']))

    entry_of = {}
    for t, hs in idx.handler.items():
        for e in hs:
            entry_of.setdefault(e['source'], []).append(t)
    seen = set(seeds)
    for sd in seeds:
        for x in by_target.get(sd, []):
            hit(direct, x, 'привязка')
        for ep in entry_of.get(sd, []):
            for x in by_target.get(ep, []):
                hit(direct, x, 'обработчик маршрута')
    frontier, depth, unmapped = list(seeds), 0, set()
    while frontier and depth < max_depth:
        nxt = []
        for n in frontier:
            for c in idx.callers.get(n, []):
                if c in seen:
                    continue
                seen.add(c); nxt.append(c)
                for x in by_target.get(c, []):
                    hit(possible, x, 'вызывает цель')
                for ep in entry_of.get(c, []):
                    if by_target.get(ep):
                        for x in by_target[ep]:
                            hit(possible, x, 'маршрут вызывает цель')
                    else:
                        unmapped.add(ep)
        frontier, depth = nxt, depth + 1
    if frontier:
        reasons.append('обход остановлен на глубине %d' % max_depth)
    for sd in seeds:
        for ep in entry_of.get(sd, []):
            if not by_target.get(ep):
                unmapped.add(ep)
    if unmapped:
        reasons.append('точки входа без привязки к процессу: %d' % len(unmapped))
    dyn = [u for u in idx.code.get('unresolved', []) if u['source'] in seen]
    if dyn:
        reasons.append('динамические вызовы на пути: %d' % len(dyn))
    if idx.code.get('coverage', {}).get('calls_unknown_target'):
        reasons.append('в графе есть вызовы без доказанной цели')
    for k in direct:
        possible.pop(k, None)
    verdict = 'impacted' if direct else 'possible' if possible else 'unknown'
    return dict(target=target_id, verdict=verdict, direct=sorted(direct.values(), key=lambda x: x['step']),
                possible=sorted(possible.values(), key=lambda x: x['step']), unmapped_entrypoints=sorted(unmapped),
                unknown_reasons=reasons, code_sha=idx.sha)


def describe(node_id, procs, mappings):
    """Бизнес-смысл технического узла — только из привязок; без них not_documented (NET-AC-088, 091)."""
    steps = {s['id']: (p, s) for p in procs for s in p['steps']}
    hits = [x for x in mappings if x['target'] == node_id and x['step_id'] in steps]
    if not hits:
        return dict(node=node_id, business='not_documented', steps=[])
    return dict(node=node_id, business='mapped', steps=[dict(process=steps[x['step_id']][0]['key'], step=steps[x['step_id']][1]['key'],
                                                              step_id=x['step_id'], confirmed=x['confirmed'], source=x['source'])
                                                         for x in sorted(hits, key=lambda x: x['id'])])


# ------------------------------------------------------------------ требования и расхождения (TTN-123, NET-AC-097)

def traceability(proc, mappings=(), requirements=None):
    """Счёт ссылок на требования по видам, шаги без требований и без привязки, битые ссылки."""
    counts = {}
    broken = []
    refs = [(None, r) for r in proc.get('refs', [])] + [(s['key'], r) for s in proc['steps'] for r in s.get('refs', [])]
    for step, r in refs:
        code = _ref_code(r)
        kind = REQ.match(code).group(1)
        counts[kind] = counts.get(kind, 0) + 1
        if requirements is not None and code not in requirements:
            broken.append(dict(step=step, ref=code))
    work = [s for s in proc['steps'] if s['type'] not in ('start', 'end', 'error_end') + GATEWAYS]
    mapped = {x['step_id'] for x in mappings}
    return dict(process=proc['id'], version=proc['version'], source_revision=proc['source'].get('revision'),
                refs_by_kind=counts, refs_total=len(refs), broken=broken,
                steps_without_refs=[s['key'] for s in work if not s.get('refs')],
                steps_without_mapping=[s['key'] for s in work if s['id'] not in mapped and s['type'] != 'manual'],
                manual_steps=[s['key'] for s in work if s['type'] == 'manual'])


def drift(proc, mappings, idx):
    """Расхождения определения и кода. Это сигнал на проверку, а не автоматическое утверждение."""
    out = []
    for x in mappings:
        if x['process_id'] != proc['id']:
            continue
        if x['target'] not in idx.nodes:
            out.append(dict(step=x['step_key'], kind='target_missing', target=x['target_qualname']))
        elif x['source'].get('revision') and idx.sha and x['source']['revision'] != idx.sha:
            out.append(dict(step=x['step_key'], kind='revision_changed', mapped_at=x['source']['revision'], code_sha=idx.sha))
    return dict(process=proc['id'], state='drift' if out else 'consistent', items=out)
