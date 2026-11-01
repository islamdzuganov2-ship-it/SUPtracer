"""Факт по задачам для дашборда (TT-95): что сделано, что нет, где стоит и что двигалось в окне.

Всё считается из записанного: бэклог с оперативным слоем (workflow snapshot), запуски ролей
(workflow.json → runs), история конвейера (handoff, stage_passed, return), даты закрытия
(completed_at) и журналы сессий (токены — tools/tokens.py). Ни одного вызова ИИ: тот же вход —
те же цифры. Сервер досок отдаёт это живым запросом /api/taskview, сборка дашборда — снимком.

    py tools/taskview.py [проект] [--hours 24]
"""
import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline as P          # noqa: E402
import workflow as w          # noqa: E402

POINTS = {'S': 1, 'M': 3, 'L': 8, 'XL': 20}
COLS = ['backlog'] + P.ORDER + ['done']
COL_TITLE = dict({'backlog': 'Вне конвейера', 'done': 'Сделано'}, **{s: P.STAGE[s]['title'] for s in P.ORDER})
# Порядок задаёт цветовой слот: «заблокировано» — второй, заметный цвет сразу после «сделано».
GROUPS = [('done', 'Сделано'), ('blocked', 'Заблокировано'), ('build', 'Реализация и выпуск'), ('analysis', 'Анализ'),
          ('idea', 'Идея'), ('backlog', 'Вне конвейера')]
LIVE = ('queued', 'running')
_profiles = {}


def stamp(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def column(t):
    """Колонка задачи: сделано, этап конвейера или «вне конвейера»."""
    if t.get('status') == 'done':
        return 'done'
    stage = (t.get('pipeline') or {}).get('stage')
    return stage if stage in P.ORDER else 'backlog'


def group(t):
    col = column(t)
    if col == 'done':
        return 'done'
    if t.get('status') == 'blocked':
        return 'blocked'
    if col in ('idea', 'analysis', 'backlog'):
        return col
    return 'build'


def progress(t):
    """Доля пройденного конвейера: этапы до текущего + доля сданных ролей текущего (0–100)."""
    if t.get('status') == 'done':
        return 100
    pipe = t.get('pipeline') or {}
    stage = pipe.get('stage')
    if stage not in P.ORDER:
        return 0
    roles = P.stage_roles(stage, pipe.get('needs'))
    done = sum(1 for r in roles if ((pipe.get('roles') or {}).get('%s/%s' % (stage, r)) or {}).get('state') == 'done')
    part = done / len(roles) if roles else 0
    return int(round(100 * (P.ORDER.index(stage) + part) / len(P.ORDER)))


def run_span(r, now):
    """(начало, конец) работы запуска; не открытый сессией — None."""
    start = stamp(r.get('launched_at') or r.get('claimed_at'))
    if not start:
        return None
    end = stamp(r.get('finished_at')) or (now if r.get('status') in LIVE else start)
    return start, max(start, end)


def run_tokens(r):
    """Токены запуска по журналу сессии, с кешем по времени изменения журнала."""
    import limits
    import tokens
    try:
        _sid, path = limits.session_of(r)
        key = (r['id'], str(path), os.path.getmtime(path)) if path else None
    except (OSError, ValueError, TypeError):
        return None
    if not key:
        return None
    if key not in _profiles:
        p = tokens.profile(r)
        _profiles[key] = p['total'] if p else None
    return _profiles[key]


def build(project=None, hours=24, now=None, with_tokens=True, top=40):
    now = now or datetime.now(timezone.utc)
    snap = w.snapshot()
    runs = w.state().get('runs') or []
    names = {k: (v.get('name') or k) for k, v in (snap.get('projects') or {}).items()}
    tasks = [t for t in snap['tasks'] if (not project or t['project'] == project) and t.get('status') != 'cancelled']
    cancelled = sum(1 for t in snap['tasks'] if (not project or t['project'] == project) and t.get('status') == 'cancelled')
    by_ref = {t['ref']: t for t in tasks}
    runs = [r for r in runs if r.get('ref') in by_ref]

    # --- сводка
    pts = lambda t: POINTS.get(t.get('size'), 1)
    live = [r for r in runs if r.get('status') in LIVE and r.get('launched_at')]
    waiting = [r for r in runs if r.get('status') in LIVE and not r.get('launched_at')]
    kpi = dict(total=len(tasks), done=sum(1 for t in tasks if t['status'] == 'done'),
               blocked=sum(1 for t in tasks if t['status'] == 'blocked'), cancelled=cancelled,
               pipeline=sum(1 for t in tasks if column(t) in P.ORDER),
               points_total=sum(pts(t) for t in tasks), points_done=sum(pts(t) for t in tasks if t['status'] == 'done'),
               working=len({r['ref'] for r in live}), waiting=len({r['ref'] for r in waiting}))

    # --- матрица проект × колонка и состав по группам (очки размера)
    rows = {}
    for t in tasks:
        row = rows.setdefault(t['project'], dict(key=t['project'], name=names.get(t['project'], t['project']),
                                                 counts={c: 0 for c in COLS}, blocked={c: 0 for c in COLS},
                                                 points={g: 0 for g, _ in GROUPS}, tasks=0))
        c = column(t)
        row['counts'][c] += 1; row['tasks'] += 1
        if t['status'] == 'blocked':
            row['blocked'][c] += 1
        row['points'][group(t)] += pts(t)
    matrix = sorted(rows.values(), key=lambda r: -r['tasks'])

    # --- поток по дням (14 дней): этапы сданы, задачи закрыты, возвраты
    days = [(now - timedelta(days=i)).date() for i in range(13, -1, -1)]
    flow = {k: [0] * len(days) for k in ('stage_passed', 'completed', 'return')}
    idx = {d: i for i, d in enumerate(days)}

    def bump(kind, at):
        d = stamp(at)
        if d and d.date() in idx:
            flow[kind][idx[d.date()]] += 1
    for t in tasks:
        for e in (t.get('pipeline') or {}).get('history') or []:
            if e.get('kind') in ('stage_passed', 'return'):
                bump(e['kind'], e.get('at'))
        if t['status'] == 'done':
            bump('completed', t.get('completed_at'))

    # --- время на этапах: сданные за 30 дней (работа запусков и ожидание) и возраст открытых
    since = now - timedelta(days=30)
    spans = {}
    for r in runs:
        s = run_span(r, now)
        if s and r.get('stage'):
            spans.setdefault((r['ref'], r['stage']), []).append(s)
    passed, ages = {s: [] for s in P.ORDER}, {s: [] for s in P.ORDER}
    for t in tasks:
        for st, info in ((t.get('pipeline') or {}).get('stages') or {}).items():
            if st not in passed:
                continue
            a, b = stamp(info.get('started')), stamp(info.get('passed_at'))
            if a and b and b >= since:
                total = (b - a).total_seconds() / 3600
                work = sum((y - x).total_seconds() for x, y in spans.get((t['ref'], st), [])) / 3600
                passed[st].append((min(work, total), max(0.0, total - work)))
            elif a and not b and st == (t.get('pipeline') or {}).get('stage') and t['status'] != 'done':
                ages[st].append((now - a).total_seconds() / 3600)
    med = lambda xs: round(statistics.median(xs), 1) if xs else 0
    stage_time = [dict(stage=s, title=P.STAGE[s]['title'], passed=len(passed[s]), work_h=med([x for x, _ in passed[s]]),
                       wait_h=med([y for _, y in passed[s]]), open=len(ages[s]), open_age_h=med(ages[s])) for s in P.ORDER]

    # --- окно: запуски и события по задачам
    ws = now - timedelta(hours=hours)
    active = {}

    def task_row(ref):
        t = by_ref[ref]
        return active.setdefault(ref, dict(ref=ref, title=t['title'], project=t['project'], size=t.get('size'),
                                           status=t['status'], column=column(t), progress=progress(t),
                                           runs=[], events=[], tokens=0, tokens_known=0))
    for r in runs:
        s = run_span(r, now)
        if not s or s[1] < ws or s[0] > now:
            continue
        tok = run_tokens(r) if with_tokens else None
        row = task_row(r['ref'])
        row['runs'].append(dict(id=r['id'], role=r.get('role') or 'исполнитель', agent=(r.get('launch') or {}).get('agent') or r.get('agent'),
                                stage=r.get('stage'), status=r.get('status'), start=s[0].isoformat(), end=s[1].isoformat(),
                                live=r.get('status') in LIVE, tokens=tok))
        if tok:
            row['tokens'] += tok; row['tokens_known'] += 1
    for ref, t in by_ref.items():
        for e in (t.get('pipeline') or {}).get('history') or []:
            at = stamp(e.get('at'))
            if at and ws <= at <= now and e.get('kind') in ('handoff', 'stage_passed', 'return'):
                task_row(ref)['events'].append(dict(kind=e['kind'], at=at.isoformat(), stage=e.get('stage'),
                                                   to_stage=e.get('to_stage'), role=e.get('role'), accepted=e.get('accepted')))
        done_at = stamp(t.get('completed_at'))
        if t['status'] == 'done' and done_at and ws <= done_at <= now:
            task_row(ref)['events'].append(dict(kind='completed', at=done_at.isoformat()))
    window = sorted(active.values(), key=lambda x: (-len(x['runs']) - len(x['events']), x['ref']))[:top]
    for x in window:
        x['runs'].sort(key=lambda r: r['start']); x['events'].sort(key=lambda e: e['at'])
    wsum = dict(hours=hours, start=ws.isoformat(), end=now.isoformat(), tasks=len(active),
                runs=sum(len(x['runs']) for x in active.values()),
                tokens=sum(x['tokens'] for x in active.values()),
                stage_passed=sum(1 for x in active.values() for e in x['events'] if e['kind'] == 'stage_passed'),
                returns=sum(1 for x in active.values() for e in x['events'] if e['kind'] == 'return'),
                completed=sum(1 for x in active.values() for e in x['events'] if e['kind'] == 'completed'))
    return dict(generated_at=now.isoformat(), project=project, kpi=kpi,
                cols=[dict(key=c, title=COL_TITLE[c]) for c in COLS], groups=[dict(key=g, title=n) for g, n in GROUPS],
                matrix=matrix, flow=dict(days=[d.isoformat() for d in days], **flow), stage_time=stage_time,
                window=dict(summary=wsum, tasks=window))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='taskview', description='Факт по задачам для дашборда')
    ap.add_argument('project', nargs='?'); ap.add_argument('--hours', type=int, default=24)
    a = ap.parse_args(argv)
    print(json.dumps(build(a.project, a.hours), ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
