# -*- coding: utf-8 -*-
"""Read only usage metadata from Codex rollout logs; never export conversation text.

Codex reports cumulative usage repeatedly (including rate-limit updates). Input
already includes cached input, and output already includes reasoning tokens.
Only positive changes of cumulative counters represent additional usage.
"""
import json
import os
from collections import defaultdict
from pathlib import Path
from pricing import openai_cost_usd


COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens")


def integer(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def home_path():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def read_records(home=None):
    """Return normalized token records and scan metadata (no prompts or outputs).

    Events from live/archived copies are merged by session and timestamp before
    calculating deltas, so a copied or resumed rollout cannot inflate usage.
    ``last_token_usage`` is deliberately not summed: it is repeated in later
    status events and is already part of ``total_token_usage``.
    """
    home = Path(home) if home is not None else home_path()
    roots = [home / "sessions", home / "archived_sessions"]
    scan = {"files": 0, "assistant_rows": 0, "deduped_rows": 0,
            "malformed_rows": 0, "unreadable_files": 0, "missing_totals": 0}
    sessions = defaultdict(list)
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            scan["files"] += 1
            sid, cwd, model = path.stem, "", "unknown"
            turn = ""
            owner_seen, fork_start = False, ""
            try:
                with path.open(encoding="utf-8-sig", errors="replace") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            scan["malformed_rows"] += 1
                            continue
                        if not isinstance(row, dict):
                            continue
                        payload = row.get("payload") or {}
                        if not isinstance(payload, dict):
                            continue
                        kind = row.get("type")
                        if kind == "session_meta" and not owner_seen:
                            owner_seen = True
                            sid = str(payload.get("id") or payload.get("session_id") or sid)
                            cwd = payload.get("cwd") or cwd
                            model = payload.get("model") or model
                            source = payload.get("source")
                            if isinstance(source, dict) and source.get("subagent"):
                                fork_start = payload.get("timestamp") or row.get("timestamp") or ""
                        elif fork_start and (row.get("timestamp") or "") < fork_start:
                            continue
                        elif kind == "turn_context":
                            cwd = payload.get("cwd") or cwd
                            model = payload.get("model") or model
                            turn = payload.get("turn_id") or turn
                        elif (kind == "event_msg" and payload.get("type") == "token_count") or kind == "token_usage_record":
                            scan["assistant_rows"] += 1
                            info = payload.get("info") or {}
                            totals = payload.get("thread_token_usage") if kind == "token_usage_record" else info.get("total_token_usage") if isinstance(info, dict) else None
                            if not isinstance(totals, dict):
                                scan["missing_totals"] += 1
                                continue
                            counters = tuple(integer(totals.get(k)) for k in COUNTERS)
                            last = payload.get("usage", {}) if kind == "token_usage_record" else info.get("last_token_usage", {})
                            if not isinstance(last, dict):
                                last = {}
                            # Keep only accounting metadata in memory.
                            sessions[sid].append((row.get("timestamp") or "", counters,
                                                  str(cwd), str(model), str(turn), integer(last.get("input_tokens")),
                                                  kind == "token_usage_record"))
            except OSError:
                scan["unreadable_files"] += 1

    records = []
    for sid, events in sessions.items():
        # Modern response records and legacy status events describe the same
        # accumulated usage. Merge before pricing: status events may omit writes.
        merged = {}
        for event in sorted(events, key=lambda e: e[0]):
            key = (event[1][0], event[1][3])
            old = merged.get(key)
            if old is None:
                merged[key] = event
            else:
                chosen = event if event[-1] and not old[-1] else old
                counters = tuple(max(a, b) for a, b in zip(old[1], event[1]))
                merged[key] = (chosen[0], counters, *chosen[2:])
        high_water = [0] * len(COUNTERS)
        long_gpt55 = any(e[3].startswith("gpt-5.5") and e[5] > 272000 for e in merged.values())
        for timestamp, current, cwd, model, _turn, request_input, _modern in sorted(merged.values(), key=lambda e: e[0]):
            delta = [max(0, n - old) for n, old in zip(current, high_water)]
            high_water = [max(n, old) for n, old in zip(current, high_water)]
            input_total, cached, cache_write, output, reasoning = delta
            if not input_total and not output:
                continue
            # Clamp auxiliary counters from partial/malformed events to their
            # parent total; never charge cached/reasoning tokens a second time.
            cached = min(cached, input_total)
            cache_write = min(cache_write, input_total - cached)
            cost = openai_cost_usd(model, input_total - cached - cache_write, output,
                                  cached, cache_write, max(request_input, 272001) if long_gpt55 and model.startswith("gpt-5.5") else request_input)
            records.append({
                "cwd": cwd, "model": model, "timestamp": timestamp, "session": sid,
                "input": input_total - cached - cache_write,
                "output": output, "cache_read": cached, "cache_write": cache_write,
                "cache_write_5m": 0, "cache_write_1h": 0,
                "thinking": min(reasoning, output), "web_search": 0, "web_fetch": 0,
                "requests": 1, "sessions": 0, "usd": cost or 0,
                "unpriced_tokens": input_total + output if cost is None else 0,
                "unpriced_requests": int(cost is None),
            })
            scan["deduped_rows"] += 1
    return records, scan, [str(p) for p in roots], any(p.is_dir() for p in roots)
