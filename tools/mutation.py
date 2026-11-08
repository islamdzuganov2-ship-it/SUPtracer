"""Мутационная проверка тестов (07: A6-08, AG-QA-06, E2E-R14; SEC-022).

Зелёные тесты ещё не значат, что они что-то проверяют. Сервер вносит в изменённый задачей Python-код
маленькие ошибки (мутанты): меняет оператор сравнения, арифметику, and/or, True/False — и гоняет тесты
проекта на копии рабочей копии. Мутант «убит», если тесты упали. Доля убитых — оценка силы тестов;
выжившие мутанты называются по месту (файл:строка, что изменено) — это готовые недостающие тест-кейсы.

Только стандартная библиотека (ast), без внешних инструментов. Копия делается во временный каталог,
рабочая копия задачи не меняется. Набор мутантов детерминирован и ограничен (LIMIT), чтобы проверка
укладывалась в разумное время.
"""
import ast
import copy
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

LIMIT = 20
THRESHOLD = 0.6
TIMEOUT = 180
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.mypy_cache', '.pytest_cache', 'dist', 'build'}

SWAP_CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
            ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
SWAP_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult, ast.Div: ast.Mult, ast.Mod: ast.FloorDiv}
SWAP_BOOL = {ast.And: ast.Or, ast.Or: ast.And}
SYMBOL = {ast.Eq: '==', ast.NotEq: '!=', ast.Lt: '<', ast.LtE: '<=', ast.Gt: '>', ast.GtE: '>=', ast.In: 'in', ast.NotIn: 'not in',
          ast.Is: 'is', ast.IsNot: 'is not', ast.Add: '+', ast.Sub: '-', ast.Mult: '*', ast.FloorDiv: '//', ast.Div: '/',
          ast.Mod: '%', ast.And: 'and', ast.Or: 'or'}


def _sites(tree):
    """Места мутаций в порядке обхода: (узел, вид, индекс в узле)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            out += [(node, 'cmp', i) for i, op in enumerate(node.ops) if type(op) in SWAP_CMP]
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in SWAP_BIN:
            out.append((node, 'bin', 0))
        elif isinstance(node, ast.BoolOp) and type(node.op) in SWAP_BOOL:
            out.append((node, 'bool', 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            out.append((node, 'const', 0))
    return out


def _apply(node, kind, i):
    if kind == 'cmp':
        old = type(node.ops[i]); node.ops[i] = SWAP_CMP[old]()
        return '%s → %s' % (SYMBOL[old], SYMBOL[SWAP_CMP[old]])
    if kind == 'bin':
        old = type(node.op); node.op = SWAP_BIN[old]()
        return '%s → %s' % (SYMBOL[old], SYMBOL[SWAP_BIN[old]])
    if kind == 'bool':
        old = type(node.op); node.op = SWAP_BOOL[old]()
        return '%s → %s' % (SYMBOL[old], SYMBOL[SWAP_BOOL[old]])
    node.value = not node.value
    return '%s → %s' % (not node.value, node.value)


def mutants(source, limit=LIMIT):
    """[(строка, описание, исходник мутанта)] — детерминированно, не больше limit, равномерно по файлу."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    sites = _sites(tree)
    if not sites:
        return []
    step = max(1, len(sites) // limit)
    out = []
    for n in range(0, len(sites), step)[:limit]:
        t2 = copy.deepcopy(tree)
        node, kind, i = _sites(t2)[n]
        desc = _apply(node, kind, i)
        out.append((getattr(node, 'lineno', 0), desc, ast.unparse(t2)))
    return out


def _copy(root, dst):
    def ignore(d, names):
        return [n for n in names if n in SKIP_DIRS]
    shutil.copytree(root, dst, ignore=ignore, dirs_exist_ok=True)


def _env():
    try:
        import sandbox_env
        return sandbox_env.minimal()
    except Exception:  # noqa: BLE001
        return None


def _tests(cmd, cwd, timeout):
    try:
        import job_runner
        r = job_runner.run(cmd, cwd=cwd, timeout=timeout, env=_env(), shell=isinstance(cmd, str))
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False                     # зависание под мутантом — тоже «убит»


def code_files(files):
    """Изменённый задачей Python-код без тестов."""
    import pipeline as P
    return sorted(f for f in files if f.endswith('.py') and not P.TEST_FILE.search(f.replace('\\', '/')))


def run(root, files, cmd, limit=LIMIT, timeout=TIMEOUT):
    """Итог: status passed|failed|not_run, score, killed/total, survivors."""
    files = code_files(files)
    if not files:
        return dict(status='not_run', reason='в изменениях нет Python-кода', score=None, total=0, killed=0, survivors=[])
    if not cmd:
        return dict(status='failed', reason='у проекта нет тестовой команды — мутантов нечем проверять', score=0.0,
                    total=0, killed=0, survivors=[])
    per_file = max(1, limit // len(files))
    with tempfile.TemporaryDirectory(prefix='tt-mut-') as d:
        work = Path(d) / 'w'
        _copy(root, work)
        if not _tests(cmd, work, timeout):
            return dict(status='failed', reason='тесты красные и без мутаций — мутационная проверка бессмысленна', score=0.0,
                        total=0, killed=0, survivors=[])
        total = killed = 0; survivors = []
        for rel in files:
            p = work / rel
            if not p.is_file():
                continue
            original = p.read_text(encoding='utf-8')
            for line, desc, text in mutants(original, per_file):
                if total >= limit:
                    break
                p.write_text(text, encoding='utf-8')
                dead = not _tests(cmd, work, timeout)
                total += 1; killed += dead
                if not dead:
                    survivors.append(dict(file=rel, line=line, mutation=desc))
            p.write_text(original, encoding='utf-8')
    if not total:
        return dict(status='not_run', reason='в изменённом коде нет мест для мутаций', score=None, total=0, killed=0, survivors=[])
    score = round(killed / total, 3)
    return dict(status='passed' if score >= THRESHOLD else 'failed', score=score, total=total, killed=killed,
                survivors=survivors[:10], threshold=THRESHOLD)


def summary(res):
    if res['status'] == 'not_run':
        return 'мутационная проверка не нужна: %s' % res['reason']
    if res.get('reason'):
        return res['reason']
    s = 'убито мутантов %d из %d (%.0f%%, порог %.0f%%)' % (res['killed'], res['total'], res['score'] * 100, THRESHOLD * 100)
    if res['survivors']:
        s += '; тесты не ловят: ' + '; '.join('%s:%d %s' % (x['file'], x['line'], x['mutation']) for x in res['survivors'][:5])
    return s
