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


def _tool_ids(line: dict) -> tuple[list[str], list[str]]:
    """(tool_use ids, tool_result tool_use_ids) present in this line."""
    uses: list[str] = []
    results: list[str] = []
    c = _api_msg(line).get("content")
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                uses.append(b["id"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id"):
                results.append(b["tool_use_id"])
    return uses, results


def check_tool_pairing(kept_lines: list[dict]) -> dict:
    """ID-based tool_use<->tool_result pairing check on the kept slice.

    orphan_results : tool_result ids with NO matching tool_use in the slice.
                     THE dangerous case a bad cut creates — the API rejects a
                     tool_result whose tool_use isn't present.
    dangling_uses  : tool_use ids with NO matching tool_result in the slice.
    dangling_at_leaf: True iff every dangling tool_use lives in the last line
                     that has any tool_use (benign: a mid-tool leaf that exists
                     in the original session too — not caused by truncation).
    """
    per_line: list[tuple[int, list[str], list[str]]] = []
    all_use, all_res = [], []
    for idx, l in enumerate(kept_lines):
        u, r = _tool_ids(l)
        all_use += u
        all_res += r
        per_line.append((idx, u, r))
    use_set, res_set = set(all_use), set(all_res)
    orphan_results = [rid for rid in all_res if rid not in use_set]
    dangling_uses = [uid for uid in all_use if uid not in res_set]
    last_use_idx = max((idx for idx, u, _ in per_line if u), default=None)
    dangling_at_leaf = bool(dangling_uses)
    for uid in dangling_uses:
        holders = [idx for idx, u, _ in per_line if uid in u]
        if any(h != last_use_idx for h in holders):
            dangling_at_leaf = False
            break
    return {
        "orphan_results": orphan_results,
        "dangling_uses": dangling_uses,
        "dangling_at_leaf": dangling_at_leaf,
    }


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
    pairing = check_tool_pairing(kept_lines)

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
    orphans = pairing["orphan_results"]
    dangling = pairing["dangling_uses"]
    print(f"  orphan tool_results (result w/o its tool_use): {len(orphans)}  "
          f"{'OK' if not orphans else 'BAD -> the API would reject this cut'}")
    print(f"  dangling tool_uses  (use w/o its result):      {len(dangling)}")
    if dangling:
        where = ("all at the leaf — benign, same as the original session"
                 if pairing["dangling_at_leaf"] else
                 "NOT all at the leaf — needs handling before writing")
        print(f"    -> {where}")
    slice_api_safe = (not orphans) and (not dangling or pairing["dangling_at_leaf"])
    print(f"  SLICE IS API-SAFE:  {slice_api_safe}  {'OK' if slice_api_safe else 'NEEDS WORK'}")
    if kept_groups:
        print("-" * 60)
        print(f"  first kept turn: {_preview(kept_groups[0][0])!r}")
        print(f"  last  kept turn: {_preview(kept_groups[-1][0])!r}")
    print("-" * 60)
    print("(read-only: nothing written)")


# --- Increment 2a: WRITE a truncated fork -------------------------------------
# Non-destructive: reads src, writes a NEW file at out_path. Never touches the
# source or any running process. Does NOT yet retarget cwd or compact the header
# for the resume picker (that's Increment 2b) — this proves the core transform.

def _first_user_index(lines: list) -> "int | None":
    for i, l in enumerate(lines):
        if isinstance(l, dict) and l.get("type") == "user":
            return i
    return None


def _find_cut(groups: list, gtok: list, keep_target: int) -> tuple:
    kept = 0
    keep_from = len(groups)
    for i in range(len(groups) - 1, -1, -1):
        if kept + gtok[i] > keep_target and kept > 0:
            break
        kept += gtok[i]
        keep_from = i
    return keep_from, kept


def truncate(src_path: Path, keep_target: int, out_path: Path) -> dict:
    import uuid as _uuid
    lines = load_jsonl(src_path)
    conv = [l for l in lines if isinstance(l, dict) and is_conversation_line(l)]
    groups = split_into_turn_groups(conv)
    gtok = [sum(est_tokens(l) for l in g) for g in groups]
    keep_from, kept_tok = _find_cut(groups, gtok, keep_target)
    kept_groups = groups[keep_from:]
    if not kept_groups:
        raise ValueError("keep_target too small — nothing kept")

    cut_uuid = kept_groups[0][0].get("uuid")
    cut_idx = next((i for i, l in enumerate(lines)
                    if isinstance(l, dict) and l.get("uuid") == cut_uuid), None)
    first_user_idx = _first_user_index(lines)
    if cut_idx is None or first_user_idx is None:
        raise ValueError("could not locate cut line / first user message")

    header = lines[:first_user_idx]                 # pre-conversation state lines
    body = [dict(l) if isinstance(l, dict) else l   # shallow-copy so we don't mutate src objects
            for l in lines[cut_idx:]]
    if isinstance(body[0], dict):
        body[0]["parentUuid"] = None                # re-root the new first message

    new_sid = str(_uuid.uuid4())
    out_lines = []
    for l in header + body:
        if isinstance(l, dict) and "sessionId" in l:
            l = {**l, "sessionId": new_sid}
        out_lines.append(l)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        for l in out_lines:
            f.write((json.dumps(l, ensure_ascii=False) if isinstance(l, dict) else l) + "\n")

    return {
        "out_path": str(out_path),
        "new_session_id": new_sid,
        "src_lines": len(lines),
        "out_lines": len(out_lines),
        "header_lines": len(header),
        "kept_groups": len(kept_groups),
        "dropped_groups": keep_from,
        "kept_est_tokens": kept_tok,
    }


def verify_output(out_path: Path) -> dict:
    """Re-parse a written file and confirm it's structurally API-safe."""
    lines = load_jsonl(out_path)
    conv = [l for l in lines if isinstance(l, dict) and is_conversation_line(l)]
    first = conv[0] if conv else None
    first_is_user = bool(first) and is_real_user_line(first)
    first_rerooted = bool(first) and first.get("parentUuid") is None
    pairing = check_tool_pairing(conv)
    api_safe = (first_is_user and first_rerooted
                and not pairing["orphan_results"]
                and (not pairing["dangling_uses"] or pairing["dangling_at_leaf"]))
    return {
        "lines": len(lines),
        "conv_lines": len(conv),
        "first_conv_is_user": first_is_user,
        "first_conv_rerooted": first_rerooted,
        "orphan_results": len(pairing["orphan_results"]),
        "dangling_uses": len(pairing["dangling_uses"]),
        "dangling_at_leaf": pairing["dangling_at_leaf"],
        "API_SAFE": api_safe,
    }


def main() -> int:
    # Session text is full of Korean / em-dashes / emoji; force UTF-8 so the
    # console (cp949 on Korean Windows) never crashes on a print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Analyze/trim a Claude Code session JSONL.")
    ap.add_argument("--analyze", metavar="JSONL", help="path to a session JSONL to analyze (read-only)")
    ap.add_argument("--truncate", metavar="JSONL", help="path to a source session JSONL to truncate (writes --out)")
    ap.add_argument("--out", metavar="JSONL", help="output path for the truncated fork (required with --truncate)")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP_TARGET, help="est tokens of recent history to keep")
    args = ap.parse_args()

    if args.analyze:
        path = Path(args.analyze).expanduser()
        if not path.is_file():
            print(f"error: not a file: {path}", file=sys.stderr)
            return 1
        analyze(path, args.keep)
        return 0

    if args.truncate:
        src = Path(args.truncate).expanduser()
        if not src.is_file():
            print(f"error: not a file: {src}", file=sys.stderr)
            return 1
        if not args.out:
            print("error: --truncate requires --out", file=sys.stderr)
            return 2
        out = Path(args.out).expanduser()
        if out.resolve() == src.resolve():
            print("error: --out must differ from source (never overwrite the original)", file=sys.stderr)
            return 2
        info = truncate(src, args.keep, out)
        print("TRUNCATED (non-destructive: source untouched):")
        for k, v in info.items():
            print(f"  {k:16}: {v}")
        print("SELF-VERIFY (re-parsed the written file):")
        v = verify_output(out)
        for k, val in v.items():
            print(f"  {k:18}: {val}")
        return 0 if v["API_SAFE"] else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
