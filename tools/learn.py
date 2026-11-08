"""Операции механизма развития агента: `tt_work op=learn action=…` и CLI `pm.py learn …` (TT-114, TT-117; ТЗ §12.3).

Права (ТЗ §12.3, REQ-RC-05, REQ-MEM-03, REQ-DEC-04):
  * чтение, синхронизация с конвейером, предложение причины и кандидатной карточки — любой источник;
    агент подтверждает причину только машинным доказательством (ci:/test:/defect:/repro:);
  * подтверждение карточки, отклонение, статус рекомендации, регистрация и активация конфигурации
    модели, набор оценки — только владелец (доска по ссылке владельца или терминал владельца).
Команд обучения и автоматического продвижения нет и не будет (`train --auto`, `promote --auto`).
"""
import json
import os
import sys
from datetime import datetime

import learning_causes as lc
import learning_events as le
import learning_gate as lg
import learning_knowledge as lk
import learning_metrics as lm
import local_mimo

OWNER_ACTIONS = ('verify_knowledge', 'reject_knowledge', 'decision_status', 'register_model', 'register_gguf', 'activate_model', 'rollback_model',
                 'register_eval')
READ_ACTIONS = ('status', 'report', 'returns', 'inspect', 'candidates', 'journal', 'models', 'card')


def _when(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=le.now().tzinfo)
    except ValueError:
        raise ValueError('Дата ISO 8601: %s' % v)


def _filters(req):
    return dict(project=req.get('project'), model_config_id=req.get('model_config_id'), agent=req.get('model') or req.get('agent'),
                since=_when(req.get('since')), until=_when(req.get('until')), work_type=req.get('work_type'), size=req.get('size'))


def refresh():
    """Синхронизировать факты конвейера и предложить причины новым возвратам. Сбой — отчёт, а не исключение."""
    try:
        out = le.sync()
        out['suggested'] = len(lc.suggest_all())
        return out
    except Exception as e:      # noqa: BLE001 — аналитика не роняет работу (§15.5)
        return dict(error=str(e)[:300])


def card(rep, cfg, record=True):
    """Одна компактная карточка «Качество ИИ-разработчика» (§12.1)."""
    m = rep['metrics']
    confirmed = [dict(c, title=lc.CATEGORIES.get(c['category'], (c['category'],))[0]) for c in rep['causes']
                 if c['category'] != 'не классифицировано'][:3]
    unclassified = next((c['count'] for c in rep['causes'] if c['category'] == 'не классифицировано'), 0)
    gate = lg.recommend(events=None, metrics=rep, project=rep['filters'].get('project'),
                        model_config_id=rep['filters'].get('model_config_id'), record=record)
    label = {'collecting_data': 'наблюдение', 'not_justified': 'наблюдение', 'process_fix_first': 'исправить процесс',
             'candidate': 'есть кандидат на эксперимент'}.get(gate['status'], gate['status'])
    return dict(config=dict(id=(cfg or {}).get('model_config_id'), model=(cfg or {}).get('model_id'),
                            quantization=(cfg or {}).get('quantization'), settings=(cfg or {}).get('system_prompt_revision')),
                accepted=rep['accepted'], open=rep['open'], insufficient=rep['metrics']['M04']['unknown_count'],
                fpar=m['M01'], rework=m['M02'], attempts=m['M03'], model_errors=m['M04'], diagnostics=m['M07'],
                top_causes=confirmed, unclassified=unclassified, status=label, decision=gate['decision_id'],
                gate_status=gate['status'], drilldown='tt_work op=learn action=returns', data_status=rep['status'])


def snapshot(limit=30):
    """Данные карточки для сборки дашборда: только чтение журнала, без синхронизации и без записи решений."""
    try:
        events = le.read_all()
        rep = lm.compute(events=events)
        out = card(rep, local_mimo.active(), record=False)
        rej = [e for e in events if e['event_type'] == 'attempt_rejected'][-limit:]
        out['returns'] = [dict(event_id=e['event_id'], task_ref=e.get('task_ref'), stage=e.get('stage'), by=e.get('role'),
                               at=e['occurred_at'], reason=(e.get('reason') or '')[:200], cause=lc.current(events, e['event_id']))
                          for e in reversed(rej)]
        out['metrics'] = {k: {f: v.get(f) for f in ('title', 'value', 'numerator', 'denominator', 'unknown_count', 'note')}
                          for k, v in rep['metrics'].items()}
        out['candidates'] = len(lk.candidates())
        return out
    except Exception as e:      # noqa: BLE001 — дашборд собирается и без этого раздела
        return dict(error=str(e)[:300])


def register_gguf(path, endpoint, origin, context_limit=4096, runtime_version=None, runtime_path=None):
    """Конфигурация из проверенного GGUF: веса по SHA-256, квантование и шаблон чата — из метаданных файла."""
    import gguf_probe
    if origin != 'owner':
        raise ValueError('Конфигурацию модели регистрирует владелец')
    if not path:
        raise ValueError('path: файл .gguf')
    info = gguf_probe.inspect(path, sha=True)
    runtime_sha = None
    if runtime_path:
        # Пин нативного рантайма (SEC-023): хеш исполняемого файла входит в конфигурацию, подмена — отказ активации.
        if not os.path.isfile(runtime_path):
            raise ValueError('runtime_path: файл рантайма не найден')
        runtime_sha = gguf_probe.file_sha256(runtime_path)
        runtime_version = runtime_version or runtime_sha[:23]
    if info['context_length'] and context_limit > info['context_length']:
        raise ValueError('context_limit больше контекста модели (%d)' % info['context_length'])
    cfg = dict(model_id=(info.get('meta') or {}).get('general.basename') or info['name'] or 'local-gguf',
               upstream_revision=info['name'], weight_sha256=info['weight_sha256'], quantization=info['quantization'],
               runtime_name='llama.cpp', runtime_version=runtime_version, chat_template_id=info['chat_template_sha256'],
               context_limit=context_limit, endpoint=endpoint, note='GGUF %s, %s, %d байт' % (info['architecture'], info['path'], info['bytes']),
               weight_path=info['path'], runtime_path=os.path.abspath(runtime_path) if runtime_path else None, runtime_sha256=runtime_sha)
    return dict(local_mimo.register(cfg, origin), gguf=dict((k, info[k]) for k in ('architecture', 'quantization', 'bytes', 'tensors', 'context_length')))


def dispatch(req, origin):
    a = req.get('action') or 'status'
    if a in OWNER_ACTIONS and origin != 'owner':
        raise ValueError('Только владелец: %s — доска по ссылке владельца или его терминал' % a)
    if a == 'sync':
        return refresh()
    if a in ('status', 'report', 'card'):
        refresh()
        rep = lm.compute(**_filters(req))
        if a == 'card':
            return card(rep, local_mimo.active())
        if a == 'report' and req.get('last_accepted'):
            rep['note'] = 'когорта: последние %s принятых задач — по датам приёмки' % req['last_accepted']
        return rep
    if a == 'returns':
        events = le.read_all(); f = _filters(req); out = []
        for e in events:
            if e['event_type'] != 'attempt_rejected':
                continue
            if f['project'] and e.get('project') != f['project']:
                continue
            c = lc.current(events, e['event_id'])
            if req.get('cause') and c.get('category') != req['cause']:
                continue
            if req.get('confirmed') is not None and (c.get('confidence') == 'verified') != bool(req['confirmed']):
                continue
            out.append(dict(event_id=e['event_id'], task_ref=e.get('task_ref'), attempt_id=e.get('attempt_id'), stage=e.get('stage'),
                            by=e.get('role'), at=e['occurred_at'], reason=e.get('reason'), evidence=e.get('evidence_refs'), cause=c))
        return dict(returns=out[-int(req.get('limit') or 100):], total=len(out))
    if a == 'inspect':
        if not req.get('ref'):
            raise ValueError('inspect: ref задачи')
        refresh()
        return lm.inspect(req['ref'])
    if a == 'classify':
        by = req.get('by') if origin == 'owner' else ('server' if origin == 'server' else 'agent')
        return lc.classify(req.get('event'), req.get('cause'), req.get('evidence') or [], by=by or 'owner', origin=origin,
                           description=req.get('description') or '', corrective_action=req.get('corrective_action'),
                           contributing=req.get('contributing') or [], preconditions=req.get('preconditions'))
    if a == 'candidates':
        return dict(candidates=lk.candidates(req.get('project')))
    if a == 'propose_knowledge':
        rec = lk.propose(req.get('event'), req.get('title'), req.get('problem'), req.get('applicability'), req.get('solution'),
                         req.get('not_when'), req.get('scope'), by=origin, origin=origin, source_commit=req.get('commit'))
        return dict(id=rec['id'], learning=rec['learning'])
    if a == 'verify_knowledge':
        rec = lk.verify(req.get('id'), by=req.get('by') or 'owner', origin=origin, evidence=req.get('evidence') or [])
        return dict(id=rec['id'], learning=rec['learning'], verified_by=rec['verified_by'])
    if a == 'reject_knowledge':
        rec = lk.reject(req.get('id'), req.get('why') or 'отклонена владельцем', origin, by=req.get('by_id'))
        return dict(id=rec['id'], status=rec['status'])
    if a == 'gate':
        refresh()
        return lg.recommend(project=req.get('project'), model_config_id=req.get('model_config_id'))
    if a == 'decision_status':
        return lg.set_status(req.get('decision'), req.get('status'), origin, req.get('reason') or '')
    if a == 'request_training':
        return lg.request_training(by=origin, reason=req.get('reason') or '')
    if a == 'check_split':
        return lg.check_split(req.get('items') or [])
    if a == 'register_eval':
        return lg.register_eval(req.get('name'), req.get('refs') or [], origin, req.get('purpose') or 'holdout')
    if a == 'models':
        reg = local_mimo.registry()
        return dict(active=reg.get('active'), configs=list(reg['configs'].values()), history=reg.get('history', [])[-20:],
                    health=local_mimo.health(local_mimo.active()) if req.get('health') else None)
    if a == 'register_model':
        return local_mimo.register(req.get('config') or {}, origin, allow_remote=bool(req.get('allow_remote')))
    if a == 'register_gguf':
        return register_gguf(req.get('path'), req.get('endpoint') or 'http://127.0.0.1:8080', origin,
                             context_limit=int(req.get('context_limit') or 4096), runtime_version=req.get('runtime_version'),
                             runtime_path=req.get('runtime_path'))
    if a == 'activate_model':
        return local_mimo.activate(req.get('id'), origin, override=req.get('override'))
    if a == 'rollback_model':
        return local_mimo.rollback(origin)
    if a == 'bench':
        import learn_bench
        cfgs = local_mimo.registry()['configs']
        cid = req.get('id') or (local_mimo.registry().get('active'))
        if cid and cid not in cfgs:
            raise ValueError('Конфигурация не найдена: %s' % cid)
        cfg = dict(cfgs[cid], model_config_id=cid) if cid else None
        out = learn_bench.run(cfg, origin=origin)
        return {k: v for k, v in out.items() if k != 'cases'} | dict(cases=[{k: c[k] for k in ('id', 'passed', 'steps', 'tampered', 'seconds', 'error')} for c in out['cases']])
    if a == 'journal':
        return le.verify()
    raise ValueError('learn: action — %s' % ', '.join(READ_ACTIONS + ('sync', 'classify', 'propose_knowledge', 'gate',
                                                                       'request_training', 'check_split', 'bench') + OWNER_ACTIONS))


USAGE = """py tools/pm.py learn status|report [--project P] [--model local_mimo] [--model-config ID] [--since ISO] [--work-type T]
py tools/pm.py learn returns [--project P] [--cause C] [--confirmed yes|no]
py tools/pm.py learn inspect <проект/id>
py tools/pm.py learn classify --event evt_… --cause <код> [--evidence ci:CI-1,test:…] [--fix updated_spec] [--why "…"]
py tools/pm.py learn knowledge candidates|propose|verify|reject …
py tools/pm.py learn training-gate [--project P]
py tools/pm.py learn models | model-register <config.json> | model-gguf <файл.gguf> [--endpoint URL] [--context 4096] [--runtime llama-server.exe] | model-activate <id> [--override "причина"] | model-rollback | bench [<id>]
py tools/pm.py learn journal | sync | card"""


def _opts(args):
    out, rest, i = {}, [], 0
    while i < len(args):
        if args[i].startswith('--'):
            k = args[i][2:].replace('-', '_')
            if i + 1 < len(args) and not args[i + 1].startswith('--'):
                out[k] = args[i + 1]; i += 2
            else:
                out[k] = True; i += 1
        else:
            rest.append(args[i]); i += 1
    return out, rest


def main(argv=None):
    import owner_auth as oa
    import workflow as w
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help', 'help'):
        print(USAGE); return 0
    cmd, (o, rest) = args[0], _opts(args[1:])
    origin = 'owner' if oa.interactive() else 'agent'       # терминал владельца, как pm.py owner-link
    req = dict(op='learn', project=o.get('project'), model=o.get('model'), model_config_id=o.get('model_config'),
               since=o.get('since'), until=o.get('until'), work_type=o.get('work_type'), size=o.get('size'))
    if cmd in ('status', 'report', 'returns', 'sync', 'journal', 'card', 'models'):
        req['action'] = cmd
        if cmd == 'returns':
            req.update(cause=o.get('cause'), confirmed=None if o.get('confirmed') is None else o['confirmed'] in ('yes', 'да', True))
        if cmd == 'report':
            req['last_accepted'] = o.get('last_accepted')
        if cmd == 'models':
            req['health'] = bool(o.get('health'))
    elif cmd == 'inspect':
        req.update(action='inspect', ref=(rest or [None])[0])
    elif cmd == 'classify':
        req.update(action='classify', event=o.get('event'), cause=o.get('cause'), evidence=[x for x in str(o.get('evidence') or '').split(',') if x],
                   corrective_action=o.get('fix'), description=o.get('why') or '', by=o.get('by') or 'owner',
                   preconditions={k: True for k in str(o.get('preconditions') or '').split(',') if k})
    elif cmd == 'knowledge':
        sub = (rest or ['candidates'])[0]
        if sub == 'candidates':
            req['action'] = 'candidates'
        elif sub == 'propose':
            req.update(action='propose_knowledge', event=o.get('event'), title=o.get('title'), problem=o.get('problem'),
                       applicability=o.get('applicability'), solution=o.get('solution'), not_when=o.get('not_when'),
                       scope=[x for x in str(o.get('scope') or '').split(',') if x], commit=o.get('commit'))
        elif sub == 'verify':
            req.update(action='verify_knowledge', id=(rest[1:] or [o.get('id')])[0], evidence=[x for x in str(o.get('evidence') or '').split(',') if x])
        elif sub == 'reject':
            req.update(action='reject_knowledge', id=(rest[1:] or [o.get('id')])[0], why=o.get('why'))
        else:
            print(USAGE); return 2
    elif cmd == 'training-gate':
        req['action'] = 'gate'
    elif cmd == 'decision':
        req.update(action='decision_status', decision=(rest or [None])[0], status=o.get('status'), reason=o.get('why'))
    elif cmd == 'model-register':
        with open(rest[0], encoding='utf-8') as f:
            req.update(action='register_model', config=json.load(f))
    elif cmd == 'model-gguf':
        req.update(action='register_gguf', path=(rest or [None])[0], endpoint=o.get('endpoint'), context_limit=o.get('context'),
                   runtime_version=o.get('runtime_version'), runtime_path=o.get('runtime'))
    elif cmd == 'model-activate':
        req.update(action='activate_model', id=(rest or [None])[0], override=o.get('override'))
    elif cmd == 'model-rollback':
        req.update(action='rollback_model')
    elif cmd == 'bench':
        req.update(action='bench', id=(rest or [None])[0])
    else:
        print(USAGE); return 2
    try:
        out = w.action({k: v for k, v in req.items() if v is not None}, origin=origin)
    except ValueError as e:
        print(e); return 1
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
