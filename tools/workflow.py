"""Доски и контракт исполнения. Ручной ввод, без автоматического сбора портфеля.

Несколько ИИ (Claude, GPT / Codex) работают с одним бэклогом. Задача может быть
назначена ИИ (executor); принятая задача несёт метку claimed_by с арендой:
пока аренда жива, другой ИИ задачу не возьмёт. Области правки (scope) не дают
двум ИИ одновременно менять одни и те же файлы проекта.
"""
import argparse
import contextlib
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
import complexity as cx

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://developers.openai.com/api/docs/guides/model-selection'
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
LEASE_MINUTES = 45           # срок метки занятости без heartbeat
LOCK_STALE_SECONDS = 120     # блокировка старше — след упавшего процесса
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
    path = data_dir() / name
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else copy.deepcopy(default)

def save(name, value):
    folder = data_dir(); folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, folder / name)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

@contextlib.contextmanager
def locked():
    folder = data_dir(); folder.mkdir(parents=True, exist_ok=True)
    path = folder / '.workflow.lock'
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY); break
        except FileExistsError:
            # Упавший процесс оставляет файл навсегда; операция длится доли секунды.
            try: stale = time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS
            except FileNotFoundError: stale = True
            if attempt == 2 or not stale:
                raise ValueError('Другая операция уже меняет задачи; повторите позже')
            with contextlib.suppress(FileNotFoundError): path.unlink()
    try:
        os.close(fd)
        yield
    finally: path.unlink()

def state():
    return read('workflow.json', dict(schema=1, focus=[], tasks={}, runs=[]))

def settings(s=None):
    return dict(dict(default_executor=DEFAULT_EXECUTOR, lease_minutes=LEASE_MINUTES),
                **(s or state()).get('settings', {}))

def model_entry(model):
    return next((m for m in MODELS if m['id'] == model), None)

def provider_of(model):
    entry = model_entry(model)
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
            t = dict(original); ref = key + '/' + t['id']
            t.update(s['tasks'].get(ref, {})); t.update(project=key, ref=ref)
            if t.get('status') == 'in-progress': t['status'] = 'in_progress'   # прежнее написание из Obsidian
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
    roots=[{k:r.get(k) for k in ('id','title','project')} for r in read('memory/index.json',{}).get('records',[])
           if r.get('kind')=='запрос' and r.get('status')=='активна']
    return dict(projects={k:dict(workspace(k,p), next_id=next_id(p)) for k,p in projects.items()},
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
    phase='assess' if t['complexity']['needs_assessment'] else 'execute' 
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
    if t.get('task_type')=='decomposition':
        result['decomposition']='Создавай подзадачи через tt_work op=plan с task_type, root=%s и assessment {complexity, uncertainty, breadth, reasons} у каждой — модель и усилия подзадачи считаются по её сложности.' % t['root']
    result['launch']='Исполнитель проверяет доступность модели, принимает запуск через take/claim и выполняет критерии. Пакет не меняет модель текущего чата.'
    # Approximation is deliberately conservative and explicitly not billing tokens.
    if len(json.dumps(result,ensure_ascii=False)) > 6000:
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
        if t.get('priority') not in ('🔴','🟡','🟢'): raise ValueError('Укажите приоритет 🔴/🟡/🟢')
        if t.get('risk') not in ('low','medium','high'): raise ValueError('Укажите риск low/medium/high')
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

def split(project, agents, tasks, runs, at):
    """Объяснимое распределение: связные группы (зависимости, общая область)
    целиком уходят одному ИИ; группы раскладываются по наименее загруженному."""
    points={'S':1,'M':3,'L':8,'XL':20}
    mine=[t for t in tasks if t['project']==project and t.get('status') not in ('done','cancelled')]
    # Загрузка: назначенное ИИ плюс то, что он держит прямо сейчас.
    holder=lambda t:t.get('executor') or (t['claim'] or {}).get('agent')
    load={a:sum(points.get(t.get('size'),3) for t in mine if holder(t)==a) for a in agents}
    free=[t for t in mine if not t.get('executor') and not t['claim'] and t.get('status') not in ('in_progress','review')]
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
    """Явный ввод пользователя/исполнителя: канонический бэклог виден
    старым потребителям (дерево, оценка, Obsidian), без импорта фактов."""
    if not patches: return
    d=read('projects.json',{})
    for ref,patch in patches.items():
        key,tid=ref.split('/',1)
        original=next(v for v in d['projects'][key]['backlog'] if v['id']==tid)
        original.update(patch)
        for k in [k for k,v in original.items() if v is None]: original.pop(k)
        s['tasks'].pop(ref,None)
    save('projects.json',d)

def start_run(s, t, run, req, at):
    """Общее для claim и take: проверка ИИ, модели, областей; метка занятости."""
    model=req.get('model'); worker=req.get('worker')
    if not isinstance(worker,str) or not worker.strip(): raise ValueError('Укажите исполнителя/чат')
    agent=provider_of(model)
    if run['status']=='queued' and run.get('agent') and run['agent']!=agent:
        raise ValueError('Запуск поставлен для ИИ %s' % run['agent'])
    if req.get('agent') and req['agent']!=agent: raise ValueError('Модель %s не принадлежит ИИ %s' % (model, req['agent']))
    if t.get('executor') and t['executor']!=agent:
        raise ValueError('Задача назначена ИИ %s; ИИ %s её не берёт' % (t['executor'], agent))
    p=packet(t['ref'], agent=agent)
    if model!=p['route']['model']:
        raise ValueError('Фактическая модель должна совпадать с паспортом: %s' % p['route']['model'])
    conflict=scope_conflict(t, snapshot()['tasks'], s['runs'], at, worker)
    if conflict: raise ValueError('Область задачи пересекается с работой: '+conflict)
    minutes=settings(s)['lease_minutes']
    until=(at+timedelta(minutes=minutes)).isoformat()
    run.update(status='running',worker=worker,model=model,agent=agent,packet=p,phase=p['phase'],
               claimed_at=at.isoformat(),heartbeat_at=at.isoformat(),lease_until=until)
    return dict(status='in_progress', claimed_by=dict(agent=agent, worker=worker, run=run['id'], model=model, lease_until=until))

def new_run(s, ref, p, at):
    run=dict(id='RUN-'+str(len(s['runs'])+1),ref=ref,status='queued',packet=p,created_at=at.isoformat())
    if p: run['phase']=p['phase']
    s['runs'].append(run); return run

def worker_id(run, agent):
    """Имя исполнителя сессии, открытой сервером: одинаковое в claim, heartbeat и finish."""
    return '%s-cli:%s' % (agent, run['id'])

def launch_prompt(t, run, route, project, worker=None, worktree=None):
    """Задание для ИИ: первая реплика сессии, открытой с доски, или текст для вставки в чат."""
    ai=PROVIDERS[route['provider']]['name']; ref=t['ref']; ws=workspace(t['project'], project)
    folder=(worktree or {}).get('path') or ws['active_copy'] or ws['path'] or 'не указана'
    lines=['Задача TaskTreker %s «%s» запущена для тебя (%s).' % (ref, t['title'], ai),
           'Рабочая папка: %s.' % folder]
    if worktree:
        lines.append('Это отдельная рабочая копия, ветка %s. Коммить в неё; в основную ветку не сливай — это решает владелец.' % worktree['branch'])
    if run.get('phase')=='assess':
        lines.append('Сначала оценка сложности, не реализация. Модель как есть: %s, усилия %s.' % (route['model'], route['effort']))
    else:
        lines.append('Модель: %s, усилия: %s. Если у тебя другая модель — переключись (/model) или сообщи.' % (route['model'], route['effort']))
    lines.append('1) Сразу прими запуск: tt_work op=claim ref=%s run=%s model=%s worker=%s' % (ref, run['id'], route['model'], worker or '<id этой сессии>'))
    lines.append('2) tt_work op=packet ref=%s agent=%s — и действуй строго по паспорту.' % (ref, route['provider']))
    lines.append('3) %s' % ('tt_work op=assess … с оценкой 1…5 и основаниями.' if run.get('phase')=='assess'
                            else 'Если работа идёт дольше 40 минут — tt_work op=heartbeat. По окончании tt_work op=finish с результатами проверок и расходом токенов.'))
    if worker: lines.append('Твой worker во всех вызовах: %s.' % worker)
    return '\n'.join(lines)

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

def expire(s, ref, at, by):
    """Просроченная метка: запуск закрывается как expired, история остаётся."""
    for r in s['runs']:
        if r['ref']==ref and r['status']=='running' and not lease_alive(r, at):
            r.update(status='expired', finished_at=at.isoformat(),
                     evidence='Аренда истекла без heartbeat; задачу перехватил '+by)
            return r

def action(req):
    op=req.get('op','list')
    agent=req.get('agent') or None
    if agent is not None: check_executor(agent)
    if op=='list':
        snap=snapshot(); tasks=[t for t in snap['tasks'] if not req.get('project') or t['project']==req['project']]
        return dict(focus=snap['focus'], settings=snap['settings'], total=len(tasks),
                    tasks=[{k:t.get(k) for k in ('ref','title','status','priority','executor','claim','needs_plan','blocked_by','route')} for t in tasks[:20]],
                    more='next / packet <ref>; list с project для уточнения')
    if op=='models':
        metrics=[]
        for m in MODELS:
            runs=[r for r in state()['runs'] if r.get('model')==m['id'] and r['status'] in ('done','failed')]
            known=[r['tokens'] for r in runs if r.get('tokens') is not None]
            metrics.append(dict(model=m['id'],provider=m['provider'],completed=len(runs),passed=sum(r['status']=='done' for r in runs),
                                known_tokens=sum(known),usage_complete=len(known)==len(runs)))
        return dict(models=MODELS,providers=PROVIDERS,lineup=LINEUP,efforts=EFFORTS,policy=POLICY,settings=settings(),measurements=metrics)
    if op=='queue':
        snap=snapshot(); byref={t['ref']:t for t in snap['tasks']}; at=now(); out=[]
        for r in active(state()['runs']):
            t=byref.get(r['ref'],{})
            if req.get('project') and r['ref'].split('/')[0]!=req['project']: continue
            if agent and t.get('executor') not in (None,'',agent): continue
            out.append(dict(r, executor=t.get('executor') or '', stale=r['status']=='running' and not lease_alive(r, at)))
        return out[:10]
    if op=='packet': return packet(req.get('ref',''), agent=agent)
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
        return dict(model_plan=model_plan(draft), efforts=EFFORTS, complexity=cx.assess(draft))
    if op=='next':
        snap=snapshot(); at=now(); s=state(); out=[]
        for t in snap['tasks']:
            if req.get('project') and t['project']!=req['project']: continue
            if t['queued_run']: continue   # уже ждёт исполнителя: видна в queue
            if not ready(t, agent): continue
            route=recommendation(t, agent=agent, default=snap['settings']['default_executor'])
            if review_required(t, route, s['runs']) or scope_conflict(t, snap['tasks'], s['runs'], at, req.get('worker')): continue
            out.append(dict(t, route=route))
            if len(out)==5: break
        return out
    extra={}
    with locked():
        s=state(); at=now()
        if op=='plan':
            project=req.get('project'); tasks=prepare_plan(project,req.get('tasks')); validate_plan(project,tasks)
            tasks=create_roots(project,tasks)   # только после полной проверки: план атомарен
            d=read('projects.json',{}); b=d['projects'][project].setdefault('backlog',[])
            # План модели прорабатывается сразу: для каждого ИИ — модель и усилия под задачу.
            b.extend(dict(t,status='queued',model_plan=model_plan(t)) for t in tasks); save('projects.json',d)
            return dict(ok=True, added=[t['id'] for t in tasks], roots=sorted({t['root'] for t in tasks}))
        if op=='focus':
            keys=req.get('projects'); known=snapshot()['projects']
            if not isinstance(keys,list) or any(not isinstance(k,str) or k not in known for k in keys): raise ValueError('Неизвестные проекты фокуса')
            s['focus']=list(dict.fromkeys(keys))
        elif op=='settings':
            changes=req.get('changes',{})
            if not isinstance(changes,dict) or set(changes)-{'default_executor','lease_minutes'}: raise ValueError('Настройки: default_executor, lease_minutes')
            if 'default_executor' in changes and changes['default_executor'] not in PROVIDERS: raise ValueError('Неизвестный ИИ по умолчанию')
            if 'lease_minutes' in changes and (type(changes['lease_minutes'])!=int or not 5<=changes['lease_minutes']<=480): raise ValueError('Аренда: от 5 до 480 минут')
            s.setdefault('settings',{}).update(changes)
        elif op=='split':
            project=req.get('project'); agents=req.get('agents') or list(PROVIDERS)
            snap=snapshot()
            if project not in snap['projects']: raise ValueError('Неизвестный проект')
            if not isinstance(agents,list) or len(agents)<2 or len(set(agents))!=len(agents) or any(a not in PROVIDERS for a in agents): raise ValueError('Нужны минимум два разных ИИ')
            result=split(project, agents, snap['tasks'], s['runs'], at)
            if req.get('dry_run', True): return dict(result, dry_run=True)
            write_patches(s, {ref:dict(executor=a) for ref,a in result['assign'].items()})
            save('workflow.json',s)
            return dict(result, dry_run=False)
        else:
            ref=req.get('ref',''); t=task_for(ref)
            patch={}
            if op=='update':
                changes=req.get('changes',{})
                if not isinstance(changes,dict) or set(changes)-{'priority','model','model_reason','model_plan','acceptance','acceptance_done','risk','work_type','due_date','root','title','why','executor','scope','task_type','new_root','assessment',*TYPE_FIELDS}: raise ValueError('Недопустимые поля изменения')
                if changes.get('assessment'):
                    cx.validate_assessment(changes['assessment'])
                    changes['assessment']=dict(changes['assessment'],reasons=changes['assessment']['reasons'].strip(),by=dict(agent='владелец'),at=at.isoformat())
                new_root=None
                if 'new_root' in changes:
                    if 'root' in changes: raise ValueError('Укажите либо существующий запрос, либо новый')
                    validate_new_root(changes); new_root=changes.pop('new_root')
                for field in ('title','why'):
                    if field in changes and (not isinstance(changes[field],str) or not changes[field].strip() or len(changes[field])>1000): raise ValueError('Заполните '+field)
                if 'root' in changes and not any(r.get('id')==changes['root'] and r.get('project')==t['project'] for r in read('memory/index.json',{}).get('records',[])): raise ValueError('Корень должен принадлежать проекту')
                if 'priority' in changes and changes['priority'] not in ('🔴','🟡','🟢'): raise ValueError('Неверный приоритет')
                if 'risk' in changes and changes['risk'] not in ('low','medium','high'): raise ValueError('Неверный риск')
                if 'work_type' in changes and changes['work_type'] not in WORK_TYPES: raise ValueError('Неверный вид работы')
                if any(r['ref']==ref and r['status']=='running' for r in s['runs']): raise ValueError('Паспорт выполняемой задачи менять нельзя: сначала остановите запуск')
                if 'acceptance' in changes and (not isinstance(changes['acceptance'],list) or not changes['acceptance'] or any(not isinstance(x,str) or not x.strip() for x in changes['acceptance'])): raise ValueError('Нужны критерии')
                if changes.get('model') and not changes.get('model_reason'): raise ValueError('Обоснуйте выбор модели')
                if 'acceptance' in changes and changes['acceptance']!=t.get('acceptance'):
                    changes['acceptance_done']=[]  # Переформулировка требует новой проверки.
                if 'model_plan' in changes:
                    # Частичная правка: строка ИИ заменяется, None возвращает автоматический выбор.
                    if not isinstance(changes['model_plan'],dict) or set(changes['model_plan'])-set(PROVIDERS): raise ValueError('model_plan: только '+', '.join(PROVIDERS))
                    merged=dict(t.get('model_plan') or {})
                    for k,v in changes['model_plan'].items():
                        if v is None: merged.pop(k,None)
                        else: merged[k]=dict(v,source='manual')
                    changes['model_plan']=merged
                validate_details(dict(t,**changes)); recommendation(dict(t,**changes)); patch.update(changes)
                patch['model_plan']=model_plan(dict(t,**changes))
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
            elif op=='status':
                target=req.get('status'); reason=(req.get('reason') or '').strip()
                if target not in MANUAL_STATUSES: raise ValueError('Статус: %s; «Готово» — через завершение с проверкой' % ', '.join(STATUS_LABELS[k] for k in MANUAL_STATUSES))
                current=t.get('status') or 'queued'
                if target==current: raise ValueError('Задача уже в статусе «%s»' % STATUS_LABELS.get(target,target))
                if target in ('blocked','cancelled') and not reason: raise ValueError('Укажите причину: она видна исполнителям и в истории')
                if current=='done' and (target!='queued' or not reason): raise ValueError('Закрытую задачу можно только вернуть в «К выполнению» с причиной')
                if len(reason)>1000: raise ValueError('Причина — до 1000 символов')
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
            elif op in ('claim','heartbeat','finish','fail','cancel','assess'):
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
                    run.update(heartbeat_at=at.isoformat(), lease_until=until)
                    patch['claimed_by']=dict(t.get('claimed_by') or {}, agent=run.get('agent'), worker=run['worker'], run=run['id'], model=run.get('model'), lease_until=until)
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
                    if op=='finish': patch['completed_at']=run['finished_at']
                    branch=((run.get('launch') or {}).get('worktree') or {}).get('branch')
                    if op=='finish' and branch:
                        # Работа в отдельной ветке не в основной ветке проекта: готово — после слияния владельцем.
                        patch.update(status='review', completed_at=None, evidence=req['evidence'],
                                     status_note='Готово в ветке %s — ждёт проверки и слияния владельцем' % branch)
                        extra=dict(status='review', branch=branch)
            else: raise ValueError('Неизвестная операция')
            write_patches(s, {ref:patch} if patch else {})
        save('workflow.json',s)
    return dict(ok=True, **extra)

def main(args=None):
    parser=argparse.ArgumentParser(description='Доски: list/next/packet; изменения через request JSON')
    parser.add_argument('op',choices=['list','next','packet','queue','models','request','worktree']); parser.add_argument('value',nargs='?')
    parser.add_argument('--agent',choices=sorted(PROVIDERS),help='ИИ, для которого строится маршрут (next, packet, queue)')
    a=parser.parse_args(args)
    try:
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
