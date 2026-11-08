"""Резервная копия канонических данных и проверка восстановления (TT3-006; ТЗ E0 п.2, §9 п.1, §11 п.2).

В копию входит то, что нельзя пересобрать скриптом:
  * ручной слой: projects.json, comments.json, competitors.json, obsidian.json, inbox.json;
  * оперативный слой: workflow.json (запуски, политика, гранты, журнал операций), ci.json;
  * память: memory/** (записи и индекс), vault_state.json.
Собранное заново (usage, repos, estimates, quality, tree, metrics, dashboard) не копируется.
Ключ владельца (owner.key) не копируется никогда: копия не должна давать права владельца.

Копия лежит вне репозитория (по умолчанию %LOCALAPPDATA%/TaskTreker/backups) — в data/ пути,
оценки и находки ИБ, в git им не место. manifest.json: sha256 и размер каждого файла,
HEAD веток системы, версии схем.

    py tools/backup.py create [--dest каталог] [--snapshots]
    py tools/backup.py verify <копия>
    py tools/backup.py restore <копия> <каталог> [--force]
    py tools/backup.py drill [--dest каталог]   # создать, проверить, восстановить во временный каталог, сравнить
    py tools/backup.py list
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FILES = ('projects.json', 'comments.json', 'competitors.json', 'obsidian.json', 'inbox.json',
         'workflow.json', 'ci.json', 'vault_state.json')
DIRS = ('memory', 'learning')     # learning: журнал опыта агента (LEARN-03), личное, вне git
NEVER = ('owner.key', '.workflow.lock')
SCHEMA = 1


def default_root():
    base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return Path(base) / 'TaskTreker' / 'backups'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def members(data, snapshots=False):
    """Относительные пути файлов копии (POSIX), отсортированы."""
    data = Path(data); out = []
    for name in FILES:
        if (data / name).is_file():
            out.append(name)
    for d in DIRS + (('snapshots',) if snapshots else ()):
        root = data / d
        if root.is_dir():
            for p in sorted(root.rglob('*')):
                if p.is_file() and p.name not in NEVER:
                    out.append(p.relative_to(data).as_posix())
    return sorted(out)


def _git_heads(repo):
    try:
        out = subprocess.run(['git', 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads'],
                             cwd=repo, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split(' ', 1) for line in out.splitlines() if ' ' in line)


def _schemas(data):
    out = {}
    for name in ('workflow.json', 'projects.json', 'memory/index.json'):
        p = Path(data) / name
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
            out[name] = d.get('schema') or d.get('schema_version') if isinstance(d, dict) else None
        except (OSError, ValueError):
            out[name] = None
    return out


def create(data, dest=None, snapshots=False, repo=None, at=None, lock=None):
    """Скопировать канонические файлы в новый каталог и записать манифест. Возвращает путь.

    lock — общая блокировка записи задач: JSON-файлы копируются под ней, чтобы workflow.json
    и projects.json попали в копию согласованными (между ними нет записи сервера)."""
    import contextlib
    data = Path(data); at = at or datetime.now(timezone.utc)
    root = Path(dest) if dest else default_root()
    target = root / ('tt-%s' % at.strftime('%Y%m%dT%H%M%S%fZ'))
    if target.exists():
        raise ValueError('Каталог копии уже есть: %s' % target)
    files = {}
    tmp = Path(str(target) + '.partial')
    def copy(rel):
        src = data / rel; dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        digest = sha256(dst)
        if digest != sha256(src):              # файл меняли во время копирования
            raise ValueError('Файл %s изменился во время копирования — повторите' % rel)
        files[rel] = dict(sha256=digest, size=dst.stat().st_size)
    try:
        rels = members(data, snapshots)
        with (lock() if lock else contextlib.nullcontext()):
            for rel in rels:
                if '/' not in rel:
                    copy(rel)
        for rel in rels:
            if '/' in rel:
                copy(rel)
        manifest = dict(schema=SCHEMA, created_at=at.isoformat(), source=str(data), files=files,
                        heads=_git_heads(repo) if repo else {}, schemas=_schemas(data),
                        excluded=list(NEVER) + ['собираемые файлы (usage, repos, estimates, quality, tree, metrics)'])
        (tmp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.rename(target)                         # копия появляется целиком или не появляется
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return target


def manifest(backup):
    p = Path(backup) / 'manifest.json'
    if not p.is_file():
        raise ValueError('Нет manifest.json: %s — это не копия TaskTreker' % backup)
    return json.loads(p.read_text(encoding='utf-8'))


def verify(backup):
    """[] — копия цела; иначе список проблем (пропал, изменён, лишний файл)."""
    backup = Path(backup); m = manifest(backup); bad = []
    for rel, meta in m['files'].items():
        p = backup / rel
        if not p.is_file():
            bad.append('нет файла: ' + rel)
        elif p.stat().st_size != meta['size'] or sha256(p) != meta['sha256']:
            bad.append('изменён: ' + rel)
    known = set(m['files']) | {'manifest.json'}
    for p in backup.rglob('*'):
        if p.is_file() and p.relative_to(backup).as_posix() not in known:
            bad.append('лишний файл: ' + p.relative_to(backup).as_posix())
    return bad


def restore(backup, target, force=False):
    """Восстановить копию в каталог target. Файл, который в target уже есть и отличается от копии,
    без force не перезаписывается: это могли быть изменения после копии (нужна сверка)."""
    backup, target = Path(backup), Path(target)
    bad = verify(backup)
    if bad:
        raise ValueError('Копия повреждена: ' + '; '.join(bad[:10]))
    m = manifest(backup)
    conflicts = [rel for rel, meta in m['files'].items()
                 if (target / rel).is_file() and sha256(target / rel) != meta['sha256']]
    if conflicts and not force:
        raise ValueError('В %s изменены после копии %d файлов (%s) — без --force не перезаписываю'
                         % (target, len(conflicts), ', '.join(conflicts[:5])))
    for rel in m['files']:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / rel, dst)
    return dict(restored=len(m['files']), overwritten=conflicts if force else [])


def compare(backup, target):
    """Побайтовое сравнение восстановленного с копией: [] — совпадает."""
    m = manifest(backup); diff = []
    for rel in m['files']:
        a, b = Path(backup) / rel, Path(target) / rel
        if not b.is_file() or a.read_bytes() != b.read_bytes():
            diff.append(rel)
    return diff


def drill(data, dest=None, repo=None, lock=None):
    """Учение: копия → проверка → восстановление во временный каталог → побайтовое сравнение."""
    b = create(data, dest, repo=repo, lock=lock)
    bad = verify(b)
    with tempfile.TemporaryDirectory(prefix='tt-restore-') as tmp:
        res = restore(b, tmp) if not bad else dict(restored=0)
        diff = compare(b, tmp) if not bad else ['копия не прошла проверку']
    m = manifest(b)
    return dict(backup=str(b), files=len(m['files']), bytes=sum(f['size'] for f in m['files'].values()),
                verify=bad, restored=res['restored'], mismatched=diff, ok=not bad and not diff)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import workflow
    import taskstate
    data = workflow.data_dir(); repo = Path(__file__).resolve().parents[1]
    lock = lambda: taskstate.locked(str(data), wait=20)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__); return 0
    cmd, rest = argv[0], argv[1:]
    opt = lambda k: rest[rest.index(k) + 1] if k in rest and rest.index(k) + 1 < len(rest) else None
    try:
        if cmd == 'create':
            b = create(data, opt('--dest'), '--snapshots' in rest, repo, lock=lock)
            m = manifest(b); print('Копия: %s · файлов %d' % (b, len(m['files'])))
        elif cmd == 'verify':
            bad = verify(rest[0]); print('Копия цела' if not bad else '\n'.join(bad)); return 1 if bad else 0
        elif cmd == 'restore':
            print(json.dumps(restore(rest[0], rest[1], '--force' in rest), ensure_ascii=False))
        elif cmd == 'drill':
            r = drill(data, opt('--dest'), repo, lock); print(json.dumps(r, ensure_ascii=False, indent=1)); return 0 if r['ok'] else 1
        elif cmd == 'list':
            root = Path(opt('--dest') or default_root())
            for b in sorted(root.glob('tt-*')) if root.is_dir() else []:
                if b.is_dir() and not b.name.endswith('.partial'):
                    print(b)
        else:
            print(__doc__); return 1
    except (ValueError, OSError, IndexError) as e:
        print('Ошибка: %s' % e); return 1
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    sys.exit(main())
