# -*- coding: utf-8 -*-
"""Локальный сервер дашборда — чтобы кнопка «Обновить данные» действительно работала.

Дашборд открывается и двойным кликом по файлу, но тогда кнопка обновления может
только показать команду: страница на file:// не имеет права ничего запускать.
Под этим сервером она запускает сбор по-настоящему.

Сервер намеренно тесный:
  · слушает только 127.0.0.1 — снаружи машины не виден;
  · отдаёт ровно один файл, dashboard.html, и ничего больше из каталога;
  · обновление только методом POST и только со своим заголовком — чтобы чужая
    вкладка в браузере не могла запустить сбор по ссылке.

  py tools/serve.py [--port 8787] [--no-browser]
"""
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")
DASH = os.path.join(ROOT, "dashboard.html")
PY = sys.executable

MODES = {
    "fast": (["refresh", "--fast"], "быстрое обновление: токены, оценки, дерево, дашборд"),
    "full": (["refresh"], "полное обновление: с пересканированием репозиториев"),
    "vault": (["vault", "sync"], "обмен с Obsidian: забрать заметки и выложить портфель"),
    # ОБН-04: проект передаётся отдельным полем и сверяется со списком проектов —
    # в командную строку попадает только известный ключ.
    "project": (["refresh", "--project"], "обновление одного проекта: только его репозиторий"),
}


def project_keys():
    try:
        with open(os.path.join(DATA, "projects.json"), encoding="utf-8") as f:
            return set(json.load(f).get("projects", {}))
    except Exception:
        return set()

_lock = threading.Lock()
STARTED = time.time()          # код Python новее — сервер нужно перезапустить
TOOLS_DIR = os.path.join(ROOT, "tools")
_auto_attempts = {}            # запуск -> время последней попытки автозапуска
_last = {"at": 0, "mode": "", "ok": None, "log": "", "seconds": 0}
SOURCES = ("all", "claude", "codex")
MAX_BODY = 1024 * 1024


def integration_status():
    import connect
    return {"claude": {"connected": connect.mcp_installed(), "label": "Claude Code"},
            "codex": {"connected": connect.codex_mcp_installed(), "label": "GPT / Codex"}}


def run_pm(args, timeout=900):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    t0 = time.time()
    try:
        r = subprocess.run([PY, os.path.join(ROOT, "tools", "pm.py")] + args,
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode == 0, out.strip(), round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        return False, "Обновление не уложилось в %d с и было прервано." % timeout, timeout
    except Exception as e:
        return False, "Не удалось запустить обновление: %r" % e, round(time.time() - t0, 1)


def _newest(names):
    times = [os.path.getmtime(os.path.join(TOOLS_DIR, n)) for n in os.listdir(TOOLS_DIR) if names(n)]
    return max(times, default=0)


def code_stale():
    """Python-модули менялись после старта сервера: нужен перезапуск, иначе работает старое."""
    return _newest(lambda n: n.endswith(".py")) > STARTED


def dashboard_stale():
    """Интерфейс собирается в dashboard.html: новые js/html в tools — пересобрать."""
    if not os.path.exists(DASH):
        return True
    return _newest(lambda n: n.endswith((".js", ".html")) or n == "build_dashboard.py") > os.path.getmtime(DASH)


def dispatch_once():
    """Автозапуск: ревью после пройденной автопроверки открывается само, если включено."""
    import workflow, orchestra
    conf = workflow.settings()
    started = []
    s = workflow.state(); runs = s["runs"]
    busy = workflow.open_sessions(runs)
    per_ai = {a: workflow.open_sessions(runs, agent=a) for a in workflow.PROVIDERS}
    tasks = {t["ref"]: t for t in workflow.snapshot()["tasks"]}
    prio = {ref: workflow.PRIORITY_ORDER.get(t.get("priority"), 1) for ref, t in tasks.items()}
    for r in sorted(runs, key=lambda r: orchestra.dispatch_order(r, tasks, prio)):
        if r.get("status") != "queued" or not r.get("auto_launch") or r.get("launched_at"):
            continue
        # Лимит одновременных сессий; Решала разбирает блокеры и получает одно место сверх него (А5).
        if busy >= conf.get("max_sessions", 4) + (1 if r.get("role") == "resolver" else 0):
            continue
        if per_ai.get(r.get("agent"), 0) >= (conf.get("sessions_per_ai") or {}).get(r.get("agent"), 99):
            continue              # у этого ИИ свой лимит сессий (А7): запуски другого ИИ идут дальше
        if orchestra.hold(s, r, tasks, conf):
            continue              # порядок анализа, лимит незавершённого, группа контрактов
        # Роли конвейера — по auto_pipeline, ревью старого потока — по auto_launch_review; передачи по лимиту — всегда.
        if not (conf["auto_pipeline"] if r.get("role") else (conf["auto_launch_review"] or r.get("failover"))):
            continue
        if workflow.limited(None, r.get("agent")):
            if not workflow.reroute_queued(r["ref"], r["id"]):
                continue               # все ИИ на лимите: откроется после сброса
            continue                   # ИИ запуска сменён — откроется в следующем проходе
        if time.time() - _auto_attempts.get(r["id"], 0) < 300:
            continue
        _auto_attempts[r["id"]] = time.time()
        try:
            launch_session(dict(ref=r["ref"], agent=r.get("agent"), run=r["id"]))
            started.append(r["id"]); busy += 1; per_ai[r.get("agent")] = per_ai.get(r.get("agent"), 0) + 1
            r["launched_at"] = workflow.now().isoformat()     # группа контрактов видит открытый запуск в этом же проходе
        except Exception as e:   # noqa: BLE001 — причина видна на карточке
            workflow.mark_launch_error(r["ref"], r["id"], e)
    return started


def limits_once():
    """Лимит токенов: найти отказ в журналах открытых сессий — пауза; срок паузы вышел —
    продолжить ту же сессию с места остановки."""
    import workflow, limits
    paused, resumed, handed = [], [], []
    for r in workflow.state()["runs"]:
        if r.get("status") not in ("queued", "running") or not r.get("launch") or r.get("paused"):
            continue
        if (r.get("launch") or {}).get("agent") == "codex" and not r["launch"].get("session"):
            sid, _ = limits.session_of(r)
            if sid:
                workflow.note_session(r["ref"], r["id"], sid)
        event = limits.detect(r)
        if event and workflow.mark_paused(r["ref"], r["id"], event):
            paused.append(r["id"])
            # Есть свободный ИИ — задача переходит к нему сразу, а не ждёт сброса лимита.
            moved = workflow.failover_run(r["ref"], r["id"])
            if moved:
                handed.append(dict(moved, from_run=r["id"]))
    for r in workflow.resume_due():
        if time.time() - _auto_attempts.get("resume:" + r["id"], 0) < 300:
            continue
        _auto_attempts["resume:" + r["id"]] = time.time()
        try:
            resume_session(r)
            resumed.append(r["id"])
        except Exception as e:   # noqa: BLE001 — причина видна на карточке, повтор через 5 минут
            workflow.mark_launch_error(r["ref"], r["id"], "Возобновление после лимита: %s" % e)
    return dict(paused=paused, resumed=resumed, handed=handed)


def resume_session(run, nudge=False):
    """Продолжить сессию: после паузы по лимиту или (nudge) после ответа без сдачи этапа.
    Та же сессия (claude --resume, codex resume); без её идентификатора — новая с точкой остановки."""
    import workflow, launcher, limits
    t = workflow.task_for(run["ref"])
    launch = run.get("launch") or {}
    agent = launch.get("agent") or run.get("agent") or workflow.settings()["default_executor"]
    session = (run.get("paused") or {}).get("session") or launch.get("session")
    if not session and agent == "codex":
        session = limits.session_of(run)[0]
    route = (run.get("packet") or {}).get("route") or {}
    if run.get("role"):
        import orchestra
        route = orchestra.role_route(t, run["role"], agent)
    if not route.get("model"):
        route = workflow.recommendation(t, agent=agent)
    prompt = NUDGE if nudge else workflow.resume_prompt(t, run)
    if run.get("budget_alert"):
        prompt = BUDGET_NOTE + "\n\n" + prompt
    if not session:
        prompt += "\n\n" + (orchestra.role_prompt(t, run, route, run.get("worker") or workflow.worker_id(run, agent))
                             if run.get("role") else workflow.action(dict(op="prompt", ref=run["ref"]))["prompt"]
                             if run["status"] == "queued" else "")
    cwd = launch.get("cwd")
    opened = open_session(agent, route, prompt.strip(), cwd, run, edits=bool(run.get("role")), resume=session)
    info = launch_info(opened, cwd, agent)
    if nudge:
        return workflow.mark_nudged(run["ref"], run["id"], info, "ответ без сдачи этапа")
    return workflow.mark_resumed(run["ref"], run["id"], info)


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def busy_work():
    """Есть ли работа, ради которой компьютеру нельзя засыпать: открытые сессии или запуски к открытию."""
    import workflow
    s = workflow.state(); conf = workflow.settings(s)
    if workflow.open_sessions(s["runs"]):
        return True
    return conf.get("auto_pipeline", True) and any(
        r.get("status") == "queued" and r.get("auto_launch") and not r.get("launched_at") for r in s["runs"])


def keep_awake(on):
    """Windows: не засыпать, пока идёт работа (А4). Экран может гаснуть — держится только система.
    Флаг действует для потока диспетчера, поэтому вызывается из него на каждом проходе."""
    if os.name != "nt":
        return False
    import ctypes
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))


def dispatcher(interval=15):
    while True:
        time.sleep(interval)
        try:
            keep_awake(busy_work())   # 35 часов простоя за неделю — из-за сна компьютера при живой очереди
        except Exception as e:   # noqa: BLE001
            sys.stderr.write("бодрствование: %s\n" % e)
        try:
            import orchestra
            orchestra.tick()          # задачи, ждавшие DoR (например, одобрения владельца)
            limits_once()             # пауза по лимиту токенов и возобновление с места остановки
            watch_once()              # сессия закончилась без сдачи этапа — продолжить или эскалировать
            budget_once()             # запуск потратил 150% ориентира — отметка и колокольчик
            notify_once()             # новое в колокольчике — системное уведомление владельцу
            dispatch_once()
        except Exception as e:   # noqa: BLE001 — сервер досок не должен падать из-за автозапуска
            sys.stderr.write("автозапуск: %s\n" % e)


def open_session(agent, route, prompt, cwd, run, edits=False, resume=None):
    """Сессия ИИ в режиме согласия владельца: пошаговое — окно, полная автономия — фон без окна."""
    import workflow, launcher
    return launcher.launch(agent, route["model"], route["effort"], prompt, cwd, edits=edits, resume=resume,
                           headless=workflow.autonomous(), run_id=run["id"],
                           env_extra={"TASKTREKER_DATA": workflow.data_dir().as_posix(),
                                      "TASKTREKER_SYSTEM": os.path.dirname(os.path.abspath(workflow.__file__))})


def launch_info(opened, cwd, agent, **extra):
    return dict(extra, pid=opened["pid"], cli=os.path.basename(opened["exe"]), session=opened.get("session"),
                cwd=cwd, agent=agent, headless=opened.get("headless", False), log=opened.get("log"),
                **({"resumed": True} if opened.get("resumed") else {}))


def session_active(run, seconds=180):
    """Сессия ещё пишет журнал: codex.exe может завершиться раньше своей сессии, и продолжение
    той же сессии упадёт с «already has an active writer». Журнал свежее 3 минут — сессия жива."""
    import limits
    try:
        _sid, path = limits.session_of(run)
        return bool(path) and time.time() - os.path.getmtime(path) < seconds
    except (OSError, ValueError):
        return False


BUDGET_NOTE = ("Запуск уже потратил больше 150% ориентира бюджета роли. Не начинай новых исследований и не перечитывай "
               "документы: доведи до handoff то, что есть, или эскалируй (op=escalate), если упёрся.")
NUDGE = ("Ты закончил ответ, но этап не сдан: сервер не получил ни handoff, ни return, ни escalate, ни pause. "
         "Владелец не ответит — работа автономная. Продолжи с места остановки: tt_work op=packet (с run), доделай "
         "работу по DoD и сдай её вызовом tt_work. Если без решения владельца дальше нельзя — op=escalate с вариантами.")
MAX_NUDGES = 2


def toast(title, text):
    """Системное уведомление Windows — отдельный сигнал владельцу, даже если страница закрыта."""
    if os.name != "nt":
        return False
    script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;"
              "$n=New-Object System.Windows.Forms.NotifyIcon; $n.Icon=[System.Drawing.SystemIcons]::Information;"
              "$n.BalloonTipTitle=$env:TT_TITLE; $n.BalloonTipText=$env:TT_TEXT; $n.Visible=$true;"
              "$n.ShowBalloonTip(15000); Start-Sleep -Seconds 16; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                         env=dict(os.environ, TT_TITLE=title[:60], TT_TEXT=text[:240]),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError:
        return False


def budget_once():
    """Сторож бюджета (Б7): открытые сессии раз в 5 минут сверяются с ориентиром стоимости запуска."""
    import workflow, limits, orchestra
    alerted = []
    for r in workflow.state()["runs"]:
        if r.get("status") not in ("queued", "running") or not r.get("launch") or r.get("budget_alert"):
            continue
        if time.time() - _auto_attempts.get("budget:" + r["id"], 0) < 300:
            continue
        _auto_attempts["budget:" + r["id"]] = time.time()
        budget = ((r.get("packet") or {}).get("route") or {}).get("budget_usd")
        if not budget:
            continue
        _tok, usd = limits.session_cost(r)
        if usd >= budget * orchestra.BUDGET_ALERT and workflow.mark_budget(r["ref"], r["id"], usd, budget):
            alerted.append(r["id"])
    return alerted


def notify_once():
    """Новое в колокольчике — системное уведомление (одно на пункт)."""
    import workflow
    s = workflow.state()
    fresh = [n for n in workflow.notifications(s, workflow.snapshot()["tasks"]) if n["id"] not in (s.get("notified") or [])]
    if not fresh:
        return []
    kinds = {"question": "вопрос Решалы", "release": "релиз в main", "launch": "сбой запуска"}
    head = fresh[0]
    toast("TaskTreker: нужен владелец" + (" (%d)" % len(fresh) if len(fresh) > 1 else ""),
          "%s · %s: %s" % (kinds.get(head["kind"], head["kind"]), head["ref"], head.get("text") or head.get("title") or ""))
    workflow.mark_notified([n["id"] for n in fresh])
    return [n["id"] for n in fresh]


def watch_once():
    """Сессии, которые закончились без сдачи этапа (процесс завершён, запуск не закрыт).

    Полная автономия: та же сессия продолжается с напоминанием (до двух раз), дальше — эскалация
    тимлиду. Пошаговое согласие: окно закрыл владелец — только отметка на карточке."""
    import workflow, launcher
    nudged, lost = [], []
    full = workflow.autonomous()
    for r in workflow.state()["runs"]:
        launch = r.get("launch") or {}
        if r.get("status") not in ("queued", "running") or r.get("paused") or not launch.get("pid"):
            continue
        last = workflow.parse_time(r.get("last_nudge_at") or (launch.get("at") or r.get("launched_at")))
        if last and (workflow.now() - last).total_seconds() < 60:
            continue                   # процесс только что открыт
        if launcher.alive(launch["pid"]) or session_active(r):
            continue
        if not full:
            if not r.get("launch_error"):
                workflow.mark_launch_error(r["ref"], r["id"], "Окно сессии закрыто, этап не сдан — откройте сессию снова")
            continue
        if r.get("nudges", 0) >= MAX_NUDGES:
            workflow.session_lost(r["ref"], r["id"], "сессия %d раза заканчивалась без сдачи этапа" % (MAX_NUDGES + 1))
            lost.append(r["id"]); continue
        try:
            resume_session(r, nudge=True)
            nudged.append(r["id"])
        except Exception as e:   # noqa: BLE001
            workflow.mark_launch_error(r["ref"], r["id"], "Продолжение сессии: %s" % e)
    return dict(nudged=nudged, lost=lost)


def launch_role(t, run, req):
    """Сессия роли конвейера: своя модель роли, рабочая копия задачи, скилл роли, право правки файлов."""
    import workflow, launcher, orchestra
    agent = req.get("agent") or run.get("agent") or workflow.settings()["default_executor"]
    if run.get("agent") and agent != run["agent"]:
        raise ValueError("Роль закреплена за ИИ %s: сменить можно в настройках ролей" % run["agent"])
    wt = (t.get("pipeline") or {}).get("worktree") or {}
    if not wt:
        with workflow.locked():
            s = workflow.state(); t = workflow.task_for(t["ref"])
            pipe = orchestra.materialize(s, t)
            if pipe:
                workflow.write_patches(s, {t["ref"]: dict(pipeline=pipe)}); workflow.save("workflow.json", s)
                t = dict(t, pipeline=pipe)
        wt = (t.get("pipeline") or {}).get("worktree") or {}
    project = workflow.read("projects.json", {})["projects"][t["project"]]
    cwd = wt.get("path") or project.get("inspection_path") or project.get("path")
    route = orchestra.role_route(t, run["role"], agent)
    worker = workflow.worker_id(run, agent)
    prompt = orchestra.role_prompt(t, run, route, worker)
    opened = open_session(agent, route, prompt, cwd, run, edits=True)
    workflow.mark_launched(t["ref"], run["id"], launch_info(opened, cwd, agent, worktree=wt or None, role=run["role"]))
    return dict(run=run["id"], agent=agent, pid=opened["pid"], cwd=cwd, worktree=wt or None, role=run["role"],
                route=route, worker=worker, prompt=prompt)


def launch_session(req):
    """«Запустить» на доске: запуск в очереди, рабочая копия, окно сессии ИИ, отметка.

    Окно открывается только здесь — по нажатию владельца на локальном сервере. MCP-инструмент
    такой операции не имеет: ИИ не открывает сессии других ИИ."""
    import workflow, launcher
    ref = str(req.get("ref") or "")
    t = workflow.task_for(ref)
    run = next((r for r in workflow.state()["runs"] if r["ref"] == ref and r["status"] == "queued"
                and (not req.get("run") or r["id"] == req["run"])), None)
    if run and run.get("role"):
        return launch_role(t, run, req)
    # ИИ: явный выбор владельца, затем закрепление запуска, исполнитель задачи, ИИ по умолчанию.
    agent = req.get("agent") or (run or {}).get("agent") or t.get("executor") or workflow.settings()["default_executor"]
    workflow.check_executor(agent)
    if run and run.get("agent") and run["agent"] != agent:
        workflow.action(dict(op="repin", ref=ref, run=run["id"], agent=agent))
        run = next(r for r in workflow.state()["runs"] if r["id"] == run["id"])
    if not run:
        workflow.action(dict(op="enqueue", ref=ref, agent=agent))
        run = next(r for r in workflow.state()["runs"] if r["ref"] == ref and r["status"] == "queued")
    project = workflow.read("projects.json", {})["projects"][t["project"]]
    ws = workflow.workspace(t["project"], project)
    base = ws["active_copy"] or ws["path"]
    if run.get("phase") == "review" or run.get("failover"):
        # Ревьюер — и ИИ, принявший задачу по лимиту токенов, — работают в ветке исполнителя.
        worktree = workflow.task_branch(workflow.state(), ref)
        if worktree is None and run.get("failover") and base and req.get("isolate", True):
            worktree = launcher.ensure_worktree(base, agent, t["id"])
    else:
        worktree = launcher.ensure_worktree(base, agent, t["id"]) if req.get("isolate", True) and base else None
    cwd = (worktree or {}).get("path") or base
    info = workflow.action(dict(op="prompt", ref=ref, worker=workflow.worker_id(run, agent), worktree=worktree))
    # Данные системы — общие: сессия в рабочей копии самой системы не должна видеть пустой data/.
    opened = open_session(agent, info["route"], info["prompt"], cwd, run)
    workflow.mark_launched(ref, run["id"], launch_info(opened, cwd, agent, worktree=worktree))
    return dict(run=run["id"], agent=agent, pid=opened["pid"], cwd=cwd, worktree=worktree,
                route=info["route"], worker=info["worker"], prompt=info["prompt"])


def start_project(req):
    """«▶ Запустить проект»: пакет готовых задач бэклога ставится в очередь, и по каждой
    открывается сессия выбранного ИИ; заодно открываются неоткрытые роли конвейера проекта.
    Сбой одного окна не останавливает остальные — причина пишется на карточку."""
    import workflow
    plan = workflow.action(dict(op="start", project=req.get("project"), agents=req.get("agents"),
                                sessions=req.get("sessions"), dry_run=False))
    opened, failed = [], []
    jobs = [dict(ref=p["ref"], agent=p["agent"], run=p["run"]) for p in plan["picks"]]
    for job in jobs:
        try:
            launch_session(job); opened.append(job["run"])
        except Exception as e:   # noqa: BLE001 — остальные сессии всё равно открываются
            workflow.mark_launch_error(job["ref"], job["run"], e)
            failed.append(dict(job, error=str(e)[:300]))
    return dict(plan, opened=opened, failed=failed)


class Handler(BaseHTTPRequestHandler):
    server_version = "TaskTreker"

    def log_message(self, fmt, *a):          # тише: журнал сервера здесь не нужен
        pass

    # ------------------------------------------------------------- проверки

    def local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        if host not in ("localhost", "127.0.0.1", "::1", ""):
            self.send_json(403, {"ok": False, "error": "только локальные запросы"})
            return False
        return True

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- маршруты

    def do_GET(self):
        if not self.local_only():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/dashboard.html"):
            return self.send_dashboard()
        if path == "/api/ping":
            return self.send_json(200, {"ok": True, "served": True,
                                        "dashboard": os.path.exists(DASH),
                                        "last": _last})
        if path == "/api/integrations":
            try:
                return self.send_json(200, {"ok": True, "providers": integration_status()})
            except Exception:
                return self.send_json(500, {"ok": False, "error": "Не удалось прочитать настройки подключения."})
        if path == "/api/sessionlog":
            import workflow, launcher
            from urllib.parse import parse_qs, urlparse
            rid = (parse_qs(urlparse(self.path).query).get("run") or [""])[0]
            run = next((r for r in workflow.state()["runs"] if r["id"] == rid), None)
            log = ((run or {}).get("launch") or {}).get("log")
            # Только журналы сессий системы: путь из данных, но внутри ops/logs/sessions.
            if not log or os.path.dirname(os.path.abspath(log)) != os.path.abspath(str(launcher.LOGS)):
                return self.send_json(404, {"ok": False, "error": "у запуска нет журнала фоновой сессии"})
            return self.send_json(200, {"ok": True, "data": dict(run=rid, alive=launcher.alive(run["launch"].get("pid")),
                                                                 log=launcher.tail(log, 300))})
        if path == "/api/cilog":
            import ci
            from urllib.parse import parse_qs, urlparse
            rid = (parse_qs(urlparse(self.path).query).get("run") or [""])[0]
            run = next((r for r in ci.runs(limit=ci.KEEP_RUNS) if r["id"] == rid), None)
            log = (run or {}).get("log")
            # Только журналы CI системы: путь из данных, но внутри ops/logs/ci.
            if not log or os.path.dirname(os.path.abspath(log)) != os.path.abspath(str(ci.logs_dir())):
                return self.send_json(404, {"ok": False, "error": "у прогона нет журнала CI"})
            return self.send_json(200, {"ok": True, "data": dict(run=rid, summary=ci.summary(run), log=ci.tail(log, 400))})
        if path == "/api/work":
            import workflow, launcher
            data = workflow.snapshot()
            for run in data["runs"]:
                # Открытая, но не принявшая задачу сессия: причина из журнала ИИ, если она есть.
                if run.get("status") == "queued" and run.get("launched_at"):
                    run["diagnosis"] = launcher.diagnose(run)
            return self.send_json(200, {"ok": True, "data": dict(data, launchers=launcher.status(),
                                                                  server=dict(stale=code_stale(), started=STARTED))})
        self.send_json(404, {"ok": False, "error": "нет такого адреса"})

    def do_POST(self):
        if not self.local_only():
            return
        path = self.path.split("?")[0]
        if path not in ("/api/refresh", "/api/connect", "/api/work", "/api/launch", "/api/start"):
            return self.send_json(404, {"ok": False, "error": "нет такого адреса"})
        if self.headers.get("X-TaskTreker") != "1":
            # Форма с чужого сайта такой заголовок поставить не может.
            return self.send_json(403, {"ok": False, "error": "запрос не от дашборда"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                return self.send_json(413, {"ok": False, "error": "слишком большой запрос"})
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            req = {}
        if not isinstance(req, dict):
            req = {}
        if path == "/api/launch":
            try:
                return self.send_json(200, {"ok": True, "data": launch_session(req)})
            except (ValueError, TypeError, KeyError, OSError) as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
        if path == "/api/start":
            try:
                return self.send_json(200, {"ok": True, "data": start_project(req)})
            except (ValueError, TypeError, KeyError, OSError) as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
        if path == "/api/work":
            import workflow
            try:
                return self.send_json(200, {"ok": True, "data": workflow.action(req)})
            except (ValueError, TypeError, KeyError) as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
        source = req.get("source", "all")
        if source not in SOURCES:
            return self.send_json(400, {"ok": False, "error": "неизвестный источник данных"})
        if path == "/api/connect":
            if not _lock.acquire(blocking=False):
                return self.send_json(409, {"ok": False, "error": "операция уже идёт"})
            try:
                ok, log, secs = run_pm(["connect", "--provider", source])
                return self.send_json(200, {"ok": ok, "log": log, "seconds": secs})
            finally:
                _lock.release()
        mode = req.get("mode", "fast")
        if not isinstance(mode, str) or mode not in MODES:
            return self.send_json(400, {"ok": False, "error": "неизвестный режим %r" % mode})
        args, title = MODES[mode]
        if mode == "project":
            key = str(req.get("project") or "")
            if key not in project_keys():
                return self.send_json(400, {"ok": False, "error": "нет такого проекта %r" % key})
            args, title = args + [key], "%s: %s" % (title, key)
        if mode != "vault" and "source" in req:
            args = args + ["--source", source]
        if not _lock.acquire(blocking=False):
            return self.send_json(409, {"ok": False, "error": "обновление уже идёт"})
        try:
            ok, log, secs = run_pm(args)
            _last.update({"at": time.time(), "mode": mode, "ok": ok, "log": log, "seconds": secs})
            self.send_json(200, {"ok": ok, "mode": mode, "title": title,
                                 "log": log, "seconds": secs})
        finally:
            _lock.release()

    def send_dashboard(self):
        if os.path.exists(DASH) and dashboard_stale():
            # Новый интерфейс без ручной пересборки: данные те же, пересобирается только страница.
            subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "build_dashboard.py")], cwd=ROOT,
                           capture_output=True, timeout=300)
        if not os.path.exists(DASH):
            ok, log, _ = run_pm(["refresh", "--fast"])
            if not os.path.exists(DASH):
                return self.send_json(500, {"ok": False, "error": "дашборд не собран", "log": log})
        with open(DASH, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    args = sys.argv[1:]
    port = 8787
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            port = int(args[i + 1])
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print("Не нашлось свободного порта в диапазоне %d–%d" % (port, port + 19))
        return 1
    url = "http://127.0.0.1:%d/" % port
    print("Дашборд: %s" % url)
    print("Кнопка «Обновить данные» на странице теперь работает по-настоящему.")
    print("Остановить — Ctrl+C.")
    if "--no-browser" not in args:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        threading.Thread(target=dispatcher, daemon=True, name="автозапуск ревью").start()
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
