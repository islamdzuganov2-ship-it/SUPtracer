"""Оперативное состояние задач — отдельно от ручного projects.json (TT-45, ревью A6).

Ручной слой (projects.json) — паспорт задачи и её итог: что сделать, критерии,
размер, кому назначена — и закрыта ли она (done/cancelled, evidence, completed_at).
Оперативный слой (workflow.json → tasks) — то, что меняется по ходу исполнения:
промежуточный статус, метка занятости, отметки критериев, автопроверка, ревью,
заметка и история статуса. Его пишет workflow.py под той же блокировкой и той же
атомарной записью файла, что и запуски: метка и запуск не расходятся.

Читают задачу все одинаково — apply()/merge_task(): ручной слой, поверх него
оперативный. Оперативный статус помнит, поверх какого ручного статуса записан
(status_base). Если ручной статус с тех пор изменился — галочка в Obsidian, правка
в редакторе, регламент, — правка человека новее, показывается она: ручной ввод
не теряется и не перетирается. Сравниваются значения: ручной статус, возвращённый
к базе (queued → blocked → queued), снова открывает статус слоя. None в слое — «поле очищено»: оно скрывает
прежнее значение, оставшееся в ручном слое от старых версий.
"""
import contextlib
import copy
import json
import os
import tempfile
import time

# Поля, которые меняются по ходу исполнения и живут в оперативном слое.
OPS = ('status', 'status_note', 'status_history', 'claimed_by', 'acceptance_done',
       'auto_check', 'review', 'evidence')
# Итог задачи: переход в закрытый статус и обратно пишется и в ручной слой.
OUTCOME = ('status', 'evidence', 'completed_at')
FINAL = ('done', 'cancelled')
LOCK_STALE_SECONDS = 120     # блокировка старше — след упавшего процесса


class Busy(ValueError):
    """Блокировку держит другая операция дольше, чем ждали."""


def read_json(folder, name, default):
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        return copy.deepcopy(default)
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def save_json(folder, name, value):
    """Атомарно: читатель видит прежний файл или новый, но не половину."""
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, os.path.join(folder, name))
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


@contextlib.contextmanager
def locked(folder, wait=0.0):
    """Общая блокировка всех, кто пишет задачи: доски, исполнители, Obsidian pull.
    wait — сколько секунд подождать занятую блокировку (доска отвечает сразу)."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, '.workflow.lock')
    deadline, cleared = time.time() + wait, False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY); break
        except (FileExistsError, PermissionError):
            # Windows: файл, который владелец удаляет, пока на нём открыт чужой дескриптор
            # (stat, антивирус), даёт PermissionError, а не FileExistsError — это та же занятость.
            # Упавший процесс оставляет файл навсегда; операция длится доли секунды.
            try: stale = time.time() - os.stat(path).st_mtime > LOCK_STALE_SECONDS
            except FileNotFoundError: stale = True
            except PermissionError: stale = False      # файл как раз удаляют — не устарел
            if stale and not cleared:
                cleared = True
                with contextlib.suppress(FileNotFoundError, PermissionError): os.unlink(path)
            elif time.time() < deadline:
                time.sleep(0.05)
            else:
                raise Busy('Другая операция уже меняет задачи; повторите позже')
    try:
        os.close(fd)
        yield
    finally: os.unlink(path)


def overlay(folder):
    """Оперативный слой: ref -> поля. Файла нет — слой пуст."""
    try:
        return read_json(folder, 'workflow.json', {}).get('tasks') or {}
    except (OSError, ValueError):
        return {}


def merge_task(manual, entry):
    """Задача, как её видят все: ручной слой, поверх него оперативный."""
    t = dict(manual)
    for key, value in (entry or {}).items():
        if key in ('status', 'status_base'): continue
        if value is None: t.pop(key, None)
        else: t[key] = value
    if entry and 'status' in entry:
        # Без status_base — запись до TT-45: оперативный статус тогда главнее.
        if 'status_base' not in entry or entry['status_base'] == manual.get('status'):
            t['status'] = entry['status']
    return t


def apply(doc, folder):
    """projects.json (весь документ) с действующим состоянием задач. Исходник не меняется."""
    tasks = overlay(folder)
    if not tasks or not isinstance(doc, dict): return doc
    out = dict(doc, projects={})
    for key, project in (doc.get('projects') or {}).items():
        backlog = project.get('backlog')
        if not isinstance(backlog, list):
            out['projects'][key] = project; continue
        out['projects'][key] = dict(project, backlog=[
            merge_task(b, tasks.get('%s/%s' % (key, b.get('id')))) if isinstance(b, dict) else b
            for b in backlog])
    return out


def split(patch, manual, entry):
    """Правка задачи -> (в ручной слой, в оперативный, что слою забыть).

    Паспорт — в ручной. Оперативные поля — в слой; статус — с базой. Переход
    в итоговый статус (done, cancelled) и выход из него — итог: status, evidence,
    completed_at уходят в ручной слой, а слой свои статус и evidence забывает."""
    current = merge_task(manual, entry).get('status')
    outcome = 'status' in patch and (patch['status'] in FINAL or current in FINAL)
    to_manual, to_ops = {}, {}
    for key, value in patch.items():
        if outcome and key in OUTCOME: to_manual[key] = value
        elif key in OPS: to_ops[key] = value
        else: to_manual[key] = value
    if 'status' in to_ops:
        to_ops['status_base'] = manual.get('status')
    if outcome and 'evidence' not in patch and (entry or {}).get('evidence') is not None:
        to_manual['evidence'] = entry['evidence']    # доказательство из работы уходит в итог, а не пропадает
    return to_manual, to_ops, ('status', 'status_base', 'evidence') if outcome else ()


def migrate(manual, tasks, ref):
    """Оперативные поля, записанные в ручной слой до TT-45, переезжают в слой —
    при первой записи этой задачи в ручной слой. Значение не теряется: слой
    его подхватывает, если своего у него нет."""
    entry = dict(tasks.get(ref) or {})
    for key in OPS:
        if key in OUTCOME or key not in manual: continue
        value = manual.pop(key)
        if key not in entry: entry[key] = value
        elif entry[key] is None: entry.pop(key)     # очищено в слое — прежнее значение не нужно
    if entry: tasks[ref] = entry
    else: tasks.pop(ref, None)


def write_entry(tasks, ref, manual, changes, forget=()):
    """Записать оперативные поля задачи. Очистка (None) поля, которого нет в ручном
    слое, просто убирает его из слоя; есть — оставляет None, скрывающий прежнее значение."""
    entry = dict(tasks.get(ref) or {})
    for key in forget: entry.pop(key, None)
    for key, value in changes.items():
        if value is None and key != 'status_base' and key not in manual: entry.pop(key, None)
        else: entry[key] = value
    if entry: tasks[ref] = entry
    else: tasks.pop(ref, None)


def commit(projects_doc, tasks, patches):
    """Применить правки задач к обоим слоям в памяти. -> изменён ли ручной слой.

    Вызывающий держит блокировку и сохраняет: workflow.json — всегда, projects.json —
    только если вернулось True. Так оперативная правка не трогает ручной файл."""
    dirty = False
    for ref, patch in patches.items():
        key, tid = ref.split('/', 1)
        manual = next(b for b in projects_doc['projects'][key]['backlog'] if b.get('id') == tid)
        to_manual, to_ops, forget = split(patch, manual, tasks.get(ref))
        # Холостая правка (то же значение, очистка отсутствующего поля) ручной файл не трогает.
        to_manual = {k: v for k, v in to_manual.items()
                     if (manual[k] != v if k in manual else v is not None)}
        if to_manual:
            migrate(manual, tasks, ref)
            manual.update(to_manual)
            for k in [k for k, v in manual.items() if v is None]: manual.pop(k)
            dirty = True
        write_entry(tasks, ref, manual, to_ops, forget)
    return dirty
