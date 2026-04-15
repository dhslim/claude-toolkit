# Cache Control TTL Ordering — Investigation & Fix

**Date:** 2026-04-15
**Bug found in:** `claude_proxy.py` — `rewrite_cache_control_markers()`
**Primary symptom:** `/btw hi` fails with "Prompt is too long"; `count_tokens` returning 400 on slow-path requests.
**Root cause:** Anthropic's TTL ordering rule violated because proxy upgrades messages to `ttl=1h` but leaves system/tools at default `ttl=5m`.

This document walks through the full investigation — what we observed, the wrong hypotheses we chased, and how we landed on the correct fix. Read this if you're ever debugging "why is my cache_control doing something weird" or "why is count_tokens rejecting a request that looks fine".

---

## TL;DR

- Anthropic processes cache_control blocks in this order: **`tools → system → messages`**.
- Within that order, TTL must be **non-increasing**: a `ttl='1h'` block CANNOT come after a `ttl='5m'` block.
- Claude Code sets system/tools markers to default TTL (= 5m).
- Our proxy was setting messages' last marker to `ttl='1h'` without touching system/tools.
- Result: `system(5m) → messages(1h)` is the forbidden "5m then 1h" pattern. Anthropic returned 400.
- count_tokens returned `None` → trim logic fell through to untrimmed passthrough → body ballooned to 1.8M tokens → Anthropic rejected again with "prompt too long".
- **Fix:** when the proxy inserts its ttl=1h messages marker, also walk `system` and `tools` arrays and upgrade any existing cache_control markers to `ttl='1h'`. Single consistent TTL across the whole request.
- **Verified:** `/btw` works; count_tokens succeeds on slow-path requests; TTL ordering errors no longer appear in proxy.log.

**Note on impact:** The visible user-facing symptom is `/btw` being unusable — that's the concrete functional bug this fix closes. The other effects (occasional count_tokens failures, cascade to passthrough) added log noise and potentially some latency, but on a Claude Max flat-rate subscription the dollar-cost framing doesn't apply. If you're reading this as "the fix saves $X/day", don't — it restores a broken feature and cleans up proxy behavior, not a direct billing impact.

---

## Background: how the proxy treats cache_control

The proxy's sliding-window design relies on aggressive prompt caching to hide its per-turn cost. On every slow-path `/v1/messages` request the proxy:

1. Computes a session key (hash of first real user message).
2. Looks up the per-session anchor from `_session_watermarks`.
3. `build_body_at_anchor(anchor)` drops groups before the anchor.
4. Calls `rewrite_cache_control_markers(body)`, which:
   - Strips every existing `cache_control` marker on messages content blocks.
   - Walks to the last message's last cacheable block and attaches one new `{"type": "ephemeral", "ttl": "1h"}` marker there.
5. Calls `count_tokens_via_anthropic(trial_bytes)` to measure the actual token cost.
6. If over `SHIFT_THRESHOLD`, advances the anchor forward.
7. Forwards the final body to Anthropic.

The design rests on two assumptions that this bug violated:

- **(A)** The messages-level marker is the only cache_control the proxy cares about. System/tools markers are "load-bearing" and Claude Code's responsibility.
- **(B)** `count_tokens` failures indicate "unknown size, pass through the original body safely."

Both assumptions turned out to be wrong in the presence of Anthropic's TTL ordering rule.

---

## Symptoms

### Visible to the user

```
> /btw hi
  Prompt is too long
  ↑/↓ scroll · f to fork · Esc to dismiss
```

`/btw hi` is a two-character prompt — it cannot possibly be too long on its own. Yet Claude Code refused to send it.

### Visible in proxy.log

Repeated entries like:

```
[proxy] → POST /v1/messages?beta=true  body=21791602B  msgs=4009 system_blocks=3
[proxy]   → sliding-window check, body=21,791,602B, prior_anchor=583
[proxy] ! count_tokens failed: 400 {"type":"error","error":{"type":"invalid_request_error",
  "message":"messages.820.content.0.cache_control.ttl: a ttl='1h' cache_control block must
  not come after a ttl='5m' cache_control block. Note that blocks are processed in the
  following order: `tools`, `system`, `messages`."}}
[proxy]   ! count_tokens returned None — passing through
[proxy] ← 400  in 8797ms
[proxy] ✗ 400 error body: {"type":"error","error":{"type":"invalid_request_error",
  "message":"prompt is too long: 1898872 tokens > 1000000 maximum"}}
```

Two distinct 400s from Anthropic in the same turn:

1. On `/v1/messages/count_tokens`: **TTL ordering violation**.
2. On `/v1/messages`: **prompt too long (1.9M > 1M)**.

And a pattern in the log that initially looked like extra full-cache rebuilds:

```
Turn N-1: cache_read=679,145  cache_create=191   out=679       ← healthy incremental
Turn N:   count_tokens failed (TTL violation)
          cache_read=0        cache_create=553,574  out=2,708  ← full miss
Turn N+1: cache_read=553,574  cache_create=2,777  out=1,462    ← recovered
```

I initially framed these full misses as "bug-induced extra rebuilds costing ~$10 each". The user correctly pushed back on two points:

1. **500k rebuilds are part of the design, not the bug.** The proxy's sliding-window ratchet deliberately fires at `SHIFT_THRESHOLD` and cuts the kept content in half, which shows up in the log as exactly this pattern (`cache_read=0`, `cache_create=~half_the_session`). That's the intended sawtooth, not a regression. Some of the `cache_read=0` turns in the log are genuine ratchet fires; others may have been TTL-cascade misses on top. From the log alone it's hard to distinguish without cross-referencing the ratchet's SHIFT events against the TTL failures.

2. **Dollar-per-event framing doesn't map to Claude Max.** On a flat-rate $100/month subscription, "this rebuild costs $10" is meaningless. The real cost dimensions are quota toward the 5-hour rolling window and latency (a full miss takes several seconds of prefill that an incremental read wouldn't). User observation was that ratchet fires in practice did NOT cause visible usage-meter spikes, which suggests Max's accounting treats cache writes generously.

So the honest framing of what this bug was actually costing before the fix:

- `/btw hi` completely non-functional — this is the concrete user-visible bug.
- count_tokens 400 errors filling proxy.log — hard to tell legitimate errors from cache_control noise.
- Possible extra latency on turns where TTL cascade happened — magnitude unverified.
- Possible quota bleed — unverified and apparently small enough to not show on the user's meter.

The $10-18/event number I originally wrote was based on API per-token pricing and is **wrong for the Claude Max case**. The fix is still worth it — `/btw` is a useful feature and the log becomes debuggable again — but don't read this doc as "recovered $X/day in real money". It didn't.

---

## Investigation path — the wrong hypotheses

### Wrong hypothesis #1: `/btw` bypasses the proxy

**Claim:** `/btw` uses a separate code path in Claude Code that doesn't go through the proxy's trim logic.

**Evidence against:** The proxy log showed the `/btw` requests arriving and being processed — they went through `sliding-window check` with `prior_anchor=583`, same as the main session. They hit the same code path, same session key, same stored anchor.

### Wrong hypothesis #2: `/btw` constructs a bigger payload

**Claim:** `/btw` sends some extra context (maybe a fork of the current session plus the sub-prompt) that makes the payload exceed the limit.

**Evidence against:** The body size at the moment of failure (~21 MB, 1.89M tokens) was the same shape as a normal slow-path main-session request. `/btw` wasn't adding context on top of main — it was using the same content. The size was the same as what the proxy successfully trimmed on adjacent turns.

### Wrong hypothesis #3: The anchor wasn't being applied

**Claim:** For some reason the proxy didn't run `build_body_at_anchor(583)`, so the full untrimmed body was measured.

**Evidence against:** The log shows `→ sliding-window check, body=21,791,602B, prior_anchor=583` — the anchor IS looked up. Separately, `trial_bytes = build_body_at_anchor(anchor)` is the FIRST thing `trim_to_sliding_window` does before even calling count_tokens. The trimmed body was built correctly.

This is where things got confusing. The trim worked. The count_tokens failed. The fallback returned the untrimmed original. Why?

### Wrong hypothesis #4: First-turn edge case

**Claim:** count_tokens only fails on the very first turn of a session, when `prior_anchor=0` and the proxy has nothing to fall back to. The rest of the time it works.

**Evidence against:** The log clearly shows `prior_anchor=583` on the failing turns. This is NOT a first-turn. The session had been running for thousands of messages. The bug was firing mid-session.

This hypothesis came from a reasonable instinct — "maybe the count_tokens failures are bootstrap issues" — but the log refuted it immediately.

### Wrong hypothesis #5: The root cause is "count_tokens can't handle big payloads"

**Claim:** count_tokens has its own max size, and when the body is too big count_tokens just refuses to measure it.

**Evidence against:** If that were the case, the error message would be `"prompt is too long"` at count_tokens. It wasn't. The count_tokens error was:

```
messages.820.content.0.cache_control.ttl: a ttl='1h' cache_control block
must not come after a ttl='5m' cache_control block.
```

That's a **structural** error, not a size error. Anthropic is rejecting the request because the cache_control markers are in the wrong TTL order, completely independent of how big the payload is.

This was the turning point. The error message had been staring at us for a while but we kept reading it as "something about size". It was actually about **ordering**.

---

## The real root cause

Anthropic's prompt caching has an **ordering rule** that isn't obvious until you read the error message carefully.

### The rule, as Anthropic stated it

From the error:

> "Blocks are processed in the following order: `tools`, `system`, `messages`."
> "A `ttl='1h'` cache_control block must not come after a `ttl='5m'` cache_control block."

In other words: within a request, the cache_control markers are implicitly ordered by their field location (tools first, then system, then messages). Within that order, TTL values must be **non-increasing**: long TTLs have to come before short TTLs, never after.

### Why Anthropic enforces this

The order rule is about cache storage correctness. Consider two requests with the same prefix:

```
Request A: [system ttl=1h][messages ttl=1h]
Request B: [system ttl=1h][messages ttl=5m]
```

A request that only differs in the messages TTL should still share the system cache. That works because both refer to the same system prefix with a 1h TTL storage commitment. The reverse is problematic:

```
Request A: [system ttl=5m][messages ttl=1h]
```

Here the request is saying "store messages for 1 hour, but store the system prefix that the messages depend on for only 5 minutes". If the system prefix expires at 5 minutes but the messages cache entry still exists at 10 minutes, the messages entry references missing data. Anthropic chose to reject this at request time rather than silently drop the longer TTL to match the shorter one.

So the rule is: the deepest cache point's TTL must be **at most as long as** every earlier cache point's TTL. Or equivalently, earlier points must have TTL **at least as long as** later points.

### Our proxy's violation

The proxy was only setting the last-message marker to `ttl=1h`. Claude Code was providing system/tools markers with the API default `ttl=5m` (no explicit TTL → defaults to 5m). Result:

```
[tools ttl=5m][system ttl=5m][messages ttl=1h]
 ^^^^^^^^^^^   ^^^^^^^^^^^^   ^^^^^^^^^^^^^
 shorter       shorter        longer  ← violation
```

Anthropic's count_tokens endpoint enforces the same validation as `/v1/messages`, so the call returned 400 with the ordering error. Our proxy's trim logic didn't recognize this as a structural error — it treated `None` as "unknown size, be safe" — and fell through to forwarding the untrimmed original 21 MB body. Anthropic then rejected that at `/v1/messages` with "prompt too long".

---

## Why was 1h in the proxy and 5m in Claude Code in the first place?

### Anthropic's choice to default to 5m

Prompt caching launched in mid-2024 with a fixed 5m TTL and no TTL option. The 1h TTL came later as an opt-in beta. Anthropic kept 5m as the default for infrastructure reasons — KV cache storage is expensive (~100GB per long session), and letting every request opt into 1h would balloon their fleet's memory footprint. The 1h TTL also costs 1.5x base on cache writes (vs. 1.25x for 5m), which pays for the extra storage time.

So the default has always been: "if you don't ask for 1h, you get 5m". Most workloads (sub-5-minute request intervals) don't need more.

### Claude Code's choice to leave the default alone

Claude Code doesn't explicitly set a TTL on its system/tools cache_control markers. It writes `{"type": "ephemeral"}` without a `ttl` field, and Anthropic fills in 5m. This is a deliberate conservative choice — Claude Code is serving a broad user base and most users don't need 1h semantics. The 25% write premium isn't worth paying for users whose sessions finish within 5 minutes.

### The proxy's choice to override for power users

Our proxy targets a different workload pattern: tool-heavy coding sessions where idle intervals are often 10-30 minutes (waiting for background tasks, reading output, thinking) and occasionally up to an hour. For this workload, 5m is catastrophic — every idle period over 5 minutes triggers a full cache rebuild. So the proxy opts into 1h explicitly on the messages marker via `rewrite_cache_control_markers`.

The bug was that the proxy only opted in **partially**. It set ttl=1h on its own marker (the last message) but didn't upgrade the system/tools markers that Claude Code had set to 5m. The TTL ordering rule requires consistency, so the partial opt-in was rejected.

---

## The fix

**Single change to `rewrite_cache_control_markers`:** before doing anything with messages, walk the `system` and `tools` fields and upgrade any existing `cache_control` markers to `ttl='1h'`.

```python
def rewrite_cache_control_markers(body: dict) -> None:
    """Replace any existing messages-level cache_control with exactly one new
    marker on the last cacheable block of the last message, with ttl=1h.

    Also UPGRADES any existing cache_control markers in `system` and `tools`
    fields to ttl=1h. Anthropic enforces ordering: processing order is
    tools → system → messages, and a ttl='1h' block cannot come after a
    ttl='5m' block. Since we place ttl=1h on messages (which is processed
    last), everything that comes before it (system, tools) must also be 1h
    — otherwise count_tokens and /v1/messages both return 400.
    """
    # Upgrade any existing system/tools cache_control markers to ttl=1h.
    # Claude Code sets these with default ttl (=5m). Without this upgrade,
    # the ordering rule is violated when we add ttl=1h on messages below.
    for field in ("system", "tools"):
        items = body.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            cc = item.get("cache_control")
            if isinstance(cc, dict):
                cc["ttl"] = "1h"

    # ... existing messages logic unchanged ...
```

Seven lines of new code. No new functions, no state, no new failure modes. The upgrade is idempotent and preserves Claude Code's decision about *where* to place markers — it only adjusts the TTL to match our outer 1h policy.

### What we deliberately did NOT do

- **Did not add new system/tools markers.** If Claude Code didn't place a marker on a particular block, we leave it alone. We only upgrade existing ones. This preserves Claude Code's judgment about what's worth caching.
- **Did not downgrade messages to 5m.** That would have eliminated the ordering violation but also thrown away the 1h cache retention that the proxy exists to provide. We keep the 1h commitment on messages and bring system/tools up to match.
- **Did not add a fallback trim path** for count_tokens failures. Initially I proposed returning `trial_bytes` instead of `body_bytes` on count_tokens failure. The user correctly pushed back: the main trim path is already proven to work, we just need to fix what's *breaking* count_tokens, not add a new safety net that papers over the breakage. Removing the TTL violation makes count_tokens succeed, which makes the existing trim path work correctly, which routes the request normally. No new code path needed.

---

## Verification

Restart the proxy (`taskkill /F /PID ...` both instances, then `python claude_proxy.py`) and observe:

### Before the fix

```
[proxy] ! count_tokens failed: 400 ... cache_control.ttl ordering violation
[proxy]   ! count_tokens returned None — passing through
[proxy] ← 400  prompt is too long: 1898872 tokens > 1000000 maximum
```

Every few turns, a TTL violation + passthrough + Anthropic rejection.

### After the fix

```
[proxy] ⏱ total=11407ms trim=1282ms (ct=1×1188ms) send=8219ms ttfb=8219ms stream=3188ms
           | in=1 cache_read=582,698 cache_create=706 out=297
```

- `ct=1×1188ms` — count_tokens succeeded, returned an integer
- `cache_read=582,698` — the longest-prefix match hit, reusing most of the prior cache
- `cache_create=706` — only the tiny delta (new assistant response + new user message) was written
- No 400 errors
- `/btw hi` responds normally with "Hi! What's up?"

### Impact (honest version)

What this fix concretely changes:

- **`/btw hi` functionality restored.** Before the fix, any `/btw` prompt would fail with "Prompt is too long" because the cascade (TTL error → count_tokens None → untrimmed passthrough → Anthropic rejection) was firing on every `/btw` invocation. This was the primary user-visible bug.
- **count_tokens no longer returns 400 on slow-path requests.** Proxy.log is clean enough to see real errors again.
- **Consistency between proxy intent and actual TTL policy.** The proxy's sliding-window design assumes long cache retention (1h) — before the fix, that intent was getting silently undermined on a subset of turns where the TTL violation triggered a cascade. Now every request either has 1h TTL everywhere or nothing.

What this fix does NOT change:

- **Sawtooth ratchet fires still happen.** Those `cache_read=0, cache_create=~half` events are part of the ratchet design, not bugs. The proxy deliberately fires a shift at `SHIFT_THRESHOLD` and drops 50% of kept content. This shows up in logs as a full miss, but it's expected.
- **No real dollar savings on Claude Max.** If you're on the $100/month subscription, cache cost is already absorbed into the flat rate. The original draft of this doc quoted "$10-18 per event" and "$170 per 1000 turns" — those numbers come from API per-token pricing and do not apply to Max. The user confirmed that ratchet fires in practice never caused visible usage-meter spikes, which backs up the read that the bug's cost impact on Max was minimal.
- **Cache hit rate number unchanged in any dramatic way.** The main session was already running at ~99% cache hit rate before the fix (because most turns didn't trigger the TTL cascade). The fix closes the remaining leaks on the subset of turns that did.

The simplest accurate summary: **`/btw` was broken and now it isn't, and the log is cleaner**. That's the whole story. Don't read a billing narrative into it.

---

## Lessons for future debugging

### 1. Read the error message you already have

The TTL ordering error was in the log from the first failing request. I spent several wrong hypotheses chasing "what makes /btw special" and "why is count_tokens refusing big payloads" before actually reading the error message word by word. The message literally says what's wrong and what the rule is — both pieces needed to debug.

### 2. Separate symptom from cause when there are cascading failures

The visible symptom was "Prompt is too long: 1.89M > 1M". That is true — 1.89M is genuinely too long. But the interesting question isn't "why is the payload 1.89M" (it's always been roughly that size), it's "why didn't the trim reduce it to the usual 500k?" Two levels of cascade:

```
1. TTL ordering violation → count_tokens returns 400
2. count_tokens 400       → trim function returns None
3. trim returns None      → proxy returns untrimmed original body
4. Untrimmed 1.89M body   → Anthropic returns "prompt too long"
```

Only fixing step 4 (e.g., adding a fallback trim on passthrough) papers over the real bug. The root cause is step 1; fix that and steps 2-4 never happen.

### 3. Intuition about "reuse the existing working path" beat my instinct to add fallback code

When I first saw the failure I proposed adding a local token estimator and a fallback trim that kicks in when count_tokens fails. The user pushed back: "the main trim path is already proven to work, why don't we just make count_tokens succeed so that path runs?" That framing immediately pointed at the TTL issue instead of the fallback logic, and produced a much smaller fix. Seven lines vs. hundreds.

This is a general principle: if something was working and stopped working, figure out what changed rather than building a new path around the thing that broke.

### 4. The `count_tokens` endpoint is a canary for `/v1/messages` validation

Anthropic's `/v1/messages/count_tokens` endpoint enforces the same request validation as `/v1/messages`. That's a useful property: if you can get count_tokens to pass, the real endpoint will too. But it's also a failure mode: **anything that breaks count_tokens also breaks the real call, even if you route around it**. Our fallback-on-count_tokens-failure path was useless — every condition that made count_tokens fail would also make `/v1/messages` fail.

If you're writing proxy logic, treat count_tokens as a dry-run validation, not as an optional measurement. Don't assume you can route past it.

### 5. The "why is it working at all" question

Before the fix, the proxy was returning unconditional untrimmed bodies whenever count_tokens failed. That should mean **every** turn after a TTL violation got rejected by Anthropic. But we were sitting there, having a long conversation with thousands of successful turns in the session's history. How?

The answer: count_tokens only fails when **our** rewrite introduces a ttl=1h marker AND the upstream request already has ttl=5m markers in system/tools. If Claude Code happened to send a request where the system had no cache_control markers at all (or had 1h markers already), the rewrite produced a consistent 1h-everywhere request and count_tokens passed. That's most turns. The TTL-violating requests were a subset — roughly 1% of turns based on log counts — and each of those turned into a passthrough-and-reject cycle. The session was working *despite* the bug, not because the bug was benign.

### 6. Don't invent economic narratives that aren't true

In the first draft of this doc I wrote "$10-18 per event" and "$170 per 1000 turns savings" based on API pricing math. The user pointed out (a) they're on Claude Max flat-rate, so those numbers are meaningless, and (b) they never observed a usage-meter spike correlating with ratchet fires, so even the "quota impact" framing wasn't showing up in practice. I had produced a confident-sounding financial impact statement that was **unverified and didn't match the user's direct observation**. That's worse than admitting the fix's value is smaller than advertised.

The discipline to learn: when writing up a fix, distinguish between "the concrete thing this closes" (in our case, `/btw` being unusable) and "the extrapolated impact" (everything else). Only commit confident numbers when you've actually measured them in the right billing model.

---

## Related design notes

- **Ordering rule also applies to count_tokens.** Anthropic mentioned the ordering in the error message but not in the public prompt-caching docs (as of early 2026). If you're designing a proxy that rewrites cache_control, the rule must be respected at the validation layer.
- **System/tools markers are separate from messages markers.** Our proxy used to treat messages as "ours" and system/tools as "Claude Code's responsibility". That division was defensible until the ordering rule made them semantically coupled. Now our rewrite function touches both, because consistency is a whole-request property.
- **The 1h TTL costs 1.5x base on write but 0.1x base on read.** Break-even is one read. Any session that uses a cache more than once wins by being on 1h if the reads span 5 minutes. For power-user workloads this is always a win.
- **Trim happens *before* cache_control rewrite** (in `build_body_at_anchor`). Good: means the rewrite only applies markers to content that will actually be sent. Bad: it also means count_tokens operates on the trimmed body, so a trimmed request with ordering violations still fails.

---

## File change

- `claude_proxy.py` — `rewrite_cache_control_markers` now upgrades system/tools cache_control markers to ttl=1h before handling messages.

Commit: [see git log for hash]

---

## Observation backlog — threshold & sawtooth cost

Things we noticed during this investigation that are NOT bugs and NOT current fixes, but are worth keeping in mind as the proxy evolves. Revisit when there's empirical data to justify changes.

### 1. `SHIFT_THRESHOLD=700000` is conservative — could potentially push higher

**Current state:** The proxy trims when count_tokens reports ≥ 700k tokens. With Anthropic's ~1.26-1.4x expansion from count_tokens to actual processed tokens, plus the 64k `max_tokens` reservation, this lands around 974k real tokens — just under the 1M model cap.

**User observation (live session):** The Claude Code status bar was showing ~63% when the proxy log reported ~503k kept tokens. That's `input_tokens ≈ 630k` at trigger time. The Anthropic-side trigger point is roughly at 87% of the 1M model window, which means we have ~130k real tokens of headroom left unused per fire cycle.

**Theoretical improvement:** Push the threshold so the proxy fires closer to 95% of the real limit instead of ~87%. Each fire cycle would buy ~40 extra turns of runway before the next fire.

**Why we didn't do it yet:** The 700k value is not arbitrary — it was lowered from 900k in an earlier commit after observing 413s at count_tokens=791,826. The expansion ratio depends on workload (code-dense content expands more, images even more). A fixed magic number high enough to hit 95% on text-heavy turns may hit 103% on code-heavy turns and trigger 413. The cost of a single 413 (retry cascade, cache invalidation, user latency spike) likely exceeds the benefit of 40 extra turns per cycle.

**What would justify pushing:**
- Collect the actual `input_tokens / count_tokens` ratio across many turns and workloads.
- Observe whether the ratio is stable enough that a safety margin of +5% is sufficient, vs. the current +30% margin.
- Implement an adaptive threshold that starts conservative and tightens based on rolling ratio observations.

**Realistic target:** Something like 90-92% of real limit, not 95%. The extra 3-5% headroom would absorb workload variance without giving up the aggressive-margin safety.

### 2. Sawtooth fires are cheaper than the math suggests

**User observation (live session):** Watching ratchet fires happen in real-time, the user did NOT notice:
- Usage meter spikes
- Noticeable latency jumps
- Any user-visible penalty

This contradicts the naive theoretical cost analysis (550k fresh prefill = ~6 second prefill + significant cost).

**Proxy log evidence:**
```
Rebuild turn:   cache_read=0, cache_create=553,574  total=48468ms  stream=42281ms  (non-stream ~6s)
Normal turn:    cache_read=622,254, cache_create=2,101  total=16516ms  stream=10563ms  (non-stream ~6s)
```

The non-stream portion (setup + prefill) is **roughly the same** for a rebuild turn and a normal cache-hit turn. Both ~6 seconds. The difference in total time is entirely in how long the output takes to stream, which is driven by output length, not by cache state.

**Hypotheses for why rebuild is cheaper than expected:**
- GPU prefill throughput is higher than we modeled (tens of thousands of tokens/second on H100/H200 class hardware).
- Anthropic's internal cache layer may have implicit dedup / radix-tree sharing that's working in our favor even when our explicit markers miss.
- Cache read has its own I/O cost (loading KV state into active memory) that narrows the gap with fresh compute.
- Claude Max's flat-rate accounting absorbs cache_create without pushing the usage meter noticeably.

**Implication for design:** The sawtooth cost we feared is much smaller than the theory predicted. This shifts the risk/benefit calculation on threshold pushing:
- **Before observation:** "Fires are expensive, so minimize fires with a big 50% drop — pay the threshold-too-conservative tax to keep fire count low."
- **After observation:** "Fires are cheap, so minimize unnecessary runway waste — push the threshold higher and accept more frequent but harmless fires."

**Open question:** How much of the "cheap rebuild" observation is specific to Claude Max accounting vs. fundamental API behavior? If a pay-per-token user ran the same workload, would they see a bigger cost spike that we just don't see because our billing is flat? The answer determines whether this observation generalizes or is Max-specific.

### 3. Things to track over future sessions

If we want to eventually make data-driven changes, log these across multiple sessions:

- Count of ratchet fires per session
- `input_tokens / count_tokens` ratio on slow-path requests (compute rolling mean, stddev)
- Non-stream time on fire turns vs. normal turns (is the prefill cost really the same, or is there session-specific variance?)
- Whether any 413s slip through despite the current 700k threshold
- Cache hit rate on the turn immediately after a fire (should be ~100% if the new prefix is being cached correctly)
- Distinction between ratchet fires (`cache_read=0` because the anchor moved) and "mystery misses" (`cache_read=0` for any other reason — would indicate a cache-invalidation bug)

With enough data, the threshold could be tuned with confidence rather than conservatism. Until then, 700k is the empirically-safe floor and premature optimization is the opposite of what this proxy needs.

### Discipline

**Do not touch `SHIFT_THRESHOLD` or add adaptive logic based on theoretical reasoning alone.** Both of these observations came from watching real behavior during a long session. Future changes should be justified by similar concrete observations, not "I ran the math and it should be fine". The proxy's stability over the past weeks has been worth more than any incremental efficiency gain, and aggressive optimization has a history of trading stability for marginal wins that don't materialize in practice.
