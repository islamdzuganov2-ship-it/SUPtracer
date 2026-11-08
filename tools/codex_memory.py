# -*- coding: utf-8 -*-
"""Local GPT/Codex conversations -> memory. Never import tools or reasoning.

Session -> user requests -> final assistant responses. Imported responses remain
attributed assistant reports, not verified completion of the manual backlog.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone

import memory as mem
import mdparse as md
from ingest import split_body
from codex_usage import home_path
from collect_usage import project_key, usage_overrides


def stable(*parts):
    return "mem-codex-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def record_fingerprint(rec):
    fields = ("title", "body", "project", "kind", "parent", "root", "source", "tags",
              "role", "model", "cwd", "session", "foreign_project", "pin", "confidence")
    return md.digest_of(json.dumps({k: rec.get(k) or "" for k in fields},
                                  ensure_ascii=False, sort_keys=True))


def human_text(payload):
    text = "\n\n".join(p.get("text", "") for p in payload.get("content", [])
                       if isinstance(p, dict) and p.get("type") in ("input_text", "output_text"))
    return clean_text(text)


def clean_text(text):
    # The desktop may log ambient page/browser snapshots as separate user rows,
    # or prepend them to a genuine request. They are not user requirements.
    text = re.sub(r'<(?:external_codex_apps_open_page|in-app-browser-context|environment_context)\b[^>]*>[\s\S]*?</(?:external_codex_apps_open_page|in-app-browser-context|environment_context)>', '', text)
    text = re.sub(r'^\s*## My request:\s*', '', text)
    if text.lstrip().startswith(("# AGENTS.md instructions", "<environment_context>",
                                "<permissions instructions>", "<subagent_notification>",
                                "[Request interrupted", "You are a worker agent")):
        return ""
    return md.redact_secrets(text)[0].strip()


def read_sessions(home):
    sessions, names = {}, {}
    scan = {"files": 0, "unreadable_files": 0, "malformed_rows": 0}
    try:
        for line in (home / "session_index.jsonl").read_text(encoding="utf-8-sig").splitlines():
            try:
                r = json.loads(line)
                names[r["id"]] = md.redact_secrets(r.get("thread_name", ""))[0]
            except (ValueError, KeyError, TypeError):
                pass
    except OSError:
        pass
    for directory in (home / "sessions", home / "archived_sessions"):
        for path in sorted(directory.rglob("*.jsonl")) if directory.is_dir() else []:
            scan["files"] += 1
            owner, session, cwd, model = None, None, "", ""
            try:
                with path.open(encoding="utf-8-sig", errors="replace") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            scan["malformed_rows"] += 1
                            continue
                        if not isinstance(row, dict):
                            continue
                        p = row.get("payload") or {}
                        if not isinstance(p, dict):
                            continue
                        stamp = row.get("timestamp", "")
                        if row.get("type") == "session_meta" and owner is None:
                            owner = p
                            sid = str(p.get("id") or p.get("session_id") or path.stem)
                            source = p.get("source") or {}
                            spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
                            session = sessions.setdefault(sid, {"id": sid, "title": names.get(sid, ""),
                                "started": p.get("timestamp") or stamp, "cwd": p.get("cwd", ""),
                                "parent_session": spawn.get("parent_thread_id", ""), "messages": {}})
                            cwd = p.get("cwd", "")
                        elif session is None:
                            continue
                        elif row.get("type") == "turn_context":
                            # Forked histories precede the child's start and are not its work.
                            if session["parent_session"] and stamp < session["started"]:
                                continue
                            cwd, model = p.get("cwd") or cwd, p.get("model") or model
                        elif row.get("type") == "response_item" and p.get("type") == "message":
                            if session["parent_session"] and stamp < session["started"]:
                                continue
                            role = p.get("role")
                            if role not in ("user", "assistant"):
                                continue
                            if role == "assistant" and p.get("phase") not in (None, "final_answer"):
                                continue
                            if role == "assistant" and p.get("channel") not in (None, "final"):
                                continue
                            body = human_text(p)
                            if not body:
                                continue
                            mid = str(p.get("id") or stable(stamp, role, body))
                            session["messages"].setdefault(mid, {"id": mid, "timestamp": stamp,
                                "role": role, "body": body, "cwd": cwd, "model": model})
            except OSError:
                scan["unreadable_files"] += 1
    return sessions, scan


def import_sessions(home=None, dry_run=False):
    home = Path(home) if home is not None else home_path()
    sessions, scan = read_sessions(home)
    projects_path = Path(mem.DATA) / "projects.json"
    projects = json.loads(projects_path.read_text(encoding="utf-8")).get("projects", {}) if projects_path.exists() else {}
    overrides = usage_overrides()
    dest = Path(mem.DATA) / "codex_sessions.json"
    try:
        previous = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    descriptors = dict(previous.get("projects", {}))
    for key, descriptor in descriptors.items():
        descriptor['paths'] = [p for p in descriptor.get('paths', []) if project_key(p, overrides) == key]
        descriptor['registered'] = key in projects
    retained = {s["id"]: s for s in previous.get("sessions", [])}
    written, requests, answers = 0, 0, 0
    seen_roots = set()
    existing = {r["id"]: r for r in mem.load_all()} if Path(mem.MEM).is_dir() else {}
    session_projects = {sid: {project_key(m['cwd'] or session['cwd'], overrides) or 'портфель'
                        for m in session['messages'].values()} or
                        {project_key(session['cwd'], overrides) or 'портфель'}
                        for sid, session in sessions.items()}
    if not dry_run:
        for old in existing.values():
            if (old.get("source_provider") == "codex" and old.get("role") == "user" and
                    old.get("status") == "активна" and not clean_text(old["body"]) and
                    old.get("import_digest") == md.digest_of(old["body"])):
                old.update(status="устарела", retired="служебный контекст приложения, не запрос пользователя")
                mem.save(old)

    def store(rec):
        nonlocal written
        old = existing.get(rec["id"])
        # Imported records may be manually edited/forgotten. Ownership changes
        # are never used to overwrite a user's record.
        if old and (old.get("status") != "активна" or old.get("import_digest") != md.digest_of(old["body"]) or
                    (old.get("import_title") and old["title"] != old["import_title"]) or
                    (old.get("import_fingerprint") and old["import_fingerprint"] != record_fingerprint(old))):
            return
        rec["import_digest"] = md.digest_of(rec["body"])
        rec["import_title"] = rec["title"]
        rec["import_fingerprint"] = record_fingerprint(rec)
        if not dry_run:
            mem.save(rec)
        written += 1

    for sid, session in sorted(sessions.items()):
        groups = {}
        for message in sorted(session["messages"].values(), key=lambda m: (m["timestamp"], m["id"])):
            key = project_key(message["cwd"] or session["cwd"], overrides) or "портфель"
            groups.setdefault(key, []).append(message)
        if not groups and session["cwd"]:
            groups[project_key(session["cwd"], overrides) or "портфель"] = []
        session_keys = []
        for key, messages in groups.items():
            cwd = messages[0]["cwd"] if messages else session["cwd"]
            descriptors.setdefault(key, {"name": projects.get(key, {}).get("name") or key,
                                         "paths": [], "provider": "codex"})
            desc = descriptors[key]
            if cwd and cwd not in desc["paths"]:
                desc["paths"].append(cwd)
            desc["registered"] = key in projects
            session_keys.append(key)
            root = stable("session", sid, key)
            seen_roots.add(root)
            parent_sid = session["parent_session"]
            parent = stable("session", parent_sid, key) if key in session_projects.get(parent_sid, set()) else ""
            extra = {"project": key if key in projects else "портфель",
                     "foreign_project": key if key not in projects and key != "портфель" else "",
                     "source_provider": "codex", "session": sid, "cwd": cwd,
                     "source": "codex-session:" + sid, "tags": ["codex", "gpt"],
                     "created": session["started"][:10], "root": root}
            title = session["title"] or next((m["body"].splitlines()[0][:140] for m in messages if m["role"] == "user"), "Сессия GPT / Codex")
            store(mem.new_record("GPT / Codex · " + title,
                "Локальная сессия GPT / Codex. Запросы и ответы сохранены с указанием автора. "
                "Ответы ассистента не подтверждают выполнение задач бэклога.",
                kind="источник", id=root, parent=parent, **extra))
            last_request = root
            for m in messages:
                request = m["role"] == "user"
                if request:
                    requests += 1
                else:
                    answers += 1
                rid = stable("message", sid, m["id"])
                title = m["body"].splitlines()[0].strip().lstrip("# ")[:140] or "Сообщение"
                title = title if request else "Ответ GPT / Codex · " + title
                # Bodies are split using the existing memory size policy.
                for i, (heading, body) in enumerate(split_body(m["body"])):
                    part_id = rid if i == 0 else stable("part", rid, str(i))
                    store(mem.new_record(title if i == 0 else (heading or title)[:160], body,
                        kind="запрос" if request else "итог", id=part_id,
                        parent=(root if request else last_request) if i == 0 else rid,
                        **dict(extra, created=m["timestamp"][:10] or extra["created"],
                               root=rid if request else last_request, role=m["role"], model=m["model"], message_time=m["timestamp"])))
                if request:
                    last_request = rid
        prior = retained.get(sid, {})
        current_requests = sum(m["role"] == "user" for m in session["messages"].values())
        current_answers = sum(m["role"] == "assistant" for m in session["messages"].values())
        if previous.get("content_filter_version") == 2:
            current_requests = max(prior.get("requests", 0), current_requests)
            current_answers = max(prior.get("answers", 0), current_answers)
        retained[sid] = {"id": sid, "title": session["title"], "projects": sorted(set(session_keys)),
                         "parent_session": session["parent_session"],
                         "requests": current_requests, "answers": current_answers}
    rerouted_roots = 0
    if not dry_run:
        current = mem.load_all()
        referenced = {r.get('parent') for r in current if r.get('status') == 'активна'}
        for old in current:
            if (old.get('source_provider') == 'codex' and old.get('kind') == 'источник' and
                    old.get('session') in sessions and old['id'] not in seen_roots and
                    old['id'] not in referenced and old.get('status') == 'активна' and
                    old.get('import_fingerprint') == record_fingerprint(old)):
                old.update(status='заменена', retired='маршрутизация проекта исправлена по каталогу сессии')
                mem.save(old)
                rerouted_roots += 1
    out = {"schema": 2, "content_filter_version": 2, "rerouted_roots": rerouted_roots, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "projects": descriptors, "sessions": list(retained.values()), "scan": scan,
           "available": any((home / d).is_dir() for d in ("sessions", "archived_sessions")),
           "requests": sum(s.get("requests", 0) for s in retained.values()),
           "answers": sum(s.get("answers", 0) for s in retained.values())}
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out, written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report, count = import_sessions(dry_run=args.dry_run)
    print("GPT / Codex: сессий %d, запросов %d, ответов %d, записей %d%s" % (
        len(report["sessions"]), report["requests"], report["answers"], count,
        " (без записи)" if args.dry_run else ""))
