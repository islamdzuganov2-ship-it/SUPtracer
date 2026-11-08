"""Пассивные признаки активности: только процессы, метаданные журналов и heartbeat.

Не вызывает ИИ, не читает содержимое журналов, не меняет состояние задач.
"""
import os
from datetime import datetime, timezone

import limits

FRESH_SECONDS = 180


def process_alive(pid):
    """True/False/None: отсутствие доступа не равно завершению процесса."""
    if not pid:
        return None
    try:
        if os.name != 'nt':
            try:
                os.kill(int(pid), 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return None
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError, TypeError):
        return None


def log_signal(run):
    """Только точная сессия; подбор по cwd мог бы приписать агенту чужую активность."""
    launch = run.get('launch') or {}
    paths = [launch.get('log')]
    if launch.get('session'):
        try:
            paths.append(limits.session_of(run)[1])
        except (OSError, ValueError):
            pass
    latest = None
    for path in paths:
        if not path:
            continue
        try:
            stat = os.stat(path)
            if stat.st_size > 0:
                latest = max(latest or 0, stat.st_mtime)
        except OSError:
            continue
    return latest


def snapshot(runs, tasks, at=None, settings=None):
    at = at or datetime.now(timezone.utc)
    stamp = at.timestamp()
    task_map = {t['ref']: t for t in tasks}
    rows, queued = [], {}
    for run in runs:
        if run.get('status') not in ('queued', 'running'):
            continue
        task = task_map.get(run['ref'], {})
        project = task.get('project') or run['ref'].split('/')[0]
        launch = run.get('launch') or {}
        if run['status'] == 'queued' and not run.get('launched_at') and not run.get('paused') and not run.get('launch_error'):
            queued[project] = queued.get(project, 0) + 1
            continue
        pid = launch.get('pid')
        alive = process_alive(pid)
        log_at = log_signal(run)
        heartbeat = limits.stamp(run.get('heartbeat_at'))
        signals = {k: v for k, v in dict(log=log_at, heartbeat=heartbeat.timestamp() if heartbeat else None).items()
                   if v is not None and v <= stamp}
        latest = max(signals.values(), default=None)
        age = max(0, int(stamp - latest)) if latest is not None else None
        paused = run.get('paused') or {}
        if paused:
            state, label = 'paused', 'Пауза: ' + ('лимит ИИ' if paused.get('reason') == 'limit' else 'смена сессии' if paused.get('reason') == 'context' else 'ожидание')
        elif alive is False:
            state, label = 'exited', 'Процесс завершён; роль ещё не сдана'
        elif age is not None and age <= FRESH_SECONDS:
            state, label = 'recent', 'Есть свежие сигналы'
        elif alive:
            state, label = 'quiet', 'Процесс жив; свежих сигналов нет'
        else:
            state, label = 'unknown', 'Активность не подтверждена'
        rows.append(dict(run=run['id'], ref=run['ref'], project=project, title=task.get('title', ''),
                         agent=run.get('agent'), model=run.get('model') or (run.get('packet') or {}).get('route', {}).get('model'),
                         role=run.get('role'), stage=run.get('stage'), state=state, label=label,
                         pid=pid, process_alive=alive, signal_age_seconds=age,
                         log_at=datetime.fromtimestamp(log_at, timezone.utc).isoformat() if log_at and log_at <= stamp else None,
                         heartbeat_at=run.get('heartbeat_at'), resume_at=paused.get('resume_at'),
                         error=str(run.get('launch_error') or '')[:300]))
    order = {'recent': 0, 'quiet': 1, 'paused': 2, 'exited': 3, 'unknown': 4}
    rows.sort(key=lambda r: (order[r['state']], r['signal_age_seconds'] if r['signal_age_seconds'] is not None else float('inf'), r['ref']))
    conf = settings or {}
    capacity = {agent: min(conf.get('max_sessions', 4), (conf.get('sessions_per_ai') or {}).get(agent, 2))
                for agent in ('codex', 'claude')}
    by_run = {r['run']: r for r in rows}
    loads = {}
    for run in runs:
        if run.get('status') not in ('queued', 'running'):
            continue
        agent = run.get('agent') or (run.get('launch') or {}).get('agent')
        if not agent:
            continue
        model = run.get('model') or ((run.get('packet') or {}).get('route') or {}).get('model') or 'Модель не назначена'
        project = task_map.get(run['ref'], {}).get('project') or run['ref'].split('/')[0]
        group = loads.setdefault((project, agent, model), dict(project=project, agent=agent, model=model,
                                                             busy=0, signals=0, queued=0, paused=0, tasks=set()))
        group['tasks'].add(run['ref'])
        row = by_run.get(run['id']) or {}
        group['busy'] += int(row.get('process_alive') is True and not run.get('paused'))
        group['signals'] += int(row.get('state') == 'recent')
        group['paused'] += int(bool(run.get('paused')))
        group['queued'] += int(run['status'] == 'queued' and not run.get('launched_at') and not run.get('paused'))
    workloads = [dict(g, tasks=len(g['tasks'])) for g in loads.values()]
    return dict(observed_at=at.isoformat(), fresh_seconds=FRESH_SECONDS, rows=rows, queued=queued,
                capacities=capacity, max_sessions=conf.get('max_sessions', 4), workloads=workloads)
