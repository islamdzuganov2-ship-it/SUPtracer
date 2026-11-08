"""Резервная копия и восстановление (TT-107; ТЗ TT3-006, E0 п.2, §9 п.1)."""
import json
import os
import tempfile
from pathlib import Path

from base import DataCase
import backup as B


class BackupRestore(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        d = Path(self.data)
        self.jwrite('comments.json', {'comments': [dict(id='C-1', text='Ёлка — «кавычки» и \\u0000', status='new')]})
        self.jwrite('workflow.json', dict(schema=1, runs=[], settings=dict(autonomy='step')))
        (d / 'memory' / 'sub').mkdir(parents=True, exist_ok=True)
        (d / 'memory' / 'mem-a.md').write_bytes('---\ntitle: А\n---\nтекст\r\nс CRLF\n'.encode('utf-8'))
        (d / 'memory' / 'sub' / 'mem-b.md').write_bytes(b'\x00\xffbinary-ish')
        (d / 'memory' / 'index.json').write_text('{"records": []}', encoding='utf-8')
        (d / 'usage.json').write_text('{}', encoding='utf-8')                    # собираемый — не копируется
        (d / 'owner.key').write_bytes(os.urandom(32))                           # ключ владельца — никогда
        (d / 'memory' / 'owner.key').write_bytes(b'x')
        self.dest = tempfile.mkdtemp(prefix='tt-bk-', dir=self.tmp)

    def test_restore_is_byte_identical(self):
        b = B.create(self.data, self.dest)
        self.assertEqual(B.verify(b), [])
        files = B.manifest(b)['files']
        self.assertIn('projects.json', files); self.assertIn('memory/sub/mem-b.md', files)
        for skip in ('usage.json', 'owner.key', 'memory/owner.key'):
            self.assertNotIn(skip, files)
        target = tempfile.mkdtemp(prefix='tt-rs-', dir=self.tmp)
        self.assertEqual(B.restore(b, target)['restored'], len(files))
        for rel in files:
            self.assertEqual((Path(self.data) / rel).read_bytes(), (Path(target) / rel).read_bytes(), rel)
        self.assertEqual(B.compare(b, target), [])

    def test_tampering_detected_and_restore_refused(self):
        b = B.create(self.data, self.dest)
        (b / 'projects.json').write_text('{"projects": {}}', encoding='utf-8')
        (b / 'memory' / 'mem-a.md').unlink()
        (b / 'evil.py').write_text('print(1)', encoding='utf-8')
        bad = B.verify(b)
        self.assertEqual(sorted(x.split(':')[0] for x in bad), ['изменён', 'лишний файл', 'нет файла'])
        with self.assertRaisesRegex(ValueError, 'повреждена'):
            B.restore(b, tempfile.mkdtemp(dir=self.tmp))

    def test_restore_does_not_clobber_later_changes(self):
        b = B.create(self.data, self.dest)
        p = Path(self.data) / 'projects.json'
        later = json.loads(p.read_text(encoding='utf-8')); later['changed_after_backup'] = True
        p.write_text(json.dumps(later), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'изменены после копии 1 файлов \\(projects.json\\)'):
            B.restore(b, self.data)
        self.assertIn('changed_after_backup', p.read_text(encoding='utf-8'))
        out = B.restore(b, self.data, force=True)
        self.assertEqual(out['overwritten'], ['projects.json'])
        self.assertNotIn('changed_after_backup', p.read_text(encoding='utf-8'))

    def test_drill_and_manifest(self):
        r = B.drill(self.data, self.dest)
        self.assertTrue(r['ok'], r); self.assertEqual(r['mismatched'], [])
        m = B.manifest(r['backup'])
        self.assertEqual(m['schemas']['workflow.json'], 1)
        self.assertFalse(any(Path(self.dest).glob('*.partial')))                 # недописанной копии не осталось
        self.assertTrue(str(r['backup']).startswith(self.dest))

    def test_default_location_outside_repository(self):
        root = B.default_root().resolve()
        repo = Path(B.__file__).resolve().parents[1]
        self.assertNotIn(str(repo).lower(), str(root).lower())
