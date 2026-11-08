# -*- coding: utf-8 -*-
"""Вычистка ключей и паролей при приёме заметок (БЕЗ-05)."""
import os
import unittest

from base import DataCase, TOOLS  # noqa: F401
import mdparse as md

# Ключи собираются из частей, чтобы сам файл теста не выглядел как утечка
# для сканеров — и для гейта портфеля.
ANT = "sk-" + "ant-" + "api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
ORT = "sk-" + "or-v1-" + "0123456789abcdef0123456789abcdef"
OAI = "sk-" + "proj-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1"
GHP = "gh" + "p_" + "A" * 36
HFT = "hf" + "_" + "b" * 34
TGM = "123456789" + ":" + "AAH" + "x" * 32
AIZ = "AI" + "za" + "S" * 35


class Redact(unittest.TestCase):
    def clean(self, text):
        return md.redact_secrets(text)

    def test_known_formats(self):
        for label, key in (("ключ Anthropic", ANT), ("ключ OpenRouter", ORT), ("ключ OpenAI", OAI),
                           ("токен GitHub", GHP), ("токен HuggingFace", HFT),
                           ("токен Telegram", TGM), ("ключ Google", AIZ)):
            with self.subTest(label=label):
                out, found = self.clean("перед %s после" % key)
                self.assertNotIn(key, out)
                self.assertIn("[скрыто: %s]" % label, out)
                self.assertEqual(found, [label])

    def test_private_key_block(self):
        head, tail = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
        block = head + "\nMIIEow\nabc\n" + tail
        out, found = self.clean("ключ:\n" + block + "\nконец")
        self.assertNotIn("MIIEow", out)
        self.assertIn("конец", out)
        self.assertIn("приватный ключ", found)

    def test_password_in_connection_url_keeps_user(self):
        out, found = self.clean("postgres://admin:S3cr3t!pass@db.local:5432/app")
        self.assertEqual(out, "postgres://admin:[скрыто]@db.local:5432/app")
        self.assertEqual(found, ["пароль в адресе"])

    def test_key_value_lines(self):
        out, _ = self.clean("пароль: qwerty\nAPI_KEY=abc123def456\ntoken: 'x9-y8-z7'")
        self.assertNotIn("qwerty", out)
        self.assertNotIn("abc123def456", out)
        self.assertNotIn("x9-y8-z7", out)
        self.assertIn("пароль: [скрыто]", out)

    def test_password_list_in_backticks(self):                         # БЕЗ-05
        out, found = self.clean("логины `admin` / `manager`, пароли `Admin123!` / `Manager123!` — демо")
        self.assertNotIn("Admin123!", out)
        self.assertNotIn("Manager123!", out)
        self.assertIn("логины `admin` / `manager`", out)                 # логины остаются
        self.assertEqual(found, ["пароль", "пароль"])

    def test_prose_is_left_alone(self):
        for text in ("Ключ: значение — это пара в словаре.", "токен: единица текста",
                     "секрет: успеха в регулярности", "sk-learn — библиотека",
                     "Пароль должен быть длиннее 12 символов.",
                     "проверяет её и пароль в `DATABASE_URL`",
                     "пароли `POSTGRES_PASSWORD` и `JWT_SECRET_KEY` перегенерированы"):
            with self.subTest(text=text):
                out, found = self.clean(text)
                self.assertEqual((out, found), (text, []))

    def test_idempotent(self):
        once, _ = self.clean("пароль: hunter22 и " + ANT)
        twice, found = self.clean(once)
        self.assertEqual(once, twice)
        self.assertEqual(found, [])


class RepoScanner(unittest.TestCase):
    """Сканер репозиториев: тревога «секрет в коде» только на похожее на секрет."""

    def hit(self, text):
        import collect_repos as cr
        label, rx = next((l, r) for l, r in cr.SECRET_RX if l == "секрет в коде")
        return cr.secret_hit(label, rx, text)

    def test_env_variable_name_is_not_a_secret(self):
        # Строки собраны из частей: иначе сам этот файл выглядел бы для сканера как утечка.
        self.assertFalse(self.hit("ENV_" + "SECRET = " + '"' + "BYBIT_API_" + "SECRET" + '"'))

    def test_literal_value_is_flagged(self):
        self.assertTrue(self.hit("SUPERADMIN_" + "PASSWORD = " + '"' + "Qw3rty!" + "2026x" + '"'))

    def test_one_real_value_among_names_is_flagged(self):
        text = ("api_" + "key = 'OPENAI_API_KEY'\n" + "pass" + "word = " + '"' + "hunter2" + "hunter2" + '"')
        self.assertTrue(self.hit(text))


class ReviewedSuspects(DataCase):
    """Вердикт «проверено глазами» помнится, пока файл не изменился."""

    def setUp(self):
        super().setUp()
        import collect_repos
        self.cr = collect_repos
        self.P = self.projects()["projects"]
        self.repo = self.P["alpha-api"]["path"]
        self.write("fake = 1\n")

    def write(self, extra):
        with open(os.path.join(self.repo, "creds.py"), "w", encoding="utf-8") as f:
            f.write("pass" + "word = " + '"' + "not-a-real-" + "one-123" + '"\n' + extra)

    def scan(self):
        return self.cr.scan_repo("alpha-api", self.P["alpha-api"])

    def test_suspect_carries_digest(self):
        [s] = self.scan()["secrets_suspect"]
        self.assertEqual((s["file"], s["kind"]), ("creds.py", "секрет в коде"))
        self.assertRegex(s["digest"], r"^[0-9a-f]{16}$")

    def test_reviewed_suspect_is_quiet_until_file_changes(self):
        r = self.scan()
        [s] = r["secrets_suspect"]
        project = dict(self.P["alpha-api"], security={"reviewed_suspects": [
            {"file": s["file"], "digest": s["digest"], "verdict": "поддельное значение в тесте"}]})
        self.assertEqual(self.cr.active_suspects(r, project), [])
        self.write("fake = 2\n")                                         # файл изменился — снова под подозрением
        self.assertEqual(len(self.cr.active_suspects(self.scan(), project)), 1)

    def test_worktree_copies_are_ignored(self):
        r = {"secrets_suspect": [{"file": ".claude/worktrees/x/creds.py", "kind": "секрет в коде", "digest": "1"}]}
        self.assertEqual(self.cr.active_suspects(r, {}), [])


class IngestRedacts(DataCase):
    def test_nothing_secret_reaches_memory_or_inbox(self):              # БЕЗ-05
        self.projects()
        p = self.note("keys.md", "---\nproject: gamma-voice\n---\n# Ключи моделей\n\n"
                                 "Для распознавания нужен ключ HuggingFace: " + HFT + "\n\n"
                                 "## Задачи\n\n- [ ] Ротировать ключ " + ANT + " до релиза\n")
        reps, _ = self.quiet(self.t.ingest.run, [p], quiet=True)
        rep = reps[0]
        self.assertEqual(sorted(set(rep["redacted"])), ["ключ Anthropic", "токен HuggingFace"])
        blob = ""
        for f in self.memory_files():
            with open(os.path.join(self.data, "memory", f), encoding="utf-8") as fh:
                blob += fh.read()
        with open(os.path.join(self.data, "inbox.json"), encoding="utf-8") as fh:
            blob += fh.read()
        with open(os.path.join(self.data, "memory", "index.json"), encoding="utf-8") as fh:
            blob += fh.read()
        self.assertNotIn(HFT, blob)
        self.assertNotIn(ANT, blob)
        self.assertIn("[скрыто: токен HuggingFace]", blob)
        with open(p, encoding="utf-8") as fh:
            self.assertIn(HFT, fh.read())                                # заметку пользователя не трогаем


if __name__ == "__main__":
    unittest.main()
