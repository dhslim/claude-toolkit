"""YouTube fetcher — metadata + transcript via yt-dlp.

Migrated from the old yt_fetch.py. Same two-pass behavior: probe available
subtitle languages first via metadata-only extract_info, then only request
langs that actually exist. Avoids HTTP 429 spam.
"""

import re
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp


def _vtt_to_text(vtt_path: Path) -> str:
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
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"&nbsp;", " ", s)
        s = s.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        lines.append(s)
    return "\n".join(lines)


# YouTube gates caption tracks behind a PO token on its default (web/tv) clients;
# the android/ios clients still expose them, so prefer those for subtitle fetching.
_CLIENT_ARGS = {"extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}}}


def fetch(url: str, lang: str = "en,ko", **_) -> dict:
    langs = [x.strip() for x in lang.split(",") if x.strip()]
    notes: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
        # Pass 1: metadata only, find what subtitle langs actually exist
        probe_opts = {"quiet": True, "no_warnings": True, "skip_download": True, **_CLIENT_ARGS}
        try:
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            return {
                "ok": False,
                "source": "youtube",
                "url": url,
                "notes": [f"yt-dlp probe failed: {e}"],
            }

        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        wanted = []
        for l in langs:
            for candidate in (l, f"{l}-orig"):
                if candidate in manual or candidate in auto:
                    wanted.append(candidate)
        seen: set[str] = set()
        wanted = [x for x in wanted if not (x in seen or seen.add(x))]

        transcript = ""
        transcript_source = None
        if wanted:
            dl_opts = {
                "quiet": True, "no_warnings": True, "skip_download": True,
                "writesubtitles": True, "writeautomaticsub": True,
                "subtitleslangs": wanted, "subtitlesformat": "vtt",
                "outtmpl": outtmpl, "ignoreerrors": True, "noprogress": True,
                **_CLIENT_ARGS,
            }
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    info = ydl.extract_info(url, download=True) or info
            except yt_dlp.utils.DownloadError:
                pass

            vid = info.get("id", "")
            for kind, label in (("subtitles", "manual"), ("automatic_captions", "auto")):
                subs = info.get(kind) or {}
                for lang in wanted + list(subs.keys()):
                    if lang not in subs:
                        continue
                    candidates = sorted(Path(tmp).glob(f"{vid}*.vtt"))
                    for c in candidates:
                        if f".{lang}." in c.name or c.name.endswith(f".{lang}.vtt"):
                            transcript = _vtt_to_text(c)
                            transcript_source = f"{label} ({lang})"
                            break
                    if transcript:
                        break
                if transcript:
                    break

            if not transcript:
                any_vtt = sorted(Path(tmp).glob("*.vtt"))
                if any_vtt:
                    transcript = _vtt_to_text(any_vtt[0])
                    transcript_source = "unknown"
        else:
            notes.append("No subtitle languages matched (tried " + ",".join(langs) + ").")

    return {
        "ok": True,
        "source": "youtube",
        "url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "author": info.get("channel") or info.get("uploader"),
        "published": _fmt_upload_date(info.get("upload_date")),
        "duration_sec": info.get("duration"),
        "engagement": {
            "views": info.get("view_count"),
            "likes": info.get("like_count"),
        },
        "description": info.get("description") or None,
        "body": None,
        "transcript": transcript or None,
        "transcript_source": transcript_source,
        "comments": None,
        "notes": notes,
        "tags": (info.get("tags") or [])[:15],
    }


def _fmt_upload_date(yyyymmdd: Optional[str]) -> Optional[str]:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return None
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
