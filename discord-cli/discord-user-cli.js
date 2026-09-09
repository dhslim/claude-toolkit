#!/usr/bin/env node
// Discord User CLI — read your own account data via your user token.
// Companion to discord-cli.js (which is a bot). Use this when you need to
// touch data the bot can't see: your DMs, your friend list, your full
// channel history as the human user.
//
// SAFETY:
//   1. Using a user token outside the official client is classified as
//      "self-botting" by Discord ToS. Risk of action is low for personal
//      read-only use but not zero.
//   2. The token in DISCORD_USER_TOKEN grants FULL account access. Treat it
//      like a password. Never commit .env. Rotate (change Discord password)
//      if you suspect leak.
//
// Usage: node discord-user-cli.js <command> [args]

const path = require("path");

try {
  require("dotenv").config({ path: path.join(__dirname, "..", ".env"), quiet: true });
} catch (_) {}

const token = process.env.DISCORD_USER_TOKEN;
if (!token) {
  console.error("Error: DISCORD_USER_TOKEN is not set.");
  console.error("Add DISCORD_USER_TOKEN=<your user token> to the repo's .env.");
  console.error("");
  console.error("How to extract your token:");
  console.error("  1. Open Discord in a browser (https://discord.com/app)");
  console.error("  2. F12 → Network tab");
  console.error("  3. Click any channel/DM (triggers an API call)");
  console.error("  4. Pick a request to discord.com/api/... → Headers → 'authorization' value");
  console.error("  5. Paste that string into .env as DISCORD_USER_TOKEN");
  process.exit(1);
}

const API = "https://discord.com/api/v10";

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function call(method, route, body, _retries = 5) {
  const headers = {
    "Authorization": token,
    "Content-Type": "application/json",
    "User-Agent":
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
      "discord/1.0.9019 Chrome/124.0.6367.243 Electron/30.2.0 Safari/537.36",
  };
  const opts = { method, headers };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(`${API}${route}`, opts);

  // Respect Discord rate limits: 429 carries retry_after (seconds). Wait + retry.
  if (res.status === 429 && _retries > 0) {
    const data = await res.json().catch(() => ({}));
    const wait = Math.ceil((data.retry_after ?? 1) * 1000) + 250;
    process.stderr.write(`\n  rate-limited, waiting ${wait}ms...\n`);
    await sleep(wait);
    return call(method, route, body, _retries - 1);
  }

  const text = await res.text();
  if (!res.ok) throw new Error(`Discord API ${res.status} on ${method} ${route}: ${text}`);
  return text ? JSON.parse(text) : null;
}

// Walk a channel's full message history. The messages endpoint returns up to
// 100 at a time, newest-first; we paginate backwards with `before` (the oldest
// id seen so far) until a short/empty page. Returns oldest-first.
async function fetchAllMessages(channelId, { max = Infinity, pageDelay = 500 } = {}) {
  const all = [];
  let before;
  while (all.length < max) {
    const q = new URLSearchParams({ limit: "100" });
    if (before) q.set("before", before);
    const page = await call("GET", `/channels/${channelId}/messages?${q}`);
    if (!page || page.length === 0) break;
    all.push(...page);
    before = page[page.length - 1].id; // oldest in this page
    process.stderr.write(`\r  fetched ${all.length} messages...`);
    if (page.length < 100) break; // last page
    await sleep(pageDelay); // be gentle on the rate limiter
  }
  process.stderr.write("\n");
  const trimmed = max === Infinity ? all : all.slice(0, max);
  return trimmed.reverse(); // newest-first -> oldest-first for reading
}

function fmtTs(iso) {
  if (!iso) return "?";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("sv-SE", { timeZone: "Asia/Seoul" }) + " KST";
}

function parseArgs(args) {
  const opts = {};
  const positional = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      opts[args[i].slice(2)] = args[i + 1];
      i++;
    } else {
      positional.push(args[i]);
    }
  }
  return { positional, opts };
}

async function openDmChannel(userId) {
  // POST /users/@me/channels with recipient_id returns the existing DM channel
  // if one exists, otherwise creates and returns a new one. Idempotent.
  const ch = await call("POST", "/users/@me/channels", { recipient_id: userId });
  return ch.id;
}

function formatMessage(m) {
  const author = m.author?.username || "unknown";
  const display = m.author?.global_name ? ` (${m.author.global_name})` : "";
  const text = (m.content || "").trim().replace(/\n/g, "\n    ");
  const attach = (m.attachments || []).length ? ` [${m.attachments.length} attach]` : "";
  return `[${fmtTs(m.timestamp)}] <${author}${display}>${attach}  id=${m.id}\n    ${text || "<no text>"}`;
}

function printMessage(m) {
  console.log(formatMessage(m) + "\n");
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.log(`Discord User CLI — YOUR account via YOUR user token (self-botting; use at risk).

Commands:
  whoami                                  Your identity (verifies token works)
  dm-list                                 List your DM channels with recipient usernames
  dm-channel       <user_id>              Get or create DM channel ID for a user
  dm-history       <user_id> [--limit N] [--before ID]
                                          Recent messages in DM with that user
  dm-dump          <user_id> [--out FILE] [--max N] [--format json|text]
                                          Walk the ENTIRE DM history (paginated).
                                          Writes to FILE if given, else prints.
  dm-grep          <user_id> <text> [--max N]
                                          Paginate the whole DM and filter locally
                                          (more reliable than Discord's search)
  dm-search        <user_id> <text>       Search your DM with that user (server-side index)
  channel-history  <channel_id> [--limit N] [--before ID]
                                          Any channel you can see, as your user
  channel-search   <channel_id> <text>    Search any channel you can see
  send-dm          <user_id> <text>       Send a DM as YOU (not the bot)

IDs are raw snowflakes (Developer Mode -> Copy ID). Times shown in KST.
Environment: DISCORD_USER_TOKEN=<your user token>`);
    return;
  }

  const cmd = args[0];
  const { positional, opts } = parseArgs(args.slice(1));

  try {
    switch (cmd) {
      case "whoami": {
        const u = await call("GET", "/users/@me");
        const tag =
          u.discriminator && u.discriminator !== "0"
            ? `${u.username}#${u.discriminator}`
            : u.username;
        console.log(`${tag}  id=${u.id}`);
        if (u.global_name) console.log(`  display: ${u.global_name}`);
        if (u.email) console.log(`  email:   ${u.email}`);
        break;
      }

      case "dm-list": {
        const channels = await call("GET", "/users/@me/channels");
        channels.sort((a, b) => (b.last_message_id || "").localeCompare(a.last_message_id || ""));
        for (const c of channels) {
          const recipients = (c.recipients || [])
            .map((r) => `${r.username}${r.global_name ? `(${r.global_name})` : ""} [${r.id}]`)
            .join(", ");
          const label = c.type === 3 ? `group: ${c.name || "(unnamed)"}` : recipients || c.name || "(?)";
          console.log(`${c.id}  ${label}`);
        }
        break;
      }

      case "dm-channel": {
        const uid = positional[0];
        if (!uid) {
          console.error("Usage: dm-channel <user_id>");
          process.exit(1);
        }
        console.log(await openDmChannel(uid));
        break;
      }

      case "dm-history": {
        const uid = positional[0];
        if (!uid) {
          console.error("Usage: dm-history <user_id> [--limit N] [--before ID]");
          process.exit(1);
        }
        const chId = await openDmChannel(uid);
        const q = new URLSearchParams({ limit: String(parseInt(opts.limit || "30")) });
        if (opts.before) q.set("before", opts.before);
        const msgs = await call("GET", `/channels/${chId}/messages?${q}`);
        for (const m of msgs.reverse()) printMessage(m);
        break;
      }

      case "dm-dump": {
        const uid = positional[0];
        if (!uid) {
          console.error("Usage: dm-dump <user_id> [--out FILE] [--max N] [--format json|text]");
          process.exit(1);
        }
        const chId = await openDmChannel(uid);
        const max = opts.max ? parseInt(opts.max) : Infinity;
        const msgs = await fetchAllMessages(chId, { max });
        const fmt =
          opts.format || (opts.out && opts.out.endsWith(".json") ? "json" : "text");
        if (opts.out) {
          const fs = require("fs");
          const payload =
            fmt === "json"
              ? JSON.stringify(msgs, null, 2)
              : msgs.map(formatMessage).join("\n\n");
          fs.writeFileSync(opts.out, payload);
          console.log(`Wrote ${msgs.length} messages to ${opts.out}`);
        } else {
          for (const m of msgs) printMessage(m);
          console.error(`(${msgs.length} messages total)`);
        }
        break;
      }

      case "dm-grep": {
        const uid = positional[0];
        const text = positional.slice(1).join(" ");
        if (!uid || !text) {
          console.error('Usage: dm-grep <user_id> "<text>" [--max N]');
          process.exit(1);
        }
        const chId = await openDmChannel(uid);
        const max = opts.max ? parseInt(opts.max) : Infinity;
        const msgs = await fetchAllMessages(chId, { max });
        const needle = text.toLowerCase();
        const hits = msgs.filter((m) => (m.content || "").toLowerCase().includes(needle));
        for (const m of hits) printMessage(m);
        console.error(`${hits.length} match(es) across ${msgs.length} messages`);
        break;
      }

      case "dm-search": {
        const uid = positional[0];
        const text = positional.slice(1).join(" ");
        if (!uid || !text) {
          console.error('Usage: dm-search <user_id> "<text>"');
          process.exit(1);
        }
        const chId = await openDmChannel(uid);
        const q = new URLSearchParams({ content: text });
        const result = await call("GET", `/channels/${chId}/messages/search?${q}`);
        const flat = (result.messages || []).flat();
        console.log(`Hits: ${result.total_results ?? flat.length}\n`);
        for (const m of flat) printMessage(m);
        break;
      }

      case "channel-history": {
        const ch = positional[0];
        if (!ch) {
          console.error("Usage: channel-history <channel_id> [--limit N] [--before ID]");
          process.exit(1);
        }
        const q = new URLSearchParams({ limit: String(parseInt(opts.limit || "30")) });
        if (opts.before) q.set("before", opts.before);
        const msgs = await call("GET", `/channels/${ch}/messages?${q}`);
        for (const m of msgs.reverse()) printMessage(m);
        break;
      }

      case "channel-search": {
        const ch = positional[0];
        const text = positional.slice(1).join(" ");
        if (!ch || !text) {
          console.error('Usage: channel-search <channel_id> "<text>"');
          process.exit(1);
        }
        const q = new URLSearchParams({ content: text });
        const result = await call("GET", `/channels/${ch}/messages/search?${q}`);
        const flat = (result.messages || []).flat();
        console.log(`Hits: ${result.total_results ?? flat.length}\n`);
        for (const m of flat) printMessage(m);
        break;
      }

      case "send-dm": {
        const uid = positional[0];
        const text = positional.slice(1).join(" ");
        if (!uid || !text) {
          console.error("Usage: send-dm <user_id> <text>");
          process.exit(1);
        }
        const chId = await openDmChannel(uid);
        const m = await call("POST", `/channels/${chId}/messages`, { content: text });
        console.log(`Sent as YOU. id=${m.id} dm_channel=${chId}`);
        break;
      }

      default:
        console.error(`Unknown command: ${cmd}`);
        process.exit(1);
    }
  } catch (err) {
    console.error("Error:", err.message);
    process.exit(1);
  }
}

main();
