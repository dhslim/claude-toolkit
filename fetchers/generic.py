"""Generic HTML fallback — for blogs, Substack, news articles, any URL
that doesn't match a more specific fetcher.

Strategy: fetch the page, extract og:* meta tags for title/desc/author,
then a crude body extraction by stripping nav/footer/script and taking
the largest <article>/<main>/<div> by text length. Not as smart as
Readability.js but zero extra deps and good enough for most blog posts.
"""

from __future__ import annotations  # PEP 604 `X | None` hints on the 3.9 venv

import re
import urllib.parse
import urllib.request
from html import unescape


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_BODY_CHARS = 50_000  # cap absurdly long pages


def _og(html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta\s+(?:property|name)=["\']og:{prop}["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return unescape(m.group(1))
    # Some sites use name="..." instead of property="..."
    m = re.search(
        rf'<meta\s+name=["\']{prop}["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    return unescape(m.group(1)) if m else None


def _title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return unescape(m.group(1).strip()) if m else None


def _strip_tags(html_chunk: str) -> str:
    # Drop script/style entirely
    html_chunk = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_chunk, flags=re.IGNORECASE | re.DOTALL)
    # Replace <br>, <p>, <li>, <h*> with newlines for readability
    html_chunk = re.sub(r"<(br|hr)\s*/?>", "\n", html_chunk, flags=re.IGNORECASE)
    html_chunk = re.sub(r"</(p|div|li|h[1-6]|article|section)\s*>", "\n\n", html_chunk, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", html_chunk)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = unescape(text).strip()
    return text


def _extract_body(html: str) -> str | None:
    """Find the chunk most likely to be the article body."""
    candidates: list[tuple[int, str]] = []
    # Prefer <article>, then <main>, then large <div>s
    for tag in ("article", "main"):
        for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.IGNORECASE | re.DOTALL):
            text = _strip_tags(m.group(1))
            if len(text) > 200:
                candidates.append((len(text), text))
    if not candidates:
        # Fall back to largest <div>
        for m in re.finditer(r"<div\b[^>]*>(.*?)</div>", html, re.IGNORECASE | re.DOTALL):
            text = _strip_tags(m.group(1))
            if len(text) > 500:
                candidates.append((len(text), text))
    if not candidates:
        # Last ditch: strip the whole page
        text = _strip_tags(html)
        if len(text) > 200:
            candidates.append((len(text), text))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    body = candidates[0][1]
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n\n[... truncated ...]"
    return body


def fetch(url: str, **_) -> dict:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            final_url = resp.geturl()
    except Exception as e:
        return {
            "ok": False, "source": "generic", "url": url,
            "notes": [f"HTTP fetch failed: {type(e).__name__}: {e}"],
        }

    title = _og(html, "title") or _title(html)
    description = _og(html, "description")
    author = _og(html, "article:author") or _og(html, "author")
    published = _og(html, "article:published_time")
    body = _extract_body(html)

    return {
        "ok": True,
        "source": "generic",
        "url": final_url,
        "title": title,
        "author": author,
        "published": published,
        "duration_sec": None,
        "engagement": {},
        "description": description,
        "body": body,
        "transcript": None,
        "comments": None,
        "notes": [
            "Generic HTML fallback — output quality varies by site.",
            f"Host: {urllib.parse.urlparse(final_url).netloc}",
        ],
    }
