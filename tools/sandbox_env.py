"""Минимальное окружение дочерних процессов (TT-123; SEC-009, OBS-05, SEC-TC-15).

Раньше сессии ИИ, прогоны CI и проверки проекта получали копию всего окружения владельца: ключи
облаков, токены GitHub, пароли из переменных — всё, что лежит в `os.environ`. Код и тесты, которые
пишет LLM, — недоверенный код. Теперь дочерний процесс получает:

  * системные переменные, без которых не работают Windows, Python, git, node (allowlist SYSTEM);
  * переменные TaskTreker (`TASKTREKER_*`) — без токенов: токен владельца в окружение не кладётся никогда;
  * только для CLI своего ИИ — его собственные учётные переменные (Claude: ANTHROPIC_*, CLAUDE_*;
    Codex: OPENAI_*, CODEX_*). Чужие ключи не передаются;
  * для тестов, CI и инструментов локальной модели — никаких учётных переменных.
Явная добавка (`extra`) всегда побеждает: её передаёт сам TaskTreker.

Это не песочница ОС: процесс работает под тем же пользователем и видит его файлы. Изоляция файлов —
отдельный пользователь Windows и ACL (см. docs/БЕЗОПАСНОСТЬ.md, SEC-009 п.2).
"""
import os

SYSTEM = ('PATH', 'PATHEXT', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR', 'HOME', 'USERPROFILE',
          'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
          'PROGRAMW6432', 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)', 'USERNAME', 'USERDOMAIN', 'COMPUTERNAME', 'OS',
          'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER', 'NUMBER_OF_PROCESSORS', 'LANG', 'LC_ALL', 'LANGUAGE', 'TZ',
          'TERM', 'SHELL', 'PYTHONIOENCODING', 'PYTHONUTF8', 'PYTHONPATH', 'VIRTUAL_ENV', 'NODE_OPTIONS', 'CUDA_PATH',
          'CUDA_VISIBLE_DEVICES', 'GIT_EXEC_PATH', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'NO_COLOR', 'FORCE_COLOR',
          'ALLUSERSPROFILE', 'PUBLIC', 'SESSIONNAME', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY')
PROVIDER = {'claude': ('ANTHROPIC_', 'CLAUDE_', 'CLAUDECODE'), 'codex': ('OPENAI_', 'CODEX_')}
NEVER = ('TASKTREKER_OWNER', 'TASKTREKER_OWNER_TOKEN')
SECRETISH = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'CREDENTIAL', 'PRIVATE')


def minimal(extra=None, provider=None, base=None):
    """Окружение дочернего процесса. provider — 'claude' | 'codex' для CLI своего ИИ; иначе без учётных данных."""
    src = dict(os.environ if base is None else base)
    keep = {k.upper() for k in SYSTEM}
    out = {}
    prefixes = PROVIDER.get(provider, ())
    for k, v in src.items():
        K = k.upper()
        if K in NEVER:
            continue
        if K in keep or K.startswith('TASKTREKER_') and not any(s in K for s in SECRETISH):
            out[k] = v
        elif prefixes and K.startswith(prefixes):
            out[k] = v
    for k, v in (extra or {}).items():
        if v is not None:
            out[k] = str(v)
    return out


def leaked(env, base=None):
    """Переменные похожие на секреты, которые попали в окружение не от своего провайдера (для тестов и аудита)."""
    return sorted(k for k in env if any(s in k.upper() for s in SECRETISH) and not k.upper().startswith(('ANTHROPIC_', 'OPENAI_',
                                                                                                         'CLAUDE_', 'CODEX_')))
