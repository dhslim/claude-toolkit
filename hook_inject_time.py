#!/usr/bin/env python3
"""UserPromptSubmit hook — injects current KST timestamp + session id into the turn.

The injected line is read by a global CLAUDE.md rule that asks Claude to end
each response with this exact string. Stdout from a UserPromptSubmit hook
is appended to the user's prompt as additional context for the model.

We also inject the reminder EVERY turn (not only via session-start CLAUDE.md) so
it lands at the most-recent context position — recency makes Claude far more
likely to actually append the stamp. The Stop hook `hook_enforce_timestamp.py`
is the deterministic backstop that catches the turns where it still forgets.

Format: <current-time>YYYY-MM-DD(Day) HH:MM:SS KST #abcd1234</current-time>
where Day is the 3-letter weekday and abcd1234 is the session id's first 8 chars.

WHY THE SESSION ID IS THERE
    The stamp doubles as a handle for finding a turn again in the MongoDB
    warehouse. The timestamp alone is a decent locator but a poor key:

      - it shares a namespace with ordinary prose, so a doc that merely QUOTES a
        timestamp is indistinguishable from the turn itself (INSTALL.md's own
        example stamp shows up 7 times in the warehouse)
      - ~20% of stamps appear in more than one session document, because
        `cfork`/`cread` mint a new session id while copying the transcript
      - a substring regex over nested array text can't use an index, so every
        lookup is a collection scan

    session_id is a real UUID and is indexed, so pairing the two turns the
    lookup from a scan into a seek, and disambiguates forks. Eight hex chars is
    ample to identify a session among a few hundred.

    The separator is a plain ASCII '#': this terminal is cp949, where a middle
    dot renders as mojibake (the same failure as the em-dash in c23ef65), and
    '#' — unlike '|' — carries no meaning inside a regex, so the stamp can be
    pasted into a query without escaping.

    Query it in Atlas (Aggregations tab — `find` returns the whole 6000-message
    document, which is unusable; see the saved "find turn by timestamp"):

      [{"$match": {"session_id": {"$regex": "^abcd1234"}}},
       {"$unwind": "$messages"},
       {"$match": {"messages.content.text": {"$regex": "14:52:32 KST"}}},
       {"$project": {"_id": 0, "role": "$messages.role",
                     "text": "$messages.content.text"}}]

FAIL-OPEN, ALWAYS
    The session id is strictly best-effort. This hook runs synchronously on every
    prompt with a 2s timeout, and it previously did no I/O at all. Reading stdin
    introduces a dependency on the harness handing us a payload, so every failure
    path — no stdin, malformed JSON, missing key — degrades to the bare timestamp
    rather than losing the stamp or stalling the turn.
"""

import json
import sys
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def short_session_id() -> str:
    """First 8 chars of the session id, or '' if unavailable for any reason."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return ''  # no stdin / not JSON → bare timestamp
    sid = payload.get('session_id') or ''
    return sid[:8] if isinstance(sid, str) else ''


if __name__ == "__main__":
    now = datetime.now(KST)
    # %a = abbreviated weekday name (Mon, Tue, Wed, Thu, Fri, Sat, Sun)
    stamp = now.strftime('%Y-%m-%d(%a) %H:%M:%S') + ' KST'

    sid = short_session_id()
    if sid:
        stamp = f'{stamp} #{sid}'

    print(f"<current-time>{stamp}</current-time>")
    print(
        "<reminder>End this response with the timestamp above as the final "
        f"line, wrapped in single backticks so it renders as inline code: "
        f"`{stamp}`. Use the value verbatim.</reminder>"
    )
