# Claude Proxy — Design History

This is the long-form companion to `claude-proxy.md`. It captures *how
we got here* — the dead ends, the wrong assumptions we corrected, the
empirical surprises, and the four distinct layers of context
management we had to deal with one at a time. Read `claude-proxy.md`
first if you just want to use the thing.

## The original problem

User had ~14.7 MB Claude Code sessions on Opus 4.6 (1M context) that
were dying with `Context limit reached · /compact or /clear to
continue`. Hard requirement was: **stay on the same conversation past
1M tokens**. Soft requirement: don't touch Claude Code source, don't
modify session JSONL files, don't manually `/compact` or `/clear`.

The conceptual frame the user landed on was CQRS: write-side (Claude
Code's local JSONL append) and read-side (what gets sent to Anthropic)
should be separable. Only the read-side needs to be trimmed; the
local history can stay full and lossless.

## Architecture: a transparent local HTTP proxy

```
Claude Code  ──POST /v1/messages──>  localhost:9999  ──HTTPS──>  api.anthropic.com
                                     │
                                     ├─ fast path (body < 950 KB):  forward unchanged
                                     │
                                     └─ slow path (body >= 950 KB):
                                        - call Anthropic /v1/messages/count_tokens
                                        - if over budget: binary search turn groups
                                        - forward trimmed body
```

The hook is `ANTHROPIC_BASE_URL`. Set it to `http://localhost:9999`
and Claude Code routes all API traffic through us. The proxy never
touches disk-side state; only the in-flight HTTPS body is rewritten,
and only when needed. Removing the env var reverts everything.

Single-file Python: FastAPI + httpx + uvicorn. ~430 lines. SSE
streaming preserved end-to-end via `aiter_bytes()`.

## Decisions we got wrong and corrected

### 1. Byte-based vs token-based budget

Initial design measured the body in **bytes** and trimmed if over a
megabyte threshold. Wrong assumption: "Anthropic has a 20 MB byte
cap, so we just need to stay under that."

**Correction**: the user pushed back — "I want to use the exact same
condition Anthropic uses." Anthropic measures **tokens**, not bytes.
A 14.7 MB body was rejected with `prompt is too long: 1148380 tokens
> 1000000 maximum`, not a byte error. So the proxy switched to
calling Anthropic's `/v1/messages/count_tokens` endpoint as the
source of truth.

### 2. The "1 MB ≤ 1 M tokens" mathematical bound

User asked: is there a body size below which we can prove tokens ≤
1 M without calling count_tokens? Yes — BPE guarantees `tokens ≤
bytes` always. So **any body under ~1 MB cannot exceed 1 M tokens**.
This gave us the fast path: under `FAST_PATH_THRESHOLD = 950000`
bytes, skip the token check entirely and forward unchanged. Above
that, slow path.

### 3. Misnaming "conservative"

I called the 1 MB threshold "보수적" (conservative). User caught it:
"1mb means we call the token checker api way more often, how is that
보수적?" Correct — 1 MB is *less permissive*, not more. A higher
threshold (3 MB) would be more conservative on API calls and *less*
safe on token correctness. The 1 MB threshold is the strict one.
Acknowledged and corrected.

### 4. Overconfidence about a "20 MB byte cap"

I asserted "Anthropic has two separate caps — 20 MB byte and 1 M
token." User: "u sure about this?" I wasn't. I added error-body
logging to the proxy to capture Anthropic's actual rejection
messages. Result: only the 1 M token cap fires from Anthropic. The
"20 MB" Claude Code mentions in `Request too large (max 20MB)` is
something else — see "Layer 5" below. Lesson: don't assert
infrastructure facts without verifying.

### 5. count_tokens under-reports vs actual processing

Empirical surprise: for the same body, `/v1/messages/count_tokens`
reported **912 K tokens** while the actual `/v1/messages` request
came back with `1148380 tokens > 1000000 maximum` — a **26 %
discrepancy**. Likely because count_tokens silently drops fields it
rejects (`metadata`, `max_tokens`, `temperature`, etc.) that the
actual processing path counts.

Fix: lower `TOKEN_BUDGET` from 950 K to **700 K**, leaving ~120 K of
headroom from the 1 M cap after the 1.26x expansion factor.

Also fix: strip the rejected fields before posting to count_tokens
(`COUNT_TOKENS_REJECTED_FIELDS = {"metadata", "stream", "max_tokens",
"stop_sequences", "temperature", "top_p", "top_k", "service_tier"}`).
Otherwise count_tokens returns 400 instead of a count.

### 6. Re-serialization bloat (153 KB bug)

When trim couldn't actually reduce the group count (single-group
case), the proxy was still re-serializing the body via `json.dumps()`
— and the re-serialized output was *larger* than the original because
the original used minified separators. Fix: when `len(best_kept_groups)
== len(groups)`, return the **original bytes** unchanged.

### 7. Gzip content-encoding mismatch

Phase 1 returned "Failed to parse JSON" because we stripped the
`content-encoding: gzip` header but forwarded the raw still-compressed
bytes. Two fixes layered: (a) added `accept-encoding: identity` to
the forwarded request headers, (b) switched from `aiter_raw()` to
`aiter_bytes()` for httpx auto-decoding.

### 8. Path matching bug

Initial code checked `"/v1/messages" in full_path` but FastAPI's
captured path param has no leading slash. Fixed to
`full_path.startswith("v1/messages")` and excluded
`"v1/messages/count_tokens"` (so we don't recursively count_tokens
ourselves).

## Turn group invariant

Trim happens at "turn group" granularity, not individual messages.
A turn group is one real user message followed by all assistant
responses and tool round-trips until the next real user message.

Why groups, not messages: Anthropic enforces three structural
invariants in `messages[]`:

1. **`tool_use` ↔ `tool_result` pair integrity.** Every `tool_use`
   must have its matching `tool_result` in the same request. Drop
   one without the other and you get a 400 `orphan tool_result` /
   `tool_use without matching tool_result`. Group boundaries always
   fall *between* turn pairs, so we never split a pair.
2. **user / assistant alternation.** Dropping whole groups from the
   start preserves the alternating structure.
3. **First message must be `user`.** Each group starts with a real
   user message, so what's left also starts with one.

`is_real_user_message()` distinguishes a user-typed message from a
user message that's actually a `tool_result` envelope. The first is
a turn-group boundary; the second is part of the previous group.

`split_into_turn_groups()` chunks the messages array by these
boundaries. `_group_preview()` extracts the human-typed text for
debug logging, skipping `<system-reminder>`, `<command-message>`,
`<task-notification>`, etc.

`trim_to_token_budget()` does an `O(log N)` binary search over how
many newest groups to keep, calling count_tokens at each step. It
returns the largest `k` such that `count_tokens(last_k_groups) ≤
budget`.

## The four-layer defense (in firing order)

The proxy alone is not enough. We discovered Claude Code itself has
multiple guards that fire *before* the request reaches the proxy.
Each one had to be identified and disabled separately.

### Layer 1 — Claude Code local pre-flight blocking check

**Symptom**: `Context limit reached · /compact or /clear to continue`.
Request never leaves Claude Code. Proxy never sees it.

**Root cause**: function `cjH(H, $, q)` in the bundled binary at
`C:\Users\user\.local\bin\claude.exe` (243 MB Bun bundle). Computes
an in-memory token estimate `H = bw(F) - n` and compares against a
per-model ceiling derived from `Nl($, _) - g_$` (model context
window minus output reserve). If `H >= ceiling`, sets
`isAtBlockingLimit: true`, the query loop sees that, yields the
"Context limit reached" error envelope, and exits without an HTTPS
call.

The check supports an undocumented escape hatch:
```js
let W = process.env.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE,
    L = W ? parseInt(W,10) : NaN,
    X = (!isNaN(L) && L>0) ? L : P;
```
If the env var is set and positive, it replaces the computed
ceiling.

**Fix**: set `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE=9000000` (or any
value larger than realistic in-memory token counts). Once set, the
local check stops firing, requests flow through to the proxy, and
the proxy does the actual trimming.

### Layer 2 — Claude Code auto-compact

**Symptom**: at ~90% of context, Claude Code spawns a background
Opus call to summarize the conversation in place. Destructive — the
old messages array is replaced with a summary. The proxy's
non-destructive trim never gets to run because by the time it would
matter, history has been rewritten locally.

**Root cause**: standard Claude Code feature, fires when token usage
crosses a threshold (`autoCompactWindow` in settings, default ~90 %
of model context).

**Fix**: env var `DISABLE_AUTO_COMPACT=1`. This is an officially
documented Anthropic escape hatch (verified via the docs reference
agent). Manual `/compact` remains available; only the automatic
trigger is suppressed.

### Layer 3 — Claude Code "compact" branch label

**Symptom**: same error label as Layer 1, slightly different
internal code path.

**Root cause**: function `wV(H, $)` in the binary has a parallel
branch that gates on `DISABLE_COMPACT && CLAUDE_CODE_MAX_CONTEXT_TOKENS`.
Setting only `DISABLE_AUTO_COMPACT=1` doesn't cover it.

**Fix**: also set `DISABLE_COMPACT=1`. Belt and suspenders alongside
`DISABLE_AUTO_COMPACT=1`.

### Layer 4 — Anthropic API hard cap (1 M tokens)

**Symptom**: API returns 400 `prompt is too long: N tokens > 1000000
maximum` where N is the actual token count Anthropic computed.

**Root cause**: Anthropic enforces a hard cap of 1,000,000 input
tokens per `/v1/messages` request on Opus 4.6 (1M context).

**Fix**: this is what the proxy itself handles. Slow path detects
bodies over `FAST_PATH_THRESHOLD`, calls count_tokens, binary-searches
turn groups, and forwards a body within `TOKEN_BUDGET = 700_000`
tokens. The 700 K budget gives ~120 K margin from the 1 M cap after
the 1.26x count_tokens-vs-actual expansion factor.

### Layer 5 (open) — upstream 20 MB byte cap

**Symptom**: `Request too large (max 20MB). Double press esc to go
back and try with a smaller file.`

**What we know**: the constant `ikH = 20971520` (= 20 × 1024 × 1024)
exists in the binary. The error message is rendered by `_f$()`. The
**only** call site for `_f$()` is the HTTP error handler:
```js
if (H instanceof sq && H.status === 413)
  return i4({content:_f$(), error:"invalid_request"});
```
That is, the message fires when Claude Code receives an **HTTP 413**
from upstream — *not* from a local pre-send byte check. There is no
client-side `body.length > ikH` guard anywhere in the binary.

**Implication**: this is upstream — either Anthropic itself has a
20 MB byte cap separate from the 1 M token cap, or some HTTP layer
(CDN, load balancer, our proxy) returns 413 at exactly 20 MB. Earlier
runs with 14.7 MB bodies passed through, so the cap is somewhere
between 14.7 and 20 MB.

**No env-var escape**: I enumerated every `CLAUDE_CODE_*` literal in
the binary (170+). None relate to request body size. There is no
`CLAUDE_CODE_MAX_REQUEST_BYTES`, `_MAX_BODY_SIZE`, or similar. The
20MB constant is hardcoded in the message, but the *enforcement* is
upstream.

**Status**: open. The proxy currently trims by token budget only. If
700 K tokens happen to serialize to over 20 MB bytes (would require
unusually byte-heavy content like long structured tool_results), the
upstream still rejects. Next iteration: add a byte-aware secondary
trim — after the token-aware trim, if the result is still over say
18 MB, drop additional groups until under. Or: handle the 413 in the
proxy by retrying with a tighter budget.

## The wrapper layer

Setting `ANTHROPIC_BASE_URL` manually for every terminal is ugly.
The user's strong preference: "when I run claude it knows what to
do." So `install_claude_proxy.py` writes a shell function that:

1. Checks if proxy is already listening on `localhost:9999` via a
   TCP connect test.
2. If not, starts `claude_proxy.py` in the background, hidden, and
   waits up to 2 seconds for it to bind.
3. Sets `ANTHROPIC_BASE_URL=http://localhost:9999` in the function's
   own environment.
4. Calls the **real** `claude` binary via `command claude` (POSIX) or
   `Get-Command claude -CommandType Application` (PowerShell), so
   the function doesn't recurse into itself.

The wrapper is delimited by markers (`# >>> claude_proxy wrapper >>>`
/ `# <<< claude_proxy wrapper <<<`) so re-running the installer
overwrites the old version cleanly. Idempotent.

### POSIX target files
- `~/.bashrc` (and `~/.bash_profile` on macOS if `.bashrc` doesn't
  exist)
- `~/.zshrc` (always, if home has one)

### Windows target files
**Both** PowerShell profiles, unconditionally:
- `~/Documents/PowerShell/Microsoft.PowerShell_profile.ps1` (PS7)
- `~/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1` (PS5)

Reason: VS Code's integrated PowerShell, Windows Terminal, and
external pwsh.exe / powershell.exe can each be PS5 or PS7. Writing
to only one profile led to a bug where `Get-Command claude`
returned `Application` (the raw .exe) instead of `Function` — the
wrapper wasn't loaded because the user's shell was reading the
*other* profile. The dual-write fix removed the ambiguity.

### Alias coverage

The wrapper applies to `claude` itself and any alias that expands to
`claude` (like `cfork = claude -r --fork-session`, `cread = claude
--read-only`, etc.). Alias expansion happens before the function
lookup, so the function intercepts everything.

## Settings.json env block as the canonical home

Originally I planned to set `DISABLE_AUTO_COMPACT` etc. in shell rc
files. Better approach surfaced once we hit the wrapper-not-loaded
PS5/PS7 issue: put static env vars in `~/.claude/settings.json` under
the `env` block.

```json
{
  "env": {
    "CLAUDE_CODE_NO_FLICKER": "1",
    "DISABLE_AUTO_COMPACT": "1",
    "DISABLE_COMPACT": "1",
    "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "9000000"
  }
}
```

Why this is the canonical location:
- Cross-platform automatic — Claude Code reads it on any OS.
- Doesn't depend on which shell rc file is loaded.
- Survives shell wrapper bugs.
- Single source of truth.
- **Unexpected**: hot-reloaded. I assumed env vars are read only at
  process startup (standard Unix model). Empirically Claude Code
  re-reads settings.json `env` on subsequent prompts — the user
  edited settings.json mid-session and the next prompt picked up
  `BLOCKING_LIMIT_OVERRIDE=9000000` without a restart. Likely the
  same hot-reload mechanism that handles hooks, statusLine, and
  CLAUDE.md. (I had told the user "you must restart"; I was wrong,
  in a useful direction.)

The wrapper still needs to set `ANTHROPIC_BASE_URL` inline because
that requires lazy proxy startup the wrapper does itself.

## Empirical data points (worth keeping)

- **First success run**: 14.7 MB body → trimmed to 13.9 MB. 499 turn
  groups → 407 (92 dropped). 2811 messages → 2135. count_tokens
  said 912 K → Anthropic actually counted 1.148 M before trim →
  200 OK after trim. 55-second Opus response.
- **count_tokens vs actual processing**: 26 % under-report.
- **Fast-path coverage**: ~99 % of requests in normal use (quota
  checks, Haiku helpers, short conversations) are under 950 KB and
  skip the slow path entirely.
- **Slow-path latency cost**: 1–3 seconds per request (count_tokens
  call + `O(log N)` binary search).
- **Cache miss on trim**: when the proxy actually drops groups, the
  prefix changes and Anthropic's prompt cache misses. Cost is higher
  than a normal request but much cheaper than `/compact` (which
  costs a full Opus summarization call). When trim doesn't fire,
  the body is byte-identical and cache hits normally.

## Files

- `claude_proxy.py` — the proxy. FastAPI + httpx + uvicorn, ~430
  lines. Token-aware two-path design. Lifespan-managed httpx client
  to avoid the deprecated `@app.on_event`.
- `install_claude_proxy.py` — cross-platform installer. Detects OS,
  picks shell rc files (POSIX) or PS profiles (Windows, both PS5
  and PS7), writes the wrapper between marker delimiters, idempotent.
  Supports `--uninstall`.
- `docs/claude-proxy.md` — user-facing reference. How to install,
  what fields exist, what envs to set, known limitations.
- `docs/claude-proxy-design.md` — this file. The journey, the
  reasoning, the corrections, and the layered architecture.

## What this does NOT do

- Does not modify session JSONL files on disk.
- Does not change Claude Code's behavior outside the HTTPS layer.
- Does not summarize or retrieve. Pure drop-oldest trim.
- Does not bypass Anthropic's actual 1 M token cap — it stays
  *under* it, which is the difference between dying at 1.0 M and
  living at 0.7 M.
- Does not (yet) handle the 20 MB upstream byte cap. Layer 5 is
  open work.

## Open questions / next iterations

1. **Layer 5 fix**: add a byte-aware secondary trim after the
   token-aware trim. Or handle 413 in the proxy by retrying with a
   tighter budget.
2. **Where exactly does the 20 MB 413 originate?** Anthropic itself,
   their CDN, our proxy's httpx layer, or a load balancer? Need to
   capture the 413 response headers to confirm.
3. **`session_half_raw.py`** — sitting untracked. Was a manual cut
   tool from earlier in the experiment when we thought local
   trimming might be the answer. Now obsoleted by the proxy
   approach. Either delete or document and keep as a reference.
4. **Binary-version drift**: every undocumented env var we rely on
   (`CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`, `DISABLE_COMPACT`) was
   verified against `2.1.104`. Future Claude Code releases may
   rename or remove them. If they ever stop working, re-grep the
   bundled binary for `BLOCKING_LIMIT_OVERRIDE`, `MAX_CONTEXT_TOKENS`,
   `isAtBlockingLimit` to find the new path.
