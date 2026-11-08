"""Машинные проверки содержания артефактов ролей (07: AG-PO-02…05, AG-SA-01…06, AG-SA-09, A5-01, A6-06,
AG-ARCH-01/02/04/05, AG-DES-01…04, AG-MKT-02…04).

Проверка не судит о качестве мысли — она ловит то, что можно поймать без чтения: число без происхождения,
нефункциональное требование без меры, критерий приёмки без строки в RTM и тест-дизайне, ссылку HLD на
несуществующий файл, ADR с одним вариантом, модель угроз без мер, экран без состояний ошибки и пустоты.
Каждое правило возвращает (ok, detail) с тем, что именно не так.
"""
import re

NUM = re.compile(r'\d')
DATE = re.compile(r'\b20\d\d(?:-\d\d)?\b')
LINK = re.compile(r'https?://\S+')
PATH = re.compile(r'`([\w./-]+\.(?:py|js|ts|tsx|jsx|md|json|html|css|go|rs|java|cs|sql|ya?ml|toml|cmd|ps1|sh))(?::\d+)?`')
FIELD = re.compile(r'`([A-Za-z_][\w.]*)`')
ORIGIN = ('гипотез', 'источник', 'unknown', 'http', 'замер', 'факт')


def section(text, needle):
    """Текст раздела, чей заголовок содержит needle (с подразделами)."""
    import pipeline as P
    for h, body in P.sections(text or ''):
        if needle.lower() in h.lower():
            return body
    return None


def content_lines(text):
    """Строки содержания: без заголовков, блоков кода и разделителей таблиц."""
    out, fence = [], False
    for l in (text or '').splitlines():
        s = l.strip()
        if s.startswith('```'):
            fence = not fence; continue
        if fence or not s or s.startswith('#') or re.fullmatch(r'\|?[\s:|-]+\|?', s):
            continue
        out.append(s)
    return out


def labeled_numbers(text, it):
    """Каждое число — с происхождением: источник, замер, факт, гипотеза или UNKNOWN (AG-PO-02/03, AG-MKT-02…04)."""
    body = section(text, it['section']) if it.get('section') else text
    if body is None:
        return False, 'нет раздела «%s»' % it['section']
    need_date = it.get('date')
    bad = []
    for l in content_lines(body):
        if not NUM.search(re.sub(r'\b(?:AC|FR|NFR|SR|BR|TC|DU|D|ADR|CF)-\d+\b', '', l)):
            continue
        low = l.lower()
        if 'гипотез' in low or 'unknown' in low:
            continue
        if not any(o in low for o in ORIGIN):
            bad.append(l[:70]); continue
        if need_date and not DATE.search(l):
            bad.append(l[:70] + ' (нет даты источника)')
    return not bad, 'чисел без происхождения: %d — %s; пометь источник с датой, «гипотеза» или UNKNOWN' % (len(bad), ' | '.join(bad[:3])) \
        if bad else 'у всех чисел указано происхождение'


def heading_any(text, it):
    import pipeline as P
    heads = P.headings(text)
    ok = any(n.lower() in h for h in heads for n in it['needles'])
    return ok, 'раздел есть' if ok else 'нет раздела: %s' % ' или '.join('«%s…»' % n for n in it['needles'])


def heading_all(text, it):
    import pipeline as P
    heads = P.headings(text)
    miss = [n for n in it['needles'] if not any(n.lower() in h for h in heads)]
    return not miss, 'нет разделов: %s' % ', '.join('«%s…»' % n for n in miss) if miss else 'разделы есть'


def words_any(text, it):
    low = (text or '').lower()
    ok = any(w in low for w in it['words'])
    return ok, 'есть' if ok else 'нет ни одного из: %s' % ', '.join(it['words'])


def words_in_section(text, it):
    body = section(text, it['section'])
    if body is None:
        return False, 'нет раздела «%s»' % it['section']
    low = body.lower()
    miss = [w for w in it['words'] if w not in low]
    return not miss, 'в разделе «%s» нет: %s' % (it['section'], ', '.join(miss)) if miss else 'все состояния описаны'


def measurable(text, it):
    """Нефункциональные требования измеримы: в каждой строке требования — число (порог, время, доля) (AG-SA-04)."""
    body = section(text, it['section'])
    if body is None:
        return False, 'нет раздела «%s»' % it['section']
    reqs = [l for l in content_lines(body) if l.startswith(('-', '*', '|')) or re.match(r'(NFR|НФТ)-?\d', l)]
    if not reqs:
        return False, 'в разделе «%s» нет ни одного требования списком или таблицей' % it['section']
    vague = [l[:70] for l in reqs if not NUM.search(re.sub(r'\b(?:NFR|SR|FR)-\d+\b', '', l))]
    return not vague, 'неизмеримые НФТ (нет порога): %s' % ' | '.join(vague[:3]) if vague else 'все %d НФТ с порогами' % len(reqs)


def acceptance(ctx):
    return [a for a in ctx.t.get('acceptance') or [] if isinstance(a, str) and a.strip()]


def covers_ac(text, ctx, it):
    """Каждый критерий приёмки задачи есть в документе (RTM: прямая и обратная трассировка, AG-SA-01)."""
    acc = acceptance(ctx)
    if not acc:
        return False, 'у задачи нет критериев приёмки — трассировать нечего'
    low = (text or '').lower()
    miss = ['AC-%d' % (i + 1) for i, a in enumerate(acc) if 'ac-%d' % (i + 1) not in low and a.strip().lower()[:40] not in low]
    return not miss, 'не трассированы: %s' % ', '.join(miss) if miss else 'все %d критериев трассированы' % len(acc)


def design_matrix(text, ctx, it):
    """Тест-дизайн: каждый AC — в позитивных, негативных и граничных случаях; ожидаемый результат есть (A6-06, AG-SA-09)."""
    acc = acceptance(ctx)
    if not acc:
        return False, 'у задачи нет критериев приёмки'
    bad = []
    for sec in ('Позитив', 'Негатив', 'Границ'):
        body = section(text, sec) if sec != 'Границ' else (section(text, 'Границ') or section(text, 'Гранич'))
        if body is None:
            bad.append('нет раздела «%s…»' % sec); continue
        low = body.lower()
        miss = ['AC-%d' % (i + 1) for i in range(len(acc)) if 'ac-%d' % (i + 1) not in low]
        if miss:
            bad.append('«%s…» без %s' % (sec, ', '.join(miss)))
    if 'ожидаем' not in (text or '').lower():
        bad.append('нет ожидаемого результата')
    return not bad, '; '.join(bad) if bad else 'каждый AC: позитивный, негативный и граничный случай'


def dev_units(text, ctx, it):
    """Задачи разработки — минимальные проверяемые шаги DU-n, каждый с AC; монолит XL не проходит (A5-01, AG-SA-06)."""
    import pipeline as P
    units = [(h, body) for h, body in P.sections(text or '') if re.match(r'(DU-\d+|DevUnit)', h.strip(), re.I)]
    if not units:
        return False, 'нет шагов разработки: заголовки «DU-1 …», «DU-2 …» — каждый с одним проверяемым результатом и AC'
    bad = []
    for h, body in units:
        if not re.search(r'\bAC-\d+', body):
            bad.append('%s без AC' % h.split()[0])
        if re.search(r'(?i)размер\W*XL\b|\bsize\W*XL\b', body):
            bad.append('%s размера XL — разбить' % h.split()[0])
    if ctx.t.get('size') == 'XL' and len(units) < 2:
        bad.append('задача XL одним шагом — разбить на шаги DU-n')
    return not bad, '; '.join(bad) if bad else 'шагов разработки: %d, у каждого AC' % len(units)


def api_version(text, ctx, it):
    """Контракт API версионирован и говорит о совместимости (AG-SA-03). Нет api.md — нечего проверять."""
    if text is None:
        return True, 'контракта API у задачи нет'
    return heading_all(text, dict(needles=['Верси', 'Совместим']))


def refs_exist(text, ctx, it):
    """Пути файлов в HLD существуют в ветке или помечены как новые (AG-ARCH-01)."""
    import pipeline as P
    bad = []
    for l in (text or '').splitlines():
        if re.search(r'(?i)нов(ый|ая|ое|ые)|new|создать|добав', l):
            continue
        for p in PATH.findall(l):
            if not P.read_file_at(ctx.repo, ctx.branch, p):
                bad.append(p)
    bad = sorted(set(bad))
    return not bad, 'нет в ветке: %s — проверь путь или пометь «новый»' % ', '.join(bad[:6]) if bad else 'ссылки HLD на файлы верны'


def adr_options(text, ctx, it):
    """В каждом ADR не меньше двух рассмотренных вариантов (AG-ARCH-02)."""
    parts = [p for p in re.split(r'\n(?=# )', text or '') if p.strip()]
    bad = []
    for i, p in enumerate(parts, 1):
        body = section(p, 'Вариант') or section(p, 'Альтернатив')
        n = len([l for l in content_lines(body or '') if l.startswith(('-', '*', '|')) or re.match(r'\d+\.', l)])
        if n < 2:
            bad.append('ADR #%d: вариантов %d' % (i, n))
    return not bad, '; '.join(bad) + ' — нужен раздел «Варианты» не меньше чем с двумя' if bad else 'в ADR не меньше двух вариантов'


def threat_table(text, ctx, it):
    """Модель угроз — таблица угроз с мерой по каждой (AG-ARCH-05, AG-SECAG-02)."""
    rows = [l for l in (text or '').splitlines() if l.strip().startswith('|') and l.count('|') >= 3]
    data = [r for r in rows[2:]] if len(rows) >= 2 else []
    if len(data) < 2:
        return False, 'в модели угроз нет таблицы угроз (шапка и хотя бы две угрозы)'
    empty = [r[:50] for r in data if not [c for c in r.strip().strip('|').split('|')][-1].strip()]
    return not empty, 'угрозы без меры: %s' % ' | '.join(empty[:3]) if empty else 'угроз: %d, у каждой мера' % len(data)


def fields_in_api(text, ctx, it):
    """Поля из спецификации передачи есть в контракте API (AG-DES-03). Нет api.md — проверять нечего."""
    import pipeline as P
    api = ctx.artifact(P.ROLES['system_analyst']['artifacts'][2])
    if api is None:
        return True, 'контракта API нет — сверять не с чем'
    body = section(text, 'Валидаци') or ''
    names = sorted(set(FIELD.findall(body)))
    miss = [n for n in names if n not in api]
    return not miss, 'полей нет в api.md: %s' % ', '.join(miss[:6]) if miss else 'полей интерфейса: %d, все в контракте' % len(names)


def legacy_fields(text, ctx, it):
    """Переделка интерфейса не теряет данных (07: AG-DES-07): каждое поле из раздела «Было»/«Старый интерфейс»
    есть в новом макете или помечено «убрано: причина». Нет раздела — задача не переделка, сверять нечего."""
    old = section(text, 'Старый интерфейс') or section(text, 'Было') or section(text, 'Legacy')
    if old is None:
        return True, 'раздела старого интерфейса нет — не переделка'
    rest = (text or '').replace(old, '')
    names = sorted(set(FIELD.findall(old)))
    miss = [n for n in names if n not in rest and not re.search(r'`%s`[^\n]*убран' % re.escape(n), old, re.I)]
    if not names:
        return False, 'в разделе старого интерфейса нет реестра полей (`поле`)'
    return not miss, 'поля старого интерфейса потеряны: %s — перенеси или пометь «убрано: причина»' % ', '.join(miss[:6]) if miss         else 'полей старого интерфейса: %d, все на месте' % len(names)


CMD = re.compile(r'```[^\n]*\n\s*\S|`[^`\n]*\b(git|py|python|pip|npm|docker|kubectl|systemctl|sc|powershell|bash|make|\./)[^`\n]*`')
VERSION = re.compile(r'(?i)(верси[яи]|version)\W{0,3}[:=]?\s*`?(v?\d+\.\d+[\w.-]*|[0-9a-f]{7,40})')


def runbook(text, ctx, it):
    """Runbook исполним человеком (07: AG-OPS-07): в разделах развёртывания и отката — команды, указана версия."""
    bad = []
    for need, title in (('Развёртывани', 'развёртывания'), ('Откат', 'отката')):
        body = section(text, need)
        if body is None:
            bad.append('нет раздела %s' % title)
        elif not CMD.search(body):
            bad.append('в разделе %s нет команды (блок кода или `команда`)' % title)
    if not VERSION.search(text or ''):
        bad.append('не указана версия выпуска (Версия: v1.2.3 или коммит)')
    return not bad, '; '.join(bad) if bad else 'команды развёртывания и отката есть, версия указана'


RULES = dict(labeled_numbers=lambda t, c, i: labeled_numbers(t, i), heading_any=lambda t, c, i: heading_any(t, i),
             heading_all=lambda t, c, i: heading_all(t, i), words_any=lambda t, c, i: words_any(t, i),
             words_in_section=lambda t, c, i: words_in_section(t, i), measurable=lambda t, c, i: measurable(t, i),
             covers_ac=covers_ac, design_matrix=design_matrix, dev_units=dev_units, api_version=api_version,
             refs_exist=refs_exist, adr_options=adr_options, threat_table=threat_table, fields_in_api=fields_in_api,
             runbook=runbook, legacy_fields=legacy_fields)
OPTIONAL = ('api_version',)          # правила, у которых отсутствие файла — не провал


def check(name, text, ctx, it):
    return RULES[name](text, ctx, it)
