"""Clone a Claude Code session JSONL into a new cwd as an independent copy.

Produces a fully standalone session that Claude treats as if it were born in
the target directory. The clone gets a fresh sessionId so it has zero identity
overlap with the source — they can coexist without collision.

Usage:
    python session_transplant.py <source.jsonl> <target-dir>

Example:
    python session_transplant.py \
        ~/.claude/projects/C--Users-user-Desktop-DooMigration/47fff34f-....jsonl \
        "C:\\Users\\user\\Desktop\\dentalchart-backend2\\family-1830"

What it does (minimal mode, non-destructive copy):
    1. Generates a brand new sessionId UUID for the clone
    2. Reads every line of the source JSONL
    3. Rewrites the per-line `cwd` field to the target path
    4. Rewrites the per-line `gitBranch` field to match the target dir's
       sibling sessions (or target git branch if no siblings). When sibling
       sessions use the literal string "HEAD" (common when the branch name
       contains characters like `#`), the clone matches that.
    5. Rewrites the per-line `sessionId` field to the new UUID
    6. If the FIRST user message has list-form content (e.g. pasted image +
       text), flattens it to a plain text string. Sessions whose first user
       message has non-string content are filtered out of the default
       "current worktree" view by Claude Code's resume picker.
    7. Writes the result to ~/.claude/projects/<encoded-target>/<newSessionId>.jsonl
    8. Leaves the source file untouched — clone has zero ties to the original

What it does NOT do (intentionally):
    - Rewrite tool_result content strings (stale paths in transcript are cosmetic)
    - Touch uuid, parentUuid, version, timestamp (message-level identity chain)
    - Delete the source
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path


def encode_cwd(cwd: str) -> str:
    """Turn an absolute path into Claude's project-dir encoding.

    C:\\Users\\user\\Desktop\\foo  ->  C--Users-user-Desktop-foo
    /home/user/foo                 ->  -home-user-foo
    """
    # Normalize slashes to backslashes-then-dashes path style
    encoded = cwd.replace("\\", "-").replace("/", "-")
    # Windows drive letter: first colon becomes double-dash
    if ":" in encoded:
        encoded = encoded.replace(":", "-", 1)
    return encoded


def current_git_branch(target_dir: Path) -> str | None:
    """Return the target dir's current git branch, or None if not a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(target_dir), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch = result.stdout.strip()
        return branch or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def sibling_git_branch(target_project_dir: Path) -> str | None:
    """Look at existing JSONL files in the target encoded project dir and
    return the most common `gitBranch` value they use. This handles cases
    where Claude Code writes `"HEAD"` literally (e.g. when the branch name
    contains `#`) — we want to match siblings, not guess via git.
    """
    if not target_project_dir.exists():
        return None
    from collections import Counter

    counter: Counter[str] = Counter()
    for jsonl in target_project_dir.glob("*.jsonl"):
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and "gitBranch" in obj:
                        counter[obj["gitBranch"]] += 1
        except OSError:
            continue
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def flatten_first_user_message(obj: dict) -> bool:
    """If this record is the first user message and its content is a list
    form (as with pasted images), collapse it to a plain text string by
    joining the text parts. Returns True if a rewrite happened.

    Claude Code's resume picker hides sessions whose first user message has
    non-string content from the default "current worktree" view; the session
    only appears after Ctrl+W (show all worktrees). Flattening brings it back.
    """
    if not isinstance(obj, dict):
        return False
    if obj.get("type") != "user" or obj.get("parentUuid") is not None:
        return False
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    text_parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            t = part.get("text", "")
            if isinstance(t, str) and t:
                text_parts.append(t)
    msg["content"] = " ".join(text_parts).strip() or "(transplanted session)"
    if "imagePasteIds" in obj:
        del obj["imagePasteIds"]
    return True


def transplant(source_file: Path, target_cwd: str) -> Path:
    if not source_file.exists():
        raise FileNotFoundError(f"Source session not found: {source_file}")
    if not source_file.is_file():
        raise ValueError(f"Source is not a file: {source_file}")

    target_cwd = str(Path(target_cwd))  # normalize separators to OS style
    target_dir = Path(target_cwd)
    if not target_dir.exists():
        raise FileNotFoundError(f"Target directory does not exist: {target_cwd}")

    target_encoded = encode_cwd(target_cwd)
    new_session_id = str(uuid.uuid4())

    projects_root = Path.home() / ".claude" / "projects"
    target_project_dir = projects_root / target_encoded

    # Prefer sibling sessions' gitBranch value (matches Claude Code's own
    # convention for this worktree). Fall back to git's branch name.
    target_branch = sibling_git_branch(target_project_dir) or current_git_branch(target_dir)

    target_project_dir.mkdir(parents=True, exist_ok=True)

    dest_file = target_project_dir / f"{new_session_id}.jsonl"

    rewritten_lines: list[str] = []
    cwd_rewrites = 0
    branch_rewrites = 0
    session_id_rewrites = 0
    first_user_flattened = False

    with source_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                rewritten_lines.append("")
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Keep malformed lines as-is rather than dropping them
                rewritten_lines.append(line)
                continue

            if isinstance(obj, dict):
                if "cwd" in obj and obj["cwd"] != target_cwd:
                    obj["cwd"] = target_cwd
                    cwd_rewrites += 1
                if target_branch and obj.get("gitBranch") not in (None, target_branch):
                    obj["gitBranch"] = target_branch
                    branch_rewrites += 1
                if "sessionId" in obj and obj["sessionId"] != new_session_id:
                    obj["sessionId"] = new_session_id
                    session_id_rewrites += 1
                if not first_user_flattened and flatten_first_user_message(obj):
                    first_user_flattened = True

            rewritten_lines.append(json.dumps(obj, ensure_ascii=False))

    with dest_file.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rewritten_lines))
        if rewritten_lines and not rewritten_lines[-1].endswith("\n"):
            f.write("\n")

    print(f"Source:        {source_file}")
    print(f"Target dir:    {target_cwd}")
    print(f"Target branch: {target_branch or '(not a git repo)'}")
    print(f"Encoded dir:   {target_encoded}")
    print(f"New sessionId: {new_session_id}")
    print(f"Wrote:         {dest_file}")
    print(f"cwd rewrites:       {cwd_rewrites}")
    print(f"gitBranch rewrites: {branch_rewrites}")
    print(f"sessionId rewrites: {session_id_rewrites}")
    print(f"first user msg flattened: {first_user_flattened}")
    print()
    print(f'Next: cd "{target_cwd}" && claude -r')
    return dest_file


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    source_file = Path(sys.argv[1]).expanduser()
    target_cwd = sys.argv[2]
    try:
        transplant(source_file, target_cwd)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
