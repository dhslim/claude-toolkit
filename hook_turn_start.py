#!/usr/bin/env python3
"""UserPromptSubmit hook — records turn start timestamp."""

import time
from pathlib import Path

STAMP_FILE = Path.home() / '.claude' / 'turn-start'

STAMP_FILE.write_text(str(int(time.time())))
