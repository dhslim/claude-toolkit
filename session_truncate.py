"""session_truncate.py — trim a Claude Code session JSONL to a recent-turns window.

MOTIVATION
    A session run through claude_proxy.py can grow far past the model's context
    window (the proxy trims at request-time). That makes the raw JSONL too big to
    resume WITHOUT the proxy — and Remote Control (/rc) requires no proxy. This
    tool pre-trims a *fork* of the session to the most-recent ~KEEP_TARGET tokens
    (verbatim, no summarization), so the fork loads proxy-free and /rc works.

    Trim, don't summarize — same philosophy as claude_proxy.py, applied once to a
    file instead of continuously to API requests. Old turns are dropped from the
    fork but remain in the original JSONL + the warehouse (recoverable via /mgo).

DESIGN
    Two proven halves, glued:
      - claude_proxy.py     -> turn-group slice logic (reused/adapted below)
      - session_transplant.py -> valid-new-session plumbing (used in a later increment)

INCREMENT 1 (this commit): READ-ONLY ANALYZER.
    Parses a session JSONL, groups the conversation into real-user turn groups,
    finds where a KEEP_TARGET cut lands, and reports what survives + structural
    sanity (first-kept-is-user, tool_use/tool_result balance). Writes NOTHING.

    Usage:
        python session_truncate.py --analyze <session.jsonl> [--keep 400000]

NOTE on tokens: this increment uses a chars/4 estimate (same cheap heuristic as
claude_proxy._estimate_group_tokens). count_tokens under-reports vs real, and
chars/4 under-reports vs count_tokens, so the REAL truncator (a later increment)
must verify with a conservative margin or the count_tokens endpoint. For proving
the *slice logic*, the estimate is sufficient.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Union

DEFAULT_KEEP_TARGET = 400_000  # est tokens of most-recent history to keep

Line = Union[dict, str]


# --- reused from claude_proxy.py, adapted for the JSONL line wrapper ----------
# In the API format a message is {role, content}. In the JSONL each line is
# {type, message: {role, content}, uuid, parentUuid, ...}. So we look at
# line["message"] for the role/content checks.

def _api_msg(line: dict) -> dict:
    m = line.get("message")
    return m if isinstance(m, dict) else {}


def is_real_user_line(line: dict) -> bool:
    """True if this JSONL line is a real human user turn (not a tool_result)."""
    if not isinstance(line, dict) or line.get("type") != "user":
        return False
    content = _api_msg(line).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return False  # tool output dressed as a user msg — not a real turn
        return True
    return False


def is_conversation_line(line: dict) -> bool:
    """Conversation lines are what the model actually sees: user + assistant.

    Everything else (mode, permission-mode, file-history-snapshot, attachment,
    ai-title, last-prompt, system, queue-operation, bridge-session) is Claude
    Code's internal machinery, handled separately.
    """
    return isinstance(line, dict) and line.get("type") in ("user", "assistant")


def split_into_turn_groups(conv_lines: list[dict]) -> list[list[dict]]:
    """Group conversation lines by real-user-initiated turn.

    Each group starts with a real user line and holds the assistant response(s)
    + tool round-trips until the next real user line. Dropping WHOLE groups
    preserves alternation, tool_use<->tool_result pairing, and first-msg-is-user.
    (Direct port of claude_proxy.split_into_turn_groups.)
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    for line in conv_lines:
        if is_real_user_line(line) and current:
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)
    return groups


# --- token estimate (chars/4, matching the proxy's cheap heuristic) -----------

def est_tokens(line: dict) -> int:
    payload = line.get("message", line)
    try:
        return len(json.dumps(payload, ensure_ascii=False)) // 4
    except (TypeError, ValueError):
        return len(str(payload)) // 4


def _content_block_types(line: dict) -> list[str]:
    c = _api_msg(line).get("content")
    if isinstance(c, list):
        return [b.get("type") for b in c if isinstance(b, dict)]
    return []


def _preview(line: dict, width: int = 60) -> str:
    c = _api_msg(line).get("content")
    text = ""
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                text = b["text"]
                break
    text = " ".join(text.split())
    return (text[: width - 1] + "…") if len(text) > width else (text or "<no-text>")


# --- loading ------------------------------------------------------------------

def load_jsonl(path: Path) -> list[Line]:
    out: list[Line] = []
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                out.append(ln)  # keep raw; never silently drop
    return out


# --- the analysis (read-only) -------------------------------------------------

def analyze(path: Path, keep_target: int) -> None:
    lines = load_jsonl(path)
    conv = [l for l in lines if isinstance(l, dict) and is_conversation_line(l)]
    groups = split_into_turn_groups(conv)
    gtok = [sum(est_tokens(l) for l in g) for g in groups]
    total = sum(gtok)

    # Walk newest -> oldest, keep whole groups until adding one more would exceed
    # keep_target (but always keep at least the last group).
    kept = 0
    keep_from = len(groups)
    for i in range(len(groups) - 1, -1, -1):
        if kept + gtok[i] > keep_target and kept > 0:
            break
        kept += gtok[i]
        keep_from = i
    kept_groups = groups[keep_from:]
    kept_lines = [l for g in kept_groups for l in g]

    # structural sanity of the kept slice
    first_ok = bool(kept_groups) and is_real_user_line(kept_groups[0][0])
    n_tool_use = sum(1 for l in kept_lines if "tool_use" in _content_block_types(l))
    n_tool_res = sum(1 for l in kept_lines if "tool_result" in _content_block_types(l))

    from collections import Counter
    type_counts = Counter(l.get("type") for l in lines if isinstance(l, dict))

    print(f"file:                 {path}")
    print(f"total JSONL lines:    {len(lines):,}")
    print(f"  line types:         {dict(type_counts)}")
    print(f"conversation lines:   {len(conv):,}")
    print(f"real-user turn groups:{len(groups):,}")
    print(f"total est tokens:     {total:,}   (chars/4 estimate)")
    print(f"KEEP_TARGET:          {keep_target:,}")
    print("-" * 60)
    print(f"CUT at group index:   {keep_from}  (0 = keep everything)")
    print(f"  keep groups:        {len(kept_groups):,}")
    print(f"  drop groups:        {keep_from:,}")
    print(f"  keep est tokens:    {kept:,}")
    print(f"  drop est tokens:    {total - kept:,}")
    print(f"  keep conv lines:    {len(kept_lines):,}")
    print("-" * 60)
    print("STRUCTURAL SANITY OF KEPT SLICE:")
    print(f"  first kept line is a real user turn: {first_ok}  {'OK' if first_ok else 'BAD'}")
    print(f"  tool_use blocks:    {n_tool_use}")
    print(f"  tool_result blocks: {n_tool_res}")
    bal = n_tool_use == n_tool_res
    print(f"  tool pairing balanced: {bal}  {'OK' if bal else 'CHECK (may be a pending/leaf tool_use)'}")
    if kept_groups:
        print("-" * 60)
        print(f"  first kept turn: {_preview(kept_groups[0][0])!r}")
        print(f"  last  kept turn: {_preview(kept_groups[-1][0])!r}")
    print("-" * 60)
    print("(read-only: nothing written)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze/trim a Claude Code session JSONL.")
    ap.add_argument("--analyze", metavar="JSONL", help="path to a session JSONL to analyze (read-only)")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP_TARGET, help="est tokens of recent history to keep")
    args = ap.parse_args()
    if not args.analyze:
        ap.print_help()
        return 2
    path = Path(args.analyze).expanduser()
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 1
    analyze(path, args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
