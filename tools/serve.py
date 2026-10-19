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
    import workflow
    if not workflow.settings()["auto_launch_review"]:
        return []
    started = []
    for r in workflow.state()["runs"]:
        if r.get("status") != "queued" or not r.get("auto_launch") or r.get("launched_at"):
            continue
        if time.time() - _auto_attempts.get(r["id"], 0) < 300:
            continue
        _auto_attempts[r["id"]] = time.time()
        try:
            launch_session(dict(ref=r["ref"], agent=r.get("agent")))
            started.append(r["id"])
        except Exception as e:   # noqa: BLE001 — причина видна на карточке
            workflow.mark_launch_error(r["ref"], r["id"], e)
    return started


def dispatcher(interval=15):
    while True:
        time.sleep(interval)
        try:
            dispatch_once()
        except Exception as e:   # noqa: BLE001 — сервер досок не должен падать из-за автозапуска
            sys.stderr.write("автозапуск: %s\n" % e)


def launch_session(req):
    """«Запустить» на доске: запуск в очереди, рабочая копия, окно сессии ИИ, отметка.

    Окно открывается только здесь — по нажатию владельца на локальном сервере. MCP-инструмент
    такой операции не имеет: ИИ не открывает сессии других ИИ."""
    import workflow, launcher
    ref = str(req.get("ref") or "")
    t = workflow.task_for(ref)
    run = next((r for r in workflow.state()["runs"] if r["ref"] == ref and r["status"] == "queued"), None)
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
    if run.get("phase") == "review":
        # Ревьюер смотрит ту самую ветку, которую сделал исполнитель.
        worktree = workflow.task_branch(workflow.state(), ref)
    else:
        worktree = launcher.ensure_worktree(base, agent, t["id"]) if req.get("isolate", True) and base else None
    cwd = (worktree or {}).get("path") or base
    info = workflow.action(dict(op="prompt", ref=ref, worker=workflow.worker_id(run, agent), worktree=worktree))
    # Данные системы — общие: сессия в рабочей копии самой системы не должна видеть пустой data/.
    opened = launcher.launch(agent, info["route"]["model"], info["route"]["effort"], info["prompt"], cwd,
                             env_extra={"TASKTREKER_DATA": workflow.data_dir().as_posix(),
                                        "TASKTREKER_SYSTEM": os.path.dirname(os.path.abspath(workflow.__file__))})
    workflow.mark_launched(ref, run["id"], dict(pid=opened["pid"], cli=os.path.basename(opened["exe"]),
                                                 cwd=cwd, worktree=worktree, agent=agent))
    return dict(run=run["id"], agent=agent, pid=opened["pid"], cwd=cwd, worktree=worktree,
                route=info["route"], worker=info["worker"], prompt=info["prompt"])


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
        if path not in ("/api/refresh", "/api/connect", "/api/work", "/api/launch"):
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
