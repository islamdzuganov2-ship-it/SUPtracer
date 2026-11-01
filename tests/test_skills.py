"""Скиллы ролей: собираются из спецификации конвейера и совпадают с ней (TT-70)."""
import os
from pathlib import Path
from unittest import mock
from base import DataCase
import pipeline as P
import skills


class SkillsTests(DataCase):
    def test_committed_skills_are_fresh(self):
        for role in P.ROLES:
            path = skills.OUT / skills.name(role) / 'SKILL.md'
            self.assertTrue(path.exists(), 'нет %s: py tools/pm.py skills build' % path)
            self.assertEqual(path.read_text(encoding='utf-8'), skills.render(role),
                             '%s устарел: py tools/pm.py skills build' % path)

    def test_skill_lists_every_dod_item_and_artifact(self):
        for role, spec in P.ROLES.items():
            text = skills.render(role)
            self.assertTrue(text.startswith('---\nname: %s\n' % skills.name(role)))
            for st in [s for s in (spec['stage'] if isinstance(spec['stage'], tuple) else (spec['stage'],)) if s]:
                for it in P.role_dod(role, st):
                    self.assertIn('`%s`' % it['id'], text, (role, it['id']))
                for it in P.DOR[st]:
                    self.assertIn(it['text'], text)
            for a in spec['artifacts']:
                self.assertIn(a['path'].split('/')[-1], text)

    def test_install_writes_both_clients_and_keeps_foreign(self):
        home = Path(self.tmp)
        (home/'.claude'/'skills'/'tt-qa').mkdir(parents=True)
        (home/'.claude'/'skills'/'tt-qa'/'SKILL.md').write_text('чужой скилл', encoding='utf-8')
        (home/'.codex').mkdir()
        with mock.patch.object(skills, 'targets', return_value=dict(claude=home/'.claude'/'skills', codex=home/'.codex'/'skills')):
            rep = skills.install()
            self.assertEqual((home/'.claude'/'skills'/'tt-qa'/'SKILL.md').read_text(encoding='utf-8'), 'чужой скилл')
            self.assertTrue((home/'.codex'/'skills'/'tt-teamlead'/'SKILL.md').exists())
            self.assertIn('skipped', rep)
            skills.uninstall()
            self.assertFalse((home/'.codex'/'skills'/'tt-teamlead').exists())
            self.assertTrue((home/'.claude'/'skills'/'tt-qa'/'SKILL.md').exists())
