# -*- coding: utf-8 -*-
"""Подключение Клода к системе на уровне пользователя (ИНТ-06).

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
import json
import os
import shutil
import subprocess
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(TOOLS, "hook.py").replace("\\", "/")
SERVER = os.path.join(TOOLS, "mcp_server.py").replace("\\", "/")
NAME = "tasktreker"
STATUS_MESSAGE = "TaskTreker: брифинг проекта"

# Переопределение путей — только для тестов: настоящие настройки они не трогают.
SETTINGS = os.environ.get("TASKTREKER_CLAUDE_SETTINGS") or os.path.expanduser("~/.claude/settings.json")


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
    data["hooks"]["SessionStart"] = kept + [hook_group()]
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


# ------------------------------------------------------------------ команды

def cmd_connect(args):
    if "--status" in args:
        return cmd_status()
    for p in (HOOK, SERVER):
        if not os.path.exists(p):
            print("Нет %s — подключать нечего." % p)
            return 1
    if ".claude/worktrees" in TOOLS.replace("\\", "/").lower() and "--force" not in args:
        print("Эта копия системы — временное рабочее дерево: после его удаления подключение сломается.")
        print("Подключайте из основного каталога системы или добавьте --force.")
        return 1
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
    return 0 if ok else 1


def cmd_disconnect(args):
    removed = remove_hook()
    print("Хук начала сессии снят." if removed else "Хука TaskTreker в настройках не было.")
    ok, out = remove_mcp()
    print("MCP-сервер снят." if ok else "MCP-сервер: %s" % (out or "не был зарегистрирован"))
    return 0


def cmd_status():
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
    sys.exit({"connect": cmd_connect, "disconnect": cmd_disconnect}.get(cmd, lambda a: cmd_status())(sys.argv[2:]))
