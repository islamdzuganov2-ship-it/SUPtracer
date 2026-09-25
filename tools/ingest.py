# -*- coding: utf-8 -*-
"""Приём входящих Markdown-файлов: разбор, классификация, раскладка по проектам.

Логика анализа живёт здесь, а не в голове ассистента. Это принципиально: если
разбирать входящие каждый раз заново рассуждением, это стоит токенов при каждом
запуске и даёт разный результат. Здесь правила записаны один раз и применяются
одинаково — ассистенту остаётся проверить спорное.

Что делает приём:
  1. Режет файл на разделы (раздел — единица смысла, файл целиком слишком крупный).
  2. Определяет вид каждого куска: запрос, решение, требование, риск, факт, итог.
  3. Определяет проект — по фронтматтеру, тегам, ссылкам и словарю проекта.
  4. Пишет записи в память (data/memory) с деревом: корень файла -> разделы.
  5. Задачи (чекбоксы и пункты планов) кладёт предложениями в data/inbox.json.

Предложения не попадают в projects.json сами. Бэклог — ручной слой: от него зависят
оценка затрат и гейт слияния, поэтому решение «это задача проекта» принимает человек
или ассистент командой apply, а не разборщик текста.

  py tools/ingest.py <файл|каталог> [...] [--project X] [--root <id запроса>] [--dry-run] [--quiet]
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mdparse as md
import memory as mem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TASKTREKER_DATA") or os.path.join(ROOT, "data")   # ИНТ-01
INBOX = os.path.join(DATA, "inbox.json")

SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules", "__pycache__", ".venv",
             ".claude", "templates", "шаблоны"}

# --------------------------------------------------------------- классификация

# Основа слова пишется с \w* — «опасн\w*» ловит «опасно», «опасность», «опасный».
# Без хвоста основа с \b на конце совпадала только сама с собой, и «утечки»,
# «приёмки», «подсистема» не ловились вообще (нашлось на размеченном наборе, ПРМ-09).
MARKERS = [
    ("запрос", 3.0, r"\b(нужно|необходимо|требуется|хочу|хотелось бы|прошу|реализовать|сделать|"
                    r"добавить|внедрить|доработать|переделать|нужна|нужен|надо|задача:|"
                    r"implement|add support|we need|should build)\b"),
    ("решение", 3.0, r"\b(решили|решено|принято решение|выбрали|выбран\w*|остановились на|"
                     r"вместо этого|будем использовать|вердикт|договорились|decided|chose)\b"),
    ("требование", 2.6, r"\b(должен|должна|должно|должны|обязан\w*|обязательн\w*|требовани\w*|"
                        r"критери\w*|приёмк\w*|приемк\w*|тз|спецификаци\w*|must|shall|acceptance)\b"),
    ("риск", 3.0, r"\b(риск\w*|опасн\w*|уязвим\w*|утечк\w*|секрет\w*|парол\w*|ключ доступа|"
                  r"пада\w*|ломает|деградац\w*|инцидент\w*|блокер\w*|критичн\w*|отвал\w*|"
                  r"vulnerab\w*|leak\w*|breach\w*)\b"),
    ("итог", 2.2, r"\b(итог\w*|результат\w*|сделано|готово|выполнено|завершен\w*|закрыт[аоы]?|"
                  r"запущен\w*|выкачен\w*|релиз\w*|done|shipped|released)\b"),
    ("факт", 1.8, r"(\d[\d\s.,]*\s*(строк|коммит|файл|кб|мб|гб|тб|тыс|млн|млрд|%|процент|мс|сек|мин|"
                  r"час|ч\b|дн|недел|км|м\b|см|мм|кг|шт|раз|дб|ггц|мгц|кгц|гц|вт|руб|₽|\$|долл|токен)|"
                  r"\bверсия\s*\d|\bv\d+\.\d+)"),
    ("источник", 2.0, r"(https?://|\bисточник\w*|\bссылк\w*|\bдокументаци\w*)"),
]

SIZE_RULES = [
    ("XL", r"\b(подсистем\w*|с нуля|архитектур\w*|переписать|миграци\w*|платформ\w*|контур\w*|"
           r"интеграци[яю] с|новый модуль|rewrite|from scratch)\b"),
    ("L", r"\b(сквозн\w*|бэкенд и фронтенд|реализовать|внедрить|разработать|спроектировать|"
          r"конвейер\w*|пайплайн\w*|синхронизаци\w*|end-to-end)\b"),
    ("S", r"\b(поправить|починить|переименовать|опечатк\w*|добавить кнопк\w*|добавить пункт|"
          r"обновить текст|документ\w*|readme|подпис\w*|описани\w*|typo|rename|tweak)\b"),
]

PRIORITY_RULES = [
    ("🔴", r"\b(критичн\w*|срочн\w*|блокер\w*|немедленно|ломает|пада\w*|безопасност\w*|утечк\w*|"
           r"critical|urgent|blocker|p0)\b"),
    ("🟢", r"\b(когда-нибудь|потом|идея|было бы неплохо|низкий приоритет|nice to have|someday)\b"),
]

TASK_SECTION_RE = re.compile(
    r"(задач|todo|to do|бэклог|backlog|план|шаг|что сделать|next|следующ|доработ)", re.I)
IMPERATIVE_RE = re.compile(
    r"^(сделать|добавить|убрать|вынести|перенести|реализовать|починить|исправить|проверить|"
    r"написать|настроить|обновить|удалить|заменить|внедрить|разобрать|описать|покрыть|"
    r"собрать|подключить|оформить|задокументировать|спроектировать|ускорить|оптимизировать|"
    r"автоматизировать|интегрировать|мигрировать|отрефакторить|переписать|разделить|объединить|"
    r"завести|поднять|ввести|зашифровать|ротировать|вывести|дописать|доработать|закрыть|"
    r"поправить|очистить|унифицировать|перевести|назначить|выделить)\b", re.I)


def classify(title, body, fm=None, tags=()):
    """Вид куска текста. Явное указание во фронтматтере или теге сильнее догадки."""
    explicit = str((fm or {}).get("kind") or (fm or {}).get("вид") or "").strip().lower()
    if explicit in mem.KINDS:
        return explicit, 1.0, "указано во фронтматтере"
    for t in tags:
        if t in mem.KINDS:
            return t, 0.95, "указано тегом #%s" % t
    text = ((title or "") + "\n" + (body or "")).lower()
    head = (title or "").lower()
    best, score, why = "заметка", 0.0, "нет явных признаков"
    for kind, w, rx in MARKERS:
        hits = len(re.findall(rx, text))
        if not hits:
            continue
        s = w * (1 + min(hits, 4) * 0.35)
        if re.search(rx, head):
            s *= 1.6                      # признак в заголовке весит больше
        if s > score:
            best, score, why = kind, s, "признаки вида «%s» в тексте" % kind
    conf = max(0.3, min(0.95, score / 8.0)) if score else 0.3
    return best, round(conf, 2), why


def guess_size(text):
    for size, rx in SIZE_RULES:
        if re.search(rx, text, re.I):
            return size
    return "M"


def guess_priority(text):
    for pr, rx in PRIORITY_RULES:
        if re.search(rx, text, re.I):
            return pr
    return "🟡"


# --------------------------------------------------------------- проекты

def load_projects():
    p = os.path.join(DATA, "projects.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("projects", {})


def _base(p):
    return os.path.basename((p.get("path") or "").replace("\\", "/").rstrip("/"))


# Проверка на случайность совпадений со словарём. Модель простая: слова заметки —
# будто бы случайная выборка из рабочего запаса языка в VOCAB_BASE основ; тогда
# со словарём из V слов заметка из N слов случайно совпадёт примерно в N·V/VOCAB_BASE
# словах. Признаком считается только совпадение в CHANCE_FACTOR раз больше случайного.
# Калибровка — на размеченном наборе и на реальном хранилище (ТЗ-01, Т14): короткие
# заметки о проекте дают превышение в десятки раз, случайные — в 2–3 раза.
VOCAB_BASE = 5000
CHANCE_FACTOR = 5.0


def _strings(v):
    """Все строки внутри вложенной структуры — полный текст описания проекта."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x)


def build_vocab(projects):
    """Словарь проекта — по чему его узнать в тексте — и вес каждого слова.

    Состав — короткие поля, которые проект называют: имя, псевдонимы, каталог,
    слоган, цель, стек, сделанное, названия задач. Длинные описания (цель надолго,
    оценка, обоснования, «почему» задач) в состав не идут: это рассуждения, а не приметы.

    Вес — по тому, у скольких проектов слово встречается во ВСЁМ их описании:
    у одного — 1, у двух — 1/2, у трёх — 1/3. Раньше вес считался по самим словарям,
    и общее слово («база», «оценка», «LLM») становилось «уникальным», стоило ему
    попасть в словарь одного проекта, хотя в описаниях оно есть у всех. На реальном
    хранилище крупные проекты от этого становились воронками: туда уезжали профиль,
    поиск работы и заметки проектов, которых портфель не ведёт."""
    vocab, df = {}, {}
    for key, p in projects.items():
        aliases = [str(a) for a in (p.get("aliases") or [])]
        parts = [key, key.replace("-", " "), p.get("name", ""), p.get("tagline", ""),
                 p.get("goal", "")] + aliases
        arch = p.get("architecture") or {}
        parts += [str(arch.get(f, "")) for f in ("stack", "style")]
        parts += [str(x) for x in (p.get("done") or []) if isinstance(x, str)]
        parts += [str(b.get("title") or "") for b in (p.get("backlog") or []) if isinstance(b, dict)]
        base = _base(p)
        parts += [base, base.replace("-", " ").replace("_", " ")]
        words = md.word_set(" ".join(parts))
        words |= {md.stem(key.lower()), md.stem(base.lower())}
        vocab[key] = words
        for w in words | md.word_set(" ".join(_strings(p))):
            df[w] = df.get(w, 0) + 1
    weight = {w: 1.0 / c for w, c in df.items()}
    # Ключ, имя каталога и псевдонимы — опознавательные знаки, их вес не размывается
    for key, p in projects.items():
        marks = [key.lower(), _base(p).lower()] + [str(a).lower() for a in (p.get("aliases") or [])]
        for w in marks:
            for s in md.word_set(w) | {md.stem(w)}:
                if s and len(s) >= 3 and df.get(s, 0) <= 1:
                    weight[s] = 3.0
    return vocab, weight


def name_index(projects):
    """Все написания проекта: ключ, имя, псевдонимы (поле aliases), имя каталога —
    в нижнем регистре и слагом. Одно место, чтобы фронтматтер, теги, ссылки и путь
    в хранилище узнавали проект одинаково."""
    idx = {}
    for k, p in projects.items():
        for n in [k, p.get("name") or "", _base(p)] + list(p.get("aliases") or []):
            n = str(n).strip()
            if not n:
                continue
            idx.setdefault(n.lower(), k)
            s = md.slugify(n)
            if s and s != "zapis":
                idx.setdefault(s, k)
    return idx


def by_name(value, idx):
    v = str(value or "").strip()
    return (idx.get(v.lower()) or idx.get(md.slugify(v))) if v else None


def by_prefix(text, idx):
    """Имя проекта в начале названия папки или файла: «CashMash Scalper — стратегия»,
    «SatLink - ТЗ». Совпадение только целым словом и только для имён от 4 символов,
    иначе «Гамма-лучи» ушли бы в проект «Гамма»."""
    s = md.slugify(text)
    best = None
    for name, k in idx.items():
        if len(name) < 4 or not re.fullmatch(r"[a-z0-9-]+", name):
            continue
        if s == name or s.startswith(name + "-"):
            if best is None or len(name) > len(best[0]):
                best = (name, k)
    return best[1] if best else None


def by_folder(parts, idx):
    """ПРМ-08: папка заметки в хранилище. Глубокая папка точнее верхней:
    «Проекты/CellWatch/Итоги.md» говорит о CellWatch, а не о «Проектах»."""
    parts = [p for p in (parts or []) if p]
    for f in reversed(parts[:-1]):
        k = by_name(f, idx) or by_prefix(f, idx)
        if k:
            return k, 0.95, "папка «%s» в хранилище" % f
    return None


def by_filename(parts, idx):
    parts = [p for p in (parts or []) if p]
    if not parts:
        return None
    stem = os.path.splitext(parts[-1])[0]
    k = by_name(stem, idx) or by_prefix(stem, idx)
    return (k, 0.9, "имя файла «%s»" % stem) if k else None


# Папки, в которых каждая подпапка — отдельный проект. Настраивается в
# data/obsidian.json (import.project_folders); без настройки — общепринятые имена.
DEFAULT_PROJECT_FOLDERS = ["Проекты/*", "Projects/*"]


def configured_project_folders():
    """Папки проектов из настройки обмена с хранилищем, иначе общепринятые."""
    try:
        with open(os.path.join(DATA, "obsidian.json"), encoding="utf-8") as f:
            v = (json.load(f).get("import") or {}).get("project_folders")
    except (OSError, ValueError, AttributeError):
        v = None
    return [str(x) for x in v] if isinstance(v, list) else list(DEFAULT_PROJECT_FOLDERS)


def project_folder(path_parts, patterns=None):
    """Имя папки проекта, в которой лежит заметка, или None.

    «Проекты/*» — каждая подпапка «Проектов» — проект; «LLM-Administrator» — эта
    папка сама проект. Файл прямо в «Проектах» в папке проекта не лежит."""
    parts = [p for p in (path_parts or []) if p][:-1]            # только папки
    low = [p.lower() for p in parts]
    best = None
    for pat in (patterns if patterns is not None else DEFAULT_PROJECT_FOLDERS):
        segs = [s for s in str(pat).replace("\\", "/").split("/") if s]
        if not segs:
            continue
        if segs[-1] == "*":
            head = [s.lower() for s in segs[:-1]]
            if len(low) > len(head) and low[:len(head)] == head:
                cand = (len(head) + 1, parts[len(head)])
            else:
                continue
        else:
            head = [s.lower() for s in segs]
            if low[:len(head)] == head:
                cand = (len(head), parts[len(head) - 1])
            else:
                continue
        if best is None or cand[0] > best[0]:
            best = cand
    return best[1] if best else None


def named_projects(text, projects):
    """Сколько раз каждый проект назван в тексте прямо — ключом, именем, псевдонимом,
    каталогом. Только целые слова и имена от 4 символов: короткие совпадают случайно."""
    low = (text or "").lower()
    found = {}
    for k, p in projects.items():
        names = {str(n).strip().lower() for n in
                 [k, p.get("name") or "", _base(p)] + list(p.get("aliases") or [])}
        names = sorted((n for n in names if len(n) >= 4), key=len, reverse=True)
        if not names:
            continue
        rx = r"(?<![\w-])(?:%s)(?![\w-])" % "|".join(re.escape(n) for n in names)
        n = len(re.findall(rx, low))
        if n:
            found[k] = n
    return found


def by_tags(tags, idx):
    """Тег проекта — решение автора. Теги нескольких проектов — заметка о портфеле."""
    found = []
    for t in tags or ():
        k = by_name(t, idx)
        if k and k not in found:
            found.append(k)
    if len(found) == 1:
        return found[0], 0.9, "проект указан тегом"
    if found:
        return "портфель", 0.6, "теги нескольких проектов: %s" % ", ".join(found)
    return None


def by_mentions(text, links, projects, idx):
    """Проект, на который заметка ссылается или который называет прямо.

    Один — он. Несколько — заметка о портфеле (профиль, обзор недели), если только
    один не упомянут много чаще остальных: в ТЗ проекта разок назван сосед — это
    всё ещё ТЗ проекта. Викиссылка считается и как ссылка, и как имя в тексте:
    ссылка — более сильное упоминание."""
    counts = dict(named_projects(text, projects))
    linked = set()
    for l in links or ():
        k = by_name(l, idx)
        if k:
            counts[k] = counts.get(k, 0) + 1
            linked.add(k)
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    (top, n1), n2 = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0)
    if n2 == 0:
        return top, (0.85 if top in linked else 0.8), \
            ("ссылка на заметку проекта" if top in linked else "проект назван в тексте")
    if n1 >= 3 * n2:
        return top, 0.8, "проект упомянут %d раз, другие — не больше %d" % (n1, n2)
    return "портфель", 0.6, "упомянуты несколько проектов: %s" % ", ".join(k for k, _ in ranked)


def route(text, fm, tags, links, projects, vocab, weight, default=None, path_parts=None, idx=None,
          project_folders=None):
    """Куда положить кусок. Возвращает (ключ проекта, уверенность 0..1, обоснование).

    ПРМ-03: признаки по убыванию силы — аргумент, фронтматтер, папка проекта из портфеля,
    папка чужого проекта, имя файла, тег, викиссылка, проект, названный в тексте,
    словарь проекта.

    Словарь — последний и самый слабый признак. Ошибка в нём опаснее отказа:
    заметка, ушедшая в чужой проект, молча засоряет его память, а неуверенная
    остаётся в «портфеле» и попадает в отчёт на проверку. Поэтому словарь
    не запускается для заметок из папок проектов, требует двух разных совпадений,
    заметного превышения над случайными и отрыва от второго места."""
    if default and default in projects:
        return default, 1.0, "проект задан аргументом"
    idx = idx if idx is not None else name_index(projects)

    for field in ("project", "проект", "проекты"):
        v = (fm or {}).get(field)
        for cand in ([v] if isinstance(v, str) else list(v or [])):
            k = by_name(cand, idx)
            if k:
                return k, 1.0, "проект указан во фронтматтере"
    hit = by_folder(path_parts, idx)
    if hit:
        return hit
    folder = project_folder(path_parts, project_folders)
    if folder:
        # Папка проекта, которого нет в портфеле: это другой проект, а не «неизвестно».
        # Угадывать по словам тут нельзя — чужой проект похож на свои общими словами.
        return "портфель", 0.9, "папка проекта «%s» — такого проекта в портфеле нет" % folder
    hit = by_filename(path_parts, idx) or by_tags(tags, idx) or by_mentions(text, links, projects, idx)
    if hit:
        return hit

    words = md.word_set(text)
    scores, hits = {}, {}
    for key, vw in vocab.items():
        hit = words & vw
        hits[key] = hit
        scores[key] = sum(weight.get(w, 0.5) for w in hit)
    if not scores:
        return "портфель", 0.2, "проектов в системе нет"
    best = max(scores, key=scores.get)
    top = scores[best]
    rest = sorted((v for k, v in scores.items() if k != best), reverse=True)
    second = rest[0] if rest else 0.0
    if top < 2.0 or len(hits[best]) < 2:
        return "портфель", 0.25, "нет уверенного совпадения ни с одним проектом"
    # Совпадения бывают и случайными: чем больше словарь проекта и длиннее заметка,
    # тем больше общих слов найдётся у любых двух текстов. Совпадений должно быть
    # заметно больше, чем дал бы случай, — иначе это не признак, а шум.
    chance = len(words) * len(vocab[best]) / float(VOCAB_BASE)
    if len(hits[best]) < CHANCE_FACTOR * chance:
        return "портфель", 0.3, "совпадений со словарём %s %d — для такой длины это почти случайность" % (
            best, len(hits[best]))
    margin = (top - second) / top if top else 0
    if margin < 0.25:
        return "портфель", 0.3, "совпадения с несколькими проектами почти равны"
    conf = max(0.3, min(0.95, 0.45 + margin * 0.5 + min(top, 12) / 40))
    return best, round(conf, 2), "совпало по словарю проекта (%.1f против %.1f)" % (top, second)


def route_section(text, tags, links, projects, idx, file_route, default=None):
    """ПРМ-13: проект раздела. У заметки один предмет, поэтому раздел наследует проект файла.

    Своим признакам раздел следует в двух случаях: у него явный тег проекта — это
    решение автора; или сам файл не привязан (нет признаков либо в нём названы
    несколько проектов, как в недельном обзоре) — тогда раздел с викиссылкой или
    прямо названным проектом говорит о своём. Словарь для разделов не работает:
    в коротком тексте общие слова решают всё, и раздел заметки о чужом проекте
    уезжал в проект с похожими словами."""
    proj, conf, _ = file_route
    if default and default in projects:
        return default, 1.0, "проект задан аргументом"
    hit = by_tags(tags, idx)
    if hit and hit[0] != "портфель":
        return hit
    if conf < 0.8:
        hit = by_mentions(text, links, projects, idx)
        if hit and hit[0] != "портфель":
            return hit
    return proj, conf, "унаследован от файла"


# --------------------------------------------------------------- входящие

def load_inbox():
    if not os.path.exists(INBOX):
        return {"schema": 1, "generated_at": "", "next_id": 1, "items": []}
    with open(INBOX, encoding="utf-8") as f:
        return json.load(f)


def save_inbox(box):
    box["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(INBOX, "w", encoding="utf-8") as f:
        json.dump(box, f, ensure_ascii=False, indent=2)


def norm_title(s):
    return re.sub(r"[^\wа-яё ]+", "", (s or "").lower()).strip()


def known_titles(projects, box):
    """Что уже есть — чтобы повторный приём того же файла не плодил задачи."""
    seen = set()
    for key, p in projects.items():
        for b in p.get("backlog", []):
            seen.add((key, norm_title(b.get("title"))))
        for d in p.get("done", []) or []:
            seen.add((key, norm_title(d)))
    for it in box.get("items", []):
        seen.add((it.get("project"), norm_title(it.get("title"))))
    return seen


# --------------------------------------------------------------- приём файла

def iter_files(paths):
    """(полный путь, путь относительно переданного каталога). Относительный путь
    нужен раскладке: папки внутри каталога заметок — признак проекта (ПРМ-08)."""
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p) and p.lower().endswith((".md", ".markdown", ".txt")):
            yield p, os.path.basename(p)
        elif os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames
                                     if d.lower() not in SKIP_DIRS and not d.startswith("."))
                for fn in sorted(filenames):
                    if fn.lower().endswith((".md", ".markdown")):
                        full = os.path.join(dirpath, fn)
                        yield full, os.path.relpath(full, p).replace("\\", "/")


MAX_RECORD_TOKENS = 1500     # ПРМ-10: записи одного порядка по размеру


def _blocks(text):
    """Абзацы с учётом блоков кода: блок кода неделим, пустая строка внутри него
    абзац не разрывает."""
    out, cur, fence = [], [], None
    for line in (text or "").splitlines():
        m = md.CODE_FENCE_RE.match(line)
        if m:
            fence = None if fence and line.strip().startswith(fence) else (fence or m.group(1))
        if not line.strip() and fence is None:
            if cur:
                out.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def _pack(units, limit, sep):
    out, cur = [], []
    for u in units:
        if cur and md.est_tokens(sep.join(cur + [u])) > limit:
            out.append(sep.join(cur))
            cur = []
        cur.append(u)
    if cur:
        out.append(sep.join(cur))
    return out


def split_body(body, limit=MAX_RECORD_TOKENS):
    """ПРМ-10: раздел длиннее limit токенов -> части. Сначала по подзаголовкам 4–6
    уровня, внутри — по абзацам, абзац длиннее limit — по строкам (таблица, список).
    Неделимое — одна строка или блок кода — остаётся целым. Возвращает [(подзаголовок, текст)]."""
    if md.est_tokens(body) <= limit:
        return [("", body)]
    out = []
    for part in re.split(r"(?m)^(?=#{4,6}\s)", body):
        if not part.strip():
            continue
        m = re.match(r"#{4,6}\s+(.+)", part)
        sub = m.group(1).strip() if m else ""
        if md.est_tokens(part) <= limit:
            out.append((sub, part.strip()))
            continue
        units = []
        for b in _blocks(part):
            if md.est_tokens(b) > limit and not md.CODE_FENCE_RE.match(b):
                units += _pack(b.splitlines(), limit, "\n")
            else:
                units.append(b)
        out += [(sub, chunk) for chunk in _pack(units, limit, "\n\n")]
    return out


def rel(path):
    """Путь для поля source. Внутри репозитория — относительный, снаружи — полный:
    ссылка вида ../../../.. нечитаема и через месяц никуда не ведёт."""
    try:
        r = os.path.relpath(path, ROOT).replace("\\", "/")
        return r if not r.startswith("..") else os.path.abspath(path).replace("\\", "/")
    except ValueError:
        return os.path.abspath(path).replace("\\", "/")


def ingest_file(path, projects, vocab, weight, box, seen, default_project=None,
                source_label=None, dry=False, attach_root=None, rel_path=None, prior=None,
                project_folders=None):
    """Один файл -> записи памяти и предложения задач. Отчёт — что куда легло.

    attach_root подвешивает документ под существующий запрос (ПРМ-04): так ТЗ,
    написанное в ответ на запрос, растёт в дереве из этого запроса, а не отдельно.
    rel_path — путь заметки внутри хранилища или каталога заметок (ПРМ-08),
    project_folders — где в хранилище лежат папки проектов."""
    idx = name_index(projects)
    path_parts = (rel_path or os.path.basename(path)).replace("\\", "/").split("/")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {"file": rel(path), "error": str(e), "records": [], "proposals": []}

    # БЕЗ-05: секреты вычищаются до разбора — ни запись, ни предложение, ни индекс
    # не должны их увидеть. Исходную заметку не трогаем: это файл пользователя.
    raw, secrets = md.redact_secrets(raw)
    fm, body = md.parse_frontmatter(raw)
    all_tags = md.tags(body, fm)
    links = md.wikilinks(body) + list(fm.get("links") or [])
    src = source_label or rel(path)
    secs = md.sections(body, 1, 3)

    # Заголовок файла: фронтматтер важнее H1, H1 важнее имени файла. Раздел, из
    # которого взят заголовок, становится вступлением корневой записи, а не отдельной.
    title_fm = str(fm.get("title") or "").strip()
    tops = [s for s in secs if s["level"] == 1]
    intro_sec = None
    if title_fm:
        file_title = title_fm
        intro_sec = next((s for s in tops if md.slugify(s["title"]) == md.slugify(title_fm)), None)
    elif tops:
        intro_sec = tops[0]
        file_title = intro_sec["title"]
    else:
        file_title = os.path.splitext(os.path.basename(path))[0]
    if intro_sec is None:
        intro_sec = next((s for s in secs if not s["title"]), None)

    intro = (intro_sec or {}).get("body", "").strip()
    gist = "\n".join([intro] + [s["title"] for s in secs if s["title"]])

    proj, conf, why = route(file_title + "\n" + body, fm, all_tags, links,
                            projects, vocab, weight, default_project, path_parts, idx,
                            project_folders)
    # Заметка из папки проекта, которого нет в портфеле: запоминаем, какого именно, —
    # по этому полю видно, сколько знания лежит у проектов, которые портфель не ведёт.
    foreign = project_folder(path_parts, project_folders) if proj == "портфель" and conf >= 0.8 else None

    kind, kconf, kwhy = classify(file_title, gist, fm, all_tags)
    # Вступление длиннее предела не обрезается молча: первая часть — тело корня,
    # остальные — продолжения под ним (ПРМ-10).
    intro_parts = split_body(intro) if intro else [("", md.first_paragraph(body, 900) or file_title)]
    root_body = intro_parts[0][1]
    root = mem.new_record(
        file_title, root_body, project=proj, kind=kind, tags=all_tags,
        links=[l for l in links if l], source=src,
        created=str(fm.get("created") or fm.get("date") or "")[:10] or mem.today(),
        confidence="%.2f" % conf,
        parent=attach_root or "", root=attach_root or "",
        id=mem.make_id(file_title, proj, src))
    root["routing"] = why
    if foreign:
        root["foreign_project"] = foreign
    records = [root]
    tree_root = attach_root or root["id"]
    for i, (sub, text) in enumerate(intro_parts[1:], 2):
        t = "%s — вступление (часть %d из %d)" % (file_title, i, len(intro_parts))
        records.append(mem.new_record(
            t, text, project=proj, kind=kind, tags=all_tags, source=src,
            parent=root["id"], root=tree_root, confidence="%.2f" % conf,
            id=mem.make_id(t, proj, src)))

    for s in secs:
        if s is intro_sec or not s["title"] or not s["body"].strip():
            continue
        if len(s["body"].strip()) < 60 and not md.tasks(s["body"]):
            continue
        if _is_task_list(s):
            continue          # список задач уезжает в предложения, дублировать в памяти нечего
        stags = md.tags(s["body"], None) or []
        skind, _, _ = classify(s["title"], s["body"], None, stags)
        sproj, sconf, swhy = route_section(s["title"] + "\n" + s["body"], stags,
                                           md.wikilinks(s["body"]), projects, idx,
                                           (proj, conf, why), default_project)
        parts = split_body(s["body"])
        for i, (sub, text) in enumerate(parts, 1):
            if len(parts) == 1:
                title = s["title"]
            elif sub:
                title = "%s — %s" % (s["title"], sub)
                if sum(1 for x, _ in parts if x == sub) > 1:
                    title += " (ч. %d)" % i
            else:
                title = "%s (часть %d из %d)" % (s["title"], i, len(parts))
            rec = mem.new_record(
                title, text, project=sproj, kind=skind,
                tags=sorted(set(stags) | set(all_tags)),
                links=md.wikilinks(text), source="%s#%s" % (src, s["title"]),
                parent=root["id"], root=tree_root, confidence="%.2f" % sconf,
                id=mem.make_id(title, sproj, src))
            rec["routing"] = swhy
            records.append(rec)
    if foreign:
        for r in records:
            if r["project"] == "портфель":
                r["foreign_project"] = foreign

    proposals = []
    for s in secs + ([{"title": "", "body": body}] if not secs else []):
        in_task_section = bool(TASK_SECTION_RE.search(s.get("title") or ""))
        holder = next((r for r in records if r["source"].endswith("#" + (s.get("title") or ""))
                       and s.get("title")), root)
        # Задача уходит в проект своего раздела: в недельном обзоре раздел «CellWatch»
        # предлагает задачи CellWatch, а не «портфелю».
        if holder is not root:
            tproj, tconf = holder["project"], float(holder["confidence"])
        elif s is intro_sec or not s.get("title"):
            tproj, tconf = proj, conf
        else:
            tproj, tconf, _ = route_section(s["title"] + "\n" + s["body"],
                                            md.tags(s["body"], None) or [], md.wikilinks(s["body"]),
                                            projects, idx, (proj, conf, why), default_project)
        target = tproj if tconf >= 0.5 and tproj in projects else "портфель"
        for t in md.tasks(s.get("body") or ""):
            if t["done"] or t["dropped"]:
                continue
            proposals.append(_proposal(t["text"], target, holder, src, s.get("title"), "чекбокс"))
        if in_task_section:
            for line in (s.get("body") or "").splitlines():
                b = re.match(r"^\s*[-*+]\s+(?!\[)(.{8,200})$", line)
                if b and IMPERATIVE_RE.match(b.group(1).strip()):
                    proposals.append(_proposal(b.group(1).strip(), target,
                                               holder, src, s.get("title"), "пункт плана"))

    fresh = []
    for p in proposals:
        k = (p["project"], norm_title(p["title"]))
        if k in seen:
            continue
        seen.add(k)
        fresh.append(p)

    kept_manual, retired = [], []
    if not dry:
        for r in records:
            old = mem.get(r["id"]) if os.path.exists(mem.record_path(r["id"])) else None
            if old and old.get("status") != "активна" and old.get("retired") == "вручную":
                kept_manual.append(r["id"])   # решение человека повторный приём не отменяет
                continue
            mem.save(r)
        # ПРМ-11: разделы, которых в заметке больше нет, устаревают — но не удаляются.
        new_ids = {r["id"] for r in records}
        for rid in (prior or {}).get(src, []):
            if rid not in new_ids and mem.retire(rid, "раздела больше нет в заметке", manual=False):
                retired.append(rid)
        for p in fresh:
            p["id"] = "IN-%d" % box.get("next_id", 1)
            box["next_id"] = box.get("next_id", 1) + 1
            box.setdefault("items", []).append(p)

    return {"file": rel(path), "project": proj, "confidence": conf, "why": why,
            "foreign_project": foreign, "kept_manual": kept_manual, "retired": retired,
            "kind": kind, "kind_why": kwhy, "records": records, "proposals": fresh,
            "redacted": secrets}


def _is_task_list(sec):
    """Раздел, который целиком состоит из пунктов плана: содержимого для памяти в нём нет."""
    body = (sec.get("body") or "").strip()
    if not body:
        return False
    lines = [l for l in body.splitlines() if l.strip()]
    bullets = [l for l in lines if re.match(r"^\s*[-*+]\s+", l)]
    return bool(TASK_SECTION_RE.search(sec.get("title") or "")) and \
        len(bullets) >= max(1, len(lines) * 0.6)


def _proposal(text, project, holder, src, section, origin):
    text = re.sub(r"\s*\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r" \1", text).strip()
    text = re.sub(r"\s*#\S+", "", text).strip(" .;")
    return {
        "id": "", "project": project, "type": "бэклог",
        "title": text[:180],
        "size": guess_size(text), "priority": guess_priority(text),
        "why": "Пришло из заметки «%s»%s — %s." % (
            os.path.basename(src), (", раздел «%s»" % section) if section else "", origin),
        "source": src + (("#" + section) if section else ""),
        "root": holder.get("root") or holder.get("id", ""),
        "memory": holder.get("id", ""),
        "status": "new", "applied_to": None,
        "created": mem.today(),
    }


def source_base(source):
    """Источник записи без раздела: «заметка.md#Раздел» -> «заметка.md»."""
    return str(source or "").split("#", 1)[0]


def active_by_source():
    """Активные записи, сгруппированные по файлу-источнику, — снимок до приёма.
    По нему видно, какие разделы исчезли из заметки (ПРМ-11)."""
    out = {}
    # Без перестройки индекса: пробный прогон не должен ничего писать на диск.
    for r in mem.load_index(rebuild_if_missing=False).get("records", []):
        if r.get("status") == "активна" and r.get("source"):
            out.setdefault(source_base(r["source"]), []).append(r["id"])
    return out


def retire_sources(srcs, why):
    """Все активные записи перечисленных файлов-источников -> «устарела».
    Файлы записей остаются. Так obsidian.py снимает записи заметок, которые
    удалили или переименовали в хранилище. Возвращает {источник: [id]}."""
    prior = active_by_source()
    done = {}
    for src in srcs:
        for rid in prior.get(src, []):
            if mem.retire(rid, why, manual=False):
                done.setdefault(src, []).append(rid)
    if done:
        mem.build_index()
    return done


def run(paths, default_project=None, dry=False, quiet=False, source_label=None,
        attach_root=None, rel_hint=None):
    """Файлы и каталоги -> приём. rel_hint — путь заметки внутри хранилища, когда
    файл передан по одному: без него раскладка не увидела бы папку проекта."""
    items = [(f, rel_hint or rel_path, source_label) for f, rel_path in iter_files(paths)]
    return run_items(items, default_project, dry, quiet, attach_root)


def run_items(items, default_project=None, dry=False, quiet=False, attach_root=None, projects=None,
              project_folders=None):
    """Пакетный приём: [(путь, путь в хранилище, метка источника)].

    Словарь проектов, входящие и снимок памяти готовятся один раз, индекс памяти
    перестраивается один раз в конце. Хранилище на сотни заметок иначе стоило бы
    сотни полных перестроений индекса. projects подменяет портфель — так оценка
    раскладки проверяет правила на своём размеченном портфеле. project_folders без
    значения берётся из настройки хранилища."""
    projects = projects if projects is not None else load_projects()
    folders = project_folders if project_folders is not None else configured_project_folders()
    vocab, weight = build_vocab(projects)
    box = load_inbox()
    seen = known_titles(projects, box)
    prior = active_by_source()
    reports = []
    for f, rel_path, label in items:
        r = ingest_file(f, projects, vocab, weight, box, seen, default_project,
                        label, dry, attach_root, rel_path, prior, folders)
        reports.append(r)
        if quiet:
            continue
        if r.get("error"):
            print("  ! %s — %s" % (r["file"], r["error"]))
            continue
        print("  %s -> %s (%.0f%%, %s)" % (r["file"], r["project"], r["confidence"] * 100, r["why"]))
        print("      записей %d · вид корня «%s» · задач предложено %d" % (
            len(r["records"]), r["kind"], len(r["proposals"])))
        if r.get("redacted"):
            print("      вычищено секретов: %d (%s) — в самой заметке они остались" % (
                len(r["redacted"]), ", ".join(sorted(set(r["redacted"])))))
        if r.get("retired"):
            print("      устарело записей: %d — этих разделов в заметке больше нет" % len(r["retired"]))
        if r.get("kept_manual"):
            print("      оставлено выведенными вручную: %d" % len(r["kept_manual"]))
    if not dry and reports:
        save_inbox(box)
        mem.build_index()
    return reports


def main():
    args = sys.argv[1:]
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 1
    default = None
    if "--project" in args:
        i = args.index("--project")
        if i + 1 < len(args):
            default = args[i + 1]
            paths = [p for p in paths if p != default]
    attach = None
    if "--root" in args:
        i = args.index("--root")
        if i + 1 < len(args):
            attach = args[i + 1]
            paths = [p for p in paths if p != attach]
    reports = run(paths, default, "--dry-run" in args, "--quiet" in args, attach_root=attach)
    ok = [r for r in reports if not r.get("error")]
    print("\nФайлов принято: %d · записей %d · предложений в inbox %d" % (
        len(ok), sum(len(r["records"]) for r in ok), sum(len(r["proposals"]) for r in ok)))
    weak = [r for r in ok if r["confidence"] < 0.5]
    if weak:
        print("Проект определён неуверенно (%d) — проверь: %s" % (
            len(weak), ", ".join(r["file"] for r in weak[:5])))
    leaky = [r for r in ok if r.get("redacted")]
    if leaky:
        print("Ключи и пароли в заметках (%d) — в память не попали, но из заметок их стоит убрать: %s" % (
            len(leaky), ", ".join(r["file"] for r in leaky[:5])))
    foreign = sorted({r["foreign_project"] for r in ok if r.get("foreign_project")})
    if foreign:
        print("Папки проектов, которых нет в портфеле (заметки в «портфеле»): %s" % ", ".join(foreign))
    if "--dry-run" in sys.argv:
        print("Пробный прогон: ничего не записано.")
    else:
        print("Дальше: py tools/pm.py inbox — разобрать предложения.")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
