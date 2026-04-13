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
  httpx + uvicorn. ~400 lines.
- `install_claude_proxy.py` — cross-platform shell-rc installer.
- `docs/claude-proxy.md` — this file.

## What it does NOT do

- It does not modify your session JSONL files.
- It does not change Claude Code's behavior outside the HTTPS layer.
- It does not summarize or retrieve. Pure drop-oldest trim.
- It does not bypass Claude Code's local pre-flight check. If Claude
  Code refuses to send a request because *its own* estimator says you're
  over context, the request never reaches the proxy and we can't help.
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
