"""Что за файл изменён: ТЗ, тест, документ или код — и какого уровня тест.

Одни правила для двух мест: проверка способа исправления дефекта в конвейере
(«поправили тест» должно значить, что в коммитах правились тесты) и метрики работ.
Правила — по пути и имени файла, без чтения содержимого.
"""
import re

# Артефакты анализа в ветке задачи: правка их после передачи в разработку — правка ТЗ.
SPEC_NAMES = ('brd.md', 'srs.md', 'hld.md', 'rtm.md', 'api.md', 'data-model.md', 'diagrams.md',
              'dev-tasks.md', 'threats.md', 'poc.md')
SPEC_DIRS = ('/adr/', '/design/', '/marketing/')
# ТЗ проектов вне конвейера — по имени документа.
SPEC_WORDS = re.compile(r'(^|[/_\-. ])(тз|техническое[ _-]задание|spec|specification|requirements|требования|srs|brd|prd)([/_\-. ]|$)', re.I)

TEST_DIR = re.compile(r'(^|/)(tests?|__tests__|spec|specs|e2e|integration|it|cypress|playwright)(/|$)', re.I)
TEST_FILE = re.compile(r'(^|/)(test_[^/]+|[^/]+_test\.[a-z0-9]+|[^/]+\.(test|spec)\.[a-z0-9]+|[^/]+Tests?\.(cs|java|kt|swift))$', re.I)
DOC_EXT = ('.md', '.rst', '.txt', '.adoc', '.docx', '.pdf')
CODE_EXT = ('.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue', '.svelte', '.go', '.rs', '.java', '.kt', '.cs',
            '.cpp', '.cc', '.c', '.h', '.hpp', '.swift', '.rb', '.php', '.sql', '.html', '.css', '.scss', '.sh', '.ps1',
            '.cmd', '.bat', '.lua', '.dart', '.gd', '.r', '.scala', '.m', '.ex', '.exs', '.yaml', '.yml', '.toml', '.json')
SKIP = re.compile(r'(^|/)(node_modules|dist|build|\.venv|venv|__pycache__|\.git|vendor|data/snapshots)(/|$)|(^|/)dashboard\.html$|\.lock$|-lock\.json$', re.I)

LEVELS = ('unit', 'integration', 'e2e', 'load', 'manual', 'static')
LEVEL_TITLES = dict(unit='модульные', integration='интеграционные', e2e='сквозные (e2e/UI)', load='нагрузочные',
                    manual='ручные', static='статический анализ')
_LEVEL_RULES = [
    ('e2e', re.compile(r'(^|/|[_.\-])(e2e|end[_-]?to[_-]?end|ui|cypress|playwright|selenium|browser|smoke)([/_.\-]|$)', re.I)),
    ('load', re.compile(r'(^|/|[_.\-])(load|perf|performance|stress|k6|locust|jmeter|bench(mark)?s?)([/_.\-]|$)', re.I)),
    ('integration', re.compile(r'(^|/|[_.\-])(integration|integ|it|api|contract|db|e2i)([/_.\-]|$)', re.I)),
]


def norm(path):
    return str(path or '').replace('\\', '/').strip('/')


def is_test(path):
    p = norm(path)
    return bool(TEST_FILE.search(p) or (TEST_DIR.search(p) and p.lower().endswith(CODE_EXT)))


def classify(path):
    """'spec' | 'test' | 'doc' | 'code' | 'other' | 'skip'."""
    p = norm(path); low = p.lower()
    if not p or SKIP.search(p):
        return 'skip'
    name = low.rsplit('/', 1)[-1]
    if '/tasks/' in '/' + low and (name in SPEC_NAMES or any(d in '/' + low for d in SPEC_DIRS)):
        return 'spec'
    if is_test(p):
        return 'test'
    if low.endswith(DOC_EXT):
        return 'spec' if SPEC_WORDS.search(name.rsplit('.', 1)[0]) else 'doc'
    if low.endswith(CODE_EXT):
        return 'code'
    return 'other'


def test_level(path):
    """Уровень теста по пути; модульный — по умолчанию."""
    p = norm(path)
    for level, rule in _LEVEL_RULES:
        if rule.search(p):
            return level
    return 'unit'


def spec_in_task(path, task_id):
    """Артефакт анализа этой задачи конвейера (docs/tasks/<id>/…)."""
    p = norm(path).lower()
    return classify(p) == 'spec' and ('/tasks/%s/' % task_id.lower()) in '/' + p
