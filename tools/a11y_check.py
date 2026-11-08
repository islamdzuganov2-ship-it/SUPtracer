"""Статическая проверка доступности разметки дашборда (07: AG-DES-05…07).

Без браузера ловит то, что видно в исходнике: язык документа, картинки без alt, кнопки без текста и
без aria-label, поля ввода без подписи (label, aria-label, title или placeholder). Разметка строится в JS
шаблонными строками — они проверяются так же, как HTML. Это не замена проверки с экранным диктором,
а нижняя планка, которую сборка держит автоматически.

    py tools/a11y_check.py [файл ...]     # по умолчанию shell.html и *.js дашборда
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT = ['shell.html', 'app.js', 'boards.js', 'board_extras.js', 'bell.js', 'tree.js', 'taskview.js', 'projview.js',
           'secview.js', 'learnview.js', 'charts.js']
TAG = re.compile(r'<(img|button|input|select|textarea)\b([^<>]*?)(/?)>(.*?)(?=</\1>|<(?:img|button|input|select|textarea)\b|$)', re.S | re.I)
ATTR = lambda name: re.compile(r'\b%s\s*=' % name, re.I)


def _named(attrs):
    return any(ATTR(a).search(attrs) for a in ('aria-label', 'aria-labelledby', 'title'))


def check_text(text, name='?'):
    """[(файл, строка, проблема)]."""
    out = []
    if re.search(r'<html\b', text, re.I) and not re.search(r'<html[^>]*\blang\s*=', text, re.I):
        out.append((name, 1, 'у <html> нет lang'))
    for m in TAG.finditer(text):
        tag, attrs, inner = m.group(1).lower(), m.group(2), m.group(4)
        line = text.count('\n', 0, m.start()) + 1
        if tag == 'img' and not ATTR('alt').search(attrs):
            out.append((name, line, '<img> без alt'))
        elif tag == 'button':
            body = re.sub(r'<[^>]+>', '', inner.split('</button>')[0] if '</button>' in inner else inner[:200]).strip()
            if not body and not _named(attrs):
                out.append((name, line, '<button> без текста и aria-label'))
        elif tag in ('input', 'select', 'textarea'):
            if re.search(r'type\s*=\s*["\']?(hidden|submit|button|checkbox|radio)', attrs, re.I):
                continue
            idm = re.search(r'\bid\s*=\s*["\']([^"\']+)', attrs)
            labelled = idm and re.search(r'<label[^>]*\bfor\s*=\s*["\']%s["\']' % re.escape(idm.group(1)), text)
            wrapped = text.rfind('<label', 0, m.start()) > text.rfind('</label>', 0, m.start())
            if not (_named(attrs) or ATTR('placeholder').search(attrs) or labelled or wrapped):
                out.append((name, line, '<%s> без подписи' % tag))
    return out


def page_lang():
    """Страницу собирает build_dashboard.py: язык задаёт его обёртка, а не фрагмент shell.html."""
    src = (ROOT / 'build_dashboard.py').read_text(encoding='utf-8')
    return [] if 'html lang=\\"ru\\"' in src or 'html lang="ru"' in src else [('build_dashboard.py', 1, 'у <html> нет lang')]


def run(files=None):
    found = [] if files else page_lang()
    for f in files or [ROOT / n for n in DEFAULT]:
        p = Path(f)
        if p.exists():
            found += check_text(p.read_text(encoding='utf-8'), p.name)
    return found


def main(argv):
    found = run(argv or None)
    for f, line, why in found:
        print('%s:%d: %s' % (f, line, why))
    print('проблем доступности: %d' % len(found))
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
