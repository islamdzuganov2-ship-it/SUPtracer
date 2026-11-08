"""Коллекторы «Архитектуры систем» — только чтение (TT-ARCH-001 NET-FR-02…04, 06, 07, 15; TT-133: TTN-013…018, E2).

Каждый источник — пара «команда из фиксированного списка» → «разборщик». Разборщики — чистые функции над
текстом или JSON, поэтому проверяются по fixture без Windows и Docker (tests/fixtures/arch/).

Что держит сбор безопасным и честным:
  * команды фиксированы, без оболочки и без подстановки ввода; запуск — tools/job_runner.py (Job Object,
    таймаут убивает дерево). Ни одной команды изменения: netsh только `show`, docker только
    `context ls`/`info`/`ps`/`inspect`. Docker не запускается; `docker compose config` не вызывается вовсе —
    он подставляет секреты из .env (NET-FR-06.6), Compose разбирается своим кодом через yaml.safe_load;
  * секреты не читаются: из docker inspect не берутся Config.Env, Cmd, Entrypoint, Mounts и чужие метки —
    только allowlist полей; из процессов — имя образа без командной строки и пути профиля; из Compose —
    без environment, env_file, secrets. Последний рубеж — scrub() по правилам mdparse.redact_secrets
    для каждой строки снимка (NET-AC-023);
  * без прав администратора недоступное — partial с причиной, а не пустота и не ошибка всего сбора;
    недоступный демон Docker — источник unavailable, остальное собирается (NET-AC-019, NET-AC-065);
  * текст netsh разбирается по числам и адресам, а не по заголовкам: на русской Windows заголовки локализованы;
  * файлы проектов — только внутри их корней из projects.json: realpath, без перехода по ссылкам,
    без рабочих копий Git (`.claude/worktrees`, `*.worktrees`, каталоги с файлом `.git`) (NET-AC-024, NET-AC-025);
  * проект контейнера — по метке working_dir Compose через project_routes, никогда по похожему имени:
    нет доказательства — unassigned, два кандидата — ambiguous (NET-AC-020, NET-AC-022).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import arch_model as m

COLLECTOR = 'tt-arch-collect/1'
TIMEOUT = 30
MAX_OUT = 20 * 1024 * 1024          # больше — источник partial: не разбираем бесконечный ответ (NET-FR-15.8)
MAX_CONTAINERS = 300
MAX_FILE = 1024 * 1024
HOST = 'local'                      # имя компьютера в модель не пишется: оно не нужно для идентичности на одном хосте

PS = ['powershell', '-NoProfile', '-NonInteractive', '-Command']
_UTC = "ToUniversalTime().ToString('o')"
WIN = {
    'tcp': PS + ["Get-NetTCPConnection | Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,"
                 "@{n='State';e={$_.State.ToString()}},OwningProcess,@{n='Created';e={$_.CreationTime.%s}} | "
                 "ConvertTo-Json -Compress" % _UTC],
    'udp': PS + ["Get-NetUDPEndpoint | Select-Object LocalAddress,LocalPort,OwningProcess,"
                 "@{n='Created';e={$_.CreationTime.%s}} | ConvertTo-Json -Compress" % _UTC],
    'process': PS + ["Get-CimInstance Win32_Process | Select-Object ProcessId,Name,"
                     "@{n='Created';e={if($_.CreationDate){$_.CreationDate.%s}}} | ConvertTo-Json -Compress" % _UTC],
    'service': PS + ["Get-CimInstance Win32_Service | Select-Object Name,State,ProcessId,StartMode | ConvertTo-Json -Compress"],
    'excluded_tcp4': ['netsh', 'interface', 'ipv4', 'show', 'excludedportrange', 'protocol=tcp'],
    'excluded_udp4': ['netsh', 'interface', 'ipv4', 'show', 'excludedportrange', 'protocol=udp'],
    'excluded_tcp6': ['netsh', 'interface', 'ipv6', 'show', 'excludedportrange', 'protocol=tcp'],
    'excluded_udp6': ['netsh', 'interface', 'ipv6', 'show', 'excludedportrange', 'protocol=udp'],
    'urlacl': ['netsh', 'http', 'show', 'urlacl'],
    'sslcert': ['netsh', 'http', 'show', 'sslcert'],
    'portproxy': ['netsh', 'interface', 'portproxy', 'show', 'all'],
}
DOCKER = {
    'contexts': ['docker', 'context', 'ls', '--format', '{{json .}}'],
    'daemon': ['docker', 'info', '--format', '{{.ID}}'],
    'ids': ['docker', 'ps', '--all', '--quiet', '--no-trunc'],
    'inspect': ['docker', 'inspect'],            # + id контейнеров из ids (шестнадцатеричные, проверяются)
}
LABELS = ('com.docker.compose.project', 'com.docker.compose.service', 'com.docker.compose.project.working_dir',
          'com.docker.compose.container-number', 'com.docker.compose.oneoff')
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.claude', 'dist', 'build', '.mypy_cache',
             '.pytest_cache', '.tox', 'site-packages', '.next', '.idea', '.vscode'}
COMPOSE_NAMES = ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml')
VERBS = ('add', 'set', 'delete', 'reset', 'start', 'stop', 'run', 'up', 'down', 'rm', 'kill', 'restart', 'create',
         'exec', 'pull', 'push', 'config')


def readonly(cmd):
    words = [w.lower() for w in cmd[1:] if not w.startswith('-') and not w.startswith('{{')]
    return not any(w in VERBS for w in words)


def _run(cmd, timeout=TIMEOUT):
    """(код, вывод) или (None, причина). Только через job_runner; вывод в журналы не пишется."""
    if not readonly(cmd):
        raise ValueError('Команда изменения запрещена коллектору: %s' % ' '.join(cmd[:4]))
    import job_runner
    try:
        p = job_runner.run(cmd, timeout=timeout, mem_mb=1024, procs=16)
        return p.returncode, p.stdout or ''
    except FileNotFoundError:
        return None, 'не найдено: %s' % cmd[0]
    except subprocess.TimeoutExpired:
        return None, '%s: нет ответа за %d с' % (cmd[0], timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, '%s: %s' % (cmd[0], type(e).__name__)


# ------------------------------------------------------------------ секреты

def scrub(value):
    """Каждая строка снимка — через правила вычистки секретов памяти (mdparse.redact_secrets)."""
    import mdparse
    if isinstance(value, str):
        return mdparse.redact_secrets(value)[0]
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def _json_rows(text):
    """ConvertTo-Json отдаёт объект для одной строки и список для многих; пустой вывод — пустой список."""
    text = (text or '').strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


# ------------------------------------------------------------------ Windows

def parse_processes(text, host_id=HOST):
    """PID -> объект процесса. Без времени создания идентичность unknown: PID переиспользуется."""
    out = {}
    for r in _json_rows(text):
        pid = r.get('ProcessId')
        if pid is None:
            continue
        name = r.get('Name') or None
        out[int(pid)] = m.process(host_id, int(pid), created=r.get('Created') or None, name=name,
                                  image=os.path.basename(name) if name else None)
    return out


def _owner(pid, created, procs):
    """Владелец сокета — процесс, только если PID жив и создан не позже сокета (NET-AC-006)."""
    p = procs.get(int(pid)) if pid is not None else None
    if not p or not p.get('created'):
        return None            # без времени создания процесса идентичность не доказана — только подсказка owner_pid
    if created and p.get('created') and p['created'] > created:
        return None            # процесс с этим PID моложе сокета: PID уже переиспользован
    return p['id']


def parse_tcp(text, procs, ev, host_id=HOST, ns='host'):
    """LISTEN — привязка-сервер; остальное — соединения, а не серверы (NET-AC-014)."""
    binds, conns = [], []
    for r in _json_rows(text):
        state = str(r.get('State') or '')
        ip, port = r.get('LocalAddress'), r.get('LocalPort')
        if ip is None or port is None or m.family(ip) is None:
            continue
        owner = _owner(r.get('OwningProcess'), r.get('Created'), procs)
        if state.lower() == 'listen':
            binds.append(dict(m.binding(host_id, ns, 'tcp', ip, port, ev, owner=owner), owner_pid=r.get('OwningProcess')))
        elif state.lower() in ('established', 'synsent', 'synreceived', 'closewait', 'finwait1', 'finwait2', 'timewait', 'lastack'):
            if r.get('RemoteAddress') and m.family(r['RemoteAddress']):
                conns.append(m.connection(host_id, ns, 'tcp', ip, port, r['RemoteAddress'], r.get('RemotePort') or 0, ev, owner))
    return binds, conns


def parse_udp(text, procs, ev, host_id=HOST, ns='host'):
    """UDP — отдельный класс: bound_observed, не «TCP LISTEN» (TTN-014)."""
    out = []
    for r in _json_rows(text):
        ip, port = r.get('LocalAddress'), r.get('LocalPort')
        if ip is None or port is None or m.family(ip) is None:
            continue
        out.append(dict(m.binding(host_id, ns, 'udp', ip, port, ev, socket_state='bound_observed',
                                  owner=_owner(r.get('OwningProcess'), r.get('Created'), procs)),
                        owner_pid=r.get('OwningProcess')))
    return out


def parse_services(text, ev, host_id=HOST):
    """Службы. Остановленная служба — lifecycle stopped и никакого listening (NET-AC-007)."""
    out = []
    for r in _json_rows(text):
        if not r.get('Name'):
            continue
        st = str(r.get('State') or '').lower()
        life = 'running' if st == 'running' else 'stopped' if st == 'stopped' else 'paused' if st == 'paused' else 'unknown'
        out.append(m.service(host_id, r['Name'], life, pid=r.get('ProcessId') or None, start_mode=r.get('StartMode'), evidence_ref=ev))
    return out


_RANGE = re.compile(r'^\s*(\d{1,5})\s+(\d{1,5})\s*(\*)?\s*$')


def parse_excluded(text, protocol, fam, ev, host_id=HOST):
    """`netsh … show excludedportrange`: строки «начало конец [*]», «*» — постоянная (administered) резервация.
    Заголовки не читаются: они локализованы."""
    out = []
    for line in (text or '').splitlines():
        g = _RANGE.match(line)
        if g:
            a, b = int(g[1]), int(g[2])
            if a <= b <= 65535:
                out.append(m.reservation(host_id, 'os_excluded_range', ev, protocol=protocol, family=fam, start=a, end=b,
                                         persistent=bool(g[3])))
    return out


_URL = re.compile(r'(https?://[^\s]+)', re.I)


def parse_urlacl(text, ev, host_id=HOST):
    """HTTP.sys URL ACL — резервация префикса адреса, не сокет (NET-AC-010). SDDL и учётные записи не берём."""
    out, seen = [], set()
    for line in (text or '').splitlines():
        g = _URL.search(line)
        if g and g[1] not in seen:
            seen.add(g[1])
            port = re.search(r':(\d{1,5})(?:/|$)', g[1])
            out.append(m.reservation(host_id, 'http_urlacl', ev, protocol='tcp', url_prefix=g[1],
                                     start=int(port[1]) if port else None, end=int(port[1]) if port else None))
    return out


# «IP:port : 0.0.0.0:443», «Hostname:port : site:443», на русской Windows — «IP:порт», «Имя узла:порт».
_SSL = re.compile(r'^\s*(?:IP|Hostname|Имя узла)\s*:\s*\S+\s*:\s*(\S+:\d{1,5})\s*$', re.I)


def parse_sslcert(text, ev, host_id=HOST):
    """Привязка сертификата TLS к адресу — тоже не сокет. Отпечаток и идентификатор приложения не берём."""
    out = []
    for line in (text or '').splitlines():
        g = _SSL.match(line)
        if not g:
            continue
        port = re.search(r':(\d{1,5})$', g[1])
        out.append(m.reservation(host_id, 'http_sslcert', ev, protocol='tcp', url_prefix=g[1],
                                 start=int(port[1]) if port else None, end=int(port[1]) if port else None))
    return out


_PROXY = re.compile(r'^\s*(\S+)\s+(\d{1,5})\s+(\S+)\s+(\d{1,5})\s*$')


def parse_portproxy(text, ev, host_id=HOST):
    """portproxy: две стороны — слушает (listen) и куда передаёт (connect). Прямой связи нет (NET-AC-011).
    Раздел v4tov4/v4tov6/… определяется по заголовку «… ipv4: … ipv6:», а если не распознан — unknown."""
    out, kind = [], None
    for line in (text or '').splitlines():
        low = line.lower()
        if 'ipv4' in low or 'ipv6' in low:
            fams = re.findall(r'ipv([46])', low)
            kind = 'v%sto%s' % (fams[0], 'v' + fams[1]) if len(fams) >= 2 else kind
            continue
        g = _PROXY.match(line)
        if g and not g[2].startswith('-'):
            out.append(m.reservation(host_id, 'portproxy', ev, protocol='tcp', start=int(g[2]), end=int(g[2]),
                                     listen=dict(address=g[1], port=int(g[2])), connect=dict(address=g[3], port=int(g[4])),
                                     url_prefix=kind or 'unknown'))
    return out


# ------------------------------------------------------------------ Docker

_HEX = re.compile(r'^[0-9a-f]{12,64}$')


def parse_contexts(text):
    """Имена контекстов и текущий. Адрес endpoint не берём: в нём бывают хосты и пользователи."""
    out = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('Name'):
            out.append(dict(name=r['Name'], current=bool(r.get('Current'))))
    return out


def _ports(spec, ns, daemon, ev, state, quality, cid):
    """{"8000/tcp": [{"HostIp": "", "HostPort": "8000"}]} -> привязки хоста. Пустой HostPort — порт назначит
    Docker при запуске: из конфигурации его не угадываем (NET-AC-017)."""
    out, dynamic = [], []
    for key, binds in (spec or {}).items():
        cport, _, proto = key.partition('/')
        for b in binds or []:
            hp = str(b.get('HostPort') or '').strip()
            ip = b.get('HostIp') or '0.0.0.0'
            if not hp or hp == '0':
                dynamic.append(dict(container_port=int(cport), protocol=proto or 'tcp'))
                continue
            out.append(m.binding(daemon, ns, proto or 'tcp', ip, hp, ev, socket_state='not_observed',
                                 configuration_state=state, quality=quality, container_port=int(cport), owner=cid))
    return out, dynamic


def parse_inspect(text, daemon, ev, routes=None):
    """docker inspect -> контейнеры и их привязки. Берутся только перечисленные поля (allowlist)."""
    containers, binds = [], []
    for r in json.loads(text or '[]') or []:
        st = (r.get('State') or {})
        life = st.get('Status') if st.get('Status') in m.LIFECYCLE else 'unknown'
        cfg = r.get('Config') or {}
        labels = {k: v for k, v in (cfg.get('Labels') or {}).items() if k in LABELS}
        compose = dict(project=labels.get('com.docker.compose.project'), service=labels.get('com.docker.compose.service'),
                       working_dir=labels.get('com.docker.compose.project.working_dir'))
        c = m.container(daemon, r.get('Id'), (r.get('Name') or '').lstrip('/'), life,
                        compose={k: v for k, v in compose.items() if v}, image=cfg.get('Image'), evidence_ref=ev)
        c['exposed'] = sorted(k for k in (cfg.get('ExposedPorts') or {}))           # EXPOSE — не порт хоста (NET-AC-015)
        ns = 'docker:%s' % daemon
        if life == 'running':
            # Фактические привязки — из NetworkSettings.Ports: там и динамически назначенные порты.
            got, _ = _ports((r.get('NetworkSettings') or {}).get('Ports'), ns, daemon, ev, 'published', 'verified', c['id'])
            c['dynamic_ports'] = []
        else:
            got, dyn = _ports((r.get('HostConfig') or {}).get('PortBindings'), ns, daemon, ev, 'configured', 'declared', c['id'])
            c['dynamic_ports'] = dyn
        binds += got
        if routes is not None:
            attribute(c, routes)
        containers.append(c)
    return containers, binds


def attribute(c, routes):
    """Проект контейнера по каталогу Compose (метка working_dir) — и только по нему."""
    import project_routes as pr
    wd = (c.get('compose') or {}).get('working_dir')
    if not wd:
        return c
    hit = pr.resolve_path(wd, routes)
    if hit['project']:
        c.update(project=hit['project'], project_method='compose_working_dir', project_candidates=hit['candidates'])
    elif hit['candidates']:
        c.update(project=None, project_method='ambiguous', project_candidates=hit['candidates'])
    return c


# ------------------------------------------------------------------ конфигурации проектов

def _inside(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:
        return False


def config_files(root, depth=3):
    """Файлы Compose и Dockerfile внутри корня проекта. (найдено, отклонено) — отклонённые с причиной."""
    root = os.path.realpath(root)
    found, refused = [], []
    if not os.path.isdir(root):
        return found, [dict(path=root, reason='нет каталога')]
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        level = 0 if rel == '.' else rel.count(os.sep) + 1
        keep = []
        for d in dirs:
            full = os.path.join(dirpath, d)
            if d in SKIP_DIRS or d.endswith('.worktrees') or os.path.islink(full) or os.path.isfile(os.path.join(full, '.git')):
                continue          # рабочие копии Git и ссылки — не часть приложения (NET-AC-025)
            keep.append(d)
        dirs[:] = keep if level < depth else []
        for f in files:
            low = f.lower()
            if low in COMPOSE_NAMES or low == 'dockerfile' or low.endswith('.dockerfile'):
                full = os.path.join(dirpath, f)
                if os.path.islink(full) or not _inside(full, root):
                    refused.append(dict(path=os.path.relpath(full, root), reason='ссылка за пределы проекта'))
                elif os.path.getsize(full) > MAX_FILE:
                    refused.append(dict(path=os.path.relpath(full, root), reason='файл больше 1 МБ'))
                else:
                    found.append(full)
    return sorted(found), refused


_VAR = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)')


def _compose_port(spec):
    """Короткая или длинная запись ports -> (ip, host, container, protocol, unresolved). Значение
    с переменной не подставляется: в отчёте имя переменной, порт хоста неизвестен."""
    if isinstance(spec, dict):
        return (spec.get('host_ip') or '0.0.0.0', spec.get('published'), spec.get('target'), spec.get('protocol') or 'tcp', [])
    s = str(spec)
    unresolved = _VAR.findall(s)
    proto = 'tcp'
    if '/' in s:
        s, proto = s.rsplit('/', 1)
    parts = s.rsplit(':', 2) if s.count(':') <= 2 else None
    if parts is None:                                   # IPv6: [::1]:8080:80
        g = re.match(r'^\[([^\]]+)\]:([^:]*):(.+)$', s)
        return (g[1], g[2], g[3], proto, unresolved) if g else (None, None, None, proto, unresolved)
    if len(parts) == 1:
        return ('0.0.0.0', None, parts[0], proto, unresolved)
    if len(parts) == 2:
        return ('0.0.0.0', parts[0], parts[1], proto, unresolved)
    return (parts[0].strip('[]') or '0.0.0.0', parts[1], parts[2], proto, unresolved)


def parse_compose(path, root, project=None, text=None):
    """Compose-файл -> объявленные привязки и сервисы. environment, env_file, secrets, command не читаются."""
    import yaml
    rel = os.path.relpath(path, root).replace('\\', '/')
    raw = text if text is not None else Path(path).read_text(encoding='utf-8', errors='replace')
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        return dict(file=rel, services=[]), []
    name = data.get('name') if isinstance(data.get('name'), str) else None
    ev = m.evidence('declared_config', 'compose', COLLECTOR, locator=rel)
    services, binds = [], []
    for sname, svc in (data.get('services') or {}).items():
        if not isinstance(svc, dict):
            continue
        ns = 'compose:%s/%s' % (project or name or rel, sname)
        entry = dict(service=sname, container_name=svc.get('container_name'), compose_name=name,
                     expose=[str(x) for x in svc.get('expose') or []], unresolved_ports=[])
        for spec in svc.get('ports') or []:
            ip, hp, cp, proto, unres = _compose_port(spec)
            try:
                hp_i = int(hp) if hp not in (None, '') and not unres else None
                cp_i = int(str(cp).split('-')[0]) if cp not in (None, '') and not _VAR.search(str(cp)) else None
            except ValueError:
                hp_i = cp_i = None
            if hp_i is None or m.family(ip or '') is None:
                entry['unresolved_ports'].append(dict(spec=scrub(str(spec)) if not isinstance(spec, dict) else 'long-syntax',
                                                      variables=unres, container_port=cp_i, protocol=proto))
                continue
            binds.append(m.binding('declared', ns, proto, ip, hp_i, ev['id'], socket_state='not_observed',
                                   configuration_state='configured', quality='declared', container_port=cp_i, owner=project))
        services.append(entry)
    return dict(file=rel, compose_name=name, services=services, evidence=ev), binds


_EXPOSE = re.compile(r'^\s*EXPOSE\s+(.+)$', re.I | re.M)


def parse_dockerfile(text):
    """EXPOSE — объявление, а не публикация на хосте (exposed_declared)."""
    out = []
    for g in _EXPOSE.finditer(text or ''):
        for tok in g[1].split():
            if re.fullmatch(r'\d{1,5}(/(tcp|udp))?', tok):
                out.append(tok if '/' in tok else tok + '/tcp')
    return out


def match_declared(declared, runtime, containers):
    """Объявленный в Compose порт ↔ фактическая привязка того же сервиса (NET-AC-016, NET-AC-017).
    Сопоставление — по проекту, сервису Compose и порту контейнера, не по номеру порта хоста."""
    by_c = {c['id']: c for c in containers}
    out = []
    for d in declared:
        proj, _, svc = d['namespace_id'].partition(':')[2].rpartition('/')
        hits = [r for r in runtime if r.get('container_port') == d.get('container_port') and r['protocol'] == d['protocol']
                and (by_c.get(r.get('owner')) or {}).get('compose', {}).get('service') == svc
                and (by_c.get(r.get('owner')) or {}).get('project') == d.get('owner')]
        if not hits:
            out.append(dict(declared=d['id'], runtime=None, outcome='not_observed'))
        for h in hits:
            out.append(dict(declared=d['id'], runtime=h['id'], outcome='match' if h['port'] == d['port'] else 'drift',
                            declared_port=d['port'], runtime_port=h['port']))
    return out


# ------------------------------------------------------------------ сбор целиком

def _source(name, fn):
    t = time.monotonic()
    try:
        state, reason, objs = fn()
    except Exception as e:                        # сбой одного источника не роняет остальные (NET-AC-065)
        state, reason, objs = 'unavailable', 'сбой разбора: %s' % type(e).__name__, []
    return m.source(name, state, reason, duration_ms=int((time.monotonic() - t) * 1000)), objs


def _call(run, cmd):
    code, out = run(cmd)
    if code is None:
        return None, out
    if code != 0:
        return None, 'код возврата %s' % code
    if len(out or '') > MAX_OUT:
        return None, 'ответ больше %d МБ' % (MAX_OUT // 1048576)
    return out, None


def collect_windows(run=_run, host_id=HOST):
    objs, srcs = [], []
    procs = {}

    def proc():
        nonlocal procs
        out, why = _call(run, WIN['process'])
        if out is None:
            return 'unavailable', why, []
        procs = parse_processes(out, host_id)
        named = sum(1 for p in procs.values() if p.get('created'))
        return ('available' if named == len(procs) else 'partial',
                None if named == len(procs) else 'время создания известно у %d из %d процессов' % (named, len(procs)),
                list(procs.values()))

    def sock(kind):
        def f():
            out, why = _call(run, WIN[kind])
            if out is None:
                return 'unavailable', why, []
            ev = m.evidence('observed_socket', 'windows:%s' % kind, COLLECTOR, locator=WIN[kind][-1][:40])
            if kind == 'tcp':
                b, c = parse_tcp(out, procs, ev['id'], host_id)
                found = b + c
            else:
                found = parse_udp(out, procs, ev['id'], host_id)
            orphans = sum(1 for x in found if not x.get('owner'))
            return ('partial' if orphans else 'available',
                    'владелец не установлен у %d (права или PID исчез)' % orphans if orphans else None, [ev] + found)
        return f

    def services():
        out, why = _call(run, WIN['service'])
        if out is None:
            return 'unavailable', why, []
        ev = m.evidence('runtime_metadata', 'windows:service', COLLECTOR)
        return 'available', None, [ev] + parse_services(out, ev['id'], host_id)

    def netsh(kind, parser, *args):
        def f():
            out, why = _call(run, WIN[kind])
            if out is None:
                return 'unavailable', why, []
            ev = m.evidence('runtime_metadata', 'netsh:%s' % kind, COLLECTOR)
            return 'available', None, [ev] + parser(out, *args, ev['id'], host_id)
        return f

    plan = [('windows:process', proc), ('windows:tcp', sock('tcp')), ('windows:udp', sock('udp')),
            ('windows:service', services)]
    for k in ('excluded_tcp4', 'excluded_udp4', 'excluded_tcp6', 'excluded_udp6'):
        plan.append(('netsh:' + k, netsh(k, parse_excluded, k[9:12], 'ipv' + k[-1])))
    plan += [('netsh:urlacl', netsh('urlacl', parse_urlacl)), ('netsh:sslcert', netsh('sslcert', parse_sslcert)),
             ('netsh:portproxy', netsh('portproxy', parse_portproxy))]
    for name, fn in plan:
        s, o = _source(name, fn)
        srcs.append(s); objs += o
    return objs, srcs


def collect_docker(run=_run, which=shutil.which, routes=None):
    objs, srcs = [], []
    if not which('docker'):
        return [], [m.source('docker', 'unavailable', 'клиент docker не найден в PATH')]
    out, why = _call(run, DOCKER['contexts'])
    ctx = parse_contexts(out) if out else []
    srcs.append(m.source('docker:contexts', 'available' if out is not None else 'unavailable', why))
    daemon, why = _call(run, DOCKER['daemon'])
    daemon = (daemon or '').strip()
    if not daemon:
        srcs.append(m.source('docker:engine', 'unavailable', (why or 'нет ID демона') + '; Docker не запускался'))
        return objs, srcs
    daemon_id = re.sub(r'[^\w:-]', '', daemon)[:80]
    ids, why = _call(run, DOCKER['ids'])
    if ids is None:
        srcs.append(m.source('docker:engine', 'unavailable', why))
        return objs, srcs
    ids = [i for i in ids.split() if _HEX.match(i)]
    partial = len(ids) > MAX_CONTAINERS
    ids = ids[:MAX_CONTAINERS]
    if ids:
        text, why = _call(run, DOCKER['inspect'] + ids)
        if text is None:
            srcs.append(m.source('docker:engine', 'unavailable', why))
            return objs, srcs
        ev = m.evidence('runtime_metadata', 'docker:inspect', COLLECTOR)
        cs, bs = parse_inspect(text, daemon_id, ev['id'], routes)
        objs += [ev] + cs + bs
    srcs.append(m.source('docker:engine', 'partial' if partial else 'available',
                         'контейнеров больше %d' % MAX_CONTAINERS if partial else None))
    for c in ctx:
        c['daemon'] = daemon_id if c['current'] else None
    return objs, srcs


def collect_configs(projects, read=None):
    """Объявления по проектам портфеля: Compose и Dockerfile внутри их корней."""
    configs, binds, refused = [], [], []
    for key, meta in sorted(projects.items()):
        root = meta.get('path')
        if not root or not os.path.isdir(root):
            continue
        files, bad = config_files(root)
        refused += [dict(project=key, **b) for b in bad]
        for f in files:
            try:
                if os.path.basename(f).lower() in COMPOSE_NAMES:
                    info, b = parse_compose(f, os.path.realpath(root), key)
                    configs.append(dict(project=key, **{k: v for k, v in info.items() if k != 'evidence'}))
                    binds += ([info['evidence']] if 'evidence' in info else []) + b
                else:
                    configs.append(dict(project=key, file=os.path.relpath(f, os.path.realpath(root)).replace('\\', '/'),
                                        exposed=parse_dockerfile(Path(f).read_text(encoding='utf-8', errors='replace'))))
            except Exception as e:
                refused.append(dict(project=key, path=os.path.relpath(f, root), reason='не разобран: %s' % type(e).__name__))
    return configs, binds, refused


def collect(run=_run, which=shutil.which, projects=None, system=None):
    """Полный снимок. Источники независимы: недоступный один не отменяет остальные."""
    import platform
    import project_routes as pr
    projects = projects or {}
    routes = pr.paths(projects)
    objs, srcs = [m.host(HOST, system or platform.system())], []
    if (system or platform.system()) == 'Windows':
        o, s = collect_windows(run); objs += o; srcs += s
    else:
        srcs.append(m.source('windows', 'unavailable', 'не Windows'))
    o, s = collect_docker(run, which, routes); objs += o; srcs += s
    cfg, b, refused = collect_configs(projects)
    objs += b
    srcs.append(m.source('configs', 'partial' if refused else 'available',
                         '%d файлов не разобрано или отклонено' % len(refused) if refused else None))
    snap = m.snapshot(objs, srcs)
    snap.update(configs=cfg, refused=refused, conflicts=m.conflicts([x for x in snap['objects'] if x['kind'] == 'binding']))
    return scrub(snap)


def save(snap, data_dir=None):
    """data/arch/inventory.json — личные данные, вне git (как learning/, security/)."""
    import taskstate
    base = Path(data_dir or os.environ.get('TASKTREKER_DATA') or Path(__file__).resolve().parent.parent / 'data') / 'arch'
    taskstate.save_json(str(base), 'inventory.json', snap)
    return base / 'inventory.json'


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    data = Path(os.environ.get('TASKTREKER_DATA') or Path(__file__).resolve().parent.parent / 'data')
    projects = json.loads((data / 'projects.json').read_text(encoding='utf-8')).get('projects', {})
    snap = collect(projects=projects)
    if '--save' in argv:
        print('сохранено:', save(snap, data))
    counts = {}
    for o in snap['objects']:
        counts[o['kind']] = counts.get(o['kind'], 0) + 1
    print(json.dumps(dict(complete=snap['complete'], partial=snap['partial_reasons'], counts=counts,
                          configs=len(snap['configs']), refused=len(snap['refused']), conflicts=len(snap['conflicts'])),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
