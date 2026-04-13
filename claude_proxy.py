"""Claude Code API proxy — token-aware trim.

Listens on localhost:9999 and forwards every request to api.anthropic.com.
When a /v1/messages body would exceed Anthropic's token cap (the real
limit — there is no separate byte cap we've observed), drops the oldest
"turn groups" until Anthropic's own count_tokens endpoint reports the
request as under budget. Otherwise forwards unchanged.

Two-path design:
    Fast path — body < FAST_PATH_THRESHOLD bytes
        BPE guarantees tokens <= bytes, so if the body is small enough
        it cannot possibly exceed the token cap. Forward unchanged.
        Zero overhead. This covers 99% of requests (quota checks, Haiku
        helper calls, small Opus requests).

    Slow path — body >= FAST_PATH_THRESHOLD bytes
        Ask Anthropic's /v1/messages/count_tokens endpoint for the exact
        token count. If under TOKEN_BUDGET, forward unchanged. If over,
        binary-search turn groups to find the maximum newest-N that fits,
        then forward the trimmed body.

Usage:
    python claude_proxy.py

Env vars:
    CLAUDE_PROXY_FAST_BYTES    body size (bytes) below which to skip the
                               token check. Default 950,000 (~0.9 MB).
    CLAUDE_PROXY_TOKEN_BUDGET  token ceiling we trim to. Default 950,000
                               (margin under Anthropic's 1,000,000 cap).
    CLAUDE_PROXY_QUIET         if set, only log trims and errors.

Then in another terminal:
    $env:ANTHROPIC_BASE_URL="http://localhost:9999"
    claude

What a "turn group" is:
    A real user message followed by the assistant response(s) and tool
    round-trips until the next real user message. Dropping whole groups
    preserves tool_use/tool_result pair integrity, user/assistant
    alternation, and the "first message must be user" invariant.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn


UPSTREAM = "https://api.anthropic.com"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 9999

# The only real cap Anthropic enforces on /v1/messages is a TOKEN cap
# (1,000,000 for Opus 4.6 [1m]). There is no separate byte cap — we saw
# a 14.7 MB body rejected with "prompt is too long: 1147702 tokens >
# 1000000 maximum", so bytes themselves are fine.
#
# We use a two-path design:
#   - Fast path: if body is small enough that it cannot mathematically
#     exceed the token cap (BPE guarantees tokens <= bytes), forward as-is.
#   - Slow path: ask Anthropic's /v1/messages/count_tokens endpoint for
#     the exact token count, trim turn groups until it fits, forward.
#
# FAST_PATH_THRESHOLD is in bytes. Under this, we skip the token check
# entirely. 950_000 is a safe margin under 1M (accounts for special
# tokens Anthropic adds internally).
FAST_PATH_THRESHOLD = int(os.environ.get("CLAUDE_PROXY_FAST_BYTES", "950000"))

# TOKEN_BUDGET is in tokens. This is what we actually try to stay under
# when the slow path fires.
#
# IMPORTANT: count_tokens API systematically UNDER-reports vs actual
# processing. We empirically observed count_tokens=912k while Anthropic's
# real processing reported 1,148k for the same body — a 26% discrepancy.
# Likely cause: count_tokens doesn't see max_tokens reservation or
# certain special internal additions. We use a generous safety margin.
#
# 700_000 budget × ~1.26 expansion = ~882_000 actual processing tokens,
# leaving ~118k margin from the 1M cap.
TOKEN_BUDGET = int(os.environ.get("CLAUDE_PROXY_TOKEN_BUDGET", "700000"))

# Verbose by default. Set CLAUDE_PROXY_QUIET=1 to only log trims and errors.
QUIET = os.environ.get("CLAUDE_PROXY_QUIET", "").strip() not in ("", "0", "false", "False")

# Headers we should NOT forward (they get recalculated or refer to our proxy)
STRIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "accept-encoding",  # force identity — avoid compression mismatch on relay
}
STRIP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
}


# Shared client for connection pooling. Created here, closed in lifespan().
client = httpx.AsyncClient(
    base_url=UPSTREAM,
    timeout=httpx.Timeout(None),  # let upstream decide
    http2=False,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup (nothing to do) + shutdown (close client)."""
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(lifespan=lifespan)


def log(msg: str) -> None:
    print(f"[proxy] {msg}", flush=True)


def vlog(msg: str) -> None:
    """Verbose log — suppressed when QUIET=1."""
    if not QUIET:
        print(f"[proxy] {msg}", flush=True)


def short_auth(auth: str | None) -> str:
    if not auth:
        return "<none>"
    return auth[:20] + "...<redacted>"


def describe_body(body_bytes: bytes) -> str:
    """Summarize a JSON body for logging."""
    try:
        obj = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return f"<non-json, {len(body_bytes)} bytes>"

    model = obj.get("model", "?")
    messages = obj.get("messages", [])
    system = obj.get("system")
    parts = [f"model={model}", f"msgs={len(messages)}"]
    if isinstance(system, str):
        parts.append(f"system={len(system)}B")
    elif isinstance(system, list):
        parts.append(f"system_blocks={len(system)}")
    return " ".join(parts)


# ---------- trim logic ----------

def is_real_user_message(msg) -> bool:
    """True if this is a user message from the human (not a tool_result)."""
    if not isinstance(msg, dict):
        return False
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        # If any block is a tool_result, this is a "fake user" message
        # (a tool output dressed up to satisfy alternation rules).
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return False
        return True
    return False


def split_into_turn_groups(messages: list) -> list[list]:
    """Group messages by real-user-initiated turn.

    Each group starts with a real user message and contains the
    assistant response(s) and any tool round-trips until the next
    real user message.

    Dropping whole groups preserves:
      - user/assistant alternation
      - tool_use <-> tool_result pair integrity
      - "first message must be user" invariant
    """
    groups: list[list] = []
    current: list = []
    for m in messages:
        if is_real_user_message(m) and current:
            groups.append(current)
            current = []
        current.append(m)
    if current:
        groups.append(current)
    return groups


def _group_preview(group: list, width: int = 30) -> str:
    """Get a short preview of the real user text in a group.

    Claude Code injects <system-reminder> blocks into user messages, so the
    first text block is usually framework noise. We prefer the first block
    that is NOT a system-reminder or slash-command envelope.
    """

    def _clean_text(t: str) -> str:
        return t.strip().replace("\n", " ").replace("\r", " ")

    def _is_framework_noise(t: str) -> bool:
        s = t.lstrip()
        return (
            s.startswith("<system-reminder>")
            or s.startswith("<command-message>")
            or s.startswith("<command-name>")
            or s.startswith("<local-command")
            or s.startswith("<task-notification>")
            or s.startswith("<bash-input>")
        )

    for m in group:
        if not is_real_user_message(m):
            continue
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Prefer the first text block that isn't a framework envelope.
            chosen = ""
            fallback = ""
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "text":
                    continue
                t = b.get("text", "") or ""
                if not fallback:
                    fallback = t
                if not _is_framework_noise(t):
                    chosen = t
                    break
            text = chosen or fallback
        else:
            text = ""
        text = _clean_text(text)
        if len(text) > width:
            text = text[: width - 1] + "…"
        return text or "<empty>"
    return "<no-user-msg>"


# Headers that count_tokens needs forwarded (auth + versioning).
COUNT_TOKENS_FORWARD_HEADERS = {
    "authorization",
    "x-api-key",
    "anthropic-version",
    "anthropic-beta",
}


# Fields that /v1/messages accepts but /v1/messages/count_tokens rejects.
# Strip these before posting to count_tokens.
COUNT_TOKENS_REJECTED_FIELDS = {
    "metadata",
    "stream",
    "max_tokens",
    "stop_sequences",
    "temperature",
    "top_p",
    "top_k",
    "service_tier",
}


async def count_tokens_via_anthropic(
    body_bytes: bytes, request_headers: dict
) -> int | None:
    """Ask Anthropic exactly how many tokens this /v1/messages body has.

    Returns the integer input_tokens count, or None on any failure
    (so the caller can fall back to pass-through).
    """
    ct_headers = {
        k: v
        for k, v in request_headers.items()
        if k.lower() in COUNT_TOKENS_FORWARD_HEADERS
    }
    ct_headers["content-type"] = "application/json"
    ct_headers["accept-encoding"] = "identity"

    # count_tokens is strict about which fields it accepts. Strip the ones
    # that only /v1/messages allows, otherwise we get 400 invalid_request.
    try:
        body_obj = json.loads(body_bytes.decode("utf-8"))
        if isinstance(body_obj, dict):
            for k in COUNT_TOKENS_REJECTED_FIELDS:
                body_obj.pop(k, None)
            ct_body_bytes = json.dumps(body_obj).encode("utf-8")
        else:
            ct_body_bytes = body_bytes
    except Exception:
        ct_body_bytes = body_bytes

    try:
        resp = await client.post(
            "/v1/messages/count_tokens",
            content=ct_body_bytes,
            headers=ct_headers,
        )
    except httpx.HTTPError as e:
        log(f"! count_tokens network error: {e}")
        return None

    if resp.status_code != 200:
        try:
            err_body = resp.text[:300]
        except Exception:
            err_body = "<unreadable>"
        log(f"! count_tokens failed: {resp.status_code} {err_body}")
        return None

    try:
        data = resp.json()
        return int(data.get("input_tokens"))
    except Exception as e:
        log(f"! count_tokens parse error: {e}")
        return None


async def trim_to_token_budget(
    body_bytes: bytes, budget_tokens: int, request_headers: dict
) -> tuple[bytes, dict | None]:
    """Trim oldest turn groups until Anthropic reports <= budget tokens.

    Uses binary search over turn groups to minimize count_tokens calls.
    Returns (final_bytes, trim_info). trim_info is None if no trim happened.
    """
    vlog(f"  → trim check, body={len(body_bytes):,}B")
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except Exception as e:
        log(f"  ! body parse failed: {e}")
        return body_bytes, None
    if not isinstance(body, dict):
        return body_bytes, None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return body_bytes, None

    original_messages = list(messages)
    groups = split_into_turn_groups(original_messages)
    if not groups:
        return body_bytes, None

    # First, check how many tokens the full body actually has.
    initial_tokens = await count_tokens_via_anthropic(body_bytes, request_headers)
    if initial_tokens is None:
        # count_tokens unreachable — fall back to pass-through.
        log(f"  ! count_tokens returned None — passing through")
        return body_bytes, None
    if initial_tokens <= budget_tokens:
        # Already fits. No trim needed.
        vlog(f"  → {initial_tokens:,}tok within budget, no trim")
        return body_bytes, None

    total_groups = len(groups)

    async def tokens_for_last_n_groups(n: int) -> tuple[int | None, bytes]:
        trial_msgs = [m for g in groups[-n:] for m in g]
        body["messages"] = trial_msgs
        trial_bytes = json.dumps(body).encode("utf-8")
        tokens = await count_tokens_via_anthropic(trial_bytes, request_headers)
        return tokens, trial_bytes

    # Binary search for the largest n (keep n newest groups) that fits.
    lo, hi = 1, total_groups
    best_n = 0
    best_bytes = body_bytes
    best_tokens = initial_tokens

    while lo <= hi:
        mid = (lo + hi) // 2
        mid_tokens, mid_bytes = await tokens_for_last_n_groups(mid)
        if mid_tokens is None:
            break
        if mid_tokens <= budget_tokens:
            best_n = mid
            best_bytes = mid_bytes
            best_tokens = mid_tokens
            lo = mid + 1
        else:
            hi = mid - 1

    if best_n == 0:
        # Even the last group alone is over budget. Forward it anyway —
        # Anthropic may reject, but this is the structurally minimum
        # request we can form.
        body["messages"] = groups[-1]
        best_bytes = json.dumps(body).encode("utf-8")
        best_n = 1
        best_tokens = await count_tokens_via_anthropic(best_bytes, request_headers)

    # Reassemble kept groups so the window preview matches what we forward.
    best_kept_groups = groups[-best_n:]
    first_kept_index = total_groups - best_n

    kept_previews = [_group_preview(g) for g in best_kept_groups]
    first_dropped_preview = None
    last_dropped_preview = None
    if first_kept_index > 0:
        first_dropped_preview = _group_preview(groups[0])
        last_dropped_preview = _group_preview(groups[first_kept_index - 1])

    return best_bytes, {
        "original_size": len(body_bytes),
        "final_size": len(best_bytes),
        "original_groups": total_groups,
        "kept_groups": best_n,
        "dropped_groups": total_groups - best_n,
        "original_messages": len(original_messages),
        "kept_messages": sum(len(g) for g in best_kept_groups),
        "first_kept_index": first_kept_index,
        "kept_previews": kept_previews,
        "first_dropped_preview": first_dropped_preview,
        "last_dropped_preview": last_dropped_preview,
        "tokens_before": initial_tokens,
        "tokens_after": best_tokens,
    }


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(request: Request, full_path: str):
    started = time.monotonic()

    # Read incoming body
    body = await request.body()
    body_size = len(body)

    # Filter headers before forwarding, and force identity encoding
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in STRIP_REQUEST_HEADERS
    }
    fwd_headers["accept-encoding"] = "identity"

    # Keep query string intact
    url = f"/{full_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    vlog(
        f"→ {request.method} {url}  "
        f"body={body_size}B  "
        f"auth={short_auth(request.headers.get('authorization'))}  "
        f"{describe_body(body) if body_size else ''}"
    )

    # Two-path design for /v1/messages:
    #   - Fast path: body under FAST_PATH_THRESHOLD bytes cannot exceed the
    #     token cap (BPE guarantees tokens <= bytes), so skip the token check.
    #   - Slow path: ask Anthropic's count_tokens endpoint for the exact
    #     count, trim turn groups if over TOKEN_BUDGET.
    # Skip count_tokens itself to avoid recursion (we call it directly below).
    is_messages = (
        full_path.startswith("v1/messages")
        and not full_path.startswith("v1/messages/count_tokens")
    )
    if is_messages and body_size >= FAST_PATH_THRESHOLD:
        trimmed, info = await trim_to_token_budget(body, TOKEN_BUDGET, dict(request.headers))
        if info is not None:
            body = trimmed
            body_size = len(body)
            log(
                f"✂ trimmed: "
                f"{info['tokens_before']:,}tok → {info['tokens_after']:,}tok  "
                f"({info['original_size']:,}B → {info['final_size']:,}B)  "
                f"groups: {info['original_groups']} → {info['kept_groups']} "
                f"(dropped {info['dropped_groups']})  "
                f"msgs: {info['original_messages']} → {info['kept_messages']}"
            )
            # Show the "window" — which user messages survived.
            start_idx = info["first_kept_index"]
            previews = info["kept_previews"]
            window_items = []
            for i, p in enumerate(previews):
                window_items.append(f"[{start_idx + i}] \"{p}\"")
            window_str = "  ".join(window_items)
            log(f"  window: {window_str}")
            if info["first_dropped_preview"] is not None:
                log(
                    f"  dropped: [0] \"{info['first_dropped_preview']}\" … "
                    f"[{start_idx - 1}] \"{info['last_dropped_preview']}\""
                )

    # Build the upstream request. We stream the response back as it arrives.
    upstream_req = client.build_request(
        method=request.method,
        url=url,
        content=body,
        headers=fwd_headers,
    )

    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        log(f"✗ upstream connection failed: {e}")
        return StreamingResponse(
            iter([b'{"type":"error","error":{"type":"proxy_error","message":"'
                  + str(e).encode("utf-8")
                  + b'"}}']),
            status_code=502,
            media_type="application/json",
        )

    elapsed_ms = (time.monotonic() - started) * 1000
    vlog(f"← {upstream_resp.status_code}  in {elapsed_ms:.0f}ms  (streaming...)")

    # On error (4xx/5xx), buffer the body so we can log it, then relay.
    # This helps diagnose why Anthropic rejected (token cap, byte cap, etc).
    async def relay():
        try:
            if upstream_resp.status_code >= 400:
                # Read whole body, log it, then replay to client.
                chunks = []
                async for chunk in upstream_resp.aiter_bytes():
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
                try:
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    log(f"✗ {upstream_resp.status_code} error body: {body_text[:500]}")
                except Exception:
                    log(f"✗ {upstream_resp.status_code} error body (undecodable): {len(body_bytes)}B")
                yield body_bytes
            else:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
        finally:
            await upstream_resp.aclose()

    # Filter response headers
    response_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in STRIP_RESPONSE_HEADERS
    }

    return StreamingResponse(
        relay(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


if __name__ == "__main__":
    log(f"starting on http://{LISTEN_HOST}:{LISTEN_PORT}")
    log(f"upstream:    {UPSTREAM}")
    log(
        f"fast path:   body < {FAST_PATH_THRESHOLD:,} bytes "
        f"({FAST_PATH_THRESHOLD / 1024 / 1024:.2f} MB)"
    )
    log(f"token budget: {TOKEN_BUDGET:,} tokens")
    log(f"mode:         {'quiet' if QUIET else 'verbose'}")
    log("set ANTHROPIC_BASE_URL=http://localhost:9999 in your client")
    uvicorn.run(
        app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="warning",  # silence uvicorn's own access log — we have our own
        access_log=False,
    )
