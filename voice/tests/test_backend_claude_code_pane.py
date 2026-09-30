"""DESIGN.md §Acceptance E1, E2 — nonce ownership and dialog occurrence identity.

Covers the pane's capabilities: binding, classification, dialog occurrence identity, option
structure, owner loss, and the session registry. The composer/keyboard surface is out of scope:
the backend does not drive it.

The pane fixture is a REAL `orca terminal read --json` response captured while this repo's own
probe was the running Bash tool call, so the nonce-hit counts are measurements, not inventions.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from typing import Any

from voice.backend.claude_code import pane as paneg
from voice.backend.claude_code.pane import (
    CLS_DIALOG,
    CLS_PROMPT,
    CLS_UNKNOWN,
    CLS_WORKING,
    LOST_PID_GONE,
    LOST_PID_REUSED,
    LOST_TTY,
    REFUSE_BAD_NONCE,
    REFUSE_NO_HANDLE,
    REFUSE_NO_OWNER,
    REFUSE_READ_FAILED,
    REFUSE_REGISTRY,
    REFUSE_UNPROVEN,
    Pane,
    classify,
    dialog_identity,
    lstart_epoch,
    options_in,
    registry_claims,
    registry_epoch,
)

FIXTURE_DIR = Path(os.environ.get(
    "VOICE_LISTEN_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures")))

RAW = json.loads((FIXTURE_DIR / "pane-raw.json").read_text())
REAL_TAIL: list[str] = RAW["result"]["terminal"]["tail"]
REAL_HANDLE = RAW["result"]["terminal"]["handle"]
REAL_NONCE = "vlbind-7c2e91a4"          # measured: 2 hits in the owning pane

OWNER = {"pid": 51548, "start": "Sat Sep  5 23:30:58 2026", "tty": "ttys023",
         "comm": "claude"}
# `procStart` is the SAME instant rendered as UTC ctime, derived here so the fixture is correct
# in whatever zone the suite runs.
REGISTRY = {"pid": OWNER["pid"], "sessionId": "s1", "cwd": "/tmp/wt",
            "procStart": time.strftime(paneg._CTIME,
                                       time.gmtime(lstart_epoch(OWNER["start"]))),
            "status": "running"}

# The two values the daemon would supply; the module carries no defaults.
TEST_READ_TIMEOUT = 5.0
# `ps` start-time granularity: both sources render whole seconds.
TEST_START_GRANULARITY = 1.0
# The `ps` subprocess bound.
TEST_PS_TIMEOUT = 5.0

RULE = "─" * 40
STATUS = "  ? for shortcuts"


def envelope(tail: list[str], *, handle: str = REAL_HANDLE,
             incarnation: str | None = "inc-1", worktree: str | None = "/tmp/wt") -> dict:
    terminal: dict[str, Any] = {"handle": handle, "tail": list(tail), "status": "running"}
    if incarnation is not None:
        terminal["incarnationId"] = incarnation
    if worktree is not None:
        terminal["worktreePath"] = worktree
    return {"ok": True, "result": {"terminal": terminal}}


class FakeRun:
    """Records argv and replays canned envelopes. Never touches a real terminal.

    Asserting on `argv` is how these tests prove the zero-keystroke property: every recorded
    command must be a `read` or a `show`."""

    def __init__(self, read_tail: list[str], *, show: dict | None = None,
                 read_fails: bool = False) -> None:
        self.read_tail = read_tail
        self.show = show if show is not None else {"handle": REAL_HANDLE,
                                                   "incarnationId": "inc-1"}
        self.read_fails = read_fails
        self.argv: list[list[str]] = []

    async def __call__(self, argv: list[str], timeout: float) -> dict:
        self.argv.append(list(argv))
        if "read" in argv:
            if self.read_fails:
                return {"stdout": "", "stderr": "boom", "returncode": 1}
            return {"stdout": json.dumps(envelope(self.read_tail)), "returncode": 0}
        if "show" in argv:
            return {"stdout": json.dumps({"ok": True, "result": {"terminal": self.show}}),
                    "returncode": 0}
        return {"stdout": "", "returncode": 1}

    def sent_keystrokes(self) -> list[list[str]]:
        return [a for a in self.argv if "send" in a or "key" in a or "type" in a]


class E1NonceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """E1 — the nonce proves pane ownership with zero keystrokes; zero hits refuses."""

    def setUp(self) -> None:
        # `owner_fingerprint` must never shell out during a test.
        self._real = paneg.owner_fingerprint
        paneg.owner_fingerprint = lambda pid, **kw: (
            dict(OWNER) if int(pid) == OWNER["pid"] else {})

    def tearDown(self) -> None:
        paneg.owner_fingerprint = self._real

    async def test_E1_a_valid_running_command_binds(self):
        run = FakeRun(REAL_TAIL)
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal=REAL_HANDLE, run=run)
        out = await pane.bind(nonce=REAL_NONCE, session_id="s1",
                              ancestor_pid=OWNER["pid"], registry=REGISTRY)
        self.assertTrue(out["bound"])
        self.assertEqual(out["hits"], 2)        # measured: the wrapped line renders twice
        self.assertEqual(out["owner"]["start"], OWNER["start"])
        self.assertIsNone(out["refusal"])

    async def test_E1_binding_sends_zero_keystrokes(self):
        """The property the whole design exists for: only read and show ever run."""
        run = FakeRun(REAL_TAIL)
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal=REAL_HANDLE, run=run)
        await pane.bind(nonce=REAL_NONCE, session_id="s1", ancestor_pid=OWNER["pid"],
                        registry=REGISTRY)
        self.assertEqual(run.sent_keystrokes(), [])
        for argv in run.argv:
            self.assertIn(argv[2], ("read", "show"))

    async def test_E1_nonce_in_old_transcript_text_refuses(self):
        """The nonce is on screen, but as prose. Prose is not proof."""
        tail = ["⏺ I will use the nonce vlbind-7c2e91a4 for the probe.",
                "  Ran 1 shell command", RULE, "❯", RULE, STATUS]
        run = FakeRun(tail)
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal=REAL_HANDLE, run=run)
        out = await pane.bind(nonce=REAL_NONCE, session_id="s1",
                              ancestor_pid=OWNER["pid"], registry=REGISTRY)
        self.assertFalse(out["bound"])
        self.assertEqual(out["refusal"], REFUSE_UNPROVEN)
        self.assertEqual(run.sent_keystrokes(), [])

    async def test_E1_a_sibling_pane_scoring_zero_hits_is_a_hard_refuse(self):
        tail = ["⏺ different session entirely", "  ⎿  $ npm test", RULE, "❯", RULE]
        run = FakeRun(tail)
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal="term_0d6cf065-4007-416d-94be-7a25a2066312", run=run)
        out = await pane.bind(nonce=REAL_NONCE, session_id="s1",
                              ancestor_pid=OWNER["pid"], registry=REGISTRY)
        self.assertFalse(out["bound"])
        self.assertEqual(out["refusal"], REFUSE_UNPROVEN)
        self.assertEqual(out["hits"], 0)

    async def test_E1_a_malformed_nonce_refuses_before_any_read(self):
        run = FakeRun(REAL_TAIL)
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal=REAL_HANDLE, run=run)
        out = await pane.bind(nonce="has spaces", session_id="s1",
                              ancestor_pid=OWNER["pid"], registry=REGISTRY)
        self.assertEqual(out["refusal"], REFUSE_BAD_NONCE)
        self.assertEqual(run.argv, [], "a caller bug must not cost a pane read")

    async def test_E1_no_handle_refuses(self):
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal="", run=FakeRun(REAL_TAIL))
        out = await pane.bind(nonce=REAL_NONCE, session_id="s1",
                              ancestor_pid=OWNER["pid"], registry=REGISTRY)
        self.assertEqual(out["refusal"], REFUSE_NO_HANDLE)

    async def test_E1_a_dead_ancestor_refuses(self):
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal=REAL_HANDLE, run=FakeRun(REAL_TAIL))
        out = await pane.bind(nonce=REAL_NONCE, session_id="s1",
                              ancestor_pid=999999, registry=REGISTRY)
        self.assertEqual(out["refusal"], REFUSE_NO_OWNER)

    async def test_E1_a_failed_read_refuses(self):
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal=REAL_HANDLE, run=FakeRun(REAL_TAIL, read_fails=True))
        out = await pane.bind(nonce=REAL_NONCE, session_id="s1",
                              ancestor_pid=OWNER["pid"], registry=REGISTRY)
        self.assertEqual(out["refusal"], REFUSE_READ_FAILED)

    async def test_E1_a_registry_naming_another_session_refuses(self):
        pane = Pane(read_timeout=TEST_READ_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
                    ps_timeout=TEST_PS_TIMEOUT, terminal=REAL_HANDLE, run=FakeRun(REAL_TAIL))
        out = await pane.bind(nonce=REAL_NONCE, session_id="s1",
                              ancestor_pid=OWNER["pid"],
                              registry={**REGISTRY, "sessionId": "another"})
        self.assertEqual(out["refusal"], REFUSE_REGISTRY)


class E2DialogOccurrenceTests(unittest.TestCase):
    """E2 — dialog identity carries an occurrence counter; the same wording later is new."""

    def _permission(self, target="build/"):
        return "\n".join([
            f"│ Bash wants to run rm -rf {target} ?                     │",
            "│ ❯ 1. Yes                                                │",
            "│   2. Yes, and don't ask again                           │",
            "│   3. No                                                 │",
        ])

    def test_E2_a_dialog_held_on_screen_keeps_its_occurrence(self):
        screen = self._permission()
        first = classify(screen, previous=None, at=1.0)
        second = classify(screen, previous=first, at=2.0)
        self.assertEqual(first["dialog"]["occurrence"], 1)
        self.assertEqual(second["dialog"]["occurrence"], 1)
        self.assertEqual(first["dialog"]["hash"], second["dialog"]["hash"])

    def test_E2_identical_text_after_an_absence_is_a_new_occurrence(self):
        """The case the whole counter exists for: an approval for the first must not answer
        the second."""
        screen = self._permission()
        first = classify(screen, previous=None, at=1.0)
        faded = dict(first["dialog"])
        faded["present"] = False
        gone = {**first, "dialog": faded}
        second = classify(screen, previous=gone, at=3.0)
        self.assertEqual(second["dialog"]["occurrence"], 2)
        self.assertEqual(second["dialog"]["hash"], first["dialog"]["hash"])

    def test_E2_different_text_is_a_new_occurrence(self):
        first = classify(self._permission("build/"), previous=None, at=1.0)
        second = classify(self._permission("dist/"), previous=first, at=2.0)
        self.assertEqual(second["dialog"]["occurrence"], 2)
        self.assertNotEqual(second["dialog"]["hash"], first["dialog"]["hash"])

    def test_E2_redraw_noise_does_not_mint_an_occurrence(self):
        """A resize repaints with different box glyphs and spacing. Same dialog."""
        first = classify(self._permission(), previous=None, at=1.0)
        noisy = self._permission().replace("│", "┃").replace("   ", "  ")
        second = classify(noisy, previous=first, at=2.0)
        self.assertEqual(second["dialog"]["hash"], first["dialog"]["hash"])
        self.assertEqual(second["dialog"]["occurrence"], 1)

    def test_E2_the_occurrence_survives_a_class_change_in_between(self):
        """Dialog, then working, then the same dialog: still a new occurrence, because the
        dialog was ABSENT in between."""
        first = classify(self._permission(), previous=None, at=1.0)
        faded = dict(first["dialog"])
        faded["present"] = False
        after_work = {"class": CLS_WORKING, "dialog": faded, "at": 2.0, "reason": ""}
        second = classify(self._permission(), previous=after_work, at=3.0)
        self.assertEqual(second["dialog"]["occurrence"], 2)

    def test_E2_identity_binds_the_tool_id_when_one_is_unambiguous(self):
        record = dialog_identity("a question", options=((1, "Yes"), (2, "No")),
                                 previous=None, tool_id="tool-7")
        self.assertEqual(record["tool_id"], "tool-7")


class ClassifyTests(unittest.TestCase):
    """The classifier fails closed: nothing unrecognized comes back permissive."""

    def test_an_empty_screen_is_unknown(self):
        out = classify("", previous=None, at=1.0)
        self.assertEqual(out["class"], CLS_UNKNOWN)
        self.assertEqual(out["reason"], "empty_screen")

    def test_a_spinner_is_working(self):
        out = classify("✢ Whirring… (3m 47s · ↓ 12.2k tokens)", previous=None, at=1.0)
        self.assertEqual(out["class"], CLS_WORKING)

    def test_a_rendered_tool_call_is_working(self):
        out = classify("  ⎿  $ npm test", previous=None, at=1.0)
        self.assertEqual(out["class"], CLS_WORKING)

    def test_a_fully_rendered_composer_is_a_prompt(self):
        out = classify("\n".join([RULE, "❯", RULE]), previous=None, at=1.0)
        self.assertEqual(out["class"], CLS_PROMPT)

    def test_a_half_drawn_composer_is_unknown(self):
        """One rule means we caught the pane mid-repaint. A prompt read from half a screen is
        exactly the false 'idle' this refuses to report."""
        out = classify("\n".join([RULE, "❯"]), previous=None, at=1.0)
        self.assertEqual(out["class"], CLS_UNKNOWN)
        self.assertEqual(out["reason"], "composer_partial")

    def test_a_dialog_wins_over_the_composer_below_it(self):
        """Order is a safety decision: a dialog drawn above a composer must not read as idle."""
        screen = "\n".join([
            "│ Bash wants to run rm -rf build/ ?  │",
            "│ ❯ 1. Yes                           │",
            "│   3. No                            │",
            RULE, "❯", RULE,
        ])
        self.assertEqual(classify(screen, previous=None, at=1.0)["class"], CLS_DIALOG)

    def test_a_half_drawn_dialog_is_unknown_not_a_dialog(self):
        """A cursor row with no complete option block: still drawing, or a widget we cannot
        enumerate. Both stay unknown, which forbids arming."""
        screen = "❯ 1. Yes"
        out = classify(screen, previous=None, at=1.0)
        self.assertEqual(out["class"], CLS_UNKNOWN)
        self.assertEqual(out["reason"], "dialog_incomplete")

    def test_a_dialog_is_recognized_by_shape_in_any_language(self):
        """Recognition is the cursor-bearing numbered block, never the wording. A localized
        harness draws the same shape and must classify identically."""
        screen = "\n".join(["│ 要运行 rm -rf build/ 吗？ │",
                            "│ ❯ 1. 是 │", "│   2. 否 │"])
        self.assertEqual(classify(screen, previous=None, at=1.0)["class"], CLS_DIALOG)

    def test_prose_with_no_cursor_row_is_not_a_dialog(self):
        """Wording alone can never mint a dialog now: without a picker there is no widget."""
        screen = "\n".join(["Do you want to proceed?", "  1. Yes", "  2. No"])
        self.assertNotEqual(classify(screen, previous=None, at=1.0)["class"], CLS_DIALOG)

    def test_no_english_pattern_table_survives(self):
        from voice.backend.claude_code import pane as module
        self.assertFalse(hasattr(module, "_PERMISSION_PATTERNS"))
        self.assertFalse(hasattr(module, "_QUESTION_PATTERNS"))


class OptionStructureTests(unittest.TestCase):
    def test_options_come_back_as_ordered_index_text_pairs(self):
        lines = ["│ ❯ 1. Yes │", "│   2. Yes, and don't ask again │", "│   3. No │"]
        self.assertEqual(options_in(lines),
                         ((1, "Yes"), (2, "Yes, and don't ask again"), (3, "No")))

    def test_border_glyphs_are_stripped_before_matching(self):
        """Without stripping, every real dialog falls through to unknown."""
        self.assertEqual(options_in(["┃ ❯ 1. Yes ┃"]), ((1, "Yes"),))

    def test_order_is_preserved_as_rendered(self):
        """Option 2 grants more than was asked, so order is what keeps a broad standing grant
        from being picked in place of the narrow one."""
        lines = ["  1. Yes", "  2. Yes, and don't ask again", "  3. No"]
        self.assertEqual([index for index, _text in options_in(lines)], [1, 2, 3])

    def test_prose_that_is_not_an_option_row_is_not_an_option(self):
        self.assertEqual(options_in(["this sentence mentions 1. thing"]), ())


class E6OwnerLossTests(unittest.TestCase):
    """E6, first half — owner loss produces a REASON, never a new pane to follow."""

    def _binding(self):
        return {"owner": dict(OWNER), "incarnation": "inc-1", "handle": REAL_HANDLE}

    def test_E6_a_healthy_binding_is_not_lost(self):
        self.assertIsNone(paneg.owner_lost(self._binding(), current_owner=dict(OWNER),
                                           current_show={"incarnationId": "inc-1"}))

    def test_E6_a_gone_pid_is_lost(self):
        self.assertEqual(paneg.owner_lost(self._binding(), current_owner={},
                                          current_show={}), LOST_PID_GONE)

    def test_E6_a_reused_pid_is_lost(self):
        """Same number, different start time: a different process entirely."""
        other = {**OWNER, "start": "Sun Sep  6 10:00:00 2026"}
        self.assertEqual(paneg.owner_lost(self._binding(), current_owner=other,
                                          current_show={}), LOST_PID_REUSED)

    def test_E6_a_changed_tty_is_lost(self):
        other = {**OWNER, "tty": "ttys099"}
        self.assertEqual(paneg.owner_lost(self._binding(), current_owner=other,
                                          current_show={}), LOST_TTY)

    def test_E6_an_absent_incarnation_does_not_fence_a_healthy_binding(self):
        """Measured: the runtime returns this on some calls and None on others. Treating
        absence as change would fence a healthy binding whenever it felt terse."""
        self.assertIsNone(paneg.owner_lost(self._binding(), current_owner=dict(OWNER),
                                           current_show={}))

    def test_E6_owner_loss_never_names_a_replacement(self):
        reason = paneg.owner_lost(self._binding(), current_owner={}, current_show={})
        self.assertIsInstance(reason, str)


class RegistryTests(unittest.TestCase):
    def test_utc_and_local_renderings_of_one_instant_agree(self):
        """`procStart` is UTC ctime and `ps lstart` is local, so the two are never byte-equal;
        both reduce to the same epoch."""
        claimed, why = registry_claims(REGISTRY, pid=OWNER["pid"], session_id="s1",
                                       owner=dict(OWNER),
                                       start_granularity=TEST_START_GRANULARITY)
        self.assertIsNone(why)
        self.assertEqual(claimed["pid"], OWNER["pid"])
        self.assertEqual(claimed["session_id"], "s1")

    def test_a_recycled_pid_whose_registry_outlived_its_owner_refuses(self):
        stale = {**REGISTRY, "procStart": time.strftime(paneg._CTIME, time.gmtime(0))}
        claimed, why = registry_claims(stale, pid=OWNER["pid"], session_id="s1",
                                       owner=dict(OWNER),
                                       start_granularity=TEST_START_GRANULARITY)
        self.assertIsNone(claimed)
        self.assertEqual(why, "registry_start_mismatch")

    def test_a_missing_registry_refuses(self):
        claimed, why = registry_claims(None, pid=OWNER["pid"], session_id="s1",
                                       owner=dict(OWNER),
                                       start_granularity=TEST_START_GRANULARITY)
        self.assertIsNone(claimed)
        self.assertEqual(why, "registry_missing")

    def test_an_unparseable_start_is_none_not_zero(self):
        self.assertIsNone(registry_epoch("not a date"))
        self.assertIsNone(lstart_epoch(""))


if __name__ == "__main__":
    unittest.main()
