"""Ключ владельца: кто принимает решения владельца и чем они подписаны (TT3-014, TTU-018, FR-003).

Решения владельца (одобрение релиза, грант автономии, настройки, снятие аварийной остановки)
принимает только доверенный источник — доска TaskTreker с токеном владельца. CLI `pm.py work`,
MCP `tt_work` и HTTP без токена — источник «агент», по умолчанию тоже «агент»: отказ, а не допуск.

Решения, от которых зависит слияние в main, подписаны HMAC ключом владельца. Ключ лежит вне
репозитория и вне data/ (`%LOCALAPPDATA%/TaskTreker/owner.key`), его создаёт сервер досок.
Поэтому правка `workflow.json` руками не создаёт действительного одобрения: подпись не сойдётся.

Граница честная: агент, работающий под той же учётной записью Windows, физически может прочитать
файл ключа. Защита рассчитана на ИИ, который действует через штатные интерфейсы (MCP, CLI, HTTP,
правка данных), — в том числе под влиянием подсказки из чужого текста. Полная изоляция от
намеренного злоумышленника требует отдельной учётной записи ОС, это вне области системы.
"""
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

OWNER, AGENT = 'owner', 'agent'
ORIGINS = (OWNER, AGENT)
HEADER = 'X-TaskTreker-Owner'

_path = None          # тесты подставляют свой путь; переменной окружения нет намеренно:
                      # процесс агента не должен подменять ключ, которым проверяются решения


def key_path():
    if _path:
        return Path(_path)
    base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return Path(base) / 'TaskTreker' / 'owner.key'


def key(create=False):
    """Ключ владельца или None. create=True — создать при первом запуске сервера досок."""
    p = key_path()
    try:
        k = p.read_bytes().strip()
    except OSError:
        k = b''
    if len(k) >= 32:
        return k
    if not create:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, 'xb') as f:                 # исключительное создание: два сервера не затрут ключ друг друга
            f.write(secrets.token_hex(32).encode())
    except FileExistsError:
        pass
    return key(False)


def _canon(rec):
    return json.dumps({k: v for k, v in rec.items() if k != 'sig'}, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def sign(rec):
    """Запись с подписью. Без ключа подписать нельзя — решение владельца не создаётся."""
    k = key(create=True)
    if not k:
        raise ValueError('Нет ключа владельца: решение владельца не может быть подписано')
    return dict(rec, sig=hmac.new(k, _canon(rec), hashlib.sha256).hexdigest())


def verify(rec):
    if not isinstance(rec, dict) or not isinstance(rec.get('sig'), str):
        return False
    k = key()
    if not k:
        return False
    return hmac.compare_digest(rec['sig'], hmac.new(k, _canon(rec), hashlib.sha256).hexdigest())


def board_token(create=False):
    """Токен владельца для доски. Выводится из ключа: смена ключа отзывает и токен, и все гранты."""
    k = key(create=create)
    return hmac.new(k, b'tasktreker-board-owner', hashlib.sha256).hexdigest() if k else None


def check_token(token):
    want = board_token()
    return bool(want and isinstance(token, str) and hmac.compare_digest(token, want))


def interactive():
    """Человек за терминалом: оба потока — консоль. У сессий ИИ ввод и вывод перенаправлены."""
    import sys
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
