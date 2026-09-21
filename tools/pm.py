# -*- coding: utf-8 -*-
"""TaskTreker — командная строка системы проектного управления.

  py tools/pm.py init                 первый запуск: разложить примеры в рабочие файлы
  py tools/pm.py refresh              пересобрать всё: токены, репозитории, оценки, дашборд
  py tools/pm.py refresh --fast       без пересканирования репозиториев (быстро)
  py tools/pm.py comment <проект> "текст" [--kind техдолг|идея|баг|вопрос]
  py tools/pm.py comments             показать комментарии
  py tools/pm.py status [проект]      сводка в терминал
  py tools/pm.py snapshot             сохранить снимок для истории трендов
  py tools/pm.py check                проверки перед слиянием в тестирование и main
  py tools/pm.py open                 открыть дашборд в браузере
"""
import json
import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
PY = sys.executable


def jload(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default if default is not None else {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jsave(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def run(script, *args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([PY, os.path.join(ROOT, "tools", script)] + list(args),
                       cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace")
    return r.returncode


def fmt(n):
    return "{:,}".format(int(n)).replace(",", " ")


def cmd_refresh(args):
    fast = "--fast" in args
    print("→ токены из журналов сессий")
    run("collect_usage.py")
    if not fast:
        print("\n→ технический срез репозиториев")
        run("collect_repos.py")
    else:
        print("\n→ репозитории пропущены (--fast)")
    print("\n→ оценка оставшихся задач")
    run("estimate.py")
    print("\n→ сборка дашборда")
    run("build_dashboard.py")
    cmd_snapshot([])
    print("\nГотово. Открыть: py tools/pm.py open")


def cmd_comment(args):
    if len(args) < 2:
        print('Использование: py tools/pm.py comment <проект> "текст" [--kind техдолг|идея|баг|вопрос]')
        projects = jload("projects.json").get("projects", {})
        print("Проекты: " + ", ".join(projects) + ", портфель")
        return 1
    project, text = args[0], args[1]
    kind = "комментарий"
    if "--kind" in args:
        i = args.index("--kind")
        if i + 1 < len(args):
            kind = args[i + 1]
    projects = jload("projects.json").get("projects", {})
    if project not in projects and project != "портфель":
        print("Нет такого проекта: %s" % project)
        print("Доступны: " + ", ".join(projects) + ", портфель")
        return 1
    store = jload("comments.json", {"schema": 1, "next_id": 1, "comments": []})
    cid = store.get("next_id", 1)
    store["comments"].append({
        "id": cid, "project": project, "kind": kind, "text": text,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "new", "moved_to": None,
    })
    store["next_id"] = cid + 1
    jsave("comments.json", store)
    print("Комментарий #%d записан к проекту «%s» (%s)." % (cid, project, kind))
    print("Он попадёт в бэклог при следующем разборе Клодом. Пересобрать дашборд: py tools/pm.py refresh --fast")
    return 0


def cmd_comments(args):
    store = jload("comments.json", {"comments": []})
    cs = store.get("comments", [])
    if not cs:
        print("Комментариев нет.")
        return 0
    for c in cs:
        mark = "✓" if c.get("status") == "processed" else "•"
        print("%s #%-3d [%s] %s — %s" % (mark, c["id"], c.get("project", "?"), c.get("kind", ""), c["text"]))
        if c.get("moved_to"):
            print("      → перенесён в %s" % c["moved_to"])
    new = sum(1 for c in cs if c.get("status") != "processed")
    print("\nВсего %d, неразобранных %d." % (len(cs), new))
    return 0


def cmd_status(args):
    P = jload("projects.json").get("projects", {})
    U = jload("usage.json").get("projects", {})
    R = jload("repos.json").get("repos", {})
    E = jload("estimates.json").get("projects", {})

    def usage_of(key):
        path = (P[key].get("path") or "").lower().rstrip("/")
        base = os.path.basename(path)
        for c in (key, key.lower(), base, "doc:" + base, "doc:" + base.replace("_", "-")):
            if c in U:
                return U[c]
        return None

    only = [a for a in args if not a.startswith("-")]
    keys = only or sorted(P, key=lambda k: (P[k].get("tier", 9), -(usage_of(k)["usd"] if usage_of(k) else 0)))

    if not only:
        spent = sum(usage_of(k)["usd"] for k in P if usage_of(k))
        rem = sum(E.get(k, {}).get("remaining_usd", 0) for k in P)
        print("ПОРТФЕЛЬ: %d проектов · потрачено $%s · осталось $%s\n" % (len(P), fmt(spent), fmt(rem)))
        print("%-16s %-22s %5s %5s %10s %10s %8s" % ("проект", "статус", "готов", "ИБ", "потрач.", "остал.", "коммит"))
        print("-" * 82)
        for k in keys:
            p, u, e = P[k], usage_of(k), E.get(k, {})
            g = (R.get(k) or {}).get("git") or {}
            rd = p.get("readiness", {})
            print("%-16s %-22s %4s%% %4s%% %10s %10s %8s" % (
                p["name"][:16], p.get("status", "")[:22], rd.get("functional", "—"), rd.get("security", "—"),
                "$" + fmt(u["usd"]) if u else "—",
                "$" + fmt(e.get("remaining_usd", 0)) if e.get("remaining_usd") else "—",
                g.get("commits", "нет git")))
        return 0

    for k in keys:
        if k not in P:
            print("нет проекта %s" % k)
            continue
        p, u, e, r = P[k], usage_of(k), E.get(k, {}), R.get(k, {})
        rd = p.get("readiness", {})
        print("=" * 70)
        print("%s — %s" % (p["name"], p.get("tagline", "")))
        print("цель: %s · статус: %s" % (p.get("goal", ""), p.get("status", "")))
        print("готовность: функц %s%% · эксплуат %s%% · доки %s%% · тесты %s%% · ИБ %s%%" % (
            rd.get("functional"), rd.get("production"), rd.get("docs"), rd.get("tests"), rd.get("security")))
        if u:
            print("потрачено: %s токенов, $%s (%d сессий, %d запросов)" % (
                fmt(u["billable"]), fmt(u["usd"]), u["sessions"], u["requests"]))
        if e:
            print("осталось:  %s токенов, $%s (%d задач)" % (
                fmt(e["remaining_tokens"]), fmt(e["remaining_usd"]), len(e["items"])))
        if r.get("code"):
            print("код: %s строк, %d файлов, %d тестовых, %d TODO" % (
                fmt(r["code"]["loc_total"]), r["code"]["files_total"], r["code"]["test_files"], r["code"]["todo_markers"]))
        print("\nследующие шаги:")
        for a in p.get("next_actions", []):
            print("  · %s" % a)
        print("\nбэклог:")
        for b in e.get("items", []):
            print("  %s %-14s [%s] %s — %s, $%s" % (
                b.get("priority", " "), b["id"], b["size"], b["title"][:60],
                b.get("status", ""), fmt(b["est_usd"])))
        print()
    return 0


def cmd_snapshot(args):
    U = jload("usage.json")
    P = jload("projects.json").get("projects", {})
    E = jload("estimates.json")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = {
        "date": day,
        "total_tokens": U.get("total", {}).get("tokens"),
        "total_usd": U.get("total", {}).get("usd"),
        "remaining_usd": E.get("totals", {}).get("remaining_usd"),
        "projects": {k: {
            "functional": P[k].get("readiness", {}).get("functional"),
            "security": P[k].get("readiness", {}).get("security"),
            "backlog_items": len(E.get("projects", {}).get(k, {}).get("items", [])),
            "remaining_usd": E.get("projects", {}).get(k, {}).get("remaining_usd"),
        } for k in P},
    }
    d = os.path.join(DATA, "snapshots")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, day + ".json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print("снимок сохранён: data/snapshots/%s.json" % day)
    return 0


def cmd_open(args):
    p = os.path.join(ROOT, "dashboard.html")
    if not os.path.exists(p):
        print("Дашборда нет. Собрать: py tools/pm.py refresh")
        return 1
    webbrowser.open("file:///" + p.replace("\\", "/"))
    print("открыт " + p)
    return 0


def cmd_init(args):
    """Разложить примеры в рабочие файлы — первый запуск после клонирования."""
    pairs = [("projects.example.json", "projects.json"),
             ("comments.example.json", "comments.json"),
             ("competitors.example.json", "competitors.json")]
    made = []
    for src, dst in pairs:
        s, d = os.path.join(DATA, src), os.path.join(DATA, dst)
        if os.path.exists(d):
            print("  уже есть, не трогаю: data/%s" % dst)
            continue
        if not os.path.exists(s):
            print("  нет примера data/%s — пропускаю" % src)
            continue
        with open(s, encoding="utf-8") as f:
            data = f.read()
        with open(d, "w", encoding="utf-8") as f:
            f.write(data)
        made.append(dst)
        print("  создан data/%s" % dst)
    if made:
        print("\nТеперь впишите свои проекты в data/projects.json — главное поле `path`,")
        print("по нему собираются и код, и расход токенов. Затем: py tools/pm.py refresh")
    else:
        print("\nВсё на месте. Обновить: py tools/pm.py refresh")
    return 0


def cmd_check(args):
    return run("check.py", *args)


COMMANDS = {"init": cmd_init, "refresh": cmd_refresh, "comment": cmd_comment, "comments": cmd_comments,
            "status": cmd_status, "snapshot": cmd_snapshot, "check": cmd_check,
            "open": cmd_open}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print("Неизвестная команда: %s" % cmd)
        print(__doc__)
        return 1
    return COMMANDS[cmd](sys.argv[2:]) or 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
