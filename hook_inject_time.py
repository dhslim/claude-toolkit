#!/usr/bin/env python3
"""UserPromptSubmit hook — injects current KST timestamp into the turn context.

The injected line is read by a global CLAUDE.md rule that asks Claude to end
each response with this exact timestamp. Stdout from a UserPromptSubmit hook
is appended to the user's prompt as additional context for the model.

Format: <current-time>YYYY-MM-DD(Day) HH:MM:SS KST</current-time>
where Day is the 3-letter abbreviated day of week (Mon, Tue, ..., Sun).
"""

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

if __name__ == "__main__":
    now = datetime.now(KST)
    # %a = abbreviated weekday name (Mon, Tue, Wed, Thu, Fri, Sat, Sun)
    stamp = now.strftime('%Y-%m-%d(%a) %H:%M:%S')
    print(f"<current-time>{stamp} KST</current-time>")
