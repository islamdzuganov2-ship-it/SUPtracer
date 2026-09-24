# -*- coding: utf-8 -*-
"""Брифинг сессии — то, с чего ассистент начинает работу.

Это замена чтению файлов данных в контекст. Полный портфель со всеми записями
памяти, бэклогами, находками и историей — это десятки тысяч токенов, и раньше
они попадали в окно целиком: платились за запись кеша, потом за его чтение,
и так каждую сессию. Здесь вместо данных выдаётся их упаковка в заданный бюджет:
что есть, где лежит и какой командой достать целиком.

Правило простое: брифинг отвечает на вопрос «что происходит», а не «что написано».
За «что написано» отвечает recall — и только по конкретной записи.

  py tools/context.py [--budget 4000] [--project X] [--json] [--no-memory]
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mdparse as md
import memory as mem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return {} if default is None else default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def fmt(n):
    try:
        return "{:,}".format(int(n)).replace(",", " ")
    except Exception:
        return str(n)


def money(n):
    return "$" + fmt(round(n or 0))


def usage_for(key, projects, usage):
    path = (projects[key].get("path") or "").lower().replace("\\", "/").rstrip("/")
    base = os.path.basename(path)
    for c in (key, key.lower(), base, "doc:" + base, "doc:" + base.replace("_", "-")):
        if c in usage:
            return usage[c]
    return None


def full_size():
    """Сколько стоило бы прочитать всё подряд — база для сравнения с брифингом."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(DATA):
        dirnames[:] = [d for d in dirnames if d != "snapshots"]
        for fn in filenames:
            if fn.endswith((".json", ".md")) and not fn.endswith(".example.json"):
                try:
                    with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                        total += md.est_tokens(f.read())
                except OSError:
                    pass
    return total


def collect(project=None):
    P = load("projects.json").get("projects", {})
    U = load("usage.json", {"projects": {}}).get("projects", {})
    R = load("repos.json", {"repos": {}}).get("repos", {})
    E = load("estimates.json", {"projects": {}}).get("projects", {})
    C = load("comments.json", {"comments": []}).get("comments", [])
    IN = load("inbox.json", {"items": []}).get("items", [])
    T = load("tree.json", {"stats": {}})
    keys = [k for k in P if not project or k == project]
    return P, U, R, E, C, IN, T, keys


def alerts(P, U, R, E, C, IN, keys):
    """Что требует решения сейчас. Первое, что должен увидеть ассистент."""
    out = []
    new_comments = [c for c in C if c.get("status") != "processed"]
    if new_comments:
        out.append("КОММЕНТАРИИ: %d неразобранных — %s" % (
            len(new_comments), "; ".join("#%s %s: %s" % (c.get("id"), c.get("project"),
                                                         (c.get("text") or "")[:70])
                                         for c in new_comments[:4])))
    new_inbox = [i for i in IN if i.get("status") == "new"]
    if new_inbox:
        out.append("ВХОДЯЩИЕ: %d предложений из заметок ждут решения — py tools/pm.py inbox" % len(new_inbox))
    for k in keys:
        r = R.get(k) or {}
        g = r.get("git") or {}
        sec = P[k].get("security") or {}
        if r and not r.get("exists", True):
            out.append("%s: каталога проекта нет на диске" % k)
        if g.get("dirty_files"):
            out.append("%s: %d незакоммиченных файлов" % (k, g["dirty_files"]))
        if r.get("exists") and not g:
            out.append("%s: нет git — история работы не сохраняется" % k)
        susp = [s for s in (r.get("secrets_suspect") or []) if ".claude/worktrees" not in str(s)]
        if susp:
            out.append("%s: подозрение на секреты в %d местах — проверить глазами" % (k, len(susp)))
        crit = sec.get("open_critical") or []
        if crit:
            out.append("%s: открытых критичных находок ИБ %d — %s" % (k, len(crit), crit[0][:80]))
        if not (P[k].get("readiness") or {}).get("basis"):
            out.append("%s: пустой readiness.basis — цифры готовности ничем не обоснованы" % k)
    return out


def build(budget=4000, project=None, with_memory=True):
    P, U, R, E, C, IN, T, keys = collect(project)
    lines, spent_tokens = [], 0

    def put(s, cost_only=False):
        nonlocal spent_tokens
        c = md.est_tokens(s)
        if spent_tokens + c > budget:
            return False
        spent_tokens += c
        if not cost_only:
            lines.append(s)
        return True

    gen = load("usage.json", {}).get("generated_at") or ""
    put("ОПЕРАТИВНАЯ ПАМЯТЬ TASKTREKER · %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    put("Данные лежат в системе, не в контексте. Ниже — указатель, а не содержимое.")
    put("  подробности по проекту  py tools/pm.py status <проект>")
    put("  запись памяти целиком   py tools/pm.py recall <id|слова>")
    put("  записать новое знание   py tools/pm.py remember <проект> \"заголовок\" --text \"...\" --kind решение")
    put("  разобрать входящие      py tools/pm.py inbox  ·  принять: py tools/pm.py apply <IN-n>")
    put("  пересобрать факты       py tools/pm.py refresh" + (" (последний сбор %s)" % gen[:16] if gen else ""))
    put("")

    al = alerts(P, U, R, E, C, IN, keys)
    if al:
        put("ТРЕБУЕТ РЕШЕНИЯ")
        for a in al[:12]:
            put("  ! " + a)
        put("")

    spent = sum((usage_for(k, P, U) or {}).get("usd", 0) for k in keys)
    rem = sum(E.get(k, {}).get("remaining_usd", 0) for k in keys)
    st = T.get("stats") or {}
    put("ПОРТФЕЛЬ: проектов %d · потрачено %s · осталось %s · дерево работ %d узлов, глубина %d" % (
        len(keys), money(spent), money(rem), st.get("nodes", 0), st.get("depth", 0)))

    order = sorted(keys, key=lambda k: (P[k].get("tier", 9),
                                        -((usage_for(k, P, U) or {}).get("usd", 0))))
    for k in order:
        p, u, e = P[k], usage_for(k, P, U) or {}, E.get(k, {})
        rd = p.get("readiness") or {}
        g = (R.get(k) or {}).get("git") or {}
        put("")
        put("· %s [%s] — %s" % (p.get("name", k), k, p.get("status", "")))
        put("  цель: %s | готовность ф%s%% э%s%% д%s%% т%s%% иб%s%%" % (
            p.get("goal", ""), rd.get("functional", "—"), rd.get("production", "—"),
            rd.get("docs", "—"), rd.get("tests", "—"), rd.get("security", "—")))
        put("  потрачено %s · осталось %s на %d задач%s" % (
            money(u.get("usd", 0)), money(e.get("remaining_usd", 0)),
            len(e.get("items", [])),
            (" · коммитов %s" % g["commits"]) if g.get("commits") is not None else ""))
        acts = p.get("next_actions") or []
        if acts:
            put("  дальше: " + " | ".join(a[:90] for a in acts[:2]))
        top = sorted(e.get("items", []), key=lambda b: (b.get("priority") != "🔴", b.get("id", "")))
        for b in top[:3]:
            put("    %s %-7s %-2s %s" % (b.get("priority", " "), b.get("id"), b.get("size"),
                                         (b.get("title") or "")[:88]))
        if len(top) > 3:
            put("    … ещё %d задач — py tools/pm.py status %s" % (len(top) - 3, k))

    if with_memory:
        put("")
        left = max(300, budget - spent_tokens - 120)
        b = mem.brief(budget=left, project=project)
        for ln in b.splitlines():
            put(ln)

    body = "\n".join(lines)
    total, mine = full_size(), md.est_tokens(body)
    if total > mine:
        tail = ("\n\nБрифинг ~%s токенов вместо ~%s, если читать data/ целиком (%d%% экономии). "
                "Недостающее берётся точечно: recall, status, inbox." % (
                    fmt(mine), fmt(total), int(100 - 100 * mine / total)))
    else:
        # Данных в системе пока меньше, чем в самом брифинге, — сравнивать нечего.
        tail = ("\n\nБрифинг ~%s токенов. Данных в системе пока мало: экономия появится, "
                "когда наберутся проекты и записи памяти." % fmt(mine))
    return body + tail


def build_json(project=None):
    P, U, R, E, C, IN, T, keys = collect(project)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alerts": alerts(P, U, R, E, C, IN, keys),
        "projects": {k: {
            "name": P[k].get("name"), "status": P[k].get("status"), "goal": P[k].get("goal"),
            "readiness": P[k].get("readiness", {}),
            "spent_usd": (usage_for(k, P, U) or {}).get("usd", 0),
            "remaining_usd": E.get(k, {}).get("remaining_usd", 0),
            "backlog": [{"id": b.get("id"), "title": b.get("title"), "size": b.get("size"),
                         "priority": b.get("priority"), "status": b.get("status")}
                        for b in E.get(k, {}).get("items", [])],
            "next_actions": P[k].get("next_actions", []),
        } for k in keys},
        "memory": [{"id": r["id"], "kind": r["kind"], "project": r["project"],
                    "title": r["title"], "tokens": r["tokens"]}
                   for r in mem.load_index().get("records", []) if r["status"] == "активна"],
        "inbox_new": [i for i in IN if i.get("status") == "new"],
        "tree_stats": T.get("stats", {}),
    }


def main():
    args = sys.argv[1:]
    def opt(name, d=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else d
    if "--json" in args:
        print(json.dumps(build_json(opt("--project")), ensure_ascii=False, indent=2))
        return 0
    print(build(budget=int(opt("--budget", 4000)), project=opt("--project"),
                with_memory="--no-memory" not in args))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
