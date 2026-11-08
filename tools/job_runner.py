"""Запуск недоверенных тестов и проверок с лимитами ОС (07: A2-07, E2E-R30).

Код и тесты пишет LLM — это недоверенный код. Раньше они шли обычным subprocess: без предела памяти, числа
процессов и с полным выходом в сеть; таймаут убивал только родителя, внуки оставались жить.

Теперь на Windows процесс создаётся приостановленным и помещается в Job Object до первой инструкции:
  * память всей группы процессов — не больше mem_mb (аллокация сверх — отказ в самом процессе);
  * процессов в группе — не больше procs (fork-бомба упирается в лимит);
  * таймаут и выход раннера закрывают задание — KILL_ON_JOB_CLOSE убивает всё дерево, включая внуков;
  * сеть: HTTP(S)_PROXY указывают на закрытый порт 127.0.0.1:9, NO_PROXY — только петля — библиотеки, уважающие
    прокси (pip, requests, urllib, npm), в сеть не выйдут. Это не брандмауэр: сокет напрямую не закрыт.
На других ОС — те же пределы через setrlimit.

Изоляция файлов (A2-07, SEC-009 п.2, SEC-TC-16/17). Если владелец завёл пользователя песочницы и сохранил его
учётные данные в диспетчере Windows (общие учётные данные с целью `tasktreker-sandbox`), процесс запускается от его
имени: CreateProcessWithLogonW приостановленным → Job Object → продолжение. ACL владельца закрывают этому пользователю
`data/` и профиль владельца, писать он может только в каталоги рабочих копий (`.claude/worktrees`, `<repo>.worktrees`).
Пароль читается из диспетчера в память на время вызова и затирается; в журналы и окружение не попадает.
Вывод, TEMP и свой домашний каталог (с .gitconfig: автор tt-sandbox, safe.directory) — в `<корень рабочих копий>/.sbx/`.
Нет учётных данных или рабочая копия вне этих каталогов — прежний режим `job` (тот же пользователь, те же лимиты);
какой режим сработал, видно в `CompletedProcess.sandbox`. TASKTREKER_SANDBOX_USER=require — без песочницы отказ,
=0 — не использовать.
"""
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

MEM_MB = 4096
PROCS = 64
OFFLINE_PROXY = 'http://127.0.0.1:9'
CRED_TARGET = 'tasktreker-sandbox'
IO_DIR = '.sbx'                  # коротко: пути тестов под ним не должны упираться в MAX_PATH
# Без профиля владельца: пути в его профиль песочнице всё равно закрыты, а TEMP указывает в каталог обмена.
PROFILE_VARS = ('USERPROFILE', 'HOME', 'HOMEPATH', 'HOMEDRIVE', 'APPDATA', 'LOCALAPPDATA', 'USERNAME', 'USERDOMAIN')
# Дочерний процесс под песочницей: запускает команду и пишет вывод в файлы каталога обмена.
CHILD = ("import json,subprocess,sys;a=json.load(open(sys.argv[1],encoding='utf-8'));o=open(a['out'],'wb');"
         "e=o if a['merge'] else open(a['err'],'wb')\n"
         "try:rc=subprocess.call(a['cmd'],cwd=a['cwd'],stdout=o,stderr=e,stdin=subprocess.DEVNULL,shell=a['shell'])\n"
         "except OSError as x:e.write(('не запустился: %s'%x).encode('utf-8'));rc=127\n"
         "o.close();e.close();sys.exit(rc)")


def offline_env(env):
    env = dict(env if env is not None else os.environ)
    for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env[k] = OFFLINE_PROXY
    env['NO_PROXY'] = env['no_proxy'] = 'localhost,127.0.0.1,::1'      # петля — не сеть: локальный рантайм модели, тестовые серверы
    env['PIP_NO_INDEX'] = '1'
    env['TASKTREKER_SANDBOX'] = 'job'
    return env


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _nt = ctypes.WinDLL('ntdll')

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ('r', 'w', 'o', 'rb', 'wb', 'ob')]

    class _BASIC(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class _EXT(ctypes.Structure):
        _fields_ = [('Basic', _BASIC), ('Io', _IO), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _nt.NtResumeProcess.argtypes = (wintypes.HANDLE,)

    LIMIT_ACTIVE_PROCESS, LIMIT_JOB_MEMORY, LIMIT_KILL_ON_CLOSE, LIMIT_DIE_ON_EXCEPTION = 0x8, 0x200, 0x2000, 0x400
    CREATE_SUSPENDED, CREATE_NO_WINDOW = 0x4, 0x08000000

    def _job(mem_mb, procs):
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            raise OSError(ctypes.get_last_error(), 'CreateJobObject')
        info = _EXT()
        info.Basic.LimitFlags = LIMIT_ACTIVE_PROCESS | LIMIT_JOB_MEMORY | LIMIT_KILL_ON_CLOSE | LIMIT_DIE_ON_EXCEPTION
        info.Basic.ActiveProcessLimit = procs
        info.JobMemoryLimit = mem_mb * 1048576
        if not _k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error(); _k32.CloseHandle(h)
            raise OSError(err, 'SetInformationJobObject')
        return h

    class _CRED(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                    ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                    ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                    ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                    ('UserName', wintypes.LPWSTR)]

    class _SI(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR), ('lpDesktop', wintypes.LPWSTR),
                    ('lpTitle', wintypes.LPWSTR), ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD), ('dwXSize', wintypes.DWORD),
                    ('dwYSize', wintypes.DWORD), ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD),
                    ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD), ('wShowWindow', wintypes.WORD),
                    ('cbReserved2', wintypes.WORD), ('lpReserved2', ctypes.c_void_p), ('hStdInput', wintypes.HANDLE),
                    ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]

    class _PI(ctypes.Structure):
        _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE), ('dwProcessId', wintypes.DWORD),
                    ('dwThreadId', wintypes.DWORD)]

    _adv = ctypes.WinDLL('advapi32', use_last_error=True)
    _adv.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CRED)))
    _adv.CredFree.argtypes = (ctypes.c_void_p,)
    _adv.CreateProcessWithLogonW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                                             wintypes.LPCWSTR, ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_void_p,
                                             wintypes.LPCWSTR, ctypes.POINTER(_SI), ctypes.POINTER(_PI))
    _k32.ResumeThread.argtypes = (wintypes.HANDLE,)
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    CRED_TYPE_GENERIC, LOGON_WITH_PROFILE, CREATE_UNICODE_ENVIRONMENT = 1, 1, 0x400

    def _credential():
        """(пользователь, домен, буфер пароля) из диспетчера учётных данных или None. Пароль — изменяемый буфер: его затирают."""
        p = ctypes.POINTER(_CRED)()
        if not _adv.CredReadW(CRED_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            return None
        try:
            c = p.contents
            user = c.UserName or ''
            n = c.CredentialBlobSize
            if not user or not n:
                return None
            raw = ctypes.string_at(c.CredentialBlob, n)
            pwd = ctypes.create_unicode_buffer(raw.decode('utf-16-le', 'replace'))
            raw = None
            domain, _, name = user.rpartition('\\')
            return name, (domain or '.'), pwd
        finally:
            _adv.CredFree(p)

    def _wipe(buf):
        ctypes.memset(buf, 0, ctypes.sizeof(buf))

    def _env_block(env):
        items = sorted(('%s=%s' % (k, v) for k, v in env.items() if k and '=' not in k[1:] and '\0' not in str(v)),
                       key=str.upper)
        return ctypes.create_unicode_buffer('\0'.join(items) + '\0\0')

    def _run_as_sandbox(cred, cmd, cwd, timeout, env, mem_mb, procs, merge, shell, io):
        name, domain, pwd = cred
        spec = io / 'spec.json'
        out, err = io / 'out.bin', io / 'err.bin'
        spec.write_text(json.dumps(dict(cmd=cmd, cwd=str(cwd), out=str(out), err=str(err), merge=merge, shell=shell),
                                   ensure_ascii=False), encoding='utf-8')
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline([sys.executable, '-I', '-c', CHILD, str(spec)]))
        block = _env_block(env)
        si = _SI(); si.cb = ctypes.sizeof(si)
        pi = _PI()
        job = _job(mem_mb, procs)
        try:
            ok = _adv.CreateProcessWithLogonW(name, domain, ctypes.cast(pwd, ctypes.c_void_p), LOGON_WITH_PROFILE, None, line,
                                              CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                                              ctypes.cast(block, ctypes.c_void_p), str(cwd), ctypes.byref(si), ctypes.byref(pi))
            _wipe(pwd)
            if not ok:
                raise OSError(ctypes.get_last_error(), 'CreateProcessWithLogonW (%s)' % name)
            try:
                if not _k32.AssignProcessToJobObject(job, pi.hProcess):
                    e = ctypes.get_last_error(); _k32.TerminateProcess(pi.hProcess, 1)
                    raise OSError(e, 'AssignProcessToJobObject')
                _k32.ResumeThread(pi.hThread)
                ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
                if _k32.WaitForSingleObject(pi.hProcess, ms) != 0:
                    _k32.TerminateJobObject(job, 1)
                    _k32.WaitForSingleObject(pi.hProcess, 5000)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                code = wintypes.DWORD()
                _k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
                rc = code.value - (1 << 32) if code.value >= (1 << 31) else code.value
            finally:
                _k32.CloseHandle(pi.hThread); _k32.CloseHandle(pi.hProcess)
        finally:
            _wipe(pwd)
            _k32.CloseHandle(job)
        read = lambda p: p.read_bytes() if p.is_file() else b''   # noqa: E731
        return rc, read(out), (b'' if merge else read(err))


def sandbox_roots():
    """Каталоги, которым владелец выдал ACL песочницы: рабочие копии самой установки TaskTreker.
    Не любой «*.worktrees» на диске: временный репозиторий теста лежит в TEMP владельца, песочнице он закрыт."""
    env = os.environ.get('TASKTREKER_SANDBOX_ROOTS')
    if env:
        return [Path(x).resolve() for x in env.split(os.pathsep) if x.strip()]
    here = Path(__file__).resolve().parents[1]
    parts = here.parts
    repo = Path(*parts[:parts.index('.claude')]) if '.claude' in parts else here
    return [repo / '.claude' / 'worktrees', repo.parent / (repo.name + '.worktrees')]


def sandbox_root(cwd, roots=None):
    """Каталог рабочих копий, куда песочнице разрешена запись (ACL владельца), или None."""
    p = Path(cwd or os.getcwd()).resolve()
    for r in (sandbox_roots() if roots is None else roots):
        r = Path(r).resolve()
        if p == r or r in p.parents:
            return r
    return None


def sandbox_user(cwd=None):
    """Учётные данные пользователя песочницы есть и каталог годится — (cred, root); иначе (None, причина)."""
    mode = os.environ.get('TASKTREKER_SANDBOX_USER', 'auto').strip().lower()
    if os.name != 'nt' or mode in ('0', 'off', 'no'):
        return None, 'выключено' if os.name == 'nt' else 'не Windows'
    root = sandbox_root(cwd)
    if root is None:
        return None, 'рабочая копия вне каталогов песочницы'
    cred = _credential()
    if cred is None:
        return None, 'нет учётных данных %s в диспетчере Windows' % CRED_TARGET
    return (cred, root), None


def sandbox_cmd(cmd):
    """Лаунчер py и python из PATH владельца лежат в его профиле — песочнице они закрыты. Тот же интерпретатор,
    что у сервера (на его каталог владелец выдал RX), и без флагов выбора версии лаунчера."""
    if isinstance(cmd, (list, tuple)) and cmd and Path(str(cmd[0])).stem.lower() in ('py', 'python', 'python3'):
        rest = list(cmd[1:])
        if Path(str(cmd[0])).stem.lower() == 'py':
            while rest and (rest[0][:2] in ('-2', '-3') or rest[0].startswith('-V:')):
                rest.pop(0)
        return [sys.executable] + rest
    if isinstance(cmd, str):
        head, _, tail = cmd.strip().partition(' ')
        if head.lower() in ('py', 'py.exe', 'python', 'python.exe', 'python3'):
            return subprocess.list2cmdline([sys.executable]) + (' ' + tail if tail else '')
    return cmd


def _emit(data, target, text, encoding):
    """Вывод песочницы — туда же, куда subprocess отдал бы его: PIPE → в результат, файл → в файл, None → на экран."""
    if target == subprocess.PIPE:
        return data.decode(encoding, 'replace') if text else data
    if target is None:
        sys.stdout.buffer.write(data); sys.stdout.flush()
    elif target not in (subprocess.DEVNULL, subprocess.STDOUT):
        import io as _io
        if isinstance(target, _io.TextIOBase):
            target.write(data.decode(encoding, 'replace'))
        else:
            target.write(data)
        target.flush()
    return None


def run(cmd, cwd=None, timeout=600, env=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, mem_mb=MEM_MB,
        procs=PROCS, offline=True, shell=False, encoding='utf-8'):
    """Как subprocess.run, но в задании ОС с лимитами. TimeoutExpired — как у subprocess, дерево процессов уже убито."""
    env = offline_env(env) if offline else env
    found, why = sandbox_user(cwd)
    if found is None and os.environ.get('TASKTREKER_SANDBOX_USER', '').strip().lower() == 'require':
        raise OSError('песочница обязательна (TASKTREKER_SANDBOX_USER=require), но недоступна: %s' % why)
    if found is not None:
        cred, root = found
        child = dict(env if env is not None else os.environ)
        for k in [k for k in child if k.upper() in PROFILE_VARS]:
            del child[k]
        # Каталог обмена — в <repo>.worktrees, если он есть: путь короче, и временные файлы теста не выглядят
        # «временным рабочим деревом .claude/worktrees» для проверок самой системы. Иначе — в корне рабочей копии.
        io = next((r for r in sandbox_roots() if r.name.endswith('.worktrees') and r.is_dir()), root) / IO_DIR / uuid.uuid4().hex[:8]
        home = io / 'h'
        for d in ('t', 'h/AppData/Roaming', 'h/AppData/Local'):
            (io / d).mkdir(parents=True, exist_ok=True)
        # Свой дом песочницы: ~ раскрывается, а глобальный .gitconfig с автором и safe.directory — с наименьшим
        # приоритетом, настройки самих репозиториев и -c сильнее.
        (home / '.gitconfig').write_text('[user]\n\tname = tt-sandbox\n\temail = tt-sandbox@localhost\n'
                                         '[safe]\n\tdirectory = *\n', encoding='utf-8')
        child.update(TEMP=str(io / 't'), TMP=str(io / 't'), TASKTREKER_SANDBOX='user', USERPROFILE=str(home), HOME=str(home),
                     HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive):], APPDATA=str(home / 'AppData' / 'Roaming'),
                     LOCALAPPDATA=str(home / 'AppData' / 'Local'), USERNAME=cred[0])
        try:
            rc, out, err = _run_as_sandbox(cred, sandbox_cmd(cmd), Path(cwd or os.getcwd()).resolve(), timeout, child, mem_mb, procs,
                                           stderr == subprocess.STDOUT, shell, io)
        finally:
            shutil.rmtree(io, ignore_errors=True)
        r = subprocess.CompletedProcess(cmd, rc, _emit(out, stdout, text, encoding), _emit(err, stderr, text, encoding))
        r.sandbox = 'user'
        return r
    kw = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=shell)
    if text:
        kw.update(text=True, encoding=encoding, errors='replace')
    if os.name != 'nt':
        def limits():
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1048576,) * 2)
            resource.setrlimit(resource.RLIMIT_NPROC, (procs,) * 2)
            os.setsid()
        p = subprocess.Popen(cmd, preexec_fn=limits, **kw)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(p.pid, signal.SIGKILL); p.communicate()
            raise
        r = subprocess.CompletedProcess(cmd, p.returncode, out, err); r.sandbox = 'rlimit'
        return r
    job = _job(mem_mb, procs)
    try:
        p = subprocess.Popen(cmd, creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW, **kw)
        if not _k32.AssignProcessToJobObject(job, int(p._handle)):
            err = ctypes.get_last_error(); p.kill(); p.wait()
            raise OSError(err, 'AssignProcessToJobObject')
        _nt.NtResumeProcess(int(p._handle))
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _k32.TerminateJobObject(job, 1); p.communicate()
            raise
        r = subprocess.CompletedProcess(cmd, p.returncode, out, err); r.sandbox = 'job'
        return r
    finally:
        _k32.CloseHandle(job)        # KILL_ON_JOB_CLOSE: оставшиеся внуки умирают вместе с заданием


if __name__ == '__main__':
    r = run(sys.argv[1:], stdout=None, stderr=None)
    sys.exit(r.returncode)
