---
name: digest
description: "Fetch and summarize any social media or web post — YouTube, Instagram, Reddit, blogs, Substack, news. Auto-detects platform from URL."
disable-model-invocation: true
argument-hint: <url> [--lang en,ko]
---

# /digest

Universal content digest. One command, any supported platform. The script dispatches to the right fetcher based on the URL's domain.

## Command

Run this **as a background task** (`run_in_background`) — fetches can take 5–15 seconds depending on the platform. The user expects to keep typing while it runs; present the summary when it completes.

```
( set -f; {{VENV_PYTHON}} {{SCRIPT_DIR}}/digest.py $ARGUMENTS )
```

`set -f` (inside a subshell, so it doesn't leak) disables shell globbing for this one call — without it, zsh tries to glob-expand the `?` in a YouTube URL (`watch?v=…`) and aborts with "no matches found" before the script ever runs.

**IMPORTANT**: The paths above are placeholders. During install, replace `{{VENV_PYTHON}}` and `{{SCRIPT_DIR}}` with the absolute paths to this repo's venv python and script directory.

If `$ARGUMENTS` is empty, ask the user for a URL.

## Supported platforms (and what to expect)

| Platform | Quality anonymous | Comments? |
|---|---|---|
| YouTube (videos, Shorts, live archives) | full — title, desc, transcript | sometimes |
| Instagram (public posts, reels, carousels) | full caption + all carousel images + hashtags + counts (Instaloader) | ❌ login-required (returns `LoginRequiredException`) |
| Threads (single post page) | full first-post text + author + image URL (og:* crawler-surface) | ❌ login-required; reply chains too |
| Reddit (any public thread) | full — post body + top 25 comments by score | ✅ yes, full tree |
| Substack / Medium / blogs / news | generic HTML — title + body | site-dependent |

Anything else routes to the generic HTML fallback.

## How to present results

The script prints metadata header, then sections (Description/Caption, Body, Transcript, Comments, Notes) depending on what the platform returned. Use everything that's there.

Default output format for your summary:

1. **One-line gist**: title + what the content is fundamentally about (≤ 20 words)
2. **Key points**: 3–6 bullets covering the main claims, framework, or steps. Use the source's own structure when it has one (e.g. "4 ways to X" → 4 bullets)
3. **Worth noting** (only if applicable): caveats, tradeoffs, the creator's recommendation, anything surprising. Quote sparingly.

If the source has comments (Reddit), surface the highest-signal one or two as well — often the comments add context the post itself doesn't.

If the auto-generated transcript has homophone errors (e.g. "cash" → "cache"), silently correct them in the summary.

## Failure modes — read the Notes section

The script always exits cleanly and prints structured output. Look at the `=== Notes ===` block at the bottom:

- **"FETCH FAILED"** at the top → no useful data. Tell the user what platform failed and why.
- **"Instagram likely requires login"** → suggest the user paste the caption text directly.
- **"Used og:* meta tag fallback"** → output is incomplete; only caption/title available.
- **"Rate-limit"** / **HTTP 429** → suggest waiting a minute and retrying.
- **"Generic HTML fallback"** → expect some noise in the body extraction.

If a result is incomplete (e.g., Instagram with no caption), say so explicitly in your summary rather than guessing.
