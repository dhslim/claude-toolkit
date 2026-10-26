#!/usr/bin/env python3
"""SessionStart hook — notify (throttled, non-blocking) when claude-toolkit is
behind origin/main.

The "am I behind?" mechanism: git history is a chain of commits; HEAD is the
commit this machine is on, origin/main is where GitHub's main was at the last
fetch. `git rev-list --count HEAD..origin/main` counts the commits origin/main
has that HEAD doesn't — that count is "N behind". It's pure local graph math
(instant, offline); only `git fetch` touches the network.

The count is cheap and local, so we cache only the *fetch* (the one thing that
needs the network), never the number:
  - Fast path (default run): recompute the count LIVE against the current HEAD
    and, if > 0, inject an instruction telling Claude to let the user know. No
    network, so startup never lags — and because the number is never cached, a
    `git pull` clears the notice instantly instead of lingering until the TTL.
  - Refresh path (`--refresh`, spawned DETACHED): git fetch to advance
    origin/main + stamp the fetch time, at most ~once/24h, in a separate process
    — the network cost is off the startup path entirely.

Notify-only: it never pulls; you pull when you're ready. Opt out with
TOOLKIT_NO_UPDATE_CHECK=1. First run stays silent (no fetch of our own yet).
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

# Windows: run under pythonw.exe (no console), so a console child like git.exe
# would CREATE a visible console window. CREATE_NO_WINDOW suppresses that flash.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _git(*args, timeout=10):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def local_behind():
    """Commits origin/main has that HEAD doesn't — LOCAL graph math, no network.

    Recomputed on every run against the CURRENT HEAD, so a `git pull` drops it to 0
    immediately. Returns None if git is unavailable (caller then stays silent).
    """
    try:
        out = _git("rev-list", "--count", "HEAD..origin/main").stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def refresh():
    """Detached mode: git fetch to advance origin/main, then stamp the fetch time.

    The behind-count is deliberately NOT stored — it is recomputed live on every run
    (see local_behind), so a `git pull` clears the notice instantly instead of
    lingering until the TTL. The cache exists ONLY to throttle the network fetch.
    The timestamp is stamped even if the fetch fails, so a failing fetch (offline)
    isn't retried on every single session start.
    """
    try:
        _git("fetch", "--quiet", "origin", "main", timeout=30)
    except Exception:
        pass
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
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
    """Return the cache dict only if well-formed ({"ts": <number>}), else None.

    Validating the shape here keeps main()'s `now - cache["ts"]` arithmetic total: a
    corrupt or hand-edited cache degrades to "first run" (silent + refresh) instead of
    raising and breaking the silent-and-safe contract.
    """
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("ts"), (int, float)) else None


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

    # Throttle only the network FETCH (which advances origin/main): refresh in the
    # background when we've never fetched or the last fetch is older than the TTL.
    if not cache or (now - cache.get("ts", 0)) > TTL_SECONDS:
        spawn_refresh()

    # First run: no fetch of our own yet → stay silent rather than judge a stale
    # clone; the refresh we just spawned stamps the cache for next session.
    if not cache:
        return

    # Count LIVE against the current HEAD (local, instant, no network). Because the
    # number is never cached, a `git pull` drops it to 0 and the notice vanishes at
    # once — no lingering false-nag until the TTL.
    behind = local_behind()
    if not behind or behind <= 0:
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
        f"(as of the last background fetch). Briefly tell the user their "
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
