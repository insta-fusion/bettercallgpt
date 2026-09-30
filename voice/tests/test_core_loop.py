"""The loop's behaviour, end to end over fakes: one decision, two injections, one lock.

Everything here is driven by events and observations, never by waiting: each test says what the
wire reported and asserts what the loop did.
"""
from __future__ import annotations

import asyncio
import json
import unittest

from voice.agent.broker import Broker, option_phrase
from voice.agent.loop import AgentLoop
from voice.backend import base as backend_base
from voice.conversation.log import ConversationLog
from voice.live import base as live_base
from voice.tests.core_fakes import (
    FakeBackend,
    FakeLedger,
    FakeSession,
    FakeSink,
    FakeStrategy,
    event,
    run,
)


def make_loop(**kwargs):
    session = kwargs.pop("session", FakeSession())
    strategy = kwargs.pop("strategy", FakeStrategy())
    backend = kwargs.pop("backend", FakeBackend())
    ledger = kwargs.pop("ledger", FakeLedger())
    sink = kwargs.pop("sink", FakeSink())
    loop = AgentLoop(session=session, strategy=strategy, backend=backend, ledger=ledger,
                     log=kwargs.pop("log", ConversationLog()),
                     broker=kwargs.pop("broker", Broker()), sink=sink, **kwargs)
    return loop, session, backend, ledger, sink


async def speak(loop, item_id, text, seq=1, *, failed=False):
    """One completed user turn on the wire."""
    await loop.handle_live_event(event(seq, live_base.KIND_INPUT_COMMITTED, input_item_id=item_id))
    await loop.handle_live_event(event(seq + 1, live_base.KIND_INPUT_TRANSCRIPT,
                                       input_item_id=item_id,
                                       payload={"text": text, "complete": True, "failed": failed}))


async def respond(loop, response_id, origin="user", seq=10):
    await loop.handle_live_event(event(seq, live_base.KIND_RESPONSE_CREATED,
                                       response_id=response_id, payload={"origin": origin}))


def request(*, transcript, interpretation, priority, item_id, response_id, call_id="call-1"):
    return live_base.Request(transcript=transcript, interpretation=interpretation,
                             priority=priority, input_item_id=item_id, response_id=response_id,
                             call_id=call_id, generation=0)


class A2DispatchTests(unittest.TestCase):
    def test_A2_dispatch_carries_transcript_and_interpretation_labelled_and_unedited(self):
        async def scenario():
            loop, session, backend, ledger, _ = make_loop()
            await speak(loop, "item-1", "把测试跑一下")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="把测试跑一下",
                                               interpretation="运行测试套件",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return session, backend, ledger

        session, backend, ledger = run(scenario())
        self.assertEqual(len(backend.sends), 1)
        self.assertEqual(backend.sends[0]["transcript"], "把测试跑一下")
        self.assertEqual(backend.sends[0]["interpretation"], "运行测试套件")
        self.assertEqual(len(session.outputs), 1)
        self.assertEqual(session.responses, [])

    def test_A2_the_wal_record_precedes_the_actuation(self):
        async def scenario():
            ledger = FakeLedger()
            backend = FakeBackend()
            order: list[str] = []
            original_record = ledger.record_op
            original_send = backend.send

            def record_op(*args, **kwargs):
                order.append("record")
                return original_record(*args, **kwargs)

            async def send(*args, **kwargs):
                order.append("send")
                return await original_send(*args, **kwargs)

            ledger.record_op = record_op
            backend.send = send
            loop, _s, _b, _l, _k = make_loop(ledger=ledger, backend=backend)
            await speak(loop, "item-1", "跑测试")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑测试", interpretation="跑测试",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return order, ledger

        order, ledger = run(scenario())
        self.assertEqual(order, ["record", "send"])
        self.assertEqual(list(ledger.outcomes.values()), ["posted"])

    def test_A2_a_call_with_an_ambiguous_join_is_refused_not_dispatched(self):
        async def scenario():
            loop, session, backend, _l, _k = make_loop()
            await speak(loop, "item-1", "第一句", seq=1)
            await speak(loop, "item-2", "第二句", seq=3)
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="第一句", interpretation="第一句",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return session, backend

        session, backend = run(scenario())
        self.assertEqual(backend.sends, [])
        self.assertFalse(session.outputs[0][1]["accepted"])
        self.assertEqual(session.outputs[0][1]["reason"], "ambiguous")

    def test_A2_a_posted_call_output_opens_no_response(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await speak(loop, "item-1", "跑测试")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑测试", interpretation="跑测试",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            await speak(loop, "item-2", "再跑 lint", seq=3)
            await respond(loop, "resp-2", seq=12)
            await loop.handle_decision(request(transcript="再跑 lint", interpretation="再跑 lint",
                                               priority="next", item_id="item-2",
                                               response_id="resp-2", call_id="call-2"))
            return session

        session = run(scenario())
        self.assertEqual(len(session.outputs), 2)
        self.assertEqual(len(session.responses), 0)


class SpeechModeTests(unittest.TestCase):
    """The follow-up response after a call output overrides to `tool_choice: none`, or the model
    answers a receipt with another call instead of speaking (R11 variant A)."""

    def _responses_after(self, decision):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await speak(loop, "item-1", "跑测试")
            await respond(loop, "resp-1")
            await loop.handle_decision(decision)
            return session

        return run(scenario())

    def test_a_posted_dispatch_answers_the_call_without_a_response(self):
        session = self._responses_after(request(transcript="跑测试", interpretation="跑测试",
                                                priority="now", item_id="item-1",
                                                response_id="resp-1"))
        self.assertEqual(len(session.outputs), 1)
        self.assertEqual(session.outputs[0][1]["outcome"], "posted")
        self.assertEqual(session.responses, [])

    def test_a_refused_call_also_answers_with_a_speaking_response(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_OWNER_LOST, text="pane gone"))
            await speak(loop, "item-1", "跑测试")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑测试", interpretation="跑测试",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return session

        session = run(scenario())
        self.assertEqual(session.responses[-1]["tool_choice"], "none")

    def test_a_result_narration_does_not_invite_another_tool_call(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await speak(loop, "item-1", "跑 lint")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑 lint", interpretation="跑 lint",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, turn_id="turn-A", tag="req-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RESULT, turn_id="turn-A", text="lint 全绿"))
            return session

        session = run(scenario())
        self.assertEqual(session.responses[-1]["tool_choice"], "none")

    def test_the_challenge_response_must_be_able_to_speak(self):
        """The challenge is the one response whose whole job is SAYING something in our exact
        words; an unspoken challenge would gather no delivery evidence and the consent gate
        would silently never arm. It carries `tool_choice: none` for that reason."""
        dialog = backend_base.Dialog(occurrence_id="occ-1", kind="dialog",
                                     prompt="Bash wants to run rm -rf build/ ?",
                                     action=None, scope=None,
                                     options=(("1", "Yes"), ("2", "No")))

        async def scenario():
            loop, session, _b, _l, _k = make_loop(backend=FakeBackend(dialog=dialog))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_DIALOG, dialog=dialog, payload={"transition": "open"}))
            return loop, session

        loop, session = run(scenario())
        self.assertEqual(session.responses[0]["origin"], "challenge")
        self.assertEqual(session.responses[0]["tool_choice"], "none")
        # And the words it is told to speak are the broker's, in full.
        self.assertEqual(session.responses[0]["instructions"], loop.broker.challenge_text())


class UnknownDecisionTests(unittest.TestCase):

    def test_an_unhandled_decision_type_touches_nothing(self):
        """If the wire maps converse to no decision at all, the loop must stay inert rather
        than guess. This is the contract-only coordination, asserted."""

        class Unknown:
            call_id = "c-unknown"
            generation = 0

        async def scenario():
            loop, session, backend, ledger, _k = make_loop()
            await loop.handle_decision(Unknown())
            return session, backend, ledger

        session, backend, ledger = run(scenario())
        self.assertEqual(backend.sends, [])
        self.assertEqual(ledger.ops, {})
        self.assertEqual(session.outputs, [])


class NextIsTheHarnessQueueTests(unittest.TestCase):
    """`interrupt: false` is sent at once with `priority: next`; Claude Code queues it behind its
    running turn itself. The loop keeps no queue of its own (a duplicate of the harness's,
    deleted 2026-09-21)."""

    def test_next_while_working_is_still_sent_and_carries_next(self):
        async def scenario():
            backend = FakeBackend(activity="working", turn_id="turn-A")
            loop, session, _b, ledger, _k = make_loop(backend=backend)
            await speak(loop, "item-1", "等测试跑完再跑 lint")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="等测试跑完再跑 lint",
                                               interpretation="跑 lint", priority="next",
                                               item_id="item-1", response_id="resp-1"))
            return loop, session, backend, ledger

        loop, session, backend, ledger = run(scenario())
        self.assertEqual(len(backend.sends), 1)
        self.assertEqual(backend.sends[0]["priority"], "next")
        self.assertEqual(session.outputs[0][1]["outcome"], "posted")
        self.assertEqual([op["kind"] for op in ledger.ops.values()], ["send"])

    def test_A4_an_op_written_without_an_outcome_recovers_as_uncertain(self):
        ledger = FakeLedger()
        ledger.record_op("op-1", "req-1", 1, "send", {})
        self.assertEqual(ledger.recover(), {"op-1": "uncertain"})

    def test_A4_next_while_idle_dispatches_immediately(self):
        async def scenario():
            loop, _s, backend, _l, _k = make_loop(backend=FakeBackend(activity="idle"))
            await speak(loop, "item-1", "顺便跑个 lint")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="顺便跑个 lint",
                                               interpretation="跑 lint", priority="next",
                                               item_id="item-1", response_id="resp-1"))
            return backend

        backend = run(scenario())
        self.assertEqual(len(backend.sends), 1)


class A5StopTests(unittest.TestCase):
    def test_A5_a_stop_is_a_request_now_and_the_backend_frame_carries_it(self):
        async def scenario():
            backend = FakeBackend(activity="working", turn_id="turn-A")
            loop, _s, _b, _l, _k = make_loop(backend=backend)
            await speak(loop, "item-1", "先停掉,别跑了")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="先停掉,别跑了",
                                               interpretation="停止当前任务", priority="now",
                                               item_id="item-1", response_id="resp-1"))
            return backend

        backend = run(scenario())
        self.assertEqual(len(backend.sends), 1)
        self.assertEqual(backend.sends[0]["priority"], "now")
        self.assertEqual(backend.sends[0]["transcript"], "先停掉,别跑了")


class ResultsAlwaysEnterTests(unittest.TestCase):
    """Nothing is shelved. Measured live 2026-09-21: the supersede/retain machinery this replaces
    lost both answers of a run -- one shelved behind room noise, one dropped by an id collision
    with an earlier run. Every backend word enters the session; whether it is stale is the
    model's judgment."""

    def _after_a_new_sentence(self):
        async def scenario():
            loop, session, backend, ledger, _k = make_loop()
            await speak(loop, "item-1", "跑一下 lint,看看有没有问题", seq=1)
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑一下 lint,看看有没有问题",
                                               interpretation="跑 lint", priority="now",
                                               item_id="item-1", response_id="resp-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, turn_id="turn-A", tag="req-1"))
            # The operator says something else (or the room does) before the answer lands.
            await speak(loop, "item-2", "EJ out.", seq=20)
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RESULT, turn_id="turn-A",
                text="lint 发现 3 个问题:未使用的 import"))
            return loop, session, ledger

        return run(scenario())

    def test_a_result_arriving_after_a_new_sentence_is_injected_and_spoken(self):
        loop, session, _ledger = self._after_a_new_sentence()
        injected = [t for t in session.item_texts() if "未使用的 import" in t]
        self.assertEqual(len(injected), 1)
        self.assertTrue(injected[0].startswith("[后台] 请求 req-1"), injected[0])
        self.assertEqual(session.responses[-1], {"origin": "narration", "instructions": None,
                                                 "tool_choice": "none"})
        self.assertEqual(loop.log.request("req-1").state, "resolved")

    def test_a_result_for_a_turn_nobody_asked_by_voice_is_context_not_speech(self):
        """The relay's probe, or an answer to something the operator typed at the keyboard:
        the words enter the session so the voice knows, but nothing is narrated -- the
        operator is already reading it."""
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, turn_id="turn-P", tag="probe-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RESULT, turn_id="turn-P", text="Restarting the daemon now."))
            return session

        session = run(scenario())
        self.assertEqual(session.item_texts(), ["[后台]\nRestarting the daemon now."])
        self.assertEqual(session.responses, [])

    def test_progress_is_injected_silently_with_its_request_label(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await speak(loop, "item-1", "跑 lint")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑 lint", interpretation="跑 lint",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            before = len(session.responses)
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, turn_id="turn-A", tag="req-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_PROGRESS, turn_id="turn-A", text="正在跑 ruff…"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_PROGRESS, turn_id="turn-A", text="   "))
            return session, len(session.responses) - before

        session, created = run(scenario())
        self.assertEqual(session.item_texts()[-1], "[后台·进行中] 请求 req-1\n正在跑 ruff…")
        self.assertEqual(created, 0, "progress never opens a speaking slot")

    def test_what_the_operator_typed_is_injected_silently(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_TYPED, turn_id="turn-T", text="git status"))
            return session

        session = run(scenario())
        self.assertEqual(session.item_texts(), ["[终端·你打的]\ngit status"])
        self.assertEqual(session.responses, [])

    def test_a_current_results_content_does_enter_the_session(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await speak(loop, "item-1", "跑 lint")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑 lint", interpretation="跑 lint",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, turn_id="turn-A", tag="req-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RESULT, turn_id="turn-A", text="lint 全绿"))
            return session

        session = run(scenario())
        self.assertTrue(any("lint 全绿" in text for text in session.item_texts()))


class D3AckTests(unittest.TestCase):
    def test_D3_a_turn_with_no_decision_records_no_task(self):
        async def scenario():
            strategy = FakeStrategy()          # yields nothing, whatever the model said
            loop, _s, backend, ledger, _k = make_loop(strategy=strategy)
            await speak(loop, "item-1", "顺便把 lint 跑了")
            await loop.handle_live_event(event(10, live_base.KIND_RESPONSE_CREATED,
                                               response_id="resp-1", payload={"origin": "user"}))
            await loop.handle_live_event(event(11, live_base.KIND_OUTPUT_TRANSCRIPT,
                                               response_id="resp-1",
                                               payload={"text": "好的,我来跑。", "done": True}))
            await loop.handle_live_event(event(12, live_base.KIND_RESPONSE_DONE,
                                               response_id="resp-1",
                                               payload={"status": "completed"}))
            return loop, backend, ledger

        loop, backend, ledger = run(scenario())
        self.assertEqual(backend.sends, [])
        self.assertEqual(ledger.ops, {})
        self.assertEqual(list(loop.log.requests()), [])


class C3DeliveryEvidenceTimingTests(unittest.TestCase):
    """Live wire order is transcript `done` → (speaker still playing) → `response.done`. The
    loop may only judge delivery on conclusive evidence; "not finished yet" is not a verdict."""

    def _dialog(self):
        return backend_base.Dialog(occurrence_id="occ-1", kind="dialog",
                                   prompt="Bash wants to run rm -rf build/ ?",
                                   action=None, scope=None,
                                   options=(("1", "Yes"), ("2", "No")))

    def _arm(self, loop, dialog, sink, *, end_before_transcript: bool):
        async def scenario():
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_DIALOG, dialog=dialog, payload={"transition": "open"}))
            await loop.handle_live_event(event(30, live_base.KIND_RESPONSE_CREATED,
                                               response_id="resp-ch",
                                               payload={"origin": "challenge"}))
            if end_before_transcript:
                sink.ended.add("resp-ch")
            await loop.handle_live_event(event(31, live_base.KIND_OUTPUT_TRANSCRIPT,
                                               response_id="resp-ch",
                                               payload={"text": loop.broker.challenge_text(),
                                                        "done": True}))
            await loop.handle_live_event(event(32, live_base.KIND_RESPONSE_DONE,
                                               response_id="resp-ch",
                                               payload={"status": "completed"}))
        run(scenario())

    def test_a_transcript_that_finishes_while_audio_still_plays_does_not_tombstone(self):
        dialog = self._dialog()
        backend = FakeBackend(activity="dialog", dialog=dialog)
        loop, _s, _b, _l, sink = make_loop(backend=backend)
        self._arm(loop, dialog, sink, end_before_transcript=False)
        self.assertIsNotNone(loop.broker.armed, "still playing is not an interruption")
        self.assertFalse(loop.broker.is_delivered())

    def test_delivery_is_judged_again_when_the_operator_answers(self):
        dialog = self._dialog()
        backend = FakeBackend(activity="dialog", dialog=dialog)
        loop, _s, _b, _l, sink = make_loop(backend=backend)
        self._arm(loop, dialog, sink, end_before_transcript=False)
        sink.ended.add("resp-ch")            # the speaker finished after response.done
        run(speak(loop, "item-9", option_phrase(1), seq=40))
        self.assertEqual(backend.answers, [("occ-1", "1")])

    def test_a_moved_audio_epoch_still_tombstones(self):
        dialog = self._dialog()
        backend = FakeBackend(activity="dialog", dialog=dialog)
        loop, _s, _b, _l, sink = make_loop(backend=backend)
        sink.epoch_moved.add("resp-ch")
        self._arm(loop, dialog, sink, end_before_transcript=False)
        self.assertIsNone(loop.broker.armed)


class TerminalOnlyDialogTests(unittest.TestCase):
    """The production daemon has no key-press surface: a dialog is never armed as a spoken
    challenge; the operator hears that the backend is waiting at the terminal."""

    def test_a_dialog_is_narrated_not_armed(self):
        dialog = backend_base.Dialog(occurrence_id="occ-1", kind="dialog",
                                     prompt="Bash wants to run rm -rf build/ ?",
                                     action=None, scope=None,
                                     options=(("1", "Yes"), ("2", "No")))
        backend = FakeBackend(activity="dialog", dialog=dialog)
        loop, session, _b, _l, _k = make_loop(backend=backend, broker=Broker(terminal_only=True))
        run(loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_DIALOG, dialog=dialog, payload={"transition": "open"})))
        self.assertIsNone(loop.broker.armed)
        self.assertEqual([o for _i, o in session.items], ["narration"])
        text = session.items[-1][0]["content"][0]["text"]
        self.assertIn("rm -rf build/", text)
        self.assertEqual(session.responses[-1]["tool_choice"], "none")
        # And the exact phrase later presses nothing: there is no arm to consume.
        run(speak(loop, "item-9", option_phrase(1), seq=40))
        self.assertEqual(backend.answers, [])


class C3LoopConsentTests(unittest.TestCase):
    def _armed_loop(self):
        dialog = backend_base.Dialog(occurrence_id="occ-1", kind="dialog",
                                     prompt="Bash wants to run rm -rf build/ ?",
                                     action=None, scope=None,
                                     options=(("1", "Yes"), ("2", "No")))
        backend = FakeBackend(activity="dialog", dialog=dialog)
        loop, session, _b, ledger, sink = make_loop(backend=backend)

        async def arm_and_deliver():
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_DIALOG, dialog=dialog, payload={"transition": "open"}))
            # The provider creates the challenge response and renders it fully.
            await loop.handle_live_event(event(30, live_base.KIND_RESPONSE_CREATED,
                                               response_id="resp-ch",
                                               payload={"origin": "challenge"}))
            sink.ended.add("resp-ch")
            await loop.handle_live_event(event(31, live_base.KIND_OUTPUT_TRANSCRIPT,
                                               response_id="resp-ch",
                                               payload={"text": loop.broker.challenge_text(),
                                                        "done": True}))
            await loop.handle_live_event(event(32, live_base.KIND_RESPONSE_DONE,
                                               response_id="resp-ch",
                                               payload={"status": "completed"}))

        run(arm_and_deliver())
        return loop, session, backend, ledger

    def test_C3_the_challenge_is_spoken_on_a_challenge_origin_response(self):
        loop, session, _b, _l = self._armed_loop()
        self.assertTrue(loop.broker.is_delivered())
        origins = [origin for _item, origin in session.items]
        self.assertIn("challenge", origins)
        self.assertEqual(session.responses[0]["origin"], "challenge")

    def test_C3_the_exact_phrase_presses_the_dialog(self):
        loop, _s, backend, ledger = self._armed_loop()
        run(speak(loop, "item-9", option_phrase(1), seq=40))
        self.assertEqual(backend.answers, [("occ-1", "1")])
        self.assertEqual(loop.stats.effects_executed, 1)
        self.assertEqual(list(ledger.outcomes.values()), ["applied"])

    def test_C3_an_unrelated_utterance_presses_nothing(self):
        loop, _s, backend, _l = self._armed_loop()
        run(speak(loop, "item-9", "对,你刚才说的那个说的是对的。", seq=40))
        self.assertEqual(backend.answers, [])
        self.assertEqual(loop.stats.effects_refused, 1)

    def test_C3_a_failed_transcript_consumes_the_arm_so_a_later_phrase_cannot_execute(self):
        """The wire-to-loop path for a failed transcription. The provider committed an item and
        could not hear it; that is still the next thing the operator said, so it consumes the
        arm. A later utterance of the exact phrase answers a question nobody is asking."""
        loop, _s, backend, _l = self._armed_loop()
        self.assertTrue(loop.broker.is_delivered())

        run(loop.handle_decision(live_base.TranscriptFailed(input_item_id="item-9",
                                                            generation=0)))
        self.assertIsNone(loop.broker.armed, "the failed item consumed the arm")
        self.assertEqual(backend.answers, [])

        # The operator now says the exact phrase. It must not execute.
        run(speak(loop, "item-10", option_phrase(1), seq=50))
        self.assertEqual(backend.answers, [],
                         "an arm must never outlive the next thing the operator said")

    def test_C3_a_failed_transcript_never_dispatches(self):
        loop, _s, backend, ledger = self._armed_loop()
        run(loop.handle_decision(live_base.TranscriptFailed(input_item_id="item-9",
                                                            generation=0)))
        self.assertEqual(backend.sends, [])
        self.assertEqual([op for op in ledger.ops.values() if op["kind"] == "send"], [])

    def test_C3_the_answer_is_recorded_before_it_is_pressed(self):
        loop, _s, backend, ledger = self._armed_loop()
        run(speak(loop, "item-9", option_phrase(1), seq=40))
        answer_ops = [op for op in ledger.ops.values() if op["kind"] == "answer"]
        self.assertEqual(len(answer_ops), 1)
        self.assertEqual(answer_ops[0]["payload"]["choice"], "1")


class OwnerLossTests(unittest.TestCase):
    def test_owner_loss_refuses_every_later_actuation(self):
        async def scenario():
            loop, session, backend, _l, _k = make_loop()
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_OWNER_LOST, text="pane gone"))
            await speak(loop, "item-1", "跑测试")
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑测试", interpretation="跑测试",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return loop, session, backend

        loop, session, backend = run(scenario())
        self.assertEqual(backend.sends, [])
        self.assertEqual(loop.owner_lost, "pane gone")
        self.assertEqual(session.outputs[-1][1]["reason"], "owner_lost")


class ResetTests(unittest.TestCase):
    def test_a_reconnect_clears_the_arm_and_tells_the_model(self):
        dialog = backend_base.Dialog(occurrence_id="occ-1", kind="dialog",
                                     prompt="Bash wants to run rm -rf build/ ?",
                                     action=None, scope=None,
                                     options=(("1", "Yes"), ("2", "No")))

        async def scenario():
            loop, session, _b, _l, _k = make_loop(backend=FakeBackend(dialog=dialog))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_DIALOG, dialog=dialog, payload={"transition": "open"}))
            await loop.handle_live_event(event(50, live_base.KIND_DISCONNECTED, generation=1))
            return loop, session

        loop, session = run(scenario())
        self.assertIsNone(loop.broker.armed)
        self.assertTrue(any("effect_cleared" in text for text in session.item_texts()))


if __name__ == "__main__":
    unittest.main()


class BackendWordsArriveAsWordsTests(unittest.TestCase):
    """A backend result reaches the model as labelled prose, never as a JSON envelope: a model
    handed field names reads field names back. Structured bodies (the challenge) stay
    structured -- they are identity, not content to speak from."""

    async def _resolved(self):
        loop, session, backend, ledger, _k = make_loop()
        await speak(loop, "item-1", "跑一下 lint", seq=1)
        await respond(loop, "resp-1")
        await loop.handle_decision(request(transcript="跑一下 lint", interpretation="跑一下 lint",
                                           priority="now", item_id="item-1", response_id="resp-1"))
        await loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_RECEIPT, turn_id="turn-A", tag="req-1"))
        await loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_RESULT, turn_id="turn-A", text="lint 干净,0 个问题"))
        return loop, session

    def test_a_result_is_labelled_prose_with_the_request_id_and_verbatim_text(self):
        _loop, session = run(self._resolved())
        item = session.item_texts()[-1]
        self.assertTrue(item.startswith("[后台]"), item)
        self.assertIn("req-1", item)
        self.assertTrue(item.endswith("lint 干净,0 个问题"), item)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(item)

    def test_a_refused_call_carries_the_outcome_the_prompt_names(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await speak(loop, "item-1", "第一句", seq=1)
            await speak(loop, "item-2", "第二句", seq=3)
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="第一句", interpretation="第一句",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return session

        session = run(scenario())
        self.assertEqual(session.outputs[0][1]["outcome"], "refused")
        self.assertFalse(session.outputs[0][1]["accepted"])


class HeardAndSpokenAreOnDiskTests(unittest.TestCase):
    """What the operator was heard to say and what the voice said are appended to the ledger as
    evidence records. They decide nothing; they exist so "it said something stale" can be checked
    against a file instead of memory."""

    def test_a_completed_input_transcript_is_recorded_as_heard(self):
        async def scenario():
            loop, _s, _b, ledger, _k = make_loop()
            await speak(loop, "item-1", "跑一下 lint", seq=1)
            return ledger

        ledger = run(scenario())
        heard = [r for r in ledger.appended if r["kind"] == "heard"]
        self.assertEqual(heard, [{"kind": "heard", "item_id": "item-1",
                                  "text": "跑一下 lint", "failed": False}])

    def test_a_finished_output_transcript_is_recorded_as_spoken(self):
        async def scenario():
            loop, _s, _b, ledger, _k = make_loop()
            await respond(loop, "resp-1", origin="narration")
            await loop.handle_live_event(event(11, live_base.KIND_OUTPUT_TRANSCRIPT,
                                               response_id="resp-1",
                                               payload={"text": "好,交过去了", "done": True}))
            await loop.handle_live_event(event(12, live_base.KIND_OUTPUT_TRANSCRIPT,
                                               response_id="resp-1",
                                               payload={"text": "好,交", "done": False}))
            return ledger

        ledger = run(scenario())
        spoken = [r for r in ledger.appended if r["kind"] == "spoken"]
        self.assertEqual(spoken, [{"kind": "spoken", "response_id": "resp-1",
                                   "text": "好,交过去了"}])


class ReceiptsCarryWordsTests(unittest.TestCase):
    """A receipt says what it means. Measured live: a wordless `posted` receipt was narrated as
    "I have no output, send it to me"."""

    def test_a_posted_receipt_tells_the_model_the_answer_comes_later(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await speak(loop, "item-1", "跑一下 lint", seq=1)
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="跑一下 lint", interpretation="跑一下 lint",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return session

        session = run(scenario())
        out = session.outputs[-1][1]
        self.assertEqual(out["outcome"], "posted")
        self.assertIn("[后台]", out["note"])
        self.assertIn("不是结果", out["note"])

class RunawayAndStrayReceiptTests(unittest.TestCase):
    """Two live defects from 2026-09-20: a speaking response that never stops, and a receipt for a
    tag the loop never issued (the relay's probe) being mapped as a voice request."""

    def test_a_second_message_item_in_a_narration_response_is_cut(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await respond(loop, "resp-n", origin="narration")
            for i in (1, 2, 3):
                await loop.handle_live_event(event(20 + i, live_base.KIND_OUTPUT_ITEM,
                                                   response_id="resp-n", item_id=f"msg-{i}",
                                                   payload={"type": "message"}))
            return loop, session

        loop, session = run(scenario())
        self.assertEqual(session.cancelled, ["resp-n", "resp-n"])
        self.assertEqual(loop.stats.runaway_cuts, 2)

    def test_a_user_response_may_carry_one_message_beside_its_call_but_not_two(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await respond(loop, "resp-u")
            await loop.handle_live_event(event(21, live_base.KIND_OUTPUT_ITEM, response_id="resp-u",
                                               item_id="msg-1", payload={"type": "message"}))
            first = list(session.cancelled)
            await loop.handle_live_event(event(22, live_base.KIND_OUTPUT_ITEM, response_id="resp-u",
                                               item_id="msg-2", payload={"type": "message"}))
            return first, session.cancelled

        first, after = run(scenario())
        self.assertEqual(first, [])
        self.assertEqual(after, ["resp-u"])

    def test_a_receipt_for_a_tag_we_never_issued_maps_no_request(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, turn_id="turn-P", tag="probe-1"))
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RESULT, turn_id="turn-P", text="Restarting the daemon now."))
            return loop, session

        loop, session = run(scenario())
        self.assertEqual(loop._request_of_turn, {})
        self.assertEqual(session.responses, [], "not a voice request: nothing to narrate")

class DecisionsAreOnDiskTests(unittest.TestCase):
    def test_every_decision_and_every_refusal_is_appended(self):
        async def scenario():
            loop, _s, _b, ledger, _k = make_loop()
            await speak(loop, "item-1", "第一句", seq=1)
            await speak(loop, "item-2", "第二句", seq=3)
            await respond(loop, "resp-1")
            await loop.handle_decision(request(transcript="第一句", interpretation="第一句",
                                               priority="now", item_id="item-1",
                                               response_id="resp-1"))
            return ledger

        ledger = run(scenario())
        kinds = [(r["kind"], r.get("tool"), r.get("reason")) for r in ledger.appended
                 if r["kind"] in ("decided", "call_refused")]
        self.assertEqual(kinds, [("decided", "request", None), ("call_refused", None, "ambiguous")])


class ObservationsAreOnDiskTests(unittest.TestCase):
    def test_every_backend_observation_is_appended_before_mapping(self):
        async def scenario():
            loop, _s, _b, ledger, _k = make_loop()
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, turn_id="turn-P", tag="probe-1",
                payload={"disposition": "queued"}))
            return ledger

        ledger = run(scenario())
        seen = [r for r in ledger.appended if r["kind"] == "observed"]
        self.assertEqual(seen, [{"kind": "observed", "obs": backend_base.OBS_RECEIPT,
                                 "turn_id": "turn-P", "tag": "probe-1", "disposition": "queued",
                                 "activity": None, "text": ""}])


class OneAckPerHandoffTests(unittest.TestCase):
    """Measured live 2026-09-20: the model acknowledged beside its request call, then the
    posted receipt opened a second speaking slot and it acknowledged again -- or answered the
    question itself. A delivered request whose response already spoke gets no second slot."""

    async def _dispatch(self, *, spoke: bool):
        loop, session, _b, _l, _k = make_loop()
        await speak(loop, "item-1", "跑一下 lint", seq=1)
        await respond(loop, "resp-1")
        if spoke:
            await loop.handle_live_event(event(11, live_base.KIND_OUTPUT_ITEM, response_id="resp-1",
                                               item_id="msg-1", payload={"type": "message"}))
        before = len(session.responses)
        await loop.handle_decision(request(transcript="跑一下 lint", interpretation="跑一下 lint",
                                           priority="now", item_id="item-1", response_id="resp-1"))
        return session, len(session.responses) - before

    def test_a_posted_request_whose_turn_already_spoke_opens_no_second_slot(self):
        session, created = run(self._dispatch(spoke=True))
        self.assertEqual(session.outputs[-1][1]["outcome"], "posted")
        self.assertEqual(created, 0)

    def test_a_posted_request_whose_turn_was_silent_opens_no_slot_either(self):
        _session, created = run(self._dispatch(spoke=False))
        self.assertEqual(created, 0)


class RunawayCutKeyedOnTranscriptsTests(unittest.TestCase):
    """Live 2026-09-21: 20 preamble sentences in one narration response; the wire never sent
    output_item.added, so the item-count cut never fired. Finished transcripts count too."""

    def test_the_second_finished_transcript_in_a_narration_response_cuts_it(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await respond(loop, "resp-n", origin="narration")
            for i in (1, 2, 3):
                await loop.handle_live_event(event(20 + i, live_base.KIND_OUTPUT_TRANSCRIPT,
                                                   response_id="resp-n", item_id=f"msg-{i}",
                                                   payload={"text": f"我来简单说一下 {i}", "done": True}))
            return session

        session = run(scenario())
        self.assertEqual(session.cancelled, ["resp-n", "resp-n"])

    def test_the_same_item_announced_and_finished_counts_once(self):
        async def scenario():
            loop, session, _b, _l, _k = make_loop()
            await respond(loop, "resp-n", origin="narration")
            await loop.handle_live_event(event(21, live_base.KIND_OUTPUT_ITEM, response_id="resp-n",
                                               item_id="msg-1", payload={"type": "message"}))
            await loop.handle_live_event(event(22, live_base.KIND_OUTPUT_TRANSCRIPT,
                                               response_id="resp-n", item_id="msg-1",
                                               payload={"text": "一句", "done": True}))
            return session

        session = run(scenario())
        self.assertEqual(session.cancelled, [])


class SessionEdgesTests(unittest.TestCase):
    """The voice's own edges as words the model reacts to in its own words: nothing here is
    a line to say, and the goodbye is awaited on the WIRE, not on a clock."""

    def test_notice_open_tells_the_model_and_asks_it_to_speak(self):
        loop, session, *_ = make_loop()
        run(loop.notice_open())
        self.assertEqual(session.item_texts(), ["[语音]\n已就绪"])
        self.assertEqual(session.responses,
                         [{"origin": "narration", "instructions": None, "tool_choice": "none"}])

    def test_farewell_returns_only_when_its_own_response_is_done(self):
        loop, session, *_ = make_loop()

        async def scenario():
            loop.live_pumping = True                # as inside `_pump_live`
            waiting = asyncio.ensure_future(loop.farewell("stopped by control"))
            await asyncio.sleep(0)
            self.assertEqual(session.item_texts(), ["[语音]\n要关了:stopped by control"])
            self.assertEqual(session.responses[-1]["origin"], "farewell")
            # Another narration finishing is not the goodbye.
            await loop.handle_live_event(event(1, live_base.KIND_RESPONSE_CREATED,
                                               response_id="r-result",
                                               payload={"origin": "narration"}))
            await loop.handle_live_event(event(2, live_base.KIND_RESPONSE_DONE,
                                               response_id="r-result",
                                               payload={"status": "completed"}))
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
            await loop.handle_live_event(event(3, live_base.KIND_RESPONSE_CREATED,
                                               response_id="r-bye",
                                               payload={"origin": "farewell"}))
            await loop.handle_live_event(event(4, live_base.KIND_RESPONSE_DONE,
                                               response_id="r-bye",
                                               payload={"status": "completed"}))
            await asyncio.wait_for(waiting, 1.0)

        run(scenario())

    def test_farewell_returns_at_once_when_the_live_pump_already_ended(self):
        """The socket dropped but the ending's reason is something else (a control stop
        after the drop): nothing can carry a goodbye, so the daemon's bound must not be
        waited out (Grok review)."""
        loop, session, *_ = make_loop(session=FakeSession(events=[]))

        async def scenario():
            self.assertFalse(loop.live_pumping, "not yet running: nothing to say it through")
            await asyncio.wait_for(loop.farewell("stopped by control"), 0.5)
            await loop._pump_live()                 # the replayed stream ends at once
            self.assertFalse(loop.live_pumping)
            await asyncio.wait_for(loop.farewell("stopped by control"), 0.5)
            self.assertEqual(session.responses, [], "nothing was asked of a dead socket")

        run(scenario())


class IdleSignalsTests(unittest.TestCase):
    """What the daemon's idle close reads from the loop: something happened. Connection upkeep
    is not something happening. The loop holds no clock for it."""

    def test_a_live_event_and_an_observation_each_stir(self):
        loop, *_ = make_loop()

        async def scenario():
            self.assertFalse(loop.stirred.is_set())
            await loop.handle_live_event(event(1, live_base.KIND_INPUT_SPEECH_STARTED))
            self.assertTrue(loop.stirred.is_set())
            loop.stirred.clear()
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_PROGRESS, turn_id="t1", text=""))
            self.assertTrue(loop.stirred.is_set())

        run(scenario())

    def test_speaking_lasts_from_speech_start_to_its_end(self):
        """One utterance is one event on the wire; the idle close needs to know it is still
        going, and that a dropped wire ends it."""
        loop, *_ = make_loop()

        async def scenario():
            self.assertFalse(loop.speaking)
            for ending in (live_base.KIND_INPUT_SPEECH_STOPPED, live_base.KIND_INPUT_COMMITTED,
                           live_base.KIND_DISCONNECTED):
                await loop.handle_live_event(event(1, live_base.KIND_INPUT_SPEECH_STARTED))
                self.assertTrue(loop.speaking)
                await loop.handle_live_event(event(2, ending))
                self.assertFalse(loop.speaking, ending)

        run(scenario())

    def test_errors_and_reconnects_do_not_stir(self):
        loop, *_ = make_loop()

        async def scenario():
            for n, kind in enumerate((live_base.KIND_ERROR, live_base.KIND_DISCONNECTED,
                                      live_base.KIND_SESSION_STARTED, live_base.KIND_CLOSED)):
                await loop.handle_live_event(event(n + 1, kind, payload={"message": "x"}))
            self.assertFalse(loop.stirred.is_set())

        run(scenario())
