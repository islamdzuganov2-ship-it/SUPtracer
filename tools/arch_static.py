"""Статический граф Python-кода проекта (TT-ARCH-001 ARCH-FR-02; TT-134: TTN-094…101, NET-AC-069…072, 078…080).

Только чтение. Файлы разбираются ast.parse — код проекта не импортируется, не исполняется, зависимости не
ставятся (TTN-099, NET-AC-079). Git опрашивается командами только чтения через job_runner.

Что собирается: пакеты, модули, классы, функции и методы с файлом и строкой; импорты (imports) отдельно от
вызовов (calls_static); явные HTTP-маршруты (декораторы route/get/post… и ветки `if path == "/x"` в do_GET/do_POST
обработчика http.server), команды CLI (словарь команд вида COMMANDS = {"имя": функция} и argparse add_parser +
set_defaults(func=…)), фоновые задания (Thread/Process/Timer с target).

Что не придумывается:
  * вызов разрешается, только если цель доказана: своя функция модуля, функция импортированного внутреннего
    модуля, метод того же класса через self/cls. Остальное — счётчик в coverage, а не связь;
  * getattr с вычисляемым именем или вызов результата getattr, importlib.import_module, __import__, eval, exec,
    лямбда в таблице команд — запись unresolved с местом и причиной (ARCH-FR-02.5, NET-AC-071);
  * импорт не даёт сетевой связи и не даёт вызова (NET-AC-030, 069, 078);
  * импорт, который подходит к двум модулям репозитория, — resolution=ambiguous к обоим, а не выбор наугад.

Охват (TTN-095, NET-AC-080): только внутри корня, realpath, ссылки не открываются, скрытые каталоги, .venv,
node_modules, dist, build, временные каталоги и рабочие копии Git пропускаются; лимиты файлов, размера и времени
— в coverage с причиной.
"""
import ast
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import arch_collect as ac
import arch_graph as g

PARSER = 'python-ast/%d.%d' % sys.version_info[:2]
SKIP_DIRS = set(ac.SKIP_DIRS) | {'tmp', 'temp', '.cache', 'htmlcov', '.eggs', '.ruff_cache', 'env', 'out', 'target'}
MAX_FILE = 1_000_000
MAX_FILES = 5000
TIME_BUDGET = 120.0
ROUTE_VERBS = {'route': None, 'get': 'GET', 'post': 'POST', 'put': 'PUT', 'delete': 'DELETE', 'patch': 'PATCH',
               'websocket': 'WS', 'api_route': None}
HTTP_HANDLERS = {'do_GET': 'GET', 'do_POST': 'POST', 'do_PUT': 'PUT', 'do_DELETE': 'DELETE', 'do_PATCH': 'PATCH',
                 'do_HEAD': 'HEAD'}
JOB_CALLS = ('Thread', 'Process', 'Timer')
COMMAND_TABLE = re.compile(r'command', re.I)
STDLIB = frozenset(getattr(sys, 'stdlib_module_names', ())) | {'__future__'}


# ------------------------------------------------------------------ охват

def _inside(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:
        return False


def files(root, max_files=MAX_FILES, max_file=MAX_FILE, include_hidden=()):
    """(файлы [(rel, full)], отклонённые [{path, reason}], обрезано ли по лимиту)."""
    root = os.path.realpath(root)
    found, refused, truncated = [], [], False
    for dirpath, dirs, names in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dirs):
            full = os.path.join(dirpath, d)
            rel = os.path.relpath(full, root).replace('\\', '/')
            if os.path.islink(full) or not _inside(full, root):
                refused.append(dict(path=rel, reason='ссылка'))
            elif d in SKIP_DIRS or d.endswith('.egg-info') or d.endswith('.worktrees'):
                continue
            elif d.startswith('.') and rel not in include_hidden:
                continue                                                  # скрытые — только по явному разрешению
            elif os.path.isfile(os.path.join(full, '.git')):
                continue                                                  # рабочая копия Git — не часть приложения
            else:
                keep.append(d)
        dirs[:] = keep
        for f in sorted(names):
            if not f.endswith('.py'):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace('\\', '/')
            if os.path.islink(full) or not _inside(full, root):
                refused.append(dict(path=rel, reason='ссылка за пределы проекта'))
            elif os.path.getsize(full) > max_file:
                refused.append(dict(path=rel, reason='skipped_size'))
            elif len(found) >= max_files:
                truncated = True
            else:
                found.append((rel, full))
    return found, refused, truncated


def module_name(rel):
    parts = rel[:-3].split('/')
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts) or '__init__'


# ------------------------------------------------------------------ версия

def git_version(root, repo_id, run=None):
    """commit_sha, ветка, грязное ли дерево — командами только чтения. Нет git или нет доступа — None."""
    run = run or ac._run
    base = ['git', '-C', str(root)]
    code, sha = run(base + ['rev-parse', 'HEAD'])
    sha = sha.strip() if code == 0 and re.fullmatch(r'[0-9a-f]{40}', (sha or '').strip()) else None
    code, br = run(base + ['rev-parse', '--abbrev-ref', 'HEAD'])
    br = br.strip() if code == 0 and br.strip() else None
    code, st = run(base + ['status', '--porcelain', '--untracked-files=no'])
    dirty = bool(st.strip()) if code == 0 else None
    return g.version_ref(repo_id, sha, br, dirty)


# ------------------------------------------------------------------ разбор

class Module:
    def __init__(self, rel, name, tree, digest):
        self.rel, self.name, self.tree, self.digest = rel, name, tree, digest
        self.node = None
        self.defs = {}          # имя верхнего уровня -> узел функции или класса
        self.methods = {}       # имя класса -> {метод: узел}
        self.binds = {}         # локальное имя -> ('module', [модули]) | ('symbol', [модули], имя) | ('external', имя)
        self.package = '.'.join(name.split('.')[:-1]) if not rel.endswith('__init__.py') else name


def _const_str(n):
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


class Scanner:
    def __init__(self, root, repo_id, commit_sha=None):
        self.root, self.repo_id, self.sha = root, repo_id, commit_sha
        self.modules = {}
        self.nodes, self.edges, self.unresolved = [], [], []
        self.cov = dict(files=0, parsed=0, syntax_error=0, skipped_encoding=0, modules=0, packages=0, classes=0,
                        functions=0, routes=0, cli_commands=0, jobs=0, calls_resolved=0, calls_external=0,
                        calls_unknown_target=0, imports_internal=0, imports_external=0, imports_ambiguous=0,
                        executed_code=False, parser=PARSER)
        self.repo = g.node('repo', repo_id, repo_id)
        self.nodes.append(self.repo)
        self.by_base = {}

    def prov(self, mod, n):
        return g.provenance(self.repo_id, self.sha, mod.rel, getattr(n, 'lineno', None), PARSER, getattr(n, 'col_offset', None))

    def add(self, e):
        self.edges.append(e)

    # -------- проход 1: модули, определения, импорты
    def load(self, rel, full):
        self.cov['files'] += 1
        data = Path(full).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        try:
            tree = ast.parse(data, filename=rel)
        except SyntaxError:
            self.cov['syntax_error'] += 1
            tree = None
        except (ValueError, UnicodeDecodeError):
            self.cov['skipped_encoding'] += 1
            tree = None
        name = module_name(rel)
        mod = Module(rel, name, tree, digest)
        mod.node = g.node('module', self.repo_id, name, rel, 1, sha256=digest,
                          parse_state='parsed' if tree is not None else 'syntax_error')
        self.modules[name] = mod
        self.nodes.append(mod.node)
        self.cov['modules'] += 1
        if tree is not None:
            self.cov['parsed'] += 1

    def packages(self):
        """Пакеты — каталоги с модулями; contains: репозиторий → пакет → модуль."""
        pk = {}
        for name, mod in sorted(self.modules.items()):
            parts = mod.rel.split('/')[:-1]
            parent = self.repo
            for i in range(len(parts)):
                q = '.'.join(parts[:i + 1])
                if q not in pk:
                    pk[q] = g.node('package', self.repo_id, q, '/'.join(parts[:i + 1]), None)
                    self.nodes.append(pk[q])
                    self.add(g.edge('contains', parent, pk[q], g.provenance(self.repo_id, self.sha, pk[q]['file'], None, PARSER)))
                parent = pk[q]
            self.add(g.edge('contains', parent, mod.node, g.provenance(self.repo_id, self.sha, mod.rel, 1, PARSER)))
        for name, mod in self.modules.items():
            self.by_base.setdefault(name.rsplit('.', 1)[-1], []).append(name)

    def defs(self, mod):
        def walk(body, owner, prefix, cls=None):
            for st in body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = 'class' if isinstance(st, ast.ClassDef) else 'function'
                    q = '%s.%s' % (prefix, st.name)
                    n = g.node(kind, self.repo_id, q, mod.rel, st.lineno, name=st.name,
                               method=bool(cls) and kind == 'function', is_async=isinstance(st, ast.AsyncFunctionDef))
                    st._tt_node = n
                    self.nodes.append(n); self.cov['classes' if kind == 'class' else 'functions'] += 1
                    self.add(g.edge('contains', owner, n, self.prov(mod, st)))
                    if owner is mod.node:
                        mod.defs[st.name] = n
                    if cls is not None and kind == 'function':
                        mod.methods.setdefault(cls, {})[st.name] = n
                    walk(st.body, n, q, st.name if kind == 'class' else None)
                elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                    for part in ('body', 'orelse', 'finalbody'):
                        walk(getattr(st, part, []) or [], owner, prefix, cls)
                    for h in getattr(st, 'handlers', []) or []:
                        walk(h.body, owner, prefix, cls)
        walk(mod.tree.body, mod.node, mod.name)

    def candidates(self, mod, name, level=0):
        """Модули репозитория, к которым может относиться импорт `name` из модуля mod."""
        if level:
            base = mod.package.split('.') if mod.package else []
            base = base[:len(base) - (level - 1)] if level > 1 else base
            full = '.'.join([p for p in base if p] + ([name] if name else []))
            return [full] if full in self.modules else []
        out = []
        if name in self.modules:
            out.append(name)
        local = '.'.join([p for p in mod.package.split('.') if p] + [name]) if mod.package else name
        if local != name and local in self.modules:
            out.append(local)                  # скрипты каталога: `import serve` рядом с tools/serve.py
        return out

    def imports(self, mod):
        for n in ast.walk(mod.tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.import_edge(mod, n, a.name, 0, a.asname or a.name.split('.')[0], whole=a.asname is None)
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == '*':
                        self.unresolved.append(g.unresolved(mod.node, 'star_import', self.prov(mod, n), hint=n.module))
                        self.import_edge(mod, n, n.module or '', n.level, None)
                        continue
                    sub = ((n.module + '.') if n.module else '') + a.name
                    if self.candidates(mod, sub, n.level):
                        self.import_edge(mod, n, sub, n.level, a.asname or a.name)
                    else:
                        self.import_edge(mod, n, n.module or '', n.level, a.asname or a.name, symbol=a.name)

    def import_edge(self, mod, n, name, level, local, whole=False, symbol=None):
        cands = self.candidates(mod, name, level)
        prov = self.prov(mod, n)
        if len(cands) == 1:
            self.cov['imports_internal'] += 1
            self.add(g.edge('imports', mod.node, self.modules[cands[0]].node, prov))
            if local:
                if whole and '.' in name:
                    # `import a.b` связывает имя `a`; атрибут b у него — модуль a.b, а не функция.
                    top = self.candidates(mod, name.split('.')[0], 0)
                    if len(top) == 1:
                        mod.binds[local] = ('module', top)
                else:
                    mod.binds[local] = ('symbol', cands, symbol) if symbol else ('module', cands)
            return
        if level and not cands:
            return                                            # относительный импорт за пределы найденных модулей
        top = (name or '').split('.')[0]
        if not cands and not top:
            return
        if not cands and top not in STDLIB:
            # Есть модуль с таким именем в другом каталоге репозитория (sys.path меняется в рантайме) —
            # кандидат, но не доказанная цель.
            cands = sorted(self.by_base.get(name.split('.')[-1], [])) if '.' not in name else []
        if cands:
            self.cov['imports_ambiguous'] += 1
            for c in cands:
                self.add(g.edge('imports', mod.node, self.modules[c].node, prov, resolution='ambiguous'))
            return                                            # имя не связывается: вызовы через него не разрешаются
        ext = g.node('external_module', self.repo_id, top, stdlib=top in STDLIB)
        self.nodes.append(ext)
        self.cov['imports_external'] += 1
        # Стандартная библиотека — проверяемый факт; сторонний пакет — имя, установку анализ не проверял.
        self.add(g.edge('imports', mod.node, ext, prov, resolution='external',
                        quality='verified' if top in STDLIB else 'inferred'))
        if local:
            mod.binds[local] = ('external', name if not symbol else name + '.' + symbol)

    # -------- проход 2: вызовы, маршруты, команды, задания
    def target(self, mod, func, cls):
        """(узел | None, причина): цель вызова, только если она доказана."""
        if isinstance(func, ast.Name):
            if func.id in mod.defs:
                return mod.defs[func.id], 'resolved'
            b = mod.binds.get(func.id)
            if b and b[0] == 'symbol':
                hits = [self.modules[c].defs[b[2]] for c in b[1] if b[2] in self.modules[c].defs]
                return (hits[0], 'resolved') if len(hits) == 1 else (None, 'unknown')
            if b and b[0] == 'external':
                return None, 'external'
            return None, 'unknown'
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = func.value.id
            if base in ('self', 'cls') and cls and func.attr in mod.methods.get(cls, {}):
                return mod.methods[cls][func.attr], 'resolved'
            b = mod.binds.get(base)
            if b and b[0] == 'module':
                hits = [self.modules[c].defs[func.attr] for c in b[1] if func.attr in self.modules[c].defs]
                return (hits[0], 'resolved') if len(hits) == 1 else (None, 'unknown')
            if b and b[0] == 'external':
                return None, 'external'
        return None, 'unknown'

    def dynamic(self, mod, call, ctx):
        """Динамические места: reflection, динамический импорт, eval/exec. Отмечаются, а не разрешаются."""
        f = call.func
        name = f.id if isinstance(f, ast.Name) else None
        attr = f.attr if isinstance(f, ast.Attribute) else None
        base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
        prov = self.prov(mod, call)
        if isinstance(f, ast.Call) and isinstance(f.func, ast.Name) and f.func.id == 'getattr':
            hint = _const_str(f.args[1]) if len(f.args) > 1 else None
            return g.unresolved(ctx, 'getattr', prov, hint=hint and ac.scrub(hint))
        if name == 'getattr' and len(call.args) > 1 and _const_str(call.args[1]) is None:
            return g.unresolved(ctx, 'getattr', prov)
        if name in ('eval', 'exec', 'compile'):
            return g.unresolved(ctx, 'eval_exec', prov, hint=name)
        if name == '__import__' or (attr == 'import_module' and base in ('importlib',) + tuple(
                k for k, v in mod.binds.items() if v[0] == 'external' and v[1].split('.')[0] == 'importlib')):
            hint = _const_str(call.args[0]) if call.args else None
            return g.unresolved(ctx, 'dynamic_import', prov, hint=hint and ac.scrub(hint))
        if name == 'import_module' and mod.binds.get('import_module', ('',))[0] == 'external':
            hint = _const_str(call.args[0]) if call.args else None
            return g.unresolved(ctx, 'dynamic_import', prov, hint=hint and ac.scrub(hint))
        return None

    def body_calls(self, mod, body, ctx, cls):
        """Вызовы в теле ctx без заходов во вложенные определения (у них свои узлы)."""
        for n in shallow(body):
            if isinstance(n, ast.Call):
                self.call(mod, n, ctx, cls)

    def call(self, mod, n, ctx, cls):
        u = self.dynamic(mod, n, ctx)
        if u:
            self.unresolved.append(u)
            return
        t, why = self.target(mod, n.func, cls)
        if t is not None:
            self.cov['calls_resolved'] += 1
            self.add(g.edge('calls_static', ctx, t, self.prov(mod, n)))
        elif why == 'external':
            self.cov['calls_external'] += 1
        else:
            self.cov['calls_unknown_target'] += 1
        self.job(mod, n, ctx, cls)

    def job(self, mod, n, ctx, cls):
        f = n.func
        nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if nm not in JOB_CALLS:
            return
        tgt = next((k.value for k in n.keywords if k.arg == 'target'), None)
        if tgt is None and nm == 'Timer' and len(n.args) > 1:
            tgt = n.args[1]
        if tgt is None:
            return
        t, _ = self.target(mod, tgt, cls) if isinstance(tgt, (ast.Name, ast.Attribute)) else (None, 'unknown')
        label = '%s->%s' % (ctx['qualname'], t['qualname'] if t else '?')
        jn = g.node('job', self.repo_id, label, mod.rel, n.lineno, via=nm, handler=t['id'] if t else None)
        self.nodes.append(jn); self.cov['jobs'] += 1
        self.add(g.edge('starts_job', ctx, jn, self.prov(mod, n)))
        if t is None:
            self.unresolved.append(g.unresolved(ctx, 'dispatch_unknown', self.prov(mod, n), hint='target фонового задания'))

    def route(self, mod, fn_node, method, path, n, match='exact', framework='decorator', handler_kind='function'):
        path = ac.scrub(path)
        # Модуль в ключе: одинаковый путь двух приложений одного репозитория — два маршрута (NET-AC-072).
        r = g.node('route', self.repo_id, '%s:%s %s' % (mod.name, method or 'ANY', path), mod.rel, n.lineno, method=method or 'ANY',
                   path=path, match=match, framework=framework)
        self.nodes.append(r); self.cov['routes'] += 1
        self.add(g.edge('handles_route', fn_node, r, self.prov(mod, n), handler_kind=handler_kind))

    def decorators(self, mod, st, n):
        for d in st.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)):
                continue
            verb = d.func.attr
            path = _const_str(d.args[0]) if d.args else _const_str(next((k.value for k in d.keywords if k.arg == 'path'), None))
            if verb in ROUTE_VERBS and path and path.startswith('/'):
                methods = [ROUTE_VERBS[verb]] if ROUTE_VERBS[verb] else []
                for k in d.keywords:
                    if k.arg == 'methods' and isinstance(k.value, (ast.List, ast.Tuple)):
                        methods = [_const_str(x).upper() for x in k.value.elts if _const_str(x)]
                for meth in methods or ['ANY']:
                    self.route(mod, n, meth, path, st)
            elif verb == 'command':
                name = _const_str(d.args[0]) if d.args else st.name
                self.command(mod, n, name, st, 'decorator')

    def http_branches(self, mod, st, n, method, cls):
        """Ветки `if path == "/x"` в do_GET/do_POST: маршрут → метод-обработчик ветки (или сам do_X)."""
        def tests(t):
            out = []
            if isinstance(t, ast.Compare) and len(t.ops) == 1 and _is_path(t.left):
                c = t.comparators[0]
                if isinstance(t.ops[0], ast.Eq) and _const_str(c):
                    out.append((_const_str(c), 'exact'))
                elif isinstance(t.ops[0], ast.In) and isinstance(c, (ast.Tuple, ast.List, ast.Set)):
                    out += [(_const_str(x), 'exact') for x in c.elts if _const_str(x)]
            elif (isinstance(t, ast.Call) and isinstance(t.func, ast.Attribute) and t.func.attr == 'startswith'
                  and _is_path(t.func.value) and t.args and _const_str(t.args[0])):
                out.append((_const_str(t.args[0]), 'prefix'))
            return [(p, m) for p, m in out if p.startswith('/')]

        def sole(body):
            """Ветка из одного `return self.x(...)` / `x(...)` — делегирование обработчику x."""
            if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)) or not isinstance(body[0].value, ast.Call):
                return None
            t, _ = self.target(mod, body[0].value.func, cls)
            return t if t is not None and t['id'] != n['id'] else None

        branches, allow, stack = [], [], list(st.body)
        while stack:
            s = stack.pop(0)
            if isinstance(s, ast.If):
                found = tests(s.test)
                if found:
                    branches.append((found, s, sole(s.body)))
                t = s.test
                if (isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.NotIn) and _is_path(t.left)
                        and isinstance(t.comparators[0], (ast.Tuple, ast.List, ast.Set))):
                    # Список допустимых путей: путь без своей ветки обрабатывает остаток метода.
                    allow += [(_const_str(x), s) for x in t.comparators[0].elts if (_const_str(x) or '').startswith('/')]
                stack[0:0] = s.orelse
        seen = {p for found, _, _ in branches for p, _ in found}
        for p, s in allow:
            if p not in seen:
                seen.add(p)
                self.route(mod, n, method, p, s, framework='http.server', handler_kind='fallthrough')
        # Метод, который вызывается в модуле не один раз (send_json), — общий ответчик, а не обработчик маршрута.
        sites = {}
        for c in ast.walk(mod.tree):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name) \
                    and c.func.value.id == 'self':
                sites[c.func.attr] = sites.get(c.func.attr, 0) + 1
        for found, s, h in branches:
            h = h if h is not None and (not h.get('method') or sites.get(h['name'], 0) == 1) else None
            for path, match in found:
                self.route(mod, h or n, method, path, s, match=match, framework='http.server',
                           handler_kind='function' if h else 'inline')

    def command(self, mod, ctx, name, n, framework, handler=None, lam=None):
        name = ac.scrub(name)
        c = g.node('cli_command', self.repo_id, '%s:%s' % (mod.name, name), mod.rel, n.lineno, name=name, framework=framework)
        self.nodes.append(c); self.cov['cli_commands'] += 1
        if handler is not None:
            self.add(g.edge('handles_command', handler, c, self.prov(mod, n)))
        else:
            self.add(g.edge('handles_command', ctx, c, self.prov(mod, n), resolution='dynamic_unresolved',
                            handler_kind='dispatcher'))
            self.unresolved.append(g.unresolved(ctx, 'dispatch_lambda' if lam is not None else 'dispatch_unknown',
                                                self.prov(mod, n), hint=lam))

    def command_tables(self, mod):
        for st in mod.tree.body:
            if not (isinstance(st, ast.Assign) and isinstance(st.value, ast.Dict) and len(st.targets) == 1
                    and isinstance(st.targets[0], ast.Name) and COMMAND_TABLE.search(st.targets[0].id)):
                continue
            keys = [_const_str(k) for k in st.value.keys]
            if not keys or None in keys:
                continue
            for k, v in zip(keys, st.value.values):
                t, _ = self.target(mod, v, None) if isinstance(v, (ast.Name, ast.Attribute)) else (None, 'unknown')
                lam = None
                if isinstance(v, ast.Lambda):
                    script = next((_const_str(a) for c in ast.walk(v.body) if isinstance(c, ast.Call)
                                   for a in c.args if _const_str(a) and _const_str(a).endswith('.py')), None)
                    lam = 'передаёт в %s' % script if script else 'лямбда'
                self.command(mod, mod.node, k, v, 'table:' + st.targets[0].id, handler=t, lam=lam)

    def argparse(self, mod, body, ctx, cls):
        parsers = {}
        nodes = list(shallow(body))
        for n in nodes:
            if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                    and n.value.func.attr == 'add_parser' and n.value.args and _const_str(n.value.args[0])):
                parsers[n.targets[0].id] = (_const_str(n.value.args[0]), n)
        handlers = {}
        for n in nodes:
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'set_defaults'
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in parsers):
                for k in n.keywords:
                    if k.arg == 'func' and isinstance(k.value, (ast.Name, ast.Attribute)):
                        handlers[n.func.value.id] = self.target(mod, k.value, cls)[0]
        for var, (name, n) in parsers.items():
            self.command(mod, ctx, name, n, 'argparse', handler=handlers.get(var))

    def resolve(self, mod):
        self.body_calls(mod, mod.tree.body, mod.node, None)
        self.command_tables(mod)
        self.argparse(mod, mod.tree.body, mod.node, None)

        def walk(body, cls=None):
            for st in body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    n = st._tt_node
                    if isinstance(st, ast.ClassDef):
                        self.body_calls(mod, st.body, n, st.name)
                        walk(st.body, st.name)
                        continue
                    self.body_calls(mod, st.body, n, cls)
                    self.decorators(mod, st, n)
                    self.argparse(mod, st.body, n, cls)
                    if cls and st.name in HTTP_HANDLERS:
                        self.http_branches(mod, st, n, HTTP_HANDLERS[st.name], cls)
                    walk(st.body, cls)
                elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                    for part in ('body', 'orelse', 'finalbody'):
                        walk(getattr(st, part, []) or [], cls)
                    for h in getattr(st, 'handlers', []) or []:
                        walk(h.body, cls)
        walk(mod.tree.body)


def shallow(body):
    """Узлы тела без заходов во вложенные def/class: у вложенных определений свои узлы и свой разбор."""
    stack = list(reversed(body))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield n
        stack.extend(reversed(list(ast.iter_child_nodes(n))))


def _is_path(n):
    return (isinstance(n, ast.Name) and n.id == 'path') or (isinstance(n, ast.Attribute) and n.attr == 'path')


# ------------------------------------------------------------------ проект

def map_project(root, projects):
    """Проект репозитория — по пути из реестра (project_routes). Два владельца — ambiguous, ни одного —
    unassigned. Похожее имя каталога доказательством не считается (ARCH-FR-04.1, TTN-100)."""
    import project_routes
    hit = project_routes.resolve_path(os.path.realpath(root), project_routes.paths(projects or {}))
    if hit['project']:
        return dict(project=hit['project'], method='confirmed_path', candidates=hit['candidates'])
    return dict(project=None, method='ambiguous' if hit['candidates'] else 'unassigned', candidates=hit['candidates'])


def link_runtime(graph, inventory, manual):
    """Связь узла кода с объектом runtime (сервис, контейнер, привязка) — только подтверждённая владельцем
    (TTN-100, NET-AC-077). Автоматически по имени или порту связь не строится. Ссылка на объект, которого нет
    в снимке, или на узел вне графа отклоняется; два разных объекта для одного узла — ambiguous, без связи."""
    nodes = {n['id'] for n in graph['nodes']}
    objs = {o['id']: o for o in (inventory or {}).get('objects', [])}
    by_code = {}
    refused = []
    for m in manual or []:
        if m.get('code') not in nodes or m.get('runtime') not in objs:
            refused.append(dict(m, reason='нет такого узла кода или объекта runtime'))
            continue
        by_code.setdefault(m['code'], []).append(m)
    links, ambiguous = [], []
    for code, ms in sorted(by_code.items()):
        targets = sorted({x['runtime'] for x in ms})
        if len(targets) > 1:
            ambiguous.append(dict(code=code, candidates=targets))
            continue
        x = ms[0]
        links.append(dict(kind='mapping', code=code, runtime=x['runtime'], runtime_kind=objs[x['runtime']]['kind'],
                          method='user_confirmed', by=x.get('by'), at=x.get('at'),
                          deployment=g.deployment(graph['version_ref'], x.get('artifact'))))
    return dict(links=links, ambiguous=ambiguous, refused=refused,
                unmapped=sum(1 for n in graph['nodes'] if n['kind'] in ('route', 'cli_command', 'job')) - len(links))


# ------------------------------------------------------------------ сбор

def scan(root, repo_id, projects=None, run=None, max_files=MAX_FILES, max_file=MAX_FILE, budget=TIME_BUDGET):
    """Граф кода репозитория. Код проекта не исполняется: только чтение байтов и ast.parse."""
    t0 = time.monotonic()
    root = os.path.realpath(root)
    version = git_version(root, repo_id, run)
    found, refused, truncated = files(root, max_files, max_file)
    s = Scanner(root, repo_id, version['commit_sha'])
    for rel, full in found:
        if time.monotonic() - t0 > budget:
            truncated = True
            break
        s.load(rel, full)
    s.packages()
    parsed = [mod for mod in s.modules.values() if mod.tree is not None]
    for mod in parsed:
        s.defs(mod)
    for mod in parsed:
        s.imports(mod)
    for mod in parsed:
        s.resolve(mod)
    cov = dict(s.cov, refused=len(refused), truncated=truncated, duration_ms=int((time.monotonic() - t0) * 1000),
               unresolved=len(s.unresolved), limits=dict(max_files=max_files, max_file=max_file, budget_s=budget))
    by_reason = {}
    for u in s.unresolved:
        by_reason[u['reason']] = by_reason.get(u['reason'], 0) + 1
    cov['unresolved_by_reason'] = by_reason
    out = g.graph(version, s.nodes, s.edges, s.unresolved, cov, map_project(root, projects))
    for kind, key in (('module', 'modules'), ('package', 'packages'), ('class', 'classes'), ('function', 'functions'),
                      ('route', 'routes'), ('cli_command', 'cli_commands'), ('job', 'jobs')):
        cov[key] = sum(1 for n in out['nodes'] if n['kind'] == kind)   # после слияния повторов
    out['refused'] = refused
    return out


def save(graph, data_dir=None):
    import taskstate
    base = Path(data_dir or os.environ.get('TASKTREKER_DATA') or Path(__file__).resolve().parent.parent / 'data') / 'arch' / 'code'
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', graph['version_ref']['repo_id']) + '.json'
    taskstate.save_json(str(base), name, graph)
    return base / name


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    data = Path(os.environ.get('TASKTREKER_DATA') or Path(__file__).resolve().parent.parent / 'data')
    args = [a for a in argv if not a.startswith('--')]
    root = args[0] if args else str(Path(__file__).resolve().parent.parent)
    projects = json.loads((data / 'projects.json').read_text(encoding='utf-8')).get('projects', {})
    proj = map_project(root, projects)
    graph = scan(root, proj['project'] or os.path.basename(os.path.realpath(root)), projects)
    if '--save' in argv:
        print('сохранено:', save(graph, data))
    kinds, etypes = {}, {}
    for n in graph['nodes']:
        kinds[n['kind']] = kinds.get(n['kind'], 0) + 1
    for e in graph['edges']:
        etypes[e['type']] = etypes.get(e['type'], 0) + 1
    print(json.dumps(dict(version=graph['version_ref'], deployment=graph['deployment']['state'], project=graph['project'],
                          nodes=kinds, edges=etypes, coverage=graph['coverage']), ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
