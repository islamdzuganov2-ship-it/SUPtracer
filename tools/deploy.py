"""Тяжёлые проверки конвейера: тесты проекта и слияния веток (деплой).

Идут фоновым заданием, не под блокировкой задач. Слияния — только здесь, сервером,
по пройденному DoD: агенты не сливают ветки сами.

- merge_to_base — ветка задачи в базовую ветку проекта (основная рабочая копия);
- deploy_test   — базовая ветка в тестовую (своя рабочая копия <repo>.worktrees/deploy-<ветка>);
- deploy_main   — тестовая ветка в main (так же);
- verify*       — команда verify проекта в рабочей копии нужной ветки. Без verify у проекта
  пункт засчитывается только с доказательством ручной проверки от агента.
"""
import re
import subprocess
from pathlib import Path

import autocheck

VERIFY_TIMEOUT = 900


def _git(args, cwd, timeout=120):
    return subprocess.run(['git', '-c', 'core.quotepath=off', *args], cwd=cwd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


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


def run(check, ctx, evidence=''):
    """(ok, detail) тяжёлой проверки для пункта DoD."""
    repo, b, t = ctx.repo, ctx.branches, ctx.t
    cmd = ctx.project.get('verify')
    def need_verify(cwd):
        if not cmd:
            ok = len((evidence or '').strip()) >= 20
            return ok, ('команда verify у проекта не задана — засчитано по доказательству агента' if ok else
                        'у проекта нет команды verify: опишите в доказательстве, как проверено')
        return verify(cwd, cmd)
    try:
        if check == 'verify':
            return need_verify(ctx.worktree.get('path') or repo)
        if check == 'verify_base':
            return need_verify(repo)
        if check == 'merge_to_base':
            ok, info = autocheck.merge(repo, ctx.branch, b['base'], 'Слияние %s: %s (%s)' % (ctx.branch, t['title'], t['id']))
            return ok, info
        if check in ('deploy_test', 'verify_test'):
            wt = branch_worktree(repo, b['test'], b['main'])
            if check == 'verify_test':
                return need_verify(wt)
            return merge_into(wt, b['base'], 'Деплой в %s: %s (%s)' % (b['test'], t['title'], t['id']))
        if check in ('deploy_main', 'verify_main'):
            wt = branch_worktree(repo, b['main'])
            if check == 'verify_main':
                return need_verify(wt)
            return merge_into(wt, b['test'], 'Деплой в %s: %s (%s)' % (b['main'], t['title'], t['id']))
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:300]
    return False, 'неизвестная проверка ' + check
