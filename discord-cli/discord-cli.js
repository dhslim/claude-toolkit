#!/usr/bin/env node
// Discord CLI - read + send via the Discord REST API (@discordjs/rest).
// Phase 1 of the Discord integration: on-demand read/send, no gateway, no
// always-on process. Single-shot request/response, exits cleanly.
//
// Usage: node discord-cli.js <command> [args]
//
// Commands:
//   whoami                                  Show the bot's own identity (good token test)
//   guilds                                  List servers (guilds) the bot is in
//   channels <guild_id>                     List text channels in a guild
//   history <channel_id> [--limit N] [--before ID] [--after ID]
//   message <channel_id> <message_id>       Show one message (full JSON)
//   send    <channel_id> <text> [--reply <message_id>]
//   dm      <user_id> <text>                Open-or-create a DM and send
//   react   <channel_id> <message_id> <emoji>
//   delete  <channel_id> <message_id>
//
// IDs are raw Discord snowflakes (enable Developer Mode -> right-click -> Copy ID).
// Timestamps are shown in KST (Asia/Seoul).
// Environment: DISCORD_TOKEN must be set (the bot token).

const { REST } = require("@discordjs/rest");
const { Routes } = require("discord-api-types/v10");

// Load DISCORD_TOKEN from the repo's .env (one level up) if it isn't already in
// the environment, so `node discord-cli.js` works without a manual export.
// dotenv does NOT override vars already set, so an exported token still wins.
try {
  require("dotenv").config({ path: require("path").join(__dirname, "..", ".env"), quiet: true });
} catch (_) { /* dotenv optional — env may already provide DISCORD_TOKEN */ }

const token = process.env.DISCORD_TOKEN;
if (!token) {
  console.error("Error: DISCORD_TOKEN environment variable is not set.");
  console.error("Set it in the repo's .env (DISCORD_TOKEN=...) — the bot token from the Discord Developer Portal -> Bot -> Reset Token.");
  process.exit(1);
}

const rest = new REST({ version: "10" }).setToken(token);

// ---------------- helpers ----------------

function fmtTs(iso) {
  if (!iso) return "?";
  // Discord message timestamps are ISO-8601 UTC; render as KST.
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("sv-SE", { timeZone: "Asia/Seoul" }) + " KST";
}

function authorName(m) {
  const a = m.author || {};
  const tag = a.discriminator && a.discriminator !== "0" ? `${a.username}#${a.discriminator}` : a.username;
  return `${tag || "unknown"}${a.bot ? " [bot]" : ""}`;
}

function attachmentBadge(m) {
  const n = (m.attachments || []).length;
  return n ? ` [${n} attachment${n > 1 ? "s" : ""}]` : "";
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

// ---------------- main ----------------

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.log(`Discord CLI - Commands:
  whoami                                   Bot identity (token test)
  guilds                                   List servers the bot is in
  channels <guild_id>                      List text channels in a guild
  history  <channel_id> [--limit N] [--before ID] [--after ID]
  message  <channel_id> <message_id>       Show one message (full JSON)
  send     <channel_id> <text> [--reply <message_id>]
  dm       <user_id> <text>                Open-or-create a DM and send
  react    <channel_id> <message_id> <emoji>
  delete   <channel_id> <message_id>

IDs are raw snowflakes (Developer Mode -> Copy ID). Times shown in KST.
Environment: DISCORD_TOKEN=<bot token>`);
    return;
  }

  const command = args[0];
  const { positional, opts } = parseArgs(args.slice(1));

  try {
    switch (command) {
      case "whoami": {
        const u = await rest.get(Routes.user("@me"));
        console.log(`${u.username}${u.discriminator && u.discriminator !== "0" ? "#" + u.discriminator : ""}  id=${u.id}  bot=${u.bot === true}`);
        break;
      }

      case "guilds": {
        const guilds = await rest.get(Routes.userGuilds());
        if (!guilds.length) {
          console.log("(bot is in no servers — invite it first)");
          break;
        }
        for (const g of guilds) console.log(`${g.id}  ${g.name}`);
        break;
      }

      case "channels": {
        const guild = positional[0];
        if (!guild) { console.error("Usage: channels <guild_id>"); process.exit(1); }
        const chans = await rest.get(Routes.guildChannels(guild));
        // 0=text, 5=announcement, 15=forum; show text-ish channels first
        for (const c of chans.sort((a, b) => (a.position || 0) - (b.position || 0))) {
          const kind = { 0: "text", 2: "voice", 4: "category", 5: "news", 15: "forum" }[c.type] || `type${c.type}`;
          console.log(`${c.id}  #${c.name}  (${kind})`);
        }
        break;
      }

      case "history": {
        const ch = positional[0];
        if (!ch) { console.error("Usage: history <channel_id> [--limit N]"); process.exit(1); }
        const q = new URLSearchParams({ limit: String(parseInt(opts.limit || "20")) });
        if (opts.before) q.set("before", opts.before);
        if (opts.after) q.set("after", opts.after);
        const msgs = await rest.get(`${Routes.channelMessages(ch)}?${q}`);
        // API returns newest-first; show oldest-first for readability
        for (const m of msgs.reverse()) {
          let text = (m.content || "").trim().replace(/\n/g, "\n    ");
          console.log(`[${fmtTs(m.timestamp)}] <${authorName(m)}>${attachmentBadge(m)}  id=${m.id}\n    ${text || "<no text>"}\n`);
        }
        break;
      }

      case "message": {
        const ch = positional[0];
        const id = positional[1];
        if (!ch || !id) { console.error("Usage: message <channel_id> <message_id>"); process.exit(1); }
        const m = await rest.get(`${Routes.channelMessage(ch, id)}`);
        console.log(`[${fmtTs(m.timestamp)}] <${authorName(m)}>  id=${m.id}\n`);
        console.log(JSON.stringify(m, null, 2));
        break;
      }

      case "send": {
        const ch = positional[0];
        const text = positional.slice(1).join(" ");
        if (!ch || !text) { console.error("Usage: send <channel_id> <text> [--reply <message_id>]"); process.exit(1); }
        const body = { content: text };
        if (opts.reply) body.message_reference = { message_id: opts.reply };
        const m = await rest.post(Routes.channelMessages(ch), { body });
        console.log(`Sent. id=${m.id} channel=${ch}`);
        break;
      }

      case "dm": {
        const user = positional[0];
        const text = positional.slice(1).join(" ");
        if (!user || !text) { console.error("Usage: dm <user_id> <text>"); process.exit(1); }
        const chan = await rest.post(Routes.userChannels(), { body: { recipient_id: user } });
        const m = await rest.post(Routes.channelMessages(chan.id), { body: { content: text } });
        console.log(`DM sent. id=${m.id} dm_channel=${chan.id}`);
        break;
      }

      case "react": {
        const ch = positional[0];
        const id = positional[1];
        const emoji = positional[2];
        if (!ch || !id || !emoji) { console.error("Usage: react <channel_id> <message_id> <emoji>"); process.exit(1); }
        await rest.put(Routes.channelMessageOwnReaction(ch, id, encodeURIComponent(emoji)));
        console.log(`Reacted ${emoji} to ${id}`);
        break;
      }

      case "delete": {
        const ch = positional[0];
        const id = positional[1];
        if (!ch || !id) { console.error("Usage: delete <channel_id> <message_id>"); process.exit(1); }
        await rest.delete(Routes.channelMessage(ch, id));
        console.log(`Deleted ${id} from ${ch}`);
        break;
      }

      default:
        console.error(`Unknown command: ${command}`);
        process.exit(1);
    }
  } catch (err) {
    console.error(`Discord API error: ${err.message}`);
    if (err.rawError) console.error(JSON.stringify(err.rawError, null, 2));
    process.exit(1);
  }
}

main();
