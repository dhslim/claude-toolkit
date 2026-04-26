# Slack CLI

Lightweight command-line tool for interacting with Slack. Built on [`@slack/web-api`](https://www.npmjs.com/package/@slack/web-api).

## Setup

### 1. Install dependencies

```bash
cd ~/.local/slack-cli
npm install
```

### 2. Create a Slack App and get tokens

1. Go to [api.slack.com/apps](https://api.slack.com/apps) > **Create New App** > **From scratch**
2. Go to **OAuth & Permissions** and add these scopes:

**Bot Token Scopes:**
- `channels:history` - Read channel messages
- `channels:read` - List channels
- `chat:write` - Send messages
- `groups:history` - Read private channel messages
- `groups:read` - List private channels
- `users:read` - List users
- `users:read.email` - Read user emails
- `files:read` - List and download files
- `files:write` - Upload files

3. **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-...`)
4. Optionally, add **User Token Scopes** and copy the **User OAuth Token** (`xoxp-...`) for broader access

### 3. Configure environment

Add to `~/.bashrc`:

```bash
alias slack='node $HOME/.local/slack-cli/slack-cli.js'
export SLACK_TOKEN="xoxb-your-bot-token"
export SLACK_USER_TOKEN="xoxp-your-user-token"  # optional, for broader access
```

Then `source ~/.bashrc`.

## Commands

### Channels & Users

```bash
# List channels
slack channels

# List users
slack users

# Get user details
slack userinfo U05967QFE0K
```

### Reading Messages

```bash
# Recent messages in a channel
slack history C06K2E96CAD

# More messages
slack history C06K2E96CAD --limit 50

# Messages in a time range (Slack timestamps)
slack history C06K2E96CAD --oldest 1774400000 --latest 1774440000

# Full details of a specific message (includes file attachments, reactions, etc.)
slack message C06K2E96CAD 1774435503.370649

# Read a thread
slack thread C06K2E96CAD 1774414884.156789
```

### Sending Messages

```bash
# Send to a channel
slack send C06K2E96CAD "Hello world"

# Reply in a thread
slack send C06K2E96CAD "Reply text" --thread_ts 1774414884.156789
```

### Files & Images

```bash
# List files in a channel
slack files C06K2E96CAD

# List only images
slack files C06K2E96CAD --types images

# Get file details (URLs, thumbnails, metadata)
slack fileinfo F0AE6K3AERK

# Download a file to current directory
slack download F0AE6K3AERK

# Download to a specific directory
slack download F0AE6K3AERK --out ~/Downloads
```

## Tips

- Channel IDs look like `C06K2E96CAD` — find them with `slack channels`
- Message timestamps look like `1774435503.370649` — find them in `slack history` output
- Use `slack message` to see the full JSON of a message, including attached files
- To use the user token instead of bot token: `SLACK_TOKEN="$SLACK_USER_TOKEN" slack ...`
- The bot token needs `files:read` scope to use `files`, `fileinfo`, and `download` commands
