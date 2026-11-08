"""Причины возврата попытки (TT-113; ТЗ LEARN-04, §6 REQ-RC-01…05, AT-02…04, AT-06).

Правила, которые делают причины честными:
  * у возврата всегда есть причина; без доказательств это `unknown`, а не ошибка модели (REQ-RC-01);
  * автоматика только предлагает причину (`cause_suggested`, confidence=unverified). Статус verified
    даёт воспроизводимое машинное доказательство (ссылка на прогон CI, тест, дефект с тестом) или
    подтверждение владельца или уполномоченного ревьюера. Заявление самой модели о своей вине —
    не подтверждение (REQ-RC-05);
  * ошибка модели (`model_*`) подтверждается, только если ТЗ было достаточным, нужный контекст
    передан, инструменты исправны, а дефект воспроизводится (REQ-RC-04);
  * переклассификация — новое событие `classification_updated`; прежние суждения остаются
    в истории. Основная подтверждённая причина у события одна, сопутствующих может быть несколько
    (REQ-RC-03).
"""
import re

import learning_events as le

CATEGORIES = {
    'spec_ambiguity': ('Неполное или двусмысленное ТЗ', 'Уточнить ТЗ и критерии', 'spec'),
    'missing_context': ('Агент не получил существующее знание', 'Улучшить поиск и пакет контекста', 'context'),
    'bad_decomposition': ('Слишком большая или неверно разделённая задача', 'Декомпозировать', 'decomposition'),
    'tool_failure': ('Сломан агентный инструмент', 'Исправить инструмент или протокол', 'tool'),
    'environment_failure': ('Среда или ресурсы', 'Наладить окружение', 'tool'),
    'test_oracle_error': ('Ошибка в тестах или критериях проверки', 'Исправить тест или эталон', 'spec'),
    'integration_or_conflict': ('Конфликт веток или интеграции', 'Синхронизировать изменения', 'decomposition'),
    'model_instruction_failure': ('Модель нарушила явное правило при полном корректном контексте', 'Анализ повторяемости поведения', 'model'),
    'model_reasoning_failure': ('Неверная логика при достаточных входных данных', 'Проверить паттерн на независимых задачах', 'model'),
    'model_code_generation_failure': ('Систематический дефект генерации кода', 'Проверить примеры и тесты', 'model'),
    'model_tool_use_failure': ('Ошибка выбора или параметров исправного инструмента', 'Улучшить инструкции и демонстрации', 'instructions'),
    'requirement_change': ('Требования изменены после сдачи', 'Новая версия ТЗ, модель не штрафуется', 'spec'),
    'unknown': ('Данных не хватает', 'Собрать доказательства', None),
}
MODEL = tuple(k for k in CATEGORIES if k.startswith('model_'))
CONFIDENCE = ('unverified', 'verified', 'rejected')
MACHINE_PREFIX = ('ci:', 'test:', 'defect:', 'repro:')
CORRECTIVE = ('updated_spec', 'context_added', 'tool_fixed', 'environment_fixed', 'test_fixed', 'decomposed',
              'instructions_updated', 'code_fixed', 'merged_upstream', 'none')
AUTHORIZED = ('owner', 'reviewer', 'qa')        # кто может подтверждать словом (не модель-исполнитель)

# Подсказки по тексту отказа: только предложение, не подтверждение.
HINTS = [
    ('environment_failure', r'out of memory|\boom\b|cuda error|no space left|modulenotfounderror|no module named|'
                            r'command not found|не найден интерпретатор|timeout expired|connection refused|среда|окружени'),
    ('tool_failure', r'patch (?:failed|не примен)|не применил|apply failed|tool error|инструмент'),
    ('test_oracle_error', r'устаревш\w* тест|тест ожидает|wrong expectation|эталон'),
    ('integration_or_conflict', r'merge conflict|конфликт|rebase|не сливается|conflict'),
    ('spec_ambiguity', r'неоднознач|двусмысл|не определено|ambigu|уточн\w* тз'),
    ('missing_context', r'не передан|нет контекст|не знал|missing context|не учтён контракт'),
    ('bad_decomposition', r'слишком крупн|несвязанн\w* изменени|разбить задачу'),
    ('requirement_change', r'новое требование|изменил\w* требован|владелец добавил|requirement change'),
]


def suggest(event):
    """Предложенная причина возврата по тексту и доказательствам; без признаков — unknown."""
    text = ' '.join(str(event.get(k) or '') for k in ('reason', 'detail')).lower()
    for cat, rx in HINTS:
        if re.search(rx, text):
            return dict(category=cat, confidence='unverified', basis='текст отказа: /%s/' % rx.split('|')[0])
    return dict(category='unknown', confidence='unverified', basis='признаков нет')


def history(events, target):
    """Все суждения о событии по порядку: предложение, переклассификации."""
    return [e for e in events if e.get('target_event') == target and e['event_type'] in ('cause_suggested', 'classification_updated')]


def current(events, target):
    """Действующее суждение: последнее classification_updated, иначе предложение, иначе unknown."""
    h = history(events, target)
    upd = [e for e in h if e['event_type'] == 'classification_updated']
    pick = (upd or h or [None])[-1]
    if not pick:
        return dict(category='unknown', confidence='unverified')
    return dict(pick.get('cause') or {}, event_id=pick['event_id'])


def _machine(refs):
    return [r for r in refs or [] if isinstance(r, str) and r.startswith(MACHINE_PREFIX)]


def classify(target, category, evidence=(), by='agent', origin='agent', description='', corrective_action=None,
             contributing=(), preconditions=None, events=None, at=None):
    """Назначить или переклассифицировать причину возврата. Возвращает событие.

    by — кто судит (owner, reviewer, qa, server, agent, local_mimo); origin — источник операции.
    verified ставится, только если есть машинное доказательство или авторизованное подтверждение,
    а для ошибки модели — ещё и подтверждённые предпосылки (ТЗ, контекст, инструменты, воспроизводимость)."""
    if category not in CATEGORIES:
        raise ValueError('Причина: %s' % ', '.join(CATEGORIES))
    if corrective_action is not None and corrective_action not in CORRECTIVE:
        raise ValueError('corrective_action: %s' % ', '.join(CORRECTIVE))
    events = events if events is not None else le.read_all()
    tgt = next((e for e in events if e['event_id'] == target), None)
    if not tgt or tgt['event_type'] not in ('attempt_rejected', 'runtime_unavailable'):
        raise ValueError('Событие возврата не найдено: %s' % target)
    evidence = [str(x)[:200] for x in evidence or []]
    machine = _machine(evidence)
    authorized = origin == 'owner' or (by in AUTHORIZED and origin in ('owner', 'reviewer', 'qa'))
    self_claim = by in ('local_mimo', 'developer') or (tgt.get('agent') and by == tgt.get('agent'))
    why = []
    confidence = 'unverified'
    if category == 'unknown':
        why.append('неизвестная причина не подтверждается')
    elif self_claim and not machine:
        why.append('заявление исполнителя о собственной причине не подтверждение (REQ-RC-05)')
    elif machine or authorized:
        confidence = 'verified'
    else:
        why.append('нет машинного доказательства и подтверждения владельца или ревьюера')
    if category in MODEL and confidence == 'verified':
        pre = preconditions or {}
        missing = [k for k in ('spec_sufficient', 'context_available', 'tools_working', 'reproducible') if pre.get(k) is not True]
        if missing:
            confidence = 'unverified'
            why.append('ошибка модели не доказана: не подтверждено %s (REQ-RC-04)' % ', '.join(missing))
    prev = current(events, target)
    n = len(history(events, target))
    cause = dict(category=category, description=description[:400], confidence=confidence, assigned_by=by,
                 confirmed_by=by if confidence == 'verified' else None,
                 confirmed_at=le.iso(at) if confidence == 'verified' else None,
                 corrective_action=corrective_action, contributing=[c for c in contributing if c in CATEGORIES and c != category],
                 preconditions=preconditions or None, previous=prev.get('category'), why_unverified=why or None)
    return le.record('classification_updated', dict(component='learn', target=target, n=n, category=category, by=by),
                     at=at, origin=origin, project=tgt.get('project'), task_ref=tgt.get('task_ref'), attempt_id=tgt.get('attempt_id'),
                     run_id=tgt.get('run_id'), target_event=target, cause=cause, evidence_refs=evidence)


def suggest_all(events=None):
    """Предложить причины всем возвратам без суждения (фоновая обработка, только unverified)."""
    events = events if events is not None else le.read_all()
    judged = {e.get('target_event') for e in events if e['event_type'] in ('cause_suggested', 'classification_updated')}
    out = []
    for e in events:
        if e['event_type'] == 'attempt_rejected' and e['event_id'] not in judged:
            s = suggest(e)
            out.append(le.make('cause_suggested', dict(component='learn', target=e['event_id'], kind='suggest'),
                               origin='server', project=e.get('project'), task_ref=e.get('task_ref'),
                               attempt_id=e.get('attempt_id'), run_id=e.get('run_id'), target_event=e['event_id'],
                               cause=dict(category=s['category'], confidence='unverified', assigned_by='server', basis=s['basis'])))
    return le.append(out)


def requirement_changes(events):
    """Попытки, после которых ТЗ задачи изменилось (spec_revision другая) — кандидаты requirement_change (AT-04)."""
    by_task = {}
    for e in events:
        if e['event_type'] == 'attempt_submitted' and e.get('spec_revision'):
            by_task.setdefault(e['task_ref'], []).append(e)
    out = {}
    for ref, atts in by_task.items():
        for a, b in zip(atts, atts[1:]):
            if a['spec_revision'] != b['spec_revision']:
                out[a['attempt_id']] = b['spec_revision']
    return out
