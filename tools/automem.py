# -*- coding: utf-8 -*-
"""Импорт авто-памяти ассистента в память портфеля (ИНТ-07).

Claude Code ведёт свою память сам: ~/.claude/projects/<каталог>/memory/*.md —
по файлу на факт, во фронтматтере name, description и metadata.type (user,
feedback, project, reference). Эта память видна только в сессиях своего каталога
и живёт отдельно от системы. Импорт делает её частью памяти портфеля: запись
на файл, в проект по каталогу сессии, вид по типу, всё под одним корнем
«Авто-память ассистента» в дереве проекта.

  py tools/automem.py [--dry-run] [--root <каталог projects>]

Файлы авто-памяти только читаются. Повторный импорт не дублирует: идентификатор
записи зависит от проекта и файла. Файл, исчезнувший из авто-памяти, делает
запись «устаревшей» — файл записи остаётся (ПАМ-09).
"""
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest
import mdparse as md
import memory as mem
from project_routes import resolve_slug

PROJECTS_ROOT = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")
PREFIX = "claude-memory:"

# Тип авто-памяти -> вид записи. Отзыв о работе — требование к тому, как работать;
# сведения о пользователе и проекте — факты; ссылки — источники.
KIND_BY_TYPE = {"feedback": "требование", "user": "факт", "project": "факт", "reference": "источник"}


def slug(path):
    """Имя каталога, под которым Claude Code хранит проект: всё, кроме латиницы и цифр, — дефис."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path or "").replace("/", "\\").rstrip("\\"))


def project_for_slug(name, projects):
    """Проект по имени каталога авто-памяти. Рабочее дерево проекта
    (…--claude-worktrees-…) — тот же проект; из нескольких подходящих — самый длинный путь."""
    return resolve_slug(name, projects) or "портфель"


def title_of(fm, body, fname):
    t = str(fm.get("description") or "").strip()
    if not t:
        t = next((l.lstrip("# ").strip() for l in body.splitlines() if l.startswith("#")), "")
    t = t or str(fm.get("name") or "").strip() or os.path.splitext(fname)[0]
    if len(t) > 160:
        t = t[:160].rsplit(" ", 1)[0] + "…"
    return t


def collect(root=None, projects=None):
    """Файлы авто-памяти -> [(каталог, проект, путь, фронтматтер, тело)]."""
    root = root or PROJECTS_ROOT
    projects = projects if projects is not None else ingest.load_projects()
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        mdir = os.path.join(root, name, "memory")
        if not os.path.isdir(mdir):
            continue
        proj = project_for_slug(name, projects)
        for fn in sorted(os.listdir(mdir)):
            if not fn.lower().endswith(".md") or fn.upper() == "MEMORY.MD":
                continue                  # MEMORY.md — оглавление остальных файлов, не факт
            path = os.path.join(mdir, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    raw = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            out.append((name, proj, path, raw))
    return out


def run(root=None, dry=False, projects=None, quiet=False):
    """Импорт. Возвращает отчёт: {проект: {"records": n, "secrets": [файлы]}, "retired": n}."""
    projects = projects if projects is not None else ingest.load_projects()
    items = collect(root, projects)
    by_source = {r.get('source'): r for r in mem.load_all() if r.get('source', '').startswith(PREFIX)}
    groups, records, leaky = {}, [], []
    for name, proj, path, raw in items:
        raw, found = md.redact_secrets(raw)        # БЕЗ-05: логины и ключи в память не идут
        fm, body = md.parse_frontmatter(raw)
        fname = os.path.basename(path)
        if found:
            leaky.append("%s/%s" % (name, fname))
        if name not in groups:
            pname = (projects.get(proj) or {}).get("name") or proj
            gtitle = "Авто-память ассистента: %s" % pname
            gsrc = PREFIX + name
            groups[name] = mem.new_record(
                gtitle,
                "Память, которую Claude Code вёл сам в сессиях каталога `%s` "
                "(~/.claude/projects/%s/memory). Система импортирует её, чтобы знание было "
                "в одном месте и попадало в брифинг проекта; сами файлы авто-памяти не меняются."
                % (name, name),
                project=proj, kind="источник", tags=["авто-память"], source=gsrc,
                id=(by_source.get(gsrc) or {}).get('id') or mem.make_id(gtitle, proj, gsrc))
        grp = groups[name]
        mtype = str(fm.get("type") or "").strip().lower()
        created = str(fm.get("modified") or "")[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}$", created):
            created = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).strftime("%Y-%m-%d")
        title = title_of(fm, body, fname)
        src = "%s%s/%s" % (PREFIX, name, fname)
        rec = mem.new_record(
            title, body, project=proj, kind=KIND_BY_TYPE.get(mtype, "заметка"),
            tags=["авто-память"] + ([mtype] if mtype else []), source=src, created=created,
            parent=grp["id"], root=grp["id"], id=(by_source.get(src) or {}).get('id') or mem.make_id(title, proj, src))
        rec["automem_type"] = mtype
        records.append(rec)
        grp["created"] = min(grp["created"], created)

    # Исчезнувшие файлы: их записи устаревают. Снимок — до записи новых.
    prior = {s: ids for s, ids in ingest.active_by_source().items() if s.startswith(PREFIX)}
    current = {r["source"] for r in records} | {g["source"] for g in groups.values()}
    gone = sorted(s for s in prior if s not in current)

    report = {}
    for r in records:
        report.setdefault(r["project"], {"records": 0})["records"] += 1
    if not dry:
        for rec in list(groups.values()) + records:
            old = mem.get(rec["id"]) if os.path.exists(mem.record_path(rec["id"])) else None
            if old and old.get("status") != "активна" and old.get("retired") == "вручную":
                continue                  # выведенное человеком повторный импорт не оживляет
            if old:
                rec["created"] = old.get("created") or rec["created"]
            rec['import_digest'] = md.digest_of(rec['body'])
            rec['import_title'] = rec['title']
            if old:
                if old.get('import_digest') and old['import_digest'] != md.digest_of(old['body']):
                    rec['body'] = old['body']
                    rec['import_digest'] = old['import_digest']
                if old.get('import_title') and old['title'] != old['import_title']:
                    rec['title'] = old['title']
                    rec['import_title'] = old['import_title']
                tags = sorted(set(old.get('tags',[])) | set(rec.get('tags',[])))
                rec = dict(old, **rec)
                rec['tags'] = tags
            mem.save(rec)
        retired = ingest.retire_sources(gone, "файла больше нет в авто-памяти ассистента") if gone else {}
        mem.build_index()
    else:
        retired = {s: prior[s] for s in gone}

    if not quiet:
        for proj in sorted(report):
            print("  %-18s записей %d" % (proj, report[proj]["records"]))
        n_ret = sum(len(v) for v in retired.values())
        print("Файлов авто-памяти: %d · каталогов: %d%s" % (
            len(records), len(groups), (" · устарело записей: %d" % n_ret) if n_ret else ""))
        if leaky:
            print("Логины, пароли или ключи в авто-памяти (%d) — в систему не попали: %s" % (
                len(leaky), ", ".join(leaky[:6])))
        if dry:
            print("Пробный прогон: ничего не записано.")
    return {"projects": report, "files": len(records), "groups": len(groups),
            "retired": sum(len(v) for v in retired.values()), "redacted": leaky}


def main():
    args = sys.argv[1:]
    root = None
    if "--root" in args:
        i = args.index("--root")
        root = args[i + 1] if i + 1 < len(args) else None
    run(root, dry="--dry-run" in args)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
