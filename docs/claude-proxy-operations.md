# Claude Proxy — Operations, Robustness, and What "Just Works" Really Means

This document is a learning-focused record of how the proxy is wired into the
daily Claude Code workflow, which failure modes are real, what the fragility
points are, and what hardening options exist at what cost. It was written on
2026-04-14 after the Option 5 sliding-window design landed in commit `fec7ffc`.
The motivation: the proxy has become load-bearing for big sessions, and
"load-bearing" is the moment you owe it the operational rigor you'd give any
critical path piece of infra.

If you're reading this cold, start with
[`claude-proxy-design.md`](claude-proxy-design.md) for the historical design
arc and [`claude-proxy.md`](claude-proxy.md) for user-facing setup. This doc
is about what happens AFTER it's installed: how it survives reboots, what
breaks it silently, and how you'd notice.

---

## The core question this document answers

> *"If I opened a fresh directory and started Claude Code on it, would
>  everything just be set up properly like magic?"*

The short answer is **yes on this machine, within a single PowerShell
invocation, in any directory**. The long answer involves walking through the
chain of things that have to be true for "yes" to hold, and understanding
which links in that chain are robust and which are fragile.

---

## The install chain, step by step

Every time you type `claude` in a terminal, a chain of events has to succeed
before your request reaches Anthropic. Understanding this chain is prerequisite
to understanding where it can break.

1. **PowerShell loads `$PROFILE`** at terminal open. This runs whatever is in
   `Microsoft.PowerShell_profile.ps1` for your user account.

2. **The profile defines a `claude` function** (written by
   `install_claude_proxy.py`). This function wraps the real `claude.exe` so
   that every invocation of `claude` goes through it first.

3. **You type `claude`.** The function runs.

4. **The wrapper checks if `localhost:9999` is listening.** This is done via
   a `Test-NetConnection` or equivalent — a fast port check.

5. **If port 9999 is dead, the wrapper starts the proxy** in a hidden
   background process using `Start-Process -WindowStyle Hidden python
   claude_proxy.py`. The proxy runs under whichever `python` is first in your
   PATH and reads `claude_proxy.py` from the claude-toolkit directory.

6. **The wrapper sets `$env:ANTHROPIC_BASE_URL="http://localhost:9999"`** —
   but only for the function's child process. This does NOT pollute the
   parent terminal's environment.

7. **The wrapper invokes the real `claude.exe`** via `& claude.exe @args`,
   passing through whatever arguments you gave.

8. **Claude Code reads `ANTHROPIC_BASE_URL`** and sends all API requests to
   `localhost:9999` instead of `api.anthropic.com`.

9. **The proxy intercepts each request**, runs the sliding-window trim logic,
   and forwards the mutated body to `api.anthropic.com`.

10. **Anthropic processes the request** (with prompt cache, 20-block lookback,
    etc.) and streams the response back through the proxy to Claude Code to
    your terminal.

**Every one of these 10 steps is a potential failure point.** The rest of this
document enumerates which ones are rock-solid, which are fragile, and what you
can do about the fragile ones.

---

## What truly works automatically

These are the "just works like magic" parts. You don't think about them
because they're reliable enough to be invisible.

### Per-terminal invocation

Opening a new PowerShell window and typing `claude` succeeds regardless of:
- Which directory you're in (the wrapper is directory-agnostic)
- Whether a previous terminal is still open
- How many parallel Claude Code sessions are running
- Whether the proxy was previously running (wrapper auto-starts if not)

This is the "fresh directory" case the question was about. It works because
the wrapper is defined globally in `$PROFILE`, runs on every new PowerShell,
and handles proxy bootstrapping idempotently.

### Per-directory workflow

Claude Code doesn't care about cwd when talking to the API — the proxy doesn't
either. You can `cd` anywhere, start a session, and the trim logic operates
identically. The only thing `cwd` affects is Claude Code's "project directory"
view, which is orthogonal to the proxy's concerns.

### Post-reboot recovery

After a Windows reboot, the proxy is NOT running (it's not in Task Scheduler).
But the first `claude` command you run in any terminal detects `port 9999`
is dead, auto-starts the proxy, and proceeds. You might notice a ~2-second
delay on the very first command after boot; you wouldn't notice subsequent
commands.

### Transparency to Claude Code

Claude Code has no knowledge the proxy exists. It only sees
`ANTHROPIC_BASE_URL=http://localhost:9999` and treats that as its API endpoint.
This is by design: Claude Code's behavior is unchanged. No configuration
inside Claude Code is required. No plugins, no hooks, no special flags. The
proxy is completely invisible from Claude Code's perspective, which is why it
doesn't need to be kept in sync with Claude Code updates at the level of
*interaction* (only at the level of *contract*, which is much more stable).

---

## Fragility catalog

These are the parts that CAN break, ranked roughly by likelihood and severity.
The point of enumerating them isn't to be paranoid — it's to know what to
check first when something goes wrong.

### 1. Cross-machine: does NOT just work on a different computer

**Severity: HIGH for multi-machine users. LOW if you only use one machine.**

The install is per-machine. On a fresh Windows box, laptop, or remote dev box,
none of this exists:
- The `$PROFILE` edit
- The `claude_proxy.py` file
- The Python venv with `httpx`, `fastapi`, `uvicorn`
- Windows networking rules allowing localhost binds

You'd need to re-run the installer on each machine. The installer itself
(`install_claude_proxy.py`) is not currently packaged for one-command
cross-machine install.

**Symptom of this failure**: you open Claude Code on a different computer, hit
the context cap mid-session, and get `Request too large` errors with no
explanation. The proxy isn't there to trim for you.

**Detection**: check `netstat -ano | findstr :9999` on the target machine. If
nothing is listening, the proxy isn't installed.

**Fix**: re-run `.venv/Scripts/python.exe install_claude_proxy.py` (or `.venv/bin/python` on macOS/Linux) on the target machine. See
[Hardening option D](#d-cross-machine-installer-bundle) for making this easier.

### 2. Proxy venv path is hardcoded

**Severity: MEDIUM. Rare but silent.**

The wrapper starts the proxy via a hardcoded python command. If that python
doesn't have the required dependencies (`httpx`, `fastapi`, `uvicorn`), the
proxy fails to start, the wrapper silently falls back to direct claude, and
you don't notice until you hit the cap.

**Symptom**: `proxy.err.log` contains import errors. Claude Code works but
doesn't benefit from the proxy — over-cap sessions fail.

**Detection**: `proxy.err.log` is small when healthy. If it's bigger than a
few hundred bytes, something is wrong at startup.

**Fix**: re-create the venv and update the wrapper's python path.

### 3. Mid-session proxy crash

**Severity: MEDIUM. Forces manual recovery.**

If the proxy process dies while you're in the middle of a Claude Code session
(uncaught exception, OOM, whatever), the next request from Claude Code gets
`ConnectionRefused`. The wrapper only re-launches the proxy when you run
`claude` FRESH — it does not watchdog a running proxy.

**Symptom**: mid-conversation `ConnectionRefused` error. You haven't typed
anything unusual; it just suddenly fails.

**Detection**: the claude terminal itself shows the connection error. Checking
`netstat -ano | findstr :9999` confirms nothing is listening.

**Fix**: close the terminal, open a new one, run `claude` (wrapper re-starts
proxy), resume the session.

### 4. `$PROFILE` reset

**Severity: HIGH (invisible).**

If Windows resets your PowerShell profile (user profile repair, PowerShell 7
vs 5 divergence, manual cleanup, Windows Update weirdness), the `claude`
wrapper function disappears. Your `claude` command still works — it just runs
real claude directly — but you lose the proxy benefit and **don't know it's
gone**.

**Symptom**: big sessions start hitting `Request too large` errors again. Proxy
is (probably) still running in the background from a previous terminal, but
your new terminals aren't routing to it.

**Detection**: in a fresh PowerShell terminal, run `Get-Command claude`. If
the output shows `claude` as an Application (claude.exe), the wrapper is
missing. If it shows as Function, the wrapper is loaded.

**Fix**: re-run `.venv/Scripts/python.exe install_claude_proxy.py` (or `.venv/bin/python` on macOS/Linux) to restore the wrapper.

### 5. Claude Code version drift

**Severity: LOW today, could become HIGH with a future Claude Code release.**

We depend on two contracts that Anthropic could change:

1. **`ANTHROPIC_BASE_URL` env var** — the official mechanism for redirecting
   Claude Code's API requests. Very likely to stay supported (it's the standard
   OpenAI/Anthropic SDK env var). Low risk.

2. **`CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`** — an internal, undocumented env
   var we use to bypass Claude Code's *client-side* token blocker. Higher risk.
   Anthropic could rename or remove this in any release. See
   [`claude-proxy-design.md`](claude-proxy-design.md) for the backstory on
   how we found it.

**Symptom of contract drift**: after a Claude Code update, everything
that worked before suddenly doesn't. Hard to diagnose because the code on your
machine didn't change — the behavior Claude Code expects did.

**Detection**: watch release notes for Claude Code. If a future version
removes the bypass, re-grep the bundled binary for equivalents.

**Fix**: re-investigate how the new Claude Code version decides what to send,
update the proxy to match.

### 6. Session anchor state lost on proxy restart

**Severity: LOW (by design).**

The sliding-window anchor (`_session_watermarks`) lives in the proxy process's
RAM. When the proxy restarts for any reason — crash, reboot, you killing it —
the dict is wiped. Every previously-shifted session starts over with
`prior_anchor = 0`, meaning the next over-threshold request computes a fresh
shift and pays the 3–5 min re-prefill cost.

**Symptom**: after a proxy restart, the first big request on any previously-
shifted session is slow (3–5 min) even though "nothing changed." Subsequent
requests on that session return to fast.

**Detection**: in `proxy.log`, look for `anchor 0→N` after a process restart
where the session was previously at `anchor M` (M > 0). That's one unnecessary
shift cost per session per proxy restart.

**Fix**: see [Hardening option C](#c-persistent-anchor-state). Not urgent
unless you're restarting the proxy frequently.

### 7. Tools array mutation silently invalidates cache

**Severity: LOW to MEDIUM. Hard to detect.**

Claude Code caches the `tools` field via `system[2]`'s breakpoint. If any Claude
Code feature dynamically adds or removes a tool mid-session — MCP server
reconnect, slash command hot-reload, new skill installation — the tools bytes
change, `system[2]`'s snapshot is invalidated, and the cache cascade is:
`tools → system → messages`, so EVERYTHING after the tools breakpoint becomes
a cache miss on the next request.

**Symptom**: unexpectedly slow turn in a session that was running fast. No
visible cause in the proxy log (our shift didn't fire; the cache just silently
missed upstream).

**Detection**: in the response's `usage` field, `cache_creation_input_tokens`
suddenly jumps up for a single turn even though no shift fired. This is
Anthropic writing a new snapshot because the old one couldn't match.

**Fix**: nothing we can do proxy-side. Accept the occasional slow turn.
Long-term: avoid reloading MCP servers mid-session if you care about cache
health.

---

## Hardening options, rated by cost and reward

These are the things you COULD do to make the proxy more robust, but haven't
yet (as of `fec7ffc`). Each is evaluated on effort required vs. reliability
improvement.

### A. Task Scheduler auto-start at login

**Cost**: ~5 minutes of Windows Task Scheduler config.

**Reward**: proxy is running 100% of the time your user is logged in. The
"first claude command after boot has a ~2s delay" disappears. Most crucially,
if the proxy crashes, you see a process-exit notification in Task Scheduler
instead of discovering the outage when `ConnectionRefused` errors start showing
up.

**Setup** (once):
1. Open Task Scheduler
2. Create Task → Triggers → At log on → Specific user (your user)
3. Actions → Start a program →
   `C:\Users\user\Desktop\dentweb-migration\.venv\Scripts\python.exe`
4. Arguments: `C:\Users\user\Desktop\claude-toolkit\claude_proxy.py`
5. Settings → "Run task as soon as possible after a scheduled start is missed"
   → OFF (we only want it at logon)
6. Settings → "If the task fails, restart every" → 1 minute → up to 3 attempts

**Downside**: doesn't handle mid-session crashes (Task Scheduler doesn't
auto-restart a running task that dies, only missed triggers). For that, see
option B or use a supervisor like NSSM.

**Recommendation**: **Do this.** It's the highest-leverage hardening step.

### B. Startup healthcheck in the wrapper

**Cost**: ~20 lines of PowerShell + ~10 lines of Python.

**Reward**: catches the "port 9999 is listening but the proxy is actually
broken" case (e.g., process hung, can't parse requests, auth forwarding
broken). The wrapper sends a trivial `GET /health` to the proxy before
trusting it. If the healthcheck fails, the wrapper kills the stale process
and restarts.

**Implementation sketch**:
1. Add a `GET /health` endpoint to `claude_proxy.py` that returns
   `{"ok": true, "anchors_tracked": len(_session_watermarks)}` without
   forwarding anything upstream.
2. In the wrapper function, after the port check, curl the healthcheck.
   If it fails, kill the old PID (or `Stop-Process -Id ...`) and launch fresh.

**Downside**: adds ~500ms to every `claude` invocation (healthcheck round-trip
plus PowerShell overhead). Not a big deal interactively but adds up if you're
launching claude in a script.

**Recommendation**: nice-to-have, not urgent. Adds it only if you see stale-
proxy failures in practice.

### C. Persistent anchor state

**Cost**: ~20 lines of Python. Single JSON file on disk.

**Reward**: `_session_watermarks` survives proxy restarts. No "one unnecessary
slow turn per session per proxy restart" cost. Better post-reboot experience
on long-running sessions.

**Implementation sketch**:
1. Add a module-level `_STATE_FILE = Path(__file__).parent / "proxy-state.json"`.
2. On startup, load `_session_watermarks` from the file if it exists.
3. On every update to `_session_watermarks`, serialize the whole dict and
   write it atomically (`write to .tmp, rename over the real file`).
4. Add `.gitignore` entry for `proxy-state.json`.

**Downside**: one more file to track. Occasional stale entries from old
sessions that no longer exist. A bug in the serializer could persist a bad
state across restarts.

**Recommendation**: skip until you see the "extra slow turn after restart"
cost becoming annoying. The LRU bound (`_MAX_SESSIONS = 100`) keeps the file
small regardless.

### D. Cross-machine installer bundle

**Cost**: ~1 evening of hardening work.

**Reward**: `curl https://... | python` (or equivalent) on any Windows box
sets up the proxy + wrapper + venv in one command. Makes remote dev boxes
(`dentium-chart-client-dev`, `dentium-chart-server-dev`, etc.) first-class
proxy citizens. Biggest usability win if you do multi-machine work.

**Implementation sketch**:
1. Make `install_claude_proxy.py` self-contained: detect/create a venv,
   `pip install` dependencies, find `$PROFILE`, write the wrapper, optionally
   register Task Scheduler.
2. Bundle into a single downloadable script or commit to the repo with a
   clear install command.
3. Consider a Linux/macOS path too if your dev boxes are non-Windows.

**Downside**: significant work. Installer scripts are their own category of
fragility (they have to handle weird machine configurations, PowerShell
versions, antivirus interference, Python not being in PATH, etc.).

**Recommendation**: **Do this** if you use Claude Code on remote dev boxes
more than a few times a month. Otherwise defer.

### E. Visible proxy status indicator

**Cost**: ~30 lines via a Claude Code hook.

**Reward**: at-a-glance confirmation that the proxy is healthy every time you
start a Claude Code session. Prevents the "silent degradation" failure mode
(case 4 in the fragility catalog above).

**Implementation sketch**:
1. Use Claude Code's `UserPromptSubmit` hook or similar.
2. On each prompt, the hook checks `netstat -ano | findstr :9999` (or curls
   the healthcheck if option B is done).
3. Hook adds a status line to the displayed prompt: "[proxy: OK]" or
   "[proxy: MISSING — falling back to direct API]".

**Downside**: adds a small amount of visual noise every prompt. Requires the
Claude Code hook infra to be stable (which it is).

**Recommendation**: nice. Low priority but a good first-line-of-defense for
detecting silent failures.

---

## The idempotent / self-configuring / hermetic framework

Here's a useful mental model for judging how robust any infra tool is. I use
this scale to rate claude-toolkit against itself over time.

**Idempotent**: running the setup multiple times produces the same result.
You can re-run `install_claude_proxy.py` a second time and nothing breaks;
it just no-ops or updates in place.

**Self-configuring**: no manual steps. The setup discovers its own
prerequisites, makes its own decisions, and requires no user input beyond
"run this."

**Hermetic**: no dependencies on ambient environment state. The tool doesn't
break if your PATH is weird, your env vars are polluted, your registry keys
are missing. It brings everything it needs.

A tool scores 0–10 on each axis. True 10/10 across all three is basically
impossible outside sealed commercial products.

### Current proxy score (as of `fec7ffc`)

| Axis | Score | Why |
|---|---|---|
| **Idempotent** | 9/10 | `install_claude_proxy.py` handles re-runs cleanly. Minor ding for "if you edit `$PROFILE` manually in between, it might duplicate entries." |
| **Self-configuring** | 6/10 | Requires you to know where Python is, know that the venv has deps, know about `$PROFILE`, know about Task Scheduler (if you want option A). Many implicit prerequisites. |
| **Hermetic** | 5/10 | Depends on an external Python install with specific packages. Depends on PowerShell. Depends on `$PROFILE` being writable. Not self-contained. |
| **OVERALL** | **7/10** | Better than most hobby tools, worse than commercial desktop apps. |

### What would move the score up

- **Installer bundle** (option D) → +1 on all three axes
- **Python bundled with install** (e.g., via PyInstaller) → +2 on hermetic
- **Task Scheduler auto-registration in installer** (option A) → +1 on
  self-configuring
- **Healthcheck endpoint + wrapper uses it** (option B) → +0.5 on hermetic

A realistic near-term ceiling is about 9/10 (idempotent 10, self-config 9,
hermetic 7). True 10 across all three would require shipping a signed binary,
which is beyond the scope of a personal toolkit.

---

## How to notice when something is broken

The hardest failure modes are the silent ones. Here's a checklist of
signals that tell you the proxy isn't healthy:

### Fast checks (under 30 seconds)

1. **`netstat -ano | findstr :9999`** — is anything listening? If no, proxy
   is dead.
2. **`Get-Command claude`** — is `claude` a Function or an Application? If
   Application, wrapper is missing from `$PROFILE`.
3. **Tail `proxy.log`** — are there recent entries? If the newest entry is
   from yesterday but you've used Claude Code today, your requests aren't
   going through the proxy.
4. **Check `proxy.err.log`** — if it's bigger than a few hundred bytes,
   something is crashing at startup.

### Slower checks (30 seconds to a few minutes)

5. **Start a tiny test session**: open claude with a throwaway request,
   watch the proxy log for the request appearing. If you don't see it, your
   claude isn't routing to the proxy.
6. **Check `usage` in a response**: if `cache_read_input_tokens` is 0 on a
   big session where you'd expect cache hits, something is breaking the byte-
   prefix stability. Could be a proxy bug (recent change to trim logic) or
   upstream (Claude Code changed how it constructs bodies).

### Passive monitoring

7. **Ambient slowness**: if big sessions feel noticeably slower than
   yesterday, the cache probably isn't hitting. Before debugging anything
   else, check the usage numbers.
8. **Unexpected `ConnectionRefused`**: always means the proxy is either
   dead or unreachable from the current terminal. Not a "maybe" — it's
   definitive.

---

## Philosophy — why this matters for a personal tool

You might reasonably ask: "this is a personal toolkit, why write a whole
operations doc for it?"

Two reasons:

**1. It's load-bearing.** The proxy went from "optional nice-to-have" to
"required for my workflow to function" in about two hours of real usage.
Once something is load-bearing, it deserves the rigor of load-bearing infra
— not because it's complicated, but because when it breaks you'll be
stressed, probably mid-thought, and you won't want to re-derive how it all
fits together. Writing it down once, when you're calm, is dramatically
cheaper than rediscovering it when you're not.

**2. The learning is the point.** This entire design journey — fix 1 → fix 2
→ option 3 → option 4 → option 5 → the probe investigation → the docs
reading — represents a huge amount of hard-won understanding about how
Anthropic's API, Claude Code's internals, and prompt caching actually interact.
That understanding is the thing that's actually valuable, more valuable than
the ~800 lines of Python we wrote at the end. If you ever want to build
another LLM-adjacent tool, or teach someone else how caching works, the
ideas documented here are directly transferable. The code is specific; the
mental model is general.

Documentation in a learning-first workflow isn't "what do we need to run
this" — it's "what did we learn by building this, and how do we make that
available to future us." That's a different standard.

---

## Meta-observations about infrastructure engineering

A few things that came up in this design journey that generalize beyond
prompt caching:

### "Byte stability" is a surprisingly sharp concept

Anthropic's cache matches on byte-identical prefixes, and that single
constraint drove every design decision we made. The lesson isn't about
caching specifically — it's that **when a system's invariants are physical
(bytes, positions, order) rather than semantic (intent, content, meaning),
all your abstractions have to respect the physical invariant even when it
feels pedantic**. Renaming a variable doesn't break semantic equivalence; it
does break byte identity. A middleware that "just strips a stale field from
the JSON" doesn't change meaning; it does change bytes. Infrastructure that
looks at bytes is brutally literal.

### Ratchets are everywhere and worth naming

We invented (well, reinvented) the ratchet pattern three different times in
this design — first for "drop oldest N" (head ratchet), then for "drop middle
groups" (middle ratchet), then for "anchor position" (sliding window anchor).
Each time the pattern was the same: a variable that moves monotonically in
one direction, guaranteeing an invariant that makes some downstream piece
(in our case, the cache) trustable. Once you have the word, you recognize
the pattern everywhere: database sequence numbers, Lamport clocks, Signal
protocol keys, Git commit IDs. Monotonic state is an underrated primitive.

### The first shift teaches you the steady state

Much of our debugging went into "what does the first shift look like?"
because every subsequent shift is just a repeat. The steady-state behavior
of a stateful system is usually simpler than the first-time bootstrap. For
our proxy, once the first shift has happened on a session, every subsequent
turn runs at ~99% cache hit until the next shift (~100–250 turns later).
The first shift is where bugs live, the steady state is where confidence
lives. When debugging new stateful systems, focus your effort on the first
transition; everything else tends to follow.

### Documentation IS the artifact for learning-first work

Traditional infrastructure documentation says "here's how to run it, here
are the knobs." Learning-first documentation says "here's how we got to
the design we got to, what we considered and rejected, and what the
mental model is." This doc is written in the second style because that
style is what makes future-you (or future-teammate-you) competent faster.
If I'd written this doc in the first style, it would be half as long and
twice as useless six months from now when you're trying to remember why
SHIFT_RATIO is 0.5 and not 0.3.

---

## Changelog

- **2026-04-14** — Initial version. Written after commit `fec7ffc` landed
  the Option 5 sliding-window design. Captures what "just works" means as of
  today, known fragility points, and hardening options.
