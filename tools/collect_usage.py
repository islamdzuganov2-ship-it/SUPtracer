# -*- coding: utf-8 -*-
"""Фактические затраты токенов по проектам — из журналов сессий Claude Code.

Читает ~/.claude/projects/<slug>/*.jsonl, дедуплицирует ответы ассистента по
requestId (каждый ответ пишется в журнал дважды), складывает usage по проектам,
моделям и месяцам, считает денежный эквивалент по прайсу API. -> data/usage.json
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pricing import cost_usd  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def project_key_from_cwd(cwd):
    """Рабочий каталог сессии -> ключ проекта. Worktrees сворачиваются в родителя."""
    if not cwd:
        return ""
    p = cwd.replace("\\", "/").lower()
    m = re.search(r"/projects/([^/]+)", p)
    if m:
        name = m.group(1)
        if name.endswith(".worktrees"):
            name = name[: -len(".worktrees")]
        return name
    m = re.search(r"/documents/разработка/([^/]+)(?:/([^/]+))?", p)
    if m:
        # «проекты Cursor» — не проект, а папка-контейнер: берём следующий уровень
        if m.group(1).startswith("проекты") and m.group(2):
            return "doc:" + m.group(2)
        return "doc:" + m.group(1)
    m = re.search(r"/documents/([^/]+)", p)
    if m:
        return "doc:" + m.group(1)
    return os.path.basename(p.rstrip("/"))


def slug_to_key(slug):
    s = slug.lower()
    s = re.sub(r"--claude-worktrees-.*$", "", s)
    m = re.search(r"c--users-[^-]+-projects-(.+)$", s)
    if m:
        return m.group(1).replace("-", "_")
    return s


FIELDS = ("input", "output", "cache_write", "cache_write_5m", "cache_write_1h",
          "cache_read", "thinking", "web_search", "web_fetch", "requests", "sessions",
          "usd", "active_seconds")

# Пауза длиннее этого порога считается перерывом, а не работой: иначе забытая
# на ночь сессия даёт восемь часов «работы» там, где её не было.
IDLE_GAP_SECONDS = 15 * 60


def zero():
    return dict.fromkeys(FIELDS, 0)


def add(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def main():
    projects = defaultdict(zero)
    by_model = defaultdict(lambda: defaultdict(zero))
    by_month = defaultdict(lambda: defaultdict(zero))
    sess = defaultdict(set)
    span = {}
    # (проект, сессия) -> отсортированные отметки времени ответов
    beats = defaultdict(list)
    days = defaultdict(set)
    files_scanned = rows_total = rows_used = 0

    for slug in sorted(os.listdir(SESSIONS_DIR)):
        sdir = os.path.join(SESSIONS_DIR, slug)
        if not os.path.isdir(sdir):
            continue
        for fname in sorted(os.listdir(sdir)):
            if not fname.endswith(".jsonl"):
                continue
            files_scanned += 1
            seen = set()
            with open(os.path.join(sdir, fname), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    rows_total += 1
                    msg = d.get("message") or {}
                    rid = d.get("requestId") or msg.get("id")
                    u = msg.get("usage") or {}
                    if not rid or rid in seen or not u:
                        continue
                    seen.add(rid)
                    rows_used += 1

                    key = project_key_from_cwd(d.get("cwd", "")) or slug_to_key(slug)
                    model = msg.get("model") or "unknown"
                    ts = d.get("timestamp") or ""
                    month = ts[:7] if len(ts) >= 7 else "unknown"

                    st = u.get("server_tool_use") or {}
                    otd = u.get("output_tokens_details") or {}
                    cc = u.get("cache_creation") or {}
                    cw_total = int(u.get("cache_creation_input_tokens") or 0)
                    cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
                    cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
                    if cw5 + cw1h == 0 and cw_total:
                        cw5 = cw_total
                    cread = int(u.get("cache_read_input_tokens") or 0)
                    in_t = int(u.get("input_tokens") or 0)
                    out_t = int(u.get("output_tokens") or 0)

                    rec = {
                        "input": in_t,
                        "output": out_t,
                        "cache_write": cw_total,
                        "cache_write_5m": cw5,
                        "cache_write_1h": cw1h,
                        "cache_read": cread,
                        "thinking": int(otd.get("thinking_tokens") or 0),
                        "web_search": int(st.get("web_search_requests") or 0),
                        "web_fetch": int(st.get("web_fetch_requests") or 0),
                        "requests": 1,
                        "sessions": 0,
                        "usd": cost_usd(model, in_t, out_t, cw5, cw1h, cread),
                    }
                    add(projects[key], rec)
                    add(by_model[key][model], rec)
                    add(by_month[key][month], rec)
                    sid = d.get("sessionId") or fname
                    sess[key].add(sid)
                    if ts:
                        beats[(key, sid)].append(ts)
                        days[key].add(ts[:10])
                    if ts:
                        s = span.setdefault(key, [ts, ts])
                        if ts < s[0]:
                            s[0] = ts
                        if ts > s[1]:
                            s[1] = ts

    for key, ids in sess.items():
        projects[key]["sessions"] = len(ids)

    # Активное время: сумма промежутков между соседними ответами внутри сессии,
    # промежутки длиннее порога отбрасываются как перерыв.
    from datetime import datetime as _dt

    def _parse(t):
        try:
            return _dt.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    active = defaultdict(float)
    active_month = defaultdict(lambda: defaultdict(float))
    for (key, _sid), stamps in beats.items():
        pts = sorted(p for p in (_parse(t) for t in stamps) if p)
        for a, b in zip(pts, pts[1:]):
            gap = b - a
            if 0 < gap <= IDLE_GAP_SECONDS:
                active[key] += gap
                active_month[key][_dt.fromtimestamp(b, timezone.utc).strftime("%Y-%m")] += gap
    for key in projects:
        projects[key]["active_seconds"] = round(active.get(key, 0.0))

    def billable(p):
        return p["input"] + p["output"] + p["cache_write"] + p["cache_read"]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SESSIONS_DIR,
        "scan": {"files": files_scanned, "assistant_rows": rows_total, "deduped_rows": rows_used},
        "projects": {},
    }
    for key in sorted(projects, key=lambda k: -billable(projects[k])):
        p = dict(projects[key])
        p["billable"] = billable(p)
        p["usd"] = round(p["usd"], 2)
        p["first_seen"], p["last_seen"] = span.get(key, ["", ""])
        p["by_model"] = {m: dict(v, usd=round(v["usd"], 2)) for m, v in sorted(by_model[key].items())}
        p["by_month"] = {m: dict(v, usd=round(v["usd"], 2),
                                 active_seconds=round(active_month[key].get(m, 0.0)))
                         for m, v in sorted(by_month[key].items())}
        p["active_hours"] = round(p["active_seconds"] / 3600.0, 1)
        p["calendar_days"] = len(days.get(key, ()))
        p["hours_per_day"] = round(p["active_hours"] / p["calendar_days"], 1) if p["calendar_days"] else 0.0
        out["projects"][key] = p

    out["total"] = {
        "active_hours": round(sum(p["active_hours"] for p in out["projects"].values()), 1),
        "calendar_days": len(set().union(*days.values())) if days else 0,
        "tokens": sum(p["billable"] for p in out["projects"].values()),
        "usd": round(sum(p["usd"] for p in out["projects"].values()), 2),
        "output_tokens": sum(p["output"] for p in out["projects"].values()),
        "sessions": sum(p["sessions"] for p in out["projects"].values()),
    }

    dest = os.path.join(ROOT, "data", "usage.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    fmt = lambda n: "{:,}".format(int(n)).replace(",", " ")
    print("файлов %d · строк %d · после дедупа %d" % (files_scanned, rows_total, rows_used))
    print("проектов %d · токенов %s · $%s · активной работы %.0f ч за %d календарных дней" % (
        len(out["projects"]), fmt(out["total"]["tokens"]), fmt(out["total"]["usd"]),
        out["total"]["active_hours"], out["total"]["calendar_days"]))
    for k, p in out["projects"].items():
        print("  %-22s %14s  $%9.2f  %7.1f ч  дней %3d  сессий %3d" % (
            k, fmt(p["billable"]), p["usd"], p["active_hours"], p["calendar_days"], p["sessions"]))
    print("-> " + dest)


if __name__ == "__main__":
    main()
