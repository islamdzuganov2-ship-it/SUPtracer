"""Сканирование безопасности кода задачи (TT-125; SEC-012, SEC-013, SEC-TC-18/19/20).

Сканер запускает сервер, а не агент, и на конкретном коммите: прогон CI тестовой ветки после зелёных
шагов (ci.run_pipeline) или `pm.py security scan <проект>`. Результат — в data/security/scans.json
(вне git: там пути и находки). Найденное не подменяет решение: High/Critical в файлах задачи блокируют
выпуск через security_triage.blocking(), остальное — сигнал.

Сканеры:
  * builtin — всегда: секреты по шаблонам (значение маскируется до записи), опасные вызовы Python по AST
    (shell=True, eval/exec, pickle, yaml.load без Loader, verify=False, os.system).
  * bandit, semgrep (только локальные правила security/rules — защищённый эталон), gitleaks (--redact) —
    если установлены. Не установлен — статус not_run с причиной, а не «чисто».
  * pip-audit — только по явному включению (security.scanners в настройках): он ходит в сеть.
Учётных данных сканеры не получают: окружение — sandbox_env.minimal().
"""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

DEFAULT = ('builtin', 'bandit', 'semgrep', 'gitleaks')
SEVERITIES = ('info', 'low', 'medium', 'high', 'critical')
TEXT_EXT = {'.py', '.js', '.ts', '.tsx', '.jsx', '.json', '.yml', '.yaml', '.toml', '.ini', '.cfg', '.env', '.md', '.txt',
            '.sh', '.ps1', '.cmd', '.bat', '.html', '.css', '.sql', '.xml', '.properties', '.conf', ''}
MAX_FILE = 1_000_000
TIMEOUT = 300
KEEP = 60
SECRET = re.compile(r'(?:api[_-]?key|secret|token|passw(?:or)?d)\s*[:=]\s*["\']?[A-Za-z0-9_\-/+=]{16,}|sk-[A-Za-z0-9]{20,}|'
                    r'ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', re.I)


def mask(text):
    """Значение секрета не пишется никуда: ни в находку, ни в журнал."""
    return SECRET.sub(lambda m: m.group(0)[:4] + '…[скрыто]', str(text or ''))[:300]


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def store_path():
    import workflow as w
    return w.data_dir() / 'security' / 'scans.json'


def load():
    try:
        return json.loads(store_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'scans': []}


def save(d):
    p = store_path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8'); os.replace(tmp, p)


def finding(scanner, rule, severity, file, line, message, snippet=''):
    file = str(file or '').replace('\\', '/').lstrip('./')
    sev = severity if severity in SEVERITIES else 'medium'
    key = hashlib.sha256('|'.join([scanner, rule, file, re.sub(r'\s+', ' ', mask(snippet)).strip()[:200]]).encode('utf-8')).hexdigest()[:16]
    return dict(key=key, scanner=scanner, rule=rule, severity=sev, file=file, line=line, message=mask(message))


def _tracked(root):
    r = subprocess.run(['git', '-c', 'core.quotepath=off', 'ls-files', '-z'], cwd=root, capture_output=True, timeout=120)
    if r.returncode != 0:
        return [p.relative_to(root).as_posix() for p in Path(root).rglob('*') if p.is_file() and '.git' not in p.parts]
    return [f for f in r.stdout.decode('utf-8', 'replace').split('\0') if f]


def _test_path(f):
    return bool(re.search(r'(^|/)(tests?|fixtures|spec|__tests__|examples?)/', f))


class _Py(ast.NodeVisitor):
    def __init__(self, f, lines):
        self.f, self.lines, self.out = f, lines, []

    def add(self, node, rule, sev, msg):
        ln = getattr(node, 'lineno', 0)
        self.out.append(finding('builtin', rule, sev, self.f, ln, msg, self.lines[ln - 1] if 0 < ln <= len(self.lines) else ''))

    def visit_Call(self, node):
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ''
        owner = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else ''
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if 'shell' in kw and isinstance(kw['shell'], ast.Constant) and kw['shell'].value is True:
            self.add(node, 'PY-SHELL', 'high', 'subprocess с shell=True: строка команды уходит в оболочку — инъекция команд')
        if name in ('eval', 'exec') and not owner:
            self.add(node, 'PY-EVAL', 'medium', '%s(): выполнение произвольного кода из строки' % name)
        if owner == 'os' and name in ('system', 'popen'):
            self.add(node, 'PY-OSSYS', 'medium', 'os.%s: команда через оболочку' % name)
        if owner in ('pickle', 'cPickle', 'marshal') and name in ('load', 'loads'):
            self.add(node, 'PY-PICKLE', 'medium', '%s.%s: десериализация недоверенных данных выполняет код' % (owner, name))
        if owner == 'yaml' and name == 'load' and 'Loader' not in kw:
            self.add(node, 'PY-YAML', 'medium', 'yaml.load без Loader: используйте yaml.safe_load')
        if 'verify' in kw and isinstance(kw['verify'], ast.Constant) and kw['verify'].value is False:
            self.add(node, 'PY-TLS', 'medium', 'verify=False: проверка TLS-сертификата отключена')
        self.generic_visit(node)


def builtin(root):
    out = []
    for f in _tracked(root):
        p = Path(root) / f
        if p.suffix.lower() not in TEXT_EXT or not p.is_file():
            continue
        try:
            if p.stat().st_size > MAX_FILE:
                continue
            text = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if SECRET.search(line):
                test = _test_path(f)
                out.append(finding('builtin', 'SECRET', 'medium' if test else 'high', f, i,
                                   'похоже на секрет в коде%s' % (' (тестовые данные)' if test else ''), line))
        if p.suffix == '.py':
            try:
                v = _Py(f, lines); v.visit(ast.parse(text)); out += v.out
            except (SyntaxError, ValueError, RecursionError):
                pass
    return out


def _run(cmd, root, timeout=TIMEOUT):
    import sandbox_env
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                          stdin=subprocess.DEVNULL, env=sandbox_env.minimal(dict(PYTHONIOENCODING='utf-8')))


def bandit(root):
    r = _run([shutil.which('bandit'), '-r', '.', '-f', 'json', '-q', '-x', './.git,./node_modules,./.venv'], root)
    d = json.loads(r.stdout or '{}')
    return [finding('bandit', x.get('test_id', '?'), (x.get('issue_severity') or 'medium').lower(), x.get('filename'),
                    x.get('line_number'), x.get('issue_text'), x.get('code', '')) for x in d.get('results', [])]


def semgrep(root):
    rules = Path(root) / 'security' / 'rules'
    if not rules.is_dir():
        raise LookupError('нет локальных правил security/rules — внешние правила требуют сети')
    r = _run([shutil.which('semgrep'), '--config', str(rules), '--json', '--metrics=off', '--disable-version-check', '--quiet'], root)
    d = json.loads(r.stdout or '{}')
    sev = {'ERROR': 'high', 'WARNING': 'medium', 'INFO': 'low'}
    return [finding('semgrep', x.get('check_id', '?'), sev.get((x.get('extra') or {}).get('severity'), 'medium'), x.get('path'),
                    (x.get('start') or {}).get('line'), (x.get('extra') or {}).get('message'), (x.get('extra') or {}).get('lines', ''))
            for x in d.get('results', [])]


def gitleaks(root):
    with tempfile.TemporaryDirectory() as tmp:
        rep = Path(tmp) / 'r.json'
        _run([shutil.which('gitleaks'), 'detect', '--no-git', '--redact', '--source', '.', '--report-format', 'json',
              '--report-path', str(rep), '--exit-code', '0'], root)
        d = json.loads(rep.read_text(encoding='utf-8') or '[]') if rep.exists() else []
    return [finding('gitleaks', x.get('RuleID', '?'), 'medium' if _test_path(str(x.get('File', ''))) else 'high', x.get('File'),
                    x.get('StartLine'), 'gitleaks: ' + str(x.get('Description', '')), x.get('Fingerprint', '')) for x in d]


def pip_audit(root):
    req = Path(root) / 'requirements.txt'
    if not req.exists():
        raise LookupError('нет requirements.txt')
    r = _run([shutil.which('pip-audit'), '-r', str(req), '-f', 'json', '--progress-spinner', 'off'], root)
    d = json.loads(r.stdout or '{}')
    deps = d.get('dependencies', d if isinstance(d, list) else [])
    return [finding('pip-audit', v.get('id', '?'), 'high', 'requirements.txt', None,
                    '%s %s: %s (исправлено в %s)' % (x.get('name'), x.get('version'), v.get('id'), ', '.join(v.get('fix_versions') or []) or '—'),
                    x.get('name', '') + v.get('id', ''))
            for x in deps for v in x.get('vulns', [])]


RUNNERS = {'builtin': (None, builtin), 'bandit': ('bandit', bandit), 'semgrep': ('semgrep', semgrep),
           'gitleaks': ('gitleaks', gitleaks), 'pip-audit': ('pip-audit', pip_audit)}


def scanners(settings=None):
    conf = ((settings or {}).get('security') or {}).get('scanners')
    return [x for x in (conf if isinstance(conf, list) and conf else DEFAULT) if x in RUNNERS]


def scan(root, project, sha=None, ref=None, source='manual', settings=None, record=True):
    """Прогнать сканеры в рабочей копии root. Возвращает запись прогона; record — сохранить в scans.json."""
    if settings is None:
        try:
            import workflow as w
            settings = w.settings()
        except Exception:  # noqa: BLE001
            settings = {}
    if not sha:
        r = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True)
        sha = r.stdout.strip() if r.returncode == 0 else None
    res, findings = {}, []
    for name in scanners(settings):
        exe, fn = RUNNERS[name]
        if exe and not shutil.which(exe):
            res[name] = dict(status='not_run', note='%s не установлен' % exe); continue
        try:
            got = fn(root)
            res[name] = dict(status='ran', count=len(got)); findings += got
        except LookupError as e:
            res[name] = dict(status='not_run', note=str(e))
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            res[name] = dict(status='error', note=mask(str(e))[:200])
    seen, uniq = set(), []
    for f in findings:
        if f['key'] not in seen:
            seen.add(f['key']); uniq.append(f)
    rec = dict(id='SCAN-%s-%s' % (datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'), os.urandom(2).hex()), project=project,
               ref=ref, sha=sha, at=now(), source=source, scanners=res, findings=uniq,
               counts={s: sum(1 for f in uniq if f['severity'] == s) for s in SEVERITIES})
    if record:
        d = load(); d['scans'] = (d.get('scans') or [])[-(KEEP - 1):] + [rec]; save(d)
    return rec


def latest(project, sha=None):
    scans = [x for x in load().get('scans') or [] if x.get('project') == project]
    if sha:
        exact = [x for x in scans if x.get('sha') == sha]
        return exact[-1] if exact else None
    return scans[-1] if scans else None
