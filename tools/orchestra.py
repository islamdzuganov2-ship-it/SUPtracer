"""Тимлид-оркестратор конвейера: двигает задачу по этапам, держит DoR/DoD, ведёт журнал.

Оркестратор — сервер, а не ИИ: проверяет DoR и DoD по спецификации (pipeline.py), создаёт
запуски ролей, принимает передачи. ИИ-тимлид вызывается на воротах «анализ → разработка»
и «тест → main» и при эскалациях. Каждое событие — передача, отказ в приёме, возврат,
эскалация, решение ворот — пишется в журнал тимлида: это и есть «агент сообщает тимлиду».

Операции (через tt_work): idea, handoff, return, escalate, gate, approve_release.
Обычные операции задачи (take, finish, complete…) к задаче конвейера не применяются:
сдать работу можно только через handoff с выполненным DoD.
"""
import copy
import os
import re
import time

import ci
import failure
import filekinds as fk
import pipeline as P
import recipes
import owner_auth as oa
import release_policy as rp
import workflow as w

MAX_ATTEMPTS = 3          # провалов DoD у роли подряд — дальше эскалация тимлиду
MAX_LOOPS = 3             # возвратов между одной парой этапов — дальше эскалация
SCHEMA_VERSION = 'handoff-1'   # версия договора сдачи этапа (07: A1-03, A1-04)
UNCERTAIN = ('assumptions', 'questions', 'conflicts', 'unverified')   # неопределённость роли — отдельными полями (07: A1-06)
SAME_REJECTS = 3          # одинаковых отказов сдачи в одном запуске — агент упёрся, дальше Решала (07: A8-05)
RETURN_TO = {'reviewer': ['development'], 'qa': ['development'], 'devops': ['development', 'testing'],
             'developer': ['analysis'], 'system_analyst': ['idea'], 'solution_architect': ['idea'],
             'designer': ['idea'], 'marketer': ['idea']}


# Реестр дефектов: кто нашёл, на каком уровне теста, чем исправлено. Способ исправления
# проверяется по коммитам ветки задачи после возврата, а не по словам исполнителя.
SEVERITIES = ('critical', 'major', 'minor')
FIX_METHODS = dict(spec_change='изменение ТЗ', code_to_spec='приведение кода к ТЗ',
                   workaround='обходная мера', test_fix='поправка теста', not_a_defect='не дефект')
DEFECT_ROLES = ('reviewer', 'qa', 'devops')
DEFECT_KINDS = ('code_defect', 'test_gap')


def history(pipe, kind, **e):
    """Полная история передач и возвратов для метрик: журнал режется, история — нет (до 400 событий)."""
    h = pipe.setdefault('history', []); h.append(dict(e, kind=kind, at=e.get('at') or w.now().isoformat()))
    pipe['history'] = h[-400:]


def branch_head(ctx):
    if not ctx.repo or not ctx.branch:
        return None
    return P._git(['rev-parse', ctx.branch], ctx.repo).stdout.strip() or None


def changed_since(ctx, commit):
    """Файлы, изменённые в ветке задачи после коммита (момента регистрации дефекта)."""
    if not ctx.repo or not ctx.branch:
        return []
    rng = '%s..%s' % (commit, ctx.branch) if commit else ctx.branch
    out = P._git(['-c', 'core.quotepath=false', 'log', '--format=', '--name-only', rng], ctx.repo).stdout
    return sorted({f.strip() for f in out.splitlines() if f.strip()})


def validate_handoff(req):
    """Договор сдачи (07: A1-03, A1-04, A1-06): версия, обязательные поля и типы — ошибка до любых побочных эффектов."""
    v = req.get('schema_version')
    if v is not None and v != SCHEMA_VERSION:
        raise ValueError('schema_version: сервер принимает %s, прислано %s — обнови паспорт (op=packet)' % (SCHEMA_VERSION, v))
    if not isinstance(req.get('summary'), str) or len(req['summary'].strip()) < 20:
        raise ValueError('summary: что сделано — для тимлида (не короче 20 символов)')
    items = req.get('items')
    if not isinstance(items, dict) or not items:
        raise ValueError('items: обязательное поле {id пункта: {status: done|na, evidence}}')
    for k, v in items.items():
        if not isinstance(v, dict) or v.get('status') not in ('done', 'na') or not isinstance(v.get('evidence', ''), str):
            raise ValueError('items.%s: {status: done|na, evidence: строка}' % k)
    for k in UNCERTAIN:
        x = req.get(k)
        if x is not None and (not isinstance(x, list) or any(not isinstance(i, str) or not i.strip() for i in x)):
            raise ValueError('%s: список строк' % k)
    for k in ('verdict', 'reviewed_sha'):
        if req.get(k) is not None and not isinstance(req[k], str): raise ValueError('%s: строка' % k)
    if req.get('signoff') is not None and not isinstance(req['signoff'], bool): raise ValueError('signoff: true или false')
    if req.get('ac_matrix') is not None and not isinstance(req['ac_matrix'], dict):
        raise ValueError('ac_matrix: {AC-n: {files, tests, verdict}}')
    if req.get('comparison') is not None and not isinstance(req['comparison'], dict):
        raise ValueError('comparison: {agree: bool, differences: [{topic, first, second}]}')
    if req.get('unverified') and (req.get('verdict') == 'approve' or req.get('signoff') is True):
        raise ValueError('unverified: с непроверенным нельзя approve или signoff — проверь или верни задачу (op=return)')


def uncertain_stop(s, t, pipe, run, handoff, req, at):
    """Вопрос или противоречие роли: этап не сдаётся, а уходит на разбор (07: A1-06, E2E-R02)."""
    role, stage = run['role'], run['stage']
    reg = pipe.setdefault('conflicts', []); ids = []
    for kind in ('conflicts', 'questions'):
        for text in handoff.get(kind) or []:
            c = dict(id='CF-%d' % (len(reg) + 1), kind='conflict' if kind == 'conflicts' else 'question', text=text.strip()[:600],
                     role=role, stage=stage, run=run['id'], agent=run.get('agent'), at=at.isoformat(), status='open')
            reg.append(c); ids.append(c['id'])
    _autocommit(s, t, pipe, run, 'WIP: вопросы и противоречия ' + ', '.join(ids))
    k = key(stage, role); entry = pipe['roles'].setdefault(k, {})
    run.update(status='done', finished_at=at.isoformat(), evidence='Остановка на уточнение: ' + ', '.join(ids), handoff=handoff)
    entry.update(state='pending', handoff=handoff, at=at.isoformat(),
                 feedback=[dict(id='uncertain', text='Ждёт решения по %s' % ', '.join(ids), detail=req['summary'].strip()[:300])])
    history(pipe, 'uncertain', role=role, stage=stage, run=run['id'], conflicts=ids, at=at.isoformat())
    prob = escalate(s, t, pipe, at, '%s: требования не дают однозначной работы — %s' % (
        P.ROLES[role]['title'], '; '.join('%s %s' % (c['id'], c['text'][:160]) for c in reg if c['id'] in ids)[:900]),
        role, run.get('agent'), source='role', tried=req['summary'].strip()[:500], need='Ответ на вопросы и снятие противоречий')
    for c in reg:
        if c['id'] in ids: c['problem'] = prob['id']
    w.write_patches(s, {t['ref']: dict(pipeline=pipe, claimed_by=None, status='blocked',
                                       status_note='%s у Решалы: вопросы и противоречия %s' % (prob['id'], ', '.join(ids)))})
    return dict(accepted=False, blocked=True, conflicts=ids, escalated=prob['id'])


def upstream_uncertain(pipe):
    """Допущения и непроверенное предыдущих ролей — следующей роли, чтобы не принять их за факт."""
    out = []
    for k, e in (pipe.get('roles') or {}).items():
        h = e.get('handoff') or {}
        for f in ('assumptions', 'unverified'):
            for x in h.get(f) or []:
                out.append(dict(source=k, kind=f, text=x[:300]))
    return out[:30]


def register_defects(pipe, run, raw, ctx, at, required):
    """Проверить и записать дефекты возврата. Ничего не пишет, если хоть один неверен."""
    raw = raw if raw is not None else []
    if not isinstance(raw, list) or any(not isinstance(d, dict) for d in raw):
        raise ValueError('defects: список {title, severity, level[, test]}')
    if required and not raw:
        raise ValueError('defects: возврат QA регистрирует дефекты — хотя бы один {title, severity, level}')
    for d in raw:
        if not isinstance(d.get('title'), str) or len(d['title'].strip()) < 10:
            raise ValueError('defects: суть дефекта (title) не короче 10 символов')
        if d.get('severity') not in SEVERITIES: raise ValueError('defects: severity — critical, major или minor')
        if d.get('level') not in fk.LEVELS: raise ValueError('defects: level — уровень теста: ' + ', '.join(fk.LEVELS))
        if d.get('kind', 'code_defect') not in DEFECT_KINDS:
            raise ValueError('defects: kind — code_defect (ошибка кода) или test_gap (тест не ловит ошибку)')
        if not (d.get('test') or d.get('file') or d.get('repro')):
            raise ValueError('defects: NEEDS_REPRO — у дефекта «%s» нет ни теста, ни файла, ни шагов воспроизведения (test, file, repro)'
                             % d['title'].strip()[:60])
        if d.get('file'):
            path = re.sub(r':\d+(-\d+)?$', '', str(d['file']).strip())
            if not P.read_file_at(ctx.repo, ctx.branch, path):
                raise ValueError('defects: NEEDS_REPRO — файла %s нет в ветке задачи: дефект без проверяемой ссылки не принимается' % path)
    head = branch_head(ctx); out = []
    reg = pipe.setdefault('defects', [])
    for d in raw:
        e = dict(id='D-%d' % (len(reg) + 1), title=d['title'].strip()[:300], severity=d['severity'], level=d['level'],
                 test=str(d.get('test') or '')[:100], found_by=run['role'], found_stage=run['stage'], found_agent=run.get('agent'),
                 found_model=run.get('model'), at=at.isoformat(), status='open', base=head,
                 kind=d.get('kind', 'code_defect'), file=str(d.get('file') or '')[:200], repro=str(d.get('repro') or '')[:1000])
        reg.append(e); out.append(e['id'])
    return out


def check_fixes(t, pipe, ctx, fixes):
    """Разработчик сдаёт этап после возврата: у каждого открытого дефекта — способ, подтверждённый коммитами."""
    fixes = fixes if isinstance(fixes, dict) else {}
    fails, done = [], {}
    for d in [x for x in pipe.get('defects') or [] if x['status'] == 'open']:
        f = fixes.get(d['id']); fid = 'defect:' + d['id']
        if not isinstance(f, dict) or f.get('method') not in FIX_METHODS:
            fails.append(dict(id=fid, text=d['title'], detail='укажите fixes.%s.method: %s' % (d['id'], ', '.join(FIX_METHODS)))); continue
        files = changed_since(ctx, d.get('base'))
        kinds = {fk.classify(x) for x in files}
        spec = [x for x in files if fk.classify(x) == 'spec']
        note = str(f.get('note') or '').strip()
        method = f['method']
        if method == 'spec_change' and not spec:
            why = 'изменение ТЗ: после регистрации дефекта не правились артефакты анализа (SRS, BRD, HLD, API…)'
        elif method in ('code_to_spec', 'workaround') and 'code' not in kinds:
            why = '%s: после регистрации дефекта в ветке нет правок кода' % FIX_METHODS[method]
        elif method == 'not_a_defect' and len(note) < 30:
            why = 'не дефект: докажите в note, почему поведение верно по ТЗ (не короче 30 символов) — это ложное срабатывание проверяющего'
        elif method == 'workaround' and len(note) < 20:
            why = 'обходная мера: опишите в note, что обходится и почему не исправлено по ТЗ (не короче 20 символов)'
        elif method == 'test_fix' and 'test' not in kinds:
            why = 'поправка теста: после регистрации дефекта не правились файлы тестов'
        elif method == 'test_fix' and f.get('test_level') not in fk.LEVELS:
            why = 'поправка теста: укажите test_level — ' + ', '.join(fk.LEVELS)
        else:
            done[d['id']] = dict(method=method, test_level=f.get('test_level') if method == 'test_fix' else None, note=note[:500],
                                 files=[x for x in files if fk.classify(x) in ('spec', 'code', 'test')][:20])
            continue
        fails.append(dict(id=fid, text=d['title'], detail=why))
    return fails, done


def key(stage, role):
    return '%s/%s' % (stage, role)


def is_pipeline(t):
    return bool(t.get('pipeline'))


def new_pipe(source):
    return dict(stage='idea', roles={}, stages={}, gates={}, approvals={}, journal=[], loops={}, source=source)


def project_of(t):
    return w.read('projects.json', {}).get('projects', {}).get(t['project'], {})


def journal(s, pipe, ref, kind, text, role=None, stage=None):
    """Событие для тимлида: в задачу и в общий журнал тимлида."""
    e = dict(at=w.now().isoformat(), ref=ref, kind=kind, role=role, stage=stage or pipe.get('stage'), text=str(text)[:600])
    pipe.setdefault('journal', []).append(e); pipe['journal'] = pipe['journal'][-60:]
    tl = s.setdefault('teamlead', []); tl.append(e); s['teamlead'] = tl[-300:]
    return e


def role_order(s, t, role):
    """Порядок ИИ для роли: закрепление владельцем, затем таблица роли; ревьюер — не тот, кто разрабатывал."""
    pin = (w.settings(s).get('roles') or {}).get(role)
    prof = P.ROUTING[role]
    order = list(prof['prefer'])
    if prof.get('cross'):
        other = next((e.get('agent') for k, e in ((t.get('pipeline') or {}).get('roles') or {}).items()
                      if k.endswith('/' + prof['cross']) and e.get('agent')), None)
        if other:
            order = [a for a in order if a != other] + [other]
    if pin:
        order = [pin] + [a for a in order if a != pin]
    return order


def role_agent(s, t, role, exclude=()):
    """ИИ роли через provider_balance.choose (FR-015): жёсткий фильтр, затем порядок роли
    (balance_mode=prefer) или дефицит доли (balanced). Решение пишется в provider_decisions.
    Все пригодные на лимите — первый по порядку (сессия встанет на паузу до сброса)."""
    import local_mimo as lm
    if role == 'developer' and lm.pilot(w.settings(s), t.get('project')):
        import mimo_supervisor as sup
        ok, why = sup.capable(lm.active(), t.get('pipeline'))
        if ok:
            return lm.AGENT             # пилот LEARN-02: разработчик — только local_mimo, без баланса и failover
        # Пригодность, а не отказ: неаттестованная модель не получает CRITICAL (07: A3-02, A9-07) — паспорт тот же, ИИ внешний.
        s.setdefault('capability_routes', []).append(dict(ref=t['ref'], role=role, reason=why, at=w.now().isoformat()))
        s['capability_routes'] = s['capability_routes'][-100:]
        exclude = tuple(exclude) + (lm.AGENT,)
    import provider_balance as pb
    if role == 'teamlead':
        # Ворота решает не тот ИИ, чью работу они пропускают (07: LEAD-04): авторы этапа исключаются, пока есть другой.
        authors = stage_authors(t)
        if authors:
            d = pb.choose(s, t, role, w.now(), tuple(exclude) + tuple(authors))
            ok = bool(d['selected'])
            s.setdefault('independence_routes', []).append(dict(ref=t['ref'], role=role, authors=sorted(authors), independent=ok,
                                                                 at=w.now().isoformat()))
            s['independence_routes'] = s['independence_routes'][-100:]
            if ok:
                return d['selected']
    d = pb.choose(s, t, role, w.now(), exclude)
    if d['selected']:
        return d['selected']
    # Нет пригодного по политике: всё равно не автор проверяемой работы сложной задачи (E2E-R06, E2E-R22) —
    # независимость сильнее role_affinity; запуск встанет в ожидание лимита, а не сделает самопроверку.
    dev = pb.author(t, role)
    order = role_order(s, t, role)
    return next((a for a in order if not (dev and a == dev and pb.strict(t))), order[0])


def stage_authors(t):
    """ИИ, сделавшие работу текущего этапа задачи (кроме тимлида и Решалы)."""
    pipe = t.get('pipeline') or {}
    st = pipe.get('stage')
    return {e.get('agent') for k, e in (pipe.get('roles') or {}).items()
            if k.split('/')[0] == st and k.split('/')[-1] not in ('teamlead', 'resolver') and e.get('agent')}


def run_route(t, run, agent):
    """Маршрут уже поставленного запуска: модель роли, у второго ревью CRITICAL — второе мнение (другая модель)."""
    if agent == 'local_mimo':
        import local_mimo as lm
        return lm.route(t, run['role'], run)           # закреплённая за запуском конфигурация (A9-06)
    route = role_route(t, run['role'], agent)
    dual = run.get('dual') or {}
    if dual.get('second_of'):
        s = w.state()
        first = next((x for x in s['runs'] if x['id'] == dual['second_of']), None) or {}
        route = second_route(route, agent, dict(agent=first.get('agent'), model=run_model(s, first.get('id'))))
    return route


def role_route(t, role, agent, gate=None):
    """Модель и усилия роли: класс и усилия из таблицы роли по сложности задачи, не ниже min_tier роли."""
    if agent == 'local_mimo':
        import local_mimo as lm
        return lm.route(t, role)
    prof = P.ROUTING[role]
    cx_ = w.cx.assess(t); level = cx_['tier']
    tier = max(P.ROLES[role]['min_tier'], prof['tier'][level - 1])
    model = w.LINEUP[agent][tier]
    effort = prof['effort'][level - 1]
    allowed = w.EFFORTS[agent]
    if effort not in allowed:
        effort = allowed[-1]            # у Codex выше high усилий нет
    reason = ('роль «%s»: %s; сложность задачи %.2f (класс %d) → %s, усилия %s' % (
        P.ROLES[role]['title'], prof['why'], cx_['score'], level, model, effort))
    return dict(model=model, provider=agent, effort=effort, reason=reason, source='role', role=role,
                complexity=dict(score=cx_['score'], tier=level), context_budget=2000,
                total_token_budget=w.budget_for(model, effort), max_attempts=2, provisional=False,
                budget_tokens=budget_tokens(tier, effort), policy_version=w.POLICY['version'])


# Ориентир объёма запуска роли в токенах (вход, выход, запись и чтение кеша — всё, что обработала модель)
# по классу модели; усилия — множителем. Мерило — количество токенов, а не деньги (Л4).
# Замер 92 сессий Claude до лёгкого старта: тимлид ≈1.3 млн, PO ≈2.0 млн, аналитик и архитектор ≈5–6 млн.
RUN_BUDGET_TOKENS = {1: 1_000_000, 2: 2_500_000, 3: 5_000_000}
BUDGET_ALERT = 1.5      # сторож бюджета: напоминание, когда запуск обработал 150% ориентира (Б7)


def budget_tokens(tier, effort):
    return int(round(RUN_BUDGET_TOKENS[tier] * w.EFFORT_COST.get(effort, 1.0), -4))


def ensure_worktree(t, pipe):
    if pipe.get('worktree'):
        return pipe['worktree']
    import launcher
    p = project_of(t); repo = p.get('inspection_path') or p.get('path')
    wt = launcher.ensure_task_worktree(repo, t['id']) if repo else None
    if wt:
        pipe['worktree'] = dict(path=wt['path'], branch=wt['branch'], base=wt.get('base'))
    return pipe.get('worktree')


COMMIT_RULE = ('Если git commit не проходит (песочница не даёт писать в .git, «index.lock: Permission denied») — '
               'не трать попытки и не эскалируй: сервер сам закоммитит файлы рабочей копии при handoff, return, escalate или pause.')


def autocommit(pipe, run, label):
    """Песочница Codex не даёт агенту писать в .git: незакоммиченное в рабочей копии задачи коммитит сервер.
    Возвращает короткий хеш коммита или None (нечего коммитить, нет рабочей копии, git отказал)."""
    path = ((pipe or {}).get('worktree') or {}).get('path')
    if not path or not os.path.isdir(path):
        return None
    st = P._git(['status', '--porcelain'], path)
    if st.returncode != 0 or not st.stdout.strip():
        return None
    msg = ('%s: %s\n\nАвтокоммит сервера TaskTreker: правки агента %s (%s) остались незакоммиченными '
           '— песочница ИИ не даёт писать в .git.' % (run.get('role') or 'исполнитель', str(label)[:120], run.get('agent'), run['id']))
    for _ in range(3):
        P._git(['add', '-A'], path)
        c = P._git(['-c', 'user.name=TaskTreker', '-c', 'user.email=tasktreker@localhost', 'commit', '-q', '-m', msg], path)
        if c.returncode == 0:
            return P._git(['rev-parse', '--short', 'HEAD'], path).stdout.strip()
        if 'index.lock' not in (c.stderr or '') + (c.stdout or ''):
            return None
        time.sleep(1)                   # в копии коммитит другая роль
    return None


def _autocommit(s, t, pipe, run, label):
    sha = autocommit(pipe, run, label)
    if sha:
        journal(s, pipe, t['ref'], 'autocommit', '%s: сервер закоммитил правки агента (%s)' % (run.get('role') or run['id'], sha),
                run.get('role'), run.get('stage'))
    return sha


def write_index(s, t, pipe, run):
    """Указатель задачи в ветку (TT-92): после принятой сдачи этапа идеи или анализа сервер собирает
    docs/tasks/<id>/INDEX.md из закоммиченных артефактов и коммитит только его."""
    wt = (pipe.get('worktree') or {}).get('path')
    if not wt or not os.path.isdir(wt):
        return None
    text = P.task_index(ctx_for(t, pipe))
    rel = P.index_path(t['id'])
    f = os.path.join(wt, rel)
    try:
        with open(f, encoding='utf-8') as fh:
            if fh.read() == text:
                return None
    except OSError:
        pass
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    P._git(['add', '--', rel], wt)
    c = P._git(['-c', 'user.name=TaskTreker', '-c', 'user.email=tasktreker@localhost', 'commit', '-q', '-m',
                'INDEX.md: указатель задачи %s (сервер TaskTreker)' % t['id'], '--', rel], wt)
    if c.returncode == 0:
        journal(s, pipe, t['ref'], 'index', 'Сервер обновил указатель задачи %s' % rel, run.get('role'), run.get('stage'))
        return rel
    return None


def materialize(s, t):
    """Рабочая копия задачи — в момент, когда роль её берёт. Возвращает правку pipeline или None."""
    pipe = t.get('pipeline')
    if not pipe or pipe.get('worktree'):
        return None
    pipe = copy.deepcopy(pipe)
    if ensure_worktree(t, pipe):
        journal(s, pipe, t['ref'], 'worktree', 'Рабочая копия задачи: %s, ветка %s' % (pipe['worktree']['path'], pipe['worktree']['branch']))
        return pipe
    return None


def ctx_for(t, pipe, handoff=None):
    return P.Context(dict(t, pipeline=pipe), project_of(t), pipe.get('worktree'), handoff)


def active_run(s, ref, role=None, gate=None):
    return next((r for r in s['runs'] if r['ref'] == ref and r['status'] in ('queued', 'running')
                 and (role is None or r.get('role') == role) and (gate is None or r.get('gate') == gate)), None)


def create_role_run(s, t, pipe, stage, role, at, feedback=None, gate=None, second_of=None):
    if active_run(s, t['ref'], role=role, gate=gate):
        return None
    t = dict(t, pipeline=pipe)          # ревьюер выбирается по разработчику этого конвейера
    agent = role_agent(s, t, role, exclude=(second_of['agent'],) if second_of and second_of.get('agent') else ())
    run = w.new_run(s, t['ref'], None, at)
    dec = (s.get('provider_decisions') or [None])[-1]
    if dec and dec.get('task_ref') == t['ref'] and dec.get('role') == role and not dec.get('run_id'):
        dec['run_id'] = run['id']
    cap = (s.get('capability_routes') or [None])[-1]
    if cap and cap['ref'] == t['ref'] and cap['role'] == role and not cap.get('run_id'):
        cap['run_id'] = run['id']
        journal(s, pipe, t['ref'], 'capability_route', '%s: local_mimo не взят — %s; роль у %s, расход внешних токенов виден в запуске %s' % (
            P.ROLES[role]['title'], cap['reason'], w.provider_name(agent), run['id']), role, stage)
    ind = (s.get('independence_routes') or [None])[-1]
    if ind and ind['ref'] == t['ref'] and ind['role'] == role and not ind.get('run_id'):
        ind['run_id'] = run['id']
        journal(s, pipe, t['ref'], 'independence', 'ворота: %s; авторы этапа — %s' % (
            'тимлид не из авторов этапа' if ind['independent'] else 'другого ИИ нет — тимлид совпадает с автором, решение помечено',
            ', '.join(w.provider_name(a) for a in ind['authors'])), role, stage)
        run['independent'] = ind['independent']
    route = role_route(t, role, agent)
    if second_of:
        route = second_route(route, agent, second_of)
        run['dual'] = dict(second_of=second_of.get('run'))
    ident = run_identity(pipe, role, agent)
    if ident.get('skill_installed') is False:
        journal(s, pipe, t['ref'], 'skill_stale', 'Скилл %s у %s не совпадает с регламентом сервера (%s): '
                'py tools/pm.py skills install' % (ident['skill'], w.provider_name(agent), ident['skill_sha']), role, stage)
    run.update(identity=ident)
    run.update(role=role, stage=stage, phase='role', agent=agent, gate=gate,
               auto_launch=w.settings(s).get('auto_pipeline', True) and (not (stage == 'idea' and pipe.get('pause_auto'))
                                                                        or t['project'] in s.get('active_projects', [])),
               packet=dict(route=route, phase='role'))
    if role not in ('teamlead', 'resolver'):
        k = key(stage, role); prev = pipe['roles'].get(k) or {}
        pipe['roles'][k] = dict(state='queued', run=run['id'], attempts=prev.get('attempts', 0) + (1 if feedback else 0),
                                feedback=feedback, agent=agent)
        if feedback and prev.get('failures'):
            pipe['roles'][k]['failures'] = prev['failures']       # история отпечатков живёт, пока роль не сдала этап
    journal(s, pipe, t['ref'], 'run', '%s: запуск %s поставлен для %s (%s · %s)' % (
        P.ROLES[role]['title'], run['id'], w.provider_name(agent), route['model'], route['effort']), role, stage)
    return run


CONTRACT_LIVE = ('analysis', 'development', 'review', 'deploy_test', 'testing', 'deploy_main')


def publish_contract(s, t, pipe, ctx, at):
    """Аналитик сдал анализ задачи из группы контрактов: хеш контракта — в реестр группы. Изменился —
    задачи группы, прошедшие анализ по старому контракту, помечаются устаревшими (07: A5-06, E2E-R30):
    им не сдать разработку и не выйти в main, пока анализ не сверен заново. Возвращать их сервер сам
    не берётся — это решает роль, у которой задача, по понятной причине в DoD."""
    grp = pipe.get('contract_group')
    digest = P.contract_digest(ctx)
    if not grp or not digest:
        return []
    reg = s.setdefault('contracts', {})
    k = '%s/%s' % (t['project'], grp)
    prev = reg.get(k)
    pipe['contract_seen'] = dict(group=grp, digest=digest, at=at.isoformat())
    if pipe.get('contract_stale') and prev and prev.get('digest') == digest:
        pipe.pop('contract_stale', None)                          # анализ сверен с действующим контрактом
    elif pipe.get('contract_stale') and prev and prev.get('ref') != t['ref']:
        return []                                                 # контракт задачи расходится с действующим: не перебивать чужой
    else:
        pipe.pop('contract_stale', None)
    reg[k] = dict(digest=digest, ref=t['ref'], at=at.isoformat(), previous=(prev or {}).get('digest'))
    if not prev or prev.get('digest') == digest:
        return []
    hit = []
    for o in w.snapshot()['tasks']:
        op = o.get('pipeline') or {}
        if o['ref'] == t['ref'] or o.get('project') != t['project'] or op.get('contract_group') != grp:
            continue
        seen = (op.get('contract_seen') or {}).get('digest')
        if not seen or seen == digest or op.get('stage') not in CONTRACT_LIVE or op.get('stage') == 'analysis' \
                or o.get('status') in ('done', 'cancelled'):
            continue
        op = copy.deepcopy(op)
        op['contract_stale'] = dict(group=grp, by=t['ref'], digest=digest, seen=seen, at=at.isoformat())
        journal(s, op, o['ref'], 'contract_stale', 'Контракт группы «%s» изменён задачей %s: эта задача прошла анализ по старому '
                '(%s → %s) — разработка и выпуск остановлены до повторного анализа' % (grp, t['ref'], seen, digest))
        w.write_patches(s, {o['ref']: dict(pipeline=op)})
        hit.append(o['ref'])
    if hit:
        journal(s, pipe, t['ref'], 'contract_changed', 'Контракт группы «%s» изменён: устарели %s' % (grp, ', '.join(hit)))
    return hit


DUAL_MODES = ('critical', 'off')


def dual_required(s, pipe):
    """Двойное независимое ревью (07: A7-03, AG-REV-07): для задач с расчётным риском CRITICAL.
    Это ужесточение, поэтому действует и при adaptive_mode=shadow; выключает только владелец (dual_review=off)."""
    import adaptive_pipeline as ap
    if w.settings(s).get('dual_review', 'critical') == 'off':
        return False
    r = ap.route(pipe)
    return 'CRITICAL' in (r.get('applied'), (r.get('plan') or {}).get('mode'))


def second_route(route, agent, first):
    """Второй ревьюер того же ИИ, что и первый, — на другой модели линейки: иначе это повтор, а не второе мнение."""
    if agent != first.get('agent') or route.get('model') != first.get('model'):
        return route
    alt = [m for _, m in sorted((w.LINEUP.get(agent) or {}).items(), reverse=True) if m != route.get('model')]
    return dict(route, model=alt[0], second_opinion='другая модель того же ИИ: независимого ИИ, кроме автора, нет') if alt else route


def run_model(s, rid):
    r = next((x for x in s['runs'] if x['id'] == rid), None) or {}
    return r.get('model') or ((r.get('packet') or {}).get('route') or {}).get('model')


def dual_review(s, t, pipe, at):
    """None — этап ревью можно закрывать; иначе причина ожидания. Первое одобрение записывается,
    затем ставится второй ревьюер; оба одобрили — этап сдан. Возврат любого — обычный возврат разработчику."""
    if not dual_required(s, pipe):
        return None
    e = pipe['roles'].get(key('review', 'reviewer')) or {}
    rid = e.get('run'); verdict = (e.get('handoff') or {}).get('verdict')
    dr = pipe.setdefault('dual_review', {})
    rec = dict(run=rid, agent=e.get('agent'), model=run_model(s, rid), verdict=verdict, at=at.isoformat())
    if not dr.get('first'):
        dr['first'] = rec
    elif dr['first'].get('run') != rid and not dr.get('second'):
        f = dr['first']
        rec['independence'] = 'provider' if rec['agent'] != f.get('agent') else ('model' if rec['model'] != f.get('model') else 'session')
        dr['second'] = rec
        journal(s, pipe, t['ref'], 'dual_review', 'Двойное ревью CRITICAL: оба одобрили (%s · %s и %s · %s; независимость: %s)' % (
            w.provider_name(f.get('agent')), f.get('model'), w.provider_name(rec['agent']), rec['model'], rec['independence']), 'reviewer', 'review')
    if dr.get('second'):
        return None
    if not active_run(s, t['ref'], role='reviewer'):
        run = create_role_run(s, t, pipe, 'review', 'reviewer', at, second_of=dr['first'])
        if run:
            journal(s, pipe, t['ref'], 'dual_review', 'Задача CRITICAL: первое ревью одобрено (%s), поставлено второе независимое — %s' % (
                dr['first'].get('run'), run['id']), 'reviewer', 'review')
    return 'Этап «Ревью»: второе независимое ревью (задача CRITICAL)'


DUAL_ANALYSIS_MODES = ('off', 'critical')
ALT_ANALYSIS = 'analysis-2.md'           # собственный анализ второго аналитика — рядом с артефактами первого


def dual_analysis_required(s, pipe):
    """Два независимых анализа (07: E2E-R05) — для задач CRITICAL, если владелец включил dual_analysis=critical.
    Второй анализ удваивает расход этапа, поэтому по умолчанию выключен."""
    import adaptive_pipeline as ap
    if w.settings(s).get('dual_analysis', 'off') != 'critical':
        return False
    r = ap.route(pipe)
    return 'CRITICAL' in (r.get('applied'), (r.get('plan') or {}).get('mode'))


def _branch_head(t, pipe):
    ctx = ctx_for(t, pipe)
    return ci.head_sha(ctx.repo, 'refs/heads/' + ctx.branch) if ctx.repo and ctx.branch else ''


def dual_analysis(s, t, pipe, at):
    """None — анализ можно вести к воротам; иначе причина ожидания. Первый анализ записывается, второй ставится
    другому ИИ (role_agent исключает первого); он пишет свой вывод в analysis-2.md, артефакты первого не трогает."""
    if not dual_analysis_required(s, pipe):
        return None
    da = pipe.setdefault('dual_analysis', {})
    if da.get('second'):
        return None
    e = pipe['roles'].get(key('analysis', 'system_analyst')) or {}
    if not da.get('first'):
        rid = e.get('run')
        da['first'] = dict(run=rid, agent=e.get('agent'), model=run_model(s, rid), at=at.isoformat(), sha=_branch_head(t, pipe))
    if not active_run(s, t['ref'], role='system_analyst'):
        run = create_role_run(s, t, pipe, 'analysis', 'system_analyst', at, second_of=da['first'])
        if run:
            journal(s, pipe, t['ref'], 'dual_analysis', 'Задача CRITICAL: первый анализ сдан (%s · %s), поставлен второй независимый — %s (%s)' % (
                da['first']['run'], w.provider_name(da['first'].get('agent')), run['id'], w.provider_name(run.get('agent'))),
                'system_analyst', 'analysis')
    return 'Этап «Анализ»: второй независимый анализ (задача CRITICAL)'


def dual_second_check(t, pipe, ctx, req, run):
    """Провалы сдачи второго аналитика: свой документ, сравнение с первым, артефакты первого не тронуты."""
    da = pipe.get('dual_analysis') or {}
    fails = []
    path = P.art_path(t['id'], P.ART + '/' + ALT_ANALYSIS)
    text = ctx.read(path) or ''
    if len(text.strip()) < 400:
        fails.append(dict(id='dual-own', text='Свой анализ записан',
                          detail='второй анализ — в %s (не короче 400 символов): требования, решения, риски своими словами' % path))
    cmp_ = req.get('comparison') or {}
    diffs = cmp_.get('differences')
    ok = isinstance(diffs, list) and all(isinstance(d, dict) and all(isinstance(d.get(k), str) and d[k].strip()
                                                                       for k in ('topic', 'first', 'second')) for d in diffs)
    if not ok or (not diffs and cmp_.get('agree') is not True) or (diffs and cmp_.get('agree') is True):
        fails.append(dict(id='dual-compare', text='Сравнение с первым анализом',
                          detail='comparison: {agree: true, differences: []} — если согласен во всём, иначе '
                                 '{agree: false, differences: [{topic, first, second}]} по каждому расхождению'))
    base = (da.get('first') or {}).get('sha')
    if base and ctx.repo and ctx.branch:
        import subprocess
        r = subprocess.run(['git', 'diff', '--name-only', base, 'refs/heads/' + ctx.branch], cwd=ctx.repo,
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        own = {path, P.art_path(t['id'], P.ART + '/INDEX.md')}
        touched = [f for f in r.stdout.split() if f and f not in own] if r.returncode == 0 else []
        if touched:
            fails.append(dict(id='dual-independent', text='Артефакты первого анализа не изменены',
                              detail='второй анализ правит только %s; изменены: %s — верните их' % (path, ', '.join(touched[:8]))))
    return fails


def dual_second_record(s, t, pipe, at, run, req):
    """Обе версии сохранены; каждое расхождение — запись DIS-n в реестре, открытая до решения на воротах."""
    da = pipe.setdefault('dual_analysis', {})
    cmp_ = req.get('comparison') or {}
    ids = []
    for d in cmp_.get('differences') or []:
        rec = disagreement(s, t, pipe, at, dict(da.get('first') or {}, position=d['first'][:500]),
                           dict(run=run['id'], agent=run.get('agent'), model=run.get('model'), position=d['second'][:500]),
                           stage='analysis', topic=d['topic'][:200])
        ids.append(rec['id'])
    da['second'] = dict(run=run['id'], agent=run.get('agent'), model=run.get('model'), at=at.isoformat(),
                        agree=not ids, disagreements=ids, document=P.art_path(t['id'], P.ART + '/' + ALT_ANALYSIS))
    journal(s, pipe, t['ref'], 'dual_analysis', 'Второй анализ сдан (%s · %s): %s' % (
        w.provider_name(run.get('agent')), run.get('model'),
        'расхождений нет' if not ids else 'расхождения %s — решение на воротах анализа' % ', '.join(ids)), 'system_analyst', 'analysis')


def open_disagreements(s, pipe, stage='analysis'):
    ids = set((pipe.get('dual_analysis') or {}).get('disagreements') or []) | set(
        ((pipe.get('dual_analysis') or {}).get('second') or {}).get('disagreements') or [])
    return [d for d in s.get('disagreements') or [] if d['id'] in ids and d.get('stage') == stage and d.get('resolution') == 'open']


def disagreement(s, t, pipe, at, first, second, stage='review', topic=None):
    """Реестр разногласий (07: A7-04, E2E-R05): обе позиции, кто и на чём, как разрешено. Строгая позиция побеждает:
    возврат разработчику идёт как обычно, а расхождение остаётся следом для тимлида и метрик качества ревью."""
    reg = s.setdefault('disagreements', [])
    n = max([int(x['id'].split('-')[1]) for x in reg if str(x.get('id', '')).startswith('DIS-')] or [0]) + 1
    if stage == 'analysis':
        d = dict(id='DIS-%d' % n, ref=t['ref'], stage='analysis', topic=topic, at=at.isoformat(), first=first, second=second,
                 resolution='open', rule='молчаливого компромисса нет: ворота анализа называют решение по каждому расхождению')
    else:
        d = dict(id='DIS-%d' % n, ref=t['ref'], stage='review', at=at.isoformat(), first=first, second=second,
                 resolution='return', rule='строгая позиция побеждает: возврат разработчику, оба ревью заново на новом коде')
    reg.append(d); s['disagreements'] = reg[-300:]
    pipe.setdefault('disagreements', []).append(dict(id=d['id'], at=d['at'], first=first.get('run'), second=second.get('run')))
    if stage == 'analysis':
        pipe.setdefault('dual_analysis', {}).setdefault('disagreements', []).append(d['id'])
        journal(s, pipe, t['ref'], 'disagreement', '%s: анализы разошлись по «%s» — первый: %s; второй: %s' % (
            d['id'], topic, (first.get('position') or '')[:200], (second.get('position') or '')[:200]), 'system_analyst', 'analysis')
        return d
    journal(s, pipe, t['ref'], 'disagreement', '%s: ревьюеры разошлись — %s одобрил, %s вернул: %s' % (
        d['id'], first.get('run'), second.get('run'), (second.get('reason') or '')[:300]), 'reviewer', 'review')
    return d


def identity_drift(pipe, run, role):
    """Что изменилось в скилле и политике с постановки запуска: '' — ничего."""
    was = run.get('identity') or {}
    if not was: return ''
    now = run_identity(pipe, role, run.get('agent'))
    out = []
    if was.get('policy_version') != now.get('policy_version'):
        out.append('политика %s → %s' % (was.get('policy_version'), now.get('policy_version')))
    if was.get('skill_sha') and now.get('skill_sha') and was['skill_sha'] != now['skill_sha']:
        out.append('скилл %s %s → %s' % (now.get('skill'), was['skill_sha'], now['skill_sha']))
    return '; '.join(out)


def run_identity(pipe, role, agent):
    """Паспорт запуска роли (07: A0-01, A0-03, A0-05): версия скилла и политики, голова ветки на старте.
    По base_head сервер потом видит, что именно изменил этот запуск (зона правок роли)."""
    import hashlib, skills
    out = dict(skill=skills.name(role), policy_version=w.POLICY['version'])
    try:
        text = skills.render(role)
        out['skill_sha'] = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
        f = skills.targets()['codex' if agent == 'codex' else 'claude'] / skills.name(role) / 'SKILL.md'
        out['skill_installed'] = (f.read_text(encoding='utf-8') == text) if f.exists() else None
    except Exception:  # noqa: BLE001 — паспорт не должен ронять постановку запуска
        out['skill_sha'] = None
    wt = (pipe.get('worktree') or {}).get('path')
    out['base_head'] = ci.head_sha(wt) if wt and os.path.isdir(wt) else ''
    return out


def zone_check(t, pipe, run, role):
    """Файлы, изменённые этим запуском вне зоны роли: [] — всё в своей зоне. Без base_head — по ветке от базы."""
    wt = pipe.get('worktree') or {}
    path, branch = wt.get('path'), wt.get('branch')
    if not path or not os.path.isdir(path):
        return []
    start = (run.get('identity') or {}).get('base_head')
    if not start:
        if role != 'developer':
            return []                  # старый запуск без паспорта: не с чем сравнить, не наказываем задним числом
        start = ctx_for(t, pipe).branches.get('base')
    r = P._git(['diff', '--name-only', start, 'HEAD'], path)
    files = [f for f in r.stdout.splitlines() if f.strip()] if r.returncode == 0 else []
    return P.zone_violations(role, t, files, t.get('scope'))


def hold(s, run, tasks, conf=None):
    """Почему запуск роли пока не открывать (None — можно). Решает диспетчер сервера досок.

    А1 — роль ждёт сдачи предшественника на этапе; А2 — PO не запускается, пока в анализе проекта
    уже WIP задач; А6 — анализ задач одной группы контрактов идёт по очереди."""
    sprint_hold = w.sprints.reason(s, run['ref'], tasks)
    if sprint_hold:
        return sprint_hold
    t = tasks.get(run['ref']); role, stage = run.get('role'), run.get('stage')
    if not t or not role or not t.get('pipeline'):
        return None
    import provider_balance as pb
    dev = pb.author(t, role)
    if dev and run.get('agent') == dev and pb.strict(t) and not (w.settings(s).get('roles') or {}).get(role):
        # TTU-010: проверку сложной или рисковой задачи не делает её автор — ждём независимого ИИ.
        return 'ждёт независимого ИИ: %s выполнял проверяемую работу' % dev
    pipe = t['pipeline']; conf = conf or w.settings(s)
    if stage in P.STAGE:
        dep = P.waits_for(stage, role, pipe.get('needs'))
        if dep and (pipe['roles'].get(key(stage, dep)) or {}).get('state') != 'done':
            return 'ждёт сдачи роли «%s»' % P.ROLES[dep]['title']
    if role == 'product_owner' and stage == 'idea':
        wip = conf.get('wip_analysis', w.WIP_ANALYSIS)
        n = sum(1 for x in tasks.values() if x.get('project') == t['project'] and (x.get('pipeline') or {}).get('stage') == 'analysis'
                and not (x['pipeline'].get('blocked')))
        if n >= wip:
            return 'в анализе проекта уже %d задач (лимит %d): сначала довести их' % (n, wip)
    grp = pipe.get('contract_group')
    if grp and stage == 'analysis':
        for r in s['runs']:
            other = tasks.get(r['ref'])
            if r['ref'] != run['ref'] and r.get('stage') == 'analysis' and r.get('launched_at') and r['status'] in ('queued', 'running') \
                    and other and (other.get('pipeline') or {}).get('contract_group') == grp:
                return 'анализ группы контрактов «%s» идёт в %s: задачи группы — по очереди' % (grp, r['ref'])
    return None


def server_gate(s, t, pipe, gate):
    """Основание закрыть ворота без тимлида или None. Только простые задачи (класс 1) без возвратов и дефектов."""
    import adaptive_pipeline as ap
    if ap.applied(pipe) == 'CRITICAL':
        return None                    # маршрут CRITICAL: решение на воротах — только тимлид
    if w.cx.assess(t)['tier'] != 1 or pipe.get('loops') or any(d['status'] == 'open' for d in pipe.get('defects') or []):
        return None
    if gate == 'analysis':
        if (pipe.get('dual_analysis') or {}).get('disagreements'):
            return None                # расхождения двух анализов решает тимлид или владелец, не сервер
        tr = P.trace(ctx_for(t, pipe))
        if tr['dangling'] or tr['untraced'] or not tr['requirements']:
            return None
        return 'простая задача: DoD ролей анализа сдан, коды требований сходятся (%d требований в RTM)' % tr['requirements']
    if gate == 'release':
        import ci
        run = ci.last(ref=t['ref'], scope='test')
        qa = (pipe['roles'].get(key('testing', 'qa')) or {}).get('handoff') or {}
        if not qa.get('signoff'):
            return None
        # Автоодобрение — только по зелёному прогону на текущей голове тестовой ветки (TT3-012/013).
        ctx = ctx_for(t, pipe)
        test = (ctx.branches or {}).get('test')
        head = ci.head_sha(ctx.repo, 'refs/heads/' + test) if ctx.repo and test else ''
        ok, why = ci.green(run, head) if head else (False, 'HEAD тестовой ветки не определён')
        if ok:
            return 'простая задача: подпись QA, CI на тестовой ветке — %s, открытых дефектов нет' % why[:120]
    return None


def gate_digest(t, pipe, gate):
    """Сводка для тимлида: коды и их сверка, размеры артефактов, история возвратов — вместо чтения всего подряд."""
    ctx = ctx_for(t, pipe)
    out = dict(trace=P.trace(ctx), loops=pipe.get('loops') or {},
               defects=[{k: d.get(k) for k in ('id', 'title', 'severity', 'status')} for d in pipe.get('defects') or []],
               dod={k: [x['id'] for x in (e.get('result') or []) if x.get('ok') is False] for k, e in pipe['roles'].items()},
               rule='Сначала эта сводка; документы читай разделами (op=doc) там, где сводка показывает расхождение или риск.')
    if gate == 'release':
        import ci
        out['ci'] = {sc: ci.summary(ci.last(ref=t['ref'], scope=sc)) for sc in ('task', 'test')}
    return out


def product_owner_recovery():
    """Уведомление PO о доступности ИИ; сервер исполняет обход без отдельной LLM-сессии."""
    current = w.state(); at = w.now()
    pending = current.get('provider_recoveries') or {}
    expired = {a: e for a, e in (current.get('provider_limits') or {}).items()
               if not w.limited(current, a, at) and not e.get('recovery_notified_at')}
    if not pending and not expired:
        return []
    with w.locked():
        s = w.state(); events = dict(s.get('provider_recoveries') or {})
        for agent, entry in (s.get('provider_limits') or {}).items():
            if not w.limited(s, agent, at) and not entry.get('recovery_notified_at'):
                events.setdefault(agent, dict(at=at.isoformat(), reason='наступил срок сброса лимита'))
                entry['recovery_notified_at'] = at.isoformat()
        for agent, event in events.items():
            journal(s, {}, '', 'production_resumed',
                    'Product Owner: %s — %s. Обход готовых задач с конца конвейера: '
                    'Деплой в main → Тестирование → Деплой в тест → Ревью → Разработка → Анализ → Идея; '
                    'с учётом спринта, DoR/DoD и свободных сессий.' % (agent, event['reason']), role='product_owner')
        s['provider_recoveries'] = {}
        w.save('workflow.json', s)
        return list(events)


def dispatch_order(run, tasks, prio):
    """Порядок FR-014 п.3: релиз → тест → деплой в тест → ревью → доработка после возврата →
    разработка → анализ → идея (новое). Внутри уровня — Решала, приоритет и возраст."""
    import flow_policy
    st = run.get('stage'); t = tasks.get(run.get('ref')) or {}
    entry = ((t.get('pipeline') or {}).get('roles') or {}).get(key(st, run.get('role'))) or {}
    rework = bool(entry.get('feedback')) and entry.get('run') == run.get('id')
    return (flow_policy.finish_rank(st, None, rework), 0 if run.get('role') == 'resolver' else 1,
            prio.get(run['ref'], 1), run.get('created_at') or '')


def seal_release(s, t, pipe, at):
    """Пакет доказательств выпуска с отпечатком (07: E2E-R39, AG-OPS-10): из чего сложилось решение о main."""
    import release_bundle as rb
    try:
        ctx = ctx_for(t, pipe)
        main = ci.head_sha(ctx.repo, 'refs/heads/' + ctx.branches['main']) if ctx.repo else ''
        b = rb.build(t, pipe, main, at); p = rb.write(b)
        pipe['release_bundle'] = dict(path=p.as_posix(), digest=b['digest'], main_sha=main, at=at.isoformat())
        journal(s, pipe, t['ref'], 'release_bundle', 'Пакет доказательств выпуска: %s (%s)' % (p.name, b['digest'][:19]))
    except Exception as e:  # noqa: BLE001 — выпуск уже в main; отсутствие пакета видно в журнале и на доске
        pipe['release_bundle'] = dict(error=str(e)[:300], at=at.isoformat())
        journal(s, pipe, t['ref'], 'release_bundle_failed', 'Пакет доказательств выпуска не собран: %s' % str(e)[:300])


def status_of(s, ref, pipe):
    if pipe['stage'] == 'done':
        return 'done'
    if pipe.get('blocked'):
        return 'blocked'
    running = any(r['ref'] == ref and r['status'] == 'running' for r in s['runs'])
    return 'in_progress' if running else 'queued'


def orchestrate(s, t, pipe, at):
    """Двигать задачу, пока можно: DoR этапа → запуски ролей → ворота → следующий этап.
    Возвращает правку задачи (status, status_note, pipeline)."""
    note = None
    import adaptive_pipeline as ap
    # Адаптивный маршрут (FR-001): профиль на приёме, повторная оценка по фактическому диффу.
    conf = w.settings(s)

    def reroute():
        # Повторная оценка перед каждым этапом после разработки: дифф, вышедший за FAST, возвращает задачу.
        ev = ap.refresh(s, t, pipe, at, conf, project_of(t), journal=lambda kind, text: journal(s, pipe, t['ref'], kind, text))
        if ev:
            history(pipe, 'reroute', previous=ev['previous'], mode=ev['mode'], at=at.isoformat())
            return_to(s, t, pipe, at, 'idea', [], 'фактический дифф поднял маршрут %s → %s (%s); аннулированы проверки: %s' % (
                ev['previous'], ev['mode'], '; '.join(ev['reasons'][:3]), ', '.join(ev['invalidated']) or 'нет'), 'Маршрут')
        return ev

    reroute()
    for _ in range(len(P.ORDER) + 3):
        stage = pipe['stage']
        if stage == 'done' or pipe.get('blocked'):
            break
        st = pipe['stages'].setdefault(stage, {})
        if stage in ap.POST_DEV and not st.get('dor_ok') and reroute():
            continue
        if stage in ap.skipped_stages(pipe) and not st.get('passed'):
            st.update(passed=True, passed_at=at.isoformat(), skipped='маршрут FAST')
            journal(s, pipe, t['ref'], 'stage_skipped', 'Этап «%s» не требуется маршрутом FAST: %s' % (
                P.STAGE[stage]['title'], '; '.join(ap.route(pipe)['plan']['risk_reasons'][:2])), stage=stage)
            history(pipe, 'stage_skipped', stage=stage, at=at.isoformat())
            pipe['stage'] = P.ORDER[P.ORDER.index(stage) + 1]
            continue
        if not st.get('dor_ok'):
            if stage != 'idea':      # идее рабочая копия не нужна: её создаёт запуск роли (materialize)
                ensure_worktree(t, pipe)
            dor = P.check_dor(stage, ctx_for(t, pipe), pipe)
            st['dor'] = [dict(id=it['id'], ok=ok, detail=d) for it, ok, d in dor]
            fails = [d for it, ok, d in dor if not ok]
            waiting_owner = bool(fails) and all(it['check'] == 'owner_release' for it, ok, _ in dor if not ok)
            if waiting_owner and (pipe.get('approvals') or {}).get('release'):
                # Одобрение есть, но не действует (другой коммит тестовой ветки, нет подписи, грант истёк):
                # убрать, чтобы колокольчик снова спросил владельца, а не молчал над мёртвой записью.
                why = next((d for it, ok, d in dor if not ok), '')
                pipe['approvals'].pop('release')
                journal(s, pipe, t['ref'], 'approval_void', 'Одобрение релиза больше не действует: ' + why, stage=stage)
            if waiting_owner and w.autonomous(s) and not rp.killed(s):
                # Полная автономия: согласие владельца дано заранее грантом — проекты, ветка, срок (TT3-014).
                ctx = ctx_for(t, pipe)
                grant, gwhy = rp.active_grant(s, t['project'], ctx.branches['main'], at)
                head = ci.head_sha(ctx.repo, 'refs/heads/' + ctx.branches['test']) if ctx.repo else ''
                if grant and head:
                    pipe.setdefault('approvals', {})['release'] = rp.approval(t['ref'], head, at, 'грант полной автономии', grant['id'])
                    journal(s, pipe, t['ref'], 'approved', 'Релиз в main на %s одобрен грантом %s владельца (до %s)' % (
                        head[:7], grant['id'], grant['expires_at'][:10]))
                    dor = P.check_dor(stage, ctx, pipe)
                    st['dor'] = [dict(id=it['id'], ok=ok, detail=d) for it, ok, d in dor]
                    fails = [d for it, ok, d in dor if not ok]
                elif st.get('grant_note') != gwhy:
                    st['grant_note'] = gwhy
                    journal(s, pipe, t['ref'], 'dor_wait', 'Полная автономия, но релиз ждёт владельца: ' + (gwhy if not grant else 'нет коммита тестовой ветки'), stage=stage)
            if fails:
                waiting_owner = all(it['check'] == 'owner_release' for it, ok, _ in dor if not ok)
                note = ('Ждёт одобрения релиза владельцем' if waiting_owner else
                        'DoR этапа «%s» не выполнен: %s' % (P.STAGE[stage]['title'], '; '.join(fails)))
                if st.get('dor_note') != note:
                    journal(s, pipe, t['ref'], 'dor_wait' if waiting_owner else 'dor_failed', note, stage=stage)
                    st['dor_note'] = note
                break
            st.update(dor_ok=True, started=at.isoformat(), dor_note=None)
            if stage == 'development' and not pipe.get('ac_freeze'):
                # Критерии приёмки замораживаются до первого патча (07: A1-02, AG-DEV-02, A5-05): их меняет только
                # возврат в анализ или идею, а не правка паспорта по ходу разработки.
                pipe['ac_freeze'] = dict(digest=P.ac_digest(t.get('acceptance')), at=at.isoformat(), count=len(t.get('acceptance') or []))
            journal(s, pipe, t['ref'], 'stage_started', 'Этап «%s» начат: DoR выполнен' % P.STAGE[stage]['title'], stage=stage)
        roles = P.stage_roles(stage, pipe.get('needs'))
        states = {r: (pipe['roles'].get(key(stage, r)) or {}).get('state') for r in roles}
        for r in roles:
            dep = P.waits_for(stage, r, pipe.get('needs'))
            if states[r] in (None, 'pending') and not (dep and states[dep] != 'done'):
                create_role_run(s, t, pipe, stage, r, at, feedback=(pipe['roles'].get(key(stage, r)) or {}).get('feedback'))
        states = {r: (pipe['roles'].get(key(stage, r)) or {}).get('state') for r in roles}
        if any(v != 'done' for v in states.values()):
            busy = [P.ROLES[r]['title'] + (' (после: %s)' % P.ROLES[P.waits_for(stage, r, pipe.get('needs'))]['title']
                                           if states[r] in (None, 'pending') and P.waits_for(stage, r, pipe.get('needs')) else '')
                    for r, v in states.items() if v != 'done']
            note = 'Этап «%s»: работают %s' % (P.STAGE[stage]['title'], ', '.join(busy))
            break
        if stage == 'review':
            wait = dual_review(s, t, pipe, at)
            if wait:
                note = wait
                break
        if stage == 'analysis':
            wait = dual_analysis(s, t, pipe, at)
            if wait:
                note = wait
                break
        gate = P.STAGE[stage].get('gate')
        if gate:
            g = pipe['gates'].get(gate)
            if not g:
                why = server_gate(s, t, pipe, gate)
                if why:
                    # Простая задача с чистыми проверками: ворота закрывает сервер, тимлид не открывается (Б6).
                    g = pipe['gates'][gate] = dict(decision='approve', notes=why, at=at.isoformat(), by='сервер')
                    journal(s, pipe, t['ref'], 'gate', 'Сервер (%s): approve — %s' % (gate, why), 'teamlead')
                    history(pipe, 'gate', gate=gate, decision='approve', by='server', at=at.isoformat())
            if not g:
                if not active_run(s, t['ref'], role='teamlead', gate=gate):
                    create_role_run(s, t, pipe, stage, 'teamlead', at, gate=gate)
                note = 'Ждёт ворот тимлида «%s»' % ('анализ → разработка' if gate == 'analysis' else 'тест → main')
                break
            if g.get('decision') != 'approve':
                break
        st.update(passed=True, passed_at=at.isoformat())
        journal(s, pipe, t['ref'], 'stage_passed', 'Этап «%s» сдан по DoD' % P.STAGE[stage]['title'], stage=stage)
        history(pipe, 'stage_passed', stage=stage, at=at.isoformat())
        idx = P.ORDER.index(stage)
        if idx == len(P.ORDER) - 1:
            pipe['stage'] = 'done'
            journal(s, pipe, t['ref'], 'done', 'Задача в main: все этапы сданы')
            seal_release(s, t, pipe, at)
            break
        pipe['stage'] = P.ORDER[idx + 1]
    status = status_of(s, t['ref'], pipe)
    patch = dict(pipeline=pipe, status=status, status_note=note if status != 'done' else None)
    if status == 'done':
        patch.update(completed_at=at.isoformat(), evidence='Конвейер пройден: %s' % ' → '.join(P.STAGE[x]['title'] for x in P.ORDER))
    return patch


# ---------------------------------------------------------------- операции

def op_to_idea(s, req, at):
    """Владелец переносит задачи в «Идея»: агенты начинают их заново по конвейеру.

    Одна задача (ref), проект (project) или все проекты (project='*'). Закрытые и отменённые
    не трогаются. Активные запуски отменяются, задача вне конвейера становится задачей
    конвейера на этапе «Идея» (её описание — вход для Product Owner), задача конвейера
    возвращается на «Идея». Сессии сами не открываются: «▶ Запустить проект» или автозапуск
    в пределах лимита сессий. dry_run (по умолчанию) — только показать."""
    reason = (req.get('reason') or '').strip() or 'Владелец перенёс задачу в «Идея»: работа заново по конвейеру'
    snap = w.snapshot(); project = req.get('project')
    if req.get('ref'):
        tasks = [w.task_for(req['ref'])]
    elif project == '*':
        tasks = snap['tasks']
    elif project in snap['projects']:
        tasks = [t for t in snap['tasks'] if t['project'] == project]
    else:
        raise ValueError('Укажите ref задачи, project или project="*" (все проекты)')
    byref = {t['ref']: t for t in snap['tasks']}
    moved, skipped = [], []
    for t in tasks:
        t = byref.get(t['ref'], t)
        if t.get('status') in ('done', 'cancelled'):
            skipped.append(dict(ref=t['ref'], why='закрыта' if t.get('status') == 'done' else 'отменена')); continue
        pipe = t.get('pipeline')
        if pipe and pipe.get('stage') == 'idea' and not pipe.get('blocked'):
            skipped.append(dict(ref=t['ref'], why='уже в «Идея»')); continue
        moved.append(dict(ref=t['ref'], title=t['title'], from_status=t.get('status'), stage=(pipe or {}).get('stage'),
                          runs=[r['id'] for r in s['runs'] if r['ref'] == t['ref'] and r['status'] in ('queued', 'running')]))
    summary = dict(moved=len(moved), skipped=len(skipped), items=moved[:300], skipped_items=skipped[:100],
                   projects=sorted({m['ref'].split('/')[0] for m in moved}),
                   cancel_runs=sum(len(m['runs']) for m in moved))
    if req.get('dry_run', True):
        return dict(summary, dry_run=True)
    gaps = [m for m in moved if not byref[m['ref']].get('root') or not (byref[m['ref']].get('why') or '').strip()]
    if gaps:   # без исходного запроса и описания DoR «Идеи» не пройти: берутся из самой задачи
        d = w.read('projects.json', {})
        for m in gaps:
            t = byref[m['ref']]; fix = {}
            if not (t.get('why') or '').strip():
                fix['why'] = t['title']
            if not t.get('root'):
                fix['root'] = w.create_roots(t['project'], [dict(new_root=dict(title=t['title'][:200], text=(t.get('why') or t['title'])[:5000]))])[0]['root']
            for b in d['projects'][t['project']].get('backlog', []):
                if b['id'] == t['id']:
                    b.update(fix)
            byref[m['ref']] = dict(t, **fix)
        w.save('projects.json', d)
    patches = {}
    for m in moved:
        t = byref[m['ref']]
        for r in s['runs']:
            if r['ref'] == t['ref'] and r['status'] in ('queued', 'running'):
                r.update(status='cancelled', finished_at=at.isoformat(), evidence=reason)
        old = t.get('pipeline')
        if old:
            pipe = copy.deepcopy(old)
            return_to(s, t, pipe, at, 'idea', [], reason, 'Владелец')
            pipe['roles'] = {k: v for k, v in pipe['roles'].items() if not k.startswith('idea/')}
        else:
            pipe = new_pipe('перенос')
            journal(s, pipe, t['ref'], 'idea', 'Задача перенесена в «Идея» владельцем (была «%s»): %s' % (m['from_status'] or 'без статуса', t['title']), stage='idea')
        pipe['pause_auto'] = True          # массовый перенос не открывает окна сам: запуск — кнопкой проекта
        history(pipe, 'to_idea', from_status=m['from_status'], from_stage=m['stage'], at=at.isoformat())
        hist = list(t.get('status_history') or [])[-19:]
        hist.append(dict(at=at.isoformat(), from_status=t.get('status'), to='idea', reason=reason, by='владелец'))
        patch = orchestrate(s, dict(t, pipeline=pipe), pipe, at)
        patch.update(claimed_by=None, status_history=hist)
        patches[t['ref']] = patch
    w.write_patches(s, patches)
    return dict(summary, dry_run=False)


def op_idea(s, req, at):
    """Идея от владельца (терминал Claude/Codex, доска, CLI) — задача в колонке «Идея»."""
    project = req.get('project'); title = (req.get('title') or '').strip(); text = (req.get('text') or '').strip()
    d = w.read('projects.json', {})
    if project not in d.get('projects', {}): raise ValueError('Выберите проект: он задаёт папку и репозиторий для агентов')
    if not 5 <= len(title) <= 200: raise ValueError('Название идеи — от 5 до 200 символов')
    if len(text) < 20: raise ValueError('Опишите идею своими словами (не короче 20 символов)')
    source = req.get('source') if req.get('source') in ('claude', 'codex', 'board', 'cli', 'system') else 'system'
    task = dict(id=w.next_id(d['projects'][project]), title=title, why=text[:1000], size='M',
                priority=req.get('priority') if req.get('priority') in ('🔴', '🟡', '🟢') else '🟡',
                risk='medium', status='queued', source='идея: ' + source, new_root=dict(title=title, text=text))
    dups = similar_tasks(d['projects'][project].get('backlog') or [], project, title, text)
    task = w.create_roots(project, [task])[0]
    d = w.read('projects.json', {}); d['projects'][project].setdefault('backlog', []).append(task); w.save('projects.json', d)
    ref = project + '/' + task['id']
    pipe = new_pipe(source)
    journal(s, pipe, ref, 'idea', 'Новая идея (%s): %s' % (source, title), stage='idea')
    if dups:
        pipe['duplicates'] = dups          # PO решает: слить, связать или это другое (AG-PO-09)
        journal(s, pipe, ref, 'duplicates', 'Похожие задачи проекта: %s' % '; '.join('%s «%s» (%s)' % (x['ref'], x['title'][:60], x['score'])
                                                                                 for x in dups), stage='idea')
    t = dict(task, project=project, ref=ref)
    w.write_patches(s, {ref: orchestrate(s, t, pipe, at)})
    return dict(ref=ref, id=task['id'], root=task['root'], duplicates=dups)


_WORD = re.compile(r'[a-zа-яё0-9]{4,}', re.I)


def _stems(text):
    return {w_[:6] for w_ in _WORD.findall((text or '').lower())}


def similar_tasks(backlog, project, title, text, limit=5, threshold=0.34):
    """Похожие задачи проекта по словам названия и описания (корни слов, Жаккар) — кандидаты в дубли."""
    a = _stems(title) | _stems(text)
    out = []
    for b in backlog:
        if b.get('status') == 'cancelled':
            continue
        bt = _stems(b.get('title')) | _stems(b.get('why'))
        if not a or not bt:
            continue
        score = len(a & bt) / len(a | bt)
        if score >= threshold or (b.get('title') or '').strip().lower() == title.strip().lower():
            out.append(dict(ref='%s/%s' % (project, b['id']), title=b.get('title', ''), status=b.get('status'), score=round(score, 2)))
    return sorted(out, key=lambda x: -x['score'])[:limit]


def _run(s, req, t, role=None):
    run = next((r for r in s['runs'] if r['id'] == req.get('run') and r['ref'] == t['ref']), None)
    if not run or not run.get('role'): raise ValueError('Запуск роли не найден')
    if run['status'] != 'running': raise ValueError('Запуск не выполняется: сначала claim')
    if req.get('worker') != run.get('worker'): raise ValueError('Сдаёт работу только принявший запуск исполнитель')
    if role and run['role'] != role: raise ValueError('Это запуск роли %s' % run['role'])
    return run


def op_handoff(s, req, t, at):
    """Роль сдаёт этап. Сервер проверяет DoD; без выполненного DoD передача не принимается."""
    pipe = copy.deepcopy(t['pipeline']); run = _run(s, req, t)
    role, stage = run['role'], run['stage']
    if role == 'teamlead': raise ValueError('Тимлид решает ворота через op=gate')
    if stage != pipe['stage']: raise ValueError('Задача уже не на этапе «%s»' % P.STAGE[stage]['title'])
    validate_handoff(req)                                  # ошибка договора — до коммита, журнала и проверок
    handoff = {k: req.get(k) for k in ('items', 'summary', 'needs', 'acceptance', 'verdict', 'signoff', 'contract_group',
                                       'knowledge_applied', 'ac_matrix', 'reviewed_sha', 'comparison') + UNCERTAIN
               if req.get(k) is not None}
    patch = {}
    if handoff.get('questions') or handoff.get('conflicts'):
        return uncertain_stop(s, t, pipe, run, handoff, req, at)
    if role == 'product_owner':
        acc = handoff.get('acceptance')
        if isinstance(acc, list) and all(isinstance(a, str) and a.strip() for a in acc):
            patch['acceptance'] = [a.strip() for a in acc]
        if isinstance(handoff.get('needs'), dict): pipe['needs'] = handoff['needs']
        grp = handoff.get('contract_group')
        if grp is not None:
            if not isinstance(grp, str) or not grp.strip() or len(grp) > 60:
                raise ValueError('contract_group: короткое имя группы задач с общими контрактами (API, модель данных), до 60 символов')
            pipe['contract_group'] = grp.strip().lower()
    _autocommit(s, t, pipe, run, req['summary'].strip())
    ctx = ctx_for(dict(t, **patch), pipe, handoff)
    results = P.evaluate_role(role, stage, ctx)
    fails = P.failures(results)
    fixed = {}
    if role == 'developer':
        defect_fails, fixed = check_fixes(t, pipe, ctx, req.get('fixes'))
        fails = fails + defect_fails
    second_analysis = role == 'system_analyst' and bool((run.get('dual') or {}).get('second_of'))
    if second_analysis:
        fails = fails + dual_second_check(t, pipe, ctx, req, run)
    k = key(stage, role); entry = pipe['roles'].setdefault(k, {})
    entry['result'] = [dict(id=it['id'], ok=ok, detail=d) for it, ok, d in results]
    if role == 'developer':
        _learning_handoff(run, t, ctx, handoff)
    history(pipe, 'handoff', role=role, stage=stage, agent=run.get('agent'), model=run.get('model'), run=run['id'],
            accepted=not fails, failed=[f['id'] for f in fails], at=at.isoformat())
    drift = identity_drift(pipe, run, role)
    if drift:   # скилл или политика сменились посреди запуска: этап принят по старым правилам — это видно (07: A0-05)
        journal(s, pipe, t['ref'], 'policy_drift', '%s %s: %s' % (run['id'], P.ROLES[role]['title'], drift), role, stage)
        history(pipe, 'policy_drift', role=role, stage=stage, run=run['id'], detail=drift, at=at.isoformat())
    zone = zone_check(t, pipe, run, role)
    if zone:
        import sec_audit
        sec_audit.record('zone_violation', ', '.join(zone[:8]), run=run['id'], agent=run.get('agent'), op='handoff', ref=t['ref'])
        # Сервер, а не инструкция, держит границы роли: правка вне зоны не принимается (07: A1-05, A7-02).
        fails = fails + [dict(id='zone', text='Изменения только в зоне роли',
                              detail='вне зоны роли «%s» изменены: %s — верните их (git checkout <base_head> -- <файл>) '
                                     'или передайте задачу роли, которой это положено' % (P.ROLES[role]['title'], ', '.join(zone[:8])))]
    oracle_fail = next((f for f in fails if f['id'] == 'dev-oracle'), None)
    if oracle_fail:
        import sec_audit
        sec_audit.record('oracle_changed', oracle_fail['detail'][:300], run=run['id'], agent=run.get('agent'), op='handoff', ref=t['ref'])
        # Изменение эталона приёмки решает только владелец: вопрос попадает в колокольчик (SEC-010).
        pipe['oracle_pending'] = dict(at=at.isoformat(), detail=oracle_fail['detail'][:500], run=run['id'])
    elif role == 'developer':
        pipe.pop('oracle_pending', None)
    if fails:
        journal(s, pipe, t['ref'], 'handoff_rejected', '%s: передача не принята — %s' % (
            P.ROLES[role]['title'], '; '.join('%s (%s)' % (f['id'], f['detail']) for f in fails)), role, stage)
        env = failure.envelope(fails, role, stage, run['id'], run.get('agent'), at.isoformat())
        hist = entry.setdefault('failures', []); hist.append(env); del hist[:-5]
        same = failure.repeated([h for h in hist if h.get('run') == run['id']], env['fingerprint'])
        if same >= SAME_REJECTS:
            log = pipe.setdefault('failures', []); log.append(env); del log[:-30]
            _autocommit(s, t, pipe, run, 'WIP: передача %d раза отклонена одинаково' % same)
            run.update(status='done', finished_at=at.isoformat(),
                       evidence='Эскалация сервера: одинаковый отказ сдачи %d раза (%s)' % (same, env['fingerprint']))
            entry.update(state='pending', feedback=fails)
            prob = escalate(s, t, pipe, at, '%s: передача %d раза подряд отклонена по одной причине (класс %s — %s; отпечаток %s) — %s' % (
                P.ROLES[role]['title'], same, env['cls'], env['meaning'], env['fingerprint'],
                '; '.join('%s (%s)' % (f['id'], f['detail']) for f in fails)[:600]), role, run.get('agent'))
            w.write_patches(s, {t['ref']: dict(patch, pipeline=pipe, claimed_by=None, status='blocked',
                                               status_note='Проблема %s у Решалы: одинаковый отказ сдачи' % prob['id'])})
            return dict(accepted=False, failed=fails, escalated=prob['id'], failure=env)
        w.write_patches(s, {t['ref']: dict(patch, pipeline=pipe)})
        return dict(accepted=False, failed=fails, failure=env,
                    hint='Исправь пункты и пришли handoff снова — запуск остаётся твоим.' +
                         (' Та же ошибка уже %d раза: ещё %d — и задача уйдёт Решале.' % (same, SAME_REJECTS - same) if same > 1 else ''))
    for d in pipe.get('defects') or []:
        if d['id'] in fixed:
            d.update(status='fixed', fix=dict(fixed[d['id']], at=at.isoformat(), agent=run.get('agent'), model=run.get('model')))
        elif role == 'qa' and handoff.get('signoff') and d['status'] == 'fixed':
            # Сюда доходят только при пройденном qa-retest: тест на месте, CI зелёный на этом коммите (QA-08).
            test = ctx.branches.get('test')
            d.update(status='verified', verified_at=at.isoformat(), verified_by=run.get('agent'), verified_by_runner='server-ci',
                     verified_sha=ci.head_sha(ctx.repo, 'refs/heads/' + test) if ctx.repo and test else '')
    heavy = P.pending_heavy(results)
    if stage in ('idea', 'analysis'):
        write_index(s, dict(t, **patch), pipe, run)
    run.update(status='done', finished_at=at.isoformat(), evidence=req['summary'].strip(), tokens=req.get('tokens'),
               handoff=handoff)
    entry.update(state='checking' if heavy else 'done', handoff=handoff, at=at.isoformat(), feedback=None)
    if not heavy:
        entry.pop('failures', None)
    if second_analysis:
        dual_second_record(s, t, pipe, at, run, req)
    elif role == 'system_analyst' and stage == 'analysis':
        publish_contract(s, t, pipe, ctx, at)
    if role == 'product_owner':
        pipe['brd_digest'] = P.brd_digest(ctx)          # анализ идёт по этому BRD (AG-PO-05)
    journal(s, pipe, t['ref'], 'handoff', '%s сдал(а) этап: %s%s' % (P.ROLES[role]['title'], req['summary'].strip()[:300],
            ' · фоновые проверки: ' + ', '.join(heavy) if heavy else ''), role, stage)
    if heavy:
        w.write_patches(s, {t['ref']: dict(patch, pipeline=pipe, claimed_by=None, status='in_progress',
                                           status_note='%s: идут проверки сервера (%s)' % (P.ROLES[role]['title'], ', '.join(heavy)))})
        return dict(accepted=True, checking=heavy, _pipejob=(t['ref'], role, stage, run['id']))
    w.write_patches(s, {t['ref']: dict(orchestrate(s, dict(t, **patch), pipe, at), **patch, claimed_by=None)})
    return dict(accepted=True, stage=pipe['stage'])


def _learning_handoff(run, t, ctx, handoff):
    """Сдача разработчика (§5 п.3): ревизия кода и применённые карточки — в запуск; сбой учёта работу не останавливает."""
    try:
        lr = run.setdefault('learning', {})
        lr['code_revision'] = branch_head(ctx)
        applied = handoff.get('knowledge_applied')
        if isinstance(applied, list):
            import learning_knowledge as lk
            lk.note_use(run['id'], t['ref'], [], applied=[a for a in applied if isinstance(a, str)])
    except Exception:
        pass


def job(ref, role, stage, run_id):
    """Фоновое задание: тяжёлые пункты DoD (тесты, слияния), затем решение и оркестрация."""
    t = w.task_for(ref); s0 = w.state()
    run = next(r for r in s0['runs'] if r['id'] == run_id)
    pipe0 = t['pipeline']
    results = P.evaluate_role(role, stage, ctx_for(t, pipe0, run.get('handoff') or {}), heavy=True)
    fails = P.failures(results)
    with w.locked():
        s = w.state(); at = w.now(); t = w.task_for(ref); pipe = copy.deepcopy(t['pipeline'])
        entry = pipe['roles'].setdefault(key(stage, role), {})
        entry['result'] = [dict(id=it['id'], ok=ok, detail=d) for it, ok, d in results]
        history(pipe, 'check', role=role, stage=stage, agent=run.get('agent'), model=run.get('model'), run=run_id,
                accepted=not fails, failed=[f['id'] for f in fails], at=at.isoformat())
        rolled = next((f for f in fails if f['id'] == 'dm-verify-main' and 'откачено' in (f.get('detail') or '')), None)
        if fails:
            entry['state'] = 'returned'
            text = '; '.join('%s (%s)' % (f['id'], f['detail']) for f in fails)
            journal(s, pipe, ref, 'check_failed', '%s: проверки сервера не прошли — %s' % (P.ROLES[role]['title'], text), role, stage)
            if rolled:
                # Красный CI на main: слияние уже откачено, повторный деплой того же кода бессмыслен — задача к разработчику
                run_ = dict(run, role=role, stage=stage)
                ids = register_defects(pipe, run_, [dict(title='CI на main упал после релиза: ' + rolled['detail'][:250],
                                                         severity='critical', level='e2e', repro='CI main: ' + rolled['detail'][:900])], ctx_for(t, pipe), at, required=False)
                history(pipe, 'return', role=role, stage=stage, to_stage='development', agent=run.get('agent'), model=run.get('model'),
                        run=run_id, defects=ids, roles=[], reason=rolled['detail'][:500], at=at.isoformat())
                return_to(s, t, pipe, at, 'development', [], 'CI на main упал, релиз откачен: ' + rolled['detail'][:400], 'CI')
                w.write_patches(s, {ref: dict(orchestrate(s, t, pipe, at), claimed_by=None)}); w.save('workflow.json', s)
                return dict(ok=False, failed=fails, returned='development')
            denied = next((f for f in fails if f['id'] == 'dm-merge-main' and 'запрещён политикой' in (f.get('detail') or '')), None)
            if denied and not rp.killed(s):
                # Решение владельца перестало действовать (новый коммит в тесте, отозван или истёк грант):
                # спросить снова, а не гонять DevOps по кругу — то же слияние без решения не пройдёт.
                pipe.get('approvals', {}).pop('release', None)
                pipe['stages'].setdefault(stage, {})['dor_ok'] = False
                entry.update(state='pending', feedback=fails)     # после нового решения DevOps запустится снова
                journal(s, pipe, ref, 'approval_void', 'Слияние в main остановлено политикой: ' + denied['detail'][:300], role, stage)
                w.write_patches(s, {ref: dict(orchestrate(s, t, pipe, at), claimed_by=None)}); w.save('workflow.json', s)
                return dict(ok=False, failed=fails, waiting='owner')
            # Конверт сбоя и отпечаток (07: A8-02, A8-05): та же ошибка дважды подряд — к Решале сразу,
            # третья попытка того же провала ничего не даст, только сожжёт токены.
            env = failure.envelope(fails, role, stage, run_id, run.get('agent'), at.isoformat())
            hist = entry.setdefault('failures', []); hist.append(env); del hist[:-5]
            log = pipe.setdefault('failures', []); log.append(env); del log[:-30]
            same = failure.repeated(hist, env['fingerprint'])
            if same >= 2:
                escalate(s, t, pipe, at, '%s: одна и та же ошибка %d раза подряд (класс %s — %s; отпечаток %s) — %s' % (
                    P.ROLES[role]['title'], same, env['cls'], env['meaning'], env['fingerprint'], text), role)
            elif entry.get('attempts', 0) + 1 >= MAX_ATTEMPTS:
                escalate(s, t, pipe, at, '%s: DoD не сдан %d раза подряд (класс %s) — %s' % (
                    P.ROLES[role]['title'], MAX_ATTEMPTS, env['cls'], text), role)
            else:
                create_role_run(s, t, pipe, stage, role, at, feedback=fails)
            patch = dict(pipeline=pipe, status=status_of(s, ref, pipe), status_note='Возврат роли %s: %s' % (P.ROLES[role]['title'], text[:300]))
        else:
            entry['state'] = 'done'; entry.pop('failures', None)
            journal(s, pipe, ref, 'check_passed', '%s: проверки сервера прошли' % P.ROLES[role]['title'], role, stage)
            patch = orchestrate(s, t, pipe, at)
        w.write_patches(s, {ref: patch}); w.save('workflow.json', s)
    return dict(ok=not fails, failed=fails)


# Куда вести сбой по его классу (07: AG-RES-03, E2E-R18): тест — разработчику, окружение — DevOps, политика — владельцу…
ROUTE_BY_CLASS = dict(test='developer', scope='developer', environment='devops', runtime='devops', security='owner',
                      policy='owner', quota='pause', artifact='artifact_owner', unknown='resolver')
CHECK_OWNER = (('po-', 'product_owner'), ('sa-', 'system_analyst'), ('arch-', 'solution_architect'), ('ds-', 'designer'),
               ('mkt-', 'marketer'), ('qa-', 'qa'), ('rv-', 'reviewer'), ('dev-', 'developer'), ('dt-', 'devops'), ('dm-', 'devops'))
TRIVIAL = ('quota', 'environment')       # короткий путь Решалы: один вариант решения, без выбора из двух (AG-RES-06)
CAUSE_KINDS = ('fact', 'hypothesis')


def route_hint(env, role):
    """Целевая роль по классу конверта сбоя; артефакт — владельцу проваленного пункта."""
    if not env:
        return dict(cls='unknown', target='resolver', why='конверта сбоя нет — причина неизвестна (UNKNOWN), нужен разбор')
    cls = env.get('cls') or 'unknown'; target = ROUTE_BY_CLASS.get(cls, 'resolver')
    if target == 'artifact_owner':
        cid = next((c.get('id') for c in env.get('checks') or [] if c.get('cls') == 'artifact'), '') or ''
        target = next((r for p, r in CHECK_OWNER if cid.startswith(p)), role or 'resolver')
    return dict(cls=cls, target=target, why='%s: %s' % (cls, failure.CLASSES.get(cls, '')))


def problem_evidence(s, t, pipe, role):
    """Доказательная база проблемы (AG-RES-02): конверт, прежние конверты, коммит, журнал. Ничего нет — UNKNOWN."""
    pipe = pipe or {}
    entry = (pipe.get('roles') or {}).get(key(pipe.get('stage') or '', role or '')) or {}
    envs = (entry.get('failures') or [])[-4:] or [e for e in pipe.get('failures') or [] if e.get('role') == role][-4:]
    wt = (pipe.get('worktree') or {}).get('path')
    sha = ci.head_sha(wt) if wt and os.path.isdir(wt) else ''
    runs = [r for r in s.get('runs') or [] if r['ref'] == t['ref'] and (not role or r.get('role') == role)]
    log = next(((r.get('launch') or {}).get('log') for r in reversed(runs) if (r.get('launch') or {}).get('log')), None)
    basis = 'envelope' if envs else ('log' if log else 'UNKNOWN')
    return dict(envelope=envs[-1] if envs else None, prior=envs[:-1], sha=sha, log=log, basis=basis,
                run=runs[-1]['id'] if runs else None)


def open_problem(s, t, at, reason, role=None, agent=None, stage=None, source='server', tried=None, need=None, pipe=None):
    """Проблема в реестр: её разбирает Решала, а если нужен владелец — она в колокольчике."""
    reg = s.setdefault('problems', [])
    p = dict(id='P-%d' % (len(reg) + 1), ref=t['ref'], title=t.get('title', ''), role=role, agent=agent, stage=stage,
             reason=reason[:1500], tried=(tried or '')[:1500], need=(need or '')[:1000], source=source,
             at=at.isoformat(), status='open')
    try:
        p['evidence'] = problem_evidence(s, t, pipe if pipe is not None else t.get('pipeline'), role)
        if p['evidence']['basis'] == 'UNKNOWN' and len((tried or '').strip()) >= 20:
            p['evidence']['basis'] = 'agent_report'          # что пробовал агент — свидетельство, хоть и не машинное
        p['route_hint'] = route_hint(p['evidence']['envelope'], role)
    except Exception as e:  # noqa: BLE001 — нет доказательств — это тоже факт, но проблему завести нужно
        p['evidence'] = dict(basis='UNKNOWN', error=str(e)[:200]); p['route_hint'] = route_hint(None, role)
    reg.append(p)
    return p


def problem(s, pid):
    return next((p for p in s.get('problems') or [] if p['id'] == pid), None)


def create_resolver_run(s, t, pipe, prob, at):
    """Запуск Решалы — на другом ИИ, чем агент с проблемой, если тот свободен."""
    if active_run(s, t['ref'], role='resolver'):
        return None
    tt = dict(t, pipeline=pipe) if pipe else t
    agent = role_agent(s, tt, 'resolver', exclude=(prob['agent'],) if prob.get('agent') else ())
    run = w.new_run(s, t['ref'], None, at)
    route = role_route(tt, 'resolver', agent)
    run.update(role='resolver', stage=(pipe or {}).get('stage'), phase='role', agent=agent, problem=prob['id'],
               auto_launch=w.settings(s).get('auto_pipeline', True), packet=dict(route=route, phase='role'))
    prob.update(status='resolving', run=run['id'])
    if pipe:
        journal(s, pipe, t['ref'], 'run', 'Решала: запуск %s по проблеме %s (%s · %s)' % (run['id'], prob['id'], route['model'], route['effort']), 'resolver')
    return run


def escalate(s, t, pipe, at, reason, role=None, agent=None, source='server', tried=None, need=None):
    prob = open_problem(s, t, at, reason, role, agent, pipe['stage'], source, tried, need, pipe=pipe)
    pipe['blocked'] = dict(reason=reason[:500], role=role, at=at.isoformat(), problem=prob['id'])
    journal(s, pipe, t['ref'], 'escalation', '%s → Решала: %s' % (prob['id'], reason), role)
    create_resolver_run(s, t, pipe, prob, at)
    return prob


def op_escalate(s, req, t, at):
    """Агент столкнулся с проблемой или ограничением — к Решале. Работает и для задач вне конвейера."""
    reason = (req.get('reason') or '').strip()
    if len(reason) < 20: raise ValueError('reason: в чём проблема и что нужно (не короче 20 символов)')
    tried = (req.get('tried') or '').strip(); need = (req.get('need') or '').strip()
    if not is_pipeline(t):
        run = next((r for r in s['runs'] if r['id'] == req.get('run') and r['ref'] == t['ref'] and r['status'] == 'running'), None)
        if not run: raise ValueError('Запуск не найден или не выполняется')
        if req.get('worker') != run.get('worker'): raise ValueError('Эскалирует принявший запуск исполнитель')
        run.update(status='done', finished_at=at.isoformat(), evidence='Эскалация Решале: ' + reason)
        prob = open_problem(s, t, at, reason, run.get('role'), run.get('agent'), None, 'agent', tried, need)
        create_resolver_run(s, t, None, prob, at)
        w.write_patches(s, {t['ref']: dict(status='blocked', status_note='Проблема %s у Решалы: %s' % (prob['id'], reason[:300]), claimed_by=None)})
        return dict(escalated=True, problem=prob['id'])
    pipe = copy.deepcopy(t['pipeline']); run = _run(s, req, t)
    if run['role'] == 'resolver': raise ValueError('Решала не эскалирует сам себе: позови владельца через op=resolve decision=owner')
    hit = recipes.match(s, ' '.join((reason, tried, need)), run['role']) if run['role'] != 'teamlead' else None
    if hit and hit['id'] not in (run.get('recipes') or []):
        return apply_recipe(s, t, pipe, run, hit, reason, at)      # повтор той же проблемы в запуске — уже к Решале
    _autocommit(s, t, pipe, run, 'WIP перед эскалацией: ' + reason)
    run.update(status='done', finished_at=at.isoformat(), evidence='Эскалация Решале: ' + reason)
    if run['role'] != 'teamlead':
        pipe['roles'].setdefault(key(run['stage'], run['role']), {}).update(state='pending', feedback=None)
    prob = escalate(s, t, pipe, at, '%s: %s' % (P.ROLES[run['role']]['title'], reason), run['role'], run.get('agent'), 'agent', tried, need)
    w.write_patches(s, {t['ref']: dict(pipeline=pipe, status='blocked', status_note='Проблема %s у Решалы: %s' % (prob['id'], reason[:300]), claimed_by=None)})
    return dict(escalated=True, problem=prob['id'])


def _recipe_problem(s, t, pipe, run, rid, reason, text, at):
    prob = open_problem(s, t, at, reason, run.get('role'), run.get('agent'), pipe['stage'], 'agent' if rid != 'Р3' else 'server')
    prob.update(status='resolved', decision='recipe', recipe=rid, instructions=text[:2000], decided_at=at.isoformat(),
                resolved_by=dict(agent='сервер', model=None))
    for p in s.get('playbook') or []:
        if p['id'] == rid:
            p['uses'] = p.get('uses', 0) + 1
    return prob


def apply_recipe(s, t, pipe, run, hit, reason, at):
    """Механическая проблема: рецепт сервера вместо сессии Решалы (Р1, Р2, Р4, Р5, Р6 и накопленные)."""
    role, stage = run['role'], run['stage']
    run.setdefault('recipes', []).append(hit['id'])
    text = hit['instructions']; title = '%s: %s' % (P.ROLES[role]['title'], reason)
    kind = hit['kind']
    if kind == 'wait':
        dep = P.waits_for(stage, role, pipe.get('needs'))
        if dep and (pipe['roles'].get(key(stage, dep)) or {}).get('state') != 'done':
            prob = _recipe_problem(s, t, pipe, run, hit['id'], title, text, at)
            journal(s, pipe, t['ref'], 'recipe', 'Рецепт %s (%s): %s' % (hit['id'], hit['title'], text), role, stage)
            run.update(status='cancelled', finished_at=at.isoformat(), evidence='Рецепт %s: ждёт сдачи роли «%s»' % (hit['id'], P.ROLES[dep]['title']))
            pipe['roles'].setdefault(key(stage, role), {}).update(state='pending', feedback=None)
            w.write_patches(s, {t['ref']: dict(orchestrate(s, t, pipe, at), claimed_by=None)})
            return dict(escalated=False, recipe=hit['id'], problem=prob['id'], waiting=dep, instructions=text)
        sa = [P.art_path(t['id'], a['path']) for a in P.ROLES['system_analyst']['artifacts'] if not a.get('optional')]
        text = 'ТЗ аналитика уже сдано: %s. Оглавление — tt_work op=doc ref=%s path=<путь>, раздел — section=<заголовок>.' % (
            ', '.join(sa), t['ref'])
        kind = 'advise'
    if kind == 'contract':
        tr = P.trace(ctx_for(t, pipe))
        matrix = 'определено: %s; висящие ссылки: %s; вне RTM: %s' % (
            ', '.join('%s %d' % kv for kv in sorted(tr['defined'].items())) or 'нет',
            '; '.join('%s в %s' % (k, ', '.join(v)) for k, v in tr['dangling'].items()) or 'нет',
            ', '.join(tr['untraced']) or 'нет')
        prob = _recipe_problem(s, t, pipe, run, hit['id'], title, text + ' Матрица: ' + matrix, at)
        _autocommit(s, t, pipe, run, 'WIP перед возвратом контракта: ' + reason)
        run.update(status='done', finished_at=at.isoformat(), evidence='Рецепт %s: контракт — к системному аналитику' % hit['id'])
        detail = 'Противоречие контрактов (%s, %s): %s. Матрица кодов: %s' % (P.ROLES[role]['title'], prob['id'], reason[:600], matrix)
        history(pipe, 'return', role='server', stage=pipe['stage'], to_stage='analysis', agent=None, model=None, run=run['id'],
                defects=[], roles=['system_analyst'], reason=detail[:500], at=at.isoformat())
        return_to(s, t, pipe, at, 'analysis', ['system_analyst'], detail, 'Сервер (рецепт %s)' % hit['id'])
        w.write_patches(s, {t['ref']: dict(orchestrate(s, t, pipe, at), claimed_by=None)})
        return dict(escalated=False, recipe=hit['id'], problem=prob['id'], returned='analysis', instructions=text)
    prob = _recipe_problem(s, t, pipe, run, hit['id'], title, text, at)
    sha = _autocommit(s, t, pipe, run, 'рецепт %s: %s' % (hit['id'], reason))
    journal(s, pipe, t['ref'], 'recipe', 'Рецепт %s (%s) — запуск продолжается' % (hit['id'], hit['title']), role, stage)
    w.write_patches(s, {t['ref']: dict(pipeline=pipe)})
    return dict(escalated=False, recipe=hit['id'], problem=prob['id'], committed=sha, instructions=text,
                next='Продолжай работу в этом же запуске; если рецепт не помог — op=escalate снова, тогда проблему разберёт Решала.')


def session_recipe(s, t, pipe, run, at):
    """Р3: сессия роли трижды закончилась без сдачи — сервер сверяет артефакты и возвращает роль к работе
    со списком (один раз на роль этапа). Возвращает правку задачи или None — тогда к Решале."""
    k = key(run['stage'], run['role']); entry = pipe['roles'].setdefault(k, {})
    if 'Р3' in (entry.get('recipes') or []) or run['role'] in ('teamlead', 'resolver'):
        return None
    ctx = ctx_for(t, pipe)
    missing = [P.art_path(t['id'], a['path']) for a in P.ROLES[run['role']]['artifacts']
               if not a.get('optional') and P.check_document(ctx.artifact(P.effective(a, ctx)), P.effective(a, ctx))]
    if missing:
        text = 'Сессия трижды закончилась без сдачи. Не готовы артефакты: %s. Доделай их и сдай op=handoff.' % ', '.join(missing)
    else:
        text = ('Сессия трижды закончилась без сдачи, а артефакты роли на месте и проходят проверку. '
                'Проверь пункты DoD и сразу сдай op=handoff — новых исследований не начинай.')
    prob = _recipe_problem(s, t, pipe, run, 'Р3', '%s: сессия без сдачи' % P.ROLES[run['role']]['title'], text, at)
    entry.update(state='pending', recipes=(entry.get('recipes') or []) + ['Р3'],
                 feedback=[dict(id='recipe-Р3', text='Рецепт Р3 (%s)' % prob['id'], detail=text)])
    journal(s, pipe, t['ref'], 'recipe', 'Рецепт Р3: %s' % text, run['role'], run['stage'])
    return dict(orchestrate(s, t, pipe, at), claimed_by=None)


DECISIONS = dict(continue_='продолжить с указаниями', return_='вернуть на этап', owner='нужен владелец')


def close_conflicts(pipe, prob, who, text, at):
    """Противоречия и вопросы роли закрываются решением по их проблеме (Решала или владелец)."""
    for c in pipe.get('conflicts') or []:
        if c.get('problem') == prob['id'] and c['status'] == 'open':
            c.update(status='resolved', resolution=text[:1000], resolved_by=who, resolved_at=at.isoformat())


def _resume_role(s, t, pipe, prob, at, who, text):
    """Снять блок и вернуть работу роли, у которой была проблема, с указаниями."""
    pipe.pop('blocked', None)
    close_conflicts(pipe, prob, who, text, at)
    if prob.get('role') and prob['role'] not in ('teamlead', 'resolver') and prob.get('stage'):
        pipe['roles'].setdefault(key(prob['stage'], prob['role']), {}).update(
            state='pending', feedback=[dict(id=who, text='Решение по проблеме %s (%s)' % (prob['id'], 'владелец' if who == 'owner' else 'Решала'), detail=text)])
    return orchestrate(s, t, pipe, at)


def op_resolve(s, req, t, at):
    """Решение Решалы: пути решения (не меньше двух) и одно из трёх — продолжить, вернуть, позвать владельца."""
    run = next((r for r in s['runs'] if r['id'] == req.get('run') and r['ref'] == t['ref'] and r.get('role') == 'resolver'), None)
    if not run or run['status'] != 'running': raise ValueError('Запуск Решалы не найден или не выполняется (сначала claim)')
    if req.get('worker') != run.get('worker'): raise ValueError('Решение присылает принявший запуск исполнитель')
    prob = problem(s, run.get('problem'))
    if not prob: raise ValueError('Проблема запуска не найдена')
    opts = req.get('options')
    hint = prob.get('route_hint') or {}
    need_opts = 1 if hint.get('cls') in TRIVIAL else 2
    if not isinstance(opts, list) or len(opts) < need_opts or any(not isinstance(o, dict) or len(str(o.get('title') or '').strip()) < 10 for o in opts):
        raise ValueError('options: не меньше %s {title ≥10 символов, pros, cons}' % (
            'одного пути решения (простая проблема класса %s)' % hint['cls'] if need_opts == 1 else 'двух путей решения'))
    kind, cause, probe = req.get('cause_kind'), str(req.get('cause') or '').strip(), str(req.get('probe') or '').strip()
    if kind not in CAUSE_KINDS or len(cause) < 15:
        raise ValueError('cause_kind: fact (установлено) или hypothesis (предположение); cause: причина проблемы, не короче 15 символов')
    if kind == 'hypothesis' and len(probe) < 15:
        raise ValueError('probe: гипотеза без проверки не решение — как подтвердить причину (команда, тест, журнал)')
    if kind == 'fact' and (prob.get('evidence') or {}).get('basis') == 'UNKNOWN' and len(probe) < 15:
        raise ValueError('cause_kind=fact при evidence.basis=UNKNOWN: доказательства нет — укажи probe (чем подтвердил) или hypothesis')
    decision = req.get('decision'); notes = (req.get('notes') or '').strip()
    if decision not in ('continue', 'return', 'owner'): raise ValueError('decision: continue (продолжить), return (вернуть на этап) или owner (нужен владелец)')
    if len(notes) < 30: raise ValueError('notes: почему выбран этот путь (не короче 30 символов)')
    opts = [dict(title=str(o['title']).strip()[:300], pros=str(o.get('pros') or '')[:500], cons=str(o.get('cons') or '')[:500]) for o in opts[:6]]
    if req.get('recipe') is not None:
        prob['learned'] = recipes.learn(s, req['recipe'], prob, at)    # база решений: в следующий раз ответит сервер
    prob.update(options=opts, decision=decision, notes=notes[:2000], resolved_by=dict(agent=run.get('agent'), model=run.get('model')), decided_at=at.isoformat(),
                cause=dict(kind=kind, text=cause[:1000], probe=probe[:500] or None))
    run.update(status='done', finished_at=at.isoformat(), evidence='Решала (%s): %s — %s' % (prob['id'], decision, notes[:300]))
    pipe = copy.deepcopy(t['pipeline']) if is_pipeline(t) else None
    if decision == 'owner':
        q = (req.get('question') or '').strip(); why = (req.get('why_owner') or '').strip()
        if len(q) < 20: raise ValueError('question: что именно решить владельцу (не короче 20 символов)')
        if len(why) < 20: raise ValueError('why_owner: почему без владельца нельзя (не короче 20 символов)')
        prob.update(status='owner', question=q[:1500], why_owner=why[:800], notified=False)
        note = '👤 Нужен владелец (%s): %s' % (prob['id'], q[:250])
        if pipe:
            pipe['blocked'] = dict(pipe.get('blocked') or {}, reason=note, problem=prob['id'], owner=True)
            journal(s, pipe, t['ref'], 'owner_needed', note, 'resolver')
            w.write_patches(s, {t['ref']: dict(pipeline=pipe, status='blocked', status_note=note, claimed_by=None)})
        else:
            w.write_patches(s, {t['ref']: dict(status='blocked', status_note=note, claimed_by=None)})
        return dict(decision='owner', problem=prob['id'])
    text = (req.get('instructions') or '').strip()
    if len(text) < 30: raise ValueError('instructions: что делать роли дальше (не короче 30 символов)')
    prob.update(status='resolved', instructions=text[:2000])
    if not pipe:
        w.write_patches(s, {t['ref']: dict(status='queued', claimed_by=None, status_note='Решала (%s): %s' % (prob['id'], text[:300]))})
        return dict(decision=decision, problem=prob['id'])
    if decision == 'return':
        to_stage = req.get('to_stage')
        cur = pipe['stage'] if pipe['stage'] != 'done' else P.ORDER[-1]
        if to_stage not in P.ORDER[:P.ORDER.index(cur) + 1]: raise ValueError('to_stage: этап не позже текущего')
        roles = [r for r in (req.get('roles') or []) if r in P.STAGE[to_stage]['roles']]
        if not roles and hint.get('target') in P.STAGE[to_stage]['roles']:
            roles = [hint['target']]                       # по умолчанию — роль, к которой ведёт класс сбоя
        history(pipe, 'return', role='resolver', stage=pipe['stage'], to_stage=to_stage, agent=run.get('agent'), model=run.get('model'),
                run=run['id'], defects=[], roles=roles, reason=text[:500], at=at.isoformat())
        close_conflicts(pipe, prob, 'resolver', text, at)
        return_to(s, t, pipe, at, to_stage, roles, text, 'Решала')
        patch = orchestrate(s, t, pipe, at)
    else:
        journal(s, pipe, t['ref'], 'resolved', 'Решала (%s): %s' % (prob['id'], text[:400]), 'resolver')
        patch = _resume_role(s, t, pipe, prob, at, 'resolver', text)
    w.write_patches(s, {t['ref']: dict(patch, claimed_by=None)})
    return dict(decision=decision, problem=prob['id'], stage=pipe['stage'])


def op_answer(s, req, t, at):
    """Ответ владельца на вопрос Решалы (колокольчик). Работа продолжается с ответом в замечаниях роли."""
    prob = problem(s, req.get('problem'))
    if not prob or prob['ref'] != t['ref'] or prob['status'] != 'owner': raise ValueError('Вопрос не найден или уже отвечен')
    answer = (req.get('answer') or '').strip()
    if not answer and isinstance(req.get('option'), int) and 0 <= req['option'] < len(prob.get('options') or []):
        answer = 'Выбран путь: ' + prob['options'][req['option']]['title']
    if len(answer) < 2: raise ValueError('answer: ответ владельца')
    prob.update(status='answered', answer=answer[:2000], answered_at=at.isoformat())
    text = 'Вопрос: %s\nОтвет владельца: %s' % (prob.get('question') or '', answer)
    if not is_pipeline(t):
        w.write_patches(s, {t['ref']: dict(status='queued', claimed_by=None, status_note='Ответ владельца (%s): %s' % (prob['id'], answer[:300]))})
        return dict(answered=prob['id'])
    pipe = copy.deepcopy(t['pipeline'])
    journal(s, pipe, t['ref'], 'owner_answered', 'Владелец ответил (%s): %s' % (prob['id'], answer[:400]))
    w.write_patches(s, {t['ref']: dict(_resume_role(s, t, pipe, prob, at, 'owner', text), claimed_by=None)})
    return dict(answered=prob['id'], stage=pipe['stage'])


def return_to(s, t, pipe, at, to_stage, roles, reason, by):
    """Вернуть задачу на ранний этап: этапы с него и далее снова не сданы."""
    cur = pipe['stage'] if pipe['stage'] != 'done' else P.ORDER[-1]
    loop = '%s>%s' % (cur, to_stage); pipe.setdefault('loops', {})[loop] = pipe['loops'].get(loop, 0) + 1
    if P.ORDER.index(to_stage) <= P.ORDER.index('analysis'):
        pipe.pop('ac_freeze', None)            # требования пересматриваются — заморозка снимается вместе с ними
    if P.ORDER.index(to_stage) <= P.ORDER.index('review'):
        pipe.pop('dual_review', None)          # новый код — оба ревью заново
    for st in P.ORDER[P.ORDER.index(to_stage):]:
        pipe['stages'].pop(st, None)
        gate = P.STAGE[st].get('gate')
        if gate: pipe['gates'].pop(gate, None)
        if st == 'deploy_main': pipe.get('approvals', {}).pop('release', None)
        for r in P.STAGE[st]['roles']:
            k = key(st, r)
            if k in pipe['roles']:
                pipe['roles'][k].update(state='pending', feedback=[dict(id='return', text='Возврат от: ' + by, detail=reason)]
                                        if st == to_stage and (not roles or r in roles) else None)
    pipe['stage'] = to_stage; pipe.pop('blocked', None)
    journal(s, pipe, t['ref'], 'returned', '%s вернул(а) задачу на этап «%s»: %s' % (by, P.STAGE[to_stage]['title'], reason))
    if pipe['loops'][loop] >= MAX_LOOPS:
        escalate(s, t, pipe, at, 'Задача %d раза возвращалась с «%s» на «%s»: нужен разбор тимлида' % (
            pipe['loops'][loop], P.STAGE[cur]['title'], P.STAGE[to_stage]['title']))


def op_return(s, req, t, at):
    """Роль возвращает задачу назад: ревьюер и QA — разработчику, разработчик — в анализ…"""
    pipe = copy.deepcopy(t['pipeline']); run = _run(s, req, t)
    role = run['role']; to_stage = req.get('to_stage')
    if to_stage not in RETURN_TO.get(role, []) or P.ORDER.index(to_stage) >= P.ORDER.index(run['stage']):
        allowed = [s for s in RETURN_TO.get(role, []) if P.ORDER.index(s) < P.ORDER.index(run['stage'])]
        raise ValueError('Роль %s с этапа «%s» возвращает только на: %s' % (
            P.ROLES[role]['title'], P.STAGE[run['stage']]['title'], ', '.join(allowed) or 'нельзя'))
    reason = (req.get('reason') or '').strip()
    if len(reason) < 30: raise ValueError('reason: что не так и что исправить (не короче 30 символов)')
    _autocommit(s, t, pipe, run, 'возврат: ' + reason)
    if role in ('reviewer', 'qa'):
        spec = P.ROLES[role]['artifacts'][0 if role == 'reviewer' else 1]
        miss = P.check_document(ctx_for(t, pipe).artifact(spec), spec)
        if miss: raise ValueError('Возврат с замечаниями требует артефакта: ' + miss)
    roles = [r for r in (req.get('roles') or []) if r in P.STAGE[to_stage]['roles']]
    if req.get('defects') is not None and role not in DEFECT_ROLES:
        raise ValueError('Дефекты регистрируют ревьюер, QA и DevOps')
    defects = register_defects(pipe, run, req.get('defects'), ctx_for(t, pipe), at, required=role == 'qa') if role in DEFECT_ROLES else []
    history(pipe, 'return', role=role, stage=run['stage'], to_stage=to_stage, agent=run.get('agent'), model=run.get('model'),
            run=run['id'], defects=defects, roles=roles, reason=reason[:500], at=at.isoformat())
    first = (pipe.get('dual_review') or {}).get('first')
    if role == 'reviewer' and first and first.get('run') != run['id'] and first.get('verdict') == 'approve':
        disagreement(s, t, pipe, at, first, dict(run=run['id'], agent=run.get('agent'), model=run.get('model'),
                                                 verdict='return', reason=reason[:500], defects=defects))
    if defects:
        reason += ' · дефекты: ' + ', '.join(defects)
    run.update(status='done', finished_at=at.isoformat(), evidence='Возврат на «%s»: %s' % (P.STAGE[to_stage]['title'], reason))
    return_to(s, t, pipe, at, to_stage, roles, reason, P.ROLES[role]['title'])
    w.write_patches(s, {t['ref']: dict(orchestrate(s, t, pipe, at), claimed_by=None)})
    return dict(returned=to_stage, defects=defects)


def op_gate(s, req, t, at):
    """Решение ИИ-тимлида: ворота анализа, ворота релиза, эскалация."""
    pipe = copy.deepcopy(t['pipeline']); run = _run(s, req, t, role='teamlead')
    decision = req.get('decision'); notes = (req.get('notes') or '').strip(); gate = run.get('gate')
    if decision not in ('approve', 'return'): raise ValueError('decision: approve или return')
    if len(notes) < 30: raise ValueError('notes: основания решения (не короче 30 символов)')
    pending = open_disagreements(s, pipe) if gate == 'analysis' else []
    if pending and decision == 'approve':
        miss = [d['id'] for d in pending if d['id'] not in notes]
        if miss:
            raise ValueError('Ворота анализа: два анализа разошлись — в notes назовите решение по каждому: %s '
                             '(какая позиция принята и почему). Без решения — op=escalate к владельцу' % ', '.join(miss))
    for d in pending:
        d.update(resolution='return' if decision == 'return' else 'decided', resolved_at=at.isoformat(),
                 resolved_by=dict(role='teamlead', run=run['id'], agent=run.get('agent'), model=run.get('model')), decision=notes[:500])
    run.update(status='done', finished_at=at.isoformat(), evidence='Тимлид: %s — %s' % (decision, notes))
    if gate in ('analysis', 'release'):
        pipe['gates'][gate] = dict(decision=decision, notes=notes, at=at.isoformat(), by=run.get('model'))
    journal(s, pipe, t['ref'], 'gate', 'Тимлид (%s): %s — %s' % (gate, decision, notes), 'teamlead')
    history(pipe, 'gate', gate=gate, decision=decision, by='teamlead', agent=run.get('agent'), model=run.get('model'), run=run['id'],
            at=at.isoformat())                       # след решения ворот — для доли ошибочных ворот (AG-LEAD-10)
    if decision == 'return':
        to_stage = req.get('to_stage') or ('analysis' if gate == 'analysis' else 'development')
        if to_stage not in P.ORDER[:P.ORDER.index(pipe['stage'] if pipe['stage'] != 'done' else P.ORDER[-1]) + 1]:
            raise ValueError('to_stage: этап не позже текущего')
        history(pipe, 'return', role='teamlead', stage=pipe['stage'], to_stage=to_stage, agent=run.get('agent'),
                model=run.get('model'), run=run['id'], gate=gate, defects=[], roles=req.get('roles') or [], reason=notes[:500], at=at.isoformat())
        return_to(s, t, pipe, at, to_stage, req.get('roles') or [], notes, 'Тимлид')
    elif gate == 'escalation':
        pipe.pop('blocked', None)
    w.write_patches(s, {t['ref']: dict(orchestrate(s, t, pipe, at), claimed_by=None)})
    return dict(decision=decision, stage=pipe['stage'])


def op_approve_release(s, req, t, at):
    """«Да» владельца на деплой в main."""
    pipe = copy.deepcopy(t['pipeline'])
    if pipe['stage'] != 'deploy_main' or oa.verify((pipe.get('approvals') or {}).get('release')):
        raise ValueError('Одобрение нужно только задаче в «Деплой в main», ещё не одобренной')
    if (pipe['gates'].get('release') or {}).get('decision') != 'approve':
        raise ValueError('Сначала ворота тимлида «тест → main»')
    # Одобрение — конкретного коммита тестовой ветки: новый коммит после «да» требует нового «да».
    ctx = ctx_for(t, pipe)
    head = ci.head_sha(ctx.repo, 'refs/heads/' + ctx.branches['test']) if ctx.repo else ''
    pipe.setdefault('approvals', {})['release'] = rp.approval(t['ref'], head, at)
    journal(s, pipe, t['ref'], 'approved', 'Владелец одобрил релиз в main на %s' % head[:7])
    w.write_patches(s, {t['ref']: orchestrate(s, t, pipe, at)})
    return dict(approved=True)


def op_approve_oracle(s, req, t, at):
    """Владелец одобряет изменение эталона приёмки задачи — подпись на текущий дифф защищённых файлов (SEC-010).
    Новый коммит, меняющий эталон, требует нового одобрения: подпись привязана к содержимому изменения."""
    import trusted_oracle as to
    pipe = copy.deepcopy(t['pipeline'])
    ctx = ctx_for(t, pipe)
    files, mb = to.changed(ctx.repo, ctx.branches.get('base'), ctx.branch, ctx.project) if ctx.repo and ctx.branch else (None, 'нет ветки')
    if files is None:
        raise ValueError('Эталон не проверить: ' + str(mb))
    if not files:
        raise ValueError('Эталон приёмки в ветке задачи не изменён — одобрять нечего')
    digest = to.change_digest(ctx.repo, mb, ctx.branch, files)
    pipe.setdefault('approvals', {})['oracle'] = to.approve_change(t['ref'], digest, at)
    pipe.pop('oracle_pending', None)
    journal(s, pipe, t['ref'], 'approved', 'Владелец одобрил изменение эталона приёмки: ' + ', '.join(files[:5]))
    w.write_patches(s, {t['ref']: orchestrate(s, t, pipe, at)})
    return dict(approved=True, files=files)


def tick():
    """Периодически (сервер досок): задачи, ждавшие DoR, проверяются снова."""
    moved = []
    with w.locked():
        s = w.state(); at = w.now(); patches = {}
        if rp.killed(s):
            return moved                       # аварийная остановка: задачи не двигаются
        for t in w.snapshot()['tasks']:
            pipe = t.get('pipeline')
            if not pipe or pipe['stage'] == 'done' or pipe.get('blocked'): continue
            st = (pipe.get('stages') or {}).get(pipe['stage']) or {}
            if st.get('dor_ok'): continue
            p = copy.deepcopy(pipe); patch = orchestrate(s, t, p, at)
            if p['stage'] != pipe['stage'] or (p['stages'].get(p['stage']) or {}).get('dor_ok'):
                patches[t['ref']] = patch; moved.append(t['ref'])
        if patches:
            w.write_patches(s, patches); w.save('workflow.json', s)
    return moved


# ---------------------------------------------------------------- паспорт и задание роли

def resolver_packet(t, run, agent):
    s = w.state(); prob = problem(s, run.get('problem')) or {}
    pipe = t.get('pipeline') or {}
    others = [r for r in s['runs'] if r['ref'] == t['ref'] and r['id'] != run['id']][-6:]
    other_ai = [a for a in w.PROVIDERS if a != agent]
    return dict(ref=t['ref'], title=t['title'], why=t.get('why'), acceptance=t.get('acceptance'),
                role=dict(id='resolver', title=P.ROLES['resolver']['title'], skill='tt-resolver', mission=P.ROLES['resolver']['mission']),
                route=role_route(t, 'resolver', agent), phase='role',
                problem={k: prob.get(k) for k in ('id', 'role', 'agent', 'stage', 'reason', 'tried', 'need', 'source', 'at',
                                                   'evidence', 'route_hint')},
                rules=('Причина решения — факт (с доказательством из evidence) или гипотеза (с проверкой probe). '
                       'evidence.basis=UNKNOWN — нет ни конверта, ни журнала: не выдумывай причину, начни с проверки. '
                       'route_hint — куда класс сбоя ведёт по умолчанию.'),
                stage=pipe.get('stage'), worktree=pipe.get('worktree'),
                role_state=(pipe.get('roles') or {}).get(key(prob.get('stage') or '', prob.get('role') or '')),
                journal=(pipe.get('journal') or [])[-15:],
                recent_runs=[{k: r.get(k) for k in ('id', 'role', 'agent', 'status', 'evidence', 'launch_error')} for r in others],
                known=recipes.catalog(s),
                learn=('Если решение повторяемое, приложи к op=resolve recipe={title, keywords: [2–6 слов, по которым узнать проблему], '
                       'instructions} — в следующий раз агенту ответит сервер, без сессии Решалы.'),
                second_opinion=('py "%s" ask %s "<вопрос с контекстом>" — пути решения от второго ИИ' % (w.ROOT / 'tools' / 'pm.py', other_ai[0])) if other_ai else None,
                handoff=('tt_work op=resolve ref=%s run=%s worker=<ты> options=[{title, pros, cons}, …≥2] decision=continue|return|owner '
                         'notes=… instructions=… (continue/return) to_stage=… roles=[…] (return) question=… why_owner=… (owner)' % (t['ref'], run['id'])))


def resolver_prompt(t, run, route, worker=None):
    s = w.state(); prob = problem(s, run.get('problem')) or {}
    wt = (t.get('pipeline') or {}).get('worktree') or {}
    lines = ['Ты — Решала в TaskTreker: разбираешь проблему %s по задаче %s «%s».' % (prob.get('id', '?'), t['ref'], t['title']),
             'Проблема (%s, роль %s): %s' % ('агент' if prob.get('source') == 'agent' else 'сервер', prob.get('role') or '—', prob.get('reason', '')),
             'Следуй скиллу tt-resolver — он уже в системном промпте сессии (если нет — /tt-resolver). Модель: %s, усилия: %s.' % (route['model'], route['effort']),
             'Рабочая копия задачи: %s.' % (wt.get('path') or 'нет — смотри репозиторий проекта'),
             '1) tt_work op=claim ref=%s run=%s model=%s worker=%s' % (t['ref'], run['id'], route['model'], worker or '<id этой сессии>'),
             '2) tt_work op=packet ref=%s run=%s — проблема, журнал, последние запуски.' % (t['ref'], run['id']),
             '3) Найди не меньше двух путей решения (второе мнение — у другого ИИ, команда в паспорте), выбери лучший и пришли tt_work op=resolve.',
             'Владельца зови (decision=owner) только если без его прав, денег, доступов или продуктового решения дальше нельзя.',
             w.LIMIT_RULE % (t['ref'], run['id'], worker or '<id этой сессии>'),
             *([w.AUTONOMY_RULE] if w.autonomous() else []),
             'Работай только через MCP tt_work. Твой worker во всех вызовах: %s.' % (worker or '<id этой сессии>')]
    return '\n'.join(lines)


def test_env(t):
    """Где QA тестирует: развёрнутая тестовая ветка, её рабочая копия и последний прогон CI."""
    try:
        import ci, deploy
        p = project_of(t); repo = p.get('inspection_path') or p.get('path')
        b = P.branches(repo, p)
        path = deploy.branch_worktree(repo, b['test'], b['main'])
        run = ci.last(project=t['project'], scope='test', branch=b['test'])
        return dict(branch=b['test'], path=str(path), ci=ci.summary(run), ci_log=(run or {}).get('log'),
                    note='Тестируй эту рабочую копию: в ней развёрнута работа задачи. Отчёт и тест-кейсы коммить в ветку задачи.')
    except Exception as e:   # noqa: BLE001 — паспорт не должен падать из-за окружения
        return dict(error=str(e)[:200])


def upstream_digest(t, pipe, stage, toc=True, ctx=None):
    """Артефакты предыдущих ролей: путь, размер и оглавление — чтобы читать разделами, а не целиком (Б1)."""
    ctx = ctx or ctx_for(t, pipe); out = []
    have = None
    if toc and ctx.repo and ctx.branch:
        # Один список файлов ветки вместо git show на каждый ещё не созданный артефакт.
        ls = P._git(['ls-tree', '-r', '--name-only', ctx.branch, P.art_path(t['id'], P.ART).rstrip('/') + '/'], ctx.repo)
        have = set(ls.stdout.splitlines()) if ls.returncode == 0 else None
    ctx.task_index = P.index_path(t['id']) if have and P.index_path(t['id']) in have else None
    head = ci.head_sha(ctx.repo, 'refs/heads/' + ctx.branch) if toc and ctx.repo and ctx.branch else ''
    for st in P.ORDER[:P.ORDER.index(stage) + 1 if stage in P.ORDER else 0]:
        for r in P.stage_roles(st, pipe.get('needs')):
            for a in P.ROLES[r]['artifacts']:
                path = P.art_path(t['id'], a['path'])
                done = (pipe.get('roles') or {}).get(key(st, r)) or {}
                # Происхождение (A4-01): чей, на каком коммите, когда сдан, доверие — сдан по DoD или черновик.
                e = dict(role=P.ROLES[r]['title'], path=path, optional=bool(a.get('optional')), source_id='%s:%s' % (t['ref'], path),
                         project=t.get('project'), commit=head[:12] or None, timestamp=done.get('at'),
                         trust_level='accepted' if done.get('state') == 'done' else 'draft')
                text = None if a.get('dir') or not toc or (have is not None and path not in have) else ctx.artifact(a)
                if text is not None:
                    e['tokens'] = P.tokens(text)
                    if not ctx.task_index:                  # есть INDEX.md — указатель там, не дублируем в паспорте
                        e['index'] = P.index_lines(P.doc_index(text), 15, max_level=2)
                elif toc and not a.get('dir'):
                    e['missing'] = True
                out.append(e)
    return out


def project_docs(ctx, limit=12):
    """Крупные документы проекта в ветке задачи (ТЗ, спецификация): путь и вес по размеру файла, без чтения.
    Читаются указателем op=doc — их целиком перечитывали десятки раз за неделю (Л2)."""
    if not (ctx.repo and ctx.branch):
        return []
    ls = P._git(['ls-tree', '-l', ctx.branch, 'docs/'], ctx.repo)
    out = []
    for line in ls.stdout.splitlines() if ls.returncode == 0 else []:
        meta, _, path = line.partition('\t')
        parts = meta.split()
        if len(parts) == 4 and parts[1] == 'blob' and path.endswith('.md') and parts[3].isdigit() and int(parts[3]) >= 4000:
            out.append((int(parts[3]), path))
    return ['%s ~%dт' % (path, size / 4.5) for size, path in sorted(out, reverse=True)[:limit]]


def op_doc(s, req, t, at):
    """Раздел документа ветки задачи: оглавление или текст одного раздела (Б1)."""
    pipe = t.get('pipeline') or {}
    path = str(req.get('path') or '').replace('\\', '/').lstrip('/')
    if not path and req.get('code') and P.CODE.fullmatch(str(req['code']).strip().upper()):
        path = P.source_path(t['id'], str(req['code']).strip().upper())   # код без пути — в своём документе
    if not path or '..' in path.split('/'):
        raise ValueError('path: путь документа в рабочей копии задачи, например docs/tasks/<id>/SRS.md')
    text = ctx_for(t, pipe).read(path)
    if text is None:
        raise ValueError('Нет файла %s в ветке задачи' % path)
    if str(req.get('full') or '').lower() in ('1', 'true', 'yes', 'да'):
        return full_read(t, req, path, text)
    want = str(req.get('code') or req.get('section') or '').strip()
    if not want:
        return dict(path=path, tokens=P.tokens(text), index=P.index_lines(P.doc_index(text), 80),
                    hint='section=<номер из указателя или часть заголовка> или code=<FR-3> — один раздел')
    entry, body = P.section_by(text, want)
    if entry is None:
        raise ValueError('Раздела «%s» нет. Есть: %s' % (want, '; '.join(body)[:600]))
    limit = 24000
    return dict(path=path, section='%d %s' % (entry['n'], entry['title']), lines=entry['lines'], tokens=P.tokens(body),
                text=body[:limit], truncated=len(body) > limit)


FULL_LIMIT = 400_000     # символов: больше одного документа целиком сервер не отдаёт


def full_read(t, req, path, text):
    """Документ целиком по обоснованию (TT-96): роль пишет, зачем ей весь документ. Вход её этапа и документ
    до порога сложности — без счёта; остальное — из бюджета обоснованных чтений запуска. Каждое такое чтение
    с причиной и весом — в журнал тимлида: видно, кто и зачем читал целиком."""
    why = str(req.get('why') or '').strip()
    if len(why) < 20:
        raise ValueError('why: зачем нужен весь документ, а не раздел (не короче 20 символов) — '
                         'например «сверяю согласованность всех FR с HLD перед ревью»')
    size = P.tokens(text)
    with w.locked():
        s = w.state()
        runs = [r for r in s['runs'] if r['ref'] == t['ref'] and r.get('role') and r['status'] in ('queued', 'running')]
        run = next((r for r in runs if r['id'] == req.get('run')), runs[0] if len(runs) == 1 else None)
        if not run:
            raise ValueError('run: укажи свой запуск (%s)' % (', '.join(r['id'] + ' ' + r['role'] for r in runs) or 'нет активных'))
        pol = P.read_policy(t, run['role'], w.cx.assess(t)['tier'])
        free = size <= pol['free'] or path in pol['primary'] or path in [P.art_path(t['id'], a['path']) for a in P.ROLES[run['role']]['artifacts']]
        used = sum(x['tokens'] for x in run.get('full_reads') or [] if not x.get('free'))
        if not free and used + size > pol['budget']:
            raise ValueError('Бюджет полных чтений запуска: прочитано ~%d тыс. из %d тыс. токенов (класс сложности %d), %s — ещё ~%d тыс. '
                             'Читай разделами (section=|code=). Если без документа целиком задача не решается — tt_work op=escalate.'
                             % (used // 1000, pol['budget'] // 1000, pol['tier'], path, size // 1000))
        run.setdefault('full_reads', []).append(dict(path=path, tokens=size, why=why[:300], free=free, at=w.now().isoformat(timespec='seconds')))
        tt = w.task_for(t['ref'])
        if tt.get('pipeline'):
            import copy
            pp = copy.deepcopy(tt['pipeline'])
            journal(s, pp, t['ref'], 'read_full', '%s читает целиком %s (~%d тыс. токенов%s): %s' % (
                run['role'], path, size // 1000, '' if free else ', бюджет %d/%d тыс.' % ((used + size) // 1000, pol['budget'] // 1000), why[:200]),
                run['role'], run.get('stage'))
            w.write_patches(s, {t['ref']: dict(pipeline=pp)})
        w.save('workflow.json', s)
    return dict(path=path, full=True, tokens=size, budget_left=None if free else (pol['budget'] - used - size) // 1000 * 1000,
                text=text[:FULL_LIMIT], truncated=len(text) > FULL_LIMIT)


def role_packet(t, run, agent, toc=True):
    """Паспорт роли. toc=False — без оглавления артефактов (git), для claim под блокировкой."""
    if run.get('role') == 'resolver':
        return resolver_packet(t, run, agent)
    pipe = t['pipeline']; role, stage = run['role'], run['stage']; spec = P.ROLES[role]
    route = run_route(t, run, agent)
    if role == 'product_owner':
        route['research'] = 'веб-поиск — только для продуктовой задачи; техническая: needs.research=false, po-market — «неприменимо»'
    wt = pipe.get('worktree') or {}
    entry = pipe['roles'].get(key(stage, role)) or {}
    ctx = ctx_for(t, pipe)
    arts = upstream_digest(t, pipe, stage, toc, ctx)
    dod = [] if role == 'teamlead' else P.role_dod(role, stage)
    packet = dict(ref=t['ref'], title=t['title'], why=t.get('why'), root=t.get('root'), acceptance=t.get('acceptance'),
                  role=dict(id=role, title=spec['title'], skill='tt-' + role.replace('_', '-'), mission=spec['mission']),
                  stage=dict(id=stage, title=P.STAGE[stage]['title'] if stage in P.STAGE else stage),
                  route=route, phase='role', gate=run.get('gate'), task_route=_route_brief(pipe),
                  knowledge=_knowledge(t, role),
                  workspace=dict(path=wt.get('path'), branch=wt.get('branch'), base=wt.get('base'),
                                 note='Работай только в этой рабочей копии; коммить свои файлы в ветку задачи.'),
                  artifacts=[dict(path=P.art_path(t['id'], a['path']), sections=a['sections'], min_bytes=a['min_bytes'],
                                  optional=bool(a.get('optional'))) for a in [P.effective(x, ctx) for x in P.stage_artifacts(role, stage)]],
                  dod=[dict(id=i['id'], text=i['text'], kind=i['kind'], allow_na=bool(i.get('allow_na'))) for i in dod],
                  upstream=arts, task_index=getattr(ctx, 'task_index', None), project_docs=project_docs(ctx) if toc else None,
                  needs=pipe.get('needs'), feedback=entry.get('feedback'), duplicates=pipe.get('duplicates') if role == 'product_owner' else None,
                  provenance=dict(source='server', ref=t['ref'], project=t.get('project'), branch=wt.get('branch'),
                                  policy_version=w.POLICY['version'], schema=SCHEMA_VERSION, built_at=w.now().isoformat()),
                  resume=((run.get('pauses') or [{}])[-1].get('checkpoint') or None),
                  defects=[{k: d.get(k) for k in ('id', 'title', 'severity', 'level', 'test', 'found_by', 'status')}
                           for d in pipe.get('defects') or [] if d['status'] in ('open', 'fixed')],
                  schema=dict(version=SCHEMA_VERSION, required=['summary', 'items'],
                              optional=['schema_version', 'needs', 'acceptance', 'verdict', 'signoff', 'ac_matrix', 'reviewed_sha',
                                        'contract_group', 'knowledge_applied', 'fixes', 'comparison'] + list(UNCERTAIN),
                              uncertain='assumptions — допущения, questions — вопросы, conflicts — противоречия в требованиях, '
                                        'unverified — что не проверено. Вопрос или противоречие останавливает этап до решения; '
                                        'с непроверенным нельзя approve и signoff.'),
                  upstream_uncertain=upstream_uncertain(pipe),
                  conflicts=[c for c in pipe.get('conflicts') or [] if c['status'] == 'open'],
                  pause='tt_work op=pause ref=%s run=%s worker=<ты> checkpoint=… — при исчерпании лимита токенов' % (t['ref'], run['id']),
                  handoff='tt_work op=handoff ref=%s run=%s worker=<ты> summary=… items={id: {status: done|na, evidence}}' % (t['ref'], run['id']))
    if role == 'product_owner':
        packet['handoff'] += (' acceptance=[бизнес-критерии] needs={design: bool (только если меняется интерфейс), marketing: bool,'
                              ' research: bool (false — техническая задача, без веб-поиска), reason}')
    if role == 'reviewer':
        packet['handoff'] += (' verdict=approve reviewed_sha=<голова ветки, которую ты читал> ac_matrix={"AC-1": {"files": ["путь[:строка]"],'
                              ' "tests": ["тест"], "verdict": "ok|gap|defect"}, …} — по каждому критерию и каждому изменённому файлу кода;'
                              ' замечания — op=return to_stage=development defects=[{title, severity, level, kind: code_defect|test_gap,'
                              ' test|file|repro}]')
    if role == 'product_owner':
        packet['handoff'] += (' contract_group=<имя группы, если задача делит API или модель данных с другими задачами проекта:'
                              ' их анализ пойдёт по очереди>')
    if role == 'system_analyst' and (run.get('dual') or {}).get('second_of'):
        alt = P.art_path(t['id'], P.ART + '/' + ALT_ANALYSIS)
        packet['dual_analysis'] = dict(
            first_run=run['dual']['second_of'], document=alt,
            task='Ты второй, независимый аналитик задачи CRITICAL (07: E2E-R05). Сначала составь свой анализ по BRD — требования, '
                 'решения, риски — в %s, не опираясь на артефакты первого. Только потом сравни с его SRS и перечисли каждое '
                 'расхождение. Артефакты первого не меняй: обе версии сохраняются, решение по расхождениям — на воротах.' % alt)
        packet['handoff'] += (' comparison={"agree": false, "differences": [{"topic": "…", "first": "позиция первого", "second": "твоя"}]}'
                              ' (или {"agree": true, "differences": []}); правки — только %s' % alt)
    if role == 'teamlead' and run.get('gate') == 'analysis':
        dis = [dict(id=d['id'], topic=d.get('topic'), first=(d.get('first') or {}).get('position'),
                    second=(d.get('second') or {}).get('position')) for d in open_disagreements(w.state(), pipe)]
        if dis:
            packet['disagreements'] = dis
            packet['handoff'] = (packet.get('handoff') or '') + (' — два анализа разошлись: в notes назови решение по каждому %s '
                                                                  '(какая позиция принята и почему) или op=escalate к владельцу'
                                                                  % ', '.join(d['id'] for d in dis))
    if role == 'qa' and stage == 'analysis':
        packet['handoff'] += (' — ранний тест-дизайн: test-design.md с разделами Позитивные/Негативные/Граничные случаи и '
                              'ожидаемым результатом по каждому критерию приёмки (AC-1…AC-n); код ещё не написан, не угадывай реализацию')
    elif role == 'qa':
        packet['handoff'] += ' signoff=true; дефекты — op=return to_stage=development defects=[{title, severity, level, test}]'
        packet['test_env'] = test_env(t)
    if role == 'developer' and any(d['status'] == 'open' for d in pipe.get('defects') or []):
        packet['handoff'] += (' fixes={"D-n": {"method": "spec_change|code_to_spec|workaround|test_fix", "test_level": "…", "note": "…"}}'
                              ' — по каждому открытому дефекту; способ сервер сверяет с коммитами ветки')
    if role == 'teamlead':
        packet['gate_task'] = dict(
            analysis='Проверь, что SRS, HLD, ADR, модель угроз и (если нужны) дизайн и GTM согласованы между собой и с BRD, '
                     'а разработка пройдёт без двояких трактовок.',
            release='Проверь отчёт QA, деплой в тест и риски релиза; реши, готово ли к main.',
            escalation='Прежняя эскалация тимлиду (до Решалы): разбери блокер, верни задачу или разблокируй.'
        ).get(run.get('gate'))
        packet['journal'] = pipe.get('journal', [])[-15:]
        if run.get('gate') in ('analysis', 'release'):
            packet['digest'] = gate_digest(t, pipe, run['gate'])
        packet['handoff'] = 'tt_work op=gate ref=%s run=%s worker=<ты> decision=approve|return notes=… [to_stage, roles]' % (t['ref'], run['id'])
    else:
        packet['escalate'] = ('tt_work op=escalate ref=%s run=%s worker=<ты> reason=… tried=… need=… — проблема или ограничение: '
                              'разберёт агент «Решала»' % (t['ref'], run['id']))
        if role in RETURN_TO:
            packet['return'] = 'tt_work op=return ref=%s run=%s worker=<ты> to_stage=%s reason=…' % (t['ref'], run['id'], '|'.join(RETURN_TO[role]))
    return packet


def _knowledge(t, role):
    """Проверенные карточки опыта для разработчика (LEARN-06): только verified и действующие, узко и в лимите."""
    if role != 'developer':
        return None
    try:
        import learning_knowledge as lk
        return lk.retrieve(t) or None
    except Exception:              # сбой памяти опыта не мешает работе (§15.5)
        return None


def _route_brief(pipe):
    """Маршрут задачи для роли: режим, действует ли, пропущенные этапы и нужные проверки."""
    import adaptive_pipeline as ap
    r = ap.route(pipe); plan = r.get('plan')
    if not plan:
        return None
    out = dict(mode=plan['mode'], applied=bool(r.get('applied')), reasons=plan['risk_reasons'][:3],
               required_checks=plan['required_checks'], evidence=plan['evidence_requirements'])
    if r.get('applied') == 'FAST':
        out['note'] = ('Маршрут FAST: «Идея» и «Анализ» не проводились, BRD/SRS/HLD нет — требования берутся из why и '
                       'acceptance задачи. Если работа выходит за scope или задевает безопасность, API, данные, CI — '
                       'маршрут поднимется сам по диффу.')
    return out


def read_rule(t, route):
    """Правило чтения в задании роли (TT-94): одно для Claude и Codex. У Codex нет сторожа чтений и напоминания
    о потолке контекста — правило в самом задании, дальше эстафету по потолку делает сервер."""
    rule = ('Документы задачи: сначала %s/INDEX.md (если есть) — указатель документов и реестр требований '
            'с местом определения; дальше читай только нужный раздел (Read с offset/limit или tt_work op=doc section=|code=), '
            'не целиком и не повторно.' % P.art_path(t['id'], P.ART).rstrip('/'))
    if (route.get('provider') or route.get('agent')) == 'codex':
        rule += (' Контекст сессии перечитывается каждым ходом: вырос за ~%d тыс. токенов, а этап не сдан — закоммить и '
                 'tt_work op=pause reason=context checkpoint=… — сервер откроет свежую сессию.' % (w.settings()['context_ceiling'] // 1000))
    return rule


def role_prompt(t, run, route, worker=None):
    if run.get('role') == 'resolver':
        return resolver_prompt(t, run, route, worker)
    role = run['role']; spec = P.ROLES[role]; pipe = t['pipeline']; wt = pipe.get('worktree') or {}
    skill = 'tt-' + role.replace('_', '-')
    lines = ['Ты — %s в TaskTreker. Задача %s «%s», этап «%s».' % (spec['title'], t['ref'], t['title'],
             P.STAGE[run['stage']]['title'] if run.get('stage') in P.STAGE else run.get('stage')),
             'Следуй скиллу %s — он уже в системном промпте сессии (если нет — /%s): там твой скоуп, DoR, DoD и артефакты — '
             'ровно то, что проверяет сервер.' % (skill, skill),
             'Рабочая копия: %s, ветка %s. Коммить только свои файлы; ветку не сливай — слияния делает сервер.' % (wt.get('path') or '—', wt.get('branch') or '—'),
             'Модель: %s, усилия: %s.' % (route['model'], route['effort'])]
    if run.get('gate'):
        lines.append('Это ворота тимлида: %s.' % run['gate'])
    lines += ['1) tt_work op=claim ref=%s run=%s model=%s worker=%s' % (t['ref'], run['id'], route['model'], worker or '<id этой сессии>'),
              '2) tt_work op=packet ref=%s run=%s — паспорт роли (один раз): DoD, артефакты, указатели документов, замечания.' % (t['ref'], run['id']),
              '3) Сделай работу, закоммить артефакты, затем %s — это уведомление тимлиду. Не принятая передача вернёт список несделанных пунктов.'
              % ('op=gate' if role == 'teamlead' else 'op=handoff'),
              COMMIT_RULE,
              read_rule(t, route),
              'Проблема, ограничение, нет доступа, противоречие — не жди и не спрашивай владельца: tt_work op=escalate ref=%s run=%s '
              'reason=… tried=… need=… — её разберёт агент «Решала».' % (t['ref'], run['id']),
              w.LIMIT_RULE % (t['ref'], run['id'], worker or '<id этой сессии>'),
              *([w.AUTONOMY_RULE] if w.autonomous() else []),
              *([run['failover']['handover']] if (run.get('failover') or {}).get('handover') else []),
              'Работай только через MCP tt_work. Твой worker во всех вызовах: %s.' % (worker or '<id этой сессии>')]
    return '\n'.join(lines)
