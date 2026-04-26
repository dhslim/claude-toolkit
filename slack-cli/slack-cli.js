#!/usr/bin/env node
// Slack CLI - lightweight wrapper around @slack/web-api
// Usage: node slack-cli.js <command> [options]
//
// Commands:
//   history <channel> [--limit N] [--oldest TS] [--latest TS]
//   message <channel> <timestamp>
//   thread <channel> <timestamp> [--limit N]
//   send <channel> <text> [--thread_ts TS]
//   channels [--limit N]
//   dms [--limit N]
//   users [--limit N]
//   userinfo <user_id>
//   files <channel> [--limit N] [--types images]
//   fileinfo <file_id>
//   download <file_id> [--out DIR]               (persistent — defaults to cwd)
//   fetch <channel> [--limit N]                  (ephemeral — for loading context; auto-cleans /tmp/slack-fetch after 5m TTL)
//   upload <channel> <file_path> [--title T] [--thread_ts TS] [--comment C]
//   delete <channel> <timestamp>
//   cleanup                                       (purge /tmp/slack-fetch now)
//
// Channel arg accepts: raw ID (Cxxx/Dxxx/Gxxx), #channel-name, @username
// Timestamps are displayed in KST (Asia/Seoul). Pass raw `ts=` value to thread/message commands.
// Environment: SLACK_TOKEN must be set (bot or user token)

const { WebClient } = require("@slack/web-api");
const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");

const token = process.env.SLACK_TOKEN;
if (!token) {
  console.error("Error: SLACK_TOKEN environment variable is not set.");
  console.error("Set it with: export SLACK_TOKEN=xoxb-... or export SLACK_TOKEN=xoxp-...");
  process.exit(1);
}

const client = new WebClient(token);

// ---------------- helpers ----------------

const FETCH_DIR = "/tmp/slack-fetch";
const FETCH_TTL_MIN = 5;

function formatTs(ts) {
  if (!ts) return "?";
  const d = new Date(parseFloat(ts) * 1000);
  // sv-SE locale gives ISO-like "YYYY-MM-DD HH:MM:SS"
  return d.toLocaleString("sv-SE", { timeZone: "Asia/Seoul" }) + " KST";
}

function attachmentFallback(msg) {
  if (!msg.attachments || msg.attachments.length === 0) return "";
  const a = msg.attachments[0];
  const parts = [a.title, a.text, a.fallback].filter(Boolean);
  return parts.length ? `[attachment] ${parts[0]}` : "";
}

function fileBadge(msg) {
  if (!msg.files || msg.files.length === 0) return "";
  return ` 📎(${msg.files.length} file${msg.files.length > 1 ? "s" : ""})`;
}

function ensureFetchDir() {
  if (!fs.existsSync(FETCH_DIR)) fs.mkdirSync(FETCH_DIR, { recursive: true, mode: 0o700 });
}

function pruneOldFetches() {
  if (!fs.existsSync(FETCH_DIR)) return;
  const cutoff = Date.now() - FETCH_TTL_MIN * 60 * 1000;
  for (const entry of fs.readdirSync(FETCH_DIR)) {
    const p = path.join(FETCH_DIR, entry);
    try {
      const stat = fs.statSync(p);
      if (stat.isDirectory() && stat.mtimeMs < cutoff) {
        fs.rmSync(p, { recursive: true, force: true });
      }
    } catch {}
  }
}

async function downloadFile(url, outPath) {
  const get = url.startsWith("https") ? https.get : http.get;
  return new Promise((resolve, reject) => {
    const req = (u) => get(u, { headers: { Authorization: `Bearer ${token}` } }, (res) => {
      if ([301, 302].includes(res.statusCode)) return req(res.headers.location);
      if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode}`));
      const ws = fs.createWriteStream(outPath);
      res.pipe(ws);
      ws.on("finish", () => { ws.close(); resolve(); });
      ws.on("error", reject);
    });
    req(url).on("error", reject);
  });
}

// Resolve #channel-name, @username, or raw ID -> channel ID.
// Pagination handled (Slack default is 100, we use 1000 per page).
async function resolveChannel(input) {
  if (!input) return input;
  // Already an ID? Slack IDs: C=public, D=DM, G=private/mpim
  if (/^[CDG][A-Z0-9]{8,}$/.test(input)) return input;

  if (input.startsWith("#")) {
    const name = input.slice(1);
    let cursor;
    do {
      const r = await client.conversations.list({
        limit: 1000,
        types: "public_channel,private_channel",
        ...(cursor && { cursor }),
      });
      const found = (r.channels || []).find(c => c.name === name);
      if (found) return found.id;
      cursor = r.response_metadata?.next_cursor;
    } while (cursor);
    throw new Error(`channel not found: ${input}`);
  }

  if (input.startsWith("@")) {
    const name = input.slice(1);
    let cursor;
    do {
      const r = await client.users.list({ limit: 1000, ...(cursor && { cursor }) });
      const found = (r.members || []).find(u =>
        u.name === name ||
        u.profile?.display_name === name ||
        u.real_name === name
      );
      if (found) {
        const im = await client.conversations.open({ users: found.id });
        return im.channel.id;
      }
      cursor = r.response_metadata?.next_cursor;
    } while (cursor);
    throw new Error(`user not found: ${input}`);
  }

  return input; // assume it's something Slack accepts
}

// ---------------- arg parsing ----------------

function parseArgs(args) {
  const opts = {};
  const positional = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      const key = args[i].slice(2);
      const val = args[i + 1];
      opts[key] = val;
      i++;
    } else {
      positional.push(args[i]);
    }
  }
  return { positional, opts };
}

// ---------------- main ----------------

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.log(`Slack CLI - Commands:
  history <channel> [--limit N] [--oldest TS] [--latest TS]   Read channel history
  message <channel> <timestamp>                               Read a specific message
  thread  <channel> <timestamp> [--limit N]                   Read thread replies
  send    <channel> <text> [--thread_ts TS]                   Send a message
  channels [--limit N]                                        List channels
  dms      [--limit N]                                        List DM conversations
  users    [--limit N]                                        List users
  userinfo <user_id>                                          Get user info
  files    <channel> [--limit N] [--types images]             List files in channel
  fileinfo <file_id>                                          Get file details
  download <file_id> [--out DIR]                              Download a file (persistent — defaults to cwd)
  fetch    <channel> [--limit N]                              Download every attached file in recent messages (ephemeral, /tmp/slack-fetch, TTL 5m)
  upload   <channel> <file_path> [--title T] [--thread_ts TS] [--comment C]
  delete   <channel> <timestamp>
  cleanup                                                     Purge /tmp/slack-fetch now

Channel arg: raw ID (Cxxx/Dxxx) | #channel-name | @username
Timestamps: shown as KST (Asia/Seoul). Use raw "ts=" value for thread/message commands.

Environment: SLACK_TOKEN=xoxb-... or xoxp-...`);
    return;
  }

  const command = args[0];
  const { positional, opts } = parseArgs(args.slice(1));

  try {
    switch (command) {
      case "history": {
        const arg = positional[0];
        if (!arg) { console.error("Usage: history <channel> [--limit N]"); process.exit(1); }
        const channel = await resolveChannel(arg);
        const result = await client.conversations.history({
          channel,
          limit: parseInt(opts.limit || "20"),
          ...(opts.oldest && { oldest: opts.oldest }),
          ...(opts.latest && { latest: opts.latest }),
        });
        for (const msg of result.messages || []) {
          const ts = formatTs(msg.ts);
          const user = msg.user || msg.bot_id || msg.username || "unknown";
          let text = (msg.text || "").trim();
          if (!text) text = attachmentFallback(msg);
          text = text.replace(/\n/g, "\n    ");
          const thread = msg.thread_ts && msg.reply_count ? ` [thread: ${msg.reply_count} replies]` : "";
          console.log(`[${ts}] <${user}>${thread}${fileBadge(msg)}  ts=${msg.ts}\n    ${text || "<empty>"}\n`);
        }
        if (result.has_more) console.log("... (more messages available, use --oldest/--latest to paginate)");
        break;
      }

      case "message": {
        const arg = positional[0];
        const ts = positional[1];
        if (!arg || !ts) { console.error("Usage: message <channel> <timestamp>"); process.exit(1); }
        const channel = await resolveChannel(arg);
        const result = await client.conversations.history({
          channel,
          latest: ts,
          inclusive: true,
          limit: 1,
        });
        const msg = (result.messages || [])[0];
        if (!msg) { console.log("Message not found."); break; }
        // Pretty header + raw JSON below
        const user = msg.user || msg.bot_id || "unknown";
        console.log(`[${formatTs(msg.ts)}] <${user}>${fileBadge(msg)}  ts=${msg.ts}\n`);
        console.log(JSON.stringify(msg, null, 2));
        break;
      }

      case "thread": {
        const arg = positional[0];
        const parentTs = positional[1];
        if (!arg || !parentTs) { console.error("Usage: thread <channel> <timestamp>"); process.exit(1); }
        const channel = await resolveChannel(arg);
        const result = await client.conversations.replies({
          channel,
          ts: parentTs,
          limit: parseInt(opts.limit || "50"),
        });
        for (const msg of result.messages || []) {
          const ts = formatTs(msg.ts);
          const user = msg.user || msg.bot_id || "unknown";
          let text = (msg.text || "").trim();
          if (!text) text = attachmentFallback(msg);
          text = text.replace(/\n/g, "\n    ");
          const isParent = msg.ts === parentTs ? " [PARENT]" : "";
          console.log(`[${ts}] <${user}>${isParent}${fileBadge(msg)}  ts=${msg.ts}\n    ${text || "<empty>"}\n`);
        }
        break;
      }

      case "send": {
        const arg = positional[0];
        const text = positional.slice(1).join(" ");
        if (!arg || !text) { console.error("Usage: send <channel> <text> [--thread_ts TS]"); process.exit(1); }
        const channel = await resolveChannel(arg);
        const params = { channel, text };
        if (opts.thread_ts) params.thread_ts = opts.thread_ts;
        const result = await client.chat.postMessage(params);
        console.log(`Message sent. ts=${result.ts} channel=${result.channel}`);
        break;
      }

      case "upload": {
        const arg = positional[0];
        const filePath = positional[1];
        if (!arg || !filePath) { console.error("Usage: upload <channel> <file_path> [--title TITLE] [--thread_ts TS]"); process.exit(1); }
        const channel = await resolveChannel(arg);
        const fileContent = fs.readFileSync(filePath);
        const fileName = opts.title || path.basename(filePath);
        const uploadParams = {
          channel_id: channel,
          file: fileContent,
          filename: fileName,
          title: fileName,
        };
        if (opts.thread_ts) uploadParams.thread_ts = opts.thread_ts;
        if (opts.comment) uploadParams.initial_comment = opts.comment;
        await client.filesUploadV2(uploadParams);
        console.log(`File uploaded: ${fileName} to ${channel}`);
        break;
      }

      case "delete": {
        const arg = positional[0];
        const ts = positional[1];
        if (!arg || !ts) { console.error("Usage: delete <channel> <timestamp>"); process.exit(1); }
        const channel = await resolveChannel(arg);
        await client.chat.delete({ channel, ts });
        console.log(`Deleted message ${ts} from ${channel}`);
        break;
      }

      case "channels": {
        const result = await client.conversations.list({
          limit: parseInt(opts.limit || "100"),
          types: "public_channel,private_channel",
        });
        for (const ch of result.channels || []) {
          console.log(`${ch.id}  #${ch.name}  (${ch.num_members || 0} members)`);
        }
        break;
      }

      case "dms": {
        const result = await client.conversations.list({
          limit: parseInt(opts.limit || "100"),
          types: "im",
        });
        const ims = (result.channels || []).filter(c => !c.is_user_deleted);
        const userIds = [...new Set(ims.filter(c => c.user).map(c => c.user))];
        const userMap = {};
        await Promise.all(userIds.map(async (uid) => {
          try {
            const r = await client.users.info({ user: uid });
            userMap[uid] = r.user.real_name || r.user.name;
          } catch { userMap[uid] = uid; }
        }));
        for (const c of ims) {
          if (c.is_im) {
            console.log(`${c.id}  DM with ${userMap[c.user] || c.user}  (${c.user})`);
          } else if (c.is_mpim) {
            console.log(`${c.id}  ${c.name || "(group DM)"}`);
          }
        }
        break;
      }

      case "users": {
        const result = await client.users.list({
          limit: parseInt(opts.limit || "100"),
        });
        for (const u of result.members || []) {
          if (u.deleted) continue;
          console.log(`${u.id}  ${u.real_name || u.name}  (@${u.name})`);
        }
        break;
      }

      case "userinfo": {
        const userId = positional[0];
        if (!userId) { console.error("Usage: userinfo <user_id>"); process.exit(1); }
        const result = await client.users.info({ user: userId });
        const u = result.user;
        console.log(JSON.stringify({
          id: u.id,
          name: u.name,
          real_name: u.real_name,
          display_name: u.profile.display_name,
          email: u.profile.email,
          is_bot: u.is_bot,
        }, null, 2));
        break;
      }

      case "files": {
        const arg = positional[0];
        if (!arg) { console.error("Usage: files <channel> [--limit N] [--types images]"); process.exit(1); }
        const channel = await resolveChannel(arg);
        const params = { channel, count: parseInt(opts.limit || "20") };
        if (opts.types) params.types = opts.types;
        const result = await client.files.list(params);
        for (const f of result.files || []) {
          const size = (f.size / 1024).toFixed(1) + "KB";
          const created = formatTs(f.created);
          console.log(`${f.id}  ${f.name}  (${f.mimetype}, ${size})  created=${created}  ${f.url_private_download ? "[downloadable]" : ""}`);
        }
        if (result.paging && result.paging.pages > result.paging.page) {
          console.log(`... page ${result.paging.page}/${result.paging.pages}`);
        }
        break;
      }

      case "fileinfo": {
        const fileId = positional[0];
        if (!fileId) { console.error("Usage: fileinfo <file_id>"); process.exit(1); }
        const result = await client.files.info({ file: fileId });
        const f = result.file;
        console.log(JSON.stringify({
          id: f.id,
          name: f.name,
          mimetype: f.mimetype,
          size: f.size,
          user: f.user,
          created: formatTs(f.created),
          url_private: f.url_private,
          url_private_download: f.url_private_download,
          thumb_360: f.thumb_360,
          thumb_480: f.thumb_480,
          thumb_720: f.thumb_720,
          channels: f.channels,
        }, null, 2));
        break;
      }

      case "download": {
        // Explicit single-file download → persistent. Default outDir = cwd. No TTL.
        // (For context-loading bulk fetches, use the `fetch` subcommand which is ephemeral.)
        const fileId = positional[0];
        if (!fileId) { console.error("Usage: download <file_id> [--out DIR]"); process.exit(1); }
        const result = await client.files.info({ file: fileId });
        const f = result.file;
        const url = f.url_private_download || f.url_private;
        if (!url) { console.error("No download URL available for this file."); process.exit(1); }

        const outDir = opts.out || ".";
        if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
        const outPath = path.join(outDir, f.name);
        await downloadFile(url, outPath);
        const stat = fs.statSync(outPath);
        console.log(`${f.mimetype}\t${(stat.size / 1024).toFixed(1)}KB\t${outPath}`);
        break;
      }

      case "fetch": {
        const arg = positional[0];
        if (!arg) { console.error("Usage: fetch <channel> [--limit N]"); process.exit(1); }
        const channel = await resolveChannel(arg);
        const limit = parseInt(opts.limit || "10");

        pruneOldFetches();
        ensureFetchDir();
        const outDir = fs.mkdtempSync(path.join(FETCH_DIR, "fetch-"));

        const result = await client.conversations.history({ channel, limit });
        let count = 0;
        for (const msg of result.messages || []) {
          if (!msg.files) continue;
          for (const f of msg.files) {
            const url = f.url_private_download || f.url_private;
            if (!url) continue;
            const safeName = `${f.id}_${(f.name || "file").replace(/\s+/g, "_")}`;
            const outPath = path.join(outDir, safeName);
            try {
              await downloadFile(url, outPath);
              const stat = fs.statSync(outPath);
              console.log(`${f.mimetype}\t${(stat.size / 1024).toFixed(1)}KB\t${outPath}`);
              count++;
            } catch (e) {
              console.error(`failed: ${f.name}: ${e.message}`);
            }
          }
        }
        if (count === 0) {
          fs.rmSync(outDir, { recursive: true, force: true });
          console.error(`no files in last ${limit} messages of ${arg}`);
          process.exit(1);
        }
        break;
      }

      case "cleanup": {
        if (fs.existsSync(FETCH_DIR)) {
          fs.rmSync(FETCH_DIR, { recursive: true, force: true });
          console.log(`removed ${FETCH_DIR}`);
        } else {
          console.log("nothing to clean");
        }
        break;
      }

      default:
        console.error(`Unknown command: ${command}`);
        process.exit(1);
    }
  } catch (err) {
    console.error(`Slack API error: ${err.message}`);
    if (err.data) console.error(JSON.stringify(err.data, null, 2));
    process.exit(1);
  }
}

main();
