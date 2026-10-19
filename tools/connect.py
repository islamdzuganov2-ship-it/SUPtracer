# -*- coding: utf-8 -*-
"""Подключение Claude Code и Codex к системе на уровне пользователя (ИНТ-06).

Две части, обе ставятся и снимаются одной командой:
  хук SessionStart в ~/.claude/settings.json — в начале каждой сессии брифинг
    по проекту рабочего каталога (tools/hook.py);
  MCP-сервер tasktreker уровня пользователя — инструменты tt_* в любом проекте
    (tools/mcp_server.py), регистрируется штатным CLI Claude Code.

Пути берутся от этого файла, поэтому ставить нужно из той копии системы, которая
останется жить, — из основного рабочего каталога, а не из временного рабочего дерева.

Настройки пользователя не перезаписываются целиком: хук вливается в существующий
список, чужие записи не трогаются, перед первой правкой кладётся резервная копия.

  py tools/pm.py connect              установить или обновить
  py tools/pm.py connect --status     что подключено сейчас
  py tools/pm.py disconnect           снять
"""
import glob
import copy
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import tomllib
except ImportError:  # Остальная система и Claude остаются доступны на Python 3.10.
    tomllib = None

TOOLS = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(TOOLS, "hook.py").replace("\\", "/")
SERVER = os.path.join(TOOLS, "mcp_server.py").replace("\\", "/")
NAME = "tasktreker"
STATUS_MESSAGE = "TaskTreker: брифинг проекта"

# Переопределение путей — только для тестов: настоящие настройки они не трогают.
SETTINGS = os.environ.get("TASKTREKER_CLAUDE_SETTINGS") or os.path.expanduser("~/.claude/settings.json")
PROVIDERS = ("claude", "codex", "all")


# ------------------------------------------------------------------ хук

def hook_group():
    """Форма exec (command + args), без оболочки: пути с пробелами и кириллицей
    не ломаются ни в bash, ни в PowerShell."""
    return {"hooks": [{"type": "command", "command": "py", "args": ["-3", HOOK],
                       "timeout": 20, "statusMessage": STATUS_MESSAGE}]}


def _is_ours(group):
    for h in (group or {}).get("hooks") or []:
        blob = " ".join([str(h.get("command") or "")] + [str(a) for a in (h.get("args") or [])])
        if "hook.py" in blob and ("tasktreker" in blob.lower() or h.get("statusMessage") == STATUS_MESSAGE):
            return True
    return False


def _read_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text) if text.strip() else {}


def _write_settings(data):
    """Запись через временный файл: оборванная запись не должна оставить битый
    settings.json — с ним Claude Code молча отключает ВСЕ настройки файла."""
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    backup = SETTINGS + ".tasktreker-backup"
    if os.path.exists(SETTINGS) and not os.path.exists(backup):
        shutil.copy2(SETTINGS, backup)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)
    with open(SETTINGS, encoding="utf-8") as f:     # проверка: файл читается и хук на месте
        json.load(f)


class SettingsBroken(Exception):
    """settings.json не читается: переписывать его нельзя — там чужие настройки."""


def install_hook():
    try:
        data = _read_settings()
    except ValueError as e:
        raise SettingsBroken("%s не читается как JSON (%s) — не трогаю" % (SETTINGS, e))
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    kept = [g for g in groups if not _is_ours(g)]
    replaced = len(kept) != len(groups)
    desired = kept + [hook_group()]
    if groups != desired:
        data["hooks"]["SessionStart"] = desired
        _write_settings(data)
    return "обновлён" if replaced else "установлен"


def remove_hook():
    data = _read_settings()
    groups = (data.get("hooks") or {}).get("SessionStart") or []
    kept = [g for g in groups if not _is_ours(g)]
    if len(kept) == len(groups):
        return False
    if kept:
        data["hooks"]["SessionStart"] = kept
    else:
        data["hooks"].pop("SessionStart", None)
        if not data["hooks"]:
            data.pop("hooks")
    _write_settings(data)
    return True


def hook_installed():
    try:
        return any(_is_ours(g) for g in (_read_settings().get("hooks") or {}).get("SessionStart") or [])
    except Exception:
        return False


# ------------------------------------------------------------------ MCP

def find_cli():
    """CLI Claude Code: из PATH, иначе тот, что носит с собой настольное приложение."""
    exe = shutil.which("claude")
    if exe:
        return exe
    bundled = glob.glob(os.path.expanduser("~/AppData/Roaming/Claude/claude-code/*/claude.exe"))

    def ver(p):
        parts = os.path.basename(os.path.dirname(p)).split(".")
        return tuple(int(x) if x.isdigit() else 0 for x in parts)
    return max(bundled, key=ver) if bundled else None


def _cli(args, cli=None):
    cli = cli or find_cli()
    if not cli:
        return None, "CLI Claude Code не найден"
    try:
        r = subprocess.run([cli] + args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return None, str(e)


def mcp_installed(cli=None):
    code, out = _cli(["mcp", "get", NAME], cli)
    return code == 0 and "mcp_server.py" in out


def install_mcp(cli=None):
    cli = cli or find_cli()
    if not cli:
        return False, ("CLI Claude Code не найден. Зарегистрируйте сервер вручную:\n"
                       "  claude mcp add --scope user %s -- py -3 \"%s\"" % (NAME, SERVER))
    _cli(["mcp", "remove", "--scope", "user", NAME], cli)       # обновление путей — через снятие
    code, out = _cli(["mcp", "add", "--scope", "user", NAME, "--", "py", "-3", SERVER], cli)
    return code == 0, out


def remove_mcp(cli=None):
    code, out = _cli(["mcp", "remove", "--scope", "user", NAME], cli)
    return code == 0, out


# ------------------------------------------------------------------ Codex / ChatGPT desktop

def codex_config_path():
    """Общая конфигурация локальных клиентов, с учётом штатного CODEX_HOME."""
    override = os.environ.get("TASKTREKER_CODEX_CONFIG")
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.abspath(os.path.expanduser(override or os.path.join(home, "config.toml")))


def _parse_codex(text):
    if tomllib is None:
        raise SettingsBroken("Для настройки Codex нужен Python 3.11+ (встроенный разбор TOML).")
    try:
        return tomllib.loads(text.lstrip("\ufeff"))
    except ValueError as e:
        raise SettingsBroken("Конфигурация Codex не читается как TOML (%s) — не трогаю" % e) from e


def _read_codex():
    path = codex_config_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    return path, text, _parse_codex(text)


def _codex_entry(data):
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise SettingsBroken("mcp_servers в конфигурации Codex не является таблицей — не трогаю")
    return servers.get(NAME)


def _codex_entry_ours(entry):
    if not isinstance(entry, dict) or "url" in entry:
        return False
    args = entry.get("args")
    expected = os.path.normcase(os.path.abspath(SERVER))
    return isinstance(args, list) and any(
        isinstance(arg, str) and os.path.isabs(arg)
        and os.path.normcase(os.path.abspath(arg)) == expected for arg in args)


def _toml_value(value):
    """Только значения собственной таблицы; остальной TOML остаётся побайтно прежним."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(_toml_value(k) + " = " + _toml_value(v)
                                for k, v in value.items()) + " }"
    raise SettingsBroken("Неподдерживаемое значение в таблице Codex — не трогаю")


def _codex_table_spans(text):
    """Найти отдельные таблицы сервера. Семантическая проверка ниже обязательна:
    строка внутри многострочного значения тоже может выглядеть как заголовок."""
    headers = []
    pattern = r"(?m)^[ \t]*\[[^\r\n]+\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)"
    for match in re.finditer(pattern, text):
        try:
            node = _parse_codex(match.group())
        except SettingsBroken:
            continue
        path = []
        while isinstance(node, dict) and len(node) == 1:
            key, node = next(iter(node.items()))
            path.append(key)
        headers.append((match.start(), tuple(path)))
    spans = []
    for i, (start, path) in enumerate(headers):
        if path[:2] == ("mcp_servers", NAME):
            spans.append((start, headers[i + 1][0] if i + 1 < len(headers) else len(text)))
    return spans


def _replace_codex_entry(text, data, entry):
    existing = _codex_entry(data)
    spans = _codex_table_spans(text)
    if existing is not None and not spans:
        raise SettingsBroken("Таблица tasktreker задана встроенным значением TOML. "
                             "Измените её вручную — остальную конфигурацию не трогаю.")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = ""
    if entry is not None:
        block = "[mcp_servers.%s]%s" % (NAME, newline)
        block += "".join("%s = %s%s" % (_toml_value(key), _toml_value(value), newline)
                         for key, value in entry.items())
        block += newline
    if spans:
        result = text
        for i in range(len(spans) - 1, -1, -1):
            start, end = spans[i]
            result = result[:start] + (block if i == 0 else "") + result[end:]
    else:
        result = text + (newline if text and not text.endswith("\n") else "")
        result += (newline if text else "") + block
    expected = copy.deepcopy(data)
    servers = expected.setdefault("mcp_servers", {})
    if entry is None:
        servers.pop(NAME, None)
    else:
        servers[NAME] = entry
    actual = _parse_codex(result)
    # Пустая неявная родительская таблица исчезает вместе с последним сервером.
    if not expected.get("mcp_servers") and "mcp_servers" not in actual:
        expected.pop("mcp_servers", None)
    if actual != expected:
        raise SettingsBroken("Не удалось безопасно изменить только таблицу tasktreker — не трогаю")
    return result


def _write_codex(path, original, text):
    """Проверка перед записью, резервная копия и атомарная замена."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8", newline="") as f:
            current = f.read()
    except FileNotFoundError:
        current = ""
    if current != original:
        raise SettingsBroken("Конфигурация Codex изменилась во время подключения; повторите команду.")
    backup = path + ".tasktreker-backup"
    if os.path.exists(path) and not os.path.exists(backup):
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(prefix=".tasktreker-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def codex_mcp_installed():
    try:
        _, _, data = _read_codex()
        entry = _codex_entry(data)
        return _codex_entry_ours(entry) and entry.get("enabled", True) is not False
    except (OSError, ValueError, SettingsBroken):
        return False


def install_codex_mcp():
    try:
        path, text, data = _read_codex()
        existing = _codex_entry(data)
        if existing is not None and not _codex_entry_ours(existing):
            raise SettingsBroken("Имя tasktreker в Codex уже занято другим сервером — не трогаю")
        desired = dict(existing or {})
        desired.update(command=os.path.abspath(sys.executable), args=[SERVER])
        if desired.get("enabled") is False:
            desired["enabled"] = True
        if existing == desired:
            return True, "Уже подключён: %s" % path
        result = _replace_codex_entry(text, data, desired)
        _write_codex(path, text, result)
        return True, "MCP-сервер «%s» подключён к Codex / ChatGPT desktop: %s" % (NAME, path)
    except (OSError, ValueError, SettingsBroken) as e:
        return False, str(e)


def remove_codex_mcp():
    try:
        path, text, data = _read_codex()
        entry = _codex_entry(data)
        if entry is None:
            return True, "MCP-сервер TaskTreker в Codex не был подключён."
        if not _codex_entry_ours(entry):
            raise SettingsBroken("Имя tasktreker в Codex занято другим сервером — не трогаю")
        result = _replace_codex_entry(text, data, None)
        _write_codex(path, text, result)
        return True, "MCP-сервер TaskTreker снят с Codex."
    except (OSError, ValueError, SettingsBroken) as e:
        return False, str(e)


# ------------------------------------------------------------------ команды

def _provider(args, provider):
    for i, arg in enumerate(args):
        if arg == "--provider":
            if i + 1 >= len(args):
                raise ValueError("После --provider укажите claude, codex или all.")
            provider = args[i + 1]
        elif arg.startswith("--provider="):
            provider = arg.split("=", 1)[1]
    if provider not in PROVIDERS:
        raise ValueError("Неизвестный провайдер: %s. Допустимы claude, codex, all." % provider)
    return provider


def cmd_connect(args, provider="claude"):
    try:
        provider = _provider(args, provider)
    except ValueError as e:
        print(e)
        return 1
    if "--status" in args:
        return cmd_status(provider)
    for p in (HOOK, SERVER):
        if not os.path.exists(p):
            print("Нет %s — подключать нечего." % p)
            return 1
    normalized = TOOLS.replace("\\", "/").lower()
    if any(part in normalized for part in (".claude/worktrees/", ".codex/worktrees/")) and "--force" not in args:
        print("Эта копия системы — временное рабочее дерево: после его удаления подключение сломается.")
        print("Подключайте из основного каталога системы или добавьте --force.")
        return 1
    codex_ok = True
    if provider in ("codex", "all"):
        codex_ok, out = install_codex_mcp()
        print(out)
        if codex_ok:
            print("Перезапустите MCP-серверы в настройках Codex / ChatGPT desktop или откройте новую сессию.")
        if provider == "codex":
            return 0 if codex_ok else 1
    try:
        how = install_hook()
    except SettingsBroken as e:
        print(e)
        return 1
    print("Хук начала сессии %s: %s" % (how, SETTINGS))
    ok, out = install_mcp()
    print(("MCP-сервер «%s» зарегистрирован на уровне пользователя." % NAME) if ok
          else "MCP-сервер не зарегистрирован: %s" % out)
    print("Проверка вручную: echo {} | py -3 \"%s\" --text" % HOOK)
    print("Новые сессии Claude Code подхватят подключение; открытые — после перезапуска.")
    return 0 if ok and codex_ok else 1


def cmd_disconnect(args, provider="claude"):
    try:
        provider = _provider(args, provider)
    except ValueError as e:
        print(e)
        return 1
    codex_ok = True
    if provider in ("codex", "all"):
        codex_ok, out = remove_codex_mcp()
        print(out)
        if provider == "codex":
            return 0 if codex_ok else 1
    removed = remove_hook()
    print("Хук начала сессии снят." if removed else "Хука TaskTreker в настройках не было.")
    ok, out = remove_mcp()
    print("MCP-сервер снят." if ok else "MCP-сервер: %s" % (out or "не был зарегистрирован"))
    return 0 if codex_ok else 1


def cmd_status(provider="claude"):
    try:
        provider = _provider([], provider)
    except ValueError as e:
        print(e)
        return 1
    if provider in ("codex", "all"):
        print("Codex / ChatGPT desktop MCP: %s (%s)" % (
            "подключён" if codex_mcp_installed() else "нет или отключён", codex_config_path()))
        if provider == "codex":
            return 0
    print("Хук начала сессии: %s (%s)" % ("подключён" if hook_installed() else "нет", SETTINGS))
    cli = find_cli()
    if not cli:
        print("MCP-сервер: неизвестно — CLI Claude Code не найден")
    else:
        print("MCP-сервер: %s" % ("подключён" if mcp_installed(cli) else "нет"))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"connect": cmd_connect, "disconnect": cmd_disconnect}.get(
        cmd, lambda a: cmd_connect(a + ["--status"]))(sys.argv[2:]))
