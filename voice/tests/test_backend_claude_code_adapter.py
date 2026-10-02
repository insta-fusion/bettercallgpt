"""DESIGN.md §Acceptance E3, E5, E6 — receipt attribution, result fidelity, and the owner-loss fence.

The adapter is where the separation is provable: these tests assert not only what it emits but
what it NO LONGER decides. The channel table, the `Q:` extraction, the merged state word, the
queue-release flag and the Escape effect each have a test saying they are gone.

E6's second half is a gap found in the split: no existing test asserted that a `lost` reason
actually refuses the NEXT actuation. It does now.
"""
from __future__ import annotations

import asyncio
import unittest

from voice.backend import base
from voice.backend.claude_code.adapter import (
    ClaudeCodeBackend,
    mint_tag,
    render_tag,
    result_id_for,
)


def run(coro):
    return asyncio.run(coro)


class FakeRelay:
    """Records promote/actuate and replays scripted outcomes."""

    def __init__(self) -> None:
        self.promoted: list[dict] = []
        self.actuated: list[dict] = []
        self.resolved: list[tuple[str, str]] = []
        self.promote_result: dict | None = None
        self.actuate_result: dict | None = None

    async def promote(self, instruction, *, probe: bool = False):
        self.promoted.append(dict(instruction))
        if self.promote_result is not None:
            return self.promote_result
        return {"ok": True, "kind": "relay", "instruction_id": instruction["instruction_id"],
                "tag": instruction["tag"], "text": instruction["text"],
                "interrupt": instruction.get("interrupt", False), "probe": probe}

    async def actuate(self, authorization):
        self.actuated.append(dict(authorization))
        if self.actuate_result is not None:
            return self.actuate_result
        return {"ok": True, "state": "posted", "sent_bytes": True, "never_started": False,
                "tag": authorization["tag"]}

    def resolve(self, tag, *, how):
        self.resolved.append((tag, how))
        return True


class FakePane:
    def __init__(self, pressable: bool = False) -> None:
        self.presses: list[tuple[str, str]] = []
        self.results: list[dict] = []
        if pressable:
            self.press = self._press          # only bound when the test wants a keyboard

    async def _press(self, *, occurrence_id, key):
        self.presses.append((occurrence_id, key))
        return {"ok": True}

    async def observe(self, *, binding, pending_tool_ids):
        return self.results.pop(0) if self.results else {"ok": True, "lost": None,
                                                         "classification": None}


class FakeTailer:
    state: dict = {"pending_tool_ids": frozenset()}

    def __init__(self, batches=None) -> None:
        self.batches = list(batches or [])

    def poll(self):
        return self.batches.pop(0) if self.batches else []


def make_adapter(*, pressable=False):
    pane, tailer, relay = FakePane(pressable=pressable), FakeTailer(), FakeRelay()
    adapter = ClaudeCodeBackend(pane=pane, tailer=tailer, relay=relay,
                                binding={"session_id": "s1"})
    return adapter, pane, tailer, relay


def permission_screen(occurrence=1, target="build/"):
    return {"ok": True, "lost": None, "classification": {
        "class": "dialog",
        "dialog": {"hash": "h1", "occurrence": occurrence,
                   "question": f"Bash wants to run rm -rf {target} ?",
                   "options": ((1, "Yes"), (2, "Yes, and don't ask again"), (3, "No")),
                   "present": True},
    }}


class RecordingTailer(FakeTailer):
    def __init__(self) -> None:
        super().__init__()
        self.registered: list[tuple] = []

    def register(self, tag, wire, *, producer_pid=None, strict_peer=False):
        self.registered.append((tag, wire, producer_pid, strict_peer))


class QualifiableRelay(FakeRelay):
    qualified = False

    def mark_qualified(self, ok: bool) -> None:
        self.qualified = bool(ok)


class QualificationTests(unittest.IsolatedAsyncioTestCase):
    """`promote` refuses every operator send until the relay is qualified, and only the
    daemon can qualify it: one strict-peer self-test frame whose transcript receipt proves
    the socket leads to the bound session. Measured live: the first port never posted it,
    so the first real dispatch was refused `relay_not_qualified`."""

    def _adapter(self):
        tailer, relay = RecordingTailer(), QualifiableRelay()
        flips: list[int] = []
        adapter = ClaudeCodeBackend(pane=FakePane(), tailer=tailer, relay=relay,
                                    binding={"session_id": "s1"},
                                    on_qualified=lambda: flips.append(1))
        return adapter, tailer, relay, flips

    async def test_the_probe_is_posted_strict_peer_and_its_receipt_qualifies(self):
        import os

        adapter, tailer, relay, flips = self._adapter()
        self.assertEqual(await adapter.qualify(), "")
        (tag, wire, pid, strict), = tailer.registered
        self.assertTrue(tag.startswith("⟨v#"))
        self.assertEqual(wire, f"{ClaudeCodeBackend.PROBE_TEXT} {tag}")
        self.assertEqual(pid, os.getpid())
        self.assertTrue(strict, "the probe proves identity; an unstamped carrier must not credit it")
        self.assertTrue(relay.actuated[0]["probe"], "the frame is authorized as a PROBE")
        self.assertFalse(relay.actuated[0]["interrupt"])
        # Posting is not qualification.
        self.assertFalse(relay.qualified)
        self.assertEqual(flips, [])
        adapter.ingest_transcript_event({"kind": "consumed", "tag": tag, "turn_id": "t1"})
        self.assertTrue(relay.qualified)
        self.assertEqual(flips, [1])
        # Once. A replayed receipt is not a second qualification.
        adapter.ingest_transcript_event({"kind": "consumed", "tag": tag, "turn_id": "t1"})
        self.assertEqual(flips, [1])

    async def test_a_receipt_for_another_tag_does_not_qualify(self):
        adapter, tailer, relay, flips = self._adapter()
        await adapter.qualify()
        adapter.ingest_transcript_event({"kind": "consumed", "tag": "⟨v#other⟩",
                                         "turn_id": "t1"})
        self.assertFalse(relay.qualified)
        self.assertEqual(flips, [])

    async def test_a_refused_probe_posts_nothing_and_forgets_its_tag(self):
        adapter, tailer, relay, flips = self._adapter()
        relay.promote_result = {"ok": False, "refused": "busy"}
        self.assertEqual(await adapter.qualify(), "busy")
        self.assertEqual(relay.actuated, [])
        (tag, *_), = tailer.registered
        adapter.ingest_transcript_event({"kind": "consumed", "tag": tag, "turn_id": "t1"})
        self.assertFalse(relay.qualified, "a tag that was never posted cannot come back")


class E3ReceiptAttributionTests(unittest.TestCase):
    """E3 — a receipt attributes to the turn that consumed the tag; result text passes whole."""

    def test_E3_a_consumed_receipt_names_the_turn_that_consumed_it(self):
        adapter, _p, _t, relay = make_adapter()
        adapter.ingest_transcript_event({"kind": "consumed", "tag": "⟨v#aa⟩",
                                         "turn_id": "turn-A"})
        [obs] = adapter.drain()
        self.assertEqual(obs.kind, base.OBS_RECEIPT)
        self.assertEqual(obs.tag, "⟨v#aa⟩")
        self.assertEqual(obs.turn_id, "turn-A")
        self.assertEqual(obs.payload["disposition"], "consumed")

    def test_E3_a_consumed_receipt_releases_the_relay_slot(self):
        adapter, _p, _t, relay = make_adapter()
        adapter.ingest_transcript_event({"kind": "consumed", "tag": "⟨v#aa⟩",
                                         "turn_id": "turn-A"})
        self.assertEqual(relay.resolved, [("⟨v#aa⟩", "consumed")])

    def test_E3_a_queued_receipt_does_not_release_the_slot(self):
        """Queued means Claude has not read it yet. The frame is still owed."""
        adapter, _p, _t, relay = make_adapter()
        adapter.ingest_transcript_event({"kind": "queued", "tag": "⟨v#aa⟩",
                                         "turn_id": "turn-A"})
        self.assertEqual(relay.resolved, [])
        [obs] = adapter.drain()
        self.assertEqual(obs.payload["disposition"], "queued")

    def test_E3_a_withdrawn_receipt_is_reported_and_releases_nothing(self):
        adapter, _p, _t, relay = make_adapter()
        adapter.ingest_transcript_event({"kind": "withdrawn", "tag": "⟨v#aa⟩",
                                         "turn_id": "turn-A"})
        [obs] = adapter.drain()
        self.assertEqual(obs.payload["disposition"], "withdrawn")
        self.assertEqual(relay.resolved, [])

    def test_E3_result_text_passes_whole(self):
        adapter, _p, _t, _r = make_adapter()
        long_text = "第一行\n第二行\n" + "x" * 5000
        adapter.ingest_transcript_event({"kind": "turn_result", "turn_id": "turn-A",
                                         "text": long_text, "inputs": ("a",)})
        result = adapter.drain()[0]
        self.assertEqual(result.kind, base.OBS_RESULT)
        self.assertEqual(result.text, long_text, "never clipped, never reformatted")

    def test_E3_a_result_carries_a_stable_identity(self):
        """So a replay cannot deliver the same result twice."""
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "turn_result", "turn_id": "turn-A",
                                         "text": "done"})
        result = adapter.drain()[0]
        self.assertEqual(result.payload["result_id"], result_id_for("turn-A", "done"))
        self.assertNotEqual(result_id_for("turn-A", "done"), result_id_for("turn-B", "done"))

    def test_E3_a_tag_seen_but_not_credited_emits_nothing(self):
        """`tag_seen_unresolved` is the tailer saying it will not credit this. Nothing
        downstream may act on it."""
        adapter, _p, _t, relay = make_adapter()
        adapter.ingest_transcript_event({"kind": "tag_seen_unresolved", "tag": "⟨v#aa⟩",
                                         "where": "assistant"})
        self.assertEqual(adapter.drain(), [])
        self.assertEqual(relay.resolved, [])


class E5StateAndProgressTests(unittest.TestCase):
    """E5 — the structural facts survive; the semantic labels are gone."""

    def test_E5_mid_turn_text_is_progress_with_no_channel_decision(self):
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "assistant_text", "turn_id": "turn-A",
                                         "text": "[STATUS] still going"})
        [obs] = adapter.drain()
        self.assertEqual(obs.kind, base.OBS_PROGRESS)
        # The prefix is carried, not consumed: no table decided what this line MEANS.
        self.assertEqual(obs.text, "[STATUS] still going")

    def test_E5_a_complete_prefix_does_not_promote_text_to_a_result(self):
        """The channel table is gone. Only a turn that ENDED produces a result."""
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "assistant_text", "turn_id": "turn-A",
                                         "text": "[COMPLETE] all done"})
        kinds = [obs.kind for obs in adapter.drain()]
        self.assertEqual(kinds, [base.OBS_PROGRESS])
        self.assertNotIn(base.OBS_RESULT, kinds)

    def test_E5_a_trailing_question_is_just_text(self):
        """The `Q:` chain minted a question object that rewrote the operator's next sentence.
        Gone entirely."""
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "turn_result", "turn_id": "turn-A",
                                         "text": "done.\nQ: should I deploy?"})
        result = adapter.drain()[0]
        self.assertIn("Q: should I deploy?", result.text)
        self.assertFalse(hasattr(adapter, "open_question"))
        self.assertFalse(hasattr(adapter, "answer_text"))

    def test_E5_activity_and_dialog_are_two_independent_facts(self):
        """The old code overrode the activity word to `dialog` so a router could switch on one
        value. Merging them is how a routing decision hid inside an observation."""
        adapter, pane, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "turn_opened", "turn_id": "turn-A"})
        adapter.ingest_pane_observation(permission_screen())
        state = run(adapter.state())
        self.assertEqual(state.activity, "working")
        self.assertIsNotNone(state.dialog)
        self.assertIn(state.activity, ("idle", "working", "unknown"))

    def test_E5_a_turn_end_reports_idle_and_clears_the_turn(self):
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "turn_opened", "turn_id": "turn-A"})
        adapter.drain()
        adapter.ingest_transcript_event({"kind": "turn_result", "turn_id": "turn-A",
                                         "text": "done"})
        kinds = [obs.kind for obs in adapter.drain()]
        self.assertEqual(kinds, [base.OBS_RESULT, base.OBS_STATE])
        state = run(adapter.state())
        self.assertEqual(state.activity, "idle")
        self.assertIsNone(state.turn_id)

    def test_E5_nothing_sets_a_queue_release_flag(self):
        """The semantic queue and its release flag went with bucket (c); the loop owns the
        durable `next` queue now."""
        adapter, _p, _t, _r = make_adapter()
        self.assertFalse(hasattr(adapter, "queue_release_pending"))
        self.assertFalse(hasattr(adapter, "release_queued"))


class E2DialogTransitionTests(unittest.TestCase):
    """The adapter's half of E2: open / replaced / closed, quiet when nothing changed."""

    def test_a_new_dialog_opens(self):
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_pane_observation(permission_screen())
        [obs] = adapter.drain()
        self.assertEqual(obs.kind, base.OBS_DIALOG)
        self.assertEqual(obs.payload["transition"], "open")
        self.assertEqual(obs.dialog.occurrence_id, "h1:1")
        self.assertEqual(obs.dialog.kind, "dialog")

    def test_the_same_dialog_still_up_says_nothing(self):
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_pane_observation(permission_screen())
        adapter.drain()
        adapter.ingest_pane_observation(permission_screen())
        self.assertEqual(adapter.drain(), [], "a redraw is not a new question")

    def test_a_new_occurrence_replaces(self):
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_pane_observation(permission_screen(occurrence=1))
        adapter.drain()
        adapter.ingest_pane_observation(permission_screen(occurrence=2))
        [obs] = adapter.drain()
        self.assertEqual(obs.payload["transition"], "replaced")
        self.assertEqual(obs.dialog.occurrence_id, "h1:2")

    def test_a_vanished_dialog_closes(self):
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_pane_observation(permission_screen())
        adapter.drain()
        adapter.ingest_pane_observation({"ok": True, "lost": None,
                                         "classification": {"class": "working", "dialog": None}})
        [obs] = adapter.drain()
        self.assertEqual(obs.payload["transition"], "closed")

    def test_the_dialog_carries_the_verbatim_prompt_and_rendered_options(self):
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_pane_observation(permission_screen())
        [obs] = adapter.drain()
        self.assertIn("rm -rf build/", obs.dialog.prompt)
        self.assertEqual(obs.dialog.options, (("1", "Yes"),
                                              ("2", "Yes, and don't ask again"),
                                              ("3", "No")))

    def test_action_and_scope_are_always_none(self):
        """The adapter parses no meaning out of a prompt. It used to run English regexes to name
        an action and a scope, which made this layer decide what a dialog MEANS — and a mis-parse
        would have had the operator confirming a sentence that misdescribed the effect."""
        for prompt in ("Bash wants to run rm -rf build/ ?", "Do you want to proceed?",
                       "要不要继续？", ""):
            with self.subTest(prompt=prompt):
                screen = permission_screen()
                screen["classification"]["dialog"]["question"] = prompt
                adapter, _p, _t, _r = make_adapter()
                adapter.ingest_pane_observation(screen)
                emitted = adapter.drain()
                if not emitted:
                    continue          # an empty prompt yields no dialog at all
                self.assertIsNone(emitted[0].dialog.action)
                self.assertIsNone(emitted[0].dialog.scope)

    def test_the_adapter_exposes_no_extraction_function(self):
        """Its absence is the assertion: there is no place left that reads a prompt for meaning."""
        from voice.backend.claude_code import adapter as adapter_mod
        self.assertFalse(hasattr(adapter_mod, "extract_action_scope"))

    def test_a_dialog_with_too_few_options_is_not_reported(self):
        """Nothing can be named by position, so the broker could not arm — and an unnameable
        widget must not reach it at all."""
        screen = permission_screen()
        screen["classification"]["dialog"]["options"] = ((1, "OK"),)
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_pane_observation(screen)
        self.assertEqual(adapter.drain(), [])

    def _agent_wait_screen(self, options=()):
        wait = {"waiting": True, "source": "hook", "reason": None, "since": None}
        return {"ok": True, "lost": None, "classification": {
            "class": "dialog", "agent_wait": wait,
            "dialog": {"hash": "w1", "occurrence": 1, "agent_wait": wait,
                       "question": "waiting on a prompt in the terminal (Orca agentWait: "
                                   "interactive prompt, via hook)",
                       "options": options, "present": True},
        }}

    def test_a_wait_orca_reported_opens_with_no_options_and_never_arms(self):
        """The operator hears that the agent waits; the broker still needs two options, and
        no index is on screen to press."""
        from voice.agent.broker import Broker

        adapter, pane, _t, _r = make_adapter(pressable=True)
        adapter.ingest_pane_observation(self._agent_wait_screen())
        [obs] = adapter.drain()
        self.assertEqual(obs.payload["transition"], "open")
        self.assertEqual(obs.dialog.options, ())
        broker = Broker()
        self.assertIsNone(broker.arm(obs.dialog, 1))
        self.assertEqual(broker.history[-1].reason, "not_enumerable")
        receipt = run(adapter.answer(obs.dialog.occurrence_id, "1"))
        self.assertEqual(receipt.outcome, "refused")
        self.assertEqual(pane.presses, [])

    def test_a_wait_record_with_partial_options_is_not_reported(self):
        """Options on a wait record mean a screen widget we could not finish reading."""
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_pane_observation(self._agent_wait_screen(options=((1, "OK"),)))
        self.assertEqual(adapter.drain(), [])


class E6OwnerLossFenceTests(unittest.TestCase):
    """E6 — a gap found in the split: a `lost` reason refuses the NEXT actuation."""

    def _lost(self):
        adapter, pane, _t, relay = make_adapter(pressable=True)
        adapter.ingest_pane_observation(permission_screen())
        adapter.drain()
        adapter.ingest_pane_observation({"ok": True, "lost": "owner_pid_reused",
                                         "classification": None})
        return adapter, pane, relay

    def test_E6_owner_loss_is_reported_once(self):
        adapter, _p, _r = self._lost()
        [obs] = adapter.drain()
        self.assertEqual(obs.kind, base.OBS_OWNER_LOST)
        self.assertEqual(obs.text, "owner_pid_reused")
        adapter.ingest_pane_observation({"ok": True, "lost": "owner_pid_reused",
                                         "classification": None})
        self.assertEqual(adapter.drain(), [], "terminal, not repeated")

    def test_E6_the_next_send_is_refused(self):
        adapter, _pane, relay = self._lost()
        receipt = run(adapter.send("do it", tag="req-1", priority="now",
                                   transcript="do it", interpretation="do it"))
        self.assertEqual(receipt.outcome, "refused")
        self.assertIn("owner_lost", receipt.reason)
        self.assertEqual(relay.promoted, [], "nothing may reach the relay after owner loss")

    def test_E6_the_next_dialog_answer_is_refused(self):
        adapter, pane, _relay = self._lost()
        receipt = run(adapter.answer("h1:1", "1"))
        self.assertEqual(receipt.outcome, "refused")
        self.assertIn("owner_lost", receipt.reason)
        self.assertEqual(pane.presses, [], "no key is pressed after owner loss")

    def test_E6_owner_loss_shows_in_the_state(self):
        adapter, _p, _r = self._lost()
        self.assertEqual(run(adapter.state()).owner_lost, "owner_pid_reused")


class TypedLinesReachTheLoop(unittest.TestCase):
    def test_a_turn_opened_with_text_emits_a_typed_observation(self):
        from voice.backend import base
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "turn_opened", "turn_id": "t1",
                                         "text": "git status"})
        typed = [o for o in adapter._pending if o.kind == base.OBS_TYPED]
        self.assertEqual([(o.turn_id, o.text) for o in typed], [("t1", "git status")])

    def test_a_turn_opened_without_text_emits_only_state(self):
        from voice.backend import base
        adapter, _p, _t, _r = make_adapter()
        adapter.ingest_transcript_event({"kind": "turn_opened", "turn_id": "t1", "text": ""})
        self.assertEqual([o.kind for o in adapter._pending], [base.OBS_STATE])


class SendTests(unittest.TestCase):
    def test_a_now_send_marks_the_relay_frame_as_an_interruption(self):
        adapter, _p, _t, relay = make_adapter()
        run(adapter.send("stop", tag="req-1", priority="now",
                         transcript="先停掉", interpretation="stop"))
        self.assertTrue(relay.promoted[0]["interrupt"])

    def test_a_next_send_is_not_an_interruption(self):
        """`next` is the loop's durable queue; by the time it reaches here it is an ordinary
        write."""
        adapter, _p, _t, relay = make_adapter()
        run(adapter.send("lint", tag="req-1", priority="next",
                         transcript="跑 lint", interpretation="lint"))
        self.assertFalse(relay.promoted[0]["interrupt"])

    def test_a_posted_frame_is_posted_not_applied(self):
        """`posted` means the bytes left this process, not that Claude read them. The transcript
        receipt proves consumption, and it arrives later."""
        adapter, _p, _t, _r = make_adapter()
        receipt = run(adapter.send("x", tag="req-1", priority="now",
                                   transcript="x", interpretation="x"))
        self.assertEqual(receipt.outcome, "posted")

    def test_a_write_that_began_and_cannot_be_proven_is_uncertain(self):
        adapter, _p, _t, relay = make_adapter()
        relay.actuate_result = {"ok": True, "state": "post_unknown", "sent_bytes": True,
                                "never_started": False, "tag": "⟨v#aa⟩"}
        receipt = run(adapter.send("x", tag="req-1", priority="now",
                                   transcript="x", interpretation="x"))
        self.assertEqual(receipt.outcome, "uncertain")

    def test_a_proven_never_started_failure_is_refused_not_uncertain(self):
        adapter, _p, _t, relay = make_adapter()
        relay.actuate_result = {"ok": False, "refused": "relay_connect_failed",
                                "never_started": True}
        receipt = run(adapter.send("x", tag="req-1", priority="now",
                                   transcript="x", interpretation="x"))
        self.assertEqual(receipt.outcome, "refused")

    def test_a_failure_after_the_write_began_is_uncertain_and_never_resent(self):
        adapter, _p, _t, relay = make_adapter()
        relay.actuate_result = {"ok": False, "refused": "already_attempted",
                                "never_started": False}
        receipt = run(adapter.send("x", tag="req-1", priority="now",
                                   transcript="x", interpretation="x"))
        self.assertEqual(receipt.outcome, "uncertain")

    def test_a_promote_refusal_is_a_refusal(self):
        adapter, _p, _t, relay = make_adapter()
        relay.promote_result = {"ok": False, "refused": "relay_identity_lost"}
        receipt = run(adapter.send("x", tag="req-1", priority="now",
                                   transcript="x", interpretation="x"))
        self.assertEqual(receipt.outcome, "refused")
        self.assertEqual(receipt.reason, "relay_identity_lost")


class AnswerTests(unittest.TestCase):
    def _armed(self, pressable=True):
        adapter, pane, _t, _r = make_adapter(pressable=pressable)
        adapter.ingest_pane_observation(permission_screen())
        adapter.drain()
        return adapter, pane

    def test_an_index_presses_that_rendered_option(self):
        adapter, pane = self._armed()
        receipt = run(adapter.answer("h1:1", "1"))
        self.assertEqual(receipt.outcome, "applied")
        self.assertEqual(pane.presses, [("h1:1", "1")])

    def test_every_rendered_index_is_pressable_by_position(self):
        for index in ("1", "2", "3"):
            with self.subTest(index=index):
                adapter, pane = self._armed()
                run(adapter.answer("h1:1", index))
                self.assertEqual(pane.presses, [("h1:1", index)])

    def test_an_intent_word_is_not_a_choice(self):
        """`allow` and `deny` used to be mapped onto whichever label began yes or no, so a
        reworded or reordered option list silently changed which key an approval landed on."""
        for word in ("allow", "deny", "yes", "no"):
            with self.subTest(word=word):
                adapter, pane = self._armed()
                receipt = run(adapter.answer("h1:1", word))
                self.assertEqual(receipt.outcome, "refused")
                self.assertEqual(receipt.reason, "choice_not_on_screen")
                self.assertEqual(pane.presses, [])

    def test_an_index_this_occurrence_did_not_render_refuses(self):
        adapter, pane = self._armed()
        receipt = run(adapter.answer("h1:1", "9"))
        self.assertEqual(receipt.reason, "choice_not_on_screen")
        self.assertEqual(pane.presses, [])

    def test_a_changed_occurrence_refuses(self):
        """The stale-authorization bug: the operator approved a question no longer being asked."""
        adapter, pane = self._armed()
        receipt = run(adapter.answer("h1:99", "1"))
        self.assertEqual(receipt.outcome, "refused")
        self.assertEqual(receipt.reason, "occurrence_changed")
        self.assertEqual(pane.presses, [])

    def test_answering_with_no_dialog_on_screen_refuses(self):
        adapter, _p, _t, _r = make_adapter(pressable=True)
        receipt = run(adapter.answer("h1:1", "1"))
        self.assertEqual(receipt.reason, "no_dialog")

    def test_a_binding_with_no_keyboard_surface_is_terminal_only(self):
        """Refusing is the honest outcome: the dialog stays on screen and the operator answers
        it there."""
        adapter, _pane = self._armed(pressable=False)
        receipt = run(adapter.answer("h1:1", "1"))
        self.assertEqual(receipt.outcome, "refused")
        self.assertEqual(receipt.reason, "terminal_only")


class CancelTests(unittest.TestCase):
    def test_cancel_is_refused_because_a_stop_is_a_now_send(self):
        """No Escape effect exists. A `now` send both aborts the turn AND tells the agent why;
        a bare keystroke would do the first without the second."""
        adapter, _p, _t, _r = make_adapter()
        receipt = run(adapter.cancel("turn-A"))
        self.assertEqual(receipt.outcome, "refused")
        self.assertEqual(receipt.reason, "cancel_is_a_now_send")


class TagTests(unittest.TestCase):
    def test_the_wire_tag_shape_is_the_harnesss_own(self):
        self.assertEqual(render_tag("abc123"), "⟨v#abc123⟩")
        self.assertTrue(mint_tag().startswith("⟨v#"))




if __name__ == "__main__":
    unittest.main()


class QuietPollDoesNotEndTheStreamTests(unittest.TestCase):
    """Measured 2026-09-20: the observe stream ended on the first quiet poll, so the backend
    ear died right after startup and no result ever reached the voice. With a wake injected,
    a quiet poll waits for the transcript to change and the stream goes on."""

    def test_the_stream_survives_a_quiet_poll_when_a_wake_is_injected(self):
        class Wake:
            calls = 0

            async def __call__(self):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("test over")

        wake = Wake()
        tailer = FakeTailer(batches=[[], [{"kind": "turn_opened", "turn_id": "t1"}]])
        adapter = ClaudeCodeBackend(pane=FakePane(), tailer=tailer, relay=FakeRelay(),
                                    binding={"session_id": "s1"}, wake=wake)

        async def collect():
            seen = []
            try:
                async for obs in adapter.observe():
                    seen.append(obs.kind)
            except RuntimeError:
                pass
            return seen

        seen = asyncio.run(collect())
        self.assertEqual(wake.calls, 2)
        self.assertIn("state", seen)

    def test_a_fresh_read_that_finds_a_dialog_wakes_a_parked_stream(self):
        async def never():
            await asyncio.Event().wait()

        pane = FakePane()
        adapter = ClaudeCodeBackend(pane=pane, tailer=FakeTailer(), relay=FakeRelay(),
                                    binding={"session_id": "s1"}, wake=never)

        async def scenario():
            seen = []

            async def collect():
                async for obs in adapter.observe():
                    seen.append(obs.kind)
                    if obs.kind == "dialog":
                        return

            stream = asyncio.ensure_future(collect())
            await asyncio.sleep(0.05)                    # parked on the transcript wake
            pane.results.append(permission_screen())
            await adapter.refresh()
            await asyncio.wait_for(stream, 1.0)
            return seen

        self.assertIn("dialog", asyncio.run(scenario()))

    def test_the_wake_cleans_up_before_the_stream_polls_again(self):
        order = []

        class Wake:
            async def __call__(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append("wake cleanup")

        class Tailer(FakeTailer):
            def poll(self):
                order.append("poll")
                return super().poll()

        pane = FakePane()
        adapter = ClaudeCodeBackend(pane=pane, tailer=Tailer(), relay=FakeRelay(),
                                    binding={"session_id": "s1"}, wake=Wake())

        async def scenario():
            async def collect():
                async for _obs in adapter.observe():
                    pass

            stream = asyncio.ensure_future(collect())
            await asyncio.sleep(0.05)
            pane.results.append(permission_screen())
            await adapter.refresh()
            await _until_true(lambda: order.count("poll") >= 2)
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)

        async def _until_true(predicate):
            for _ in range(100):
                if predicate():
                    return
                await asyncio.sleep(0.01)

        asyncio.run(asyncio.wait_for(scenario(), 2.0))
        self.assertEqual(order[:3], ["poll", "wake cleanup", "poll"], order)

    def test_without_a_wake_the_stream_still_ends_as_before(self):
        adapter, _p, _t, _r = make_adapter()

        async def collect():
            return [obs.kind async for obs in adapter.observe()]

        self.assertEqual(asyncio.run(collect()), [])


class ActivityComesFromTheReducer(unittest.TestCase):
    """The daemon's idle close keeps a quiet session open only while the backend is working,
    so "working" must not outlive the work. The reducer's activity is rebuilt on bootstrap
    and rotation with no event announcing it; `state` reads it there."""

    def _adapter(self, activity):
        tailer = FakeTailer()
        tailer.state = {"pending_tool_ids": frozenset(), "activity": activity}
        pane = FakePane()
        adapter = ClaudeCodeBackend(pane=pane, tailer=tailer, relay=FakeRelay(),
                                    binding={"session_id": "s1"})
        return adapter, tailer, pane

    def test_a_session_started_mid_turn_reads_working(self):
        adapter, *_ = self._adapter("working")
        self.assertEqual(asyncio.run(adapter.state()).activity, "working")

    def test_a_rebuild_that_ends_the_turn_reads_idle_without_an_event(self):
        adapter, tailer, _ = self._adapter("working")
        tailer.state = {"pending_tool_ids": frozenset(), "activity": "idle"}
        self.assertEqual(asyncio.run(adapter.state()).activity, "idle")

    def test_a_half_written_record_does_not_hide_a_running_turn(self):
        adapter, tailer, _ = self._adapter("working")
        tailer.state = {"pending_tool_ids": frozenset(), "activity": "unknown"}
        adapter._activity = "working"               # what the turn's events said
        self.assertEqual(asyncio.run(adapter.state()).activity, "working")

    def test_unknown_after_a_rebuild_to_idle_does_not_bring_working_back(self):
        adapter, tailer, _ = self._adapter("working")
        adapter._activity = "working"
        tailer.state = {"pending_tool_ids": frozenset(), "activity": "idle"}
        self.assertEqual(asyncio.run(adapter.state()).activity, "idle")
        tailer.state = {"pending_tool_ids": frozenset(), "activity": "unknown"}
        self.assertEqual(asyncio.run(adapter.state()).activity, "idle")

    def test_refresh_reads_the_pane_so_a_silent_crash_is_seen(self):
        adapter, _, pane = self._adapter("working")
        pane.results.append({"ok": False, "lost": "owner_pid_gone", "classification": None})
        self.assertIsNone(asyncio.run(adapter.state()).owner_lost)
        self.assertEqual(asyncio.run(adapter.refresh()).owner_lost, "owner_pid_gone")
