"""Доски и контракт исполнения. Ручной ввод, без автоматического сбора портфеля."""
import argparse
import contextlib
import copy
import json
import os
from pathlib import Path
import tempfile
from datetime import date, datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://developers.openai.com/api/docs/guides/model-selection'
MODELS = [
    dict(id='gpt-6-luna', tier=1, strength='Локальные правки, типовые тесты, извлечение и форматирование', weakness='Сложные зависимости и неопределённая архитектура требуют проверки', effort='medium'),
    dict(id='gpt-6.1-sol', tier=2, strength='Разработка, интеграции и задачи с несколькими файлами', weakness='Стоимость выше Luna; для простых правок избыточна', effort='medium'),
    dict(id='gpt-6-astra', tier=3, strength='Сложные исследования, архитектура и трудная диагностика', weakness='Высокий расход; применять при измеримой необходимости', effort='high'),
    dict(id='gpt-6-sol', tier=2, strength='Предыдущее поколение для разработки', weakness='Нужна сравнительная проверка с 6.1 Sol', effort='medium'),
    dict(id='gpt-5.6-sol', tier=2, strength='Совместимость с существующими процессами', weakness='Старое поколение; не назначается автоматически', effort='medium'),
    dict(id='gpt-5.6-terra', tier=1, strength='Несложные структурированные задачи', weakness='Старое поколение; выбор только после оценки', effort='medium'),
    dict(id='gpt-5.6-luna', tier=1, strength='Простые задачи предыдущего поколения', weakness='Старое поколение; выбор только после оценки', effort='medium'),
]
POLICY = dict(version=1, verified='2026-10-19', source=SOURCE,
    availability='Снимок списка моделей Codex этого хоста на 19.10.2026; проверять перед исполнением',
    evidence='Рекомендации — начальные эвристики, не результаты локального сравнения моделей',
    rules=['Сначала декомпозиция с критериями приёмки и зависимостями.',
           'Выбирать минимальный достаточный класс модели; неизвестную задачу сначала уточнить.',
           'Читать паспорт, затем только необходимые файлы и записи recall; не читать портфель целиком.',
           'Использовать поиск, локальные скрипты и кеш неизменных результатов вместо повторного анализа.',
           'После двух неудачных попыток остановиться, сохранить диагностику и пересмотреть модель или декомпозицию.',
           'При исчерпании бюджета сохранить результат и запросить пересмотр; не продолжать молча.',
           'Закрывать задачу только с результатами проверок; учитывать полный расход вместе с повторами.',
           'Сравнивать модели на одинаковых критериях: качество, токены, время, доля повторов.'])

def data_dir():
    return Path(os.environ.get('TASKTREKER_DATA') or ROOT / 'data')

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
    try: fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError: raise ValueError('Другая операция уже меняет задачи; повторите позже')
    try:
        os.close(fd)
        yield
    finally: path.unlink()

def state():
    return read('workflow.json', dict(schema=1, focus=[], tasks={}, runs=[]))

def recommendation(task):
    size = task.get('size', 'M'); risk = task.get('risk', 'unknown')
    kind = task.get('work_type', 'unknown')
    if risk == 'high' or kind in ('architecture', 'research'):
        model, reason, budget = 'gpt-6-astra', 'Высокий риск или исследовательская неопределённость', 24000
    elif size == 'S' and risk == 'low' and kind in ('docs', 'test', 'implementation'):
        model, reason, budget = 'gpt-6-luna', 'Малая задача с низким риском и проверяемым результатом', 6000
    else:
        model, reason, budget = 'gpt-6.1-sol', 'Интеграция или недостаточно данных для уверенного выбора Luna', 12000
    if task.get('model'):
        model = task['model']; reason = task.get('model_reason') or 'Явный выбор исполнителя'
    entry = next((m for m in MODELS if m['id'] == model), None)
    if not entry: raise ValueError('Модель отсутствует в каталоге')
    budget={1:6000,2:12000,3:24000}[entry['tier']]
    return dict(model=model, effort=entry['effort'], reason=reason,
                context_budget=2000, total_token_budget=budget, max_attempts=2,
                provisional=risk == 'unknown' or kind == 'unknown', policy_version=1)

def snapshot():
    projects = read('projects.json', {}).get('projects', {}); s = state(); tasks=[]
    for key, project in projects.items():
        for original in project.get('backlog', []):
            t = dict(original); ref = key + '/' + t['id']
            t.update(s['tasks'].get(ref, {})); t.update(project=key, ref=ref)
            t['route'] = recommendation(t)
            tasks.append(t)
    byref = {t['ref']: t for t in tasks}
    for t in tasks:
        t['blocked_by'] = [dep for dep in t.get('depends_on', [])
            if byref.get(dep if '/' in dep else t['project']+'/'+dep, {}).get('status') != 'done']
        t['needs_plan'] = not bool(t.get('acceptance')) or not t.get('root') or t.get('size') == 'XL'
        failures=[r for r in s['runs'] if r['ref']==t['ref'] and r['status']=='failed' and r.get('packet',{}).get('route',{}).get('model')==t['route']['model']]
        t['review_required']=len(failures)>=t['route']['max_attempts'] or sum(r.get('tokens') or 0 for r in failures)>=t['route']['total_token_budget']
        priority = {'🔴': 0, '🟡': 1, '🟢': 2}.get(t.get('priority'), 1)
        t['rank'] = [0 if t['project'] in s['focus'] else 1, priority, {'S':1,'M':3,'L':8,'XL':20}.get(t.get('size'), 3), t['ref']]
    tasks.sort(key=lambda t:t['rank'])
    roots=[{k:r.get(k) for k in ('id','title','project')} for r in read('memory/index.json',{}).get('records',[])
           if r.get('kind')=='запрос' and r.get('status')=='активна']
    return dict(projects={k:dict(name=p.get('name',k), path=p.get('path','')) for k,p in projects.items()},
                tasks=tasks, roots=roots, focus=s['focus'], runs=s['runs'][-50:], models=MODELS, policy=POLICY)

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

def task_for(ref):
    t = next((t for t in snapshot()['tasks'] if t['ref'] == ref), None)
    if not t: raise ValueError('Нет такой задачи: '+ref)
    return t

def packet(ref):
    t=task_for(ref)
    if t['needs_plan']: raise ValueError('Сначала заполните root, критерии приёмки и разбейте XL')
    if t['review_required']: raise ValueError('Лимит попыток/токенов достигнут: разбейте задачу или обоснуйте другую модель')
    if t['blocked_by']: raise ValueError('Не закрыты зависимости: '+', '.join(t['blocked_by']))
    result={k:t.get(k) for k in ('ref','title','why','root','acceptance','depends_on','route')}
    result['instructions']=POLICY['rules']
    result['read_next']=['tt_context(project='+t['project']+', budget=1200)', 'tt_recall(query='+t['root']+')']
    result['launch']='Исполнитель проверяет доступность модели, принимает запуск через claim и выполняет критерии. Пакет не меняет модель текущего чата.'
    # Approximation is deliberately conservative and explicitly not billing tokens.
    if len(json.dumps(result,ensure_ascii=False)) > 6000:
        raise ValueError('Паспорт слишком велик: сократите критерии и вынесите детали в память')
    return result

def validate_plan(project, tasks):
    projects=read('projects.json',{}).get('projects',{})
    if project not in projects: raise ValueError('Неизвестный проект')
    if not isinstance(tasks,list) or not 1 <= len(tasks) <= 50: raise ValueError('План: от 1 до 50 задач')
    existing={t['id']:t for t in projects[project].get('backlog',[])}
    ids=set()
    for t in tasks:
        if not isinstance(t,dict): raise ValueError('Задача должна быть объектом')
        for key in ('id','title','why','root'):
            if not isinstance(t.get(key),str) or not t[key].strip() or len(t[key])>1000: raise ValueError('Не заполнено поле '+key)
        if '/' in t['id'] or t['id'] in ids or t['id'] in existing: raise ValueError('Повтор или неверный id')
        ids.add(t['id'])
        if t.get('size') not in ('S','M','L'): raise ValueError('Размер S/M/L; XL сначала разбить')
        if t.get('priority') not in ('🔴','🟡','🟢'): raise ValueError('Укажите приоритет 🔴/🟡/🟢')
        if t.get('risk') not in ('low','medium','high'): raise ValueError('Укажите риск low/medium/high')
        if t.get('work_type') not in ('docs','test','implementation','architecture','research'): raise ValueError('Укажите вид работы')
        a=t.get('acceptance')
        if not isinstance(a,list) or not a or any(not isinstance(v,str) or not v.strip() for v in a): raise ValueError('Нужны критерии приёмки')
        deps=t.get('depends_on',[])
        if not isinstance(deps,list) or any(not isinstance(v,str) for v in deps): raise ValueError('depends_on должен быть списком id')
        record=read('memory/index.json',{}).get('records',[])
        if not any(r.get('id')==t['root'] and r.get('project')==project for r in record): raise ValueError('root должен ссылаться на память этого проекта')
        recommendation(t)
        validate_details(t)
    graph={**{k:v.get('depends_on',[]) for k,v in existing.items()}, **{t['id']:t.get('depends_on',[]) for t in tasks}}
    def visit(key, stack):
        if key in stack: raise ValueError('Цикл зависимостей')
        if key not in graph: raise ValueError('Неизвестная зависимость '+key)
        for dep in graph[key]: visit(dep,stack|{key})
    for t in tasks: visit(t['id'],set())

def action(req):
    op=req.get('op','list')
    if op=='list':
        snap=snapshot(); tasks=[t for t in snap['tasks'] if not req.get('project') or t['project']==req['project']]
        return dict(focus=snap['focus'], total=len(tasks), tasks=[{k:t.get(k) for k in ('ref','title','status','priority','needs_plan','blocked_by','route')} for t in tasks[:20]], more='next / packet <ref>; list с project для уточнения')
    if op=='models':
        metrics=[]
        for m in MODELS:
            runs=[r for r in state()['runs'] if r.get('model')==m['id'] and r['status'] in ('done','failed')]
            known=[r['tokens'] for r in runs if r.get('tokens') is not None]
            metrics.append(dict(model=m['id'],completed=len(runs),passed=sum(r['status']=='done' for r in runs),
                                known_tokens=sum(known),usage_complete=len(known)==len(runs)))
        return dict(models=MODELS,policy=POLICY,measurements=metrics)
    if op=='queue': return [r for r in state()['runs'] if r['status'] in ('queued','running') and (not req.get('project') or r['ref'].split('/')[0]==req['project'])][:10]
    if op=='packet': return packet(req.get('ref',''))
    if op=='next':
        snap=snapshot(); active={r['ref'] for r in snap['runs'] if r['status'] in ('queued','running')}
        return [t for t in snap['tasks'] if t['ref'] not in active and t.get('status') not in ('done','cancelled','in_progress','review','blocked') and not t['blocked_by'] and not t['needs_plan'] and not t['review_required'] and (not req.get('project') or t['project']==req['project'])][:5]
    with locked():
        s=state()
        if op=='plan':
            project=req.get('project'); tasks=req.get('tasks'); validate_plan(project,tasks)
            d=read('projects.json',{}); b=d['projects'][project].setdefault('backlog',[])
            b.extend(dict(t,status='queued') for t in tasks); save('projects.json',d)
            return dict(ok=True, added=[t['id'] for t in tasks])
        if op=='focus':
            keys=req.get('projects'); known=snapshot()['projects']
            if not isinstance(keys,list) or any(not isinstance(k,str) or k not in known for k in keys): raise ValueError('Неизвестные проекты фокуса')
            s['focus']=list(dict.fromkeys(keys))
        else:
            ref=req.get('ref',''); t=task_for(ref)
            patch=s['tasks'].setdefault(ref,{})
            if op=='update':
                changes=req.get('changes',{})
                if not isinstance(changes,dict) or set(changes)-{'priority','model','model_reason','acceptance','acceptance_done','risk','work_type','due_date','root','title','why'}: raise ValueError('Недопустимые поля изменения')
                for field in ('title','why'):
                    if field in changes and (not isinstance(changes[field],str) or not changes[field].strip() or len(changes[field])>1000): raise ValueError('Заполните '+field)
                if 'root' in changes and not any(r.get('id')==changes['root'] and r.get('project')==t['project'] for r in read('memory/index.json',{}).get('records',[])): raise ValueError('Корень должен принадлежать проекту')
                if 'priority' in changes and changes['priority'] not in ('🔴','🟡','🟢'): raise ValueError('Неверный приоритет')
                if 'risk' in changes and changes['risk'] not in ('low','medium','high'): raise ValueError('Неверный риск')
                if 'work_type' in changes and changes['work_type'] not in ('docs','test','implementation','architecture','research'): raise ValueError('Неверный вид работы')
                if any(r['ref']==ref and r['status']=='running' for r in s['runs']): raise ValueError('Паспорт выполняемой задачи менять нельзя: сначала остановите запуск')
                if 'acceptance' in changes and (not isinstance(changes['acceptance'],list) or not changes['acceptance'] or any(not isinstance(x,str) or not x.strip() for x in changes['acceptance'])): raise ValueError('Нужны критерии')
                if changes.get('model') and not changes.get('model_reason'): raise ValueError('Обоснуйте выбор модели')
                if 'acceptance' in changes and changes['acceptance']!=t.get('acceptance'):
                    changes['acceptance_done']=[]  # Переформулировка требует новой проверки.
                validate_details(dict(t,**changes)); recommendation(dict(t,**changes)); patch.update(changes)
            elif op=='complete':
                packet(ref)
                if t.get('status') in ('done','cancelled'): raise ValueError('Задача уже закрыта')
                if any(r['ref']==ref and r['status'] in ('queued','running') for r in s['runs']): raise ValueError('Завершите или отмените запуск через его контракт')
                if len(t.get('acceptance_done',[]))!=len(t.get('acceptance',[])): raise ValueError('Сначала проверьте все критерии')
                if not isinstance(req.get('evidence'),str) or not req['evidence'].strip(): raise ValueError('Опишите результат проверки')
                patch.update(status='done',evidence=req['evidence'],completed_at=datetime.now(timezone.utc).isoformat())
            elif op=='enqueue':
                p=packet(ref)
                if t.get('status') in ('done','cancelled','in_progress','review','blocked'): raise ValueError('Статус не допускает запуск')
                if any(r['ref']==ref and r['status'] in ('queued','running') for r in s['runs']): raise ValueError('Задача уже запущена или ожидает исполнителя')
                s['runs'].append(dict(id='RUN-'+str(len(s['runs'])+1),ref=ref,status='queued',packet=p,created_at=datetime.now(timezone.utc).isoformat()))
            elif op in ('claim','finish','fail','cancel'):
                run=next((r for r in s['runs'] if r['id']==req.get('run') and r['ref']==ref),None)
                if not run: raise ValueError('Запуск не найден')
                if op=='claim':
                    if run['status']!='queued': raise ValueError('Запуск уже принят или завершён')
                    p=packet(ref)
                    if req.get('model')!=p['route']['model']: raise ValueError('Фактическая модель должна совпадать с паспортом')
                    if not req.get('worker'): raise ValueError('Укажите исполнителя/чат')
                    run.update(status='running',worker=req['worker'],model=req['model'],packet=p)
                    patch['status']='in_progress'
                else:
                    if run['status'] not in (('queued','running') if op=='cancel' else ('running',)): raise ValueError('Запуск не выполняется')
                    if not isinstance(req.get('evidence'),str) or not req['evidence'].strip(): raise ValueError('Нужны результаты проверок или причина остановки')
                    tokens=req.get('tokens')
                    if tokens is not None and (type(tokens)!=int or tokens<0): raise ValueError('Токены должны быть целым неотрицательным числом')
                    run.update(status={'finish':'done','fail':'failed','cancel':'cancelled'}[op],evidence=req['evidence'],tokens=tokens,finished_at=datetime.now(timezone.utc).isoformat())
                    patch['status']='done' if op=='finish' else 'queued'
                    if op=='finish': patch['completed_at']=run['finished_at']
            else: raise ValueError('Неизвестная операция')
            # Явный ввод пользователя/исполнителя: канонический бэклог виден
            # старым потребителям (дерево, оценка, Obsidian), без импорта фактов.
            if patch:
                d=read('projects.json',{})
                original=next(v for v in d['projects'][t['project']]['backlog'] if v['id']==t['id'])
                original.update(patch); save('projects.json',d)
                s['tasks'].pop(ref,None)
        save('workflow.json',s)
    return dict(ok=True)

def main(args=None):
    parser=argparse.ArgumentParser(description='Доски: list/next/packet; изменения через request JSON')
    parser.add_argument('op',choices=['list','next','packet','queue','models','request']); parser.add_argument('value',nargs='?')
    a=parser.parse_args(args)
    try:
        req=json.loads(Path(a.value).read_text(encoding='utf-8-sig')) if a.op=='request' else dict(op=a.op,ref=a.value)
        print(json.dumps(action(req),ensure_ascii=False,indent=2)); return 0
    except (ValueError,OSError) as e:
        print(str(e)); return 1

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8'); sys.exit(main())
