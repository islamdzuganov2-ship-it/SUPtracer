"""Раздел «Токены ролей» дашборда: куда уходят токены запусков конвейера — считается из журналов сессий и workflow, без ИИ.

Что даёт странице (build):
- runs — запуски с профилем токенов (tokens.profile): старт, пик, ходы, всего, сессии, эстафеты, полные чтения,
  обоснованные чтения (run.full_reads), ориентир бюджета; у последних SERIES_RUNS — контекст по ходам, сжатый
  до SERIES_POINTS точек (максимум корзины: пик не теряется), с началами новых сессий и ходами полных чтений;
- per_handoff — токены на одну принятую сдачу этапа по ролям и неделям: всё, что роль потратила за неделю
  (вместе с возвращёнными и упавшими запусками), делённое на число принятых сдач. Мерило полезной работы;
- compare — было и стало по каждой оптимизации (обычный и лёгкий старт, паспорт с INDEX.md и без):
  медианы по роли, чтобы сравнивать одинаковую работу;
- matrix — роль × этап: медиана токенов на запуск и число запусков; outliers — запуски вдвое дороже медианы своей роли.

    py tools/tokview.py [проект] — JSON в stdout (для проверки)
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tokens                 # noqa: E402
import workflow as w          # noqa: E402

SERIES_RUNS = 40              # кривые контекста — у последних запусков: старые нужны только в сводках
SERIES_POINTS = 120
OUTLIER = 2.0                 # запуск дороже медианы своей роли во столько раз — кандидат на разбор


def _week(iso):
    try:
        y, n, _ = datetime.fromisoformat(iso).isocalendar()
        return '%d-W%02d' % (y, n)
    except (TypeError, ValueError):
        return ''


def squeeze(series, marks, limit=SERIES_POINTS):
    """Ряд длиннее limit — максимумы корзин; отметки (номера ходов) пересчитываются в номера корзин."""
    n = len(series)
    if n <= limit:
        return list(series), list(marks), 1
    step = -(-n // limit)
    return [max(series[i:i + step]) for i in range(0, n, step)], sorted({m // step for m in marks}), step


def budget(run):
    route = (run.get('packet') or {}).get('route') or {}
    if route.get('budget_tokens'):
        return int(route['budget_tokens'])
    tier = (route.get('complexity') or {}).get('tier')       # старый паспорт без ориентира — по классу сложности
    from orchestra import RUN_BUDGET_TOKENS
    return RUN_BUDGET_TOKENS.get(tier) if tier else None


def row(run, prof, with_series):
    route = (run.get('packet') or {}).get('route') or {}
    justified = [r for r in run.get('full_reads') or [] if not r.get('free')]
    out = dict(run=run['id'], ref=run['ref'], project=run['ref'].split('/')[0], role=run.get('role') or 'исполнитель',
               stage=run.get('stage') or '', agent=(run.get('launch') or {}).get('agent') or run.get('agent') or '?',
               model=route.get('model') or run.get('model') or '', tier=(route.get('complexity') or {}).get('tier'),
               lean=bool((run.get('launch') or {}).get('lean')), index=bool((run.get('packet') or {}).get('task_index')),
               status=run.get('status'), accepted=run.get('status') == 'done' and bool(run.get('handoff')),
               at=run.get('launched_at') or run.get('created_at') or '', week=_week(run.get('launched_at') or ''),
               budget=budget(run), relays=w.relays(run), justified=len(justified),
               justified_tokens=sum(int(r.get('tokens') or 0) for r in justified),
               reasons=[(r.get('why') or '')[:160] for r in justified][:5],
               **{k: prof.get(k) or 0 for k in tokens.FIELDS + ('sessions',)})
    if with_series and prof.get('series'):
        marks = prof.get('breaks') or []
        pts, brk, step = squeeze(prof['series'], marks)
        _, reads, _ = squeeze(prof['series'], prof.get('read_at') or [])
        out.update(series=pts, breaks=brk, read_at=reads, step=step)
    return out


def _med(xs):
    return int(median(xs)) if xs else 0


def per_handoff(rows):
    """Токены на принятую сдачу: по роли и неделе — все токены роли за неделю / принятые сдачи."""
    g = defaultdict(lambda: dict(tokens=0, runs=0, accepted=0))
    for r in rows:
        for key in ((r['role'], r['week']), (r['role'], '')):
            x = g[key]; x['tokens'] += r['total']; x['runs'] += 1; x['accepted'] += r['accepted']
    out = []
    for (role, week), x in sorted(g.items()):
        out.append(dict(role=role, week=week, per=x['tokens'] // x['accepted'] if x['accepted'] else None, **x))
    return out


def compare(rows):
    """Было и стало: по каждой оптимизации — медианы по ролям в группах «без» и «с»."""
    out = []
    for key, title, labels in (('lean', 'Лёгкий старт сессии (TT-90)', ('обычный', 'лёгкий')),
                               ('index', 'Указатель INDEX.md в паспорте (TT-92)', ('без INDEX.md', 'с INDEX.md'))):
        roles = []
        for role in sorted({r['role'] for r in rows}):
            a = [r for r in rows if r['role'] == role and not r[key]]
            b = [r for r in rows if r['role'] == role and r[key]]
            side = lambda xs: dict(n=len(xs), start=_med([x['start'] for x in xs]), turns=_med([x['turns'] for x in xs]),
                                   total=_med([x['total'] for x in xs]))
            roles.append(dict(role=role, before=side(a), after=side(b)))
        out.append(dict(key=key, title=title, labels=labels, roles=roles,
                        after_runs=sum(1 for r in rows if r[key])))
    return out


def matrix(rows):
    g = defaultdict(list)
    for r in rows:
        g[(r['role'], r['stage'])].append(r['total'])
    return [dict(role=k[0], stage=k[1], n=len(v), median=_med(v), max=max(v)) for k, v in sorted(g.items())]


def outliers(rows, limit=12):
    med = {role: _med([x['total'] for x in rows if x['role'] == role]) for role in {r['role'] for r in rows}}
    out = [dict(run=r['run'], ref=r['ref'], role=r['role'], total=r['total'], median=med[r['role']],
                ratio=round(r['total'] / med[r['role']], 1), turns=r['turns'], peak=r['peak'], at=r['at'])
           for r in rows if med[r['role']] and r['total'] >= OUTLIER * med[r['role']]]
    return sorted(out, key=lambda x: -x['ratio'])[:limit]


def summary(rows):
    """Сводки раздела по набору запусков: весь портфель или один проект."""
    total = sum(r['total'] for r in rows)
    acc = sum(r['accepted'] for r in rows)
    kpi = dict(runs=len(rows), total=total, accepted=acc, per=total // acc if acc else None,
               cache_read=sum(r['cache_read'] for r in rows), relays=sum(r['relays'] for r in rows),
               justified=sum(r['justified'] for r in rows), saved=sum(r['saved'] for r in rows),
               over_budget=sum(1 for r in rows if r['budget'] and r['total'] > r['budget']))
    return dict(kpi=kpi, per_handoff=per_handoff(rows), compare=compare(rows), matrix=matrix(rows), outliers=outliers(rows))


def build(project=None):
    runs = [r for r in w.state().get('runs') or [] if r.get('launch')
            and (not project or r.get('ref', '').startswith(project + '/'))]
    runs.sort(key=lambda r: r.get('launched_at') or r.get('created_at') or '')
    recent = {r['id'] for r in runs[-SERIES_RUNS:]}
    rows = []
    for r in runs:
        prof = tokens.profile(r)
        if prof:
            rows.append(row(r, prof, r['id'] in recent))
    views = {'': summary(rows)}
    for proj in sorted({r['project'] for r in rows}):
        views[proj] = summary([r for r in rows if r['project'] == proj])
    return dict(generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                ceiling=w.settings().get('context_ceiling', w.CONTEXT_CEILING), runs=rows, views=views)

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    d = build(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(d['views'][''], ensure_ascii=False, indent=1))
