"""Куратор знаний: карточки опыта в существующей памяти (TT-115; ТЗ LEARN-06, §8 REQ-MEM-01…07, AT-07, AT-08).

Второй базы знаний нет: карточка — обычная запись `data/memory/*.md` (kind «решение», статус
«активна» по семантике memory.py). Состояние проверки — поле `learning`:
  * candidate — предложена из подтверждённого возврата, в контекст разработчика НЕ попадает;
  * verified — проверена владельцем, ревьюером или объективной машинной проверкой: есть задача,
    доказательство, границы применимости, нет секретов (REQ-MEM-03).
Устаревание — штатное `memory.retire` (статус «устарела»/«заменена», superseded_by): такая карточка
из выдачи исключается (AT-08). Поле `not_when` — условия, когда решение не применять.

Выдача разработчику узкая: проект, пересечение scope и файлов, вид задачи, лимит токенов; в событие
пишутся только id и версия (хеш тела) выданных карточек (REQ-MEM-04, REQ-MEM-05).
"""
import re

import learning_causes as lc
import learning_events as le

DEFAULT_BUDGET = 1500
LIMIT = 5
SECTIONS = ('Проблема', 'Условия применимости', 'Подтверждённая причина', 'Проверенное решение', 'Доказательства',
            'Когда решение НЕ применять')


def _mem():
    import memory
    return memory


def card_body(problem, applicability, cause, solution, evidence, not_when):
    parts = []
    for title, text in zip(SECTIONS, (problem, applicability, cause, solution, '\n'.join('- %s' % e for e in evidence), not_when)):
        parts.append('## %s\n%s' % (title, (text or '—').strip()))
    return '\n\n'.join(parts)


def propose(target_event, title, problem, applicability, solution, not_when, scope, by='server', origin='agent',
            events=None, project=None, source_commit=None):
    """Кандидатная карточка из подтверждённого возврата (REQ-MEM-01). Секреты — отказ, не вычистка."""
    events = events if events is not None else le.read_all()
    rej = next((e for e in events if e['event_id'] == target_event), None)
    if not rej:
        raise ValueError('Событие возврата не найдено')
    cause = lc.current(events, target_event)
    if cause.get('confidence') != 'verified':
        raise ValueError('Карточка — только из подтверждённой причины (сейчас: %s, %s)' % (cause.get('category'), cause.get('confidence')))
    if not scope or not isinstance(scope, list):
        raise ValueError('Нужны границы применимости: scope — список путей или областей')
    evidence = list(dict.fromkeys((rej.get('evidence_refs') or []) + ['task:%s' % rej.get('task_ref')]))
    body = card_body(problem, applicability, '%s: %s' % (cause['category'], cause.get('description') or lc.CATEGORIES[cause['category']][0]),
                     solution, evidence, not_when)
    if le.had_secret(body) or le.had_secret(title):
        raise ValueError('В карточке похоже на секрет — уберите его; карточка не сохранена')
    m = _mem()
    rec = m.new_record(title, body, project=project or rej.get('project'), kind='решение', source='learn:%s' % target_event,
                       tags=['опыт', 'learn'], learning='candidate', source_task=rej.get('task_ref'), source_run=rej.get('run_id'),
                       source_event=target_event, source_commit=source_commit, scope=scope, not_when=not_when or '',
                       verified_by=None)
    m.save(rec); m.build_index()
    le.record('knowledge_candidate', dict(component='learn', knowledge=rec['id']), origin=origin, project=rec['project'],
              task_ref=rej.get('task_ref'), target_event=target_event, knowledge_refs=[dict(id=rec['id'], version=le.digest(body))],
              detail='предложил %s' % by)
    return rec


def verify(knowledge_id, by, origin, evidence=()):
    """Проверить карточку: владелец или ревьюер, либо машинное доказательство; модель сама себе не подтверждает."""
    m = _mem()
    rec = m.get(knowledge_id)
    if not rec or rec.get('learning') not in ('candidate', 'verified'):
        raise ValueError('Карточка опыта не найдена')
    machine = [e for e in evidence or [] if str(e).startswith(lc.MACHINE_PREFIX)]
    if by in ('local_mimo', 'developer'):
        raise ValueError('Исполнитель не подтверждает собственный опыт (REQ-MEM-03)')
    if origin != 'owner' and not machine and by not in ('reviewer', 'qa'):
        raise ValueError('Подтверждает владелец, ревьюер или машинная проверка (evidence ci:/test:)')
    body = rec.get('body') or ''
    missing = [s for s in ('Условия применимости', 'Доказательства') if '## ' + s not in body] + \
              (['source_task'] if not rec.get('source_task') else []) + (['scope'] if not rec.get('scope') else [])
    if missing:
        raise ValueError('Карточка неполна: %s' % ', '.join(missing))
    if le.had_secret(body):
        raise ValueError('В карточке похоже на секрет')
    if rec.get('status') != 'активна':
        raise ValueError('Карточка выведена из оборота (%s)' % rec.get('status'))
    rec.update(learning='verified', verified_by=by, verified_at=le.iso(), verified_evidence=list(evidence or [])[:10])
    m.save(rec); m.build_index()
    le.record('knowledge_verified', dict(component='learn', knowledge=knowledge_id, version=le.digest(body)), origin=origin,
              project=rec['project'], task_ref=rec.get('source_task'),
              knowledge_refs=[dict(id=knowledge_id, version=le.digest(body))], evidence_refs=list(evidence or []), detail=by)
    return rec


def reject(knowledge_id, why, origin, by=None):
    m = _mem()
    rec = m.get(knowledge_id)
    if not rec or not rec.get('learning'):
        raise ValueError('Карточка опыта не найдена')
    m.retire(knowledge_id, why=why, by=by); m.build_index()
    le.record('knowledge_rejected', dict(component='learn', knowledge=knowledge_id, by=by or ''), origin=origin,
              project=rec['project'], knowledge_refs=[dict(id=knowledge_id)], reason=why)
    return m.get(knowledge_id)


def candidates(project=None):
    return [i for i in _mem().load_index()['records'] if i.get('learning') == 'candidate' and i['status'] == 'активна'
            and (not project or i['project'] == project)]


def _overlap(a, b):
    def norm(p):
        return str(p).replace('\\', '/').strip('/').rstrip('*').rstrip('/').lower()
    for x in a or ():
        for y in b or ():
            x_, y_ = norm(x), norm(y)
            if x_ and y_ and (x_ == y_ or x_.startswith(y_ + '/') or y_.startswith(x_ + '/')):
                return True
    return False


def retrieve(task, files=None, budget=DEFAULT_BUDGET, limit=LIMIT, index=None):
    """Проверенные действующие карточки для задачи: узкий отбор по проекту и scope, лимит токенов (REQ-MEM-02/04)."""
    m = _mem()
    idx = index or m.load_index()
    words = set(re.findall(r'\w{4,}', ' '.join(str(task.get(k) or '') for k in ('title', 'why')).lower()))
    area = list(task.get('scope') or []) + list(files or [])
    picks = []
    for i in idx['records']:
        if i.get('learning') != 'verified' or i['status'] != 'активна' or i.get('superseded_by'):
            continue
        if i['project'] not in (task.get('project'), 'портфель'):
            continue
        if not _overlap(i.get('scope'), area):
            continue
        nw = set(re.findall(r'\w{4,}', str(i.get('not_when') or '').lower()))
        if nw and len(nw & words) >= 2:
            continue                       # условие «когда не применять» совпало с задачей
        score = len(words & set(i.get('keywords') or [])) + (2 if i['project'] == task.get('project') else 0)
        picks.append((score, i))
    picks.sort(key=lambda x: (-x[0], x[1]['updated']), reverse=False)
    out, spent = [], 0
    for _, i in picks[:limit * 2]:
        rec = m.get(i['id'])
        if not rec:
            continue
        body = rec.get('body') or ''
        cost = max(1, len(body) // 3)
        if spent + cost > budget:
            continue
        spent += cost
        out.append(dict(id=i['id'], title=i['title'], version=le.digest(body), scope=i.get('scope'), text=body,
                        source_id=i['id'], project=rec.get('project'), timestamp=rec.get('updated') or rec.get('date'),
                        trust_level='verified'))          # происхождение фрагмента контекста (07: A4-01)
        if len(out) >= limit:
            break
    return out


def note_use(run, ref, cards, applied=None, at=None):
    """Какие карточки выданы запуску и какие исполнитель назвал применёнными: только id и версия (REQ-MEM-05)."""
    refs = [dict(id=c['id'], version=c.get('version')) for c in cards]
    return le.record('knowledge_used', dict(component='learn', run=run, kind='applied' if applied is not None else 'given'),
                     at=at, origin='server', project=ref.split('/')[0], task_ref=ref, run_id=run, knowledge_refs=refs,
                     data=dict(applied=[a for a in applied or [] if isinstance(a, str)][:10]) if applied is not None else None)
