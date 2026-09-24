# -*- coding: utf-8 -*-
"""Проверки перед слиянием в тестирование и main.

Тестов в привычном смысле у системы учёта быть не может — она считает факты
о внешнем мире. Поэтому проверяется другое: целостность данных, согласованность
ручного и автоматического слоёв, и то, что дашборд действительно собирается.

  py tools/check.py          все проверки
  py tools/check.py --quiet  только провалы

Код возврата 0 — всё зелено, 1 — есть провалы.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")   # ИНТ-01

OK, FAIL, WARN = [], [], []


def check(name):
    def deco(fn):
        fn._name = name
        return fn
    return deco


class Missing(Exception):
    """Файла данных нет — проверка сообщает об этом, а не падает трассировкой."""


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        raise Missing("нет data/%s — запустите py tools/pm.py refresh" % name)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def usage_key(projects, usage, key):
    path = (projects[key].get("path") or "").lower().rstrip("/")
    base = os.path.basename(path)
    for c in (key, key.lower(), base, "doc:" + base, "doc:" + base.replace("_", "-")):
        if c in usage:
            return c
    return None


# --------------------------------------------------------------------------

@check("JSON-файлы читаются")
def t_json_parses():
    bad = []
    for fn in sorted(os.listdir(DATA)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(DATA, fn), encoding="utf-8") as f:
                json.load(f)
        except Exception as e:
            bad.append("%s: %s" % (fn, e))
    return bad


@check("у каждого проекта заполнены обязательные поля")
def t_required_fields():
    P = load("projects.json")["projects"]
    need = ("name", "tagline", "path", "tier", "status", "goal", "goal_long",
            "llm", "compute", "architecture", "done", "readiness", "security", "assessment")
    bad = []
    for k, p in P.items():
        missing = [f for f in need if f not in p or p[f] in ("", None, [], {})]
        if missing:
            bad.append("%s: нет %s" % (k, ", ".join(missing)))
    return bad


@check("readiness: пять цифр в диапазоне и непустой basis")
def t_readiness():
    P = load("projects.json")["projects"]
    bad = []
    for k, p in P.items():
        r = p.get("readiness", {})
        for f in ("functional", "production", "docs", "tests", "security"):
            v = r.get(f)
            if not isinstance(v, int) or not 0 <= v <= 100:
                bad.append("%s.%s = %r (нужно целое 0..100)" % (k, f, v))
        if not (r.get("basis") or "").strip():
            bad.append("%s: пустой readiness.basis — значит цифра ничем не обоснована" % k)
    return bad


@check("бэклог: у задач есть id, title и допустимый размер")
def t_backlog():
    P = load("projects.json")["projects"]
    sizes = set(load("projects.json")["size_calibration"])
    bad = []
    seen = {}
    for k, p in P.items():
        for b in p.get("backlog", []):
            bid = b.get("id")
            if not bid:
                bad.append("%s: задача без id — %r" % (k, b.get("title", "")[:40]))
                continue
            if not (b.get("title") or "").strip():
                bad.append("%s/%s: пустой title" % (k, bid))
            if b.get("size") not in sizes:
                bad.append("%s/%s: размер %r вне %s" % (k, bid, b.get("size"), sorted(sizes)))
            dup = seen.setdefault((k, bid), 0)
            seen[(k, bid)] = dup + 1
            if dup:
                bad.append("%s: id %s встречается дважды" % (k, bid))
    return bad


@check("пути проектов существуют на диске")
def t_paths():
    P = load("projects.json")["projects"]
    return ["%s: нет каталога %s" % (k, p["path"]) for k, p in P.items()
            if not os.path.isdir(p.get("path", ""))]


@check("расход токенов разнесён по проектам без потерь")
def t_usage_mapping():
    P = load("projects.json")["projects"]
    U = load("usage.json")["projects"]
    mapped = {usage_key(P, U, k) for k in P}
    orphan = [k for k in U if k not in mapped]
    return ["расход проекта %s ($%.0f) ни к чему не привязан — добавь проект в projects.json "
            "или поправь project_key_from_cwd в collect_usage.py" % (k, U[k]["usd"]) for k in orphan]


@check("оценка затрат покрывает весь бэклог")
def t_estimates():
    P = load("projects.json")["projects"]
    E = load("estimates.json")["projects"]
    bad = []
    for k, p in P.items():
        want = len(p.get("backlog", []))
        got = len(E.get(k, {}).get("items", []))
        if want != got:
            bad.append("%s: задач в бэклоге %d, в оценке %d — пересобери estimate.py" % (k, want, got))
    return bad


@check("разобранные комментарии указывают, куда перенесены")
def t_comments():
    C = load("comments.json").get("comments", [])
    P = load("projects.json")["projects"]
    bad = []
    for c in C:
        if c.get("status") == "processed":
            mt = c.get("moved_to") or ""
            if not mt:
                bad.append("#%s помечен processed, но moved_to пуст" % c.get("id"))
                continue
            proj = mt.split("/")[0].strip()
            if proj not in P and proj != "портфель":
                bad.append("#%s ссылается на неизвестный проект %r" % (c.get("id"), proj))
    return bad


@check("конкуренты: у непустого анализа есть вердикт")
def t_competitors():
    C = load("competitors.json")["projects"]
    return ["%s: есть %d конкурентов, но нет verdict" % (k, len(v.get("items", [])))
            for k, v in C.items() if v.get("items") and not (v.get("verdict") or "").strip()]


@check("в данных нет ключей и токенов")
def t_no_secrets():
    rx = [re.compile(p) for p in (
        r"sk-ant-[A-Za-z0-9\-_]{20,}", r"AKIA[0-9A-Z]{16}",
        r"ghp_[A-Za-z0-9]{30,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----")]
    bad = []
    files = [os.path.join(DATA, fn) for fn in sorted(os.listdir(DATA)) if fn.endswith(".json")]
    mem = os.path.join(DATA, "memory")
    if os.path.isdir(mem):
        # Память набивается из чужих заметок, поэтому её проверяем наравне с данными.
        files += [os.path.join(mem, fn) for fn in sorted(os.listdir(mem)) if fn.endswith((".md", ".json"))]
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read()
        except OSError:
            continue
        for r in rx:
            if r.search(txt):
                bad.append("%s: найдено совпадение с %s" % (os.path.relpath(path, DATA), r.pattern))
    return bad


@check("память: у записей есть вид, проект и непустое тело")
def t_memory():
    mem_dir = os.path.join(DATA, "memory")
    if not os.path.isdir(mem_dir):
        return []
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import memory as mem
    projects = set(load("projects.json")["projects"]) | {"портфель"}
    bad, ids = [], {}
    for rec in mem.load_all():
        if rec["kind"] not in mem.KINDS:
            bad.append("%s: вид %r вне %s" % (rec["id"], rec["kind"], list(mem.KINDS)))
        if not (rec.get("body") or "").strip():
            bad.append("%s: пустое тело — запись ни о чём" % rec["id"])
        if rec["project"] not in projects:
            bad.append("%s: проект %r не из projects.json" % (rec["id"], rec["project"]))
        if rec.get("parent") and rec["parent"] == rec["id"]:
            bad.append("%s: запись объявлена родителем самой себе" % rec["id"])
        ids[rec["id"]] = ids.get(rec["id"], 0) + 1
    for rid, n in ids.items():
        if n > 1:
            bad.append("%s: идентификатор встречается %d раз" % (rid, n))
    # Осиротевшие ссылки на родителя ломают дерево — ветвь повисает в воздухе.
    known = set(ids)
    for rec in mem.load_all():
        if rec.get("parent") and rec["parent"] not in known:
            bad.append("%s: родитель %s не найден" % (rec["id"], rec["parent"]))
    return bad


@check("индекс памяти и дерево собраны по текущим записям")
def t_memory_index():
    mem_dir = os.path.join(DATA, "memory")
    if not os.path.isdir(mem_dir) or not os.listdir(mem_dir):
        return []
    bad = []
    idx_path = os.path.join(mem_dir, "index.json")
    if not os.path.exists(idx_path):
        return ["нет data/memory/index.json — запустите py tools/pm.py refresh"]
    with open(idx_path, encoding="utf-8") as f:
        idx = json.load(f)
    on_disk = {fn[:-3] for fn in os.listdir(mem_dir) if fn.endswith(".md")}
    in_index = {r["id"] for r in idx.get("records", [])}
    if on_disk - in_index:
        bad.append("нет в индексе: %s" % ", ".join(sorted(on_disk - in_index)[:5]))
    if in_index - on_disk:
        bad.append("в индексе есть, а файла нет: %s" % ", ".join(sorted(in_index - on_disk)[:5]))
    tree_path = os.path.join(DATA, "tree.json")
    if not os.path.exists(tree_path):
        bad.append("нет data/tree.json — дерево на дашборде будет пустым")
    return bad


@check("входящие: у принятых предложений есть задача в бэклоге")
def t_inbox():
    p = os.path.join(DATA, "inbox.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        items = json.load(f).get("items", [])
    P = load("projects.json")["projects"]
    bad = []
    for it in items:
        if it.get("status") != "applied":
            continue
        ref = it.get("applied_to") or ""
        proj, _, tid = ref.partition("/")
        if proj not in P:
            bad.append("%s: принято в неизвестный проект %r" % (it.get("id"), proj))
        elif not any(b.get("id") == tid for b in P[proj].get("backlog", [])):
            bad.append("%s: задачи %s в бэклоге %s нет" % (it.get("id"), tid, proj))
    return bad


@check("дашборд собирается и содержит все проекты")
def t_dashboard_builds():
    # Собираем во временный файл: проверка не должна делать рабочее дерево грязным.
    import tempfile
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    fd, p = tempfile.mkstemp(suffix=".html", prefix="tt-check-")
    os.close(fd)
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_dashboard.py"), p],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return ["build_dashboard.py упал: %s" % (r.stderr or r.stdout)[:300]]
        if not os.path.exists(p) or not os.path.getsize(p):
            return ["дашборд не собрался"]
        with open(p, encoding="utf-8") as f:
            html = f.read()
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass
    if not os.path.exists(os.path.join(ROOT, "dashboard.html")):
        return ["собранного dashboard.html нет в репозитории — запусти pm.py refresh"]
    bad = []
    if len(html) < 50_000:
        bad.append("dashboard.html подозрительно мал: %d байт" % len(html))
    if "__DATA__" in html:
        bad.append("в дашборде остался незаполненный плейсхолдер __DATA__")
    try:
        blob = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        payload = json.loads(blob.replace("<\\/script>", "</script>"))
    except Exception as e:
        return bad + ["данные внутри дашборда не разбираются: %s" % e]
    want = set(load("projects.json")["projects"])
    got = set(payload.get("projects", {}).get("projects", {}))
    if want != got:
        bad.append("в дашборде не хватает проектов: %s" % ", ".join(sorted(want - got)))
    return bad


@check("прайс моделей сверялся недавно")
def t_pricing_fresh():
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import pricing
    try:
        checked = datetime.strptime(pricing.CHECKED, "%Y-%m-%d").date()
    except Exception:
        return ["в tools/pricing.py нет разбираемой даты CHECKED"]
    age = (date.today() - checked).days
    if age > pricing.STALE_DAYS:
        return ["прайс сверялся %d дней назад (%s), порог %d. Спросите у skill claude-api "
                "актуальные цены, поправьте таблицу и передвиньте CHECKED — на этом прайсе "
                "держится весь денежный счёт портфеля." % (age, pricing.CHECKED, pricing.STALE_DAYS)]
    return []


@check("индексы качества собраны для всех живых проектов")
def t_quality():
    P = load("projects.json")["projects"]
    R = load("repos.json")["repos"]
    Q = load("quality.json")["projects"]
    bad = []
    for k in P:
        if not (R.get(k) or {}).get("exists"):
            continue
        q = Q.get(k)
        if not q:
            bad.append("%s: нет записи в quality.json — пересоберите quality.py" % k)
        elif q.get("spec") and q["spec"].get("score") is None:
            bad.append("%s: индекс ТЗ не посчитался ни по одной компоненте" % k)
    return bad


@check("рабочее дерево чистое")
def t_git_clean():
    r = subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    dirty = [l for l in r.stdout.splitlines() if l.strip()]
    return ["не закоммичено %d файлов:\n      %s" % (len(dirty), "\n      ".join(dirty[:10]))] if dirty else []


TESTS = [t_json_parses, t_required_fields, t_readiness, t_backlog, t_paths,
         t_usage_mapping, t_estimates, t_comments, t_competitors, t_no_secrets,
         t_pricing_fresh, t_quality, t_memory, t_memory_index, t_inbox,
         t_dashboard_builds, t_git_clean]

SOFT = {t_git_clean}  # предупреждение, а не провал


def main():
    quiet = "--quiet" in sys.argv
    failed = 0
    for t in TESTS:
        try:
            problems = t() or []
        except Missing as e:
            problems = [str(e)]
        except Exception as e:
            problems = ["проверка упала: %r" % e]
        if not problems:
            if not quiet:
                print("  ok    %s" % t._name)
            continue
        soft = t in SOFT
        if not soft:
            failed += 1
        print("  %s  %s" % ("warn" if soft else "ПРОВАЛ", t._name))
        for p in problems:
            print("      · %s" % p)
    print()
    if failed:
        print("ПРОВАЛЕНО проверок: %d. В тестирование и main не сливать." % failed)
        return 1
    print("Все проверки пройдены — можно сливать.")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
