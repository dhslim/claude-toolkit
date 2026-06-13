"""Reddit fetcher — the .json suffix trick.

Any public Reddit URL appended with .json returns the full thread as
structured JSON. No auth needed for public subs. Walks the comment tree
and flattens top-N comments by score.
"""

from __future__ import annotations  # PEP 604 `X | None` hints on the 3.9 venv

import json
import urllib.parse
import urllib.request


_HEADERS = {
    # Reddit blocks default Python UA — needs anything that looks like
    # a real client.
    "User-Agent": "claude-toolkit-digest/1.0 (by /u/_local_)",
    "Accept": "application/json",
}

_MAX_COMMENTS = 25  # top N by score across the whole tree


def _walk_comments(listing: dict, out: list[dict]) -> None:
    """Recursively flatten a comment tree into a flat list."""
    if not isinstance(listing, dict):
        return
    data = listing.get("data") or {}
    children = data.get("children") or []
    for c in children:
        if not isinstance(c, dict):
            continue
        if c.get("kind") != "t1":
            continue  # skip "more" placeholders, mod actions, etc.
        cd = c.get("data") or {}
        body = cd.get("body") or ""
        if not body.strip() or cd.get("author") == "[deleted]" and not body:
            continue
        out.append({
            "author": cd.get("author"),
            "text": body,
            "score": cd.get("score"),
            "depth": cd.get("depth", 0),
        })
        # Recurse into replies
        replies = cd.get("replies")
        if isinstance(replies, dict):
            _walk_comments(replies, out)


def fetch(url: str, **_) -> dict:
    # Normalize: strip trailing slash, strip query string, add .json
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    json_url = urllib.parse.urlunparse(parsed._replace(path=path + ".json", query="", fragment=""))

    try:
        req = urllib.request.Request(json_url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return {
            "ok": False,
            "source": "reddit",
            "url": url,
            "notes": [f"Reddit JSON fetch failed: {type(e).__name__}: {e}"],
        }

    # Reddit returns an array: [post_listing, comments_listing]
    if not isinstance(payload, list) or len(payload) < 1:
        return {
            "ok": False, "source": "reddit", "url": url,
            "notes": ["Reddit returned unexpected payload shape."],
        }

    post_listing = payload[0]
    post_children = (post_listing.get("data") or {}).get("children") or []
    if not post_children:
        return {
            "ok": False, "source": "reddit", "url": url,
            "notes": ["Reddit thread not found or removed."],
        }
    post = (post_children[0] or {}).get("data") or {}

    # Walk comments if present
    flat_comments: list[dict] = []
    if len(payload) >= 2:
        _walk_comments(payload[1], flat_comments)
    # Keep the top-N by score
    flat_comments.sort(key=lambda c: c.get("score") or 0, reverse=True)
    top_comments = flat_comments[:_MAX_COMMENTS]

    return {
        "ok": True,
        "source": "reddit",
        "url": "https://reddit.com" + (post.get("permalink") or "") if post.get("permalink") else url,
        "title": post.get("title"),
        "author": post.get("author"),
        "published": _epoch_to_iso(post.get("created_utc")),
        "duration_sec": None,
        "engagement": {
            "score": post.get("score"),
            "upvote_ratio": post.get("upvote_ratio"),
            "num_comments": post.get("num_comments"),
        },
        "description": None,
        "body": post.get("selftext") or None,
        "transcript": None,
        "comments": top_comments,
        "notes": [
            f"Top {len(top_comments)} of {len(flat_comments)} comments by score.",
            f"Subreddit: r/{post.get('subreddit')}",
        ],
    }


def _epoch_to_iso(epoch) -> str | None:
    if not epoch:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return None
