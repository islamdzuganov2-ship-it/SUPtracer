# -*- coding: utf-8 -*-
"""MCP-сервер TaskTreker (ИНТ-04): память портфеля как инструменты ассистента.

Зачем. Хук начала сессии даёт ассистенту брифинг, но читать и писать знание по ходу
работы нужно инструментами, а не командами оболочки с абсолютными путями. Сервер
подключается на уровне пользователя, поэтому система становится памятью в любом
проекте, а не только в своём каталоге.

Протокол — Model Context Protocol поверх stdio, одно JSON-RPC сообщение на строку,
только стандартная библиотека. Сервер двухэпохальный:
  современные клиенты (ревизия 2026-07-28 и новее) передают версию, возможности
    и имя клиента в _meta каждого запроса; сервер отвечает на server/discover
    и ставит resultType в каждый результат;
  наследные клиенты (2025-11-25 и раньше) начинают с рукопожатия initialize.
Запрос без _meta и без initialize обслуживается по наследным правилам: для
локального инструмента совместимость важнее строгости.

Безопасность (БЕЗ-06): сервер не открывает сетевых портов и работает только
с каталогом данных системы. projects.json меняет единственный инструмент —
tt_apply, принятие предложений в бэклог; всё остальное пишет только в память
и во входящие.

  py tools/mcp_server.py        (запускает клиент MCP, не человек)
"""
import contextlib
import io
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context
import inbox
import ingest
import memory as mem
import mdparse as md
import tree

SERVER_INFO = {"name": "tasktreker", "title": "TaskTreker", "version": "1.0.1"}
MODERN_VERSIONS = ["2026-07-28"]
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SUPPORTED = MODERN_VERSIONS + LEGACY_VERSIONS
META = "io.modelcontextprotocol/"

# Кешируемые результаты современной ревизии (utilities/caching): server/discover и
# tools/list обязаны нести ttlMs и cacheScope. Без них клиент отвергает ответ целиком —
# сервер подключался, а инструменты в сессии так и не появлялись. Список инструментов
# одинаков для всех и пользовательских данных не содержит, поэтому "public".
CACHE_HINTS = {"ttlMs": 300000, "cacheScope": "public"}

# ИНТ-05: протокол работы передаётся клиенту самим сервером — так он действует
# в любом проекте, а не только там, где лежит CLAUDE.md системы.
INSTRUCTIONS = """TaskTreker — оперативная память портфеля проектов пользователя. Знание, задачи и история решений лежат в системе, а не в твоём контексте.

1. Начинай работу с tt_context (в проекте портфеля его брифинг обычно уже пришёл хуком начала сессии). Это указатель: заголовки и идентификаторы, без содержимого.
2. Не копируй данные системы в свою память и не пересказывай их целиком. Читай точечно: tt_recall по id записи, tt_status по проекту, tt_tree — что из чего выросло.
3. Узнал то, что переживёт эту сессию (решение, требование, риск, итог, факт), — запиши tt_remember в проект, а не в ответ и не в свою память. Если знание выросло из запроса пользователя, передай root — так оно встанет в дерево.
4. Устаревшее не удаляется: tt_forget с причиной, при замене — с преемником (by).
5. Бэклог меняется только принятием предложений (tt_apply) или ручной правкой по регламенту проекта. Приём заметок (tt_ingest) пишет только в память и во входящие; размер и приоритет предложений — догадка, проверь их перед tt_apply."""


# ---------------------------------------------------------------- инструменты

def _schema(props, required=()):
    s = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = list(required)
    return s


STR = {"type": "string"}
READ = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS = [
    {"name": "tt_context", "title": "Брифинг",
     "description": "Брифинг по портфелю или по одному проекту в бюджете токенов: что требует решения, "
                    "состояние, верхушка бэклога, указатель памяти без тел записей. Проект можно задать "
                    "ключом или рабочим каталогом (cwd) — тогда он определится сам.",
     "inputSchema": _schema({"project": dict(STR, description="Ключ проекта"),
                             "cwd": dict(STR, description="Рабочий каталог — проект определится по нему"),
                             "budget": {"type": "integer", "minimum": 200, "maximum": 20000,
                                        "description": "Бюджет в токенах, по умолчанию 1500"}}),
     "annotations": dict(READ, title="Брифинг")},
    {"name": "tt_recall", "title": "Прочитать запись",
     "description": "Запись памяти целиком по id или совпадения по словам с дайджестами. "
                    "full=true — тела всех совпадений в пределах бюджета.",
     "inputSchema": _schema({"query": dict(STR, description="id записи или слова из неё"),
                             "project": dict(STR, description="Поднять выше записи этого проекта"),
                             "budget": {"type": "integer", "minimum": 200, "maximum": 20000},
                             "full": {"type": "boolean"}}, ["query"]),
     "annotations": dict(READ, title="Прочитать запись")},
    {"name": "tt_search", "title": "Поиск по памяти",
     "description": "Поиск записей памяти по словам: id, вид, проект, заголовок, дайджест. "
                    "Тела не возвращает — для них tt_recall.",
     "inputSchema": _schema({"query": STR, "project": dict(STR, description="Только этот проект"),
                             "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["query"]),
     "annotations": dict(READ, title="Поиск по памяти")},
    {"name": "tt_remember", "title": "Записать знание",
     "description": "Записать знание в память проекта: решение, требование, риск, итог, факт, запрос, "
                    "источник или заметку. Тот же заголовок того же проекта перезаписывает запись, "
                    "а не плодит копию.",
     "inputSchema": _schema({
         "project": dict(STR, description="Ключ проекта или «портфель»"),
         "title": dict(STR, description="Заголовок — по нему запись находят"),
         "text": dict(STR, description="Суть своими словами"),
         "kind": {"type": "string", "enum": list(mem.KINDS)},
         "tags": {"type": "array", "items": STR},
         "root": dict(STR, description="id исходного запроса — запись встанет в его ветвь дерева"),
         "parent": dict(STR, description="id записи-родителя"),
         "pin": {"type": "boolean", "description": "Всегда показывать в брифинге"}},
         ["project", "title", "text"]),
     "annotations": dict(WRITE, title="Записать знание")},
    {"name": "tt_forget", "title": "Вывести запись",
     "description": "Вывести запись из оборота без удаления файла: статус «устарела» с причиной, "
                    "или «заменена», если указан преемник by.",
     "inputSchema": _schema({"id": STR, "why": dict(STR, description="Почему устарела"),
                             "by": dict(STR, description="id записи-преемника")}, ["id"]),
     "annotations": dict(WRITE, title="Вывести запись")},
    {"name": "tt_status", "title": "Состояние проекта",
     "description": "Сводка по портфелю или подробно по проекту: цель, готовность, затраты, "
                    "бэклог с оценкой, следующие шаги.",
     "inputSchema": _schema({"project": dict(STR, description="Ключ проекта; без него — портфель")}),
     "annotations": dict(READ, title="Состояние проекта")},
    {"name": "tt_inbox", "title": "Входящие",
     "description": "Предложения задач, найденные в заметках и ждущие решения.",
     "inputSchema": _schema({"project": STR, "all": {"type": "boolean", "description": "И разобранные"}}),
     "annotations": dict(READ, title="Входящие")},
    {"name": "tt_apply", "title": "Принять предложения",
     "description": "Принять предложения из входящих в бэклог проекта. "
                    "Номер задачи продолжает серию проекта, связь с исходным "
                    "запросом сохраняется.",
     "inputSchema": _schema({"ids": {"type": "array", "items": STR, "description": "Номера IN-n"},
                             "all": {"type": "boolean"}, "project": STR}),
     "annotations": dict(WRITE, title="Принять предложения")},
    {"name": "tt_reject", "title": "Отклонить предложения",
     "description": "Отклонить предложения с причиной. Записи остаются следом решения.",
     "inputSchema": _schema({"ids": {"type": "array", "items": STR}, "why": STR}, ["ids", "why"]),
     "annotations": dict(WRITE, title="Отклонить предложения")},
    {"name": "tt_ingest", "title": "Принять заметки",
     "description": "Разобрать Markdown-файл или каталог: записи в память, задачи во входящие, "
                    "раскладка по проектам правилами системы. Ключи и пароли вычищаются.",
     "inputSchema": _schema({"path": STR, "project": dict(STR, description="Проект для всех файлов"),
                             "root": dict(STR, description="Подвесить под этот запрос"),
                             "dry_run": {"type": "boolean"}}, ["path"]),
     "annotations": dict(WRITE, title="Принять заметки")},
    {"name": "tt_tree", "title": "Дерево работ",
     "description": "Ветвь дерева работ текстом: корневые запросы, решения, задачи, фундамент, почки.",
     "inputSchema": _schema({"project": STR, "depth": {"type": "integer", "minimum": 1, "maximum": 8}}),
     "annotations": dict(READ, title="Дерево работ")},
    {"name": "tt_projects", "title": "Проекты портфеля",
     "description": "Ключи, имена и каталоги проектов — чтобы сопоставить рабочий каталог с проектом.",
     "inputSchema": _schema({}),
     "annotations": dict(READ, title="Проекты портфеля")},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class ToolError(Exception):
    """Ошибка выполнения: уходит ассистенту как isError, чтобы он мог поправиться."""


def _captured(fn, *a, **kw):
    """Модули системы печатают в stdout — а stdout сервера принадлежит протоколу."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = fn(*a, **kw)
    return res, buf.getvalue().strip()


def _projects():
    return ingest.load_projects()


def _check_project(key, allow_portfolio=True):
    P = _projects()
    if key in P or (allow_portfolio and key == "портфель"):
        return key
    raise ToolError("Нет проекта «%s». Проекты: %s" % (key, ", ".join(sorted(P)) or "—"))


def t_context(a):
    P = _projects()
    focus = a.get("project")
    if focus:
        _check_project(focus, allow_portfolio=False)
    elif a.get("cwd"):
        focus = context.project_for_path(a["cwd"], P)
    return context.build(budget=int(a.get("budget") or 1500), focus=focus, hook=True)


def t_recall(a):
    ok, text = mem.recall_text(a["query"], budget=int(a.get("budget") or 4000),
                               full=bool(a.get("full")), project=a.get("project"),
                               cmd="tt_recall с id")
    if not ok:
        raise ToolError(text)
    return text


def t_search(a):
    hits = mem.search(a["query"], project=a.get("project"), limit=int(a.get("limit") or 12))
    if a.get("project"):
        hits = [h for h in hits if h["project"] == a["project"]]
    if not hits:
        return "Ничего не найдено по «%s»." % a["query"]
    return "\n".join("[%s] %s · %s · %s%s\n    %s" % (
        h["id"], h["kind"], h["project"], h["title"],
        "" if h["status"] == "активна" else " (%s)" % h["status"], h["digest"][:160]) for h in hits)


def t_remember(a):
    project = _check_project(a["project"])
    kind = a.get("kind") or "заметка"
    if kind not in mem.KINDS:
        raise ToolError("Вид «%s» не из списка: %s" % (kind, ", ".join(mem.KINDS)))
    title, text = a["title"].strip(), a["text"].strip()
    if not title or not text:
        raise ToolError("Нужны непустые title и text.")
    text, found = md.redact_secrets(text)                 # БЕЗ-05 и для записи вручную
    rec = mem.new_record(title, text, project=project, kind=kind, tags=a.get("tags") or [],
                         root=a.get("root") or "", parent=a.get("parent") or "",
                         source="mcp", pin=bool(a.get("pin")))
    mem.save(rec)
    mem.build_index()
    note = " Вычищено секретов: %d." % len(found) if found else ""
    return "Записано: %s (%s, проект %s).%s" % (rec["id"], kind, project, note)


def t_forget(a):
    if a.get("by") and not mem.get(a["by"]):
        raise ToolError("Нет записи-преемника %s." % a["by"])
    rec = mem.retire(a["id"], a.get("why") or "", by=a.get("by"))
    if not rec:
        raise ToolError("Нет записи %s." % a["id"])
    mem.build_index()
    return "%s: %s%s. Файл не удалён." % (rec["id"], rec["status"],
                                           (" — преемник %s" % a["by"]) if a.get("by") else "")


def t_status(a):
    import pm
    if a.get("project"):
        _check_project(a["project"], allow_portfolio=False)
    _, out = _captured(pm.cmd_status, [a["project"]] if a.get("project") else [])
    return out or "Портфель пуст."


def t_inbox(a):
    args = (["--project", a["project"]] if a.get("project") else []) + (["--all"] if a.get("all") else [])
    _, out = _captured(inbox.cmd_list, args)
    return out


def t_apply(a):
    if a.get("all"):
        args = ["--all"] + (["--project", a["project"]] if a.get("project") else [])
    elif a.get("ids"):
        args = [str(i) for i in a["ids"]]
    else:
        raise ToolError("Укажите ids или all=true.")
    code, out = _captured(inbox.cmd_apply, args)
    if code:
        raise ToolError(out)
    return out + "\nОценку и дерево пересчитает следующий сбор (pm.py refresh --fast)."


def t_reject(a):
    _, out = _captured(inbox.cmd_reject, [str(i) for i in a["ids"]] + ["--why", a["why"]])
    return out


def t_ingest(a):
    path = a["path"]
    if not os.path.exists(path):
        raise ToolError("Нет файла или каталога: %s" % path)
    if a.get("project"):
        _check_project(a["project"], allow_portfolio=False)
    reports, out = _captured(ingest.run, [path], a.get("project"), bool(a.get("dry_run")),
                             False, None, a.get("root"))
    ok = [r for r in reports if not r.get("error")]
    summary = "Файлов %d · записей %d · предложений %d%s" % (
        len(ok), sum(len(r["records"]) for r in ok), sum(len(r["proposals"]) for r in ok),
        " (пробный прогон — ничего не записано)" if a.get("dry_run") else "")
    return (out + "\n\n" + summary).strip()


def t_tree(a):
    if a.get("project"):
        _check_project(a["project"])
    return tree.render_text(a.get("project"), depth=int(a.get("depth") or 4))


def t_projects(a):
    P = _projects()
    if not P:
        return "Портфель пуст."
    return "\n".join("%s — %s · %s · %s" % (k, p.get("name", k), p.get("status", ""), p.get("path", ""))
                     for k, p in sorted(P.items()))


HANDLERS = {"tt_context": t_context, "tt_recall": t_recall, "tt_search": t_search,
            "tt_remember": t_remember, "tt_forget": t_forget, "tt_status": t_status,
            "tt_inbox": t_inbox, "tt_apply": t_apply, "tt_reject": t_reject,
            "tt_ingest": t_ingest, "tt_tree": t_tree, "tt_projects": t_projects}

INSTRUCTIONS += "\n6. Начни с tt_work op=queue: там явные запросы запуска с доски. До реализации разложи новый запрос через op=plan: root, критерии, размер, риск и зависимости. Затем next/packet; выбери рекомендованную модель, соблюдай бюджет. Принимай claim только на фактической модели; finish требует доказательств проверок. Не запускай повторно уже принятую задачу. Каталог и измерения: op=models."
TOOLS.append({"name": "tt_work", "title": "Планирование и исполнение задач",
    "description": "Доски и экономное исполнение: list, queue, models, next, packet; plan (project, tasks), focus (projects), update (ref, changes), enqueue (ref), claim (ref, run, worker, model), finish/fail/cancel (ref, run, evidence, tokens). complete (ref, evidence) закрывает вручную проверенную задачу без активного запуска; нужны все acceptance_done. Начни с queue/next; packet содержит модель, бюджет и критерии. Запуск с доски ставит запрос в очередь, claim подтверждает реальное начало.",
    "inputSchema": _schema({"op": STR, "project": STR, "ref": STR, "run": STR,
        "worker": STR, "model": STR, "evidence": STR, "tokens": {"type":"integer"},
        "tasks": {"type":"array", "items":{"type":"object"}},
        "projects": {"type":"array", "items":STR}, "changes":{"type":"object"}}, ["op"]),
    "annotations": dict(WRITE, idempotentHint=False, title="Планирование и исполнение")})
def t_work(a):
    import workflow
    return json.dumps(workflow.action(a), ensure_ascii=False)
HANDLERS['tt_work'] = t_work


def _validate(tool, args):
    """Проверка входа по схеме инструмента: обязательные поля, типы, лишние поля.
    Ошибка входа — ошибка выполнения, ассистент увидит её и поправит вызов."""
    schema = tool["inputSchema"]
    if not isinstance(args, dict):
        raise ToolError("arguments должны быть объектом")
    missing = [k for k in schema.get("required", []) if args.get(k) in (None, "")]
    if missing:
        raise ToolError("Не хватает полей: %s" % ", ".join(missing))
    props = schema.get("properties", {})
    extra = [k for k in args if k not in props]
    if extra:
        raise ToolError("Неизвестные поля: %s" % ", ".join(extra))
    types = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
    for k, v in args.items():
        want = props[k].get("type")
        if want and v is not None:
            py = types.get(want)
            if py and (not isinstance(v, py) or (want == "integer" and isinstance(v, bool))):
                raise ToolError("Поле %s должно быть %s" % (k, want))
            if "enum" in props[k] and v not in props[k]["enum"]:
                raise ToolError("Поле %s: допустимо %s" % (k, ", ".join(props[k]["enum"])))


def call_tool(name, args):
    tool = next(t for t in TOOLS if t["name"] == name)
    try:
        _validate(tool, args or {})
        text = HANDLERS[name](args or {})
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:
        # Трассировка — в stderr для журнала клиента; ассистенту — суть.
        traceback.print_exc(file=sys.stderr)
        return {"content": [{"type": "text", "text": "Сбой инструмента: %s: %s" % (type(e).__name__, e)}],
                "isError": True}


# ---------------------------------------------------------------- протокол

class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Server:
    def __init__(self):
        self.legacy_version = None       # задаётся рукопожатием initialize

    def capabilities(self):
        return {"tools": {"listChanged": False}}

    def era(self, params):
        """Какими правилами обслуживать запрос. Возвращает (современный ли, версия)."""
        meta = (params or {}).get("_meta") or {}
        version = meta.get(META + "protocolVersion")
        if version is not None:
            if version not in SUPPORTED:
                raise RpcError(-32022, "Unsupported protocol version",
                               {"supported": SUPPORTED, "requested": version})
            if META + "clientCapabilities" not in meta:
                raise RpcError(-32602, "Missing required _meta field: %sclientCapabilities" % META)
            return True, version
        return False, self.legacy_version

    def handle(self, msg):
        """Одно сообщение -> ответ или None (для уведомлений)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        if "id" not in msg:
            return None                  # уведомления (initialized, cancelled) ответа не требуют
        rid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        try:
            if method == "initialize":
                return _result(rid, self.initialize(params), modern=False)
            if method == "server/discover":
                self.era(params)         # проверит версию, если клиент её прислал
                return _result(rid, {"supportedVersions": SUPPORTED, "capabilities": self.capabilities(),
                                     "instructions": INSTRUCTIONS}, modern=True, cacheable=True)
            modern, _ = self.era(params)
            if method == "ping":
                return _result(rid, {}, modern)
            if method == "tools/list":
                return _result(rid, {"tools": TOOLS}, modern, cacheable=True)
            if method == "tools/call":
                name = params.get("name")
                if name not in TOOL_NAMES:
                    raise RpcError(-32602, "Unknown tool: %s" % name)
                return _result(rid, call_tool(name, params.get("arguments") or {}), modern)
            raise RpcError(-32601, "Method not found: %s" % method)
        except RpcError as e:
            return _error(rid, e.code, e.message, e.data)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return _error(rid, -32603, "Internal error: %s" % type(e).__name__)

    def initialize(self, params):
        want = params.get("protocolVersion")
        # Наследные правила: поддерживаем версию клиента — отвечаем ей же, иначе своей последней.
        self.legacy_version = want if want in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {"protocolVersion": self.legacy_version, "capabilities": self.capabilities(),
                "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}


def _result(rid, result, modern, cacheable=False):
    if modern:
        result = dict(result, resultType="complete")
        if cacheable:
            result.update(CACHE_HINTS)
        result["_meta"] = dict(result.get("_meta") or {}, **{META + "serverInfo": SERVER_INFO})
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def serve(stdin=None, stdout=None):
    """Цикл чтения: одна строка — одно сообщение; пакеты (массивы) наследных версий тоже."""
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    server = Server()

    def send(obj):
        out.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        out.flush()

    while True:
        line = inp.readline()
        if not line:
            break                        # клиент закрыл stdin — штатное завершение
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception:
            send(_error(None, -32700, "Parse error"))
            continue
        if isinstance(msg, list):
            replies = [r for r in (server.handle(m) for m in msg) if r is not None]
            if replies:
                send(replies)
        else:
            reply = server.handle(msg)
            if reply is not None:
                send(reply)
    return 0


if __name__ == "__main__":
    sys.exit(serve())
