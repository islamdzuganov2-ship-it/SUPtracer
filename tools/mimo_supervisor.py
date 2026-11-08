"""Надзор за локальной моделью (07: A9-05, A9-06, A9-07, A3-02, AG-LDEV-08, AG-ARCH-09, AG-DEV-06).

- Ресурсы: перед загрузкой весов — свободная VRAM против размера GGUF; одна загрузка за раз (файл блокировки GPU).
  Не хватает — безопасный отказ (пауза запуска), проектные данные не трогаются.
- Рантайм: если health не отвечает, а у конфигурации есть runtime_path и weight_path — сервер сам поднимает
  llama-server на loopback и ждёт health.
- Закрепление сессии: запуск работает на той конфигурации, с которой начал, даже если владелец сменил активную.
- WIP-коммит после каждой правки и при сбое: оборванная работа не теряется, повтор начинается с рабочей копии.
- Пригодность к задаче: CRITICAL получает только конфигурация с пройденным бенчмарком допуска; иначе роль уходит
  внешнему ИИ с тем же паспортом, решение видно в журнале.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import learning_events as le

LOCK_NAME = 'gpu.lock'
LOCK_TTL = 6 * 3600             # блокировка старше — брошена упавшим процессом
OVERHEAD_MB = 700               # KV-кеш и буферы рантайма сверх весов при контексте 8К
SPAWN_WAIT = 90


def _lock_path():
    return le.root() / LOCK_NAME


def _alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % int(pid), '/NH'], capture_output=True, text=True)
        return str(pid) in (r.stdout or '')
    try:
        os.kill(int(pid), 0); return True
    except OSError:
        return False


def acquire(run_id, pid=None, now=None):
    """Взять GPU. None — взято; иначе запись держателя."""
    p = _lock_path(); now = now or time.time(); pid = pid or os.getpid()
    if p.exists():
        try:
            cur = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cur = {}
        if cur.get('run') != run_id and now - float(cur.get('ts') or 0) < LOCK_TTL and _alive(cur.get('pid')):
            return cur
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(run=run_id, pid=pid, ts=now, at=datetime.now(timezone.utc).isoformat())), encoding='utf-8')
    return None


def release(run_id):
    p = _lock_path()
    try:
        if json.loads(p.read_text(encoding='utf-8')).get('run') == run_id:
            p.unlink()
    except (OSError, ValueError):
        pass


def need_mb(cfg):
    w = cfg.get('weight_path')
    try:
        size = os.path.getsize(w) / 1048576 if w else 0
    except OSError:
        size = 0
    return int(size + OVERHEAD_MB) if size else None


def free_mb():
    import hardware_probe as hp
    gpus, _, _ = hp.gpu()
    vals = [g['vram_free_mb'] for g in gpus if g.get('vram_free_mb') is not None]
    return max(vals) if vals else None


def resources(cfg, free_fn=free_mb):
    """(ok, причина). Без GPU-данных отказ: обещать загрузку вслепую нельзя."""
    need = need_mb(cfg)
    if not need:
        return False, 'неизвестен размер весов (weight_path) — загрузка без оценки памяти запрещена'
    free = free_fn()
    if free is None:
        return False, 'нет данных о свободной VRAM (nvidia-smi) — загрузка весов %d МБ вслепую запрещена' % need
    if free < need:
        return False, 'resources: свободно VRAM %d МБ, весам с контекстом нужно ~%d МБ — запуск на паузе, данные не тронуты' % (free, need)
    return True, 'VRAM: свободно %d МБ, нужно ~%d МБ' % (free, need)


def spawn_cmd(cfg):
    u = urlparse(cfg['endpoint'])
    return [cfg['runtime_path'], '-m', cfg['weight_path'], *(['--lora', cfg['adapter_path']] if cfg.get('adapter_path') else []),
            '--host', '127.0.0.1', '--port', str(u.port or 8089),
            '-c', str(cfg.get('context_limit') or 8192), '-ngl', '99', '--jinja']


def ensure_runtime(cfg, health_fn, free_fn=free_mb, popen=subprocess.Popen, wait=SPAWN_WAIT, sleep=time.sleep):
    """Поднять рантайм, если он не отвечает и конфигурация знает, как его запустить (AG-ARCH-09)."""
    h = health_fn(cfg)
    if h.get('ok'):
        return h
    if not cfg.get('runtime_path') or not cfg.get('weight_path'):
        return dict(ok=False, reason='%s; запуск рантайма не настроен (runtime_path, weight_path)' % h.get('reason'))
    if cfg.get('adapter_path') or cfg.get('adapter_sha256'):
        import local_mimo as lm
        bad = lm.adapter_problem(cfg)                  # несовместимый LoRA не грузится вовсе (A10-04)
        if bad:
            return dict(ok=False, reason=bad, adapter=True)
    ok, why = resources(cfg, free_fn)
    if not ok:
        return dict(ok=False, reason=why, resources=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    popen(spawn_cmd(cfg), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    t0 = time.time()
    while time.time() - t0 < wait:
        sleep(2)
        h = health_fn(cfg)
        if h.get('ok'):
            return dict(h, spawned=True)
    return dict(ok=False, reason='рантайм запущен, но health не ответил за %d с' % wait)


def critical(pipe):
    import adaptive_pipeline as ap
    r = ap.route(pipe)
    return r.get('applied') == 'CRITICAL' or (r.get('plan') or {}).get('mode') == 'CRITICAL'


def capable(cfg, pipe):
    """(ok, причина): годится ли конфигурация для этой задачи. Имя модели — не аттестация (A3-02)."""
    if not critical(pipe):
        return True, 'задача не CRITICAL'
    if not cfg:
        return False, 'CRITICAL: активной конфигурации local_mimo нет'
    import learn_bench
    ok, why = learn_bench.qualified(cfg)
    return (True, 'CRITICAL: конфигурация прошла бенчмарк допуска') if ok else \
        (False, 'CRITICAL требует аттестованной модели: %s' % why)


def pinned(run, registry):
    """Конфигурация, закреплённая за запуском (A9-06): смена активной не меняет модель посреди работы."""
    cid = ((run or {}).get('learning') or {}).get('model_config_id')
    return registry['configs'].get(cid) if cid else None


def wip_commit(root, label):
    """WIP-коммит рабочей копии (DEV-06): после каждой правки и при сбое."""
    subprocess.run(['git', 'add', '-A'], cwd=root, capture_output=True)
    r = subprocess.run(['git', '-c', 'user.name=local_mimo', '-c', 'user.email=local_mimo@localhost', 'commit', '-q', '-m', label[:200]],
                       cwd=root, capture_output=True)
    return r.returncode == 0
