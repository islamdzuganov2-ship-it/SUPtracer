"""Баланс Claude/Codex и mono-failover (FR-015, FR-016; TTU-004…010).

Источник истины о провайдере один — `workflow.json`: `provider_limits` (отказы с временем
сброса) и сами запуски (успешные сдачи этапов). Здесь только чистые функции поверх них
(TTU-004): второго хранилища состояния провайдера нет. Пишется лишь история режима
(`provider_modes`) и журнал решений (`provider_decisions`) — это следы, а не состояние.

Здоровье провайдера:
  rate_limited — подтверждённый отказ, сброс в будущем;
  unavailable  — CLI не найден;
  degraded     — лимит прошёл, восстановление не подтверждено: нужно 2 успеха с разрывом
                 ≥60 с и ≥180 с с момента сброса. До того — не больше одной пробной сессии
                 (в режиме balanced), чтобы не было ping-pong;
  ready        — работает.
Режим: balanced (оба ready), mono_claude / mono_codex, paused_all (никто не может работать),
degraded_unknown (нет ни одного ready, но кто-то восстанавливается).

Выбор ИИ для роли — в два шага:
  1) жёсткий фильтр: закрепление владельцем, eligible роли, здоровье, независимость ревью;
  2) среди пригодных — дефицит доли принятой работы (цель 50/50) плюс бонус предпочтения роли
     (`affinity_bonus`, 0.10: предпочтительный ИИ роли уступает, только когда второй отстал
     ниже 45 %). Порядок задач (finish-first) баланс не меняет — он выбирает только ИИ.
Режим `balance_mode`: prefer — прежний порядок таблицы роли (по умолчанию), баланс считается
рядом в shadow; balanced — выбирает баланс. Включает и задаёт доли только владелец.

Независимость ревью (TTU-010) — защита, а не функция, действует в обоих режимах: ревьюер
и Решала сложной (класс 3) или рисковой задачи не назначаются тому ИИ, который делал работу.
Независимого нет — запуск ждёт его, а не уходит автору.
"""
from datetime import datetime, timedelta, timezone

PROVIDERS = ('claude', 'codex')
MODES = ('prefer', 'balanced')
DEFAULTS = dict(balance_mode='prefer', target_shares={'claude': 0.5, 'codex': 0.5}, affinity_bonus=0.10, role_affinity={})
WINDOWS = (7, 30)
MIN_EVENTS, MIN_UNITS, TOLERANCE = 20, 30, 0.05
OK_NEEDED, OK_GAP, COOLDOWN = 2, timedelta(seconds=60), timedelta(seconds=180)
KEEP = 300
STRICT_RISK = ('high', 'critical')


def _w():
    import workflow
    return workflow


def _t(v):
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def conf(s):
    st = (s or {}).get('settings') or {}
    out = {k: st.get(k, v) for k, v in DEFAULTS.items()}
    if out['balance_mode'] not in MODES:
        out['balance_mode'] = 'prefer'
    return out


def agent_of(run):
    return run.get('agent') or (run.get('launch') or {}).get('agent')


# ---------------------------------------------------------------- здоровье и режим (FR-016)

def ok_times(s, agent, since):
    """Подтверждённые успехи провайдера после момента since: принятые сдачи этапов и успехи из журнала сессий."""
    entry = (s.get('provider_limits') or {}).get(agent) or {}
    out = [_t(x) for x in entry.get('oks') or []]
    for r in s.get('runs') or []:
        if agent_of(r) == agent and r.get('status') == 'done' and r.get('handoff') is not None:
            out.append(_t(r.get('finished_at')))
    return sorted({t for t in out if t and t > since})


def health(s, agent, at, cli=None):
    """provider_health (ТЗ §5.6): state, observed_at, resets_at, source, confidence, last_success_at."""
    entry = (s.get('provider_limits') or {}).get(agent)
    base = dict(provider=agent, observed_at=at.isoformat(), quota_kind=(entry or {}).get('kind') or None,
                resets_at=None, source='workflow.provider_limits', confidence='high', last_success_at=None)
    until = _t((entry or {}).get('until'))
    if entry and until and until > at:
        return dict(base, state='rate_limited', resets_at=until.isoformat(),
                    confidence='high' if entry.get('kind') else 'estimated',
                    note='время сброса из журнала сессии' if entry.get('kind') else 'время сброса неизвестно — пауза по умолчанию')
    if cli is not None and not cli(agent):
        return dict(base, state='unavailable', source='launcher.find_cli', note='CLI не найден')
    if entry and until:
        oks = ok_times(s, agent, until)
        last = oks[-1].isoformat() if oks else None
        confirmed = len(oks) >= OK_NEEDED and oks[-1] - oks[0] >= OK_GAP and at - until >= COOLDOWN
        if not confirmed:
            return dict(base, state='degraded', last_success_at=last, recovered_at=until.isoformat(), oks=len(oks),
                        note='лимит прошёл, восстановление не подтверждено (%d из %d успехов, пауза %d с)'
                             % (len(oks), OK_NEEDED, COOLDOWN.seconds))
        return dict(base, state='ready', last_success_at=last, confirmed=True)
    return dict(base, state='ready')


def mode(s, at, cli=None):
    hs = {a: health(s, a, at, cli)['state'] for a in PROVIDERS}
    ready = [a for a in PROVIDERS if hs[a] == 'ready']
    if len(ready) == 2:
        m = 'balanced'
    elif ready:
        m = 'mono_' + ready[0]
    elif any(v == 'degraded' for v in hs.values()):
        m = 'degraded_unknown'
    else:
        m = 'paused_all'
    return m, hs


def open_sessions(s, agent):
    return sum(1 for r in s.get('runs') or [] if agent_of(r) == agent and r.get('status') in ('queued', 'running')
               and r.get('launched_at') and not r.get('paused'))


def probe_busy(s, agent, at, cli=None):
    """degraded: одна пробная сессия, пока восстановление не подтверждено."""
    return health(s, agent, at, cli)['state'] == 'degraded' and open_sessions(s, agent) >= 1


def sync(s, at, cli=None):
    """Под общей блокировкой: записать смену режима; подтверждённое восстановление снимает запись отказа."""
    m, hs = mode(s, at, cli)
    for a in PROVIDERS:
        entry = (s.get('provider_limits') or {}).get(a)
        if entry and health(s, a, at, cli).get('confirmed'):
            del s['provider_limits'][a]
    hist = s.setdefault('provider_modes', [])
    if not hist or hist[-1]['mode'] != m:
        hist.append(dict(mode=m, at=at.isoformat(), states=hs))
        del hist[:-200]
        return True
    return False


def mode_at(s, t):
    """Режим в момент t по истории; до первой записи — None (история не велась)."""
    cur = None
    for h in s.get('provider_modes') or []:
        if _t(h['at']) and _t(h['at']) <= t:
            cur = h['mode']
        else:
            break
    return cur


# ---------------------------------------------------------------- принятая работа (TTU-006)

def backlog():
    """Задачи бэклога по ref — для класса сложности; дешевле полного снимка доски."""
    out = {}
    for key, p in ((_w().read('projects.json', {}) or {}).get('projects') or {}).items():
        for t in p.get('backlog') or []:
            if t.get('id'):
                out['%s/%s' % (key, t['id'])] = dict(t, ref='%s/%s' % (key, t['id']), project=key)
    return out


def units(s, tasks, at, days):
    """eligible_accepted_work_units за окно: только принятые сдачи этапов внешних ИИ,
    только в периоды balanced (или до начала истории режимов — тогда provisional)."""
    since = at - timedelta(days=days)
    byref = {t['ref']: t for t in tasks} if isinstance(tasks, list) else (tasks or {})
    w = _w(); out = {a: dict(units=0, events=0) for a in PROVIDERS}; provisional = False; skipped_mono = 0
    for r in s.get('runs') or []:
        a = agent_of(r); fin = _t(r.get('finished_at'))
        if a not in out or r.get('status') != 'done' or r.get('handoff') is None or not r.get('role') or not fin or fin < since:
            continue
        m = mode_at(s, fin)
        if m is None:
            provisional = True
        elif m != 'balanced':
            skipped_mono += 1; continue
        t = byref.get(r['ref']) or {}
        tier = w.cx.assess(t)['tier'] if t else 2
        out[a]['units'] += 1 * tier                      # role_weight = 1 (provisional: истории для калибровки нет)
        out[a]['events'] += 1
    total_u = sum(v['units'] for v in out.values()); total_e = sum(v['events'] for v in out.values())
    for a in out:
        out[a]['share'] = round(out[a]['units'] / total_u, 3) if total_u else None
    status = ('insufficient_sample' if total_e < MIN_EVENTS or total_u < MIN_UNITS else
              'in_tolerance' if all(abs(out[a]['share'] - 0.5) <= TOLERANCE for a in out) else 'skewed')
    return dict(days=days, providers=out, units=total_u, events=total_e, status=status, provisional=provisional,
                skipped_mono=skipped_mono, weights='role_complexity_v1 (role_weight=1, provisional)')


# ---------------------------------------------------------------- выбор ИИ (TTU-005, TTU-007, TTU-010)

def strict(t):
    w = _w()
    import adaptive_pipeline as ap
    return w.cx.assess(t)['tier'] == 3 or (t or {}).get('risk') in STRICT_RISK or ap.applied((t or {}).get('pipeline')) == 'CRITICAL'


def author(t, role):
    """ИИ, чью работу проверяет роль с перекрёстной проверкой (ревьюер — разработчика)."""
    import pipeline as P
    cross = (P.ROUTING.get(role) or {}).get('cross')
    if not cross:
        return None
    return next((e.get('agent') for k, e in ((t.get('pipeline') or {}).get('roles') or {}).items()
                 if k.endswith('/' + cross) and e.get('agent')), None)


def eligible(s, t, role, at, exclude=(), cli=None):
    """(пригодные ИИ, {ИИ: причина исключения}, policy_reasons) — шаг 1, детерминированно."""
    w = _w(); st = w.settings(s); c = conf(s)
    excluded = {}; policy = {}
    pin = (st.get('roles') or {}).get(role)
    aff = (c['role_affinity'] or {}).get(role) or {}
    dev = author(t, role) if role else None
    for a in PROVIDERS:
        if aff.get('eligible') and a not in aff['eligible']:
            policy[a] = 'не входит в eligible роли (role_affinity)'
        elif dev and a == dev and strict(t):
            # Инвариант, а не предпочтение (AG-REV-01, A3-05): закрепление роли владельцем его не отменяет.
            policy[a] = 'независимость: этот ИИ выполнял проверяемую работу (сложная или рисковая задача)' + (
                '; закрепление роли за ним не действует для проверки собственной работы' if pin == a else '')
        elif a in exclude:
            policy[a] = 'исключён вызывающим (источник передачи или эскалации)'
    for a in PROVIDERS:
        if a in policy:
            excluded[a] = policy[a]; continue
        h = health(s, a, at, cli)
        if h['state'] in ('rate_limited', 'unavailable'):
            excluded[a] = '%s%s' % (h['state'], (' до ' + h['resets_at'][:16]) if h.get('resets_at') else '')
        elif c['balance_mode'] == 'balanced' and probe_busy(s, a, at, cli):
            excluded[a] = 'degraded: пробная сессия уже открыта'
    return [a for a in PROVIDERS if a not in excluded], excluded, policy


def legacy_order(s, t, role):
    import orchestra
    return orchestra.role_order(s, t, role) if role else list(PROVIDERS)


def scores(s, t, role, cands, at, share):
    import pipeline as P
    c = conf(s); tgt = c['target_shares']
    pref = (P.ROUTING.get(role) or {}).get('prefer', PROVIDERS)[0] if role else None
    dev = author(t, role) if role else None
    out = {}
    for a in cands:
        cur = share.get(a)
        debt = (tgt.get(a, 0.5) - (cur if cur is not None else tgt.get(a, 0.5)))
        out[a] = round(debt + (c['affinity_bonus'] if a == pref else 0) - (1.0 if a == dev else 0), 4)
    return out


def choose(s, t, role, at, exclude=(), cli=None, record=True, run_id=None):
    """provider_decision: selected (или None, если ждать), eligible, excluded_reasons, shadow, доли до выбора."""
    c = conf(s)
    cands, excluded, policy = eligible(s, t, role, at, exclude, cli)
    m, hs = mode(s, at, cli)
    rep = units(s, backlog(), at, 30)
    share = {a: rep['providers'][a]['share'] for a in PROVIDERS}
    order = legacy_order(s, t, role)
    legacy = next((a for a in order if a in cands), None)
    sc = scores(s, t, role, cands, at, share)
    pin = (_w().settings(s).get('roles') or {}).get(role)
    # Закрепление владельцем сильнее баланса (TC-38), но не здоровья: закреплённый на лимите — роль второму.
    balanced = pin if pin in cands else max(cands, key=lambda a: (sc[a], -order.index(a) if a in order else 0)) if cands else None
    selected = balanced if c['balance_mode'] == 'balanced' else legacy
    waiting = None
    if not selected:
        # Все пригодные по политике на лимите — ждать первого по порядку (сессия встанет до сброса).
        fallback = [a for a in order if a not in policy]
        selected = fallback[0] if fallback else None
        waiting = 'нет здорового пригодного ИИ: запуск ждёт %s' % (selected or 'изменения политики')
    d = dict(task_ref=(t or {}).get('ref'), role=role, selected=selected, eligible=cands, excluded_reasons=excluded,
             mode=m, balance_mode=c['balance_mode'], shadow_selected=legacy if c['balance_mode'] == 'balanced' else balanced,
             balanced_share_before=share, target=c['target_shares'], debt=sc, quality_estimate=None, cost_estimate=None,
             quota_state=hs, sample=rep['status'], waiting=waiting,
             policy_constrained=bool(pin in cands and len(cands) > 1) or any('eligible' in v for v in policy.values()),
             policy_version=((s.get('policy') or {}).get('version')) or 0, at=at.isoformat(), run_id=run_id)
    d['reason'] = waiting or ('баланс: дефицит доли и предпочтение роли' if c['balance_mode'] == 'balanced'
                              else 'порядок роли (баланс в shadow: выбрал бы %s)' % d['shadow_selected'])
    if record:
        log = s.setdefault('provider_decisions', []); log.append(d); del log[:-KEEP]
    return d


# ---------------------------------------------------------------- отчёт

def report(s, tasks, at, cli=None):
    c = conf(s); m, hs = mode(s, at, cli)
    import pipeline as P
    pins = (_w().settings(s).get('roles') or {})
    constrained = [dict(role=r, pinned=a, note='роль закреплена владельцем: 50/50 по ней недостижимо') for r, a in pins.items() if r in P.ROUTING]
    dec = s.get('provider_decisions') or []
    return dict(mode=m, health={a: health(s, a, at, cli) for a in PROVIDERS}, balance_mode=c['balance_mode'],
                target_shares=c['target_shares'], windows={d: units(s, tasks, at, d) for d in WINDOWS},
                policy_constrained=constrained, mode_history=(s.get('provider_modes') or [])[-20:],
                decisions=dec[-20:], shadow_differs=sum(1 for d in dec if d.get('shadow_selected') not in (None, d.get('selected'))),
                rule='Доли считаются по принятым этапам в периоды balanced, не по токенам и не по запросам. '
                     'Меньше %d событий или %d единиц — insufficient_sample.' % (MIN_EVENTS, MIN_UNITS))
