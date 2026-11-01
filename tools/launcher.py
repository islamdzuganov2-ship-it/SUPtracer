"""Запуск сессии ИИ по задаче: окно Claude Code или Codex с моделью, усилиями и заданием.

Приложения Claude и Codex несут свои командные строки (claude.exe, codex.exe), даже если
их нет в PATH. Два режима согласия владельца (настройка autonomy):

  step — пошаговое согласие: НОВОЕ ОКНО с интерактивной сессией, владелец видит работу
         и подтверждает действия (правки файлов в рабочей копии роли — без вопросов);
  full — полная автономия по одному согласию: сессия без окна в фоне, журнал в
         ops/logs/sessions/<запуск>.log. Claude — `-p --permission-mode auto` (классификатор
         Claude Code сам разрешает безопасное и отклоняет опасное), Codex — `exec --approve-for-me`
         (песочница workspace-write и автоматическая проверка запросов).

Безопасность: команда собирается списком аргументов без оболочки; модель и усилия берутся
из каталога и проверяются; рабочая папка — из ручного projects.json и должна существовать;
вызывается только локальным сервером досок по нажатию владельца.
"""
import json
import os
import re
import shutil
import subprocess
import uuid
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


ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / 'ops' / 'logs' / 'sessions'


LEAN_TOOLS = 'Bash,Read,Edit,Write,Glob,Grep'
WEB_TOOLS = 'WebSearch,WebFetch'
WEB_ROLES = ('product_owner', 'marketer', 'resolver')


def _user_auto_mode():
    """Правила автоматического режима из пользовательских настроек Claude: лёгкая сессия их сама не читает."""
    try:
        with open(os.path.expanduser('~/.claude/settings.json'), encoding='utf-8') as fh:
            return json.load(fh).get('autoMode')
    except (OSError, ValueError):
        return None


def lean(provider, role, research=True, own=()):
    """Лёгкий старт сессии роли конвейера (Л1). Замер: сессия Claude начиналась с 43–46 тыс. токенов
    до первого действия — плагины, чужие MCP-серверы, сотня скиллов, полный список инструментов
    TaskTreker; всё это едет в каждом ходе. Роли нужны файлы, Bash, tt_work и свой скилл.

    Возвращает (аргументы командной строки, переменные окружения). TASKTREKER_LEAN=0 — обычный старт."""
    if os.environ.get('TASKTREKER_LEAN') == '0':
        return [], {}
    env = {'TASKTREKER_OWN': ','.join(own)}
    if provider == 'codex':
        return ['-c', 'mcp_servers.tasktreker.env.TASKTREKER_MCP_ROLE="1"'], env
    if provider != 'claude':
        return [], {}
    import skills
    tools_dir = Path(__file__).resolve().parent
    LOGS.mkdir(parents=True, exist_ok=True)

    def hook(script):
        return dict(type='command', command='py', args=['-3', (tools_dir / script).as_posix()], timeout=20)
    mcp = LOGS / 'lean-mcp.json'
    mcp.write_text(json.dumps({'mcpServers': {'tasktreker': dict(type='stdio', command='py',
                   args=['-3', (tools_dir / 'mcp_server.py').as_posix()], env={'TASKTREKER_MCP_ROLE': '1'})}}), encoding='utf-8')
    conf = dict(hooks=dict(PreToolUse=[dict(matcher='Read', hooks=[hook('read_guard.py')])],
                           SessionStart=[dict(matcher='compact', hooks=[hook('read_guard.py')])]))
    auto = _user_auto_mode()
    if auto:
        conf['autoMode'] = auto
    settings = LOGS / 'lean-settings.json'
    settings.write_text(json.dumps(conf, ensure_ascii=False), encoding='utf-8')
    skill = LOGS / ('skill-%s.md' % role)
    skill.write_text(skills.render(role), encoding='utf-8')
    tools = LEAN_TOOLS + (',' + WEB_TOOLS if role in WEB_ROLES and research else '')
    return ['--strict-mcp-config', '--mcp-config', str(mcp), '--setting-sources', 'project,local', '--settings', str(settings),
            '--tools', tools, '--append-system-prompt-file', str(skill), '--exclude-dynamic-system-prompt-sections'], env


def git_common_dir(cwd):
    """Каталог .git основного репозитория: рабочая копия задачи коммитит туда — песочнице Codex он нужен на запись."""
    r = _git(['rev-parse', '--path-format=absolute', '--git-common-dir'], cwd) if cwd else None
    return r.stdout.strip() if r is not None and r.returncode == 0 and r.stdout.strip() else None


def headless_command(provider, exe, model, effort, prompt, cwd, session=None, resume=False, extra=()):
    """Сессия без окна для полной автономии: работает до сдачи этапа и завершается.
    extra — аргументы лёгкого старта (lean); у Claude они до --model: --tools и --mcp-config принимают несколько значений."""
    if provider == 'claude':
        head = ['--resume', session] if resume else (['--session-id', session] if session else [])
        return [exe, '-p', *head, '--permission-mode', 'auto', *extra, '--model', model, '--effort', effort, prompt]
    if provider == 'codex':
        gitdir = git_common_dir(cwd)
        effort_cfg = ['-c', 'model_reasoning_effort="%s"' % effort]
        if resume:   # exec resume не принимает флаги песочницы — те же правила через -c
            roots = ['-c', 'sandbox_workspace_write.writable_roots=%s' % json.dumps([gitdir] if gitdir else [])]
            return [exe, 'exec', 'resume', session, '-m', model, *effort_cfg, *extra, '-c', 'sandbox_mode="workspace-write"',
                    *roots, '--skip-git-repo-check', prompt]
        # --approve-for-me сам включает песочницу workspace-write; вместе с -s Codex его не принимает.
        return [exe, 'exec', '-m', model, *effort_cfg, *extra, '-C', cwd, '--approve-for-me',
                *(['--add-dir', gitdir] if gitdir else []), '--skip-git-repo-check', prompt]
    raise ValueError('Неизвестный ИИ: ' + str(provider))


def ask_command(provider, exe, model, question):
    """Второе мнение для Решалы: другой ИИ отвечает только чтением, ничего не меняя."""
    if provider == 'claude':
        return [exe, '-p', '--permission-mode', 'plan', '--model', model, '--effort', 'high', question]
    if provider == 'codex':
        return [exe, 'exec', '-m', model, '-c', 'model_reasoning_effort="high"', '-s', 'read-only', '--skip-git-repo-check', question]
    raise ValueError('Неизвестный ИИ: ' + str(provider))


def ask(provider, question, cwd=None, model=None, timeout=900):
    exe = find_cli(provider)
    if not exe:
        raise ValueError(status()[provider]['note'])
    if not model:
        import workflow
        model = workflow.LINEUP[provider][2]
    r = subprocess.run(ask_command(provider, exe, model, question), cwd=cwd or os.getcwd(), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=timeout, stdin=subprocess.DEVNULL)
    return (r.stdout or '').strip() or (r.stderr or '').strip()[-2000:]


def open_background(cmd, cwd, log_path, env_extra=None):
    """Процесс без окна, вывод — в журнал. Ctrl+C в окне сервера его не останавливает. Возвращает pid."""
    log_path = Path(log_path); log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **(env_extra or {}))
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    with open(log_path, 'ab') as log:
        log.write(('\n===== %s\n' % ' '.join(str(c) for c in cmd[:-1])).encode('utf-8', 'replace'))
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=flags, start_new_session=os.name != 'nt')
    return proc.pid


def alive(pid):
    """Жив ли процесс сессии. Неизвестно — считаем живым: окно не освобождается раньше времени."""
    if not pid:
        return False
    if os.name != 'nt':
        try:
            os.kill(int(pid), 0); return True
        except OSError:
            return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code)); k.CloseHandle(h)
        return bool(ok) and code.value == 259             # STILL_ACTIVE
    except Exception:   # noqa: BLE001
        return True


def tail(path, lines=200):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 200_000))
            text = f.read().decode('utf-8', 'replace')
    except OSError:
        return ''
    return '\n'.join(text.splitlines()[-lines:])


def command(provider, exe, model, effort, prompt, cwd, edits=False, session=None, resume=False, extra=()):
    """edits=True — сессия роли конвейера: правка файлов в рабочей копии задачи без вопросов
    (Claude: acceptEdits; Codex: песочница workspace-write), команды — по подтверждению.

    session — идентификатор сессии: Claude получает его при открытии (--session-id), чтобы
    сервер нашёл журнал сессии; resume=True продолжает эту сессию после паузы по лимиту
    (claude --resume, codex resume) — с местом остановки и всей перепиской."""
    if provider == 'claude':
        head = ['--resume', session] if resume else (['--session-id', session] if session else [])
        return [exe, *head, *extra, '--model', model, '--effort', effort, *(['--permission-mode', 'acceptEdits'] if edits else []), prompt]
    if provider == 'codex':
        return [exe, *(['resume', session] if resume else []), '-m', model, '-c', 'model_reasoning_effort="%s"' % effort, *extra, '-C', cwd,
                *(['-s', 'workspace-write', '-a', 'on-request'] if edits else []), prompt]
    raise ValueError('Неизвестный ИИ: ' + str(provider))


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')


def ensure_task_worktree(repo, task_id, base=None):
    """Рабочая копия задачи конвейера: одна ветка task/<задача> для всех ролей — от идеи
    до деплоя артефакты и код идут вместе."""
    return ensure_worktree(repo, None, task_id, base)


def ensure_worktree(repo, agent, task_id, base=None):
    """Отдельная рабочая копия задачи: ветка ai/<ИИ>/<задача> от базовой ветки.

    Лежит рядом с репозиторием, в <repo>.worktrees/<ИИ>-<задача>, чтобы не попадать в git
    самого проекта. Повторный вызов возвращает ту же копию. Не git — None: работа идёт в папке."""
    repo = Path(repo)
    if not (repo / '.git').exists():
        return None
    slug = re.sub(r'[^a-z0-9.-]+', '-', task_id.lower()).strip('-')
    branch = 'task/%s' % slug if agent is None else 'ai/%s/%s' % (agent, slug)
    path = repo.parent / (repo.name + '.worktrees') / ('task-%s' % slug if agent is None else '%s-%s' % (agent, slug))
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


def launch(provider, model, effort, prompt, cwd, env_extra=None, edits=False, resume=None, headless=False, run_id=None, extra=()):
    """Открыть сессию. resume — идентификатор сессии, которую продолжить (пауза по лимиту, перезапуск).
    headless — полная автономия: без окна, с журналом. Новой сессии Claude сервер сам назначает
    идентификатор; Codex назначает свой — его находит limits.codex_session по папке и времени запуска."""
    exe = find_cli(provider)
    if not exe:
        raise ValueError(status()[provider]['note'])
    if not cwd or not Path(cwd).is_dir():
        raise ValueError('Рабочая папка проекта не найдена: %s' % (cwd or 'не указана'))
    session = resume or (str(uuid.uuid4()) if provider == 'claude' else None)
    if headless:
        cmd = headless_command(provider, exe, model, effort, prompt, cwd, session=session, resume=bool(resume), extra=extra)
        log = LOGS / ('%s.log' % (run_id or session or 'session'))
        pid = open_background(cmd, cwd, log, env_extra)
        return dict(pid=pid, exe=exe, cwd=cwd, session=session, resumed=bool(resume), headless=True, log=str(log), lean=bool(extra))
    cmd = command(provider, exe, model, effort, prompt, cwd, edits, session=session, resume=bool(resume), extra=extra)
    return dict(pid=open_window(cmd, cwd, env_extra), exe=exe, cwd=cwd, session=session, resumed=bool(resume), headless=False,
                lean=bool(extra))


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
