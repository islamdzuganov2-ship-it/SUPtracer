"""Доски и контракт исполнения. Ручной ввод, без автоматического сбора портфеля.

Несколько ИИ (Claude, GPT / Codex) работают с одним бэклогом. Задача может быть
назначена ИИ (executor); принятая задача несёт метку claimed_by с арендой:
пока аренда жива, другой ИИ задачу не возьмёт. Области правки (scope) не дают
двум ИИ одновременно менять одни и те же файлы проекта.
"""
import argparse
import json
import os
from collections import Counter
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import complexity as cx
import taskstate as ts
import sprints
import flow_policy as fp
import owner_auth as oa
import release_policy as rp

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://developers.openai.com/api/docs/guides/model-selection'
def provider_name(agent):
    """Имя исполнителя для заметок: внешние ИИ из PROVIDERS, локальный исполнитель пилота — свой."""
    if agent in PROVIDERS: return PROVIDERS[agent]['name']
    return 'MiMo (локально)' if agent=='local_mimo' else str(agent)

PROVIDERS = {
    'codex': dict(name='GPT / Codex', short='GPT', client='Codex CLI, ChatGPT Desktop, расширение IDE',
                  source=SOURCE, verified='2026-10-19'),
    'claude': dict(name='Claude', short='Claude', client='Claude Code (терминал, приложение, IDE)',
                   source='skill claude-api, таблица моделей от 2026-10-06', verified='2026-10-19'),
}
MODELS = [
    dict(id='gpt-6-luna', provider='codex', tier=1, strength='Локальные правки, типовые тесты, извлечение и форматирование', weakness='Сложные зависимости и неопределённая архитектура требуют проверки', effort='medium'),
    dict(id='gpt-6.1-sol', provider='codex', tier=2, strength='Разработка, интеграции и задачи с несколькими файлами', weakness='Стоимость выше Luna; для простых правок избыточна', effort='medium'),
    dict(id='gpt-6-astra', provider='codex', tier=3, strength='Сложные исследования, архитектура и трудная диагностика', weakness='Высокий расход; применять при измеримой необходимости', effort='high'),
    dict(id='gpt-6-sol', provider='codex', tier=2, strength='Предыдущее поколение для разработки', weakness='Нужна сравнительная проверка с 6.1 Sol', effort='medium'),
    dict(id='gpt-5.6-sol', provider='codex', tier=2, strength='Совместимость с существующими процессами', weakness='Старое поколение; не назначается автоматически', effort='medium'),
    dict(id='gpt-5.6-terra', provider='codex', tier=1, strength='Несложные структурированные задачи', weakness='Старое поколение; выбор только после оценки', effort='medium'),
    dict(id='gpt-5.6-luna', provider='codex', tier=1, strength='Простые задачи предыдущего поколения', weakness='Старое поколение; выбор только после оценки', effort='medium'),
    dict(id='claude-haiku-5-5', provider='claude', tier=1, strength='Малые правки, тесты по образцу, извлечение; самый дешёвый', weakness='Многофайловые изменения и архитектура — только с проверкой', effort='medium'),
    dict(id='claude-sonnet-5-5', provider='claude', tier=2, strength='Повседневная разработка, интеграции, агентная работа с инструментами', weakness='Для простых правок избыточен', effort='medium'),
    dict(id='claude-opus-5-5', provider='claude', tier=3, strength='Архитектура, трудная диагностика, длинные агентные задачи', weakness='Дороже Sonnet вдвое; применять при измеримой необходимости', effort='high'),
    dict(id='claude-fable-5-1', provider='claude', tier=3, strength='Самые трудные исследования и долгие автономные задачи', weakness='Дороже Opus; только явный выбор с обоснованием', effort='high'),
]
# Автоматический выбор: класс задачи -> модель линейки ИИ. Остальные модели — только явно.
LINEUP = {'codex': {1: 'gpt-6-luna', 2: 'gpt-6.1-sol', 3: 'gpt-6-astra'},
          'claude': {1: 'claude-haiku-5-5', 2: 'claude-sonnet-5-5', 3: 'claude-opus-5-5'}}
# Уровни усилий: у Claude 5.5 — low…max (skill claude-api); у Codex в каталоге — low…high.
EFFORTS = {'claude': ('low', 'medium', 'high', 'xhigh', 'max'), 'codex': ('low', 'medium', 'high')}
EFFORT_COST = {'low': .75, 'medium': 1., 'high': 1.25, 'xhigh': 1.5, 'max': 2.}  # ориентир бюджета, не тариф
DEFAULT_EXECUTOR = 'codex'   # прежнее поведение; меняется op=settings
STATUS_LABELS = {'queued': 'К выполнению', 'in_progress': 'В работе', 'review': 'Проверка',
                 'blocked': 'Заблокировано', 'done': 'Готово', 'cancelled': 'Отменено'}
MANUAL_STATUSES = ('queued', 'in_progress', 'review', 'blocked', 'cancelled')   # «Готово» — только с проверкой
ASSESS_BUDGET = 4000         # оценка сложности короче исполнения
PACKET_LIMIT = 6000          # символов JSON в паспорте — с запасом, не счёт токенов
FEEDBACK_CUT = ' …[сокращено до лимита паспорта]'
LEASE_MINUTES = 45           # срок метки занятости без heartbeat
LOCK_STALE_SECONDS = ts.LOCK_STALE_SECONDS
PRIORITIES = ('🔴', '🟡', '🟢')
RISKS = ('low', 'medium', 'high')
WORK_TYPES = ('docs', 'test', 'implementation', 'architecture', 'research')
DIRECTIONS = ('аналитика', 'разработка', 'дизайн', 'маркетинг', 'продажи', 'эксплуатация', 'другое')
# Тип задачи определяет, какие входные данные нужны исполнителю. Поля типа
# обязательны: без ТЗ разработка, а без вопроса аналитика идут вслепую.
TASK_TYPES = {
    'dev_by_spec': dict(name='Разработка по готовому ТЗ', direction='разработка',
        hint='ТЗ уже есть: реализовать требования, проверить критерии.',
        work_types=('implementation', 'test', 'docs'),
        fields=dict(spec_ref='ТЗ: файл, раздел или коды требований')),
    'analysis_spec': dict(name='Аналитика и создание ТЗ', direction='аналитика',
        hint='Исследовать вопрос и записать ТЗ, по которому потом пойдёт разработка.',
        work_types=('research', 'docs', 'architecture'),
        fields=dict(question='Предмет аналитики: что нужно выяснить или решить',
                    inputs='Источники и входные данные', deliverable='Куда записать ТЗ: файл или раздел')),
    'decomposition': dict(name='Проработка и декомпозиция на задачи', direction=None,
        hint='Разобрать цель и создать задачи по направлениям: аналитика, разработка, дизайн, маркетинг, продажи…',
        work_types=('architecture', 'research'),
        fields=dict(directions='Направления, по которым создать задачи')),
    'direction_task': dict(name='Задача направления: дизайн, маркетинг, продажи…', direction=None,
        hint='Работа одного направления с понятным результатом.',
        work_types=('implementation', 'docs', 'research', 'test'),
        fields=dict(direction='Направление', deliverable='Ожидаемый результат: что и где будет готово')),
}
TYPE_FIELDS = ('spec_ref', 'question', 'inputs', 'deliverable', 'directions', 'direction', 'constraints')
POLICY = dict(version=3, verified='2026-10-19', source=SOURCE,
    sources={k: v['source'] for k, v in PROVIDERS.items()},
    availability='Снимок моделей Codex этого хоста и таблицы моделей Claude на 19.10.2026; проверять перед исполнением',
    evidence='Рекомендации — начальные эвристики, не результаты локального сравнения моделей',
    rules=['Сначала декомпозиция с критериями приёмки и зависимостями.',
           'Выбирать минимальный достаточный класс модели; неизвестную задачу сначала уточнить.',
           'Читать паспорт, затем только необходимые файлы и записи recall; не читать портфель целиком.',
           'Использовать поиск, локальные скрипты и кеш неизменных результатов вместо повторного анализа.',
           'После двух неудачных попыток остановиться, сохранить диагностику и пересмотреть модель или декомпозицию.',
           'При исчерпании бюджета сохранить результат и запросить пересмотр; не продолжать молча.',
           'Закрывать задачу только с результатами проверок; учитывать полный расход вместе с повторами.',
           'Сравнивать модели на одинаковых критериях: качество, токены, время, доля повторов.',
           'Задачу с меткой claimed_by другого ИИ не брать и её область (scope) не править.',
           'Работать в своей ветке/рабочей копии; продлевать аренду heartbeat, пока работа идёт.',
           'Модель и усилия брать из плана модели задачи для своего ИИ; поднимать усилия раньше, чем класс модели.'])

def data_dir():
    return Path(os.environ.get('TASKTREKER_DATA') or ROOT / 'data')

def now():
    return datetime.now(timezone.utc)

def read(name, default):
    return ts.read_json(str(data_dir()), name, default)

def save(name, value):
    ts.save_json(str(data_dir()), name, value)

def locked():
    return ts.locked(str(data_dir()))

def state():
    """Запуски и оперативное состояние задач (tasks: ref -> поля, см. taskstate.py)."""
    return read('workflow.json', dict(schema=1, focus=[], tasks={}, runs=[]))

REVIEW_VERDICTS = {'approve': 'Принято', 'changes': 'Нужна доработка', 'spec_change': 'Принято с изменением ТЗ'}

MAX_SESSIONS = 4   # окон ИИ, которые сервер досок держит открытыми одновременно
SESSIONS_PER_AI = 2   # и не больше стольких на каждый ИИ (А7)
WIP_ANALYSIS = 6   # задач проекта в анализе одновременно; пока лимит занят, PO новые идеи не разбирает (А2)
# Эстафета (TT-91): контекст сессии роли перерос потолок — свежая сессия с точки остановки. Каждый ход
# заново читает весь контекст, поэтому сессия на 150 тыс. токенов платит за каждый шаг вдесятеро больше
# свежей (старт ~15 тыс.). 0 — эстафета выключена.
CONTEXT_CEILING = 90_000
MAX_RELAYS = 4     # эстафет на запуск — дальше сессия работает как есть: значит, этап не дробится
# Режим согласия владельца: step — окна и вопросы на каждом шаге; full — одно согласие на всё:
# сессии без окон, без вопросов, релиз в main без отдельного «да». В удалённый репозиторий — никогда.
AUTONOMY = ('step', 'full')
AUTONOMY_RULE = ('ПОЛНАЯ АВТОНОМИЯ: владелец заранее дал согласие на всю работу. Не задавай вопросов владельцу '
                 'и не жди ответа — его не будет: неясное решай сам в рамках скоупа своей роли, допущения записывай '
                 'в артефакт (раздел «Допущения»). Проблема, ограничение или то, что без владельца решить нельзя, — '
                 'op=escalate к агенту «Решала» (reason, tried, need): он найдёт решение или сам позовёт владельца. '
                 'Работа заканчивается только вызовом tt_work: handoff, return, escalate или pause (для задач вне '
                 'конвейера — finish или fail). Закончить ответом без такого вызова нельзя — сервер вернёт тебя к работе.')

def autonomous(s=None):
    return settings(s)['autonomy']=='full'

def _balance_defaults():
    import provider_balance as pb, adaptive_pipeline as ap, copy
    return copy.deepcopy(dict(pb.DEFAULTS, **ap.DEFAULTS, learning=dict(enabled=True, pilot_projects=[])))

def settings(s=None):
    return dict(dict(default_executor=DEFAULT_EXECUTOR, lease_minutes=LEASE_MINUTES,
                     reviewer='same', auto_merge=True, auto_launch_review=True, auto_pipeline=True, roles={},
                     max_sessions=MAX_SESSIONS, autonomy='step', failover=True,
                     sessions_per_ai={p: SESSIONS_PER_AI for p in PROVIDERS}, wip_analysis=WIP_ANALYSIS,
                     context_ceiling=CONTEXT_CEILING, **fp.DEFAULTS, **_balance_defaults()),
                **(s or state()).get('settings', {}))

def model_entry(model):
    return next((m for m in MODELS if m['id'] == model), None)

def provider_of(model):
    entry = model_entry(model)
    if not entry:
        import local_mimo
        entry = local_mimo.model_entry(model)      # локальный исполнитель пилота (LEARN-02)
    if not entry: raise ValueError('Модель отсутствует в каталоге: ' + str(model))
    return entry['provider']

def check_executor(value):
    if value not in ('', None) and value not in PROVIDERS:
        raise ValueError('ИИ-исполнитель: ' + ', '.join(PROVIDERS) + ' или пусто')

def auto_plan(task, provider):
    """Модель и усилия из многофакторной оценки сложности (complexity.py)."""
    c = cx.assess(task)
    effort, why = cx.effort_for(c, EFFORTS[provider], task)
    return dict(model=LINEUP[provider][c['tier']], effort=effort,
                reason=cx.summary(c) + '; усилия: ' + why, source='auto')

def validate_plan_entry(provider, entry):
    if not isinstance(entry, dict): raise ValueError('План модели %s: объект model, effort, reason' % provider)
    m = model_entry(entry.get('model'))
    if not m or m['provider'] != provider: raise ValueError('План модели %s: модель не из линейки этого ИИ' % provider)
    if entry.get('effort') not in m.get('efforts', EFFORTS[provider]): raise ValueError('План модели %s: допустимые усилия %s' % (provider, ', '.join(EFFORTS[provider])))
    if not isinstance(entry.get('reason'), str) or not entry['reason'].strip(): raise ValueError('План модели %s: обоснуйте ручной выбор' % provider)

def model_plan(task):
    """Модель и усилия под задачу для каждого ИИ. Ручные строки сохраняются,
    автоматические пересчитываются при изменении размера, риска или вида работы."""
    stored = task.get('model_plan') or {}
    if not isinstance(stored, dict): raise ValueError('model_plan должен быть объектом по ИИ')
    out = {}
    for provider in PROVIDERS:
        entry = stored.get(provider)
        if entry and entry.get('source') != 'auto':
            validate_plan_entry(provider, entry)
            out[provider] = dict(model=entry['model'], effort=entry['effort'], reason=entry['reason'].strip(), source='manual')
        else:
            out[provider] = auto_plan(task, provider)
    if set(stored) - set(PROVIDERS): raise ValueError('model_plan: только ' + ', '.join(PROVIDERS))
    return out

def budget_for(model, effort):
    entry = model_entry(model)
    base = {1: 6000, 2: 12000, 3: 24000}[entry['tier']]
    return int(round(base * EFFORT_COST[effort] / EFFORT_COST[entry['effort']], -2))

def recommendation(task, agent=None, default=None):
    """Маршрут: какой ИИ, какая модель, с какими усилиями, какой бюджет.

    ИИ берётся из назначения задачи, затем из того, кто спрашивает (agent),
    затем из настройки по умолчанию. Модель и усилия — из плана модели задачи
    (model_plan); прежнее поле model сильнее плана."""
    risk = task.get('risk', 'unknown'); kind = task.get('work_type', 'unknown')
    executor = task.get('executor') or ''
    check_executor(executor)
    if agent is not None: check_executor(agent)
    provider = executor or agent or default or DEFAULT_EXECUTOR
    plan = model_plan(task)[provider]
    model, effort, reason, source = plan['model'], plan['effort'], plan['reason'], plan['source']
    if task.get('model'):
        owner = provider_of(task['model'])
        if executor and owner != executor:
            raise ValueError('Модель %s принадлежит ИИ %s, а задача назначена %s' % (task['model'], owner, executor))
        # Явная модель задаёт ИИ, если задача никому не назначена.
        model, provider, source = task['model'], owner, 'manual'
        effort = model_plan(task)[owner]['effort'] if model_plan(task)[owner]['model'] == model else model_entry(model)['effort']
        reason = task.get('model_reason') or 'Явный выбор исполнителя'
    return dict(model=model, provider=provider, effort=effort, reason=reason, source=source,
                context_budget=2000, total_token_budget=budget_for(model, effort), max_attempts=2,
                provisional=risk == 'unknown' or kind == 'unknown', policy_version=POLICY['version'])

def parse_time(value):
    try: return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError): return None

def lease_alive(run, at=None):
    """Живая аренда. У старых запусков без срока аренда бессрочна."""
    if run.get('status') != 'running': return False
    until = parse_time(run.get('lease_until'))
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)      # старые записи без пояса — это UTC
    return until is None or until > (at or now())

def active(runs):
    return [r for r in runs if r.get('status') in ('queued', 'running')]

def norm_scope(scope):
    out=[]
    for p in scope or []:
        p=p.strip().replace('\\', '/').lower()
        while p.startswith('./'): p=p[2:]
        p=p.strip('/')
        out.append('' if p=='.' else p)   # '' — весь репозиторий
    return out

def scopes_overlap(a, b):
    """Пересечение областей по префиксу пути. Пустая область пересечения не даёт:
    неизвестное не блокирует, но паспорт предупреждает об этом."""
    a, b = norm_scope(a), norm_scope(b)
    return any(x == y or x.startswith(y + '/') or y.startswith(x + '/') or not x or not y for x in a for y in b)

def validate_scope(scope):
    if not isinstance(scope, list) or len(scope) > 20 or any(
            not isinstance(p, str) or not p.strip() or len(p) > 200 or '..' in p for p in scope):
        raise ValueError('scope: список до 20 относительных путей без ".."')

def review_required(t, route, runs):
    failures = [r for r in runs if r['ref'] == t['ref'] and r['status'] in ('failed', 'expired')
                and r.get('packet', {}).get('route', {}).get('model') == route['model']]
    return len(failures) >= route['max_attempts'] or sum(r.get('tokens') or 0 for r in failures) >= route['total_token_budget']

def claim_view(run, at):
    return dict(run=run['id'], agent=run.get('agent') or (model_entry(run.get('model')) or {}).get('provider'),
                worker=run.get('worker'), model=run.get('model'), lease_until=run.get('lease_until'),
                stale=not lease_alive(run, at))

def routes(t):
    """Рекомендация для каждого ИИ — то, что показывается рядом с «Запустить»."""
    out = {}
    for p in PROVIDERS:
        legacy = t.get('model') if (model_entry(t.get('model')) or {}).get('provider') == p else None
        try: r = recommendation(dict(t, executor=p, model=legacy), agent=p)
        except ValueError: r = recommendation(dict(t, executor=p, model=None, model_plan=None), agent=p)
        out[p] = {k: r[k] for k in ('model', 'effort', 'reason', 'source', 'total_token_budget')}
    return out

def snapshot():
    projects = read('projects.json', {}).get('projects', {}); s = state(); tasks = []
    conf = settings(s); at = now()
    for key, project in projects.items():
        for original in project.get('backlog', []):
            ref = key + '/' + original['id']
            t = ts.merge_task(original, s['tasks'].get(ref)); t.update(project=key, ref=ref)
            if t.get('status') == 'in-progress': t['status'] = 'in_progress'   # прежнее написание из Obsidian
            # Ручная правка: критерии строкой, а не списком. Доска ждёт список — паспорт падал на .map (A11-06).
            for f in ('acceptance', 'depends_on'):
                if isinstance(t.get(f), str):
                    t[f] = [x.strip() for x in (t[f].splitlines() if f == 'acceptance' else t[f].replace(';', ',').split(','))
                            if x.strip()]
            t['complexity'] = cx.assess(t)
            try: t['route'] = recommendation(t, default=conf['default_executor'])
            except ValueError as e:
                # Ручная правка projects.json не должна ронять все доски.
                t['route'] = dict(recommendation(dict(t, model=None, executor=None, model_plan=None), default=conf['default_executor']), error=str(e))
            if not t.get('executor') and not t.get('model'):
                t['alternatives'] = {p: recommendation(t, agent=p)['model'] for p in PROVIDERS}
            t['routes'] = routes(t)
            tasks.append(t)
    byref = {t['ref']: t for t in tasks}
    running = {r['ref']: r for r in s['runs'] if r['status'] == 'running'}
    queued = {r['ref']: r for r in s['runs'] if r['status'] == 'queued'}
    for t in tasks:
        t['blocked_by'] = [dep for dep in t.get('depends_on', [])
            if byref.get(dep if '/' in dep else t['project']+'/'+dep, {}).get('status') != 'done']
        t['needs_plan'] = not bool(t.get('acceptance')) or not t.get('root') or t.get('size') == 'XL'
        t['review_required'] = review_required(t, t['route'], s['runs'])
        t['claim'] = claim_view(running[t['ref']], at) if t['ref'] in running else None
        held = t['claim']['agent'] if t['claim'] else queued[t['ref']].get('agent') if t['ref'] in queued else None
        if held in PROVIDERS and not t.get('executor') and not t.get('model'):
            # Карточка показывает то, на чём задача реально идёт или поставлена, а не маршрут по умолчанию.
            t['route'] = recommendation(t, agent=held)
        t['queued_run'] = queued[t['ref']]['id'] if t['ref'] in queued else None
        priority = {'🔴': 0, '🟡': 1, '🟢': 2}.get(t.get('priority'), 1)
        t['rank'] = [0 if t['project'] in s['focus'] else 1, priority, {'S':1,'M':3,'L':8,'XL':20}.get(t.get('size'), 3), t['ref']]
    tasks.sort(key=lambda t:t['rank'])
    import ci
    cilast = {}
    for r in ci.runs(limit=ci.KEEP_RUNS):
        if r.get('ref'): cilast.setdefault(r['ref'], {})[r['scope']] = {k: r.get(k) for k in ('id', 'status', 'branch', 'sha', 'finished', 'note')}
    import release_bundle as rb
    for t in tasks:
        if t.get('pipeline'):
            t['ci'] = cilast.get(t['ref']) or {}
            # Состояние доказательств выпуска — источник и дата, STALE и NOT_RUN не красятся зелёным (07: A11-07).
            t['evidence_state'] = rb.evidence_state(t['pipeline'], (t['ci'].get('test') or {}).get('sha'))
    roots=[{k:r.get(k) for k in ('id','title','project')} for r in read('memory/index.json',{}).get('records',[])
           if r.get('kind')=='запрос' and r.get('status')=='активна']
    import pipeline as P
    return dict(sprint=sprints.view(s, tasks), flow=fp.snapshot(s, tasks, now()), pipeline=P.spec_for_board(), teamlead=s.get('teamlead',[])[-80:], active_projects=s.get('active_projects',[]),
                provider_limits={a:v for a,v in (s.get('provider_limits') or {}).items() if limited(s, a)},
                problems=(s.get('problems') or [])[-200:], notifications=notifications(s, tasks),
                projects={k:dict(workspace(k,p), next_id=next_id(p)) for k,p in projects.items()},
                task_types=TASK_TYPES, directions=DIRECTIONS,
                tasks=tasks, roots=roots, focus=s['focus'], runs=s['runs'][-50:], models=MODELS, policy=POLICY,
                providers=PROVIDERS, lineup=LINEUP, efforts=EFFORTS, settings=conf)

def validate_details(task):
    due=task.get('due_date','')
    if not isinstance(due,str): raise ValueError('Срок должен быть датой ГГГГ-ММ-ДД')
    if due:
        try:
            if date.fromisoformat(due).isoformat()!=due: raise ValueError()
        except ValueError: raise ValueError('Срок должен быть датой ГГГГ-ММ-ДД')
    done=task.get('acceptance_done',[])
    if not isinstance(done,list) or any(type(i)!=int or i<0 or i>=len(task.get('acceptance',[])) for i in done) or len(done)!=len(set(done)):
        raise ValueError('Неверные отметки критериев')
    check_executor(task.get('executor', ''))
    if 'scope' in task: validate_scope(task['scope'])
    validate_type(task)
    if task.get('assessment') is not None: cx.validate_assessment(task['assessment'])

def validate_type(task):
    kind = task.get('task_type')
    if kind in (None, ''):
        return   # задачи до появления типов и планы ассистентов без типа остаются законными
    if kind not in TASK_TYPES: raise ValueError('Тип задачи: ' + ', '.join(TASK_TYPES))
    spec = TASK_TYPES[kind]
    for field, label in spec['fields'].items():
        value = task.get(field)
        if field == 'directions':
            if not isinstance(value, list) or not value or any(v not in DIRECTIONS for v in value) or len(set(value)) != len(value):
                raise ValueError('Не заполнено поле «%s»: одно или несколько из %s' % (label, ', '.join(DIRECTIONS)))
        elif field == 'direction':
            if value not in DIRECTIONS: raise ValueError('Не заполнено поле «%s»' % label)
        elif not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError('Не заполнено поле «%s»' % label)
    if task.get('work_type') not in spec['work_types']:
        raise ValueError('Вид работы для типа «%s»: %s' % (spec['name'], ', '.join(spec['work_types'])))
    if task.get('constraints') is not None and (not isinstance(task['constraints'], str) or len(task['constraints']) > 2000):
        raise ValueError('Ограничения — текст до 2000 символов')

def git_remote(path):
    """URL origin из .git/config без запуска git: рабочее место ИИ, не сбор фактов."""
    if not path: return None
    cfg = Path(path) / '.git'
    try:
        if cfg.is_file():   # рабочая копия: .git — файл со ссылкой на каталог
            cfg = Path(cfg.read_text(encoding='utf-8').split('gitdir:', 1)[1].strip()).parents[1]
        section = None
        for line in (cfg / 'config').read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('['): section = line
            elif section == '[remote "origin"]' and line.startswith('url'):
                return line.split('=', 1)[1].strip()
    except (OSError, IndexError):
        return None
    return None

def workspace(key, project):
    """Где ИИ работает над задачей: выбор проекта однозначно задаёт папку и репозиторий."""
    path = project.get('inspection_path') or project.get('path') or ''
    return dict(project=key, name=project.get('name', key), path=project.get('path', ''),
                active_copy=project.get('inspection_path') or '', repo=git_remote(path) or git_remote(project.get('path')),
                exists=bool(path) and Path(path).exists())

def next_id(project, taken=()):
    """Следующий номер в серии проекта: id_prefix или самый частый префикс бэклога."""
    import re
    ids = [t.get('id', '') for t in project.get('backlog', [])] + list(taken)
    found = [m.groups() for m in (re.match(r'^(.*?)(\d+)$', i) for i in ids) if m]
    prefix = project.get('id_prefix')
    if not prefix:
        counts = {}
        for pre, _ in found: counts[pre] = counts.get(pre, 0) + 1
        prefix = max(counts, key=lambda k: (counts[k], k)) if counts else 'T-'
    elif not any(pre == prefix for pre, _ in found):
        # id_prefix «EGE» при серии «EGE-01…»: продолжать серию с разделителем, а не начинать «EGE01».
        same = [pre for pre, _ in found if pre.rstrip('-_. ') == prefix.rstrip('-_. ')]
        if same: prefix = max(set(same), key=same.count)
    numbers = [int(n) for pre, n in found if pre == prefix]
    width = max((len(n) for pre, n in found if pre == prefix), default=2)
    return prefix + str(max(numbers, default=0) + 1).zfill(width)

def task_for(ref):
    t = next((t for t in snapshot()['tasks'] if t['ref'] == ref), None)
    if not t: raise ValueError('Нет такой задачи: '+ref)
    return t

def others_active(t, runs, at, worker=None):
    """Активные запуски того же проекта у других исполнителей."""
    return [r for r in runs if r['ref'] != t['ref'] and r['ref'].split('/')[0] == t['project']
            and r['status'] == 'running' and lease_alive(r, at) and r.get('worker') != worker]

def packet(ref, agent=None):
    t=task_for(ref); s=state(); at=now()
    route=recommendation(t, agent=agent, default=settings(s)['default_executor'])
    phase='review' if t.get('status')=='review' else 'assess' if t['complexity']['needs_assessment'] else 'execute'
    if t['needs_plan']: raise ValueError('Сначала заполните root, критерии приёмки и разбейте XL')
    if review_required(t, route, s['runs']): raise ValueError('Лимит попыток/токенов достигнут: разбейте задачу или обоснуйте другую модель')
    if t['blocked_by']: raise ValueError('Не закрыты зависимости: '+', '.join(t['blocked_by']))
    result={k:t.get(k) for k in ('ref','title','why','root','acceptance','depends_on','scope')}
    if t.get('task_type') in TASK_TYPES:
        result['task_type']=dict(id=t['task_type'], name=TASK_TYPES[t['task_type']]['name'],
                                 **{k:t[k] for k in TYPE_FIELDS if t.get(k) not in (None,'',[])})
    project=read('projects.json',{}).get('projects',{}).get(t['project'],{})
    result['workspace']=dict(workspace(t['project'],project), note='Работать только в этом проекте и его папке; путь active_copy, если указан, — текущая рабочая копия.')
    live=next((r for r in s['runs'] if r['ref']==t['ref'] and r['status'] in ('queued','running') and (r.get('launch') or {}).get('worktree')),None)
    if live:
        result['workspace'].update(worktree=live['launch']['worktree'], note='Работать в отдельной рабочей копии задачи: %s, ветка %s.' % (live['launch']['worktree']['path'], live['launch']['worktree']['branch']))
    result['route']=route
    result['instructions']=POLICY['rules']
    result['read_next']=['tt_context(project='+t['project']+', budget=1200)', 'tt_recall(query='+t['root']+')']
    parallel=[]
    for r in others_active(t, s['runs'], at)[:5]:
        other=next((x for x in snapshot()['tasks'] if x['ref']==r['ref']), {})
        parallel.append(dict(ref=r['ref'], title=other.get('title'), agent=r.get('agent'), worker=r.get('worker'),
                             scope=other.get('scope') or [], lease_until=r.get('lease_until')))
    result['coordination']=dict(
        parallel=parallel, branch='ai/%s/%s' % (route['provider'], t['id'].lower()),
        note=('Параллельно работают другие исполнители: не правь их scope, держи изменения в своей ветке.'
              if parallel else 'Других активных исполнителей в проекте нет.')
             + ('' if t.get('scope') else ' Область задачи (scope) не указана — пересечения не проверяются.'))
    result['phase']=phase
    result['complexity']={k:t['complexity'][k] for k in ('score','tier','confidence','conflicts','unknown')}
    if phase=='assess':
        # Оценка идёт на текущей модели как есть; после неё план пересчитывается.
        route=dict(route, total_token_budget=ASSESS_BUDGET); result['route']=route
        result['assess']=dict(
            task='Не реализуй задачу. Оцени её: complexity, uncertainty, breadth — целые 1…5, reasons — основания.',
            scale='1 — тривиально/ясно/одно место; 3 — обычная работа; 5 — трудно/много неизвестного/широко по системе.',
            call='tt_work op=assess ref=%s run=<RUN> worker=<ты> assessment={complexity, uncertainty, breadth, reasons}' % t['ref'],
            after='Сервер назначит модель и усилия и поставит запуск исполнения. Если назначенная модель не твоя — остановись и сообщи владельцу.')
    if phase=='review':
        wt=task_branch(s, t['ref']) or {}
        ac=t.get('auto_check') or {}
        result['review']=dict(
            task='Не дорабатывай код. Проверь результат: каждый критерий приёмки, соответствие ТЗ (spec_ref) и решение в коде.',
            branch=wt.get('branch'), base=wt.get('base'), worktree=wt.get('path'),
            diff='git diff %s...%s' % (wt.get('base') or '<база>', wt.get('branch') or '<ветка>'),
            auto_check=[dict(name=c['name'], ok=c['ok'], detail=c['detail'][:200]) for c in ac.get('checks', [])],
            verdicts='approve — всё выполнено по ТЗ; spec_change — выполнено, но решение в коде отклоняется от ТЗ и ТЗ нужно изменить (spec: deviation, proposal); changes — нужна доработка.',
            call='tt_work op=review ref=%s run=<RUN> worker=<ты> verdict=… criteria=[{ok, note} на каждый критерий по порядку] conclusion=… [spec={deviation, proposal}]' % t['ref'],
            after='approve и spec_change при пройденной автопроверке сливают ветку и закрывают задачу; changes возвращает её на доработку.')
    ac=t.get('auto_check') or {}
    if phase=='execute' and ac.get('ok') is False:
        failed=[dict(name=c['name'], detail=c['detail'][-300:]) for c in ac.get('checks',[]) if not c['ok']]
        result['auto_check_feedback']=dict(failed=failed,
            instructions=('Доработай ту же ветку %s. ' % ac.get('branch','')) +
                         ('Сначала влей в неё базовую ветку (git merge %s) и разреши конфликты, сохранив обе стороны. ' % ac.get('base','') if any(c['key']=='merge' and not c['ok'] for c in ac.get('checks',[])) else '') +
                         'Исправь остальные провалы, прогони проверки, закоммить и пришли finish — автопроверка повторится сама.')
    if t.get('review') and t['review'].get('verdict')=='changes' and phase=='execute':
        # Выполненные критерии уже в acceptance_done: доработке нужны замечания только по невыполненным.
        result['review_feedback']=dict(conclusion=t['review']['conclusion'],
            criteria=[dict(text=c['text'], ok=False, note=c.get('note','')) if not c['ok'] else dict(text=c['text'], ok=True)
                      for c in t['review']['criteria']])
    if t.get('task_type')=='decomposition':
        result['decomposition']='Создавай подзадачи через tt_work op=plan с task_type, root=%s и assessment {complexity, uncertainty, breadth, reasons} у каждой — модель и усилия подзадачи считаются по её сложности.' % t['root']
    result['launch']='Исполнитель проверяет доступность модели, принимает запуск через take/claim и выполняет критерии. Пакет не меняет модель текущего чата.'
    # Approximation is deliberately conservative and explicitly not billing tokens.
    size=len(json.dumps(result,ensure_ascii=False))
    if size > PACKET_LIMIT and 'review_feedback' in result:
        # Заключение пишет ревьюер, не автор паспорта: длинное ужимается, а не блокирует доработку.
        fb=result['review_feedback']; cut=size-PACKET_LIMIT+len(FEEDBACK_CUT)
        fb['conclusion']=fb['conclusion'][:max(len(fb['conclusion'])-cut, 500)]+FEEDBACK_CUT
        size=len(json.dumps(result,ensure_ascii=False))
    if size > PACKET_LIMIT:
        raise ValueError('Паспорт слишком велик: сократите критерии и вынесите детали в память')
    return result

def prepare_plan(project, tasks):
    """Заполнить то, что выводится однозначно: номер, вид работы по типу."""
    projects=read('projects.json',{}).get('projects',{})
    if project not in projects: raise ValueError('Выберите проект: он задаёт папку и репозиторий для ИИ')
    if not isinstance(tasks,list): raise ValueError('План: от 1 до 50 задач')
    out=[]; taken=[]
    for t in tasks:
        if not isinstance(t,dict): raise ValueError('Задача должна быть объектом')
        t=dict(t)
        if not t.get('id'):
            t['id']=next_id(projects[project], taken)
        taken.append(t['id'])
        kind=TASK_TYPES.get(t.get('task_type'))
        if kind and not t.get('work_type'): t['work_type']=kind['work_types'][0]
        if kind and kind['direction'] and not t.get('direction'): t['direction']=kind['direction']
        out.append(t)
    return out

def validate_new_root(t):
    nr=t.get('new_root')
    if not isinstance(nr,dict): raise ValueError('Новый запрос: нужны заголовок и текст')
    for key,label in (('title','заголовок запроса'),('text','текст запроса')):
        if not isinstance(nr.get(key),str) or not nr[key].strip() or len(nr[key])>(200 if key=='title' else 5000):
            raise ValueError('Не заполнено поле «%s»' % label)

def create_roots(project, tasks):
    """Новые исходные запросы — записи памяти вида «запрос» в проекте задачи."""
    if not any('new_root' in t for t in tasks): return tasks
    import memory as mem, mdparse as md
    stamp=now().isoformat()
    for i,t in enumerate(tasks):
        if 'new_root' not in t: continue
        nr=t.pop('new_root')
        text,_=md.redact_secrets(nr['text'].strip())
        # Соль со временем: одинаковый заголовок не перезапишет чужой запрос.
        rec=mem.new_record(nr['title'].strip(), text, project=project, kind='запрос', source='board',
                           id=mem.make_id(nr['title'], project, 'board:%s:%d' % (stamp, i)))
        mem.save(rec); t['root']=rec['id']
    mem.build_index()
    return tasks

def validate_plan(project, tasks):
    projects=read('projects.json',{}).get('projects',{})
    if project not in projects: raise ValueError('Выберите проект: он задаёт папку и репозиторий для ИИ')
    if not isinstance(tasks,list) or not 1 <= len(tasks) <= 50: raise ValueError('План: от 1 до 50 задач')
    existing={t['id']:t for t in projects[project].get('backlog',[])}
    records=read('memory/index.json',{}).get('records',[])
    ids=set()
    for t in tasks:
        if not isinstance(t,dict): raise ValueError('Задача должна быть объектом')
        if 'new_root' in t:
            if t.get('root'): raise ValueError('Укажите либо существующий запрос, либо новый')
            validate_new_root(t); t=dict(t,root='новый запрос')
        for key in ('id','title','why','root'):
            if not isinstance(t.get(key),str) or not t[key].strip() or len(t[key])>1000: raise ValueError('Не заполнено поле '+key)
        if '/' in t['id'] or t['id'] in ids or t['id'] in existing: raise ValueError('Повтор или неверный id')
        ids.add(t['id'])
        if t.get('size') not in ('S','M','L'): raise ValueError('Размер S/M/L; XL сначала разбить')
        if t.get('priority') not in PRIORITIES: raise ValueError('Укажите приоритет 🔴/🟡/🟢')
        if t.get('risk') not in RISKS: raise ValueError('Укажите риск low/medium/high')
        if t.get('work_type') not in WORK_TYPES: raise ValueError('Укажите вид работы')
        a=t.get('acceptance')
        if not isinstance(a,list) or not a or any(not isinstance(v,str) or not v.strip() for v in a): raise ValueError('Нужны критерии приёмки')
        deps=t.get('depends_on',[])
        if not isinstance(deps,list) or any(not isinstance(v,str) for v in deps): raise ValueError('depends_on должен быть списком id')
        if t['root']!='новый запрос' and not any(r.get('id')==t['root'] and r.get('project')==project for r in records): raise ValueError('Исходный запрос должен быть из памяти этого проекта — или создайте новый')
        validate_details(t)
        recommendation(t)
    graph={**{k:v.get('depends_on',[]) for k,v in existing.items()}, **{t['id']:t.get('depends_on',[]) for t in tasks}}
    def visit(key, stack):
        if key in stack: raise ValueError('Цикл зависимостей')
        if key not in graph: raise ValueError('Неизвестная зависимость '+key)
        for dep in graph[key]: visit(dep,stack|{key})
    for t in tasks: visit(t['id'],set())

def ready(t, agent=None):
    """Можно ли этому ИИ взять задачу прямо сейчас (без учёта областей).
    Задача с просроченной меткой доступна для перехвата."""
    stale = bool(t['claim'] and t['claim']['stale'])
    if t['claim'] and not stale: return False
    if t.get('status') in ('done','cancelled','review','blocked') or (t.get('status')=='in_progress' and not stale): return False
    return not t['blocked_by'] and not t['needs_plan'] and (agent is None or t.get('executor') in (None, '', agent))

def scope_conflict(t, tasks, runs, at, worker=None):
    byref={x['ref']:x for x in tasks}
    for r in others_active(t, runs, at, worker):
        other=byref.get(r['ref'],{})
        if t.get('scope') and other.get('scope') and scopes_overlap(t['scope'], other['scope']):
            return '%s (%s · %s)' % (r['ref'], r.get('agent') or '?', r.get('worker') or '?')
    return None

def free_tasks(project, tasks):
    """Задачи, которые распределение может отдать ИИ: открытые, без назначения и без метки занятости."""
    return [t for t in tasks if t['project']==project and t.get('status') not in ('done','cancelled')
            and not t.get('executor') and not t['claim'] and t.get('status') not in ('in_progress','review')]

def check_assign(assign, project, agents, result, tasks):
    """Показанное распределение применяется только если оно не выходит за пределы того, что можно распределить:
    свободные задачи проекта, известные ИИ, и связная группа уходит одному ИИ целиком."""
    if not isinstance(assign,dict) or not assign: raise ValueError('assign: словарь «задача → ИИ» с хотя бы одной задачей')
    eligible={t['ref'] for t in free_tasks(project, tasks)}
    closed=sorted(r for r in assign if r not in eligible)
    if closed: raise ValueError('Не распределяются (назначены вручную, заняты или закрыты): '+', '.join(closed))
    unknown=sorted({a for a in assign.values() if a not in agents})
    if unknown: raise ValueError('ИИ не из распределения: '+', '.join(unknown))
    for g in result['groups']:
        got={assign[r] for r in g['tasks'] if r in assign}
        if len(got)>1: raise ValueError('Связная группа уходит одному ИИ целиком: '+', '.join(g['tasks']))

SIZE_POINTS={'S':1,'M':3,'L':8,'XL':20}
PRIORITY_ORDER={'🔴':0,'🟡':1,'🟢':2}

def per_ai(points):
    """Сколько параллельных сессий на ИИ даёт объём готового бэклога (очки размера S=1, M=3, L=8)."""
    return 1 if points < 10 else 2 if points < 40 else 3

def open_sessions(runs, at=None, hours=12, agent=None):
    """Сессии, открытые сервером и ещё не закончившие: выполняются или открыты и ждут claim.
    Пауза по лимиту окна не занимает; открытое, но брошенное окно перестаёт считаться через 12 ч.
    agent — только сессии этого ИИ."""
    edge=(at or now())-timedelta(hours=hours); n=0
    for r in runs:
        if r.get('paused') or not r.get('launched_at'): continue
        if agent and r.get('agent')!=agent: continue
        pid=(r.get('launch') or {}).get('pid')
        if pid and r.get('status') in ('queued','running'):
            import launcher
            if not launcher.alive(pid): continue      # окно закрыто или сессия завершилась
        if r.get('status')=='running' or (r.get('status')=='queued' and (parse_time(r['launched_at']) or edge) > edge): n+=1
    return n

def mark_nudged(ref, run_id, info, reason):
    """Сессия закончилась без сдачи этапа — открыта снова (та же сессия) с напоминанием."""
    with locked():
        s=state(); at=now(); t=task_for(ref)
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref),None)
        if not run: return None
        run['nudges']=run.get('nudges',0)+1
        launches=list(run.get('launches') or []); launches.append(dict(info, at=at.isoformat(), nudge=reason))
        run.update(launches=launches[-5:], launch=dict(run.get('launch') or {}, **info), last_nudge_at=at.isoformat())
        run.pop('launch_error',None)
        if run['status']=='running':
            until=(at+timedelta(minutes=settings(s)['lease_minutes'])).isoformat(); run['lease_until']=until
        patch=dict(status_note='Сессия завершилась без сдачи — продолжена автоматически (%d)' % run['nudges'])
        if t.get('pipeline'):
            import orchestra, copy
            pipe=copy.deepcopy(t['pipeline'])
            orchestra.journal(s, pipe, ref, 'nudge', '%s: %s — сессия продолжена (%d-й раз)' % (run.get('role') or run['id'], reason, run['nudges']),
                              run.get('role'), run.get('stage'))
            patch['pipeline']=pipe
        write_patches(s, {ref: patch}); save('workflow.json',s)
        return run

def session_lost(ref, run_id, reason):
    """Сессия несколько раз закончилась без сдачи: роль — к Решале; Решала — вопрос владельцу;
    задача вне конвейера — провал запуска."""
    with locked():
        s=state(); at=now(); t=task_for(ref)
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref and r['status'] in ('queued','running')),None)
        if not run: return None
        run.update(status='failed', finished_at=at.isoformat(), evidence='Сессия завершалась без сдачи: '+reason)
        import orchestra, copy
        if run.get('role')=='resolver':
            prob=orchestra.problem(s, run.get('problem'))
            if prob:
                prob.update(status='owner', question='Решала не смог разобрать проблему: %s. Как поступить?' % prob['reason'][:600],
                            why_owner='Сессия Решалы %d раза завершилась без решения (%s)' % (3, reason), notified=False,
                            options=prob.get('options') or [])
            write_patches(s, {ref: dict(status='blocked', claimed_by=None, status_note='👤 Нужен владелец: Решала не справился с проблемой')})
        elif t.get('pipeline') and run.get('role') and run['role']!='teamlead' and orchestra.session_recipe(s, t, copy.deepcopy(t['pipeline']), run, at) is not None:
            pipe=copy.deepcopy(t['pipeline'])
            write_patches(s, {ref: orchestra.session_recipe(s, t, pipe, run, at)})   # Р3: роль снова в работе со списком
        elif t.get('pipeline') and run.get('role') and run['role']!='teamlead':
            pipe=copy.deepcopy(t['pipeline'])
            pipe['roles'].setdefault(orchestra.key(run['stage'], run['role']), {}).update(state='pending', feedback=None)
            prob=orchestra.escalate(s, t, pipe, at, '%s: %s' % (run['role'], reason), run['role'], run.get('agent'))
            write_patches(s, {ref: dict(pipeline=pipe, status='blocked', status_note='Проблема %s у Решалы: %s' % (prob['id'], reason[:300]), claimed_by=None)})
        else:
            write_patches(s, {ref: dict(status='queued', claimed_by=None, status_note='Сессия завершалась без сдачи: '+reason[:300])})
        save('workflow.json',s)
        return run

def start_plan(project, agents, sessions, tasks, runs, at):
    """«Запустить проект»: какие задачи бэклога и каким ИИ отдать одним нажатием.

    Кандидаты — готовые задачи вне конвейера и задачи конвейера, чья роль ждёт неоткрытую сессию.
    Число сессий — из объёма этого бэклога (per_ai) и не больше свободных мест под лимитом
    одновременных сессий; владелец может уменьшить. Порядок — приоритет, затем сколько открытых
    задач ждут эту, затем меньший размер. Две задачи с пересекающейся областью в пакет не попадают;
    назначенный ИИ соблюдается; сессии делятся между выбранными ИИ поровну."""
    mine=[t for t in tasks if t['project']==project]
    byref={t['ref']:t for t in mine}
    open_=[t for t in mine if t.get('status') not in ('done','cancelled')]
    waiting=Counter(d for t in open_ for d in t.get('depends_on') or [])
    cands=[]; skipped=[]
    for r in runs:
        t=byref.get(r['ref'])
        if t and r.get('role') and r['status']=='queued' and not r.get('launched_at') and not r.get('paused'):
            cands.append(dict(kind='role', task=t, run=r))
    piped={c['task']['ref'] for c in cands}
    for t in open_:
        if t.get('pipeline'):
            if t['ref'] not in piped:
                pipe=t['pipeline']
                skipped.append(dict(ref=t['ref'], title=t['title'], why='конвейер: %s' % (
                    'эскалация у тимлида' if pipe.get('blocked') else t.get('status_note') or 'роль уже работает')))
            continue
        why=('уже ждёт исполнителя' if t.get('queued_run') else
             'в работе' if t.get('status') in ('in_progress','review') and not (t['claim'] or {}).get('stale') else
             'заблокирована' if t.get('status')=='blocked' else
             'ждёт задачи ' + ', '.join(t['blocked_by']) if t['blocked_by'] else
             'нет плана (критерии, размер) — перенесите в «Идея», PO проработает' if t['needs_plan'] else
             'назначена ИИ %s — не выбран' % t['executor'] if t.get('executor') and t['executor'] not in agents else
             'занята другим ИИ' if not ready(t) else None)
        if why: skipped.append(dict(ref=t['ref'], title=t['title'], why=why))
        else: cands.append(dict(kind='task', task=t))
    key=lambda c:(PRIORITY_ORDER.get(c['task'].get('priority'),1), -waiting[c['task']['id']], SIZE_POINTS.get(c['task'].get('size'),3), c['task']['id'])
    cands.sort(key=key)
    points=sum(SIZE_POINTS.get(c['task'].get('size'),3) for c in cands)
    busy=open_sessions(runs, at); cap_total=settings()['max_sessions']; room=max(0, cap_total-busy)
    auto=min(len(cands), per_ai(points)*len(agents), room)
    limit=auto if sessions in (None,0) else min(int(sessions), len(cands))
    cap=-(-limit//len(agents)) if limit else 0
    load={a:0 for a in agents}; count={a:0 for a in agents}; picks=[]
    sprint_state=state()                  # только предпросмотр; применение под locked()
    for c in cands:
        if len(picks)>=limit: break
        t=c['task']
        hold=sprints.reason(sprint_state, t['ref'], tasks)
        if hold:
            skipped.append(dict(ref=t['ref'], title=t['title'], why=hold)); continue
        if c['kind']=='task' and (scope_conflict(t, tasks, runs, at) or any(t.get('scope') and p['scope'] and scopes_overlap(t['scope'], p['scope']) for p in picks)):
            skipped.append(dict(ref=t['ref'], title=t['title'], why='область пересекается с задачей в работе или в пакете')); continue
        pinned=t.get('executor') if c['kind']=='task' else None
        free=[a for a in ([pinned] if pinned else agents) if count[a]<cap]
        if not free: continue      # назначенный ИИ уже получил свою долю сессий — задача в следующий запуск
        free=[a for a in free if not limited(None, a, at)] or free     # ИИ с исчерпанным лимитом — последним
        if c['kind']=='role':      # у роли свой порядок ИИ (таблица ролей), в пределах выбранных
            import orchestra
            pref=orchestra.role_order(state(), t, c['run']['role'])
            free=sorted(free, key=lambda a:(limited(None, a, at), pref.index(a) if a in pref else 9))
        agent=free[0] if c['kind']=='role' else min(free, key=lambda a:(count[a], load[a], agents.index(a)))
        if c['kind']=='role':
            import orchestra
            route=orchestra.run_route(t, c['run'], agent)
            pick=dict(kind='role', run=c['run']['id'], role=c['run']['role'], stage=c['run'].get('stage'))
        else:
            try: packet(t['ref'], agent=agent)   # негодный паспорт не должен ронять весь пакет
            except ValueError as e:
                skipped.append(dict(ref=t['ref'], title=t['title'], why='паспорт: %s' % e)); continue
            route=recommendation(t, agent=agent); pick=dict(kind='task')
        load[agent]+=SIZE_POINTS.get(t.get('size'),3); count[agent]+=1
        sprints.admit(sprint_state, t['ref'], tasks)
        picks.append(dict(pick, ref=t['ref'], title=t['title'], size=t.get('size'), priority=t.get('priority'), agent=agent,
                          model=route['model'], effort=route['effort'], waiting=waiting[t['id']], scope=t.get('scope') or []))
    picked={p['ref'] for p in picks}
    later=[dict(ref=c['task']['ref'], title=c['task']['title']) for c in cands if c['task']['ref'] not in picked]
    n=len(cands)
    reason=('Кандидатов %d на %d очк. → по %d сессии на ИИ × %d ИИ = %d; свободно мест %d из %d (открыто %d)' % (
                n, points, per_ai(points), len(agents), per_ai(points)*len(agents), room, cap_total, busy) if n
            else 'Бэклог пуст' if not open_ else 'Запускать нечего: ' + '; '.join(sorted({x['why'].split(' — ')[0].split(':')[0] for x in skipped}))[:300])
    return dict(project=project, agents=agents,
                backlog=dict(open=len(open_), ready=n, points=points, blocked=sum(1 for t in open_ if t['blocked_by'] or t.get('status')=='blocked'),
                             needs_plan=sum(1 for t in open_ if t['needs_plan'] and not t.get('pipeline')),
                             in_work=sum(1 for t in open_ if t.get('status') in ('in_progress','review')),
                             pipeline=sum(1 for t in open_ if t.get('pipeline'))),
                per_ai=per_ai(points), auto_sessions=auto, sessions=len(picks), open_sessions=busy, max_sessions=cap_total,
                picks=[{k:v for k,v in p.items() if k!='scope'} for p in picks], later=later[:20], skipped=skipped[:60], reason=reason)

def split(project, agents, tasks, runs, at):
    """Объяснимое распределение: связные группы (зависимости, общая область)
    целиком уходят одному ИИ; группы раскладываются по наименее загруженному."""
    points={'S':1,'M':3,'L':8,'XL':20}
    mine=[t for t in tasks if t['project']==project and t.get('status') not in ('done','cancelled')]
    # Загрузка: назначенное ИИ плюс то, что он держит прямо сейчас.
    holder=lambda t:t.get('executor') or (t['claim'] or {}).get('agent')
    load={a:sum(points.get(t.get('size'),3) for t in mine if holder(t)==a) for a in agents}
    free=free_tasks(project, tasks)
    parent={t['ref']:t['ref'] for t in free}
    def find(x):
        while parent[x]!=x: parent[x]=parent[parent[x]]; x=parent[x]
        return x
    def union(a,b):
        if a in parent and b in parent: parent[find(a)]=find(b)
    for t in free:
        for dep in t.get('depends_on',[]): union(t['ref'], dep if '/' in dep else project+'/'+dep)
    for i,a in enumerate(free):
        for b in free[i+1:]:
            if a.get('scope') and b.get('scope') and scopes_overlap(a['scope'],b['scope']): union(a['ref'],b['ref'])
    groups={}
    for t in free: groups.setdefault(find(t['ref']),[]).append(t)
    # Закреплённые за ИИ задачи тянут к нему свою группу: цепочку не рвём.
    pinned={}; owner={t['ref']:t.get('executor') for t in mine if t.get('executor') in agents}
    for t in mine:
        for dep in t.get('depends_on',[]):
            ref=dep if '/' in dep else project+'/'+dep
            if t['ref'] in owner and ref in parent: pinned.setdefault(find(ref),owner[t['ref']])
            if ref in owner and t['ref'] in parent: pinned.setdefault(find(t['ref']),owner[ref])
    assign={}; plan=[]
    for key,group in sorted(groups.items(), key=lambda kv:(-sum(points.get(t.get('size'),3) for t in kv[1]), kv[0])):
        weight=sum(points.get(t.get('size'),3) for t in group)
        agent=pinned.get(key) or min(agents, key=lambda a:(load[a], agents.index(a)))
        load[agent]+=weight
        for t in group: assign[t['ref']]=agent
        plan.append(dict(agent=agent, points=weight, tasks=sorted(t['ref'] for t in group),
                         why='продолжает цепочку, уже закреплённую за этим ИИ' if key in pinned
                             else 'связная группа из %d задач (зависимости или общая область) не делится; наименьшая загрузка' % len(group) if len(group)>1
                             else 'наименьшая загрузка'))
    return dict(assign=assign, load=load, groups=plan)

def write_patches(s, patches):
    """Правки задач по слоям (TT-45): паспорт и итог — в ручной projects.json, ход
    исполнения — в слой tasks этого же workflow.json. Дерево, оценка и Obsidian читают
    оба слоя через taskstate.apply. Ручной файл переписывается, только если правка его
    касается, и раньше workflow.json: при сбое между записями ручной статус новее базы
    слоя и побеждает — задача не покажется незакрытой после записанного итога."""
    if not patches: return
    d=read('projects.json',{})
    if ts.commit(d, s.setdefault('tasks',{}), patches): save('projects.json',d)

def _learning_start(run, t, p):
    """Запуск разработчика (§5 п.2): конфигурация модели, ревизия ТЗ, хеш паспорта, выданные карточки.
    Только ссылки и хеши; сбой учёта не мешает взять работу (§15.5)."""
    try:
        import learning_events as le, learning_knowledge as lk
        cards=p.get('knowledge') or []
        run['learning']=dict(model_config_id=(p.get('route') or {}).get('model_config_id') or '%s:%s' % (run.get('agent'), run.get('model')),
                             spec_revision=le.spec_revision(t), context_digest=le.digest({k:v for k,v in p.items() if k!='knowledge'}),
                             knowledge_refs=[dict(id=c['id'], version=c.get('version')) for c in cards])
        if cards: lk.note_use(run['id'], t['ref'], cards)
    except Exception:
        pass

def input_hash(p):
    """Отпечаток входного паспорта: канонический JSON, sha256. Одинаковый вход — одинаковый отпечаток."""
    import hashlib
    return 'sha256:'+hashlib.sha256(json.dumps(p, ensure_ascii=False, sort_keys=True, default=str, separators=(',',':')).encode('utf-8')).hexdigest()


def replay(s, ref, run_id):
    """Воспроизведение запуска (07: A1-07): сохранённый вход, его отпечаток и проверка, след решений по запуску."""
    run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref),None)
    if not run: raise ValueError('Запуск не найден')
    p=run.get('packet') or {}
    stored=run.get('input_hash')
    t=next((x for x in snapshot()['tasks'] if x['ref']==ref),{})
    pipe=t.get('pipeline') or {}
    trace=[e for e in pipe.get('history') or [] if e.get('run')==run_id]
    return dict(ref=ref, run=run_id, role=run.get('role'), agent=run.get('agent'), model=run.get('model'),
                identity=run.get('identity'), route=p.get('route'), input=p, input_hash=stored,
                input_intact=None if not stored else stored==input_hash(p), status=run.get('status'),
                claimed_at=run.get('claimed_at'), finished_at=run.get('finished_at'), trace=trace,
                journal=[e for e in pipe.get('journal') or [] if run_id in (e.get('text') or '')][-20:])


def start_run(s, t, run, req, at):
    """Общее для claim и take: проверка ИИ, модели, областей; метка занятости."""
    model=req.get('model'); worker=req.get('worker')
    if not isinstance(worker,str) or not worker.strip(): raise ValueError('Укажите исполнителя/чат')
    agent=provider_of(model)
    if run['status']=='queued' and run.get('agent') and run['agent']!=agent:
        raise ValueError('Запуск поставлен для ИИ %s' % run['agent'])
    if req.get('agent') and req['agent']!=agent: raise ValueError('Модель %s не принадлежит ИИ %s' % (model, req['agent']))
    if t.get('executor') and t['executor']!=agent and not run.get('role') and not run.get('failover'):
        raise ValueError('Задача назначена ИИ %s; ИИ %s её не берёт' % (t['executor'], agent))
    extra_patch={}
    sprints.admit(s, t['ref'], snapshot()['tasks'])
    if run.get('role'):
        import orchestra
        pipe=orchestra.materialize(s, t)
        if pipe: t=dict(t, pipeline=pipe); extra_patch['pipeline']=pipe
        p=orchestra.role_packet(t, run, agent, toc=False)   # под блокировкой: без чтения артефактов из git
    elif run.get('failover') and run.get('packet'):
        p=dict(packet(t['ref'], agent=agent), route=run['packet']['route'])   # маршрут передачи, а не назначенного ИИ
    else:
        p=packet(t['ref'], agent=agent)
    if model!=p['route']['model']:
        raise ValueError('Фактическая модель должна совпадать с паспортом: %s' % p['route']['model'])
    conflict=scope_conflict(t, snapshot()['tasks'], s['runs'], at, worker)
    if conflict: raise ValueError('Область задачи пересекается с работой: '+conflict)
    minutes=settings(s)['lease_minutes']
    until=(at+timedelta(minutes=minutes)).isoformat()
    run.update(status='running',worker=worker,model=model,agent=agent,packet=p,phase=p['phase'],
               claimed_at=at.isoformat(),heartbeat_at=at.isoformat(),lease_until=until,
               input_hash=input_hash(p))   # отпечаток входа: по нему op=replay доказывает, на чём работал запуск (07: A1-07)
    if run.get('role')=='developer':
        _learning_start(run, t, p)
    provider_succeeded(s, agent, at)
    # Доработка — факт для метрик: изменение кода по итогам проверки, а не первая реализация.
    if not run.get('role') and p['phase'] not in ('review','assess'):
        rework='autocheck' if (t.get('auto_check') or {}).get('ok') is False else 'review' if (t.get('review') or {}).get('verdict')=='changes' else None
        if rework: run['rework']=rework
    if run.get('role'):
        import pipeline as P
        return dict(extra_patch, status='in_progress', status_note='%s: %s (%s)' % (P.ROLES[run['role']]['title'], provider_name(agent), worker),
                    claimed_by=dict(agent=agent, worker=worker, run=run['id'], model=model, lease_until=until, role=run['role']))
    note={'review':'Идёт ревью', 'assess':'Идёт оценка сложности'}.get(p['phase'],
          'Доработка' if (t.get('auto_check') or {}).get('ok') is False or (t.get('review') or {}).get('verdict')=='changes' else None)
    return dict(status='review' if p['phase']=='review' else 'in_progress', status_note=note and '%s: %s (%s)' % (note, provider_name(agent), worker),
                claimed_by=dict(agent=agent, worker=worker, run=run['id'], model=model, lease_until=until))

def new_run(s, ref, p, at):
    run=dict(id='RUN-'+str(len(s['runs'])+1),ref=ref,status='queued',packet=p,created_at=at.isoformat())
    if p: run['phase']=p['phase']
    s['runs'].append(run); return run

def worker_id(run, agent):
    """Имя исполнителя сессии, открытой сервером: одинаковое в claim, heartbeat и finish."""
    return '%s-cli:%s' % (agent, run['id'])

def launch_prompt(t, run, route, project, worker=None, worktree=None):
    """Задание для ИИ: первая реплика сессии, открытой с доски, или текст для вставки в чат."""
    ai=provider_name(route['provider']); ref=t['ref']; ws=workspace(t['project'], project)
    folder=(worktree or {}).get('path') or ws['active_copy'] or ws['path'] or 'не указана'
    lines=['Задача TaskTreker %s «%s» запущена для тебя (%s).' % (ref, t['title'], ai),
           'Рабочая папка: %s.' % folder]
    if worktree:
        lines.append('Это отдельная рабочая копия, ветка %s. Коммить в неё; в основную ветку не сливай — это решает владелец.' % worktree['branch'])
    if run.get('phase') not in ('review','assess') and ((t.get('auto_check') or {}).get('ok') is False or (t.get('review') or {}).get('verdict')=='changes'):
        lines.append('Это ДОРАБОТКА: начни с замечаний в паспорте (auto_check_feedback / review_feedback).')
    if run.get('phase')=='review':
        lines.append('Это РЕВЬЮ, не доработка: проверь каждый критерий приёмки, соответствие ТЗ и автопроверку по паспорту. Модель: %s, усилия %s.' % (route['model'], route['effort']))
    elif run.get('phase')=='assess':
        lines.append('Сначала оценка сложности, не реализация. Модель как есть: %s, усилия %s.' % (route['model'], route['effort']))
    else:
        lines.append('Модель: %s, усилия: %s. Если у тебя другая модель — переключись (/model) или сообщи.' % (route['model'], route['effort']))
    lines.append('1) Сразу прими запуск: tt_work op=claim ref=%s run=%s model=%s worker=%s' % (ref, run['id'], route['model'], worker or '<id этой сессии>'))
    lines.append('2) tt_work op=packet ref=%s agent=%s — и действуй строго по паспорту.' % (ref, route['provider']))
    lines.append('3) %s' % ('tt_work op=assess … с оценкой 1…5 и основаниями.' if run.get('phase')=='assess'
                            else 'tt_work op=review с verdict (approve, spec_change или changes), criteria по каждому критерию и conclusion. Код не меняй.' if run.get('phase')=='review'
                            else 'Если работа идёт дольше 40 минут — tt_work op=heartbeat. По окончании tt_work op=finish с результатами проверок и расходом токенов.'))
    if worker: lines.append('Твой worker во всех вызовах: %s.' % worker)
    lines.append(sprints.RULE + ' Состав: tt_work op=sprint.')
    lines.append(LIMIT_RULE % (ref, run['id'], worker or '<id этой сессии>'))
    if autonomous(): lines.append(AUTONOMY_RULE)
    if (run.get('failover') or {}).get('handover'): lines.append(run['failover']['handover'])
    lines.append('Задачу веди только через MCP-инструмент tt_work. Если его нет — команды системы: py "%s" work …; pm.py из рабочей копии задачи не используй: там может быть старый код.' % (ROOT / 'tools' / 'pm.py'))
    return '\n'.join(lines)

def mega(n):
    return '%.1f млн' % (n / 1e6) if n >= 1e6 else '%d тыс.' % round(n / 1e3)

def mark_budget(ref, run_id, tokens, budget):
    """Сторож бюджета: запуск обработал больше 150% ориентира токенов — отметка, журнал тимлида, колокольчик (Б7, Л4)."""
    with locked():
        s=state()
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref),None)
        if not run or run.get('budget_alert'): return False
        run['budget_alert']=dict(at=now().isoformat(), tokens=int(tokens), budget=budget)
        s.setdefault('teamlead',[]).append(dict(at=now().isoformat(), ref=ref, kind='budget', role=run.get('role'),
            text='Запуск %s обработал %s токенов — больше 150%% ориентира %s: проверьте, не ходит ли он по кругу' % (run_id, mega(tokens), mega(budget))))
        save('workflow.json',s)
        return True

def mark_launch_error(ref, run_id, message):
    """Автозапуск не удался (нет CLI, папки, лимит): причина на карточке, без повторов каждую минуту."""
    with locked():
        s=state()
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref),None)
        if run: run.update(launch_error=str(message)[:300], launch_error_at=now().isoformat()); save('workflow.json',s)

def mark_launched(ref, run_id, info):
    """Сессия открыта сервером: время, процесс, рабочая копия. Это ещё не принятие — его делает claim."""
    with locked():
        s=state()
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref and r['status']=='queued'),None)
        if not run: raise ValueError('Запуск не ждёт исполнителя')
        launches=list(run.get('launches') or [])
        launches.append(dict(info, at=now().isoformat()))
        run.update(launched_at=launches[-1]['at'], launches=launches[-5:], launch=launches[-1])
        save('workflow.json',s)
        return run


def reserve_sprint(ref):
    """До открытия процесса: атомарно занять место, общее для всех диспетчеров."""
    with locked():
        s=state(); tasks=snapshot()['tasks']
        if not any(t['ref']==ref for t in tasks): raise ValueError('Задача не найдена')
        sprints.admit(s, ref, tasks)
        save('workflow.json',s)


def confirm_main(s, t):
    """Старый поток: проверяем ветку задачи в main, а не строку evidence/status."""
    import subprocess
    if t.get('pipeline'):
        raise ValueError('В конвейере main подтверждает DoD этапа deploy_main')
    if t.get('status')!='done': raise ValueError('Сначала завершите проверку задачи')
    wt=task_branch(s,t['ref'])
    if not wt or not wt.get('path') or not wt.get('branch'):
        raise ValueError('Нет ветки исполнения: доведите задачу через конвейер до main')
    import pipeline
    project=read('projects.json',{})['projects'][t['project']]
    main=pipeline.branches(wt['path'],project)['main']
    def git(*args):
        return subprocess.run(['git',*args],cwd=wt['path'],capture_output=True,text=True,timeout=15)
    tip=git('rev-parse','--verify','refs/heads/'+wt['branch'])
    target=git('rev-parse','--verify','refs/heads/'+main)
    if tip.returncode or target.returncode or git('merge-base','--is-ancestor',tip.stdout.strip(),target.stdout.strip()).returncode:
        raise ValueError('Ветка задачи ещё не влита в '+main)
    receipt=dict(branch=wt['branch'], commit=tip.stdout.strip(), main=main, main_commit=target.stdout.strip(), at=now().isoformat())
    s.setdefault('main_receipts',{})[t['ref']]=receipt
    return receipt

def pause_patch(s, t, run, event, at, by, checkpoint=None):
    """Пауза запуска по исчерпанию лимита токенов: метка и запуск остаются за исполнителем
    до возобновления, аренда продлена до него — задачу никто не перехватит."""
    import limits
    when=limits.resume_at(event, at)
    run['paused']=dict(reason='limit', by=by, at=at.isoformat(), resume_at=when.isoformat(timespec='seconds'),
                       resets_at=(event or {}).get('resets_at'), kind=(event or {}).get('kind') or '',
                       message=(event or {}).get('message') or '', checkpoint=(checkpoint or '')[:2000],
                       session=(run.get('launch') or {}).get('session') or (event or {}).get('session'))
    agent=run.get('agent') or (run.get('launch') or {}).get('agent')
    if agent: note_limit(s, agent, when, at, (event or {}).get('kind') or '', t['ref'])
    patch=dict(status_note='⏸ Пауза: исчерпан лимит токенов %s — возобновление %s UTC с места остановки' % (
        PROVIDERS.get(agent,{}).get('name','ИИ'), when.strftime('%d.%m %H:%M')))
    if run['status']=='running':
        until=(when+timedelta(minutes=settings(s)['lease_minutes'])).isoformat()
        run['lease_until']=until
        patch['claimed_by']=dict(t.get('claimed_by') or {}, lease_until=until, paused_until=run['paused']['resume_at'])
    if t.get('pipeline'):
        import orchestra, copy
        pipe=copy.deepcopy(t['pipeline'])
        orchestra.journal(s, pipe, t['ref'], 'paused', '%s: пауза по лимиту токенов (%s), возобновление %s UTC%s' % (
            run.get('role') or run['id'], 'агент' if by=='agent' else 'сервер по журналу сессии', when.strftime('%d.%m %H:%M'),
            '; точка остановки: '+checkpoint[:200] if checkpoint else ''), run.get('role'), run.get('stage'))
        patch['pipeline']=pipe
    return patch

def relays(run):
    return len([p for p in (run.get('pauses') or []) + [run.get('paused') or {}] if p.get('relay')])

def relay_patch(s, t, run, at, by, checkpoint):
    """Эстафета (TT-91): пауза с возобновлением сразу и без сессии — сервер откроет новую сессию роли
    (не --resume) с точкой остановки. Запуск и метка остаются за исполнителем."""
    run['paused']=dict(reason='context', relay=True, relay_no=relays(run)+1, by=by, at=at.isoformat(),
                       resume_at=at.isoformat(timespec='seconds'), checkpoint=(checkpoint or '')[:3000], session=None,
                       old_session=(run.get('launch') or {}).get('session'))
    patch=dict(status_note='↻ Эстафета: контекст сессии перерос потолок — свежая сессия с точки остановки')
    if t.get('pipeline'):
        import orchestra, copy
        pipe=copy.deepcopy(t['pipeline'])
        orchestra.journal(s, pipe, t['ref'], 'relay', '%s: эстафета №%d (%s) — свежая сессия; точка остановки: %s' % (
            run.get('role') or run['id'], run['paused']['relay_no'], 'агент' if by=='agent' else 'сервер по журналу сессии',
            (checkpoint or '')[:200]), run.get('role'), run.get('stage'))
        patch['pipeline']=pipe
    return patch

def relay(ref, run_id, checkpoint, by='server'):
    """Эстафета по решению сервера: сессия закончилась, а её контекст выше потолка — продолжать её дороже,
    чем начать свежую. Возвращает паузу или None (не тот запуск, уже на паузе, эстафеты исчерпаны)."""
    with locked():
        s=state(); at=now()
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref and r['status'] in ('queued','running')),None)
        if not run or run.get('paused') or relays(run)>=MAX_RELAYS: return None
        write_patches(s, {ref: relay_patch(s, task_for(ref), run, at, by, checkpoint)})
        save('workflow.json',s); return run['paused']

def note_limit(s, agent, until, at, kind='', ref=None):
    """У ИИ исчерпан лимит токенов до until: новые запуски и передачи его обходят."""
    lim=s.setdefault('provider_limits',{})
    old=parse_time((lim.get(agent) or {}).get('until'))
    if not old or old < until:
        lim[agent]=dict(until=until.isoformat(timespec='seconds'), at=at.isoformat(), kind=kind, ref=ref)

def limited(s, agent, at=None):
    lim=((s if s is not None else state()).get('provider_limits') or {}).get(agent)
    until=parse_time(lim['until']) if lim else None
    return bool(until and until > (at or now()))


def provider_succeeded(s, agent, observed_at):
    """Подтверждённая работа снимает только предшествующий ей отказ провайдера."""
    # Запись отказа не удаляется сразу (FR-016 п.5): лимит считается прошедшим, провайдер —
    # восстанавливающимся (degraded), пока не наберётся 2 успеха с разрывом ≥60 с и ≥180 с после
    # сброса. Подтверждённое восстановление снимает запись в provider_balance.sync.
    entry = s.get('provider_limits', {}).get(agent)
    failed_at = parse_time(entry.get('at')) if entry else None
    if not entry or not failed_at or observed_at <= failed_at or observed_at > now():
        return False
    stamp = observed_at.isoformat()
    if stamp in (entry.get('oks') or []):
        return False                       # повторный callback того же успеха
    entry['oks'] = (list(entry.get('oks') or []) + [stamp])[-5:]
    until = parse_time(entry.get('until'))
    if until and until > observed_at:
        entry.update(until=stamp, cleared='успех после отказа: блокировка была устаревшей')
    s.setdefault('provider_recoveries', {}).setdefault(agent, dict(at=stamp, reason='успешная работа ИИ'))
    return True


def provider_sync(cli=None):
    """Режим провайдеров (FR-016): записать смену режима и снять подтверждённые восстановления."""
    import provider_balance as pb
    with locked():
        s = state()
        before = ((s.get('provider_modes') or [{}])[-1].get('mode'), json.dumps(s.get('provider_limits') or {}, sort_keys=True))
        pb.sync(s, now(), cli)
        if ((s.get('provider_modes') or [{}])[-1].get('mode'), json.dumps(s.get('provider_limits') or {}, sort_keys=True)) != before:
            save('workflow.json', s)
        return s['provider_modes'][-1]['mode']


def mark_provider_available(agent, observed_at):
    with locked():
        s = state()
        changed = provider_succeeded(s, agent, observed_at)
        if changed:
            save('workflow.json', s)
        return changed

def cli_ok(agent):
    try:
        import launcher
        return bool(launcher.find_cli(agent))
    except Exception:   # noqa: BLE001
        return True

def handover_text(src, run):
    pz=run.get('paused') or {}
    return ('Передача от %s: у него исчерпан лимит токенов (до %s UTC). %s' % (
        PROVIDERS.get(src,{}).get('name',src), (pz.get('resume_at') or '?')[:16].replace('T',' '),
        'Точка остановки: '+pz['checkpoint'] if pz.get('checkpoint') else
        'Продолжи с текущего состояния рабочей копии и ветки задачи: незакоммиченные правки предыдущего ИИ лежат там же — проверь git status и git log.'))

def failover(s, t, run, at):
    """Лимит токенов у ИИ запуска: задача переходит к другому ИИ с моделью и усилиями под роль
    и сложность. Нет свободного ИИ или передача выключена — None (запуск ждёт сброса лимита)."""
    if not settings(s).get('failover', True) or run['status'] not in ('queued','running'): return None
    src=run.get('agent') or (run.get('launch') or {}).get('agent')
    if src=='local_mimo': return None      # пилот LEARN-02: разработчик local_mimo не передаётся GPT/Claude (REQ-DEV-02/03)
    import orchestra, copy, provider_balance as pb
    # Повтор того же события (TC-49): запуск уже передан — второго нового запуска нет.
    if any((r.get('failover') or {}).get('run')==run['id'] and r['status'] in ('queued','running') for r in s['runs']): return None
    dec=pb.choose(s, t, run.get('role'), at, exclude=(src,), cli=cli_ok)
    to=dec['selected'] if dec['selected'] in dec['eligible'] else None
    if not to: return None
    text=handover_text(src, run)
    run['pauses']=(run.get('pauses') or [])[-9:]+([dict(run.pop('paused'), handed_to=to)] if run.get('paused') else [])
    run.update(status='cancelled', finished_at=at.isoformat(), evidence='Передано %s: %s' % (PROVIDERS[to]['name'], text))
    info=dict(from_agent=src, run=run['id'], handover=text, at=at.isoformat())
    if run.get('role'):
        route=orchestra.run_route(t, run, to)
        new=new_run(s, t['ref'], None, at); dec['run_id']=new['id']
        new.update(role=run['role'], stage=run.get('stage'), gate=run.get('gate'), phase='role', agent=to, auto_launch=True,
                   packet=dict(route=route, phase='role'), failover=info)
        if run.get('dual'): new['dual']=run['dual']             # второе ревью CRITICAL остаётся вторым мнением
        if run.get('problem'):
            new['problem']=run['problem']
            prob=orchestra.problem(s, run['problem'])
            if prob: prob['run']=new['id']
        pipe=copy.deepcopy(t['pipeline']) if t.get('pipeline') else None
        note='Передано %s: лимит токенов у %s' % (PROVIDERS[to]['name'], PROVIDERS.get(src,{}).get('name',src))
        if pipe and run['role'] not in ('teamlead','resolver'):
            k=orchestra.key(run['stage'], run['role']); e=pipe['roles'].setdefault(k, {})
            e.update(run=new['id'], state='queued', agent=to, feedback=list(e.get('feedback') or [])+[dict(id='handover', text='Передача между ИИ', detail=text)])
        if pipe:
            orchestra.journal(s, pipe, t['ref'], 'failover', '%s: %s → %s (%s · %s) — лимит токенов у %s' % (
                run['role'], PROVIDERS.get(src,{}).get('name',src), PROVIDERS[to]['name'], route['model'], route['effort'],
                PROVIDERS.get(src,{}).get('name',src)), run['role'], run.get('stage'))
        if run['role']=='resolver':      # задача остаётся заблокированной: проблему разбирает Решала на другом ИИ
            patch=dict(status_note='Решала: '+note, **({'pipeline':pipe} if pipe else {}))
        else:
            patch=dict(pipeline=pipe, status='queued', claimed_by=None, status_note=note)
    else:
        route=recommendation(dict(t, executor=to, model=None), agent=to)
        new=new_run(s, t['ref'], dict(packet(t['ref'], agent=to), route=route), at)
        new.update(agent=to, auto_launch=True, failover=info)
        patch=dict(status='queued', claimed_by=None, status_note='Передано %s (%s · %s): лимит токенов у %s' % (
            PROVIDERS[to]['name'], route['model'], route['effort'], PROVIDERS.get(src,{}).get('name',src)))
    return dict(run=new['id'], agent=to, model=route['model'], effort=route['effort'], patch=patch)

def emergency_stop(s, at, origin, reason):
    """Аварийная остановка: каждый открытый запуск — на паузу с точкой остановки, незакоммиченные
    правки рабочей копии сохраняются коммитом WIP. Процессы завершает action после записи состояния.
    Неоткрытые запуски не трогаются: при остановке их не откроет ни сервер, ни take/claim."""
    import orchestra
    tasks={t['ref']:t for t in snapshot()['tasks']}
    out=[]; patches={}
    who='владелец' if origin==oa.OWNER else 'агент'
    for r in s['runs']:
        if r.get('status') not in ('queued','running') or (r['status']=='queued' and not r.get('launched_at')): continue
        t=tasks.get(r['ref']) or {}; launch=r.get('launch') or {}
        wip=orchestra.autocommit(t.get('pipeline') or dict(worktree=launch.get('worktree')), r, 'WIP при аварийной остановке')
        r['paused']=dict(reason='kill_switch', by=origin, at=at.isoformat(), resume_at=None, wip=wip,
                         checkpoint=('Аварийная остановка (%s): %s. Правки сохранены %s.' % (
                             who, reason, 'коммитом '+wip if wip else 'в рабочей копии (незакоммиченного не было)'))[:2000],
                         session=(r.get('paused') or {}).get('session') or launch.get('session'), before=r.get('paused'))
        if r['status']=='running': r['lease_until']=(at+timedelta(days=30)).isoformat()   # метку никто не перехватит
        patches[r['ref']]=dict(status_note='⛔ Аварийная остановка: '+reason[:200])
        out.append(dict(run=r['id'], ref=r['ref'], pid=launch.get('pid'), wip=wip))
    write_patches(s, patches)
    return out

def failover_run(ref, run_id):
    """Сервер: после паузы по лимиту — передать задачу другому ИИ, если он свободен."""
    with locked():
        s=state(); at=now(); t=task_for(ref)
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref),None)
        if not run or not run.get('paused'): return None
        reason = run['paused'].get('reason')
        if reason != 'limit' and not (reason == 'context' and limited(s, run.get('agent'), at)): return None
        pid=(run.get('launch') or {}).get('pid')
        wip=None
        if pid:
            # TTU-009: один писатель на рабочую копию. Незакоммиченное прежнего ИИ — коммитом,
            # его процесс закрывается после записи передачи.
            import orchestra
            wip=orchestra.autocommit(t.get('pipeline') or dict(worktree=(run.get('launch') or {}).get('worktree')), run, 'WIP перед передачей другому ИИ')
        out=failover(s, t, run, at)
        if not out: return None
        if wip: out['wip']=wip
        write_patches(s, {ref: out.pop('patch')}); save('workflow.json',s)
    if pid:
        import launcher
        if launcher.alive(pid): launcher.terminate(pid)
    return out

def reroute_queued(ref, run_id, unavailable=False):
    """Ждущий, ещё не открытый запуск закреплён за ИИ на лимите — сменить ИИ в самом запуске.

    Работы ещё не было, передавать нечего: ни нового запуска, ни отмены, ни заметки о передаче.
    (Раньше здесь создавался новый запуск — при чередовании лимитов запуски множились сотнями.)"""
    with locked():
        s=state(); at=now(); t=task_for(ref)
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref and r['status']=='queued' and not r.get('launched_at')),None)
        if not run: return None
        src=run.get('agent')
        if src=='local_mimo': return src      # без failover: ждёт свой runtime
        if not unavailable and not limited(s, src, at): return src
        if not settings(s).get('failover', True): return None
        import orchestra, provider_balance as pb
        dec=pb.choose(s, t, run.get('role'), at, exclude=(src,), cli=cli_ok, run_id=run['id'])
        to=dec['selected'] if dec['selected'] in dec['eligible'] else None
        if not to: return None
        patch={}
        if run.get('role'):
            route=orchestra.run_route(t, run, to)
            if t.get('pipeline') and run['role'] not in ('teamlead','resolver'):
                import copy
                pipe=copy.deepcopy(t['pipeline'])
                e=(pipe.get('roles') or {}).get(orchestra.key(run.get('stage'), run['role']))
                if e and e.get('run')==run['id']:
                    e['agent']=to; patch['pipeline']=pipe
        else:
            route=recommendation(dict(t, executor=to, model=None), agent=to)
            run['failover']=dict(from_agent=src, inplace=True, handover='')   # пропуск назначения задачи прежнему ИИ
        run['packet']=dict(run.get('packet') or {}, route=route)
        run['agent']=to
        run['repins']=(run.get('repins') or [])[-9:]+[dict(at=at.isoformat(), from_agent=src, to=to,
            by='CLI недоступен' if unavailable else 'лимит токенов')]
        run.pop('launch_error', None)
        run.pop('launch_error_at', None)
        if patch: write_patches(s, {ref: patch})
        save('workflow.json',s)
        return to

def environment_requests(s):
    """Команды развёртывания окружений из projects.json, которые владелец ещё не одобрил (или они изменились)."""
    import ci
    out=[]
    for key,p in (read('projects.json',{}).get('projects') or {}).items():
        for env,d in ((ci.config(p).get('deploy') or {}).items()):
            if not isinstance(d,dict) or not d.get('cmd'): continue
            ok,why=rp.environment_ok(dict(s, policy={}), key, env, d)
            if not ok:
                fp=rp.env_fingerprint(d)
                out.append(dict(id='e:%s/%s:%s' % (key,env,fp), kind='environment', ref=None, title='Окружение %s · %s' % (key,env),
                                text='%s. Команда: %s%s' % (why, ' '.join(map(str,d['cmd']))[:300], ('; проверка: '+' '.join(map(str,d['health']))[:200]) if d.get('health') else ''),
                                env_key=rp.env_key(key,env), fingerprint=fp, at=None))
    return out

def notifications(s, tasks):
    """Колокольчик: что ждёт именно владельца. Вопросы Решалы, релизы в пошаговом режиме, сбои запуска."""
    out=[]; byref={t['ref']:t for t in tasks}
    for p in s.get('problems') or []:
        if p.get('status')=='owner':
            out.append(dict(id='q:'+p['id'], kind='question', ref=p['ref'], title=p.get('title') or byref.get(p['ref'],{}).get('title',''),
                            text=p.get('question'), why=p.get('why_owner'), options=[o.get('title') for o in p.get('options') or []],
                            problem=p['id'], at=p.get('decided_at') or p['at']))
    k=rp.killed(s)
    if k:
        out.append(dict(id='k:'+(k.get('at') or ''), kind='kill_switch', ref=None, title='Аварийная остановка',
                        text=rp.kill_reason(s), at=k.get('at')))
    grant,why=rp.active_grant(s, '*', 'main', now())
    if autonomous(s) and not grant:
        out.append(dict(id='g:grant', kind='grant', ref=None, title='Полная автономия без гранта',
                        text='Релизы в main ждут вашего «да»: %s. Выключите и включите полную автономию заново.' % why,
                        at=(settings(s).get('autonomy_changed') or {}).get('at')))
    import operation_ids as oi
    for op in oi.pending(s):
        out.append(dict(id='o:'+op['id'], kind='reconcile', ref=op.get('ref'), title='Прерванная операция '+(op.get('check') or ''),
                        text='%s: процесс прервался во время действия, результат по Git не проверить. Проверьте сами: повторить или признать выполненной.' % op['id'],
                        operation=op['id'], at=op.get('at')))
    out.extend(environment_requests(s))
    at=now()
    for ref,b in ((s.get('delivery_sprints') or {}).get('blocked') or {}).items():
        due=parse_time(b.get('recheck_at'))
        if due and due<=at:     # FR-014 п.5: блокировка не прячется — срок перепроверки прошёл
            out.append(dict(id='fb:%s:%s' % (ref, b.get('recheck_at')), kind='flow_block', ref=ref, title=byref.get(ref,{}).get('title',''),
                            text='Внешняя блокировка с %s: %s. Ответственный: %s. Срок перепроверки прошёл — слот задачи по-прежнему занят.'
                                 % ((b.get('since') or '')[:10], b.get('reason'), b.get('responsible')), at=b.get('recheck_at')))
    for t in tasks:
        pipe=t.get('pipeline') or {}
        # Ждёт «да», пока нет подписанного одобрения: неподписанная (ручная или старая) запись не считается.
        if pipe.get('stage')=='deploy_main' and (pipe.get('gates') or {}).get('release',{}).get('decision')=='approve'                 and not oa.verify((pipe.get('approvals') or {}).get('release')):
            out.append(dict(id='r:'+t['ref'], kind='release', ref=t['ref'], title=t['title'],
                            text='Ворота тимлида «тест → main» пройдены — нужен ваш «да» на релиз в main', at=pipe['gates']['release'].get('at')))
        op_=pipe.get('oracle_pending')
        if op_:
            out.append(dict(id='q:%s:%s' % (t['ref'], op_.get('at')), kind='oracle', ref=t['ref'], title=t['title'],
                            text=op_.get('detail') or 'Разработчик изменил эталон приёмки', at=op_.get('at'),
                            why='эталон приёмки задаёт, что считать готовым; агент, который его меняет, проверяет сам себя'))
    for r in s.get('runs') or []:
        b=r.get('budget_alert')
        if b and r.get('status') in ('queued','running'):
            out.append(dict(id='b:'+r['id'], kind='budget', ref=r['ref'], title=byref.get(r['ref'],{}).get('title',''),
                            text='Запуск %s (%s) обработал %s токенов при ориентире %s — сессии напомнено завершить этап'
                                 % (r['id'], r.get('role') or 'исполнитель', mega(b.get('tokens') or 0), mega(b['budget'])), run=r['id'], at=b['at']))
        if r.get('status') in ('queued','running') and r.get('launch_error') and not r.get('paused'):
            out.append(dict(id='l:'+r['id'], kind='launch', ref=r['ref'], title=byref.get(r['ref'],{}).get('title',''),
                            text=r['launch_error'], run=r['id'], at=r.get('launch_error_at')))
    return sorted(out, key=lambda n: n.get('at') or '', reverse=True)

def note_session(ref, run_id, session):
    """Найденный id сессии Codex — в запуск: по нему продолжение после лимита и токены агента."""
    with locked():
        s=state()
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref),None)
        if not run or (run.get('launch') or {}).get('session'): return
        run['launch']=dict(run.get('launch') or {}, session=session)
        if run.get('launches'): run['launches'][-1]=dict(run['launches'][-1], session=session)
        save('workflow.json', s)

def mark_notified(ids):
    """Какие уведомления владельцу уже отправлены системным уведомлением — повторно не шлются."""
    with locked():
        s=state(); s['notified']=(list(s.get('notified') or []) + [i for i in ids if i not in (s.get('notified') or [])])[-500:]
        save('workflow.json', s)

def mark_paused(ref, run_id, event):
    """Сервер нашёл в журнале сессии отказ по лимиту: пауза до сброса лимита."""
    with locked():
        s=state(); at=now()
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref and r['status'] in ('queued','running')),None)
        if not run or run.get('paused'): return None
        write_patches(s, {ref: pause_patch(s, task_for(ref), run, event, at, 'server')})
        save('workflow.json',s); return run['paused']

def resume_due(at=None):
    at=at or now()
    s = state()
    if rp.killed(s): return []          # аварийная остановка: ничего не продолжаем
    return [r for r in s['runs'] if r.get('status') in ('queued','running') and r.get('paused')
            and ((parse_time(r['paused'].get('resume_at')) or at) <= at
                 or (r['paused'].get('reason') == 'limit' and not limited(s, r.get('agent'), at)))]

def mark_resumed(ref, run_id, info, by='server'):
    """Сессия продолжена после паузы: пауза уходит в историю, аренда — обычная."""
    with locked():
        s=state(); at=now(); t=task_for(ref)
        run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref and r.get('paused')),None)
        if not run: return None
        done=dict(run.pop('paused'), resumed_at=at.isoformat(), resumed_by=by, resumed_session=bool((info or {}).get('resumed')))
        run['pauses']=(run.get('pauses') or [])[-9:]+[done]
        patch=dict(status_note='%s: %s' % ('Эстафета — свежая сессия' if done.get('relay') else 'Возобновлено после лимита токенов',
                                           run.get('role') or run.get('phase') or 'исполнение'))
        if run['status']=='running':
            until=(at+timedelta(minutes=settings(s)['lease_minutes'])).isoformat()
            run.update(lease_until=until, heartbeat_at=at.isoformat())
            cb=dict(t.get('claimed_by') or {}, lease_until=until); cb.pop('paused_until',None); patch['claimed_by']=cb
        if info:
            launches=list(run.get('launches') or []); launches.append(dict(info, at=at.isoformat()))
            run.update(launches=launches[-5:], launch=dict(run.get('launch') or {}, **info))
        if t.get('pipeline'):
            import orchestra, copy
            pipe=copy.deepcopy(t['pipeline'])
            orchestra.journal(s, pipe, ref, 'resumed', '%s: работа возобновлена %s (%s)' % (
                run.get('role') or run['id'], 'эстафетой' if done.get('relay') else 'после лимита токенов',
                'та же сессия' if (info or {}).get('resumed') else 'новая сессия' if info else by),
                run.get('role'), run.get('stage'))
            patch['pipeline']=pipe
        write_patches(s, {ref: patch}); save('workflow.json',s)
        return done

LIMIT_RULE=('Лимит токенов: если пришло предупреждение, что лимит почти исчерпан, — закоммить незавершённое в ветку задачи '
            '(WIP), затем tt_work op=pause ref=%s run=%s worker=%s checkpoint="что сделано, что осталось, следующий шаг" '
            'и остановись. Сервер сам продолжит эту же сессию после сброса лимита (время сброса — из журнала сессии, '
            'без него — через 5 часов). Если лимит оборвал тебя без предупреждения, сервер увидит это в журнале сам. '
            'После «продолжай» не начинай заново: продолжи с точки остановки.')

def resume_prompt(t, run):
    """Первая реплика после паузы: та же сессия продолжает, новая — получает точку остановки."""
    pz=run.get('paused') or {}; worker=run.get('worker') or worker_id(run, run.get('agent') or 'claude')
    lines=['Эстафета: прежняя сессия этой роли переросла потолок контекста, это новая сессия с пустым контекстом. '
           'Продолжай задачу TaskTreker %s с точки остановки; читай только нужное — по указателям паспорта.' % t['ref']
           if pz.get('relay') else 'Лимит токенов восстановлен — продолжай задачу TaskTreker %s с места остановки.' % t['ref']]
    if pz.get('checkpoint'): lines.append('Точка остановки: %s' % pz['checkpoint'])
    if run['status']=='running':
        lines.append('Запуск %s уже твой (worker %s): claim не нужен. Сначала tt_work op=heartbeat ref=%s run=%s worker=%s, '
                     'затем tt_work op=packet ref=%s%s — проверь, что изменилось, и продолжай.' % (
                         run['id'], worker, t['ref'], run['id'], worker, t['ref'], ' run='+run['id'] if run.get('role') else ''))
    else:
        lines.append('Запуск %s ещё не принят: выполни задание из первой реплики — начни с tt_work op=claim.' % run['id'])
    return '\n'.join(lines)

def task_branch(s, ref):
    """Рабочая копия и ветка последнего запуска исполнения задачи."""
    for r in reversed(s['runs']):
        wt=(r.get('launch') or {}).get('worktree')
        if r['ref']==ref and wt and r.get('phase')!='review': return wt
    return None

def reviewer_for(s, implementer):
    choice=settings(s)['reviewer']
    if choice=='same' or not implementer: return implementer or settings(s)['default_executor']
    if choice=='other': return next((p for p in PROVIDERS if p!=implementer), implementer)
    return choice

def spawn_autocheck(ref, run_id):
    """Автопроверка — отдельным процессом: тесты проекта идут минутами, блокировку не держим."""
    if os.environ.get('TASKTREKER_AUTOCHECK_SYNC')=='1':
        return autocheck_run(ref, run_id)
    import subprocess, sys
    logs=ROOT/'ops'/'logs'; logs.mkdir(parents=True, exist_ok=True)
    flags=(subprocess.DETACHED_PROCESS|subprocess.CREATE_NO_WINDOW) if os.name=='nt' else 0
    with open(logs/'autocheck.log','a',encoding='utf-8') as log:
        subprocess.Popen([sys.executable, str(Path(__file__)), 'autocheck', ref, '--run', run_id],
                         env=dict(os.environ, TASKTREKER_DATA=data_dir().as_posix()), stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, creationflags=flags, cwd=str(ROOT))

def autocheck_run(ref, run_id):
    """Проверить ветку задачи и поставить ревью ИИ (или вернуть на доработку)."""
    import autocheck
    s=state(); t=task_for(ref)
    run=next((r for r in s['runs'] if r['id']==run_id and r['ref']==ref),None)
    wt=task_branch(s, ref)
    if not run or not wt: raise ValueError('Нет ветки задачи для автопроверки')
    project=read('projects.json',{}).get('projects',{}).get(t['project'],{})
    ws=workspace(t['project'],project); repo=ws['active_copy'] or ws['path']
    base=wt.get('base') or autocheck._git(['rev-parse','--abbrev-ref','HEAD'],repo).stdout.strip()
    verify=project.get('verify')
    if not verify:                         # SEC-003: без verify — шаг тестов CI проекта (ci или автоопределение)
        import ci
        step=next((st for st in ci.config(dict(project, path=wt['path'])).get('steps') or [] if st.get('kind','test')=='test'), None)
        verify=step and [ci._subst(a, dict(repo=wt['path'], branch=wt['branch'])) for a in step.get('cmd') or []]
    result=autocheck.run_checks(repo, wt['path'], wt['branch'], base, t.get('scope'), verify, verify_na=project.get('verify_na'))
    result['run']=run_id
    with locked():
        s=state(); at=now()
        patch=dict(auto_check=result)
        for r in s['runs']:   # итог проверки — и в запуске: история проверок для метрик
            if r['id']==run_id and r['ref']==ref:
                r['autocheck']=dict(ok=bool(result['ok']), at=at.isoformat(),
                                    checks=[dict(name=c.get('name'), ok=bool(c.get('ok'))) for c in result.get('checks') or []])
        if result['ok']:
            agent=reviewer_for(s, run.get('agent'))
            route=recommendation(dict(t, status='review'), agent=agent)
            rv=new_run(s, ref, dict(route=route, phase='review'), at)
            rv.update(agent=agent, auto_launch=settings(s)['auto_launch_review'])
            patch.update(status='review', status_note='Автопроверка пройдена — ждёт ревью ИИ (%s)' % rv['id'])
        else:
            failed='; '.join('%s: %s' % (c['name'], c['detail'][:120]) for c in result['checks'] if not c['ok'])
            patch.update(status='queued', status_note='Автопроверка не пройдена — доработка: '+failed)
        write_patches(s, {ref: patch}); save('workflow.json', s)
    return result

def record_spec_change(t, spec, review_record, at):
    """Отклонение от ТЗ: решение в памяти и задача на изменение ТЗ в бэклоге."""
    import memory as mem
    title='Отклонение от ТЗ в %s: %s' % (t['id'], spec['deviation'][:120])
    rec=mem.new_record(title, 'Решение в коде: %s\n\nПредложение для ТЗ: %s\n\nРевью: %s' % (spec['deviation'], spec['proposal'], review_record['conclusion']),
                       project=t['project'], kind='решение', root=t.get('root') or '', parent=t.get('root') or '', source='review:'+t['ref'])
    mem.save(rec); mem.build_index()
    d=read('projects.json',{}); project=d['projects'][t['project']]
    task=dict(id=next_id(project), title=('Изменить ТЗ по решению в %s' % t['id'])[:200], why=spec['deviation'][:1000],
              root=t.get('root') or rec['id'], size='S', priority='🟡', risk='low', task_type='analysis_spec', work_type='docs',
              direction='аналитика', question=spec['deviation'][:2000], inputs=('Ревью %s: %s' % (t['id'], review_record['conclusion']))[:2000],
              deliverable=(t.get('spec_ref') or 'ТЗ проекта')[:2000], depends_on=[],
              acceptance=['ТЗ дополнено: %s' % spec['proposal'][:300], 'В ТЗ есть ссылка на решение %s и задачу %s' % (rec['id'], t['id'])],
              status='queued', source='review:'+t['ref'])
    task['model_plan']=model_plan(task)
    project.setdefault('backlog',[]).append(task); save('projects.json', d)
    return task['id'], rec['id']

def is_text(limit):
    return lambda v, t: isinstance(v, str) and bool(v.strip()) and len(v) <= limit

def is_criteria(v, t):
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x.strip() for x in v)

def root_of_project(v, t):
    return any(r.get('id') == v and r.get('project') == t['project'] for r in read('memory/index.json', {}).get('records', []))

# I4: что можно менять op=update и как проверяется значение — по строке на поле:
# поле -> (проверка значения (value, задача) -> bool, сообщение). Проверка может и сама
# бросить ValueError со своим текстом. None — поле проверяется вместе со всей задачей
# (validate_details, recommendation, model_plan): его допустимость зависит от других полей.
# В каком слое поле живёт — taskstate.OPS; здесь только проверка.
UPDATE_FIELDS = {
    'title':           (is_text(1000), 'Заполните title'),
    'why':             (is_text(1000), 'Заполните why'),
    'root':            (root_of_project, 'Корень должен принадлежать проекту'),
    'new_root':        (lambda v, t: validate_new_root(dict(new_root=v)) is None, None),
    'priority':        (lambda v, t: v in PRIORITIES, 'Неверный приоритет'),
    'risk':            (lambda v, t: v in RISKS, 'Неверный риск'),
    'work_type':       (lambda v, t: v in WORK_TYPES, 'Неверный вид работы'),
    'acceptance':      (is_criteria, 'Нужны критерии'),
    'assessment':      (lambda v, t: not v or cx.validate_assessment(v) is None, None),
    'model_plan':      (lambda v, t: isinstance(v, dict) and not set(v) - set(PROVIDERS), 'model_plan: только ' + ', '.join(PROVIDERS)),
    'model':           (None, None),
    'model_reason':    (None, None),
    'acceptance_done': (None, None),
    'due_date':        (None, None),
    'executor':        (None, None),
    'scope':           (None, None),
    'task_type':       (None, None),
    **{k: (None, None) for k in TYPE_FIELDS},
}
# Правила, связывающие поля одной правки.
UPDATE_RULES = (
    (lambda c: 'new_root' in c and 'root' in c, 'Укажите либо существующий запрос, либо новый'),
    (lambda c: c.get('model') and not c.get('model_reason'), 'Обоснуйте выбор модели'),
)

def update_patch(t, changes, at):
    """op=update: поля — по таблице UPDATE_FIELDS, связи — по UPDATE_RULES, затем вся
    задача после правки. -> (правка задачи, новый запрос или None)."""
    if not isinstance(changes, dict) or set(changes) - set(UPDATE_FIELDS): raise ValueError('Недопустимые поля изменения')
    for key, value in changes.items():
        check, error = UPDATE_FIELDS[key]
        if check and not check(value, t): raise ValueError(error)
    for broken, error in UPDATE_RULES:
        if broken(changes): raise ValueError(error)
    changes = dict(changes); new_root = changes.pop('new_root', None)
    if changes.get('assessment'):
        changes['assessment'] = dict(changes['assessment'], reasons=changes['assessment']['reasons'].strip(), by=dict(agent='владелец'), at=at.isoformat())
    if 'acceptance' in changes and changes['acceptance'] != t.get('acceptance'):
        changes['acceptance_done'] = []   # Переформулировка требует новой проверки.
    if 'model_plan' in changes:
        # Частичная правка: строка ИИ заменяется, None возвращает автоматический выбор.
        merged = dict(t.get('model_plan') or {})
        for k, v in changes['model_plan'].items():
            if v is None: merged.pop(k, None)
            else: merged[k] = dict(v, source='manual')
        changes['model_plan'] = merged
    after = dict(t, **changes)
    validate_details(after); recommendation(after)
    changes['model_plan'] = model_plan(after)
    return changes, new_root

def expire(s, ref, at, by):
    """Просроченная метка: запуск закрывается как expired, история остаётся."""
    for r in s['runs']:
        if r['ref']==ref and r['status']=='running' and not lease_alive(r, at):
            r.update(status='expired', finished_at=at.isoformat(),
                     evidence='Аренда истекла без heartbeat; задачу перехватил '+by)
            return r

OWNER_ONLY = ('Это решение владельца: оно принимается только на доске TaskTreker, открытой ссылкой владельца '
              '(py tools/pm.py owner-link в своём терминале)')
# Остановка разрешена при аварийной остановке: она только сужает действия или не начинает новых.
STOP_ALLOWED = {'flow_block','flow_unblock','reconcile','pause','cancel','fail','stop_all','resume_all','stop_project','settings','status','plan','update','focus','confirm_main'}

def needs_owner(req):
    """Решения владельца (TT3-014, TTU-018): политика, релиз, ответы, массовые действия."""
    op=req.get('op')
    if op=='learn':
        import learn
        return req.get('action') in learn.OWNER_ACTIONS
    if op=='security': return req.get('action')=='triage'
    if op in ('approve_release','approve_oracle','answer','resume_all','reconcile','release_slot','flow_exception'): return True
    if op=='settings': return not req.get('preview')
    if op in ('to_idea','start'): return not req.get('dry_run', True)
    return False

READ_OPS = {'adaptive','balance','flags','flow','sprint','list','models','queue','packet','doc','prompt','route','next','replay'}

def action(req, origin=None):
    """Запрос с operation_id идемпотентен (TT3-017): повтор возвращает прежний ответ и ничего
    не выполняет второй раз; другой запрос под тем же id и одновременный второй — отказ."""
    if isinstance(req, dict) and (origin or oa.AGENT)!=oa.OWNER and needs_owner(req):
        import sec_audit                     # попытка агента сделать действие владельца — в журнал с цепочкой (A2-08)
        sec_audit.record('owner_only_denied', 'op=%s action=%s changes=%s' % (req.get('op'), req.get('action'), sorted((req.get('changes') or {}).keys())),
                         run=req.get('run'), agent=req.get('agent') or req.get('worker'), op=req.get('op'), ref=req.get('ref'))
        raise ValueError(OWNER_ONLY)
    if not isinstance(req, dict) or 'operation_id' not in req or req.get('op','list') in READ_OPS:
        return _action(req, origin)
    import operation_ids as oi
    req=dict(req); op_id=req.pop('operation_id')
    if (origin or oa.AGENT)!=oa.OWNER and needs_owner(req): raise ValueError(OWNER_ONLY)
    key, cached = oi.claim_request(op_id, req, origin or oa.AGENT)
    if cached is not None:
        return dict(cached, replay=True, operation_id=op_id)
    try:
        out=_action(req, origin)
    except BaseException:
        oi.release_request(key); raise
    oi.done_request(key, out)
    return out

def _action(req, origin=None):
    """Единая точка изменения оперативного слоя. origin — кто просит: oa.OWNER только у доски
    с токеном владельца (serve.py); CLI, MCP и любой вызов без origin — агент."""
    origin=origin or oa.AGENT
    if origin not in oa.ORIGINS: raise ValueError('Неизвестный источник запроса')
    if not isinstance(req, dict): raise ValueError('Запрос — объект')
    if origin!=oa.OWNER and needs_owner(req): raise ValueError(OWNER_ONLY)
    op=req.get('op','list')
    agent=req.get('agent') or None
    if agent is not None: check_executor(agent)
    if op=='sprint':
        with locked():
            s=state(); tasks=snapshot()['tasks']
            sprints.sync(s,tasks)
            save('workflow.json',s)
            return sprints.view(s,tasks,project=req.get('project') or None)
    if op=='learn':
        # Механизм развития агента (LEARN-01…09): чтение, синхронизация, причины, карточки, решения.
        import learn
        return learn.dispatch(req, origin or oa.AGENT)
    if op=='security':
        # Сканирование и триаж ИБ (SEC-012…014): снимок, находки, скан; снять находку — только владелец.
        import security_triage
        return security_triage.dispatch(req, origin or oa.AGENT)
    if op=='adaptive':
        # Адаптивные маршруты (FR-001): сводка по задачам конвейера или маршрут одной задачи (ref).
        import adaptive_pipeline as ap, risk_policy as rpol
        snap=snapshot(); conf=settings()
        if req.get('ref'):
            t=next((x for x in snap['tasks'] if x['ref']==req['ref']), None)
            if not t: raise ValueError('Задача не найдена')
            proj=read('projects.json',{}).get('projects',{}).get(t['project'],{})
            cls=rpol.classify(t, tests=ap.tests_available(proj))
            return dict(ref=t['ref'], now=ap.compile_plan(t, cls, None, (t.get('pipeline') or {}).get('needs')),
                        stored=ap.route(t.get('pipeline')), adaptive_mode=ap.conf(conf)['adaptive_mode'])
        return ap.summary([t for t in snap['tasks'] if t.get('pipeline')], conf)
    if op=='balance':
        # Баланс Claude/Codex (FR-015/016): режим, здоровье, доли принятой работы 7/30 дней, решения.
        import provider_balance as pb
        return pb.report(state(), pb.backlog(), now(), cli_ok)
    if op=='flags':
        import flags as ff
        return dict(flags=ff.view(settings()), rule='Флаги по умолчанию выключены; меняет владелец (op=settings changes.flags); нереализованный не включается')
    if op=='flow':
        # flow_snapshot (FR-014 п.7): слоты, стадии, блокировки, причины запрета, shadow-отличия.
        return fp.snapshot(state(), snapshot()['tasks'], now(), project=req.get('project') or None)
    if op=='list':
        snap=snapshot(); tasks=[t for t in snap['tasks'] if not req.get('project') or t['project']==req['project']]
        try: offset=max(0,int(req.get('offset') or 0)); limit=max(1,min(100,int(req.get('limit') or 20)))
        except (TypeError,ValueError): raise ValueError('offset и limit — целые числа (limit до 100)')
        page=tasks[offset:offset+limit]; nxt=offset+limit if offset+limit<len(tasks) else None
        return dict(focus=snap['focus'], settings=snap['settings'], total=len(tasks), offset=offset, limit=limit, next_offset=nxt,
                    tasks=[{k:t.get(k) for k in ('ref','title','status','priority','executor','claim','needs_plan','blocked_by','route')} for t in page],
                    more=('следующая страница: offset=%d; ' % nxt if nxt is not None else '')+'next / packet <ref>; list с project для уточнения')
    if op=='models':
        metrics=[]
        for m in MODELS:
            runs=[r for r in state()['runs'] if r.get('model')==m['id'] and r['status'] in ('done','failed')]
            known=[r['tokens'] for r in runs if r.get('tokens') is not None]
            metrics.append(dict(model=m['id'],provider=m['provider'],completed=len(runs),passed=sum(r['status']=='done' for r in runs),
                                known_tokens=sum(known),usage_complete=len(known)==len(runs)))
        return dict(models=MODELS,providers=PROVIDERS,lineup=LINEUP,efforts=EFFORTS,policy=POLICY,settings=settings(),measurements=metrics)
    if op=='queue':
        snap=snapshot(); byref={t['ref']:t for t in snap['tasks']}; at=now(); out=[]; s=state()
        for r in active(s['runs']):
            t=byref.get(r['ref'],{})
            if req.get('project') and r['ref'].split('/')[0]!=req['project']: continue
            if agent and t.get('executor') not in (None,'',agent): continue
            out.append(dict(r, executor=t.get('executor') or '', sprint_hold=sprints.reason(s,r['ref'],byref),
                            stale=r['status']=='running' and not lease_alive(r, at)))
        return out[:10]
    if op=='replay':
        return replay(state(), task_for(req.get('ref',''))['ref'], req.get('run'))
    if op=='packet':
        t=task_for(req.get('ref',''))
        role_run=next((r for r in state()['runs'] if r['id']==req.get('run') and r['ref']==t['ref'] and r.get('role')=='resolver'),None)
        if role_run:      # Решала: свой паспорт и у задачи вне конвейера
            import orchestra
            return orchestra.role_packet(t, role_run, role_run.get('agent') or agent or settings()['default_executor'])
        if t.get('pipeline'):
            import orchestra
            runs=[r for r in state()['runs'] if r['ref']==t['ref'] and r.get('role') and r['status'] in ('queued','running')]
            run=next((r for r in runs if r['id']==req.get('run')), runs[0] if len(runs)==1 else None)
            if not run: raise ValueError('Укажите run: паспорт задачи конвейера — это паспорт роли (%s)' % ', '.join(r['id']+' '+r['role'] for r in runs) or 'нет активных запусков')
            return orchestra.role_packet(t, run, run.get('agent') or agent or settings()['default_executor'])
        return packet(t['ref'], agent=agent)
    if op=='doc':
        # Раздел документа ветки задачи: оглавление или один раздел — вместо чтения файла целиком.
        import orchestra
        return orchestra.op_doc(None, req, task_for(req.get('ref','')), now())
    if op=='prompt':
        # Задание для ждущего запуска — повторно, если владелец закрыл окно после «Запустить».
        t=task_for(req.get('ref',''))
        run=next((r for r in state()['runs'] if r['ref']==t['ref'] and r['status']=='queued'),None)
        if not run: raise ValueError('У задачи нет запуска, ждущего исполнителя')
        route=(run.get('packet') or {}).get('route') or recommendation(t, agent=run.get('agent'))
        project=read('projects.json',{}).get('projects',{}).get(t['project'],{})
        worker=req.get('worker') or (worker_id(run, run['agent']) if run.get('agent') else None)
        return dict(run=run['id'], phase=run.get('phase'), route=route, agent=run.get('agent') or route.get('provider'),
                    worker=worker, prompt=launch_prompt(t, run, route, project, worker, req.get('worktree') or (run.get('launch') or {}).get('worktree')))
    if op=='route':
        # Черновик формы: какой план модели получит задача, ничего не записывая.
        draft=req.get('task')
        if not isinstance(draft,dict): raise ValueError('route: нужен task с size, risk, work_type')
        keys=('size','risk','work_type','model_plan','task_type','title','why','acceptance','scope','depends_on','assessment',*TYPE_FIELDS)
        draft={k:draft.get(k) for k in keys if draft.get(k) not in (None,'',[])}
        import adaptive_pipeline as ap, risk_policy as rpol
        return dict(model_plan=model_plan(draft), efforts=EFFORTS, complexity=cx.assess(draft),
                    adaptive=ap.compile_plan(draft, rpol.classify(draft), None))
    if op=='next':
        snap=snapshot(); at=now(); s=state(); out=[]
        for t in snap['tasks']:
            if req.get('project') and t['project']!=req['project']: continue
            if t['queued_run']: continue   # уже ждёт исполнителя: видна в queue
            if not ready(t, agent): continue
            if sprints.reason(s,t['ref'],snap['tasks']): continue
            route=recommendation(t, agent=agent, default=snap['settings']['default_executor'])
            if review_required(t, route, s['runs']) or scope_conflict(t, snap['tasks'], s['runs'], at, req.get('worker')): continue
            out.append(dict(t, route=route))
            sprints.admit(s,t['ref'],snap['tasks'])    # резерв только в локальном предпросмотре
            if len(out)==4: break
        return out
    extra={}
    with locked():
        s=state(); at=now()
        if rp.killed(s) and op not in STOP_ALLOWED and not (op=='split' and req.get('dry_run', True)):
            raise ValueError(rp.kill_reason(s))
        if op in ('idea','handoff','return','escalate','gate','approve_release','approve_oracle','to_idea','resolve','answer'):
            import orchestra
            if op=='idea':
                extra=orchestra.op_idea(s, req, at)
            elif op=='to_idea':
                extra=orchestra.op_to_idea(s, req, at)
                if extra.get('dry_run'): return dict(ok=True, **extra)
            else:
                t=task_for(req.get('ref',''))
                if not orchestra.is_pipeline(t) and op not in ('escalate','resolve','answer'): raise ValueError('Задача не идёт по конвейеру')
                extra=getattr(orchestra,'op_'+op)(s, req, t, at)
            save('workflow.json',s)
            job=extra.pop('_pipejob',None)
        elif op=='plan':
            project=req.get('project'); tasks=prepare_plan(project,req.get('tasks')); validate_plan(project,tasks)
            tasks=create_roots(project,tasks)   # только после полной проверки: план атомарен
            d=read('projects.json',{}); b=d['projects'][project].setdefault('backlog',[])
            # План модели прорабатывается сразу: для каждого ИИ — модель и усилия под задачу.
            b.extend(dict(t,status='queued',model_plan=model_plan(t)) for t in tasks); save('projects.json',d)
            return dict(ok=True, added=[t['id'] for t in tasks], roots=sorted({t['root'] for t in tasks}))
        elif op=='focus':
            keys=req.get('projects'); known=snapshot()['projects']
            if not isinstance(keys,list) or any(not isinstance(k,str) or k not in known for k in keys): raise ValueError('Неизвестные проекты фокуса')
            s['focus']=list(dict.fromkeys(keys))
        elif op=='settings':
            changes=req.get('changes',{})
            if not isinstance(changes,dict) or set(changes)-{'default_executor','lease_minutes','reviewer','auto_merge','auto_launch_review','auto_pipeline','roles','max_sessions','autonomy','consent','failover','sessions_per_ai','wip_analysis','context_ceiling','environments','flags','dual_review','dual_analysis',*fp.DEFAULTS,*_balance_defaults()}: raise ValueError('Настройки: default_executor, lease_minutes, reviewer, auto_merge, auto_launch_review, auto_pipeline, roles, max_sessions, sessions_per_ai, wip_analysis, context_ceiling, autonomy, environments, flags, '+', '.join([*fp.DEFAULTS, *_balance_defaults()]))
            if 'learning' in changes:
                v=changes['learning']; projs=read('projects.json',{}).get('projects',{})
                if not isinstance(v,dict) or set(v)-{'enabled','pilot_projects'} or ('enabled' in v and type(v['enabled']) is not bool) \
                        or not isinstance(v.get('pilot_projects',[]),list) or set(v.get('pilot_projects',[]))-set(projs):
                    raise ValueError('learning: {"enabled": bool (сбор опыта), "pilot_projects": [проекты, где разработчик — local_mimo]}')
            if 'dual_review' in changes and changes['dual_review'] not in ('critical','off'):
                raise ValueError('dual_review: critical (второе независимое ревью для задач CRITICAL, 07: A7-03) или off')
            if 'dual_analysis' in changes and changes['dual_analysis'] not in ('critical','off'):
                raise ValueError('dual_analysis: critical (второй независимый анализ другим ИИ для задач CRITICAL, 07: E2E-R05) или off')
            if 'adaptive_mode' in changes and changes['adaptive_mode'] not in ('off','shadow','enforce'):
                raise ValueError('adaptive_mode: off, shadow (маршрут считается, конвейер полный) или enforce (FR-001)')
            if 'adaptive_fast_allow' in changes:
                v=changes['adaptive_fast_allow']
                if not isinstance(v,list) or set(v)-set(WORK_TYPES) or 'architecture' in v or 'research' in v:
                    raise ValueError('adaptive_fast_allow: виды работы, которым разрешён FAST: docs, test, implementation')
            if 'balance_mode' in changes and changes['balance_mode'] not in ('prefer','balanced'):
                raise ValueError('balance_mode: prefer (порядок роли) или balanced (доли принятой работы, FR-015)')
            if 'target_shares' in changes:
                v=changes['target_shares']
                if not isinstance(v,dict) or set(v)!=set(PROVIDERS) or any(type(x) not in (int,float) or not 0<=x<=1 for x in v.values()) or abs(sum(v.values())-1)>1e-6:
                    raise ValueError('target_shares: {"claude": доля, "codex": доля}, от 0 до 1, сумма 1 (50/50, 60/40, только один ИИ)')
                changes['target_shares']={k:float(x) for k,x in v.items()}
            if 'affinity_bonus' in changes and (type(changes['affinity_bonus']) not in (int,float) or not 0<=changes['affinity_bonus']<=0.3):
                raise ValueError('affinity_bonus: от 0 до 0.3 (вес предпочтения роли против долга доли)')
            if 'role_affinity' in changes:
                import pipeline as P
                v=changes['role_affinity']
                if not isinstance(v,dict) or any(r not in P.ROUTING or not isinstance(x,dict) or set(x)-{'eligible'}
                                                 or not isinstance(x.get('eligible',[]),list) or not x.get('eligible') or set(x['eligible'])-set(PROVIDERS)
                                                 for r,x in v.items()):
                    raise ValueError('role_affinity: {роль: {"eligible": ["claude"|"codex", ...]}}')
            if 'flags' in changes:
                # Флаги TT3 (TT3-008): нереализованный не включается, связанный меняет свою настройку.
                import flags as ff
                expanded=ff.apply(changes.pop('flags'), settings(s))
                if set(expanded)&set(changes)-{'flags'}: raise ValueError('flags и та же настройка в одном запросе — оставьте одно')
                changes.update(expanded)
            if 'flow_mode' in changes and changes['flow_mode'] not in fp.MODES: raise ValueError('flow_mode: batch (спринт до main) или rolling (слот на задачу, FR-014)')
            for key,lo,hi in (('wip_global',1,20),('wip_per_project',1,10),('blocked_exception_limit',0,5)):
                if key in changes and (type(changes[key])!=int or not lo<=changes[key]<=hi): raise ValueError('%s: от %d до %d' % (key,lo,hi))
            if 'environments' in changes:
                # Одобренные команды развёртывания: «проект/окружение» → отпечаток (null — отозвать).
                v=changes['environments']
                if not isinstance(v,dict) or any(not isinstance(k,str) or k.count('/')!=1 or not (x is None or (isinstance(x,str) and len(x)==16)) for k,x in v.items()):
                    raise ValueError('environments: {"проект/окружение": отпечаток из 16 символов или null}')
                merged={**(settings(s).get('environments') or {}), **v}
                changes['environments']={k:x for k,x in merged.items() if x}
            if 'autonomy' in changes:
                if changes['autonomy'] not in AUTONOMY: raise ValueError('autonomy: full (полная автономия) или step (пошаговое согласие)')
                if changes['autonomy']=='full' and changes.pop('consent',None) is not True:
                    raise ValueError('Полная автономия включается только с явным согласием владельца (consent=true)')
                changes['autonomy_changed']=dict(at=at.isoformat(), to=changes['autonomy'], by='владелец')
            changes.pop('consent',None)
            if 'max_sessions' in changes and (type(changes['max_sessions'])!=int or not 1<=changes['max_sessions']<=12): raise ValueError('max_sessions: от 1 до 12 одновременных сессий')
            if 'sessions_per_ai' in changes:
                v=changes['sessions_per_ai']
                if not isinstance(v,dict) or any(a not in PROVIDERS or type(n)!=int or not 0<=n<=12 for a,n in v.items()):
                    raise ValueError('sessions_per_ai: {claude|codex: от 0 до 12 сессий}')
                changes['sessions_per_ai']=dict(settings(s).get('sessions_per_ai') or {}, **v)
            if 'wip_analysis' in changes and (type(changes['wip_analysis'])!=int or not 1<=changes['wip_analysis']<=50):
                raise ValueError('wip_analysis: от 1 до 50 задач в анализе на проект')
            if 'context_ceiling' in changes and (type(changes['context_ceiling'])!=int or not (changes['context_ceiling']==0 or 40_000<=changes['context_ceiling']<=400_000)):
                raise ValueError('context_ceiling: 0 (выключено) или от 40 000 до 400 000 токенов')
            if 'reviewer' in changes and changes['reviewer'] not in ('same','other',*PROVIDERS): raise ValueError('Ревьюер: same, other или ИИ')
            if 'roles' in changes:
                import pipeline as P
                if not isinstance(changes['roles'],dict) or any(r not in P.ROLES or a not in PROVIDERS for r,a in changes['roles'].items()):
                    raise ValueError('roles: {роль: claude|codex}')
                changes['roles']=dict(settings(s).get('roles') or {}, **changes['roles'])
            for key in ('auto_merge','auto_launch_review','auto_pipeline','failover','prefer_finish'):
                if key in changes and type(changes[key])!=bool: raise ValueError(key+': true или false')
            if 'default_executor' in changes and changes['default_executor'] not in PROVIDERS: raise ValueError('Неизвестный ИИ по умолчанию')
            if 'lease_minutes' in changes and (type(changes['lease_minutes'])!=int or not 5<=changes['lease_minutes']<=480): raise ValueError('Аренда: от 5 до 480 минут')
            diff=rp.preview(settings(s), {k:v for k,v in changes.items() if k!='autonomy_changed'})
            if req.get('preview'):
                return dict(ok=True, preview=diff, policy_version=rp.policy(s).get('version',0), applied=False)
            s.setdefault('settings',{}).update(changes)
            if changes.get('autonomy')=='full':
                # Согласие один раз на профиль: грант на локальный main, со сроком, без внешнего push.
                rp.policy(s)['grant']=rp.issue_grant(at)
            elif changes.get('autonomy')=='step':
                rp.revoke_grant(s, at, 'владелец выключил полную автономию')
            extra=dict(policy_version=rp.record(s, at, origin, diff), preview=diff)
        elif op=='stop_all':
            # Аварийная остановка (TT3-016). Включить может кто угодно: она только запрещает.
            reason=(req.get('reason') or '').strip()
            if len(reason)<5: raise ValueError('reason: почему останавливаем (не короче 5 символов)')
            if rp.killed(s): raise ValueError(rp.kill_reason(s))
            rp.policy(s)['kill_switch']=dict(on=True, at=at.isoformat(), by=origin, reason=reason[:300])
            extra=dict(stopped=emergency_stop(s, at, origin, reason), kill_switch=rp.policy(s)['kill_switch'])
            rp.record(s, at, origin, [dict(key='kill_switch', before=False, after=True)], 'kill_switch')
        elif op=='reconcile':
            import operation_ids as oi
            extra=dict(operation=oi.resolve(s, req.get('operation') or '', req.get('decision'), at, origin))
        elif op in ('release_slot','flow_exception','flow_block','flow_unblock'):
            # Поток задач (FR-014): закрыть слот и исключение — только владелец; блокировку
            # отмечает и исполнитель — она ничего не освобождает, только делает видимым.
            ref=req.get('ref') or ''; tasks=snapshot()['tasks']
            if not any(t['ref']==ref for t in tasks): raise ValueError('Задача не найдена: '+ref)
            sprints.sync(s, tasks)
            if op!='release_slot' and ref not in fp.open_refs(s, tasks) and op!='flow_exception':
                raise ValueError('Задача %s не в потоке: слот не занят' % ref)
            if op=='release_slot': extra=dict(slot=fp.release_slot(s, ref, req.get('reason'), tasks, at, origin))
            elif op=='flow_exception':
                extra=dict(exception=fp.add_exception(s, ref, req.get('kind'), req.get('reason'), req.get('expires_at'), at, origin))
            elif op=='flow_block':
                extra=dict(blocked=fp.block(s, ref, req.get('reason'), req.get('responsible'), req.get('recheck_at'), at, origin))
            else: extra=dict(unblocked=fp.unblock(s, ref, at, origin))
        elif op=='resume_all':
            if not rp.killed(s): raise ValueError('Аварийная остановка не включена')
            k=rp.policy(s)['kill_switch']
            rp.policy(s)['kill_switch']=dict(k, on=False, released_at=at.isoformat())
            resumed=[]
            for r in s['runs']:
                if r.get('status') in ('queued','running') and (r.get('paused') or {}).get('reason')=='kill_switch':
                    r['paused']['resume_at']=at.isoformat(timespec='seconds'); resumed.append(r['id'])
            extra=dict(resumed=resumed)
            rp.record(s, at, origin, [dict(key='kill_switch', before=True, after=False)], 'kill_switch')
        elif op=='start':
            project=req.get('project'); snap=snapshot()
            if project not in snap['projects']: raise ValueError('Неизвестный проект')
            agents=req.get('agents') or [settings(s)['default_executor']]
            if not isinstance(agents,list) or not agents or len(set(agents))!=len(agents) or any(a not in PROVIDERS for a in agents): raise ValueError('agents: один или оба ИИ')
            sessions=req.get('sessions')
            if sessions is not None and (type(sessions)!=int or not 0<=sessions<=12): raise ValueError('sessions: от 1 до 12 (0 — по объёму бэклога)')
            plan=start_plan(project, agents, sessions, snap['tasks'], s['runs'], at)
            if req.get('dry_run', True): return dict(plan, dry_run=True)
            if not plan['picks']: raise ValueError(plan['reason'])
            batch='B-%s' % at.strftime('%Y%m%d%H%M%S')
            # Проект в автономной работе: все ждущие роли конвейера открываются сервером по мере
            # освобождения мест (max_sessions), а не только этот пакет.
            act=s.setdefault('active_projects',[])
            if project not in act: act.append(project)
            for r in s['runs']:
                if r['ref'].split('/')[0]==project and r.get('role') and r['status']=='queued' and not r.get('launched_at'):
                    r['auto_launch']=True
            byref={t['ref']:t for t in snap['tasks']}
            for p in plan['picks']:
                sprints.admit(s,p['ref'],snap['tasks'])
                if p['kind']=='role':   # роль конвейера: ждущий запуск переходит к выбранному ИИ
                    run=next(r for r in s['runs'] if r['id']==p['run'])
                    run.update(agent=p['agent'], batch=batch); continue
                pk=packet(p['ref'], agent=p['agent'])
                run=new_run(s, p['ref'], pk, at); run.update(agent=p['agent'], batch=batch)
                p['run']=run['id']
            save('workflow.json',s)
            return dict(plan, dry_run=False, batch=batch)
        elif op=='stop_project':
            project=req.get('project')
            if project not in snapshot()['projects']: raise ValueError('Неизвестный проект')
            s['active_projects']=[p for p in s.get('active_projects',[]) if p!=project]
            held=0
            for r in s['runs']:
                if r['ref'].split('/')[0]==project and r.get('role') and r['status']=='queued' and not r.get('launched_at') and r.get('auto_launch'):
                    r['auto_launch']=False; held+=1
            extra=dict(project=project, held=held)
        elif op=='split':
            project=req.get('project'); agents=req.get('agents') or list(PROVIDERS)
            snap=snapshot()
            if project not in snap['projects']: raise ValueError('Неизвестный проект')
            if not isinstance(agents,list) or len(agents)<2 or len(set(agents))!=len(agents) or any(a not in PROVIDERS for a in agents): raise ValueError('Нужны минимум два разных ИИ')
            result=split(project, agents, snap['tasks'], s['runs'], at)
            if req.get('dry_run', True): return dict(result, dry_run=True)
            # Применяется показанное в предпросмотре (assign: задача → ИИ), а не пересчёт заново.
            # Без assign — прежнее поведение: пересчитанное распределение.
            assign=dict(result['assign']) if req.get('assign') is None else req['assign']
            if req.get('assign') is not None: check_assign(assign, project, agents, result, snap['tasks'])
            write_patches(s, {ref:dict(executor=a) for ref,a in assign.items()})
            save('workflow.json',s)
            return dict(result, assign=assign, dry_run=False)
        else:
            ref=req.get('ref',''); t=task_for(ref)
            patch={}
            if op=='confirm_main':
                receipt=confirm_main(s,t)
                sprints.sync(s,snapshot()['tasks']); save('workflow.json',s)
                return dict(ok=True, main=receipt)
            if t.get('pipeline') and op in ('enqueue','take','complete','autocheck','repin','finish','fail') :
                raise ValueError('Задача идёт по конвейеру: работу сдают через op=handoff с выполненным DoD, возвращают через op=return')
            if t.get('pipeline') and op=='status' and req.get('status') not in ('blocked','cancelled','queued'):
                raise ValueError('Задача идёт по конвейеру: этап меняет тимлид по DoR/DoD; вручную — только блокировка и отмена')
            if op=='update':
                if any(r['ref']==ref and r['status']=='running' for r in s['runs']): raise ValueError('Паспорт выполняемой задачи менять нельзя: сначала остановите запуск')
                changes,new_root=update_patch(t, req.get('changes',{}), at); patch.update(changes)
                if new_root:   # запись памяти — только после всех проверок правки
                    patch['root']=create_roots(t['project'],[dict(new_root=new_root)])[0]['root']
            elif op=='complete':
                packet(ref)
                if t.get('status') in ('done','cancelled'): raise ValueError('Задача уже закрыта')
                if any(r['ref']==ref and r['status'] in ('queued','running') for r in s['runs']): raise ValueError('Завершите или отмените запуск через его контракт')
                if len(t.get('acceptance_done',[]))!=len(t.get('acceptance',[])): raise ValueError('Сначала проверьте все критерии')
                if not isinstance(req.get('evidence'),str) or not req['evidence'].strip(): raise ValueError('Опишите результат проверки')
                patch.update(status='done',evidence=req['evidence'],completed_at=at.isoformat(),status_note=None)
            elif op=='enqueue':
                if agent and t.get('executor') and agent!=t['executor']: raise ValueError('Задача назначена ИИ %s' % t['executor'])
                p=packet(ref, agent=agent or t.get('executor') or None)
                if t.get('status') in ('done','cancelled','in_progress','review','blocked'): raise ValueError('Статус не допускает запуск')
                if any(r['ref']==ref and r['status'] in ('queued','running') for r in s['runs']): raise ValueError('Задача уже запущена или ожидает исполнителя')
                sprints.admit(s,ref,snapshot()['tasks'])
                run=new_run(s, ref, p, at)
                if agent: run['agent']=agent   # запуск с доски закреплён за выбранным ИИ
                project=read('projects.json',{}).get('projects',{}).get(t['project'],{})
                extra=dict(run=run['id'], phase=run.get('phase'), route=p['route'], prompt=launch_prompt(t, run, p['route'], project))
            elif op=='take':
                # Атомарно: два ИИ, получившие одну задачу из next, не возьмут её оба.
                if t.get('status') in ('done','cancelled','review','blocked'): raise ValueError('Статус не допускает запуск')
                run=next((r for r in s['runs'] if r['ref']==ref and r['status'] in ('queued','running')),None)
                if run and run['status']=='running':
                    if lease_alive(run, at):
                        raise ValueError('Занято: %s · %s · до %s' % (run.get('agent') or '?', run.get('worker') or '?', run.get('lease_until') or 'без срока'))
                    expire(s, ref, at, req.get('worker') or '?'); run=None
                elif t.get('status')=='in_progress' and not run: raise ValueError('Задача в работе вне очереди: сначала смените статус')
                if run is None: run=new_run(s, ref, None, at)
                patch.update(start_run(s, t, run, req, at))
            elif op=='repin':
                # Выбор владельца сильнее прежнего закрепления: ждущий запуск переходит к выбранному ИИ.
                run=next((r for r in s['runs'] if r['id']==req.get('run') and r['ref']==ref),None)
                if not run or run['status']!='queued': raise ValueError('Сменить ИИ можно только у ждущего запуска; выполняемый — сначала отмените')
                if not agent: raise ValueError('Укажите ИИ (agent)')
                if t.get('executor') and t['executor']!=agent:
                    raise ValueError('Задача назначена ИИ %s — смените исполнителя в «Проработать задачу»' % PROVIDERS[t['executor']]['name'])
                if run.get('agent')!=agent:
                    p=packet(ref, agent=agent)
                    run.setdefault('repins',[]).append(dict(at=at.isoformat(), from_agent=run.get('agent'), to=agent, by='владелец'))
                    run.update(agent=agent, packet=p, phase=p['phase'])
                    for key in ('launched_at','launch','launch_error','launch_error_at'): run.pop(key,None)
                extra=dict(run=run['id'], agent=agent, route=run['packet']['route'])
            elif op=='autocheck':
                run=next((r for r in reversed(s['runs']) if r['ref']==ref and (r.get('launch') or {}).get('worktree') and r.get('phase')!='review'),None)
                if not run: raise ValueError('У задачи нет ветки исполнения: автопроверка проверяет работу в отдельной рабочей копии')
                if any(r['ref']==ref and r['status'] in ('queued','running') for r in s['runs']): raise ValueError('У задачи есть активный запуск: дождитесь его или отмените')
                if t.get('status') not in ('review','queued'): raise ValueError('Автопроверка — для задачи в «Проверка» или после доработки')
                patch.update(status='review', auto_check=dict(state='pending', run=run['id']), status_note='Идёт автопроверка ветки %s' % run['launch']['worktree']['branch'])
                extra=dict(_autocheck=(ref, run['id']))
            elif op=='status':
                target=req.get('status'); reason=(req.get('reason') or '').strip()
                if target not in MANUAL_STATUSES: raise ValueError('Статус: %s; «Готово» — через завершение с проверкой' % ', '.join(STATUS_LABELS[k] for k in MANUAL_STATUSES))
                current=t.get('status') or 'queued'
                if target==current: raise ValueError('Задача уже в статусе «%s»' % STATUS_LABELS.get(target,target))
                if target in ('blocked','cancelled') and not reason: raise ValueError('Укажите причину: она видна исполнителям и в истории')
                if current=='done' and (target!='queued' or not reason): raise ValueError('Закрытую задачу можно только вернуть в «К выполнению» с причиной')
                if len(reason)>1000: raise ValueError('Причина — до 1000 символов')
                if target in ('in_progress','review'):
                    sprints.admit(s,ref,snapshot()['tasks'])
                if current=='done': s.get('main_receipts',{}).pop(ref,None)
                for r in s['runs']:
                    if r['ref']!=ref or r['status'] not in ('queued','running'): continue
                    if r['status']=='running' and lease_alive(r, at) and target!='cancelled':
                        raise ValueError('Задачу выполняет %s (%s): сначала отмените запуск или дождитесь его завершения' % (r.get('agent') or '?', r.get('worker') or '?'))
                    r.update(status='cancelled', finished_at=at.isoformat(),
                             evidence='Владелец перевёл задачу в «%s»%s' % (STATUS_LABELS[target], ': '+reason if reason else ''))
                history=list(t.get('status_history') or [])[-19:]
                history.append(dict(at=at.isoformat(), from_status=current, to=target, reason=reason, by='владелец'))
                patch.update(status=target, claimed_by=None, status_history=history,
                             status_note=reason if target in ('blocked','cancelled') else None)
                if current=='done': patch.update(completed_at=None)
            elif op in ('claim','heartbeat','finish','fail','cancel','assess','review','pause'):
                run=next((r for r in s['runs'] if r['id']==req.get('run') and r['ref']==ref),None)
                if not run: raise ValueError('Запуск не найден')
                if op=='claim':
                    if run['status']!='queued': raise ValueError('Запуск уже принят или завершён')
                    patch.update(start_run(s, t, run, req, at))
                elif op=='heartbeat':
                    if run['status']!='running': raise ValueError('Запуск не выполняется')
                    if req.get('worker')!=run.get('worker'): raise ValueError('Продлевать аренду может только принявший исполнитель')
                    minutes=req.get('minutes', settings(s)['lease_minutes'])
                    if type(minutes)!=int or not 5<=minutes<=480: raise ValueError('Аренда: от 5 до 480 минут')
                    until=(at+timedelta(minutes=minutes)).isoformat()
                    if run.get('paused'):   # исполнитель продолжил сам (владелец написал «продолжай» в окне)
                        run['pauses']=(run.get('pauses') or [])[-9:]+[dict(run.pop('paused'), resumed_at=at.isoformat(), resumed_by='agent')]
                        patch['status_note']='Возобновлено после лимита токенов'
                    run.update(heartbeat_at=at.isoformat(), lease_until=until)
                    provider_succeeded(s, run.get('agent'), at)
                    patch['claimed_by']=dict(t.get('claimed_by') or {}, agent=run.get('agent'), worker=run['worker'], run=run['id'], model=run.get('model'), lease_until=until)
                    patch['claimed_by'].pop('paused_until',None)
                elif op=='pause':
                    if run['status']!='running': raise ValueError('Пауза — для выполняемого запуска')
                    if req.get('worker')!=run.get('worker'): raise ValueError('Ставит паузу принявший исполнитель')
                    if run.get('paused'): raise ValueError('Запуск уже на паузе до %s' % run['paused']['resume_at'])
                    reason=req.get('reason') or 'limit'
                    if reason not in ('limit','context'): raise ValueError('reason: limit — лимит токенов, context — потолок контекста (эстафета)')
                    cp=(req.get('checkpoint') or '').strip()
                    if len(cp)<20: raise ValueError('checkpoint: что сделано, что осталось и следующий шаг (не короче 20 символов)')
                    resets=req.get('resets_at')
                    if resets is not None and parse_time(str(resets).replace('Z','+00:00')) is None: raise ValueError('resets_at: время ISO 8601')
                    if reason=='context':
                        if relays(run)>=MAX_RELAYS: raise ValueError('Эстафет в запуске уже %d — продолжай в этой сессии и сдай этап' % MAX_RELAYS)
                        if t.get('pipeline'):
                            import orchestra
                            orchestra.autocommit(t['pipeline'], run, 'WIP перед эстафетой: '+cp)
                        patch.update(relay_patch(s, t, run, at, 'agent', cp))
                        extra=dict(relay=run['paused'], next='Остановись: сервер откроет свежую сессию этой роли с твоей точкой остановки.')
                    else:
                        if t.get('pipeline'):
                            import orchestra
                            orchestra.autocommit(t['pipeline'], run, 'WIP перед паузой по лимиту: '+cp)
                        patch.update(pause_patch(s, t, run, dict(resets_at=resets, session=(run.get('launch') or {}).get('session')), at, 'agent', cp))
                        extra=dict(paused=run['paused'])
                        moved=failover(s, dict(t, **{k:v for k,v in patch.items() if k=='pipeline'}), run, at)
                        if moved:
                            patch.update(moved.pop('patch')); extra=dict(handed_to=moved)
                elif op=='review':
                    import autocheck
                    if run['status']!='running' or run.get('phase')!='review': raise ValueError('Заключение принимает только выполняемый запуск ревью')
                    if req.get('worker')!=run.get('worker'): raise ValueError('Заключение присылает принявший ревью исполнитель')
                    verdict=req.get('verdict'); criteria=req.get('criteria'); conclusion=(req.get('conclusion') or '').strip()
                    if verdict not in REVIEW_VERDICTS: raise ValueError('verdict: approve, changes или spec_change')
                    acc=t.get('acceptance') or []
                    if not isinstance(criteria,list) or len(criteria)!=len(acc) or any(not isinstance(c,dict) or type(c.get('ok'))!=bool or not isinstance(c.get('note',''),str) for c in criteria):
                        raise ValueError('criteria: по объекту {ok, note} на каждый из %d критериев, по порядку' % len(acc))
                    if not conclusion or len(conclusion)>3000: raise ValueError('Нужно заключение (conclusion)')
                    spec=req.get('spec') if verdict=='spec_change' else None
                    if verdict=='spec_change' and (not isinstance(spec,dict) or not str(spec.get('deviation','')).strip() or not str(spec.get('proposal','')).strip()):
                        raise ValueError('spec_change: укажите spec.deviation (как решено в коде) и spec.proposal (что изменить в ТЗ)')
                    if verdict!='changes' and not all(c['ok'] for c in criteria): raise ValueError('Есть невыполненные критерии — вердикт changes')
                    if verdict!='changes' and not (t.get('auto_check') or {}).get('ok'): raise ValueError('Автопроверка не пройдена — вердикт changes')
                    tokens=req.get('tokens')
                    if tokens is not None and (type(tokens)!=int or tokens<0): raise ValueError('Токены должны быть целым неотрицательным числом')
                    record=dict(verdict=verdict, criteria=[dict(text=a, ok=c['ok'], note=c.get('note','').strip()[:500]) for a,c in zip(acc,criteria)],
                                conclusion=conclusion, spec=spec, at=at.isoformat(),
                                by=dict(agent=run.get('agent'), model=run.get('model'), worker=run.get('worker')))
                    run.update(status='done', evidence='Ревью: %s — %s' % (REVIEW_VERDICTS[verdict], conclusion), tokens=tokens,
                               finished_at=at.isoformat(), review=record)
                    patch.update(review=record, claimed_by=None)
                    extra=dict(verdict=verdict)
                    if verdict=='changes':
                        patch.update(status='queued', acceptance_done=[i for i,c in enumerate(criteria) if c['ok']],
                                     status_note='Доработка по ревью: '+conclusion[:300])
                        extra['status']='queued'
                    else:
                        if spec:
                            extra['spec_task'],extra['spec_record']=record_spec_change(t, spec, record, at)
                        wt=task_branch(s, ref)
                        merged=None
                        if wt and settings(s)['auto_merge']:
                            project=read('projects.json',{}).get('projects',{}).get(t['project'],{})
                            ws=workspace(t['project'],project); repo=ws['active_copy'] or ws['path']
                            ok,info=autocheck.merge(repo, wt['branch'], wt.get('base') or (t.get('auto_check') or {}).get('base') or 'HEAD',
                                                    'Слияние %s: %s (%s)\n\nРевью %s: %s' % (wt['branch'], t['title'], t['id'], run.get('model'), conclusion[:500]))
                            merged=dict(ok=ok, info=info, branch=wt['branch'])
                            extra['merge']=merged
                        if wt and not (merged and merged['ok']):
                            reason=merged['info'] if merged else 'автослияние выключено'
                            patch.update(status='review', status_note='Ревью: %s. Слияние не выполнено (%s) — слейте %s и закройте задачу' % (REVIEW_VERDICTS[verdict], reason, wt['branch']))
                            extra['status']='review'
                        else:
                            patch.update(status='done', completed_at=at.isoformat(), status_note=None,
                                         acceptance_done=list(range(len(acc))),
                                         evidence='Ревью %s (%s): %s%s' % (REVIEW_VERDICTS[verdict], run.get('model'), conclusion,
                                                                           '; слито %s коммитом %s' % (merged['branch'], merged['info']) if merged else ''))
                            extra['status']='done'
                elif op=='assess':
                    if run['status']!='running' or run.get('phase')!='assess': raise ValueError('Оценку принимает только выполняемый запуск оценки')
                    if req.get('worker')!=run.get('worker'): raise ValueError('Оценку присылает принявший запуск исполнитель')
                    a=req.get('assessment'); cx.validate_assessment(a)
                    tokens=req.get('tokens')
                    if tokens is not None and (type(tokens)!=int or tokens<0): raise ValueError('Токены должны быть целым неотрицательным числом')
                    assessment=dict(complexity=a['complexity'], uncertainty=a['uncertainty'], breadth=a['breadth'],
                                    reasons=a['reasons'].strip(), at=at.isoformat(),
                                    by=dict(agent=run.get('agent'), model=run.get('model'), worker=run.get('worker')))
                    run.update(status='done', evidence='Оценка сложности: '+assessment['reasons'], tokens=tokens,
                               finished_at=at.isoformat(), assessment=assessment)
                    after=dict(t, assessment=assessment)
                    patch.update(assessment=assessment, status='queued', claimed_by=None, model_plan=model_plan(after))
                    # Исполнение — новым запуском того же ИИ на назначенной модели.
                    route=recommendation(dict(after, model_plan=patch['model_plan']), agent=run.get('agent'))
                    nxt=new_run(s, ref, dict(route=route, phase='execute'), at); nxt['agent']=run.get('agent')
                    project=read('projects.json',{}).get('projects',{}).get(t['project'],{})
                    extra=dict(run=nxt['id'], route=route, switch=route['model']!=run.get('model'),
                               complexity={k:v for k,v in cx.assess(after).items() if k in ('score','tier','confidence')},
                               prompt=launch_prompt(after, nxt, route, project),
                               next=('Модель та же — можно сразу claim %s.' % nxt['id']) if route['model']==run.get('model')
                                    else 'Назначена %s · %s: исполнение — в сессии с этой моделью; запуск %s ждёт.' % (route['model'], route['effort'], nxt['id']))
                else:
                    if op=='finish' and run.get('phase')=='assess': raise ValueError('Это запуск оценки: пришлите результат через op=assess')
                    if run['status'] not in (('queued','running') if op=='cancel' else ('running',)): raise ValueError('Запуск не выполняется')
                    if op!='cancel' and run.get('worker') and req.get('worker') and req['worker']!=run['worker']: raise ValueError('Завершает запуск принявший исполнитель')
                    if not isinstance(req.get('evidence'),str) or not req['evidence'].strip(): raise ValueError('Нужны результаты проверок или причина остановки')
                    tokens=req.get('tokens')
                    if tokens is not None and (type(tokens)!=int or tokens<0): raise ValueError('Токены должны быть целым неотрицательным числом')
                    run.update(status={'finish':'done','fail':'failed','cancel':'cancelled'}[op],evidence=req['evidence'],tokens=tokens,finished_at=at.isoformat())
                    patch.update(status='done' if op=='finish' else 'queued', claimed_by=None)
                    if op=='finish': patch.update(completed_at=run['finished_at'], evidence=req['evidence'])   # итог с доказательством
                    branch=((run.get('launch') or {}).get('worktree') or {}).get('branch')
                    if op=='finish' and branch:
                        # Работа в отдельной ветке не в основной ветке проекта: готово — после слияния владельцем.
                        patch.update(status='review', completed_at=None, evidence=req['evidence'], auto_check=dict(state='pending', run=run['id']),
                                     status_note='Готово в ветке %s — идёт автопроверка' % branch)
                        extra=dict(status='review', branch=branch, _autocheck=(ref, run['id']))
            else: raise ValueError('Неизвестная операция')
            write_patches(s, {ref:patch} if patch else {})
        save('workflow.json',s)
    if op=='stop_all':
        import launcher
        for x in extra['stopped']:          # состояние уже сохранено: процессы завершаются последними
            x['terminated']=launcher.terminate(x.get('pid'))
    if op in ('idea','handoff','return','escalate','gate','approve_release','approve_oracle','to_idea','resolve','answer'):
        if job: spawn_pipejob(*job)
        return dict(ok=True, **extra)
    spawn=extra.pop('_autocheck',None)
    if spawn: spawn_autocheck(*spawn)
    return dict(ok=True, **extra)

def spawn_pipejob(ref, role, stage, run_id):
    """Тяжёлые пункты DoD (тесты, слияния) — отдельным процессом, как автопроверка."""
    if os.environ.get('TASKTREKER_PIPELINE_SYNC')=='1':
        import orchestra
        return orchestra.job(ref, role, stage, run_id)
    import subprocess, sys
    logs=ROOT/'ops'/'logs'; logs.mkdir(parents=True, exist_ok=True)
    flags=(subprocess.DETACHED_PROCESS|subprocess.CREATE_NO_WINDOW) if os.name=='nt' else 0
    with open(logs/'pipeline.log','a',encoding='utf-8') as log:
        subprocess.Popen([sys.executable, str(Path(__file__)), 'pipejob', ref, '--run', run_id, '--role', role, '--stage', stage],
                         env=dict(os.environ, TASKTREKER_DATA=data_dir().as_posix()), stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, creationflags=flags, cwd=str(ROOT))

def foreign_copy():
    """Запуск из рабочей копии задачи: код ветки может быть старше системы, а данные общие.

    Сервер досок передаёт сессии ИИ TASKTREKER_SYSTEM — каталог tools системы. Копия tools
    в другом месте не пишет в общие данные, а называет команду системы."""
    system = os.environ.get('TASKTREKER_SYSTEM')
    here = os.path.dirname(os.path.abspath(__file__))
    if system and os.path.normcase(os.path.abspath(system)) != os.path.normcase(here):
        return 'Эта копия TaskTreker — рабочая копия задачи (%s). Работай через MCP tt_work или командой системы: py "%s"' % (here, os.path.join(system, 'pm.py'))
    return None

def main(args=None):
    refusal=foreign_copy()
    if refusal:
        print(refusal); return 2
    parser=argparse.ArgumentParser(description='Доски: list/next/packet; изменения через request JSON')
    parser.add_argument('op',choices=['list','next','packet','queue','models','sprint','confirm_main','request','worktree','autocheck','pipejob']); parser.add_argument('value',nargs='?')
    parser.add_argument('--run',help='запуск для autocheck и pipejob')
    parser.add_argument('--role'); parser.add_argument('--stage')
    parser.add_argument('--agent',choices=sorted(PROVIDERS),help='ИИ, для которого строится маршрут (next, packet, queue)')
    a=parser.parse_args(args)
    try:
        if a.op=='pipejob':
            import orchestra
            print(json.dumps(orchestra.job(a.value or '', a.role, a.stage, a.run),ensure_ascii=False)); return 0
        if a.op=='autocheck':
            print(json.dumps(autocheck_run(a.value or '', a.run or ''),ensure_ascii=False,indent=2)); return 0
        if a.op=='worktree':
            import launcher
            t=task_for(a.value or ''); p=read('projects.json',{})['projects'][t['project']]
            agent=a.agent or t.get('executor') or settings()['default_executor']
            print(json.dumps(launcher.ensure_worktree(p.get('path'), agent, t['id']) or {'note':'Не git: работа идёт в папке проекта'},ensure_ascii=False,indent=2)); return 0
        if a.op=='request': req=json.loads(Path(a.value).read_text(encoding='utf-8-sig'))
        else:
            req=dict(op=a.op,ref=a.value)
            if a.agent: req['agent']=a.agent
            if a.op in ('next','queue','list') and a.value: req=dict(req,project=a.value,ref=None)
        print(json.dumps(action(req),ensure_ascii=False,indent=2)); return 0
    except (ValueError,OSError) as e:
        print(str(e)); return 1

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8'); sys.exit(main())
