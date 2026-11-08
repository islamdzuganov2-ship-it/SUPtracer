"""Исчерпание лимита токенов — по журналам сессий Claude Code и Codex, без ИИ.

Claude Code пишет отказ по лимиту строкой ассистента с error=rate_limit и quotaLimits.resetsAt
(время сброса, секунды эпохи). Codex — task_complete с codex_error_info=usage_limit_exceeded;
время сброса — в последних rate_limits (primary — пятичасовое окно, secondary — недельное).

Сервер досок по этим событиям ставит запуск роли на паузу и возобновляет ту же сессию
после сброса лимита. Читаются только метаданные и текст ошибки, переписка не копируется.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOW = timedelta(hours=5)       # окно лимита; без известного времени сброса — пауза на 5 часов
MARGIN = timedelta(minutes=2)     # после сброса: часы сервера и провайдера могут расходиться
TAIL = 400_000                    # байт с конца журнала: лимит ищется среди свежих строк


def claude_home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()


def stamp(value):
    """ISO-время или секунды эпохи → aware datetime."""
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def tail_lines(path):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - TAIL))
            data = f.read()
    except OSError:
        return []
    lines = data.decode('utf-8', errors='replace').splitlines()
    return lines[1:] if size > TAIL else lines


def claude_event(row):
    """Строка журнала Claude → событие лимита или None."""
    if row.get('type') != 'assistant' or row.get('error') != 'rate_limit':
        return None
    q = row.get('quotaLimits') or {}
    msg = row.get('message') or {}
    text = ''.join(c.get('text', '') for c in msg.get('content') or [] if isinstance(c, dict))
    return dict(provider='claude', at=row.get('timestamp') or '', resets_at=iso(stamp(q.get('resetsAt'))),
                kind=q.get('rateLimitType') or '', message=text[:200], session=row.get('sessionId') or '',
                cwd=row.get('cwd') or '')


def codex_reset(rate_limits):
    """Время сброса из rate_limits Codex: исчерпанное окно, иначе пятичасовое."""
    if not isinstance(rate_limits, dict):
        return None, ''
    windows = [(name, rate_limits.get(name)) for name in ('secondary', 'primary')]
    windows = [(n, w) for n, w in windows if isinstance(w, dict) and w.get('resets_at')]
    full = [(n, w) for n, w in windows if (w.get('used_percent') or 0) >= 100]
    name, win = (full or [x for x in windows if x[0] == 'primary'] or windows or [(None, None)])[0]
    if not win:
        return None, ''
    return iso(stamp(win.get('resets_at'))), 'five_hour' if name == 'primary' else 'seven_day'


def iso(value):
    return value.isoformat(timespec='seconds') if value else None


def scan_claude(path, since=None):
    """Последнее событие лимита в журнале сессии Claude и есть ли после него работа."""
    last, after = None, False
    for line in tail_lines(path):
        if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        e = claude_event(row)
        if e:
            if since and (stamp(e['at']) or since) < since:
                continue
            last, after = e, False
        elif last and ((row.get('message') or {}).get('usage') or {}).get('output_tokens'):
            after = True
    return None if not last or after else last


def scan_codex(path, since=None):
    last, after, limits, sid, cwd = None, False, None, '', ''
    for line in tail_lines(path):
        if not any(k in line for k in ('rate_limits', 'task_complete', 'session_meta')):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        p = row.get('payload') or {}
        if row.get('type') == 'session_meta':
            sid, cwd = str(p.get('id') or sid), p.get('cwd') or cwd
        if isinstance(p.get('rate_limits'), dict):
            limits = p['rate_limits']
        if p.get('type') == 'task_complete':
            err = p.get('error') or {}
            if err.get('codex_error_info') == 'usage_limit_exceeded':
                if since and (stamp(row.get('timestamp')) or since) < since:
                    continue
                resets, kind = codex_reset(limits)
                last, after = dict(provider='codex', at=row.get('timestamp') or '', resets_at=resets, kind=kind,
                                   message=str(err.get('message') or '')[:200], session=sid, cwd=cwd), False
            elif last:
                after = True
    return None if not last or after else last


def claude_session_file(session):
    if not session:
        return None
    return next(iter(sorted((claude_home() / 'projects').glob('*/%s.jsonl' % session))), None)


def codex_session(cwd, since):
    """Сессия Codex, открытая в этой папке после since: (id, путь) или (None, None)."""
    root = codex_home() / 'sessions'
    want = os.path.normcase(os.path.normpath(cwd or ''))
    edge = since.timestamp() - 60 if since else 0
    try:
        files = [p for p in root.glob('*/*/*/rollout-*.jsonl') if p.stat().st_mtime >= edge]
    except OSError:
        return None, None
    best = None
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                first = f.readline()
            row = json.loads(first)
        except (OSError, ValueError):
            continue
        p = row.get('payload') or {}
        started = stamp(p.get('timestamp') or row.get('timestamp'))
        if row.get('type') != 'session_meta' or os.path.normcase(os.path.normpath(p.get('cwd') or '')) != want:
            continue
        if since and started and started < since - timedelta(minutes=1):
            continue
        if best is None or (started and started < best[2]):   # первая сессия после запуска — наша
            best = (str(p.get('id') or ''), path, started or datetime.max.replace(tzinfo=timezone.utc))
    return (best[0], best[1]) if best else (None, None)


def codex_index(home=None):
    """Все сессии Codex: (папка, начало, id) — по первой строке журнала, переписка не читается."""
    root = Path(home) if home else codex_home()
    out = []
    for path in list((root / 'sessions').glob('*/*/*/rollout-*.jsonl')) + list((root / 'archived_sessions').glob('*.jsonl')):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                row = json.loads(f.readline())
        except (OSError, ValueError):
            continue
        p = row.get('payload') or {}
        if row.get('type') == 'session_meta' and p.get('id'):
            out.append((os.path.normcase(os.path.normpath(p.get('cwd') or '')), stamp(p.get('timestamp') or row.get('timestamp')), str(p['id'])))
    return out


def match_codex(index, cwd, since, window=timedelta(minutes=10)):
    """Сессия Codex, начатая в этой папке в течение окна после запуска."""
    want = os.path.normcase(os.path.normpath(cwd or ''))
    hits = sorted((st, sid) for c, st, sid in index if c == want and st and since and since - timedelta(minutes=1) <= st <= since + window)
    return hits[0][1] if hits else None


def session_cost(run):
    """(токены, $) сессии запуска по её журналу — для сторожа бюджета. Нет журнала — (0, 0.0)."""
    sid, path = session_of(run)
    if not path:
        return 0, 0.0
    agent = (run.get('launch') or {}).get('agent') or run.get('agent')
    try:
        fh = open(path, encoding='utf-8', errors='replace')
    except OSError:
        return 0, 0.0
    tokens, usd = 0, 0.0
    with fh:
        if agent == 'codex':
            from codex_usage import COUNTERS
            from pricing import openai_cost_usd
            top = [0] * len(COUNTERS)
            for line in fh:
                if '"token_count"' not in line and '"token_usage_record"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                p = row.get('payload') or {}
                tot = p.get('thread_token_usage') or ((p.get('info') or {}).get('total_token_usage') if isinstance(p.get('info'), dict) else None)
                if isinstance(tot, dict):
                    top = [max(a, int(tot.get(k) or 0)) for a, k in zip(top, COUNTERS)]
            inp, cached, cw, out = top[0], min(top[1], top[0]), top[2], top[3]
            model = ((run.get('packet') or {}).get('route') or {}).get('model') or run.get('model') or ''
            usd = openai_cost_usd(model, max(0, inp - cached - cw), out, cached, cw) or 0.0
            return inp + out, round(usd, 4)
        from pricing import cost_usd
        seen = set()
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get('message') or {}
            u = msg.get('usage') if isinstance(msg, dict) else None
            rid = row.get('requestId') or msg.get('id')
            if row.get('type') != 'assistant' or not isinstance(u, dict) or not rid or rid in seen:
                continue
            seen.add(rid)
            cc = u.get('cache_creation') or {}
            cw5, cw1 = int(cc.get('ephemeral_5m_input_tokens') or 0), int(cc.get('ephemeral_1h_input_tokens') or 0)
            if not cw5 + cw1:
                cw5 = int(u.get('cache_creation_input_tokens') or 0)
            i, o, cr = int(u.get('input_tokens') or 0), int(u.get('output_tokens') or 0), int(u.get('cache_read_input_tokens') or 0)
            tokens += i + o + cw5 + cw1 + cr
            usd += cost_usd(msg.get('model') or '', i, o, cw5, cw1, cr) or 0.0
    return tokens, round(usd, 4)


def session_of(run):
    """Идентификатор и журнал сессии, открытой сервером для запуска."""
    launch = run.get('launch') or {}
    if launch.get('agent') == 'claude' or (launch.get('session') and launch.get('agent') != 'codex'):
        return launch.get('session'), claude_session_file(launch.get('session'))
    if launch.get('agent') == 'codex':
        if launch.get('session'):
            sid = launch['session']
            path = next(iter((codex_home() / 'sessions').glob('*/*/*/rollout-*%s.jsonl' % sid)), None)
            if path:
                return sid, path
        return codex_session(launch.get('cwd'), stamp(run.get('launched_at')))
    return None, None


def success_at(run, since):
    """Время успешного ответа после отказа, по журналу именно этой сессии."""
    _, path = session_of(run)
    if not path:
        return None
    latest = None
    agent = (run.get('launch') or {}).get('agent') or run.get('agent')
    for line in tail_lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = stamp(row.get('timestamp'))
        if not at or at <= since:
            continue
        if agent == 'claude' and row.get('type') == 'assistant':
            if row.get('error'):
                latest = None
            elif ((row.get('message') or {}).get('usage') or {}).get('output_tokens', 0) > 0:
                latest = at
        elif agent == 'codex' and row.get('type') == 'event_msg':
            event = row.get('payload') or {}
            if event.get('type') == 'task_complete':
                latest = None if event.get('error') else at
    return latest


def detect(run):
    """Событие лимита в сессии запуска после его последнего (воз)обновления, иначе None."""
    session, path = session_of(run)
    if not path:
        return None
    since = stamp((run.get('pauses') or [{}])[-1].get('resumed_at')) or stamp(run.get('launched_at'))
    event = (scan_codex if (run.get('launch') or {}).get('agent') == 'codex' else scan_claude)(path, since)
    if event:
        event['session'] = event.get('session') or session
    return event


def resume_at(event, at):
    """Когда возобновлять: сброс лимита из журнала (+2 мин), без него — через 5 часов."""
    resets = stamp((event or {}).get('resets_at'))
    if resets:
        return max(resets, at) + MARGIN
    return at + WINDOW
