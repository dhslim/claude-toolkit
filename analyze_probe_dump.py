"""Analyze a probe-dump JSON file to inform Option 3 design decisions.

Reports:
  1. Top-level request structure (model, tools, system, messages count, bytes)
  2. All cache_control marker locations with details
  3. Turn-group breakdown (reusing claude_proxy's split logic)
  4. Per-group token/byte estimates + cumulative totals
  5. HEAD_GROUP_COUNT candidates: groups needed to reach 50k / 100k / 150k / 200k "tokens"
     ("tokens" here is estimated as chars // 4, which over-estimates slightly vs real BPE
      but is fine for picking a head threshold)
  6. Previews of the first N real user messages so we know what "foundation" looks like
  7. Any unusual content-block types worth flagging

Run:
    python analyze_probe_dump.py probe-dump-<ts>.json
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent

# Reuse split_into_turn_groups / is_real_user_message from the proxy for consistency.
spec = importlib.util.spec_from_file_location("claude_proxy", HERE / "claude_proxy.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


def estimate_tokens(s: str) -> int:
    """Rough BPE estimate: chars // 4. Fine for threshold picking."""
    return max(1, len(s) // 4)


def message_size_bytes_and_text(msg: dict) -> tuple[int, int, set[str]]:
    """Return (byte_size, estimated_tokens, content_block_types) for a message."""
    raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    total_chars = 0
    block_types: set[str] = set()
    content = msg.get("content")
    if isinstance(content, str):
        total_chars += len(content)
        block_types.add("str")
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            block_types.add(b.get("type") or "unknown")
            if b.get("type") == "text":
                total_chars += len(b.get("text") or "")
            elif b.get("type") == "tool_use":
                # tool_use includes input as dict — count its JSON size
                input_json = json.dumps(b.get("input") or {}, ensure_ascii=False)
                total_chars += len(input_json)
            elif b.get("type") == "tool_result":
                tc = b.get("content")
                if isinstance(tc, str):
                    total_chars += len(tc)
                elif isinstance(tc, list):
                    for sub in tc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            total_chars += len(sub.get("text") or "")
            elif b.get("type") == "thinking":
                total_chars += len(b.get("thinking") or "")
    return len(raw), estimate_tokens(str(total_chars and total_chars or "")) if False else estimate_tokens(" " * total_chars), block_types


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <probe-dump.json>")
        sys.exit(1)

    dump_path = Path(sys.argv[1])
    if not dump_path.exists():
        # Try interpreting as a filename in the same directory
        dump_path = HERE / sys.argv[1]
    if not dump_path.exists():
        print(f"File not found: {sys.argv[1]}")
        sys.exit(1)

    raw = dump_path.read_bytes()
    body = json.loads(raw.decode("utf-8"))

    print("=" * 72)
    print(f"probe dump: {dump_path.name}")
    print("=" * 72)

    # 1. Top-level structure
    print("\n[1] Top-level request structure")
    print(f"  raw bytes:    {len(raw):,}")
    print(f"  model:        {body.get('model')}")
    print(f"  max_tokens:   {body.get('max_tokens')}")
    print(f"  stream:       {body.get('stream')}")
    tools = body.get("tools") or []
    print(f"  tools:        {len(tools)} tools")
    if tools:
        tool_names = [t.get("name", "?") for t in tools[:8] if isinstance(t, dict)]
        print(f"                first few: {', '.join(tool_names)}")
    sys_field = body.get("system")
    if isinstance(sys_field, list):
        print(f"  system:       list of {len(sys_field)} blocks")
        for i, b in enumerate(sys_field):
            if isinstance(b, dict):
                t = b.get("text", "")
                print(f"    [{i}] type={b.get('type')} len={len(t):,} chars "
                      f"cache_control={'cache_control' in b}")
    elif isinstance(sys_field, str):
        print(f"  system:       string, {len(sys_field):,} chars")
    else:
        print(f"  system:       {type(sys_field).__name__}")
    messages = body.get("messages") or []
    print(f"  messages:     {len(messages)}")

    # 2. Cache control markers
    print("\n[2] Cache control markers (exact locations)")
    def walk_cc(obj, path: str, out: list):
        if isinstance(obj, dict):
            if "cache_control" in obj:
                out.append((path, obj.get("type"), obj.get("cache_control")))
            for k, v in obj.items():
                if k == "cache_control":
                    continue
                walk_cc(v, f"{path}.{k}", out)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk_cc(v, f"{path}[{i}]", out)

    cc_list: list = []
    walk_cc(body, "body", cc_list)
    print(f"  found {len(cc_list)} cache_control markers:")
    for path, btype, cc in cc_list:
        print(f"    {path}  (block.type={btype}, cache_control={cc})")

    # 3. Turn-group breakdown
    print("\n[3] Turn groups (via claude_proxy.split_into_turn_groups)")
    groups = cp.split_into_turn_groups(messages)
    print(f"  total groups: {len(groups)}")
    print(f"  avg msgs/group: {len(messages) / max(1, len(groups)):.1f}")

    # 4. Per-group stats
    def group_stats(group: list) -> tuple[int, int, set[str]]:
        total_bytes = 0
        total_text = 0
        types: set[str] = set()
        for m in group:
            content = m.get("content")
            if isinstance(content, str):
                total_text += len(content)
                types.add("str")
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    types.add(b.get("type") or "unknown")
                    if b.get("type") == "text":
                        total_text += len(b.get("text") or "")
                    elif b.get("type") == "tool_use":
                        total_text += len(json.dumps(b.get("input") or {}, ensure_ascii=False))
                    elif b.get("type") == "tool_result":
                        tc = b.get("content")
                        if isinstance(tc, str):
                            total_text += len(tc)
                        elif isinstance(tc, list):
                            for sub in tc:
                                if isinstance(sub, dict) and sub.get("type") == "text":
                                    total_text += len(sub.get("text") or "")
                    elif b.get("type") == "thinking":
                        total_text += len(b.get("thinking") or "")
            total_bytes += len(json.dumps(m, ensure_ascii=False).encode("utf-8"))
        return total_bytes, estimate_tokens(" " * total_text) if False else (total_text // 4), types

    per_group: list[tuple[int, int, set[str]]] = []
    for g in groups:
        per_group.append(group_stats(g))

    cum_tokens = 0
    cum_bytes = 0
    thresholds = [50_000, 100_000, 150_000, 200_000, 300_000, 500_000]
    hits: dict[int, int | None] = {t: None for t in thresholds}

    for i, (b, t, _) in enumerate(per_group):
        cum_bytes += b
        cum_tokens += t
        for thresh in thresholds:
            if hits[thresh] is None and cum_tokens >= thresh:
                hits[thresh] = i + 1  # number of groups needed to cross threshold

    print("\n[4] HEAD_GROUP_COUNT candidates (cumulative tokens from group 0)")
    for thresh in thresholds:
        n = hits[thresh]
        if n is not None:
            # show what's actually cumulated at that point
            bsum = sum(g[0] for g in per_group[:n])
            tsum = sum(g[1] for g in per_group[:n])
            print(f"  reach {thresh:>7,} tok: after {n:>3} groups  "
                  f"(actual: {tsum:>7,} tok ≈ {bsum:>12,} B)")
        else:
            print(f"  reach {thresh:>7,} tok: never reached in {len(groups)} groups")

    total_tokens = sum(g[1] for g in per_group)
    total_bytes = sum(g[0] for g in per_group)
    print(f"\n  full session totals: {total_tokens:,} tok ≈ {total_bytes:,} B in messages")

    # 5. First 10 group previews (so we see what "head" would contain)
    print("\n[5] First 10 group previews (what the HEAD would look like)")
    for i in range(min(10, len(groups))):
        preview = cp._group_preview(groups[i], width=60)
        b, t, types = per_group[i]
        print(f"  [{i:>3}] ({len(groups[i])} msgs, {t:,} tok, types={sorted(types)}): \"{preview}\"")

    # 6. Last 5 group previews (tail)
    print("\n[6] Last 5 group previews (what the TAIL contains)")
    start = max(0, len(groups) - 5)
    for i in range(start, len(groups)):
        preview = cp._group_preview(groups[i], width=60)
        b, t, types = per_group[i]
        print(f"  [{i:>3}] ({len(groups[i])} msgs, {t:,} tok, types={sorted(types)}): \"{preview}\"")

    # 7. Groups above a "huge" threshold (anything that might blow up head or budget)
    print("\n[7] Giant groups (>50k tok, worth special-casing)")
    giants = [(i, g[1], g[0]) for i, g in enumerate(per_group) if g[1] > 50_000]
    if giants:
        print(f"  found {len(giants)} groups >50k tokens")
        for i, t, b in giants[:20]:
            preview = cp._group_preview(groups[i], width=40)
            print(f"    [{i:>4}] {t:>7,} tok, {b:>12,} B — \"{preview}\"")
    else:
        print("  no groups above 50k tokens — head candidacy is uniform")

    # 8. All unique content block types across the body
    print("\n[8] All unique content block types across messages")
    all_types: set[str] = set()
    for _, _, types in per_group:
        all_types |= types
    print(f"  {sorted(all_types)}")


if __name__ == "__main__":
    main()
