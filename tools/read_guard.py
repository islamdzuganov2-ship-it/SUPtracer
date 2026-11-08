"""Сторож чтений сессии роли конвейера (Л3, TT-96): хуки PreToolUse на Read, PostToolUse и SessionStart.

Замер 92 сессий ролей: документы читали целиком 127 раз по ~16 тыс. символов; прочитанное остаётся
в контексте и едет в каждом следующем ходе, поэтому лишнее чтение оплачивается много раз.

Полное чтение не запрещено, оно должно быть обосновано ролью и сложностью задачи
(политика — pipeline.read_policy, приходит в TASKTREKER_READ_POLICY):
- свой артефакт роли и вход её этапа (аналитику — BRD, архитектору и QA — SRS) — целиком;
- документ не больше порога класса сложности (3, 5 или 8 тыс. токенов) — целиком;
- крупнее — сначала указатель (разделы, вес, строки, коды). Если задача действительно требует
  документа целиком, роль пишет зачем: tt_work op=doc full=true why=… — сервер выдаёт текст,
  записывает обоснование в журнал тимлида и считает бюджет обоснованных чтений запуска;
- повторное полное чтение неизменившегося файла от 8 КБ — отказ: его текст уже в контексте.
Чтение с offset/limit не ограничивается. Сжатие контекста (SessionStart compact)
сбрасывает учёт: после него прочитанного в контексте уже нет. Любая ошибка — чтение
разрешено: сторож не должен останавливать работу.

После каждого инструмента (PostToolUse) сторож смотрит журнал сессии: контекст выше потолка —
напоминание об эстафете (TT-91); обработано больше ориентира токенов запуска — напоминание
о бюджете прямо в сессии, а не при следующем продолжении.

Учёт — в TASKTREKER_READS (по умолчанию временная папка): <сессия>.json с прочитанными
файлами и счётчиком сбережённого; его читает отчёт pm.py tokens.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BIG_TOKENS = 3000          # порог без политики запуска (класс сложности 1)
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


def policy():
    try:
        return json.loads(os.environ.get('TASKTREKER_READ_POLICY') or '{}')
    except ValueError:
        return {}


def primary(path, pol):
    norm = str(path).replace('\\', '/').lower()
    return any(o and ('/' + o.strip('/').lower()) in norm for o in pol.get('primary') or [])


def deny(reason):
    return dict(hookSpecificOutput=dict(hookEventName='PreToolUse', permissionDecision='deny', permissionDecisionReason=reason))


def ceiling_note(path, st):
    """Контекст сессии перерос потолок (TT-91) — один раз (и повторно на 130%) сказать роли сдать точку
    остановки для эстафеты. Контекст — из хвоста журнала сессии, без чтения всего файла."""
    ceiling = int(os.environ.get('TASKTREKER_CEILING') or 0)
    if not ceiling:
        return None
    import tokens
    ctx = tokens._last_usage_claude(path)
    level = 2 if ctx >= ceiling * 1.3 else 1 if ctx >= ceiling else 0
    if level <= st.get('ceiling_warned', 0):
        return None
    st['ceiling_warned'] = level
    cmd = os.environ.get('TASKTREKER_RELAY') or 'tt_work op=pause reason=context checkpoint="…"'
    return ('Контекст этой сессии — %d тыс. токенов, потолок %d тыс.: каждый следующий ход перечитывает его целиком. '
            'Если этап почти сдан — сдай handoff. Иначе доведи текущий шаг, закоммить и вызови %s '
            '(checkpoint: что сделано, что осталось, следующий шаг, нужные пути и разделы), затем остановись — '
            'сервер откроет свежую сессию этой роли с твоей точкой остановки.' % (ctx // 1000, ceiling // 1000, cmd))


def processed(path, st):
    """Сколько токенов сессия уже обработала (вход, запись и чтение кеша, выход) — по журналу, с места прошлого
    подсчёта: хук не перечитывает журнал целиком на каждом инструменте. Ответ в журнале дважды — по requestId."""
    pos = st.get('b_pos', 0); total = st.get('b_total', 0); seen = st.get('b_ids', [])
    with open(path, 'rb') as fh:
        fh.seek(pos); chunk = fh.read()
    end = chunk.rfind(b'\n') + 1                  # только целые строки: последняя может дописываться
    for line in chunk[:end].decode('utf-8', 'replace').splitlines():
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get('message') or {}
        u = msg.get('usage') if isinstance(msg, dict) else None
        rid = row.get('requestId')
        if row.get('type') != 'assistant' or not isinstance(u, dict) or not rid or rid in seen:
            continue
        seen.append(rid)
        total += sum(int(u.get(k) or 0) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'))
    st.update(b_pos=pos + end, b_total=total, b_ids=seen[-50:])
    return total


def budget_note(path, st):
    """Запуск обработал ориентир токенов роли (100%) и полтора ориентира (150%) — сказать в самой сессии (TT-94)."""
    budget = int(os.environ.get('TASKTREKER_BUDGET') or 0)
    if not budget:
        return None
    used = processed(path, st)
    level = 2 if used >= budget * 1.5 else 1 if used >= budget else 0
    if level <= st.get('budget_warned', 0):
        return None
    st['budget_warned'] = level
    return ('Запуск обработал %.1f млн токенов — %d%% ориентира роли (%.1f млн). %s'
            % (used / 1e6, used * 100 // budget, budget / 1e6,
               'Сужай работу: только то, что нужно для DoD; не исследуй шире и не перечитывай.' if level == 1 else
               'Доведи этап до handoff коротким путём или подними блокер tt_work op=escalate — '
               'сервер отметил перерасход в журнале тимлида.'))


def post_tool(data):
    """PostToolUse: напоминания о потолке контекста и о бюджете запуска — каждое не чаще раза на уровень."""
    path = data.get('transcript_path')
    if not path or not os.path.isfile(path):
        return None
    session = data.get('session_id'); st = load(session)
    notes = [n for n in (ceiling_note(path, st), budget_note(path, st)) if n]
    save(session, st)
    if not notes:
        return None
    return dict(hookSpecificOutput=dict(hookEventName='PostToolUse', additionalContext='\n'.join(notes)))


def decide(data):
    """Ответ хука (dict) или None — чтение разрешено."""
    if data.get('hook_event_name') == 'PostToolUse':
        return post_tool(data)
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
    pol = policy()
    if p.suffix.lower() == '.md' and not own(p) and not primary(p, pol):
        import pipeline as P
        text = p.read_text(encoding='utf-8', errors='replace')
        size = P.tokens(text)
        if size > int(pol.get('free') or BIG_TOKENS):
            st['denied'] += 1; st['saved_tokens'] += size
            save(session, st)
            idx = P.index_lines(P.doc_index(text), 60)
            rel = str(p).replace('\\', '/')
            rel = rel[rel.find('docs/'):] if 'docs/' in rel else rel
            full = ('tt_work op=doc ref=%s run=%s path=%s full=true why="<зачем нужен весь документ>"'
                    % (pol.get('ref') or '<ref>', pol.get('run') or '<run>', rel))
            return deny('%s — ~%d токенов: для этой роли и задачи (класс сложности %s) целиком без причины не читается. '
                        'Указатель (номер · заголовок · вес · строки · коды):\n%s\n'
                        'Обычно хватает раздела: Read с offset=<первая строка> limit=<число строк> '
                        'или tt_work op=doc path=<путь> section=<номер>|code=<FR-3>. '
                        'Если задача действительно требует документа целиком (сквозная сверка, согласованность всех разделов), '
                        'обоснуй: %s — бюджет таких чтений запуска ~%d тыс. токенов.'
                        % (p.name, size, pol.get('tier') or '—', '\n'.join(idx) or '(заголовков нет — читай частями через offset/limit)',
                           full, int(pol.get('budget') or 0) // 1000))
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
