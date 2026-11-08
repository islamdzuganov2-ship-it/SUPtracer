"""Многофакторная оценка сложности задачи для выбора модели и усилий.

Правила вида «аналитика — всегда старшая модель» переплачивают за малые задачи
и не видят содержания. Здесь сложность складывается из нескольких независимых
признаков, у каждого — вес и объяснимый вклад. Отдельно считается уверенность:
неполные данные, противоречащие друг другу признаки и балл на границе классов её
снижают. Неуверенную оценку, а также аналитику и декомпозицию по умолчанию,
уточняет ИИ (фаза оценки): его оценка весит больше эвристики.

Модуль — чистые функции без доступа к данным: проверяется тестами напрямую.
"""
import re

SIZE = {'S': .15, 'M': .5, 'L': .8, 'XL': 1.}
RISK = {'low': .1, 'medium': .5, 'high': .9}
WORK = {'docs': .15, 'test': .3, 'implementation': .45, 'research': .6, 'architecture': .75}
TYPE = {'dev_by_spec': .3, 'direction_task': .35, 'analysis_spec': .55, 'decomposition': .65}
WEIGHTS = dict(size=.22, risk=.2, work=.14, type=.1, criteria=.1, breadth=.12, text=.12)
# Границы классов модели по баллу 0…1 и ступени усилий внутри класса.
TIER_EDGES = (.34, .6)
EFFORT_STEPS = {1: ((.8, 'low'), (1.01, 'medium')),
                2: ((.65, 'medium'), (1.01, 'high')),
                3: ((.6, 'high'), (.9, 'xhigh'), (1.01, 'max'))}
LADDER = ('low', 'medium', 'high', 'xhigh', 'max')
# Потолок усилий класса: если нужно больше — это довод за модель старше, а не за усилия.
TIER_CAP = {1: 'medium', 2: 'high', 3: 'max'}
UP = ('архитектур', 'безопасн', 'миграц', 'интеграц', 'производительн', 'параллел', 'конкурент',
      'транзакц', 'неопредел', 'алгоритм', 'оптимизац', 'масштаб', 'распредел', 'протокол',
      'шифрован', 'авториз', 'персональн', 'рефактор', 'гонк', 'совместим', 'регресс', 'платёж', 'платеж')
DOWN = ('опечат', 'переименов', 'оформлен', 'подпис', 'цвет', 'отступ', 'readme', 'перевод',
        'текст кнопк', 'ссылк', 'косметич', 'мелк')
NEEDS_AI = ('analysis_spec', 'decomposition')   # по умолчанию уточняются оценкой ИИ
CONFIDENCE_FLOOR = .65


def _criteria(n):
    return .5 if n == 0 else {1: .2, 2: .3, 3: .4}.get(n, .55 if n <= 5 else .75)


def _breadth(task):
    scope = task.get('scope') or []
    norm = [str(p).strip().strip('/').lstrip('./') for p in scope]
    if not scope: value, known = .4, False
    elif any(p in ('', '.') for p in norm): value, known = .9, True
    else: value, known = {1: .2, 2: .35, 3: .45}.get(len(scope), .65), True
    dirs = task.get('directions') or []
    if dirs:
        value, known = max(value, {1: .3, 2: .5, 3: .6}.get(len(dirs), .8)), True
    deps = len(task.get('depends_on') or [])
    return min(1., value + min(.3, .1 * deps)), known


def _text(task):
    fields = ('title', 'why', 'question', 'spec_ref', 'inputs', 'deliverable', 'constraints')
    text = ' '.join(str(task.get(k) or '') for k in fields) + ' ' + ' '.join(task.get('acceptance') or [])
    text = text.lower()
    up = sorted({w for w in UP if w in text}); down = sorted({w for w in DOWN if w in text})
    return max(0., min(1., .5 + .1 * (len(up) - len(down)))), up, down


def heuristic(task):
    """Балл по признакам задачи, вклады факторов и неизвестные признаки."""
    unknown = []
    size = SIZE.get(task.get('size'))
    if size is None: size = .5; unknown.append('размер')
    risk = RISK.get(task.get('risk'))
    if risk is None: risk = .5; unknown.append('риск')
    work = WORK.get(task.get('work_type'))
    if work is None: work = .45; unknown.append('вид работы')
    kind = TYPE.get(task.get('task_type'), .45)
    n = len(task.get('acceptance') or [])
    if not n: unknown.append('критерии приёмки')
    breadth, breadth_known = _breadth(task)
    text, up, down = _text(task)
    values = dict(size=size, risk=risk, work=work, type=kind, criteria=_criteria(n), breadth=breadth, text=text)
    labels = dict(size='Размер', risk='Риск', work='Вид работы', type='Тип задачи', criteria='Критерии приёмки',
                  breadth='Широта: область, направления, зависимости', text='Сигналы в тексте')
    notes = dict(size=task.get('size') or '?', risk=task.get('risk') or '?', work=task.get('work_type') or '?',
                 type=task.get('task_type') or 'без типа', criteria='%d шт.' % n,
                 breadth=('известна' if breadth_known else 'не указана'),
                 text=('сложнее: ' + ', '.join(up) if up else '') + ('; ' if up and down else '') + ('проще: ' + ', '.join(down) if down else '') or 'нет')
    factors = [dict(key=k, label=labels[k], value=round(v, 2), weight=WEIGHTS[k],
                    contribution=round(WEIGHTS[k] * v, 3), note=notes[k]) for k, v in values.items()]
    return sum(f['contribution'] for f in factors), factors, unknown, values, (up, down)


def ai_score(a):
    """Оценка ИИ: сложность, неопределённость, широта по шкале 1…5."""
    return .5 * (a['complexity'] - 1) / 4 + .3 * (a['uncertainty'] - 1) / 4 + .2 * (a['breadth'] - 1) / 4


def validate_assessment(a):
    if not isinstance(a, dict): raise ValueError('assessment: объект complexity, uncertainty, breadth, reasons')
    for key, label in (('complexity', 'сложность'), ('uncertainty', 'неопределённость'), ('breadth', 'широта')):
        if type(a.get(key)) != int or not 1 <= a[key] <= 5: raise ValueError('Оценка: %s — целое от 1 до 5' % label)
    if not isinstance(a.get('reasons'), str) or not a['reasons'].strip() or len(a['reasons']) > 2000:
        raise ValueError('Оценка: опишите основания (reasons)')


def assess(task):
    """Итог: балл, уверенность, класс, положение в классе, нужна ли оценка ИИ."""
    score, factors, unknown, v, (up, down) = heuristic(task)
    conflicts = []
    if abs(v['size'] - v['risk']) >= .6:
        conflicts.append('размер и риск расходятся')
    if up and v['size'] <= .15 and len(up) >= 2:
        conflicts.append('малый размер, но в тексте признаки сложности')
    if down and v['work'] >= .6:
        conflicts.append('исследование или архитектура, но в тексте признаки простоты')
    if v['criteria'] >= .75 and v['size'] <= .15:
        conflicts.append('много критериев для малой задачи')
    heur = score
    a = task.get('assessment')
    if a:
        score = .35 * heur + .65 * ai_score(a)
    # Страховка: высокий риск не опускается в младший класс без оценки ИИ.
    floor = None
    if not a and task.get('risk') == 'high':
        floor = .34 if v['size'] <= .15 and v['work'] < .6 else .6
        score = max(score, floor)
    tier = 1 if score < TIER_EDGES[0] else 2 if score < TIER_EDGES[1] else 3
    lo, hi = ((0., TIER_EDGES[0]), TIER_EDGES, (TIER_EDGES[1], 1.))[tier - 1]
    position = max(0., min(1., (score - lo) / (hi - lo)))
    edge = min(abs(score - e) for e in TIER_EDGES)
    confidence = 1. - .15 * len(unknown) - .15 * len(conflicts) - (.1 if edge < .04 else 0) \
        - (.1 if task.get('task_type') in NEEDS_AI else 0)
    if a: confidence = max(confidence, .85)
    confidence = round(max(.1, min(1., confidence)), 2)
    needs = not a and (confidence < CONFIDENCE_FLOOR or task.get('task_type') in NEEDS_AI)
    return dict(score=round(score, 3), heuristic=round(heur, 3), tier=tier, position=round(position, 2),
                confidence=confidence, needs_assessment=needs, unknown=unknown, conflicts=conflicts,
                floor=floor, factors=factors, assessed=bool(a))


def effort_for(c, allowed, task):
    """Усилия по положению балла внутри класса; неопределённость поднимает на ступень."""
    effort = next(name for edge, name in EFFORT_STEPS[c['tier']] if c['position'] < edge)
    why = 'положение в классе %.2f' % c['position']
    a = task.get('assessment')
    bump = []
    if (a and a['uncertainty'] >= 4) or (not a and c['confidence'] < .5): bump.append('высокая неопределённость')
    if task.get('risk') == 'high': bump.append('высокий риск')
    if bump:
        raised = LADDER[min(LADDER.index(TIER_CAP[c['tier']]), LADDER.index(effort) + 1)]
        if raised != effort: effort, why = raised, why + '; ' + ' и '.join(bump) + ' — ступенью выше'
    while effort not in allowed:   # у линейки GPT нет xhigh и max
        effort = LADDER[LADDER.index(effort) - 1]
        why += '; старший доступный уровень'
    return effort, why


def summary(c):
    top = sorted(c['factors'], key=lambda f: -f['contribution'])[:3]
    parts = ['балл %.2f (класс %d), уверенность %.2f' % (c['score'], c['tier'], c['confidence'])]
    parts.append('главное: ' + ', '.join('%s %s' % (f['label'].split(':')[0].lower(), f['note']) for f in top))
    if c['assessed']: parts.append('учтена оценка ИИ')
    if c['floor']: parts.append('высокий риск поднял класс')
    if c['conflicts']: parts.append('противоречия: ' + '; '.join(c['conflicts']))
    if c['needs_assessment']: parts.append('сначала оценка ИИ')
    return '; '.join(parts)
