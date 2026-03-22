#!/usr/bin/env python3
"""Stop hook — plays a notification sound if the turn took longer than 30s.

Cross-platform:
  - macOS: afplay Glass sound
  - Windows: Windows Proximity Notification sound
  - Linux/SSH: BEL character (terminal bell)
"""

import platform
import sys
import time
from pathlib import Path

STAMP_FILE = Path.home() / '.claude' / 'turn-start'
THRESHOLD = 30  # seconds


def main():
    try:
        start = int(STAMP_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return

    elapsed = int(time.time()) - start
    if elapsed <= THRESHOLD:
        return

    system = platform.system()
    if system == 'Darwin':
        import subprocess
        subprocess.Popen(
            ['afplay', '/System/Library/Sounds/Glass.aiff'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    elif system == 'Windows':
        import winsound
        sound = Path(__file__).resolve().parent / 'notify.wav'
        winsound.PlaySound(str(sound), winsound.SND_FILENAME)
    # Linux/SSH: no reliable sound method — skip silently


if __name__ == '__main__':
    main()
