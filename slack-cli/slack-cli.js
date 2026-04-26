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
//   users [--limit N]
//   files <channel> [--limit N] [--types images]
//   fileinfo <file_id>
//   download <file_id> [--out DIR]
//
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

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.log(`Slack CLI - Commands:
  history <channel> [--limit N] [--oldest TS] [--latest TS]  Read channel history
  message <channel> <timestamp>                              Read a specific message
  thread  <channel> <timestamp> [--limit N]                  Read thread replies
  send    <channel> <text> [--thread_ts TS]                  Send a message
  channels [--limit N]                                       List channels
  users [--limit N]                                          List users
  userinfo <user_id>                                         Get user info
  files    <channel> [--limit N] [--types images]            List files in channel
  fileinfo <file_id>                                         Get file details
  download <file_id> [--out DIR]                             Download a file

Environment: SLACK_TOKEN=xoxb-... or xoxp-...`);
    return;
  }

  const command = args[0];
  const { positional, opts } = parseArgs(args.slice(1));

  try {
    switch (command) {
      case "history": {
        const channel = positional[0];
        if (!channel) { console.error("Usage: history <channel> [--limit N]"); process.exit(1); }
        const result = await client.conversations.history({
          channel,
          limit: parseInt(opts.limit || "20"),
          ...(opts.oldest && { oldest: opts.oldest }),
          ...(opts.latest && { latest: opts.latest }),
        });
        for (const msg of result.messages || []) {
          const ts = msg.ts;
          const user = msg.user || msg.bot_id || "unknown";
          const text = (msg.text || "").replace(/\n/g, "\n    ");
          const thread = msg.thread_ts && msg.reply_count ? ` [thread: ${msg.reply_count} replies]` : "";
          console.log(`[${ts}] <${user}>${thread}\n    ${text}\n`);
        }
        if (result.has_more) console.log("... (more messages available, use --oldest/--latest to paginate)");
        break;
      }

      case "message": {
        const channel = positional[0];
        const ts = positional[1];
        if (!channel || !ts) { console.error("Usage: message <channel> <timestamp>"); process.exit(1); }
        const result = await client.conversations.history({
          channel,
          latest: ts,
          inclusive: true,
          limit: 1,
        });
        const msg = (result.messages || [])[0];
        if (!msg) { console.log("Message not found."); break; }
        console.log(JSON.stringify(msg, null, 2));
        break;
      }

      case "thread": {
        const channel = positional[0];
        const ts = positional[1];
        if (!channel || !ts) { console.error("Usage: thread <channel> <timestamp>"); process.exit(1); }
        const result = await client.conversations.replies({
          channel,
          ts,
          limit: parseInt(opts.limit || "50"),
        });
        for (const msg of result.messages || []) {
          const user = msg.user || msg.bot_id || "unknown";
          const text = (msg.text || "").replace(/\n/g, "\n    ");
          const isParent = msg.ts === ts ? " [PARENT]" : "";
          console.log(`[${msg.ts}] <${user}>${isParent}\n    ${text}\n`);
        }
        break;
      }

      case "send": {
        const channel = positional[0];
        const text = positional.slice(1).join(" ");
        if (!channel || !text) { console.error("Usage: send <channel> <text> [--thread_ts TS]"); process.exit(1); }
        const params = { channel, text };
        if (opts.thread_ts) params.thread_ts = opts.thread_ts;
        const result = await client.chat.postMessage(params);
        console.log(`Message sent. ts=${result.ts} channel=${result.channel}`);
        break;
      }

      case "upload": {
        const channel = positional[0];
        const filePath = positional[1];
        if (!channel || !filePath) { console.error("Usage: upload <channel> <file_path> [--title TITLE] [--thread_ts TS]"); process.exit(1); }
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
        const result = await client.filesUploadV2(uploadParams);
        console.log(`File uploaded: ${fileName} to ${channel}`);
        break;
      }

      case "delete": {
        const channel = positional[0];
        const ts = positional[1];
        if (!channel || !ts) { console.error("Usage: delete <channel> <timestamp>"); process.exit(1); }
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
        const channel = positional[0];
        if (!channel) { console.error("Usage: files <channel> [--limit N] [--types images]"); process.exit(1); }
        const params = { channel, count: parseInt(opts.limit || "20") };
        if (opts.types) params.types = opts.types;
        const result = await client.files.list(params);
        for (const f of result.files || []) {
          const size = (f.size / 1024).toFixed(1) + "KB";
          console.log(`${f.id}  ${f.name}  (${f.mimetype}, ${size})  ${f.url_private_download ? "[downloadable]" : ""}`);
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
          created: new Date(f.created * 1000).toISOString(),
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
        const fileId = positional[0];
        if (!fileId) { console.error("Usage: download <file_id> [--out DIR]"); process.exit(1); }
        const result = await client.files.info({ file: fileId });
        const f = result.file;
        const url = f.url_private_download || f.url_private;
        if (!url) { console.error("No download URL available for this file."); process.exit(1); }
        const outDir = opts.out || ".";
        const outPath = path.join(outDir, f.name);
        const get = url.startsWith("https") ? https.get : http.get;
        await new Promise((resolve, reject) => {
          get(url, { headers: { Authorization: `Bearer ${token}` } }, (res) => {
            if (res.statusCode === 302 || res.statusCode === 301) {
              get(res.headers.location, { headers: { Authorization: `Bearer ${token}` } }, (res2) => {
                const ws = fs.createWriteStream(outPath);
                res2.pipe(ws);
                ws.on("finish", () => { ws.close(); resolve(); });
                ws.on("error", reject);
              }).on("error", reject);
            } else if (res.statusCode === 200) {
              const ws = fs.createWriteStream(outPath);
              res.pipe(ws);
              ws.on("finish", () => { ws.close(); resolve(); });
              ws.on("error", reject);
            } else {
              reject(new Error(`HTTP ${res.statusCode}`));
            }
          }).on("error", reject);
        });
        const stat = fs.statSync(outPath);
        console.log(`Downloaded: ${outPath} (${(stat.size / 1024).toFixed(1)}KB)`);
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
