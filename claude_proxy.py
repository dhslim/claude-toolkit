"""Claude Code API proxy — sliding-window trim with three absolute-value knobs.

Listens on localhost:9999 and forwards every request to api.anthropic.com.
When a /v1/messages body would exceed Anthropic's token cap, uses a
per-session sliding-window anchor that advances monotonically: when the
count_tokens result crosses SHIFT_TRIGGER, an estimate-based single-pass
slice walks the turn groups newest → oldest and keeps as many as fit
under KEEP_TARGET tokens, then a count_tokens safety re-check advances
the anchor further if the estimate undershot the real cap.

Between shifts the anchor doesn't move, so the byte prefix stays stable
and Anthropic's prompt cache hits at ~99% rate via the 20-block lookback —
same behavior as Claude Code's native caching strategy. At shift moments,
one turn pays a ~3–5 min cache-miss penalty to establish a new snapshot.
On average ~1 shift per 100–250 turns, so the slow-turn rate is ~1%.

Two-path design:
    Fast path — body < FAST_PATH_THRESHOLD bytes
        BPE guarantees tokens <= bytes, so if the body is small enough
        it cannot possibly exceed the token cap. Forward unchanged.
        Zero overhead. This covers 99% of requests (quota checks, Haiku
        helper calls, small Opus requests).

    Slow path — body >= FAST_PATH_THRESHOLD bytes
        Apply the per-session sliding-window anchor (front-drop everything
        before groups[anchor]). Ask count_tokens for the exact size. If over
        SHIFT_TRIGGER, run a single-pass slice newest → oldest that keeps
        groups whose cumulative estimated size stays under KEEP_TARGET,
        then a single count_tokens re-check; if real count is still over
        MODEL_CAP, advance one group at a time until under. Always rewrite
        the messages-level cache_control to point at the new last cacheable
        block.

Usage:
    python claude_proxy.py

Env vars:
    CLAUDE_PROXY_FAST_BYTES       body size (bytes) below which to skip the
                                  token check. Default 950,000 (~0.9 MB).
    CLAUDE_PROXY_MODEL_CAP        Anthropic's actual /v1/messages token
                                  ceiling. Default 1,000,000. Used for the
                                  post-slice safety check and ⚠ over-cap
                                  warning. Don't change unless the API
                                  limit itself changes.
    CLAUDE_PROXY_SHIFT_TRIGGER    count_tokens value above which a shift
                                  fires. Default 700,000. (Legacy
                                  CLAUDE_PROXY_SHIFT_THRESHOLD also honored.)
    CLAUDE_PROXY_KEEP_TARGET      absolute token target the slice aims to
                                  land at. Default 400,000.
    CLAUDE_PROXY_QUIET            if set, only log trims and errors.

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

import hashlib
import json
import os
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

_KST = timezone(timedelta(hours=9))


def _ts() -> str:
    """Return current KST timestamp for log line prefixing."""
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")

_SCRIPT_DIR = Path(__file__).resolve().parent
_log_path = os.environ.get("CLAUDE_PROXY_LOG_FILE", str(_SCRIPT_DIR / "proxy.log"))
if _log_path:
    try:
        _fh = open(_log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = _fh
        sys.stderr = _fh
    except Exception:
        pass

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

# Three absolute-value constants govern the sliding window. No ratios, no
# multiplication — each number means exactly what it says.
#
#   MODEL_CAP_TOKENS  The actual Anthropic /v1/messages token ceiling
#                     (1,000,000 for Opus 4.x with context-1m-2025-08-07).
#                     Requests over this 400 with "prompt is too long".
#                     Used ONLY for the post-slice safety check and the
#                     ⚠ over-cap log warning. Not used in math elsewhere.
#                     Treat as immutable; this is the API's number, not ours.
#
#   SHIFT_TRIGGER     The count_tokens value above which a shift fires.
#                     700K leaves ~300K headroom under MODEL_CAP so we have
#                     time to react before the next turn's growth pushes
#                     us into the cap.
#
#   KEEP_TARGET       The absolute number of tokens we slice down to when a
#                     shift fires. 400K gives ~300K margin under TRIGGER so
#                     several conversation turns can grow before the next
#                     shift fires (cache-stability win), and gives ~600K
#                     margin under MODEL_CAP so the chars/4 estimator
#                     undershooting can't push the real count over the cap.
#
# IMPORTANT: count_tokens API systematically UNDER-reports vs the token
# count Anthropic's actual /v1/messages processing uses. We empirically
# observed a 413 at count_tokens=791,826 on 2026-04-14. The true ratio is
# workload-dependent: ~1.26x for prose/code, observed up to ~2.8x for
# code-dense / JSON-heavy STT2 sessions. Because _estimate_group_tokens
# is chars/4 (cheap), it under-counts even more than count_tokens does.
# The slice-to-target loop therefore PESSIMISTICALLY runs a safety re-check
# via count_tokens after the estimate-based slice, and advances further if
# the real count is still over MODEL_CAP.
MODEL_CAP_TOKENS = int(os.environ.get("CLAUDE_PROXY_MODEL_CAP", "1000000"))
SHIFT_TRIGGER = int(os.environ.get(
    "CLAUDE_PROXY_SHIFT_TRIGGER",
    os.environ.get("CLAUDE_PROXY_SHIFT_THRESHOLD", "700000"),  # backwards-compat
))
KEEP_TARGET = int(os.environ.get(
    "CLAUDE_PROXY_KEEP_TARGET",
    os.environ.get("CLAUDE_PROXY_SHIFT_TARGET", "400000"),  # also accepted
))

# Backwards-compat aliases — older code paths may still reference the
# old names. SHIFT_RATIO no longer drives behavior; computed for legacy
# log/diagnostic compatibility only.
HARD_CAP_TOKENS = MODEL_CAP_TOKENS
SHIFT_THRESHOLD = SHIFT_TRIGGER
SHIFT_RATIO = KEEP_TARGET / MODEL_CAP_TOKENS if MODEL_CAP_TOKENS else 0.0

# Verbose by default. Set CLAUDE_PROXY_QUIET=1 to only log trims and errors.
QUIET = os.environ.get("CLAUDE_PROXY_QUIET", "").strip() not in ("", "0", "false", "False")

# Statusline integration: every successful /v1/messages forward writes the
# real token count we sent upstream into a PER-SESSION TSV file. The session
# id comes from the request header `x-claude-code-session-id` (matches the
# transcript filename UUID statusline can derive from .transcript_path).
# statusline.sh reads only ITS session's file, so concurrent CC sessions
# don't pollute each other's bars.
LAST_SEND_DIR = Path.home() / ".claude" / "proxy_last_send"


def _publish_last_send(tokens: int, session_id: str | None) -> None:
    """Write <tokens>\\t<unix_ts>\\n to per-session TSV. Never raises.

    No session_id → skip the write (don't fall back to a global file; that
    would re-introduce the cross-session pollution this design fixes).
    """
    if not session_id:
        return
    # Basic safety: session ids are UUIDs in practice; reject anything else
    # to prevent path traversal if a header ever carries weird content.
    if not all(c.isalnum() or c == "-" for c in session_id) or len(session_id) > 64:
        return
    try:
        LAST_SEND_DIR.mkdir(parents=True, exist_ok=True)
        path = LAST_SEND_DIR / f"{session_id}.tsv"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(f"{int(tokens)}\t{int(time.time())}\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        # Statusline state is best-effort — must never break the forward.
        pass

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
    print(f"[{_ts()} KST] [proxy] {msg}", flush=True)


def vlog(msg: str) -> None:
    """Verbose log — suppressed when QUIET=1."""
    if not QUIET:
        print(f"[{_ts()} KST] [proxy] {msg}", flush=True)


def short_auth(auth: str | None) -> str:
    if not auth:
        return "<none>"
    return auth[:20] + "...<redacted>"


def extract_usage_from_sse(buf: bytes) -> str:
    # SSE 스트림에서 usage 정보를 뽑아낸다 (cache hit/miss 진단용)
    try:
        text = buf.decode("utf-8", errors="replace")
    except Exception:
        return "<undecodable>"

    input_t = cache_read = cache_create = output_t = None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            obj = json.loads(data_str)
        except Exception:
            continue
        t = obj.get("type")
        if t == "message_start":
            u = obj.get("message", {}).get("usage", {}) or {}
            input_t = u.get("input_tokens", input_t)
            cache_read = u.get("cache_read_input_tokens", cache_read)
            cache_create = u.get("cache_creation_input_tokens", cache_create)
            if "output_tokens" in u:
                output_t = u["output_tokens"]
        elif t == "message_delta":
            u = obj.get("usage", {}) or {}
            if "output_tokens" in u:
                output_t = u["output_tokens"]

    parts = []
    if input_t is not None:
        parts.append(f"in={input_t:,}")
    if cache_read is not None:
        parts.append(f"cache_read={cache_read:,}")
    if cache_create is not None:
        parts.append(f"cache_create={cache_create:,}")
    if output_t is not None:
        parts.append(f"out={output_t:,}")
    return " ".join(parts) if parts else "<no-usage>"


def _normalize_cc_1m_suffix(body: dict, headers_dict: dict) -> bool:
    """Strip CC 2.1.145's malformed [1m] suffix on model id; move to anthropic-beta.

    Claude Code 2.1.145 sends model="claude-opus-4-7[1m]" instead of using the
    proper anthropic-beta: context-1m-2025-08-07 header. Anthropic returns 404
    because that model id doesn't exist, and CC misinterprets the 404 as
    "Context limit reached / /clear to continue" on the FIRST prompt of a
    session.

    This fix rewrites the body's model to the bare id and adds the 1m context
    beta to the anthropic-beta header (comma-separated list).

    Returns True if a rewrite happened, False otherwise. Mutates both args.
    """
    model = body.get("model", "")
    if not (isinstance(model, str) and model.endswith("[1m]")):
        return False
    body["model"] = model[: -len("[1m]")]
    # Collect any existing anthropic-beta value across mixed-case keys.
    existing = ""
    for k in list(headers_dict.keys()):
        if k.lower() == "anthropic-beta":
            existing = headers_dict[k] or existing
            del headers_dict[k]
    betas = [b.strip() for b in existing.split(",") if b.strip()]
    if "context-1m-2025-08-07" not in betas:
        betas.append("context-1m-2025-08-07")
    headers_dict["anthropic-beta"] = ",".join(betas)
    return True


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


def session_key_for_messages(messages: list) -> str | None:
    """SHA-256 (16 hex chars) of the first real user message's cleaned text.

    Stable within a conversation — the first user message doesn't change until
    /compact or /clear, at which point the prefix is genuinely different and a
    fresh key is correct. Returns None if no real user message exists.

    Uses the same framework-noise filter as _group_preview so stray
    <system-reminder> injections don't perturb the key.
    """

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

    for m in messages:
        if not is_real_user_message(m):
            continue
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
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
        text = text.strip()
        if not text:
            return None
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return None


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


# 진단용 카운터 — proxy() handler가 turn 시작 시 리셋
_count_tokens_calls = 0
_count_tokens_elapsed_ms = 0.0

# Per-session sliding-window anchors. Key = hash of first real user message.
# Value = anchor_group_index — the earliest turn group the request includes.
# Everything before the anchor is dropped. The anchor is a ratchet: it only
# moves forward, never backward, so the byte prefix between shifts is stable
# and Anthropic's prompt cache keeps hitting. Bounded LRU so memory can't
# leak on long-running proxies.
_MAX_SESSIONS = 100
_session_watermarks: "OrderedDict[str, int]" = OrderedDict()

# One-shot probe: dumps the next real /v1/messages body to a file for
# offline inspection (counting cache_control markers, inspecting structure).
# Fires once per proxy process, then stays off.
_probe_fired = False
_PROBE_DUMP_DIR = os.environ.get("CLAUDE_PROXY_PROBE_DIR") or os.path.dirname(
    os.path.abspath(__file__)
)


def _count_cache_control_markers(body_obj: dict) -> dict:
    """Walk a /v1/messages body and count explicit cache_control markers.

    Returns a dict with counts per section so we know how many breakpoint
    slots Claude Code is already using and where, before designing the
    head/tail cache strategy.
    """
    counts = {"system": 0, "tools": 0, "messages": 0, "total": 0, "locations": []}

    def _scan_block(block, where: str):
        if isinstance(block, dict) and "cache_control" in block:
            counts[where] += 1
            counts["total"] += 1
            counts["locations"].append(
                f"{where}: type={block.get('type')} ttl={block.get('cache_control', {}).get('ttl', '5m')}"
            )

    sys_field = body_obj.get("system")
    if isinstance(sys_field, list):
        for b in sys_field:
            _scan_block(b, "system")
    elif isinstance(sys_field, dict):
        _scan_block(sys_field, "system")

    tools = body_obj.get("tools") or []
    if isinstance(tools, list):
        for t in tools:
            _scan_block(t, "tools")

    messages = body_obj.get("messages") or []
    if isinstance(messages, list):
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and "cache_control" in b:
                        counts["messages"] += 1
                        counts["total"] += 1
                        counts["locations"].append(
                            f"messages[{i}]:{b.get('type')} ttl={b.get('cache_control', {}).get('ttl', '5m')}"
                        )

    return counts


def _probe_dump_once(body_bytes: bytes) -> None:
    """Dump the next real /v1/messages body for offline inspection.

    Fires at most once per proxy process. Failures are swallowed — probes
    must never affect the real request path.
    """
    global _probe_fired
    if _probe_fired:
        return
    try:
        body_obj = json.loads(body_bytes.decode("utf-8"))
        counts = _count_cache_control_markers(body_obj)
        ts = int(time.time())
        dump_path = os.path.join(_PROBE_DUMP_DIR, f"probe-dump-{ts}.json")
        summary_path = os.path.join(_PROBE_DUMP_DIR, f"probe-dump-{ts}.summary.txt")
        with open(dump_path, "wb") as f:
            f.write(body_bytes)
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"cache_control markers in body\n")
            f.write(f"  total:    {counts['total']}\n")
            f.write(f"  system:   {counts['system']}\n")
            f.write(f"  tools:    {counts['tools']}\n")
            f.write(f"  messages: {counts['messages']}\n\n")
            f.write("locations:\n")
            for loc in counts["locations"]:
                f.write(f"  {loc}\n")
            f.write(f"\nbody_bytes: {len(body_bytes):,}\n")
            f.write(f"model:      {body_obj.get('model')}\n")
            f.write(f"msg_count:  {len(body_obj.get('messages') or [])}\n")
        log(
            f"[probe] captured /v1/messages body: "
            f"{counts['total']} cache_control markers "
            f"(system={counts['system']}, tools={counts['tools']}, messages={counts['messages']}) "
            f"→ {os.path.basename(dump_path)}"
        )
        _probe_fired = True
    except Exception as e:
        log(f"[probe] dump failed: {e}")
        _probe_fired = True  # don't keep retrying on a broken path


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

    global _count_tokens_calls, _count_tokens_elapsed_ms
    _count_tokens_calls += 1
    ct_t0 = time.monotonic()
    try:
        resp = await client.post(
            "/v1/messages/count_tokens",
            content=ct_body_bytes,
            headers=ct_headers,
        )
    except httpx.HTTPError as e:
        _count_tokens_elapsed_ms += (time.monotonic() - ct_t0) * 1000
        log(f"! count_tokens network error: {e}")
        return None
    _count_tokens_elapsed_ms += (time.monotonic() - ct_t0) * 1000

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


def _is_cacheable_block(block: object) -> bool:
    """True if a content block can legally hold an explicit cache_control marker.

    Anthropic docs: thinking blocks cannot hold explicit cache_control, and
    empty text blocks are never cached. Everything else (text with content,
    tool_use, tool_result, image, document) is a legal target.
    """
    if not isinstance(block, dict):
        return False
    btype = block.get("type")
    if btype == "thinking":
        return False
    if btype == "text" and not (block.get("text") or "").strip():
        return False
    return True


def rewrite_cache_control_markers(body: dict) -> None:
    """Replace any existing messages-level cache_control with exactly one new
    marker on the last cacheable block of the last message, with ttl=1h.

    Also UPGRADES any existing cache_control markers in `system` and `tools`
    fields to ttl=1h. Anthropic enforces ordering: processing order is
    tools → system → messages, and a ttl='1h' block cannot come after a
    ttl='5m' block. Since we place ttl=1h on messages (which is processed
    last), everything that comes before it (system, tools) must also be 1h
    — otherwise count_tokens and /v1/messages both return 400.

    Mutates `body` in place. If the last message has a string content,
    converts it to block form so we can attach the marker.
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

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    # Strip ALL existing cache_control on message content blocks so we don't
    # accumulate stale markers from prior Claude Code insertions.
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and "cache_control" in b:
                del b["cache_control"]

    # Insert one new marker on the last cacheable block of the last message.
    last_msg = messages[-1]
    if not isinstance(last_msg, dict):
        return
    content = last_msg.get("content")

    # String content → convert to block form so we can attach cache_control.
    if isinstance(content, str):
        if content.strip():
            last_msg["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ]
        return

    if not isinstance(content, list) or not content:
        return

    for b in reversed(content):
        if _is_cacheable_block(b):
            b["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            return


def _estimate_group_tokens(group: list) -> int:
    """Cheap chars/4 estimate of tokens in a turn group. Used only for
    rounding the midpoint target to the nearest group boundary — the real
    token count comes from count_tokens_via_anthropic.
    """
    total_chars = 0
    for m in group:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    total_chars += len(b.get("text") or "")
                elif btype == "tool_use":
                    total_chars += len(json.dumps(b.get("input") or {}, ensure_ascii=False))
                elif btype == "tool_result":
                    tc = b.get("content")
                    if isinstance(tc, str):
                        total_chars += len(tc)
                    elif isinstance(tc, list):
                        for sub in tc:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                total_chars += len(sub.get("text") or "")
                elif btype == "thinking":
                    total_chars += len(b.get("thinking") or "")
    return max(1, total_chars // 4)


async def trim_to_sliding_window(
    body_bytes: bytes,
    model_cap_tokens: int,
    shift_trigger_tokens: int,
    keep_target_tokens: int,
    request_headers: dict,
    prior_anchor: int = 0,
) -> tuple[bytes, dict | None]:
    """Slice the kept window down to keep_target_tokens when over trigger.

    Semantics (post-2026-05-22 revision):
      - Trigger: count_tokens > shift_trigger_tokens fires a shift.
      - Slice:   one pass newest → oldest over turn groups, keeping groups
                 whose cumulative _estimate_group_tokens stays under
                 keep_target_tokens. Stops at the first overshoot. Always
                 keeps at least the latest group, even if it alone exceeds
                 the target.
      - Safety:  _estimate_group_tokens is chars/4 — empirically under-counts
                 real tokens by up to ~2.8x on code-dense bodies. After the
                 estimate-based slice, do ONE count_tokens re-check. If the
                 real count is still over model_cap_tokens, advance the anchor
                 one group at a time until under or only the latest group
                 remains. This is the cheap insurance the chars/4 estimator
                 needs to avoid 400s.

    Between shifts the anchor doesn't move, so the byte prefix stays stable
    and Anthropic's prompt cache hits at ~99%. Each shift is one byte-prefix
    break that re-establishes the cache snapshot at the target size.

    Returns (final_bytes, info). info is None if nothing changed (no prior
    anchor, body under trigger, no cache_control rewrite needed). Otherwise
    info carries diagnostics including the new anchor and whether a shift fired.
    """
    vlog(f"  → sliding-window check, body={len(body_bytes):,}B, prior_anchor={prior_anchor}")
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
    total_groups = len(groups)
    if total_groups == 0:
        return body_bytes, None
    original_size = len(body_bytes)

    # Clamp prior_anchor to a valid range. Never drop everything — always
    # keep at least 1 group so the request remains structurally valid.
    anchor = max(0, min(prior_anchor, total_groups - 1))

    def build_body_at_anchor(a: int) -> bytes:
        kept_groups = groups[a:]
        kept_msgs = [m for g in kept_groups for m in g]
        body["messages"] = kept_msgs
        rewrite_cache_control_markers(body)
        return json.dumps(body).encode("utf-8")

    # Build the body at the current (prior) anchor and measure real tokens.
    trial_bytes = build_body_at_anchor(anchor)
    initial_tokens = await count_tokens_via_anthropic(trial_bytes, request_headers)
    if initial_tokens is None:
        log("  ! count_tokens returned None — passing through")
        return body_bytes, None

    anchor_before = anchor
    tokens_before = initial_tokens
    shift_fired = False

    if initial_tokens > shift_trigger_tokens:
        # Trigger fired. Single-pass slice toward keep_target_tokens: walk
        # groups newest → oldest, keep groups whose cumulative estimated
        # size stays under the target, stop at the first overshoot. Always
        # keep at least the latest group.
        kept_count = 0
        kept_tokens = 0
        for g in reversed(groups):  # newest first
            gsize = _estimate_group_tokens(g)
            if kept_count > 0 and kept_tokens + gsize > keep_target_tokens:
                break  # would overshoot, stop
            kept_count += 1
            kept_tokens += gsize

        # Translate "kept_count groups from the tail" back into an anchor
        # integer so the per-session watermark logic continues working.
        new_anchor = total_groups - kept_count
        # Anchor must advance monotonically (>= prior anchor) and never
        # drop the last group.
        new_anchor = min(max(new_anchor, anchor), total_groups - 1)

        if new_anchor > anchor:
            anchor = new_anchor
            trial_bytes = build_body_at_anchor(anchor)
            new_tokens = await count_tokens_via_anthropic(trial_bytes, request_headers)
            if new_tokens is not None:
                initial_tokens = new_tokens
            shift_fired = True

        # Safety re-check — runs WHENEVER count_tokens reports over the
        # KEEP_TARGET, not just when the estimate-based slice advanced the
        # anchor. The chars/4 estimator under-counts real tokens (~2.5–2.8x
        # on code-dense STT2 sessions); without this loop the slice often
        # lands near MODEL_CAP, not near KEEP_TARGET.
        #
        # Convergence: each iteration measures avg tokens per remaining
        # group from the actual count, then jumps proportionally toward the
        # target. Usually converges in 1–3 iterations even when starting
        # 2.5x over target. Falls back to a single-group step when within
        # one group's worth of the target. Always bounded by total_groups.
        safety_steps = 0
        while (
            initial_tokens is not None
            and initial_tokens > keep_target_tokens
            and anchor < total_groups - 1
        ):
            remaining_groups = total_groups - anchor
            if remaining_groups <= 1:
                break
            # Proportional jump: avg tokens per kept group × overshoot
            avg_tok_per_group = initial_tokens / remaining_groups
            overshoot = initial_tokens - keep_target_tokens
            groups_to_drop = max(1, int(overshoot / max(avg_tok_per_group, 1)))
            # Never drop the last group; advance at least 1.
            groups_to_drop = min(groups_to_drop, total_groups - 1 - anchor)
            anchor += groups_to_drop
            safety_steps += 1
            trial_bytes = build_body_at_anchor(anchor)
            checked = await count_tokens_via_anthropic(trial_bytes, request_headers)
            if checked is None:
                break
            initial_tokens = checked
            shift_fired = True  # safety advance counts as a shift
        if safety_steps:
            log(
                f"  [safety re-shift] anchor -> {anchor}, "
                f"tokens -> {initial_tokens:,}  ({safety_steps} extra step(s))"
            )

        if shift_fired:
            # Edge cases after safety loop exits:
            #   - over MODEL_CAP: single retained group exceeds the cap.
            #     Pathological; log loudly and let Anthropic 400.
            #   - over KEEP_TARGET but under MODEL_CAP: anchor maxed out
            #     before reaching the target. Safe to send (no 400 risk)
            #     but didn't hit the trim aspiration. Note quietly.
            if initial_tokens is not None and initial_tokens > model_cap_tokens:
                log(
                    f"  [WARN] post-slice still over MODEL_CAP "
                    f"({initial_tokens:,} tok) — single group too large, "
                    f"sending anyway"
                )

    final_bytes = trial_bytes
    kept_count = total_groups - anchor
    dropped_count = anchor

    kept_preview = _group_preview(groups[anchor]) if anchor < total_groups else "<empty>"
    first_dropped_preview = _group_preview(groups[0]) if dropped_count > 0 else None
    last_dropped_preview = _group_preview(groups[anchor - 1]) if anchor > 0 else None

    return final_bytes, {
        "original_size": original_size,
        "final_size": len(final_bytes),
        "original_groups": total_groups,
        "kept_groups": kept_count,
        "dropped_groups": dropped_count,
        "original_messages": len(original_messages),
        "kept_messages": sum(len(g) for g in groups[anchor:]),
        "anchor_before": anchor_before,
        "anchor_after": anchor,
        "shift_fired": shift_fired,
        "tokens_before": tokens_before,
        "tokens_after": initial_tokens,
        "first_kept_preview": kept_preview,
        "first_dropped_preview": first_dropped_preview,
        "last_dropped_preview": last_dropped_preview,
    }


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(request: Request, full_path: str):
    global _count_tokens_calls, _count_tokens_elapsed_ms
    _count_tokens_calls = 0
    _count_tokens_elapsed_ms = 0.0
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

    # CC 2.1.145 bug: sends model="claude-opus-4-7[1m]" as a literal id instead
    # of using anthropic-beta: context-1m-2025-08-07. Anthropic returns 404,
    # which CC misinterprets as "Context limit reached" on the FIRST prompt.
    # Rewrite the body's model to the bare id and add the 1m beta header.
    _did_1m_rewrite = False
    if body:
        try:
            _body_obj = json.loads(body.decode("utf-8"))
            if isinstance(_body_obj, dict) and _normalize_cc_1m_suffix(_body_obj, fwd_headers):
                body = json.dumps(_body_obj).encode("utf-8")
                body_size = len(body)
                _did_1m_rewrite = True
                log(
                    f"🔧 rewrote model=claude-opus-4-7[1m] → claude-opus-4-7 + "
                    f"anthropic-beta: context-1m-2025-08-07"
                )
        except Exception:
            pass

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
    #   - Slow path: apply per-session sliding window anchor (front-drop),
    #     then halve-on-threshold if count_tokens reports over SHIFT_THRESHOLD.
    # Skip count_tokens itself to avoid recursion (we call it directly below).
    is_messages = (
        full_path.startswith("v1/messages")
        and not full_path.startswith("v1/messages/count_tokens")
    )

    trim_elapsed_ms = 0.0
    # Real token count we'll forward — populated by slow path from
    # count_tokens, or estimated from body size for fast path. Used by
    # _publish_last_send for the statusline.
    actual_tokens_sent: int | None = None
    if is_messages and body_size >= FAST_PATH_THRESHOLD:
        # One-shot probe: capture the next slow-path /v1/messages body for
        # offline inspection of cache_control usage. Fires once per process.
        # Off by default; set CLAUDE_PROXY_PROBE_DUMP=1 to re-enable.
        if not _probe_fired and os.environ.get("CLAUDE_PROXY_PROBE_DUMP"):
            _probe_dump_once(body)
        # Look up this session's sliding-window anchor (if any) before trimming.
        # Session key = hash of first real user message text. Stable until
        # /compact or /clear, at which point a new key is correct.
        session_key: str | None = None
        prior_anchor = 0
        try:
            body_obj = json.loads(body.decode("utf-8"))
            if isinstance(body_obj, dict) and isinstance(body_obj.get("messages"), list):
                session_key = session_key_for_messages(body_obj["messages"])
        except Exception:
            session_key = None
        if session_key is not None and session_key in _session_watermarks:
            prior_anchor = _session_watermarks[session_key]
            _session_watermarks.move_to_end(session_key)  # LRU touch

        trim_t0 = time.monotonic()
        trimmed, info = await trim_to_sliding_window(
            body,
            MODEL_CAP_TOKENS,
            SHIFT_TRIGGER,
            KEEP_TARGET,
            fwd_headers,
            prior_anchor=prior_anchor,
        )
        trim_elapsed_ms = (time.monotonic() - trim_t0) * 1000
        if info is not None:
            body = trimmed
            body_size = len(body)
            actual_tokens_sent = info.get("tokens_after")
            # Advance (or initialize) the session anchor. LRU evict if needed.
            if session_key is not None:
                _session_watermarks[session_key] = info["anchor_after"]
                _session_watermarks.move_to_end(session_key)
                while len(_session_watermarks) > _MAX_SESSIONS:
                    _session_watermarks.popitem(last=False)
            shift_tag = " [SHIFT]" if info["shift_fired"] else ""
            log(
                f"✂ sliding-window{shift_tag}: "
                f"{info['tokens_before']:,}tok → {info['tokens_after']:,}tok  "
                f"({info['original_size']:,}B → {info['final_size']:,}B)  "
                f"anchor {info['anchor_before']}→{info['anchor_after']}  "
                f"groups {info['original_groups']}→{info['kept_groups']} "
                f"(dropped {info['dropped_groups']})  "
                f"msgs {info['original_messages']}→{info['kept_messages']}"
            )
            if info.get("first_dropped_preview") is not None:
                log(
                    f"  dropped: [0] \"{info['first_dropped_preview']}\" … "
                    f"[{info['anchor_after'] - 1}] \"{info['last_dropped_preview']}\""
                )
            log(f"  first kept: [{info['anchor_after']}] \"{info['first_kept_preview']}\"")

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

    upstream_send_ms = (time.monotonic() - started) * 1000
    vlog(f"← {upstream_resp.status_code}  in {upstream_send_ms:.0f}ms  (streaming...)")

    # [TX] Publish the real forwarded-token count for statusline.sh.
    # Only on successful /v1/messages forwards (not count_tokens diagnostics).
    # Per-session: pull session id from CC's header (case-insensitive).
    if is_messages and 200 <= upstream_resp.status_code < 300:
        if actual_tokens_sent is None:
            # Fast path or slow-path no-op: use crude bytes/4 estimate.
            # Fast path bodies are always small, so this is fine for the bar.
            actual_tokens_sent = body_size // 4
        _cc_session_id: str | None = None
        for _k, _v in request.headers.items():
            if _k.lower() == "x-claude-code-session-id":
                _cc_session_id = _v
                break
        _publish_last_send(actual_tokens_sent, _cc_session_id)

    # On error (4xx/5xx), buffer the body so we can log it, then relay.
    # This helps diagnose why Anthropic rejected (token cap, byte cap, etc).
    async def relay():
        first_byte_t = None
        # 진단용: SSE 스트림 전체를 버퍼에 모아서 usage 추출 (cache_read 등)
        diag_buffer: list[bytes] = []
        try:
            if upstream_resp.status_code >= 400:
                # Read whole body, log it, then replay to client.
                chunks = []
                async for chunk in upstream_resp.aiter_bytes():
                    if first_byte_t is None:
                        first_byte_t = time.monotonic()
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
                try:
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    log(f"✗ {upstream_resp.status_code} error body: {body_text[:500]}")
                except Exception:
                    log(f"✗ {upstream_resp.status_code} error body (undecodable): {len(body_bytes)}B")
                # Forensic dumps. Two distinct failure modes get separate dump
                # files so we can correlate without crossing wires:
                #   - proxy_1m_dump.json     → for [1m] rewrites (Bug A: quota
                #                              check rejected by Anthropic)
                #   - proxy_over_cap_dump.json → for 400 "prompt is too long"
                #                              (Bug B: sliding-window trim
                #                              undershot the hard cap)
                # Each overwrites on every fire — we only need the latest
                # occurrence to do a post-mortem.
                def _write_dump(filename: str, label: str) -> None:
                    try:
                        dump = {
                            "ts": time.time(),
                            "ts_kst": _ts() + " KST",
                            "request": {
                                "method": request.method,
                                "url": f"{UPSTREAM}{url}",
                                "headers": {k: ("<redacted>" if k.lower() == "authorization" else v)
                                            for k, v in fwd_headers.items()},
                                "body": body.decode("utf-8", errors="replace"),
                                "body_size": len(body),
                            },
                            "response": {
                                "status": upstream_resp.status_code,
                                "headers": dict(upstream_resp.headers),
                                "body": body_text if 'body_text' in dir() else "",
                                "body_len": len(body_bytes),
                            },
                        }
                        dump_path = str(_SCRIPT_DIR / filename)
                        with open(dump_path, "w", encoding="utf-8") as df:
                            json.dump(dump, df, indent=2, ensure_ascii=False)
                        log(f"📦 dumped {label} forensics to {dump_path}")
                    except Exception as e:
                        log(f"⚠ {label} dump failed: {e}")

                if _did_1m_rewrite:
                    _write_dump("proxy_1m_dump.json", "[1m] probe")

                # Over-cap 400: Anthropic rejected because the trimmed body
                # was still over the 1M token cap. With single-pass slice-
                # to-target this should only happen when a single retained
                # turn group is itself bigger than the cap (we already log
                # a ⚠ warning in that case). Either way, dump forensics.
                if (
                    upstream_resp.status_code == 400
                    and isinstance(body_text, str)
                    and "prompt is too long" in body_text
                ):
                    _write_dump("proxy_over_cap_dump.json", "over-cap 400")
                yield body_bytes
            else:
                async for chunk in upstream_resp.aiter_bytes():
                    if first_byte_t is None:
                        first_byte_t = time.monotonic()
                    diag_buffer.append(chunk)
                    yield chunk
        finally:
            await upstream_resp.aclose()
            # 진단 로그: 모든 timing + cache usage 한 줄로
            now = time.monotonic()
            total_ms = (now - started) * 1000
            ttfb_ms = ((first_byte_t - started) * 1000) if first_byte_t else -1
            stream_ms = ((now - first_byte_t) * 1000) if first_byte_t else 0
            usage_str = extract_usage_from_sse(b"".join(diag_buffer)) if diag_buffer else "<error>"
            log(
                f"⏱ total={total_ms:.0f}ms "
                f"trim={trim_elapsed_ms:.0f}ms "
                f"(ct={_count_tokens_calls}×{_count_tokens_elapsed_ms:.0f}ms) "
                f"send={upstream_send_ms:.0f}ms "
                f"ttfb={ttfb_ms:.0f}ms "
                f"stream={stream_ms:.0f}ms "
                f"| {usage_str}"
            )

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
    log(
        f"sliding window: model_cap={MODEL_CAP_TOKENS:,} tok  "
        f"trigger={SHIFT_TRIGGER:,} tok  "
        f"keep_target={KEEP_TARGET:,} tok"
    )
    log(f"mode:         {'quiet' if QUIET else 'verbose'}")
    log("set ANTHROPIC_BASE_URL=http://localhost:9999 in your client")
    uvicorn.run(
        app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="warning",  # silence uvicorn's own access log — we have our own
        access_log=False,
    )
