import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infohub import config
from infohub.organize.codex_client import resolve_command


class CodexCommand(unittest.TestCase):
    def test_path_explicit_and_missing_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / 'Codex With Spaces.exe'
            executable.touch()
            self.assertEqual(resolve_command(str(executable)), str(executable.resolve()))
            with patch('shutil.which', return_value=str(executable)):
                self.assertEqual(resolve_command('custom-codex'), str(executable.resolve()))
            with self.assertRaisesRegex(FileNotFoundError, 'Settings'):
                resolve_command(str(Path(tmp) / 'missing.exe'))
            with patch.object(config, 'SETTINGS_FILE', Path(tmp) / 'settings.json'):
                saved = config.write_settings({'agent_tasks.codex_command':str(executable)})
                self.assertEqual(saved['agent_tasks.codex_command'], str(executable))
                self.assertEqual(config.write_settings({'agent_tasks.codex_command':''})['agent_tasks.codex_command'], 'codex')
                for invalid in (False, 'codex\nanything', str(executable) + ' app-server', str(Path(tmp)/'missing.exe')):
                    with self.assertRaises(ValueError):
                        config.write_settings({'agent_tasks.codex_command':invalid})

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop bundle discovery')
    def test_desktop_fallback_without_path_and_updated_bundle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'LOCALAPPDATA':tmp}), patch('shutil.which', return_value=None):
            root = Path(tmp) / 'OpenAI' / 'Codex' / 'bin'
            for name, stamp in [('old',100), ('new',200)]:
                path = root / name / 'codex.exe'
                path.parent.mkdir(parents=True)
                path.touch()
                os.utime(path, (stamp,stamp))
            self.assertEqual(resolve_command(), str((root/'new'/'codex.exe').resolve()))
            self.assertEqual(resolve_command(''), resolve_command())
            with self.assertRaises(FileNotFoundError):
                resolve_command(str(root/'deleted'/'codex.exe'))
            wrapper = Path(tmp)/'codex.cmd'
            wrapper.touch()
            with patch('shutil.which', return_value=str(wrapper)):
                self.assertTrue(resolve_command().endswith('codex.exe'))
                with self.assertRaises(FileNotFoundError):
                    resolve_command(str(wrapper))
