"""Instagram fetcher — Instaloader-based with graceful fallback.

Strategy (in order):
  1. Instaloader anonymous — caption + all carousel images + counts.
     Comments attempted but expected to often fail anonymously in 2026.
  2. yt-dlp fallback — for video reels where Instaloader misses.
  3. og:meta fallback — for any remaining oddities.

Anonymous-only by default. No credentials stored. If the user later wants
comments reliably, they can opt in via IG_USERNAME env var (NOT implemented
in this MVP — added once we know anonymous limits).
"""

import re
import urllib.parse
import urllib.request
from typing import Optional

import instaloader
import yt_dlp


_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_COMMENTS = 25  # cap for the digest output


def _shortcode_from_url(url: str) -> Optional[str]:
    """Extract the shortcode from any Instagram post/reel URL.

    Patterns we accept:
      https://www.instagram.com/p/DYz94WFDSyL/
      https://www.instagram.com/reel/XYZ/
      https://www.instagram.com/p/<code>/?... (with query params)
    """
    m = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?", url)
    return m.group(1) if m else None


def _strip_query(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))


def _try_instaloader(shortcode: str) -> Optional[dict]:
    """Attempt anonymous Instaloader fetch. Returns dict on success, None on fail."""
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,  # We do this manually below for control
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception as e:
        return {"_error": f"Instaloader fetch failed: {type(e).__name__}: {e}"}

    # Carousel: iterate sidecar nodes to get every image / video URL
    media_urls: list[dict] = []
    try:
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                media_urls.append({
                    "type": "video" if node.is_video else "image",
                    "url": node.video_url if node.is_video else node.display_url,
                })
        else:
            media_urls.append({
                "type": "video" if post.is_video else "image",
                "url": post.video_url if post.is_video else post.url,
            })
    except Exception as e:
        media_urls.append({"error": f"sidecar walk failed: {e}"})

    # Comments — attempt anonymous; this is the most likely point of failure
    comments_data: list[dict] = []
    comments_error: Optional[str] = None
    try:
        for i, comment in enumerate(post.get_comments()):
            if i >= _MAX_COMMENTS:
                break
            comments_data.append({
                "author": comment.owner.username,
                "text": comment.text,
                "likes": comment.likes_count,
                "created_at": comment.created_at_utc.strftime("%Y-%m-%d %H:%M UTC") if comment.created_at_utc else None,
            })
    except Exception as e:
        comments_error = f"{type(e).__name__}: {e}"

    return {
        "_error": None,
        "title": post.caption[:80] if post.caption else None,
        "author": post.owner_username,
        "published": post.date_utc.strftime("%Y-%m-%d %H:%M UTC") if post.date_utc else None,
        "description": post.caption,
        "engagement": {
            "likes": post.likes,
            "comments_count": post.comments,
            "video_views": post.video_view_count if post.is_video else None,
        },
        "media": media_urls,
        "comments": comments_data,
        "comments_error": comments_error,
        "hashtags": post.caption_hashtags,
        "mentions": post.caption_mentions,
        "is_video": post.is_video,
        "typename": post.typename,
    }


def _try_ytdlp(url: str) -> Optional[dict]:
    """Last-ditch: yt-dlp for video reels (it doesn't handle image posts)."""
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def fetch(url: str, **_) -> dict:
    notes: list[str] = []
    clean_url = _strip_query(url)
    shortcode = _shortcode_from_url(clean_url)

    if not shortcode:
        return {
            "ok": False, "source": "instagram", "url": url,
            "notes": ["Could not parse shortcode from URL — supported: /p/, /reel/, /tv/"],
        }

    # Path 1: Instaloader
    il = _try_instaloader(shortcode)
    if il and not il.get("_error"):
        if il.get("comments_error"):
            notes.append(f"Comments fetch failed anonymously: {il['comments_error']}")
            notes.append("Comments are increasingly login-gated in 2026. Caption + images still captured.")
        elif not il["comments"] and il["engagement"]["comments_count"]:
            notes.append(f"Post has {il['engagement']['comments_count']} comments but anonymous fetch returned 0 (likely rate-limited).")

        # Format media for the body field — list of URLs the user can open
        media_lines: list[str] = []
        for m in il["media"]:
            if m.get("error"):
                media_lines.append(f"  [media error: {m['error']}]")
            else:
                media_lines.append(f"  [{m['type']}] {m['url']}")

        body_parts = [
            f"Type: {il['typename']}  (is_video={il['is_video']})",
            "",
            "Media URLs:",
            *media_lines,
        ]
        if il["hashtags"]:
            body_parts += ["", "Hashtags: " + " ".join(f"#{h}" for h in il["hashtags"])]
        if il["mentions"]:
            body_parts += ["", "Mentions: " + " ".join(f"@{m}" for m in il["mentions"])]

        return {
            "ok": True,
            "source": "instagram",
            "url": f"https://www.instagram.com/p/{shortcode}/",
            "title": il["title"],
            "author": il["author"],
            "published": il["published"],
            "duration_sec": None,
            "engagement": il["engagement"],
            "description": il["description"],
            "body": "\n".join(body_parts),
            "transcript": None,
            "comments": il["comments"] or None,
            "notes": notes + [f"Fetched via Instaloader (anonymous). Shortcode: {shortcode}"],
        }
    else:
        if il and il.get("_error"):
            notes.append(il["_error"])

    # Path 2: yt-dlp fallback (video reels only)
    ytdlp_info = _try_ytdlp(clean_url)
    if ytdlp_info:
        notes.append("Instaloader failed; using yt-dlp fallback (video reels only).")
        return {
            "ok": True, "source": "instagram", "url": clean_url,
            "title": ytdlp_info.get("title"),
            "author": ytdlp_info.get("uploader"),
            "published": None,
            "duration_sec": ytdlp_info.get("duration"),
            "engagement": {
                "views": ytdlp_info.get("view_count"),
                "likes": ytdlp_info.get("like_count"),
            },
            "description": ytdlp_info.get("description"),
            "body": None,
            "transcript": None,
            "comments": None,
            "notes": notes,
        }

    # Path 3: all failed
    return {
        "ok": False, "source": "instagram", "url": clean_url,
        "notes": notes + [
            "Instaloader + yt-dlp both failed.",
            "Likely login-gated. Open the post in your browser, copy caption + comments, and paste here.",
        ],
    }
