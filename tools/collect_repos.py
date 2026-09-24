# -*- coding: utf-8 -*-
"""Технический срез репозиториев: git, объём кода, тесты, стек, признаки ИБ.

Читает список проектов из data/projects.json (поле path) и снимает факты прямо
с диска. Ничего не выдумывает: чего нет — того нет. -> data/repos.json
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".gradle", ".idea", ".vscode", "Library",
    "Temp", "Logs", "obj", "bin", "target", "archive", "site-packages",
    ".next", ".nuxt", "coverage", "htmlcov", "Obj", "Build", "UserSettings",
}
CODE_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".kt": "Kotlin", ".kts": "Kotlin", ".java": "Java",
    ".cs": "C#", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".c": "C", ".cpp": "C++", ".h": "C/C++ header",
    ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell", ".vue": "Vue",
}
DOC_EXT = {".md", ".rst", ".adoc"}

SECRET_PATTERNS = [
    ("приватный ключ", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("ключ Anthropic", r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    ("ключ OpenAI", r"sk-[A-Za-z0-9]{32,}"),
    ("токен Telegram", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("секрет в коде", r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*[\x22\x27][^\x22\x27{$\s]{8,}[\x22\x27]"),
]
SECRET_RX = [(label, re.compile(rx)) for label, rx in SECRET_PATTERNS]
SECRET_SCAN_EXT = {".py", ".ts", ".tsx", ".js", ".kt", ".java", ".cs", ".go",
                   ".yml", ".yaml", ".json", ".env", ".ini", ".cfg", ".toml",
                   ".sh", ".ps1", ".properties", ".xml"}

# Коды требований: латиница и кириллица, «ТЗ-05», «УК-42», «SEC-01», «БТ-123», «D-037».
# Ищем в документах — чтобы посчитать требования, и в коде — чтобы посчитать трассируемость.
REQ_CODE = re.compile(r"\b([A-ZА-ЯЁ]{1,5}[-‑–]\d{1,3})\b")

# Не требования, а внешние стандарты, форматы и обозначения — их коды выглядят так же.
NOT_REQUIREMENTS = {
    "ISO", "IEC", "ГОСТ", "RFC", "CVE", "CWE", "ITU", "P", "R", "EN", "DIN", "ANSI",
    "IEEE", "UTF", "SHA", "MD", "AES", "RSA", "ECDSA", "HTTP", "HTTPS", "TLS", "SSL",
    "API", "UI", "UX", "CSS", "HTML", "JSON", "XML", "SQL", "USB", "PCI", "GPU", "CPU",
    "RAM", "SSD", "HDD", "LTE", "NR", "GSM", "UMTS", "CDMA", "WCAG", "ARIA", "NDA",
    "ПП", "ФЗ", "СП", "СНиП", "ТУ", "ОКВЭД", "ИНН", "КПП",
}

# Признаки незакрытых мест в документации: вопрос не решён, решение не принято.
OPEN_QUESTION = re.compile(
    r"(?i)(\bTBD\b|\bTODO\b|\[\?\]|\bно пока\b|требует уточнени|требует подтвержд|"
    r"требует проверк|остаётся открыт|остается открыт|открытый вопрос|не решен|не решён|"
    r"не определен|не определён|под вопросом|уточнить)")

# Структура документа: заголовки и таблицы отличают спецификацию от потока мыслей.
HEADING = re.compile(r"^#{1,6}\s+\S", re.M)
TABLE_ROW = re.compile(r"^\|.+\|\s*$", re.M)


def sh(args, cwd=None):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def walk(path, max_files=300000):
    n = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".worktrees")]
        for fn in filenames:
            yield os.path.join(dirpath, fn)
            n += 1
            if n >= max_files:
                return


def count_lines(fp):
    try:
        with open(fp, "rb") as f:
            return f.read().count(b"\n") + 1
    except OSError:
        return 0


def scan_repo(key, meta):
    path = meta.get("path") or ""
    res = {"key": key, "path": path, "exists": os.path.isdir(path)}
    if not res["exists"]:
        return res

    if os.path.isdir(os.path.join(path, ".git")):
        log1 = sh(["git", "log", "-1", "--format=%ad|%s", "--date=short"], path)
        first = sh(["git", "log", "--reverse", "--format=%ad", "--date=short"], path)
        res["git"] = {
            "commits": int(sh(["git", "rev-list", "--count", "HEAD"], path) or 0),
            "branch": sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], path),
            "branches": len([b for b in sh(["git", "branch", "-a"], path).splitlines() if b.strip()]),
            "first_commit": first.split("\n")[0] if first else "",
            "last_commit": log1.split("|")[0] if log1 else "",
            "last_subject": log1.split("|", 1)[1] if "|" in log1 else "",
            "dirty_files": len([l for l in sh(["git", "status", "--short"], path).splitlines() if l.strip()]),
            "remote": sh(["git", "remote", "get-url", "origin"], path),
        }
    else:
        res["git"] = None

    langs = Counter()
    loc = Counter()
    docs = doc_lines = test_files = total_files = todo = 0
    secrets = []
    weird = []
    # ТЗ: коды требований в документах и в коде, структура, незакрытые вопросы
    req_in_docs = Counter()
    req_in_code = set()
    headings = tables = open_questions = 0
    doc_mtimes = []
    code_mtimes = []
    for fp in walk(path):
        total_files += 1
        ext = os.path.splitext(fp)[1].lower()
        base = os.path.basename(fp).lower()
        try:
            rel = os.path.relpath(fp, path).replace("\\", "/")
        except ValueError:
            weird.append(fp)  # зарезервированные имена Windows (nul, con, aux...)
            continue
        is_code = ext in CODE_EXT
        if is_code:
            lang = CODE_EXT[ext]
            langs[lang] += 1
            loc[lang] += count_lines(fp)
            if (base.startswith("test_") or base.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts"))
                    or "/tests/" in rel or "/test/" in rel or "Tests" in rel):
                test_files += 1
        elif ext in DOC_EXT:
            docs += 1
            doc_lines += count_lines(fp)
        if is_code or ext in DOC_EXT:
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    txt = f.read(3_000_000)
            except OSError:
                continue
            try:
                mt = os.path.getmtime(fp)
                (code_mtimes if is_code else doc_mtimes).append(mt)
            except OSError:
                pass
            todo += len(re.findall(r"\b(TODO|FIXME|HACK|XXX|ЗАГЛУШКА)\b", txt))
            if is_code:
                req_in_code.update(REQ_CODE.findall(txt))
            else:
                req_in_docs.update(REQ_CODE.findall(txt))
                headings += len(HEADING.findall(txt))
                tables += len(TABLE_ROW.findall(txt))
                open_questions += len(OPEN_QUESTION.findall(txt))
            if ext in SECRET_SCAN_EXT:
                for label, rx in SECRET_RX:
                    if rx.search(txt):
                        secrets.append({"file": rel, "kind": label})
                        break

    res["code"] = {
        "files_total": total_files,
        "languages": dict(langs.most_common()),
        "loc": dict(loc.most_common()),
        "loc_total": sum(loc.values()),
        "test_files": test_files,
        "doc_files": docs,
        "doc_lines": doc_lines,
        "todo_markers": todo,
    }
    res["secrets_suspect"] = secrets[:40]
    res["weird_paths"] = weird[:20]

    # --- ТЗ: количество, структура и трассируемость ---
    # Схемой нумерации считаем префикс, встретившийся не меньше трёх раз: одиночное
    # «A-1» — это опечатка или ссылка на стандарт, а не система требований.
    def family(code):
        return re.split(r"[-‑–]", code, 1)[0]

    fams = Counter(family(c) for c in req_in_docs)
    real_fams = {f for f, n in fams.items() if n >= 3 and f not in NOT_REQUIREMENTS}
    reqs = {c for c in req_in_docs if family(c) in real_fams}
    traced = reqs & req_in_code

    # Свежесть — по git, а не по mtime: checkout переписывает время файлов,
    # и на свежеклонированном репозитории всё выглядит одинаково новым.
    if res["git"]:
        # --no-merges обязателен: коммит слияния затрагивает и доки, и код одновременно,
        # и без него обе даты всегда совпадают.
        newest_doc = sh(["git", "log", "-1", "--no-merges", "--format=%at", "--",
                         "*.md", "*.rst", "*.adoc"], path)
        newest_code = sh(["git", "log", "-1", "--no-merges", "--format=%at", "--",
                          "*.py", "*.ts", "*.tsx", "*.js", "*.kt", "*.java", "*.cs", "*.go"], path)
        newest_doc = int(newest_doc) if newest_doc.isdigit() else 0
        newest_code = int(newest_code) if newest_code.isdigit() else 0
        # Дисциплина документирования: доля содержательных коммитов, в которых
        # документация менялась вместе с кодом. Отвечает на вопрос, живут ли ТЗ
        # вместе с разработкой или дописываются задним числом.
        doc_commits = set(sh(["git", "log", "--no-merges", "--format=%H", "--",
                              "*.md", "*.rst", "*.adoc"], path).split())
        code_commits = set(sh(["git", "log", "--no-merges", "--format=%H", "--",
                               "*.py", "*.ts", "*.tsx", "*.js", "*.kt", "*.java",
                               "*.cs", "*.go"], path).split())
        both = doc_commits & code_commits
        doc_discipline = round(100.0 * len(both) / len(code_commits)) if code_commits else None
    else:
        newest_doc = max(doc_mtimes) if doc_mtimes else 0
        newest_code = max(code_mtimes) if code_mtimes else 0
        code_commits = both = ()
        doc_discipline = None
    res["spec"] = {
        "doc_files": docs,
        "doc_lines": doc_lines,
        "requirements": len(reqs),
        "requirement_families": sorted(real_fams),
        "traced_in_code": len(traced),
        "traceability_pct": round(100.0 * len(traced) / len(reqs)) if reqs else None,
        "headings": headings,
        "table_rows": tables,
        "open_questions": open_questions,
        "doc_to_code_ratio": round(doc_lines / max(1, sum(loc.values())), 3),
        "code_commits": len(code_commits),
        "commits_with_docs": len(both),
        "doc_discipline_pct": doc_discipline,
        "newest_doc": datetime.fromtimestamp(newest_doc, timezone.utc).strftime("%Y-%m-%d") if newest_doc else "",
        "newest_code": datetime.fromtimestamp(newest_code, timezone.utc).strftime("%Y-%m-%d") if newest_code else "",
        # Документация отстаёт от кода на столько дней. Отрицательное — доки свежее кода.
        "docs_lag_days": round((newest_code - newest_doc) / 86400) if (newest_doc and newest_code) else None,
    }

    def has(*names):
        return any(os.path.exists(os.path.join(path, n)) for n in names)

    env_tracked = False
    if res["git"]:
        tracked = sh(["git", "ls-files"], path)
        env_tracked = bool(re.search(r"(^|/)\.env$", tracked, re.M))
    res["hygiene"] = {
        "gitignore": has(".gitignore"),
        "env_example": has(".env.example", "ops/.env.example", "config/.env.example"),
        "env_in_git": env_tracked,
        "env_on_disk": has(".env", "ops/.env"),
        "ci": has(".github/workflows", ".gitlab-ci.yml", "azure-pipelines.yml", "Jenkinsfile"),
        "docker": has("docker-compose.yml", "Dockerfile", "dockerfile"),
        "lockfile": has("package-lock.json", "poetry.lock", "yarn.lock", "pnpm-lock.yaml",
                        "requirements.txt", "Gemfile.lock"),
        "license": has("LICENSE", "LICENSE.md", "LICENCE"),
        "readme": has("README.md", "readme.md"),
        "claude_md": has("CLAUDE.md"),
    }

    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    res["disk_bytes"] = total
    return res


def main():
    with open(os.path.join(ROOT, "data", "projects.json"), encoding="utf-8") as f:
        projects = json.load(f)["projects"]
    only = sys.argv[1:] or None
    out_path = os.path.join(ROOT, "data", "repos.json")
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "repos": {}}
    if only and os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            out["repos"] = json.load(f).get("repos", {})
    for key, meta in projects.items():
        if only and key not in only:
            continue
        sys.stderr.write("скан %s ...\n" % key)
        out["repos"][key] = scan_repo(key, meta)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    for k, r in out["repos"].items():
        if not r.get("exists"):
            print("  %-16s — каталога нет" % k)
            continue
        c, g = r["code"], (r["git"] or {})
        print("  %-16s LOC %7d · файлов %6d · тестов %4d · TODO %4d · коммитов %4s · подозр.секретов %d" % (
            k, c["loc_total"], c["files_total"], c["test_files"], c["todo_markers"],
            g.get("commits", "—"), len(r["secrets_suspect"])))
    print("-> " + out_path)


if __name__ == "__main__":
    main()
