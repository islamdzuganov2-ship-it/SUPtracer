# -*- coding: utf-8 -*-
"""Разбор Markdown и Obsidian-разметки — общая основа для памяти, приёма и хранилища.

Только стандартная библиотека: система должна работать на голом Python без сети.
YAML здесь не полный, а ровно тот поднабор, который встречается во фронтматтере
заметок Obsidian: скаляры, списки блоком и в строку. Чего не знаем — кладём строкой,
а не роняем разбор: заметка пользователя важнее строгости парсера.
"""
import hashlib
import re
import unicodedata

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
TAG_RE = re.compile(r"(?:^|[\s(])#([^\s#,.;:!?()\[\]{}]{2,60})")
TASK_RE = re.compile(r"^\s*[-*+]\s+\[([ xX~/-])\]\s+(.+?)\s*$")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")

CYR_RE = re.compile(r"[\u0400-\u04FF]")


# ---------------------------------------------------------------- фронтматтер

def _scalar(v):
    v = v.strip()
    if not v:
        return ""
    if v[0] in "\"'" and len(v) > 1 and v[-1] == v[0]:
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
    low = v.lower()
    if low in ("true", "yes", "да"):
        return True
    if low in ("false", "no", "нет"):
        return False
    if low in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def parse_frontmatter(text):
    """Текст заметки -> (словарь фронтматтера, тело без него)."""
    m = FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    out, key = {}, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\s*-\s+", line) and key:
            cur = out.get(key)
            if not isinstance(cur, list):
                out[key] = [] if cur in ("", None) else [cur]
            out[key].append(_scalar(re.sub(r"^\s*-\s+", "", line)))
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            out[key] = _scalar(v)
    return out, text[m.end():]


def _q(v):
    s = str(v)
    bad = s != s.strip() or "\n" in s or ": " in s
    if not bad and s[:1] in ("[", "{", ">", "|", "&", "*", "!", "%", "@", "#", "-", "'", '"', "`"):
        bad = True
    if bad:
        return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return s


def dump_frontmatter(d):
    """Словарь -> блок фронтматтера. Порядок ключей сохраняется — так diff читаем."""
    lines = ["---"]
    for k, v in d.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, bool):
            lines.append("%s: %s" % (k, "true" if v else "false"))
        elif isinstance(v, (int, float)):
            lines.append("%s: %s" % (k, v))
        elif isinstance(v, (list, tuple)):
            lines.append("%s:" % k)
            lines += ["  - %s" % _q(x) for x in v]
        else:
            lines.append("%s: %s" % (k, _q(v)))
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- структура

def strip_code(body):
    """Тело без блоков кода — чтобы решётки и скобки внутри кода не ловились как теги."""
    out, fence = [], None
    for line in (body or "").splitlines():
        m = CODE_FENCE_RE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif line.strip().startswith(fence):
                fence = None
            continue
        if fence is None:
            out.append(line)
    return "\n".join(out)


def sections(body, min_level=1, max_level=3):
    """Тело -> разделы по заголовкам. Текст до первого заголовка идёт разделом без имени.

    Раздел — естественная единица смысла в заметке, поэтому память хранит именно
    разделы, а не файлы целиком: так запись получается одного порядка по размеру.
    """
    lines = (body or "").splitlines()
    out, cur = [], {"level": 0, "title": "", "lines": [], "line": 1}
    fence = None
    for i, line in enumerate(lines, 1):
        m = CODE_FENCE_RE.match(line)
        if m:
            fence = None if fence and line.strip().startswith(fence) else (fence or m.group(1))
        h = None if fence else HEAD_RE.match(line)
        if h and min_level <= len(h.group(1)) <= max_level:
            if cur["lines"] or cur["title"]:
                out.append(cur)
            cur = {"level": len(h.group(1)), "title": h.group(2).strip(), "lines": [], "line": i}
        else:
            cur["lines"].append(line)
    if cur["lines"] or cur["title"]:
        out.append(cur)
    for s in out:
        s["body"] = "\n".join(s["lines"]).strip()
        del s["lines"]
    return [s for s in out if s["body"] or s["title"]]


def wikilinks(text):
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(text or "")]


def tags(text, fm=None):
    out = set()
    for t in TAG_RE.finditer(strip_code(text or "")):
        tag = t.group(1)
        if not re.fullmatch(r"[\d/]+", tag):       # решётка перед числом — не тег
            out.add(tag.lower().lstrip("/"))
    for key in ("tags", "tag", "теги"):
        v = (fm or {}).get(key)
        if isinstance(v, str):
            out |= {x.strip().lstrip("#").lower() for x in re.split(r"[,\s]+", v) if x.strip()}
        elif isinstance(v, (list, tuple)):
            out |= {str(x).strip().lstrip("#").lower() for x in v if str(x).strip()}
    return sorted(out)


def tasks(body):
    """Чекбоксы Obsidian -> задачи. Пусто — открыта, x — закрыта, / и ~ — в работе."""
    out = []
    for i, line in enumerate(strip_code(body).splitlines(), 1):
        m = TASK_RE.match(line)
        if m:
            mark = m.group(1).lower()
            out.append({
                "done": mark == "x",
                "wip": mark in ("/", "~"),
                "dropped": mark == "-",
                "text": re.sub(r"\s+", " ", m.group(2)).strip(),
                "line": i,
            })
    return out


def first_paragraph(body, limit=400):
    """Первый содержательный абзац. Заголовки пропускаются: они уже есть в title,
    а нужна именно суть — по ней определяется вид записи и строится дайджест."""
    for chunk in re.split(r"\n\s*\n", strip_code(body or "").strip()):
        if HEAD_RE.match(chunk.strip().splitlines()[0] if chunk.strip() else ""):
            chunk = "\n".join(chunk.strip().splitlines()[1:])
        c = re.sub(r"\s+", " ", chunk).strip()
        c = re.sub(r"^[-*+>\s]+", "", c)
        if len(c) > 12:
            return c[:limit].strip()
    return re.sub(r"\s+", " ", strip_code(body or "")).strip()[:limit]


# ---------------------------------------------------------------- измерения

def est_tokens(text):
    """Оценка токенов. Кириллица дороже латиницы примерно вдвое — это важно:
    по-русски тот же смысл стоит больше, и бюджет брифинга считается по факту."""
    s = text or ""
    if not s:
        return 0
    cyr = len(CYR_RE.findall(s))
    ratio = cyr / max(1, len(s))
    per_token = 4.0 - 1.8 * ratio          # ~4 симв/токен латиница, ~2.2 кириллица
    return max(1, int(len(s) / per_token))


def digest_of(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


_TRANS = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(s, limit=60):
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = "".join(_TRANS.get(c, c) for c in s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:limit].rstrip("-") or "zapis")


# ---------------------------------------------------------------- слова

STOP = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне
было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас
опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам
без будто чего раз тоже себе под будет тогда кто этот того потому этого какой совсем ним здесь этом один
почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при об другой хоть после над больше тот
через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть
том нельзя такой им более всегда конечно всю между это эта наш нужно делать сделать также нибудь
the and for that with this from have has are was were will your you our not but all can any
one two who why how when where which into out off over under then than some such more most new old
""".split())


def stem(w):
    """Грубая обрезка русских окончаний. Не лингвистика, а сопоставление ключей:
    «дашборда» и «дашборд» должны попасть в одну корзину."""
    w = w.lower()
    if len(w) <= 4:
        return w
    for suf in ("ования", "ованию", "ами", "ями", "ого", "ему", "ому", "ыми", "ими", "ах", "ях",
                "ов", "ев", "ий", "ый", "ой", "ая", "яя", "ую", "юю", "ее", "ие", "ые", "ам", "ям",
                "ом", "ем", "ей", "ью", "ия", "а", "я", "ы", "и", "о", "е", "у", "ю", "ь", "й"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def keywords(text, limit=24):
    """Слова, по которым заметка сопоставляется с проектом — в виде корней."""
    words = re.findall(r"[0-9a-zA-Zа-яёА-ЯЁ_\-\.]{3,}", (text or "").lower())
    freq = {}
    for w in words:
        w = w.strip("-._")
        if len(w) < 3 or w in STOP or re.fullmatch(r"[\d.\-_]+", w):
            continue
        s = stem(w)
        freq[s] = freq.get(s, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def word_set(text):
    """Множество корней — для пересечения заметки с проектом."""
    out = set()
    for w in re.findall(r"[0-9a-zA-Zа-яёА-ЯЁ_\-]{3,}", (text or "").lower()):
        w = w.strip("-_")
        if len(w) >= 3 and w not in STOP and not re.fullmatch(r"[\d\-_]+", w):
            out.add(stem(w))
    return out
