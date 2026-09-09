# Slash Commands

Skill manifests live in `skills/`; each is symlinked or copied into `~/.claude/skills/`.

## `/mgo` Skill — Recent Activity Viewer

Custom Claude Code slash command to query recent activity across all machines.

```
/mgo 10      # last 10 minutes (default unit)
/mgo 2h      # last 2 hours
/mgo 1d      # last 1 day
```

- Always runs as a background task — type your follow-up prompt immediately
- Queries MongoDB for sessions with recent `last_synced_at`, then filters messages by timestamp
- Returns data across all devices (Mac, Windows, GPU servers, SSH sessions)
- Claude summarizes the results: projects, topics, decisions, code changes
- Skill file: `~/.claude/skills/mgo/SKILL.md`
- Query script: `mongo_recent.py`

## `/transplant` Skill — Session Cloner

Clone a Claude Code session JSONL from one working directory to another, so the cloned session shows up in `claude -r` from the target directory as if it had been created there.

```
/transplant <source.jsonl> <target-directory>
```

What it rewrites in the clone:
- Fresh `sessionId` UUID (zero identity overlap with the source — they coexist)
- Per-line `cwd` field
- Per-line `gitBranch` (auto-detected from sibling sessions in the target encoded dir)
- First user message flattened from list-form to plain string (otherwise the picker hides the session from the default "current worktree" view)

What it leaves alone (intentional):
- The source file itself — completely untouched
- Tool result content strings (cosmetic stale paths in transcript)
- `uuid`, `parentUuid`, `version`, `timestamp` (message-level identity chain)

- Skill file: `~/.claude/skills/transplant/SKILL.md`
- Script: `session_transplant.py`

## `/grill-me` Skill — Interactive Quizmaster

User-invoked slash command that turns Claude into a tough-but-fair technical interviewer. Give it something to grill you on — a topic, a file/module, `this PR`, `this diff`, a commit, or just the current branch's changes — optionally with a difficulty/mode hint (`hard`, `brutal`, `interview`, `FAANG-style`, `quick`, `warmup`, `rapid fire`). Claude gathers the source material, then asks one question at a time, evaluates each answer (tagging it solid / shaky / partial / gap), probes deeper on shallow answers, supports hint requests for partial credit, and ends with a debrief of strengths and weak spots to review.

```
/grill-me this PR
/grill-me sync_conversations.py hard
/grill-me "MongoDB write concern" interview
/grill-me                 # offers to grill on the current branch's changes
```

Distinct from `/pushback` (which is one-shot adversarial review of *your* claim); `/grill-me` is a multi-turn quiz where Claude evaluates *your* answers.

- Skill file: `~/.claude/skills/grill-me/SKILL.md`
- Pure instructions — no script, no env vars, no path placeholders

## `/digest` Skill — Universal Content Digest

User-invoked slash command that fetches and summarizes any social media or web post. One command, any supported platform — `digest.py` auto-detects the platform from the URL's domain and dispatches to the matching fetcher under `fetchers/`.

```
/digest <url> [--lang en,ko]
```

| Platform | Quality (anonymous) | Comments? |
|---|---|---|
| YouTube (videos, Shorts, live archives) | full — title, desc, transcript | sometimes |
| Instagram (public posts, reels, carousels) | full caption + carousel images + counts (Instaloader) | ❌ login-required |
| Threads (single post page) | first-post text + author + image URL (og:* surface) | ❌ login-required |
| Reddit (any public thread) | full — post body + top 25 comments by score | ✅ full tree |
| Substack / Medium / blogs / news | generic HTML — title + body | site-dependent |

Anything else routes to the generic HTML fallback. Adding a new platform = drop a module in `fetchers/` + one line in the dispatch table.

- Always runs as a background task — fetches take 5–15s; type your follow-up immediately
- Skill file: `~/.claude/skills/digest/SKILL.md` (vendored copy in `skills/digest/SKILL.md`)
- Dispatcher: `digest.py`; fetchers: `fetchers/`
- Deps: `yt-dlp` (YouTube + many video sites), `instaloader` (Instagram); Threads/Reddit/generic use stdlib only

