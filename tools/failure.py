"""Типизированный конверт сбоя и отпечаток ошибки (07: A8-01, A8-02, A8-05, AG-RES-02/03).

Провал сдачи этапа — не строка в журнале, а запись с классом, затронутой проверкой и отпечатком.
Отпечаток не зависит от чисел, хешей, путей к временным каталогам и времени: одна и та же ошибка
у разных попыток даёт один отпечаток. По нему сервер видит, что роль упёрлась в одно и то же,
и отдаёт задачу Решале сразу, не тратя ещё одну попытку на повтор того же провала.

Класс сбоя выбирается правилом, а не моделью: по id проверки и по известным признакам в тексте.
"""
import hashlib
import re

# Класс → что это значит и кто обычно чинит. Порядок правил важен: первое совпадение выигрывает.
CLASSES = {
    'policy': 'решение или политика владельца: агент не исправит, нужен владелец',
    'security': 'находка ИБ или изменённый эталон приёмки',
    'environment': 'CI не настроен, нет инструмента, окружение недоступно',
    'test': 'тесты или CI упали на коде задачи',
    'artifact': 'нет артефакта, раздела или критерия в документах задачи',
    'scope': 'изменения вне зоны роли или вне scope задачи',
    'quota': 'лимит ИИ или бюджет токенов',
    'runtime': 'сбой исполнителя: сессия, модель, рантайм',
    'unknown': 'не распознано правилом — разбирает Решала',
}

RULES = (
    ('policy', r'запрещён политикой|owner|владел|одобрен|approval|грант'),
    ('security', r'^dev-oracle$|^dor-evidence$|эталон|секрет|secret|security|уязвим|находк'),
    ('environment', r'не настроен|not_configured|не найден[ао]? (?:команд|инструмент)|command not found|no such file|нет инструмента|недоступ'),
    ('test', r'^(dev-verify|dev-tests|qa-ci|dt-ci|dm-ci|ci)\b|тест|\btests?\b|\bci\b|упал|failed'),
    ('scope', r'^zone$|вне зоны|scope'),
    ('quota', r'лимит|quota|rate.?limit|budget|бюджет'),
    ('runtime', r'runtime|рантайм|сесси|timeout|таймаут'),
    ('artifact', r'артефакт|раздел|artifact|section|критери|\.md\b'),
)

_NOISE = (
    (re.compile(r'[0-9a-f]{7,64}', re.I), '<sha>'),
    (re.compile(r'\b\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?'), '<time>'),
    (re.compile(r'[A-Za-z]:[\\/][^\s,;:)\'"]+|/tmp/[^\s,;:)\'"]+'), '<path>'),
    (re.compile(r'\d+(?:[.,]\d+)?'), '<n>'),
    (re.compile(r'\s+'), ' '),
)


def normalize(text):
    s = (text or '').lower()
    for rx, rep in _NOISE:
        s = rx.sub(rep, s)
    return s.strip()[:400]


def classify(fail):
    cid = str(fail.get('id') or '')
    text = ' '.join(str(fail.get(k) or '') for k in ('text', 'detail'))
    for cls, rx in RULES:
        if re.search(rx, cid, re.I) or re.search(rx, text, re.I):
            return cls
    return 'unknown'


def fingerprint(fails):
    """Один отпечаток на набор провалов: id проверок и нормализованный текст, порядок не важен."""
    parts = sorted('%s|%s' % (f.get('id'), normalize(f.get('detail') or f.get('text'))) for f in fails or [])
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()[:16]


def envelope(fails, role, stage, run=None, agent=None, at=None):
    """Конверт сбоя этапа: класс (самый «тяжёлый» из провалов), проверки, отпечаток, ссылка на запуск."""
    items = [dict(id=f.get('id'), cls=classify(f), detail=str(f.get('detail') or f.get('text') or '')[:300]) for f in fails or []]
    order = list(CLASSES)
    cls = min((i['cls'] for i in items), key=order.index) if items else 'unknown'
    return dict(cls=cls, meaning=CLASSES[cls], role=role, stage=stage, run=run, agent=agent, at=at,
                checks=items, fingerprint=fingerprint(fails))


def repeated(history, fp):
    """Сколько последних конвертов подряд этой роли и этапа несут тот же отпечаток (включая текущий)."""
    n = 0
    for e in reversed(history or []):
        if e.get('fingerprint') != fp:
            break
        n += 1
    return n
