---
name: basecamp
description: Load the context basecamp (the user's journal and control tower at ~/basecamp) holds about the current repo — open threads, promises and deadlines, who asked, recent decisions, clinic and schema facts, gotchas. Use at the start of work in any repo other than basecamp, or when the user asks what basecamp knows, what's open, or what was decided about this repo.
argument-hint: "[topic to focus on, e.g. 서울메이 or HIRA]"
---

# Context from basecamp

basecamp lives at `~/basecamp` (Windows `C:\Users\user\basecamp`, Mac `~/basecamp`). This skill only **reads** it.
basecamp collects work from other repos itself with its own `/catchup`, so don't write to it from here.

1. `git -C ~/basecamp pull -q`. If that fails, read what's on disk and say it may be stale.
2. Work out what this session is about:
   - the current repo: its folder name and `git remote -v`
   - `$ARGUMENTS`, if given, narrows it further (a clinic, a topic)
3. Read these from `~/basecamp` and keep only the parts about this repo, its clinics and schemas, or the topic:
   - `repos.md`: this repo's row (purpose, notes)
   - `clinics.md`: the clinics and schemas this repo migrates or serves
   - `threads.md`: matching threads in Active, Waiting on others, and recent Done
   - `journal/<YYYY-MM>/`: today's file and the two before it. Log lines about this repo, its clinics or the topic, and the Tomorrow lists
   - `incidents.md`, and `handover/overview.md` (the section for this repo), only if they mention it
4. Reply with a short brief, about 15 lines at most:
   - **Open here:** threads, next steps, who asked, deadlines or promises (with dates)
   - **Recent:** what was done or decided lately, with dates
   - **Watch out:** gotchas and warnings, e.g. `chart_db_stage` is 운영, or "dev selmai currently holds 명치과"
   - Cite the basecamp file for each point, as a full path
5. If nothing in basecamp mentions this repo, say so in one line and stop.

Before changing this repo, still read its own `CLAUDE.md`; basecamp context doesn't replace it.
Never repeat secrets, 주민번호, patient names or phone numbers, even if a basecamp file contains one by mistake.
