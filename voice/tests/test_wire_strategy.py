"""DESIGN.md §Acceptance A2 and B2 — the join's ordering, and the truncate guard.

A2 fixes what the R0 replay can only show once: a call and its evidencing transcript can
arrive in either order, and a transcript can FAIL. All three orderings must reach the same
dispatch, and the failed one must still surface the operator's words rather than swallow
them — the loop decides what an unevidenced dispatch is worth, not the strategy.

B2 fixes the truncate guard: `audio_end_ms` is what RENDERED, and an item we cannot name
unambiguously is not cut at all.
"""
from __future__ import annotations

import json
import unittest

from voice.live.base import Request
from voice.live.realtime import RealtimeSession, parse_arguments
from voice.live.strategy_function import TOOL_NAMES, TOOLS, FunctionStrategy
from voice.tests.wire_fakes import FakeSink, FakeSocket, RecordingSession


def _adapter(sink=None) -> RealtimeSession:
    return RealtimeSession(provider="voice_live", model="m", api_key="k",
                           endpoint="https://x.openai.azure.com", api_version="2026-07-15",
                           sink=sink, connect=FakeSocket.connector([]),
                           open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)


def committed(item_id: str) -> dict:
    return {"type": "input_audio_buffer.committed", "item_id": item_id}


def created(response_id: str) -> dict:
    return {"type": "response.created", "response": {"id": response_id}}


def call(response_id: str, call_id: str, name: str = "relay", **args) -> dict:
    return {"type": "response.output_item.done", "response_id": response_id,
            "item": {"id": f"item_{call_id}", "type": "function_call", "name": name,
                     "call_id": call_id, "arguments": json.dumps(args, ensure_ascii=False)}}


def transcript_ok(item_id: str, text: str) -> dict:
    return {"type": "conversation.item.input_audio_transcription.completed",
            "item_id": item_id, "content_index": 0, "transcript": text}


def transcript_failed(item_id: str) -> dict:
    return {"type": "conversation.item.input_audio_transcription.failed",
            "item_id": item_id, "content_index": 0,
            "error": {"message": "audio too short"}}


def audio_delta(response_id: str, item_id: str, nbytes: int) -> dict:
    return {"type": "response.audio.delta", "response_id": response_id,
            "item_id": item_id, "delta": nbytes}


class _Harness:
    def __init__(self, sink=None):
        self.sink = sink or FakeSink()
        self.adapter = _adapter(self.sink)
        self.session = RecordingSession()
        self.strategy = FunctionStrategy(self.session, self.sink)

    async def run(self, stream: list[dict]) -> list:
        decisions = []
        for raw in stream:
            for event in self.adapter.map_event(raw):
                decisions.extend(await self.strategy.feed(event))
        return decisions


class A2DispatchWaitsForTheTranscript(unittest.IsolatedAsyncioTestCase):

    async def test_a2_call_before_transcript_dispatches_when_the_transcript_lands(self):
        h = _Harness()
        # This is R0's real order: the model calls, the ASR finishes afterwards.
        mid = await h.run([committed("item_U"), created("resp_1"),
                           call("resp_1", "call_1", text="跑一下测试", interrupt=True)])
        self.assertEqual(mid, [], "no dispatch before the evidence arrives")
        late = await h.run([transcript_ok("item_U", "跑一下测试吧")])
        self.assertEqual(len(late), 1)
        request = late[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.transcript, "跑一下测试吧")
        self.assertEqual(request.interpretation, "跑一下测试")
        self.assertEqual(request.input_item_id, "item_U")
        self.assertEqual(request.response_id, "resp_1")
        self.assertEqual(request.priority, "now")

    async def test_a2_transcript_before_call_dispatches_on_the_call(self):
        h = _Harness()
        early = await h.run([committed("item_U"), transcript_ok("item_U", "跑一下测试吧"),
                             created("resp_1")])
        self.assertEqual(early, [])
        out = await h.run([call("resp_1", "call_1", text="跑一下测试", interrupt=False)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].transcript, "跑一下测试吧")
        self.assertEqual(out[0].priority, "next")

    async def test_a2_a_failed_transcript_is_terminal_and_yields_TranscriptFailed(self):
        """It used to release a Request with an empty transcript, handing the backend the
        model's interpretation of words nobody could verify — the one case where the
        reconstruction has no evidence beside it at all."""
        h = _Harness()
        out = await h.run([committed("item_U"), created("resp_1"),
                           call("resp_1", "call_1", text="停掉那个任务", interrupt=True),
                           transcript_failed("item_U")])
        self.assertEqual([type(d).__name__ for d in out], ["TranscriptFailed"])
        self.assertEqual(out[0].input_item_id, "item_U")
        self.assertFalse(h.session.call_outputs[-1][1]["ok"])

    async def test_a2_a_BLANK_completed_transcript_is_not_evidence(self):
        """A `completed` with blank text is the provider saying it heard nothing. Read as
        success it built a Request from the model's `interpretation` alone — a
        reconstruction of words no transcript backs, handed to a backend as though the
        operator had been heard."""
        for blank in ("", "   ", "\n\t "):
            with self.subTest(text=repr(blank)):
                h = _Harness()
                out = await h.run([
                    committed("item_U"), created("resp_1"),
                    call("resp_1", "call_1", text="删掉 build 目录", interrupt=True),
                    transcript_ok("item_U", blank)])
                self.assertEqual([type(d).__name__ for d in out], ["TranscriptFailed"])
                self.assertFalse(h.session.call_outputs[-1][1]["ok"])

    async def test_a2_the_strategy_rejects_a_blank_transcript_marked_complete(self):
        """The STRATEGY's own guard, fed directly.

        The adapter now marks a blank `completed` as incomplete, so a test that goes
        through it can never reach this branch — and the two guards would cover for each
        other, leaving either free to be deleted. A provider that one day reports
        `complete: true` with empty text meets this one.
        """
        from voice.live.base import LiveEvent

        h = _Harness()
        await h.run([committed("item_U"), created("resp_1"),
                     call("resp_1", "call_1", text="删掉 build", interrupt=True)])
        blank = LiveEvent(seq=99, kind="input.transcript", input_item_id="item_U",
                          payload={"text": "   ", "complete": True})
        out = await h.strategy.feed(blank)
        self.assertEqual([type(d).__name__ for d in out], ["TranscriptFailed"])

    async def test_a2_the_adapter_marks_a_blank_completed_as_incomplete(self):
        """The WIRE's own guard, pinned separately for the same reason."""
        adapter = _adapter()
        for text in ("", "   "):
            with self.subTest(text=repr(text)):
                event = adapter.map_event({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "u", "transcript": text})[0]
                self.assertFalse(event.payload["complete"])
                self.assertTrue(event.payload["blank"])
        good = adapter.map_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "u", "transcript": "停"})[0]
        self.assertTrue(good.payload["complete"])
        self.assertNotIn("blank", good.payload)

    async def test_a2_a_blank_transcript_with_no_call_still_tells_the_loop(self):
        h = _Harness()
        out = await h.run([committed("item_U"), transcript_ok("item_U", "")])
        self.assertEqual([type(d).__name__ for d in out], ["TranscriptFailed"])

    async def test_a2_a_transcript_of_real_words_is_still_evidence(self):
        """The guard must not reject a short but real answer."""
        h = _Harness()
        out = await h.run([committed("u"), created("r"),
                           call("r", "c", text="停", interrupt=True),
                           transcript_ok("u", "停")])
        self.assertEqual([type(d).__name__ for d in out], ["Request"])
        self.assertEqual(out[0].transcript, "停")

    async def test_a2_a_failed_transcript_with_no_call_still_tells_the_loop(self):
        """An armed effect is consumed by the next committed item WHATEVER its transcript
        outcome, so the loop has to hear about a failure nothing was waiting on."""
        h = _Harness()
        out = await h.run([committed("item_U"), transcript_failed("item_U")])
        self.assertEqual([type(d).__name__ for d in out], ["TranscriptFailed"])

    async def test_a2_two_candidate_input_items_refuse_rather_than_guess(self):
        h = _Harness()
        out = await h.run([committed("item_A"), committed("item_B"), created("resp_1"),
                           call("resp_1", "call_1", text="跑测试", interrupt=True),
                           transcript_ok("item_A", "a"), transcript_ok("item_B", "b")])
        self.assertEqual(out, [])
        self.assertEqual([r["reason"] for r in h.strategy.refusals], ["ambiguous_input_item"])
        self.assertEqual(h.session.call_outputs[0][1]["ok"], False)

    async def test_a2_the_second_turn_joins_only_its_own_input_item(self):
        h = _Harness()
        out = await h.run([
            committed("item_A"), created("resp_1"),
            call("resp_1", "call_1", text="第一件事", interrupt=True),
            transcript_ok("item_A", "第一件事"),
            committed("item_B"), created("resp_2"),
            call("resp_2", "call_2", text="第二件事", interrupt=False),
            transcript_ok("item_B", "第二件事"),
        ])
        self.assertEqual([r.input_item_id for r in out], ["item_A", "item_B"])
        self.assertEqual([r.response_id for r in out], ["resp_1", "resp_2"])


class ToolSchemaAndMalformedArguments(unittest.IsolatedAsyncioTestCase):

    def test_the_schema_is_closed_and_carries_no_consent_tool(self):
        self.assertEqual([t["name"] for t in TOOLS], ["relay"])
        self.assertEqual(TOOL_NAMES, {"relay"})
        self.assertEqual(TOOLS[0]["parameters"]["required"], ["text", "interrupt"])
        names = {t["name"] for t in TOOLS}
        # G4: a model must never hold consent, so no such tool may exist to call.
        self.assertFalse(names & {"propose_effect", "confirm_effect"})
        for tool in TOOLS:
            self.assertEqual(tool["type"], "function")
            self.assertIn("parameters", tool)

    def test_malformed_arguments_are_none_never_an_empty_dict(self):
        # An empty object is a VALID call for a parameterless tool, so "nothing arrived"
        # must stay distinguishable from "the model sent {}".
        self.assertIsNone(parse_arguments(None))
        self.assertIsNone(parse_arguments(""))
        self.assertIsNone(parse_arguments("   "))
        self.assertIsNone(parse_arguments("{not json"))
        self.assertIsNone(parse_arguments('["a"]'))
        self.assertEqual(parse_arguments("{}"), {})
        self.assertEqual(parse_arguments('{"text":"x"}'), {"text": "x"})

    async def test_malformed_arguments_make_no_decision_and_tell_the_model(self):
        h = _Harness()
        out = await h.run([
            committed("item_U"), created("resp_1"), transcript_ok("item_U", "x"),
            {"type": "response.output_item.done", "response_id": "resp_1",
             "item": {"id": "i", "type": "function_call", "name": "relay",
                      "call_id": "call_bad", "arguments": "{oops"}},
        ])
        self.assertEqual(out, [])
        self.assertEqual([r["reason"] for r in h.strategy.refusals], ["malformed_arguments"])
        call_id, output = h.session.call_outputs[0]
        self.assertEqual(call_id, "call_bad")
        self.assertFalse(output["ok"])
        self.assertIn("error", output)

    async def test_unknown_tool_makes_no_decision_and_tells_the_model(self):
        h = _Harness()
        out = await h.run([committed("item_U"), created("resp_1"),
                           call("resp_1", "call_x", name="launch_missiles", target="x")])
        self.assertEqual(out, [])
        self.assertEqual([r["reason"] for r in h.strategy.refusals], ["unknown_tool"])
        self.assertFalse(h.session.call_outputs[0][1]["ok"])

    async def test_no_tool_at_all_decides_on_a_non_user_response(self):
        """Blocker 2: a call on a challenge response is never a decision."""
        for name, args in (("relay", {"text": "跑测试", "interrupt": True}),):
            with self.subTest(tool=name):
                h = _Harness()
                h.adapter._pending_origins["n1"] = "challenge"
                out = await h.run([
                    committed("item_U"),
                    {"type": "response.created",
                     "response": {"id": "resp_C",
                                  "metadata": {"origin": "challenge", "nonce": "n1"}}},
                    transcript_ok("item_U", "x"),
                    call("resp_C", "c", name=name, **args)])
                self.assertEqual(out, [], f"{name} decided on a challenge response")
                self.assertEqual([r["reason"] for r in h.strategy.refusals],
                                 ["not_user_origin"])
                self.assertEqual(h.session.call_outputs[-1][1],
                                 {"ok": False, "error": "not a user turn"})

    async def test_a_non_boolean_interrupt_is_refused_rather_than_defaulted(self):
        h = _Harness()
        out = await h.run([committed("item_U"), created("resp_1"),
                           transcript_ok("item_U", "x"),
                           call("resp_1", "c1", text="做点事", interrupt="whenever")])
        self.assertEqual(out, [])
        self.assertEqual([r["reason"] for r in h.strategy.refusals], ["malformed_arguments"])


class B2TruncateOnlyWithOneRenderedItem(unittest.IsolatedAsyncioTestCase):

    async def _speaking(self, sink: FakeSink) -> _Harness:
        h = _Harness(sink)
        await h.run([committed("item_U"), created("resp_S"),
                     {"type": "response.output_item.added", "response_id": "resp_S",
                      "item": {"id": "item_audio", "type": "message"}},
                     audio_delta("resp_S", "item_audio", 19200)])
        return h

    async def test_b2_one_rendered_item_truncates_at_exactly_rendered_ms(self):
        sink = FakeSink()
        h = await self._speaking(sink)
        sink.rendered[("resp_S", "item_audio")] = 400
        await h.run([{"type": "input_audio_buffer.speech_started",
                      "item_id": "item_next", "audio_start_ms": 5072}])
        self.assertEqual(h.session.cancelled, ["resp_S"])
        self.assertEqual(sink.cancelled, ["resp_S"])
        self.assertEqual(h.session.truncates, [("item_audio", 400)])

    async def test_b2_a_wire_done_response_still_playing_is_cut_locally_and_truncated(self):
        """Playback outlasts the wire ~3x. `response.done` has arrived, the speakers still
        hold the tail: the operator interrupts what they HEAR. No wire cancel (the provider
        already closed it — cancelling earns an error), but the local cut and the truncate
        happen. Measured live: the first port bailed here and a short answer could not be
        interrupted at all."""
        sink = FakeSink()
        h = await self._speaking(sink)
        await h.run([{"type": "response.done", "response": {"id": "resp_S", "status": "completed",
                                                            "output": []}}])
        sink.rendered[("resp_S", "item_audio")] = 400
        await h.run([{"type": "input_audio_buffer.speech_started",
                      "item_id": "item_next", "audio_start_ms": 5072}])
        self.assertEqual(h.session.cancelled, [], "a done response is not cancelled on the wire")
        self.assertEqual(sink.cancelled, ["resp_S"], "the speakers ARE cut")
        self.assertEqual(h.session.truncates, [("item_audio", 400)])

    async def test_b2_a_fully_heard_response_is_left_alone(self):
        """Every frame rendered: nothing to cut, and no epoch spent — the consent gate's
        delivery evidence for a challenge that was heard in full must survive the operator
        answering it."""
        sink = FakeSink()
        h = await self._speaking(sink)
        await h.run([{"type": "response.done", "response": {"id": "resp_S", "status": "completed",
                                                            "output": []}}])
        sink.rendered[("resp_S", "item_audio")] = 1200
        sink.ended.add("resp_S")
        await h.run([{"type": "input_audio_buffer.speech_started",
                      "item_id": "item_next", "audio_start_ms": 7000}])
        self.assertEqual(h.session.cancelled, [])
        self.assertEqual(sink.cancelled, [])
        self.assertEqual(h.session.truncates, [])

    async def test_b2_zero_rendered_frames_cancel_only_no_truncate(self):
        sink = FakeSink()
        h = await self._speaking(sink)
        # Audio arrived and was queued, but nothing was handed to the device.
        await h.run([{"type": "input_audio_buffer.speech_started", "item_id": "item_next"}])
        self.assertEqual(h.session.cancelled, ["resp_S"])
        self.assertEqual(h.session.truncates, [],
                         "an audio_end_ms we cannot prove must not be sent")

    async def test_b2_two_rendered_audio_items_cancel_only(self):
        sink = FakeSink()
        h = _Harness(sink)
        await h.run([committed("item_U"), created("resp_S"),
                     audio_delta("resp_S", "item_one", 19200),
                     audio_delta("resp_S", "item_two", 19200)])
        sink.rendered[("resp_S", "item_one")] = 400
        sink.rendered[("resp_S", "item_two")] = 120
        await h.run([{"type": "input_audio_buffer.speech_started", "item_id": "item_next"}])
        self.assertEqual(h.session.cancelled, ["resp_S"])
        self.assertEqual(h.session.truncates, [], "an ambiguous item must not be cut")

    async def test_b2_speech_started_with_nothing_speaking_cancels_nothing(self):
        h = _Harness()
        await h.run([{"type": "input_audio_buffer.speech_started", "item_id": "item_U"}])
        self.assertEqual(h.session.cancelled, [])
        self.assertEqual(h.session.truncates, [])

    async def test_b2_a_finished_response_is_not_cancelled_on_the_wire_but_is_still_cut(self):
        """`response.done` closes the response on the WIRE only. Cancelling it there earns an
        error event; but 400 ms of it has rendered and the rest is still in the speakers,
        so the operator's interruption cuts the sink and truncates the item. (This test
        used to assert no truncate — that was the defect that made short answers
        uninterruptible.)"""
        sink = FakeSink()
        h = await self._speaking(sink)
        sink.rendered[("resp_S", "item_audio")] = 400
        await h.run([{"type": "response.done",
                      "response": {"id": "resp_S", "status": "completed"}},
                     {"type": "input_audio_buffer.speech_started", "item_id": "item_next"}])
        self.assertEqual(h.session.cancelled, [])
        self.assertEqual(sink.cancelled, ["resp_S"])
        self.assertEqual(h.session.truncates, [("item_audio", 400)])

    async def test_b2_truncate_is_sent_as_given_the_adapter_does_not_clamp(self):
        """Clamping is the strategy's job — it owns the sink and therefore the evidence."""
        socket = FakeSocket([])
        adapter = RealtimeSession(provider="voice_live", model="m", api_key="k",
                                  endpoint="https://x.openai.azure.com",
                                  api_version="2026-07-15",
                                  connect=FakeSocket.connector([], socket=socket),
                                  open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)
        from voice.live.base import SessionConfig
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.truncate("item_audio", 400)
        sent = json.loads(socket.sent[-1])
        self.assertEqual(sent, {"type": "conversation.item.truncate",
                                "item_id": "item_audio", "content_index": 0,
                                "audio_end_ms": 400})


class RenderedMsIsFramesNotQueuedBytes(unittest.TestCase):

    def test_rendered_ms_counts_only_what_the_stream_accepted(self):
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            self.assertIsNone(sink.rendered_ms("r", "i"))
            sink.play("r", "i", b"\x00" * (24 * 2 * 100))     # 100 ms at 24 kHz pcm16
            sink.drain()
            self.assertEqual(sink.rendered_ms("r", "i"), 100)
            rendered = sink.cancel("r")
            self.assertEqual(rendered, 24 * 100)              # frames, not bytes
        finally:
            sink.close()

    def test_the_epoch_drops_a_chunk_already_dequeued_when_the_cut_lands(self):
        """The WRITER-side half of the epoch, pinned on its own.

        The two drop points cover for each other, so a test that only replays a stream
        passes with either one broken. This one pins the writer-side check alone.

        The writer takes a chunk off the queue BEFORE it checks anything, so between that
        `get()` and the `write()` the chunk is in no queue at all — `cancel()` cannot reach
        it however thoroughly it drains. Holding the writer in exactly that gap is the only
        way to exercise the check, and it is the gap a real barge-in lands in.
        """
        import threading

        from voice.audio.io import PlaybackSink

        in_the_gap = threading.Event()
        cut_done = threading.Event()

        class GapStream:
            """Blocks the writer between the dequeue and the device write."""

            def __init__(self):
                self.writes = []
                self.calls = 0

            def write(self, pcm):
                self.calls += 1
                self.writes.append(pcm)

            def abort(self): self.aborts = getattr(self, "aborts", 0) + 1
            def stop(self): pass
            def close(self): pass

        stream = GapStream()
        sink = PlaybackSink(open_output=lambda: stream)

        original_get = sink._q.get
        holding = {"armed": False}

        def gated_get(*args, **kw):
            entry = original_get(*args, **kw)
            if holding["armed"] and entry is not None and entry[0] == "r":
                holding["armed"] = False
                in_the_gap.set()      # the chunk is OFF the queue and not yet written
                cut_done.wait()       # ...and the cut lands right here
            return entry

        sink._q.get = gated_get       # type: ignore[method-assign]
        holding["armed"] = True
        sink.play("r", "i", b"\x22\x22" * 240)
        sink.start()
        try:
            self.assertTrue(in_the_gap.wait(timeout=5), "the writer never reached the gap")
            sink.cancel("r")          # drains the queue — but this chunk is not in it
            cut_done.set()
            sink.drain()
        finally:
            cut_done.set()
            sink.close()
        self.assertEqual(stream.writes, [],
                         "a chunk cancelled while in the writer's hand reached the device")

    def test_the_epoch_drops_a_play_that_arrives_after_the_cut(self):
        """The `play()`-side half, pinned on its own: the writer is never even offered it."""
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        # No writer thread at all, so only `play()` can refuse this.
        sink.cancel("r")
        sink.play("r", "i", b"\x33\x33" * 240)
        self.assertTrue(sink._q.empty(), "a cancelled response's audio was queued anyway")

    def test_queued_but_never_written_audio_is_not_counted_as_rendered(self):
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        # No start(): the writer thread never runs, so nothing reaches the device.
        sink.play("r", "i", b"\x00" * 4800)
        self.assertIsNone(sink.rendered_ms("r", "i"))
        self.assertEqual(sink.cancel("r"), 0)
        self.assertEqual(stream.writes, [])


if __name__ == "__main__":
    unittest.main()


class C2DeliveryEvidenceFromTheSink(unittest.TestCase):
    """`reached_end` and `epoch_unchanged` — the sink's half of the broker's evidence.

    C2 requires all three of: the challenge response is done, its transcript contains the
    full challenge text, and its local playback reached the last rendered frame with the
    epoch unchanged. The third is these two methods, and they answer about the DEVICE, not
    about the wire: a response the provider finished but whose audio is still queued has
    not been delivered to anyone.
    """

    def _sink(self):
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        return PlaybackSink(open_output=lambda: stream), stream

    def test_reached_end_is_false_while_frames_are_still_queued(self):
        sink, _stream = self._sink()
        # No writer thread, so what is played stays queued: the provider can be finished
        # while the operator has heard nothing at all.
        sink.play("r", "i", b"\x00" * 4800)
        sink.note_response_done("r")
        self.assertFalse(sink.reached_end("r"))

    def test_reached_end_is_false_before_the_response_is_done(self):
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.drain()
            # Everything queued has rendered, but the wire has not said the response is
            # over — more audio may still be coming.
            self.assertFalse(sink.reached_end("r"))
        finally:
            sink.close()

    def test_reached_end_is_true_after_drain_and_done(self):
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.drain()
            sink.note_response_done("r")
            self.assertTrue(sink.reached_end("r"))
        finally:
            sink.close()

    def test_reached_end_order_does_not_matter(self):
        """`response.done` routinely precedes the last frame reaching the device."""
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.note_response_done("r")          # the wire finishes first
            sink.drain()
            self.assertTrue(sink.reached_end("r"))
        finally:
            sink.close()

    def test_a_response_that_rendered_nothing_never_reached_its_end(self):
        """Crediting silence as delivered is exactly the failure C2 exists to prevent."""
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.note_response_done("r")
            self.assertFalse(sink.reached_end("r"))
        finally:
            sink.close()

    def test_reached_end_is_per_response(self):
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r1", "i", b"\x00" * 4800)
            sink.drain()
            sink.note_response_done("r1")
            sink.play("r2", "i", b"\x00" * 4800)
            sink.note_response_done("r2")
            self.assertTrue(sink.reached_end("r1"))
            sink.drain()
            self.assertTrue(sink.reached_end("r2"))
        finally:
            sink.close()

    def test_epoch_unchanged_is_true_for_an_uninterrupted_response(self):
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.drain()
            sink.note_response_done("r")
            self.assertTrue(sink.epoch_unchanged("r"))
        finally:
            sink.close()

    def test_epoch_unchanged_is_false_after_a_cancel_of_that_response(self):
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.drain()
            sink.cancel("r")
            self.assertFalse(sink.epoch_unchanged("r"))
        finally:
            sink.close()

    def test_a_cancel_of_another_response_leaves_this_epoch_intact(self):
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r1", "i", b"\x00" * 4800)
            sink.drain()
            sink.cancel("r2")
            self.assertTrue(sink.epoch_unchanged("r1"),
                            "another response's cut must not invalidate this one")
        finally:
            sink.close()

    def test_a_cancel_before_anything_rendered_did_not_break_the_epoch(self):
        """Nothing was heard, so nothing was interrupted. `reached_end` still refuses it,
        which is what keeps the un-played response from being credited as delivered."""
        sink, _stream = self._sink()
        sink.cancel("r")                # no writer ran: zero frames rendered
        self.assertTrue(sink.epoch_unchanged("r"))
        sink.note_response_done("r")
        self.assertFalse(sink.reached_end("r"))

    def test_an_interrupted_response_fails_the_evidence_as_a_whole(self):
        """The C2 shape end to end: cut mid-answer, so neither half holds."""
        sink, _stream = self._sink()
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.drain()
            sink.play("r", "i", b"\x00" * 4800)     # still queued when the cut lands
            sink.cancel("r")
            sink.note_response_done("r")
            self.assertFalse(sink.epoch_unchanged("r"))
            # The queue was dropped by the cut, so nothing of it is pending any more —
            # delivery is refused on the epoch, which is the honest reason.
            self.assertTrue(sink.reached_end("r"))
        finally:
            sink.close()


class TheAdapterTellsTheSinkWhenAResponseIsDone(unittest.IsolatedAsyncioTestCase):

    def test_response_done_reaches_the_sink(self):
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        adapter = _adapter(sink)
        sink.start()
        try:
            adapter.map_event({"type": "response.audio.delta", "response_id": "r",
                               "item_id": "i",
                               "delta": __import__("base64").b64encode(b"\x00" * 4800)
                               .decode("ascii")})
            sink.drain()
            self.assertFalse(sink.reached_end("r"))
            adapter.map_event({"type": "response.done",
                               "response": {"id": "r", "status": "completed"}})
            self.assertTrue(sink.reached_end("r"),
                            "the adapter never told the sink the response was over")
        finally:
            sink.close()

    def test_a_sink_without_the_hook_is_not_required_to_have_one(self):
        """The replay fakes do not track delivery; the adapter must not demand it."""
        adapter = _adapter(FakeSink())
        events = adapter.map_event({"type": "response.done",
                                    "response": {"id": "r", "status": "completed"}})
        self.assertEqual([e.kind for e in events], ["response.done"])


class TheWriterCancelBarrier(unittest.TestCase):
    """Blocker 5: a chunk dequeued before `cancel()` must not be written after it.

    The old shape checked the cancelled set, RELEASED the lock, then wrote. A cut landing
    in that window let the operator hear the response they had just interrupted. The check
    and the write now happen under one hold, and `cancel()` additionally aborts the
    device's own ring — software state alone does not stop a buffer that is already
    sounding.
    """

    def test_a_cut_racing_the_write_cannot_slip_a_chunk_past_the_barrier(self):
        """A cut arriving mid-write serializes behind it, then aborts.

        The probe has to sit between the cancelled-set check and the device write: a probe
        placed earlier proves nothing, because `cancel()` simply wins the lock first and
        both the racy and the fixed shape pass.
        """
        import threading

        from voice.audio.io import PlaybackSink

        writing = threading.Event()
        release = threading.Event()
        order: list[str] = []

        class ProbeStream:
            def __init__(self):
                self.writes = []
                self.aborts = 0
                self.active = True

            def write(self, pcm):
                writing.set()
                release.wait(timeout=5)
                order.append("write")
                self.writes.append(pcm)

            def abort(self):
                order.append("abort")
                self.aborts += 1
                self.active = False

            def start(self):
                self.active = True

            def stop(self): pass
            def close(self): pass

        stream = ProbeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        cutter = None
        try:
            sink.play("r", "i", b"\x22\x22" * 240)
            self.assertTrue(writing.wait(timeout=5), "the writer never reached the write")
            # The cut lands while the writer is inside its write. It must BLOCK on the
            # device barrier rather than abort a chunk already going out.
            cutter = threading.Thread(target=lambda: sink.cancel("r"))
            cutter.start()
            release.set()
            cutter.join(timeout=5)
            sink.play("r", "i", b"\x33\x33" * 240)   # after the cut: never written
            sink.drain()
        finally:
            release.set()
            if cutter is not None:
                cutter.join(timeout=5)
            sink.close()
        self.assertEqual(stream.writes, [b"\x22\x22" * 240],
                         "a chunk written after the cut reached the device")
        self.assertEqual(order, ["write", "abort"],
                         "the cut must serialize behind the in-flight write, then abort")

    def test_the_stream_is_live_again_after_a_cut_so_the_next_reply_is_heard(self):
        """THE BLOCKER. `abort()` leaves a PortAudio stream stopped, so without a restart
        the next reply's first write raises, the writer thread dies, and the operator hears
        nothing for the rest of the call after one barge-in."""
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.play("A", "a", b"\x11" * 4800)
            sink.drain()
            sink.cancel("A")
            self.assertTrue(stream.active, "the stream was left stopped after the cut")

            # The NEXT response must be heard in full.
            for chunk in (b"\x22" * 4800, b"\x33" * 4800, b"\x44" * 4800):
                sink.play("B", "b", chunk)
            sink.drain()
        finally:
            sink.close()
        self.assertFalse(sink.dead, "the writer thread died after the barge-in")
        played = b"".join(stream.writes)
        for chunk in (b"\x22" * 4800, b"\x33" * 4800, b"\x44" * 4800):
            self.assertIn(chunk, played, "a frame of the next reply never reached the device")
        self.assertEqual(sink.rendered_ms("B", "b"), 300)

    def test_a_cut_of_the_old_response_never_aborts_the_new_ones_audio(self):
        """A's cancel arriving while B is mid-write must wait, not cut B."""
        import threading

        from voice.audio.io import PlaybackSink

        writing_b = threading.Event()
        cut_attempted = threading.Event()
        events: list[str] = []

        class ProbeStream:
            def __init__(self):
                self.writes = []
                self.active = True

            def write(self, pcm):
                if pcm.startswith(b"\xbb"):
                    writing_b.set()
                    # Held until the cut has HAD ITS CHANCE to abort. Under the device
                    # barrier the cut blocks here and cannot; without it, it aborts now and
                    # `abort` lands before `wrote-B`, which is what the order assertion
                    # catches. Nothing signals this release — a cut that is correctly
                    # blocked would never fire it — so the write proceeds on its own.
                    cut_attempted.wait(timeout=0.5)
                    events.append("wrote-B")
                self.writes.append(pcm)

            def abort(self):
                events.append("abort")
                self.active = False

            def start(self):
                self.active = True

            def stop(self): pass
            def close(self): pass

        stream = ProbeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        cutter = None
        try:
            sink.play("A", "a", b"\xaa" * 480)     # renders, so A is abortable
            sink.drain()
            sink.play("B", "b", b"\xbb" * 480)     # the writer blocks inside this one
            self.assertTrue(writing_b.wait(timeout=5))
            cutter = threading.Thread(target=lambda: sink.cancel("A"))
            cutter.start()
            cutter.join(timeout=5)
            sink.drain()
        finally:
            cut_attempted.set()
            if cutter is not None:
                cutter.join(timeout=5)
            sink.close()
        self.assertEqual(events, ["wrote-B", "abort"],
                         "A's cut aborted the device while B was writing")
        self.assertIn(b"\xbb" * 480, b"".join(stream.writes))
        self.assertEqual(sink.rendered_ms("B", "b"), 10)
        self.assertTrue(sink.epoch_unchanged("B"), "B was not the response that was cut")

    def test_cancel_aborts_the_device_buffer_when_something_had_rendered(self):
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.drain()
            self.assertEqual(stream.aborts, 0)
            sink.cancel("r")
        finally:
            sink.close()
        self.assertEqual(stream.aborts, 1,
                         "frames already in PortAudio's ring keep sounding without abort")

    def test_cancel_does_not_abort_when_this_response_never_rendered(self):
        """Aborting then would cut the tail of whatever is legitimately playing."""
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.cancel("never-played")
        finally:
            sink.close()
        self.assertEqual(stream.aborts, 0)

    def test_a_device_that_cannot_abort_does_not_fail_the_cut(self):
        from voice.audio.io import PlaybackSink

        class NoAbort:
            def __init__(self):
                self.writes = []

            def write(self, pcm):
                self.writes.append(pcm)

            def abort(self):
                raise OSError("device does not support abort")

            def stop(self): pass
            def close(self): pass

        stream = NoAbort()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.play("r", "i", b"\x00" * 4800)
            sink.drain()
            rendered = sink.cancel("r")      # must not raise
        finally:
            sink.close()
        self.assertEqual(rendered, 2400)
        self.assertFalse(sink.epoch_unchanged("r"))


class ADeadDeviceIsNeverEvidence(unittest.TestCase):
    """The speaker is gone, so nothing was heard — and nothing may claim it was.

    Reproduced before the fix: `reached_end` returned True with ZERO device writes, because
    a frame the writer discarded for want of a stream was still counted as rendered. An
    unheard consent challenge would have satisfied C2's third delivery evidence, and the
    next thing the operator said could have executed an irreversible effect.
    """

    class DeadOnStart:
        """Aborts, then refuses to come back up."""

        def __init__(self):
            self.writes = []
            self.active = True

        def write(self, pcm):
            if not self.active:
                raise RuntimeError("write to an inactive stream")
            self.writes.append(pcm)

        def abort(self):
            self.active = False

        def start(self):
            raise OSError("cannot restart")

        def stop(self): pass
        def close(self): pass

    def _sink_whose_device_dies(self):
        from voice.audio.io import PlaybackSink

        made = {"n": 0}
        first = self.DeadOnStart()

        def opener():
            made["n"] += 1
            if made["n"] == 1:
                return first
            raise OSError("cannot replace either")

        sink = PlaybackSink(open_output=opener)
        sink.start()
        sink.play("A", "a", b"\x11" * 4800)
        sink.drain()
        sink.cancel("A")          # abort -> restart fails -> replacement fails
        return sink, first

    def test_restart_and_replacement_both_failing_leaves_no_device(self):
        sink, stream = self._sink_whose_device_dies()
        try:
            self.assertIsNone(sink._stream)
            self.assertTrue(sink.device_dead)
        finally:
            sink.close()

    def test_frames_written_to_no_device_are_dropped_not_rendered(self):
        sink, stream = self._sink_whose_device_dies()
        try:
            before = len(stream.writes)
            sink.play("B", "b", b"\x22" * 4800)
            sink.drain()
            self.assertEqual(len(stream.writes), before, "a frame reached a dead device")
            self.assertIsNone(sink.rendered_ms("B", "b"),
                              "a frame nobody heard was counted as rendered")
            self.assertEqual(sink.dropped_frames("B"), 2400)
        finally:
            sink.close()

    def test_pending_is_cleared_as_dropped_so_reached_end_cannot_hang_or_lie(self):
        sink, _stream = self._sink_whose_device_dies()
        try:
            sink.play("B", "b", b"\x22" * 4800)
            sink.drain()
            sink.note_response_done("B")
            self.assertFalse(sink.reached_end("B"))
            self.assertFalse(sink.epoch_unchanged("B"))
        finally:
            sink.close()

    def test_a_response_rendered_BEFORE_the_device_died_still_reports_no_delivery(self):
        """Its tail was in a buffer that never drained, so what was heard is unknown."""
        sink, _stream = self._sink_whose_device_dies()
        try:
            sink.note_response_done("A")
            self.assertFalse(sink.reached_end("A"))
            self.assertFalse(sink.epoch_unchanged("A"))
        finally:
            sink.close()

    def test_a_write_that_raises_drops_everything_queued_behind_it(self):
        from voice.audio.io import PlaybackSink

        class FailsOnSecond:
            def __init__(self):
                self.writes = []

            def write(self, pcm):
                if len(self.writes) >= 1:
                    raise OSError("device vanished")
                self.writes.append(pcm)

            def abort(self): pass
            def start(self): pass
            def stop(self): pass
            def close(self): pass

        stream = FailsOnSecond()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.play("C", "c", b"\x33" * 4800)
            sink.drain()
            sink.play("C", "c", b"\x44" * 4800)
            sink.drain()
            sink.note_response_done("C")
            self.assertTrue(sink.device_dead)
            self.assertFalse(sink.reached_end("C"),
                             "a half-played response must not claim it reached its end")
            self.assertGreater(sink.dropped_frames("C"), 0)
        finally:
            sink.close()


class TheBrokerRefusesDeliveryOnADeadDevice(unittest.IsolatedAsyncioTestCase):
    """C2 end to end: a real broker, a real sink, and a device that dies.

    This is where the sink's bookkeeping becomes a consent decision, so it is asserted
    against the actual `Broker` rather than the flags in isolation. Before the fix the
    sink reported delivery for a challenge nobody heard, and the next thing the operator
    said could have executed an irreversible effect.
    """

    class Dialog:
        occurrence_id = "occ-1"
        kind = "permission"
        prompt = "Delete build/?"
        action = "rm -rf build/"
        scope = "build/"
        options = (("1", "yes"), ("2", "no"))

    def _armed_broker(self):
        from voice.agent.broker import Broker

        broker = Broker()
        armed = broker.arm(self.Dialog(), revision=1)
        self.assertIsNotNone(armed, "the fixture dialog must be armable")
        broker.speaking("resp_challenge")
        return broker

    def test_a_challenge_whose_audio_never_played_is_tombstoned_not_delivered(self):
        from voice.audio.io import PlaybackSink

        class DeadStream:
            def __init__(self):
                self.writes = []

            def write(self, pcm):
                raise OSError("device vanished")

            def abort(self): pass
            def start(self): pass
            def stop(self): pass
            def close(self): pass

        stream = DeadStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        broker = self._armed_broker()
        try:
            sink.play("resp_challenge", "item_c", b"\x00" * 4800)
            sink.drain()
            sink.note_response_done("resp_challenge")
            self.assertTrue(sink.device_dead)
            # Exactly what `loop._check_rendered` does with the sink's answers.
            broker.note_rendered("resp_challenge",
                                 reached_end=bool(sink.reached_end("resp_challenge")),
                                 epoch_unchanged=bool(sink.epoch_unchanged("resp_challenge")))
        finally:
            sink.close()
        self.assertFalse(broker.is_delivered(),
                         "a challenge nobody heard was treated as delivered")
        self.assertIsNone(broker.armed, "the unheard challenge must be tombstoned")

    def test_a_challenge_written_to_NO_device_is_also_refused(self):
        """The other death: restart and replacement both failed, so `_stream` is None and
        every later frame is discarded silently. This is the shape the reviewer
        reproduced — `reached_end=True` with zero device writes."""
        from voice.audio.io import PlaybackSink

        class DiesOnRestart:
            def __init__(self):
                self.writes = []
                self.active = True

            def write(self, pcm):
                if not self.active:
                    raise RuntimeError("inactive")
                self.writes.append(pcm)

            def abort(self):
                self.active = False

            def start(self):
                raise OSError("cannot restart")

            def stop(self): pass
            def close(self): pass

        made = {"n": 0}

        def opener():
            made["n"] += 1
            if made["n"] == 1:
                return DiesOnRestart()
            raise OSError("cannot replace either")

        sink = PlaybackSink(open_output=opener)
        sink.start()
        broker = self._armed_broker()
        try:
            # The device dies on a DIFFERENT response, then the challenge is spoken into
            # the wreckage. `epoch_unchanged` is deliberately not the thing under test
            # here — the cut belongs to "earlier", so only `reached_end` can catch this.
            sink.play("earlier", "i", b"\x11" * 4800)
            sink.drain()
            sink.cancel("earlier")               # kills the device outright
            self.assertIsNone(sink._stream)
            sink.play("resp_challenge", "item_c", b"\x00" * 4800)
            sink.drain()
            sink.note_response_done("resp_challenge")
            reached = bool(sink.reached_end("resp_challenge"))
            self.assertFalse(reached,
                             "a challenge written to no device claimed it reached its end")
            broker.note_rendered("resp_challenge", reached_end=reached,
                                 epoch_unchanged=True)   # the cut was another response's
        finally:
            sink.close()
        self.assertFalse(broker.is_delivered())
        self.assertIsNone(broker.armed, "the unheard challenge must be tombstoned")

    def test_a_challenge_that_really_played_is_delivered(self):
        """The guard must not refuse a working speaker, or consent could never be given."""
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        broker = self._armed_broker()
        try:
            sink.play("resp_challenge", "item_c", b"\x00" * 4800)
            sink.drain()
            sink.note_response_done("resp_challenge")
            self.assertFalse(sink.device_dead)
            broker.note_rendered("resp_challenge",
                                 reached_end=bool(sink.reached_end("resp_challenge")),
                                 epoch_unchanged=bool(sink.epoch_unchanged("resp_challenge")))
        finally:
            sink.close()
        self.assertIsNotNone(broker.armed, "a heard challenge must stay armed")
        self.assertTrue(broker.armed.evidence_rendered)
