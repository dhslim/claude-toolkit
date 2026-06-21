#!/usr/bin/env python3
"""Stop hook — deterministically enforces the closing KST timestamp.

A CLAUDE.md instruction asking Claude to end every reply with the injected
`<current-time>` is only probabilistic — on long sessions it gets buried and
dropped. This hook is the backstop: when Claude tries to end its turn, we read
the message it just produced and, if the final line is NOT a backtick-wrapped
`...KST` timestamp, we BLOCK the stop and tell Claude to append it. Claude then
continues and emits the stamp.

Loop safety: Claude Code sets `stop_hook_active: true` on the continuation that
results from a Stop-hook block. We allow immediately in that case, so the worst
case is exactly ONE extra round-trip per offending turn — never an infinite loop.

Stop-hook contract:
  stdin  : JSON with `transcript_path` and `stop_hook_active`
  block  : print {"decision": "block", "reason": "..."} and exit 0
  allow  : exit 0 with no output
"""

import json
import re
import sys

# Matches the required closing line, e.g.  `2026-06-20(Sat) 19:14:46 KST`
STAMP_RE = re.compile(r"`\d{4}-\d{2}-\d{2}\([A-Za-z]{3}\) \d{2}:\d{2}:\d{2} KST`")

REASON = (
    "Your response is missing its closing timestamp. Append the injected "
    "<current-time> value as the final line, wrapped in single backticks so it "
    "renders as inline code, e.g. `2026-06-20(Sat) 19:14:46 KST`. Use the value "
    "from this turn's <current-time> verbatim; do not guess. Reply with only "
    "that line."
)


def recent_assistant_last_lines(transcript_path, n=3):
    """Last non-empty line of each of the last `n` assistant messages that had text.

    We look at the last few — not just the very last — because on tool-heavy turns
    the final stamped message can be read a beat before it's flushed, landing the
    check on an intermediate tool-preamble or an unstamped stop-hook continuation
    and false-blocking. As long as one recent message carries the stamp, we're fine.
    """
    last_lines = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, str):
                    joined = content.strip()
                else:
                    joined = "".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                # Skip tool-only turns (no text block).
                if joined:
                    last_lines.append(joined.splitlines()[-1].strip())
    except FileNotFoundError:
        return []
    return last_lines[-n:]


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # can't parse input → fail open (allow stop)

    # Already a continuation from this hook → don't block again (loop guard).
    if payload.get("stop_hook_active"):
        return

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return  # nothing to inspect → fail open

    recent = recent_assistant_last_lines(transcript_path, n=3)
    if not recent:
        return  # no assistant text this turn (e.g. pure tool turn) → allow

    # Allow if ANY of the last few assistant messages ended with a valid stamp;
    # only block on a genuine, sustained miss (none of them stamped).
    if any(STAMP_RE.search(line) for line in recent):
        return

    print(json.dumps({"decision": "block", "reason": REASON}))


if __name__ == "__main__":
    main()
