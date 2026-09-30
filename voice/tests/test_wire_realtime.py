"""The adapter's own rules: serialized origins, generation, and the event mapping table.

The origin queue is what makes A1's join possible at all, so it is tested here on its own
terms rather than only through a replay: a broker-created response must never be able to
take the `user` attribution that belongs to a committed input item.
"""
from __future__ import annotations

import json
import unittest

from voice.live.base import (
    KIND_CALL,
    KIND_DISCONNECTED,
    KIND_ERROR,
    KIND_INPUT_COMMITTED,
    KIND_INPUT_SPEECH_STARTED,
    KIND_INPUT_SPEECH_STOPPED,
    KIND_INPUT_TRANSCRIPT,
    KIND_OUTPUT_AUDIO,
    KIND_OUTPUT_ITEM,
    KIND_OUTPUT_TRANSCRIPT,
    KIND_RESPONSE_CREATED,
    KIND_RESPONSE_DONE,
    KIND_SESSION_STARTED,
    SessionConfig,
)
from voice.live.realtime import RealtimeSession
from voice.tests.wire_fakes import FakeSink, FakeSocket


def _adapter(socket: FakeSocket | None = None, sink=None) -> RealtimeSession:
    return RealtimeSession(provider="voice_live", model="m", api_key="k",
                           endpoint="https://x.openai.azure.com", api_version="2026-07-15",
                           sink=sink,
                           connect=FakeSocket.connector([], socket=socket),
                           open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)


class OriginIsCorrelatedByProviderMetadata(unittest.IsolatedAsyncioTestCase):
    """Origin travels WITH the response, in `response.metadata`, not by arrival order.

    The FIFO this replaces mis-attributed the moment the provider interleaved one of its
    own: server VAD can open a user turn between our create and its `response.created`, and
    that user response would then wear our `challenge` origin while the challenge wore
    `user`. A call on the first is refused when it should dispatch; a call on the second
    dispatches when it must be refused.
    """

    async def test_a_provider_started_response_is_origin_user(self):
        adapter = _adapter()
        events = adapter.map_event({"type": "response.created",
                                    "response": {"id": "resp_1"}})
        self.assertEqual(events[0].kind, KIND_RESPONSE_CREATED)
        self.assertEqual(events[0].payload["origin"], "user")
        self.assertNotIn("unknown_origin", events[0].payload)

    async def test_the_create_carries_an_origin_and_a_nonce_in_its_metadata(self):
        socket = FakeSocket([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.create_response("challenge")
        sent = json.loads(socket.sent[-1])
        metadata = sent["response"]["metadata"]
        self.assertEqual(metadata["origin"], "challenge")
        self.assertTrue(metadata["nonce"])

    async def test_the_echoed_nonce_names_its_own_origin_whatever_the_order(self):
        socket = FakeSocket([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.create_response("challenge")
        challenge_nonce = json.loads(socket.sent[-1])["response"]["metadata"]["nonce"]
        await adapter.create_response("narration")
        narration_nonce = json.loads(socket.sent[-1])["response"]["metadata"]["nonce"]

        # The provider answers them OUT OF ORDER, with one of its own in between.
        second = adapter.map_event({"type": "response.created", "response": {
            "id": "r2", "metadata": {"origin": "narration", "nonce": narration_nonce}}})
        provider = adapter.map_event({"type": "response.created",
                                      "response": {"id": "r_user"}})
        first = adapter.map_event({"type": "response.created", "response": {
            "id": "r1", "metadata": {"origin": "challenge", "nonce": challenge_nonce}}})
        self.assertEqual(second[0].payload["origin"], "narration")
        self.assertEqual(provider[0].payload["origin"], "user")
        self.assertEqual(first[0].payload["origin"], "challenge")

    async def test_an_interleaved_provider_response_keeps_its_user_origin(self):
        """The exact case the FIFO got wrong."""
        socket = FakeSocket([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.create_response("challenge")
        nonce = json.loads(socket.sent[-1])["response"]["metadata"]["nonce"]
        # Server VAD opens a user turn BEFORE our challenge is acknowledged.
        interleaved = adapter.map_event({"type": "response.created",
                                         "response": {"id": "r_user"}})
        self.assertEqual(interleaved[0].payload["origin"], "user")
        ours = adapter.map_event({"type": "response.created", "response": {
            "id": "r_challenge", "metadata": {"origin": "challenge", "nonce": nonce}}})
        self.assertEqual(ours[0].payload["origin"], "challenge")

    async def test_metadata_naming_no_pending_create_is_refused_as_unknown(self):
        adapter = _adapter()
        events = adapter.map_event({"type": "response.created", "response": {
            "id": "r1", "metadata": {"origin": "user", "nonce": "never-sent"}}})
        self.assertTrue(events[0].payload["unknown_origin"])
        self.assertNotEqual(events[0].payload["origin"], "user",
                            "metadata we never sent must not be able to claim a user turn")

    async def test_a_replayed_nonce_is_unknown_the_second_time(self):
        socket = FakeSocket([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.create_response("challenge")
        nonce = json.loads(socket.sent[-1])["response"]["metadata"]["nonce"]
        body = {"type": "response.created",
                "response": {"id": "r1", "metadata": {"origin": "challenge",
                                                      "nonce": nonce}}}
        self.assertEqual(adapter.map_event(body)[0].payload["origin"], "challenge")
        self.assertTrue(adapter.map_event(body)[0].payload["unknown_origin"])

    async def test_a_create_that_never_left_does_not_shift_later_attribution(self):
        class Broken(FakeSocket):
            async def send(self, payload: str) -> None:
                if json.loads(payload).get("type") == "response.create":
                    raise ConnectionError("socket gone")
                await super().send(payload)

        socket = Broken([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        with self.assertRaises(ConnectionError):
            await adapter.create_response("challenge")
        events = adapter.map_event({"type": "response.created", "response": {"id": "r1"}})
        self.assertEqual(events[0].payload["origin"], "user")

    async def test_create_response_carries_instructions_and_tool_choice(self):
        socket = FakeSocket([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.create_response("challenge", instructions="说这句话",
                                      tool_choice="none")
        body = json.loads(socket.sent[-1])["response"]
        self.assertEqual(body["instructions"], "说这句话")
        self.assertEqual(body["tool_choice"], "none")
        self.assertEqual(body["metadata"]["origin"], "challenge")

    async def test_reconnect_bumps_the_generation_and_forgets_pending_nonces(self):
        socket = FakeSocket([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.create_response("challenge")
        nonce = json.loads(socket.sent[-1])["response"]["metadata"]["nonce"]
        self.assertEqual(adapter.note_reconnect(), 1)
        events = adapter.map_event({"type": "response.created", "response": {
            "id": "r1", "metadata": {"origin": "challenge", "nonce": nonce}}})
        self.assertEqual(events[0].generation, 1)
        self.assertTrue(events[0].payload["unknown_origin"],
                        "an origin promised on a dead socket must not survive it")


class EventMapping(unittest.TestCase):

    def setUp(self) -> None:
        self.sink = FakeSink()
        self.adapter = _adapter(sink=self.sink)

    def one(self, raw: dict):
        events = self.adapter.map_event(raw)
        self.assertEqual(len(events), 1, f"{raw['type']} mapped to {len(events)} events")
        return events[0]

    def test_session_created(self):
        event = self.one({"type": "session.created", "session": {"id": "sess_1"}})
        self.assertEqual(event.kind, KIND_SESSION_STARTED)
        self.assertEqual(event.payload["session_id"], "sess_1")

    def test_committed_carries_the_input_item_id(self):
        event = self.one({"type": "input_audio_buffer.committed", "item_id": "item_U"})
        self.assertEqual(event.kind, KIND_INPUT_COMMITTED)
        self.assertEqual(event.input_item_id, "item_U")

    def test_speech_started_and_stopped_carry_their_ms_ranges(self):
        started = self.one({"type": "input_audio_buffer.speech_started",
                            "item_id": "item_U", "audio_start_ms": 1104})
        self.assertEqual(started.kind, KIND_INPUT_SPEECH_STARTED)
        self.assertEqual(started.start_ms, 1104)
        stopped = self.one({"type": "input_audio_buffer.speech_stopped",
                            "item_id": "item_U", "audio_end_ms": 5664})
        self.assertEqual(stopped.kind, KIND_INPUT_SPEECH_STOPPED)
        self.assertEqual(stopped.end_ms, 5664)

    def test_completed_transcription_is_complete_true(self):
        event = self.one({"type": "conversation.item.input_audio_transcription.completed",
                          "item_id": "item_U", "transcript": "跑测试"})
        self.assertEqual(event.kind, KIND_INPUT_TRANSCRIPT)
        self.assertEqual(event.payload, {"text": "跑测试", "complete": True})

    def test_failed_transcription_is_complete_false_with_empty_text(self):
        event = self.one({"type": "conversation.item.input_audio_transcription.failed",
                          "item_id": "item_U", "error": {"message": "too short"}})
        self.assertEqual(event.kind, KIND_INPUT_TRANSCRIPT)
        self.assertEqual(event.payload["complete"], False)
        self.assertEqual(event.payload["text"], "")
        self.assertEqual(event.payload["error"], "too short")

    def test_output_item_added_carries_the_item_type(self):
        event = self.one({"type": "response.output_item.added", "response_id": "r",
                          "item": {"id": "i", "type": "function_call", "name": "request",
                                   "call_id": "c"}})
        self.assertEqual(event.kind, KIND_OUTPUT_ITEM)
        self.assertEqual(event.payload["type"], "function_call")
        self.assertEqual(event.item_id, "i")

    def test_audio_delta_sends_bytes_to_the_sink_and_frames_to_the_event(self):
        import base64
        pcm = b"\x01\x02" * 240                     # 240 frames = 10 ms
        event = self.one({"type": "response.audio.delta", "response_id": "r",
                          "item_id": "i",
                          "delta": base64.b64encode(pcm).decode("ascii")})
        self.assertEqual(event.kind, KIND_OUTPUT_AUDIO)
        self.assertEqual(event.payload["frames"], 240)
        self.assertEqual(self.sink.plays, [("r", "i", pcm)])

    def test_a_stripped_fixture_delta_carries_frames_but_no_bytes(self):
        event = self.one({"type": "response.audio.delta", "response_id": "r",
                          "item_id": "i", "delta": 19200})
        self.assertEqual(event.payload["frames"], 9600)
        self.assertEqual(self.sink.plays, [], "a byte count is not audio")

    def test_transcript_delta_and_done(self):
        delta = self.one({"type": "response.audio_transcript.delta", "response_id": "r",
                          "item_id": "i", "delta": "后台"})
        self.assertEqual(delta.kind, KIND_OUTPUT_TRANSCRIPT)
        self.assertEqual(delta.payload, {"text": "后台", "done": False})
        done = self.one({"type": "response.audio_transcript.done", "response_id": "r",
                         "item_id": "i", "transcript": "后台现在正在跑一个很长的流程"})
        self.assertEqual(done.payload["done"], True)
        self.assertEqual(done.payload["text"], "后台现在正在跑一个很长的流程")

    def test_function_call_item_done_becomes_a_call_with_parsed_arguments(self):
        event = self.one({"type": "response.output_item.done", "response_id": "r",
                          "item": {"id": "i", "type": "function_call", "name": "request",
                                   "call_id": "call_1",
                                   "arguments": '{"text":"跑测试","priority":"now"}'}})
        self.assertEqual(event.kind, KIND_CALL)
        self.assertEqual(event.payload["name"], "request")
        self.assertEqual(event.payload["call_id"], "call_1")
        self.assertEqual(event.payload["arguments"],
                         {"text": "跑测试", "priority": "now"})

    def test_a_malformed_call_carries_none_arguments(self):
        event = self.one({"type": "response.output_item.done", "response_id": "r",
                          "item": {"id": "i", "type": "function_call", "name": "request",
                                   "call_id": "c", "arguments": ""}})
        self.assertIsNone(event.payload["arguments"])

    def test_response_done_carries_its_status(self):
        for status in ("completed", "cancelled", "failed"):
            event = self.one({"type": "response.done",
                              "response": {"id": "r", "status": status}})
            self.assertEqual(event.kind, KIND_RESPONSE_DONE)
            self.assertEqual(event.payload["status"], status)

    def test_error_carries_its_message(self):
        event = self.one({"type": "error", "error": {"message": "bad request"}})
        self.assertEqual(event.kind, KIND_ERROR)
        self.assertEqual(event.payload["message"], "bad request")

    def test_an_unknown_event_maps_to_nothing_rather_than_a_guess(self):
        self.assertEqual(self.adapter.map_event({"type": "rate_limits.updated"}), [])

    def test_seq_is_monotonic_across_every_mapped_event(self):
        seqs = []
        for raw in ({"type": "input_audio_buffer.committed", "item_id": "a"},
                    {"type": "response.created", "response": {"id": "r"}},
                    {"type": "response.done", "response": {"id": "r", "status": "completed"}}):
            seqs.extend(e.seq for e in self.adapter.map_event(raw))
        self.assertEqual(seqs, sorted(set(seqs)))


class BeforeTheSocketOpens(unittest.IsolatedAsyncioTestCase):
    """The daemon starts the microphone before the socket. Frames captured in that window
    are fire-and-forget tasks with nowhere to go: they drop, silently. A control message in
    the same state is a caller bug and still raises (measured live: 78 unretrieved
    `not connected` tracebacks on the first start, one per pre-connect frame)."""

    async def test_audio_before_start_is_dropped_not_raised(self):
        session = _adapter()
        self.assertIsNone(await session.send_audio(b"\x00" * 320))

    async def test_a_control_message_before_start_still_raises(self):
        session = _adapter()
        with self.assertRaises(RuntimeError):
            await session.create_response("user")


class Disconnect(unittest.IsolatedAsyncioTestCase):

    async def test_a_dead_socket_yields_disconnected_with_the_generation_that_died(self):
        socket = FakeSocket([{"type": "session.created", "session": {"id": "s"}}])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        kinds = [event.kind async for event in adapter.events()]
        self.assertEqual(kinds, [KIND_SESSION_STARTED, KIND_DISCONNECTED])

    async def test_close_marks_the_stream_closed_rather_than_disconnected(self):
        socket = FakeSocket([])
        adapter = _adapter(socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        await adapter.close()
        self.assertTrue(socket.closed)
        kinds = [event.kind async for event in adapter.events()]
        self.assertEqual(kinds, [KIND_CLOSED := "closed"])


class NoTimeLiteralsInTheWireModules(unittest.TestCase):
    """DESIGN.md §Timers: the only permitted bounds are constructor ports, and no sleeps."""

    def test_the_bounds_are_constructor_parameters_with_no_module_default(self):
        import inspect

        from voice.live import realtime

        signature = inspect.signature(realtime.RealtimeSession.__init__)
        for name in ("open_bound_s", "close_bound_s", "reconnect_bound_s"):
            parameter = signature.parameters[name]
            self.assertIs(parameter.default, inspect.Parameter.empty,
                          f"{name} must be injected, never defaulted in the module")

    def test_no_sleep_in_the_wire_modules(self):
        import pathlib

        from voice.audio import io as audio_io
        from voice.live import realtime, strategy_function

        for module in (realtime, strategy_function):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("sleep(", source, f"{module.__name__} must not sleep")
        # audio/io.py holds ONE sleep: AudioLock's retry between lock attempts. A file
        # lock offers no readiness event a selector can wait on, so waiting means retrying
        # — and the cadence is INJECTED (`poll_s`), never a literal in this module.
        source = pathlib.Path(audio_io.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("sleep("), 1)
        self.assertIn("time.sleep(self._poll_s)", source)
        self.assertNotIn("POLL_S =", source, "the cadence must not be declared here")

    def test_the_lock_refuses_to_wait_without_an_injected_cadence(self):
        from voice.audio.io import AudioLock

        with self.assertRaises(ValueError):
            AudioLock(path="/tmp/never-created.lock").acquire(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
