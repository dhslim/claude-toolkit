# claude-toolkit

Two systems that extend Claude Code past what it ships with, plus tooling built on them.

## The proxy

Claude Code sessions die at the context limit. This keeps one conversation alive past 1M tokens by rewriting request bodies in flight — Claude Code is unmodified, the only hook is `ANTHROPIC_BASE_URL`, and unsetting it reverts everything.

```
Claude Code ──POST /v1/messages──> localhost:9999 ──HTTPS──> api.anthropic.com
                                        │
                                        ├─ fast path (< 950 KB): forward unchanged
                                        │
                                        └─ slow path: count_tokens → binary-search
                                           turn groups → forward trimmed body
```

Write-side and read-side are split: the local JSONL stays full and lossless, only the wire payload is trimmed.

**Why it still caches.** Prompt caching is a byte-exact prefix match, so naive trimming rebuilds the cache every turn. The proxy holds a **monotonic anchor** — a watermark that only advances — so between shifts each request is "same prefix + new tail", the append-only shape caching rewards. One full-price rebuild per advance, cache reads in between. Sustains ~99% hit rate. SSE streaming is preserved end-to-end.

→ [`claude_proxy.py`](claude_proxy.py) · [design history](docs/claude-proxy-design.md) · [a debugging writeup](docs/cache-control-ttl-ordering.md)

## The warehouse

Claude Code stores sessions as local JSONL with no cross-machine sync and 30-day retention. This mirrors every session from every machine into MongoDB Atlas, on hooks, no cron:

| Hook | Fires | Covers |
|---|---|---|
| `Stop` | after each response | the normal path (~99%) |
| `SessionEnd` | `/exit` or terminal close | exit after interruption |
| `SessionStart` | every launch (`--scan`) | sessions missed by a force-quit |

Only uncovered case: force-quit, then never open Claude again. Mongo is a strict superset of local disk — it retains sessions after Claude Code's own cleanup purges them.

→ [`sync_conversations.py`](sync_conversations.py) · [`hook_sync.py`](hook_sync.py) · [schema](docs/schema.md)

## Install

```bash
git clone https://github.com/dhslim/claude-toolkit.git
```

Open Claude Code and paste the install prompt from [INSTALL.md](INSTALL.md). It handles venv, dependencies, `.env`, and hooks on any OS.

For the proxy: `python install_claude_proxy.py` — writes the shell wrapper that sets `ANTHROPIC_BASE_URL`.

## Slash commands

| Command | Does |
|---|---|
| `/mgo 2h` | recent activity across all machines |
| `/digest <url>` | fetch + summarize a social/web post |
| `/grill-me <topic>` | Claude quizzes you on a topic, file, PR, or diff |
| `/transplant <jsonl> <dir>` | clone a session into another working directory |

A daily quiz also fires on the `Stop` hook — generated from yesterday's conversations, once per day across all machines.

→ [details](docs/skills.md)

## Where to look

| To see… | Read |
|---|---|
| The hardest problem here | [`claude_proxy.py`](claude_proxy.py) → `trim_to_sliding_window`, `rewrite_cache_control_markers` |
| How the design evolved, including wrong turns | [docs/claude-proxy-design.md](docs/claude-proxy-design.md) |
| Multi-layer debugging against a black-box API | [docs/cache-control-ttl-ordering.md](docs/cache-control-ttl-ordering.md) |
| Working around a 1.5s hook timeout | [`hook_sync.py`](hook_sync.py) — detached child process |
| Implementation decisions and tradeoffs | [docs/design-notes.md](docs/design-notes.md) |

## Debugging

```bash
tail -20 sync.log                          # sync activity
tail -f proxy.log                          # proxy activity
.venv/bin/python sync_conversations.py --scan   # manual full sync
```

## Claude Code internals

Notes written while building this — mostly undocumented behaviour:

- [docs/sessions.md](docs/sessions.md) — session storage on disk, JSONL line format, resume-picker filter rules
- [docs/scrollback.md](docs/scrollback.md) — fullscreen rendering, alt-buffer scrollback, keyboard shortcuts
- [docs/hacks.md](docs/hacks.md) · [docs/quiz-considerations.md](docs/quiz-considerations.md)
