# -*- coding: utf-8 -*-
"""Хук начала сессии Claude Code (ИНТ-03).

Подключается на уровне пользователя и потому запускается в начале КАЖДОЙ сессии,
в любом каталоге. Отсюда два жёстких правила:

  1. Никогда не ломать сессию. Даже если код системы сломан на середине правки,
     хук печатает одну строку и выходит с кодом 0. Поэтому импорт модулей системы
     стоит внутри защиты, а не в начале файла.
  2. Не тратить чужой контекст. Вне портфеля — одна строка; в проекте портфеля —
     брифинг по нему в бюджете context.HOOK_BUDGET.

Событие хука приходит JSON-объектом на stdin, из него нужен только cwd.

  py tools/hook.py < событие.json
"""
import os
import sys


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        raw = sys.stdin.read() if sys.stdin and not sys.stdin.isatty() else ""
    except Exception:
        raw = ""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import context
        print(context.hook_output(context.parse_event(raw)))
    except BaseException as e:            # включая SyntaxError при импорте
        print("TaskTreker: брифинг не собрался (%s). Портфель: py \"%s\" context" % (
            type(e).__name__,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "pm.py").replace("\\", "/")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
