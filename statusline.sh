#!/bin/bash
input=$(cat)
MODEL=$(echo "$input" | jq -r '.model.display_name // "Claude"')
DIR=$(echo "$input" | jq -r '.cwd // "~"')
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)

# Context bar
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

# Color codes
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
RESET='\033[0m'

if [ "$PCT" -ge 90 ]; then BAR_COLOR="$RED"
elif [ "$PCT" -ge 70 ]; then BAR_COLOR="$YELLOW"
else BAR_COLOR="$GREEN"; fi

# Rate limits
FIVE_H=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
FIVE_H_RESET=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
NOW=$(date +%s)

# Line 1: model, git, context, rate limits
LINE="${CYAN}${MODEL}${RESET}"
[ -n "$BRANCH" ] && LINE="$LINE | ${GREEN}${BRANCH}${RESET} [${STATUS}]"
LINE="$LINE | ${BAR_COLOR}[${BAR}]${RESET} ${PCT}%"

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
# Line 2: full path
echo -e "$DIR"
