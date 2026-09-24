# -*- coding: utf-8 -*-
"""Память портфеля — хранилище знания вне контекста ассистента.

Смысл модуля. Раньше всё, что ассистент знает о проектах, жило в его окне контекста:
каждая сессия заново читала большие файлы, платила за запись кеша и за его чтение,
а между сессиями знание терялось. Здесь знание лежит на диске записями постоянного
размера, а ассистент получает не их, а указатель: короткий брифинг из заголовков
и идентификаторов. Тело записи подтягивается по требованию — recall.

Формат записи — Markdown с фронтматтером, то есть родной формат Obsidian.
Это не совпадение: хранилище и хранилище заметок пользователя должны быть одним
и тем же форматом, иначе интеграция превращается в конвертацию.

  data/memory/<id>.md      запись
  data/memory/index.json   производный индекс (собирается, руками не правится)

  py tools/memory.py index
  py tools/memory.py add --project X --kind факт --title "..." --text "..."
  py tools/memory.py brief [--budget 1500] [--project X]
  py tools/memory.py recall <id|слова> [--full] [--budget 4000]
  py tools/memory.py forget <id> --why "почему устарела"
  py tools/memory.py stats
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mdparse as md

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MEM = os.path.join(DATA, "memory")
INDEX = os.path.join(MEM, "index.json")

KINDS = ("запрос", "решение", "факт", "требование", "риск", "итог", "источник", "заметка")
STATUSES = ("активна", "устарела", "заменена")

# Сколько записей одного вида попадает в брифинг в первую очередь. Запрос — это
# корень дерева работ, он нужен всегда; заметка полезна, но подождёт до recall.
KIND_WEIGHT = {"запрос": 5.0, "решение": 4.0, "требование": 3.5, "риск": 3.5,
               "итог": 3.0, "факт": 2.5, "источник": 1.5, "заметка": 1.0}


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dir():
    os.makedirs(MEM, exist_ok=True)


# ------------------------------------------------------------------ запись

def make_id(title, project="", salt=""):
    """Идентификатор стабилен по смыслу записи: один и тот же заголовок в одном
    проекте даёт один id, поэтому повторный приём того же файла не плодит дубли."""
    base = md.slugify(title, 26) or "zapis"
    h = md.digest_of("%s|%s|%s" % (project, title.strip().lower(), salt))[:6]
    return "mem-%s-%s" % (base, h)


def record_path(rid):
    return os.path.join(MEM, rid + ".md")


def new_record(title, body, project="портфель", kind="заметка", **extra):
    rid = extra.pop("id", None) or make_id(title, project, extra.get("source", ""))
    rec = {
        "id": rid,
        "kind": kind if kind in KINDS else "заметка",
        "project": project or "портфель",
        "title": (title or "").strip()[:200],
        "status": extra.pop("status", "активна"),
        "created": extra.pop("created", today()),
        "updated": today(),
        "tags": sorted(set(extra.pop("tags", []) or [])),
        "links": list(dict.fromkeys(extra.pop("links", []) or [])),
        "parent": extra.pop("parent", "") or "",
        "root": extra.pop("root", "") or "",
        "source": extra.pop("source", "") or "",
        "confidence": extra.pop("confidence", "") or "",
        "pin": bool(extra.pop("pin", False)),
        "body": (body or "").strip(),
    }
    for k, v in extra.items():                 # незнакомые поля не теряем
        if v not in ("", None, [], {}):
            rec[k] = v
    return rec


def dump_record(rec):
    fm = {k: rec.get(k) for k in ("id", "kind", "project", "title", "status", "created",
                                  "updated", "tags", "links", "parent", "root", "source",
                                  "confidence", "pin")}
    for k, v in rec.items():
        if k not in fm and k != "body":
            fm[k] = v
    return md.dump_frontmatter({k: v for k, v in fm.items() if v not in ("", None, [], {}, False)}) \
        + "\n" + (rec.get("body") or "").strip() + "\n"


def save(rec):
    ensure_dir()
    p = record_path(rec["id"])
    old = read(p) if os.path.exists(p) else None
    if old:
        rec.setdefault("created", old.get("created", today()))
        if old.get("body", "").strip() == (rec.get("body") or "").strip() and \
           old.get("title") == rec.get("title"):
            rec["updated"] = old.get("updated", today())
    with open(p, "w", encoding="utf-8") as f:
        f.write(dump_record(rec))
    return p


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    fm, body = md.parse_frontmatter(raw)
    rid = fm.get("id") or os.path.splitext(os.path.basename(path))[0]
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    links = fm.get("links") or []
    if isinstance(links, str):
        links = [links]
    links = list(dict.fromkeys(list(links) + md.wikilinks(body)))
    rec = dict(fm)
    rec.update({
        "id": rid,
        "kind": fm.get("kind", "заметка"),
        "project": fm.get("project", "портфель"),
        "title": fm.get("title") or (md.sections(body)[0]["title"] if md.sections(body) else rid),
        "status": fm.get("status", "активна"),
        "created": str(fm.get("created", ""))[:10] or today(),
        "updated": str(fm.get("updated", ""))[:10] or today(),
        "tags": [str(t).lower() for t in tags],
        "links": [str(x) for x in links],
        "parent": str(fm.get("parent", "") or ""),
        "root": str(fm.get("root", "") or ""),
        "source": str(fm.get("source", "") or ""),
        "pin": bool(fm.get("pin", False)),
        "body": body.strip(),
        "path": path,
    })
    return rec


def load_all(include_stale=True):
    ensure_dir()
    out = []
    for fn in sorted(os.listdir(MEM)):
        if not fn.endswith(".md"):
            continue
        r = read(os.path.join(MEM, fn))
        if r and (include_stale or r.get("status") == "активна"):
            out.append(r)
    return out


# ------------------------------------------------------------------ индекс

def build_index(records=None):
    """Индекс — то, что читают все остальные модули. В нём нет тел записей:
    он должен оставаться дешёвым даже когда записей тысячи."""
    records = records if records is not None else load_all()
    items, by_project, by_kind = [], {}, {}
    stored_tokens = 0
    for r in records:
        t = md.est_tokens(r["body"])
        stored_tokens += t
        digest = md.first_paragraph(r["body"], 220)
        items.append({
            "id": r["id"], "project": r["project"], "kind": r["kind"], "title": r["title"],
            "status": r["status"], "created": r["created"], "updated": r["updated"],
            "tags": r["tags"], "links": r["links"], "parent": r["parent"], "root": r["root"],
            "source": r["source"], "pin": r["pin"],
            "chars": len(r["body"]), "tokens": t, "digest": digest,
            "keywords": md.keywords(r["title"] + " " + r["body"], 14),
            "file": os.path.relpath(r.get("path") or record_path(r["id"]), ROOT).replace("\\", "/"),
        })
        by_project[r["project"]] = by_project.get(r["project"], 0) + 1
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    items.sort(key=lambda x: (x["project"], -KIND_WEIGHT.get(x["kind"], 1), x["updated"]), reverse=False)
    idx = {
        "schema": 1,
        "generated_at": now_iso(),
        "totals": {
            "records": len(items),
            "active": sum(1 for i in items if i["status"] == "активна"),
            "stored_tokens": stored_tokens,
            "index_tokens": 0,
            "by_project": by_project,
            "by_kind": by_kind,
        },
        "records": items,
    }
    idx["totals"]["index_tokens"] = md.est_tokens(json.dumps(idx, ensure_ascii=False))
    ensure_dir()
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    return idx


def load_index(rebuild_if_missing=True):
    if os.path.exists(INDEX):
        try:
            with open(INDEX, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return build_index() if rebuild_if_missing else {"records": [], "totals": {}}


# ------------------------------------------------------------------ поиск

def score(item, query_words, project=None):
    if not query_words and not project:
        return 0.0
    hay = set(item.get("keywords") or []) | {md.stem(w) for w in re.findall(r"\w{3,}", item["title"].lower())}
    hay |= {md.stem(t) for t in item.get("tags") or []}
    s = len(hay & query_words) * 3.0
    if query_words and query_words <= hay:      # запрос целиком укладывается в запись
        s += 2.0
    if project and item["project"] == project:
        s += 2.5
    s += KIND_WEIGHT.get(item["kind"], 1.0) * 0.2
    if item.get("pin"):
        s += 4.0
    if item.get("status") != "активна":
        s -= 3.0
    return s


def search(query, project=None, limit=12, index=None):
    idx = index or load_index()
    qw = md.word_set(query) if query else set()
    direct = [i for i in idx["records"] if i["id"] == (query or "").strip()]
    if direct:
        return direct
    scored = [(score(i, qw, project), i) for i in idx["records"]]
    scored = [(s, i) for s, i in scored if s > 1.5]
    scored.sort(key=lambda si: (-si[0], si[1]["updated"]))
    return [i for _, i in scored[:limit]]


def get(rid):
    p = record_path(rid)
    if os.path.exists(p):
        return read(p)
    for r in load_all():
        if r["id"] == rid or md.slugify(r["title"]) == md.slugify(rid):
            return r
    return None


# ------------------------------------------------------------------ брифинг

def brief(budget=1500, project=None, index=None, kinds=None):
    """Упаковка памяти в бюджет токенов: заголовки и идентификаторы, без тел.

    Это и есть «чтение из системы вместо чтения в контекст»: ассистент видит,
    что в памяти есть, и берёт целиком только то, что нужно сейчас.
    """
    idx = index or load_index()
    recs = [r for r in idx["records"] if r["status"] == "активна"]
    if project:
        recs = [r for r in recs if r["project"] == project]
    if kinds:
        recs = [r for r in recs if r["kind"] in kinds]
    recs.sort(key=lambda r: (-(10.0 if r["pin"] else 0) - KIND_WEIGHT.get(r["kind"], 1.0),
                             r["project"], r["updated"]))

    groups, lines, used = {}, [], 0
    for r in recs:
        groups.setdefault(r["project"], []).append(r)

    skipped = 0
    for proj in sorted(groups, key=lambda p: (-len(groups[p]), p)):
        head = "## %s — %d записей" % (proj, len(groups[proj]))
        block = [head]
        for r in groups[proj]:
            line = "  [%s] %-9s %s" % (r["id"], r["kind"], r["title"])
            if r["digest"] and len(line) < 110:
                line += " — " + r["digest"][:110]
            cost = md.est_tokens(line)
            if used + cost > budget:
                skipped += 1
                continue
            used += cost
            block.append(line)
        if len(block) > 1:
            lines += block
    total = idx["totals"].get("stored_tokens", 0)
    header = ("ПАМЯТЬ TASKTREKER · записей %d · на диске ~%s токенов · в брифинге ~%s\n"
              "Тела записей не загружены. Прочитать целиком: py tools/pm.py recall <id>" % (
                  len(recs), fmt(total), fmt(used)))
    if skipped:
        header += "\nНе поместилось в бюджет: %d записей — py tools/pm.py recall <слова>" % skipped
    return header + "\n\n" + "\n".join(lines) if lines else header + "\n\n(память пуста)"


def fmt(n):
    try:
        return "{:,}".format(int(n)).replace(",", " ")
    except Exception:
        return str(n)


def render(rec, budget=None):
    """Запись в текст для чтения ассистентом — без JSON-обвязки, она стоит токенов."""
    head = "[%s] %s · %s · проект %s · обновлено %s" % (
        rec["id"], rec["title"], rec["kind"], rec["project"], rec["updated"])
    meta = []
    if rec.get("tags"):
        meta.append("теги: " + ", ".join(rec["tags"]))
    if rec.get("root"):
        meta.append("корень: " + rec["root"])
    if rec.get("parent"):
        meta.append("родитель: " + rec["parent"])
    if rec.get("links"):
        meta.append("связи: " + ", ".join(rec["links"][:8]))
    if rec.get("source"):
        meta.append("источник: " + rec["source"])
    body = rec.get("body") or ""
    if budget:
        while md.est_tokens(body) > budget and len(body) > 400:
            body = body[: int(len(body) * 0.8)].rsplit("\n", 1)[0] + "\n…(обрезано по бюджету)"
    return head + ("\n" + " · ".join(meta) if meta else "") + "\n\n" + body


# ------------------------------------------------------------------ CLI

def cmd_index(args):
    idx = build_index()
    t = idx["totals"]
    print("записей %d (активных %d) · на диске ~%s токенов · индекс ~%s токенов" % (
        t["records"], t["active"], fmt(t["stored_tokens"]), fmt(t["index_tokens"])))
    print("-> " + os.path.relpath(INDEX, ROOT))
    return 0


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def cmd_add(args):
    title = _opt(args, "--title")
    text = _opt(args, "--text", "")
    if not title:
        print('Нужен --title. Пример: memory.py add --project tasktreker --kind факт --title "..." --text "..."')
        return 1
    tags = [t.strip() for t in (_opt(args, "--tags", "") or "").split(",") if t.strip()]
    rec = new_record(title, text, project=_opt(args, "--project", "портфель"),
                     kind=_opt(args, "--kind", "заметка"), tags=tags,
                     source=_opt(args, "--source", "cli"), parent=_opt(args, "--parent", ""),
                     root=_opt(args, "--root", ""), pin="--pin" in args)
    p = save(rec)
    build_index()
    print("записано %s -> %s" % (rec["id"], os.path.relpath(p, ROOT)))
    return 0


def cmd_brief(args):
    print(brief(budget=int(_opt(args, "--budget", 1500)), project=_opt(args, "--project")))
    return 0


def cmd_recall(args):
    q = " ".join(a for a in args if not a.startswith("--"))
    budget = int(_opt(args, "--budget", 4000))
    if not q:
        print("Что вспомнить? py tools/pm.py recall <id|слова>")
        return 1
    rec = get(q.strip())
    if rec:
        print(render(rec, budget))
        return 0
    hits = search(q, limit=int(_opt(args, "--limit", 6)))
    if not hits:
        print("Ничего не найдено по «%s». Индекс: py tools/pm.py refresh" % q)
        return 1
    if "--full" in args:
        left = budget
        for h in hits:
            r = get(h["id"])
            if not r:
                continue
            chunk = render(r, max(300, left // max(1, len(hits))))
            left -= md.est_tokens(chunk)
            print(chunk + "\n" + "-" * 60)
            if left <= 0:
                print("(бюджет исчерпан)")
                break
        return 0
    print("Найдено %d — прочитать целиком: py tools/pm.py recall <id>\n" % len(hits))
    for h in hits:
        print("[%s] %-9s %s\n    %s" % (h["id"], h["kind"], h["title"], h["digest"][:150]))
    return 0


def cmd_forget(args):
    rid = next((a for a in args if not a.startswith("--")), None)
    if not rid:
        print("Что забыть? memory.py forget <id> --why «почему»")
        return 1
    rec = get(rid)
    if not rec:
        print("нет записи %s" % rid)
        return 1
    rec["status"] = "устарела"
    rec["updated"] = today()
    why = _opt(args, "--why", "")
    if why:
        rec["body"] = (rec.get("body") or "") + "\n\n> Устарела %s: %s" % (today(), why)
    save(rec)
    build_index()
    print("%s помечена устаревшей (файл не удалён — история важна)" % rec["id"])
    return 0


def cmd_stats(args):
    idx = load_index()
    t = idx["totals"]
    print("записей %d, активных %d" % (t.get("records", 0), t.get("active", 0)))
    print("на диске ~%s токенов, индекс ~%s токенов" % (
        fmt(t.get("stored_tokens", 0)), fmt(t.get("index_tokens", 0))))
    b = md.est_tokens(brief(1500, index=idx))
    saved = max(0, t.get("stored_tokens", 0) - b)
    print("брифинг ~%s токенов -> не читается в контекст ~%s токенов" % (fmt(b), fmt(saved)))
    for k, v in sorted(t.get("by_kind", {}).items(), key=lambda kv: -kv[1]):
        print("  %-10s %d" % (k, v))
    return 0


CMDS = {"index": cmd_index, "add": cmd_add, "brief": cmd_brief, "recall": cmd_recall,
        "forget": cmd_forget, "stats": cmd_stats}


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
