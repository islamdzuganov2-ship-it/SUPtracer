# -*- coding: utf-8 -*-
"""Индексы качества: проработки, ТЗ и инженерной гигиены.

Каждый индекс — взвешенная сумма нормированных компонент. Компоненты
сохраняются вместе со счётом, поэтому у любой цифры видно, из чего она
собрана и что именно её уронило. Оценок «на глаз» здесь нет: всё считается
из repos.json и projects.json.

-> data/quality.json
"""
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, default=None):
    p = os.path.join(ROOT, "data", name)
    if not os.path.exists(p):
        if default is None:
            raise SystemExit("Нет data/%s. Сначала: py tools/pm.py refresh" % name)
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def ramp(value, zero_at, full_at):
    """Линейная нормировка: zero_at -> 0, full_at -> 1. Работает и на убывание."""
    if value is None:
        return None
    if full_at == zero_at:
        return 0.0
    return clamp((value - zero_at) / (full_at - zero_at))


def component(label, raw, score, weight, note):
    return {"label": label, "raw": raw, "score": None if score is None else round(score, 3),
            "weight": weight, "note": note}


def combine(components):
    """Взвешенная сумма по компонентам, у которых есть счёт. Отсутствующие
    не обнуляют индекс, а выбывают из знаменателя — иначе проект без git
    получал бы ноль за то, чего у него просто не измеряли."""
    used = [c for c in components if c["score"] is not None]
    if not used:
        return None
    total_w = sum(c["weight"] for c in used)
    if not total_w:
        return None
    return round(100.0 * sum(c["score"] * c["weight"] for c in used) / total_w)


def spec_quality(r):
    """Качество ТЗ: не объём документации, а её формализация и связь с кодом."""
    s = r.get("spec") or {}
    c = r.get("code") or {}
    docs = s.get("doc_files") or 0
    doc_lines = s.get("doc_lines") or 0
    loc = c.get("loc_total") or 0

    comps = []

    # Объём: документация должна быть соразмерна коду. 0.05 строки доков
    # на строку кода — минимум, 0.4 — насыщение, дальше растёт вода, а не польза.
    ratio = s.get("doc_to_code_ratio")
    comps.append(component(
        "Объём относительно кода", ratio,
        None if not loc else ramp(ratio, 0.02, 0.40),
        1.0, "%s строк документации на строку кода" % (ratio if ratio is not None else "—")))

    # Структура: спецификация имеет заголовки и таблицы, поток мыслей — нет.
    per_doc = (s.get("headings") or 0) / docs if docs else None
    comps.append(component(
        "Структурированность", None if per_doc is None else round(per_doc, 1),
        ramp(per_doc, 2, 25) if per_doc is not None else None,
        1.0, "%s заголовков на документ" % (round(per_doc, 1) if per_doc is not None else "—")))

    # Формализация: есть ли вообще система нумерации требований.
    fams = len(s.get("requirement_families") or [])
    reqs = s.get("requirements") or 0
    comps.append(component(
        "Формализация требований", reqs,
        ramp(reqs, 0, 120) if docs else None,
        1.5, "%d требований с кодами в %d схемах нумерации" % (reqs, fams)))

    # Трассируемость — главное. Требование, которого нет в коде, не реализовано
    # либо не размечено; и то и другое означает, что по ТЗ нельзя принимать работу.
    tr = s.get("traceability_pct")
    comps.append(component(
        "Трассируемость в код", tr,
        None if tr is None else clamp(tr / 70.0),
        2.5, "%s%% кодов требований встречается в коде" % (tr if tr is not None else "—")))

    # Дисциплина: документация живёт вместе с кодом или дописывается задним числом.
    dd = s.get("doc_discipline_pct")
    comps.append(component(
        "Документация вместе с кодом", dd,
        None if dd is None else clamp(dd / 60.0),
        1.5, "%s%% коммитов с кодом трогали и документацию" % (dd if dd is not None else "—")))

    # Незакрытые вопросы — штраф, но мягкий: честно помеченный открытый вопрос
    # лучше умолчания. Наказывается только их плотность.
    oq = s.get("open_questions") or 0
    density = 1000.0 * oq / doc_lines if doc_lines else None
    comps.append(component(
        "Плотность открытых вопросов", None if density is None else round(density, 2),
        None if density is None else 1.0 - ramp(density, 0.3, 4.0),
        1.0, "%d незакрытых мест, %s на 1000 строк" % (oq, round(density, 2) if density is not None else "—")))

    return {"score": combine(comps), "components": comps}


def engineering_quality(r):
    """Качество проработки: то, что отличает продукт от наброска."""
    c = r.get("code") or {}
    h = r.get("hygiene") or {}
    g = r.get("git")
    loc = c.get("loc_total") or 0
    files = c.get("files_total") or 0

    comps = []

    # Тесты — доля тестовых файлов. 15% принято считать рабочим минимумом.
    tf = c.get("test_files") or 0
    share = 100.0 * tf / files if files else None
    comps.append(component(
        "Покрытие тестами", None if share is None else round(share, 1),
        None if share is None else clamp(share / 15.0),
        2.5, "%d тестовых файлов из %d, %s%%" % (tf, files, round(share, 1) if share is not None else "—")))

    # Версионирование. Отсутствие git — единственная компонента, которая
    # обнуляется жёстко: работа существует в одном экземпляре.
    commits = (g or {}).get("commits") or 0
    comps.append(component(
        "Версионирование", commits if g else 0,
        0.0 if not g else clamp(commits / 30.0),
        2.0, "%d коммитов" % commits if g else "репозитория нет"))

    # Гигиена поставки: без этого проект нельзя передать другому человеку.
    flags = {"CI": h.get("ci"), "lock-файл": h.get("lockfile"), "README": h.get("readme"),
             "LICENSE": h.get("license"), ".gitignore": h.get("gitignore")}
    have = sum(1 for v in flags.values() if v)
    comps.append(component(
        "Гигиена поставки", "%d из %d" % (have, len(flags)),
        have / float(len(flags)),
        1.5, "есть: %s" % (", ".join(k for k, v in flags.items() if v) or "ничего")))

    # Незакрытые пометки в коде.
    todo = c.get("todo_markers") or 0
    dens = 1000.0 * todo / loc if loc else None
    comps.append(component(
        "Чистота кода", None if dens is None else round(dens, 2),
        None if dens is None else 1.0 - ramp(dens, 0.2, 5.0),
        1.0, "%d пометок TODO/FIXME, %s на 1000 строк" % (todo, round(dens, 2) if dens is not None else "—")))

    # Незакоммиченная работа — риск потерять её и признак незавершённого хода.
    dirty = (g or {}).get("dirty_files")
    comps.append(component(
        "Рабочее дерево", dirty,
        None if dirty is None else (1.0 if dirty == 0 else clamp(1.0 - dirty / 20.0)),
        1.0, "нечего коммитить" if dirty == 0 else "%s незакоммиченных файлов" % dirty))

    # Подозрение на секреты в коде — прямой риск.
    sec = [s for s in (r.get("secrets_suspect") or []) if ".claude/worktrees" not in s.get("file", "")]
    comps.append(component(
        "Секреты в коде", len(sec),
        1.0 if not sec else clamp(1.0 - len(sec) / 3.0),
        1.5, "чисто" if not sec else "подозрений: %d" % len(sec)))

    return {"score": combine(comps), "components": comps}


def portfolio_signals(P, R, U, E, usage_of):
    """Портфельные сигналы: то, что видно только если смотреть на все проекты разом.

    Каждый сигнал — не украшение, а вопрос, на который проджект-менеджер обязан
    иметь ответ. Уровень: ok / watch / act.
    """
    today = datetime.now(timezone.utc).date()
    own = [k for k in P if (P[k].get("tier") or 9) < 4]
    sig = []

    def add(key, title, value, level, what, why):
        sig.append({"key": key, "title": title, "value": value, "level": level,
                    "what": what, "why": why})

    # --- 1. Концентрация внимания -------------------------------------------
    spend = {k: (usage_of(k) or {}).get("usd", 0) for k in own}
    total = sum(spend.values()) or 1
    top = max(spend, key=spend.get)
    share = 100.0 * spend[top] / total
    add("concentration", "Концентрация внимания", "%d%%" % round(share),
        "act" if share > 55 else "watch" if share > 35 else "ok",
        "%s забирает %d%% всех денег портфеля; остальным %d проектам достаётся %d%%"
        % (P[top]["name"], round(share), len(own) - 1, round(100 - share)),
        "Портфель из одного проекта — это не портфель. Пока внимание сосредоточено, "
        "остальные проекты не стоят на месте — они устаревают.")

    # --- 2. Незавершённое производство ---------------------------------------
    wip = [k for k in own if "активн" in (P[k].get("status") or "").lower()
           or "разработк" in (P[k].get("status") or "").lower()
           or "исследован" in (P[k].get("status") or "").lower()]
    add("wip", "Одновременно в работе", str(len(wip)),
        "act" if len(wip) > 3 else "watch" if len(wip) > 2 else "ok",
        "проектов в активном статусе: %s" % (", ".join(P[k]["name"] for k in wip) or "нет"),
        "Переключение между проектами стоит дороже самой работы. Больше трёх "
        "одновременно — верный способ не закончить ни одного.")

    # --- 3. Простой: проекты без движения ------------------------------------
    stale = []
    for k in own:
        g = (R.get(k) or {}).get("git") or {}
        last = g.get("last_commit") or ""
        if not last:
            continue
        try:
            d = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if d >= 21:
            stale.append((P[k]["name"], d))
    stale.sort(key=lambda x: -x[1])
    add("stale", "Проекты без движения", str(len(stale)),
        "act" if len(stale) >= 4 else "watch" if stale else "ok",
        "; ".join("%s — %d дн." % s for s in stale[:6]) or "все проекты двигались за последние три недели",
        "Проект без движения дольше месяца — это не пауза, а нерешённый вопрос: "
        "его надо либо возобновить, либо закрыть. Третьего состояния не бывает.")

    # --- 4. Работа в одном экземпляре ----------------------------------------
    nogit = [P[k]["name"] for k in own
             if (R.get(k) or {}).get("exists") and not (R.get(k) or {}).get("git")]
    noremote = [P[k]["name"] for k in own
                if ((R.get(k) or {}).get("git") or {}).get("commits")
                and not ((R.get(k) or {}).get("git") or {}).get("remote")]
    add("single_copy", "Работа без резервной копии", str(len(nogit) + len(noremote)),
        "act" if nogit else "watch" if noremote else "ok",
        ("без git: %s. " % ", ".join(nogit) if nogit else "")
        + ("без удалённого репозитория: %s" % ", ".join(noremote) if noremote else ""),
        "Единственный экземпляр на одном диске — это не риск, а отложенная потеря. "
        "Дешевле всего устраняется из всего списка.")

    # --- 5. Долг безопасности в деньгах --------------------------------------
    sec_usd = 0.0
    sec_tasks = 0
    for k in own:
        for it in (E.get(k) or {}).get("items", []):
            t = (it.get("id", "") + " " + it.get("title", "")).lower()
            if any(w in t for w in ("иб-", "sec-", "безопасн", "уязвим", " audit", "аудит")):
                sec_usd += it.get("est_usd", 0)
                sec_tasks += 1
    rem = sum((E.get(k) or {}).get("remaining_usd", 0) for k in own) or 1
    add("security_debt", "Долг безопасности", "$%d" % round(sec_usd),
        "act" if sec_usd / rem > 0.3 else "watch" if sec_usd else "ok",
        "%d задач, %d%% всего оставшегося бэклога портфеля" % (sec_tasks, round(100 * sec_usd / rem)),
        "Безопасность — единственный вид долга, который превращается в блокер "
        "внедрения целиком, а не по частям. Её нельзя закрыть наполовину.")

    # --- 6. Осталось против потраченного -------------------------------------
    worst = []
    for k in own:
        u, e = usage_of(k), E.get(k) or {}
        if not u or not e.get("remaining_usd"):
            continue
        ratio = e["remaining_usd"] / max(1.0, u["usd"])
        if ratio >= 1.0:
            worst.append((P[k]["name"], ratio))
    worst.sort(key=lambda x: -x[1])
    add("remaining_ratio", "Осталось больше, чем вложено", str(len(worst)),
        "watch" if worst else "ok",
        "; ".join("%s — ×%.1f" % w for w in worst[:5]) or "нет таких проектов",
        "Если оставшийся бэклог дороже уже потраченного, проект пройден меньше "
        "чем наполовину — независимо от того, что показывает процент готовности.")

    # --- 7. Цена пункта готовности -------------------------------------------
    costs = []
    for k in own:
        u = usage_of(k)
        f = (P[k].get("readiness") or {}).get("functional") or 0
        if u and f:
            costs.append((P[k]["name"], u["usd"] / f))
    costs.sort(key=lambda x: -x[1])
    if costs:
        add("cost_per_point", "Цена пункта готовности", "$%.0f" % costs[0][1],
            "watch" if costs[0][1] > 30 else "ok",
            "дороже всего у «%s» — $%.0f за пункт; дешевле всего у «%s» — $%.2f"
            % (costs[0][0], costs[0][1], costs[-1][0], costs[-1][1]),
            "Показывает, где прогресс даётся дорого. Растущая цена пункта — "
            "первый признак, что проект входит в болото, задолго до срыва сроков.")

    # --- 8. Разрыв «умеет» и «можно включить» --------------------------------
    gaps = []
    for k in own:
        rd = P[k].get("readiness") or {}
        f, pr = rd.get("functional") or 0, rd.get("production") or 0
        if f - pr >= 30:
            gaps.append((P[k]["name"], f - pr, f, pr))
    gaps.sort(key=lambda x: -x[1])
    add("readiness_gap", "Разрыв функции и эксплуатации", str(len(gaps)),
        "act" if gaps and gaps[0][1] >= 45 else "watch" if gaps else "ok",
        "; ".join("%s — %d п.п. (%d%% против %d%%)" % g for g in gaps[:4]) or "нигде не превышает 30 п.п.",
        "Система умеет много, но её нельзя включить. Этот разрыв закрывается не "
        "функциями, а поставкой — и почти всегда оказывается длиннее, чем кажется.")

    # --- 9. Неразобранные комментарии ----------------------------------------
    C = load("comments.json", {"comments": []}).get("comments", [])
    new = [c for c in C if c.get("status") != "processed"]
    add("comments", "Неразобранные комментарии", str(len(new)),
        "act" if len(new) > 5 else "watch" if new else "ok",
        "; ".join((c.get("text") or "")[:60] for c in new[:3]) or "все разобраны",
        "Мысль, записанная и не разобранная, хуже незаписанной: она создаёт "
        "ощущение, что вопрос учтён, хотя он не поставлен в план.")

    # --- 10. Свежесть самих данных -------------------------------------------
    gen = (U and load("usage.json", {}).get("generated_at")) or ""
    age_h = None
    if gen:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds() / 3600
        except Exception:
            pass
    add("data_age", "Возраст данных", "%.0f ч" % age_h if age_h is not None else "—",
        "act" if (age_h or 0) > 168 else "watch" if (age_h or 0) > 48 else "ok",
        "факты собраны %s" % (gen[:16].replace("T", " ") if gen else "неизвестно когда"),
        "Дашборд, который показывает устаревшую правду, вреднее отсутствующего: "
        "по нему принимают решения с той же уверенностью.")

    return sig


def main():
    P = load("projects.json")["projects"]
    R = load("repos.json", {"repos": {}})["repos"]
    U = load("usage.json", {"projects": {}})["projects"]
    E = load("estimates.json", {"projects": {}})["projects"]

    def usage_of(key):
        path = (P[key].get("path") or "").lower().rstrip("/")
        base = os.path.basename(path)
        for c in (key, key.lower(), base, "doc:" + base, "doc:" + base.replace("_", "-")):
            if c in U:
                return U[c]
        return None

    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "method": "Взвешенная сумма нормированных компонент. Компоненты без данных выбывают "
                     "из знаменателя, а не обнуляют индекс. Пороги нормировки заданы в tools/quality.py "
                     "и подобраны так, чтобы 100 означало «дальше улучшать незачем», а не «идеально».",
           "projects": {}}

    for key in P:
        r = R.get(key) or {}
        if not r.get("exists"):
            out["projects"][key] = {"spec": None, "engineering": None, "economics": None}
            continue
        u = usage_of(key)
        rd = P[key].get("readiness") or {}
        econ = None
        if u:
            hours = u.get("active_hours") or 0
            func = rd.get("functional") or 0
            econ = {
                "usd_spent": u.get("usd"),
                "active_hours": hours,
                "usd_per_hour": round(u["usd"] / hours, 1) if hours else None,
                # Сколько стоил один пункт готовности. Сравнимо между проектами
                # и показывает, где прогресс даётся дорого.
                "usd_per_readiness_point": round(u["usd"] / func, 1) if func else None,
                "hours_per_readiness_point": round(hours / func, 2) if func else None,
                "tokens_per_hour": round(u["billable"] / hours) if hours else None,
            }
        out["projects"][key] = {
            "spec": spec_quality(r),
            "engineering": engineering_quality(r),
            "economics": econ,
        }

    out["portfolio_signals"] = portfolio_signals(P, R, U, E, usage_of)

    dest = os.path.join(ROOT, "data", "quality.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    marks = {"ok": "  ok ", "watch": " ждём", "act": " ДЕЙСТ"}
    print("портфельные сигналы:")
    for s in out["portfolio_signals"]:
        print("  %s %-30s %-8s %s" % (marks.get(s["level"], "?"), s["title"], s["value"], s["what"][:70]))
    print()
    print("%-16s %6s %6s %9s %9s" % ("проект", "ТЗ", "инжен.", "$/час", "$/пункт"))
    print("-" * 52)
    for k, v in out["projects"].items():
        e = v.get("economics") or {}
        print("%-16s %6s %6s %9s %9s" % (
            k,
            (v["spec"] or {}).get("score", "—") if v["spec"] else "—",
            (v["engineering"] or {}).get("score", "—") if v["engineering"] else "—",
            e.get("usd_per_hour", "—"), e.get("usd_per_readiness_point", "—")))
    print("-> " + dest)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
