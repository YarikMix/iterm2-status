#!/usr/bin/env python3
"""Notification click action: select the originating UUID, then activate iTerm."""

import subprocess
import sys

from status import terminal_id


def main():
    if len(sys.argv) != 3 or terminal_id(sys.argv[1]) != sys.argv[1]:
        return 1
    terminal, it2 = sys.argv[1:]
    try:
        subprocess.run([it2, 'session', 'focus', terminal], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=True)
        subprocess.run([it2, 'app', 'activate'], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
