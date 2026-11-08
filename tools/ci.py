"""CI/CD TaskTreker: сборка, проверки, развёртывание, тег релиза и откат — по конфигурации проекта.

Конфигурация — поле `ci` проекта в projects.json (ручной слой):

    "ci": {
      "steps": [
        {"name": "Политика изменений", "kind": "lint", "cmd": ["{repo}/.venv/Scripts/python.exe", "-X", "utf8",
                 "scripts/check-change-policy.py", "--base", "{base}"], "on": ["task"]},
        {"name": "Тесты бэкенда", "kind": "test", "cmd": ["{repo}/.venv/Scripts/python.exe", "-m", "pytest", "backend/tests", "-q"]},
        {"name": "Flutter analyze", "kind": "lint", "cmd": ["{repo}/.tools/flutter/bin/flutter.bat", "analyze"], "cwd": "app"}
      ],
      "deploy": {"test": {"cmd": [...], "health": [...]}, "main": {"cmd": [...], "health": [...]}},
      "tag": true
    }

Шаг: name, kind (install|lint|build|test|e2e|security), cmd (список аргументов), cwd (относительно
рабочей копии), on (где запускать: task — ветка задачи до слияния, test — тестовая ветка после деплоя,
main — main после релиза; по умолчанию все три), timeout (секунды), optional (провал не валит прогон).
Плейсхолдеры: {repo} — основная копия репозитория (там .venv и инструменты, которых нет в рабочих
копиях), {cwd} — рабочая копия прогона, {base}/{test}/{main} — ветки проекта, {branch} — ветка прогона.

Без `ci` берётся прежняя команда verify проекта, без неё — автоопределение по репозиторию
(скрипты проверки проекта, Python, Node, Flutter). Прогоны — data/ci.json (автоматический файл),
журналы шагов — ops/logs/ci/<прогон>.log.

    py tools/ci.py show <проект>          конфигурация и откуда она взята
    py tools/ci.py run <проект> [ветка]   прогон вручную (scope main)
    py tools/ci.py export <проект>        .github/workflows/tasktreker-ci.yml в репозитории проекта
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_dir():
    return Path(os.environ.get('TASKTREKER_DATA') or ROOT / 'data')


def logs_dir():
    """ops/logs/ci рядом с данными: у тестов свой каталог данных — и свои журналы."""
    return data_dir().parent / 'ops' / 'logs' / 'ci'


KINDS = ('install', 'lint', 'build', 'test', 'e2e', 'security')
SCOPES = ('task', 'test', 'main')
# Итог прогона (TT3-009). Разрешает слияние, ворота и релиз только passed — и только через green():
# not_configured — шагов нет, stale — ветка ушла вперёд во время прогона, error — шаг не смог запуститься.
# skipped — так до TT-102 назывался not_configured; старые записи ci.json читаются, но зелёными не считаются.
STATUSES = ('running', 'passed', 'failed', 'not_configured', 'stale', 'error')
STEP_TIMEOUT = 1200
KEEP_RUNS = 400


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _git(args, cwd, timeout=120):
    return subprocess.run(['git', '-c', 'core.quotepath=off', *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


# ---------------------------------------------------------------- конфигурация

def detect(repo):
    """Шаги по содержимому репозитория — когда у проекта нет `ci` и `verify`."""
    repo = Path(repo)
    steps = []
    if (repo / 'scripts' / 'check.ps1').exists():
        steps.append(dict(name='Проверки проекта (scripts/check.ps1)', kind='test',
                          cmd=['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', 'scripts/check.ps1']))
        return steps
    if (repo / 'scripts' / 'check.sh').exists():
        return [dict(name='Проверки проекта (scripts/check.sh)', kind='test', cmd=['bash', 'scripts/check.sh'])]
    py = '{repo}/.venv/Scripts/python.exe' if (repo / '.venv' / 'Scripts' / 'python.exe').exists() else sys.executable
    if any((repo / f).exists() for f in ('pyproject.toml', 'setup.py', 'requirements.txt')) and \
            any((repo / d).is_dir() for d in ('tests', 'test')):
        steps.append(dict(name='Тесты Python', kind='test', cmd=[py, '-m', 'pytest', '-q']))
    pkg = repo / 'package.json'
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding='utf-8')).get('scripts') or {}
        except ValueError:
            scripts = {}
        for key, kind in (('lint', 'lint'), ('build', 'build'), ('test', 'test')):
            if key in scripts:
                steps.append(dict(name='npm run ' + key, kind=kind, cmd=['npm', 'run', key]))
    for sub in ('', 'app'):
        if (repo / sub / 'pubspec.yaml').exists():
            flutter = '{repo}/.tools/flutter/bin/flutter.bat' if (repo / '.tools' / 'flutter').exists() else 'flutter'
            cwd = sub or '.'
            steps += [dict(name='Flutter analyze', kind='lint', cmd=[flutter, 'analyze'], cwd=cwd),
                      dict(name='Flutter test', kind='test', cmd=[flutter, 'test'], cwd=cwd)]
            break
    return steps


def config(project):
    """Конфигурация CI проекта и её источник: projects.json, verify или автоопределение."""
    repo = project.get('inspection_path') or project.get('path') or ''
    ci = project.get('ci')
    if isinstance(ci, dict) and ci.get('steps'):
        return dict(ci, source='projects.json')
    if project.get('verify'):
        return dict(steps=[dict(name='Проверки проекта (verify)', kind='test', cmd=project['verify'])],
                    deploy=(ci or {}).get('deploy') or {}, tag=True, source='verify')
    steps = detect(repo) if repo and Path(repo).is_dir() else []
    return dict(steps=steps, deploy=(ci or {}).get('deploy') or {}, tag=True, source='auto' if steps else 'none')


def validate(ci):
    """Ошибка понятным текстом или None — для ручной правки `ci` в projects.json."""
    if not isinstance(ci, dict):
        return 'ci должен быть объектом'
    for i, st in enumerate(ci.get('steps') or []):
        if not isinstance(st, dict) or not st.get('name') or not isinstance(st.get('cmd'), list) or not st['cmd']:
            return 'шаг %d: нужны name и cmd (список аргументов)' % (i + 1)
        if st.get('kind', 'test') not in KINDS:
            return 'шаг %s: kind — один из %s' % (st['name'], ', '.join(KINDS))
        if any(s not in SCOPES for s in st.get('on') or SCOPES):
            return 'шаг %s: on — из %s' % (st['name'], ', '.join(SCOPES))
    for env, d in (ci.get('deploy') or {}).items():
        if env not in ('test', 'main') or not isinstance(d, dict):
            return 'deploy: только test и main, каждый — объект с cmd и health'
    return None


def _child_env():
    """Шаги CI исполняют код из ветки задачи — недоверенный: без ключей владельца (SEC-009)."""
    import sandbox_env
    return sandbox_env.minimal(dict(PYTHONIOENCODING='utf-8'))


def _subst(value, ctx):
    out = str(value)
    for k, v in ctx.items():
        out = out.replace('{%s}' % k, str(v))
    return out


# ---------------------------------------------------------------- прогоны

def _load():
    try:
        return json.loads((data_dir() / 'ci.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(runs=[])


def _save(doc):
    doc['runs'] = doc['runs'][-KEEP_RUNS:]
    tmp = data_dir() / 'ci.json.tmp'
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, data_dir() / 'ci.json')


def record(run):
    """Записать прогон (новый или обновлённый) — короткая запись под собственной блокировкой файла."""
    lock = data_dir() / '.ci.lock'
    for _ in range(100):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.close(fd); break
        except FileExistsError:
            if time.time() - lock.stat().st_mtime > 60:
                lock.unlink(missing_ok=True)
            time.sleep(0.1)
    try:
        doc = _load()
        doc['runs'] = [r for r in doc['runs'] if r['id'] != run['id']] + [run]
        _save(doc)
    finally:
        lock.unlink(missing_ok=True)


def runs(ref=None, scope=None, limit=50):
    rs = [r for r in _load()['runs'] if (ref is None or r.get('ref') == ref) and (scope is None or r.get('scope') == scope)]
    return rs[-limit:]


def last(ref=None, scope=None, branch=None, project=None):
    for r in reversed(_load()['runs']):
        if (ref is None or r.get('ref') == ref) and (scope is None or r.get('scope') == scope) \
                and (branch is None or r.get('branch') == branch) and (project is None or r.get('project') == project):
            return r
    return None


def run_pipeline(project_key, project, cwd, branch, scope, ref=None, trigger='', branches=None):
    """Прогнать шаги CI для scope в рабочей копии cwd. Запись прогона: status из STATUSES, sha_full — что проверено."""
    cfg = config(project)
    repo = project.get('inspection_path') or project.get('path') or cwd
    b = dict(branches or {})
    ctx = dict(repo=Path(repo).as_posix(), cwd=Path(cwd).as_posix(), branch=branch,
               base=b.get('base', ''), test=b.get('test', ''), main=b.get('main', ''))
    sha_full = head_sha(cwd)
    sha = sha_full[:7]
    rid = 'CI-%s-%s' % (datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'), os.urandom(3).hex())
    logs_dir().mkdir(parents=True, exist_ok=True)
    log = logs_dir() / (rid + '.log')
    run = dict(id=rid, project=project_key, ref=ref, branch=branch, sha=sha, scope=scope, trigger=trigger,
               sha_full=sha_full, source=cfg['source'], started=now(), status='running', steps=[], log=str(log))
    steps = [s for s in cfg['steps'] if scope in (s.get('on') or SCOPES)]
    if not any(not s.get('optional') for s in steps):
        # Нечего проверять — не «пропущено», а «не настроено»: релиз по такому прогону невозможен (fail-closed).
        run.update(status='not_configured', finished=now(),
                   note='обязательных шагов CI для «%s» нет (источник: %s): задайте поле ci в projects.json или verify'
                        % (scope, cfg['source']))
        record(run)
        return run
    record(run)
    final = 'passed'
    with open(log, 'w', encoding='utf-8') as out:
        for st in steps:
            cmd = [_subst(a, ctx) for a in st['cmd']]
            wd = Path(cwd) / _subst(st.get('cwd', '.'), ctx)
            out.write('\n===== %s · %s\n$ %s\n' % (st['name'], st.get('kind', 'test'), ' '.join(cmd))); out.flush()
            t0 = time.time()
            try:
                # Недоверенный код тестов — в задании ОС: память, процессы, время, сеть (07: A2-07). network: true — шаг с сетью.
                import job_runner
                r = job_runner.run(cmd, cwd=str(wd), stdout=out, stderr=subprocess.STDOUT, text=False,
                                   timeout=st.get('timeout', STEP_TIMEOUT), env=_child_env(), offline=not st.get('network'))
                status = 'passed' if r.returncode == 0 else 'failed'
                note = 'код %d' % r.returncode
            except subprocess.TimeoutExpired:
                status, note = 'failed', 'превышено время %d с' % st.get('timeout', STEP_TIMEOUT)
            except OSError as e:
                status, note = 'error', 'не запустился: %s' % e
            out.write('----- %s: %s (%s)\n' % (st['name'], status, note)); out.flush()
            run['steps'].append(dict(name=st['name'], kind=st.get('kind', 'test'), status=status, note=note,
                                     seconds=round(time.time() - t0, 1), optional=bool(st.get('optional'))))
            if status != 'passed' and not st.get('optional'):
                final = status
                break               # дальше не идём: падение раньше — дешевле
    if final == 'passed' and sha_full and head_sha(cwd) != sha_full:
        # Пока шли проверки, в рабочей копии появился новый коммит: проверено не то, что будет слито.
        final = 'stale'
        run['note'] = 'во время прогона HEAD сменился с %s на %s — нужен новый прогон' % (sha, head_sha(cwd)[:7])
    if final == 'passed' and scope in ('test', 'main'):
        # Сканирование ИБ на том же коммите, сервером (SEC-012): находки решают ворота выпуска, не итог CI.
        try:
            import security_scan
            sc = security_scan.scan(cwd, project_key, sha=sha_full, ref=ref, source='ci:' + scope)
            run['security'] = dict(id=sc['id'], counts=sc['counts'], scanners={k: v['status'] for k, v in sc['scanners'].items()})
        except Exception as e:  # noqa: BLE001 — сбой сканера не красит CI, но виден как not_run
            run['security'] = dict(error=str(e)[:200])
    run.update(status=final, finished=now(), tail=tail(log, 30))
    record(run)
    return run


def head_sha(cwd, rev='HEAD'):
    """Полный SHA ревизии или '' — сверка идёт по полному, короткий только для показа."""
    try:
        r = _git(['rev-parse', '--verify', '--quiet', rev + '^{commit}'], cwd)
    except (OSError, subprocess.SubprocessError):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''


def green(run, head=None):
    """(ok, причина): разрешает ли прогон слияние, ворота или релиз. Единственное место такого решения (TT3-009…013).

    Зелёный только passed, у которого хотя бы один обязательный шаг реально прошёл, и — если передан head —
    проверенный SHA совпадает с текущим. Нет прогона, not_configured, skipped старых записей, stale, error,
    running — всё отказ: текстом агента машинную проверку не заменить."""
    if not run:
        return False, 'прогона CI нет'
    st = run.get('status')
    if st != 'passed':
        why = {'not_configured': 'CI не настроен', 'skipped': 'CI не настроен (прогон без шагов)',
               'stale': 'прогон на устаревшем коммите', 'error': 'шаг CI не запустился',
               'running': 'прогон ещё идёт', 'failed': 'CI упал'}.get(st, 'неизвестный итог CI «%s»' % st)
        return False, '%s: %s' % (why, summary(run))
    if not any(s.get('status') == 'passed' and not s.get('optional') for s in run.get('steps') or []):
        return False, 'в прогоне %s нет пройденных обязательных шагов' % run.get('id')
    if head:
        done = run.get('sha_full') or ''
        # Старые записи хранят только короткий SHA — тогда сверяем префикс, но не пустоту.
        same = done == head if done else bool(run.get('sha')) and head.startswith(run['sha'])
        if not same:
            return False, 'прогон %s проверял %s, а ветка уже на %s — нужен новый прогон' % (
                run.get('id'), (done or run.get('sha') or '?')[:7], head[:7])
    return True, summary(run)


def summary(run):
    if not run:
        return 'прогона CI нет'
    if run['status'] in ('skipped', 'not_configured'):
        return run.get('note') or 'CI не настроен'
    bad = [s for s in run['steps'] if s['status'] != 'passed' and not s.get('optional')]
    word = {'passed': 'пройден', 'stale': 'УСТАРЕЛ', 'error': 'НЕ ЗАПУСТИЛСЯ', 'running': 'идёт'}.get(run['status'], 'УПАЛ')
    head = '%s %s на %s@%s' % (run['id'], word, run.get('branch'), run.get('sha'))
    if run['status'] == 'stale':
        return '%s: %s' % (head, run.get('note') or 'ветка ушла вперёд')
    if bad:
        return '%s: шаг «%s» — %s; журнал %s' % (head, bad[0]['name'], bad[0]['note'], run['log'])
    return '%s: %s' % (head, ', '.join('%s %.0fс' % (s['name'], s['seconds']) for s in run['steps']))


def tail(path, n=200):
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    return '\n'.join(lines[-n:])


# ---------------------------------------------------------------- деплой, тег, откат

def deploy(project, env, cwd, branches=None):
    """Развёртывание окружения env (test|main) и проверка работоспособности. Нет команды — (None, причина)."""
    d = (config(project).get('deploy') or {}).get(env) or {}
    if not d.get('cmd'):
        return None, 'команда развёртывания для «%s» не задана — окружение не разворачивается, проверяется ветка' % env
    repo = project.get('inspection_path') or project.get('path') or cwd
    ctx = dict(repo=Path(repo).as_posix(), cwd=Path(cwd).as_posix(), **{k: v for k, v in (branches or {}).items()})
    for key, title in (('cmd', 'развёртывание'), ('health', 'проверка работоспособности')):
        if not d.get(key):
            continue
        cmd = [_subst(a, ctx) for a in d[key]]
        try:
            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=d.get('timeout', STEP_TIMEOUT), stdin=subprocess.DEVNULL, env=_child_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, '%s не выполнено: %s' % (title, e)
        if r.returncode != 0:
            return False, '%s упало (код %d): %s' % (title, r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()[-300:])
    return True, 'окружение «%s» развёрнуто и отвечает' % env


def tag_release(cwd, task_id, title):
    name = 'release/%s-%s' % (datetime.now(timezone.utc).strftime('%Y%m%d-%H%M'), task_id.lower())
    r = _git(['-c', 'user.name=TaskTreker', '-c', 'user.email=tasktreker@localhost', 'tag', '-a', name, '-m',
              'Релиз %s: %s' % (task_id, title)], cwd)
    return (r.returncode == 0), (name if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200])


def rollback(cwd, sha):
    """Откатить слияние в main: revert merge-коммита sha (первый родитель — main)."""
    r = _git(['-c', 'user.name=TaskTreker', '-c', 'user.email=tasktreker@localhost', 'revert', '-m', '1', '--no-edit', sha], cwd)
    if r.returncode != 0:
        _git(['revert', '--abort'], cwd)
        return False, 'откат не удался: %s' % (r.stderr or r.stdout).strip()[:200]
    return True, 'слияние %s откачено коммитом %s' % (sha, _git(['rev-parse', '--short', 'HEAD'], cwd).stdout.strip())


# ---------------------------------------------------------------- GitHub Actions

def export_github(project, branches):
    """Тот же конвейер для GitHub Actions: push в базовую, тестовую и main ветку и pull request."""
    cfg = config(project)
    repo = Path(project.get('inspection_path') or project.get('path'))
    win = any('.ps1' in ' '.join(s['cmd']) or '.bat' in ' '.join(s['cmd']) or '.exe' in ' '.join(s['cmd']) for s in cfg['steps'])
    lines = ['# tasktreker: generated — py tools/ci.py export; правьте поле ci проекта в TaskTreker, а не этот файл',
             'name: TaskTreker CI', 'on:', '  push:', '    branches: [%s]' % ', '.join(json.dumps(b) for b in
                                                                             sorted({branches.get('base'), branches.get('test'), branches.get('main')} - {None, ''})),
             '  pull_request:', 'jobs:', '  ci:', '    runs-on: %s' % ('windows-latest' if win else 'ubuntu-latest'),
             '    steps:', '      - uses: actions/checkout@v4']
    for st in cfg['steps']:
        cmd = ' '.join(a.replace('{repo}', '${{ github.workspace }}').replace('{cwd}', '${{ github.workspace }}')
                       .replace('{base}', branches.get('base', '')).replace('{test}', branches.get('test', ''))
                       .replace('{main}', branches.get('main', '')) for a in st['cmd'])
        lines += ['      - name: %s' % json.dumps(st['name'], ensure_ascii=False)]
        if st.get('cwd') and st['cwd'] != '.':
            lines.append('        working-directory: %s' % st['cwd'])
        if st.get('optional'):
            lines.append('        continue-on-error: true')
        lines.append('        run: %s' % cmd)
    dest = repo / '.github' / 'workflows' / 'tasktreker-ci.yml'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return dest


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in ('show', 'run', 'export', 'runs'):
        print(__doc__); return 1
    import pipeline as P
    projects = json.loads((data_dir() / 'projects.json').read_text(encoding='utf-8'))['projects']
    if args[0] == 'runs':
        for r in runs(limit=int(args[1]) if len(args) > 1 else 20):
            print(summary(r))
        return 0
    key = args[1] if len(args) > 1 else ''
    if key not in projects:
        print('Нет такого проекта: %s' % key); return 1
    p = projects[key]; repo = p.get('inspection_path') or p.get('path')
    branches = P.branches(repo, p)
    if args[0] == 'show':
        cfg = config(p); err = validate(cfg)
        print(json.dumps(dict(cfg, branches=branches, error=err), ensure_ascii=False, indent=2)); return 0
    if args[0] == 'export':
        print(export_github(p, branches)); return 0
    branch = args[2] if len(args) > 2 else branches['main']
    import deploy as D
    wt = D.branch_worktree(repo, branch, branches['main'])
    r = run_pipeline(key, p, wt, branch, 'main', trigger='вручную', branches=branches)
    print(summary(r))
    return 0 if green(r)[0] else 1


if __name__ == '__main__':
    sys.exit(main())
