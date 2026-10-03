"""A permission prompt reported by the plugin's hooks module (permission.json), end to end
over fakes: announced as a dialog the operator answers at the keyboard, never armed, and said
once when the pane sees the same prompt (DESIGN.md §Consent, §Backend split)."""
from __future__ import annotations

import asyncio
import unittest

from voice.agent.broker import Broker
from voice.agent.loop import DIALOG_WAITING, PERMISSION_ASKED
from voice.backend import base
from voice.backend.claude_code.adapter import ClaudeCodeBackend
from voice.tests.core_fakes import FakeBackend
from voice.tests.test_backend_claude_code_adapter import FakePane, FakeRelay, FakeTailer
from voice.tests.test_core_loop import make_loop

ECHO_S = 5.0
NO_DIALOG = {"ok": True, "lost": None, "classification": {"class": "working", "dialog": None}}
ASKED = "Claude is asking to use Bash: touch hello.txt — answer on your keyboard"


def screen(command="touch hello.txt", *, occurrence=1, digest=None):
    """A Bash permission dialog as the pane reads it (whitespace collapsed by the pane)."""
    return {"ok": True, "lost": None, "classification": {
        "class": "dialog",
        "dialog": {"hash": digest or command, "occurrence": occurrence,
                   "question": f"Bash command {command} Do you want to proceed?",
                   "options": ((1, "Yes"), (2, "Yes, and don't ask again"), (3, "No")),
                   "present": True},
    }}


class Clock:
    def __init__(self, at: float = 100.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class CountingPane(FakePane):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def observe(self, *, binding, pending_tool_ids):
        self.reads += 1
        return await super().observe(binding=binding, pending_tool_ids=pending_tool_ids)


def adapter(*, dialogs: bool, clock: Clock | None = None):
    pane = CountingPane()
    backend = ClaudeCodeBackend(pane=pane, tailer=FakeTailer(), relay=FakeRelay(),
                                binding={"session_id": "s1", "dialogs": dialogs},
                                now=clock or Clock(), echo_s=ECHO_S)
    return backend, pane


def announce(backend, tool="Bash", summary="touch hello.txt", at=1790000000.25):
    return asyncio.run(backend.announce_permission(tool=tool, summary=summary, at=at))


class ScreenlessAnnouncement(unittest.TestCase):
    def test_the_prompt_is_one_open_dialog_with_no_options(self):
        backend, pane = adapter(dialogs=False)
        self.assertTrue(announce(backend))
        [obs] = backend.drain()
        self.assertEqual(obs.kind, base.OBS_DIALOG)
        self.assertEqual(obs.payload["transition"], "open")
        self.assertEqual(obs.dialog.prompt, ASKED)
        self.assertEqual(obs.dialog.options, ())
        self.assertEqual(obs.dialog.occurrence_id, "hook:1790000000.25")
        self.assertEqual(pane.reads, 0, "a screen-less binding has no screen to read")
        self.assertIsNone(asyncio.run(backend.state()).dialog,
                          "the backend's dialog stays the screen's alone")

    def test_a_tool_without_a_summary_is_named_alone(self):
        backend, _pane = adapter(dialogs=False)
        announce(backend, tool="mcp__github__create_issue", summary="mcp__github__create_issue")
        announce(backend, tool="WebFetch", summary="", at=2.0)
        self.assertEqual([o.dialog.prompt for o in backend.drain()],
                         ["Claude is asking to use mcp__github__create_issue — answer on your keyboard",
                          "Claude is asking to use WebFetch — answer on your keyboard"])

    def test_the_loop_narrates_it_and_nothing_can_answer_it(self):
        backend, _pane = adapter(dialogs=False)
        announce(backend)
        [obs] = backend.drain()
        for broker in (Broker(terminal_only=True), Broker()):
            loop, session, fake, _l, _k = make_loop(backend=FakeBackend(), broker=broker)
            asyncio.run(loop.handle_observation(obs))
            self.assertIsNone(loop.broker.armed, "no options: never armable, whatever the mode")
            self.assertEqual(fake.answers, [])
            if broker.terminal_only:
                [(item, origin)] = session.items
                self.assertEqual(origin, "narration")
                text = item["content"][0]["text"]
                self.assertIn(PERMISSION_ASKED, text, "said as a request")
                self.assertNotIn(DIALOG_WAITING, text, "not as a dialog known to be on screen")
                self.assertIn(ASKED, text)

    def test_after_owner_loss_nothing_is_said(self):
        backend, _pane = adapter(dialogs=False)
        backend.ingest_pane_observation({"ok": False, "lost": "owner_exited"})
        backend.drain()
        self.assertFalse(announce(backend))
        self.assertEqual(backend.drain(), [])

    def test_it_wakes_a_parked_stream(self):
        async def never():
            await asyncio.Event().wait()

        backend = ClaudeCodeBackend(pane=FakePane(), tailer=FakeTailer(), relay=FakeRelay(),
                                    binding={"session_id": "s1", "dialogs": False}, wake=never)

        async def scenario():
            async def first_dialog():
                async for obs in backend.observe():
                    if obs.kind == base.OBS_DIALOG:
                        return obs

            stream = asyncio.ensure_future(first_dialog())
            await asyncio.sleep(0.05)                    # parked on the transcript wake
            await backend.announce_permission(tool="Bash", summary="ls", at=1.0)
            return await asyncio.wait_for(stream, 1.0)

        self.assertEqual(asyncio.run(scenario()).dialog.prompt,
                         "Claude is asking to use Bash: ls — answer on your keyboard")


class OrcaSaysItOnce(unittest.TestCase):
    """On a pane that reads dialogs, one request has two witnesses; the operator hears it once.
    Only the SAME request is silenced: its tool and summary must be in the dialog's text."""

    def test_the_screen_first_then_the_hook_stays_quiet(self):
        clock = Clock(100.0)
        backend, pane = adapter(dialogs=True, clock=clock)
        backend.ingest_pane_observation(screen())
        [said] = backend.drain()
        self.assertEqual(said.payload["transition"], "open")
        clock.at = 102.0
        pane.results.append(screen())                     # still on screen when re-read
        self.assertFalse(announce(backend))
        self.assertEqual(pane.reads, 1, "the screen is read before deciding")
        self.assertEqual(backend.drain(), [])

    def test_the_fresh_read_finds_it_and_the_hook_stays_quiet(self):
        backend, pane = adapter(dialogs=True)
        pane.results.append(screen())
        self.assertFalse(announce(backend))
        [obs] = backend.drain()
        self.assertEqual(obs.dialog.occurrence_id, "touch hello.txt:1", "the screen's own")

    def test_a_wrapped_command_on_screen_still_matches(self):
        backend, pane = adapter(dialogs=True)
        pane.results.append(screen("touch hel lo.txt"))     # wrapped mid-word by the terminal
        self.assertFalse(announce(backend))

    def test_the_hook_first_then_the_screen_stays_quiet(self):
        clock = Clock(100.0)
        backend, pane = adapter(dialogs=True, clock=clock)
        pane.results.append(NO_DIALOG)                    # not drawn yet
        self.assertTrue(announce(backend))
        [hook] = backend.drain()
        self.assertEqual(hook.dialog.options, ())
        clock.at = 101.0
        backend.ingest_pane_observation(screen())
        self.assertEqual(backend.drain(), [])
        self.assertEqual(asyncio.run(backend.state()).dialog.occurrence_id, "touch hello.txt:1",
                         "quiet, but the screen's dialog is still tracked")
        # One announcement pays for one sighting: the next request on screen is said again.
        backend.ingest_pane_observation(NO_DIALOG)
        [closed] = backend.drain()
        self.assertEqual(closed.payload["transition"], "closed")
        clock.at = 102.0
        backend.ingest_pane_observation(screen())
        [again] = backend.drain()
        self.assertEqual(again.payload["transition"], "open")

    def test_a_then_b_while_a_is_still_drawn(self):
        """The reviewer's case: A is on screen (said by the pane); B's request arrives before
        the pane has seen B. B is a different request, so its hook is said."""
        clock = Clock(100.0)
        backend, pane = adapter(dialogs=True, clock=clock)
        backend.ingest_pane_observation(screen("rm -rf build/"))
        backend.drain()
        clock.at = 101.0
        pane.results.append(screen("rm -rf build/"))      # A still drawn at the fresh read
        self.assertTrue(announce(backend))                # B: touch hello.txt
        [b] = backend.drain()
        self.assertEqual((b.payload["source"], b.dialog.prompt), ("hook", ASKED))
        # Then B replaces A on screen: `replaced` is never narrated, and B was heard already.
        clock.at = 102.0
        backend.ingest_pane_observation(screen())
        [replaced] = backend.drain()
        self.assertEqual(replaced.payload["transition"], "replaced")

    def test_a_replacement_on_screen_does_not_silence_its_hook(self):
        """B arrived on screen as `replaced` (never said by the pane), and it is this request:
        the hook is still said."""
        clock = Clock(100.0)
        backend, pane = adapter(dialogs=True, clock=clock)
        backend.ingest_pane_observation(screen("rm -rf build/"))
        backend.drain()
        clock.at = 101.0
        pane.results.append(screen())                     # B replaced A before the hook arrived
        self.assertTrue(announce(backend))
        kinds = [(o.payload["transition"], o.payload.get("source")) for o in backend.drain()]
        self.assertEqual(kinds, [("replaced", None), ("open", "hook")])

    def test_an_unrelated_screen_after_the_hook_is_still_said(self):
        clock = Clock(100.0)
        backend, pane = adapter(dialogs=True, clock=clock)
        pane.results.append(NO_DIALOG)
        self.assertTrue(announce(backend))                # touch hello.txt
        backend.drain()
        clock.at = 101.0
        backend.ingest_pane_observation(screen("rm -rf build/"))
        [opened] = backend.drain()
        self.assertEqual(opened.payload["transition"], "open")

    def test_outside_the_window_both_are_said(self):
        clock = Clock(100.0)
        backend, pane = adapter(dialogs=True, clock=clock)
        backend.ingest_pane_observation(screen())
        backend.drain()
        clock.at = 100.0 + ECHO_S + 1
        pane.results.append(screen())
        self.assertTrue(announce(backend))
        self.assertEqual([o.payload.get("source") for o in backend.drain()], ["hook"])

    def test_a_request_without_a_summary_never_silences(self):
        backend, pane = adapter(dialogs=True)
        pane.results.append(screen())
        self.assertTrue(announce(backend, summary=""))


if __name__ == "__main__":
    unittest.main()
