"""Политика выпуска и полномочий владельца (TT3-014, TT3-016, TTU-018; ТЗ TT-U-001 FR-003).

Что разрешает слияние в локальный main — только одно из двух подписанных решений владельца:

  * одобрение релиза — конкретной задачи (ref) на конкретном коммите тестовой ветки (sha).
    Тестовая ветка ушла вперёд — одобрение больше не действует: владелец видел другое состояние;
  * грант полной автономии — профиль `local_release`: какие проекты, какие ветки, срок,
    только локальный main, внешний push и деплой запрещены. Выдаётся один раз на профиль,
    когда владелец включает полную автономию на доске; выключение автономии отзывает грант.

Аварийная остановка сильнее любого разрешения. Отсутствие решения — явная блокировка
с причиной, а не повторный вопрос в цикле.

Состояние живёт в `workflow.json` → `policy` (версия, журнал изменений, грант, остановка) —
второго хранилища нет. Подпись — `owner_auth`.
"""
import hashlib
import json
import secrets
from datetime import timedelta

import owner_auth as oa

GRANT_DAYS = 30
LOG_KEEP = 200
PROFILE = 'local_release'
REVOCATION = ('revoked_at', 'revoked_why')
BLOCKING = ('blocker', 'critical', 'major')     # открытый дефект такого уровня блокирует main (FR-002)    # отзыв дописывается к подписанному гранту


def policy(s):
    return s.setdefault('policy', dict(version=0, log=[]))


def view(s):
    """Политика для доски и брифинга: без подписей."""
    p = (s or {}).get('policy') or {}
    g = p.get('grant')
    return dict(version=p.get('version', 0), kill_switch=p.get('kill_switch'),
                grant={k: v for k, v in g.items() if k != 'sig'} if g else None,
                log=[{k: v for k, v in e.items()} for e in (p.get('log') or [])[-20:]])


# ---------------------------------------------------------------- аварийная остановка

def killed(s):
    k = ((s or {}).get('policy') or {}).get('kill_switch') or {}
    return k if k.get('on') else None


def kill_reason(s):
    k = killed(s)
    return ('Аварийная остановка с %s UTC (%s): новые действия запрещены, снять может только владелец на доске'
            % ((k.get('at') or '')[:16].replace('T', ' '), k.get('reason') or 'без причины')) if k else None


# ---------------------------------------------------------------- грант автономии

def issue_grant(at, projects='*', branches=('main',), days=GRANT_DAYS, by='владелец'):
    return oa.sign(dict(kind='autonomy_grant', id='G-' + at.strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(3),
                        profile=PROFILE, projects=projects if projects == '*' else sorted(projects),
                        branches=sorted(branches), release_target='local_main', external_deploy_allowed=False,
                        issued_at=at.isoformat(), expires_at=(at + timedelta(days=days)).isoformat(), by=by))


def grant_check(g, project, branch, at):
    """(ok, причина) — покрывает ли действующий грант слияние в branch проекта project."""
    if not g:
        return False, 'гранта полной автономии нет — нужно одобрение владельца'
    if not oa.verify({k: v for k, v in g.items() if k not in REVOCATION}):
        return False, 'грант не подписан ключом владельца — недействителен'
    if g.get('kind') != 'autonomy_grant' or g.get('profile') != PROFILE or g.get('release_target') != 'local_main':
        return False, 'грант другого профиля'
    if g.get('revoked_at'):
        return False, 'грант отозван %s' % g['revoked_at'][:16]
    if (g.get('expires_at') or '') <= at.isoformat():
        return False, 'грант истёк %s — включите полную автономию заново' % (g.get('expires_at') or '')[:16]
    if g.get('projects') != '*' and project not in (g.get('projects') or []):
        return False, 'грант не покрывает проект %s' % project
    if branch not in (g.get('branches') or []):
        return False, 'грант не покрывает ветку %s' % branch
    return True, 'грант %s до %s' % (g['id'], g['expires_at'][:10])


def active_grant(s, project, branch, at):
    g = ((s or {}).get('policy') or {}).get('grant')
    ok, why = grant_check(g, project, branch, at)
    return (g if ok else None), why


def revoke_grant(s, at, why):
    p = policy(s)
    g = p.get('grant')
    if g and not g.get('revoked_at'):
        # Отзыв не требует подписи: он только сужает права. Подпись исходного гранта остаётся,
        # а revoked_at вне подписанного содержимого проверяется отдельно.
        p['grant'] = dict(g, revoked_at=at.isoformat(), revoked_why=why)


def external_push_allowed(s, project, at):
    """Внешний push / деплой (AC-42): грант по умолчанию его не даёт, поле в гранте всегда False."""
    g, _ = active_grant(s, project, 'main', at)
    return bool(g and g.get('external_deploy_allowed') is True)


# ---------------------------------------------------------------- одобрение релиза

def approval(ref, sha, at, by='владелец', grant=None):
    if not sha:
        raise ValueError('Не найден коммит тестовой ветки: одобрять нечего')
    return oa.sign(dict(kind='release_approval', ref=ref, sha=sha, at=at.isoformat(), by=by, grant=grant,
                        nonce=secrets.token_hex(8)))


def release_ok(s, pipe, ref, project, branch, sha, at):
    """(ok, причина) — можно ли сейчас слить тестовую ветку на коммите sha в main задачи ref."""
    why = kill_reason(s)
    if why:
        return False, why
    a = ((pipe or {}).get('approvals') or {}).get('release')
    if not a:
        return False, 'ждёт «Одобрить релиз» владельца'
    if not oa.verify(a) or a.get('kind') != 'release_approval':
        return False, 'одобрение не подписано ключом владельца (записано вручную или до TT-104) — нужно одобрение на доске'
    if a.get('ref') != ref:
        return False, 'одобрение выдано задаче %s, а не %s' % (a.get('ref'), ref)
    if not sha:
        return False, 'не найден коммит тестовой ветки'
    bad = [d['id'] for d in (pipe or {}).get('defects') or []
           if d.get('status') == 'open' and d.get('severity') in BLOCKING]
    if bad:
        return False, 'открыты дефекты %s: релиз с ними запрещён даже с одобрением' % ', '.join(bad)
    if a.get('sha') != sha:
        return False, 'тестовая ветка изменилась после одобрения (%s → %s) — нужно новое одобрение' % (
            (a.get('sha') or '')[:7], sha[:7])
    if a.get('grant'):
        g = ((s or {}).get('policy') or {}).get('grant') or {}
        if g.get('id') != a['grant']:
            return False, 'грант %s, по которому одобрен релиз, заменён или удалён' % a['grant']
        ok, gwhy = grant_check(g, project, branch, at)
        if not ok:
            return False, gwhy
    return True, 'одобрено %s %s на %s' % (a.get('by'), (a.get('at') or '')[:16], sha[:7])


# ---------------------------------------------------------------- внешние окружения

def env_key(project, env):
    return '%s/%s' % (project, env)


def env_fingerprint(d):
    """Отпечаток команды развёртывания: команда, проверка работоспособности, тайм-аут."""
    body = json.dumps(dict(cmd=d.get('cmd'), health=d.get('health'), timeout=d.get('timeout')),
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]


def environment_ok(s, project, env, d):
    """(ok, причина) — одобрил ли владелец именно эту команду развёртывания окружения env.
    Команды из projects.json не доверенные (FR-003): их правят и агенты. Изменилась — снова к владельцу."""
    why = kill_reason(s)
    if why:
        return False, why
    fp = env_fingerprint(d)
    approved = (((s or {}).get('settings') or {}).get('environments') or {}).get(env_key(project, env))
    if approved == fp:
        return True, 'команда окружения %s одобрена владельцем (%s)' % (env, fp)
    return False, ('команда развёртывания «%s» %s — нужно одобрение владельца на доске (отпечаток %s)'
                   % (env, 'изменилась после одобрения' if approved else 'не одобрена владельцем', fp))


# ---------------------------------------------------------------- версия политики

def preview(current, changes):
    """Что изменится — без применения (TTU-018)."""
    return [dict(key=k, before=current.get(k), after=v) for k, v in sorted(changes.items()) if current.get(k) != v]


def record(s, at, origin, diff, what='settings'):
    """Новая версия политики и запись журнала. Хеш — от итоговых настроек и гранта."""
    p = policy(s)
    p['version'] = int(p.get('version') or 0) + 1
    body = json.dumps(dict(settings=s.get('settings') or {}, grant=p.get('grant'), kill=p.get('kill_switch')),
                      ensure_ascii=False, sort_keys=True, default=str)
    p['hash'] = hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]
    p['log'] = (p.get('log') or [])[-(LOG_KEEP - 1):] + [dict(version=p['version'], at=at.isoformat(), origin=origin,
                                                              what=what, diff=diff, hash=p['hash'])]
    return p['version']
