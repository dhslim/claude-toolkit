#!/usr/bin/env python3
"""SessionStart hook — notify (throttled, non-blocking) when claude-toolkit is
behind origin/main.

The "am I behind?" mechanism: git history is a chain of commits; HEAD is the
commit this machine is on, origin/main is where GitHub's main was at the last
fetch. `git rev-list --count HEAD..origin/main` counts the commits origin/main
has that HEAD doesn't — that count is "N behind". It's pure local graph math
(instant, offline); only `git fetch` touches the network.

So we split the two:
  - Fast path (default run): read a CACHED behind-count and, if > 0, inject an
    instruction telling Claude to let the user know. Never touches the network,
    so session startup never lags.
  - Refresh path (`--refresh`, spawned DETACHED): git fetch + recount + rewrite
    the cache, at most ~once/24h, in a separate process — the network cost is
    off the startup path entirely.

Notify-only: it never pulls; you pull when you're ready. Opt out with
TOOLKIT_NO_UPDATE_CHECK=1. First run stays silent (nothing cached yet).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATE = Path.home() / ".claude" / "toolkit_drift.json"
TTL_SECONDS = 24 * 60 * 60


def _git(*args, timeout=10):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def refresh():
    """Detached mode: fetch origin/main, recount behind, rewrite the cache.

    Bounded by timeouts; any failure (offline, etc.) is swallowed silently — a
    missed refresh just means the cached count is a bit older next time.
    """
    try:
        _git("fetch", "--quiet", "origin", "main", timeout=30)
        out = _git("rev-list", "--count", "HEAD..origin/main").stdout.strip()
        behind = int(out) if out.isdigit() else 0
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"behind": behind, "ts": time.time()}),
                         encoding="utf-8")
    except Exception:
        pass


def spawn_refresh():
    """Fire-and-forget a detached `--refresh` so the fetch never blocks startup."""
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  stdin=subprocess.DEVNULL)
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive the parent exiting.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True  # setsid: detach from the hook process
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--refresh"], **kwargs)
    except Exception:
        pass


def read_cache():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None


def main():
    if "--refresh" in sys.argv[1:]:
        refresh()
        return

    # Consume the hook payload on stdin (we don't need it) so we never leave a
    # writer blocked on the pipe.
    try:
        sys.stdin.read()
    except Exception:
        pass

    if os.environ.get("TOOLKIT_NO_UPDATE_CHECK") == "1":
        return

    cache = read_cache()
    now = time.time()

    # Kick a background refresh when we've never checked or the check is stale.
    if not cache or (now - cache.get("ts", 0)) > TTL_SECONDS:
        spawn_refresh()

    # First run: nothing cached yet → stay silent (don't nag); the refresh we just
    # spawned will populate the count for next session.
    if not cache:
        return

    behind = cache.get("behind", 0)
    if not isinstance(behind, int) or behind <= 0:
        return

    # Local, no-network changelog of exactly what a `git pull` would bring in.
    incoming = ""
    try:
        incoming = _git("log", "--oneline", "-8", "HEAD..origin/main",
                        timeout=5).stdout.strip()
    except Exception:
        pass

    context = (
        f"[toolkit-drift] claude-toolkit is {behind} commit(s) behind origin/main "
        f"(as of the last background check). Briefly tell the user their "
        f"claude-toolkit is {behind} commit(s) behind and suggest running "
        f"`git pull` in {REPO} when convenient."
    )
    if incoming:
        context += f" Incoming commits:\n{incoming}"

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
