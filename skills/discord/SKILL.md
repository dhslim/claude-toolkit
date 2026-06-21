---
name: discord
description: Read and send Discord messages via the discord-cli wrapper around the Discord REST API
disable-model-invocation: true
argument-hint: <command> [args] (e.g. whoami, guilds, channels <guild_id>, history <channel_id>, send <channel_id> "hi")
---

# Discord CLI

Run as a normal foreground command (single request/response, exits cleanly):

```
node {{SCRIPT_DIR}}/discord-cli/discord-cli.js $ARGUMENTS
```

**IMPORTANT**: The path above is a placeholder — during install replace `{{SCRIPT_DIR}}` with the absolute path to this repo's directory.

`DISCORD_TOKEN` must be set in the environment Claude Code's Bash tool inherits (it lives in the repo's `.env`). It's the bot token from the Discord Developer Portal → Bot → Reset Token.

If `$ARGUMENTS` is empty, run with no args to print the command list.

## Commands

- `whoami` — the bot's own identity (use this first to verify the token works)
- `guilds` — list servers (guilds) the bot is in
- `channels <guild_id>` — list channels in a guild
- `history <channel_id> [--limit N] [--before ID] [--after ID]` — recent messages (oldest-first)
- `message <channel_id> <message_id>` — full JSON of one message
- `send <channel_id> <text> [--reply <message_id>]` — send a message (optionally as a reply)
- `dm <user_id> <text>` — open-or-create a DM channel and send
- `react <channel_id> <message_id> <emoji>` — add a reaction
- `delete <channel_id> <message_id>` — delete a message

All IDs are raw Discord **snowflakes**. Enable Developer Mode (User Settings → Advanced → Developer Mode), then right-click a channel / message / user → **Copy ID**. There is no name-based lookup — pass IDs. Times are shown in KST.

## How to present results

- For `history`, summarize concisely: who said what, newest activity first in your summary, surface IDs the user may need (channel/message). Don't dump raw JSON unless asked.
- For `send`/`dm`/`react`/`delete`, just confirm success and echo the returned message ID.
- For `whoami`/`guilds`/`channels`, present as a short labeled list.

## Failure modes

- `DISCORD_TOKEN ... not set` → the token isn't in the environment; check `.env`.
- `Discord API error: Missing Access` / `50001` → the bot isn't in that guild, or lacks permission there.
- Empty `content` on messages → the **Message Content Intent** isn't enabled on the bot (Developer Portal → Bot → Privileged Gateway Intents). Tell the user to enable it.
- `Unknown Guild/Channel` → wrong snowflake, or the bot can't see it.
