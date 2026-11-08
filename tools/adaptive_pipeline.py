"""Адаптивный маршрут задачи конвейера (TT-109; ТЗ FR-001, §5.1 route_plan, E2).

route_plan компилируется из профиля риска (risk_policy) и существующих этапов pipeline.STAGES:
DoR и DoD ролей не дублируются, маршрут лишь говорит, какие этапы обязательны и какие проверки
нужны. Три режима работы (`adaptive_mode`, меняет владелец):

  * off — маршрут не считается;
  * shadow (по умолчанию) — маршрут считается и пишется в задачу и журнал, конвейер идёт прежним
    полным путём. Так видно, сколько анализа сэкономил бы FAST и где маршрут поднялся бы по диффу;
  * enforce — маршрут действует:
      - FAST пропускает «Идею» и «Анализ», но только для видов работы из `adaptive_fast_allow`
        (по умолчанию пусто: сначала сравнение в shadow). Ревью, тест, тестирование и релиз — как раньше;
      - CRITICAL запрещает серверное закрытие ворот и требует независимого проверяющего;
      - повторная оценка по фактическому диффу поднимает FAST → STANDARD/CRITICAL и возвращает
        задачу на «Идею»: недостаточные проверки аннулируются (TC-08, AC-49).

Слот задачи в потоке (FR-014) маршрут не трогает: смена режима — это возврат этапа, а не новая задача.
"""
import hashlib
import json

import pipeline as P
import risk_policy as rp

SCHEMA_VERSION = 1
POLICY_VERSION = 'adaptive-v1'
ADAPTIVE_MODES = ('off', 'shadow', 'enforce')
DEFAULTS = dict(adaptive_mode='shadow', adaptive_fast_allow=[])
POST_DEV = ('review', 'deploy_test', 'testing', 'deploy_main')
HISTORY = 20

CHECKS = {
    'FAST': ['unit', 'ci_branch', 'ci_test', 'release_policy'],
    'STANDARD': ['unit', 'integration', 'regression', 'ci_branch', 'ci_test', 'release_policy'],
    'CRITICAL': ['unit', 'integration', 'regression', 'contract', 'security', 'ci_branch', 'ci_test', 'ci_main',
                 'release_policy', 'independent_review'],
}
EVIDENCE = {
    'FAST': ['зелёный CI на голове ветки задачи и тестовой ветки', 'одобрение ревьюера', 'подпись QA'],
    'STANDARD': ['зелёный CI на голове ветки задачи и тестовой ветки', 'одобрение ревьюера', 'подпись QA',
                 'трассировка требований (RTM)'],
    'CRITICAL': ['зелёный CI на актуальном SHA всех веток', 'одобрение независимого ревьюера (не автора)',
                 'подпись QA без открытых дефектов', 'трассировка требований (RTM)', 'решение тимлида на воротах',
                 'проверка безопасности и контрактов'],
}


def conf(settings):
    out = dict(DEFAULTS)
    out.update({k: (settings or {}).get(k) for k in DEFAULTS if (settings or {}).get(k) is not None})
    return out


def fingerprint(task, files=None):
    keys = ('title', 'why', 'acceptance', 'scope', 'risk', 'size', 'work_type', 'task_type', 'assessment')
    raw = json.dumps(dict({k: (task or {}).get(k) for k in keys}, files=sorted(files or [])), ensure_ascii=False, sort_keys=True)
    return 'sha256:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()


def compile_plan(task, cls, files=None, needs=None):
    """route_plan по ТЗ §5.1 из профиля риска и этапов конвейера."""
    mode = cls['mode']
    skipped = ['idea', 'analysis'] if mode == 'FAST' else []
    stages = [s for s in P.ORDER if s not in skipped]
    required = []
    for st in stages:
        for r in P.stage_roles(st, needs if mode != 'CRITICAL' else dict(needs or {})):
            if r not in required:
                required.append(r)
    optional = sorted({r for st in stages for r in P.STAGE[st].get('optional', {})} - set(required))
    gates = [P.STAGE[s]['gate'] for s in stages if P.STAGE[s].get('gate')]
    checks = list(CHECKS[mode])
    if cls.get('profile') == 'docs_only':
        checks = ['docs', 'ci_branch', 'ci_test', 'release_policy']
    return dict(schema_version=SCHEMA_VERSION, task_ref=(task or {}).get('ref'), mode=mode, risk=cls['risk'],
                confidence=cls['confidence'], risk_reasons=cls['risk_reasons'], profile=cls.get('profile'),
                stages=stages, skipped_stages=skipped, required_roles=required, optional_roles=optional, gates=gates,
                server_gates=mode != 'CRITICAL', independent_review='strict' if mode == 'CRITICAL' else 'cross',
                required_checks=checks, evidence_requirements=EVIDENCE[mode], release_level='local_main',
                classified_on=cls['stage'], policy_version=POLICY_VERSION, risk_policy=rp.POLICY_VERSION,
                inputs_fingerprint=fingerprint(task, files))


def tests_available(project):
    try:
        import ci
        return any(st.get('kind') == 'test' for st in ci.config(project or {}).get('steps') or [])
    except Exception:
        return False


def applied_mode(plan, task, settings):
    """Какой маршрут действует: None — прежний полный путь (off/shadow или FAST не разрешён)."""
    c = conf(settings)
    if c['adaptive_mode'] != 'enforce' or not plan:
        return None
    if plan['mode'] == 'FAST' and (task or {}).get('work_type') not in (c['adaptive_fast_allow'] or []):
        return None
    if plan['mode'] == 'STANDARD':
        return None                    # STANDARD — это и есть текущий маршрут
    return plan['mode']


def route(pipe):
    return (pipe or {}).get('route') or {}


def applied(pipe):
    return route(pipe).get('applied')


def skipped_stages(pipe):
    """Этапы, которые действующий маршрут не требует (только enforce FAST)."""
    r = route(pipe)
    return list((r.get('plan') or {}).get('skipped_stages') or []) if r.get('applied') == 'FAST' else []


def diff_files(t, pipe, ctx=None):
    """Фактический дифф ветки задачи от базовой: (файлы, строки) или (None, None), пока кода нет."""
    wt = (pipe or {}).get('worktree') or {}
    if not wt.get('branch'):
        return None, None
    import orchestra
    ctx = ctx or orchestra.ctx_for(t, pipe)
    base = (ctx.branches or {}).get('base')
    if not ctx.repo or not base:
        return None, None
    rng = '%s...%s' % (base, ctx.branch)
    out = P._git(['-c', 'core.quotepath=false', 'diff', '--numstat', rng], ctx.repo)
    if out.returncode:
        return None, None
    files, lines = [], 0
    for row in out.stdout.splitlines():
        parts = row.split('\t')
        if len(parts) == 3:
            files.append(parts[2].strip())
            lines += sum(int(x) for x in parts[:2] if x.isdigit())
    art = P.ART.format(id=t['id']) + '/'
    files = [f for f in files if not f.startswith(art)]      # артефакты задачи — не изменение продукта
    return (files, lines) if files else (None, None)


def refresh(s, t, pipe, at, settings, project=None, journal=None):
    """Посчитать или обновить маршрут задачи. Возвращает событие повышения маршрута или None.

    Профиль на приёме — по тексту и scope; после разработки — по фактическому диффу, и он только
    поднимает маршрут. Запись — в pipe['route']: plan, applied, history."""
    c = conf(settings)
    if c['adaptive_mode'] == 'off':
        return None
    r = pipe.setdefault('route', {})
    plan = r.get('plan')
    files = lines = None
    stage = pipe.get('stage')
    if stage in POST_DEV or stage == 'done':
        if r.get('diff_head') and r.get('diff_head') == _head(t, pipe):
            files = r.get('diff_files'); lines = r.get('diff_lines')
        else:
            files, lines = diff_files(t, pipe)
            r.update(diff_head=_head(t, pipe), diff_files=files, diff_lines=lines)
    fp = fingerprint(t, files)
    key = json.dumps(c, sort_keys=True, ensure_ascii=False)
    if plan and plan.get('inputs_fingerprint') == fp and r.get('conf_key') == key:
        return None
    tests = tests_available(project)
    if plan and files is not None:
        cls = rp.reevaluate(plan['mode'], t, files, lines, tests)
    else:
        cls = rp.classify(t, files, lines, tests)
    floor = r.get('floor')
    if floor and rp.RANK[cls['mode']] < rp.RANK[floor]:
        # Маршрут, поднятый фактическим диффом, текстом задачи не понижается — даже после возврата на «Идею».
        cls = dict(cls, mode=floor, risk_reasons=cls['risk_reasons'] + ['фактический дифф поднял маршрут до %s' % floor])
    if cls['stage'] == 'diff' and rp.RANK[cls['mode']] > rp.RANK['FAST']:
        r['floor'] = rp.stricter(cls['mode'], floor or 'FAST')
    new = compile_plan(t, cls, files, pipe.get('needs'))
    before = plan['mode'] if plan else None
    was = r.get('applied')
    act = applied_mode(new, t, settings)
    if act == 'FAST' and was != 'FAST' and stage != 'idea':
        act = None                     # FAST решается на приёме: начатый анализ на полпути не обрывается
    r.update(plan=new, applied=act, conf_key=key, at=at.isoformat(), adaptive_mode=c['adaptive_mode'])
    raised = bool(before) and rp.RANK[new['mode']] > rp.RANK[before]
    h = r.setdefault('history', [])
    h.append(dict(at=at.isoformat(), stage=stage, mode=new['mode'], classified_on=new['classified_on'], applied=r['applied'],
                  raised=raised, reasons=new['risk_reasons'][:4]))
    r['history'] = h[-HISTORY:]
    if journal and before != new['mode']:
        journal('route', 'Маршрут %s%s (%s): %s' % (
            '%s → ' % before if before else '', new['mode'], 'действует' if r['applied'] else c['adaptive_mode'],
            '; '.join(new['risk_reasons'][:3])))
    if raised and was == 'FAST' and stage in POST_DEV:
        return dict(previous=before, mode=new['mode'], reasons=new['risk_reasons'], invalidated=sorted(
            set(CHECKS[new['mode']]) - set(CHECKS[before])))
    return None


def _head(t, pipe):
    wt = (pipe or {}).get('worktree') or {}
    if not wt.get('branch'):
        return None
    import orchestra
    ctx = orchestra.ctx_for(t, pipe)
    return orchestra.branch_head(ctx)


def summary(tasks, settings):
    """Сводка по задачам конвейера: сколько каких маршрутов, что сэкономил бы FAST, где маршрут поднялся."""
    c = conf(settings)
    by = {m: 0 for m in rp.MODES}; raised, items = [], []
    for t in tasks:
        r = route(t.get('pipeline'))
        if not r.get('plan'):
            continue
        p = r['plan']; by[p['mode']] += 1
        items.append(dict(ref=t['ref'], mode=p['mode'], applied=r.get('applied'), stage=(t.get('pipeline') or {}).get('stage'),
                          confidence=p['confidence'], reasons=p['risk_reasons'][:3], skipped_stages=p['skipped_stages']))
        if any(e.get('raised') for e in r.get('history') or []):
            raised.append(t['ref'])
    return dict(adaptive_mode=c['adaptive_mode'], fast_allow=c['adaptive_fast_allow'], by_mode=by, raised=raised,
                would_skip=[i['ref'] for i in items if i['mode'] == 'FAST' and not i['applied']],
                items=items[:200], policy_version=POLICY_VERSION, risk_policy=rp.POLICY_VERSION)
