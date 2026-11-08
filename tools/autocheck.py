"""Автопроверка ветки задачи и слияние после ревью.

Проверка — факты, которые не требуют суждения: в ветке есть коммиты, изменённые файлы
лежат в области задачи, проверки проекта проходят, ветка сливается без конфликтов,
рабочая копия чиста. Суждение о критериях приёмки и соответствии ТЗ делает ревью ИИ;
слияние выполняется только после него и только при пройденной автопроверке.
"""
import subprocess
from datetime import datetime, timezone

VERIFY_TIMEOUT = 900   # проверки проекта: не дольше 15 минут


def _git(args, cwd, timeout=120):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def _tail(text, n=600):
    text = (text or '').strip()
    return text if len(text) <= n else '…' + text[-n:]


def _inside(path, scope):
    path = path.replace('\\', '/').strip('/').lower()
    for p in scope:
        p = p.replace('\\', '/').strip('/').lower()
        while p.startswith('./'): p = p[2:]
        if p in ('', '.') or path == p or path.startswith(p + '/'):
            return True
    return False


def run_checks(repo, worktree, branch, base, scope=None, verify=None, timeout=VERIFY_TIMEOUT):
    """Проверки ветки. Возвращает {ok, at, checks:[{key, name, ok, detail}], files}."""
    checks, files = [], []
    def add(key, name, ok, detail=''):
        checks.append(dict(key=key, name=name, ok=bool(ok), detail=detail))

    ahead = _git(['rev-list', '--count', '%s..%s' % (base, branch)], repo)
    count = int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 0
    add('commits', 'В ветке есть коммиты', count > 0,
        '%d коммит(ов) поверх %s' % (count, base) if ahead.returncode == 0 else _tail(ahead.stderr))

    diff = _git(['diff', '--name-only', '%s...%s' % (base, branch)], repo)
    files = [f for f in diff.stdout.splitlines() if f.strip()]
    if scope:
        outside = [f for f in files if not _inside(f, scope)]
        add('scope', 'Изменения в области задачи', not outside,
            'вне области: ' + ', '.join(outside[:10]) if outside else '%d файл(ов) в области' % len(files))
    else:
        add('scope', 'Изменения в области задачи', True, 'область не задана — проверка пропущена')

    status = _git(['status', '--porcelain'], worktree)
    dirty = [l for l in status.stdout.splitlines() if l.strip()]
    add('clean', 'Всё закоммичено', not dirty, 'незакоммичено: ' + ', '.join(l[3:] for l in dirty[:10]) if dirty else 'рабочая копия чиста')

    merge = _git(['merge-tree', '--write-tree', '--name-only', base, branch], repo)
    add('merge', 'Сливается без конфликтов', merge.returncode == 0,
        'конфликт: ' + _tail(merge.stdout, 300) if merge.returncode == 1 else ('чисто' if merge.returncode == 0 else _tail(merge.stderr)))

    if verify:
        try:
            r = subprocess.run(verify, cwd=worktree, capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
            add('verify', 'Проверки проекта: ' + ' '.join(verify), r.returncode == 0,
                _tail((r.stdout or '') + '\n' + (r.stderr or '')))
        except (OSError, subprocess.TimeoutExpired) as e:
            add('verify', 'Проверки проекта: ' + ' '.join(verify), False, str(e)[:300])
    else:
        add('verify', 'Проверки проекта', True, 'команда verify у проекта не задана — проверка пропущена')

    return dict(ok=all(c['ok'] for c in checks), at=datetime.now(timezone.utc).isoformat(),
                checks=checks, files=files[:50], branch=branch, base=base)


def merge(repo, branch, base, message):
    """Слить ветку задачи в базовую в основной рабочей копии проекта.

    Только если там выгружена базовая ветка и нет незакоммиченных изменений отслеживаемых
    файлов; при конфликте слияние отменяется. Возвращает (ok, коммит или причина)."""
    current = _git(['rev-parse', '--abbrev-ref', 'HEAD'], repo).stdout.strip()
    if current != base:
        return False, 'в основной копии выгружена %s, а не %s' % (current or '?', base)
    dirty = _git(['status', '--porcelain', '--untracked-files=no'], repo).stdout.strip()
    if dirty:
        return False, 'в основной копии есть незакоммиченные изменения'
    r = _git(['merge', '--no-ff', branch, '-m', message], repo)
    if r.returncode != 0:
        _git(['merge', '--abort'], repo)
        return False, 'слияние не удалось: ' + _tail(r.stderr or r.stdout, 300)
    return True, _git(['rev-parse', '--short', 'HEAD'], repo).stdout.strip()
