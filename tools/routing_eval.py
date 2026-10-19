# -*- coding: utf-8 -*-
"""Качество раскладки заметок на размеченном наборе (ПРМ-09).

Раскладка — правила, а у правил есть точность. Без измерения «кажется, раскладывает
правильно» ничем не отличается от «раскладывает неправильно, но никто не заметил».

Размеченная заметка — обычная заметка с двумя полями во фронтматтере, которые приём
не читает: expect_project и expect_kind. Прогон — тот же приём, что в работе, только
пробный: ничего не пишется. Сравнивается корневая запись файла.

  py tools/routing_eval.py <каталог> [--projects файл.json] [--min-project 85] [--min-kind 70]

Встроенный набор: tests/fixtures/routing (вымышленный портфель из _projects.json).
Код возврата 1 — точность ниже порога.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest
import mdparse as md

MIN_PROJECT = 85.0     # ПРМ-09: точность раскладки по проекту
MIN_KIND = 70.0        # ПРМ-09: точность вида записи


def load_cases(folder):
    """Размеченные заметки каталога: [(путь, путь в каталоге, проект, вид, смешанная)]."""
    out = []
    for path, rel in ingest.iter_files([folder]):
        with open(path, encoding="utf-8") as f:
            fm, _ = md.parse_frontmatter(f.read())
        if fm.get("expect_project"):
            out.append((path, rel, str(fm["expect_project"]), str(fm.get("expect_kind") or ""),
                        str(fm.get("expect_mixed") or "").lower() in ("true", "yes", "да", "1")))
    return out


def evaluate(folder, projects=None):
    cases = load_cases(folder)
    if projects is None and os.path.exists(os.path.join(folder, "_projects.json")):
        with open(os.path.join(folder, "_projects.json"), encoding="utf-8") as f:
            projects = json.load(f).get("projects", {})
    # Папки проектов — общепринятые, а не из настройки хранилища: набор размечен под них,
    # и результат не должен зависеть от того, чьё хранилище подключено.
    reports = ingest.run_items([(p, rel, None) for p, rel, _, _, _ in cases],
                               dry=True, quiet=True, projects=projects,
                               project_folders=ingest.DEFAULT_PROJECT_FOLDERS)
    rows, p_ok, k_ok, k_total = [], 0, 0, 0
    for (path, rel, want_p, want_k, mixed), r in zip(cases, reports):
        got_p, got_k = r.get("project"), r.get("kind")
        hit_p = got_p == want_p
        hit_k = (got_k == want_k) if want_k else None
        p_ok += hit_p
        if want_k:
            k_total += 1
            k_ok += bool(hit_k)
        # Раздел в чужом проекте — та же опасная ошибка, что и файл, только мельче и
        # незаметнее: отчёт о приёме показывает проект файла, а не каждого раздела.
        # Заметка с разделами о разных проектах помечается expect_mixed и тут не судится.
        stray = [] if mixed else [x["title"] for x in r.get("records", [])[1:]
                                  if x["project"] not in (want_p, "портфель")]
        rows.append({"file": rel, "want_project": want_p, "got_project": got_p,
                     "confidence": r.get("confidence"), "why": r.get("why"),
                     "want_kind": want_k, "got_kind": got_k,
                     "project_ok": hit_p, "kind_ok": hit_k, "stray_sections": stray})
    n = len(cases)
    # Ошибки раскладки неравноценны. Заметка, оставшаяся в «портфеле», попадает в отчёт
    # о неуверенной раскладке и ждёт человека. Заметка, ушедшая в чужой проект, молча
    # засоряет его память и брифинг — это и есть опасная ошибка.
    wrong = [r for r in rows if not r["project_ok"] and r["got_project"] != "портфель"]
    unassigned = [r for r in rows if not r["project_ok"] and r["got_project"] == "портфель"]
    return {
        "cases": n,
        "project_accuracy": round(100.0 * p_ok / n, 1) if n else None,
        "kind_accuracy": round(100.0 * k_ok / k_total, 1) if k_total else None,
        "misrouted": len(wrong),
        "unassigned": len(unassigned),
        "stray_sections": sum(len(r["stray_sections"]) for r in rows),
        "rows": rows,
    }


def main():
    args = sys.argv[1:]

    def opt(name, d=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else d

    folder = next((a for a in args if not a.startswith("--") and a not in
                   (opt("--projects"), opt("--min-project"), opt("--min-kind"))), None)
    if not folder or not os.path.isdir(folder):
        print(__doc__)
        return 1
    projects = None
    if opt("--projects"):
        with open(opt("--projects"), encoding="utf-8") as f:
            projects = json.load(f).get("projects", {})
    res = evaluate(folder, projects)
    min_p = float(opt("--min-project", MIN_PROJECT))
    min_k = float(opt("--min-kind", MIN_KIND))

    misses = [r for r in res["rows"]
              if not r["project_ok"] or r["kind_ok"] is False or r["stray_sections"]]
    for r in misses:
        marks = []
        if not r["project_ok"]:
            marks.append("проект %s вместо %s (%.0f%%, %s)" % (
                r["got_project"], r["want_project"], (r["confidence"] or 0) * 100, r["why"]))
        if r["kind_ok"] is False:
            marks.append("вид «%s» вместо «%s»" % (r["got_kind"], r["want_kind"]))
        if r["stray_sections"]:
            marks.append("разделы в чужом проекте: %s" % ", ".join(r["stray_sections"][:3]))
        print("  × %s — %s" % (r["file"], "; ".join(marks)))
    print("\nЗаметок %d · точность по проекту %s%% (порог %s) · по виду %s%% (порог %s)" % (
        res["cases"], res["project_accuracy"], min_p, res["kind_accuracy"], min_k))
    print("Ошибки по проекту: в чужой проект %d · оставлено в «портфеле» на проверку %d"
          " · разделов в чужом проекте %d" % (res["misrouted"], res["unassigned"], res["stray_sections"]))
    ok = (res["project_accuracy"] or 0) >= min_p and (res["kind_accuracy"] or 0) >= min_k
    print("Порог пройден." if ok else "НИЖЕ ПОРОГА — правила раскладки нужно чинить, а не порог двигать.")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
