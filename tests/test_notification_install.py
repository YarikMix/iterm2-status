"""Installer regressions using real child processes and temporary config files."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_install import installer, ROOT

try:
    import tomllib
except ImportError:
    tomllib = None


@unittest.skipUnless(tomllib, 'Optional notification configuration requires Python 3.11+')
class NotificationInstaller(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.paths = installer.locations(self.home, ROOT, {})
        self.paths['config'].parent.mkdir(parents=True)
        self.codex = self.home / 'codex'
        self.codex.write_text(
            '#!' + sys.executable + '\n'
            'import json,sys\nfrom pathlib import Path\n'
            f'root = Path({str(self.home)!r})\n'
            f'config = Path({str(self.paths["config"])!r})\n'
            'args = sys.argv[1:]\n'
            'with (root/"codex-calls.jsonl").open("a") as out: out.write(json.dumps(args)+"\\n")\n'
            'if args == ["plugin", "add", "iterm2-status@personal"]:\n'
            '    if (root/"fail-add").exists(): sys.exit(1)\n'
            '    text = config.read_text() if config.exists() else ""\n'
            '    if \'[plugins."iterm2-status@personal"]\' not in text:\n'
            '        text += \'\\n[plugins."iterm2-status@personal"]\\nenabled = true\\n\'\n'
            '    config.write_text(text)\n'
            'elif args == ["plugin", "list", "--marketplace", "personal", "--json"]:\n'
            '    print(json.dumps({"installed": [{"name": "iterm2-status"}]}))\n'
            'elif args == ["plugin", "remove", "iterm2-status@personal"]:\n'
            '    config.write_text(config.read_text().replace("enabled = true", "enabled = false"))\n'
            'else: sys.exit(2)\n'
        )
        self.codex.chmod(0o700)
        self.notifier = self.home / 'terminal-notifier'
        self.notifier.write_text('#!/bin/sh\nexit 0\n')
        self.notifier.chmod(0o700)

    def install(self, enable=True, dry_run=False, launchers=False):
        args = argparse.Namespace(workspace=None, workspace_name=None, parent_profile=None,
                                  no_workspace=False, no_launchers=not launchers, dry_run=dry_run,
                                  notifications=enable, no_notifications=not enable,
                                  terminal_notifier=str(self.notifier))
        # Capability discovery is external to these regressions. run/subprocess
        # are deliberately real: the executable modifies config.toml during add.
        with patch.object(installer, 'prerequisites', return_value=([], {}, str(self.codex), '/fake/it2')):
            with redirect_stdout(io.StringIO()):
                installer.install(self.paths, args)

    def config(self):
        return tomllib.loads(self.paths['config'].read_text())

    def calls(self):
        path = self.home / 'codex-calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_first_enable_preserves_config_written_by_codex_plugin_add(self):
        self.paths['config'].write_text('model = "example"\n[tui]\nnotifications = true\n')
        self.install()
        self.assertEqual(self.config()['plugins']['iterm2-status@personal'], {'enabled': True})
        self.assertIs(self.config()['tui']['notifications'], False)
        self.assertEqual(self.config()['model'], 'example')
        self.assertEqual(self.calls(), [['plugin', 'add', 'iterm2-status@personal']])

    def test_first_enable_works_when_codex_creates_config(self):
        self.install()
        self.assertTrue(self.config()['plugins']['iterm2-status@personal']['enabled'])
        self.assertIs(self.config()['tui']['notifications'], False)

    def test_dotted_and_inline_tui_are_rejected_before_any_writes_or_add(self):
        for text in ['tui.notifications = true\n',
                     'tui = { notifications = true, status_line = ["weekly-limit"] }\n']:
            with self.subTest(text=text):
                self.paths['config'].write_text(text)
                with self.assertRaises(ValueError):
                    self.install()
                self.assertEqual(self.paths['config'].read_text(), text)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.paths['settings'].exists())
                self.assertFalse(self.paths['canonical'].exists())

    def test_invalid_input_toml_is_rejected_before_add(self):
        self.paths['config'].write_text('model = "secret fixture text\n')
        with self.assertRaises(ValueError) as caught:
            self.install()
        self.assertNotIn('secret fixture text', str(caught.exception))
        self.assertEqual(self.calls(), [])

    def test_invalid_native_notification_value_is_rejected_before_add(self):
        self.paths['config'].write_text('[tui]\nnotifications = [["approval-requested"]]\n')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.calls(), [])

    def test_disable_restores_true_and_keeps_new_settings_and_plugin_registration(self):
        self.paths['config'].write_text('[tui]\nnotifications = true # original\nstatus_line = ["weekly-limit"]\n')
        self.install()
        self.paths['config'].write_text(self.paths['config'].read_text() + '\n[features]\nexample = true\n')
        self.install(enable=False)
        self.assertIs(self.config()['tui']['notifications'], True)
        self.assertEqual(self.config()['tui']['status_line'], ['weekly-limit'])
        self.assertTrue(self.config()['features']['example'])
        self.assertTrue(self.config()['plugins']['iterm2-status@personal']['enabled'])
        self.assertIn('notifications = true # original', self.paths['config'].read_text())
        self.assertIs(json.loads(self.paths['settings'].read_text())['notifications'], False)

    def test_repeat_enable_does_not_replace_original_notification_list(self):
        self.paths['config'].write_text('[tui]\nnotifications = ["approval-requested"]\n')
        self.install()
        self.install()
        self.install(enable=False)
        self.assertEqual(self.config()['tui']['notifications'], ['approval-requested'])

    def test_disable_restores_absent_native_setting(self):
        for original in ['model = "example"\n', '[tui]\n', '[tui]\nstatus_line = ["weekly-limit"]\n']:
            with self.subTest(original=original):
                self.paths['config'].write_text(original)
                self.install()
                self.install(enable=False)
                self.assertNotIn('notifications', self.config().get('tui', {}))
                self.assertEqual('tui' in self.config(), 'tui' in tomllib.loads(original))

    def test_disable_preserves_original_false(self):
        self.paths['config'].write_text('[tui]\nnotifications = false\n')
        self.install()
        self.install(enable=False)
        self.assertIs(self.config()['tui']['notifications'], False)

    def test_uninstall_restores_native_value_and_keeps_codex_removal(self):
        self.paths['config'].write_text('[tui]\nnotifications = true\nstatus_line = ["weekly-limit"]\n')
        self.install()
        with redirect_stdout(io.StringIO()):
            installer.uninstall(self.paths, False)
        self.assertIs(self.config()['tui']['notifications'], True)
        self.assertEqual(self.config()['tui']['status_line'], ['weekly-limit'])
        self.assertIs(self.config()['plugins']['iterm2-status@personal']['enabled'], False)
        self.assertIs(json.loads(self.paths['settings'].read_text())['notifications'], False)
        with redirect_stdout(io.StringIO()):
            installer.uninstall(self.paths, False)
        self.assertIs(self.config()['tui']['notifications'], True)

    def test_uninstall_with_notifications_archives_enabled_launchers(self):
        self.paths['config'].write_text('[tui]\nnotifications = true\n')
        self.install(launchers=True)
        profiles_text = self.paths['profiles'].read_text()
        profiles = json.loads(profiles_text)['Profiles']
        self.assertTrue(profiles)
        self.assertTrue(all(profile['Guid'] in installer.OWNED_GUIDS for profile in profiles))
        self.assertIs(json.loads(self.paths['settings'].read_text())['notifications'], True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(installer.uninstall(self.paths, False), 0)
        self.assertIs(self.config()['tui']['notifications'], True)
        self.assertIs(self.config()['plugins']['iterm2-status@personal']['enabled'], False)
        self.assertIs(json.loads(self.paths['settings'].read_text())['notifications'], False)
        self.assertFalse(self.paths['profiles'].exists())
        archives = list((self.paths['settings'].parent / 'backups').glob('*/codex-profiles.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_text(), profiles_text)

    def test_user_edit_of_native_notifications_is_preserved_on_disable(self):
        self.paths['config'].write_text('[tui]\nnotifications = true\n')
        self.install()
        self.paths['config'].write_text(self.paths['config'].read_text().replace(
            'notifications = false', 'notifications = ["agent-turn-complete"]'))
        self.install(enable=False)
        self.assertEqual(self.config()['tui']['notifications'], ['agent-turn-complete'])

    def test_disable_dry_run_leaves_all_files_intact(self):
        self.paths['config'].write_text('[tui]\nnotifications = true\n')
        self.install()
        before = {key: self.paths[key].read_bytes() for key in ['config', 'settings']}
        self.install(enable=False, dry_run=True)
        self.assertEqual({key: self.paths[key].read_bytes() for key in before}, before)
        self.assertEqual(len(self.calls()), 1)

    def test_conflicting_notification_flags_are_rejected_by_cli(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'install.py'),
                                 '--notifications', '--no-notifications'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('not allowed with argument', result.stderr)

    def test_failed_add_does_not_disable_native_notifications(self):
        original = '[tui]\nnotifications = true\n'
        self.paths['config'].write_text(original)
        (self.home / 'fail-add').touch()
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(self.paths['config'].read_text(), original)
        self.assertFalse(self.paths['settings'].exists())

    def test_header_inside_multiline_string_cannot_change_unrelated_values(self):
        text = 'note = """\n[tui]\nnotifications = true\n"""\n'
        self.paths['config'].write_text(text)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.paths['config'].read_text(), text)
        self.assertEqual(self.calls(), [])

    def test_legacy_enable_recovers_original_value_from_pre_enable_backup(self):
        self.paths['config'].write_text('[tui]\nnotifications = false\n')
        self.paths['settings'].parent.mkdir(parents=True)
        self.paths['settings'].write_text(json.dumps({'notifications': True, 'codex': str(self.codex)}))
        backup = self.paths['settings'].parent / 'backups/20261011T120000Z'
        backup.mkdir(parents=True)
        (backup / 'settings.json').write_text(json.dumps({'notifications': False}))
        (backup / 'codex-config.toml').write_text('[tui]\nnotifications = ["approval-requested"]\n')
        self.install()
        self.install(enable=False)
        self.assertEqual(self.config()['tui']['notifications'], ['approval-requested'])

    def test_legacy_without_recovery_record_refuses_instead_of_saving_false_as_original(self):
        original = '[tui]\nnotifications = false\n'
        self.paths['config'].write_text(original)
        self.paths['settings'].parent.mkdir(parents=True)
        self.paths['settings'].write_text(json.dumps({'notifications': True, 'codex': str(self.codex)}))
        for enable in [True, False]:
            with self.subTest(enable=enable), self.assertRaises(ValueError):
                self.install(enable=enable)
        self.assertEqual(self.paths['config'].read_text(), original)
        self.assertEqual(self.calls(), [])


class NotificationPythonRequirement(unittest.TestCase):
    def test_missing_tomllib_produces_explicit_requirement(self):
        with patch.dict(sys.modules, {'tomllib': None}):
            with self.assertRaisesRegex(ValueError, 'Python 3.11'):
                installer.without_tui_notifications('')


if __name__ == '__main__':
    unittest.main()
