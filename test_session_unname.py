#!/usr/bin/env python3
"""Regression tests for the two fixes that guard a DESTRUCTIVE operation.

WHAT IS UNDER TEST
    session_unname.py     (da74cac, 3fe230f) — liveness must fail CLOSED, on the
                          gate (--verify) AND on the command that edits files.
    strip_session_images.py (6630c0b) — only "SYNC OK" authorizes destroying the
                          local image payloads.

    Both tools destroy data that has no other copy: a stripped transcript is
    rewritten in place, and the hook that drives the image strip hard-codes
    --no-backup (hook_strip_images.py:72). So the assertions here are mostly of
    the shape "it REFUSED, and every byte on disk is still where it was" — the
    refusal is the feature.

WHAT IS DELIBERATELY NOT ASSERTED
    Report prose, column widths, the STEP 1-5 layout, backup filename timestamps,
    the compact separators the job state is rewritten with. /unbadge consumes the
    EXIT CODE, not stdout (session_unname.py:107-118), so locking the wording
    would turn every cosmetic edit into a red build.

    ONE EXCEPTION, and it is load-bearing: the refusal REASON on stderr. Three
    different conditions refuse — unreadable record, blind probe, live holder —
    and 3fe230f renumbered the live-holder refusal from 2 to 1, so a refusal for
    the WRONG reason can land on the right exit code and turn a test green on
    broken code (see the two "only evidence" tests below, which exist because
    that is not hypothetical). Those tests therefore assert one short, stable
    fragment of the stderr line — the unreadable file's own path, or the words
    'could not probe running processes' — and never the sentence around it.

HOW ISOLATION WORKS — READ THIS BEFORE ADDING A TEST
    Both modules compute their store paths at IMPORT time from Path.home()
    (session_unname.py:88-91, strip_session_images.py:26-28). There is no env-var
    seam, and scrubbing USERPROFILE does not help: Path.home() runs while the
    module is still loading. The only lever is to reassign the module attributes
    after import, which works because every function reads them as globals at call
    time. All FOUR session_unname constants must be reassigned individually —
    CLAUDE_PROJECTS/SESSIONS/JOBS are derived from CLAUDE_HOME once, at import, so
    patching CLAUDE_HOME alone silently tests against the real one. Same for
    strip_session_images.SYNC_LOG, which is derived from __file__ rather than from
    home and therefore survives every home-based trick.

    FakeHomeTest.setUp does all of it and then asserts the result, so a future
    edit that forgets one constant fails on the guard instead of walking the
    user's real sessions.

RUN (from the repo root)
    .venv\\Scripts\\python.exe -m unittest test_session_unname -v

    Stdlib unittest, no dependencies: this repo pins nothing (pyproject.toml has
    no dependencies and there is no lockfile), so a suite that needed pytest
    would be a suite that silently cannot run — which is the exact failure mode
    these two tools exist to prevent.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import session_unname as su            # noqa: E402
import strip_session_images as ss      # noqa: E402

# The real values, captured before any test can patch them. Everything below
# compares against these rather than recomputing Path.home(), so the residue
# audit is checking the same objects the modules actually shipped with.
REAL_SU = {name: getattr(su, name) for name in
           ('CLAUDE_HOME', 'CLAUDE_PROJECTS', 'CLAUDE_SESSIONS', 'CLAUDE_JOBS')}
REAL_SS = {name: getattr(ss, name) for name in ('SYNC_LOG', 'CLAUDE_PROJECTS')}
REAL_PROCESS_MAP = su.process_map

# Obviously-fake identifiers. Nothing here may ever collide with a real session:
# the 999 prefix makes a stray file recognisable at a glance if one ever escapes.
SID = '99900000-0000-4000-8000-000000000001'
SID8 = SID[:8]
FAKE_PID = 999001
FAKE_SLUG = 'C--fake-fixture-project'

# Files whose presence anywhere under a real store means a test wrote where it
# must not have. The audit at the bottom compares these against a baseline taken
# before the suite ran, so pre-existing leftovers from a real /unbadge run do not
# masquerade as this suite's residue.
DANGEROUS_GLOBS = ('*.bak-unname-*', '*.jsonl.tmp', '*.json.tmp', '*.jsonl.bak')

_TEMP_DIRS: list[Path] = []
_BASELINE: dict = {}


# ---------- fixture construction ----------
#
# Every fixture is built by json.dumps or Path.write_text. Hand-authoring JSON
# inside a shell heredoc is how this repo previously produced a corrupt fixture
# and a false PASS: the escaping was wrong, the tool skipped the line as
# unparseable, and the test asserted on a file that did not contain what it
# claimed. A serializer cannot make that mistake.

def write_jsonl(path: Path, records) -> None:
    """Write a transcript. dicts/lists are serialized; a str is written verbatim.

    The verbatim escape hatch exists because some of the most important fixture
    lines are ones a JSON serializer cannot produce from a dict — a bare scalar,
    or a line that is not JSON at all. Those are the grep trap.
    """
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
             for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def named_transcript_records(sid: str):
    """Two real name entries, plus four lines that only LOOK like them.

    The decoys are the point. `grep -c custom-title` over a transcript counts
    every message in which the words were merely discussed, which during the
    original investigation produced a false "the process re-stamps every turn"
    conclusion (session_unname.py:45-49). A strip driven by that count would
    delete real conversation.
    """
    return [
        {'type': 'user', 'sessionId': sid,
         'message': {'role': 'user', 'content': 'hello'}},
        # real #1
        {'type': 'custom-title', 'customTitle': 'my-badge', 'sessionId': sid},
        # decoy: a genuine message whose TEXT discusses both entry types
        {'type': 'assistant', 'sessionId': sid,
         'message': {'role': 'assistant',
                     'content': 'grep -c custom-title also counts agent-name '
                                'mentions, which is why this parses JSON'}},
        # real #2
        {'type': 'agent-name', 'agentName': 'my-badge', 'sessionId': sid},
        # decoy: valid JSON, but an array — .get() would explode on it
        ['custom-title', 'agent-name'],
        # decoy: valid JSON, but a bare scalar
        '"custom-title"',
        # decoy: not JSON at all
        'NOT JSON: custom-title agent-name',
        {'type': 'summary', 'summary': 'done', 'sessionId': sid},
    ]


REAL_NAME_ENTRIES = 2      # custom-title + agent-name
DECOY_LINES = 4            # prose, array, scalar, non-JSON
TOTAL_LINES = 8


def job_state_obj(sid: str):
    """A daemon-backed job state: the two name keys plus keys that must survive.

    Store 3 is what made two otherwise-correct attempts fail silently — it
    re-applies the name on respawn, so a transcript-only strip looks like it
    worked until the job comes back wearing its badge (session_unname.py:26-37).
    """
    return {
        'sessionId': sid,
        'name': 'my-badge',
        'nameSource': 'user',
        'backend': 'local',
        'template': 'default',
        'state': 'idle',
        'lifecycle': 'persistent',
        'respawnFlags': ['--model', 'opus'],
        'labels': {'team': 'toolkit', 'n': 3},
    }


def session_record_obj(sid: str, pid: int):
    return {
        'sessionId': sid, 'pid': pid, 'cwd': 'C:/fake/fixture',
        'name': 'my-badge', 'kind': 'interactive', 'status': 'idle',
        'version': '2.0.0',
    }


def image_transcript_records(sid: str, blob: str):
    """A transcript shaped like a real screenshot turn.

    Two dicts carry type=image: the content block inside the tool_result, and the
    whole toolUseResult. _walk replaces both, so one screenshot turn is two
    stripped blocks — and the toolUseResult's dimensions/originalSize go with it.
    """
    return [
        {'type': 'user', 'sessionId': sid,
         'message': {'role': 'user', 'content': 'take a screenshot'}},
        {'type': 'user', 'sessionId': sid,
         'message': {'role': 'user', 'content': [
             {'type': 'tool_result', 'tool_use_id': 'toolu_01', 'content': [
                 {'type': 'text', 'text': 'screenshot taken'},
                 {'type': 'image',
                  'source': {'type': 'base64', 'media_type': 'image/png',
                             'data': blob}},
             ]}]},
         'toolUseResult': {'type': 'image',
                           'file': {'base64': blob,
                                    'dimensions': {'width': 1280, 'height': 800}}}},
        # a line that is not JSON must survive verbatim, not be dropped
        'NOT JSON — a truncated write from a killed process',
        {'type': 'assistant', 'sessionId': sid,
         'message': {'role': 'assistant', 'content': '스크린샷 확인했습니다'}},
    ]


IMAGE_BLOCKS = 2


# ---------- helpers ----------

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_state(root: Path) -> dict:
    """rel-path -> sha256 for every file under root.

    Comparing the whole tree, not just the file under test, is what makes
    "it refused without touching anything" provable: a stray .bak, a leftover
    .tmp, or a rewritten job state all show up as a diff.
    """
    return {p.relative_to(root).as_posix(): sha256(p)
            for p in sorted(root.rglob('*')) if p.is_file()}


def run_quiet(fn, *args, **kwargs):
    """Call a command function with its (very chatty) report captured.

    The captured text is returned only so a failing assertEqual can show WHY the
    tool decided what it did. No test asserts on the prose.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return rc, out.getvalue(), err.getvalue()


def remove_tree(path: Path) -> None:
    """rmtree with a short retry.

    On Windows a just-killed process can hold its own image file open for a
    moment after exit, and a half-deleted fixture directory is residue.
    """
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.1)
    shutil.rmtree(path)      # final attempt, let the error surface


def dangerous_paths(root: Path, recursive: bool = True) -> set:
    if not root.is_dir():
        return set()
    glob = root.rglob if recursive else root.glob
    return {str(p) for pat in DANGEROUS_GLOBS for p in glob(pat)}


def _entry_names(root: Path) -> list:
    return sorted(p.name for p in root.glob('*')) if root.is_dir() else []


def setUpModule():
    """Baseline the real stores before a single fixture is built."""
    _BASELINE['claude'] = dangerous_paths(REAL_SU['CLAUDE_HOME'])
    _BASELINE['repo'] = dangerous_paths(REPO, recursive=False)
    _BASELINE['sessions'] = _entry_names(REAL_SU['CLAUDE_SESSIONS'])
    _BASELINE['jobs'] = _entry_names(REAL_SU['CLAUDE_JOBS'])


# ---------- base fixture ----------

class FakeHomeTest(unittest.TestCase):
    """A throwaway ~/.claude with one named session in all three stores."""

    def setUp(self):
        self.assertNotEqual(FAKE_PID, os.getpid(),
                            'fixture pid collides with the test runner')

        self.tmp = Path(tempfile.mkdtemp(prefix='claude-toolkit-test-')).resolve()
        _TEMP_DIRS.append(self.tmp)
        self.addCleanup(remove_tree, self.tmp)

        home = self.tmp / '.claude'
        self.projects = home / 'projects'
        self.sessions = home / 'sessions'
        self.jobs = home / 'jobs'
        for d in (self.projects / FAKE_SLUG, self.sessions, self.jobs / SID8):
            d.mkdir(parents=True)

        for name, value in (('CLAUDE_HOME', home), ('CLAUDE_PROJECTS', self.projects),
                            ('CLAUDE_SESSIONS', self.sessions), ('CLAUDE_JOBS', self.jobs)):
            self._patch(su, name, value)
        self.sync_log = self.tmp / 'sync.log'
        self._patch(ss, 'SYNC_LOG', self.sync_log)
        self._patch(ss, 'CLAUDE_PROJECTS', self.projects)
        self._assert_isolated()

        self.transcript = self.projects / FAKE_SLUG / f'{SID}.jsonl'
        write_jsonl(self.transcript, named_transcript_records(SID))
        # The 20s freshness guard (session_unname.py:583, :735) treats a
        # just-written transcript as active, so a fresh fixture can never reach
        # the code under test. Backdate it; tests that want the guard set it back.
        self.backdate(self.transcript)

        self.job_state = self.jobs / SID8 / 'state.json'
        write_json(self.job_state, job_state_obj(SID))

        self.record = self.sessions / f'{FAKE_PID}.json'
        write_json(self.record, session_record_obj(SID, FAKE_PID))

        # Default world: the probe works and reports only this test runner, so
        # the fixture's pid is genuinely absent = genuinely dead.
        self.set_process_map({os.getpid(): Path(sys.executable).name})

    # -- patch plumbing --

    def _patch(self, module, name, value):
        p = mock.patch.object(module, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _assert_isolated(self):
        for name, real in REAL_SU.items():
            now = getattr(su, name)
            self.assertNotEqual(now, real, f'session_unname.{name} still real')
            self.assertTrue(str(now).startswith(str(self.tmp)),
                            f'session_unname.{name} escaped the fixture: {now}')
        for name, real in REAL_SS.items():
            now = getattr(ss, name)
            self.assertNotEqual(now, real, f'strip_session_images.{name} still real')
            self.assertTrue(str(now).startswith(str(self.tmp)),
                            f'strip_session_images.{name} escaped: {now}')

    def set_process_map(self, value):
        """Pin the liveness probe's answer.

        `value` is the dict process_map() should return, or None for "the probe
        failed". Never {}: process_map cannot produce it (both branches end in
        `return pm or None`), and a test asserting on {} would be asserting on an
        unreachable state. The one test that cares about {} says so explicitly.
        """
        self._patch(su, 'process_map', lambda: value)

    def use_real_process_map(self):
        self._patch(su, 'process_map', REAL_PROCESS_MAP)

    # -- fixture manipulation --

    def backdate(self, path: Path, seconds: int = 3600):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def corrupt_a_session_record(self):
        """A record that is not parseable JSON — the mid-write case.

        A record is written by a live process; catching one half-written is the
        normal way to see this, which is exactly why it cannot be skipped.
        """
        bad = self.sessions / '999002.json'
        bad.write_text('{"sessionId": "99900000-0000-4000-8000-0000000',
                       encoding='utf-8')
        return bad

    def remove_the_valid_session_record(self):
        """Delete the fixture's one good record, leaving the session unclaimed.

        Records are per-pid and deleted on exit (session_unname.py:13-16, :62-64),
        so a live session with NO record is the ordinary state, not an exotic one
        — and it is the state in which a fail-closed signal is the only thing
        left between the tool and a running session. While the good record sits
        on disk, broken code can mistake it for liveness evidence and refuse for
        the wrong reason, which looks exactly like refusing for the right one.
        """
        self.record.unlink()

    def revert_args(self, **over):
        ns = argparse.Namespace(force=False, yes=True, no_backup=False)
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    # -- assertions --

    def assertStoresUntouched(self, before: dict):
        after = tree_state(self.tmp)
        self.assertEqual(before, after,
                         'a refusal wrote to disk: the whole point of refusing '
                         'is that nothing changes')

    def assertNameEntries(self, expected: int):
        info = su.scan_transcript(self.transcript)
        self.assertEqual(info['real'], expected)
        return info


# ---------- A. session_unname: liveness must fail closed ----------

class LivenessFailsClosedTest(FakeHomeTest):
    """Every refusal the gate makes, the destructive command must make too."""

    def test_unreadable_session_record_fails_the_gate_closed(self):
        """A record that will not parse makes liveness UNKNOWN, so --verify = 2.

        da74cac: read_session_records() used to swallow a record it could not
        parse. That record can belong to a LIVE session — a half-written one is
        the normal way to meet it — so skipping it let the gate report a false
        all-clear on precisely the state it exists to catch.
        """
        self.corrupt_a_session_record()
        rc, _, err = run_quiet(su.cmd_verify, only=[SID8])
        self.assertEqual(rc, 2, err)

    def test_unreadable_session_record_also_stops_the_destructive_command(self):
        """The command that edits files refuses on the same fact the gate does.

        3fe230f: cmd_report called holders_of(sid) bare, and the default did
        `read_session_records()[0]` — that [0] dropped the unreadable list on the
        floor. So --verify said STOP with exit 2 and `<sid> --yes` stripped
        anyway, judging liveness from exactly the failure the gate was built to
        fail closed on. An operator saw "no live pid claims this session"
        followed by a clean strip, with the corrupt record never mentioned.

        The stderr assertion is not decoration. Exit 2 does not say WHY it
        refused, and the live-holder branch answered 2 as well until this same
        commit renumbered it to 1 — so on reverted code this refusal arrives
        from the wrong branch and the number still matches. Naming the file it
        choked on is what tells the two apart.
        """
        bad = self.corrupt_a_session_record()
        before = tree_state(self.tmp)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 2, err)
        self.assertIn(str(bad), err,
                      'it refused, but not because of the unreadable record')
        self.assertStoresUntouched(before)

    def test_an_unreadable_record_is_refused_when_it_is_the_only_evidence(self):
        """The variant the fixture's own good record was hiding. (3fe230f)

        The test above passes on a FULL revert of 3fe230f, and the reason is
        worth writing down. The reverted cmd_report calls holders_of(sid) bare,
        so pmap is None, so sessions/999001.json — a perfectly VALID record
        naming this session — comes back as a phantom holder with
        alive='unknown' and survives the filter. cmd_revert then refuses via the
        live-holder branch, whose exit code was 2 before this commit moved it to
        1. Two defects cancel into a green assertion, and assertStoresUntouched
        cannot see it: the broken code really did write nothing, because the
        fixture never let it reach the state where it would.

        Take the good record away and there is no phantom to hide behind — the
        unreadable one is then the only thing on disk that knows anything about
        liveness, and it is the case that actually strips. Shipped: exit 2,
        every byte where it was. With the unreadable list dropped: exit 0, both
        name entries gone from the transcript, the job state's name removed, and
        nothing on stderr.
        """
        self.remove_the_valid_session_record()
        bad = self.corrupt_a_session_record()
        before = tree_state(self.tmp)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 2, err)
        self.assertIn(str(bad), err,
                      'it refused, but not because of the unreadable record')
        self.assertStoresUntouched(before)

    def test_blind_process_probe_fails_the_gate_closed(self):
        """A probe that could not run proves nothing, so --verify = 2. (da74cac)"""
        self.set_process_map(None)
        rc, _, err = run_quiet(su.cmd_verify, only=[SID8])
        self.assertEqual(rc, 2, err)

    def test_blind_process_probe_also_stops_the_destructive_command(self):
        """3fe230f: the bare holders_of() call also left pmap None, so the
        destructive path never probed a pid at all — it decided liveness from
        the mere presence of a record, the mirror of the mistake the module
        docstring warns about. Now it refuses, loudly, with the gate's own 2.

        Same caveat as the unreadable-record test: on reverted code the fixture's
        valid record refuses this for the wrong reason at the same exit code, so
        the stderr fragment is what makes the assertion mean what it says.
        """
        self.set_process_map(None)
        before = tree_state(self.tmp)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 2, err)
        self.assertIn('could not probe running processes', err,
                      'it refused, but not because the probe was blind')
        self.assertStoresUntouched(before)

    def test_a_blind_probe_is_refused_when_it_is_the_only_evidence(self):
        """The same blind spot as above, for the probe. (3fe230f)

        With no record claiming the session, an empty holder list is not a
        finding — it is the absence of one, and a probe that failed means nothing
        else was consulted either. This is precisely the reasoning
        session_unname.py:62-64 forbids: records are per-pid and deleted on exit,
        so a live session can legitimately have none, and code that reads "no
        holder + a transcript nobody touched recently" as dead strips a session
        that is still running and will re-stamp its name on the next turn.
        """
        self.remove_the_valid_session_record()
        self.set_process_map(None)
        before = tree_state(self.tmp)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 2, err)
        self.assertIn('could not probe running processes', err,
                      'it refused, but not because the probe was blind')
        self.assertStoresUntouched(before)

    def test_a_failed_probe_is_not_an_empty_process_list(self):
        """None means UNKNOWN; {} would mean "nothing is running". (da74cac)

        This is the bug in one assertion. If process_map() had encoded its
        failure as an empty dict, every holder would vanish (pmap.get(pid, '')
        is falsy, so alive is False, so holders_of filters it out) and the gate
        would report PASS for a session it knows nothing about. The two values
        must not be interchangeable, and here they produce opposite verdicts.
        """
        records, _ = su.read_session_records()

        blind = su.holders_of(SID, pmap=None, records=records)
        self.assertEqual([h['alive'] for h in blind], ['unknown'],
                         'a blind probe must keep the holder, flagged unknown')

        vanished = su.holders_of(SID, pmap={}, records=records)
        self.assertEqual(vanished, [],
                         'an empty map erases a live pid — this is why the '
                         'failure value must not be {}')

        self.set_process_map(None)
        rc_blind, _, err = run_quiet(su.cmd_verify, only=[SID8])
        self.set_process_map({})
        rc_empty, _, _ = run_quiet(su.cmd_verify, only=[SID8])

        self.assertEqual(rc_blind, 2, err)
        self.assertEqual(rc_empty, 0,
                         'documented for contrast: this PASS is what a probe '
                         'failure encoded as {} would have produced')

    def test_process_map_never_reports_an_empty_process_list(self):
        """process_map() collapses an empty result to None, on every branch.

        da74cac: the guarantee callers rely on is that {} is unreachable. A
        tasklist/ps that runs but yields nothing usable is a FAILED probe, not
        an empty machine.

        The second loop is the arm the first cannot reach: a probe that did not
        return at all. `except Exception` is not a defensive flourish — the probe
        carries its own timeout=20 (session_unname.py:199), which fires as
        TimeoutExpired on a loaded box, and a tasklist missing from PATH raises
        FileNotFoundError. Both land there, and both must answer None like every
        other failure.
        """
        for label, completed in (
            ('nonzero exit', subprocess.CompletedProcess([], 1, stdout='x\n')),
            ('empty stdout', subprocess.CompletedProcess([], 0, stdout='')),
            ('unparseable rows', subprocess.CompletedProcess([], 0, stdout='garbage\n')),
        ):
            with self.subTest(label):
                with mock.patch.object(su.subprocess, 'run', return_value=completed):
                    self.assertIsNone(REAL_PROCESS_MAP())

        for label, raised in (
            ('probe raised OSError', OSError(24, 'too many open files')),
            ('tasklist not on PATH', FileNotFoundError(2, 'no such file')),
            ('probe timed out', subprocess.TimeoutExpired('tasklist', 20)),
        ):
            with self.subTest(label):
                with mock.patch.object(su.subprocess, 'run', side_effect=raised):
                    self.assertIsNone(REAL_PROCESS_MAP())

    def test_a_probe_that_raises_is_refused_end_to_end(self):
        """A GENUINE probe failure, driven through both entry points. (da74cac,
        3fe230f)

        Everything else in this class pins process_map's answer, which proves two
        facts separately — "a broken probe returns None" just above, "callers
        refuse when handed None" everywhere else — and never joins them. Nothing
        drove a real failure through cmd_verify or cmd_revert, so this one-line
        mutation inside process_map's own failure arm passed the entire suite:

            except Exception:
        -       return None
        +       return {}

        {} is falsy for every pid, so every holder is filtered out; the gate
        printed "GATE: PASS — safe to clear" and exited 0, and the command
        rewrote both persistent stores, with a live pid still holding the
        session. This test patches the seam the shipped code actually uses —
        subprocess.run — and lets the real process_map decide what to return.
        """
        self.use_real_process_map()
        before = tree_state(self.tmp)

        with mock.patch.object(su.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('tasklist', 20)):
            rc_gate, _, err_gate = run_quiet(su.cmd_verify, only=[SID8])
            rc_cmd, _, err_cmd = run_quiet(su.cmd_revert, self.transcript, SID,
                                           self.revert_args())

        self.assertEqual(rc_gate, 2, err_gate)
        self.assertEqual(rc_cmd, 2, err_cmd)
        self.assertIn('could not probe running processes', err_cmd,
                      'it refused, but not because the probe was blind')
        self.assertStoresUntouched(before)

    def test_a_live_claude_holder_refuses_on_both_entry_points(self):
        """A named session re-stamps its transcript every turn, so a strip while
        it is live lasts seconds — and the operator is left thinking it worked.
        Gate and command both answer 1 (live / not safe yet). (da74cac, 3fe230f)
        """
        self.set_process_map({FAKE_PID: 'claude.exe'})
        before = tree_state(self.tmp)

        rc, _, err = run_quiet(su.cmd_verify, only=[SID8])
        self.assertEqual(rc, 1, err)

        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 1, err)
        self.assertStoresUntouched(before)

    def test_a_recycled_pid_is_treated_as_live_not_dead(self):
        """A pid whose image is not claude is SURFACED, never silently trusted.

        da74cac: the OS recycles pids, so a dead session's pid can be held by an
        unrelated process. The safe reading of "pid 999001 is now notepad.exe" is
        still "not proven dead" — the note is an aid to the operator, not a
        licence for the tool to proceed.
        """
        self.set_process_map({FAKE_PID: 'notepad.exe'})
        records, _ = su.read_session_records()
        holders = su.holders_of(SID, pmap={FAKE_PID: 'notepad.exe'}, records=records)
        self.assertEqual(len(holders), 1)
        self.assertTrue(holders[0]['suspect_reuse'])

        before = tree_state(self.tmp)
        rc, _, err = run_quiet(su.cmd_verify, only=[SID8])
        self.assertEqual(rc, 1, err)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 1, err)
        self.assertStoresUntouched(before)

    def test_force_overrides_the_unreadable_record_refusal(self):
        """--force is the documented escape hatch and must still work. (3fe230f)"""
        self.corrupt_a_session_record()
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID,
                               self.revert_args(force=True))
        self.assertEqual(rc, 0, err)
        self.assertNameEntries(0)

    def test_force_overrides_the_blind_probe_refusal(self):
        """--force overrides the probe-failure refusal too. (3fe230f)"""
        self.set_process_map(None)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID,
                               self.revert_args(force=True))
        self.assertEqual(rc, 0, err)
        self.assertNameEntries(0)

    def test_force_overrides_a_live_holder(self):
        """The live-holder guard was untouched by 3fe230f and --force still
        clears it — the operator who knows the pid is stale keeps their exit."""
        self.set_process_map({FAKE_PID: 'claude.exe'})
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID,
                               self.revert_args(force=True))
        self.assertEqual(rc, 0, err)
        self.assertNameEntries(0)


# ---------- A (continued). the strip itself ----------

class RevertCorrectnessTest(FakeHomeTest):
    """Once it proceeds, it must remove exactly the right bytes and no others."""

    def test_clean_state_strips_both_persistent_stores(self):
        """The happy path: nothing live, nothing unreadable, so it proceeds.

        Both persistent stores must be cleared. Missing store 3 (the job state)
        is what made two otherwise-correct attempts fail silently — the badge
        came back on respawn (session_unname.py:26-37).
        """
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 0, err)
        self.assertNameEntries(0)
        state = json.loads(self.job_state.read_text(encoding='utf-8'))
        self.assertNotIn('name', state)
        self.assertNotIn('nameSource', state)

    def test_only_real_name_entries_are_removed(self):
        """Lines that merely MENTION the entry types survive. (the grep trap)

        session_unname.py:45-49: `grep -c custom-title` counts every message in
        which the words were discussed. This fixture holds four such lines — a
        real assistant message, a JSON array, a bare JSON scalar, and a line that
        is not JSON at all — against two genuine entries. A strip driven by the
        substring would delete four turns of real conversation.
        """
        info = su.scan_transcript(self.transcript)
        self.assertEqual(info['real'], REAL_NAME_ENTRIES)
        self.assertEqual(info['false_positives'], DECOY_LINES)
        self.assertEqual(info['lines'], TOTAL_LINES)

        mentioning = [ln for ln in
                      self.transcript.read_text(encoding='utf-8').splitlines()
                      if 'custom-title' in ln or 'agent-name' in ln]
        self.assertEqual(len(mentioning), REAL_NAME_ENTRIES + DECOY_LINES,
                         'grep would delete all of these')

        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 0, err)

        after = self.transcript.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(after), TOTAL_LINES - REAL_NAME_ENTRIES)
        survivors = [ln for ln in after if 'custom-title' in ln or 'agent-name' in ln]
        self.assertEqual(len(survivors), DECOY_LINES,
                         'a decoy line was deleted — the strip is greping')
        self.assertIn('NOT JSON: custom-title agent-name', after)
        self.assertIn('"custom-title"', after)

    def test_job_state_loses_only_the_two_name_keys(self):
        """Store 3 is a supervisor's durable state: respawnFlags, lifecycle,
        labels. Removing anything beyond name/nameSource would break the job on
        its next respawn, so the contract is exactly two keys.
        """
        before = json.loads(self.job_state.read_text(encoding='utf-8'))
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 0, err)

        after = json.loads(self.job_state.read_text(encoding='utf-8'))
        self.assertEqual(set(before) - set(after), set(su.JOB_NAME_KEYS))
        for key, value in before.items():
            if key not in su.JOB_NAME_KEYS:
                self.assertEqual(after[key], value, f'{key} was altered')

    def test_a_successful_revert_writes_recoverable_backups(self):
        """The .bak siblings are the only undo. They must be byte-identical to
        what was there before, for BOTH stores.
        """
        transcript_before = self.transcript.read_bytes()
        job_state_before = self.job_state.read_bytes()

        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 0, err)

        t_baks = list(self.transcript.parent.glob('*.jsonl.bak-unname-*'))
        j_baks = list(self.job_state.parent.glob('state.json.bak-unname-*'))
        self.assertEqual(len(t_baks), 1, t_baks)
        self.assertEqual(len(j_baks), 1, j_baks)
        self.assertEqual(t_baks[0].read_bytes(), transcript_before)
        self.assertEqual(j_baks[0].read_bytes(), job_state_before)

        self.assertEqual(list(self.tmp.rglob('*.tmp')), [],
                         'a .tmp survived os.replace')

    def test_no_backup_writes_no_backup(self):
        """--no-backup means no undo — the hook path runs this way, which is
        why the gate in front of it is load-bearing."""
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID,
                               self.revert_args(no_backup=True))
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(self.tmp.rglob('*.bak-unname-*')), [])

    def test_the_session_record_store_is_never_written(self):
        """Store 1 is ephemeral and per-pid; the tool documents that it needs no
        intervention (session_unname.py:13-16). A successful revert must leave
        every session record byte-identical — writing there would corrupt the
        discovery registry other processes read.
        """
        before = tree_state(self.sessions)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 0, err)
        self.assertEqual(before, tree_state(self.sessions))

    def test_a_fresh_transcript_is_treated_as_active(self):
        """No record can claim a live session — records are per-pid and deleted
        on exit — so recent writes are the only remaining evidence. A transcript
        written moments ago is not safe to strip, whatever the pid table says.
        """
        os.utime(self.transcript, None)          # back to "just written"
        before = tree_state(self.tmp)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 1, err)
        self.assertStoresUntouched(before)


# ---------- A (continued). the real probe, not a stub ----------

@unittest.skipUnless(sys.platform == 'win32',
                     'needs tasklist and a copyable console executable')
class RealProcessProbeTest(FakeHomeTest):
    """One test that does NOT stub process_map.

    Everything above pins the probe's answer, which leaves the probe itself —
    the tasklist spawn and its CSV parse — untested. This closes that: a real
    process really named claude.exe, found by the real process_map(), refused by
    both entry points.
    """

    def test_a_genuinely_live_claude_exe_holder_is_refused(self):
        """da74cac matches the running IMAGE, not just the pid. Proven against a
        real process: ping.exe copied to claude.exe, so the image name the OS
        reports is the one the tool keys on.
        """
        system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        source = system_root / 'System32' / 'ping.exe'
        if not source.is_file():
            self.skipTest(f'no console executable to copy at {source}')

        fake_claude = self.tmp / 'bin' / 'claude.exe'
        fake_claude.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, fake_claude)

        proc = subprocess.Popen(
            [str(fake_claude), '-n', '600', '127.0.0.1'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # Registered after the temp-dir cleanup, so it runs BEFORE it (LIFO):
        # a running process holds its own image file open on Windows.
        self.addCleanup(self._reap, proc)

        self.use_real_process_map()
        image = self._await_image(proc.pid)
        if image is None:
            self.skipTest('process probe unavailable or too slow on this machine')
        self.assertEqual(image.lower(), 'claude.exe')

        write_json(self.sessions / f'{proc.pid}.json',
                   session_record_obj(SID, proc.pid))
        before = tree_state(self.tmp)

        rc, _, err = run_quiet(su.cmd_verify, only=[SID8])
        self.assertEqual(rc, 1, err)
        rc, _, err = run_quiet(su.cmd_revert, self.transcript, SID, self.revert_args())
        self.assertEqual(rc, 1, err)
        self.assertStoresUntouched(before)

    @staticmethod
    def _await_image(pid: int, timeout_s: float = 15.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            pmap = REAL_PROCESS_MAP()
            if pmap and pid in pmap:
                return pmap[pid]
            time.sleep(0.25)
        return None

    @staticmethod
    def _reap(proc):
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


# ---------- B. strip_session_images: only SYNC OK authorizes ----------

class SyncGateTest(FakeHomeTest):
    """6630c0b: SYNC_COMPLETE_PREFIXES treated all four sync outcomes as archive
    confirmation, but three of them are FAILURE markers written precisely when
    nothing reached MongoDB. wait_for_sync returned True on a failed sync, main()
    fell through to do_strip, and because hook_strip_images.py hard-codes
    --no-backup every image block in the live transcript was overwritten in
    place — no .bak on disk and no copy in the archive. Unrecoverable.
    """

    BLOB = 'iVBORw0KGgoAAAANSUhEUg' + 'A' * 96

    def setUp(self):
        super().setUp()
        self.image_transcript = self.projects / FAKE_SLUG / f'{SID}-images.jsonl'
        write_jsonl(self.image_transcript, image_transcript_records(SID, self.BLOB))
        self.sync_log.write_text('', encoding='utf-8')

    def log_line(self, marker: str, sid: str = SID8) -> str:
        """A line shaped like the ones _sync_runner.py actually writes."""
        return f'[2026-08-07 12:00:00 KST | 2026-08-07 03:00:00 UTC] {marker}  {sid}: 42 messages'

    def append_later(self, text: str, delay: float = 0.1):
        """Append AFTER the wait begins.

        wait_for_sync captures the log size at entry and reads only what lands
        after it (strip_session_images.py:71), so a fixture written up front is
        invisible by design — that is the stale-line defence, not a bug.
        """
        def run():
            time.sleep(delay)
            with self.sync_log.open('a', encoding='utf-8', newline='\n') as f:
                f.write(text + '\n')
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)

    def test_sync_ok_is_the_only_verdict_that_authorizes(self):
        """'SYNC OK' means the archive copy exists, so the local images are
        redundant and safe to destroy."""
        self.append_later(self.log_line('SYNC OK'))
        self.assertEqual(ss.wait_for_sync(SID8, timeout_s=5.0), 'ok')

    def test_every_failure_marker_refuses(self):
        """SKIP/ERR/FAIL each mean nothing was upserted. 6630c0b: all three used
        to read as confirmation. sync.log held 245 SYNC ERR and 93 SYNC FAIL
        lines at the time of the fix — every one of them authorized a strip.
        """
        for marker in ('SYNC SKIP', 'SYNC ERR', 'SYNC FAIL'):
            with self.subTest(marker):
                self.sync_log.write_text('', encoding='utf-8')
                self.append_later(self.log_line(marker))
                self.assertEqual(ss.wait_for_sync(SID8, timeout_s=5.0), 'failed')

    def test_silence_is_a_timeout_not_a_go_ahead(self):
        """No verdict is not a good verdict. Absence of evidence must not be
        read as evidence of an archive."""
        self.assertEqual(ss.wait_for_sync(SID8, timeout_s=1.0), 'timeout')

    def test_a_failure_short_circuits_instead_of_burning_the_timeout(self):
        """The hook spawns this worker with a 5s hook timeout; a verdict that is
        already known must not sit out the remaining budget. (6630c0b)"""
        self.append_later(self.log_line('SYNC ERR'))
        started = time.time()
        self.assertEqual(ss.wait_for_sync(SID8, timeout_s=10.0), 'failed')
        self.assertLess(time.time() - started, 3.0,
                        'a decided failure waited for the full timeout')

    def test_a_stale_success_line_is_not_authorization(self):
        """Yesterday's SYNC OK says nothing about THIS run.

        The sid is truncated to 8 chars and sessions are resumed, so the same
        marker legitimately recurs. Only bytes appended after the wait begins
        count — with a stale OK already on disk and a fresh failure landing
        after it, the answer is still 'failed'.
        """
        self.sync_log.write_text(self.log_line('SYNC OK') + '\n', encoding='utf-8')
        self.assertEqual(ss.wait_for_sync(SID8, timeout_s=1.0), 'timeout')

        self.append_later(self.log_line('SYNC FAIL'))
        self.assertEqual(ss.wait_for_sync(SID8, timeout_s=5.0), 'failed')

    def test_end_to_end_a_failed_sync_leaves_the_transcript_byte_identical(self):
        """The whole fix, through main(), with the hook's own argv
        (hook_strip_images.py:70-74). Before 6630c0b this run exited 0 and
        destroyed both image blocks with no backup anywhere.
        """
        before = self.image_transcript.read_bytes()
        self.append_later(self.log_line('SYNC ERR'))
        with mock.patch.object(sys, 'argv', self.hook_argv()):
            with self.assertRaises(SystemExit) as caught:
                run_quiet(ss.main)
        self.assertNotIn(caught.exception.code, (0, None),
                         'a refusal must be a non-zero exit')
        self.assertEqual(self.image_transcript.read_bytes(), before)
        self.assertEqual(list(self.image_transcript.parent.glob('*.tmp')), [])
        self.assertEqual(list(self.image_transcript.parent.glob('*.bak')), [])

    def test_end_to_end_a_successful_sync_still_strips(self):
        """Failing closed must not mean failing always: with a real SYNC OK the
        images go, the structure stays valid, and non-JSON lines pass through.
        """
        self.append_later(self.log_line('SYNC OK'))
        with mock.patch.object(sys, 'argv', self.hook_argv()):
            run_quiet(ss.main)

        text = self.image_transcript.read_text(encoding='utf-8')
        self.assertNotIn(self.BLOB, text, 'the base64 payload survived')
        self.assertEqual(text.count('[image stripped]'), IMAGE_BLOCKS)
        self.assertIn('NOT JSON — a truncated write from a killed process', text)
        self.assertIn('스크린샷 확인했습니다', text)
        for line in text.splitlines():
            if line.startswith('{'):
                json.loads(line)          # still structurally valid JSONL

    def hook_argv(self):
        """Exactly what hook_strip_images.py:70-74 passes — including
        --no-backup, which is why a wrong verdict is unrecoverable."""
        return [str(ss.__file__), str(self.image_transcript),
                '--skip-if-clean', '--no-backup', '--quiet',
                '--wait-for-sync', SID8]


# ---------- residue audit (named to sort last) ----------

class ZZResidueAuditTest(unittest.TestCase):
    """Proof that the suite left nothing behind.

    A test suite for tools that destroy data has to demonstrate its own
    innocence, not assert it. Each check compares against a baseline taken in
    setUpModule, so a leftover from someone's earlier real /unbadge run cannot
    be mistaken for this suite's residue — or hide it.
    """

    def test_no_module_constant_is_left_patched(self):
        for name, real in REAL_SU.items():
            self.assertEqual(getattr(su, name), real, f'session_unname.{name} leaked')
        for name, real in REAL_SS.items():
            self.assertEqual(getattr(ss, name), real,
                             f'strip_session_images.{name} leaked')
        self.assertIs(su.process_map, REAL_PROCESS_MAP, 'process_map still stubbed')

    def test_every_temporary_directory_was_removed(self):
        self.assertTrue(_TEMP_DIRS, 'no fixtures were built — did anything run?')
        survivors = [str(p) for p in _TEMP_DIRS if p.exists()]
        self.assertEqual(survivors, [])

    def test_nothing_was_written_under_the_real_claude_home(self):
        self.assertEqual(dangerous_paths(REAL_SU['CLAUDE_HOME']), _BASELINE['claude'],
                         'a backup or temp file appeared in the real ~/.claude')
        for key, root in (('sessions', REAL_SU['CLAUDE_SESSIONS']),
                          ('jobs', REAL_SU['CLAUDE_JOBS'])):
            if not root.is_dir():
                continue
            now = {p.name for p in root.glob('*')}
            self.assertTrue(set(_BASELINE[key]) <= now,
                            f'a pre-existing entry disappeared from {root}')
            self.assertEqual([n for n in now - set(_BASELINE[key])
                              if n.startswith('999')], [],
                             'a fixture record leaked into the real store')

    def test_nothing_was_written_into_the_repository(self):
        self.assertEqual(dangerous_paths(REPO, recursive=False), _BASELINE['repo'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
