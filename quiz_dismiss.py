#!/usr/bin/env python3
"""Dismiss today's quiz so it won't show again until tomorrow."""

from pathlib import Path
from _shared import today_kst

MARKER_FILE = Path(__file__).resolve().parent / 'quiz-last-dismissed.txt'

today = today_kst()
MARKER_FILE.write_text(today, encoding='utf-8')
print(f'Quiz dismissed for {today}')
