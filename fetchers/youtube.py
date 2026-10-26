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
        offered = list(manual) + [k for k in auto if k not in manual]

        # YouTube tags caption tracks with region and variant suffixes -- 'en-US',
        # 'en-GB', 'pt-BR', 'ko-KR', plus yt-dlp's own '-orig'. Matching the bare
        # code exactly therefore MISSES tracks that plainly exist: the video
        # NUhDP30IRKk offers en-US, zh-Hans and zh-Hant, and asking for 'en' found
        # nothing at all. So accept the exact code first (a real 'en' track beats a
        # regional one), then fall back to any tag whose primary subtag matches.
        def _primary(tag: str) -> str:
            return tag.split("-", 1)[0].lower()

        wanted = []
        for l in langs:
            low = l.lower()
            exact = [t for t in offered if t.lower() == low]
            orig = [t for t in offered if t.lower() == f"{low}-orig"]
            regional = [t for t in offered
                        if _primary(t) == low and t not in exact and t not in orig]
            wanted.extend(exact + orig + regional)
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
            dl_error = None
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    info = ydl.extract_info(url, download=True) or info
            except yt_dlp.utils.DownloadError as e:
                # Keep it: pass 1 already gave us usable metadata, so a failed
                # subtitle pass must not sink the whole fetch. But a silently
                # swallowed error is worse -- it makes "captions don't exist"
                # and "the download broke" produce the identical empty result.
                dl_error = str(e)

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

            if not transcript:
                # The probe SAID these tracks exist, yet nothing landed on disk.
                # Report which, and why -- otherwise this is indistinguishable
                # from a video that simply has no captions, and the reader
                # cannot tell a broken fetch from an honest absence.
                notes.append(
                    "Subtitle tracks were offered (" + ",".join(wanted) + ") but "
                    "none downloaded" + (f": {dl_error}" if dl_error else
                                         " (no .vtt written).")
                )
        else:
            offered = sorted(set(manual) | set(auto))
            notes.append(
                "No subtitle languages matched (tried " + ",".join(langs) + ")."
                + (" Available: " + ",".join(offered[:25])
                   + (f" (+{len(offered) - 25} more)" if len(offered) > 25 else "")
                   if offered else " This video offers no caption tracks at all.")
            )

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
