#!/usr/bin/env python3
"""Strip image blocks from a Claude Code session JSONL.

Replaces every {"type": "image", ...} content block with
{"type": "text", "text": "[image stripped]"} to keep the JSON structurally
valid while dropping base64 image payloads. Useful for:

  - Shrinking bloated sessions before `claude --resume`
  - Avoiding the "many-image dimension limit (2000px)" error on resume

Usage:
    python strip_session_images.py <session-file-path>
    python strip_session_images.py <session-id-substring>
    python strip_session_images.py <path> --wait-for-sync <sid>

A .bak copy is written alongside the original unless --no-backup is passed.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYNC_LOG = SCRIPT_DIR / 'sync.log'
CLAUDE_PROJECTS = Path.home() / '.claude' / 'projects'
SYNC_COMPLETE_PREFIXES = ('SYNC OK', 'SYNC SKIP', 'SYNC ERR', 'SYNC FAIL')


def resolve_session_path(arg: str) -> Path:
    """Accept either a direct path or a session-id (or substring)."""
    p = Path(arg)
    if p.is_file():
        return p
    matches = list(CLAUDE_PROJECTS.rglob(f'*{arg}*.jsonl'))
    if not matches:
        sys.exit(f'No session file found matching: {arg}')
    if len(matches) > 1:
        listing = '\n'.join(f'  {m}' for m in matches)
        sys.exit(f'Multiple matches:\n{listing}')
    return matches[0]


def file_has_images(path: Path) -> bool:
    """Fast byte-level scan for any image content block."""
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                if b'"type":"image"' in chunk or b'"type": "image"' in chunk:
                    return True
    except OSError:
        return False
    return False


def wait_for_sync(sid: str, timeout_s: float = 20.0) -> bool:
    """Poll sync.log for a SYNC OK/SKIP/ERR/FAIL line mentioning sid."""
    deadline = time.time() + timeout_s
    start_pos = SYNC_LOG.stat().st_size if SYNC_LOG.exists() else 0
    while time.time() < deadline:
        try:
            if SYNC_LOG.exists() and SYNC_LOG.stat().st_size > start_pos:
                with open(SYNC_LOG, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(start_pos)
                    for line in f:
                        if sid in line and any(p in line for p in SYNC_COMPLETE_PREFIXES):
                            return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def _walk(obj, counter):
    if isinstance(obj, dict):
        if obj.get('type') == 'image':
            counter[0] += 1
            return {'type': 'text', 'text': '[image stripped]'}
        return {k: _walk(v, counter) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(x, counter) for x in obj]
    return obj


def do_strip(path: Path, backup: bool):
    orig_size = path.stat().st_size
    if backup:
        bak = path.with_suffix(path.suffix + '.bak')
        shutil.copy2(path, bak)
    counter = [0]
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(path, 'r', encoding='utf-8') as fi, \
         open(tmp, 'w', encoding='utf-8', newline='\n') as fo:
        for line in fi:
            line = line.rstrip('\n')
            if not line:
                fo.write('\n')
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                fo.write(line + '\n')
                continue
            fo.write(json.dumps(_walk(rec, counter), ensure_ascii=False) + '\n')
    os.replace(tmp, path)
    return counter[0], orig_size, path.stat().st_size


def main():
    ap = argparse.ArgumentParser(
        description='Strip image blocks from a Claude Code session JSONL.',
    )
    ap.add_argument('session', help='Path to session JSONL or session-id substring')
    ap.add_argument('--no-backup', action='store_true', help='Skip writing .bak file')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--wait-for-sync', metavar='SID',
                    help='Wait up to 20s for sync.log to show completion for SID before stripping')
    ap.add_argument('--skip-if-clean', action='store_true',
                    help='Exit 0 immediately if file has no image blocks')
    args = ap.parse_args()

    path = resolve_session_path(args.session)

    if args.skip_if_clean and not file_has_images(path):
        if not args.quiet:
            print(f'{path.name}: no image blocks, skipping')
        return

    if args.wait_for_sync:
        if not wait_for_sync(args.wait_for_sync):
            sys.exit(f'Timed out waiting for sync of {args.wait_for_sync}; not stripping.')

    n, before, after = do_strip(path, backup=not args.no_backup)
    if not args.quiet:
        pct = (100 * (before - after) / before) if before else 0
        print(f'{path.name}: stripped {n} image block(s), '
              f'{before:,} -> {after:,} bytes ({pct:.1f}% smaller)')


if __name__ == '__main__':
    main()
