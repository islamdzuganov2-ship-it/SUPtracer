"""Проекция «Архитектуры систем» для вкладки доски (TT-ARCH-001 §7A, §8A; TT-136).

Сервер досок вызывает это после проверки сессии и флага. Права те же, что у снимка (arch_acl): владелец видит
всё, агент проекта — только объекты, графы кода и процессы своего проекта, остальные — ничего. Чужое и
несуществующее отвечают одинаково.

Чего здесь нет намеренно:
  * код проектов не исполняется — граф кода строит arch_static (ast.parse), снимок ОС и Docker —
    arch_collect через job_runner и только командами чтения;
  * удалённого сканирования и запуска развёртываний нет (NEVER_IN_MVP);
  * неизвестное не превращается в «нет»: нет снимка — так и сказано, нет привязки шага — reason code.
"""
import json
import os
import threading
import time
from pathlib import Path

import arch_acl

ROOT = Path(__file__).resolve().parent.parent
MAX_CODE_NODES = 20000
STRUCTURAL = ('start', 'end', 'error_end', 'gateway_exclusive', 'gateway_inclusive', 'gateway_parallel')          # больше — ответ урезается с пометкой truncated, а не молча


def data_dir(base=None):
    return Path(base or os.environ.get('TASKTREKER_DATA') or ROOT / 'data')


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def inventory(base=None):
    return _read(data_dir(base) / 'arch' / 'inventory.json')


def code_graphs(base=None):
    """Сохранённые графы кода: {repo_id: graph}. Повреждённый файл пропускается, а не роняет вкладку."""
    out = {}
    d = data_dir(base) / 'arch' / 'code'
    if d.is_dir():
        for p in sorted(d.glob('*.json')):
            g = _read(p)
            if isinstance(g, dict) and g.get('version_ref'):
                out[g['version_ref'].get('repo_id') or p.stem] = g
    return out


def graph_project(g):
    return (g.get('project') or {}).get('project')


def _graph_visible(g, who):
    if 'view_sensitive_topology' not in who['perms']:
        return False
    if who['origin'] == 'owner':
        return True
    return graph_project(g) is not None and graph_project(g) == who['project']


def _code_brief(repo, g):
    v = g.get('version_ref') or {}
    cov = g.get('coverage') or {}
    return dict(repo=repo, project=graph_project(g), project_method=(g.get('project') or {}).get('method'),
                commit_sha=v.get('commit_sha'), dirty=v.get('dirty'), branch=v.get('branch'),
                nodes=len(g.get('nodes') or []), edges=len(g.get('edges') or []),
                unresolved=len(g.get('unresolved') or []),
                counts={k: cov.get(k) for k in ('modules', 'classes', 'functions', 'routes', 'cli_commands', 'jobs')},
                truncated=bool(cov.get('truncated')), refused=cov.get('refused'))


def _registry(base=None):
    import arch_process
    return arch_process.Registry.load(str(data_dir(base)))


def overview(who, settings=None, base=None, scan_state=None):
    """Первый экран вкладки: что собрано, когда, насколько полно. Без прав — та же «нет прав», что у объекта."""
    if 'view_summary' not in who['perms']:
        raise PermissionError(arch_acl.NOT_FOUND)
    snap = inventory(base)
    inv = None
    if snap is not None:
        s = arch_acl.summary(snap, who)
        v = arch_acl.view(snap, who)
        inv = dict(taken_at=snap.get('taken_at'), complete=snap.get('complete'), counts=s['counts'],
                   sources=[dict(collector_id=x.get('collector_id'), state=x.get('state'), reason=x.get('reason'))
                            for x in v.get('sources') or []],
                   conflicts=len(v.get('conflicts') or []), partial_reasons=v.get('partial_reasons'))
    graphs = [_code_brief(r, g) for r, g in code_graphs(base).items() if _graph_visible(g, who)]
    procs = _registry(base).current(who)
    states = {}
    for p in procs:
        states[p.get('state')] = states.get(p.get('state'), 0) + 1
    return dict(owner=who['origin'] == 'owner', project=who['project'], inventory=inv, code=graphs,
                processes=dict(count=len(procs), states=states),
                can_scan='run_scan_local' in who['perms'], scan=scan_state,
                seeds=seeds() if who['origin'] == 'owner' else [])


def code(repo, who, base=None):
    g = code_graphs(base).get(repo)
    if g is None or not _graph_visible(g, who):
        raise LookupError(arch_acl.NOT_FOUND)
    nodes = g.get('nodes') or []
    truncated = len(nodes) > MAX_CODE_NODES
    if truncated:
        keep = {n['id'] for n in nodes[:MAX_CODE_NODES]}
        nodes = nodes[:MAX_CODE_NODES]
        edges = [e for e in g.get('edges') or [] if e['source'] in keep and e['target'] in keep]
    else:
        edges = g.get('edges') or []
    return dict(brief=_code_brief(repo, g), version_ref=g.get('version_ref'), deployment=g.get('deployment'),
                coverage=g.get('coverage'), nodes=nodes, edges=edges, unresolved=g.get('unresolved') or [],
                truncated=truncated)


def processes(who, base=None):
    """Процессы с трассировкой каждого шага: шаг → код → runtime → порт. Нет графа кода проекта —
    трассировка всё равно строится по пустому графу и честно рвётся на первом переходе."""
    import arch_process
    reg = _registry(base)
    procs = reg.current(who)
    maps = reg.mappings(who)
    graphs = code_graphs(base)
    snap = inventory(base)
    inv = arch_acl.view(snap, who) if snap is not None and who['perms'] else None
    links = _read(data_dir(base) / 'arch' / 'links.json')
    out = []
    for p in procs:
        g = next((x for x in graphs.values() if graph_project(x) == p['project_id'] and _graph_visible(x, who)), None)
        idx = arch_process.Index(g or dict(nodes=[], edges=[], version_ref={}), inv, links)
        mine = [x for x in maps if x['process_id'] == p['id']]
        steps = []
        for s in p['steps']:
            if s['type'] in STRUCTURAL:                 # начало, конец и развилки — форма процесса, не работа
                steps.append(dict(step=s, structural=True, chains=[]))
                continue
            chains = arch_process.trace(p, s['key'], mine, idx)
            steps.append(dict(step=s, structural=False, chains=[dict(c, path_mode=arch_process.classify(c)) for c in chains]))
        out.append(dict(process=p, steps=steps, paths=arch_process.paths(p), code_repo=(g or {}).get('version_ref', {}).get('repo_id'),
                        traceability=arch_process.traceability(p, mine)))
    return dict(processes=out)


def handle(route, query, who, settings=None, base=None, scan_state=None):
    """Маршруты вкладки поверх arch_acl: (код, тело)."""
    try:
        if route == 'overview':
            return 200, dict(ok=True, data=overview(who, settings, base, scan_state))
        if route == 'code':
            return 200, dict(ok=True, data=code((query.get('repo') or [''])[0], who, base))
        if route == 'processes':
            if not who['perms']:
                raise PermissionError(arch_acl.NOT_FOUND)
            return 200, dict(ok=True, data=processes(who, base))
    except (PermissionError, LookupError):
        return 404, dict(ok=False, error=arch_acl.NOT_FOUND)
    return None


# ------------------------------------------------------------------ сбор снимка с доски (только владелец)

class Scan:
    """Один сбор за раз, в фоне: снимок ОС и Docker (только чтение) и граф кода выбранных репозиториев (AST).
    Ответ сразу — 202; ход виден в overview.scan."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = dict(running=False, started=None, finished=None, ok=None, steps=[], error=None)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def start(self, projects, keys, base=None, run=None):
        with self.lock:
            if self.state['running']:
                return False
            self.state = dict(running=True, started=time.time(), finished=None, ok=None, steps=[], error=None)
        t = threading.Thread(target=self._work, args=(projects, keys, base, run), daemon=True)
        t.start()
        return True

    def _step(self, name, ok, note):
        with self.lock:
            self.state['steps'].append(dict(name=name, ok=ok, note=note, at=time.time()))

    def _work(self, projects, keys, base, run):
        import arch_collect, arch_static
        ok = True
        try:
            snap = (run or arch_collect.collect)(projects=projects)
            arch_collect.save(snap, str(data_dir(base)))
            self._step('Снимок ОС и Docker', True, '%d объектов, %s' % (
                len(snap.get('objects', [])), 'полный' if snap.get('complete') else 'частичный'))
        except Exception as e:                       # источник не должен ронять сервер досок
            ok = False
            self._step('Снимок ОС и Docker', False, str(e)[:300])
        for key in keys:
            path = str(ROOT) if key == 'tasktreker' else (projects.get(key) or {}).get('path')
            if not path or not os.path.isdir(path):
                ok = False
                self._step('Граф кода: ' + key, False, 'нет каталога проекта')
                continue
            try:
                g = arch_static.scan(path, key, projects)
                arch_static.save(g, str(data_dir(base)))
                cov = g.get('coverage') or {}
                self._step('Граф кода: ' + key, True, '%d модулей, %d функций, %d маршрутов%s' % (
                    cov.get('modules', 0), cov.get('functions', 0), cov.get('routes', 0),
                    ', урезан по лимиту' if cov.get('truncated') else ''))
            except Exception as e:
                ok = False
                self._step('Граф кода: ' + key, False, str(e)[:300])
        with self.lock:
            self.state.update(running=False, finished=time.time(), ok=ok)


SCAN = Scan()


def scan_keys(req, projects):
    """Какие репозитории разбирать: TaskTreker всегда, остальные — только явно выбранные владельцем."""
    keys = ['tasktreker']
    for k in req.get('projects') or []:
        if not isinstance(k, str) or k not in projects:
            raise ValueError('Неизвестный проект: %s' % k)
        if k not in keys:
            keys.append(k)
    return keys


# ------------------------------------------------------------------ процессы из документов (только владелец)

SEEDS = ROOT / 'tools' / 'arch_processes'


def seeds():
    """Определения процессов, записанные по документам репозитория (источник kind=doc с ref и revision)."""
    return sorted(p.stem for p in SEEDS.glob('*.json')) if SEEDS.is_dir() else []


def seed(name, who, base=None):
    """Внести процесс из документа в реестр. Входит черновиком: утверждение — отдельное действие владельца."""
    import arch_process
    if who.get('origin') != 'owner':
        raise PermissionError(arch_acl.NOT_FOUND)
    if name not in seeds():
        raise ValueError('Нет такого определения процесса')
    d = json.loads((SEEDS / (name + '.json')).read_text(encoding='utf-8'))
    src = d.pop('source')
    proc = arch_process.define(d.pop('project'), d.pop('key'), d.pop('name'), d.pop('steps'), d.pop('flows'),
                               arch_process.source(src['kind'], src.get('ref'), src.get('revision'), src.get('note')), **d)
    reg = _registry(base)
    got = reg.put(proc, who)
    reg.save()
    return dict(id=got['id'], version=got['version'], state=got['state'])
