"""Per-platform content fetchers for the /digest skill.

Each module exposes a `fetch(url, **opts)` function returning a dict with
a common shape so the dispatcher can render any source uniformly:

    {
        "ok": bool,                      # False if fetch failed / unsupported
        "source": str,                   # "youtube" | "reddit" | ...
        "url": str,                      # canonical URL of the content
        "title": str | None,
        "author": str | None,
        "published": str | None,         # ISO date if known
        "duration_sec": int | None,      # for video/audio
        "engagement": dict,              # {"views": ..., "likes": ..., ...}
        "description": str | None,       # short caption / video description
        "body": str | None,              # main article body (for blogs/posts)
        "transcript": str | None,        # for video/audio
        "comments": list[dict] | None,   # [{"author","text","score"}]
        "notes": list[str],              # warnings, missing-data caveats
    }

Adding a new platform = adding a new module here + one line in
digest.py's dispatcher.
"""
