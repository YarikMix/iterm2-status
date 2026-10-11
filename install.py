#!/usr/bin/env python3
"""Install, inspect, or remove the local Codex/iTerm2 integration (stdlib only)."""

import argparse
import copy
from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from iterm_support import config_dir, load_settings, executable
from diagnostics import check, prerequisites, live_check, display

NAME = "iterm2-status"
DEFAULT_GUID = "CODEX-ITERM2-DEFAULT-0001"
WORKSPACE_GUID = "CODEX-ITERM2-WORKSPACE-0001"
OWNED_GUIDS = {DEFAULT_GUID, WORKSPACE_GUID, "TREY-CODEX-AGENT-0001", "TREY-CODEX-ABILITIE-0001"}


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def marketplace_config(existing):
    """Only add a missing entry; never replace another plugin or source."""
    data = json.loads(json.dumps(existing)) if existing is not None else {
        "name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    if not isinstance(data, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(data.get("name", ""))):
        raise ValueError("The personal marketplace must have a valid name.")
    if not isinstance(data.get("plugins"), list) or not all(isinstance(p, dict) for p in data["plugins"]):
        raise ValueError("The marketplace plugins field must be an array.")
    matches = [p for p in data["plugins"] if p.get("name") == NAME]
    expected = {"source": "local", "path": f"./plugins/{NAME}"}
    if matches:
        if len(matches) != 1 or matches[0].get("source") != expected:
            raise ValueError("An existing iterm2-status entry points elsewhere; reconcile it first.")
    else:
        data["plugins"].append({"name": NAME, "source": expected,
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity"})
    return data


def profile_config(source, settings, previous):
    if not isinstance(previous, dict) or not isinstance(previous.get("Profiles"), list) or not all(isinstance(p, dict) for p in previous["Profiles"]):
        raise ValueError("The existing dynamic profile file is malformed; review it first.")
    if settings.get("launchers") is False:
        return dict(previous, Profiles=[p for p in previous["Profiles"] if p.get("Guid") not in OWNED_GUIDS])
    launcher = source / "scripts/launch.zsh"
    shell_command = "exec /bin/zsh " + shlex.quote(str(launcher))
    command = "/bin/zsh -lic " + shlex.quote(shell_command)
    shell = settings.get("shell", "/bin/zsh")
    command = shlex.quote(shell) + " -lic " + shlex.quote(shell_command)
    common = {"Dynamic Profile Parent Name": settings["parent_profile"],
              "Tags": ["ai", "codex", NAME], "Custom Command": "Yes", "Command": command}
    profiles = [dict(common, Name="Codex", Guid=DEFAULT_GUID, **{"Custom Directory": "Recycle"})]
    if settings.get("workspace"):
        profiles.append(dict(common, Name=settings["workspace_name"], Guid=WORKSPACE_GUID,
                             **{"Custom Directory": "Yes", "Working Directory": settings["workspace"]}))
    # This file belongs to the integration, but preserve any user-added entries.
    extras = [p for p in previous.get("Profiles", []) if p.get("Guid") not in OWNED_GUIDS]
    return dict(previous, Profiles=extras + profiles)


def save_json(path, data, backup_dir, expected):
    save_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", backup_dir, expected)


def save_text(path, content, backup_dir, expected):
    current = path.read_text() if path.exists() else None
    if current != expected:
        raise RuntimeError(f"{path} changed during installation; retry after reviewing it.")
    if current == content:
        return
    if current is not None:
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, backup_dir / path.name)
        (backup_dir / path.name).chmod(0o600)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return subprocess.run(args, check=True, text=True, **kwargs)


def locations(home, source, environ=None):
    environ = os.environ if environ is None else environ
    home = Path(home)
    codex_home = Path(environ.get("CODEX_HOME") or home / ".codex").expanduser()
    if not codex_home.is_absolute():
        raise ValueError("CODEX_HOME must be an absolute directory.")
    local = config_dir(home, environ)
    return {"home": home, "source": source, "canonical": home / "plugins" / NAME,
            "marketplace": home / ".agents/plugins/marketplace.json",
            "profiles": home / "Library/Application Support/iTerm2/DynamicProfiles/codex-profiles.json",
            "settings": local / "settings.json", "python_path": local / "python-path",
            "codex_path": local / "codex-path", "shell_path": local / "shell-path",
            "saved_path": local / "search-path", "config": codex_home / "config.toml"}


def without_paiw_notification(existing, home):
    """Replace only the known completion notifier, never PAIW's source files."""
    value = json.loads(json.dumps(existing))
    groups = value.get("hooks", {}).get("Stop", [])
    if not isinstance(groups, list):
        raise ValueError("Codex Stop hooks must be an array.")
    expected = ["node", str(Path(home) / ".paiw/tools/system/hooks/codex/notify-stop.mjs")]
    retained = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError("Codex Stop hook groups must contain a hooks array.")
        hooks = []
        for hook in group["hooks"]:
            if not isinstance(hook, dict):
                raise ValueError("Codex hooks must be objects.")
            try:
                replace = hook.get("type") == "command" and shlex.split(hook.get("command", "")) == expected
            except ValueError:
                replace = False
            if not replace:
                hooks.append(hook)
        if hooks:
            retained.append(dict(group, hooks=hooks))
    if "Stop" in value.get("hooks", {}):
        value["hooks"]["Stop"] = retained
    return value


def parse_toml(content):
    try:
        import tomllib
    except ImportError as error:
        raise ValueError("Notification configuration requires Python 3.11+ (tomllib).") from error
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        # Configuration may contain secrets. Never echo the malformed line.
        raise ValueError("Invalid Codex TOML; review config.toml before changing notifications.") from error


def edit_tui_notifications(content, replacement, remove_empty=False):
    """Edit one explicit [tui] key; validate syntax and unrelated parsed values."""
    before = parse_toml(content)
    tui = before.get('tui', {})
    if not isinstance(tui, dict):
        raise ValueError("Use an explicit [tui] table for native notification settings.")
    if 'notifications' in tui:
        value = tui['notifications']
        if not (isinstance(value, bool) or isinstance(value, list) and all(isinstance(x, str) for x in value)):
            raise ValueError("tui.notifications must be a boolean or an array of event names.")
    if replacement is not None:
        restored = parse_toml('[tui]\n' + replacement + '\n')
        value = restored.get('tui', {}).get('notifications')
        if set(restored) != {'tui'} or set(restored['tui']) != {'notifications'} or not (
            isinstance(value, bool) or isinstance(value, list) and all(isinstance(x, str) for x in value)
        ):
            raise ValueError("Saved native notification setting is invalid; review settings.json.")
    lines = content.splitlines(keepends=True)
    sections = [i for i, line in enumerate(lines) if re.fullmatch(r'\s*\[\s*(?:tui|"tui"|\x27tui\x27)\s*\]\s*(?:#.*)?', line.rstrip('\r\n'))]
    if len(sections) > 1:
        raise ValueError("Duplicate [tui] sections; review Codex config before enabling notifications.")
    original = None
    if not sections:
        if 'tui' in before:
            raise ValueError("Use an explicit [tui] section instead of dotted or inline tui settings.")
        result = content if replacement is None else (
            content.rstrip('\r\n') + ('\n\n' if content.strip() else '') + '[tui]\n' + replacement + '\n')
    else:
        start = sections[0]
        found = None
        for i in range(start + 1, len(lines)):
            if re.match(r'\s*\[', lines[i]):
                break
            key = re.match(r'\s*(?:notifications|"notifications"|\x27notifications\x27)\s*=\s*(.*)', lines[i].rstrip('\r\n'))
            if key:
                if not re.fullmatch(r'(?:true|false|\[.*\])\s*(?:#.*)?', key[1]):
                    raise ValueError("Use a single-line tui.notifications setting before enabling this integration.")
                found, original = i, lines[i].rstrip('\r\n')
                break
        if found is None and 'notifications' in before.get('tui', {}):
            raise ValueError("Could not safely locate tui.notifications; use a single-line explicit key.")
        if found is not None:
            lines[found:found + 1] = [] if replacement is None else [replacement + '\n']
        elif replacement is not None:
            if not lines[start].endswith('\n'):
                lines[start] += '\n'
            lines.insert(start + 1, replacement + '\n')
        result = ''.join(lines)
        if remove_empty and not parse_toml(result).get('tui'):
            del lines[start]
            result = ''.join(lines)
    after = parse_toml(result)
    # A header-shaped line inside a multiline TOML string must not be edited.
    # Syntax validity alone cannot catch that corruption.
    comparable = []
    for data in (copy.deepcopy(before), copy.deepcopy(after)):
        if isinstance(data.get('tui'), dict):
            data['tui'].pop('notifications', None)
            if not data['tui']:
                data.pop('tui')
        comparable.append(data)
    if comparable[0] != comparable[1]:
        raise ValueError("Notification edit would change unrelated TOML settings; review config.toml.")
    expected = parse_toml('[tui]\n' + replacement + '\n')['tui']['notifications'] if replacement is not None else None
    if after.get('tui', {}).get('notifications') != expected:
        raise ValueError("Notification edit did not produce the expected TOML setting.")
    return result, original, not bool(sections)


def without_tui_notifications(content):
    return edit_tui_notifications(content, 'notifications = false')[0]


def notification_configuration(settings, content):
    """Return a planned settings/config pair, keeping the first native value."""
    settings = dict(settings)
    if settings.get('notifications'):
        result, original, added = edit_tui_notifications(content, 'notifications = false')
        settings.setdefault('native_notifications', {'line': original, 'added_section': added})
        return settings, result
    saved = settings.pop('native_notifications', None)
    if saved is None:
        return settings, None
    if parse_toml(content).get('tui', {}).get('notifications') is not False:
        # An explicit user edit after enable takes precedence over restoration.
        return settings, content
    return settings, edit_tui_notifications(content, saved['line'], saved['added_section'])[0]


def recover_native_notifications(paths, settings):
    """Migrate older opt-in installs only from a proven pre-enable backup."""
    if not settings.get('notifications') or 'native_notifications' in settings:
        return settings
    backups = paths['settings'].parent / 'backups'
    for saved_settings in sorted(backups.glob('*/settings.json'), reverse=True):
        try:
            previous = read_json(saved_settings, {})
            if not isinstance(previous, dict) or previous.get('notifications') is True:
                continue
            for name in ['codex-config.toml', 'config.toml']:
                original = saved_settings.parent / name
                if original.exists():
                    _, line, added = edit_tui_notifications(original.read_text(), 'notifications = false')
                    return dict(settings, native_notifications={'line': line, 'added_section': added})
        except (OSError, ValueError):
            continue
    raise ValueError("Original native notification setting was not recorded by this older installation. "
                     "Restore tui.notifications from its pre-enable backup before reinstalling or uninstalling.")


def normalized_settings(value):
    if not isinstance(value, dict):
        raise ValueError("Saved settings must be a JSON object.")
    value = dict(value)
    for name in ["notifications", "launchers"]:
        if name in value and not isinstance(value[name], bool):
            raise ValueError(f"Saved setting {name} must be a boolean.")
    saved = value.get('native_notifications')
    if 'native_notifications' in value and (not isinstance(saved, dict) or set(saved) != {'line', 'added_section'} or
                             not isinstance(saved.get('added_section'), bool) or
                             saved.get('line') is not None and
                             (not isinstance(saved['line'], str) or any(c in saved['line'] for c in '\n\r\0'))):
        raise ValueError("Saved native notification setting is invalid; review settings.json.")
    for name in ["parent_profile", "workspace", "workspace_name", "iterm_app", "codex", "shell", "terminal_notifier"]:
        if name in value and (not isinstance(value[name], str) or not value[name] or
                              any(c in value[name] for c in "\n\r\0")):
            raise ValueError(f"Saved setting {name} must be a nonempty single-line string.")
    value.setdefault("parent_profile", "Default")
    value.setdefault("workspace_name", "Codex — Workspace")
    return value


def doctor(paths, as_json=False, live=False):
    checks, versions = [], {}
    try:
        settings = normalized_settings(read_json(paths["settings"], {}))
        checks, versions, codex, it2 = prerequisites(paths["home"], settings)
        checks = [dict(c, status="error") if c["id"] == "iterm_api" and c["status"] == "warning" else c for c in checks]
        manifest = read_json(paths["source"] / ".codex-plugin/plugin.json", {})
        versions["plugin"] = manifest.get("version")
        marketplace = marketplace_config(read_json(paths["marketplace"], None))["name"]
        installed = None
        if codex:
            try:
                response = run([codex, "plugin", "list", "--marketplace", marketplace, "--json"], capture_output=True)
                matches = [p for p in json.loads(response.stdout).get("installed", []) if p.get("name") == NAME]
                installed = matches[0] if len(matches) == 1 else None
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        checks.append(check("installed_plugin", bool(installed and installed.get("enabled") and
                            installed.get("version") == manifest.get("version")),
                            "The enabled plugin must match this source version; rerun the installer."))
        current = read_json(paths["profiles"], {"Profiles": []})
        expected = profile_config(paths["canonical"], settings, {"Profiles": []})["Profiles"]
        # Check actual entries and their launch commands, not just file existence.
        valid_profiles = isinstance(current, dict) and isinstance(current.get("Profiles"), list)
        for desired in expected:
            matches = [p for p in current["Profiles"] if isinstance(p, dict) and p.get("Guid") == desired["Guid"]] if valid_profiles else []
            checks.append(check("profile_workspace" if desired["Guid"] == WORKSPACE_GUID else "profile_default",
                                len(matches) == 1 and all(matches[0].get(k) == v for k, v in desired.items()),
                                "The launcher must exist and match saved preferences; rerun the installer."))
        checks.append(check("source_link", paths["canonical"].resolve() == paths["source"].resolve() and
                            (paths["canonical"] / "scripts/launch.zsh").is_file(),
                            "The canonical plugin source link must point at this checkout."))
        interpreter = paths["python_path"].read_text().strip() if paths["python_path"].exists() else ""
        from diagnostics import probe
        python_ok = bool(interpreter and probe([interpreter, "-c", "import sys; sys.exit(sys.version_info < (3,9))"]) is not None)
        checks.append(check("hook_python", python_ok, "The saved hook interpreter must run Python 3.9+; rerun the installer."))
        if settings.get("workspace"):
            checks.append(check("workspace", Path(settings["workspace"]).is_dir(),
                                "The saved workspace must exist; choose a new --workspace or --no-workspace."))
        if live:
            connection = live_check(it2)
            checks.append(connection)
            if connection["status"] == "ok":
                checks = [c for c in checks if c["id"] != "iterm_api"]
        checks.append(check("hook_trust", False, "Review and trust this plugin in Codex /hooks; trust is not inspected by doctor.", warning=True))
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Do not include malformed configuration or arbitrary child output in a
        # support report. Human-readable remediation still names the operation.
        checks.append(check("configuration", False, "Configuration could not be read; check settings and marketplace JSON, then rerun install."))
    return display({"schema_version": 1, "versions": versions, "checks": checks}, as_json)


def install(paths, args):
    source = paths["source"]
    manifest = read_json(source / ".codex-plugin/plugin.json", {})
    if manifest.get("name") != NAME or not (source / "scripts/status.py").is_file():
        raise ValueError("Run this installer from the complete iterm2-status package.")
    canonical = paths["canonical"]
    replace_link = bool(getattr(args, "replace_source_link", False) and canonical.is_symlink())
    if (canonical.exists() or canonical.is_symlink()) and canonical.resolve() != source and not replace_link:
        raise ValueError(f"{canonical} already contains another checkout; keep or move it first.")
    previous_link = os.readlink(canonical) if canonical.is_symlink() else None
    texts = {key: path.read_text() if path.exists() else None
             for key, path in paths.items() if key in {"settings", "marketplace", "profiles", "config"}}
    settings = recover_native_notifications(paths, normalized_settings(json.loads(texts["settings"]) if texts["settings"] else {}))
    if getattr(args, "no_launchers", False):
        settings["launchers"] = False
    if getattr(args, "notifications", False):
        settings["notifications"] = True
    if getattr(args, "no_notifications", False):
        settings["notifications"] = False
    if getattr(args, "terminal_notifier", None):
        settings["terminal_notifier"] = str(Path(args.terminal_notifier).expanduser().absolute())
    if settings.get("notifications"):
        notifier = executable(settings.get("terminal_notifier") or "terminal-notifier")
        if not notifier:
            raise ValueError("Notifications require an executable terminal-notifier; use --terminal-notifier PATH.")
        settings["terminal_notifier"] = notifier
    # Preflight validates both the existing and resulting TOML before any writes.
    _, config_text = notification_configuration(settings, texts["config"] or "")
    hooks_path = paths["config"].parent / "hooks.json"
    hooks_text = hooks_path.read_text() if getattr(args, "replace_paiw_notify", False) and hooks_path.exists() else None
    if getattr(args, "replace_paiw_notify", False) and not settings.get("notifications"):
        raise ValueError("--replace-paiw-notify requires enabled notifications.")
    hooks = without_paiw_notification(json.loads(hooks_text), paths["home"]) if hooks_text is not None else None
    settings.setdefault("parent_profile", "Default")
    settings.setdefault("workspace_name", "Codex — Workspace")
    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {workspace}")
        settings["workspace"] = str(workspace)
    if args.workspace_name:
        settings["workspace_name"] = args.workspace_name
    if args.parent_profile:
        settings["parent_profile"] = args.parent_profile
    for name in ["iterm_app", "codex", "shell"]:
        value = getattr(args, name, None)
        if value:
            settings[name] = str(Path(value).expanduser().absolute())
    if "shell" not in settings:
        selected_shell = os.environ.get("SHELL", "/bin/zsh")
        settings["shell"] = selected_shell if Path(selected_shell).name in {"bash", "zsh", "fish"} else "/bin/zsh"
    if Path(settings["shell"]).name not in {"bash", "zsh", "fish"} or not os.access(settings["shell"], os.X_OK):
        raise ValueError("Choose an executable bash, zsh, or fish with --shell PATH.")
    if args.no_workspace:
        settings.pop("workspace", None)
    settings = normalized_settings(settings)
    checks, versions, codex, it2 = prerequisites(paths["home"], settings)
    if any(c["status"] == "error" for c in checks):
        display({"schema_version": 1, "versions": versions, "checks": checks})
        raise ValueError("Prerequisites are missing; no installation files were changed.")
    settings["codex"] = codex
    runtime_values = [("python_path", sys.executable), ("codex_path", codex),
                      ("shell_path", settings["shell"]), ("saved_path", os.environ.get("PATH") or os.defpath)]
    for key, value in runtime_values:
        if not value or any(c in value for c in "\n\r\0"):
            raise ValueError(f"The {key} value must be a single-line path.")
    marketplace = marketplace_config(json.loads(texts["marketplace"]) if texts["marketplace"] else None)
    profiles = profile_config(canonical, settings, json.loads(texts["profiles"]) if texts["profiles"] else {"Profiles": []})
    selector = f"{NAME}@{marketplace['name']}"
    print("Install", manifest["version"], "from", source)
    print("Register", selector, "in", paths["marketplace"])
    print("Launchers:", ", ".join(p["Name"] for p in profiles["Profiles"] if p.get("Guid") in OWNED_GUIDS))
    if settings.get("notifications"):
        print("Notifications: input, approval and completion for background tabs; native TUI banners disabled.")
    if hooks is not None and hooks != json.loads(hooks_text):
        print("Legacy PAIW completion notifier: back up and remove its Codex registration only.")
    if replace_link and canonical.resolve() != source:
        print("Source symlink: back up and repoint to this checkout.")
    if args.dry_run:
        print("Dry run: no files or settings changed.")
        return 0
    # Serialize our own installers; detect other programs' edits before writing.
    with installation_lock(paths):
        if (os.readlink(canonical) if canonical.is_symlink() else None) != previous_link:
            raise RuntimeError("Source link changed during installation; review and retry.")
        if hooks_text is not None and hooks_path.read_text() != hooks_text:
            raise RuntimeError("Codex hooks changed during installation; review and retry.")
        for key, expected_text in texts.items():
            path = paths[key]
            if (path.read_text() if path.exists() else None) != expected_text:
                raise RuntimeError("Configuration changed during installation; review and retry.")
        backup = paths["settings"].parent / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup.mkdir(parents=True, mode=0o700)
        if paths["config"].exists():
            shutil.copy2(paths["config"], backup / "codex-config.toml")
            (backup / "codex-config.toml").chmod(0o600)
        if not canonical.exists():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            if not canonical.is_symlink():
                canonical.symlink_to(source, target_is_directory=True)
        if replace_link and canonical.resolve() != source:
            (backup / "source-link.json").write_text(json.dumps({"path": str(canonical), "target": previous_link}) + "\n")
            (backup / "source-link.json").chmod(0o600)
            temporary_link = canonical.with_name(".iterm2-status-install-link-" + str(os.getpid()))
            temporary_link.symlink_to(source, target_is_directory=True)
            os.replace(temporary_link, canonical)
        # Existing entries stay byte-for-byte unchanged during upgrades.
        if texts["marketplace"] is None or json.loads(texts["marketplace"]) != marketplace:
            save_json(paths["marketplace"], marketplace, backup, texts["marketplace"])
        try:
            run([codex, "plugin", "add", selector])
        except (OSError, subprocess.SubprocessError) as error:
            if replace_link and previous_link is not None and canonical.resolve() == source:
                canonical.unlink()
                canonical.symlink_to(previous_link, target_is_directory=True)
            raise RuntimeError(f"Codex installation failed. Launchers were not changed. Backups: {backup}. Fix the prerequisite and rerun this installer.") from error
        if texts["profiles"] is not None or profiles["Profiles"]:
            save_json(paths["profiles"], profiles, backup, texts["profiles"])
        if hooks is not None and hooks != json.loads(hooks_text):
            save_json(hooks_path, hooks, backup, hooks_text)
        # Codex itself adds its plugin registration to this file. Take a
        # fresh snapshot after add and preserve those changes in our edit.
        current_config = paths["config"].read_text() if paths["config"].exists() else None
        planned_settings, config_text = notification_configuration(settings, current_config or "")
        if config_text is not None:
            save_text(paths["config"], config_text, backup, current_config)
        save_json(paths["settings"], planned_settings, backup, texts["settings"])
        for key, value in runtime_values:
            save_text(paths[key], value + "\n", backup, paths[key].read_text() if paths[key].exists() else None)
        for item in checks:
            if item["status"] == "warning":
                print("SETUP:", item["message"])
        print("Backups:", backup)
        print("Installed. Start a new Codex session; use /hooks if any hooks need review.")
        return 0


@contextmanager
def installation_lock(paths):
    directory = paths["settings"].parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(directory / "install.lock", os.O_CREAT | os.O_RDWR, 0o600), "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another installer is running; retry after it finishes.") from error
        yield


def uninstall(paths, dry_run):
    if dry_run:
        return _uninstall(paths, dry_run)
    with installation_lock(paths):
        return _uninstall(paths, dry_run)


def _uninstall(paths, dry_run):
    marketplace = marketplace_config(read_json(paths["marketplace"], None))["name"]
    path = paths["profiles"]
    previous = path.read_text() if path.exists() else None
    data = json.loads(previous) if previous else {"Profiles": []}
    retained = [p for p in data["Profiles"] if p.get("Guid") not in OWNED_GUIDS]
    settings_text = paths['settings'].read_text() if paths['settings'].exists() else None
    settings = recover_native_notifications(paths, normalized_settings(json.loads(settings_text) if settings_text else {}))
    managed_notifications = 'notifications' in settings or 'native_notifications' in settings
    if managed_notifications:
        settings['notifications'] = False
    config_text = paths['config'].read_text() if paths['config'].exists() else None
    notification_configuration(settings, config_text or '')
    if dry_run:
        print("Would uninstall", f"{NAME}@{marketplace}", "and archive only this integration's launchers.")
        if managed_notifications:
            print("Would disable plugin notifications and restore the saved native TUI notification setting.")
        return 0
    from iterm_support import executable
    codex = executable(settings.get("codex") or "codex")
    if not codex:
        raise ValueError("Codex CLI was not found; restore it before uninstalling the plugin.")
    response = run([codex, "plugin", "list", "--marketplace", marketplace, "--json"], capture_output=True)
    installed = [p for p in json.loads(response.stdout).get("installed", []) if p.get("name") == NAME]
    if installed:
        run([codex, "plugin", "remove", f"{NAME}@{marketplace}"])
    backup = paths["settings"].parent / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # remove, like add, can change config.toml; restore only the native key on
    # top of that new snapshot rather than restoring an entire old config.
    current_config = paths['config'].read_text() if paths['config'].exists() else None
    planned_settings, restored_config = notification_configuration(settings, current_config or '')
    if restored_config is not None:
        save_text(paths['config'], restored_config, backup, current_config)
    if managed_notifications:
        save_json(paths['settings'], planned_settings, backup, settings_text)
    if previous is not None:
        if retained:
            save_json(path, dict(data, Profiles=retained), backup, previous)
        else:
            backup.mkdir(parents=True, mode=0o700)
            if path.read_text() != previous:
                raise RuntimeError("Profiles changed during uninstall; review them before archiving.")
            shutil.move(str(path), str(backup / path.name))
    print("Uninstalled. Source, saved preferences, marketplace entry, and backups were kept.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="install", choices=["install", "doctor", "uninstall"])
    parser.add_argument("--workspace", help="Optional directory for a second launcher (saved for upgrades)")
    parser.add_argument("--workspace-name", help="Name of the workspace launcher")
    parser.add_argument("--parent-profile", help="Existing iTerm profile to inherit; defaults to Default")
    parser.add_argument("--no-workspace", action="store_true", help="Remove the optional workspace launcher")
    parser.add_argument("--no-launchers", action="store_true", help="Disable this plugin's launchers; preserve unrelated profiles")
    notification_flags = parser.add_mutually_exclusive_group()
    notification_flags.add_argument("--notifications", action="store_true", help="Enable macOS notifications for input, approval and completion")
    notification_flags.add_argument("--no-notifications", action="store_true", help="Disable plugin banners and restore the previous native TUI notification setting")
    parser.add_argument("--terminal-notifier", help="Executable terminal-notifier (saved for hooks)")
    parser.add_argument("--replace-source-link", action="store_true", help="Repoint an existing source symlink to this checkout")
    parser.add_argument("--replace-paiw-notify", action="store_true", help="Back up and remove only the legacy PAIW Codex completion hook")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--iterm-app", help="Custom iTerm.app location (saved for hooks and diagnostics)")
    parser.add_argument("--codex", help="Explicit Codex CLI executable (saved for launchers)")
    parser.add_argument("--shell", help="Login shell for launcher: bash, zsh, or fish")
    parser.add_argument("--json", action="store_true", help="Doctor: shareable report without paths or session content")
    parser.add_argument("--live", action="store_true", help="Doctor: also check a live iTerm connection (may prompt for access)")
    args = parser.parse_args()
    if args.workspace and args.no_workspace:
        parser.error("--workspace and --no-workspace cannot be used together")
    if (args.json or args.live) and args.action != "doctor":
        parser.error("--json and --live apply only to doctor")
    try:
        paths = locations(Path.home(), Path(__file__).resolve().parent)
        if args.action == "doctor":
            return doctor(paths, args.json, args.live)
        if args.action == "uninstall":
            return uninstall(paths, args.dry_run)
        return install(paths, args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("Error:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
