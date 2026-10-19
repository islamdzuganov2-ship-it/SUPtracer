# -*- coding: utf-8 -*-
"""Дерево работ: из чего проект вырос и куда растёт дальше.

Портфель — ствол. Проект — крупная ветвь. Дальше ветвление идёт по смыслу:

  корневой запрос          то, с чего всё началось: «нужно вынести кеш»
    решения и требования   чем ответили на запрос
    задачи                 во что запрос превратился в бэклоге
  фундамент                сделанное — то, на что теперь опирается всё остальное
  почки                    предложения из входящих: ещё не задачи, но уже видно

Связь «задача выросла из запроса» берётся из поля root задачи бэклога — его
проставляет приём входящих и команда apply. Там, где связи нет, задача висит
на проекте напрямую: дерево показывает и это, потому что несвязанная работа —
сама по себе наблюдение.

Толщина ветви в дашборде — вес поддерева в токенах. Дата узла — когда он появился;
по ней дерево разрастается на таймлайне, а не рисуется сразу целиком.

-> data/tree.json
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mdparse as md

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")   # ИНТ-01


def load(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return {} if default is None else default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def node(nid, label, kind, **kw):
    n = {"id": nid, "label": label, "kind": kind, "weight": 0, "date": "", "children": []}
    n.update(kw)
    return n


def usage_for(key, projects, usage):
    path = (projects[key].get("path") or "").lower().replace("\\", "/").rstrip("/")
    base = os.path.basename(path)
    for c in (key, key.lower(), base, "doc:" + base, "doc:" + base.replace("_", "-")):
        if c in usage:
            return usage[c]
    return None


def build(write=True):
    projects = load("projects.json").get("projects", {})
    usage = load("usage.json", {"projects": {}}).get("projects", {})
    repos = load("repos.json", {"repos": {}}).get("repos", {})
    est = load("estimates.json", {"projects": {}}).get("projects", {})
    memidx = load(os.path.join("memory", "index.json"), {"records": []})
    inbox = load("inbox.json", {"items": []}).get("items", [])
    records = [r for r in memidx.get("records", []) if r.get("status") == "активна"]
    discovered = load("codex_sessions.json").get("projects", {})

    memory_nodes, memory_roots = _memory_forest(records, projects)

    trunk = node("портфель", "Портфель", "портфель", detail="Все проекты в одном стволе")
    total_weight = 0

    for key in sorted(projects, key=lambda k: (projects[k].get("tier", 9), k)):
        p = projects[key]
        u = usage_for(key, projects, usage) or {}
        g = (repos.get(key) or {}).get("git") or {}
        e = est.get(key, {})
        spent = int(u.get("billable") or 0)

        pn = node("p:" + key, p.get("name", key), "проект", project=key,
                  weight=spent, date=(p.get('created_at') or g.get("first_commit") or "")[:10],
                  status=p.get("status", ""),
                  detail="%s · готовность %s%%" % (p.get("goal", ""),
                                                   ("не оценено" if (p.get("readiness") or {}).get("assessed") is False else
                                                    (p.get("readiness") or {}).get("functional", "—"))),
                  spent_tokens=spent, spent_usd=round(u.get("usd") or 0, 2),
                  cost_complete=u.get("cost_complete", True),
                  commits=g.get('project_commits', g.get("commits")))

        # --- фундамент: сделанное
        done = list(p.get("done") or [])
        if done:
            share = spent // max(1, len(done))
            first = (p.get('created_at') or g.get("first_commit") or "")[:10]
            last = (p.get('reviewed_at') if g.get('history_since') else g.get('last_commit') or '')[:10]
            fnode = node("f:" + key, "Фундамент — сделано %d" % len(done), "фундамент",
                         project=key, date=first,
                         detail="То, на что опирается всё остальное в проекте")
            for i, d in enumerate(done):
                date, estimated = _done_date(d, i, len(done), first, last)
                fnode["children"].append(node(
                    "d:%s:%d" % (key, i), d[:160], "сделано", project=key,
                    weight=share, weight_basis="равная доля фактических затрат проекта",
                    date=date, date_estimated=estimated, detail=d))
            pn["children"].append(fnode)

        # --- корневые запросы из памяти и то, что из них выросло
        est_items = {b.get("id"): b for b in e.get("items", [])}
        commits = g.get("task_commits") or {}
        attached = set()
        pn["children"].extend(memory_roots.get(key, []))
        for b in p.get("backlog", []):
            target = memory_nodes.get(b.get("root") or b.get("memory") or "")
            if target is not None and target["project"] == key:
                target["children"].append(_task_node(key, b, est_items, commits.get(b.get("id"))))
                attached.add(b.get("id"))

        # --- задачи, не привязанные ни к какому запросу
        loose = [b for b in p.get("backlog", []) if b.get("id") not in attached]
        if loose:
            bn = node("b:" + key, "Ветви — задач %d" % len(loose), "бэклог", project=key,
                      date=today(), future=True,
                      detail="Задачи бэклога без связи с исходным запросом")
            for b in loose:
                bn["children"].append(_task_node(key, b, est_items, commits.get(b.get("id"))))
            pn["children"].append(bn)

        # --- почки: предложения из входящих
        buds = [i for i in inbox if i.get("project") == key and i.get("status") == "new"]
        if buds:
            bud = node("i:" + key, "Почки — предложений %d" % len(buds), "почки", project=key,
                       date=today(), future=True,
                       detail="Пришло из заметок, ещё не принято в бэклог")
            for i in buds:
                bud["children"].append(node(
                    "in:" + str(i.get("id")), i.get("title", "")[:160], "предложение",
                    project=key, future=True, date=i.get("created", today()),
                    size=i.get("size", ""), detail=i.get("why", ""), source=i.get("source", "")))
            pn["children"].append(bud)

        total_weight += spent
        trunk["children"].append(pn)

    # --- записи памяти вне известных проектов. Заметки проектов, которых портфель
    # не ведёт (ПРМ-08), — отдельными ветвями: видно, сколько знания лежит у каждого.
    orphan_projects = {}
    for key in set(discovered) | set(usage):
        if key not in projects:
            orphan_projects[key] = []
    for r in records:
        if r["project"] not in projects:
            proj = r.get("foreign_project") or r["project"]
            if proj not in projects:
                orphan_projects.setdefault(proj, []).append(r)
    for proj, recs in sorted(orphan_projects.items(), key=lambda kv: (kv[0] == "портфель", kv[0])):
        foreign = proj in discovered or proj in usage or any(x.get("foreign_project") for x in recs)
        u = usage.get(proj, {})
        found = discovered.get(proj, {})
        pn = node("p:" + proj, proj + (" — вне портфеля" if foreign else ""), "проект", project=proj,
                  detail=("Обнаружен в локальных сессиях; ещё не добавлен в ручной портфель. " +
                          "; ".join(found.get("paths", [])) if proj in discovered else
                          "Проект из журналов расхода или памяти; ещё не добавлен в ручной портфель" if foreign
                          else "Записи без проекта в projects.json"),
                  weight=int(u.get("billable") or 0), spent_tokens=int(u.get("billable") or 0),
                  spent_usd=u.get("usd", 0), cost_complete=u.get("cost_complete", True),
                  source_provider=found.get("provider", ""), discovered=foreign)
        pn["children"].extend(memory_roots.get(proj, []))
        if pn["children"] or foreign:
            trunk["children"].append(pn)

    _rollup(trunk)
    stats = _stats(trunk)
    out = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": trunk,
        "stats": stats,
        "legend": {
            "портфель": "ствол", "проект": "крупная ветвь", "запрос": "корневой запрос",
            "решение": "чем ответили", "требование": "условие приёмки", "риск": "угроза",
            "итог": "чем кончилось", "факт": "проверяемое число", "заметка": "прочее",
            "фундамент": "сделанное", "сделано": "готовая часть основания",
            "бэклог": "задачи без связи с запросом", "задача": "запланированная работа",
            "почки": "предложения из входящих", "предложение": "ещё не задача",
            "коммит": "реализация задачи — коммит с её номером",
        },
        "note": "Толщина ветви — вес поддерева в токенах. Вес узлов «сделано» — равная доля "
                "фактических затрат проекта, это оценка, а не измерение.",
    }
    dest = os.path.join(DATA, "tree.json")
    if not write:                 # для чтения по запросу (MCP): без побочного эффекта на диске
        return out, None
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out, dest


def _memory_forest(records, projects):
    """Каждая активная запись видна ровно один раз, даже если её родитель устарел.

    Восстановление касается только отображения: исходные связи не переписываются.
    Межпроектные связи и циклы не должны скрывать записи или переносить их в чужой проект.
    """
    by_id = {r["id"]: r for r in records}
    groups = {rid: r["project"] if r["project"] in projects else
              r.get("foreign_project") or r["project"] for rid, r in by_id.items()}
    parents, notes = {}, {}
    for rid, r in by_id.items():
        par = r.get("parent")
        if not par:
            continue
        if par not in by_id:
            notes[rid] = "Родитель недоступен в активной памяти; запись показана отдельной ветвью."
        elif groups[par] != groups[rid]:
            notes[rid] = "Родитель относится к другому проекту; запись показана в своём проекте."
        else:
            parents[rid] = par

    # Обходим каждую цепочку один раз. Разрываем одну связь цикла детерминированно.
    checked = set()
    for rid in sorted(by_id):
        path, current = set(), rid
        while current not in checked:
            if current in path:
                parents.pop(current, None)
                notes[current] = "В связях памяти обнаружен цикл; ветвь показана отдельно."
                break
            path.add(current)
            if current not in parents:
                break
            current = parents[current]
        checked.update(path)

    nodes, roots = {}, {}
    for rid, r in by_id.items():
        nodes[rid] = node("m:" + rid, r["title"][:160], r.get("kind", "заметка"),
                          project=groups[rid], weight=r.get("tokens", 0),
                          date=(r.get("created") or "")[:10], detail=r.get("digest", ""),
                          memory=rid, source=r.get("source", ""), tags=r.get("tags", []))
        for key in ("source_provider", "session", "role", "model", "cwd"):
            if r.get(key):
                nodes[rid][key] = r[key]
        if rid in notes:
            nodes[rid].update(link_note=notes[rid], parent_memory=r.get("parent", ""))
    for rid, r in sorted(by_id.items(), key=lambda item: (item[1].get("created", ""), item[0])):
        if rid in parents:
            nodes[parents[rid]]["children"].append(nodes[rid])
        else:
            roots.setdefault(groups[rid], []).append(nodes[rid])
    return nodes, roots


def _task_node(key, b, est_items, commits=None):
    """ДРВ-05: коммиты, где назван номер задачи, — листья под ней; дата задачи —
    дата первого такого коммита: так дерево показывает, когда работа началась."""
    e = est_items.get(b.get("id"), {})
    commits = list(commits or [])
    n = node("t:%s:%s" % (key, b.get("id")), b.get("title", "")[:160], "задача",
             project=key, weight=int(e.get("est_tokens") or 0),
             usd=e.get("est_usd"), size=b.get("size", ""),
             status=b.get("status", ""), priority=b.get("priority", ""),
             future=b.get("status") != "done",
             date=commits[0]["date"] if commits else (b.get("created", "") or today()),
             detail=b.get("why", ""), task=b.get("id"))
    for c in commits:
        n["children"].append(node("c:%s:%s:%s" % (key, b.get("id"), c["sha"]), c["subject"][:160],
                                  "коммит", project=key, date=c["date"], sha=c["sha"],
                                  detail="коммит %s" % c["sha"]))
    return n


def _done_date(text, i, n, first, last):
    """ДРВ-06: дата пункта «сделано». Своя дата в тексте — она; иначе оценка по месту
    в списке между первым и последним коммитом. Признак оценки хранится в узле."""
    own = re.search(r"\b(20\d\d-\d\d-\d\d)\b", text or "")
    if own:
        return own.group(1), False
    if not first:
        return "", False
    try:
        a = datetime.strptime(first, "%Y-%m-%d")
        b = datetime.strptime(last or first, "%Y-%m-%d")
    except ValueError:
        return first, True
    return (a + (b - a) * ((i + 1) / float(n))).strftime("%Y-%m-%d"), True


def _rollup(n):
    """Вес поддерева и дата — снизу вверх. Дата ветви = самая ранняя дата в ней:
    ветвь появилась тогда, когда появился её первый узел."""
    w = int(n.get("weight") or 0)
    dates = [n.get("date")] if n.get("date") else []
    depth = 0
    for c in n["children"]:
        _rollup(c)
        w += c["subtree_weight"]
        if c.get("first_date"):
            dates.append(c["first_date"])
        depth = max(depth, c["depth"] + 1)
    n["subtree_weight"] = w
    n["first_date"] = min(dates) if dates else ""
    n["depth"] = depth
    n["count"] = 1 + sum(c["count"] for c in n["children"])
    return n


def _stats(trunk):
    by_kind, months, nodes = {}, {}, 0
    stack = [trunk]
    while stack:
        n = stack.pop()
        nodes += 1
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
        d = (n.get("date") or n.get("first_date") or "")[:7]
        if d:
            months[d] = months.get(d, 0) + 1
        stack += n["children"]
    return {
        "nodes": nodes, "depth": trunk["depth"], "by_kind": by_kind,
        "weight": trunk["subtree_weight"],
        "growth": dict(sorted(months.items())),
        "months": sorted(months),
        "future": sum(1 for _ in _walk(trunk) if _.get("future")),
    }


def _walk(n):
    yield n
    for c in n["children"]:
        yield from _walk(c)


WIDE = 25        # ДРВ-07: сколько записей памяти показывать у широкого узла

MARK = {"проект": "▣", "запрос": "◆", "решение": "✓", "требование": "≡", "риск": "!",
        "итог": "■", "факт": "#", "источник": "↗", "заметка": "·", "фундамент": "▤",
        "сделано": "▪", "бэклог": "▷", "задача": "○", "почки": "◌", "предложение": "◌",
        "коммит": "•"}


def render_text(project=None, depth=4, max_lines=150):
    """Ветвь дерева текстом — для ассистента, которому не нужна картинка, а нужно
    понять, что из чего выросло. Строится в памяти, на диск ничего не пишет."""
    out, _ = build(write=False)
    root = out["root"]
    if project:
        root = next((c for c in root["children"] if c.get("project") == project), None)
        if root is None:
            return "Проекта «%s» в дереве нет." % project
    lines = []

    def walk(n, d):
        if len(lines) >= max_lines:
            return
        extra = []
        if n.get("task"):
            extra.append(n["task"])
        if n.get("status") and n["kind"] == "задача":
            extra.append(n["status"])
        if n.get("memory"):
            extra.append(n["memory"])
        if n.get("sha"):
            extra.append(n["sha"])
        lines.append("%s%s %s%s" % ("  " * d, MARK.get(n["kind"], "·"), n.get("label", "")[:100],
                                   (" [%s]" % ", ".join(extra)) if extra else ""))
        if d >= depth:
            if n["children"]:
                lines.append("%s  … ещё %d узлов" % ("  " * d, n["count"] - 1))
            return
        # ДРВ-07: у широкого узла — первые WIDE записей памяти; фундамент, задачи
        # и почки видны всегда, иначе сотни заметок заслонили бы бэклог.
        seen, rest = 0, 0
        for c in n["children"]:
            if c.get("memory"):
                seen += 1
                if seen > WIDE:
                    rest += 1
                    continue
            walk(c, d + 1)
        if rest and len(lines) < max_lines:
            lines.append("%s  … ещё %d записей — найти: tt_recall <слова>" % ("  " * d, rest))

    walk(root, 0)
    if len(lines) >= max_lines:
        lines.append("… дерево обрезано на %d строках" % max_lines)
    legend = "◆ запрос · ✓ решение · ≡ требование · ! риск · ■ итог · ▤ фундамент · ○ задача · ◌ почка"
    return "\n".join(lines) + "\n\n" + legend


def main():
    out, dest = build()
    s = out["stats"]
    print("дерево: узлов %d, глубина %d, вес %s токенов, будущих узлов %d" % (
        s["nodes"], s["depth"], "{:,}".format(s["weight"]).replace(",", " "), s["future"]))
    kinds = ", ".join("%s %d" % (k, v) for k, v in sorted(s["by_kind"].items(), key=lambda kv: -kv[1]))
    print("виды узлов: " + kinds)
    print("-> " + os.path.relpath(dest, ROOT))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
