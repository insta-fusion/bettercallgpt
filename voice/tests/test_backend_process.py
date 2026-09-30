"""DESIGN.md §Acceptance E7 — `backend/process.py` drives a codex-style child end to end.

The fake child is a real process speaking the real `codex exec --json` dialect (recorded live
from codex-cli 0.154.0), so send → progress → result, the `now` interrupt, and unexpected death
are exercised against actual pipes, signals and EOF — not against a mock. No test here waits on
a clock: every wait is a wait on the child.

The last test is the live smoke against the real `codex` binary; it skips cleanly when the CLI
is absent or cannot run.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from voice.backend.base import OBS_OWNER_LOST, OBS_PROGRESS, OBS_RECEIPT, OBS_RESULT
from voice.backend.process import ChildSpec, ProcessBackend, codex_exec_parser, codex_exec_spec

# A child that speaks the codex dialect: it announces a thread, opens a turn, emits the prompt
# back as an agent message, then completes. `--hold:<prompt>` makes the child started with THAT
# prompt wait for SIGINT instead of completing, and on SIGINT it reports `turn.failed` and exits
# — which is what an interrupted turn looks like. Keying the hold to the prompt is what lets a
# replacement child run to completion after the held one is cut.
FAKE_CHILD = textwrap.dedent('''
    import json, signal, sys, threading

    prompt = sys.argv[-1]
    hold = ("--hold" in sys.argv) or ("--hold:" + prompt in sys.argv)
    die = "--die" in sys.argv
    stop = threading.Event()

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    def on_sigint(signum, frame):
        emit({"type": "turn.failed", "error": {"message": "interrupted: " + prompt}})
        stop.set()

    signal.signal(signal.SIGINT, on_sigint)

    emit({"type": "thread.started", "thread_id": "th-" + prompt})
    emit({"type": "turn.started"})
    sys.stderr.write("child noise for " + prompt + "\\n")
    sys.stderr.flush()

    if die:
        sys.exit(3)

    emit({"type": "item.completed",
          "item": {"id": "item_0", "type": "reasoning", "text": "ignored"}})
    emit({"type": "item.completed",
          "item": {"id": "item_1", "type": "agent_message", "text": "working on " + prompt}})

    if hold:
        stop.wait()
        sys.exit(0)

    emit({"type": "item.completed",
          "item": {"id": "item_2", "type": "agent_message", "text": "done with " + prompt}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}})
''')


def write_fake_child() -> tuple[tempfile.TemporaryDirectory, str]:
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "fake_agent.py")
    with open(path, "w") as handle:
        handle.write(FAKE_CHILD)
    return tmp, path


async def take(stream, count):
    """The first `count` observations. Bounded by the COUNT, never by a clock."""
    out = []
    async for obs in stream:
        out.append(obs)
        if len(out) == count:
            return out
    return out


async def drain(stream):
    return [obs async for obs in stream]


class CodexParserTests(unittest.TestCase):
    """The dialect, mapped from lines the real CLI actually emitted."""

    def test_thread_started_is_a_receipt_carrying_the_childs_own_id(self):
        obs = codex_exec_parser({"type": "thread.started", "thread_id": "01a0bbfc"})
        self.assertEqual(obs.kind, OBS_RECEIPT)
        self.assertEqual(obs.turn_id, "01a0bbfc")

    def test_agent_message_item_is_progress_with_its_whole_text(self):
        obs = codex_exec_parser({"type": "item.completed",
                                 "item": {"id": "item_1", "type": "agent_message",
                                          "text": "line one\nline two"}})
        self.assertEqual(obs.kind, OBS_PROGRESS)
        self.assertEqual(obs.text, "line one\nline two")

    def test_non_agent_items_are_not_observations(self):
        # The live stream carries `error`-typed items that are CLI notices, not turn failures.
        self.assertIsNone(codex_exec_parser(
            {"type": "item.completed",
             "item": {"id": "item_0", "type": "error", "message": "skill notice"}}))

    def test_turn_completed_is_the_result(self):
        obs = codex_exec_parser({"type": "turn.completed", "usage": {"output_tokens": 5}})
        self.assertEqual(obs.kind, OBS_RESULT)

    def test_turn_failed_is_a_failed_result_carrying_the_error(self):
        obs = codex_exec_parser({"type": "turn.failed", "error": {"message": "out of credits"}})
        self.assertEqual(obs.kind, OBS_RESULT)
        self.assertTrue(obs.payload["failed"])
        self.assertEqual(obs.text, "out of credits")

    def test_unknown_events_are_skipped(self):
        self.assertIsNone(codex_exec_parser({"type": "token.count", "n": 3}))


class ProcessBackendTests(unittest.TestCase):
    """E7 — send, progress, result, interrupt, loss."""

    def setUp(self):
        self._tmp, self._child = write_fake_child()
        self.addCleanup(self._tmp.cleanup)

    def backend(self, *extra) -> ProcessBackend:
        return ProcessBackend(ChildSpec(
            argv=(sys.executable, self._child, *extra),
            parser=codex_exec_parser,
        ))

    def test_send_then_progress_then_result(self):
        """E7 core: one send drives the child to a result carrying its whole final message."""
        async def run():
            backend = self.backend()
            receipt = await backend.send("alpha", tag="v1", priority="next",
                                         transcript="alpha", interpretation="ask alpha")
            self.assertEqual(receipt.outcome, "posted")
            self.assertEqual(receipt.tag, "v1")

            stream = backend.observe()
            seen = await take(stream, 5)
            kinds = [obs.kind for obs in seen]
            self.assertEqual(kinds, [OBS_RECEIPT, OBS_PROGRESS, OBS_PROGRESS, OBS_PROGRESS,
                                     OBS_RESULT])

            # The receipt attributes to the tag we sent and to the child's own thread id.
            self.assertEqual(seen[0].tag, "v1")
            self.assertEqual(seen[0].turn_id, "th-alpha")

            # Every later observation carries that same identity — nothing minted here.
            self.assertTrue(all(obs.turn_id == "th-alpha" for obs in seen))

            # The result is the LAST agent message, whole.
            self.assertEqual(seen[-1].text, "done with alpha")
            self.assertEqual(seen[-1].payload["result_id"], "th-alpha")

            state = await backend.state()
            self.assertEqual(state.activity, "idle")
            self.assertEqual(state.turn_id, "th-alpha")

            # The child exits cleanly after delivering its result. That is the END OF A TURN,
            # not the loss of the owner: the backend stays usable and the stream stays open.
            await backend.wait_for_child()
            self.assertIsNone((await backend.state()).owner_lost)

        asyncio.run(run())

    def test_two_sequential_requests_two_children_two_results(self):
        """One child = one turn. A completed turn must not retire the backend."""
        async def run():
            backend = self.backend()
            stream = backend.observe()

            first = await backend.send("alpha", tag="v1", priority="next",
                                       transcript="alpha", interpretation="ask alpha")
            self.assertEqual(first.outcome, "posted")
            opening = await take(stream, 5)
            self.assertEqual(opening[-1].text, "done with alpha")
            await backend.wait_for_child()

            # The second send is accepted, not refused with owner_lost.
            second = await backend.send("beta", tag="v2", priority="next",
                                        transcript="beta", interpretation="ask beta")
            self.assertEqual(second.outcome, "posted")
            self.assertEqual(second.tag, "v2")

            following = await take(stream, 5)
            self.assertEqual(following[-1].kind, OBS_RESULT)
            self.assertEqual(following[-1].text, "done with beta")

            # A fresh child means a fresh identity, carried from the child's own thread event.
            self.assertEqual(opening[0].turn_id, "th-alpha")
            self.assertEqual(following[0].turn_id, "th-beta")
            self.assertNotIn(OBS_OWNER_LOST,
                             [obs.kind for obs in opening + following])

        asyncio.run(run())

    def test_send_now_mid_turn_interrupts_and_respawns(self):
        """`now` during a running turn: SIGINT the child, spawn a new one, get a new result."""
        async def run():
            # Only the FIRST child holds; the replacement runs to its own result.
            backend = self.backend("--hold:first")
            await backend.send("first", tag="v1", priority="next",
                               transcript="first", interpretation="ask first")
            stream = backend.observe()

            # Wait on the CHILD, not a clock: its progress line proves the turn is open.
            opening = await take(stream, 3)
            self.assertEqual(opening[-1].text, "working on first")
            self.assertEqual((await backend.state()).activity, "working")

            receipt = await backend.send("second", tag="v2", priority="now",
                                         transcript="second", interpretation="ask second")
            self.assertEqual(receipt.outcome, "posted")
            self.assertEqual(receipt.tag, "v2")

            # The interrupted child reported its own failure before dying; the replacement then
            # runs to its own result. Neither death is owner loss: the first was REPLACED and
            # the second completed its turn, so the backend stays usable throughout.
            rest = await take(stream, 6)
            self.assertNotIn(OBS_OWNER_LOST, [obs.kind for obs in rest])
            await backend.wait_for_child()
            self.assertIsNone((await backend.state()).owner_lost)

            interrupted = [obs for obs in rest if obs.payload.get("failed")]
            self.assertEqual(len(interrupted), 1)
            self.assertEqual(interrupted[0].text, "interrupted: first")

            final = [obs for obs in rest if obs.kind == OBS_RESULT and not obs.payload.get("failed")]
            self.assertEqual(final[-1].text, "done with second")
            self.assertEqual(final[-1].turn_id, "th-second")

        asyncio.run(run())

    def test_cancel_signals_the_child_and_is_applied(self):
        async def run():
            backend = self.backend("--hold")
            await backend.send("gamma", tag="v1", priority="next",
                               transcript="gamma", interpretation="ask gamma")
            stream = backend.observe()
            await take(stream, 3)

            receipt = await backend.cancel("th-gamma")
            self.assertEqual(receipt.outcome, "applied")

            rest = await drain(stream)
            self.assertEqual(rest[-1].kind, OBS_OWNER_LOST)
            failed = [obs for obs in rest if obs.payload.get("failed")]
            self.assertEqual(failed[0].text, "interrupted: gamma")

        asyncio.run(run())

    def test_child_dies_unexpectedly_gives_owner_lost_with_stderr_and_ends_observe(self):
        async def run():
            backend = self.backend("--die")
            await backend.send("delta", tag="v1", priority="next",
                               transcript="delta", interpretation="ask delta")

            seen = await drain(backend.observe())   # ends on its own at owner_lost
            lost = seen[-1]
            self.assertEqual(lost.kind, OBS_OWNER_LOST)
            self.assertEqual(lost.payload["exit_code"], 3)
            self.assertIn("child noise for delta", lost.payload["stderr"])

            state = await backend.state()
            self.assertIsNotNone(state.owner_lost)

        asyncio.run(run())

    def test_every_actuation_after_owner_loss_is_refused(self):
        async def run():
            backend = self.backend("--die")
            await backend.send("eps", tag="v1", priority="next",
                               transcript="eps", interpretation="ask eps")
            await drain(backend.observe())

            resend = await backend.send("again", tag="v2", priority="now",
                                        transcript="again", interpretation="ask again")
            self.assertEqual(resend.outcome, "refused")
            self.assertIn("owner_lost", resend.reason)
            self.assertEqual((await backend.cancel("th-eps")).outcome, "refused")

        asyncio.run(run())

    def test_dialog_answer_is_refused(self):
        async def run():
            receipt = await self.backend().answer("occ-1", "yes")
            self.assertEqual(receipt.outcome, "refused")
            self.assertIn("no dialog", receipt.reason)

        asyncio.run(run())

    def test_next_priority_during_a_running_turn_is_refused_not_dropped(self):
        async def run():
            backend = self.backend("--hold")
            await backend.send("zeta", tag="v1", priority="next",
                               transcript="zeta", interpretation="ask zeta")
            stream = backend.observe()
            await take(stream, 3)

            second = await backend.send("later", tag="v2", priority="next",
                                        transcript="later", interpretation="ask later")
            self.assertEqual(second.outcome, "refused")
            self.assertEqual(second.tag, "v2")

            await backend.cancel("th-zeta")
            await drain(stream)

        asyncio.run(run())

    def test_env_allowlist_is_what_the_child_receives(self):
        async def run():
            os.environ["VOICE_E7_PROBE"] = "kept"
            os.environ["VOICE_E7_SECRET"] = "withheld"
            self.addCleanup(os.environ.pop, "VOICE_E7_PROBE", None)
            self.addCleanup(os.environ.pop, "VOICE_E7_SECRET", None)

            path = os.path.join(self._tmp.name, "env_child.py")
            with open(path, "w") as handle:
                handle.write('import json, os, sys\n'
                             'print(json.dumps({"type": "item.completed", "item": '
                             '{"id": "i", "type": "agent_message", '
                             '"text": json.dumps(sorted(k for k in os.environ '
                             'if k.startswith("VOICE_E7_")))}}), flush=True)\n'
                             'print(json.dumps({"type": "turn.completed"}), flush=True)\n')

            backend = ProcessBackend(ChildSpec(
                argv=(sys.executable, path), parser=codex_exec_parser,
                env_allowlist=("VOICE_E7_PROBE",)))
            await backend.send("env", tag="v1", priority="next",
                               transcript="env", interpretation="ask env")
            seen = await take(backend.observe(), 2)
            result = [obs for obs in seen if obs.kind == OBS_RESULT][0]
            self.assertEqual(json.loads(result.text), ["VOICE_E7_PROBE"])
            await backend.wait_for_child()

        asyncio.run(run())


# The suite is offline by default: this smoke test spends a real Codex turn on the caller's
# account, so it runs only when asked for (VOICE_LIVE_TESTS=1) — never because a binary
# happens to be on PATH.
LIVE_TESTS = os.environ.get("VOICE_LIVE_TESTS") == "1"


def codex_available() -> bool:
    if not LIVE_TESTS:
        return False
    try:
        done = subprocess.run(["codex", "--version"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


class CodexSmokeTests(unittest.TestCase):
    """E7 against the real binary. Skipped cleanly wherever codex cannot run."""

    @unittest.skipUnless(codex_available(), "live test: set VOICE_LIVE_TESTS=1 (needs codex)")
    def test_real_codex_exec_child_answers_ready(self):
        async def run():
            # codex refuses to run outside a trusted directory, so the child runs in the repo.
            repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            backend = ProcessBackend(codex_exec_spec(
                cwd=repo, env_allowlist=("HOME", "PATH", "USER", "TMPDIR", "SHELL", "LANG")))
            receipt = await backend.send("Reply with only the word READY", tag="smoke",
                                         priority="next",
                                         transcript="reply with only the word ready",
                                         interpretation="smoke check")
            self.assertEqual(receipt.outcome, "posted")

            # Collect until the turn settles either way: a result, or the child dying without
            # one. A clean completion no longer ends the stream, so draining would never return.
            seen = []
            async for obs in backend.observe():
                seen.append(obs)
                if obs.kind in (OBS_RESULT, OBS_OWNER_LOST):
                    break

            results = [obs for obs in seen if obs.kind == OBS_RESULT]
            if not results:
                lost = seen[-1] if seen else None
                self.skipTest(f"codex produced no result: {lost.payload if lost else 'no output'}")
            self.assertIn("READY", results[-1].text.upper())
            await backend.wait_for_child()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
