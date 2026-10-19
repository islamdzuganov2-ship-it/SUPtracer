# -*- coding: utf-8 -*-
"""Брифинг сессии — то, с чего ассистент начинает работу (ТЗ-01, раздел 5.1).

Это замена чтению файлов данных в контекст. Полный портфель со всеми записями
памяти, бэклогами, находками и историей — это десятки тысяч токенов, и раньше
они попадали в окно целиком: платились за запись кеша, потом за его чтение,
и так каждую сессию. Здесь вместо данных выдаётся их упаковка в заданный бюджет:
что есть, где лежит и какой командой достать целиком.

Брифинг отвечает на вопрос «что происходит», а не «что написано». За «что написано»
отвечает recall — и только по конкретной записи.

Два режима:
  портфель   все проекты, для работы в самой системе;
  проект     один проект целиком, остальные одной строкой — для сессии в каталоге
             проекта; его выбирает хук начала сессии по рабочему каталогу.

  py tools/context.py [--budget 4000] [--project X | --cwd ПУТЬ] [--json] [--no-memory]
  py tools/context.py --hook            читает событие хука со stdin (ИНТ-03)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_repos
import mdparse as md
import memory as mem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")   # ИНТ-01
PM = os.path.join(ROOT, "tools", "pm.py").replace("\\", "/")

CLOSED = ("done", "cancelled", "dropped")   # закрытые задачи не входят в «осталось»
HOOK_BUDGET = 1200         # ИНТ-03: брифинг в начале каждой сессии не должен съедать экономию


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


# ------------------------------------------------------------------ проект по каталогу

def _norm(path):
    return os.path.abspath(path or "").replace("\\", "/").rstrip("/").lower()


def project_for_path(path, projects):
    """ИНТ-02: рабочий каталог сессии -> ключ проекта.

    Совпадением считается сам каталог проекта, любой вложенный в него (включая
    .claude/worktrees) и соседний «<проект>.worktrees». Из нескольких подходящих
    берётся самый глубокий путь: вложенный проект важнее объемлющего."""
    from project_routes import paths, resolve_path
    return resolve_path(path, paths(projects))['project']


# ------------------------------------------------------------------ сбор

def full_size():
    """Сколько стоило бы прочитать данные подряд — база для сравнения с брифингом.
    Память берётся из индекса, а не читается заново: брифинг зовётся в начале каждой
    сессии и не должен сам стоить дорого."""
    total = 0
    if os.path.isdir(DATA):
        for fn in os.listdir(DATA):
            if fn.endswith(".json") and not fn.endswith(".example.json"):
                try:
                    with open(os.path.join(DATA, fn), encoding="utf-8") as f:
                        total += md.est_tokens(f.read())
                except OSError:
                    pass
    idx = mem.load_index(rebuild_if_missing=False)
    recs = idx.get("records", [])
    total += (idx.get("totals") or {}).get("stored_tokens", 0) + 60 * len(recs)
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


def project_alerts(k, P, R):
    out = []
    r = R.get(k) or {}
    g = r.get("git") or {}
    sec = P[k].get("security") or {}
    if r and not r.get("exists", True):
        out.append("%s: каталога проекта нет на диске" % k)
    if g.get("dirty_files"):
        out.append("%s: %d незакоммиченных файлов" % (k, g["dirty_files"]))
    # Без git тревожно там, где есть код. Проект из одних заметок в хранилище Obsidian
    # живёт историей хранилища, а не git — как и на дашборде.
    if r.get("exists") and not g and ((r.get("code") or {}).get("loc_total") or 0) > 0:
        out.append("%s: нет git — история работы не сохраняется" % k)
    susp = collect_repos.active_suspects(r, P[k])
    if susp:
        out.append("%s: подозрение на секреты в %d местах — проверить глазами" % (k, len(susp)))
    crit = sec.get("open_critical") or []
    if crit:
        out.append("%s: открытых критичных находок ИБ %d — %s" % (k, len(crit), crit[0][:80]))
    if not (P[k].get("readiness") or {}).get("basis"):
        out.append("%s: пустой readiness.basis — цифры готовности ничем не обоснованы" % k)
    return out


def alerts(P, U, R, E, C, IN, keys, focus=None):
    """Что требует решения сейчас. В режиме проекта — его тревоги и портфельные счётчики."""
    out = []
    new_comments = [c for c in C if c.get("status") != "processed"]
    mine = [c for c in new_comments if not focus or c.get("project") == focus]
    if mine:
        out.append("КОММЕНТАРИИ: %d неразобранных — %s" % (
            len(mine), "; ".join("#%s %s: %s" % (c.get("id"), c.get("project"),
                                                 (c.get("text") or "")[:70]) for c in mine[:4])))
    elif focus and new_comments:
        out.append("КОММЕНТАРИИ: по другим проектам неразобранных %d" % len(new_comments))
    new_inbox = [i for i in IN if i.get("status") == "new"]
    mine_inbox = [i for i in new_inbox if not focus or i.get("project") == focus]
    if mine_inbox:
        out.append("ВХОДЯЩИЕ: %d предложений из заметок ждут решения" % len(mine_inbox))
    elif focus and new_inbox:
        out.append("ВХОДЯЩИЕ: по другим проектам %d предложений" % len(new_inbox))
    for k in ([focus] if focus else keys):
        out += project_alerts(k, P, R)
    return out


def _project_block(k, P, U, R, E, full):
    p, u, e = P[k], usage_for(k, P, U) or {}, E.get(k, {})
    rd = p.get("readiness") or {}
    if rd.get("assessed") is False:
        rd = dict.fromkeys(("functional", "production", "docs", "tests", "security"), "не оценено")
    g = (R.get(k) or {}).get("git") or {}
    backlog_open = [b for b in p.get("backlog", []) if b.get("status") not in CLOSED]
    est_items = e.get("items", [])
    # Оценку собирает скрипт раз в сутки; если бэклог правили после, её сумме верить
    # нельзя — задачи берём из бэклога и прямо говорим, что сумма старая.
    stale = len(est_items) != len(p.get("backlog", []))
    open_items = backlog_open if stale else [b for b in est_items if not b.get("closed")]
    lines = [
        "· %s [%s] — %s" % (p.get("name", k), k, p.get("status", "")),
        ("  цель: %s | готовность не оценена" % p.get('goal','')) if p.get('readiness',{}).get('assessed') is False else "  цель: %s | готовность ф%s%% э%s%% д%s%% т%s%% иб%s%%" % (
            p.get("goal", ""), rd.get("functional", "—"), rd.get("production", "—"),
            rd.get("docs", "—"), rd.get("tests", "—"), rd.get("security", "—")),
        "  потрачено %s · осталось %s на %d задач%s%s" % (
            money(u.get("usd", 0)), money(e.get("remaining_usd", 0)), len(open_items),
            (" · коммитов %s" % g.get('project_commits', g["commits"])) if g.get("commits") is not None else "",
            " (оценка старше бэклога — refresh)" if stale else ""),
    ]
    acts = p.get("next_actions") or []
    if acts:
        shown = acts if full else acts[:2]
        lines.append("  дальше: " + " | ".join(a[:110 if full else 90] for a in shown))
    top = sorted(open_items, key=lambda b: (b.get("priority") != "🔴", str(b.get("id", ""))))
    limit = 6 if full else 3
    for b in top[:limit]:
        lines.append("    %s %-7s %-2s %s" % (b.get("priority", " "), b.get("id"), b.get("size"),
                                           (b.get("title") or "")[:88]))
    if len(top) > limit:
        lines.append("    … ещё %d задач — status %s" % (len(top) - limit, k))
    if full:
        crit = (p.get("security") or {}).get("open_critical") or []
        for c in crit[:3]:
            lines.append("  ИБ: " + c[:110])
    return lines


def _one_line(k, P, U, E):
    p, e = P[k], E.get(k, {})
    rd = p.get("readiness") or {}
    left = [b for b in e.get("items", []) if not b.get("closed")]
    return "  %s [%s] ф%s%% иб%s%% · осталось %s, задач %d" % (
        p.get("name", k)[:28], k, rd.get("functional", "—"), rd.get("security", "—"),
        money(e.get("remaining_usd", 0)), len(left))


def _commands(hook):
    """В чужом проекте относительный tools/pm.py не сработает — даём абсолютный путь
    и имена инструментов MCP-сервера, если он подключён."""
    if hook:
        return ["Инструменты: tt_recall <id|слова>, tt_remember, tt_status <проект>, tt_inbox "
                "(MCP-сервер tasktreker). Без MCP: py \"%s\" recall|remember|status." % PM]
    return ["  подробности по проекту  py tools/pm.py status <проект>",
            "  запись памяти целиком   py tools/pm.py recall <id|слова>",
            "  записать новое знание   py tools/pm.py remember <проект> \"заголовок\" --text \"...\" --kind решение",
            "  разобрать входящие      py tools/pm.py inbox  ·  принять: py tools/pm.py apply <IN-n>"]


def _tail(mine, total):
    if total > mine:
        return ("Брифинг ~%s токенов вместо ~%s, если читать data/ целиком (%d%% экономии). "
                "Недостающее берётся точечно: recall, status, inbox." % (
                    fmt(mine), fmt(total), int(100 - 100 * mine / total)))
    # ПАМ-11: данных меньше, чем в самом брифинге, — сравнивать нечего, процент не выдумываем.
    return ("Брифинг ~%s токенов. Данных в системе пока мало: экономия появится, "
            "когда наберутся проекты и записи памяти." % fmt(mine))


def build(budget=4000, project=None, with_memory=True, focus=None, hook=False):
    """ПАМ-05: результат вместе с хвостом не длиннее budget токенов по оценке est_tokens.
    ПАМ-06: при focus проект раскрыт целиком, остальные — одной строкой."""
    P, U, R, E, C, IN, T, keys = collect(project)
    if focus and focus not in P:
        focus = None
    reserve = md.est_tokens(_tail(99999, 999999)) + 8
    limit = max(60, budget - reserve)
    lines, used = [], 0

    def put(s):
        nonlocal used
        c = md.est_tokens(s) + 1                  # +1 — перевод строки
        if used + c > limit:
            return False
        used += c
        lines.append(s)
        return True

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if focus:
        put("TASKTREKER · проект %s · %s" % (focus, stamp))
        put("Память проекта лежит в системе, ниже указатель. Не копируй её к себе — читай точечно, "
            "новое знание записывай в систему.")
    else:
        gen = load("usage.json", {}).get("generated_at") or ""
        put("ОПЕРАТИВНАЯ ПАМЯТЬ TASKTREKER · %s" % stamp)
        put("Данные лежат в системе, не в контексте. Ниже — указатель, а не содержимое.")
        if gen:
            put("  последний сбор фактов %s" % gen[:16].replace("T", " "))
    for c in _commands(hook):
        put(c)
    put("Перед реализацией: tt_work queue → plan/next → take (agent — свой ИИ); чужую метку claimed_by не трогать, heartbeat пока работаешь, finish только с проверками. CLI: pm.py work next --agent claude.")
    if any(u.get("cost_complete") is False for u in U.values()):
        put("Стоимость части расхода неизвестна: суммы в $ показывают только оценённую часть, не полную цену.")
    put("")

    al = alerts(P, U, R, E, C, IN, keys, focus)
    if al:
        put("ТРЕБУЕТ РЕШЕНИЯ")
        for a in al[:12 if not focus else 8]:
            put("  ! " + a)
        put("")

    if focus:
        for ln in _project_block(focus, P, U, R, E, full=True):
            put(ln)
        others = [k for k in keys if k != focus]
        if others:
            put("")
            put("ОСТАЛЬНОЙ ПОРТФЕЛЬ (%d)" % len(others))
            order = sorted(others, key=lambda k: (P[k].get("tier", 9), k))
            for k in order:
                if not put(_one_line(k, P, U, E)):
                    break
    else:
        spent = sum((usage_for(k, P, U) or {}).get("usd", 0) for k in keys)
        rem = sum(E.get(k, {}).get("remaining_usd", 0) for k in keys)
        st = T.get("stats") or {}
        put("ПОРТФЕЛЬ: проектов %d · потрачено %s · осталось %s · дерево работ %d узлов, глубина %d" % (
            len(keys), money(spent), money(rem), st.get("nodes", 0), st.get("depth", 0)))
        order = sorted(keys, key=lambda k: (P[k].get("tier", 9),
                                            -((usage_for(k, P, U) or {}).get("usd", 0))))
        for k in order:
            put("")
            for ln in _project_block(k, P, U, R, E, full=False):
                put(ln)

    if with_memory:
        left = limit - used - 4
        if left > 40:
            put("")
            b = mem.brief(budget=left, project=focus or project,
                          recall_cmd="tt_recall <id>" if hook else "py tools/pm.py recall <id>")
            for ln in b.splitlines():
                if not put(ln):
                    break

    body = "\n".join(lines).rstrip()
    total = full_size()
    out = body + "\n\n" + _tail(md.est_tokens(body), total)
    # Оценка суммы строк и оценка целого текста расходятся на единицы токенов:
    # добиваем гарантию бюджета, снимая строки с конца указателя памяти.
    while md.est_tokens(out) > budget and len(lines) > 1:
        lines.pop()
        body = "\n".join(lines).rstrip()
        out = body + "\n\n" + _tail(md.est_tokens(body), total)
    return out


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


def parse_event(raw):
    """Событие хука. Клиент присылает корректный JSON, но путь Windows с одиночными
    обратными слэшами (ручной вызов, чужой клиент) ломает разбор — тогда достаём cwd
    регулярным выражением: без каталога брифинг ушёл бы не в тот проект."""
    if not (raw or "").strip():
        return {}
    try:
        ev = json.loads(raw)
        return ev if isinstance(ev, dict) else {}
    except Exception:
        m = re.search(r'"cwd"\s*:\s*"([^"]*)"', raw)
        return {"cwd": m.group(1).replace("\\\\", "\\")} if m else {}


def hook_output(event):
    """ИНТ-03: ответ хуку начала сессии. Вне портфеля — одна строка, чтобы не тратить
    чужой контекст; в проекте портфеля — брифинг по нему."""
    cwd = (event or {}).get("cwd") or os.getcwd()
    P = load("projects.json").get("projects", {})
    key = project_for_path(cwd, P)
    if not key:
        if _norm(cwd) == _norm(ROOT) or _norm(cwd).startswith(_norm(ROOT) + "/"):
            return build(budget=HOOK_BUDGET, hook=True)
        return ("TaskTreker: каталог вне портфеля. Портфель и память доступны инструментами "
                "tt_context и tt_recall (MCP-сервер tasktreker).")
    return build(budget=HOOK_BUDGET, focus=key, hook=True)


def main():
    args = sys.argv[1:]

    def opt(name, d=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else d

    if "--hook" in args:
        # Хук не имеет права ломать сессию: любая ошибка — одна строка, код возврата 0.
        try:
            raw = sys.stdin.read() if not sys.stdin.isatty() else ""
            print(hook_output(parse_event(raw)))
        except Exception as e:
            print("TaskTreker: брифинг не собрался (%s). Команда: py \"%s\" context" % (
                type(e).__name__, PM))
        return 0
    if "--json" in args:
        print(json.dumps(build_json(opt("--project")), ensure_ascii=False, indent=2))
        return 0
    focus = opt("--project")
    if not focus and opt("--cwd"):
        focus = project_for_path(opt("--cwd"), load("projects.json").get("projects", {}))
    print(build(budget=int(opt("--budget", 4000)), focus=focus,
                with_memory="--no-memory" not in args))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
