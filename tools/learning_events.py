"""Журнал наблюдений механизма развития агента (TT-112; ТЗ LEARN-03, REQ-DATA-01…06, §5, §7).

Что это и чего это не делает:
  * источник истины задач — projects.json и workflow.json, CI — ci.json, знаний — data/memory.
    Журнал хранит только то, чего там нет: попытки (предъявления результата) с привязкой к
    конфигурации модели, диагностику причин, использование знаний, решения и provenance;
  * события попыток выводятся из `pipeline.history` идемпотентной синхронизацией: у события
    `source` — ссылка на первичную запись, `event_id` — хеш источника. Повторная синхронизация,
    повторный webhook или команда не создают второго события и не удваивают метрики (AT-05);
  * журнал append-only, помесячные файлы `data/learning/events/ГГГГ-ММ.jsonl`. Исправление —
    новое событие (`classification_updated`, `evidence_attached`, `record_invalidated`), старое
    не меняется (REQ-DATA-03);
  * в событие не попадают тексты промптов, ответы модели, содержимое файлов и секреты: только
    короткие поля, ссылки и хеши; строки проходят вычистку секретов (REQ-DATA-06, AT-11);
  * повреждённая строка одного месяца не ломает чтение остальных: она пропускается и
    называется в `verify` (REQ-DATA-05). Каталог `data/learning/` исключён из git (REQ-DATA-04).
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
EVENT_TYPES = (
    'model_config_registered', 'attempt_submitted', 'attempt_rejected', 'attempt_accepted', 'task_accepted',
    'runtime_unavailable', 'cause_suggested', 'classification_updated', 'evidence_attached', 'record_invalidated',
    'knowledge_used', 'knowledge_candidate', 'knowledge_verified', 'knowledge_rejected', 'spec_revised',
    'decision_recorded', 'decision_status', 'tool_call', 'eval_registered', 'export_checked', 'training_quarantine', 'model_activation_refused', 'model_benchmark', 'model_activation_override', 'model_rollback',
)
MAX_TEXT = 400
SAFE_KEYS = ('schema_version', 'event_id', 'event_type', 'occurred_at', 'recorded_at', 'source', 'origin', 'project',
             'task_ref', 'run_id', 'attempt_id', 'attempt_no', 'model_config_id', 'agent', 'model', 'spec_revision',
             'context_digest', 'code_revision', 'stage', 'role', 'result', 'cause', 'evidence_refs', 'knowledge_refs',
             'target_event', 'reason', 'files', 'checks', 'work_type', 'size', 'tier', 'detail', 'decision', 'data')
SECRET = re.compile(r'(?:api[_-]?key|secret|token|passw(?:or)?d|bearer)\s*[:=]\s*["\']?[A-Za-z0-9_\-/+=.]{8,}'
                    r'|sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}'
                    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----', re.I)


def _w():
    import workflow
    return workflow


def root():
    return Path(_w().data_dir()) / 'learning'


def events_dir():
    return root() / 'events'


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(at=None):
    return (at or now()).isoformat()


def digest(value):
    raw = value if isinstance(value, (bytes, str)) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    raw = raw.encode('utf-8') if isinstance(raw, str) else raw
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def redact(value, limit=MAX_TEXT):
    """Строка без секретов и не длиннее limit; вложенные структуры — рекурсивно."""
    if isinstance(value, str):
        out = SECRET.sub('[секрет удалён]', value)
        return out if len(out) <= limit else out[:limit - 1] + '…'
    if isinstance(value, list):
        return [redact(v, limit) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k)[:60]: redact(v, limit) for k, v in list(value.items())[:40]}
    return value


def had_secret(value):
    return bool(SECRET.search(json.dumps(value, ensure_ascii=False))) if value is not None else False


def event_id(source):
    """Стабильный id по источнику: одно и то же наблюдение всегда даёт один id (идемпотентность)."""
    return 'evt_' + hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:24]


def _month(at):
    return (at if isinstance(at, str) else iso(at))[:7]


def _files():
    d = events_dir()
    return sorted(d.glob('*.jsonl')) if d.is_dir() else []


def read_all(problems=None):
    """Все события по порядку записи. Повреждённые строки пропускаются и попадают в problems."""
    out = []
    for f in _files():
        with open(f, encoding='utf-8', errors='replace') as fh:
            for no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if not isinstance(e, dict) or 'event_id' not in e:
                        raise ValueError('нет event_id')
                    out.append(e)
                except ValueError as exc:
                    if problems is not None:
                        problems.append(dict(file=f.name, line=no, error=str(exc)[:120]))
    return out


def ids():
    return {e['event_id'] for e in read_all()}


def _locked():
    import taskstate as ts
    os.makedirs(root(), exist_ok=True)
    return ts.locked(str(root()), wait=10.0)


def make(event_type, source, at=None, **fields):
    if event_type not in EVENT_TYPES:
        raise ValueError('Неизвестный вид события: %s' % event_type)
    at = at or now()
    e = dict(schema_version=SCHEMA_VERSION, event_id=event_id(dict(source, type=event_type)), event_type=event_type,
             occurred_at=fields.pop('occurred_at', None) or iso(at), recorded_at=iso(), source=source)
    secret = had_secret(fields)
    for k, v in fields.items():
        if k in SAFE_KEYS and v not in (None, '', [], {}):
            e[k] = redact(v)
    if secret:
        e['redacted'] = True
    return e


def append(events):
    """Записать новые события; уже записанные (тот же event_id) пропускаются. Возвращает записанные."""
    events = [e for e in events if e]
    if not events:
        return []
    with _locked():
        known = ids(); out = []
        by_month = {}
        for e in events:
            if e['event_id'] in known:
                continue
            known.add(e['event_id']); out.append(e)
            by_month.setdefault(_month(e['occurred_at']), []).append(e)
        if by_month:
            events_dir().mkdir(parents=True, exist_ok=True)
            for month, rows in by_month.items():
                with open(events_dir() / ('%s.jsonl' % month), 'a', encoding='utf-8') as fh:
                    for e in rows:
                        fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + '\n')
                    fh.flush(); os.fsync(fh.fileno())
        return out


def record(event_type, source, at=None, **fields):
    """Одно событие (команда или наблюдение). Повтор с тем же source — тот же id, без второй записи."""
    e = make(event_type, source, at, **fields)
    got = append([e])
    return dict(e, duplicate=not got)


def verify():
    """Проверка журнала: строки, контрольные суммы месяцев, повторы id (REQ-DATA-05)."""
    problems = []; months = {}
    seen = {}; dup = []
    for e in read_all(problems):
        if e['event_id'] in seen:
            dup.append(e['event_id'])
        seen[e['event_id']] = True
    for f in _files():
        months[f.stem] = dict(sha256=digest(f.read_bytes()), bytes=f.stat().st_size)
    return dict(ok=not problems and not dup, events=len(seen), months=months, problems=problems[:50], duplicates=dup[:50])


# ------------------------------------------------------------ синхронизация с конвейером

def model_config_of(run):
    """Конфигурация модели запуска: локальный исполнитель пишет свою, внешние — по имени модели."""
    cfg = (run.get('learning') or {}).get('model_config_id') or (run.get('launch') or {}).get('model_config_id') or run.get('model_config_id')
    if cfg:
        return cfg
    return ('%s:%s' % (run.get('agent'), run.get('model'))) if run.get('model') else None


def spec_revision(t):
    return digest(dict(title=t.get('title'), why=t.get('why'), acceptance=t.get('acceptance'), scope=t.get('scope')))


def derive(t, runs_by_id):
    """События попыток задачи из её pipeline.history: только разработчик, по порядку предъявлений."""
    pipe = t.get('pipeline') or {}
    hist = pipe.get('history') or []
    out = []; attempt = None; n = 0
    ref = t['ref']; project = t.get('project')
    base = dict(project=project, task_ref=ref, work_type=t.get('work_type'), size=t.get('size'))
    for i, h in enumerate(hist):
        src = dict(component='pipeline', ref=ref, kind=h.get('kind'), at=h.get('at'), run=h.get('run'), role=h.get('role'))
        if h.get('kind') == 'handoff' and h.get('role') == 'developer':
            n += 1
            run = runs_by_id.get(h.get('run')) or {}
            attempt = 'ATT-%s-%s-%02d' % (project, t.get('id'), n)
            out.append(make('attempt_submitted', src, occurred_at=h.get('at'), **base, run_id=h.get('run'),
                            attempt_id=attempt, attempt_no=n, model_config_id=model_config_of(dict(run, agent=h.get('agent') or run.get('agent'),
                                                                                                  model=h.get('model') or run.get('model'))),
                            agent=h.get('agent'), model=h.get('model'), stage='development', role='developer',
                            spec_revision=(run.get('learning') or {}).get('spec_revision'),
                            context_digest=(run.get('learning') or {}).get('context_digest'),
                            code_revision=(run.get('learning') or {}).get('code_revision'),
                            knowledge_refs=(run.get('learning') or {}).get('knowledge_refs'),
                            result='submitted' if h.get('accepted') else 'rejected_dod'))
            if not h.get('accepted'):
                out.append(make('attempt_rejected', dict(src, kind='handoff_rejected'), occurred_at=h.get('at'), **base,
                                run_id=h.get('run'), attempt_id=attempt, attempt_no=n, stage='development', role='server',
                                result='rejected', reason='DoD разработчика не выполнен: ' + ', '.join(h.get('failed') or []),
                                evidence_refs=['pipeline:%s#%d' % (ref, i)] + ['dod:%s' % f for f in (h.get('failed') or [])],
                                cause=dict(category='unknown', confidence='unverified')))
        elif h.get('kind') == 'return' and attempt and h.get('to_stage') in ('development', 'analysis', 'idea'):
            out.append(make('attempt_rejected', src, occurred_at=h.get('at'), **base, run_id=h.get('run'), attempt_id=attempt,
                            attempt_no=n, stage=h.get('stage'), role=h.get('role'), result='returned',
                            reason=h.get('reason'), evidence_refs=['pipeline:%s#%d' % (ref, i)] + ['defect:%s/%s' % (ref, d) for d in h.get('defects') or []],
                            cause=dict(category='unknown', confidence='unverified')))
        elif h.get('kind') == 'check' and attempt and h.get('role') == 'developer' and not h.get('accepted'):
            out.append(make('attempt_rejected', src, occurred_at=h.get('at'), **base, run_id=h.get('run'), attempt_id=attempt,
                            attempt_no=n, stage='development', role='server', result='rejected',
                            reason='проверки сервера: ' + ', '.join(h.get('failed') or []),
                            evidence_refs=['pipeline:%s#%d' % (ref, i)], cause=dict(category='unknown', confidence='unverified')))
    if pipe.get('stage') == 'done' or t.get('status') == 'done':
        at = t.get('completed_at') or (hist[-1].get('at') if hist else None)
        out.append(make('task_accepted', dict(component='pipeline', ref=ref, kind='done'), occurred_at=at, **base,
                        attempt_id=attempt, attempt_no=n, result='accepted', evidence_refs=['task:%s' % ref]))
    return out


def sync(tasks=None, state=None):
    """Идемпотентно перенести факты конвейера в журнал. Сбой синхронизации задачу не трогает."""
    w = _w()
    if not enabled():
        return dict(added=0, disabled=True)
    tasks = tasks if tasks is not None else w.snapshot()['tasks']
    state = state if state is not None else w.state()
    runs = {r['id']: r for r in state.get('runs') or []}
    new = []
    for t in tasks:
        if t.get('pipeline'):
            new += derive(t, runs)
    got = append(new)
    return dict(added=len(got), seen=len(new))


def enabled(settings=None):
    s = settings if settings is not None else _w().settings()
    return bool(((s or {}).get('learning') or {}).get('enabled', True))


def for_task(ref, events=None):
    return [e for e in (events if events is not None else read_all()) if e.get('task_ref') == ref]
