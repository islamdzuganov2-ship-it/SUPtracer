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
import time

import filekinds as fk
import pipeline as P
import workflow as w

MAX_ATTEMPTS = 3          # провалов DoD у роли подряд — дальше эскалация тимлиду
MAX_LOOPS = 3             # возвратов между одной парой этапов — дальше эскалация
RETURN_TO = {'reviewer': ['development'], 'qa': ['development'], 'devops': ['development', 'testing'],
             'developer': ['analysis'], 'system_analyst': ['idea'], 'solution_architect': ['idea'],
             'designer': ['idea'], 'marketer': ['idea']}


# Реестр дефектов: кто нашёл, на каком уровне теста, чем исправлено. Способ исправления
# проверяется по коммитам ветки задачи после возврата, а не по словам исполнителя.
SEVERITIES = ('critical', 'major', 'minor')
FIX_METHODS = dict(spec_change='изменение ТЗ', code_to_spec='приведение кода к ТЗ',
                   workaround='обходная мера', test_fix='поправка теста')
DEFECT_ROLES = ('reviewer', 'qa', 'devops')


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
    head = branch_head(ctx); out = []
    reg = pipe.setdefault('defects', [])
    for d in raw:
        e = dict(id='D-%d' % (len(reg) + 1), title=d['title'].strip()[:300], severity=d['severity'], level=d['level'],
                 test=str(d.get('test') or '')[:100], found_by=run['role'], found_stage=run['stage'], found_agent=run.get('agent'),
                 found_model=run.get('model'), at=at.isoformat(), status='open', base=head)
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
    """ИИ роли: первый в порядке роли, у которого не исчерпан лимит токенов. Все на лимите — первый
    (сессия встанет на паузу до сброса)."""
    order = [a for a in role_order(s, t, role) if a not in exclude] or role_order(s, t, role)
    free = [a for a in order if not w.limited(s, a)]
    return (free or order)[0]


def role_route(t, role, agent, gate=None):
    """Модель и усилия роли: класс и усилия из таблицы роли по сложности задачи, не ниже min_tier роли."""
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
                policy_version=w.POLICY['version'])


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


def create_role_run(s, t, pipe, stage, role, at, feedback=None, gate=None):
    if active_run(s, t['ref'], role=role, gate=gate):
        return None
    t = dict(t, pipeline=pipe)          # ревьюер выбирается по разработчику этого конвейера
    agent = role_agent(s, t, role)
    run = w.new_run(s, t['ref'], None, at)
    route = role_route(t, role, agent)
    run.update(role=role, stage=stage, phase='role', agent=agent, gate=gate,
               auto_launch=w.settings(s).get('auto_pipeline', True) and (not (stage == 'idea' and pipe.get('pause_auto'))
                                                                        or t['project'] in s.get('active_projects', [])),
               packet=dict(route=route, phase='role'))
    if role not in ('teamlead', 'resolver'):
        k = key(stage, role); prev = pipe['roles'].get(k) or {}
        pipe['roles'][k] = dict(state='queued', run=run['id'], attempts=prev.get('attempts', 0) + (1 if feedback else 0),
                                feedback=feedback, agent=agent)
    journal(s, pipe, t['ref'], 'run', '%s: запуск %s поставлен для %s (%s · %s)' % (
        P.ROLES[role]['title'], run['id'], w.PROVIDERS[agent]['name'], route['model'], route['effort']), role, stage)
    return run


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
    for _ in range(len(P.ORDER) + 1):
        stage = pipe['stage']
        if stage == 'done' or pipe.get('blocked'):
            break
        st = pipe['stages'].setdefault(stage, {})
        if not st.get('dor_ok'):
            if stage != 'idea':      # идее рабочая копия не нужна: её создаёт запуск роли (materialize)
                ensure_worktree(t, pipe)
            dor = P.check_dor(stage, ctx_for(t, pipe), pipe)
            st['dor'] = [dict(id=it['id'], ok=ok, detail=d) for it, ok, d in dor]
            fails = [d for it, ok, d in dor if not ok]
            waiting_owner = bool(fails) and all(it['check'] == 'owner_release' for it, ok, _ in dor if not ok)
            if waiting_owner and w.autonomous(s):
                # Полная автономия: согласие владельца на релиз дано заранее, один раз.
                ch = w.settings(s).get('autonomy_changed') or {}
                pipe.setdefault('approvals', {})['release'] = dict(at=at.isoformat(), by='владелец заранее: полная автономия', consent_at=ch.get('at'))
                journal(s, pipe, t['ref'], 'approved', 'Релиз в main одобрен по согласию владельца на полную автономию (%s)' % (ch.get('at') or '')[:16])
                dor = P.check_dor(stage, ctx_for(t, pipe), pipe)
                st['dor'] = [dict(id=it['id'], ok=ok, detail=d) for it, ok, d in dor]
                fails = [d for it, ok, d in dor if not ok]
            if fails:
                waiting_owner = all(it['check'] == 'owner_release' for it, ok, _ in dor if not ok)
                note = ('Ждёт одобрения релиза владельцем' if waiting_owner else
                        'DoR этапа «%s» не выполнен: %s' % (P.STAGE[stage]['title'], '; '.join(fails)))
                if st.get('dor_note') != note:
                    journal(s, pipe, t['ref'], 'dor_wait' if waiting_owner else 'dor_failed', note, stage=stage)
                    st['dor_note'] = note
                break
            st.update(dor_ok=True, started=at.isoformat(), dor_note=None)
            journal(s, pipe, t['ref'], 'stage_started', 'Этап «%s» начат: DoR выполнен' % P.STAGE[stage]['title'], stage=stage)
        roles = P.stage_roles(stage, pipe.get('needs'))
        for r in roles:
            if (pipe['roles'].get(key(stage, r)) or {}).get('state') in (None, 'pending'):
                create_role_run(s, t, pipe, stage, r, at, feedback=(pipe['roles'].get(key(stage, r)) or {}).get('feedback'))
        states = {r: (pipe['roles'].get(key(stage, r)) or {}).get('state') for r in roles}
        if any(v != 'done' for v in states.values()):
            busy = [P.ROLES[r]['title'] for r, v in states.items() if v != 'done']
            note = 'Этап «%s»: работают %s' % (P.STAGE[stage]['title'], ', '.join(busy))
            break
        gate = P.STAGE[stage].get('gate')
        if gate:
            g = pipe['gates'].get(gate)
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
    task = w.create_roots(project, [task])[0]
    d = w.read('projects.json', {}); d['projects'][project].setdefault('backlog', []).append(task); w.save('projects.json', d)
    ref = project + '/' + task['id']
    pipe = new_pipe(source)
    journal(s, pipe, ref, 'idea', 'Новая идея (%s): %s' % (source, title), stage='idea')
    t = dict(task, project=project, ref=ref)
    w.write_patches(s, {ref: orchestrate(s, t, pipe, at)})
    return dict(ref=ref, id=task['id'], root=task['root'])


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
    handoff = {k: req.get(k) for k in ('items', 'summary', 'needs', 'acceptance', 'verdict', 'signoff') if req.get(k) is not None}
    if not isinstance(handoff.get('items') or {}, dict): raise ValueError('items: {id пункта: {status: done|na, evidence}}')
    if not isinstance(req.get('summary'), str) or len(req['summary'].strip()) < 20:
        raise ValueError('summary: что сделано — для тимлида (не короче 20 символов)')
    patch = {}
    if role == 'product_owner':
        acc = handoff.get('acceptance')
        if isinstance(acc, list) and all(isinstance(a, str) and a.strip() for a in acc):
            patch['acceptance'] = [a.strip() for a in acc]
        if isinstance(handoff.get('needs'), dict): pipe['needs'] = handoff['needs']
    _autocommit(s, t, pipe, run, req['summary'].strip())
    ctx = ctx_for(dict(t, **patch), pipe, handoff)
    results = P.evaluate_role(role, stage, ctx)
    fails = P.failures(results)
    fixed = {}
    if role == 'developer':
        defect_fails, fixed = check_fixes(t, pipe, ctx, req.get('fixes'))
        fails = fails + defect_fails
    k = key(stage, role); entry = pipe['roles'].setdefault(k, {})
    entry['result'] = [dict(id=it['id'], ok=ok, detail=d) for it, ok, d in results]
    history(pipe, 'handoff', role=role, stage=stage, agent=run.get('agent'), model=run.get('model'), run=run['id'],
            accepted=not fails, failed=[f['id'] for f in fails], at=at.isoformat())
    if fails:
        journal(s, pipe, t['ref'], 'handoff_rejected', '%s: передача не принята — %s' % (
            P.ROLES[role]['title'], '; '.join('%s (%s)' % (f['id'], f['detail']) for f in fails)), role, stage)
        w.write_patches(s, {t['ref']: dict(patch, pipeline=pipe)})
        return dict(accepted=False, failed=fails, hint='Исправь пункты и пришли handoff снова — запуск остаётся твоим.')
    for d in pipe.get('defects') or []:
        if d['id'] in fixed:
            d.update(status='fixed', fix=dict(fixed[d['id']], at=at.isoformat(), agent=run.get('agent'), model=run.get('model')))
        elif role == 'qa' and handoff.get('signoff') and d['status'] == 'fixed':
            d.update(status='verified', verified_at=at.isoformat(), verified_by=run.get('agent'))
    heavy = P.pending_heavy(results)
    run.update(status='done', finished_at=at.isoformat(), evidence=req['summary'].strip(), tokens=req.get('tokens'),
               handoff=handoff)
    entry.update(state='checking' if heavy else 'done', handoff=handoff, at=at.isoformat(), feedback=None)
    journal(s, pipe, t['ref'], 'handoff', '%s сдал(а) этап: %s%s' % (P.ROLES[role]['title'], req['summary'].strip()[:300],
            ' · фоновые проверки: ' + ', '.join(heavy) if heavy else ''), role, stage)
    if heavy:
        w.write_patches(s, {t['ref']: dict(patch, pipeline=pipe, claimed_by=None, status='in_progress',
                                           status_note='%s: идут проверки сервера (%s)' % (P.ROLES[role]['title'], ', '.join(heavy)))})
        return dict(accepted=True, checking=heavy, _pipejob=(t['ref'], role, stage, run['id']))
    w.write_patches(s, {t['ref']: dict(orchestrate(s, dict(t, **patch), pipe, at), **patch, claimed_by=None)})
    return dict(accepted=True, stage=pipe['stage'])


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
        if fails:
            entry['state'] = 'returned'
            text = '; '.join('%s (%s)' % (f['id'], f['detail']) for f in fails)
            journal(s, pipe, ref, 'check_failed', '%s: проверки сервера не прошли — %s' % (P.ROLES[role]['title'], text), role, stage)
            if entry.get('attempts', 0) + 1 >= MAX_ATTEMPTS:
                escalate(s, t, pipe, at, '%s: DoD не сдан %d раза подряд — %s' % (P.ROLES[role]['title'], MAX_ATTEMPTS, text), role)
            else:
                create_role_run(s, t, pipe, stage, role, at, feedback=fails)
            patch = dict(pipeline=pipe, status=status_of(s, ref, pipe), status_note='Возврат роли %s: %s' % (P.ROLES[role]['title'], text[:300]))
        else:
            entry['state'] = 'done'
            journal(s, pipe, ref, 'check_passed', '%s: проверки сервера прошли' % P.ROLES[role]['title'], role, stage)
            patch = orchestrate(s, t, pipe, at)
        w.write_patches(s, {ref: patch}); w.save('workflow.json', s)
    return dict(ok=not fails, failed=fails)


def open_problem(s, t, at, reason, role=None, agent=None, stage=None, source='server', tried=None, need=None):
    """Проблема в реестр: её разбирает Решала, а если нужен владелец — она в колокольчике."""
    reg = s.setdefault('problems', [])
    p = dict(id='P-%d' % (len(reg) + 1), ref=t['ref'], title=t.get('title', ''), role=role, agent=agent, stage=stage,
             reason=reason[:1500], tried=(tried or '')[:1500], need=(need or '')[:1000], source=source,
             at=at.isoformat(), status='open')
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
    prob = open_problem(s, t, at, reason, role, agent, pipe['stage'], source, tried, need)
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
    _autocommit(s, t, pipe, run, 'WIP перед эскалацией: ' + reason)
    run.update(status='done', finished_at=at.isoformat(), evidence='Эскалация Решале: ' + reason)
    if run['role'] != 'teamlead':
        pipe['roles'].setdefault(key(run['stage'], run['role']), {}).update(state='pending', feedback=None)
    prob = escalate(s, t, pipe, at, '%s: %s' % (P.ROLES[run['role']]['title'], reason), run['role'], run.get('agent'), 'agent', tried, need)
    w.write_patches(s, {t['ref']: dict(pipeline=pipe, status='blocked', status_note='Проблема %s у Решалы: %s' % (prob['id'], reason[:300]), claimed_by=None)})
    return dict(escalated=True, problem=prob['id'])


DECISIONS = dict(continue_='продолжить с указаниями', return_='вернуть на этап', owner='нужен владелец')


def _resume_role(s, t, pipe, prob, at, who, text):
    """Снять блок и вернуть работу роли, у которой была проблема, с указаниями."""
    pipe.pop('blocked', None)
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
    if not isinstance(opts, list) or len(opts) < 2 or any(not isinstance(o, dict) or len(str(o.get('title') or '').strip()) < 10 for o in opts):
        raise ValueError('options: не меньше двух путей решения {title ≥10 символов, pros, cons}')
    decision = req.get('decision'); notes = (req.get('notes') or '').strip()
    if decision not in ('continue', 'return', 'owner'): raise ValueError('decision: continue (продолжить), return (вернуть на этап) или owner (нужен владелец)')
    if len(notes) < 30: raise ValueError('notes: почему выбран этот путь (не короче 30 символов)')
    opts = [dict(title=str(o['title']).strip()[:300], pros=str(o.get('pros') or '')[:500], cons=str(o.get('cons') or '')[:500]) for o in opts[:6]]
    prob.update(options=opts, decision=decision, notes=notes[:2000], resolved_by=dict(agent=run.get('agent'), model=run.get('model')), decided_at=at.isoformat())
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
        history(pipe, 'return', role='resolver', stage=pipe['stage'], to_stage=to_stage, agent=run.get('agent'), model=run.get('model'),
                run=run['id'], defects=[], roles=roles, reason=text[:500], at=at.isoformat())
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
    if to_stage not in RETURN_TO.get(role, []):
        raise ValueError('Роль %s возвращает только на: %s' % (P.ROLES[role]['title'], ', '.join(RETURN_TO.get(role, [])) or 'нельзя'))
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
    run.update(status='done', finished_at=at.isoformat(), evidence='Тимлид: %s — %s' % (decision, notes))
    if gate in ('analysis', 'release'):
        pipe['gates'][gate] = dict(decision=decision, notes=notes, at=at.isoformat(), by=run.get('model'))
    journal(s, pipe, t['ref'], 'gate', 'Тимлид (%s): %s — %s' % (gate, decision, notes), 'teamlead')
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
    if pipe['stage'] != 'deploy_main' or (pipe.get('approvals') or {}).get('release'):
        raise ValueError('Одобрение нужно только задаче в «Деплой в main», ещё не одобренной')
    if (pipe['gates'].get('release') or {}).get('decision') != 'approve':
        raise ValueError('Сначала ворота тимлида «тест → main»')
    pipe.setdefault('approvals', {})['release'] = dict(at=at.isoformat(), by='владелец')
    journal(s, pipe, t['ref'], 'approved', 'Владелец одобрил релиз в main')
    w.write_patches(s, {t['ref']: orchestrate(s, t, pipe, at)})
    return dict(approved=True)


def tick():
    """Периодически (сервер досок): задачи, ждавшие DoR, проверяются снова."""
    moved = []
    with w.locked():
        s = w.state(); at = w.now(); patches = {}
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
                problem={k: prob.get(k) for k in ('id', 'role', 'agent', 'stage', 'reason', 'tried', 'need', 'source', 'at')},
                stage=pipe.get('stage'), worktree=pipe.get('worktree'),
                role_state=(pipe.get('roles') or {}).get(key(prob.get('stage') or '', prob.get('role') or '')),
                journal=(pipe.get('journal') or [])[-15:],
                recent_runs=[{k: r.get(k) for k in ('id', 'role', 'agent', 'status', 'evidence', 'launch_error')} for r in others],
                second_opinion=('py "%s" ask %s "<вопрос с контекстом>" — пути решения от второго ИИ' % (w.ROOT / 'tools' / 'pm.py', other_ai[0])) if other_ai else None,
                handoff=('tt_work op=resolve ref=%s run=%s worker=<ты> options=[{title, pros, cons}, …≥2] decision=continue|return|owner '
                         'notes=… instructions=… (continue/return) to_stage=… roles=[…] (return) question=… why_owner=… (owner)' % (t['ref'], run['id'])))


def resolver_prompt(t, run, route, worker=None):
    s = w.state(); prob = problem(s, run.get('problem')) or {}
    wt = (t.get('pipeline') or {}).get('worktree') or {}
    lines = ['Ты — Решала в TaskTreker: разбираешь проблему %s по задаче %s «%s».' % (prob.get('id', '?'), t['ref'], t['title']),
             'Проблема (%s, роль %s): %s' % ('агент' if prob.get('source') == 'agent' else 'сервер', prob.get('role') or '—', prob.get('reason', '')),
             'Следуй скиллу tt-resolver (/tt-resolver). Модель: %s, усилия: %s.' % (route['model'], route['effort']),
             'Рабочая копия задачи: %s.' % (wt.get('path') or 'нет — смотри репозиторий проекта'),
             '1) tt_work op=claim ref=%s run=%s model=%s worker=%s' % (t['ref'], run['id'], route['model'], worker or '<id этой сессии>'),
             '2) tt_work op=packet ref=%s run=%s — проблема, журнал, последние запуски.' % (t['ref'], run['id']),
             '3) Найди не меньше двух путей решения (второе мнение — у другого ИИ, команда в паспорте), выбери лучший и пришли tt_work op=resolve.',
             'Владельца зови (decision=owner) только если без его прав, денег, доступов или продуктового решения дальше нельзя.',
             w.LIMIT_RULE % (t['ref'], run['id'], worker or '<id этой сессии>'),
             *([w.AUTONOMY_RULE] if w.autonomous() else []),
             'Работай только через MCP tt_work. Твой worker во всех вызовах: %s.' % (worker or '<id этой сессии>')]
    return '\n'.join(lines)


def role_packet(t, run, agent):
    if run.get('role') == 'resolver':
        return resolver_packet(t, run, agent)
    pipe = t['pipeline']; role, stage = run['role'], run['stage']; spec = P.ROLES[role]
    route = role_route(t, role, agent)
    wt = pipe.get('worktree') or {}
    entry = pipe['roles'].get(key(stage, role)) or {}
    arts = []
    for st in P.ORDER[:P.ORDER.index(stage) + 1 if stage in P.ORDER else 0]:
        for r in P.stage_roles(st, pipe.get('needs')):
            for a in P.ROLES[r]['artifacts']:
                arts.append(dict(role=P.ROLES[r]['title'], path=P.art_path(t['id'], a['path']), optional=bool(a.get('optional'))))
    dod = [] if role == 'teamlead' else P.role_dod(role, stage)
    packet = dict(ref=t['ref'], title=t['title'], why=t.get('why'), root=t.get('root'), acceptance=t.get('acceptance'),
                  role=dict(id=role, title=spec['title'], skill='tt-' + role.replace('_', '-'), mission=spec['mission']),
                  stage=dict(id=stage, title=P.STAGE[stage]['title'] if stage in P.STAGE else stage),
                  route=route, phase='role', gate=run.get('gate'),
                  workspace=dict(path=wt.get('path'), branch=wt.get('branch'), base=wt.get('base'),
                                 note='Работай только в этой рабочей копии; коммить свои файлы в ветку задачи.'),
                  artifacts=[dict(path=P.art_path(t['id'], a['path']), sections=a['sections'], min_bytes=a['min_bytes'],
                                  optional=bool(a.get('optional'))) for a in spec['artifacts']],
                  dod=[dict(id=i['id'], text=i['text'], kind=i['kind'], allow_na=bool(i.get('allow_na'))) for i in dod],
                  upstream=arts, needs=pipe.get('needs'), feedback=entry.get('feedback'),
                  resume=((run.get('pauses') or [{}])[-1].get('checkpoint') or None),
                  defects=[{k: d.get(k) for k in ('id', 'title', 'severity', 'level', 'test', 'found_by', 'status')}
                           for d in pipe.get('defects') or [] if d['status'] in ('open', 'fixed')],
                  pause='tt_work op=pause ref=%s run=%s worker=<ты> checkpoint=… — при исчерпании лимита токенов' % (t['ref'], run['id']),
                  handoff='tt_work op=handoff ref=%s run=%s worker=<ты> summary=… items={id: {status: done|na, evidence}}' % (t['ref'], run['id']))
    if role == 'product_owner':
        packet['handoff'] += ' acceptance=[бизнес-критерии] needs={design: bool, marketing: bool, reason}'
    if role == 'reviewer': packet['handoff'] += ' verdict=approve; замечания — op=return to_stage=development [defects=[…]]'
    if role == 'qa': packet['handoff'] += ' signoff=true; дефекты — op=return to_stage=development defects=[{title, severity, level, test}]'
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
        packet['handoff'] = 'tt_work op=gate ref=%s run=%s worker=<ты> decision=approve|return notes=… [to_stage, roles]' % (t['ref'], run['id'])
    else:
        packet['escalate'] = ('tt_work op=escalate ref=%s run=%s worker=<ты> reason=… tried=… need=… — проблема или ограничение: '
                              'разберёт агент «Решала»' % (t['ref'], run['id']))
        if role in RETURN_TO:
            packet['return'] = 'tt_work op=return ref=%s run=%s worker=<ты> to_stage=%s reason=…' % (t['ref'], run['id'], '|'.join(RETURN_TO[role]))
    return packet


def role_prompt(t, run, route, worker=None):
    if run.get('role') == 'resolver':
        return resolver_prompt(t, run, route, worker)
    role = run['role']; spec = P.ROLES[role]; pipe = t['pipeline']; wt = pipe.get('worktree') or {}
    skill = 'tt-' + role.replace('_', '-')
    lines = ['Ты — %s в TaskTreker. Задача %s «%s», этап «%s».' % (spec['title'], t['ref'], t['title'],
             P.STAGE[run['stage']]['title'] if run.get('stage') in P.STAGE else run.get('stage')),
             'Следуй скиллу %s (/%s): там твой скоуп, DoR, DoD и артефакты — ровно то, что проверяет сервер.' % (skill, skill),
             'Рабочая копия: %s, ветка %s. Коммить только свои файлы; ветку не сливай — слияния делает сервер.' % (wt.get('path') or '—', wt.get('branch') or '—'),
             'Модель: %s, усилия: %s.' % (route['model'], route['effort'])]
    if run.get('gate'):
        lines.append('Это ворота тимлида: %s.' % run['gate'])
    lines += ['1) tt_work op=claim ref=%s run=%s model=%s worker=%s' % (t['ref'], run['id'], route['model'], worker or '<id этой сессии>'),
              '2) tt_work op=packet ref=%s run=%s — паспорт роли: DoD, артефакты, предыдущие артефакты, замечания.' % (t['ref'], run['id']),
              '3) Сделай работу, закоммить артефакты, затем %s — это уведомление тимлиду. Не принятая передача вернёт список несделанных пунктов.'
              % ('op=gate' if role == 'teamlead' else 'op=handoff'),
              COMMIT_RULE,
              'Проблема, ограничение, нет доступа, противоречие — не жди и не спрашивай владельца: tt_work op=escalate ref=%s run=%s '
              'reason=… tried=… need=… — её разберёт агент «Решала».' % (t['ref'], run['id']),
              w.LIMIT_RULE % (t['ref'], run['id'], worker or '<id этой сессии>'),
              *([w.AUTONOMY_RULE] if w.autonomous() else []),
              *([run['failover']['handover']] if (run.get('failover') or {}).get('handover') else []),
              'Работай только через MCP tt_work. Твой worker во всех вызовах: %s.' % (worker or '<id этой сессии>')]
    return '\n'.join(lines)
