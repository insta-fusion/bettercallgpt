"""DESIGN.md §Acceptance A1, A5, B1 — replayed against the RECORDED wire.

Each fixture line is one event with a client `seq`/`t`/`dir` plus the raw Voice Live event
(audio deltas stripped to byte counts). `dir: "in"` is the only direction that reaches the
mapper; `out` lines are what the spike client sent and `mark` lines are its own test
annotations, both skipped.

What a replay proves that a hand-built event list cannot: the ORDER is the provider's, not
ours — in R0 the function call lands eight events before the transcript that evidences it
(seq 47 vs seq 55), which is exactly the ordering A2 pins down in isolation.
"""
from __future__ import annotations

import base64
import json
import unittest
import zlib
from pathlib import Path

from voice.live.base import Request, SessionConfig
from voice.live.realtime import RENAME, RealtimeSession
from voice.live.strategy_function import FunctionStrategy
from voice.tests.wire_fakes import FakeSink, FakeSocket, RecordingSession, fixture_events


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _marked_pcm(event_id: str, nbytes: int) -> bytes:
    """`nbytes` of PCM unique to one recorded delta.

    The fixtures stripped audio to byte counts, so a replay has to supply samples. Making
    each delta's payload distinctive is what lets a test say WHICH delta a byte on the
    device came from — silence would make every delta indistinguishable from every other.
    """
    if nbytes <= 0:
        return b""
    seed = (zlib.crc32(event_id.encode()) % 0x7FFF) or 1   # stable across runs
    return (seed.to_bytes(2, "little") * ((nbytes // 2) + 1))[:nbytes]


def _session(sink: FakeSink | None = None,
             socket: FakeSocket | None = None) -> RealtimeSession:
    """An adapter wired to nothing: `map_event` is pure, so no socket is needed.

    Pass a `socket` only when the test also WRITES (a `create_response`, whose origin the
    serialized queue then owes to the next `response.created`).
    """
    return RealtimeSession(provider="voice_live", model="gpt-realtime-2.1",
                           api_key="k", endpoint="https://x.openai.azure.com",
                           api_version="2026-07-15", sink=sink,
                           connect=FakeSocket.connector([], socket=socket),
                           open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)


async def _started(sink: FakeSink | None = None) -> RealtimeSession:
    adapter = _session(sink, socket=FakeSocket([]))
    await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
    return adapter


def _as_relay(raw: dict) -> dict:
    """The fixtures were recorded under the five-tool schema (`request(text, priority)`); the
    schema is now one tool, `relay(text, interrupt)`. Only the call's NAME and argument shape
    are translated -- the provider's ordering and ids, which is what a replay proves, are
    untouched."""
    item = raw.get("item") if isinstance(raw, dict) else None
    if not (isinstance(item, dict) and item.get("type") == "function_call"
            and item.get("name") == "request"):
        return raw
    try:
        args = json.loads(item.get("arguments") or "{}")
    except json.JSONDecodeError:
        return raw
    args = {"text": args.get("text", ""), "interrupt": args.get("priority") == "now"}
    return {**raw, "item": {**item, "name": "relay",
                            "arguments": json.dumps(args, ensure_ascii=False)}}


async def _replay(name: str, *, sink: FakeSink | None = None,
                  inject=None) -> tuple[list, RecordingSession, FunctionStrategy]:
    """Map every inbound event of a fixture and feed it to the strategy.

    `inject(raw, index)` may return extra RAW events to splice in before that line —
    that is how A1's interleaved challenge response is added to a real timeline.
    """
    adapter = _session(sink)
    recorder = RecordingSession()
    strategy = FunctionStrategy(recorder, sink or FakeSink())
    decisions = []
    for index, raw in enumerate(fixture_events(FIXTURES / f"{name}.jsonl")):
        raw = _as_relay(raw)
        extra = inject(raw, index) if inject else None
        for injected in (extra or []):
            for event in adapter.map_event(injected):
                decisions.extend(await strategy.feed(event))
        for event in adapter.map_event(raw):
            decisions.extend(await strategy.feed(event))
    return decisions, recorder, strategy


class A1TurnIdentity(unittest.IsolatedAsyncioTestCase):
    """A1 — every `request` dispatch carries a unique input item and its own response."""

    async def test_a1_every_request_has_a_unique_input_item_and_matching_response(self):
        decisions, _recorder, strategy = await _replay("voice-rt-R0")
        requests = [d for d in decisions if type(d).__name__ == "Request"]
        self.assertTrue(requests, "R0 must produce request dispatches")

        input_items = [r.input_item_id for r in requests]
        self.assertEqual(len(input_items), len(set(input_items)),
                         "two requests joined the same input item")
        for request in requests:
            self.assertTrue(request.input_item_id, "a request without an input item id")
            self.assertTrue(request.response_id, "a request without a response id")
            self.assertTrue(request.call_id, "a request without a call id")
            # The response it names must be the one whose candidate list held that item.
            record = strategy._responses[request.response_id]
            self.assertEqual(record.origin, "user")

    async def test_a1_transcript_and_interpretation_both_ride_unedited(self):
        decisions, _recorder, _strategy = await _replay("voice-rt-R0")
        requests = [d for d in decisions if type(d).__name__ == "Request"]
        first = requests[0]
        # The recorded turn: ASR heard "revert", the model reconstructed "live" (recorded session).
        # BOTH survive, neither is edited into the other.
        self.assertEqual(first.transcript, "先确认一下，现在这个版本是不是已经revert了?")
        self.assertEqual(first.interpretation, "先确认一下，现在这个版本是不是已经 live 了。")
        self.assertNotEqual(first.transcript, first.interpretation)

    async def test_a1_interleaved_challenge_response_is_excluded_from_the_join(self):
        """A broker challenge created between a committed item and its call must not
        capture that item, and its own call must be refused."""
        challenge_id = "resp_CHALLENGE_INJECTED"

        def inject(raw, index):
            # Right after the FIRST committed input item, before its user response.
            if raw.get("type") == "input_audio_buffer.committed" and index < 20:
                return [
                    {"type": "response.created",
                     "response": {"id": challenge_id, "status": "in_progress"}},
                ]
            return None

        socket = FakeSocket([])
        adapter = _session(socket=socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        recorder = RecordingSession()
        strategy = FunctionStrategy(recorder, FakeSink())
        decisions = []
        injected_once = False
        for raw in fixture_events(FIXTURES / "voice-rt-R0.jsonl"):
            raw = _as_relay(raw)
            if not injected_once and raw.get("type") == "input_audio_buffer.committed":
                injected_once = True
                # The broker asked for this one, so its origin is `challenge` — carried
                # in the metadata the provider echoes, not inferred from arrival order.
                await adapter.create_response("challenge")
                nonce = json.loads(socket.sent[-1])["response"]["metadata"]["nonce"]
                for event in adapter.map_event(
                        {"type": "response.created",
                         "response": {"id": challenge_id, "status": "in_progress",
                                      "metadata": {"origin": "challenge",
                                                   "nonce": nonce}}}):
                    decisions.extend(await strategy.feed(event))
            for event in adapter.map_event(raw):
                decisions.extend(await strategy.feed(event))

        self.assertEqual(strategy._responses[challenge_id].origin, "challenge")
        self.assertEqual(strategy._responses[challenge_id].candidates, [],
                         "a challenge response must claim no input item")
        requests = [d for d in decisions if type(d).__name__ == "Request"]
        self.assertTrue(requests, "the user turn still dispatches around the challenge")
        self.assertNotIn(challenge_id, {r.response_id for r in requests})

    async def test_a1_a_call_on_a_challenge_response_is_refused_not_dispatched(self):
        socket = FakeSocket([])
        adapter = _session(socket=socket)
        await adapter.start(SessionConfig(instructions="", voice="", tools=[]))
        recorder = RecordingSession()
        strategy = FunctionStrategy(recorder, FakeSink())
        await adapter.create_response("challenge")
        nonce = json.loads(socket.sent[-1])["response"]["metadata"]["nonce"]
        stream = [
            {"type": "input_audio_buffer.committed", "item_id": "item_U"},
            {"type": "response.created",
             "response": {"id": "resp_C",
                          "metadata": {"origin": "challenge", "nonce": nonce}}},
            {"type": "response.output_item.done", "response_id": "resp_C",
             "item": {"id": "item_call", "type": "function_call", "name": "relay",
                      "call_id": "call_1",
                      "arguments": json.dumps({"text": "跑测试", "interrupt": True})}},
            {"type": "conversation.item.input_audio_transcription.completed",
             "item_id": "item_U", "content_index": 0, "transcript": "跑测试"},
        ]
        decisions = []
        for raw in stream:
            for event in adapter.map_event(raw):
                decisions.extend(await strategy.feed(event))
        self.assertEqual(decisions, [])
        self.assertEqual([r["reason"] for r in strategy.refusals], ["not_user_origin"])
        self.assertEqual(recorder.call_outputs[0][1]["ok"], False)


class A5CorrectionIsPriorityNow(unittest.IsolatedAsyncioTestCase):
    """A5 — the recorded correction 「先停掉…」 dispatches as `request(priority="now")`."""

    async def test_a5_r2_correction_yields_request_priority_now(self):
        decisions, _recorder, _strategy = await _replay("voice-rt-R2")
        requests = [d for d in decisions if type(d).__name__ == "Request"]
        self.assertTrue(requests, "R2 must produce a request")
        self.assertTrue(any(r.priority == "now" for r in requests),
                        f"no now-priority request in {[r.priority for r in requests]}")
        correction = [r for r in requests if r.priority == "now"][-1]
        self.assertTrue(correction.input_item_id)
        self.assertTrue(correction.response_id)


class MixedResponseUnderRequired(unittest.IsolatedAsyncioTestCase):
    """One response carrying BOTH a spoken message and a function call.

    `tool_choice: "required"` suppresses free speech only as a tendency: across 100
    required-mode turns, 2 leaked audio (spike round R11). The fixture is that exact
    turn, lifted from the recorded session (seq 116-206) — the message arrives at
    `output_index` 0 and the call at index 1, with different item ids, so both must land
    without confusing each other.
    """

    FIXTURE = "voice-rt-R11-D-mixed"
    RESPONSE = "resp_SYN000018"
    AUDIO_ITEM = "item_SYN000058"
    CALL_ITEM = "item_SYN000059"

    async def test_audio_and_call_of_one_response_both_land_without_confusion(self):
        sink = FakeSink()
        adapter = _session(sink)
        recorder = RecordingSession()
        strategy = FunctionStrategy(recorder, sink)
        decisions = []
        kinds: list[tuple[str, str | None]] = []
        for raw in fixture_events(FIXTURES / f"{self.FIXTURE}.jsonl"):
            for event in adapter.map_event(_as_relay(raw)):
                kinds.append((event.kind, event.item_id))
                decisions.extend(await strategy.feed(event))

        # The call still dispatches, joined to the one input item of this turn.
        requests = [d for d in decisions if isinstance(d, Request)]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].response_id, self.RESPONSE)
        self.assertEqual(requests[0].call_id, "call_SYN000018")
        self.assertEqual(strategy.refusals, [])

        # The audio of the SAME response reached the audio path under its own item id.
        audio = [(kind, item) for kind, item in kinds if kind == "output.audio"]
        self.assertTrue(audio, "the leaked message produced no audio events")
        self.assertEqual({item for _kind, item in audio}, {self.AUDIO_ITEM})
        self.assertNotEqual(self.AUDIO_ITEM, self.CALL_ITEM)

        # The call item is never mistaken for an audio item, so it can never be truncated.
        record = strategy._responses[self.RESPONSE]
        self.assertEqual(record.audio_items, [self.AUDIO_ITEM])
        self.assertNotIn(self.CALL_ITEM, record.audio_items)

    async def test_the_leaked_audio_is_not_treated_as_the_narration_follow_up(self):
        """It is the model speaking ON the user turn, not the `tool_choice: "none"` reply.

        The distinction matters because the follow-up is a separate response with its own
        id and a `narration` origin; crediting this one as the follow-up would let the loop
        think it had already spoken the result it has not yet fetched.
        """
        sink = FakeSink()
        adapter = _session(sink)
        recorder = RecordingSession()
        strategy = FunctionStrategy(recorder, sink)
        for raw in fixture_events(FIXTURES / f"{self.FIXTURE}.jsonl"):
            for event in adapter.map_event(raw):
                await strategy.feed(event)

        # Exactly one response exists in this slice, and it is the USER turn's.
        self.assertEqual(list(strategy._responses), [self.RESPONSE])
        self.assertEqual(strategy._responses[self.RESPONSE].origin, "user")
        # Nothing here created a narration response; the loop sends that one afterwards.
        self.assertEqual(recorder.created, [])

    async def test_a_speech_started_during_a_mixed_response_truncates_its_audio_item(self):
        """The cut must name the AUDIO item, never the function_call item beside it."""
        sink = FakeSink()
        adapter = _session(sink)
        recorder = RecordingSession()
        strategy = FunctionStrategy(recorder, sink)
        for raw in fixture_events(FIXTURES / f"{self.FIXTURE}.jsonl"):
            if raw.get("type") == "response.done":
                break               # interrupt while the response is still open
            for event in adapter.map_event(raw):
                await strategy.feed(event)
        sink.rendered[(self.RESPONSE, self.AUDIO_ITEM)] = 640
        for event in adapter.map_event({"type": "input_audio_buffer.speech_started",
                                        "item_id": "item_LATER"}):
            await strategy.feed(event)
        self.assertEqual(recorder.cancelled, [self.RESPONSE])
        self.assertEqual(recorder.truncates, [(self.AUDIO_ITEM, 640)])


class B1CancelledAudioNeverReachesTheSpeaker(unittest.IsolatedAsyncioTestCase):
    """B1 — after `speech_started`, no delta of the cancelled response id is written."""

    async def test_b1_r6_deltas_of_the_cancelled_response_never_reach_the_stream(self):
        """The recorded barge-in, replayed against a REAL `PlaybackSink` and a fake device.

        The fixture's audio deltas were stripped to byte counts at capture, so each one is
        re-inflated to that many bytes of silence before it is replayed. The ids, the order
        and the interruption point are the wire's own; only the sample values are ours.
        """
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        # R6: speech_started (seq 68) lands mid-audio of this response, which the server
        # then reports `cancelled` (seq 74). Its item is the one that was speaking.
        cancelled_response = "resp_SYN000028"
        cancelled_item = "item_SYN000084"

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        recorder = RecordingSession()
        adapter = _session(sink)
        strategy = FunctionStrategy(recorder, sink)
        try:
            interrupted = False
            before_cut = 0            # bytes of the cancelled response offered pre-barge-in
            after_cut: list[bytes] = []   # the exact payloads offered after it
            for raw in fixture_events(FIXTURES / "voice-rt-R6.jsonl"):
                kind = raw.get("type")
                if kind in ("response.audio.delta", "response.output_audio.delta"):
                    # Re-inflate the stripped delta into a payload unique to this event,
                    # so a byte found on the device can be traced to the delta it came from.
                    nbytes = int(raw.get("delta") or 0)
                    pcm = _marked_pcm(raw.get("event_id", ""), nbytes)
                    raw = {**raw, "delta": base64.b64encode(pcm).decode("ascii")}
                    if raw.get("response_id") == cancelled_response:
                        if interrupted:
                            after_cut.append(pcm)
                        else:
                            before_cut += nbytes
                if kind == "input_audio_buffer.speech_started" \
                        and raw.get("item_id") == "item_SYN000085":
                    # The writer is a thread: let what the wire ALREADY delivered reach the
                    # device before the cut lands, the way it does live. Draining after the
                    # cut instead would credit zero rendered frames and make the assertion
                    # below pass for the wrong reason.
                    sink.drain()
                    interrupted = True
                for event in adapter.map_event(raw):
                    await strategy.feed(event)
            sink.drain()
            written = b"".join(stream.writes)
        finally:
            sink.close()

        self.assertTrue(interrupted, "R6 must contain the mid-answer barge-in")
        self.assertTrue(after_cut,
                        "R6 must carry deltas of the cancelled response after the cut")
        self.assertEqual(recorder.cancelled, [cancelled_response])
        self.assertGreater(before_cut, 0, "the answer was audible before the cut")
        self.assertGreater(sink.rendered_ms(cancelled_response, cancelled_item) or 0, 0)
        # THE ASSERTION B1 EXISTS FOR: not one byte of a post-cut delta reached the device.
        for pcm in after_cut:
            self.assertNotIn(pcm, written,
                             "a delta of the cancelled response reached the speaker")

    async def test_b1_late_deltas_of_a_cancelled_id_are_dropped_by_the_epoch(self):
        """The unit half of B1: the id is what identifies the audio, not its arrival time."""
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.play("resp_A", "item_A", b"\x01\x02" * 240)
            sink.drain()
            sink.cancel("resp_A")
            # Everything after the cut, however it arrives, is not the operator's present.
            sink.play("resp_A", "item_A", b"\x03\x04" * 240)
            sink.play("resp_B", "item_B", b"\x05\x06" * 240)
            sink.drain()
        finally:
            sink.close()
        self.assertEqual(len(stream.writes), 2)
        self.assertEqual(stream.writes[0], b"\x01\x02" * 240)
        self.assertEqual(stream.writes[1], b"\x05\x06" * 240)


class RenameTableCoversTheFixtures(unittest.TestCase):
    """Every pre-GA name that actually appears in the recordings must be mapped."""

    def test_rename_table_maps_every_pre_ga_name_in_the_fixtures(self):
        pre_ga = set()
        for name in ("voice-rt-R0", "voice-rt-R2", "voice-rt-R6"):
            for raw in fixture_events(FIXTURES / f"{name}.jsonl"):
                kind = raw.get("type", "")
                if kind.startswith("response.audio"):
                    pre_ga.add(kind)
        self.assertTrue(pre_ga, "the fixtures must carry pre-GA audio event names")
        unmapped = sorted(k for k in pre_ga if k not in RENAME)
        self.assertEqual(unmapped, [], f"pre-GA names with no GA mapping: {unmapped}")
        for pre, ga in RENAME.items():
            self.assertTrue(ga.startswith("response.output_audio"))

    def test_mapper_lifts_pre_ga_audio_delta_to_the_ga_kind(self):
        adapter = _session()
        events = adapter.map_event({"type": "response.audio.delta", "response_id": "r",
                                    "item_id": "i", "delta": 19200})
        self.assertEqual([e.kind for e in events], ["output.audio"])
        self.assertEqual(events[0].payload["frames"], 9600)


class SessionWiring(unittest.IsolatedAsyncioTestCase):
    """The URL, headers and session.update the two providers actually get."""

    def test_voice_live_uri_uses_the_cognitiveservices_alias_and_api_version(self):
        adapter = _session()
        self.assertEqual(
            adapter.uri(),
            "wss://x.cognitiveservices.azure.com/voice-live/realtime"
            "?api-version=2026-07-15&model=gpt-realtime-2.1")
        self.assertEqual(adapter.headers(), {"api-key": "k"})

    def test_openai_uri_and_bearer(self):
        adapter = RealtimeSession(provider="openai", model="gpt-realtime",
                                  api_key="sk-x", connect=FakeSocket.connector([]),
                                  open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)
        self.assertEqual(adapter.uri(), "wss://api.openai.com/v1/realtime?model=gpt-realtime")
        self.assertEqual(adapter.headers()["Authorization"], "Bearer sk-x")

    def test_voice_live_refuses_to_guess_an_api_version(self):
        with self.assertRaises(ValueError):
            RealtimeSession(provider="voice_live", model="m", api_key="k",
                            endpoint="https://x.openai.azure.com",
                            connect=FakeSocket.connector([]),
                            open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)

    async def test_session_update_carries_tools_and_passes_extra_through(self):
        socket = FakeSocket([])
        adapter = RealtimeSession(provider="voice_live", model="m", api_key="k",
                                  endpoint="https://x.openai.azure.com",
                                  api_version="2026-07-15",
                                  connect=FakeSocket.connector([], socket=socket),
                                  open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)
        from voice.live.strategy_function import TOOLS
        await adapter.start(SessionConfig(
            instructions="你是语音助手", voice="marin", tools=TOOLS, tool_choice="auto",
            extra={"turn_detection": {"type": "azure_semantic_vad", "silence_duration_ms": 500},
                   "input_audio_echo_cancellation": {"type": "server_echo_cancellation"}}))
        sent = json.loads(socket.sent[0])
        self.assertEqual(sent["type"], "session.update")
        session = sent["session"]
        self.assertEqual(session["instructions"], "你是语音助手")
        self.assertEqual(session["voice"], {"name": "marin", "type": "openai"})
        self.assertEqual(session["tool_choice"], "auto")
        self.assertEqual([t["name"] for t in session["tools"]], ["relay"])
        # extra is the daemon's, passed through untouched.
        self.assertEqual(session["turn_detection"]["silence_duration_ms"], 500)
        self.assertEqual(session["input_audio_echo_cancellation"]["type"],
                         "server_echo_cancellation")


if __name__ == "__main__":
    unittest.main()
