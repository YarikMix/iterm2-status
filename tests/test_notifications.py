import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from test_status import event, UUID, UUID2, SCRIPT

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import iterm_support as support


class Notifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.bin = self.data / 'bin'
        self.bin.mkdir()
        fake = ('#!' + sys.executable + '\n'
                'import json,os,sys,time\nfrom pathlib import Path\n'
                'd=Path(os.environ["PLUGIN_DATA"])\n'
                'with (d/(Path(sys.argv[0]).name+".jsonl")).open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        for name in ['it2', 'osascript', 'terminal-notifier']:
            path = self.bin / name
            path.write_text(fake + ('print((d/"context").read_text())\n' if name == 'osascript' else ''))
            path.chmod(0o700)
        (self.data / 'context').write_text('false\nМоя задача $(touch unsafe)')
        self.env = dict(os.environ, PLUGIN_DATA=str(self.data), CODEX_ITERM2_IT2=str(self.bin / 'it2'),
                        ITERM_SESSION_ID=UUID, TERM_PROGRAM='iTerm.app',
                        XDG_CONFIG_HOME=str(self.data / 'config'), PATH=str(self.bin) + os.pathsep + os.defpath)
        self.settings = self.data / 'config/codex-iterm2-status/settings.json'
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({'notifications': True, 'terminal_notifier': str(self.bin / 'terminal-notifier')}))

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, name, terminal=UUID, **fields):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT)], input=json.dumps(event(name, **fields)),
                                env=dict(self.env, ITERM_SESSION_ID=terminal), text=True, capture_output=True, timeout=3)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '{}\n', ''))

    def notices(self, expected=None, timeout=3):
        path = self.data / 'terminal-notifier.jsonl'
        deadline = time.monotonic() + timeout
        while True:
            result = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            if expected is None or len(result) >= expected or time.monotonic() >= deadline:
                return result
            time.sleep(0.02)

    def test_slow_notifier_delivers_after_hook_exits(self):
        path = self.bin / 'terminal-notifier'
        path.write_text(path.read_text().replace('d=Path', 'time.sleep(1.2)\nd=Path'))
        self.hook('UserPromptSubmit')
        started = time.monotonic()
        self.hook('Stop')
        elapsed = time.monotonic() - started
        notices = self.notices(expected=1, timeout=2)
        self.assertEqual(len(notices), 1, 'The notifier was killed before it could deliver the banner')
        self.assertLess(elapsed, 0.8, 'A slow notification must not make Codex wait')

    def test_input_permission_and_completion_notify_once_per_transition(self):
        self.hook('UserPromptSubmit')
        tool = dict(tool_name='request_user_input', tool_input={'questions': ['private prompt']})
        self.hook('PreToolUse', **tool)
        self.hook('PreToolUse', tool_name='exec_command', tool_input={'command': 'private command'})
        self.hook('PostToolUse', **tool)
        self.hook('PermissionRequest', tool_name='Bash', tool_input={'command': 'private command'})
        self.hook('PermissionRequest', tool_name='Bash', tool_input={'command': 'another private command'})
        self.hook('Stop')
        self.hook('Stop')
        notices = self.notices(expected=3)
        self.assertEqual(len(notices), 3)
        self.assertEqual([a[a.index('-subtitle') + 1] for a in notices], ['Нужен ответ', 'Нужно разрешение', 'Ответ готов'])
        self.assertEqual(notices[0][notices[0].index('-title') + 1], 'Codex · Моя задача $(touch unsafe)')
        self.assertEqual(notices[0][notices[0].index('-message') + 1], 'Проект: test-project')
        for text in ['private prompt', 'private command']:
            self.assertNotIn(text, json.dumps(notices))
            self.assertNotIn(text, (self.data / (UUID + '.json')).read_text())

    def test_active_session_suppresses_and_consumes_notice(self):
        (self.data / 'context').write_text('true\nTask')
        self.hook('UserPromptSubmit')
        self.hook('PermissionRequest', tool_name='Bash')
        (self.data / 'context').write_text('false\nTask')
        self.hook('PreToolUse', tool_name='exec_command')
        self.assertEqual(self.notices(), [])
        self.hook('Stop')
        self.assertEqual(len(self.notices(expected=1)), 1)

    def test_banner_can_show_tab_title_derived_from_first_prompt_without_caching_it(self):
        title = 'Разобрать первый запрос пользователя'
        (self.data / 'context').write_text('false\n' + title)
        self.hook('UserPromptSubmit', prompt='Raw prompt event is not banner text')
        self.hook('Stop')
        notice = self.notices(expected=1)[0]
        self.assertEqual(notice[notice.index('-title') + 1], 'Codex · ' + title)
        self.assertNotIn('Raw prompt event', json.dumps(notice, ensure_ascii=False))
        cached = json.dumps(json.loads((self.data / (UUID + '.json')).read_text()), ensure_ascii=False)
        self.assertNotIn(title, cached)
        self.assertNotIn('Raw prompt event', cached)

    def test_two_sessions_and_new_turns_are_independent(self):
        for terminal in [UUID, UUID2, UUID]:
            self.hook('UserPromptSubmit', terminal=terminal)
            self.hook('Stop', terminal=terminal)
        notices = self.notices(expected=3)
        self.assertEqual(len(notices), 3)
        self.assertEqual([a[a.index('-group') + 1] for a in notices], ['codex-' + UUID, 'codex-' + UUID2, 'codex-' + UUID])

    def test_start_interrupt_end_and_unowned_stop_do_not_notify(self):
        for name in ['Stop', 'SessionStart', 'UserPromptSubmit', 'Interrupt', 'Stop', 'SessionEnd']:
            self.hook(name)
        self.assertEqual(self.notices(), [])

    def test_completion_waits_for_background_agents(self):
        self.hook('UserPromptSubmit')
        self.hook('SubagentStart', agent_id='one')
        self.hook('Stop')
        self.assertEqual(self.notices(), [])
        self.hook('SubagentStop', agent_id='one')
        self.assertEqual(len(self.notices(expected=1)), 1)

    def test_later_background_agent_does_not_repeat_completed_turn(self):
        self.hook('UserPromptSubmit')
        self.hook('Stop')
        self.hook('SubagentStart', agent_id='later')
        self.hook('SubagentStop', agent_id='later')
        self.assertEqual(len(self.notices(expected=1)), 1)

    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('osacompile') and Path('/Applications/iTerm.app').exists(), 'Requires macOS/iTerm dictionary')
    def test_metadata_script_compiles_with_installed_iterm_dictionary(self):
        from notifications import CONTEXT_SCRIPT
        source = self.data / 'context.applescript'
        source.write_text(CONTEXT_SCRIPT)
        result = subprocess.run(['/usr/bin/osacompile', '-o', str(self.data / 'context.scpt'), str(source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_focus_does_not_activate_another_session(self):
        self.hook('UserPromptSubmit')
        self.hook('Stop')
        notice = self.notices(expected=1)[0]
        action = shlex.split(notice[notice.index('-execute') + 1])
        fake = self.bin / 'it2'
        fake.write_text(fake.read_text() + 'sys.exit(1)\n')
        result = subprocess.run(action, env=self.env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        calls = [json.loads(line) for line in (self.data / 'it2.jsonl').read_text().splitlines()]
        self.assertEqual(calls[-1], ['session', 'focus', UUID])
        self.assertNotIn(['app', 'activate'], calls)

    def test_stale_session_and_turn_cannot_notify(self):
        self.hook('UserPromptSubmit', turn_id='new')
        self.hook('Stop', turn_id='old')
        self.hook('Stop', session_id='old-session')
        self.assertEqual(self.notices(), [])

    def test_concurrent_duplicate_stop_notifies_once(self):
        self.hook('UserPromptSubmit')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.hook('Stop'), range(4)))
        self.notices(expected=1)
        time.sleep(0.1)
        self.assertEqual(len(self.notices()), 1)

    def test_click_action_focuses_originating_uuid_and_activates_iterm(self):
        self.hook('UserPromptSubmit', terminal=UUID2)
        self.hook('Stop', terminal=UUID2)
        self.assertEqual(len(self.notices(expected=1)), 1)
        notice = self.notices()[0]
        action = shlex.split(notice[notice.index('-execute') + 1])
        result = subprocess.run(action, env=self.env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        calls = [json.loads(line) for line in (self.data / 'it2.jsonl').read_text().splitlines()]
        self.assertEqual(calls[-2:], [['session', 'focus', UUID2], ['app', 'activate']])
        self.assertFalse((self.data / 'unsafe').exists())

    def test_disabled_notifications_do_not_contact_notification_services(self):
        self.settings.write_text('{}')
        self.hook('UserPromptSubmit')
        self.hook('Stop')
        self.assertEqual(self.notices(), [])
        self.assertFalse((self.data / 'osascript.jsonl').exists())

    def test_context_failure_or_unknown_focus_does_not_notify(self):
        for content in ['unknown\nTask', '']:
            (self.data / 'context').write_text(content)
            self.hook('UserPromptSubmit')
            self.hook('Stop')
        self.assertEqual(self.notices(), [])

    def test_slow_notifier_and_context_do_not_block_hooks(self):
        for name in ['terminal-notifier', 'osascript']:
            path = self.bin / name
            original = path.read_text()
            path.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(8)\n')
            self.hook('UserPromptSubmit')
            started = time.monotonic()
            self.hook('Stop')
            self.assertLess(time.monotonic() - started, 2.5)
            path.write_text(original)


    def test_hook_falls_back_to_path_when_saved_notifier_is_gone(self):
        self.settings.write_text(json.dumps({'notifications': True, 'terminal_notifier': str(self.data / 'gone/terminal-notifier')}))
        self.hook('UserPromptSubmit')
        self.hook('Stop')
        self.assertEqual(len(self.notices(expected=1)), 1)

    def test_hook_without_any_notifier_stays_silent(self):
        self.settings.write_text(json.dumps({'notifications': True, 'terminal_notifier': str(self.data / 'gone/terminal-notifier')}))
        (self.bin / 'terminal-notifier').unlink()
        self.hook('UserPromptSubmit')
        self.hook('Stop')
        self.assertEqual(self.notices(), [])


class NotifierDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make(self, name, code=0, sleep=0):
        directory = self.root / name
        directory.mkdir()
        path = directory / 'terminal-notifier'
        path.write_text('#!/bin/sh\n' + (f'sleep {sleep}\n' if sleep else '') + f'exit {code}\n')
        path.chmod(0o700)
        return path

    def env(self, *entries):
        return {'PATH': os.pathsep.join(str(e) for e in entries)}

    def test_saved_path_wins_without_scanning_path(self):
        saved = self.make('saved', code=1)
        other = self.make('other')
        self.assertEqual(support.find_terminal_notifier(str(saved), self.env(other.parent)), str(saved))

    def test_skips_broken_and_finds_working_on_path(self):
        broken = self.make('a', code=1)
        working = self.make('b')
        environ = self.env('', '/nonexistent', broken.parent, working.parent)
        self.assertEqual(support.find_terminal_notifier(str(self.root / 'gone'), environ), str(working))

    def test_hanging_candidate_is_skipped(self):
        hanging = self.make('a', sleep=5)
        working = self.make('b')
        started = time.monotonic()
        found = support.find_terminal_notifier(None, self.env(hanging.parent, working.parent), timeout=0.2)
        self.assertEqual(found, str(working))
        self.assertLess(time.monotonic() - started, 3)

    def test_no_working_candidate_returns_none(self):
        broken = self.make('a', code=1)
        self.assertIsNone(support.find_terminal_notifier(None, self.env(broken.parent)))


if __name__ == '__main__':
    unittest.main()
