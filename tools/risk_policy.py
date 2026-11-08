"""Профиль риска задачи: FAST / STANDARD / CRITICAL (TT-109; ТЗ FR-001, E2, TC-07/08, AC-49).

Классификатор консервативный: ошибка в сторону строгого маршрута стоит лишних токенов,
ошибка в сторону короткого — пропущенного анализа и проверки. Правила:

  * CRITICAL — риск high/critical, сложная задача (класс 3) или признак опасной области
    в тексте задачи, её scope или фактическом диффе: аутентификация и права, оплата,
    криптография и секреты, миграции и схема данных, публичный API, CI, правила выпуска,
    зависимости. Слово в тексте и путь в диффе — равные признаки;
  * FAST — только когда доказано, что можно: риск low, размер S, класс 1, есть scope и критерии
    приёмки, у проекта есть машинные тесты, дифф (если уже есть) мал и не выходит за scope.
    Задача только из документов — FAST с профилем docs_only, релизную политику не обходит;
  * всё остальное — STANDARD. Риск не оценён — сокращённый маршрут запрещён (STANDARD,
    низкая уверенность), неоднозначность поднимает маршрут, а не опускает.

Повторная оценка по фактическому диффу (reevaluate) маршрут только поднимает: дифф,
оказавшийся меньше ожиданий, не отменяет уже начатых проверок.
"""
import re

POLICY_VERSION = 'risk-v1'
MODES = ('FAST', 'STANDARD', 'CRITICAL')
RANK = {m: i for i, m in enumerate(MODES)}

FAST_MAX_FILES = 5
FAST_MAX_LINES = 150
FAST_MIN_CONFIDENCE = 0.8

# Опасные области: (категория, слова путей и текста, основы русских слов).
# Слова сравниваются с токенами целиком, основы — с началом русского слова.
AREAS = [
    ('auth', 'аутентификация и права доступа',
     {'auth', 'oauth', 'login', 'logout', 'session', 'sessions', 'permission', 'permissions', 'acl', 'rbac', 'jwt', 'sso',
      'owner', 'grant', 'grants', 'saml', 'csrf', 'cors'},
     ('авториз', 'аутентиф', 'прав доступ', 'права доступ', 'полномоч', 'логин', 'вход в систем', 'пароль', 'парол')),
    ('payment', 'оплата и деньги', {'payment', 'payments', 'billing', 'invoice', 'stripe', 'checkout', 'refund', 'wallet'},
     ('оплат', 'платеж', 'платёж', 'биллинг', 'счёт на оплату', 'возврат денег')),
    ('crypto', 'криптография и секреты',
     {'crypto', 'cipher', 'encrypt', 'decrypt', 'hmac', 'signature', 'secret', 'secrets', 'token', 'tokens', 'key', 'keys',
      'password', 'passwords', 'credential', 'credentials', 'cert', 'certs', 'tls', 'ssl', 'env'},
     ('криптограф', 'шифр', 'цифров подпис', 'электронн подпис', 'секрет', 'ключ api', 'api ключ', 'сертификат', 'токен доступ')),
    ('data', 'данные, схема и миграции',
     {'migration', 'migrations', 'migrate', 'schema', 'schemas', 'alembic', 'sql', 'database', 'db', 'ddl', 'backup',
      'restore'},
     ('миграц', 'схем данных', 'схема данных', 'базы данных', 'база данных', 'удаление данных', 'резервн', 'восстановлен данных')),
    ('api', 'публичный API и контракты',
     {'api', 'openapi', 'swagger', 'graphql', 'proto', 'grpc', 'webhook', 'webhooks', 'endpoint', 'endpoints', 'contract', 'sdk'},
     ('контракт', 'эндпоинт', 'внешн интерфейс', 'внешнего интерфейс')),
    ('release', 'CI и правила выпуска',
     {'ci', 'workflows', 'github', 'gitlab', 'pipeline', 'release', 'deploy', 'deployment', 'jenkinsfile', 'dockerfile',
      'helm', 'terraform', 'policy', 'policies'},
     ('релизн', 'правил релиз', 'правила релиз', 'правил выпуск', 'правила выпуск', 'деплой', 'развёртыв', 'развертыв', 'политик')),
    ('deps', 'зависимости', {'requirements', 'lock', 'pipfile', 'poetry', 'pyproject', 'package', 'gemfile', 'cargo', 'gomod',
                             'pubspec', 'vendor'},
     ('зависимост', 'обновить библиотек')),
    ('security', 'безопасность', {'security', 'sanitize', 'xss', 'injection', 'vuln', 'cve', 'firewall', 'sandbox'},
     ('безопасн', 'уязвим', 'инъекц', 'иб ')),
]
DOC_EXT = ('.md', '.txt', '.rst', '.adoc')
TEST_HINT = re.compile(r'(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+\.py$|_test\.(py|go|js|ts)$|\.(spec|test)\.(js|ts|tsx)$')
_TOKEN = re.compile(r'[a-z0-9]+|[а-яё]+', re.I)


def tokens(text):
    return {x.lower() for x in _TOKEN.findall(text or '')}


def area_hits(text=None, paths=()):
    """Опасные области в тексте и путях: {категория: [признак, ...]}."""
    hits = {}
    low = ' ' + ' '.join(_TOKEN.findall((text or '').lower())) + ' '
    words = tokens(text)
    for cat, _, eng, stems in AREAS:
        found = sorted(words & eng) + [s for s in stems if (' ' + s) in low]
        for p in paths or ():
            pl = p.lower().replace('\\', '/')
            if pl.endswith(DOC_EXT):
                continue              # документ о миграции — не миграция
            parts = set(re.split(r'[^a-z0-9]+', pl)) - {''}
            if parts & eng:
                found.append(p)
        if found:
            hits[cat] = found[:5]
    return hits


def docs_only(paths):
    return bool(paths) and all(p.lower().endswith(DOC_EXT) or p.lower().startswith('docs/') and not TEST_HINT.search(p.lower())
                               for p in paths)


def _within(path, scope):
    path = path.replace('\\', '/').strip('/')
    for s in scope or ():
        s = s.replace('\\', '/').strip('/').rstrip('*').rstrip('/')
        if not s or path == s or path.startswith(s + '/') or ('*' not in s and path.startswith(s) and s.endswith('.')):
            return True
    return False


def classify(task, files=None, lines=None, tests=True, tier=None):
    """Профиль задачи. files/lines — фактический дифф (None — диффа ещё нет), tests — у проекта есть
    машинные тесты, tier — класс сложности (None — посчитать). Возвращает dict(mode, risk, confidence,
    risk_reasons, signals, profile, stage)."""
    t = task or {}
    if tier is None:
        import complexity
        tier = complexity.assess(t)['tier']
    text = ' '.join(str(t.get(k) or '') for k in ('title', 'why', 'task_type'))
    text += ' ' + ' '.join(str(a) for a in t.get('acceptance') or [])
    scope = list(t.get('scope') or [])
    paths = list(files or []) + scope
    hits = area_hits(text, paths)
    risk = t.get('risk') if t.get('risk') in ('low', 'medium', 'high', 'critical') else None
    reasons, blockers = [], []          # reasons — почему не короче, blockers — почему не FAST
    mode = 'STANDARD'
    if risk in ('high', 'critical'):
        mode = 'CRITICAL'; reasons.append('риск %s' % risk)
    if tier == 3:
        mode = 'CRITICAL'; reasons.append('сложная задача (класс 3)')
    for cat, label, *_ in AREAS:
        if cat in hits:
            mode = 'CRITICAL'; reasons.append('%s: %s' % (label, ', '.join(hits[cat][:3])))
    doc = (t.get('work_type') == 'docs' and (not files or docs_only(files))) or (files is not None and docs_only(files))
    if mode != 'CRITICAL':
        if risk is None: blockers.append('риск не оценён')
        elif risk != 'low': blockers.append('риск %s' % risk)
        if t.get('size') != 'S': blockers.append('размер %s' % (t.get('size') or 'не указан'))
        if tier != 1: blockers.append('класс сложности %s' % tier)
        if not scope: blockers.append('scope не определён')
        if not t.get('acceptance'): blockers.append('нет критериев приёмки')
        if not tests and not doc: blockers.append('у проекта нет машинных тестов')
        if t.get('work_type') in ('architecture', 'research'): blockers.append('вид работы %s' % t['work_type'])
        if files is not None:
            if len(files) > FAST_MAX_FILES: blockers.append('дифф: %d файлов > %d' % (len(files), FAST_MAX_FILES))
            if lines is not None and lines > FAST_MAX_LINES: blockers.append('дифф: %d строк > %d' % (lines, FAST_MAX_LINES))
            out = [f for f in files if scope and not _within(f, scope)]
            if out: blockers.append('дифф вне scope: %s' % ', '.join(out[:3]))
        if not blockers:
            mode = 'FAST'
        else:
            reasons += blockers
    confidence = 0.9 if files is not None else 0.75
    if risk is None: confidence -= 0.25
    if mode == 'FAST' and files is None:
        confidence = 0.8              # до диффа FAST условный: решит повторная оценка
    confidence = round(max(0.1, min(1.0, confidence)), 2)
    if mode == 'FAST' and confidence < FAST_MIN_CONFIDENCE:
        mode = 'STANDARD'; reasons.append('уверенность %.2f ниже порога FAST' % confidence)
    return dict(mode=mode, risk=risk or 'unknown', confidence=confidence, risk_reasons=reasons or ['обычное изменение'],
                signals=hits, profile='docs_only' if mode == 'FAST' and doc else None,
                stage='diff' if files is not None else 'intake', tier=tier, policy_version=POLICY_VERSION)


def stricter(a, b):
    return a if RANK[a] >= RANK[b] else b


def reevaluate(prev_mode, task, files, lines=None, tests=True, tier=None):
    """Повторная оценка по фактическому диффу: маршрут только поднимается (TC-08, AC-49)."""
    now = classify(task, files, lines, tests, tier)
    mode = stricter(prev_mode, now['mode'])
    return dict(now, mode=mode, previous=prev_mode, raised=RANK[mode] > RANK[prev_mode],
                lowered_ignored=RANK[now['mode']] < RANK[prev_mode])


def evaluate(cases):
    """Эталонный набор: [{task, files?, lines?, tests?, expect}] → точность и опасные понижения."""
    rows, wrong, dangerous = [], [], []
    for c in cases:
        got = classify(c['task'], c.get('files'), c.get('lines'), c.get('tests', True), c.get('tier'))['mode']
        rows.append(got == c['expect'])
        if got != c['expect']:
            wrong.append(dict(id=c.get('id'), expect=c['expect'], got=got))
        if RANK[got] < RANK[c['expect']] and c['expect'] == 'CRITICAL':
            dangerous.append(dict(id=c.get('id'), expect=c['expect'], got=got))
    by = {m: sum(1 for c in cases if c['expect'] == m) for m in MODES}
    return dict(total=len(cases), accuracy=round(sum(rows) / len(rows), 3) if rows else 0.0, by_mode=by,
                wrong=wrong, critical_downgrades=dangerous)


def main(argv=None):
    import json, sys
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2 or argv[0] != 'eval':
        print('py tools/risk_policy.py eval <набор.json>'); return 2
    with open(argv[1], encoding='utf-8') as f:
        rep = evaluate(json.load(f)['cases'])
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if not rep['critical_downgrades'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
