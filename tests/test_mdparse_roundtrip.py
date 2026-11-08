"""Фронтматтер: строка после записи и чтения остаётся той же строкой (TT-103).

Заголовок записи памяти «да» читался как True — индекс памяти падал TypeError, а с ним
tt_remember и pm.py remember: память портфеля переставала писаться.
"""
import os
from base import DataCase
import mdparse as md
import memory


class RoundTripTests(DataCase):
    AMBIGUOUS = ['да', 'Нет', 'true', 'yes', 'no', '42', '-7', '3.5', 'null', '~', 'None', '[a, b]', "'q'"]

    def test_ambiguous_strings_survive_dump_and_parse(self):
        for s in self.AMBIGUOUS + ['обычный заголовок', 'Итог: готово', '#тег', '- пункт']:
            fm, _ = md.parse_frontmatter(md.dump_frontmatter(dict(title=s)) + '\nтело')
            self.assertEqual(fm['title'], s, s)

    def test_plain_strings_stay_unquoted(self):
        self.assertEqual(md._q('обычный заголовок'), 'обычный заголовок')

    def test_real_values_keep_types(self):
        fm, _ = md.parse_frontmatter(md.dump_frontmatter(dict(pin=True, n=3, tags=['a', 'b'])))
        self.assertEqual((fm['pin'], fm['n'], fm['tags']), (True, 3, ['a', 'b']))

    def test_old_unquoted_record_reads_title_as_text_and_index_builds(self):
        # Файл в старом виде, как его записал импорт до исправления: title без кавычек.
        memory.ensure_dir()
        path = memory.record_path('mem-old-da')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('---\nid: mem-old-da\nkind: запрос\nproject: портфель\ntitle: да\nstatus: активна\n---\nтело записи\n')
        rec = memory.read(path)
        self.assertEqual(rec['title'], 'да')
        idx = memory.build_index()
        self.assertTrue(os.path.exists(path))
        self.assertIn('mem-old-da', str(idx))

    def test_saved_record_with_ambiguous_title_reads_back(self):
        rec = memory.new_record('да', 'суть', project='портфель', kind='решение')
        memory.save(rec)
        self.assertEqual(memory.read(memory.record_path(rec['id']))['title'], 'да')
