"""Threads (Meta) fetcher — og:* meta tag extraction via crawler UA.

Discovery: Threads serves rich og:* tags ONLY when the User-Agent is a
known social media link-preview crawler (facebookexternalhit, Discordbot).
With a normal browser UA, the page is JS-rendered with no useful meta.

Anonymous gets us:
  - Full post text (og:description)
  - Author + handle (og:title)
  - Thumbnail image (og:image)
  - Canonical URL (og:url)

Misses (login-required):
  - Replies / thread continuation
  - Comments

For the typical /digest use case (single Threads post → summarize),
this is enough. Reply chains need login or copy-paste.
"""

import re
import urllib.parse
import urllib.request
from html import unescape


# facebookexternalhit is Meta's own crawler — it serves the og:* preview
# tags that any embedded-preview surface (Slack, Discord, FB) would see.
# Using this UA is no more deceptive than how those services fetch previews.
_CRAWLER_HEADERS = {
    "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Accept-Language": "en-US,en;q=0.9",
}


def _og(html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta\s+property=["\']og:{prop}["\']\s+content=["\']([^"\']*)["\']',
        html, re.IGNORECASE,
    )
    return unescape(m.group(1)) if m else None


def _parse_handle(og_title: str | None) -> tuple[str | None, str | None]:
    """Title looks like: 'Solomon Eseme | AI Backend Engineer (@kaperskyguru) on Threads'
    Return (display_name, handle).
    """
    if not og_title:
        return None, None
    m = re.match(r"^(.+?)\s*\(@([^)]+)\)\s*on Threads\s*$", og_title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return og_title, None


def _parse_url(url: str) -> tuple[str | None, str | None]:
    """Extract (handle, post_id) from a Threads URL.

    Patterns:
      https://www.threads.com/@handle/post/<id>
      https://www.threads.net/@handle/post/<id>
    """
    m = re.search(r"/@([^/]+)/post/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def fetch(url: str, **_) -> dict:
    # Strip tracking params
    parsed = urllib.parse.urlparse(url)
    clean_url = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
    handle, post_id = _parse_url(clean_url)

    try:
        req = urllib.request.Request(clean_url, headers=_CRAWLER_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            final_url = resp.geturl()
    except Exception as e:
        return {
            "ok": False, "source": "threads", "url": clean_url,
            "notes": [f"HTTP fetch failed: {type(e).__name__}: {e}"],
        }

    og_title = _og(html, "title")
    og_desc = _og(html, "description")
    og_image = _og(html, "image")
    og_site = _og(html, "site_name")

    if not og_desc and not og_title:
        return {
            "ok": False, "source": "threads", "url": clean_url,
            "notes": [
                "Page returned but no og:description/title found.",
                "Threads may have changed its crawler-preview surface.",
                "Workaround: open in your browser, copy the post, paste here.",
            ],
        }

    display_name, parsed_handle = _parse_handle(og_title)

    return {
        "ok": True,
        "source": "threads",
        "url": final_url,
        "title": display_name or og_title,
        "author": f"@{parsed_handle or handle}" if (parsed_handle or handle) else None,
        "published": None,  # Not in og: tags
        "duration_sec": None,
        "engagement": {},   # Not in og: tags
        "description": og_desc,
        "body": (
            f"Image: {og_image}" if og_image else None
        ),
        "transcript": None,
        "comments": None,
        "notes": [
            f"Site: {og_site or 'Threads'}",
            "Fetched via og:* meta tags (crawler-preview surface).",
            "Replies and comments are login-gated — not included.",
            "If the post is a 'teaser → continuation in reply' format, "
            "only the first post's text is in og:description.",
        ],
    }
