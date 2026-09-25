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
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
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

    def test_prose_is_left_alone(self):
        for text in ("Ключ: значение — это пара в словаре.", "токен: единица текста",
                     "секрет: успеха в регулярности", "sk-learn — библиотека",
                     "Пароль должен быть длиннее 12 символов."):
            with self.subTest(text=text):
                out, found = self.clean(text)
                self.assertEqual((out, found), (text, []))

    def test_idempotent(self):
        once, _ = self.clean("пароль: hunter22 и " + ANT)
        twice, found = self.clean(once)
        self.assertEqual(once, twice)
        self.assertEqual(found, [])


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
