"""Защищённый эталон приёмки и доказательства выпуска (TT-124; SEC-010/011, OBS-09, SEC-TC-04/05/06/17/26).

Эталон (oracle) — файлы, которыми проверяют работу, а не сама работа: приёмочные тесты, правила
безопасности, конфигурация CI. Разработчик-ИИ пишет код и свои unit-тесты, но эталон не меняет:
иначе он «чинит» проверку вместо кода.

  * Защищённые пути проекта — `oracle_paths` в projects.json (решение владельца), иначе ORACLE_DEFAULT.
  * Нарушение считается по git, а не по чьему-то отчёту: файлы эталона, изменённые коммитами задачи
    (`merge-base(база, ветка)..ветка`). Копии состояния, которую можно подправить, нет.
  * Изменение эталона по делу (новое требование) разрешает только подписанное одобрение владельца
    `oracle_change` на этот набор изменений (хеш списка файлов и их содержимого).
  * `evidence()` собирает SecurityEvidence выпуска: SHA коммита, хеш эталона на нём, хеш политики,
    версию проверяющего кода, итог CI на этом SHA, журнал прогона. Агент такую запись не создаёт:
    её пишет сервер в момент проверки ворот, статус passed берётся только из ci.json.
"""
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

ORACLE_DEFAULT = ('tests/acceptance/', 'security/', 'ci/', '.github/workflows/', 'docs/acceptance/')
POLICY_VERSION = 'release-sec-v1'
TRUSTED = ('release_policy.py', 'trusted_oracle.py', 'ci.py', 'pipeline.py', 'deploy.py', 'security_triage.py', 'session_auth.py')


def _git(args, cwd):
    return subprocess.run(['git', '-c', 'core.quotepath=off', *args], cwd=cwd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=120)


def paths(project):
    p = (project or {}).get('oracle_paths')
    return tuple(x.replace('\\', '/').lstrip('./') for x in p) if isinstance(p, list) and p else ORACLE_DEFAULT


def _hit(f, ps):
    f = f.replace('\\', '/')
    return any(f == p.rstrip('/') or f.startswith(p if p.endswith('/') else p + '/') or f == p for p in ps)


def changed(repo, base, rev, project):
    """Файлы эталона, изменённые коммитами задачи (после точки ветвления от базы)."""
    mb = _git(['merge-base', base, rev], repo).stdout.strip()
    if not mb:
        return None, 'не найдена точка ветвления %s и %s' % (base, rev)
    files = [f for f in _git(['diff', '--name-only', mb, rev], repo).stdout.splitlines() if f.strip()]
    return [f for f in files if _hit(f, paths(project))], mb


def manifest_sha(repo, rev, project):
    """Хеш эталона на коммите: пути и blob-хеши всех файлов защищённых путей."""
    out = _git(['ls-tree', '-r', rev, '--', *paths(project)], repo).stdout
    return 'sha256:' + hashlib.sha256(out.encode('utf-8')).hexdigest()


def change_digest(repo, mb, rev, files):
    body = _git(['diff', mb, rev, '--', *files], repo).stdout if files else ''
    return 'sha256:' + hashlib.sha256((json.dumps(sorted(files)) + body).encode('utf-8')).hexdigest()


def approve_change(ref, digest, at, by='владелец'):
    import owner_auth as oa
    return oa.sign(dict(kind='oracle_change', ref=ref, digest=digest, at=at.isoformat(), by=by))


def intact(pipe, ref, repo, base, rev, project):
    """(ok, detail, changed_files). Неизменный эталон — ok; изменённый — только с подписью владельца на этот дифф."""
    if not repo or not rev:
        return False, 'нет ветки для проверки эталона', []
    files, mb = changed(repo, base, rev, project)
    if files is None:
        return False, mb, []
    if not files:
        return True, 'эталон приёмки не изменён (%s)' % ', '.join(paths(project)[:3]), []
    digest = change_digest(repo, mb, rev, files)
    a = ((pipe or {}).get('approvals') or {}).get('oracle')
    import owner_auth as oa
    if a and oa.verify(a) and a.get('kind') == 'oracle_change' and a.get('ref') == ref and a.get('digest') == digest:
        return True, 'эталон изменён с одобрения владельца %s: %s' % ((a.get('at') or '')[:16], ', '.join(files[:5])), files
    return False, ('изменён эталон приёмки без одобрения владельца: %s — верните файлы или попросите владельца одобрить '
                   'изменение требований' % ', '.join(files[:8])), files


def runner_version():
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for name in TRUSTED:
        p = here / name
        if p.exists():
            h.update(name.encode() + b'\0' + p.read_bytes())
    return 'sha256:' + h.hexdigest()[:32]


def policy_sha(settings):
    keys = ('autonomy', 'adaptive_mode', 'security_scan_required', 'release', 'policy')
    return 'sha256:' + hashlib.sha256(json.dumps({k: (settings or {}).get(k) for k in keys}, sort_keys=True,
                                                 ensure_ascii=False, default=str).encode('utf-8')).hexdigest()[:32]


def _log_digest(path):
    try:
        return 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return None


def evidence(ref, run_id, repo, sha, project, settings, check_id, status, ci_run=None, at=None, waiver=None):
    """Запись SecurityEvidence (поля ТЗ §4). source — всегда server: агент её не формирует."""
    from datetime import datetime, timezone
    at = at or datetime.now(timezone.utc)
    return dict(task_ref=ref, run_id=run_id, commit_sha=sha, test_bundle_sha=manifest_sha(repo, sha, project) if repo and sha else None,
                policy_sha=policy_sha(settings), policy_version=POLICY_VERSION, runner_version=runner_version(), check_id=check_id,
                status=status, started_at=(ci_run or {}).get('started'), completed_at=(ci_run or {}).get('finished') or at.isoformat(),
                log_digest=_log_digest((ci_run or {}).get('log')), ci_run=(ci_run or {}).get('id'), source='server',
                environment_fingerprint='%s %s / %s' % (platform.system(), platform.release(), sys.version.split()[0]),
                waiver_ref=waiver)
