"""Возможности сбора для модуля «Архитектура систем» — только чтение (TT-ARCH-001, E0: TTN-005, TTN-006).

Отчёт отвечает на один вопрос: что коллекторы E1+ смогут прочитать на этой машине, а что нет и почему.
Сам он ничего не собирает о сети и процессах и ничего не меняет:
  * команды — фиксированный список ниже, без оболочки и без подстановки ввода; каждая — запрос версии
    или наличия команды. netsh, docker ps/inspect, Get-NetTCPConnection здесь не вызываются — это E1;
  * Docker не запускается никогда: `docker version`/`docker context show`/`docker compose version`
    только спрашивают клиента и демон; недоступный демон — это факт «daemon: unavailable», а не повод
    стартовать Docker Desktop или службу;
  * запуск — через tools/job_runner.py (Job Object: лимит памяти и процессов, таймаут убивает дерево,
    прокси на закрытый порт). Не песочница tt-sandbox: у неё нет прав на Docker, и проверялись бы её права,
    а не права коллектора;
  * в отчёт попадают только версии, имена возможностей и признаки прав. Имя компьютера, пользователь,
    пути профиля, адреса endpoint контекста Docker и переменные окружения не пишутся. Вывод команд
    в отчёт не копируется, из него берётся одно поле по шаблону;
  * чего источник не доказал — `unknown` с причиной, а не `available`.

Запуск: py tools/arch_capabilities.py [--json]. Тесты подставляют run(cmd) -> (код, stdout).
"""
import ctypes
import json
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

TIMEOUT = 20
AVAILABLE, UNAVAILABLE, UNKNOWN = 'available', 'unavailable', 'unknown'

PS = ['powershell', '-NoProfile', '-NonInteractive', '-Command']
# Имя возможности -> (команда, шаблон поля в выводе). Только чтение: версии и наличие команд.
WINDOWS = {
    'powershell': (PS + ['$PSVersionTable.PSVersion.ToString()'], r'^(\d+\.\d+[\d.]*)\s*$'),
    'cim': (PS + ['(Get-Command Get-CimInstance -ErrorAction Stop).Name'], r'^(Get-CimInstance)\s*$'),
    'nettcpip': (PS + ['(Get-Command Get-NetTCPConnection -ErrorAction Stop).Source'], r'^(NetTCPIP)\s*$'),
    'netsh': (['netsh', 'interface', 'portproxy', 'show', 'v4tov4'], None),   # вывод не сохраняется, только код
}
DOCKER = {
    # `docker version` без демона возвращает 1 даже для клиента — клиента спрашиваем отдельно.
    'client': (['docker', '--version'], r'^Docker version (\d+\.\d+[\w.+-]*)'),
    'daemon': (['docker', 'version', '--format', '{{.Server.Version}}'], r'^(\d+\.\d+[\w.+-]*)\s*$'),
    'context': (['docker', 'context', 'show'], r'^([\w.-]{1,64})\s*$'),
    'compose': (['docker', 'compose', 'version', '--short'], r'^v?(\d+\.\d+[\w.+-]*)\s*$'),
}
FORBIDDEN = ('start', 'run', 'up', 'restart', 'stop', 'rm', 'kill', 'add', 'set', 'delete', 'reset', 'create')


def _run(cmd, timeout=TIMEOUT):
    """(код, stdout) или (None, причина). Через job_runner: лимиты ОС и убийство дерева по таймауту."""
    import job_runner
    try:
        p = job_runner.run(cmd, timeout=timeout, mem_mb=512, procs=16)
        return p.returncode, p.stdout or ''
    except FileNotFoundError:
        return None, 'не найдено: %s' % cmd[0]
    except subprocess.TimeoutExpired:
        return None, '%s: нет ответа за %d с' % (cmd[0], timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, '%s: %s' % (cmd[0], str(e)[:120])


def readonly(cmd):
    """Команда из списка не меняет систему: ни одного глагола изменения в аргументах."""
    words = [w.lower() for w in cmd[1:] if not w.startswith('-') and not w.startswith('{{')]
    return not any(w in FORBIDDEN for w in words)


def check(name, cmd, pattern, run=_run, which=shutil.which):
    src = ' '.join(cmd[:4])
    if not which(cmd[0]):
        return dict(name=name, status=UNAVAILABLE, reason='команда %s не найдена в PATH' % cmd[0], source=src)
    code, out = run(cmd)
    if code is None:
        return dict(name=name, status=UNKNOWN, reason=out, source=src)
    if code != 0:
        # Ненулевой код без разбора причины — недоступно для этого пользователя или нет компонента.
        return dict(name=name, status=UNAVAILABLE, reason='код возврата %s' % code, source=src)
    if pattern is None:
        return dict(name=name, status=AVAILABLE, reason='команда отвечает', source=src)
    line = next((l.strip() for l in (out or '').splitlines() if l.strip()), '')
    m = re.match(pattern, line)
    if not m:
        return dict(name=name, status=UNKNOWN, reason='ответ не распознан', source=src)
    return dict(name=name, status=AVAILABLE, value=m.group(1), source=src)


def elevated():
    """True/False — процесс с правами администратора; None — неизвестно (не Windows или нет API)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return None


def probe(run=_run, which=shutil.which, system=None, admin=None, at=None):
    system = system or platform.system()
    out = dict(module='architecture', stage='E0', kind='capabilities', readonly=True,
               observed_at=(at or datetime.now(timezone.utc)).isoformat(timespec='seconds'),
               os=dict(system=system, release=platform.release() if system == platform.system() else None,
                       version=platform.version() if system == platform.system() else None),
               windows=[], docker=[])
    if system == 'Windows':
        out['windows'] = [check(n, c, p, run, which) for n, (c, p) in WINDOWS.items()]
        adm = elevated() if admin is None else admin
        # Без прав администратора PID чужих служб и часть netsh http не читаются — это ограничение E1, не сбой.
        out['windows'].append(dict(name='elevated', status=UNKNOWN if adm is None else AVAILABLE if adm else UNAVAILABLE,
                                   reason='права администратора процесса' if adm is not None else 'признак прав не получен',
                                   source='IsUserAnAdmin'))
    else:
        out['windows'] = [dict(name='windows', status=UNAVAILABLE, reason='не Windows: %s' % system, source='platform')]
    out['docker'] = [check(n, c, p, run, which) for n, (c, p) in DOCKER.items()]
    client = next(d for d in out['docker'] if d['name'] == 'client')
    if client['status'] != AVAILABLE:
        for d in out['docker'][1:]:
            if d['status'] == AVAILABLE:
                continue
            d.update(status=UNKNOWN if client['status'] == UNKNOWN else UNAVAILABLE,
                     reason='клиент Docker недоступен: %s' % client.get('reason', ''))
    daemon = next(d for d in out['docker'] if d['name'] == 'daemon')
    if daemon['status'] != AVAILABLE:
        daemon['reason'] = (daemon.get('reason') or '') + '; Docker не запускался (только чтение)'
    return out


def text(r):
    rows = ['Возможности сбора «Архитектура систем» — %s, только чтение' % r['observed_at'],
            'ОС: %s %s' % (r['os']['system'], r['os'].get('release') or '')]
    for part in ('windows', 'docker'):
        rows.append(part.capitalize() + ':')
        for c in r[part]:
            rows.append('  %-10s %-11s %s' % (c['name'], c['status'], c.get('value') or c.get('reason') or ''))
    return '\n'.join(rows)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    r = probe()
    print(json.dumps(r, ensure_ascii=False, indent=1) if '--json' in argv else text(r))
    return 0


if __name__ == '__main__':
    sys.exit(main())
