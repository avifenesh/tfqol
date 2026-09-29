from pathlib import Path
import tempfile
import unittest
from voiced.setup import enable_codex_accessibility


class SetupTests(unittest.TestCase):
    def test_launcher_override_preserves_arguments_actions_and_metadata(self):
        original='[Desktop Entry]\nName=My Codex\nExec=env MODE=personal /usr/bin/codex-desktop %u\n\n[Desktop Action new-window]\nExec=/usr/bin/codex-desktop --new-instance\n\n[Desktop Action update]\nExec=/usr/bin/codex-update-manager check-now\n'
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.desktop';source.write_text(original)
            target=Path(directory)/'user/codex-desktop.desktop'
            enable_codex_accessibility(source,target)
            content=target.read_text()
            self.assertIn('Name=My Codex',content)
            self.assertIn('Exec=env MODE=personal /usr/bin/codex-desktop --force-renderer-accessibility %u',content)
            self.assertIn('Exec=/usr/bin/codex-desktop --new-instance --force-renderer-accessibility',content)
            self.assertIn('Exec=/usr/bin/codex-update-manager check-now',content)
            enable_codex_accessibility(source,target)
            self.assertEqual(content,target.read_text())
            self.assertEqual(original,source.read_text())

    def test_existing_user_customization_is_preserved_and_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'codex-desktop.desktop'
            original='Name=Custom\nExec=/usr/bin/codex-desktop --ozone-platform=x11 %u\n'
            target.write_text(original)
            enable_codex_accessibility(Path(directory)/'absent',target)
            self.assertIn('--ozone-platform=x11',target.read_text())
            self.assertEqual(target.with_suffix('.desktop.voiced-backup').read_text(),original)


if __name__=='__main__':unittest.main()
