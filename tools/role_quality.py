"""Качество ролей из истории конвейера (07: A11-02, A11-03, AG-*-10, A0-06).

Не число сообщений и не доля approve, а то, что видно по последствиям:
- ревьюер и QA: найденные дефекты, пропущенные дальше по конвейеру (escaped), recall = найдено / (найдено + пропущено),
  ложные срабатывания — дефекты, которые разработчик доказал как «не дефект» (fix.method = not_a_defect);
- тимлид: доля ворот approve, после которых задачу вернули на этап до ворот (ошибочные ворота);
- Решала: доля решений, после которых по той же задаче и роли снова открылась проблема (промах маршрута);
- разработчик: сдача этапа с первой попытки — считает metrics.quality;
- наблюдательное сравнение (не A/B): пропущенные дефекты на задачу с двойным ревью и без. Малые выборки помечены.
- дублирование ролей: общие артефакты и одинаковые пункты DoD у разных ролей.
"""
from collections import Counter, defaultdict

LATE = {'reviewer': ('testing', 'deploy_test', 'deploy_main'), 'qa': ('deploy_main',)}
GATE_BEFORE = {'analysis': ('idea', 'analysis'), 'release': ('development', 'review', 'deploy_test', 'testing')}
MIN_SAMPLE = 5


def _rate(num, den):
    return dict(num=num, den=den, value=round(num / den, 3) if den else None, small=den < MIN_SAMPLE)


def _passed(hist, stage):
    return any(h.get('kind') == 'stage_passed' and h.get('stage') == stage for h in hist)


def finders(tasks):
    """Ревьюер и QA: найдено, пропущено, ложные срабатывания."""
    out = {}
    for role, late in LATE.items():
        found = escaped = fp = 0
        by_agent = defaultdict(lambda: dict(found=0, escaped=0, fp=0))
        for t in tasks:
            pipe = t.get('pipeline') or {}
            hist = pipe.get('history') or []
            stage = 'review' if role == 'reviewer' else 'testing'
            approver = next((h.get('agent') for h in hist if h.get('kind') == 'handoff' and h.get('role') == role and h.get('accepted')), None)
            for d in pipe.get('defects') or []:
                if d.get('found_by') == role:
                    found += 1; by_agent[d.get('found_agent') or '?']['found'] += 1
                    if (d.get('fix') or {}).get('method') == 'not_a_defect':
                        fp += 1; by_agent[d.get('found_agent') or '?']['fp'] += 1
                elif d.get('found_stage') in late and _passed(hist, stage):
                    escaped += 1; by_agent[approver or '?']['escaped'] += 1
        out[role] = dict(found=found, escaped=escaped, false_positive=fp,
                         recall=_rate(found, found + escaped), false_positive_rate=_rate(fp, found),
                         by_agent={a: dict(v, recall=_rate(v['found'], v['found'] + v['escaped'])) for a, v in by_agent.items()})
    return out


def teamlead(tasks):
    approved = wrong = 0
    for t in tasks:
        hist = (t.get('pipeline') or {}).get('history') or []
        for i, h in enumerate(hist):
            if h.get('kind') != 'gate' or h.get('decision') != 'approve':
                continue
            approved += 1
            back = GATE_BEFORE.get(h.get('gate'), ())
            if any(x.get('kind') == 'return' and x.get('to_stage') in back for x in hist[i + 1:]):
                wrong += 1
    return dict(approved=approved, wrong=wrong, wrong_gate_rate=_rate(wrong, approved))


def resolver(problems):
    decided = [p for p in problems if p.get('decided_at') and p.get('decision') in ('continue', 'return')]
    miss = 0
    for p in decided:
        if any(q is not p and q.get('ref') == p.get('ref') and q.get('role') == p.get('role') and (q.get('at') or '') > p['decided_at']
               for q in problems):
            miss += 1
    causes = Counter((p.get('cause') or {}).get('kind') or 'не указана' for p in decided)
    return dict(decided=len(decided), repeated=miss, routing_accuracy=_rate(len(decided) - miss, len(decided)), causes=dict(causes))


def ablation(tasks):
    """Наблюдение, не эксперимент: задачи с двойным ревью и без, пропущенные после ревью дефекты на задачу."""
    groups = dict(dual=[0, 0], single=[0, 0])
    for t in tasks:
        pipe = t.get('pipeline') or {}
        if not _passed(pipe.get('history') or [], 'review'):
            continue
        g = 'dual' if (pipe.get('dual_review') or {}).get('second') else 'single'
        groups[g][0] += 1
        groups[g][1] += sum(1 for d in pipe.get('defects') or [] if d.get('found_stage') in LATE['reviewer'])
    return {g: dict(tasks=n, escaped=e, per_task=round(e / n, 2) if n else None, small=n < MIN_SAMPLE) for g, (n, e) in groups.items()} | \
        dict(kind='observational', note='сравнение без рандомизации: CRITICAL-задачи сложнее, разница — не эффект второй модели')


def overlap():
    """Дублирование ролей (A0-06): общие файлы артефактов и одинаковые пункты DoD у разных ролей."""
    import pipeline as P
    paths, texts = defaultdict(set), defaultdict(set)
    for role, r in P.ROLES.items():
        for a in r.get('artifacts') or []:
            paths[a['path']].add(role)
        dod = r.get('dod') or {}
        for it in (sum(dod.values(), []) if isinstance(dod, dict) else dod):
            texts[it['text'].strip().lower()].add(role)
    shared = {p: sorted(rs) for p, rs in paths.items() if len(rs) > 1}
    same = {t: sorted(rs) for t, rs in texts.items() if len(rs) > 1}
    return dict(shared_artifacts=shared, same_dod=same,
                note='общий RTM — намеренно: аналитик ведёт трассировку, QA дописывает тест-кейсы' if shared else None)


def gate_ablation(tasks):
    """Нужен ли ИИ-тимлид по классу задач (07: AG-LEAD-10): ворота, закрытые сервером, против ворот ИИ-тимлида
    по размеру задачи — доля ошибочных approve и число возвратов, которые тимлид поймал сам. Наблюдение, не A/B."""
    out = defaultdict(lambda: dict(server=[0, 0], teamlead=[0, 0], caught=0))
    for t in tasks:
        hist = (t.get('pipeline') or {}).get('history') or []
        cls = t.get('size') or '?'
        for i, h in enumerate(hist):
            if h.get('kind') != 'gate':
                continue
            who = 'server' if h.get('by') == 'server' else 'teamlead'
            if h.get('decision') == 'return':
                out[cls]['caught'] += who == 'teamlead'
                continue
            back = GATE_BEFORE.get(h.get('gate'), ())
            wrong = any(x.get('kind') == 'return' and x.get('to_stage') in back for x in hist[i + 1:])
            out[cls][who][0] += 1; out[cls][who][1] += wrong
    res = {}
    for cls, g in sorted(out.items()):
        res[cls] = dict(server=_rate(g['server'][1], g['server'][0]), teamlead=_rate(g['teamlead'][1], g['teamlead'][0]),
                        teamlead_caught=g['caught'])
    return dict(by_size=res, kind='observational',
                note='сервер закрывает только простые задачи с чистыми проверками — сравнение внутри размера, не эффект')


def collect(tasks, problems):
    return dict(finders=finders(tasks), teamlead=teamlead(tasks), resolver=resolver(problems), ablation=ablation(tasks),
                gate_ablation=gate_ablation(tasks), overlap=overlap())
