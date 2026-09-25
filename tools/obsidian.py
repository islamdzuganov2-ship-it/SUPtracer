# -*- coding: utf-8 -*-
"""Обмен с хранилищем Obsidian — в обе стороны.

Внутрь (pull). Заметки хранилища читаются, разбираются приёмом (ingest.py) и ложатся
в память и во входящие. Повторный проход дешёвый: файл, который не изменился по хешу,
не разбирается заново.

Наружу (push). Портфель выкладывается в хранилище обычными заметками: страница
портфеля, страница на проект, дерево работ и записи памяти. Заметки связаны
викиссылками, поэтому граф Obsidian показывает ту же структуру, что и дашборд.

Обратная связь по задачам. В выложенной странице проекта бэклог — это чекбоксы.
Отметил задачу сделанной в Obsidian — при следующем pull статус задачи в
projects.json станет done. Это единственная запись приёма в ручной слой, и она
намеренная: галочку ставит человек, это такой же ввод, как pm.py comment.
Отключается флагом --no-status.

Защита от петли. Всё, что система выложила сама, помечено полем tasktreker
во фронтматтере и при чтении пропускается — иначе система разбирала бы
собственный вывод и бесконечно плодила записи.

  py tools/obsidian.py init <путь к хранилищу>
  py tools/obsidian.py status
  py tools/obsidian.py pull [--all] [--dry-run] [--no-status]
  py tools/obsidian.py push [--dry-run]
  py tools/obsidian.py sync            pull, затем push
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest
import mdparse as md
import memory as mem
import tree as tree_mod

ROOT =os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")   # ИНТ-01
CONF = os.path.join(DATA, "obsidian.json")
STATE = os.path.join(DATA, "vault_state.json")

MARK = "tasktreker"          # поле-метка «эту заметку выложила система»

DEFAULT_CONF = {
    "schema": 1,
    "vault": "",
    "import": {
        "folders": [],
        "tags": [],
        "ignore": [".obsidian", ".trash", "templates", "шаблоны", "archive", "архив"],
        # ПРМ-08: где лежат папки проектов. «Проекты/*» — каждая подпапка — проект;
        # имя без звёздочки — эта папка сама проект. Папка проекта, которого нет
        # в портфеле, уходит в «портфель», а не угадывается по словам.
        "project_folders": ["Проекты/*", "Projects/*"],
    },
    "export": {"enabled": True, "folder": "TaskTreker", "memory": True, "tree": True},
    "status_sync": True,
    "auto": {"pull_on_refresh": False, "push_on_refresh": False},
}


def jload(path, default=None):
    if not os.path.exists(path):
        return dict(default or {})
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(default or {})


def jsave(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def conf():
    c = jload(CONF, DEFAULT_CONF)
    for k, v in DEFAULT_CONF.items():
        c.setdefault(k, v)
        if isinstance(v, dict):
            for kk, vv in v.items():
                c[k].setdefault(kk, vv)
    return c


def vault_path(c=None):
    c = c or conf()
    v = (c.get("vault") or "").strip()
    return v if v and os.path.isdir(v) else ""


def export_root(c=None):
    c = c or conf()
    return os.path.join(vault_path(c), (c["export"].get("folder") or "TaskTreker"))


def safe_name(s):
    """Имя файла для Obsidian: кириллицу оставляем, запрещённые символы убираем."""
    s = re.sub(r"[\\/:*?\"<>|#\[\]^]", " ", str(s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:90] or "без имени")


# ------------------------------------------------------------------ чтение

def is_generated(path):
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(600)
    except (OSError, UnicodeDecodeError):
        return False
    fm, _ = md.parse_frontmatter(head if head.startswith("---") else "")
    return bool(fm.get(MARK))


def wanted(relpath, c):
    low = relpath.replace("\\", "/").lower()
    for ig in c["import"].get("ignore") or []:
        if ig and ig.lower() in low.split("/"):
            return False
    exp = (c["export"].get("folder") or "").lower()
    if exp and low.startswith(exp.lower() + "/"):
        return False
    folders = [f.strip("/").lower() for f in (c["import"].get("folders") or []) if f.strip()]
    if folders and not any(low == f + ".md" or low.startswith(f + "/") for f in folders):
        return False
    return True


def scan(c):
    """Заметки хранилища, которые нас касаются, с хешем содержимого."""
    root = vault_path(c)
    out = {}
    if not root:
        return out
    tags_filter = {t.lower().lstrip("#") for t in (c["import"].get("tags") or []) if t.strip()}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d.lower() not in ingest.SKIP_DIRS]
        for fn in filenames:
            if not fn.lower().endswith((".md", ".markdown")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if not wanted(rel, c) or is_generated(full):
                continue
            try:
                with open(full, encoding="utf-8") as f:
                    raw = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            if tags_filter:
                fm, body = md.parse_frontmatter(raw)
                if not (tags_filter & set(md.tags(body, fm))):
                    continue
            out[rel] = {"hash": md.digest_of(raw), "size": len(raw),
                        "mtime": int(os.path.getmtime(full))}
    return out


def cmd_pull(args):
    c = conf()
    root = vault_path(c)
    if not root:
        print("Хранилище не задано или не найдено. Сначала: py tools/pm.py vault init <путь>")
        return 1
    state = jload(STATE, {"files": {}})
    files = scan(c)
    known = state.get("files", {})
    changed = [r for r, v in files.items()
               if "--all" in args or known.get(r, {}).get("hash") != v["hash"]]
    gone = [r for r in known if r not in files]

    print("Хранилище: %s" % root)
    print("Заметок в области видимости: %d · изменилось: %d%s" % (
        len(files), len(changed), (" · исчезло: %d" % len(gone)) if gone else ""))

    status_changes = []
    if c.get("status_sync") and "--no-status" not in args:
        status_changes = sync_task_status(c, dry="--dry-run" in args)

    dry = "--dry-run" in args
    if not changed:
        print("Новых и изменившихся заметок нет.")
    else:
        order = sorted(changed)
        # Один пакетный приём: индекс памяти перестраивается один раз, а не на каждую заметку.
        reports = ingest.run_items(
            [(os.path.join(root, rel), rel, "obsidian:" + rel) for rel in order],
            dry=dry, quiet=True, project_folders=c["import"].get("project_folders"))
        for rel, r in zip(order, reports):
            if r.get("error"):
                print("  ! %s — %s" % (rel, r["error"]))
                continue
            extra = []
            if r.get("redacted"):
                extra.append("вычищено секретов %d" % len(r["redacted"]))
            if r.get("retired"):
                extra.append("устарело %d" % len(r["retired"]))
            print("  %s -> %s (%.0f%%) · записей %d · задач %d%s" % (
                rel, r.get("project"), (r.get("confidence") or 0) * 100,
                len(r.get("records", [])), len(r.get("proposals", [])),
                (" · " + ", ".join(extra)) if extra else ""))
            if not dry:
                known[rel] = dict(files[rel], ingested_at=mem.today(),
                                  project=r.get("project"),
                                  records=[x["id"] for x in r.get("records", [])])
        weak = [(rel, r) for rel, r in zip(order, reports)
                if not r.get("error") and (r.get("confidence") or 0) < 0.5]
        if weak:
            print("  Неуверенная раскладка (%d) — проверить проект вручную: %s" % (
                len(weak), ", ".join(rel for rel, _ in weak[:8])))
        leaky = [rel for rel, r in zip(order, reports) if r.get("redacted")]
        if leaky:
            print("  Ключи и пароли в заметках (%d) — в память не попали, из заметок их стоит убрать: %s" % (
                len(leaky), ", ".join(leaky[:8])))
        foreign = {}
        for r in reports:
            if r.get("foreign_project"):
                n, t = foreign.get(r["foreign_project"], (0, 0))
                foreign[r["foreign_project"]] = (n + 1, t + (r.get("foreign_tasks") or 0))
        if foreign:
            print("  Папки проектов, которых нет в портфеле, — заметки легли в «портфель», "
                  "задачи из них во входящие не попали: %s" % ", ".join(
                      "%s — %d%s" % (k, n, (" (задач %d)" % t) if t else "")
                      for k, (n, t) in sorted(foreign.items(), key=lambda kv: (-kv[1][0], kv[0]))))
            print("  Завести такой проект: добавить его в projects.json (или имя папки в aliases "
                  "существующего) и забрать заново: py tools/pm.py vault pull --all")

    # ПРМ-11: заметка удалена или переименована — её записи устаревают, файлы остаются.
    if gone and not dry:
        retired = ingest.retire_sources(["obsidian:" + rel for rel in gone],
                                        "заметка удалена или переименована в хранилище")
        for rel in gone:
            n = len(retired.get("obsidian:" + rel, []))
            print("  − %s — исчезла из хранилища, записей устарело: %d" % (rel, n))
            known.pop(rel, None)

    if status_changes:
        print("\nОтметки в Obsidian перенесены в бэклог:")
        for s in status_changes:
            print("  ✓ " + s)

    if "--dry-run" not in args:
        state["files"] = known
        state["pulled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        jsave(STATE, state)
    else:
        print("Пробный прогон: ничего не записано.")
    return 0


def sync_task_status(c, dry=False):
    """Галочки в выложенных страницах проектов -> статус задач в projects.json."""
    changes = []
    exp = export_root(c)
    pdir = os.path.join(exp, "Проекты")
    if not os.path.isdir(pdir):
        return changes
    pf = os.path.join(DATA, "projects.json")
    if not os.path.exists(pf):
        return changes
    with open(pf, encoding="utf-8") as f:
        data = json.load(f)
    projects = data.get("projects", {})
    touched = False
    for fn in sorted(os.listdir(pdir)):
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        fm, body = md.parse_frontmatter(raw)
        key = str(fm.get("project_key") or "")
        if key not in projects:
            continue
        marks = {}
        for t in md.tasks(body):
            m = re.match(r"^\*{0,2}([A-ZА-Я]{1,6}-\d+)\*{0,2}\s*[—:-]?\s*", t["text"])
            if m:
                marks[m.group(1)] = "done" if t["done"] else ("in-progress" if t["wip"] else None)
        for b in projects[key].get("backlog", []):
            want = marks.get(b.get("id"))
            if want and b.get("status") != want:
                changes.append("%s/%s: %s -> %s" % (key, b["id"], b.get("status", "—"), want))
                if not dry:
                    b["status"] = want
                    touched = True
    if touched and not dry:
        with open(pf, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return changes


# ------------------------------------------------------------------ запись

def load(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return {} if default is None else default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def write_note(path, fm, body, written, dry=False, ledger=None, copies=None):
    """Записать выгружаемую заметку. ledger — {путь: отпечаток того, что система
    записала сюда в прошлый раз}; по нему видно, правил ли файл человек (ОБС-07)."""
    # ОБС-08: обмен пишет только в свою папку. Заметки пользователя он читает,
    # но не меняет — даже если вызывающий код ошибся путём.
    own = os.path.abspath(export_root())
    if os.path.commonpath([own, os.path.abspath(path)]) != own:
        raise ValueError("выгрузка вне папки %s: %s" % (own, path))
    fm = dict(fm)
    fm[MARK] = "generated"
    fm.setdefault("updated", mem.today())
    text = md.dump_frontmatter(fm) + "\n" + body.rstrip() + "\n"
    rel = os.path.relpath(path, vault_path()).replace("\\", "/")
    if not dry:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = f.read()
            except OSError:
                old = None
        changed = old is None or _without_updated(old) != _without_updated(text)
        # ОБС-07: файл отличается от того, что система записала в прошлый раз, не только
        # галочками — значит, его правил человек. Правка не пропадает: копия рядом.
        if changed and old is not None and ledger is not None and ledger.get(rel) \
                and _print(old) != ledger[rel]:
            keep = _keep_copy(path, old)
            if copies is not None:
                copies.append(os.path.relpath(keep, vault_path()).replace("\\", "/"))
        # Перезаписываем только при изменении: иначе Obsidian считает заметку
        # тронутой при каждом обновлении и засоряет свою историю.
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            written.append(rel)
        if ledger is not None:
            ledger[rel] = _print(text)
        return changed
    written.append(rel)
    return False


def _without_updated(t):
    return re.sub(r"^updated:.*$", "", t or "", flags=re.M)


def _print(text):
    """Отпечаток заметки без даты обновления и без состояния галочек: галочки —
    законный ввод человека, их переносит в бэклог sync_task_status (ОБС-05)."""
    t = _without_updated(text).replace("\r\n", "\n")
    t = re.sub(r"^(\s*[-*+]\s+)\[[^\]]\]", r"\1[ ]", t, flags=re.M)
    return md.digest_of(t)


def _keep_copy(path, old):
    """Копия правленой заметки рядом с ней. Метка выгрузки снимается: если человек
    перенесёт копию из папки выгрузки, приём заметок прочитает её как обычную."""
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    stem = os.path.splitext(os.path.basename(path))[0]
    dest = os.path.join(os.path.dirname(path), "%s — правка %s.md" % (stem, stamp))
    fm, body = md.parse_frontmatter(old)
    fm.pop(MARK, None)
    fm["copy_of"] = stem
    note = ("> [!warning] Система перезаписала «%s» при выгрузке %s. Ваш текст сохранён здесь — "
            "перенесите нужное в свою заметку: папку выгрузки система считает своей.\n\n" % (stem, stamp))
    with open(dest, "w", encoding="utf-8") as f:
        f.write(md.dump_frontmatter(fm) + "\n" + note + body.lstrip())
    return dest


def cmd_push(args):
    c = conf()
    root = vault_path(c)
    if not root:
        print("Хранилище не задано. Сначала: py tools/pm.py vault init <путь>")
        return 1
    if not c["export"].get("enabled", True):
        print("Выгрузка выключена в data/obsidian.json (export.enabled=false).")
        return 0
    dry = "--dry-run" in args
    exp = export_root(c)
    # Галочки, поставленные в хранилище, сначала переносятся в бэклог (ОБС-05):
    # иначе выгрузка без забора стёрла бы их, перерисовав страницы проектов.
    ticks = sync_task_status(c, dry=dry) if c.get("status_sync") else []
    P = load("projects.json").get("projects", {})
    U = load("usage.json", {"projects": {}}).get("projects", {})
    R = load("repos.json", {"repos": {}}).get("repos", {})
    E = load("estimates.json", {"projects": {}}).get("projects", {})
    T = load("tree.json", {})
    idx = mem.load_index()
    by_id = {y["id"]: y for y in idx.get("records", [])}
    written, copies = [], []
    state = jload(STATE, {"files": {}})
    ledger = state.setdefault("pushed", {})           # ОБС-07: что система записала в прошлый раз

    def usage_for(k):
        path = (P[k].get("path") or "").lower().replace("\\", "/").rstrip("/")
        base = os.path.basename(path)
        for cand in (k, k.lower(), base, "doc:" + base, "doc:" + base.replace("_", "-")):
            if cand in U:
                return U[cand]
        return {}

    # --- страницы проектов
    names = {k: safe_name(P[k].get("name") or k) for k in P}
    for k, p in P.items():
        u, e, r = usage_for(k), E.get(k, {}), R.get(k, {})
        rd = p.get("readiness") or {}
        g = (r.get("git") or {})
        recs = [x for x in idx.get("records", []) if x["project"] == k and x["status"] == "активна"]
        fm = {
            "project_key": k, "title": p.get("name", k),
            "tags": ["tasktreker", "проект", md.slugify(k)],
            "status": p.get("status", ""), "tier": p.get("tier"),
            "готовность": rd.get("functional"), "иб": rd.get("security"),
            "потрачено_usd": round(u.get("usd", 0), 2) if u else 0,
            "осталось_usd": e.get("remaining_usd", 0),
            "коммитов": g.get("commits"),
        }
        body = ["> [!info] %s" % (p.get("tagline") or ""),
                "> Цель: %s" % (p.get("goal") or ""),
                "",
                "Часть портфеля [[Портфель TaskTreker]]. Страница собрана системой — "
                "правки в тексте затрутся при следующей выгрузке, кроме галочек в бэклоге.",
                "", "## Цель", (p.get("goal_long") or p.get("goal") or ""), "",
                "## Готовность", ""]
        for label, f in (("Функционально", "functional"), ("Эксплуатация", "production"),
                         ("Документация", "docs"), ("Тесты", "tests"), ("Безопасность", "security")):
            body.append("- %s: **%s%%**" % (label, rd.get(f, "—")))
        if rd.get("basis"):
            body += ["", "Основание: " + rd["basis"]]
        done = p.get("done") or []
        if done:
            body += ["", "## Что сделано — фундамент", ""] + ["- " + d for d in done]
        items = e.get("items", []) or p.get("backlog", [])
        if items:
            body += ["", "## Бэклог", "",
                     "Отметка галочкой здесь переносится в систему при `pm.py vault pull`.", ""]
            for b in items:
                mark = "x" if b.get("status") == "done" else ("/" if b.get("status") == "in-progress" else " ")
                extra = []
                if b.get("est_usd"):
                    extra.append("$%s" % round(b["est_usd"]))
                if b.get("size"):
                    extra.append(b["size"])
                body.append("- [%s] **%s** — %s%s" % (
                    mark, b.get("id"), b.get("title", ""),
                    (" _(%s)_" % ", ".join(extra)) if extra else ""))
                if b.get("why"):
                    body.append("    - %s" % b["why"])
        sec = p.get("security") or {}
        opens = (sec.get("open_critical") or []) + (sec.get("open_high") or []) + (sec.get("open") or [])
        if sec.get("verdict") or opens:
            body += ["", "## Безопасность", ""]
            if sec.get("verdict"):
                body.append("> [!warning] " + sec["verdict"])
            body += ["- " + s for s in opens]
        acts = p.get("next_actions") or []
        if acts:
            body += ["", "## Что делать дальше", ""] + ["- " + a for a in acts]
        if recs:
            # Корни — по одному на заметку или документ; разделы видны по ссылке.
            # Записи из самого хранилища ведут на исходную заметку, а не на копию.
            roots = sorted((x for x in recs if not x.get("parent")),
                           key=lambda x: (-mem.KIND_WEIGHT.get(x["kind"], 1), x["title"]))
            from_vault = sum(1 for x in recs if from_vault_source(x))
            body += ["", "## Память по проекту", "",
                     "Записей %d%s. Ниже — корни: запросы, документы и заметки, из которых "
                     "выросло остальное." % (len(recs), (", из них из заметок хранилища %d — ссылки "
                                                         "ведут на исходные заметки" % from_vault)
                                             if from_vault else ""), ""]
            for x in roots[:PAGE_ROOTS]:
                body.append("- `%s` %s" % (x["kind"], note_link(x)))
            if len(roots) > PAGE_ROOTS:
                body.append("- … ещё %d — [[Дерево работ]]" % (len(roots) - PAGE_ROOTS))
        body += ["", "---", "", "Собрано %s · `py tools/pm.py vault push`" % mem.today()]
        write_note(os.path.join(exp, "Проекты", names[k] + ".md"), fm, "\n".join(body), written, dry, ledger, copies)

    # --- записи памяти. Пришедшие из хранилища не выгружаются: их оригинал уже там,
    # копия раздвоила бы заметки и граф. Выгружается то, чего в хранилище нет, —
    # решения, запросы и документы, принятые из других мест.
    # ИНТ-08: это и граница со смысловым поиском — индексатор читает хранилище
    # (заметки и эту выгрузку с record_id во фронтматтере), а не data/ системы.
    if c["export"].get("memory", True):
        kids_of = {}
        for y in idx.get("records", []):
            if y.get("parent"):
                kids_of.setdefault(y["parent"], []).append(y)
        for x in idx.get("records", []):
            if from_vault_source(x):
                continue
            rec = mem.get(x["id"])
            if not rec:
                continue
            fm = {"title": rec["title"], "kind": rec["kind"], "project": rec["project"],
                  "record_id": rec["id"], "status": rec["status"],
                  "created": rec["created"], "source": rec.get("source", ""),
                  "tags": ["tasktreker", "память", md.slugify(rec["kind"])] + list(rec.get("tags") or [])}
            links = []
            if rec["project"] in names:
                links.append("Проект: [[%s]]" % names[rec["project"]])
            if rec.get("parent"):
                par = by_id.get(rec["parent"])
                if par:
                    links.append("Выросло из: %s" % note_link(par))
            kids = kids_of.get(rec["id"], [])
            if kids:
                links.append("Ветви: " + ", ".join(note_link(y) for y in kids))
            body = "\n".join(links + ["", rec.get("body") or "", "",
                                      "---", "", "Запись памяти TaskTreker · `%s`" % rec["id"]])
            write_note(os.path.join(exp, "Память", memory_note_name(x) + ".md"), fm, body, written, dry, ledger, copies)

    # --- дерево
    if c["export"].get("tree", True) and T.get("root"):
        lines = ["Дерево работ портфеля: из чего проекты выросли и куда растут.", "",
                 "Те же данные интерактивно — в дашборде (`py tools/pm.py serve`).", ""]

        def walk(n, d=0):
            if d > 6:
                return
            label = n.get("label", "")
            if n["kind"] in ("проект",) and label in names.values():
                label = "[[%s]]" % label
            elif n.get("memory"):
                rec = by_id.get(n["memory"])
                if rec:
                    label = note_link(rec)
            lines.append("%s- %s **%s**%s" % (
                "    " * d, _icon(n["kind"]), label,
                (" — %s" % n["detail"][:100]) if n.get("detail") else ""))
            # ДРВ-07: у широкого узла — первые записи памяти, остальное одной строкой.
            shown = 0
            for ch in n["children"]:
                if ch.get("memory"):
                    shown += 1
                    if shown > tree_mod.WIDE:
                        continue
                walk(ch, d + 1)
            if shown > tree_mod.WIDE:
                lines.append("%s- … ещё %d записей — в дашборде или `py tools/pm.py recall <слова>`" % (
                    "    " * (d + 1), shown - tree_mod.WIDE))
        for ch in T["root"]["children"]:
            walk(ch, 0)
        st = T.get("stats", {})
        fm = {"title": "Дерево работ", "tags": ["tasktreker", "дерево"],
              "узлов": st.get("nodes"), "глубина": st.get("depth")}
        write_note(os.path.join(exp, "Дерево работ.md"), fm, "\n".join(lines), written, dry, ledger, copies)

    # --- портфель
    spent = sum(usage_for(k).get("usd", 0) for k in P)
    rem = sum(E.get(k, {}).get("remaining_usd", 0) for k in P)
    body = ["Сводка портфеля. Собирается системой TaskTreker.", "",
            "- Проектов: **%d**" % len(P),
            "- Потрачено: **$%s**" % round(spent),
            "- Осталось по бэклогу: **$%s**" % round(rem),
            "- Записей памяти: **%d**" % len(idx.get("records", [])),
            "", "## Проекты", "",
            "| Проект | Статус | Готовность | ИБ | Потрачено | Осталось |",
            "| --- | --- | ---: | ---: | ---: | ---: |"]
    for k in sorted(P, key=lambda k: (P[k].get("tier", 9), k)):
        rd = P[k].get("readiness") or {}
        body.append("| [[%s]] | %s | %s%% | %s%% | $%s | $%s |" % (
            names[k], P[k].get("status", ""), rd.get("functional", "—"), rd.get("security", "—"),
            round(usage_for(k).get("usd", 0)), round(E.get(k, {}).get("remaining_usd", 0))))
    body += ["", "## Дерево", "", "- [[Дерево работ]]", "",
             "## Как это обновляется", "",
             "- `py tools/pm.py vault push` — выложить свежий портфель сюда",
             "- `py tools/pm.py vault pull` — забрать заметки и галочки обратно в систему"]
    write_note(os.path.join(exp, "Портфель TaskTreker.md"),
               {"title": "Портфель TaskTreker", "tags": ["tasktreker", "портфель"]},
               "\n".join(body), written, dry, ledger, copies)

    if not dry:                                   # ОБН-05: свежесть выгрузки видна на дашборде
        state["pushed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        jsave(STATE, state)
    print("Хранилище: %s" % root)
    if ticks:
        print("Отметки в Obsidian перенесены в бэклог до выгрузки:")
        for t in ticks:
            print("  ✓ " + t)
    if copies:
        print("Правки в выгруженных заметках (%d) — страницы перерисованы, ваш текст сохранён копиями:" % len(copies))
        for cp in copies:
            print("  ! " + cp)
    print("Записано заметок: %d%s" % (len(written), " (пробный прогон)" if dry else ""))
    for w in written[:20]:
        print("  " + w)
    if len(written) > 20:
        print("  … ещё %d" % (len(written) - 20))
    if not written:
        print("  всё уже актуально")
    return 0


def _icon(kind):
    return {"проект": "🌳", "запрос": "🌱", "решение": "✅", "требование": "📐", "риск": "⚠️",
            "итог": "🏁", "факт": "📊", "фундамент": "🧱", "сделано": "🧱", "бэклог": "🌿",
            "задача": "🌿", "почки": "🔵", "предложение": "🔵", "коммит": "🔸"}.get(kind, "·")


def memory_note_name(rec):
    return safe_name("%s — %s" % (rec.get("kind", "заметка"), rec.get("title", rec.get("id"))))


PAGE_ROOTS = 60          # сколько корней памяти перечислять на странице проекта


def from_vault_source(rec):
    return str(rec.get("source") or "").startswith("obsidian:")


def note_link(rec):
    """Викиссылка на запись: на исходную заметку хранилища с разделом, если запись
    пришла оттуда, иначе на выгруженную заметку памяти."""
    src = str(rec.get("source") or "")
    if src.startswith("obsidian:"):
        path, _, section = src[len("obsidian:"):].partition("#")
        path = re.sub(r"\.(md|markdown)$", "", path, flags=re.I)
        alias = re.sub(r"[\[\]|#^]", " ", rec.get("title") or "").strip()
        section = re.sub(r"[\[\]|#^]", " ", section).strip()
        return "[[%s%s|%s]]" % (path, ("#" + section) if section else "", alias or path)
    return "[[%s]]" % memory_note_name(rec)


# ------------------------------------------------------------------ прочее

def cmd_init(args):
    path = next((a for a in args if not a.startswith("--")), None)
    if not path:
        print("Укажите путь к хранилищу: py tools/pm.py vault init \"C:/Users/…/Мой Vault\"")
        return 1
    path = os.path.abspath(path).replace("\\", "/")
    if not os.path.isdir(path):
        print("Каталога нет: %s" % path)
        return 1
    c = conf()
    c["vault"] = path
    if not os.path.isdir(os.path.join(path, ".obsidian")):
        print("Предупреждение: в каталоге нет .obsidian — это точно хранилище?")
    jsave(CONF, c)
    print("Хранилище записано в data/obsidian.json: %s" % path)
    print("Что читать и что выкладывать, настраивается там же (import.folders, import.tags, "
          "import.project_folders, export.folder).")
    print("Дальше: py tools/pm.py vault pull · py tools/pm.py vault push")
    return 0


def cmd_status(args):
    c = conf()
    root = vault_path(c)
    if not root:
        print("Хранилище не подключено. py tools/pm.py vault init <путь>")
        return 0
    state = jload(STATE, {"files": {}})
    files = scan(c)
    known = state.get("files", {})
    new = [r for r in files if r not in known]
    upd = [r for r, v in files.items() if r in known and known[r].get("hash") != v["hash"]]
    gone = [r for r in known if r not in files]
    print("Хранилище: %s" % root)
    print("В области видимости заметок: %d" % len(files))
    print("  новых: %d · изменившихся: %d · исчезнувших: %d" % (len(new), len(upd), len(gone)))
    for r in (new + upd)[:15]:
        print("    %s" % r)
    print("Последний pull: %s" % (state.get("pulled_at") or "не было"))
    exp = export_root(c)
    n = sum(len(fs) for _, _, fs in os.walk(exp)) if os.path.isdir(exp) else 0
    print("Выложено системой в «%s»: %d файлов" % (c["export"].get("folder"), n))
    pend = sync_task_status(c, dry=True)
    if pend:
        print("Галочек, не перенесённых в бэклог: %d" % len(pend))
        for p in pend[:10]:
            print("    " + p)
    return 0


def cmd_sync(args):
    rc = cmd_pull(args)
    print()
    return cmd_push(args) or rc


CMDS = {"init": cmd_init, "pull": cmd_pull, "push": cmd_push, "status": cmd_status, "sync": cmd_sync}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    c = sys.argv[1]
    if c not in CMDS:
        print("Неизвестная команда: %s. Есть: %s" % (c, ", ".join(CMDS)))
        return 1
    return CMDS[c](sys.argv[2:]) or 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
