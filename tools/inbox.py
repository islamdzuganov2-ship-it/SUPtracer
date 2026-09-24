# -*- coding: utf-8 -*-
"""Входящие предложения: то, что разбор заметок нашёл, но сам в бэклог не положил.

Почему отдельный слой. Бэклог в projects.json — ручной источник истины: от него
считается оценка затрат и на нём стоит гейт слияния. Разборщик текста не имеет
права туда писать, иначе одна неудачно сформулированная заметка испортит прогноз
по всему портфелю. Поэтому предложения ждут здесь, пока их не примут командой apply.

  py tools/inbox.py list [--project X] [--all]
  py tools/inbox.py apply <IN-1> [IN-2 ...] | --all [--project X]
  py tools/inbox.py reject <IN-3> --why "почему не берём"
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mdparse as md

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")   # ИНТ-01
INBOX = os.path.join(DATA, "inbox.json")
PROJECTS = os.path.join(DATA, "projects.json")

SIZE_ORDER = {"S": 0, "M": 1, "L": 2, "XL": 3}


def jload(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jsave(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def next_task_id(project_key, backlog):
    """Продолжаем нумерацию проекта, а не заводим свою: id задач читают люди."""
    prefixes = {}
    for b in backlog:
        m = re.match(r"^([A-ZА-Я]{1,6})-(\d+)$", str(b.get("id") or ""))
        if m:
            prefixes.setdefault(m.group(1), []).append(int(m.group(2)))
    if prefixes:
        pref = max(prefixes, key=lambda p: len(prefixes[p]))
        return "%s-%02d" % (pref, max(prefixes[pref]) + 1)
    letters = re.sub(r"[^A-Za-z]", "", md.slugify(project_key)).upper()
    pref = (letters[:2] or "TK")
    used = [int(m.group(1)) for b in backlog
            for m in [re.match(r"^%s-(\d+)$" % pref, str(b.get("id") or ""))] if m]
    return "%s-%02d" % (pref, (max(used) + 1) if used else 1)


def cmd_list(args):
    box = jload(INBOX, {"items": []})
    items = box.get("items", [])
    only = _opt(args, "--project")
    show_all = "--all" in args
    rows = [i for i in items
            if (show_all or i.get("status") == "new") and (not only or i.get("project") == only)]
    if not rows:
        print("Входящих предложений нет." if not items else
              "Неразобранных предложений нет (всего в файле %d)." % len(items))
        return 0
    rows.sort(key=lambda i: (i.get("project", ""), -SIZE_ORDER.get(i.get("size"), 1)))
    cur = None
    for i in rows:
        if i.get("project") != cur:
            cur = i.get("project")
            print("\n%s" % cur)
        mark = {"new": "•", "applied": "✓", "rejected": "×"}.get(i.get("status"), "?")
        print("  %s %-6s %s %-2s %s" % (mark, i.get("id"), i.get("priority", " "),
                                        i.get("size", ""), i.get("title", "")))
        print("      %s" % i.get("why", ""))
        if i.get("source"):
            print("      источник: %s" % i["source"])
        if i.get("applied_to"):
            print("      → принято как %s" % i["applied_to"])
    new = sum(1 for i in items if i.get("status") == "new")
    print("\nВсего %d, неразобранных %d. Принять: py tools/pm.py apply <IN-n>" % (len(items), new))
    return 0


def cmd_apply(args):
    box = jload(INBOX, {"items": []})
    proj_file = jload(PROJECTS)
    if not proj_file:
        print("Нет data/projects.json")
        return 1
    projects = proj_file.get("projects", {})
    ids = [a for a in args if a.startswith("IN-") or re.fullmatch(r"\d+", a)]
    ids = ["IN-" + a if a.isdigit() else a for a in ids]
    only = _opt(args, "--project")
    if "--all" in args:
        ids = [i["id"] for i in box.get("items", [])
               if i.get("status") == "new" and (not only or i.get("project") == only)]
    if not ids:
        print("Что принять? py tools/pm.py apply <IN-1> [IN-2 ...] | --all [--project X]")
        return 1

    applied, skipped = [], []
    for it in box.get("items", []):
        if it.get("id") not in ids:
            continue
        if it.get("status") != "new":
            skipped.append("%s уже %s" % (it["id"], it.get("status")))
            continue
        key = it.get("project")
        if key not in projects:
            skipped.append("%s: проекта «%s» нет в projects.json" % (it["id"], key))
            continue
        backlog = projects[key].setdefault("backlog", [])
        if any(_norm(b.get("title")) == _norm(it.get("title")) for b in backlog):
            it["status"] = "rejected"
            it["why_rejected"] = "такая задача уже есть в бэклоге"
            skipped.append("%s: такая задача в бэклоге уже есть" % it["id"])
            continue
        tid = next_task_id(key, backlog)
        task = {
            "id": tid, "title": it.get("title"), "size": it.get("size", "M"),
            "priority": it.get("priority", "🟡"), "status": "queued",
            "why": it.get("why", ""),
        }
        if it.get("root"):
            task["root"] = it["root"]
        if it.get("source"):
            task["source"] = it["source"]
        backlog.append(task)
        it["status"] = "applied"
        it["applied_to"] = "%s/%s" % (key, tid)
        applied.append("%s -> %s/%s  %s" % (it["id"], key, tid, it.get("title", "")[:70]))

    if applied:
        jsave(PROJECTS, proj_file)
        jsave(INBOX, box)
    for line in applied:
        print("  ✓ " + line)
    for line in skipped:
        print("  · " + line)
    if applied:
        print("\nПринято %d. Пересчитать оценку и дашборд: py tools/pm.py refresh --fast" % len(applied))
    return 0


def cmd_reject(args):
    box = jload(INBOX, {"items": []})
    ids = [a for a in args if not a.startswith("--")]
    why = _opt(args, "--why", "")
    n = 0
    for it in box.get("items", []):
        if it.get("id") in ids and it.get("status") == "new":
            it["status"] = "rejected"
            it["why_rejected"] = why or "отклонено вручную"
            n += 1
    if n:
        jsave(INBOX, box)
    print("Отклонено %d (записи не удалены — остаются следом решения)." % n)
    return 0


def _norm(s):
    return re.sub(r"[^\wа-яё ]+", "", (s or "").lower()).strip()


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


CMDS = {"list": cmd_list, "apply": cmd_apply, "reject": cmd_reject}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    c = sys.argv[1]
    if c not in CMDS:
        print("Неизвестная команда: %s" % c)
        return 1
    return CMDS[c](sys.argv[2:]) or 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
