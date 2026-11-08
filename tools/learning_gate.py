"""Решение о способе улучшения и ворота эксперимента LoRA (TT-116; ТЗ LEARN-07, §10 REQ-DEC-01…05, §11, AT-12…15, AT-20).

Механизм ничего не обучает и не меняет веса — ни одной функции обучения здесь нет и не будет.
Он строит дерево причин (ТЗ → контекст → инструмент → декомпозиция → инструкции → модель) и
выдаёт рекомендацию со статусом:
  * collecting_data — мало принятых задач или подтверждённых причин;
  * process_fix_first — причины в процессе (ТЗ, контекст, инструменты, декомпозиция, инструкции)
    или подтверждено меньше 80% возвратов: сначала классификация и процесс;
  * not_justified — модельных ошибок нет или паттерн не устойчив;
  * candidate — к РУЧНОМУ рассмотрению владельцем: ≥15 подтверждённых однотипных ошибок модели
    в ≥3 независимых задачах, не из одной задачи, и паттерн повторился после исправления процесса.
Статусы approved_for_experiment, rejected, experiment_failed, experiment_passed, promoted_manually
ставит только владелец. Отсутствие подтверждения — «недостаточно данных», а не «модель плохая».
"""
import json

import learning_causes as lc
import learning_events as le
import learning_metrics as lm

MIN_EVENTS = 15
MIN_TASKS = 3
MAX_TASK_SHARE = 0.5
AUTO = ('collecting_data', 'not_justified', 'process_fix_first', 'candidate')
OWNER = ('approved_for_experiment', 'rejected', 'experiment_failed', 'experiment_passed', 'promoted_manually')
LAYERS = (('spec', 'ТЗ: уточнить требования и критерии'), ('context', 'Контекст: добавить или исправить память и поиск'),
          ('tool', 'Инструмент: стабилизировать команды, patch, окружение'),
          ('decomposition', 'Декомпозиция: уменьшить задачу, зависимости, scope'),
          ('instructions', 'Инструкции: формулировка и контроль правил агента'),
          ('model', 'Модель: только устойчивый паттерн после устранения 1–5'))
PROCESS_FIXES = ('updated_spec', 'context_added', 'tool_fixed', 'environment_fixed', 'test_fixed', 'decomposed', 'instructions_updated')


def tree(events):
    """Подтверждённые причины возвратов по слоям дерева (REQ-DEC-01) и неподтверждённые отдельно."""
    rej = [e for e in events if e['event_type'] == 'attempt_rejected']
    layers = {k: dict(count=0, categories={}, title=t) for k, t in LAYERS}
    unverified = 0; model = {}
    for r in rej:
        c = lc.current(events, r['event_id'])
        if c.get('confidence') != 'verified' or c.get('category') in (None, 'unknown'):
            unverified += 1; continue
        if c['category'] == 'requirement_change':
            layers['spec']['categories']['requirement_change'] = layers['spec']['categories'].get('requirement_change', 0) + 1
            continue
        layer = lc.CATEGORIES[c['category']][2] or 'spec'
        layers[layer]['count'] += 1
        layers[layer]['categories'][c['category']] = layers[layer]['categories'].get(c['category'], 0) + 1
        if c['category'] in lc.MODEL:
            model.setdefault(c['category'], []).append(dict(event=r['event_id'], task=r.get('task_ref'), at=r['occurred_at'],
                                                            corrective=c.get('corrective_action')))
    return dict(layers=layers, unverified=unverified, rejections=len(rej), model=model)


def _pattern(cases, events):
    tasks = {}
    for c in cases:
        tasks[c['task']] = tasks.get(c['task'], 0) + 1
    biggest = max(tasks.values()) / len(cases) if cases else 0
    # Повтор после исправления процесса: была процессная мера по этой же категории — и ошибка случилась позже.
    fixes = [e for e in events if e['event_type'] == 'classification_updated' and (e.get('cause') or {}).get('corrective_action') in PROCESS_FIXES]
    first_fix = min((f['occurred_at'] for f in fixes), default=None)
    after = [c for c in cases if first_fix and c['at'] > first_fix]
    return dict(events=len(cases), tasks=len(tasks), biggest_task_share=round(biggest, 2), after_process_fix=len(after),
                examples=[c['event'] for c in cases[:5]])


def recommend(events=None, metrics=None, project=None, model_config_id=None, at=None, record=True):
    """Рекомендация (REQ-DEC-02). Записывается событием decision_recorded; повтор того же вывода — тот же id."""
    events = events if events is not None else le.read_all()
    if project:
        events = [e for e in events if not e.get('project') or e.get('project') == project]
    rep = metrics or lm.compute(events=events, project=project, model_config_id=model_config_id)
    tr = tree(events)
    m07 = rep['metrics']['M07']
    reasons, alternatives, status, pattern = [], [], None, None
    if rep['accepted'] < lm.FIRST_ANALYSIS:
        status = 'collecting_data'
        reasons.append('принято %d из %d задач первой точки анализа' % (rep['accepted'], lm.FIRST_ANALYSIS))
    if m07['denominator'] and m07['value'] is not None and m07['value'] < lm.DIAG_ALERT:
        status = status or 'process_fix_first'
        reasons.append('подтверждено %.0f%% причин возвратов (< 80%%): сначала классификация' % (100 * m07['value']))
    process = sorted(((v['count'], k) for k, v in tr['layers'].items() if k != 'model' and v['count']), reverse=True)
    model_total = tr['layers']['model']['count']
    if process and process[0][0] >= model_total:
        status = status or 'process_fix_first'
        alternatives = [dict(layer=k, count=n, action=dict(LAYERS)[k]) for n, k in process]
        reasons.append('основной слой причин — %s (%d)' % (process[0][1], process[0][0]))
    best = None
    for cat, cases in tr['model'].items():
        p = dict(_pattern(cases, events), category=cat)
        if not best or p['events'] > best['events']:
            best = p
    if best:
        ok = best['events'] >= MIN_EVENTS and best['tasks'] >= MIN_TASKS and best['biggest_task_share'] <= MAX_TASK_SHARE
        pattern = dict(best, sufficient=ok)
        if not ok:
            reasons.append('паттерн %s: %d событий в %d задачах (нужно ≥%d и ≥%d, без доминирования одной задачи)' % (
                best['category'], best['events'], best['tasks'], MIN_EVENTS, MIN_TASKS))
        elif not best['after_process_fix']:
            status = status or 'process_fix_first'
            reasons.append('паттерн не проверен после исправления процесса (инструкции, контекст, инструменты)')
        else:
            status = status or 'candidate'
    status = status or 'not_justified'
    if status == 'not_justified' and not reasons:
        reasons.append('подтверждённых устойчивых ошибок модели нет' if not best else 'паттерн недостаточен')
    window = dict(accepted=rep['accepted'], rejections=tr['rejections'])
    did = 'DEC-' + le.digest(json.dumps(dict(project=project, cfg=model_config_id, status=status, pattern=(pattern or {}).get('category'),
                                             window=window), sort_keys=True))[7:19]
    rec = dict(decision_id=did, status=status, project=project, model_config_id=model_config_id, pattern=pattern,
               sample=dict(accepted=rep['accepted'], rejections=tr['rejections'], unverified=tr['unverified']),
               diagnostics_coverage=m07['value'], alternatives=alternatives, reasons=reasons,
               proposed=('эксперимент LoRA на отдельной машине — только решением владельца' if status == 'candidate'
                         else (alternatives[0]['action'] if alternatives else 'продолжать сбор данных')),
               risk='регрессия контрольных навыков; оценка на независимом отложенном наборе обязательна' if status == 'candidate' else None,
               check_plan='сравнение базовой и кандидатной версии на закрытом holdout (§11.3)' if status == 'candidate' else None,
               approval='ожидает владельца' if status == 'candidate' else 'не требуется', tree=tr['layers'],
               training_started=False)
    if record:
        le.record('decision_recorded', dict(component='learn', decision=did), at=at, origin='server', project=project,
                  model_config_id=model_config_id, decision=did, result=status, data=dict(reasons=reasons[:6], pattern=pattern))
    return rec


def set_status(decision_id, status, origin, reason=''):
    """Ручной статус рекомендации. Только владелец; ни один статус не запускает обучение и не меняет веса."""
    if origin != 'owner':
        raise ValueError('Статус рекомендации меняет только владелец')
    if status not in OWNER:
        raise ValueError('Статус владельца: %s' % ', '.join(OWNER))
    events = le.read_all()
    if not any(e['event_type'] == 'decision_recorded' and e.get('decision') == decision_id for e in events):
        raise ValueError('Рекомендация не найдена')
    if status == 'approved_for_experiment':
        last = [e for e in events if e['event_type'] == 'decision_recorded' and e.get('decision') == decision_id][-1]
        if last.get('result') != 'candidate':
            raise ValueError('Одобрить эксперимент можно только рекомендацию со статусом candidate')
    n = sum(1 for e in events if e['event_type'] == 'decision_status' and e.get('decision') == decision_id)
    return le.record('decision_status', dict(component='learn', decision=decision_id, n=n, status=status), origin=origin,
                     decision=decision_id, result=status, reason=reason, data=dict(training_started=False))


def request_training(by='local_mimo', reason=''):
    """Просьба «обучи меня» (AT-12): только рекомендация по данным, обучения нет."""
    rec = recommend()
    return dict(training_started=False, by=by, status=rec['status'], reasons=rec['reasons'],
                note='Обучение не запускается автоматически: эксперимент — решение владельца по рекомендации candidate')


# ------------------------------------------------------------ набор для будущего эксперимента (§11.1, AT-20)

QUARANTINE_REASONS = ('нет события-источника в журнале', 'метка не подтверждена владельцем или проверкой',
                      'причина возврата не подтверждена доказательствами', 'пример из чужого проекта')


def quarantine(items, project=None, events=None):
    """Карантин обучающих примеров (SEC-019, SEC-TC-21): в набор идёт только пример с происхождением из журнала
    опыта (событие с цепочкой хешей), подтверждённой меткой и подтверждённой причиной возврата. Остальное — в
    карантин с причиной; ничего не удаляется. Сам набор не формируется и обучение не запускается."""
    events = le.read_all() if events is None else events
    by_id = {e['event_id']: e for e in events}
    ok, held = [], []
    for it in items or []:
        why = []
        src = by_id.get(it.get('event_id'))
        if not src:
            why.append(QUARANTINE_REASONS[0])
        if not it.get('verified'):
            why.append(QUARANTINE_REASONS[1])
        if src and src.get('event_type') == 'attempt_rejected':
            c = lc.current(events, src['event_id'])
            if c.get('confidence') != 'verified':
                why.append(QUARANTINE_REASONS[2])
        if project and (src or {}).get('project') not in (None, project):
            why.append(QUARANTINE_REASONS[3])
        if it.get('has_secret') or le.had_secret(it):
            why.append('секрет в примере')
        (held if why else ok).append(dict(it, quarantine=why) if why else it)
    le.record('training_quarantine', dict(component='learn', items=len(items or [])), origin='server',
              result='ok' if not held else 'held', data=dict(accepted=len(ok), held=len(held)))
    return dict(accepted=ok, quarantined=held, training_started=False)


def check_split(items):
    """Проверка манифеста набора: одна задача — в одной части, лицензия разрешена, секретов нет."""
    problems = []; where = {}
    for i, it in enumerate(items or []):
        ref, split = it.get('task_ref'), it.get('split')
        if split not in ('train', 'validation', 'holdout'):
            problems.append('строка %d: split train|validation|holdout' % i)
        if not it.get('verified'):
            problems.append('строка %d (%s): пример не подтверждён' % (i, ref))
        if it.get('license_ok') is not True:
            problems.append('строка %d (%s): нет разрешения на использование' % (i, ref))
        if it.get('has_secret') or le.had_secret(it):
            problems.append('строка %d (%s): секрет' % (i, ref))
        where.setdefault(ref, set()).add(split)
    leak = sorted(r for r, s in where.items() if len(s) > 1)
    for r in leak:
        problems.append('утечка: задача %s одновременно в %s' % (r, ', '.join(sorted(where[r]))))
    ok = not problems and bool(items)
    manifest = le.digest(json.dumps([{k: it.get(k) for k in ('task_ref', 'split', 'attempt_id')} for it in items or []], sort_keys=True))
    le.record('export_checked', dict(component='learn', manifest=manifest), origin='server', result='ok' if ok else 'rejected',
              data=dict(problems=problems[:20], items=len(items or [])))
    return dict(ok=ok, problems=problems, leak=leak, manifest=manifest, exported=False)


def register_eval(name, refs, origin, purpose='holdout'):
    """Метаданные независимого набора оценки: только ссылки и хеш, без ответов тестов."""
    if origin != 'owner':
        raise ValueError('Набор оценки регистрирует владелец')
    if not name or not refs:
        raise ValueError('Нужны имя и задачи набора')
    path = le.root() / 'eval_registry.json'
    doc = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(schema_version=1, sets=[])
    entry = dict(name=name, purpose=purpose, tasks=len(refs), refs_digest=le.digest(sorted(refs)), at=le.iso())
    doc['sets'] = [s for s in doc['sets'] if s['name'] != name] + [entry]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
    le.record('eval_registered', dict(component='learn', name=name, digest=entry['refs_digest']), origin=origin,
              data=dict(name=name, purpose=purpose, tasks=len(refs)))
    return entry
