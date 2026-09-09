# Code Review: slack-cli improvements (commit `4bc0052`, PR #1)

Local subagent review of `slack-cli/slack-cli.js` (post-change, 481 lines).
Compared against cloud `/ultrareview` run on the same PR (17 deduped findings).

Line numbers refer to the post-change file.

---

## Critical / High

### H1. Path traversal via Slack-supplied `f.name` in `download`
**File:** `slack-cli/slack-cli.js:416`

```js
const outPath = path.join(outDir, f.name);
```

`f.name` comes straight from Slack and the CLI runs as a privileged user (the PR description says `xoxp-` user token on a chart-server box). A workspace member who can upload a file named `../../etc/cron.d/whatever` (Slack does not enforce filename charset) gets that file written wherever the CLI's cwd permissions allow. `path.join("./", "../../foo")` yields `"../../foo"` — there is no normalization.

**Fix:**
```js
const safeName = path.basename(f.name).replace(/[^\w.\-]/g, "_") || `file_${f.id}`;
const outPath = path.join(path.resolve(outDir), safeName);
if (!outPath.startsWith(path.resolve(outDir) + path.sep)) {
  throw new Error("unsafe filename");
}
```

### H2. Path traversal in `fetch` despite "safe name" prefix
**File:** `slack-cli/slack-cli.js:440`

```js
const safeName = `${f.id}_${(f.name || "file").replace(/\s+/g, "_")}`;
```

`replace(/\s+/g, "_")` only collapses whitespace. It does **not** strip `/`, `\`, `..`, or NUL. The `Fxxx_` prefix does *not* save you — the traversal segments come *after* it: `path.join` will happily resolve `Fxxx_../../etc/passwd` because `../` is interpreted segment-by-segment after a slash inside the string.

**Fix:**
```js
const baseName = path.basename(f.name || "file").replace(/[^\w.\-]/g, "_");
const safeName = `${f.id}_${baseName}`;
```

### H3. `downloadFile` has no timeout — hung connection blocks indefinitely
**File:** `slack-cli/slack-cli.js:83-96`

The `https.get`/`http.get` call sets no `timeout` option and never calls `req.setTimeout()`. A stalled Slack edge node or dead TCP connection wedges the process forever.

**Fix:**
```js
const req = (u) => {
  const r = get(u, { headers: {...}, timeout: 30000 }, (res) => { ... });
  r.on("timeout", () => { r.destroy(new Error("download timeout")); });
  return r;
};
```

### H4. `downloadFile` truncated-download bug: no `res.on("end")` validation, no Content-Length check
**File:** `slack-cli/slack-cli.js:89-92`

If the upstream connection drops mid-body (TCP RST, server closes early, proxy hiccup), `res` emits `end`/`close` and `pipe` flushes whatever it received to disk. `ws` then fires `finish` and the function resolves — silently producing a truncated file. There's no `res.on("error")` handler and no comparison against `Content-Length`.

**Fix:**
```js
let received = 0;
const expected = parseInt(res.headers["content-length"] || "0", 10);
res.on("data", (chunk) => { received += chunk.length; });
res.on("error", reject);
res.on("aborted", () => reject(new Error("connection aborted")));
ws.on("finish", () => {
  ws.close();
  if (expected && received !== expected) {
    fs.unlinkSync(outPath);
    return reject(new Error(`truncated: ${received}/${expected}`));
  }
  resolve();
});
```

### H5. `downloadFile` redirect loop has no bound — infinite redirect possible
**File:** `slack-cli/slack-cli.js:86-87`

A server that returns 301→A, A→B, B→A loops forever (and stack-grows on every recursion).

**Fix:**
```js
const req = (u, depth = 0) => {
  if (depth > 5) return reject(new Error("too many redirects"));
  ...
  if ([301, 302, 303, 307, 308].includes(res.statusCode)) return req(res.headers.location, depth + 1);
```

### H6. `downloadFile` redirect across http↔https uses wrong client; token leaks on cross-host redirect
**File:** `slack-cli/slack-cli.js:84, 86`

Two bugs in one:
1. `get` is bound from the *initial* URL's protocol. If the redirect target is a different protocol, you'll call the wrong module — `http.get` against an `https://` URL throws or hangs.
2. The redirect re-sends `Authorization: Bearer <SLACK_TOKEN>` to whatever host `Location:` points at. Standard hardening is to drop `Authorization` on cross-origin redirects.

**Fix:**
```js
const req = (u, depth = 0) => {
  const parsed = new URL(u);
  const client = parsed.protocol === "https:" ? https : http;
  const isSlackHost = /\.slack\.com$|^slack\.com$|\.slack-edge\.com$|\.slack-files\.com$/.test(parsed.hostname);
  const headers = isSlackHost ? { Authorization: `Bearer ${token}` } : {};
  return client.get(u, { headers, timeout: 30000 }, ...);
};
```

### H7. `downloadFile` has no size cap — disk-fill DoS
**File:** `slack-cli/slack-cli.js:83-96`

PR description notes the box is at 83% disk. A 10 GB Slack upload downloaded by `fetch` against a busy channel will saturate disk. There's no `Content-Length` ceiling and no rolling byte-count abort.

**Fix:**
```js
const MAX_BYTES = 500 * 1024 * 1024;
res.on("data", (chunk) => {
  received += chunk.length;
  if (received > MAX_BYTES) { req.destroy(); ws.destroy(); fs.unlink(outPath, () => {}); reject(new Error("file too large")); }
});
```

### H8. Error handler `req(url).on("error", reject)` only attached to first request; redirected requests have no `error` handler
**File:** `slack-cli/slack-cli.js:94`

When a redirect calls `req(res.headers.location)` recursively, the returned `ClientRequest` is **not** assigned to anything and has no `.on("error", ...)` attached. A network error during the redirected fetch becomes an unhandled `error` event, which crashes the Node process.

**Fix:**
```js
const req = (u, depth = 0) => {
  const r = get(u, {...}, (res) => { ... });
  r.on("error", reject);
  return r;
};
```

---

## Medium

### M1. `attachmentFallback` silently drops attachments 2..N
**File:** `slack-cli/slack-cli.js:53-58`

Two layers of data loss: only `attachments[0]` is examined, and only the *first* of `[title, text, fallback]` is rendered. A message with a link unfurl + an inline error block would show only the first unfurl's title.

**Fix:**
```js
function attachmentFallback(msg) {
  if (!msg.attachments?.length) return "";
  return msg.attachments
    .map(a => [a.title, a.text, a.fallback].find(Boolean))
    .filter(Boolean)
    .map(s => `[attachment] ${s}`)
    .join("\n    ");
}
```

### M2. `dms` filters on `c.is_user_deleted` — field may not exist on `conversations.list` response
**File:** `slack-cli/slack-cli.js:318`

```js
const ims = (result.channels || []).filter(c => !c.is_user_deleted);
```

Slack's documented field on a conversation object representing a DM with a deactivated user is **`is_user_deleted`** *or* you have to dereference `c.user` and look up the user. In practice this filter may be a no-op against modern API responses.

**Needs verification** — confirm with `slack dms` against a workspace with a deactivated user; if filter is no-op, drop it and use `users.info(c.user).deleted` (with the cache built at lines 320-326).

### M3. `resolveChannel` regex `/^[CDG][A-Z0-9]{8,}$/` may miss valid IDs
**File:** `slack-cli/slack-cli.js:103`

Enterprise Grid uses `W` prefix for workspace-level user IDs and Workflow channels can have other shapes. **Needs verification** with the actual workspace this runs against.

### M4. `pruneOldFetches` swallows `readdirSync` errors
**File:** `slack-cli/slack-cli.js:69-81`

`fs.readdirSync(FETCH_DIR)` is **outside** the try/catch. The bigger problem: per-entry `try { ... } catch {}` silently masks all errors — a dir with broken perms means stale data accumulates forever and the operator never knows.

**Fix:**
```js
function pruneOldFetches() {
  let entries;
  try { entries = fs.readdirSync(FETCH_DIR); }
  catch (e) {
    if (e.code !== "ENOENT") console.error(`pruneOldFetches: ${e.message}`);
    return;
  }
  for (const entry of entries) {
    try { ... } catch (e) { console.error(`pruneOldFetches: ${entry}: ${e.message}`); }
  }
}
```

### M5. `resolveChannel` paginates on every invocation — O(workspace size) per call
**File:** `slack-cli/slack-cli.js:108-117, 124-136`

Every `slack history #foo` walks `conversations.list` from the beginning, paging in chunks of 1000. For a large workspace this is several round-trips and consumes Tier-2 rate-limit budget.

**Fix (low-effort):** memoize within the process; for proper fix, write resolved IDs to `~/.cache/slack-cli/channels.json` with TTL.

### M6. `upload`: `--title` overrides filename not just title — wrong field overloaded
**File:** `slack-cli/slack-cli.js:278, 283`

```js
const fileName = opts.title || path.basename(filePath);
const uploadParams = {
  ...
  filename: fileName,
  title: fileName,
};
```

Operator running `slack upload C0xx ./report-2026-04.csv --title "Q1 Earnings Report"` ends up with `filename="Q1 Earnings Report"` (no extension!).

**Fix:**
```js
const fileName = path.basename(filePath);
const fileTitle = opts.title || fileName;
const uploadParams = { channel_id: channel, file: fileContent, filename: fileName, title: fileTitle };
```

### M7. `channels` and `users` commands have no pagination — silently truncate large workspaces
**File:** `slack-cli/slack-cli.js:302-310, 337-345`

Workspace with 500 channels and `--limit 100` (or default 100) silently shows only the first page — `next_cursor` is ignored. No `... (more available)` indicator either.

**Fix:** loop on `response_metadata.next_cursor` until exhausted, or print `... (N more, use --limit M)` when `next_cursor` is set.

### M8. `formatTs` change: `created` field in `fileinfo` JSON output flipped from ISO 8601 to locale string
**File:** `slack-cli/slack-cli.js:393`

Previously `new Date(f.created * 1000).toISOString()` produced `2026-04-27T12:34:56.000Z`. Now it produces `"2026-04-27 21:34:56 KST"`. Any downstream JSON consumer that did `dateutil.parse(j.created)` will now succeed-but-misparse on the appended ` KST` literal.

**Fix:** keep `created` as ISO 8601 in JSON output, add a separate `created_kst` field, or only apply `formatTs` to human-readable list output.

### M9. `Promise.all` over `userIds.map(client.users.info)` is unbounded fan-out
**File:** `slack-cli/slack-cli.js:321-326`

A workspace with 500 active DMs fires 500 concurrent `users.info` calls. Slack rate limits Tier-4 at ~100/min — this will trigger 429s, and the per-call `catch { userMap[uid] = uid }` swallows them all silently.

**Fix:** chunk into batches of ~5 with `Promise.all`, or use `users.list` (already paginated for free) and build the map up front.

### M10. `users` command silent truncation
**File:** `slack-cli/slack-cli.js:337-345`

Variant of M7. The `users` command silently truncating users beyond the first 100 has higher operational impact than channels truncation.

### M11. `mkdtempSync` race: parent `FETCH_DIR` permissions
**File:** `slack-cli/slack-cli.js:65-67, 431`

TOCTOU: between `existsSync` and `mkdirSync`, another process can create `/tmp/slack-fetch` with mode 0777. On a multi-user box, another local user can pre-create `/tmp/slack-fetch` mode 0777, then watch `mkdtempSync` produce `fetch-XXXXXX` directories, read the bearer-token-authenticated downloaded files, or replace files with symlinks.

**Fix:**
```js
function ensureFetchDir() {
  fs.mkdirSync(FETCH_DIR, { recursive: true, mode: 0o700 });
  const st = fs.statSync(FETCH_DIR);
  if (st.uid !== process.getuid() || (st.mode & 0o077) !== 0) {
    throw new Error(`unsafe ${FETCH_DIR}: owner=${st.uid} mode=${(st.mode & 0o777).toString(8)}`);
  }
}
```

### M12. `download` writes through `fs.createWriteStream(outPath)` with no `O_EXCL` — symlink-attack vector
**File:** `slack-cli/slack-cli.js:89, 416`

If `outDir` is `/tmp` or even cwd on a shared box, an attacker who can pre-create `/tmp/<expected-filename>` as a symlink to `/etc/cron.d/somefile` causes `createWriteStream` to follow the symlink and overwrite the target (Node uses `O_TRUNC|O_WRONLY|O_CREAT`, no `O_EXCL`, no `O_NOFOLLOW`). Combined with H1, this is a privilege-escalation primitive on multi-user boxes.

**Fix:**
```js
const ws = fs.createWriteStream(outPath, { flags: "wx" });  // wx = O_CREAT | O_EXCL
```

---

## Low / Nit

### L1. `cleanup` removes the directory but doesn't recreate
**File:** `slack-cli/slack-cli.js:460-468`

Cosmetic; not a bug. Could log "next fetch will recreate".

### L2. `formatTs` returns `"Invalid Date KST"` for non-numeric `ts`
**File:** `slack-cli/slack-cli.js:46-51`

If `ts` is a non-numeric string, output becomes literally `"Invalid Date KST"`. Add `if (Number.isNaN(d.getTime())) return String(ts);`.

### L3. `parseArgs` does not handle `--flag` (boolean) or `--key=value` form
**File:** `slack-cli/slack-cli.js:145-159`

`--limit 50` works; `--limit=50` does not. Pure boolean flags consume the next positional as their value (`--reverse --limit 5` → `opts.reverse = "--limit"`, broken).

### L4. `download` directory creation does not respect `mode` — uses default umask
**File:** `slack-cli/slack-cli.js:415`

Downloaded Slack files may contain sensitive data and the dir gets default mode (0755 typically). Less severe than M11 because it's an operator-chosen path, but consistency with `ensureFetchDir`'s `0o700` would be safer.

### L5. `fetch` does not surface partial-failure exit code
**File:** `slack-cli/slack-cli.js:447-449, 452-456`

If 5 of 7 files download successfully and 2 fail, the process exits 0 because `count > 0`. Scripts that wrap `slack fetch` and check `$?` won't notice partial failures.

### L6. `console.error(JSON.stringify(err.data, null, 2))` could leak sensitive context
**File:** `slack-cli/slack-cli.js:476`

**Needs verification** — audit `@slack/web-api`'s error shape to confirm `err.data` never includes `Authorization` headers or `?token=` query strings before treating this as safe to dump in CI logs.

### L7. `fs.readFileSync(filePath)` in `upload` loads entire file into memory
**File:** `slack-cli/slack-cli.js:277`

A 1 GB upload uses 1 GB of RSS plus Node's V8 buffer overhead. `filesUploadV2` accepts streams.

### L8. `userinfo` accesses `u.profile.display_name` without guarding `u.profile`
**File:** `slack-cli/slack-cli.js:357`

If a deactivated/stripped user lacks a `profile` object, this throws `TypeError`. Use `u.profile?.display_name`.

---

## Summary

| Severity | Count |
|---|---|
| Critical / High | 8 (H1–H8) |
| Medium | 12 (M1–M12) |
| Low / Nit | 8 (L1–L8) |
| **Total** | **28** |

Items needing verification before action: M2, M3, L6.

## Suggested fix order

1. **H1 + H2** (path traversal — both files)
2. **M12** (symlink/`O_EXCL`)
3. **H6** (token leak on redirect)
4. **H3 + H4 + H5 + H7 + H8** (`downloadFile` robustness — single PR)
5. **M6** (upload filename/title separation)
6. **M8** (ISO 8601 timestamp regression in JSON output)
7. **M11** (FETCH_DIR TOCTOU)
8. **M9** (Promise.all fan-out)
9. **M7 + M10** (pagination)
10. **M1, M4, M5** (data-loss / observability)
11. **M2, M3, L6** — verify first
12. **L1–L8** as polish
