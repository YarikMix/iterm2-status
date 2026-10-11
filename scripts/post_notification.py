#!/usr/bin/env python3
"""Finish a single macOS banner after its hook exits; bounded, no daemon."""
import subprocess
import sys


def main():
    if len(sys.argv) < 2:
        return 0
    try:
        subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
