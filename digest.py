"""/digest dispatcher — URL → platform fetcher → uniform text output.

One entry point for fetching content from any supported social media or
publishing platform. Routes by URL domain to the appropriate fetcher
module under fetchers/, then renders the result in a consistent text
format the model can summarize.

Adding a new platform = drop a new module in fetchers/ + one line in the
dispatch table below.

Usage:
    python digest.py <url> [--lang en,ko]
"""

import argparse
import io
import sys
import urllib.parse

# Force UTF-8 stdout on Windows (cp949/cp1252 default chokes on em-dashes).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


# ---------- Routing ----------

# Each entry: (host_match_fn, importable_module_name).
# host_match_fn takes a hostname (lowercased) and returns bool.
_ROUTES: list[tuple[callable, str]] = [
    (lambda h: h in ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"),
     "fetchers.youtube"),
    (lambda h: h in ("instagram.com", "www.instagram.com"),
     "fetchers.instagram"),
    (lambda h: h in ("threads.com", "www.threads.com", "threads.net", "www.threads.net"),
     "fetchers.threads"),
    (lambda h: h in ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"),
     "fetchers.reddit"),
]


def _pick_fetcher(url: str) -> str:
    """Return the dotted module path for the best fetcher for this URL.

    Falls back to fetchers.generic for unknown hosts.
    """
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    for matches, mod in _ROUTES:
        if matches(host):
            return mod
    return "fetchers.generic"


# ---------- Rendering ----------

def _fmt_duration(sec) -> str:
    if not sec:
        return "?"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_engagement(eng: dict) -> str:
    if not eng:
        return ""
    parts = []
    for key, label in (
        ("views", "views"), ("likes", "likes"),
        ("score", "score"), ("upvote_ratio", "upvote_ratio"),
        ("num_comments", "comments"), ("comments_count", "comments"),
    ):
        v = eng.get(key)
        if v is None:
            continue
        if isinstance(v, float):
            parts.append(f"{label}={v:.2f}")
        else:
            parts.append(f"{label}={v:,}" if isinstance(v, int) else f"{label}={v}")
    return "  ".join(parts)


def render(result: dict) -> str:
    """Render a fetcher's result dict as plain text for the model."""
    if not result.get("ok"):
        lines = [f"FETCH FAILED  source={result.get('source')}  url={result.get('url')}"]
        for n in result.get("notes") or []:
            lines.append(f"  - {n}")
        return "\n".join(lines)

    lines = []
    lines.append(f"Source:      {result['source']}")
    lines.append(f"URL:         {result.get('url')}")
    if result.get("title"):
        lines.append(f"Title:       {result['title']}")
    if result.get("author"):
        lines.append(f"Author:      {result['author']}")
    if result.get("published"):
        lines.append(f"Published:   {result['published']}")
    if result.get("duration_sec"):
        lines.append(f"Duration:    {_fmt_duration(result['duration_sec'])}")
    eng = _fmt_engagement(result.get("engagement") or {})
    if eng:
        lines.append(f"Engagement:  {eng}")
    if result.get("tags"):
        lines.append(f"Tags:        {', '.join(result['tags'])}")

    if result.get("description"):
        lines.append("")
        lines.append("=== Description / Caption ===")
        lines.append(result["description"].strip())

    if result.get("body"):
        lines.append("")
        lines.append("=== Body ===")
        lines.append(result["body"].strip())

    transcript = result.get("transcript")
    if transcript:
        src = result.get("transcript_source") or ""
        lines.append("")
        lines.append(f"=== Transcript ({src}) ===")
        lines.append(transcript.strip())

    comments = result.get("comments")
    if comments:
        lines.append("")
        lines.append(f"=== Top {len(comments)} Comments ===")
        for c in comments:
            depth = "  " * (c.get("depth") or 0)
            score = c.get("score")
            score_str = f"[{score:+d}]" if isinstance(score, int) else ""
            lines.append(f"{depth}{score_str} {c.get('author', '?')}: {c.get('text', '').strip()[:500]}")

    if result.get("notes"):
        lines.append("")
        lines.append("=== Notes ===")
        for n in result["notes"]:
            lines.append(f"- {n}")

    return "\n".join(lines)


# ---------- Entry point ----------

def main():
    p = argparse.ArgumentParser(description="Fetch + render social media / publishing content.")
    p.add_argument("url")
    p.add_argument("--lang", default="en,ko",
                   help="Comma-separated subtitle langs (YouTube only). Default en,ko.")
    args = p.parse_args()

    mod_name = _pick_fetcher(args.url)
    try:
        mod = __import__(mod_name, fromlist=["fetch"])
    except ImportError as e:
        print(f"ERROR: failed to import {mod_name}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        result = mod.fetch(args.url, lang=args.lang)
    except Exception as e:
        print(f"ERROR: {mod_name}.fetch crashed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    print(render(result))


if __name__ == "__main__":
    main()
