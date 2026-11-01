"""Сторож чтений сессии роли конвейера (Л3): хук PreToolUse на Read и SessionStart.

Замер 92 сессий ролей: 60% чтений артефактов задачи — повторное чтение того же файла
в той же сессии, а ТЗ и спецификацию читали целиком десятки раз. Прочитанное остаётся
в контексте и едет в каждом следующем ходе, поэтому лишнее чтение оплачивается много раз.

Сторож отказывает в двух случаях и сразу даёт то, что нужно вместо файла:
- полное чтение файла, который в этой сессии уже прочитан и с тех пор не менялся;
- полное чтение крупного Markdown-документа, который не артефакт этой роли, —
  с указателем документа: разделы, вес, строки и коды требований.
Чтение с offset/limit не ограничивается. Сжатие контекста (SessionStart compact)
сбрасывает учёт: после него прочитанного в контексте уже нет. Любая ошибка — чтение
разрешено: сторож не должен останавливать работу.

Учёт — в TASKTREKER_READS (по умолчанию временная папка): <сессия>.json с прочитанными
файлами и счётчиком сбережённого; его читает отчёт pm.py tokens.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BIG_TOKENS = 2000          # документ крупнее — только по указателю, если он не свой
REPEAT_BYTES = 8000        # повтор мелкого файла дешевле лишнего хода с отказом: отказ — только крупным
TEXT = {'.md', '.txt', '.py', '.dart', '.js', '.ts', '.tsx', '.json', '.yaml', '.yml', '.toml', '.ps1', '.sql', '.html', '.css'}


def state_dir():
    return Path(os.environ.get('TASKTREKER_READS') or Path(tempfile.gettempdir()) / 'tasktreker-reads')


def _state_path(session):
    safe = ''.join(ch for ch in str(session or 'session') if ch.isalnum() or ch in '-_')[:80] or 'session'
    return state_dir() / (safe + '.json')


def load(session):
    try:
        return json.loads(_state_path(session).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(files={}, denied=0, saved_tokens=0)


def save(session, st):
    p = _state_path(session)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')


def own(path):
    """Свой артефакт роли: TASKTREKER_OWN — пути через запятую относительно рабочей копии."""
    norm = str(path).replace('\\', '/').lower()
    return any(o and ('/' + o.strip('/').lower()) in norm for o in (os.environ.get('TASKTREKER_OWN') or '').split(','))


def deny(reason):
    return dict(hookSpecificOutput=dict(hookEventName='PreToolUse', permissionDecision='deny', permissionDecisionReason=reason))


def ceiling_note(data):
    """PostToolUse (TT-91): контекст сессии перерос потолок — один раз (и повторно на 130%) сказать роли
    сдать точку остановки для эстафеты. Контекст — из хвоста журнала сессии, без чтения всего файла."""
    ceiling = int(os.environ.get('TASKTREKER_CEILING') or 0)
    path = data.get('transcript_path')
    if not ceiling or not path or not os.path.isfile(path):
        return None
    import tokens
    ctx = tokens._last_usage_claude(path)
    session = data.get('session_id'); st = load(session); warned = st.get('ceiling_warned', 0)
    level = 2 if ctx >= ceiling * 1.3 else 1 if ctx >= ceiling else 0
    if level <= warned:
        return None
    st['ceiling_warned'] = level
    save(session, st)
    cmd = os.environ.get('TASKTREKER_RELAY') or 'tt_work op=pause reason=context checkpoint="…"'
    return dict(hookSpecificOutput=dict(hookEventName='PostToolUse', additionalContext=(
        'Контекст этой сессии — %d тыс. токенов, потолок %d тыс.: каждый следующий ход перечитывает его целиком. '
        'Если этап почти сдан — сдай handoff. Иначе доведи текущий шаг, закоммить и вызови %s '
        '(checkpoint: что сделано, что осталось, следующий шаг, нужные пути и разделы), затем остановись — '
        'сервер откроет свежую сессию этой роли с твоей точкой остановки.' % (ctx // 1000, ceiling // 1000, cmd))))


def decide(data):
    """Ответ хука (dict) или None — чтение разрешено."""
    if data.get('hook_event_name') == 'PostToolUse':
        return ceiling_note(data)
    if data.get('hook_event_name') == 'SessionStart':
        if data.get('source') == 'compact':
            st = load(data.get('session_id')); st['files'] = {}
            save(data.get('session_id'), st)
        return None
    if data.get('tool_name') != 'Read':
        return None
    ti = data.get('tool_input') or {}
    path = ti.get('file_path')
    if not path or ti.get('offset') or ti.get('limit') or ti.get('pages'):
        return None
    p = Path(path)
    if not p.is_absolute():
        p = Path(data.get('cwd') or '.') / p
    if p.suffix.lower() not in TEXT or not p.is_file():
        return None
    info = p.stat(); sig = [info.st_mtime_ns, info.st_size]
    session = data.get('session_id'); st = load(session)
    key = os.path.normcase(str(p.resolve()))
    if st['files'].get(key) == sig and info.st_size >= REPEAT_BYTES:
        st['denied'] += 1; st['saved_tokens'] += info.st_size * 10 // 25   # ~2.5 байта на токен (TT-93)
        save(session, st)
        return deny('Файл %s уже прочитан в этой сессии и с тех пор не менялся: его текст есть в контексте выше. '
                    'Нужен фрагмент — Read с offset/limit.' % p.name)
    if p.suffix.lower() == '.md' and not own(p):
        import pipeline as P
        text = p.read_text(encoding='utf-8', errors='replace')
        if P.tokens(text) > BIG_TOKENS:
            st['denied'] += 1; st['saved_tokens'] += P.tokens(text)
            save(session, st)
            idx = P.index_lines(P.doc_index(text), 60)
            return deny('%s — ~%d токенов, целиком не читай. Указатель (номер · заголовок · вес · строки · коды):\n%s\n'
                        'Возьми нужный раздел: Read с offset=<первая строка> limit=<число строк> '
                        'или tt_work op=doc path=<путь> section=<номер>|code=<FR-3>.'
                        % (p.name, P.tokens(text), '\n'.join(idx) or '(заголовков нет — читай частями через offset/limit)'))
    st['files'][key] = sig
    save(session, st)
    return None


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace') or '{}')
        out = decide(data)
    except Exception:          # noqa: BLE001 — сторож не останавливает работу
        out = None
    if out:
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
