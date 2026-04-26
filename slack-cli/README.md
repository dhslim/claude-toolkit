# Slack CLI

Lightweight cross-platform command-line tool for interacting with Slack. Built on [`@slack/web-api`](https://www.npmjs.com/package/@slack/web-api).

Lives inside the `claude-toolkit` repo so it travels with the rest of your tooling — clone the repo on any machine (Linux / macOS / Windows), install once, and you're done.

## Setup

### 1. Prerequisites

- Node.js 18+ (`node --version`)
- The `claude-toolkit` repo cloned somewhere on disk. The rest of these instructions refer to that location as `$TOOLKIT` — substitute your actual path.

### 2. Install dependencies

```bash
cd "$TOOLKIT/slack-cli"
npm install
```

This populates `node_modules/` locally (gitignored).

### 3. Create a Slack App and get a token

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Under **OAuth & Permissions**, add these **Bot Token Scopes**:
   - `channels:history` — read channel messages
   - `channels:read` — list channels
   - `chat:write` — send messages
   - `groups:history` — read private channel messages
   - `groups:read` — list private channels
   - `users:read` — list users
   - `users:read.email` — read user emails
   - `files:read` — list and download files
   - `files:write` — upload files
3. **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-...`)
4. Optionally also add **User Token Scopes** and copy the **User OAuth Token** (`xoxp-...`) for broader access

### 4. Configure environment

Pick the section for your shell. In every example, replace `/path/to/claude-toolkit` with your actual clone path.

#### Bash / Zsh (Linux, macOS, Git-Bash on Windows)

Add to `~/.bashrc` or `~/.zshrc`:

```bash
export SLACK_TOKEN="xoxb-your-bot-token"
export SLACK_USER_TOKEN="xoxp-your-user-token"  # optional
alias slack='node /path/to/claude-toolkit/slack-cli/slack-cli.js'
```

Then `source ~/.bashrc` (or restart the shell).

#### PowerShell (Windows native)

Add to your PowerShell profile (`notepad $PROFILE`):

```powershell
$env:SLACK_TOKEN      = "xoxb-your-bot-token"
$env:SLACK_USER_TOKEN = "xoxp-your-user-token"  # optional
function slack { node "C:\path\to\claude-toolkit\slack-cli\slack-cli.js" @args }
```

Reload with `. $PROFILE`.

#### Fish

```fish
set -Ux SLACK_TOKEN      "xoxb-your-bot-token"
set -Ux SLACK_USER_TOKEN "xoxp-your-user-token"  # optional
alias slack 'node /path/to/claude-toolkit/slack-cli/slack-cli.js'
funcsave slack
```

## Commands

### Channels & users

```bash
slack channels                    # list channels
slack users                       # list users
slack userinfo U05967QFE0K        # user details
```

### Reading messages

```bash
slack history C06K2E96CAD                                    # recent messages
slack history C06K2E96CAD --limit 50
slack history C06K2E96CAD --oldest 1774400000 --latest 1774440000
slack message C06K2E96CAD 1774435503.370649                  # full message JSON
slack thread  C06K2E96CAD 1774414884.156789                  # full thread
```

### Sending messages

```bash
slack send C06K2E96CAD "Hello world"
slack send C06K2E96CAD "Reply text" --thread_ts 1774414884.156789
```

### Files & images

```bash
slack files    C06K2E96CAD
slack files    C06K2E96CAD --types images
slack fileinfo F0AE6K3AERK
slack download F0AE6K3AERK
slack download F0AE6K3AERK --out ~/Downloads
```

## Tips

- Channel IDs look like `C06K2E96CAD` — find them with `slack channels`.
- Message timestamps look like `1774435503.370649` — find them in `slack history` output.
- `slack message` returns the full JSON of a message, including attached files.
- To run a one-off command with the user token instead of the bot token: `SLACK_TOKEN="$SLACK_USER_TOKEN" slack ...`
- The bot token needs `files:read` for `files`, `fileinfo`, and `download`.
