"""Запуск сессии ИИ по задаче: окно Claude Code или Codex с моделью, усилиями и заданием.

Приложения Claude и Codex несут свои командные строки (claude.exe, codex.exe), даже если
их нет в PATH. Запуск открывает НОВОЕ ОКНО с интерактивной сессией: владелец видит работу
и подтверждает действия, как обычно. Фонового режима без окна здесь нет намеренно.

Безопасность: команда собирается списком аргументов без оболочки; модель и усилия берутся
из каталога и проверяются; рабочая папка — из ручного projects.json и должна существовать;
вызывается только локальным сервером досок по нажатию владельца.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ENV_BIN = {'claude': 'TASKTREKER_CLAUDE_BIN', 'codex': 'TASKTREKER_CODEX_BIN'}
NAMES = {'claude': 'claude.exe' if os.name == 'nt' else 'claude', 'codex': 'codex.exe' if os.name == 'nt' else 'codex'}


def _version_key(path):
    """Новая версия выше: claude-code/2.1.295/… сравнивается по числам пути."""
    nums = [int(n) for n in re.findall(r'\d+', str(path.parent.parent.name if path.parent.parent else path))]
    return (nums, path.stat().st_mtime if path.exists() else 0)


def _bundled(provider):
    home = Path(os.environ.get('APPDATA', '')) if provider == 'claude' else Path(os.environ.get('LOCALAPPDATA', ''))
    pattern = ('Claude/claude-code/*/*/' if provider == 'claude' else 'OpenAI/Codex/bin/*/') + NAMES[provider]
    found = [p for p in home.glob(pattern)] if str(home) not in ('', '.') else []
    if provider == 'codex':
        return max(found, key=lambda p: p.stat().st_mtime, default=None)
    return max(found, key=_version_key, default=None)


def find_cli(provider):
    """Путь к CLI: переменная окружения, затем PATH, затем копия внутри приложения."""
    explicit = os.environ.get(ENV_BIN[provider])
    if explicit:
        return explicit if Path(explicit).is_file() else None
    in_path = shutil.which(provider)
    if in_path:
        return in_path
    bundled = _bundled(provider)
    return str(bundled) if bundled else None


def status():
    out = {}
    for provider in ENV_BIN:
        path = find_cli(provider)
        out[provider] = dict(available=bool(path), path=path or '',
                             can_open_window=os.name == 'nt',
                             note='' if path else 'Командная строка не найдена: установите приложение или задайте ' + ENV_BIN[provider])
    return out


def command(provider, exe, model, effort, prompt, cwd):
    if provider == 'claude':
        return [exe, '--model', model, '--effort', effort, prompt]
    if provider == 'codex':
        return [exe, '-m', model, '-c', 'model_reasoning_effort="%s"' % effort, '-C', cwd, prompt]
    raise ValueError('Неизвестный ИИ: ' + str(provider))


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')


def ensure_worktree(repo, agent, task_id, base=None):
    """Отдельная рабочая копия задачи: ветка ai/<ИИ>/<задача> от базовой ветки.

    Лежит рядом с репозиторием, в <repo>.worktrees/<ИИ>-<задача>, чтобы не попадать в git
    самого проекта. Повторный вызов возвращает ту же копию. Не git — None: работа идёт в папке."""
    repo = Path(repo)
    if not (repo / '.git').exists():
        return None
    slug = re.sub(r'[^a-z0-9.-]+', '-', task_id.lower()).strip('-')
    branch = 'ai/%s/%s' % (agent, slug)
    path = repo.parent / (repo.name + '.worktrees') / ('%s-%s' % (agent, slug))
    listed = _git(['worktree', 'list', '--porcelain'], repo).stdout
    if any(line == 'worktree ' + path.as_posix() or line.replace('\\', '/') == 'worktree ' + path.as_posix()
           for line in listed.splitlines()):
        return dict(path=str(path), branch=branch, created=False)
    if path.exists():
        raise ValueError('Папка %s занята, но это не рабочая копия git' % path)
    base = base or _git(['rev-parse', '--abbrev-ref', 'HEAD'], repo).stdout.strip() or 'HEAD'
    exists = _git(['rev-parse', '--verify', '--quiet', 'refs/heads/' + branch], repo).returncode == 0
    args = ['worktree', 'add', str(path), branch] if exists else ['worktree', 'add', '-b', branch, str(path), base]
    r = _git(args, repo)
    if r.returncode != 0:
        raise ValueError('Не удалось создать рабочую копию: ' + (r.stderr.strip() or r.stdout.strip())[:300])
    return dict(path=str(path), branch=branch, base=base, created=True)


def open_window(cmd, cwd, env_extra=None):
    """Новое окно консоли с интерактивной сессией. Возвращает pid."""
    if os.name != 'nt':
        raise ValueError('Открытие окна сессии поддержано только в Windows')
    env = dict(os.environ, **(env_extra or {}))
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return proc.pid


def launch(provider, model, effort, prompt, cwd, env_extra=None):
    exe = find_cli(provider)
    if not exe:
        raise ValueError(status()[provider]['note'])
    if not cwd or not Path(cwd).is_dir():
        raise ValueError('Рабочая папка проекта не найдена: %s' % (cwd or 'не указана'))
    cmd = command(provider, exe, model, effort, prompt, cwd)
    return dict(pid=open_window(cmd, cwd, env_extra), exe=exe, cwd=cwd)


def _codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()


def diagnose(run):
    """Почему открытая сессия не приняла задачу — по журналу Codex той же папки.

    Сессия с отказом (исчерпан лимит, недоступна модель) завершается сразу, а окно остаётся
    открытым: без разбора владелец видит лишь «не принята». Читаются только метаданные
    и текст ошибки, переписка не копируется."""
    launch = run.get('launch') or {}
    if launch.get('agent') != 'codex' or not launch.get('cwd') or not run.get('launched_at'):
        return None
    try:
        from datetime import datetime
        since = datetime.fromisoformat(run['launched_at']).timestamp() - 60
    except ValueError:
        return None
    cwd = os.path.normcase(os.path.normpath(launch['cwd']))
    files = [p for p in (_codex_home() / 'sessions').glob('*/*/*/rollout-*.jsonl') if p.stat().st_mtime >= since]
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        meta_cwd, error = None, None
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"session_meta"' in line or '"task_complete"' in line:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    payload = row.get('payload') or {}
                    if row.get('type') == 'session_meta':
                        meta_cwd = payload.get('cwd')
                    elif payload.get('type') == 'task_complete' and payload.get('error'):
                        error = payload['error']
        if meta_cwd and os.path.normcase(os.path.normpath(meta_cwd)) == cwd and error:
            return dict(source='codex', code=error.get('codex_error_info') or '', message=str(error.get('message') or '')[:300])
    return None
