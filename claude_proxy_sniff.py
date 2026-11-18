"""Raw-request sniffer — capture EXACTLY what Claude Code sends, unrefined.

Listens on :9998 (separate from the real proxy on :9999, touches nothing).
Dumps EVERY request CC sends — full headers (auth value redacted only, for
safety) + the complete body, verbatim — to sniff-dumps/, then forwards
transparently to Anthropic so the throwaway session still works.

No filtering, no summarizing, no "readable" trimming: capture raw first,
analyze after, refine the presentation only once we've seen the real thing.

Usage:
    .venv/Scripts/python.exe claude_proxy_sniff.py        # :9998
    # in a SEPARATE throwaway terminal (use a FRESH chat):
    #   $env:ANTHROPIC_BASE_URL="http://localhost:9998"; claude
    #   ...type "hi", wait for the reply...
"""
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn

UPSTREAM = "https://api.anthropic.com"
DUMP_DIR = Path(__file__).resolve().parent / "sniff-dumps"
DUMP_DIR.mkdir(exist_ok=True)
_n = 0
_MAX = 16  # backstop so a big session can't write hundreds of files

client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(None), http2=False)
app = FastAPI()

STRIP_REQ = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
STRIP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection"}
REDACT = {"authorization", "x-api-key"}  # redact the VALUE only — a secret, not API info


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def relay(request: Request, path: str):
    global _n
    body = await request.body()

    if _n < _MAX and body:
        _n += 1
        headers = {k: ("<redacted>" if k.lower() in REDACT else v)
                   for k, v in request.headers.items()}
        raw = body.decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        rec = {
            "n": _n,
            "method": request.method,
            "path": "/" + path,
            "query": request.url.query,
            "headers": headers,            # ALL headers, verbatim (auth value redacted)
            "body_bytes": len(body),
            "body": parsed if parsed is not None else raw,   # complete body, untouched
        }
        safe = (path.replace("/", "_") or "root")[:40]
        fp = DUMP_DIR / f"{_n:02d}-{request.method}-{safe}.json"
        fp.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        mdl = parsed.get("model") if isinstance(parsed, dict) else None
        beta = next((v for k, v in request.headers.items() if k.lower() == "anthropic-beta"), None)
        print(f"[sniff] #{_n} {request.method} /{path} ({len(body)}B) "
              f"model={mdl!r} anthropic-beta={beta!r} -> {fp.name}", flush=True)

    fwd = {k: v for k, v in request.headers.items() if k.lower() not in STRIP_REQ}
    fwd["accept-encoding"] = "identity"
    url = "/" + path + (f"?{request.url.query}" if request.url.query else "")
    upstream = client.build_request(request.method, url, content=body, headers=fwd)
    resp = await client.send(upstream, stream=True)

    async def gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in STRIP_RESP}
    return StreamingResponse(gen(), status_code=resp.status_code,
                             headers=out_headers, media_type=resp.headers.get("content-type"))


if __name__ == "__main__":
    print(f"[sniff] :9998 -> {UPSTREAM}  |  dumping raw requests to {DUMP_DIR}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=9998, log_level="warning", access_log=False)
