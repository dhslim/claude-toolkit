"""One-off viewer: render what the proxy is currently forwarding to Anthropic.

Reads the current session's JSONL on disk, splits into turn groups, applies
the proxy's most-recent anchor (extracted from proxy.log), and writes an
HTML page showing kept vs dropped messages.

Usage:
    python view_current_context.py [session_jsonl_path] [output.html]

Defaults to the claude-toolkit session and ./current_context.html.
"""

import json
import re
import sys
from html import escape
from pathlib import Path

DEFAULT_JSONL = (
    Path.home() / ".claude" / "projects"
    / "C--Users-user-Desktop-claude-toolkit"
    / "d7e890bd-515e-47a5-aba1-2eb9c7f82d19.jsonl"
)
DEFAULT_OUT = Path(__file__).parent / "current_context.html"
PROXY_LOG = Path(__file__).parent / "proxy.log"


def find_latest_anchor():
    """Find the most recent SHIFT in proxy.log and return (anchor, ts_kst)."""
    try:
        lines = PROXY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None, None
    pattern = re.compile(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) KST\].+SHIFT\].+anchor \d+→(\d+).+groups (\d+)→(\d+)"
    )
    for line in reversed(lines):
        m = pattern.search(line)
        if m:
            return {
                "ts": m.group(1),
                "anchor": int(m.group(2)),
                "groups_before": int(m.group(3)),
                "groups_after": int(m.group(4)),
            }, line.strip()
    return None, None


def extract_messages(jsonl_path: Path):
    """Return ordered list of (role, content_text) for user/assistant entries."""
    out = []
    for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = obj.get("message") or {}
        role = msg.get("role") or t
        content = msg.get("content")
        text = _flatten_content(content)
        if not text and t == "user":
            # CC's user metadata entries may have empty content; skip those
            continue
        out.append({
            "role": role,
            "text": text,
            "ts": obj.get("timestamp", ""),
            "tool_use": _has_tool_use(content),
            "tool_result": _has_tool_result(content),
        })
    return out


def _flatten_content(content) -> str:
    """Turn message content (str or list of blocks) into a flat text preview."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            parts.append(b.get("text") or "")
        elif bt == "tool_use":
            name = b.get("name") or "?"
            parts.append(f"[tool_use: {name}]")
        elif bt == "tool_result":
            r = b.get("content")
            if isinstance(r, list):
                for sub in r:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append("[tool_result] " + (sub.get("text") or "")[:500])
            else:
                parts.append("[tool_result] " + str(r)[:500])
        elif bt == "thinking":
            parts.append("[thinking] " + (b.get("thinking") or "")[:400])
        elif bt == "image":
            parts.append("[image]")
    return "\n".join(parts).strip()


def _has_tool_use(content) -> bool:
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    return False


def _has_tool_result(content) -> bool:
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def split_into_turn_groups(messages):
    """Same semantics as claude_proxy.split_into_turn_groups: a group starts at a
    user message that's NOT a tool_result.
    """
    groups = []
    cur = []
    for m in messages:
        is_user = m["role"] == "user"
        # Real user message = user + NOT a tool_result wrapper
        is_real_user_start = is_user and not m["tool_result"]
        if is_real_user_start and cur:
            groups.append(cur)
            cur = []
        cur.append(m)
    if cur:
        groups.append(cur)
    return groups


def render_html(messages, groups, anchor_info, out_path: Path, jsonl_path: Path):
    kept_groups = groups[anchor_info["anchor"]:] if anchor_info else groups
    dropped_groups = groups[: anchor_info["anchor"]] if anchor_info else []

    kept_msg_count = sum(len(g) for g in kept_groups)
    dropped_msg_count = sum(len(g) for g in dropped_groups)

    parts = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append("<title>Current /v1/messages context — kept vs dropped</title>")
    parts.append("""<style>
      body { font: 14px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif;
             margin: 0; background: #fafaf7; color: #1d1d1f; }
      .wrap { max-width: 1000px; margin: 0 auto; padding: 32px 28px 64px; }
      header { border-bottom: 1px solid #e4e2dc; padding-bottom: 18px; margin-bottom: 24px; }
      h1 { margin: 0 0 8px; font-size: 22px; }
      .stat { display: inline-block; padding: 4px 12px; border-radius: 14px;
              font-size: 12px; font-weight: 600; margin-right: 8px; }
      .stat.kept { background: #def3e2; color: #2f7d4a; }
      .stat.dropped { background: #fbe5dc; color: #c0392b; }
      .stat.anchor { background: #f4e8df; color: #cc6633; }
      .group { border-left: 4px solid #ddd; padding: 6px 12px 8px; margin: 18px 0;
               background: white; border-radius: 4px; }
      .group.kept { border-left-color: #2f7d4a; }
      .group.dropped { border-left-color: #c0392b; opacity: 0.55; }
      .gh { font-size: 12px; color: #6b6b73; margin-bottom: 4px;
            font-family: "JetBrains Mono", Consolas, monospace; }
      .msg { padding: 8px 10px; margin: 6px 0; border-radius: 4px;
             font-size: 13px; white-space: pre-wrap; word-wrap: break-word; }
      .msg.user { background: #f0f4f8; }
      .msg.assistant { background: #fcfaf2; }
      .role { font-weight: 700; font-size: 11px; text-transform: uppercase;
              letter-spacing: .04em; color: #6b6b73; margin-bottom: 2px; }
      .truncated { color: #888; font-style: italic; font-size: 12px; }
      .anchor-bar { padding: 14px 18px; margin: 28px 0;
                    background: #f4e8df; border: 2px dashed #cc6633;
                    text-align: center; font-weight: 600; color: #cc6633;
                    border-radius: 6px; }
      details > summary { cursor: pointer; padding: 8px 0; font-weight: 600; }
    </style></head><body><div class='wrap'>""")

    parts.append("<header>")
    parts.append("<h1>Currently being sent to Anthropic API</h1>")
    parts.append(f"<div>")
    parts.append(f"<span class='stat kept'>KEPT  {kept_msg_count} msgs · {len(kept_groups)} groups</span>")
    parts.append(f"<span class='stat dropped'>DROPPED  {dropped_msg_count} msgs · {len(dropped_groups)} groups</span>")
    if anchor_info:
        parts.append(f"<span class='stat anchor'>anchor = {anchor_info['anchor']}</span>")
    parts.append("</div>")
    parts.append(f"<div style='margin-top:8px; font-size:13px; color:#6b6b73;'>")
    parts.append(f"Session JSONL: <code>{escape(str(jsonl_path))}</code><br>")
    if anchor_info:
        parts.append(f"Latest SHIFT (from proxy.log): <code>{anchor_info['ts']} KST</code> · "
                     f"groups {anchor_info['groups_before']} → {anchor_info['groups_after']}")
    parts.append("</div>")
    parts.append("</header>")

    # DROPPED groups — collapsed by default
    if dropped_groups:
        parts.append(f"<details><summary>▶ Show {len(dropped_groups)} dropped groups "
                     f"({dropped_msg_count} messages) — no longer sent to Anthropic</summary>")
        for i, g in enumerate(dropped_groups):
            parts.append(_render_group(i, g, kept=False))
        parts.append("</details>")

    # Anchor line
    parts.append(f"<div class='anchor-bar'>↓ anchor (everything below is in the current window) ↓</div>")

    # KEPT groups — expanded
    for i, g in enumerate(kept_groups, start=anchor_info["anchor"] if anchor_info else 0):
        parts.append(_render_group(i, g, kept=True))

    parts.append("</div></body></html>")
    out_path.write_text("".join(parts), encoding="utf-8")


def _render_group(idx: int, group_msgs: list, *, kept: bool) -> str:
    cls = "kept" if kept else "dropped"
    out = [f"<div class='group {cls}'>"]
    out.append(f"<div class='gh'>group {idx} · {len(group_msgs)} msg(s)</div>")
    for m in group_msgs:
        role = m["role"]
        text = m["text"]
        if len(text) > 1500:
            text = text[:1500] + "  …  [truncated]"
        out.append(f"<div class='msg {role}'>")
        out.append(f"<div class='role'>{role}</div>")
        out.append(escape(text))
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


def main():
    jsonl = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_JSONL
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT

    if not jsonl.exists():
        print(f"JSONL not found: {jsonl}", file=sys.stderr)
        sys.exit(1)

    msgs = extract_messages(jsonl)
    groups = split_into_turn_groups(msgs)
    anchor_info, line = find_latest_anchor()

    print(f"Messages: {len(msgs)}")
    print(f"Groups: {len(groups)}")
    if anchor_info:
        print(f"Latest SHIFT anchor: {anchor_info['anchor']} ({anchor_info['ts']})")
        print(f"  groups {anchor_info['groups_before']} → {anchor_info['groups_after']}")
        kept_n = len(groups) - anchor_info["anchor"]
        kept_msgs = sum(len(g) for g in groups[anchor_info["anchor"]:])
        print(f"  → kept {kept_n} groups ({kept_msgs} msgs); dropped {anchor_info['anchor']} groups")
    else:
        print("No SHIFT found in proxy.log — showing all groups as kept.")

    render_html(msgs, groups, anchor_info, out, jsonl)
    print(f"\nHTML written to: {out}")


if __name__ == "__main__":
    main()
