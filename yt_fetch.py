"""Fetch YouTube video metadata + transcript for the /youtube skill.

Usage: yt_fetch.py <url> [--lang en,ko]

Prints a structured text block: metadata header, then transcript (plain text,
timestamps stripped). Falls back to auto-generated captions if no manual subs.
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path

import yt_dlp

# Force UTF-8 stdout on Windows (default cp949/cp1252 chokes on em-dashes etc.)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def vtt_to_text(vtt_path: Path) -> str:
    raw = vtt_path.read_text(encoding="utf-8", errors="replace")
    lines = []
    seen = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.startswith("Kind:") or s.startswith("Language:"):
            continue
        if "-->" in s:
            continue
        if re.match(r"^\d+$", s):
            continue
        # Strip inline VTT tags like <c>, <00:00:01.000>, &nbsp;
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"&nbsp;", " ", s)
        s = s.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        lines.append(s)
    return "\n".join(lines)


def fetch(url: str, langs: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
        # First pass: metadata only, see which sub languages exist
        probe_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Pick langs that actually exist (manual > auto)
        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        wanted = []
        for l in langs:
            for candidate in (l, f"{l}-orig"):
                if candidate in manual or candidate in auto:
                    wanted.append(candidate)
        # Dedupe, preserve order
        seen = set()
        wanted = [x for x in wanted if not (x in seen or seen.add(x))]

        if wanted:
            dl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": wanted,
                "subtitlesformat": "vtt",
                "outtmpl": outtmpl,
                "ignoreerrors": True,
                "noprogress": True,
            }
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    info = ydl.extract_info(url, download=True) or info
            except yt_dlp.utils.DownloadError:
                pass  # Use whatever subs landed on disk

        vid = info.get("id", "")
        transcript = ""
        source = None
        # Prefer manual subs over auto
        for kind, label in (("subtitles", "manual"), ("automatic_captions", "auto")):
            subs = info.get(kind) or {}
            for lang in langs + [f"{l}-orig" for l in langs] + list(subs.keys()):
                if lang not in subs:
                    continue
                # File may be named like <id>.<lang>.vtt
                candidates = sorted(Path(tmp).glob(f"{vid}*.vtt"))
                for c in candidates:
                    if f".{lang}." in c.name or c.name.endswith(f".{lang}.vtt"):
                        transcript = vtt_to_text(c)
                        source = f"{label} ({lang})"
                        break
                if transcript:
                    break
            if transcript:
                break

        # Last-ditch: any VTT in the temp dir
        if not transcript:
            any_vtt = sorted(Path(tmp).glob("*.vtt"))
            if any_vtt:
                transcript = vtt_to_text(any_vtt[0])
                source = f"unknown ({any_vtt[0].stem.split('.')[-1]})"

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": info.get("description") or "",
        "tags": info.get("tags") or [],
        "transcript": transcript,
        "transcript_source": source,
        "webpage_url": info.get("webpage_url"),
    }


def fmt_duration(secs):
    if not secs:
        return "?"
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--lang", default="en,ko", help="comma-separated subtitle langs to try (default: en,ko)")
    args = p.parse_args()

    langs = [x.strip() for x in args.lang.split(",") if x.strip()]
    try:
        info = fetch(args.url, langs)
    except yt_dlp.utils.DownloadError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"URL:         {info['webpage_url']}")
    print(f"Title:       {info['title']}")
    print(f"Channel:     {info['channel']}  ({info.get('channel_url') or ''})")
    print(f"Duration:    {fmt_duration(info['duration'])}")
    print(f"Uploaded:    {info['upload_date']}")
    print(f"Views/Likes: {info['view_count']} / {info['like_count']}")
    if info["tags"]:
        print(f"Tags:        {', '.join(info['tags'][:15])}")
    print()
    print("=== Description ===")
    print(info["description"].strip() or "(empty)")
    print()
    print(f"=== Transcript ({info['transcript_source'] or 'NONE FOUND'}) ===")
    print(info["transcript"] or "(no captions available)")


if __name__ == "__main__":
    main()
