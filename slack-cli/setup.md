# Slack CLI Setup

## Getting a Slack Token

You need a Slack Bot Token (xoxb-...) or User Token (xoxp-...) to use this CLI.

### Option 1: Create a Slack App (Recommended)
1. Go to https://api.slack.com/apps
2. Click "Create New App" > "From scratch"
3. Name it (e.g., "CLI Bot") and select your workspace
4. Go to "OAuth & Permissions"
5. Add these Bot Token Scopes:
   - `channels:history` - Read channel messages
   - `channels:read` - List channels
   - `chat:write` - Send messages
   - `groups:history` - Read private channel messages
   - `groups:read` - List private channels
   - `users:read` - List users
   - `users:read.email` - Read user emails
6. Click "Install to Workspace" and authorize
7. Copy the "Bot User OAuth Token" (starts with xoxb-)

### Option 2: Extract from browser (User Token)
For a user token with broader access, you can extract it from Slack web app.

## Configuration

Set the token as an environment variable. Add to your ~/.bashrc or ~/.bash_profile:

```bash
export SLACK_TOKEN=xoxb-your-token-here
```

## Usage

```bash
# Create an alias (add to ~/.bashrc)
alias slack='node C:/Users/user/.local/slack-cli/slack-cli.js'

# Then use:
slack history C05AC5KD3UH --limit 10
slack message C05AC5KD3UH 1774223615.777009
slack thread C05AC5KD3UH 1774223615.777009
slack send C05AC5KD3UH "Hello world"
slack channels
slack users
```
