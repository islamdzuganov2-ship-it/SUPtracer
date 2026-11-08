"""Тяжёлые проверки конвейера: тесты проекта и слияния веток (деплой).

Идут фоновым заданием, не под блокировкой задач. Слияния — только здесь, сервером,
по пройденному DoD: агенты не сливают ветки сами.

- merge_to_base — ветка задачи в базовую ветку проекта (основная рабочая копия);
- deploy_test   — базовая ветка в тестовую (своя рабочая копия <repo>.worktrees/deploy-<ветка>);
- deploy_main   — тестовая ветка в main (так же);
- ci_* / verify* — прогон CI проекта (tools/ci.py) в рабочей копии нужной ветки: task — ветка задачи,
  test — тестовая ветка после деплоя, main — main после релиза (красный CI на main откатывает слияние);
- env_*         — развёртывание окружения и проверка работоспособности, если они заданы у проекта;
- release_tag   — тег релиза в main.
Без шагов CI у проекта пункт CI не проходит (not_configured): текст агента машинную проверку не заменяет.
Слияние в main — только по подписанному одобрению владельца или гранту (release_policy), ровно того
коммита тестовой ветки, который одобрен. При аварийной остановке ни одно слияние не выполняется.
"""
import re
import subprocess
from pathlib import Path

import autocheck
import ci

VERIFY_TIMEOUT = 900


def _git(args, cwd, timeout=120):
    return subprocess.run(['git', '-c', 'core.quotepath=off', *args], cwd=cwd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def reapply(wt, task_id):
    """Перед повторным релизом вернуть откаченное слияние задачи: иначе git сочтёт тестовую ветку уже слитой."""
    log = _git(['log', '-200', '--format=%H%x1f%s'], wt).stdout.splitlines()
    rows = [l.split('', 1) for l in log if '' in l and task_id in l]
    reverts = [h for h, subj in rows if subj.startswith('Revert "') and not subj.startswith('Revert "Revert')]
    again = [h for h, subj in rows if subj.startswith('Revert "Revert') or subj.startswith('Reapply')]
    if len(reverts) <= len(again):
        return ''
    r = _git(['-c', 'user.name=TaskTreker', '-c', 'user.email=tasktreker@localhost', 'revert', '--no-edit', reverts[0]], wt)
    if r.returncode != 0:
        _git(['revert', '--abort'], wt)
        raise ValueError('не удалось вернуть откаченный релиз %s: %s' % (reverts[0][:9], (r.stderr or r.stdout).strip()[:200]))
    return 'прежний откат %s возвращён' % reverts[0][:9]


def branch_worktree(repo, branch, start=None):
    """Рабочая копия для ветки деплоя. Нет ветки — создаётся от start (обычно main)."""
    repo = Path(repo)
    slug = re.sub(r'[^\w.-]+', '-', branch).strip('-')
    path = repo.parent / (repo.name + '.worktrees') / ('deploy-' + slug)
    listed = _git(['worktree', 'list', '--porcelain'], repo).stdout.replace('\\', '/')
    for block in listed.split('\n\n'):
        if 'branch refs/heads/%s' % branch in block:
            return Path(block.splitlines()[0].split(' ', 1)[1])
    if _git(['rev-parse', '--verify', '--quiet', 'refs/heads/' + branch], repo).returncode != 0:
        r = _git(['branch', branch, start or 'HEAD'], repo)
        if r.returncode != 0:
            raise ValueError('не удалось создать ветку %s: %s' % (branch, r.stderr.strip()[:200]))
    r = _git(['worktree', 'add', str(path), branch], repo)
    if r.returncode != 0:
        raise ValueError('не удалось открыть рабочую копию %s: %s' % (branch, r.stderr.strip()[:200]))
    return path


def verify(cwd, cmd):
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=VERIFY_TIMEOUT)
        tail = ((r.stdout or '') + '\n' + (r.stderr or '')).strip()[-400:]
        return r.returncode == 0, ('проверки прошли' if r.returncode == 0 else 'проверки упали') + ': ' + tail
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, 'проверки не запустились: %s' % e


def merged(repo, sha, branch):
    """(применено?, пояснение): коммит sha уже в ветке branch."""
    if not sha:
        return False, 'нет коммита'
    r = _git(['merge-base', '--is-ancestor', sha, 'refs/heads/' + branch], repo)
    return r.returncode == 0, '%s уже в %s' % (sha[:7], branch)


def tagged(cwd, task_id):
    """(применено?, пояснение): на текущем коммите main уже есть тег релиза этой задачи."""
    tags = [x for x in _git(['tag', '--points-at', 'HEAD'], cwd).stdout.split() if x.startswith('release/')
            and x.endswith('-' + task_id.lower())]
    return bool(tags), ('тег %s уже стоит' % tags[0]) if tags else 'тега нет'


def merge_into(cwd, source, message):
    """Слить source в ветку, выгруженную в cwd. Конфликт — слияние отменяется."""
    dirty = _git(['status', '--porcelain', '--untracked-files=no'], cwd).stdout.strip()
    if dirty:
        return False, 'в рабочей копии %s есть незакоммиченные изменения' % cwd
    r = _git(['merge', '--no-ff', source, '-m', message], cwd)
    if r.returncode != 0:
        if 'Already up to date' in (r.stdout + r.stderr) or 'Уже актуально' in (r.stdout + r.stderr):
            return True, 'уже актуально'
        _git(['merge', '--abort'], cwd)
        return False, 'слияние %s не удалось: %s' % (source, (r.stderr or r.stdout).strip()[-300:])
    return True, 'слито: ' + _git(['rev-parse', '--short', 'HEAD'], cwd).stdout.strip()


MERGES = ('merge_to_base', 'deploy_test', 'deploy_main', 'release_tag')


def run(check, ctx, evidence=''):
    """(ok, detail) тяжёлой проверки для пункта DoD."""
    repo, b, t = ctx.repo, ctx.branches, ctx.t
    key = t.get('project') or ''

    def pipeline_ci(cwd, branch, scope):
        # Fail-closed (TT3-010): текст агента не заменяет машинную проверку. Нет шагов — прогон not_configured
        # и отказ с подсказкой, что настроить; evidence в решение не входит.
        r = ci.run_pipeline(key, ctx.project, cwd, branch, scope, ref=t.get('ref'), trigger=check, branches=b)
        return ci.green(r)
    def once(name, target, sha, act, applied):
        # Повторяемое действие — с operation_id: второй раз не выполняется (TT3-017).
        import operation_ids as oi
        return oi.guarded(oi.heavy_id(name, t.get('ref'), target, sha), act, applied,
                          meta=dict(check=name, ref=t.get('ref'), target=target, sha=sha))

    def environment(env, cwd):
        # Команда развёртывания из projects.json — не доверенная: её правят и агенты. Выполняется
        # только команда, которую владелец одобрил по отпечатку (release_policy.environment_ok).
        import release_policy as rp, workflow as w
        d = (ci.config(ctx.project).get('deploy') or {}).get(env) or {}
        if not d.get('cmd'):
            ok, info = ci.deploy(ctx.project, env, cwd, b)
            return (True if ok is None else ok), info
        ok, why = rp.environment_ok(w.state(), key, env, d)
        if not ok:
            return False, why
        sha = ci.head_sha(cwd)

        def act():
            ok, info = ci.deploy(ctx.project, env, cwd, b)
            return (True if ok is None else ok), info
        return once('env_' + env, env, sha, act, None)       # результат развёртывания по Git не проверить

    try:
        if check in MERGES or check.startswith('env_'):
            import release_policy as rp, workflow as w
            why = rp.kill_reason(w.state())
            if why:
                return False, why
        if check in ('verify', 'ci_task'):
            return pipeline_ci(ctx.worktree.get('path') or repo, ctx.branch, 'task')
        if check == 'verify_base':
            return pipeline_ci(repo, b['base'], 'test')
        if check == 'merge_to_base':
            src = ci.head_sha(repo, 'refs/heads/' + ctx.branch) if ctx.branch else ''
            return once(check, b['base'], src,
                        lambda: autocheck.merge(repo, ctx.branch, b['base'], 'Слияние %s: %s (%s)' % (ctx.branch, t['title'], t['id'])),
                        lambda: merged(repo, src, b['base']))
        if check == 'deploy_test':
            # Догоняющее слияние: артефакты, закоммиченные в ветку задачи после ревью, — тоже в базовую.
            if ctx.branch and _git(['rev-list', '--count', '%s..%s' % (b['base'], ctx.branch)], repo).stdout.strip() not in ('', '0'):
                ok, info = autocheck.merge(repo, ctx.branch, b['base'], 'Слияние %s перед деплоем в тест (%s)' % (ctx.branch, t['id']))
                if not ok:
                    return False, info
            wt = branch_worktree(repo, b['test'], b['main'])
            src = ci.head_sha(repo, 'refs/heads/' + b['base'])
            return once(check, b['test'], src, lambda: merge_into(wt, src or b['base'], 'Деплой в %s: %s (%s)' % (b['test'], t['title'], t['id'])),
                        lambda: merged(repo, src, b['test']))
        if check in ('ci_test', 'verify_test'):
            return pipeline_ci(branch_worktree(repo, b['test'], b['main']), b['test'], 'test')
        if check == 'env_test':
            return environment('test', branch_worktree(repo, b['test'], b['main']))
        if check == 'deploy_main':
            # Проверка политики — здесь, перед самим слиянием (TT3-014): DoR мог пройти раньше.
            import release_policy as rp, workflow as w
            head = ci.head_sha(repo, 'refs/heads/' + b['test'])
            ok, why = rp.release_ok(w.state(), t.get('pipeline'), t.get('ref'), key, b['main'], head, w.now())
            if not ok:
                return False, 'релиз в %s запрещён политикой: %s' % (b['main'], why)
            wt = branch_worktree(repo, b['main'])

            def release():
                note = reapply(wt, t['id'])
                # Сливается одобренный коммит, а не имя ветки: ветка могла уйти вперёд после проверки.
                ok, info = merge_into(wt, head, 'Деплой в %s: %s (%s, %s)' % (b['main'], t['title'], t['id'], head[:7]))
                return ok, (note + '; ' + info) if note else info
            # Сбой после слияния, до записи статуса (AC-43): повтор найдёт коммит в main и не сольёт второй раз.
            return once(check, b['main'], head, release, lambda: merged(repo, head, b['main']))
        if check in ('ci_main', 'verify_main'):
            wt = branch_worktree(repo, b['main'])
            ok, info = pipeline_ci(wt, b['main'], 'main')
            if not ok:
                head = _git(['log', '-1', '--format=%H%x1f%P%x1f%s'], wt).stdout.strip().split('\x1f')
                if len(head) == 3 and len(head[1].split()) == 2 and t['id'] in head[2]:
                    rok, rinfo = ci.rollback(wt, head[0])
                    info += '; ' + rinfo
            return ok, info
        if check == 'env_main':
            return environment('main', branch_worktree(repo, b['main']))
        if check == 'release_tag':
            if not ci.config(ctx.project).get('tag', True):
                return True, 'тег релиза у проекта выключен'
            wt = branch_worktree(repo, b['main'])
            main = ci.head_sha(wt)
            return once(check, b['main'], main, lambda: ci.tag_release(wt, t['id'], t['title']), lambda: tagged(wt, t['id']))
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:300]
    return False, 'неизвестная проверка ' + check
