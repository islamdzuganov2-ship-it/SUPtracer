# -*- coding: utf-8 -*-
"""Общая основа тестов: изолированный каталог данных на каждый тест (ИНТ-01, КАЧ-01).

Модули системы вычисляют пути к данным при импорте, поэтому после смены
TASKTREKER_DATA их нужно перезагрузить. importlib.reload переисполняет модуль
в том же объекте, так что ссылки между модулями (ingest.mem и т. п.) остаются
верными и видят новые пути.
"""
import contextlib
import copy
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

# Порядок важен: сначала то, от чего зависят остальные.
MODULES = ["mdparse", "memory", "ingest", "inbox", "tree", "context", "obsidian", "estimate"]

_READY = {"basis": "тестовые данные"}

FIXTURE = {
    "schema": 1,
    "size_calibration": {
        "S": {"label": "малая", "commits": 1},
        "M": {"label": "средняя", "commits": 3},
        "L": {"label": "крупная", "commits": 8},
        "XL": {"label": "очень крупная", "commits": 20},
    },
    "projects": {
        "alpha-api": {
            "name": "Сервис Альфа",
            "tagline": "API партнёров с ролями и кешем ответов",
            "tier": 1,
            "status": "активная разработка",
            "goal": "Выдать API партнёрам",
            "goal_long": "Партнёры получают данные через API с разграничением ролей.",
            "architecture": {"stack": "Python FastAPI PostgreSQL", "modules": "эндпоинты, роли, кеш"},
            "done": ["Эндпоинты чтения работают", "Роли партнёров проверяются"],
            "readiness": dict(functional=60, production=30, docs=50, tests=40, security=35, **_READY),
            "security": {"open_critical": ["КОД — пароль лежит в конфиге открытым текстом"]},
            "backlog": [
                {"id": "AL-01", "title": "Добавить кеш ответов эндпоинтов", "size": "M",
                 "priority": "🔴", "status": "queued", "why": "каждый запрос собирается заново"},
                {"id": "AL-02", "title": "Поправить опечатку в README", "size": "S",
                 "priority": "🟡", "status": "done", "why": "мелочь"},
            ],
            "next_actions": ["Кеш ответов", "Роли на всех эндпоинтах"],
        },
        "beta-tool": {
            "name": "Утилита Бета",
            "tagline": "Конвертер отчётов CSV в Excel",
            "tier": 3,
            "status": "работает",
            "goal": "Собирать отчёты",
            "architecture": {"stack": "Python openpyxl"},
            "done": ["Читает CSV"],
            "readiness": dict(functional=85, production=60, docs=20, tests=0, security=70, **_READY),
            "backlog": [],
        },
        "gamma-voice": {
            "name": "Гамма",
            "tagline": "Распознавание речи на локальной модели",
            "aliases": ["Gamma Voice", "Голос"],
            "tier": 2,
            "status": "каркас",
            "goal": "Расшифровка записей",
            "architecture": {"stack": "Python whisper CUDA"},
            "done": [],
            "readiness": dict(functional=10, production=0, docs=10, tests=0, security=50, **_READY),
            "backlog": [],
        },
    },
}


def load_tools():
    ns = {}
    for name in MODULES:
        mod = sys.modules.get(name)
        ns[name] = importlib.reload(mod) if mod else importlib.import_module(name)
    return types.SimpleNamespace(**ns)


class DataCase(unittest.TestCase):
    """Каждый тест получает свой каталог: data/ для системы, notes/ для заметок,
    и по каталогу на проект — пути проектов должны существовать."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tt-test-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self._env = os.environ.get("TASKTREKER_DATA")
        os.environ["TASKTREKER_DATA"] = self.data
        self.t = load_tools()

    def tearDown(self):
        if self._env is None:
            os.environ.pop("TASKTREKER_DATA", None)
        else:
            os.environ["TASKTREKER_DATA"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ данные

    def projects(self, mutate=None):
        """Записать projects.json с настоящими путями и вернуть его."""
        d = copy.deepcopy(FIXTURE)
        for key, p in d["projects"].items():
            p["path"] = os.path.join(self.tmp, "repos", key).replace("\\", "/")
            os.makedirs(p["path"], exist_ok=True)
        if mutate:
            mutate(d)
        self.jwrite("projects.json", d)
        return d

    def jwrite(self, name, obj):
        with open(os.path.join(self.data, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def jread(self, name, default=None):
        p = os.path.join(self.data, name)
        if not os.path.exists(p):
            return default
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def note(self, rel, text):
        p = os.path.join(self.tmp, "notes", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def quiet(self, fn, *a, **kw):
        """Вызвать функцию, поймав её печать. Возвращает (результат, вывод)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = fn(*a, **kw)
        return res, buf.getvalue()

    def memory_files(self):
        d = os.path.join(self.data, "memory")
        return sorted(f for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []
