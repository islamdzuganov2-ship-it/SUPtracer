# -*- coding: utf-8 -*-
"""Учёт Claude Code и GPT / Codex с раздельными источниками в data/usage.json.

Старые поля projects/total сохраняются как сводка всех источников. usd — только
известная часть стоимости; cost_complete=False явно отмечает неполный прайс.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pricing import cost_usd  # noqa: E402
from pricing import OPENAI_CHECKED, OPENAI_SOURCE, OPENAI_PRICES  # noqa: E402
from project_routes import paths as route_paths, resolve_path, resolve_slug_entries  # noqa: E402
from codex_usage import integer, read_records as read_codex_records  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")   # ИНТ-01
SESSIONS_DIR = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or
                            os.path.join(os.path.expanduser("~"), ".claude"), "projects")


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


def usage_overrides():
    """Каталоги, расход сессий в которых принадлежит проекту, хотя каталог называется
    иначе: клон репозитория под заказчика, рабочая копия с другим именем. Задаются
    полем usage_paths проекта в projects.json. -> [(путь в нижнем регистре, ключ)],
    длинные пути первыми: вложенный каталог точнее родительского."""
    try:
        with open(os.path.join(DATA, "projects.json"), encoding="utf-8") as f:
            projects = json.load(f).get("projects", {})
    except (OSError, ValueError):
        return []
    return route_paths(projects)


def project_key(cwd, overrides=()):
    """Ключ проекта для сессии: сначала явные usage_paths, потом правило по пути."""
    result = resolve_path(cwd, overrides)
    if result['project']:
        return result['project']
    if result['method'].startswith('ambiguous'):
        return 'unassigned:ambiguous'
    wt = re.search(r"/\.codex/worktrees/[^/]+/([^/]+)(?:/|$)", (cwd or '').replace('\\','/').lower())
    if wt:
        return wt.group(1)
    return project_key_from_cwd(cwd)


def slug_to_key(slug):
    s = slug.lower()
    s = re.sub(r"--claude-worktrees-.*$", "", s)
    m = re.search(r"c--users-[^-]+-projects-(.+)$", s)
    if m:
        return m.group(1).replace("-", "_")
    return s


FIELDS = ("input", "output", "cache_write", "cache_write_5m", "cache_write_1h",
          "cache_read", "thinking", "web_search", "web_fetch", "requests", "sessions",
          "usd", "active_seconds", "unpriced_tokens", "unpriced_requests")

# Пауза длиннее этого порога считается перерывом, а не работой: иначе забытая
# на ночь сессия даёт восемь часов «работы» там, где её не было.
IDLE_GAP_SECONDS = 15 * 60


def zero():
    return dict.fromkeys(FIELDS, 0)


def add(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def read_claude_records(sessions_dir=None):
    """Preserve Claude's request-id deduplication, including copied logs."""
    from pathlib import Path
    root = Path(sessions_dir or SESSIONS_DIR)
    scan = {"files": 0, "assistant_rows": 0, "deduped_rows": 0,
            "malformed_rows": 0, "unreadable_files": 0}
    records, seen = [], set()
    if not root.is_dir():
        return records, scan, str(root), False
    for path in sorted(root.rglob("*.jsonl")):
        scan["files"] += 1
        slug = path.relative_to(root).parts[0]
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        scan["malformed_rows"] += 1
                        continue
                    if not isinstance(d, dict) or d.get("type") != "assistant":
                        continue
                    scan["assistant_rows"] += 1
                    msg = d.get("message") or {}
                    if not isinstance(msg, dict):
                        continue
                    rid = d.get("requestId") or msg.get("id")
                    u = msg.get("usage") or {}
                    if not rid or rid in seen or not isinstance(u, dict) or not u:
                        continue
                    seen.add(rid)
                    st = u.get("server_tool_use") or {}
                    otd = u.get("output_tokens_details") or {}
                    cc = u.get("cache_creation") or {}
                    cw_total = integer(u.get("cache_creation_input_tokens"))
                    cw5 = integer(cc.get("ephemeral_5m_input_tokens"))
                    cw1h = integer(cc.get("ephemeral_1h_input_tokens"))
                    if cw5 + cw1h == 0 and cw_total:
                        cw5 = cw_total
                    cread = integer(u.get("cache_read_input_tokens"))
                    in_t, out_t = integer(u.get("input_tokens")), integer(u.get("output_tokens"))
                    model = msg.get("model") or "unknown"
                    records.append({
                        "cwd": d.get("cwd") or "", "fallback_key": slug_to_key(slug), "source_slug": slug,
                        "model": model, "timestamp": d.get("timestamp") or "",
                        "session": d.get("sessionId") or path.stem,
                        "input": in_t, "output": out_t, "cache_write": cw_total,
                        "cache_write_5m": cw5, "cache_write_1h": cw1h, "cache_read": cread,
                        "thinking": integer(otd.get("thinking_tokens")),
                        "web_search": integer(st.get("web_search_requests")),
                        "web_fetch": integer(st.get("web_fetch_requests")),
                        "requests": 1, "sessions": 0,
                        "usd": cost_usd(model, in_t, out_t, cw5, cw1h, cread),
                    })
                    scan["deduped_rows"] += 1
        except OSError:
            scan["unreadable_files"] += 1
    return records, scan, str(root), True


def billable(value):
    return sum(value.get(k, 0) for k in ("input", "output", "cache_write", "cache_read"))


def finish(value, unknown=()):
    result = dict(value)
    result["billable"] = billable(result)
    result["usd"] = round(result.get("usd", 0), 2)
    result["cost_complete"] = not result.get("unpriced_requests", 0)
    result["unknown_models"] = sorted(set(unknown))
    return result


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def totals(projects):
    values = list(projects.values())
    dates = set().union(*(set(p.get("activity_dates", [])) for p in values)) if values else set()
    return {
        "active_hours": round(sum(p.get("active_seconds", 0) for p in values) / 3600, 1),
        "calendar_days": len(dates) or max((p.get("calendar_days", 0) for p in values), default=0),
        "tokens": sum(p.get("billable", 0) for p in values),
        "usd": round(sum(p.get("usd", 0) for p in values), 2),
        "output_tokens": sum(p.get("output", 0) for p in values),
        "sessions": sum(p.get("sessions", 0) for p in values),
        "cost_complete": all(p.get("cost_complete", True) for p in values),
        "unpriced_tokens": sum(p.get("unpriced_tokens", 0) for p in values),
        "unpriced_requests": sum(p.get("unpriced_requests", 0) for p in values),
        "unknown_models": sorted({m for p in values for m in p.get("unknown_models", [])}),
    }


def aggregate(records, provider, scan, source, available, overrides=()):
    projects = defaultdict(zero)
    models, months = defaultdict(lambda: defaultdict(zero)), defaultdict(lambda: defaultdict(zero))
    sessions, model_sessions, month_sessions = defaultdict(set), defaultdict(set), defaultdict(set)
    stamps, dates, unknown = defaultdict(list), defaultdict(set), defaultdict(set)
    spans = {}
    for rec in records:
        key = (project_key(rec["cwd"], overrides) if rec['cwd'] else
               resolve_slug_entries(rec.get('source_slug', ''), overrides, 'unassigned:ambiguous')) or rec.get("fallback_key") or "unassigned:" + provider
        model, ts, sid = rec["model"], rec["timestamp"], rec["session"]
        month = ts[:7] if parse_time(ts) is not None else "unknown"
        numeric = {k: rec.get(k, 0) for k in FIELDS}
        add(projects[key], numeric)
        add(models[key][model], numeric)
        add(months[key][month], numeric)
        sessions[key].add(sid)
        model_sessions[(key, model)].add(sid)
        month_sessions[(key, month)].add(sid)
        if rec.get("unpriced_requests"):
            unknown[key].add(model)
        point = parse_time(ts)
        if point is not None:
            stamps[(key, sid)].append(point)
            dates[key].add(datetime.fromtimestamp(point, timezone.utc).date().isoformat())
            span = spans.setdefault(key, [ts, ts])
            span[0], span[1] = min(span[0], ts), max(span[1], ts)

    for (key, _sid), points in stamps.items():
        points = sorted(set(points))
        for before, after in zip(points, points[1:]):
            gap = after - before
            if 0 < gap <= IDLE_GAP_SECONDS:
                projects[key]["active_seconds"] += gap
                month = datetime.fromtimestamp(after, timezone.utc).strftime("%Y-%m")
                months[key][month]["active_seconds"] += gap

    output = {}
    for key in sorted(projects, key=lambda k: -billable(projects[k])):
        p = finish(projects[key], unknown[key])
        p["sessions"] = len(sessions[key])
        p["active_seconds"] = round(p["active_seconds"])
        p["active_hours"] = round(p["active_seconds"] / 3600, 1)
        p["activity_dates"] = sorted(dates[key])
        p["calendar_days"] = len(dates[key])
        p["hours_per_day"] = round(p["active_hours"] / p["calendar_days"], 1) if p["calendar_days"] else 0
        p["first_seen"], p["last_seen"] = spans.get(key, ["", ""])
        p["by_model"] = {m: finish(dict(v, sessions=len(model_sessions[(key, m)])),
                                    [m] if v.get("unpriced_requests") else [])
                         for m, v in sorted(models[key].items())}
        p["by_month"] = {m: finish(dict(v, sessions=len(month_sessions[(key, m)]),
                                         active_seconds=round(v["active_seconds"])), unknown[key])
                         for m, v in sorted(months[key].items())}
        output[key] = p
    return {"label": "Claude" if provider == "claude" else "GPT / Codex",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source, "available": available, "scan": scan,
            "projects": output, "total": totals(output)}


def merge_values(values):
    result = zero()
    for value in values:
        add(result, {k: value.get(k, 0) for k in FIELDS})
    result = finish(result, (m for value in values for m in value.get("unknown_models", [])))
    result["cost_complete"] = all(v.get("cost_complete", True) for v in values)
    return result


def combine(providers):
    keys = {key for provider in providers.values() for key in provider.get("projects", {})}
    projects = {}
    for key in keys:
        by_provider = {name: provider["projects"][key] for name, provider in providers.items()
                       if key in provider.get("projects", {})}
        values = list(by_provider.values())
        p = merge_values(values)
        p["by_provider"] = by_provider
        for group in ("by_model", "by_month"):
            names = {name for v in values for name in v.get(group, {})}
            p[group] = {name: merge_values([v[group][name] for v in values if name in v.get(group, {})])
                        for name in sorted(names)}
        p["first_seen"] = min((v.get("first_seen", "") for v in values if v.get("first_seen")), default="")
        p["last_seen"] = max((v.get("last_seen", "") for v in values), default="")
        p["active_seconds"] = round(p["active_seconds"])
        p["active_hours"] = round(p["active_seconds"] / 3600, 1)
        p["activity_dates"] = sorted({d for v in values for d in v.get("activity_dates", [])})
        p["calendar_days"] = len(p["activity_dates"]) or max((v.get("calendar_days", 0) for v in values), default=0)
        p["hours_per_day"] = round(p["active_hours"] / p["calendar_days"], 1) if p["calendar_days"] else 0
        projects[key] = p
    return dict(sorted(projects.items(), key=lambda item: -item[1]["billable"]))


def collect(source="all", previous=None, claude_dir=None, codex_home=None):
    if source not in ("all", "claude", "codex"):
        raise ValueError("Неизвестный источник: " + source)
    previous = previous or {}
    providers = dict(previous.get("providers") or {})
    if not providers and previous.get("projects"):
        # One-time migration: historical usage.json was Claude-only.
        providers["claude"] = {k: previous[k] for k in ("source", "scan", "projects", "total", "generated_at")
                               if k in previous}
        providers["claude"].update(label="Claude", available=True)
    # Старый сборщик использовал имя каталога в нижнем регистре, новый — ключ
    # проекта. При выборочном первом сборе две формы не должны скрыть друг друга.
    try:
        with open(os.path.join(DATA, "projects.json"), encoding="utf-8") as fh:
            known = json.load(fh).get("projects", {})
    except (OSError, ValueError):
        known = {}
    aliases = defaultdict(set)
    for key, project in known.items():
        base = str(project.get("path", "")).replace("\\", "/").rstrip("/").split("/")[-1].lower()
        for alias in (key.lower(), base, "doc:" + base, "doc:" + base.replace("_", "-")):
            if alias:
                aliases[alias].add(key)
    for name, provider in list(providers.items()):
        old = provider.get("projects", {})
        normalized = {}
        changed = False
        for index, (key, value) in enumerate(old.items()):
            candidates = aliases.get(key.lower(), set())
            target = key if key in known or len(candidates) != 1 else next(iter(candidates))
            changed |= target != key
            normalized[str(index)] = {"projects": {target: value}}
        if changed:
            merged = combine(normalized)
            for value in merged.values():
                value.pop("by_provider", None)
            providers[name] = dict(provider, projects=merged, total=totals(merged))
    overrides = usage_overrides()
    routing_digest = hashlib.sha256(json.dumps(overrides, ensure_ascii=False).encode('utf-8')).hexdigest()
    for name in ("claude", "codex"):
        if source not in ("all", name):
            if name in providers and providers[name].get('projects') and providers[name].get('routing_digest') != routing_digest:
                providers[name] = dict(providers[name], routing_stale=True)
            continue
        records, scan, path, available = (read_claude_records(claude_dir) if name == "claude"
                                           else read_codex_records(codex_home))
        if (not available or scan.get("unreadable_files")) and name in providers:
            # Временно недоступный каталог не обнуляет уже собранную историю.
            providers[name] = dict(providers[name], available=available, stale=True,
                                   last_attempt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   last_scan=scan)
            continue
        providers[name] = dict(aggregate(records, name, scan, path, available, overrides), routing_digest=routing_digest)
    projects = combine(providers)
    scan = {}
    for provider in providers.values():
        add(scan, provider.get("scan", {}))
    return {"schema": 2, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pricing": {"openai_checked": OPENAI_CHECKED, "openai_source": OPENAI_SOURCE,
                        "openai_rates": OPENAI_PRICES,
                        "basis": "Standard API-equivalent; not a subscription invoice"},
            "source": "Claude Code + Codex", "selected_source": source, "scan": scan,
            "providers": providers, "projects": projects, "total": totals(projects)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("all", "claude", "codex"), default="all")
    args = parser.parse_args(argv)
    dest = os.path.join(DATA, "usage.json")
    try:
        with open(dest, encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        previous = {}
    out = collect(args.source, previous)
    os.makedirs(DATA, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="usage-", suffix=".json", dir=DATA)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    total = out["total"]
    cost = "$%.2f" % total["usd"]
    if not total["cost_complete"]:
        cost += " + неизвестная стоимость %s токенов" % format(total["unpriced_tokens"], ",")
    print("источник %s · файлов %d · токенов %s · %s" % (
        args.source, out["scan"].get("files", 0), format(total["tokens"], ","), cost))
    print("проектов %d · сессий %d · активной работы %.1f ч" % (
        len(out["projects"]), total["sessions"], total["active_hours"]))
    print("-> " + dest)


if __name__ == "__main__":
    main()
