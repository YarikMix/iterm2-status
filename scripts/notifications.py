"""Opt-in macOS banners. No transcripts, tool payloads, or active-session defaults."""

from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata

from iterm_support import executable, load_settings

LABELS = {'input': 'Нужен ответ', 'approval': 'Нужно разрешение', 'complete': 'Ответ готов'}

# Arguments, never interpolated AppleScript source. The exact UUID must exist;
# unknown focus or unavailable Automation permission is a silent no-op.
CONTEXT_SCRIPT = '''on run argv
    set targetID to item 1 of argv
    tell application "/Applications/iTerm.app"
        set visibleIDs to {}
        if frontmost then
            try
                repeat with visibleSession in sessions of current tab of current window
                    set end of visibleIDs to unique ID of visibleSession
                end repeat
            on error
                return "unknown"
            end try
        end if
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if unique ID of s is targetID then
                        set taskTitle to title of t
                        if taskTitle is "" then set taskTitle to name of s
                        return ((targetID is in visibleIDs) as string) & linefeed & taskTitle
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "unknown"
end run'''


def update_notice(previous, state, event):
    """Keep one notice per waiting kind/turn, even across repeated tool hooks."""
    name = event['hook_event_name']
    if state['session_id'] != previous.get('session_id') or name in {'UserPromptSubmit', 'Interrupt', 'SessionEnd'} or (name == 'SessionStart' and event.get('source') != 'compact'):
        state.pop('notice', None)
        state.pop('completion_pending', None)
    if name == 'Stop' and previous.get('working'):
        state['completion_pending'] = True
    kind = None
    if state['waiting']:
        # Notify on the newly requested type, not an unrelated parallel tool.
        if name == 'PermissionRequest':
            kind = 'approval'
        elif name == 'PreToolUse' and event.get('tool_name') in {'request_user_input', 'request_user_input_async'}:
            kind = 'input'
        else:
            return
    elif state['status'] == 'idle' and (state.get('completion_pending') or state.get('notice', {}).get('kind') == 'complete'):
        kind = 'complete'
        state.pop('completion_pending', None)
    if kind is None:
        state.pop('notice', None)
        return
    if state.get('notice', {}).get('kind') != kind:
        state['notice'] = {'kind': kind, 'token': time.time_ns(), 'handled': False}


def clean_title(value, limit=110):
    value = ''.join(c for c in value if unicodedata.category(c) not in {'Cc', 'Cf'})
    # Codex puts a transient braille spinner before its tab title.
    value = value.lstrip()
    if value and '\u2800' <= value[0] <= '\u28ff':
        value = value[1:]
    return ' '.join(value.split())[:limit]


def notify(state, terminal, it2, deadline, claim):
    notice = state.get('notice')
    if not notice or notice.get('handled'):
        return
    settings = load_settings()
    if settings.get('notifications') is not True:
        return
    notifier = executable(settings.get('terminal_notifier'))
    osascript = shutil.which('osascript')
    if not notifier or not osascript or deadline - time.monotonic() < 0.1:
        return
    app = next((p for p in Path(it2).resolve().parents if p.suffix == '.app'), Path('/Applications/iTerm.app'))
    app_literal = '"' + str(app).replace('\\', '\\\\').replace('"', '\\"') + '"'
    script = CONTEXT_SCRIPT.replace('"/Applications/iTerm.app"', app_literal)
    result = subprocess.run([osascript, '-e', script, terminal], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=min(0.5, max(0.05, deadline - time.monotonic())), check=False)
    focus, separator, title = result.stdout.strip().partition('\n')
    if result.returncode or not separator or focus not in {'true', 'false'}:
        return
    if not claim() or focus == 'true':
        return
    title = clean_title(title) or state.get('project') or 'Codex'
    action = shlex.join([sys.executable, '-B', str(Path(__file__).with_name('focus.py')), terminal, it2])
    # App startup can take several seconds (for example under Rosetta). Hand
    # delivery to a bounded child without tying it to the hook's 3s lifetime.
    subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('post_notification.py')),
                    notifier, '-title', 'Codex · ' + title,
                    '-subtitle', LABELS[notice['kind']], '-message', 'Проект: ' + state.get('project', ''),
                    '-group', 'codex-' + terminal, '-execute', action, '-sound', 'default'],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   close_fds=True, start_new_session=True)


