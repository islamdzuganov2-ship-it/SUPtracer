"""Пакет доказательств выпуска (07: E2E-R39, A11-07, AG-OPS-10, AG-SECAG-10).

Когда задача уходит в main, сервер собирает в один файл всё, на чём основан выпуск: коммиты main и теста,
прогоны CI, записи SecurityEvidence, подписанное одобрение, политику, заморозку критериев, двойное ревью,
мутационную проверку, контракт группы, дефекты и маршрут. Файл подписан отпечатком sha256 канонического JSON:
`verify` пересчитывает его и ловит любую правку задним числом.

Файлы лежат в data/releases/<проект>/<id>-<sha7>.json — личные данные владельца, вне git.

Состояние доказательств для доски (`evidence_state`): PASS — последняя проверка выпуска прошла на текущем
коммите тестовой ветки; STALE — прошла, но тестовая ветка ушла вперёд; FAILED — не прошла; NOT_RUN — не было.
Доска не подменяет STALE и NOT_RUN зелёным: у каждого состояния есть источник и дата.
"""
import hashlib
import json
import sys
from pathlib import Path

VERSION = 'bundle-1'
STATES = ('PASS', 'STALE', 'FAILED', 'NOT_RUN')


def root():
    import workflow as w
    return w.data_dir() / 'releases'


def digest(bundle):
    body = {k: v for k, v in bundle.items() if k != 'digest'}
    return 'sha256:' + hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _pick(d, keys):
    return {k: d.get(k) for k in keys if d and d.get(k) is not None}


def build(t, pipe, main_sha, at):
    """Пакет из данных сервера. Ничего не берёт из текста агента, кроме уже проверенных сервером полей."""
    ev = pipe.get('evidence') or []
    roles = pipe.get('roles') or {}
    dev = roles.get('development/developer') or {}
    mutation = next((r for r in dev.get('result') or [] if r.get('id') == 'dev-mutation'), None)
    rel = (pipe.get('approvals') or {}).get('release') or {}
    b = dict(version=VERSION, ref=t['ref'], title=t.get('title'), project=t.get('project') or t['ref'].split('/')[0],
             at=at.isoformat(), main_sha=main_sha,
             test_sha=(ev[-1].get('commit_sha') if ev else None),
             acceptance=list(t.get('acceptance') or []), ac_freeze=pipe.get('ac_freeze'),
             route=_pick(pipe.get('route') or {}, ('applied', 'shadow', 'plan')),
             evidence=[_pick(e, ('check_id', 'status', 'commit_sha', 'ci_run', 'test_bundle_sha', 'policy_sha', 'policy_version',
                                 'runner_version', 'log_digest', 'completed_at', 'source', 'waiver_ref')) for e in ev[-6:]],
             approval={k: v for k, v in rel.items() if k != 'nonce'} or None,
             gates={k: _pick(g, ('decision', 'at', 'agent', 'model')) for k, g in (pipe.get('gates') or {}).items()},
             dual_review=pipe.get('dual_review'), mutation=mutation,
             contract=dict(group=pipe.get('contract_group'), seen=pipe.get('contract_seen')) if pipe.get('contract_group') else None,
             defects=[_pick(d, ('id', 'title', 'severity', 'kind', 'status', 'found_by', 'fix')) for d in pipe.get('defects') or []],
             conflicts=[_pick(c, ('id', 'kind', 'status', 'resolution')) for c in pipe.get('conflicts') or []],
             security=[_pick(e, ('check_id', 'status', 'commit_sha')) for e in ev if e.get('check_id') == 'release_evidence'][-1:],
             handoffs=[dict(role=k, at=e.get('at'), state=e.get('state')) for k, e in roles.items()])
    b['digest'] = digest(b)
    return b


def path_for(b):
    rid = b['ref'].split('/', 1)[-1]
    return root() / b['project'] / ('%s-%s.json' % (rid, (b.get('main_sha') or 'nosha')[:7]))


def write(b):
    p = path_for(b); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding='utf-8')
    return p


def verify(path):
    """(ok, detail): отпечаток пакета совпадает с содержимым."""
    try:
        b = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        return False, 'пакет не читается: %s' % e
    if b.get('version') != VERSION:
        return False, 'неизвестная версия пакета %s' % b.get('version')
    want = digest(b)
    if b.get('digest') != want:
        return False, 'отпечаток не совпадает: пакет изменён после выпуска'
    return True, 'пакет %s цел: %s' % (b['ref'], want[:19])


def evidence_state(pipe, test_sha):
    """Состояние доказательств выпуска для доски — с источником и датой, без подмены зелёным."""
    ev = [e for e in (pipe or {}).get('evidence') or [] if e.get('check_id') == 'release_evidence']
    if not ev:
        return dict(state='NOT_RUN', detail='проверка выпуска не запускалась', source='server', at=None)
    last = ev[-1]
    base = dict(source=last.get('source') or 'server', at=last.get('completed_at'), sha=(last.get('commit_sha') or '')[:10])
    if last.get('status') != 'passed':
        return dict(base, state='FAILED', detail='проверка выпуска не прошла на %s' % base['sha'])
    if test_sha and last.get('commit_sha') and not str(test_sha).startswith(last['commit_sha'][:7]) \
            and not last['commit_sha'].startswith(str(test_sha)[:7]):
        return dict(base, state='STALE', detail='доказательство на %s, тестовая ветка уже на %s' % (base['sha'], str(test_sha)[:10]))
    return dict(base, state='PASS', detail='проверка выпуска прошла на %s' % base['sha'])


def main(argv):
    if len(argv) >= 2 and argv[0] == 'verify':
        bad = 0
        for f in argv[1:]:
            ok, detail = verify(f); bad += not ok
            print(('OK   ' if ok else 'FAIL ') + detail)
        return 1 if bad else 0
    print('py tools/release_bundle.py verify <файл.json> [...]')
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
