"""Профиль токенов сессий ролей (Л4): мерило — количество токенов, а не деньги.

По журналу каждой сессии запуска: старт (контекст первого хода — сколько модель прочитала
до первого действия), пик контекста, число ходов, запись и чтение кеша, выход, всего;
полные и повторные чтения файлов; сколько сберёг сторож чтений. Отчёт сводит это по ролям
и делит запуски на обычный и лёгкий старт — разница видна в цифрах, а не на словах.

    py tools/pm.py tokens [проект] [--role R] [--since ГГГГ-ММ-ДД] [--json]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import limits                 # noqa: E402
import read_guard             # noqa: E402
import workflow as w          # noqa: E402

FIELDS = ('start', 'peak', 'turns', 'input', 'cache_write', 'cache_read', 'output', 'total', 'full_reads', 'repeat_reads', 'saved')


def _claude(path):
    seen, turns, ids, reads = set(), [], {}, defaultdict(int)
    full = repeat = 0
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get('message') or {}
            if not isinstance(msg, dict):
                continue
            u = msg.get('usage')
            rid = row.get('requestId')
            if row.get('type') == 'assistant' and isinstance(u, dict) and rid and rid not in seen:
                seen.add(rid); turns.append(u)
            for b in msg.get('content') if isinstance(msg.get('content'), list) else []:
                if b.get('type') == 'tool_use' and b.get('name') == 'Read' and b.get('id') not in ids:
                    ids[b.get('id')] = 1                     # журнал пишет ответ дважды
                    inp = b.get('input') or {}
                    if not (inp.get('offset') or inp.get('limit')):
                        full += 1; reads[inp.get('file_path')] += 1
                        repeat += reads[inp.get('file_path')] > 1
    if not turns:
        return None
    ctx = [int(t.get('input_tokens') or 0) + int(t.get('cache_creation_input_tokens') or 0) + int(t.get('cache_read_input_tokens') or 0)
           for t in turns]
    tot = lambda k: sum(int(t.get(k) or 0) for t in turns)
    p = dict(start=ctx[0], peak=max(ctx), turns=len(turns), input=tot('input_tokens'), cache_write=tot('cache_creation_input_tokens'),
             cache_read=tot('cache_read_input_tokens'), output=tot('output_tokens'), full_reads=full, repeat_reads=repeat)
    p['total'] = p['input'] + p['cache_write'] + p['cache_read'] + p['output']
    return p


def _codex(path):
    ctx, top = [], None
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if '"token_count"' not in line:
                continue
            try:
                info = (json.loads(line).get('payload') or {}).get('info') or {}
            except ValueError:
                continue
            if isinstance(info, dict) and info.get('last_token_usage'):
                ctx.append(int(info['last_token_usage'].get('input_tokens') or 0)); top = info.get('total_token_usage') or top
    if not ctx or not top:
        return None
    inp, cached, out = int(top.get('input_tokens') or 0), int(top.get('cached_input_tokens') or 0), int(top.get('output_tokens') or 0)
    return dict(start=ctx[0], peak=max(ctx), last=ctx[-1], turns=len(ctx), input=inp - cached, cache_write=0, cache_read=cached,
                output=out, total=inp + out, full_reads=0, repeat_reads=0)


def _sessions(run):
    """Сессии запуска по порядку: после эстафеты или паузы в новой сессии их несколько."""
    out, seen = [], set()
    for entry in list(run.get('launches') or []) + [run.get('launch') or {}]:
        probe = dict(run, launch=dict(run.get('launch') or {}, **entry), launched_at=entry.get('at') or run.get('launched_at'))
        try:
            sid, path = limits.session_of(probe)
        except (OSError, ValueError):
            continue
        if path and str(path) not in seen:
            seen.add(str(path)); out.append((sid, path))
    return out


def profile(run):
    """Профиль токенов запуска по всем его сессиям или None — журнала нет.
    Старт — первой сессии, пик — наибольший, остальное — сумма; sessions — сколько сессий (эстафеты видны)."""
    agent = (run.get('launch') or {}).get('agent') or run.get('agent')
    total = None
    for sid, path in _sessions(run):
        p = (_codex if agent == 'codex' else _claude)(path)
        if not p:
            continue
        p['saved'] = int(read_guard.load(sid).get('saved_tokens') or 0) if sid else 0
        if total is None:
            total = dict(p, sessions=1)
            continue
        for k in FIELDS:
            if k == 'peak':
                total[k] = max(total[k], p[k])
            elif k != 'start':
                total[k] += p[k]
        total['sessions'] += 1
    return total


def _last_usage_claude(path, tail=400_000):
    """Контекст последнего хода Claude — по хвосту журнала, без чтения всего файла."""
    with open(path, 'rb') as fh:
        fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - tail))
        lines = fh.read().decode('utf-8', 'replace').splitlines()
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            u = (json.loads(line).get('message') or {}).get('usage')
        except ValueError:
            continue
        if isinstance(u, dict):
            return int(u.get('input_tokens') or 0) + int(u.get('cache_creation_input_tokens') or 0) + int(u.get('cache_read_input_tokens') or 0)
    return 0


def context_now(run):
    """Контекст последнего хода текущей сессии запуска: столько модель читает на каждом следующем шаге."""
    try:
        _sid, path = limits.session_of(run)
    except (OSError, ValueError):
        return 0
    if not path:
        return 0
    if ((run.get('launch') or {}).get('agent') or run.get('agent')) == 'codex':
        p = _codex(path)
        return 0 if not p else p['last']
    return _last_usage_claude(path)


def last_words(run, limit=1500):
    """Последние реплики модели в текущей сессии — сырьё для точки остановки, собранной сервером."""
    try:
        _sid, path = limits.session_of(run)
    except (OSError, ValueError):
        return ''
    if not path:
        return ''
    texts = []
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if '"text"' not in line and '"agent_message"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get('message') or {}
            if row.get('type') == 'assistant' and isinstance(msg.get('content'), list):
                texts += [b.get('text') for b in msg['content'] if b.get('type') == 'text' and b.get('text')]
            pay = row.get('payload') or {}
            if pay.get('type') == 'agent_message' and pay.get('message'):
                texts.append(pay['message'])
    out = []
    for t in reversed(texts):
        if t in out:
            continue                 # журнал Claude пишет ответ дважды
        out.insert(0, t)
        if sum(len(x) for x in out) >= limit:
            break
    return ' … '.join(out)[-limit:]


def collect(project=None, role=None, since=None):
    rows = []
    for r in w.state().get('runs') or []:
        if not r.get('launch') or (project and not r.get('ref', '').startswith(project + '/')):
            continue
        if role and (r.get('role') or 'исполнитель') != role:
            continue
        if since and (r.get('launched_at') or '') < since:
            continue
        p = profile(r)
        if p:
            rows.append(dict(p, role=r.get('role') or 'исполнитель', agent=(r['launch'].get('agent') or '?'),
                             lean=bool(r['launch'].get('lean')), relays=w.relays(r), run=r['id'], ref=r['ref']))
    return rows


def summary(rows):
    groups = defaultdict(list)
    for x in rows:
        groups[(x['role'], x['agent'], 'лёгкий' if x['lean'] else 'обычный')].append(x)
    out = []
    for (role, agent, start), xs in sorted(groups.items()):
        n = len(xs)
        out.append(dict(role=role, agent=agent, start_mode=start, runs=n, relays=sum(x.get('relays', 0) for x in xs),
                        **{k: int(sum(x[k] for x in xs) / n) for k in FIELDS}))
    return out


def fmt(n):
    return '%.2f млн' % (n / 1e6) if n >= 1e6 else '%.1f тыс' % (n / 1e3)


def report(rows):
    lines = ['Токены сессий ролей — среднее на запуск (всё = вход + запись кеша + чтение кеша + выход)', '',
             '%-20s %-6s %-8s %4s %10s %10s %6s %11s %11s %11s %6s %8s %6s' % (
                 'роль', 'ИИ', 'старт', 'N', 'контекст', 'пик', 'ходов', 'чтение кеша', 'выход', 'всего', 'повт.', 'сберёг', 'эстаф.')]
    for g in summary(rows):
        lines.append('%-20s %-6s %-8s %4d %10s %10s %6d %11s %11s %11s %6d %8s %6d' % (
            g['role'][:20], g['agent'], g['start_mode'], g['runs'], fmt(g['start']), fmt(g['peak']), g['turns'],
            fmt(g['cache_read']), fmt(g['output']), fmt(g['total']), g['repeat_reads'], fmt(g['saved']), g['relays']))
    if rows:
        tot = sum(x['total'] for x in rows); cr = sum(x['cache_read'] for x in rows)
        lines += ['', 'Запусков %d, всего %s токенов; из них чтение кеша %d%% — это контекст, перечитанный в каждом ходе.'
                  % (len(rows), fmt(tot), 100 * cr // max(1, tot)),
                  'Старт × ходы — главная статья: каждый токен стартового контекста оплачивается столько раз, сколько ходов в сессии.']
    else:
        lines.append('Нет запусков с журналом сессии.')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='pm.py tokens', description='Профиль токенов сессий ролей')
    ap.add_argument('project', nargs='?'); ap.add_argument('--role'); ap.add_argument('--since'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    rows = collect(a.project, a.role, a.since)
    print(json.dumps(dict(summary=summary(rows), runs=rows), ensure_ascii=False, indent=1) if a.json else report(rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
