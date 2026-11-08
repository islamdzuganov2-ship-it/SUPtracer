"""Политика путей для операций агента с файлами владельца (TT-122; SEC-008, OBS-06, SEC-TC-10/11).

`tt_ingest` по MCP раньше принимал любой существующий путь: агент мог затянуть в память и
в контекст `~/.ssh`, чужой проект или файл по сетевому пути. Теперь путь проверяется так:

  * сетевые (UNC `\\\\host\\share`, `//host`) и служебные (`\\\\?\\`, `\\\\.\\`) пути — отказ;
  * путь приводится к настоящему (`realpath`): ссылки, junction и `..` раскрываются ДО проверки,
    поэтому ссылка внутри разрешённого каталога, ведущая наружу, не проходит;
  * настоящий путь должен лежать внутри разрешённого корня: хранилище Obsidian, каталоги проектов
    портфеля (`path`, `inspection_path`, `usage_paths`) и явные `ingest_roots` в `data/obsidian.json`;
  * временный каталог системы разрешён только для отдельных файлов Markdown — так ассистент
    передаёт заметку на приём, а каталог Temp целиком не читается;
  * читаются только текстовые заметки (.md, .markdown, .txt).
Ручная команда владельца `pm.py ingest` этой политикой не ограничена: это его терминал.
"""
import json
import os
import tempfile

TEXT = ('.md', '.markdown', '.txt')


def _norm(p):
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _inside(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                 # разные диски
        return False


def roots(data_dir, projects):
    out = []
    try:
        with open(os.path.join(data_dir, 'obsidian.json'), encoding='utf-8') as f:
            ob = json.load(f)
        out += [ob.get('vault')] + list(ob.get('ingest_roots') or [])
    except (OSError, ValueError):
        pass
    for p in (projects or {}).values():
        out += [p.get('path'), p.get('inspection_path')] + list(p.get('usage_paths') or [])
    seen = []
    for r in out:
        if isinstance(r, str) and r.strip() and not _network(r) and os.path.isdir(r):
            n = _norm(r)
            if n not in seen and os.path.dirname(n) != n:          # корень диска целиком не разрешается
                seen.append(n)
    return seen


def _network(p):
    s = str(p).replace('/', '\\')
    return s.startswith('\\\\')


def check(path, data_dir, projects, allow_temp_files=True):
    """Настоящий путь, если чтение разрешено; иначе ValueError с причиной."""
    if not isinstance(path, str) or not path.strip() or '\x00' in path:
        raise ValueError('путь: непустая строка')
    if _network(path):
        raise ValueError('сетевые и служебные пути не принимаются: %s' % path[:120])
    if not os.path.exists(path):
        raise ValueError('Нет файла или каталога: %s' % path[:200])
    real = _norm(path)
    if _network(real):
        raise ValueError('путь ведёт на сетевой ресурс через ссылку')
    if os.path.isfile(real) and not real.endswith(TEXT):
        raise ValueError('принимаются только заметки %s' % ', '.join(TEXT))
    allowed = roots(data_dir, projects)
    if any(_inside(real, r) for r in allowed):
        return real
    tmp = _norm(tempfile.gettempdir())
    if allow_temp_files and os.path.isfile(real) and _inside(real, tmp):
        return real
    raise ValueError('путь вне разрешённых корней (хранилище заметок, каталоги проектов, ingest_roots в data/obsidian.json): %s'
                     % path[:200])
