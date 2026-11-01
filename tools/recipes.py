"""Рецепты Решалы: механические проблемы агентов сервер закрывает сам, без сессии ИИ (TT-88).

Разбор недели 24–31.10: 13 из 22 проблем были механическими — песочница не даёт писать в .git,
архитектор открыт раньше аналитика, сессия заканчивается без сдачи, «????» вместо кириллицы,
противоречие контрактов, обрезанный список задач. Каждой такой проблеме — рецепт; Решале
достаются только содержательные.

База решений растёт: Решала может приложить к решению `recipe` {title, keywords, instructions}.
Такой рецепт хранится в workflow.json → playbook и в следующий раз отвечает агенту сам.
"""
import re

# kind: advise — ответ агенту, запуск продолжается; wait — роль ждёт предшественника;
# contract — задача к системному аналитику с матрицей кодов. Сессия Р3 разбирается в session_lost.
BUILTIN = [
    dict(id='Р1', kind='advise', title='Нет записи в .git (песочница)',
         match=r'index\.lock|permission denied|\.git\b|не (могу|удаётся|получается) (за)?коммит|commit (failed|не проход)|отказано в доступе',
         instructions='Не трать попытки на git commit: сервер сам закоммитил файлы рабочей копии сейчас и закоммитит при '
                      'handoff, return, escalate или pause. Продолжай работу и сдай этап op=handoff.'),
    dict(id='Р2', kind='wait', title='Нет входа от системного аналитика', roles=('solution_architect', 'designer'),
         match=r'(нет|отсутств\w*|не найден\w*|не готов\w*|пуст\w*)\W+(\w+\W+){0,3}(srs|тз|аналитик\w*|техническ\w+ задани\w*)|'
               r'(srs|тз)\W+(\w+\W+){0,3}(нет|отсутств\w*|не найден\w*|не готов\w*)',
         instructions='Архитектор и дизайнер работают по готовому ТЗ: запуск снят и откроется сам, когда системный аналитик сдаст SRS.'),
    dict(id='Р4', kind='advise', title='«????» вместо текста — кодировка',
         match=r'\?{4,}|кодировк\w*|encoding|unicodeencode|cp1251|charmap|mojibake|кракозябр\w*',
         instructions='Пиши файлы только в UTF-8: в PowerShell — Set-Content/Out-File с -Encoding utf8, в Python — '
                      'open(..., encoding="utf-8"), для вывода — PYTHONIOENCODING=utf-8. Перепиши файлы, где вместо кириллицы «????»: '
                      'сервер не принимает передачу с такими артефактами.'),
    dict(id='Р5', kind='contract', title='Противоречие контрактов между артефактами', roles=('solution_architect', 'designer', 'developer', 'marketer'),
         match=r'(противореч\w*|конфликт\w*|расход\w*|не совпада\w*|несоответств\w*)\W+(\w+\W+){0,6}(контракт\w*|api|srs|hld|adr|модел\w* данных|тз)|'
               r'(контракт\w*|api|srs|hld|adr)\W+(\w+\W+){0,6}(противореч\w*|конфликт\w*|расход\w*|не совпада\w*)',
         instructions='Контракт (SRS, API, модель данных) принадлежит системному аналитику: задача возвращена ему с матрицей кодов; '
                      'остальные роли продолжат по исправленному контракту.'),
    dict(id='Р6', kind='advise', title='Список задач обрезан',
         match=r'(список|list)\W+(\w+\W+){0,4}(обрез\w*|не весь|только \d+|не видно|не помеща\w*|truncat\w*)|только (первые )?\d+ задач',
         instructions='Список задач постраничный: tt_work op=list project=<проект> offset=<сколько пропустить> limit=<до 100>; '
                      'в ответе total и next_offset.'),
]
SESSION_RECIPE = dict(id='Р3', title='Сессия трижды закончилась без сдачи этапа',
                      text='Сервер проверил артефакты роли и вернул её к работе со списком: что сделано и чего не хватает для handoff.')
GARBLED = re.compile(r'\?{4,}')


def catalog(s=None):
    """Известные решения: встроенные рецепты и накопленные Решалой — для паспорта Решалы и скиллов."""
    out = [dict(id=r['id'], title=r['title'], instructions=r['instructions']) for r in BUILTIN]
    out.insert(2, dict(id=SESSION_RECIPE['id'], title=SESSION_RECIPE['title'], instructions=SESSION_RECIPE['text']))
    out += [dict(id=p['id'], title=p['title'], instructions=p['instructions'], keywords=p['keywords'], uses=p.get('uses', 0))
            for p in (s or {}).get('playbook') or []]
    return out


def match(s, text, role):
    """Рецепт для проблемы агента или None. Накопленные рецепты — по всем ключевым словам."""
    low = (text or '').lower()
    for r in BUILTIN:
        if r.get('roles') and role not in r['roles']:
            continue
        if re.search(r['match'], low):
            return r
    for p in (s or {}).get('playbook') or []:
        if p.get('keywords') and all(k in low for k in p['keywords']):
            return dict(p, kind='advise')
    return None


def learn(s, raw, prob, at):
    """Рецепт от Решалы — в базу решений. Возвращает id или ошибку ValueError."""
    if not isinstance(raw, dict):
        raise ValueError('recipe: {title, keywords: [2–6 слов из текста проблемы], instructions}')
    title = str(raw.get('title') or '').strip(); text = str(raw.get('instructions') or '').strip()
    kws = [str(k).strip().lower() for k in raw.get('keywords') or [] if str(k).strip()]
    if len(title) < 10 or len(text) < 30 or not 2 <= len(kws) <= 6 or any(len(k) < 3 for k in kws):
        raise ValueError('recipe: title ≥10 символов, instructions ≥30, keywords — 2–6 слов не короче 3 символов')
    book = s.setdefault('playbook', [])
    entry = dict(id='К-%d' % (len(book) + 1), title=title[:200], keywords=kws, instructions=text[:1500],
                 problem=prob['id'], at=at.isoformat(), uses=0)
    book.append(entry)
    return entry['id']
