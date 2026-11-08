"""Триаж находок безопасности и блокировка выпуска (TT-128; SEC-014, SEC-011, SEC-TC-21/22/23).

  * Блокирует выпуск открытая находка High/Critical в файлах, которые меняла задача (политика «новый код»:
    старый долг виден на дашборде, но не держит чужую задачу). Без списка файлов — все файлы проекта.
  * Обязательные сканеры (`security.required` в настройках владельца) не прошли на коммите — тоже блок:
    NOT_RUN не равно «чисто».
  * Снять находку можно только решением владельца: false_positive или accepted_risk с причиной и сроком.
    Решение подписано (owner_auth); запись в data/security/triage.json руками его не создаёт.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import security_scan as ss

BLOCKING = ('high', 'critical')
VERDICTS = ('false_positive', 'accepted_risk', 'fixed')
ACCEPT_DAYS = 30


def _path():
    import workflow as w
    return w.data_dir() / 'security' / 'triage.json'


def load():
    try:
        return json.loads(_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'decisions': {}}


def _save(d):
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8'); os.replace(tmp, p)


def waived(key, at=None):
    """Действующее подписанное решение владельца по находке или None."""
    import owner_auth as oa
    d = (load().get('decisions') or {}).get(key)
    if not d or not oa.verify(d) or d.get('key') != key:
        return None
    exp = d.get('expires_at')
    if exp and (at or datetime.now(timezone.utc)).isoformat() > exp:
        return None
    return d


def decide(key, verdict, why, origin, at=None, days=ACCEPT_DAYS):
    import owner_auth as oa
    if origin != oa.OWNER:
        raise ValueError('Снять находку безопасности может только владелец (терминал владельца или доска с токеном владельца)')
    if verdict not in VERDICTS:
        raise ValueError('verdict: ' + ' | '.join(VERDICTS))
    if len(str(why or '').strip()) < 10:
        raise ValueError('Причина решения обязательна (не короче 10 символов)')
    at = at or datetime.now(timezone.utc)
    rec = dict(kind='security_waiver', key=key, verdict=verdict, why=str(why).strip()[:500], at=at.isoformat(), by='владелец',
               expires_at=(at + timedelta(days=days)).isoformat() if verdict == 'accepted_risk' else None)
    d = load(); d.setdefault('decisions', {})[key] = oa.sign(rec); _save(d)
    return rec


def required(settings=None):
    r = ((settings or {}).get('security') or {}).get('required')
    return [x for x in r if isinstance(x, str)] if isinstance(r, list) else []


def blocking(project, ref=None, files=None, sha=None, settings=None):
    """Причины, по которым выпуск запрещён: [] — блокирующих находок нет."""
    if settings is None:
        try:
            import workflow as w
            settings = w.settings()
        except Exception:  # noqa: BLE001
            settings = {}
    scan = ss.latest(project, sha) if sha else ss.latest(project)
    out = []
    need = required(settings)
    if need:
        if not scan:
            return ['обязательное сканирование (%s) на коммите %s не проводилось' % (', '.join(need), (sha or '?')[:7])]
        for n in need:
            st = (scan.get('scanners') or {}).get(n) or {}
            if st.get('status') != 'ran':
                out.append('сканер %s: %s' % (n, st.get('note') or st.get('status') or 'не запускался'))
    if not scan:
        return out
    fs = {f.replace('\\', '/') for f in files} if files is not None else None
    for f in scan.get('findings') or []:
        if f.get('severity') in BLOCKING and (fs is None or f.get('file') in fs) and not waived(f['key']):
            out.append('%s %s %s:%s — %s' % (f['severity'].upper(), f['rule'], f['file'], f.get('line') or '?', f['message'][:120]))
    return out


def snapshot(limit=8):
    """Для дашборда: последние сканы по проектам, счётчики, NOT_RUN, открытые блокирующие."""
    scans = ss.load().get('scans') or []
    last = {}
    for x in scans:
        last[x['project']] = x
    rows = []
    for p, x in sorted(last.items()):
        open_ = [f for f in x.get('findings') or [] if not waived(f['key'])]
        rows.append(dict(project=p, sha=(x.get('sha') or '')[:7], at=x.get('at'), source=x.get('source'),
                         scanners={k: v.get('status') for k, v in (x.get('scanners') or {}).items()},
                         counts={s: sum(1 for f in open_ if f['severity'] == s) for s in ss.SEVERITIES},
                         waived=len(x.get('findings') or []) - len(open_),
                         top=[dict(severity=f['severity'], rule=f['rule'], file=f['file'], line=f.get('line'), message=f['message'][:140],
                                   key=f['key']) for f in sorted(open_, key=lambda f: -ss.SEVERITIES.index(f['severity']))[:limit]]))
    return dict(projects=rows, scans=len(scans))


def dispatch(req, origin):
    """tt_work op=security: action = scan | findings | snapshot | triage (только владелец)."""
    import workflow as w
    a = req.get('action') or 'snapshot'
    if a == 'snapshot':
        return snapshot()
    if a == 'findings':
        x = ss.latest(req.get('project'))
        if not x:
            return dict(project=req.get('project'), scan=None, note='сканирования ещё не было: pm.py security scan <проект>')
        return dict(x, findings=[dict(f, waived=bool(waived(f['key']))) for f in x['findings']])
    if a == 'scan':
        snap = w.snapshot(); p = (snap.get('projects') or {}).get(req.get('project'))
        if not p:
            raise ValueError('Неизвестный проект')
        root = p.get('inspection_path') or p.get('path')
        if not root or not os.path.isdir(root):
            raise ValueError('У проекта нет рабочей копии для сканирования')
        x = ss.scan(root, req.get('project'), source='tt_work')
        return dict(id=x['id'], sha=x['sha'], counts=x['counts'], scanners=x['scanners'])
    if a == 'triage':
        return decide(req.get('key'), req.get('verdict'), req.get('why'), origin)
    raise ValueError('action: snapshot | findings | scan | triage')


USAGE = """pm.py security — сканирование и триаж находок ИБ
  scan <проект>                       прогнать сканеры в рабочей копии проекта
  findings <проект>                   находки последнего сканирования
  status                              сводка по портфелю
  triage <key> --verdict false_positive|accepted_risk|fixed --why "…"   решение владельца (терминал владельца)"""


def main(argv=None):
    import sys
    import owner_auth as oa
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help', 'help'):
        print(USAGE); return 0
    origin = oa.OWNER if oa.interactive() else oa.AGENT
    cmd, rest = args[0], args[1:]
    opt = {}
    i = 0; pos = []
    while i < len(rest):
        if rest[i].startswith('--') and i + 1 < len(rest):
            opt[rest[i][2:]] = rest[i + 1]; i += 2
        else:
            pos.append(rest[i]); i += 1
    try:
        if cmd == 'scan':
            out = dispatch(dict(action='scan', project=(pos or [None])[0]), origin)
        elif cmd == 'findings':
            out = dispatch(dict(action='findings', project=(pos or [None])[0]), origin)
        elif cmd == 'status':
            out = snapshot()
        elif cmd == 'triage':
            out = dispatch(dict(action='triage', key=(pos or [None])[0], verdict=opt.get('verdict'), why=opt.get('why')), origin)
        else:
            print(USAGE); return 2
    except ValueError as e:
        print('Ошибка: %s' % e); return 1
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
