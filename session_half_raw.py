#!/usr/bin/env python3
"""Keep the back half of a session JSONL as raw, drop the front half entirely.

Rationale: /compact is lossy — it rewrites the whole conversation into a
summary, which destroys tool-call granularity needed for active problem
solving. This script instead slices the file at a byte-percentage point,
snaps forward to the next real human user message, and writes a new
session file containing only the retained tail.

Usage:
    python session_half_raw.py <jsonl_path> [fraction]
        fraction defaults to 0.5 (keep back 50%)

The first retained user message has parentUuid nulled, and every line gets
its sessionId rewritten to a fresh UUID so the new file shows up as a
separate entry in the resume picker.
"""
import json
import sys
import uuid
from pathlib import Path


COMPACT_MARKER = "This session is being continued from a previous conversation"


def is_human_user(obj: dict) -> bool:
    """True iff this is a human-typed user message (not a tool_result carrier)."""
    if obj.get("type") != "user":
        return False
    msg = obj.get("message", {})
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
        return True
    return False


def _content_text(obj: dict) -> str:
    msg = obj.get("message", {})
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def is_compact_root(obj: dict) -> bool:
    """True iff this is the synthetic user message /compact emits."""
    if not is_human_user(obj):
        return False
    return COMPACT_MARKER in _content_text(obj)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: session_half_raw.py <jsonl_path> [fraction]")
        sys.exit(1)

    src = Path(sys.argv[1])
    fraction = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5

    if not src.exists():
        print(f"ERROR: {src} not found")
        sys.exit(1)

    total_size = src.stat().st_size
    midpoint = int(total_size * fraction)

    # Pass 1: index every line with its byte offset + parsed obj (if valid).
    entries: list[tuple[int, dict]] = []
    header_permission = None
    byte_offset = 0
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            line_bytes = len(line.encode("utf-8"))
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                byte_offset += line_bytes
                continue
            if header_permission is None and obj.get("type") == "permission-mode":
                header_permission = obj
            entries.append((byte_offset, obj))
            byte_offset += line_bytes

    # Fraction-based cut: first human user message at/past midpoint.
    cut_idx = None
    for i, (offset, obj) in enumerate(entries):
        if offset >= midpoint and is_human_user(obj):
            cut_idx = i
            break

    if cut_idx is None:
        print("ERROR: no human user message found past midpoint")
        sys.exit(1)

    cut_offset = entries[cut_idx][0]
    retained = [dict(obj) for _, obj in entries[cut_idx:]]
    retained[0]["parentUuid"] = None

    # Re-link any /compact boundaries inside the retained range.
    # /compact emits a `system` message with subtype=compact_boundary whose
    # parentUuid is null (starting a new tree) but whose logicalParentUuid
    # points to the pre-compact last assistant message. If that target uuid
    # is present in the retained range, we can rewrite parentUuid to match
    # logicalParentUuid and stitch the two trees into one, making the raw
    # pre-compact turns visible again on resume.
    retained_uuids = {o.get("uuid") for o in retained if o.get("uuid")}
    relinks = 0
    for obj in retained:
        if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
            logical = obj.get("logicalParentUuid")
            if logical and logical in retained_uuids and obj.get("parentUuid") is None:
                obj["parentUuid"] = logical
                relinks += 1
    cut_reason = f"fraction={fraction:.0%}, relinked {relinks} compact boundary"

    new_sid = str(uuid.uuid4())
    out_path = src.parent / f"{new_sid}.jsonl"

    first_uuid = retained[0].get("uuid") or new_sid
    first_ts = retained[0].get("timestamp") or ""
    synthetic_snapshot = {
        "type": "file-history-snapshot",
        "messageId": first_uuid,
        "snapshot": {
            "messageId": first_uuid,
            "trackedFileBackups": {},
            "timestamp": first_ts,
        },
        "isSnapshotUpdate": False,
    }

    with out_path.open("w", encoding="utf-8") as f:
        if header_permission is not None:
            hp = dict(header_permission)
            hp["sessionId"] = new_sid
            f.write(json.dumps(hp, ensure_ascii=False) + "\n")
        f.write(json.dumps(synthetic_snapshot, ensure_ascii=False) + "\n")

        for obj in retained:
            if "sessionId" in obj:
                obj["sessionId"] = new_sid
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    new_size = out_path.stat().st_size
    print(f"Source:      {src}")
    print(f"  size:      {total_size:,} bytes")
    print(f"  midpoint:  {midpoint:,} bytes ({fraction:.0%})")
    print(f"  cut at:    {cut_offset:,} bytes  [{cut_reason}]")
    print(f"New file:    {out_path}")
    print(f"  size:      {new_size:,} bytes ({new_size / total_size:.1%} of source)")
    print(f"  lines:     {len(retained) + 2}")
    print(f"  sessionId: {new_sid}")
    print()
    print(f"First retained user message:")
    msg_content = retained[0].get("message", {}).get("content", "")
    if isinstance(msg_content, str):
        preview = msg_content[:200].replace("\n", " ")
    else:
        preview = json.dumps(msg_content, ensure_ascii=False)[:200]
    print(f"  {preview}")


if __name__ == "__main__":
    main()
