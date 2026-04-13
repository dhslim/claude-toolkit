# Claude Code Context Proxy

A transparent local HTTP proxy that lets you keep talking to Claude past
its 1M-token context cap. Without it, long Claude Code sessions die with
`Context limit reached · /compact or /clear to continue`. With it, the
proxy silently drops the oldest turn groups when needed and forwards a
trimmed request to Anthropic. You don't see the error.

## What it solves

Claude Opus 4.6 (1M context) has a hard cap of 1,000,000 input tokens
per request. Long coding sessions reach this surprisingly quickly because
tool calls, file reads, and bash output accumulate in `messages[]`.
Claude Code's only built-in escape hatches are `/compact` (which
destructively summarizes) or `/clear` (which throws everything away).

This proxy gives you a third option: stay on the conversation, lose only
the oldest turns, no manual intervention.

## Architecture

```
Claude Code
    │  POST /v1/messages
    │  ANTHROPIC_BASE_URL=http://localhost:9999
    ▼
┌──────────────────────────────────────────────┐
│  claude_proxy.py                              │
│  Two-path design:                             │
│    Fast path (body < 950 KB)                  │
│      → forward unchanged                      │
│    Slow path (body >= 950 KB)                 │
│      → call Anthropic /v1/messages/count_tokens │
│      → if over TOKEN_BUDGET, binary-search    │
│        turn groups to find max newest-N fit   │
│      → forward trimmed body                   │
└──────────────────────────────────────────────┘
    │
    ▼
api.anthropic.com
```

The proxy never touches your session JSONL on disk. Claude Code's local
state is unchanged. Only the request body sent over HTTPS is rewritten,
and only when needed. Set `ANTHROPIC_BASE_URL` back to the default and
everything reverts.

## Why a token-aware design

We initially measured by bytes (under the assumption that Anthropic's
20 MB byte cap was the binding constraint). That turned out to be wrong
in two ways:

1. **There is no separate byte cap we can observe.** A 14.7 MB body was
   accepted at the byte level — Anthropic's own error message was
   `prompt is too long: 1148380 tokens > 1000000 maximum`. The byte
   number Claude Code surfaces in its UI as "Request too large (max
   20 MB)" appears to be a client-side guard, not an API cap.

2. **`count_tokens` under-reports vs actual processing.** For the same
   body, the `/v1/messages/count_tokens` endpoint reported 912k tokens
   while the actual `/v1/messages` request was rejected at 1148k — a
   26% discrepancy. Likely because count_tokens doesn't see fields it
   rejects (`metadata`, `max_tokens`, etc.) that Anthropic counts when
   actually processing.

So we trim with a generous safety margin: `TOKEN_BUDGET=700_000`.
With the observed ~1.26x expansion factor, that maps to ~880k actual
processing tokens, leaving ~120k margin from the 1M cap.

## Turn group invariant

Trim happens at "turn group" granularity, not individual messages. A
turn group is one real user message followed by all the assistant
responses and tool round-trips until the next real user message.

Dropping whole groups preserves three things Anthropic requires:

1. **`tool_use` ↔ `tool_result` pair integrity.** Every `tool_use` must
   have its matching `tool_result` in the same request, or you get a
   400 with `orphan tool_result` / `tool_use without matching
   tool_result`. Group boundaries always fall between turn pairs, so we
   never split a pair.

2. **user/assistant alternation.** Dropping groups from the start keeps
   the remainder a clean alternating sequence.

3. **First message must be `user`.** Each group starts with a real user
   message, so what's left also starts with a user message.

## Fields

- `FAST_PATH_THRESHOLD` (env: `CLAUDE_PROXY_FAST_BYTES`, default 950000)
  Body sizes below this skip the token check entirely. BPE guarantees
  `tokens <= bytes`, so anything under ~950 KB cannot exceed 1M tokens.

- `TOKEN_BUDGET` (env: `CLAUDE_PROXY_TOKEN_BUDGET`, default 700000)
  Token ceiling we trim to. Conservative because of the count_tokens
  discrepancy above.

- `CLAUDE_PROXY_QUIET=1`
  Suppress per-request logs. Only trims and errors are printed.

## Installing

```bash
python install_claude_proxy.py
```

The installer detects your OS and shell and adds a `claude` function to
your shell rc file (`$PROFILE` on Windows / PowerShell, `~/.bashrc` and
`~/.zshrc` on macOS and Linux). The function:

1. Checks if proxy is already running on localhost:9999
2. If not, starts it lazily in the background (hidden window)
3. Sets `ANTHROPIC_BASE_URL=http://localhost:9999` for this child
4. Calls the real `claude` binary, passing through your args

After install, just run `claude` as you normally would. Aliases that
expand to `claude` (like `cfork = claude -r --fork-session`) get
wrapped automatically because the alias expansion hits our function.

To remove:

```bash
python install_claude_proxy.py --uninstall
```

Removes the wrapper from your rc files and reverts the `claude`
command to native behavior. The proxy script itself stays on disk —
delete `claude_proxy.py` if you want it gone too.

## Companion setting: disable Claude Code's auto-compact

The proxy and Claude Code's built-in auto-compact are two different
context-management layers operating on the same problem. Auto-compact
fires *before* requests leave Claude Code — it destructively
summarizes the conversation by spawning an Opus call to rewrite the
message history in place. The proxy can't intervene: by the time a
trimmed body would matter, auto-compact has already rewritten history.

To make the proxy the single source of truth for context management,
disable auto-compact via the official Anthropic env var:

```json
// ~/.claude/settings.json
{
  "env": {
    "DISABLE_AUTO_COMPACT": "1"
  }
}
```

Or set it in your shell environment directly. The `/compact` slash
command remains available for manual use; only the automatic trigger
is suppressed.

With auto-compact off, Claude Code sends raw history every turn, the
proxy trims at the HTTPS layer, and your local JSONL session stays
fully intact. With auto-compact on, you get layered behavior: Claude
Code summarizes around 90% of context, then if anything still grows
past 1M, the proxy catches it. Pick whichever matches your taste —
"single non-destructive layer" vs "two layers, lossy first".

Note: `DISABLE_AUTO_COMPACT` is the official env var (with
underscores). The settings.json schema does *not* expose a
corresponding `autoCompactEnabled` boolean — only `autoCompactWindow`
for tuning the trigger threshold. If you'd rather just delay
auto-compact than disable it, set `autoCompactWindow` to its max
(`1000000`) instead.

## Critical companion setting: bypass Claude Code's local blocking limit

Disabling auto-compact alone is not enough. Claude Code 2.1.104 has a
*third* context-management layer that fires before either the proxy or
auto-compact: a local pre-flight blocking check. Before sending any
HTTPS request, Claude Code counts tokens locally over its in-memory
message array and, if the count meets a per-model ceiling, refuses to
send the request at all. The user sees:

```
Context limit reached · /compact or /clear to continue
```

Because the request never leaves the process, **the proxy never sees
it**, and our token-aware trim cannot help. This is the documented
"local pre-flight" limitation in the previous section, and it is the
single most common reason users hit a wall even with the proxy and
auto-compact disabled.

The check lives in a function (minified name `cjH`) that compares
the local token estimate against a ceiling derived from the model's
context window minus a reserve. There is an undocumented escape hatch
env var that overrides the ceiling directly:

```json
// ~/.claude/settings.json
{
  "env": {
    "DISABLE_AUTO_COMPACT": "1",
    "DISABLE_COMPACT": "1",
    "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "9000000"
  }
}
```

`CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` raises the local blocking
ceiling to whatever you set (9M is far past any realistic 1M-context
session). Once set, Claude Code stops refusing locally, the request
flows out through the wrapper to the proxy, and the proxy trims it to
fit Anthropic's actual 1M cap. End-to-end working chain.

`DISABLE_COMPACT=1` is included alongside because the blocking check
also gates on a separate `DISABLE_COMPACT && CLAUDE_CODE_MAX_CONTEXT_TOKENS`
branch in function `wV`, and it changes the error label string. Belt
and suspenders.

**Caveats**:

- These env vars are **undocumented** — they are internal escape
  hatches embedded in the bundled Bun binary. They were verified
  against Claude Code `2.1.104`. Future releases may rename or remove
  them. If they ever stop working, re-grep the binary for
  `BLOCKING_LIMIT_OVERRIDE`, `MAX_CONTEXT_TOKENS`, or
  `isAtBlockingLimit`.
- Settings.json `env` block is **hot-reloaded** by Claude Code on
  subsequent prompts — empirically verified. We initially assumed
  startup-only (standard Unix env semantics) and told users they had
  to restart. They don't. Edit settings.json mid-session and the next
  prompt picks up the new values, the same way hooks and CLAUDE.md
  reload. (If a session is already showing the blocking error,
  saving settings and sending one more prompt is enough.)
- The local pre-flight check is purely in-memory; it never calls
  Anthropic's `count_tokens`. So a "lying proxy" that returns a
  fake-low count from `count_tokens` would not help — there is no
  outgoing counting request to intercept.

With all four layers handled — local blocking (env var), auto-compact
(env var), API hard cap (proxy trim), wrapper auto-start (shell
function) — the user types `claude` and just keeps working past 1M.

## Running the proxy manually (without the wrapper)

```bash
python claude_proxy.py
```

Then in another terminal:

```bash
export ANTHROPIC_BASE_URL=http://localhost:9999
claude
```

Useful for debugging — you see verbose logs in real time.

## Known limitations

- **Upstream 20 MB byte cap (Layer 5, open issue)**: in addition to
  Anthropic's 1 M token cap, *something* upstream returns HTTP 413
  when the request body crosses 20 MB. Claude Code surfaces this as
  `Request too large (max 20MB). Double press esc to go back and try
  with a smaller file.` The check is **not** client-side — Claude
  Code's only `_f$()` call site is its 413 error handler, and there
  is no `body.length > 20MB` guard anywhere in the binary. So the
  413 originates from Anthropic itself, their CDN, a load balancer,
  or our proxy's HTTP layer — currently unverified which. The proxy
  trims by token budget (700 K) only, not bytes. Sessions where
  700 K tokens happen to serialize to over 20 MB (unusual but
  possible with byte-heavy structured tool_results) will still hit
  the 413. **Fix in progress**: byte-aware secondary trim, or
  413-retry-with-tighter-budget in the proxy. See
  `claude-proxy-design.md` "Layer 5" for the full investigation.

- **Latency**: each slow-path request adds 1–3 seconds (count_tokens +
  binary-search trim). The trim binary-search is `O(log N)` group
  count_tokens calls plus the actual `/v1/messages` call.

- **Cache miss on trim**: when trim fires, the prefix changes, so
  Anthropic's prompt cache misses. Cost is higher than a normal request
  but still cheaper than `/compact` (which costs a full summarization
  Opus call). When trim doesn't fire (fast path or just under budget),
  the body is byte-identical to the original and cache hits normally.

- **Lossy**: dropped turn groups are gone from Claude's perspective.
  Information that lived in an old assistant response is lost. If a
  more recent assistant response had summarized that information, that
  summary survives — and Claude reads its own past responses, so the
  effect is closer to "Claude has a fading memory" than "Claude
  forgets cliff-edge".

- **Single-group oversize**: if even one turn group alone exceeds
  budget (large pasted file, huge tool_result), we forward it anyway
  and Anthropic may reject. No content-level truncation yet.

- **count_tokens discrepancy**: empirically ~26% under-reporting. We
  pad the budget; if Anthropic changes their internal counting later,
  the budget may need re-tuning.

## Files

- `claude_proxy.py` — the proxy itself. Single-file Python, FastAPI +
  httpx + uvicorn. ~430 lines.
- `install_claude_proxy.py` — cross-platform shell-rc installer.
- `docs/claude-proxy.md` — this file. User-facing reference.
- `docs/claude-proxy-design.md` — the long-form design history.
  Explains *how we got here*: the four (now five) defense layers, the
  empirical surprises, the wrong assumptions we corrected, and why
  each piece exists. Read this when you need to understand or extend
  the system, not just use it.

## What it does NOT do

- It does not modify your session JSONL files.
- It does not change Claude Code's behavior outside the HTTPS layer
  (other than reading env vars from settings.json that Claude Code
  itself respects).
- It does not summarize or retrieve. Pure drop-oldest trim.
- It does not (yet) handle Layer 5 — the upstream 20 MB byte cap.
  Bodies that serialize to over 20 MB still get a 413 from upstream.
  Token-aware trim is necessary but not always sufficient.

(The proxy *does* now bypass Claude Code's local pre-flight blocking
check via the `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` env var documented
in the "Critical companion setting" section above. An earlier version
of this doc said it didn't, which was true before we found the
escape hatch.)
  In practice this happens when resuming a session that's already past
  the cap before the first new turn fires.

## Empirical data (from the day this was built)

- Body 14.7 MB / Claude Code UI 89% / count_tokens 912k → real Anthropic
  count 1.148M → rejected.
- After trim with budget 700k tokens: 14.7 MB → 13.9 MB, 499 groups →
  407 groups (92 dropped), 2811 messages → 2135 messages → 200 OK.
- Fast path threshold of 950 KB covers ~99% of requests in normal use
  (quota checks, Haiku helper calls, short conversations). Slow path
  fires only on long sessions with significant accumulated history.
