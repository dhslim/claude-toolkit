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


def last_assistant_message(transcript_path):
    """Inspect the final assistant message. Returns (has_tool_use, last_text_line).

    (None, "") if there is no assistant message / it can't be read.

    has_tool_use=True means the last assistant message ended on a tool call — i.e.
    it's an intermediate tool step, OR the genuine text-final reply simply hasn't
    been flushed to the transcript yet. Either way it is NOT a finished text
    response, so the stamp rule does not apply to it. We only judge a pure-text
    final message. No counting, no window — the message's own shape tells us
    whether it's the kind of message that must carry a stamp.
    """
    last = None
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
                if entry.get("type") == "assistant":
                    last = entry
    except FileNotFoundError:
        return None, ""
    if last is None:
        return None, ""
    content = last.get("message", {}).get("content", [])
    if isinstance(content, str):
        text, has_tool = content.strip(), False
    else:
        has_tool = any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        )
        text = "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    last_line = text.splitlines()[-1].strip() if text else ""
    return has_tool, last_line


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

    has_tool, last_line = last_assistant_message(transcript_path)
    if has_tool is None:
        return  # no assistant message to inspect → allow
    if has_tool:
        return  # ended on a tool call (intermediate, or reply not flushed yet) → allow
    if not last_line:
        return  # text-final but empty → nothing to enforce → allow
    if STAMP_RE.search(last_line):
        return  # finished text reply is stamped → allow

    print(json.dumps({"decision": "block", "reason": REASON}))


if __name__ == "__main__":
    main()
