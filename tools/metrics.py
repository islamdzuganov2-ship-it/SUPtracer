"""Метрики работ по спринтам — факт из журналов сессий, git и конвейера, без ИИ.

Спринт — окно лимита токенов: от восстановления лимита до его сброса. Границы берутся
из журналов: Claude пишет время сброса при отказе по лимиту, Codex — с каждым отчётом
о расходе. Где окна неизвестны, спринт — пятичасовой блок от первой активности
(так считает окна сам провайдер). Окна разных ИИ, наложившиеся друг на друга, — один спринт.

По проекту и спринту: токены и деньги Claude и Codex раздельно, коммиты, фичи (закрытые
задачи), документы — сколько и какие, строки кода и тестов. По проекту целиком: уровни
тестов и итоги проверок, правки ТЗ после передачи в разработку, изменения кода по итогам
тестирования, дефекты и способы исправления, качество ролей и ИИ по передачам и переделкам.

Каждая цифра — подсчёт записанных событий. Файл автоматический: data/metrics.json.
py tools/metrics.py
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import filekinds as fk  # noqa: E402
import role_quality  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('TASKTREKER_DATA') or ROOT / 'data')
WINDOW = timedelta(hours=5)
PROVIDERS = ('claude', 'codex')
ANALYSIS_ROLES = ('product_owner', 'system_analyst', 'solution_architect', 'designer', 'marketer')   # правки после анализа — поздние изменения ТЗ


def load(name, default):
    try:
        with open(DATA / name, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def stamp(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        v = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def iso(dt):
    return dt.isoformat(timespec='minutes') if dt else None


# ---------------------------------------------------------------- спринты

def windows_from(usage):
    """Известные окна лимита: [(начало, конец, ИИ)]."""
    out = []
    for name, prov in (usage.get('providers') or {}).items():
        for e in prov.get('limits') or []:
            end = stamp(e.get('resets_at'))
            if not end or e.get('kind') == 'seven_day':
                continue
            length = timedelta(minutes=e.get('window_minutes') or 300)
            out.append((end - length, end, name))
    return sorted(out)


def limit_hits(usage):
    """Отказы по лимиту: [(когда, ИИ, вид окна, проект)]."""
    out = []
    for name, prov in (usage.get('providers') or {}).items():
        for e in prov.get('limits') or []:
            at = stamp(e.get('at'))
            if at and e.get('kind') != 'window':
                out.append((at, name, e.get('kind') or 'five_hour', e.get('project') or ''))
    return sorted(out)


def activity_hours(usage):
    hours = set()
    for prov in (usage.get('providers') or {}).values():
        for p in (prov.get('projects') or {}).values():
            for h in p.get('by_hour') or {}:
                t = stamp(h + ':00:00+00:00')
                if t:
                    hours.add(t)
    return sorted(hours)


def build_sprints(usage):
    """Окна лимита и блоки активности → непересекающиеся спринты по времени."""
    spans = [[a, b, {who}, 'limit'] for a, b, who in windows_from(usage)]
    covered = lambda t: any(a <= t < b for a, b, _, _ in spans)
    starts = sorted(a for a, *_ in spans)
    for h in activity_hours(usage):
        if covered(h) or covered(h + timedelta(minutes=59)):
            continue
        end = h + WINDOW
        nxt = next((a for a in starts if a > h), None)
        if nxt and nxt < end:
            end = nxt
        spans.append([h, end, set(), 'activity']); starts.append(h); starts.sort()
    spans.sort(key=lambda x: x[0])
    # Окна Claude и Codex сдвинуты друг относительно друга: склейка перекрывающихся окон
    # тянула цепочку в один «спринт» на сутки и больше. Перекрытое окно начинается там,
    # где кончилось предыдущее, — спринт не длиннее одного окна; хвост короче 15 минут
    # отходит предыдущему спринту.
    merged = []
    for a, b, who, basis in spans:
        if merged and a < merged[-1][1]:
            m = merged[-1]; m[2] |= who
            if b - m[1] < timedelta(minutes=15):
                m[1] = max(m[1], b); continue
            a = m[1]
        merged.append([a, b, set(who), basis])
    hits = limit_hits(usage)
    out = []
    for i, (a, b, who, basis) in enumerate(merged):
        inside = [h for h in hits if a <= h[0] < b]
        out.append(dict(id='S%d' % (i + 1), start=iso(a), end=iso(b), basis=basis,
                        windows=sorted(who), limit_hit=sorted({h[1] for h in inside}),
                        weekly=any(h[2] == 'seven_day' for h in inside)))
    return out


class Index:
    """Поиск спринта по времени."""
    def __init__(self, sprints):
        self.items = [(stamp(s['start']), stamp(s['end']), s['id']) for s in sprints]

    def find(self, t):
        t = stamp(t) if not isinstance(t, datetime) else t
        if not t:
            return None
        for a, b, sid in self.items:
            if a <= t < b:
                return sid
        return None

    def hour(self, h):
        """Час расхода: спринт, в который попадает его начало, иначе — любая минута часа (окно началось внутри часа)."""
        t = stamp(h + ':00:00+00:00')
        if not t:
            return None
        return next((sid for m in (0, 15, 30, 45, 59) for sid in [self.find(t + timedelta(minutes=m))] if sid), None)


# ---------------------------------------------------------------- git

def git(args, cwd, timeout=120):
    try:
        r = subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout)
        return r.stdout if r.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def commits(repo, since):
    """Коммиты всех веток с файлами: [{hash, at, subject, files: [(path, +, -)]}]."""
    if not repo or not (Path(repo) / '.git').exists():
        return []
    out = git(['log', '--all', '--no-merges', '--numstat', '--format=%x1e%H%x1f%aI%x1f%s', '--since=' + since], repo)
    result, seen = [], set()
    for chunk in out.split('\x1e'):
        lines = chunk.strip('\n').splitlines()
        if not lines or '\x1f' not in lines[0]:
            continue
        h, at, subject = (lines[0].split('\x1f') + ['', ''])[:3]
        if h in seen:
            continue
        seen.add(h)
        files = []
        for ln in lines[1:]:
            parts = ln.split('\t')
            if len(parts) == 3:
                add = int(parts[0]) if parts[0].isdigit() else 0
                rem = int(parts[1]) if parts[1].isdigit() else 0
                files.append((parts[2].split(' => ')[-1].strip('{}'), add, rem))
        result.append(dict(hash=h, at=at, subject=subject, files=files))
    return result


def branch_commits(repo, branch, since=None):
    """Коммиты ветки задачи после момента: [(время, [файлы])]."""
    if not repo or not branch:
        return []
    out = git(['log', '--no-merges', '--name-only', '--format=%x1e%aI', branch, *(['--since=' + since] if since else [])], repo)
    res = []
    for chunk in out.split('\x1e'):
        lines = [l for l in chunk.strip('\n').splitlines() if l.strip()]
        if lines:
            res.append((stamp(lines[0]), lines[1:]))
    return res


def test_levels(repo):
    if not repo or not (Path(repo) / '.git').exists():
        return {}
    files = [f for f in git(['ls-files'], repo).splitlines() if f.strip()]
    return dict(Counter(fk.test_level(f) for f in files if fk.classify(f) == 'test'))


# ---------------------------------------------------------------- проект

def blank_sprint(s):
    return dict(id=s['id'], start=s['start'], end=s['end'], basis=s['basis'], limit_hit=s['limit_hit'], weekly=s['weekly'],
                tokens=dict(claude=0, codex=0), usd=dict(claude=0.0, codex=0.0), requests=dict(claude=0, codex=0),
                commits=0, features=[], docs=dict(changed=0, spec=0, files=[]),
                code=dict(added=0, removed=0, files=0), tests=dict(added=0, removed=0, files=0))


def ref_pattern(task):
    keys = [task['id']] + [r for r in task.get('refs') or [] if isinstance(r, str) and r.strip()]
    return re.compile(r'(?<![\w-])(%s)(?![\w-])' % '|'.join(re.escape(k) for k in keys), re.I)


def project_metrics(key, p, usage, sprints, idx, wf, since):
    by_id = {s['id']: blank_sprint(s) for s in sprints}
    touched = set()
    for name in PROVIDERS:
        u = (((usage.get('providers') or {}).get(name) or {}).get('projects') or {}).get(key) or {}
        for h, (tok, usd, req) in (u.get('by_hour') or {}).items():
            sid = idx.hour(h)
            if sid:
                s = by_id[sid]; s['tokens'][name] += tok; s['usd'][name] = round(s['usd'][name] + usd, 4); s['requests'][name] += req
                touched.add(sid)
    repo = p.get('inspection_path') or p.get('path')
    log = commits(repo, since)
    doc_files = defaultdict(dict)
    for c in log:
        sid = idx.find(c['at'])
        if not sid:
            continue
        s = by_id[sid]; s['commits'] += 1; touched.add(sid)
        for path, add, rem in c['files']:
            kind = fk.classify(path)
            if kind in ('code', 'test'):
                box = s['code' if kind == 'code' else 'tests']
                box['added'] += add; box['removed'] += rem; box['files'] += 1
            elif kind in ('doc', 'spec'):
                d = doc_files[sid].setdefault(path, dict(path=path, kind=kind, added=0, removed=0, commits=0))
                d['added'] += add; d['removed'] += rem; d['commits'] += 1
    for sid, files in doc_files.items():
        s = by_id[sid]; rows = sorted(files.values(), key=lambda d: -(d['added'] + d['removed']))
        s['docs'] = dict(changed=len(rows), spec=sum(d['kind'] == 'spec' for d in rows), files=rows[:25])
    # Фичи: закрытые задачи — по времени закрытия, без него — по последнему коммиту с её номером.
    for t in p.get('backlog') or []:
        if t.get('status') != 'done':
            continue
        when = t.get('completed_at')
        if not when:
            pat = ref_pattern(t)
            hits = [c['at'] for c in log if pat.search(c['subject'])]
            when = max(hits) if hits else None
        sid = idx.find(when)
        if sid:
            by_id[sid]['features'].append(dict(id=t['id'], title=t.get('title', '')[:120])); touched.add(sid)
    rows = [by_id[s['id']] for s in sprints if s['id'] in touched]
    out = dict(sprints=rows, totals=totals(rows), outside_commits=sum(1 for c in log if not idx.find(c['at'])))
    out.update(quality(key, p, wf, repo))
    out['tests']['levels'] = test_levels(repo)
    out['team'] = team(key, p, wf, usage)
    return out


EXECUTOR = 'executor'     # исполнитель задач вне конвейера — тоже агент в сводке
_CX = None


def codex_sessions():
    global _CX
    if _CX is None:
        try:
            import limits
            _CX = limits.codex_index()
        except Exception:   # noqa: BLE001 — без журналов Codex токены его запусков неизвестны
            _CX = []
    return _CX


def role_title(role):
    import pipeline as P
    return 'Исполнитель (вне конвейера)' if role == EXECUTOR else P.ROLES.get(role, {}).get('title', role)


def run_sessions(r):
    out = []
    for l in [r.get('launch') or {}] + list(r.get('launches') or []):
        if l.get('session') and l['session'] not in out:
            out.append(l['session'])
    return out


def upcoming(t):
    """Роли, которым задача ещё предстоит: этапы впереди (и несданные роли текущего), ворота, Решала."""
    import pipeline as P
    pipe = t.get('pipeline')
    if not pipe:
        return {EXECUTOR: 1}, {}
    sure, maybe = Counter(), Counter()
    if pipe.get('stage') == 'done':
        return sure, maybe
    needs = pipe.get('needs')
    gates = pipe.get('gates') or {}
    for st in P.ORDER[P.ORDER.index(pipe['stage']):]:
        spec = P.STAGE[st]
        for role in spec['roles']:
            if st == pipe['stage'] and ((pipe.get('roles') or {}).get('%s/%s' % (st, role)) or {}).get('state') == 'done':
                continue
            flag = (spec.get('optional') or {}).get(role)
            if flag and needs is not None and not needs.get(flag):
                continue
            (maybe if flag and needs is None else sure)[role] += 1
        if spec.get('gate') and (gates.get(spec['gate']) or {}).get('decision') != 'approve':
            sure['teamlead'] += 1
    if pipe.get('blocked'):
        sure['resolver'] += 1
    return sure, maybe


def team(key, p, wf, usage):
    """Сводка по агентам-ролям: выполнено, токены, предстоит, возвраты и их причины."""
    sessions = {n: ((usage.get('providers') or {}).get(n) or {}).get('sessions') or {} for n in PROVIDERS}
    tasks = {key + '/' + t['id']: t for t in p.get('backlog') or []}
    cx_index = codex_sessions()
    rows = defaultdict(lambda: dict(done=set(), runs=0, failed=0, tokens=0, usd=0.0, seen=set()))
    for r in wf.get('runs') or []:
        t = tasks.get(r.get('ref'))
        if not t or (r.get('status') in ('cancelled', 'queued') and not r.get('launched_at')):
            continue                    # не открывавшийся запуск — ещё не работа агента
        role = r.get('role') or EXECUTOR
        agent = r.get('agent') or (r.get('launch') or {}).get('agent') or '?'
        x = rows[(role, agent)]
        x['runs'] += 1
        if r.get('status') == 'failed':
            x['failed'] += 1
        ev = str(r.get('evidence') or '')
        if r.get('status') == 'done' and not ev.startswith('Эскалация'):
            x['done'].add(r['ref'])
        sids = run_sessions(r)
        if agent == 'codex' and not sids and cx_index:
            # Codex назначает id сессии сам: старые запуски сопоставляются по папке и времени открытия.
            import limits
            for l in [r.get('launch') or {}] + list(r.get('launches') or []):
                sid = limits.match_codex(cx_index, l.get('cwd'), stamp(l.get('at') or r.get('launched_at')))
                if sid and sid not in sids:
                    sids.append(sid)
        for sid in sids:
            if sid in x['seen']:
                continue
            x['seen'].add(sid)
            tok, usd = (sessions.get(agent) or {}).get(sid, [0, 0])
            x['tokens'] += tok; x['usd'] += usd
    agents = [dict(role=role, title=role_title(role), agent=agent, done=len(x['done']), runs=x['runs'], failed=x['failed'],
                   tokens=x['tokens'], usd=round(x['usd'], 2), sessions=len(x['seen']))
              for (role, agent), x in sorted(rows.items())]
    roles = defaultdict(lambda: dict(upcoming=0, maybe=0, returns=0, reasons=[], dod_rejects=0, dod_top=Counter(), by=Counter()))
    for ref, t in tasks.items():
        if t.get('status') in ('done', 'cancelled'):
            continue
        sure, maybe = upcoming(t)
        for role, n in sure.items(): roles[role]['upcoming'] += n
        for role, n in maybe.items(): roles[role]['maybe'] += n
    import pipeline as P
    for ref, t in tasks.items():
        pipe = t.get('pipeline') or {}
        texts = [j['text'] for j in pipe.get('journal') or [] if j.get('kind') == 'returned']
        for h in pipe.get('history') or []:
            if h.get('kind') == 'return':
                targets = h.get('roles') or P.STAGE.get(h.get('to_stage'), {}).get('roles', [])
                reason = h.get('reason') or next((x.split(':', 1)[-1].strip() for x in texts if (h.get('to_stage') and P.STAGE[h['to_stage']]['title'] in x)), '') or 'причина не записана'
                for role in targets:
                    r = roles[role]; r['returns'] += 1; r['by'][h.get('role') or '?'] += 1
                    r['reasons'].append(dict(task=ref.split('/', 1)[1], by=role_title(h.get('role')), reason=reason[:300], at=h.get('at')))
            elif h.get('kind') in ('handoff', 'check') and not h.get('accepted') and h.get('role'):
                r = roles[h['role']]; r['dod_rejects'] += 1
                r['dod_top'].update(h.get('failed') or [])
    summary = {role: dict(title=role_title(role), upcoming=v['upcoming'], maybe=v['maybe'], returns=v['returns'],
                          returned_by={role_title(k): n for k, n in v['by'].items()},
                          reasons=sorted(v['reasons'], key=lambda x: x.get('at') or '')[-12:],
                          dod_rejects=v['dod_rejects'], dod_top=v['dod_top'].most_common(6))
               for role, v in roles.items()}
    return dict(agents=agents, roles=summary)


def merge_team(blocks):
    agents = {}
    roles = {}
    for b in blocks:
        for a in b['agents']:
            k = (a['role'], a['agent']); x = agents.setdefault(k, dict(a, done=0, runs=0, failed=0, tokens=0, usd=0.0, sessions=0))
            for f in ('done', 'runs', 'failed', 'tokens', 'sessions'):
                x[f] += a[f]
            x['usd'] = round(x['usd'] + a['usd'], 2)
        for role, v in b['roles'].items():
            x = roles.setdefault(role, dict(title=v['title'], upcoming=0, maybe=0, returns=0, returned_by=Counter(), reasons=[], dod_rejects=0, dod_top=Counter()))
            for f in ('upcoming', 'maybe', 'returns', 'dod_rejects'):
                x[f] += v[f]
            x['returned_by'].update(v['returned_by']); x['dod_top'].update(dict(v['dod_top'])); x['reasons'] += v['reasons']
    for x in roles.values():
        x['returned_by'] = dict(x['returned_by']); x['dod_top'] = x['dod_top'].most_common(6)
        x['reasons'] = sorted(x['reasons'], key=lambda r: r.get('at') or '')[-12:]
    return dict(agents=sorted(agents.values(), key=lambda a: (a['role'], a['agent'])), roles=roles)


def totals(rows):
    t = dict(sprints=len(rows), tokens=dict(claude=0, codex=0), usd=dict(claude=0.0, codex=0.0), commits=0, features=0,
             docs=0, spec_docs=0, code_added=0, code_removed=0, test_added=0, test_removed=0, limit_hits=0)
    for r in rows:
        for n in PROVIDERS:
            t['tokens'][n] += r['tokens'][n]; t['usd'][n] = round(t['usd'][n] + r['usd'][n], 2)
        t['commits'] += r['commits']; t['features'] += len(r['features']); t['docs'] += r['docs']['changed']
        t['spec_docs'] += r['docs']['spec']; t['code_added'] += r['code']['added']; t['code_removed'] += r['code']['removed']
        t['test_added'] += r['tests']['added']; t['test_removed'] += r['tests']['removed']; t['limit_hits'] += bool(r['limit_hit'])
    return t


# ---------------------------------------------------------------- качество работ

def first(history, kind, **match):
    for h in history:
        if h.get('kind') == kind and all(h.get(k) == v for k, v in match.items()):
            return stamp(h.get('at'))
    return None


def quality(key, p, wf, repo):
    tasks = [dict(t, ref=key + '/' + t['id']) for t in p.get('backlog') or []]
    runs = [r for r in wf.get('runs') or [] if str(r.get('ref', '')).startswith(key + '/')]
    spec_events, code_events = [], []
    dev_tasks = tested_tasks = 0
    defects = []
    roles = defaultdict(lambda: dict(runs=set(), handoffs=0, accepted=0, first_try=0, rejected=0, returns=0, rework_edits=0, tasks=set(), clean_tasks=set()))
    import pipeline as P
    for t in tasks:
        pipe = t.get('pipeline')
        if not pipe:
            rv = t.get('review') or {}
            if rv.get('verdict') == 'spec_change':
                spec_events.append(dict(task=t['id'], at=rv.get('at'), source='ревью: отклонение от ТЗ', files=[]))
            continue
        hist = pipe.get('history') or []
        branch = (pipe.get('worktree') or {}).get('branch')
        analysis_done = first(hist, 'stage_passed', stage='analysis')
        testing_start = first(hist, 'stage_passed', stage='review')
        if analysis_done:
            dev_tasks += 1
            for at, files in branch_commits(repo, branch, iso(analysis_done)):
                spec = [f for f in files if fk.spec_in_task(f, t['id'])]
                if spec and at and at > analysis_done:
                    spec_events.append(dict(task=t['id'], at=iso(at), source='правка артефактов анализа', files=spec[:10]))
        for h in hist:
            if h.get('kind') == 'return' and h.get('to_stage') in ('analysis', 'idea') and analysis_done:
                spec_events.append(dict(task=t['id'], at=h.get('at'), source='возврат в анализ: %s' % h.get('role'), files=[]))
        if testing_start:
            tested_tasks += 1
            for at, files in branch_commits(repo, branch, iso(testing_start)):
                code = [f for f in files if fk.classify(f) == 'code']
                if code and at and at > testing_start:
                    code_events.append(dict(task=t['id'], at=iso(at), source='код после начала тестирования', files=code[:10]))
            for h in hist:
                if h.get('kind') == 'return' and h.get('role') in ('qa', 'devops'):
                    code_events.append(dict(task=t['id'], at=h.get('at'), source='возврат %s в разработку' % h.get('role'), files=[]))
        for d in pipe.get('defects') or []:
            defects.append(dict(d, task=t['id']))
        # Роли: передачи, приём с первой попытки, возвраты к роли, правки своих артефактов после сдачи этапа.
        seen_runs = set()
        for h in hist:
            if h.get('kind') not in ('handoff', 'check') or not h.get('role'):
                continue
            k = (h['role'], h.get('agent') or '?'); r = roles[k]
            r['tasks'].add(t['id']); r['runs'].add(h.get('run'))
            if h['kind'] == 'handoff':
                r['handoffs'] += 1
                r['accepted' if h.get('accepted') else 'rejected'] += 1
                if h.get('run') not in seen_runs:
                    seen_runs.add(h.get('run'))
                    if h.get('accepted'):
                        r['first_try'] += 1
            elif not h.get('accepted'):
                r['rejected'] += 1; r['first_try'] -= 1 if h.get('run') in seen_runs else 0
        returned = defaultdict(int)
        for h in hist:
            if h.get('kind') != 'return':
                continue
            targets = h.get('roles') or P.STAGE.get(h.get('to_stage'), {}).get('roles', [])
            for role in targets:
                returned[role] += 1
        agents = {}
        for k in pipe.get('roles') or {}:
            st, role = k.split('/', 1)
            agents[role] = (pipe['roles'][k] or {}).get('agent') or '?'
        for role, n in returned.items():
            if role in agents:
                roles[(role, agents[role])]['returns'] += n
        if analysis_done:
            for at, files in branch_commits(repo, branch, iso(analysis_done)):
                if not at or at <= analysis_done:
                    continue
                for role in ANALYSIS_ROLES:
                    if role not in agents:
                        continue
                    own = [P.art_path(t['id'], a['path']) for a in P.ROLES[role]['artifacts']]
                    if any(f.startswith(o.rstrip('/')) for f in files for o in own):
                        roles[(role, agents[role])]['rework_edits'] += 1
        for role, agent in agents.items():
            r = roles[(role, agent)]; r['tasks'].add(t['id'])
            if not returned.get(role):
                r['clean_tasks'].add(t['id'])
    # Задачи вне конвейера: доработки по автопроверке и ревью, итоги автопроверок.
    rework = Counter(r.get('rework') for r in runs if r.get('rework'))
    checks = [r['autocheck'] for r in runs if r.get('autocheck')]
    for t in tasks:
        ac = t.get('auto_check') or {}
        if ac.get('run') and not any(r.get('id') == ac.get('run') and r.get('autocheck') for r in runs) and ac.get('ok') is not None:
            checks.append(dict(ok=bool(ac['ok']), at=ac.get('at') or ac.get('finished_at')))
    for r in runs:
        if r.get('rework'):
            code_events.append(dict(task=r['ref'].split('/', 1)[1], at=r.get('claimed_at'), source='доработка по %s' % (
                'автопроверке' if r['rework'] == 'autocheck' else 'ревью'), files=[]))
    legacy_dev = sum(1 for t in tasks if not t.get('pipeline') and (t.get('auto_check') or t.get('review')))
    role_rows = []
    for (role, agent), r in sorted(roles.items()):
        n = len(r['tasks'])
        if role not in P.ROLES or not n:
            continue
        role_rows.append(dict(role=role, title=P.ROLES[role]['title'], agent=agent, tasks=n, handoffs=r['handoffs'],
                              accepted=r['accepted'], rejected=r['rejected'], first_try=max(0, r['first_try']),
                              first_try_pct=round(100 * max(0, r['first_try']) / len(r['runs'])) if r['runs'] else None,
                              returns=r['returns'], rework_edits=r['rework_edits'],
                              clean_pct=round(100 * len(r['clean_tasks']) / n) if n else None))
    return dict(
        tests=dict(checks=len(checks), passed=sum(1 for c in checks if c.get('ok')), failed=sum(1 for c in checks if not c.get('ok')),
                   last=max(checks, key=lambda c: c.get('at') or '') if checks else None,
                   pipeline_checks=sum(1 for t in tasks for h in (t.get('pipeline') or {}).get('history') or [] if h.get('kind') == 'check'),
                   pipeline_failed=sum(1 for t in tasks for h in (t.get('pipeline') or {}).get('history') or [] if h.get('kind') == 'check' and not h.get('accepted')),
                   signoffs=sum(1 for t in tasks for h in (t.get('pipeline') or {}).get('history') or [] if h.get('kind') == 'handoff' and h.get('role') == 'qa' and h.get('accepted'))),
        spec_changes=dict(events=len(spec_events), tasks=len({e['task'] for e in spec_events}), base=dev_tasks + legacy_dev,
                          per_task=round(len(spec_events) / (dev_tasks + legacy_dev), 2) if dev_tasks + legacy_dev else None,
                          items=sorted(spec_events, key=lambda e: e.get('at') or '')[-30:]),
        code_after_testing=dict(events=len(code_events), tasks=len({e['task'] for e in code_events}), base=tested_tasks + legacy_dev,
                                per_task=round(len(code_events) / (tested_tasks + legacy_dev), 2) if tested_tasks + legacy_dev else None,
                                rework_runs=dict(rework), items=sorted(code_events, key=lambda e: e.get('at') or '')[-30:]),
        defects=defect_summary(defects),
        roles=role_rows,
        agents=agent_summary(role_rows, defects, runs),
        role_quality=role_quality.collect([t for t in tasks if t.get('pipeline')],
                                          [x for x in wf.get('problems') or [] if str(x.get('ref', '')).startswith(key + '/')]),
    )


def defect_summary(defects):
    fixed = [d for d in defects if d.get('status') in ('fixed', 'verified')]
    return dict(found=len(defects), fixed=len(fixed), verified=sum(d.get('status') == 'verified' for d in defects),
                open=sum(d.get('status') == 'open' for d in defects),
                by_method=dict(Counter((d.get('fix') or {}).get('method') for d in fixed)),
                test_fix_levels=dict(Counter((d.get('fix') or {}).get('test_level') for d in fixed if (d.get('fix') or {}).get('method') == 'test_fix')),
                by_level=dict(Counter(d.get('level') for d in defects)), by_severity=dict(Counter(d.get('severity') for d in defects)),
                by_finder=dict(Counter(d.get('found_by') for d in defects)),
                items=[{k: d.get(k) for k in ('task', 'id', 'title', 'severity', 'level', 'found_by', 'status', 'at')}
                       | dict(method=(d.get('fix') or {}).get('method'), test_level=(d.get('fix') or {}).get('test_level'))
                       for d in defects[-40:]])


def agent_summary(role_rows, defects, runs):
    out = {}
    for name in PROVIDERS:
        rows = [r for r in role_rows if r['agent'] == name]
        done = [r for r in runs if r.get('agent') == name and r.get('status') in ('done', 'failed')]
        out[name] = dict(handoffs=sum(r['handoffs'] for r in rows), first_try=sum(r['first_try'] for r in rows),
                         rejected=sum(r['rejected'] for r in rows), returns=sum(r['returns'] for r in rows),
                         defects_found=sum(1 for d in defects if d.get('found_agent') == name),
                         defects_fixed=sum(1 for d in defects if (d.get('fix') or {}).get('agent') == name),
                         runs=len(done), failed_runs=sum(r.get('status') == 'failed' for r in done),
                         rework_runs=sum(1 for r in runs if r.get('agent') == name and r.get('rework')))
    return out


def portfolio(projects, sprints):
    """Сводка портфеля: те же метрики, сложенные по проектам — для режима «Весь портфель»."""
    by_id = {s['id']: blank_sprint(s) for s in sprints}
    touched = set()
    for key, m in projects.items():
        for r in m['sprints']:
            s = by_id[r['id']]; touched.add(r['id'])
            for n in PROVIDERS:
                s['tokens'][n] += r['tokens'][n]; s['usd'][n] = round(s['usd'][n] + r['usd'][n], 4); s['requests'][n] += r['requests'][n]
            s['commits'] += r['commits']
            s['features'] += [dict(f, id='%s/%s' % (key, f['id'])) for f in r['features']]
            for box in ('code', 'tests'):
                for f in ('added', 'removed', 'files'):
                    s[box][f] += r[box][f]
            s['docs']['changed'] += r['docs']['changed']; s['docs']['spec'] += r['docs']['spec']
            s['docs']['files'] += [dict(d, path='%s: %s' % (key, d['path'])) for d in r['docs']['files']]
    rows = [by_id[s['id']] for s in sprints if s['id'] in touched]
    for r in rows:
        r['docs']['files'] = sorted(r['docs']['files'], key=lambda d: -(d['added'] + d['removed']))[:25]
    ms = list(projects.values())
    add = lambda path: sum(_get(m, path) or 0 for m in ms)
    checks = [m['tests']['last'] for m in ms if m['tests'].get('last')]
    levels = Counter()
    for m in ms:
        levels.update(m['tests'].get('levels') or {})
    def changes(name):
        events, base = add((name, 'events')), add((name, 'base'))
        items = sorted((dict(e, task='%s/%s' % (k, e['task'])) for k, m in projects.items() for e in m[name]['items']),
                       key=lambda e: e.get('at') or '')[-30:]
        out = dict(events=events, tasks=add((name, 'tasks')), base=base, per_task=round(events / base, 2) if base else None, items=items)
        if name == 'code_after_testing':
            out['rework_runs'] = dict(sum((Counter(m[name].get('rework_runs') or {}) for m in ms), Counter()))
        return out
    defects = dict(found=add(('defects', 'found')), fixed=add(('defects', 'fixed')), verified=add(('defects', 'verified')),
                   open=add(('defects', 'open')),
                   items=sorted((dict(d, task='%s/%s' % (k, d['task'])) for k, m in projects.items() for d in m['defects']['items']),
                                key=lambda d: d.get('at') or '')[-40:])
    for f in ('by_method', 'test_fix_levels', 'by_level', 'by_severity', 'by_finder'):
        defects[f] = dict(sum((Counter(m['defects'].get(f) or {}) for m in ms), Counter()))
    roles = {}
    for m in ms:
        for r in m['roles']:
            k = (r['role'], r['agent']); x = roles.setdefault(k, dict(r, tasks=0, handoffs=0, accepted=0, rejected=0, first_try=0,
                                                                   returns=0, rework_edits=0, _runs=0, _clean=0))
            for f in ('tasks', 'handoffs', 'accepted', 'rejected', 'first_try', 'returns', 'rework_edits'):
                x[f] += r[f]
            x['_runs'] += round(100 * r['first_try'] / r['first_try_pct']) if r.get('first_try_pct') else r['first_try']
            x['_clean'] += round(r['tasks'] * (r.get('clean_pct') or 0) / 100)
    role_rows = []
    for x in roles.values():
        runs, clean = x.pop('_runs'), x.pop('_clean')
        x['first_try_pct'] = round(100 * x['first_try'] / runs) if runs else None
        x['clean_pct'] = round(100 * clean / x['tasks']) if x['tasks'] else None
        role_rows.append(x)
    agents = {n: {f: sum(m['agents'][n][f] for m in ms) for f in (ms[0]['agents'][n] if ms else {})} for n in PROVIDERS}
    return dict(sprints=rows, totals=totals(rows), outside_commits=sum(m['outside_commits'] for m in ms),
                tests=dict(checks=add(('tests', 'checks')), passed=add(('tests', 'passed')), failed=add(('tests', 'failed')),
                           last=max(checks, key=lambda c: c.get('at') or '') if checks else None,
                           pipeline_checks=add(('tests', 'pipeline_checks')), pipeline_failed=add(('tests', 'pipeline_failed')),
                           signoffs=add(('tests', 'signoffs')), levels=dict(levels)),
                spec_changes=changes('spec_changes'), code_after_testing=changes('code_after_testing'),
                defects=defects, roles=sorted(role_rows, key=lambda r: (r['role'], r['agent'])), agents=agents)


def _get(m, path):
    for p in path:
        m = (m or {}).get(p)
    return m


def collect():
    import taskstate
    usage = load('usage.json', {})
    projects = taskstate.apply(load('projects.json', {}), str(DATA)).get('projects') or {}
    wf = load('workflow.json', {})
    sprints = build_sprints(usage)
    idx = Index(sprints)
    since = (sprints[0]['start'] if sprints else (datetime.now(timezone.utc) - timedelta(days=90)).isoformat())
    out = dict(generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), window_hours=5,
               basis='Спринт — окно лимита токенов (время сброса из журналов Claude и Codex); без него — 5-часовой блок активности.',
               sprints=sprints, projects={})
    for key, p in projects.items():
        out['projects'][key] = project_metrics(key, p, usage, sprints, idx, wf, since)
    out['portfolio'] = portfolio(out['projects'], sprints)
    out['portfolio']['team'] = merge_team([m['team'] for m in out['projects'].values()])
    return out


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    out = collect()
    dest = DATA / 'metrics.json'
    tmp = dest.with_suffix('.tmp')
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, dest)
    active = sum(1 for p in out['projects'].values() if p['sprints'])
    print('спринтов %d · проектов с работой %d -> %s' % (len(out['sprints']), active, dest))
    return 0


if __name__ == '__main__':
    sys.exit(main())
