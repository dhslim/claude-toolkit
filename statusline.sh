#!/bin/bash
input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name // "Claude"')
DIR=$(echo "$input" | jq -r '.cwd // "~"')
# Effort level, straight from CC's statusline payload (low/medium/high/xhigh).
# NOTE: ultracode reports as "xhigh" here BY DESIGN — CC collapses it (the effort
# enum stops at xhigh; ultracode is a session-only overlay; upstream
# anthropics/claude-code#63468). We accept that and do NOT try to distinguish
# them: the only signal is the transcript, and scraping it is poisonable by any
# text that mentions the /effort toggle. So "(xhigh)" may mean xhigh OR ultracode.
EFFORT=$(echo "$input" | jq -r '.effort.level // empty')

# ---- Forward-token bar (preferred) -------------------------------------
# claude_proxy.py writes "<tokens>\t<unix_ts>\n" to a PER-SESSION TSV file
# at ~/.claude/proxy_last_send/<session_id>.tsv on every successful
# /v1/messages forward. We look up THIS session's file (via session_id
# from CC's input JSON) so concurrent CC sessions don't pollute each
# other's bars. Display: "TX:<K>K/700K [bar] PCT%".
#
# Fall back to CC's total_input_tokens against the model's real limit if
# the per-session file is missing or stale (>5 min old) so fresh installs,
# proxy-off sessions, or new sessions before their first forward still
# show something sensible.
SESSION_ID=$(echo "$input" | jq -r '.session_id // empty')
TX_FILE=""
if [ -n "$SESSION_ID" ]; then
    TX_FILE="$HOME/.claude/proxy_last_send/${SESSION_ID}.tsv"
fi
SHIFT_TRIGGER=700000
NOW=$(date +%s)
USE_TX=0
TX_TOKENS=0
if [ -n "$TX_FILE" ] && [ -f "$TX_FILE" ]; then
    LINE_TSV=$(head -n1 "$TX_FILE" 2>/dev/null)
    TX_TOKENS=${LINE_TSV%%	*}
    TX_TS=${LINE_TSV##*	}
    # Strip stray whitespace/newlines
    TX_TOKENS=$(echo "$TX_TOKENS" | tr -dc '0-9')
    TX_TS=$(echo "$TX_TS" | tr -dc '0-9')
    if [ -n "$TX_TOKENS" ] && [ -n "$TX_TS" ]; then
        AGE=$((NOW - TX_TS))
        if [ "$AGE" -ge 0 ] && [ "$AGE" -lt 300 ]; then
            USE_TX=1
        fi
    fi
fi

if [ "$USE_TX" = "1" ]; then
    PCT=$((TX_TOKENS * 100 / SHIFT_TRIGGER))
    [ "$PCT" -gt 100 ] && PCT=100
    TX_K=$((TX_TOKENS / 1000))
    SEG_LABEL="TX:${TX_K}K/700K"
else
    # Fallback: CC's local pre-trim view. CC 2.1.145 reports
    # context_window_size=200000 for all models even though Opus 4.7 and
    # Sonnet 4.6 have 1M extended context; map by model name.
    USED_TOKENS=$(echo "$input" | jq -r '.context_window.total_input_tokens // 0')
    case "$MODEL" in
        *Opus*|*Sonnet*)  REAL_LIMIT=1000000 ;;
        *Haiku*)          REAL_LIMIT=200000 ;;
        *)                REAL_LIMIT=200000 ;;
    esac
    if [ "$USED_TOKENS" -gt 0 ]; then
        PCT=$((USED_TOKENS * 100 / REAL_LIMIT))
        [ "$PCT" -gt 100 ] && PCT=100
    else
        PCT=0
    fi
    SEG_LABEL="ctx"
fi

# Bar
BAR_WIDTH=10
FILLED=$((PCT * BAR_WIDTH / 100))
EMPTY=$((BAR_WIDTH - FILLED))
printf -v FILL "%${FILLED}s"
printf -v PAD "%${EMPTY}s"
BAR="${FILL// /█}${PAD// /░}"

# Git info
BRANCH=""
STATUS=""
if git -C "$DIR" rev-parse --git-dir > /dev/null 2>&1; then
    BRANCH=$(git -C "$DIR" branch --show-current 2>/dev/null)
    if [ -z "$(git -C "$DIR" status --porcelain 2>/dev/null)" ]; then
        STATUS="up-to-date"
    else
        STATUS="modified"
    fi
fi

# Colors
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
MAGENTA='\033[35m'
RESET='\033[0m'

if [ "$PCT" -ge 90 ]; then BAR_COLOR="$RED"
elif [ "$PCT" -ge 70 ]; then BAR_COLOR="$YELLOW"
else BAR_COLOR="$GREEN"; fi

# Transcript (session JSON) size
TRANSCRIPT=$(echo "$input" | jq -r '.transcript_path // empty')
SIZE_STR=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    BYTES=$(stat -c%s "$TRANSCRIPT" 2>/dev/null || wc -c < "$TRANSCRIPT" | tr -d ' ')
    if [ "$BYTES" -ge 1048576 ]; then
        SIZE_STR=$(awk "BEGIN {printf \"%.1fMB\", $BYTES/1048576}")
    elif [ "$BYTES" -ge 1024 ]; then
        SIZE_STR=$(awk "BEGIN {printf \"%.1fKB\", $BYTES/1024}")
    else
        SIZE_STR="${BYTES}B"
    fi
fi

# Rate limits
FIVE_H=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
FIVE_H_RESET=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')

# Toolkit version + drift (dedicated segment, independent of cwd). The
# claude-toolkit repo path is written to ~/.claude/claude_toolkit_dir at install
# time (it differs per machine). Show the deployed short-SHA and how many commits
# it is behind origin/main, computed from LOCAL refs only (NO network fetch in the
# statusline), so it reflects the last fetch/pull. Silently skipped if the path
# file is absent or the directory is not a git repo.
TK_SEG=""
TK_DIR=$(cat "$HOME/.claude/claude_toolkit_dir" 2>/dev/null)
if [ -n "$TK_DIR" ] && git -C "$TK_DIR" rev-parse --git-dir > /dev/null 2>&1; then
    TK_SHA=$(git -C "$TK_DIR" rev-parse --short HEAD 2>/dev/null)
    TK_BEHIND=$(git -C "$TK_DIR" rev-list --count HEAD..origin/main 2>/dev/null | tr -dc '0-9')
    [ -z "$TK_BEHIND" ] && TK_BEHIND=0
    if [ "$TK_BEHIND" -ge 10 ]; then TK_COLOR="$RED"
    elif [ "$TK_BEHIND" -ge 1 ]; then TK_COLOR="$YELLOW"
    else TK_COLOR="$GREEN"; fi
    if [ "$TK_BEHIND" -ge 1 ]; then
        TK_SEG="${TK_COLOR}tk:${TK_SHA} ${TK_BEHIND} behind${RESET}"
    else
        TK_SEG="${TK_COLOR}tk:${TK_SHA}${RESET}"
    fi
fi

# Line 1
LINE="${CYAN}${MODEL}${RESET}"
[ -n "$EFFORT" ] && LINE="$LINE ${MAGENTA}(${EFFORT})${RESET}"
[ -n "$BRANCH" ] && LINE="$LINE | ${GREEN}${BRANCH}${RESET} [${STATUS}]"
[ -n "$TK_SEG" ] && LINE="$LINE | ${TK_SEG}"
LINE="$LINE | ${SEG_LABEL} ${BAR_COLOR}[${BAR}]${RESET} ${PCT}%"
[ -n "$SIZE_STR" ] && LINE="$LINE | ${SIZE_STR}"

if [ -n "$FIVE_H" ]; then
    FH=$(echo "$FIVE_H" | jq 'ceil')
    if [ "$FH" -ge 80 ]; then LIM_COLOR="$RED"
    elif [ "$FH" -ge 50 ]; then LIM_COLOR="$YELLOW"
    else LIM_COLOR="$GREEN"; fi
    FIVE_TTL=""
    if [ -n "$FIVE_H_RESET" ]; then
        REMAINING=$((FIVE_H_RESET - NOW))
        if [ "$REMAINING" -gt 0 ]; then
            FIVE_TTL="$(($REMAINING / 3600))h$(($REMAINING % 3600 / 60))m"
        fi
    fi
    LINE="$LINE | ${LIM_COLOR}${FIVE_TTL}: ${FH}%${RESET}"
fi

echo -e "$LINE"
# Line 2: full path (printf %s -- avoid echo -e, which would treat \c, \n etc. in Windows paths as escapes)
printf '%s\n' "$DIR"
