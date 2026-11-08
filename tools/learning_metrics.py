"""Метрики пилота локального разработчика M01…M11 (TT-114; ТЗ LEARN-05, §9, AT-01, AT-02, AT-06, AT-17, AT-18).

Каждая метрика — не голый процент, а dict(value, numerator, denominator, n, unknown_count, coverage,
period, sources). Нет знаменателя — value=None («нет данных»), а не 0 и не 100%. Незавершённые задачи
в доли успеха не входят и показываются отдельно (AT-17). Отсутствие CI — не успешный CI (AT-06).

Источники: журнал наблюдений (попытки и суждения о причинах), задачи и запуски workflow.json,
прогоны ci.json. Метрики ролей ревьюера и QA остаются в metrics.py — здесь на них только ссылка.
"""
from datetime import datetime, timedelta

import learning_causes as lc
import learning_events as le

FIRST_ANALYSIS = 50          # первая точка анализа: 50 принятых задач (§9.3)
REGRESSION_WINDOW_DAYS = 14
DIAG_ALERT = 0.80            # меньше 80% подтверждённых причин — сначала классификация, не выводы о модели


def _t(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None


def metric(mid, title, num, den, n=None, unknown=0, sources=(), note=None, value=None):
    v = value if value is not None else (round(num / den, 3) if den else None)
    total = (den or 0) + (unknown or 0)
    return dict(id=mid, title=title, value=v, numerator=num, denominator=den, n=n if n is not None else den,
                unknown_count=unknown, coverage=round(den / total, 3) if total else None,
                sources=list(sources)[:20], note=note if v is not None or note else 'нет данных')


def cohort(events, tasks, runs, project=None, model_config_id=None, agent=None, since=None, until=None,
           work_type=None, size=None):
    """Задачи с попытками разработчика, отобранные фильтрами. Возвращает [{ref, task, attempts, rejections, accepted, ...}]."""
    byref = {t['ref']: t for t in tasks}
    groups = {}
    for e in events:
        if e.get('task_ref') and e['event_type'] in ('attempt_submitted', 'attempt_rejected', 'task_accepted', 'knowledge_used',
                                                     'evidence_attached'):
            groups.setdefault(e['task_ref'], []).append(e)
    out = []
    for ref, evs in groups.items():
        atts = [e for e in evs if e['event_type'] == 'attempt_submitted']
        if not atts:
            continue
        if project and ref.split('/')[0] != project:
            continue
        if model_config_id and not any(e.get('model_config_id') == model_config_id for e in atts):
            continue
        if agent and not any(e.get('agent') == agent for e in atts):
            continue
        t = byref.get(ref) or {}
        if work_type and (t.get('work_type') or atts[0].get('work_type')) != work_type:
            continue
        if size and (t.get('size') or atts[0].get('size')) != size:
            continue
        acc = next((e for e in evs if e['event_type'] == 'task_accepted'), None)
        at = _t(acc['occurred_at']) if acc else None
        if acc and ((since and at and at < since) or (until and at and at > until)):
            continue
        out.append(dict(ref=ref, task=t, attempts=atts, rejections=[e for e in evs if e['event_type'] == 'attempt_rejected'],
                        accepted=acc, accepted_at=at, knowledge=[e for e in evs if e['event_type'] == 'knowledge_used'],
                        evidence=[e for e in evs if e['event_type'] == 'evidence_attached'],
                        runs=[r for r in runs if r.get('ref') == ref]))
    return out


def _ci_passed(ref):
    try:
        import ci
        return [r for r in ci.runs(ref=ref, limit=50) if r.get('status') == 'passed']
    except Exception:
        return []


def compute(events=None, tasks=None, state=None, now=None, **filters):
    w = le._w()
    events = events if events is not None else le.read_all()
    tasks = tasks if tasks is not None else w.snapshot()['tasks']
    state = state if state is not None else w.state()
    now = now or le.now()
    rows = cohort(events, tasks, state.get('runs') or [], **filters)
    acc = [r for r in rows if r['accepted']]
    open_ = [r for r in rows if not r['accepted']]
    causes = {}
    for r in rows:
        for rej in r['rejections']:
            causes[rej['event_id']] = lc.current(events, rej['event_id'])
    spec_changed = lc.requirement_changes(events)

    def req_change(rej):
        c = causes[rej['event_id']]
        return (c.get('category') == 'requirement_change' and c.get('confidence') == 'verified') or rej.get('attempt_id') in spec_changed

    M = {}
    first = [r for r in acc if len(r['attempts']) == 1 and not r['rejections']]
    M['M01'] = metric('M01', 'Принято с первого предъявления (FPAR)', len(first), len(acc), sources=[r['ref'] for r in first],
                      note='знаменатель — принятые задачи когорты; открытые не входят')
    rework = [r for r in acc if r['rejections']]
    rework_model = [r for r in rework if not all(req_change(x) for x in r['rejections'])]
    dist = {}
    for r in acc:
        dist[len(r['rejections'])] = dist.get(len(r['rejections']), 0) + 1
    M['M02'] = dict(metric('M02', 'Задачи с возвратом на доработку', len(rework), len(acc), sources=[r['ref'] for r in rework]),
                    distribution={str(k): v for k, v in sorted(dist.items())},
                    excluding_requirement_change=metric('M02r', 'Без возвратов из-за изменения требований', len(rework_model), len(acc))['value'])
    M['M03'] = metric('M03', 'Предъявлений на принятую задачу', sum(len(r['attempts']) for r in acc), len(acc),
                      value=round(sum(len(r['attempts']) for r in acc) / len(acc), 2) if acc else None,
                      note='предъявление — сдача разработчика (handoff), не число вызовов модели')
    diag, unk, model_bad = [], [], []
    for r in acc:
        cs = [causes[x['event_id']] for x in r['rejections']]
        if any(c.get('confidence') == 'verified' and c.get('category') in lc.MODEL for c in cs):
            model_bad.append(r['ref'])
        if all(c.get('confidence') == 'verified' for c in cs):
            diag.append(r)
        else:
            unk.append(r)
    M['M04'] = metric('M04', 'Подтверждённые ошибки модели', len([x for x in model_bad if x in {d['ref'] for d in diag}]) +
                      0, len(diag), unknown=len(unk), sources=model_bad,
                      note='знаменатель — принятые задачи с полной диагностикой; unknown не считается ошибкой модели')
    window = timedelta(days=REGRESSION_WINDOW_DAYS)
    mature = [r for r in acc if r['accepted_at'] and now - r['accepted_at'] >= window]
    reg = []
    for r in mature:
        pipe = r['task'].get('pipeline') or {}
        hit = any((d.get('regression') or d.get('level') == 'regression') and _t(d.get('at')) and
                  r['accepted_at'] <= _t(d.get('at')) <= r['accepted_at'] + window for d in pipe.get('defects') or [])
        hit = hit or any(((e.get('data') or {}).get('kind') == 'regression') for e in r['evidence'])
        if hit:
            reg.append(r['ref'])
    M['M05'] = metric('M05', 'Подтверждённые регрессии за %d дней' % REGRESSION_WINDOW_DAYS, len(reg), len(mature),
                      unknown=len(acc) - len(mature), sources=reg, note='незрелые задачи (окно не закрыто) не считаются «без дефектов»')
    need, covered = [], []
    for r in acc:
        if (r['task'].get('work_type') or '') == 'docs':
            continue
        need.append(r)
        if _ci_passed(r['ref']):
            covered.append(r['ref'])
    M['M06'] = metric('M06', 'Принятые задачи с подтверждённым тестовым evidence', len(covered), len(need), sources=covered,
                      note='пропущенный или отсутствующий CI — не покрытие')
    rej_all = [x for r in rows for x in r['rejections']]
    ver = [x for x in rej_all if causes[x['event_id']].get('confidence') == 'verified']
    M['M07'] = dict(metric('M07', 'Возвраты с подтверждённой причиной', len(ver), len(rej_all), sources=[x['event_id'] for x in ver]),
                    alert=bool(rej_all) and len(ver) / len(rej_all) < DIAG_ALERT)
    scoped = [r for r in acc if r['task'].get('scope')]
    comply = [r for r in scoped if not any(causes[x['event_id']].get('category') == 'model_instruction_failure' and
                                           causes[x['event_id']].get('confidence') == 'verified' for x in r['rejections'])]
    M['M08'] = metric('M08', 'Без подтверждённого нарушения ограничений', len(comply), len(scoped),
                      unknown=len(acc) - len(scoped), note='без проверенных ограничений (scope) — N/A')
    secs, missing = [], 0
    for r in acc:
        total, ok = 0.0, True
        for run in r['runs']:
            if run.get('status') not in ('done', 'cancelled'):
                continue
            a, b = _t(run.get('claimed_at')), _t(run.get('finished_at'))
            if not a or not b:
                ok = False; break
            paused = sum(((_t(p.get('resumed_at')) or b) - (_t(p.get('at')) or a)).total_seconds()
                         for p in run.get('pauses') or [] if _t(p.get('at')))
            total += max(0.0, (b - a).total_seconds() - paused)
        if ok and r['runs']:
            secs.append(total)
        else:
            missing += 1
    M['M09'] = metric('M09', 'Продуктивное время до приёмки, часов (медиана)', None, len(secs), unknown=missing,
                      value=round(sorted(secs)[len(secs) // 2] / 3600, 2) if secs else None,
                      note='сумма активных интервалов запусков без пауз; без отметок времени — unknown')
    returned = [r for r in rows if any(k.get('knowledge_refs') for k in r['knowledge'])]
    applied = [r for r in returned if any((k.get('data') or {}).get('applied') for k in r['knowledge'])]
    M['M10'] = metric('M10', 'Применённые проверенные карточки', len(applied), len(returned), sources=[r['ref'] for r in applied],
                      note='ссылка на карточку ещё не польза: числитель — карточки, названные применёнными при сдаче')
    local_tok, ext_tok, tok_missing = 0, 0, 0
    for r in acc:
        for run in r['runs']:
            tk = run.get('tokens')
            total = sum(v for v in (tk or {}).values() if isinstance(v, (int, float))) if isinstance(tk, dict) else tk
            if not isinstance(total, (int, float)):
                tok_missing += 1; continue
            if run.get('agent') == 'local_mimo':
                local_tok += total
            else:
                ext_tok += total
    M['M11'] = dict(metric('M11', 'Токенов на принятую задачу', local_tok + ext_tok, len(acc),
                           value=round((local_tok + ext_tok) / len(acc)) if acc and (local_tok + ext_tok) else None,
                           unknown=tok_missing, note='локальные и внешние токены раздельно; денег у локального исполнителя нет'),
                    local_tokens=local_tok, external_tokens=ext_tok, runs_without_tokens=tok_missing)
    top = {}
    for x in rej_all:
        c = causes[x['event_id']]
        k = c.get('category') if c.get('confidence') == 'verified' else 'не классифицировано'
        top[k] = top.get(k, 0) + 1
    status = 'insufficient_data' if len(acc) < FIRST_ANALYSIS else ('diagnostics_first' if M['M07']['alert'] else 'analysable')
    return dict(filters={k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in filters.items() if v},
                accepted=len(acc), open=len(open_), open_refs=[r['ref'] for r in open_][:50],
                status=status, first_analysis_at=FIRST_ANALYSIS, metrics=M,
                causes=sorted(({'category': k, 'count': v, 'share': round(v / len(rej_all), 3)} for k, v in top.items()),
                              key=lambda x: -x['count']),
                rejections=len(rej_all), role_metrics='metrics.py → роли ревьюера и QA')


def inspect(ref, events=None, tasks=None, state=None):
    """Задача → попытки → отказ → доказательство → причина и её история → карточки знаний."""
    events = events if events is not None else le.read_all()
    evs = le.for_task(ref, events)
    out = []
    for a in [e for e in evs if e['event_type'] == 'attempt_submitted']:
        rej = [e for e in evs if e['event_type'] == 'attempt_rejected' and e.get('attempt_id') == a['attempt_id']]
        out.append(dict(attempt_id=a['attempt_id'], run_id=a.get('run_id'), model_config_id=a.get('model_config_id'),
                        at=a['occurred_at'], result=a.get('result'), spec_revision=a.get('spec_revision'),
                        knowledge_refs=a.get('knowledge_refs'),
                        rejections=[dict(event_id=r['event_id'], stage=r.get('stage'), by=r.get('role'), reason=r.get('reason'),
                                         evidence=r.get('evidence_refs'), cause=lc.current(events, r['event_id']),
                                         history=[dict(at=h['occurred_at'], cause=h.get('cause'), by=(h.get('cause') or {}).get('assigned_by'))
                                                  for h in lc.history(events, r['event_id'])]) for r in rej]))
    acc = next((e for e in evs if e['event_type'] == 'task_accepted'), None)
    know = [e for e in evs if e['event_type'] in ('knowledge_used', 'knowledge_candidate', 'knowledge_verified')]
    return dict(ref=ref, attempts=out, accepted=bool(acc), accepted_at=acc and acc['occurred_at'],
                knowledge=[dict(type=k['event_type'], refs=k.get('knowledge_refs'), at=k['occurred_at']) for k in know],
                events=len(evs))
