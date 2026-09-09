# Quiz System — Considerations & TODOs

## Current Behavior
- `quiz_check.py` runs on the **Stop hook** after every Claude response
- Uses `{"decision": "block", "reason": "..."}` to inject quiz instructions
- Checks `stop_hook_active` to prevent infinite loops
- Detailed instructions written to a temp file; user sees a short one-liner
- Quiz dismissed via explicit user request ("skip quiz") or completion

## Issues Observed

### 1. Stop hook fires repeatedly
The block fires after **every** response until the quiz is taken or dismissed. If the user is chatting without addressing the quiz, they see "Stop hook error" on every turn. This is noisy.

**Options:**
- **A) "Shown in this session" guard** — block once per session, then exit 0 on subsequent calls. Quieter, but Claude might forget about the quiz after the first block.
- **B) Non-blocking approach** — use `systemMessage` instead of `decision: "block"`. Shows a reminder without forcing Claude to act. Less intrusive but easier to ignore.
- **C) Hybrid** — block on the first Stop, then switch to `systemMessage` reminders on subsequent ones.

### 2. Claude can still ignore the block
Even with `decision: "block"`, Claude got sidetracked by user questions in testing. The block gives Claude another turn, but doesn't guarantee it will prioritize the quiz.

### 3. SessionStart vs Stop tradeoffs
- **SessionStart**: stdout is injected into context directly, but doesn't cover overnight/resumed sessions where the day rolls over.
- **Stop**: covers all cases (guaranteed to fire), but requires JSON `decision/block` format and creates repeated noise.
- **Current choice**: Stop only. SessionStart was removed.

### 4. Multiple quizzes saved per day
If the user restarts sessions without completing the quiz, a new quiz agent is launched each time. Each save creates a new MongoDB document (by design — "never overwrites"). Could accumulate unused quizzes.

**Options:**
- Check if a quiz was already generated today before launching the agent
- Reuse the most recent saved quiz instead of generating a new one

### 5. "Stop hook error" label in UI
Claude Code labels the blocking response as "Stop hook error" which looks like something went wrong. It's actually working as intended — just misleading UX. This is a Claude Code UI thing, not something we control.

### 6. Background agent timing
The quiz agent takes ~50-60 seconds to generate and save. During this time, every response triggers the Stop hook block again. The user sees repeated "quiz pending" messages while waiting.

**Options:**
- Cache quiz results so subsequent sessions don't regenerate
- Pre-generate quiz via cron (uses Anthropic API separately)

### 7. Question quality issues
- **Ambiguous wording**: e.g. "discovering repos" could mean finding repos in general vs how the daily report locates them. Questions need clearer context.
- **Stale information**: quiz is based on yesterday's conversations, but decisions discussed yesterday may have already been superseded today. e.g. a question about the platform/ directory structure is outdated after switching from Node.js to Python.

**Options:**
- Improve the quiz generation prompt to require precise, unambiguous phrasing
- Add context to each question (e.g. "In the context of the daily report workflow...")
- Accept that some questions will reference outdated decisions — that's inherent to a "yesterday's conversations" quiz

## Decisions Already Made
- **Dismissed vs Shown**: replaced auto "shown" marker with explicit "dismissed" — quiz keeps showing until user actively takes or skips it
- **Stop-only**: removed quiz from SessionStart hook since Stop is guaranteed to fire
- **Short reason**: user sees one-liner, Claude reads full instructions from file
- **stop_hook_active guard**: prevents infinite loops when Stop hook blocks
- **Global markers**: taken/dismissed stored in MongoDB (`quiz-markers` collection) with local files as fast cache. One MongoDB read per day per machine, rest served from cache.
