"""Аппаратная диагностика целевой машины (TT3-005; ТЗ §1 «Оборудование», G0).

Только чтение: ни одна настройка, драйвер или файл системы не меняется. Чего узнать
не удалось, помечено `null` с причиной в `unknown` — значения не угадываются.

    py tools/hardware_probe.py            # отчёт текстом
    py tools/hardware_probe.py --json     # отчёт JSON
"""
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

TIMEOUT = 15
NVSMI_QUERY = 'name,memory.total,memory.free,memory.used,driver_version,utilization.gpu'


def _run(cmd, timeout=TIMEOUT):
    """(код, stdout) или (None, причина) — без исключений наружу."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace',
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return p.returncode, p.stdout
    except FileNotFoundError:
        return None, 'не найдено: %s' % cmd[0]
    except (OSError, subprocess.SubprocessError) as e:
        return None, '%s: %s' % (cmd[0], e)


def _mb(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def memory():
    """ОЗУ в МБ: (всего, свободно) или (None, None)."""
    if os.name == 'nt':
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX(); st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys // 2 ** 20, st.ullAvailPhys // 2 ** 20
        return None, None
    try:
        info = {}
        with open('/proc/meminfo', encoding='ascii') as f:
            for line in f:
                k, v = line.split(':', 1); info[k] = int(v.split()[0])
        return info['MemTotal'] // 1024, info.get('MemAvailable', info.get('MemFree', 0)) // 1024
    except (OSError, KeyError, ValueError):
        return None, None


def cpu_name():
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as k:
                return winreg.QueryValueEx(k, 'ProcessorNameString')[0].strip()
        except OSError:
            pass
    try:
        with open('/proc/cpuinfo', encoding='utf-8') as f:
            for line in f:
                if line.startswith('model name'):
                    return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def parse_nvsmi(csv):
    gpus = []
    for line in (csv or '').strip().splitlines():
        parts = [x.strip() for x in line.split(',')]
        if len(parts) < 5:
            continue
        gpus.append(dict(name=parts[0], vram_total_mb=_mb(parts[1]), vram_free_mb=_mb(parts[2]), vram_used_mb=_mb(parts[3]),
                         driver=parts[4] or None, utilization_pct=_mb(parts[5]) if len(parts) > 5 else None))
    return gpus


def cuda_version(header):
    # Старые драйверы пишут «CUDA Version», новые — «CUDA UMD Version».
    m = re.search(r'CUDA (?:UMD )?Version:\s*([\d.]+)', header or '')
    return m.group(1) if m else None


def gpu(run=_run):
    """Список GPU NVIDIA и версия CUDA драйвера; без nvidia-smi — пустой список и причина."""
    code, out = run(['nvidia-smi', '--query-gpu=' + NVSMI_QUERY, '--format=csv,noheader,nounits'])
    if code != 0:
        return [], None, 'nvidia-smi: %s' % (out.strip()[:200] if code is None else 'код %s' % code)
    gpus = parse_nvsmi(out)
    code2, head = run(['nvidia-smi'])
    return gpus, cuda_version(head) if code2 == 0 else None, None if gpus else 'nvidia-smi не вернул ни одного GPU'


def probe(run=_run, data_dir=None):
    unknown = {}
    total, free = memory()
    if total is None:
        unknown['ram'] = 'объём ОЗУ не прочитан'
    gpus, cuda, why = gpu(run)
    if why:
        unknown['gpu'] = why
    if gpus and not cuda:
        unknown['cuda'] = 'версия CUDA драйвера не найдена в выводе nvidia-smi'
    disk = None
    if data_dir and os.path.isdir(data_dir):
        u = shutil.disk_usage(data_dir); disk = dict(path=data_dir, total_gb=round(u.total / 2 ** 30, 1), free_gb=round(u.free / 2 ** 30, 1))
    name = cpu_name()
    if not name:
        unknown['cpu'] = 'модель процессора не прочитана'
    return dict(schema=1, at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                os=dict(system=platform.system(), release=platform.release(), version=platform.version(), machine=platform.machine()),
                python=platform.python_version(), cpu=dict(name=name, logical_cores=os.cpu_count()),
                ram=dict(total_mb=total, free_mb=free), gpus=gpus, cuda_driver=cuda, disk=disk, unknown=unknown,
                note='Только чтение: конфигурация не менялась. Свободная VRAM — на момент замера, '
                     'её занимают и другие программы.')


def text(r):
    lines = ['ОС: %s %s (%s)' % (r['os']['system'], r['os']['release'], r['os']['version']),
             'Python: %s' % r['python'],
             'CPU: %s, логических ядер %s' % (r['cpu']['name'] or '—', r['cpu']['logical_cores']),
             'ОЗУ: всего %s МБ, свободно %s МБ' % (r['ram']['total_mb'], r['ram']['free_mb'])]
    for g in r['gpus']:
        lines.append('GPU: %s, VRAM всего %s МБ, свободно %s МБ, драйвер %s, загрузка %s%%' % (
            g['name'], g['vram_total_mb'], g['vram_free_mb'], g['driver'], g['utilization_pct']))
    lines.append('CUDA (драйвер): %s' % (r['cuda_driver'] or '—'))
    if r['disk']:
        lines.append('Диск данных: свободно %s из %s ГБ' % (r['disk']['free_gb'], r['disk']['total_gb']))
    for k, v in r['unknown'].items():
        lines.append('не определено (%s): %s' % (k, v))
    return '\n'.join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import workflow
        data = str(workflow.data_dir())
    except Exception:            # noqa: BLE001 — диагностика работает и без данных системы
        data = None
    r = probe(data_dir=data)
    print(json.dumps(r, ensure_ascii=False, indent=2) if '--json' in argv else text(r))
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    sys.exit(main())
