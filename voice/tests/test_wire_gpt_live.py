"""The GPT-Live provider and the provider registry.

Replays are RECORDED wires (`tests/fixtures/voice-gl-*.jsonl`, audio stripped to byte counts):

  * `voice-gl-live-0923.jsonl` — a recorded live `gpt-live-1` session: seven delegations, none
    answered by the test client (the model then repeated "我正在确认"). Timing, fragment
    boundaries and event order are as recorded; the spoken text is synthetic and ids are
    remapped (tests/fixtures/README.md).
  * `voice-gl-S2.jsonl`, `voice-gl-S5.jsonl`, `voice-gl-S6.jsonl` — the 2026-09-19 spike
    scenarios: correction mid-work, two delegations, barge-in.

Rows are asserted BY NAME (the exact span text of each delegation, the exact barge time), so
an empty or truncated fixture fails instead of passing vacuously.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from voice.agent.loop import AgentLoop
from voice.backend import base as backend_base
from voice.live import base as live_base
from voice.live import gpt_live, providers
from voice.live.gpt_live import AUDIO_ITEM, GptLiveSession, GptLiveVoice, fit, utf8_len
from voice.live.port import SessionVoice
from voice.live.strategy_delegation import BLANK_SPAN_NOTE, DelegationStrategy
from voice.tests.core_fakes import FakeBackend, FakeLedger
from voice.tests.wire_fakes import FakeSink, FakeSocket, fixture_events

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
VOICE = Path(__file__).resolve().parents[1]


def _session(sink=None, socket=None) -> GptLiveSession:
    return GptLiveSession(model="gpt-live-1", api_key="k",
                          endpoint="https://res.openai.azure.com/openai/v1",
                          sink=sink, connect=FakeSocket.connector([], socket),
                          open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)


def _replay(name: str, session: GptLiveSession, *, flush: bool = False) -> list[live_base.LiveEvent]:
    out: list[live_base.LiveEvent] = []
    for raw in fixture_events(FIXTURES / name):
        for ev in session.map_event(raw):
            out.append(ev)
            if flush and ev.kind == live_base.KIND_INPUT_SPEECH_STARTED:
                session.flush_playback()
    return out


def _spans(events) -> list[str]:
    return [e.payload["text"] for e in events if e.kind == live_base.KIND_DELEGATION_CREATED]


class RecordingLiveSession(GptLiveSession):
    """A GptLiveSession whose appends are recorded instead of sent."""

    def __init__(self, sink=None) -> None:
        super().__init__(model="gpt-live-1", api_key="k", endpoint="https://res.openai.azure.com",
                         sink=sink, connect=FakeSocket.connector([]),
                         open_bound_s=1.0, close_bound_s=1.0, reconnect_bound_s=1.0)
        self.appends: list[tuple[str, str | None, str]] = []

    async def append(self, kind, delegation_id, content):
        self.appends.append((kind, delegation_id, content))


# ------------------------------------------------------------------ the registry

class Registry(unittest.TestCase):

    def test_three_providers_parallel(self):
        self.assertEqual(providers.names(), ["gpt_live", "openai", "voice_live"])
        self.assertEqual(providers.DEFAULT_PROVIDER, "voice_live")

    def test_each_profile_names_its_own_credentials(self):
        self.assertEqual(providers.get("voice_live").required_env,
                         ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"))
        self.assertEqual(providers.get("openai").required_env, ("OPENAI_API_KEY",))
        # gpt-live-1 lives on a different resource: never the Voice Live key.
        self.assertEqual(providers.get("gpt_live").required_env,
                         ("VOICE_GPT_LIVE_ENDPOINT", "VOICE_GPT_LIVE_API_KEY"))

    def test_every_profile_prompt_exists(self):
        for name in providers.names():
            with self.subTest(provider=name):
                self.assertTrue((VOICE / "prompts" / "providers" / providers.get(name).prompt).is_file())

    def test_capabilities_are_declared_per_provider(self):
        caps = {n: providers.get(n).capabilities for n in providers.names()}
        self.assertTrue(caps["voice_live"].response_identity)
        self.assertTrue(caps["voice_live"].server_echo_cancellation)
        self.assertFalse(caps["openai"].server_echo_cancellation)
        self.assertFalse(caps["gpt_live"].response_identity)
        self.assertFalse(caps["gpt_live"].server_echo_cancellation)
        self.assertFalse(caps["gpt_live"].function_tools)

    def test_the_gpt_live_profile_builds_its_own_parts(self):
        values = {"VOICE_GPT_LIVE_ENDPOINT": "https://canada.openai.azure.com",
                  "VOICE_GPT_LIVE_API_KEY": "k"}
        with mock.patch.object(providers.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)):
            profile = providers.get("gpt_live")
            session = profile.build_session(None, FakeSocket.connector([]),
                                            providers.Bounds(1.0, 2.0, 3.0))
        self.assertIsInstance(session, GptLiveSession)
        self.assertEqual(session.uri(), "wss://canada.openai.azure.com/openai/v1/live/sessions")
        self.assertEqual(session._model, "gpt-live-1")
        self.assertEqual((session.open_bound_s, session.close_bound_s, session.reconnect_bound_s),
                         (1.0, 2.0, 3.0))
        self.assertIsInstance(profile.build_strategy(session, None), DelegationStrategy)
        self.assertIsInstance(profile.build_voice(session), GptLiveVoice)
        self.assertEqual(profile.tools(), [])
        self.assertEqual(profile.session_extra(), {})

    def test_the_realtime_profiles_keep_the_realtime_parts(self):
        from voice.live.realtime import RealtimeSession
        from voice.live.strategy_function import TOOLS, FunctionStrategy

        for name in ("voice_live", "openai"):
            with self.subTest(provider=name):
                profile = providers.get(name)
                values = {"AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com",
                          "VOICE_LIVE_API_VERSION": "2026-07-15"}
                with mock.patch.object(providers.voice_config, "cfg",
                                       side_effect=lambda n, d="": values.get(n, d)):
                    session = profile.build_session(None, FakeSocket.connector([]),
                                                    providers.Bounds(1.0, 2.0, 3.0))
                self.assertIsInstance(session, RealtimeSession)
                self.assertIsInstance(profile.build_strategy(session, None), FunctionStrategy)
                voice = profile.build_voice(session)
                self.assertIsInstance(voice, SessionVoice)
                self.assertIs(voice.capabilities, profile.capabilities)
                self.assertIs(profile.tools(), TOOLS)

    def test_the_daemon_names_no_provider(self):
        """The daemon asks the registry. A provider name or a provider class in its source
        means a second place to edit when a provider is added."""
        text = (VOICE / "app" / "daemon.py").read_text(encoding="utf-8")
        for needle in ('"voice_live"', '"openai"', '"gpt_live"', "RealtimeSession",
                       "FunctionStrategy", "GptLiveSession", "strategy_function",
                       "REQUIRED_ENV"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

    def test_the_daemon_builds_gpt_live_end_to_end(self):
        import tempfile

        from voice.app import daemon as daemon_mod

        values = {"VOICE_GPT_LIVE_ENDPOINT": "https://canada.openai.azure.com",
                  "VOICE_GPT_LIVE_API_KEY": "secret-never-printed",
                  "VOICE_BACKEND": "process", "VOICE_PROCESS_ARGV": "cat"}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)):
            daemon = daemon_mod.VoiceDaemon(session_id="s", provider="gpt_live",
                                            backend_name="process", state=Path(tmp),
                                            connect=FakeSocket.connector([]),
                                            open_output=lambda: None)
            daemon.build()
            self.addCleanup(daemon.ledger.close)
            self.assertIsInstance(daemon.session, GptLiveSession)
            self.assertIsInstance(daemon.strategy, DelegationStrategy)
            from voice.live.port import RedactedVoice
            self.assertIsInstance(daemon.loop.voice, RedactedVoice)   # one redaction point
            self.assertIsInstance(daemon.loop.voice._inner, GptLiveVoice)
            self.assertTrue(daemon.loop.broker.terminal_only)
            self.assertEqual(daemon.tools, [])
            self.assertEqual(daemon._session_extra(), {})
            self.assertIn("delegate", daemon._instructions())

    def test_gpt_live_credentials_are_checked_by_name_only(self):
        from voice.app import daemon as daemon_mod

        with mock.patch.object(daemon_mod.voice_config, "cfg", return_value=""):
            with self.assertRaises(daemon_mod.MissingCredentials) as caught:
                daemon_mod.check_credentials(
                    "gpt_live", {"VOICE_GPT_LIVE_ENDPOINT": "https://canada.openai.azure.com"})
        self.assertIn("VOICE_GPT_LIVE_API_KEY", str(caught.exception))
        self.assertNotIn("canada", str(caught.exception))

    def test_every_voice_port_method_the_loop_uses_exists_on_both_ports(self):
        from voice.agent import loop as loop_mod
        from voice.live import strategy_delegation

        used = set(re.findall(r"self\.voice\.([A-Za-z_]+)", inspect.getsource(loop_mod)))
        self.assertGreaterEqual(used, {"context", "announce", "challenge", "receipt", "result",
                                       "cut", "capabilities"})
        for cls in (SessionVoice, GptLiveVoice):
            with self.subTest(port=cls.__name__):
                self.assertEqual(sorted(m for m in used if not hasattr(cls, m)), [])
        used = set(re.findall(r"self\._session\.([A-Za-z_]+)",
                              inspect.getsource(strategy_delegation)))
        self.assertEqual(sorted(m for m in used if not hasattr(GptLiveSession, m)), [])


# ------------------------------------------------------------------ the wire

class Wire(unittest.IsolatedAsyncioTestCase):

    async def test_start_sends_a_strict_session_start(self):
        sock = FakeSocket([])
        s = _session(socket=sock)
        await s.start(live_base.SessionConfig(instructions="hi", voice="marin",
                                              tools=[{"type": "function", "name": "relay"}]))
        self.assertEqual(sock.uri, "wss://res.openai.azure.com/openai/v1/live/sessions")
        self.assertEqual(list(sock.headers), ["api-key"])
        sent = json.loads(sock.sent[0])
        self.assertEqual(sent["type"], "session.start")
        # Strict schema: no tools, no tool_choice, no modalities — only documented fields.
        self.assertEqual(sorted(sent["session"]), ["audio", "delegation", "instructions", "model"])
        self.assertEqual(sent["session"]["delegation"], {"type": "client"})
        self.assertEqual(sent["session"]["audio"], {"output": {"voice": "marin"}})

    async def test_audio_goes_up_as_input_audio_append(self):
        sock = FakeSocket([])
        s = _session(socket=sock)
        await s.send_audio(b"\x01\x00")          # before the socket: dropped, never raised
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        await s.send_audio(b"")                  # empty is a protocol error: never sent
        await s.send_audio(b"\x01\x00\x02\x00")
        kinds = [json.loads(m)["type"] for m in sock.sent]
        self.assertEqual(kinds, ["session.start", "session.input_audio.append"])
        self.assertEqual(base64.b64decode(json.loads(sock.sent[1])["audio"]), b"\x01\x00\x02\x00")

    async def test_append_binds_the_delegation_and_fits_the_budget(self):
        sock = FakeSocket([])
        s = _session(socket=sock)
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        await s.append("thinking", None, "上下文")
        await s.append("commentary", "item_D1", "字" * 3000)
        general, bound = (json.loads(m) for m in sock.sent[1:])
        self.assertEqual(general, {"type": "session.thinking.append", "delegation_id": None,
                                   "content": "上下文"})
        self.assertEqual(bound["type"], "session.commentary.append")
        self.assertEqual(bound["delegation_id"], "item_D1")
        self.assertLessEqual(utf8_len(bound["content"]), 500)
        self.assertTrue(bound["content"].endswith(gpt_live.TRUNCATED_MARK))
        with self.assertRaises(ValueError):
            await s.append("speak", None, "x")

    async def test_close_asks_for_a_graceful_end_first(self):
        sock = FakeSocket([])
        s = _session(socket=sock)
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        await s.close()
        self.assertEqual(json.loads(sock.sent[-1]), {"type": "session.close"})
        self.assertTrue(sock.closed)

    async def test_gpt_live_is_a_live_session_without_realtime_controls(self):
        """The base seam is start/send_audio/events/close; the realtime verbs are an OPTIONAL
        protocol GPT-Live does not implement — no raise-only stubs."""
        from voice.live.realtime import RealtimeSession

        s = _session()
        for verb in ("start", "send_audio", "events", "close"):
            self.assertTrue(callable(getattr(s, verb)), verb)
        self.assertNotIsInstance(s, live_base.RealtimeControls)
        self.assertTrue(issubclass(RealtimeSession, live_base.RealtimeControls))
        for verb in ("create_response", "add_item", "call_output", "cancel_response",
                     "truncate"):
            self.assertFalse(hasattr(s, verb), verb)

    async def test_the_loop_refuses_a_bare_session_without_realtime_controls(self):
        with self.assertRaises(TypeError):
            AgentLoop(session=_session(), strategy=DelegationStrategy(_session()),
                      backend=FakeBackend(), ledger=FakeLedger(), sink=FakeSink())

    async def test_nothing_is_sent_after_the_server_closed(self):
        """Review r1 #7: a closed session must not keep streaming the microphone."""
        sock = FakeSocket([])
        s = _session(socket=sock)
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        s.map_event({"type": "session.closed", "reason": "expired", "usage": {"seconds": 1}})
        await s.send_audio(b"\x01\x00")
        await s.append("commentary", "item_X", "late")
        self.assertEqual([json.loads(m)["type"] for m in sock.sent], ["session.start"])

    async def test_a_stalled_graceful_close_is_bounded_and_still_closes(self):
        """Review r1 #6: the close frame's send is inside the injected bound."""
        class Stalls(FakeSocket):
            async def send(self, payload):
                if "session.close" in payload:
                    await asyncio.Event().wait()
                self.sent.append(payload)
        sock = Stalls([])
        s = GptLiveSession(model="gpt-live-1", api_key="k", endpoint="https://r.openai.azure.com",
                           connect=FakeSocket.connector([], sock), open_bound_s=1.0,
                           close_bound_s=0.05, reconnect_bound_s=1.0)
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        await asyncio.wait_for(s.close(), 1.0)
        self.assertTrue(sock.closed)

    async def test_a_cancelled_close_still_closes_the_transport(self):
        """Review r2 #4: cancelling close() mid-send must not strand the socket."""
        class Stalls(FakeSocket):
            async def send(self, payload):
                if "session.close" in payload:
                    await asyncio.Event().wait()
                self.sent.append(payload)
        sock = Stalls([])
        s = GptLiveSession(model="gpt-live-1", api_key="k", endpoint="https://r.openai.azure.com",
                           connect=FakeSocket.connector([], sock), open_bound_s=1.0,
                           close_bound_s=5.0, reconnect_bound_s=1.0)
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        task = asyncio.ensure_future(s.close())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(sock.closed)

    async def test_events_end_on_session_closed(self):
        sock = FakeSocket([{"type": "session.started", "session": {"id": "live_1"}},
                           {"type": "session.closed", "reason": "close_requested",
                            "usage": {"seconds": 85.8}}])
        s = _session(socket=sock)
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        kinds = [e.kind async for e in s.events()]
        self.assertEqual(kinds, [live_base.KIND_SESSION_STARTED, live_base.KIND_CLOSED])
        self.assertEqual(s.usage, {"seconds": 85.8})


class Mapper(unittest.TestCase):

    def test_audio_rides_in_delta_not_audio(self):
        """2026-09-23: the first test client read `audio` and played silence for 118 s."""
        sink = FakeSink()
        s = _session(sink=sink)
        pcm = b"\x10\x00" * 240
        s.map_event({"type": "session.output_audio.delta",
                     "delta": base64.b64encode(pcm).decode(), "start_ms": 0, "end_ms": 10})
        s.map_event({"type": "session.output_audio.delta",
                     "audio": base64.b64encode(pcm).decode()})
        self.assertEqual(sink.plays, [("gpt-live:0:0", AUDIO_ITEM, pcm)])

    def test_the_live_session_partitions_seven_delegations_exactly(self):
        s = _session()
        events = _replay("voice-gl-live-0923.jsonl", s)
        self.assertEqual(_spans(events), [
            "你现在能听见我吗……帮我看一下今天的测试结果,顺便说一下你用的是哪个模型",
            "喂,测试结果,哪个模型",
            "你现在能听到我说话吗?为什么还没有回复我",
            "那我先说一下需求",
            "你先告诉我你是什么模型吧",
            "那你帮我看一下,就是... 呃...构建为什么失败,是哪个步骤失败",
            "就是你看到的是哪个步骤失败",
        ])
        # Exactly once: every input fragment before the last cursor is in one span, and the
        # ones after it are still pending — none dropped, none duplicated.
        fragments = [raw for raw in fixture_events(FIXTURES / "voice-gl-live-0923.jsonl")
                     if raw["type"] == "session.input_transcript.delta"]
        claimed = sum(e.payload["fragments"] for e in events
                      if e.kind == live_base.KIND_DELEGATION_CREATED)
        self.assertEqual(claimed + len(s._pending), len(fragments))
        self.assertEqual(s.late_fragments, 0)

    def test_each_delegation_is_a_joined_turn_named_by_its_own_id(self):
        s = _session()
        events = _replay("voice-gl-S5.jsonl", s)
        # The delegation's turn — partial per-word activity events carry no input item.
        first = [e for e in events if e.kind in (live_base.KIND_INPUT_COMMITTED,
                                                  live_base.KIND_INPUT_TRANSCRIPT,
                                                  live_base.KIND_RESPONSE_CREATED,
                                                  live_base.KIND_DELEGATION_CREATED)
                 and not (e.payload or {}).get("partial")][:4]
        did = first[3].delegation_id
        self.assertTrue(did.startswith("item_"), did)
        self.assertEqual([e.kind for e in first], [
            live_base.KIND_INPUT_COMMITTED, live_base.KIND_INPUT_TRANSCRIPT,
            live_base.KIND_RESPONSE_CREATED, live_base.KIND_DELEGATION_CREATED])
        self.assertEqual({first[0].input_item_id, first[1].input_item_id, first[3].input_item_id},
                         {f"{did}:span"})
        self.assertEqual(first[2].response_id, did)
        self.assertEqual(first[2].payload["origin"], "user")
        self.assertEqual([e.offset_ms for e in events
                          if e.kind == live_base.KIND_DELEGATION_CREATED], [3200, 5400])
        self.assertEqual(_spans(events), ["帮我把测试跑一遍", "是问变法,并也跑一下"])

    def test_a_correction_spoken_after_the_model_answered_still_lands_in_its_span(self):
        events = _replay("voice-gl-S2.jsonl", _session())
        (span,) = _spans(events)
        self.assertIn("算了", span)
        self.assertIn("先看", span)

    def test_a_late_fragment_is_counted_never_moved_to_the_next_delegation(self):
        s = _session()
        s.map_event({"type": "session.input_transcript.delta", "delta": "跑测试",
                     "start_ms": 600, "end_ms": 800})
        s.map_event({"type": "session.delegation.created", "offset_ms": 1000,
                     "delegation": {"id": "item_A", "type": "delegation", "target": "client"}})
        s.map_event({"type": "session.input_transcript.delta", "delta": "吧",
                     "start_ms": 800, "end_ms": 1000})         # before cursor 1000: late
        s.map_event({"type": "session.input_transcript.delta", "delta": "再看日志",
                     "start_ms": 1400, "end_ms": 1600})
        events = s.map_event({"type": "session.delegation.created", "offset_ms": 1800,
                              "delegation": {"id": "item_B", "type": "delegation",
                                             "target": "client"}})
        self.assertEqual(s.late_fragments, 1)
        self.assertEqual(_spans(events), ["再看日志"])

    def test_a_server_side_delegation_is_not_ours(self):
        events = _session().map_event({"type": "session.delegation.created", "offset_ms": 1,
                                       "delegation": {"id": "item_R", "target": "responses"}})
        self.assertEqual([e.kind for e in events], [live_base.KIND_ERROR])

    def test_the_spike_tail_never_cuts_the_answer(self):
        """S6: 「你好,我想问一下」 (600–2200 ms) is the turn the model answers at 2200 ms; its
        tail must not cut 「嗨,你说。」, and 「嗯」 at 5000 ms began after that voice ended."""
        sink = FakeSink()
        s = _session(sink=sink)
        events = _replay("voice-gl-S6.jsonl", s, flush=True)
        self.assertEqual([e.start_ms for e in events
                          if e.kind == live_base.KIND_INPUT_SPEECH_STARTED], [])
        self.assertEqual(sink.cancelled, [])

    def _voice(self, s, start, end, *, audio=True):
        kind = "session.output_audio.delta" if audio else "session.output_transcript.delta"
        return s.map_event({"type": kind, "delta": 4800 if audio else "好",
                            "start_ms": start, "end_ms": end})

    def _heard(self, s, start):
        """The barge-in events one operator word produces (its partial-transcript activity
        event is asserted separately)."""
        return [(e.kind, e.start_ms) for e in s.map_event(
            {"type": "session.input_transcript.delta", "delta": "停",
             "start_ms": start, "end_ms": start + 200})
            if e.kind == live_base.KIND_INPUT_SPEECH_STARTED]

    def test_a_barge_and_its_word_keep_event_seq_monotonic(self):
        s = _session(sink=FakeSink())
        self._voice(s, 0, 1000)
        events = s.map_event({"type": "session.input_transcript.delta", "delta": "停",
                              "start_ms": 500, "end_ms": 700})
        self.assertEqual([e.kind for e in events],
                         [live_base.KIND_INPUT_SPEECH_STARTED, live_base.KIND_INPUT_TRANSCRIPT])
        self.assertLess(events[0].seq, events[1].seq)

    def test_every_word_is_presence_for_the_idle_close(self):
        """Closing review 1: a word that interrupts nothing still emits an activity event,
        which the loop counts as presence — and nothing joins or dispatches on it."""
        s = _session()
        events = s.map_event({"type": "session.input_transcript.delta", "delta": "我想",
                              "start_ms": 100, "end_ms": 300})
        self.assertEqual([(e.kind, e.input_item_id, e.payload["complete"]) for e in events],
                         [(live_base.KIND_INPUT_TRANSCRIPT, None, False)])

    def test_words_inside_the_voiced_segment_interrupt_and_nothing_else_does(self):
        s = _session(sink=FakeSink())
        self._voice(s, 2000, 2100)
        self._voice(s, 2100, 2200)
        self.assertEqual(self._heard(s, 1800), [], "the tail of the answered turn")
        self.assertEqual(self._heard(s, 2400), [], "after the voice ended")
        self.assertEqual(self._heard(s, 2100),
                         [(live_base.KIND_INPUT_SPEECH_STARTED, 2100)])

    def test_a_new_answer_after_a_pause_opens_a_new_segment(self):
        """Review r1 #3: old answer 0–500, question 1000, new answer from 1500. A delayed
        transcript of the question (1000) must not cut the new answer."""
        s = _session(sink=FakeSink())
        self._voice(s, 0, 500)
        self._voice(s, 1500, 1600)
        self.assertEqual(self._heard(s, 1000), [])

    def test_late_audio_of_a_cut_answer_is_dropped_not_replayed_under_a_new_key(self):
        """Review r1 #4: after the cut, audio contiguous with the cut segment is the old
        answer arriving late; the next range after a gap is new speech."""
        sink = FakeSink()
        s = _session(sink=sink)
        pcm = base64.b64encode(b"\x01\x00" * 240).decode()

        def audio(start, end):
            s.map_event({"type": "session.output_audio.delta", "delta": pcm,
                         "start_ms": start, "end_ms": end})
        audio(0, 100)
        self._heard(s, 50)
        self.assertEqual(s.flush_playback(), 0)
        audio(100, 200)                       # late, contiguous with the cut answer
        audio(900, 1000)                      # after a gap: the new answer
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0", "gpt-live:0:1"])
        self.assertEqual(s.dropped_late_audio, 1)
        self.assertEqual(sink.cancelled, ["gpt-live:0:0"])

    def _pcm_audio(self, s, start, end):
        return s.map_event({"type": "session.output_audio.delta",
                            "delta": base64.b64encode(b"\x01\x00" * 240).decode(),
                            "start_ms": start, "end_ms": end})

    def test_a_gap_after_the_cut_does_not_readmit_the_old_answer(self):
        """Review r2 #1a: cut 0–100 by words 50–250; old audio at 150–200 crosses a gap but
        still overlaps the operator's words — stale."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 0, 100)
        s.map_event({"type": "session.input_transcript.delta", "delta": "停",
                     "start_ms": 50, "end_ms": 250})
        s.flush_playback()
        self._pcm_audio(s, 150, 200)
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0"])
        self._pcm_audio(s, 400, 500)          # after the operator's words: the new answer
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0", "gpt-live:0:1"])

    def test_nothing_older_than_the_admitted_answer_plays_after_it(self):
        """Review r2 #1b: the new answer's transcript arrives first (900–1000); old audio at
        100–200 arriving after it is stale."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 0, 100)
        s.map_event({"type": "session.input_transcript.delta", "delta": "停",
                     "start_ms": 50, "end_ms": 250})
        s.flush_playback()
        s.map_event({"type": "session.output_transcript.delta", "delta": "好",
                     "start_ms": 900, "end_ms": 1000})
        self._pcm_audio(s, 100, 200)          # under the cut floor
        self._pcm_audio(s, 300, 400)          # past the words, but older than the admitted answer
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0"])
        self.assertEqual(s.dropped_late_audio, 2)

    def test_an_overlap_revealed_by_later_output_still_interrupts_once(self):
        """Review r2 #2: output 0–100, words 150–250, then output 100–300. The overlap is
        known only when the later output arrives; it must still cut, exactly once, and that
        output's audio must not play under the cut epoch."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 0, 100)
        self.assertEqual(self._heard(s, 150), [])
        events = self._pcm_audio(s, 100, 300)
        self.assertEqual([e.kind for e in events][0], live_base.KIND_INPUT_SPEECH_STARTED)
        self.assertEqual(len(sink.plays), 1, "the overlapping chunk is withheld")
        s.flush_playback()
        self.assertEqual(self._pcm_audio(s, 300, 400), [], "contiguous tail of the cut answer")
        self.assertEqual(sink.cancelled, ["gpt-live:0:0"])

    def test_known_cancelled_speech_past_the_words_stays_dead(self):
        """Review r3 #1: the transcript already told us the answer runs 0–1000; after the cut
        (words 50–250) its audio at 300–400 is still the cancelled answer."""
        sink = FakeSink()
        s = _session(sink=sink)
        s.map_event({"type": "session.output_transcript.delta", "delta": "长回答",
                     "start_ms": 0, "end_ms": 1000})
        self._pcm_audio(s, 0, 100)
        s.map_event({"type": "session.input_transcript.delta", "delta": "停",
                     "start_ms": 50, "end_ms": 250})
        s.flush_playback()
        self._pcm_audio(s, 300, 400)
        self._pcm_audio(s, 1000, 1100)        # contiguous continuation: still dead
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0"])
        self._pcm_audio(s, 1500, 1600)        # after a gap, clear of it: the next answer
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0", "gpt-live:0:1"])

    def test_output_advancing_elsewhere_never_erases_pending_words(self):
        """Review r3 #2: audio 0–100, words 150–250, transcript 400–600, audio 100–300 —
        the word at 150 is covered once the later audio arrives, and must still cut."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 0, 100)
        self.assertEqual(self._heard(s, 150), [])
        s.map_event({"type": "session.output_transcript.delta", "delta": "嗯",
                     "start_ms": 400, "end_ms": 600})
        events = self._pcm_audio(s, 100, 300)
        self.assertEqual(events[0].kind, live_base.KIND_INPUT_SPEECH_STARTED)
        self.assertEqual(len(sink.plays), 1)

    def test_a_late_fragment_still_counts_as_talking_over_the_model(self):
        """Review r3 #3: a fragment past its delegation's cursor loses its TEXT, not its
        timing — words at 100 over audio 0–1000 are an interruption."""
        s = _session(sink=FakeSink())
        s.map_event({"type": "session.output_audio.delta", "delta": 4800,
                     "start_ms": 0, "end_ms": 1000})
        s.map_event({"type": "session.delegation.created", "offset_ms": 500,
                     "delegation": {"id": "item_Z", "target": "client"}})
        self.assertEqual(self._heard(s, 100), [(live_base.KIND_INPUT_SPEECH_STARTED, 100)])
        self.assertEqual(s.late_fragments, 1)

    def test_cancelled_speech_extends_through_the_floor(self):
        """Review r4 #1: transcript 0–300, audio 0–100, words 50–250 (cut), audio 200–400
        (below the floor, but it extends the cut answer), audio 400–500 — still that answer."""
        sink = FakeSink()
        s = _session(sink=sink)
        s.map_event({"type": "session.output_transcript.delta", "delta": "回答",
                     "start_ms": 0, "end_ms": 300})
        self._pcm_audio(s, 0, 100)
        s.map_event({"type": "session.input_transcript.delta", "delta": "停",
                     "start_ms": 50, "end_ms": 250})
        s.flush_playback()
        self._pcm_audio(s, 200, 400)
        self._pcm_audio(s, 400, 500)
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0"])

    def test_a_later_answer_announced_before_the_cut_survives_it(self):
        """Review r4 #2: audio 0–100, words 150–250, transcript 400–600 (a separate answer
        after the words), delayed audio 100–300 cuts; audio 400–600 must still play."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 0, 100)
        self._heard(s, 150)
        s.map_event({"type": "session.output_transcript.delta", "delta": "新回答",
                     "start_ms": 400, "end_ms": 600})
        events = self._pcm_audio(s, 100, 300)
        self.assertEqual(events[0].kind, live_base.KIND_INPUT_SPEECH_STARTED)
        s.flush_playback()
        self._pcm_audio(s, 400, 600)
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0", "gpt-live:0:1"])

    def test_a_live_segment_that_comes_to_touch_cancelled_speech_dies_with_it(self):
        """Review r5 #1: transcript 0–300, audio 0–100, words 50–250 (cut), transcript
        400–600, audio 200–450 bridges the two — so 400–600 was the cancelled answer too."""
        sink = FakeSink()
        s = _session(sink=sink)
        s.map_event({"type": "session.output_transcript.delta", "delta": "回答",
                     "start_ms": 0, "end_ms": 300})
        self._pcm_audio(s, 0, 100)
        s.map_event({"type": "session.input_transcript.delta", "delta": "停",
                     "start_ms": 50, "end_ms": 250})
        s.flush_playback()
        s.map_event({"type": "session.output_transcript.delta", "delta": "续",
                     "start_ms": 400, "end_ms": 600})
        self._pcm_audio(s, 450, 500)          # queued under 400–600's own key
        self._pcm_audio(s, 200, 450)          # the bridge: 400–600 was the cut answer
        self._pcm_audio(s, 500, 600)
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0", "gpt-live:0:1"])
        self.assertEqual(sink.cancelled, ["gpt-live:0:0", "gpt-live:0:1"],
                         "the queued audio of the absorbed segment is cancelled too")
        self.assertEqual(s.live_keys(), [])

    def test_a_later_answer_already_queued_keeps_its_audio_through_the_cut(self):
        """Review r5 #2: audio 0–100, words 150–250, audio 400–600 QUEUED, then a delayed
        transcript 100–300 reveals the overlap. The cut cancels 0–300's key only."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 0, 100)
        self._heard(s, 150)
        self._pcm_audio(s, 400, 600)
        events = s.map_event({"type": "session.output_transcript.delta", "delta": "嗯",
                              "start_ms": 100, "end_ms": 300})
        self.assertEqual(events[0].kind, live_base.KIND_INPUT_SPEECH_STARTED)
        s.flush_playback()
        self.assertEqual([k for k, _i, _p in sink.plays], ["gpt-live:0:0", "gpt-live:0:1"])
        self.assertEqual(sink.cancelled, ["gpt-live:0:0"])
        self.assertEqual(s.live_keys(), ["gpt-live:0:1"])

    def test_a_bridge_kills_queued_audio_whatever_order_it_arrived_in(self):
        """Review r6 #1: audio 400–600 QUEUED first, transcript 0–300, words 50–250 (cut),
        then transcript 200–450 bridges them: 400–600's queued audio must go."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 400, 600)
        s.map_event({"type": "session.output_transcript.delta", "delta": "前",
                     "start_ms": 0, "end_ms": 300})
        s.map_event({"type": "session.input_transcript.delta", "delta": "停",
                     "start_ms": 50, "end_ms": 250})
        s.flush_playback()
        s.map_event({"type": "session.output_transcript.delta", "delta": "桥",
                     "start_ms": 200, "end_ms": 450})
        self.assertIn("gpt-live:0:0", sink.cancelled, "the queued later audio is cancelled")
        self.assertEqual(s.live_keys(), [])

    def test_a_survivor_of_one_cut_can_still_be_talked_over(self):
        """Review r6 #2: after a cut that spared 400–600, words at 450 interrupt it."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._pcm_audio(s, 0, 100)
        self._heard(s, 150)
        self._pcm_audio(s, 400, 600)
        s.map_event({"type": "session.output_transcript.delta", "delta": "嗯",
                     "start_ms": 100, "end_ms": 300})
        s.flush_playback()
        self.assertEqual(self._heard(s, 450), [(live_base.KIND_INPUT_SPEECH_STARTED, 450)])
        s.flush_playback()
        self.assertEqual(sink.cancelled, ["gpt-live:0:0", "gpt-live:0:1"])
        self.assertEqual(self._pcm_audio(s, 600, 700), [], "its continuation is dead too")

    def test_the_recorded_answer_is_never_cut_by_the_question_it_answers(self):
        """Review r7 #1, on the RECORDED wire: 「...話嗎」 and 「听得到」 both start at 33600 ms.
        With synthesized PCM on every recorded audio delta, nothing is cut and all plays."""
        sink = FakeSink()
        s = _session(sink=sink)
        barges = []
        for raw in fixture_events(FIXTURES / "voice-gl-live-0923.jsonl"):
            if raw["type"] == "session.output_transcript.delta" and raw.get("start_ms") == 33600:
                s.map_event({"type": "session.output_audio.delta",
                             "delta": base64.b64encode(b"\x01\x00" * 240).decode(),
                             "start_ms": 33600, "end_ms": 33800})
            for ev in s.map_event(raw):
                if ev.kind == live_base.KIND_INPUT_SPEECH_STARTED:
                    barges.append(ev.start_ms)
                    s.flush_playback()
        # The first cut is the operator's real barge-in over the long answer (41800 ms),
        # never the boundary where the question met 「听得到，很清楚。」 (33600–34600 ms).
        self.assertEqual(len(sink.plays), 1)
        self.assertTrue(barges, "the recorded session does contain real barge-ins")
        self.assertGreater(barges[0], 34600)

    def test_one_flush_aborts_the_real_device_once_and_spares_the_survivor(self):
        """Review r8: ten rendered historical segments + a delayed overlap used to abort the
        device once PER segment, letting the writer advance the surviving answer between
        aborts. One flush = one abort, and the survivor keeps playing."""
        from voice.audio.io import PlaybackSink
        from voice.tests.wire_fakes import FakeStream

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        self.addCleanup(sink.close, bound_s=1.0)
        s = _session(sink=sink)
        for n in range(10):                   # ten earlier answers, all rendered
            self._pcm_audio(s, n * 200, n * 200 + 100)
        sink.drain()
        self._heard(s, 1950)
        self._pcm_audio(s, 2200, 2700)        # the separate later answer
        s.map_event({"type": "session.output_transcript.delta", "delta": "嗯",
                     "start_ms": 1900, "end_ms": 2100})
        s.flush_playback()
        self.assertEqual(stream.aborts, 1)
        self.assertEqual(s.live_keys(), ["gpt-live:0:10"])
        self._pcm_audio(s, 2700, 2800)
        sink.drain()
        self.assertTrue(stream.writes, "the survivor still reaches the device")

    def test_a_flush_always_reaches_the_sink(self):
        """Review r1 #5: an empty queue does not mean the device finished playing; the sink
        decides whether to abort (it does when anything of the epoch rendered)."""
        sink = FakeSink()
        s = _session(sink=sink)
        self._voice(s, 100, 200, audio=False)
        s.flush_playback()
        self.assertEqual(sink.cancelled, ["gpt-live:0:0"])
        self.assertEqual(s.live_keys(), [])


# ------------------------------------------------------------------ strategy + loop

class Strategy(unittest.IsolatedAsyncioTestCase):

    async def test_a_delegation_becomes_one_request_carrying_the_words_verbatim(self):
        session = RecordingLiveSession()
        strategy = DelegationStrategy(session)
        decisions = []
        for raw in fixture_events(FIXTURES / "voice-gl-S5.jsonl"):
            for ev in session.map_event(raw):
                decisions.extend(await strategy.feed(ev))
        self.assertEqual([type(d).__name__ for d in decisions], ["Request", "Request"])
        first = decisions[0]
        self.assertEqual(first.transcript, "帮我把测试跑一遍")
        self.assertEqual(first.interpretation, first.transcript)
        self.assertEqual(first.priority, "next")
        self.assertEqual(first.call_id, first.response_id)
        self.assertEqual(first.input_item_id, f"{first.call_id}:span")

    async def test_a_blank_delegation_is_answered_and_fails_the_turn(self):
        session = RecordingLiveSession()
        strategy = DelegationStrategy(session)
        decisions = []
        for ev in session.map_event({"type": "session.delegation.created", "offset_ms": 10,
                                     "delegation": {"id": "item_E", "target": "client"}}):
            decisions.extend(await strategy.feed(ev))
        self.assertEqual(decisions, [live_base.TranscriptFailed(input_item_id="item_E:span",
                                                                generation=0)])
        self.assertEqual(session.appends, [("commentary", "item_E", BLANK_SPAN_NOTE)])


class LoopOverGptLive(unittest.IsolatedAsyncioTestCase):

    def _loop(self, session, backend):
        return AgentLoop(session=session, strategy=DelegationStrategy(session),
                         backend=backend, ledger=FakeLedger(), sink=FakeSink(),
                         voice=GptLiveVoice(session))

    async def test_every_delegation_is_dispatched_and_answered_on_its_own_id(self):
        session = RecordingLiveSession()
        backend = FakeBackend()
        loop = self._loop(session, backend)
        self.assertTrue(loop.broker.terminal_only, "no response identity → terminal-only")
        for raw in fixture_events(FIXTURES / "voice-gl-live-0923.jsonl"):
            for ev in session.map_event(raw):
                await loop.handle_live_event(ev)
        self.assertEqual([s["transcript"] for s in backend.sends][1], "喂,测试结果,哪个模型")
        self.assertEqual(len(backend.sends), 7)
        self.assertEqual(loop.stats.dispatched, 7)
        # The posted receipt is QUIET context bound to the delegation that asked.
        receipts = [a for a in session.appends if a[0] == "thinking"]
        self.assertEqual(len(receipts), 7)
        self.assertTrue(all(did and did.startswith("item_") for _k, did, _t in receipts))

        # The backend answers the second request: spoken, bound to THAT delegation.
        second_call = receipts[1][1]
        tag = backend.sends[1]["tag"]
        await loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_RECEIPT, tag=tag, turn_id="turn-2"))
        await loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_RESULT, turn_id="turn-2", text="今天是 2026-09-23。"))
        kind, did, text = session.appends[-1]
        self.assertEqual((kind, did), ("commentary", second_call))
        self.assertIn("2026-09-23", text)

    async def test_two_delegations_folded_into_one_turn_are_both_answered(self):
        """Review r2 #3: the backend folds D2 into D1's running turn; the one result must
        close BOTH delegations on their own ids, and be spoken once."""
        session = RecordingLiveSession()
        backend = FakeBackend()
        loop = self._loop(session, backend)
        for raw in fixture_events(FIXTURES / "voice-gl-S5.jsonl"):
            for ev in session.map_event(raw):
                await loop.handle_live_event(ev)
        d1, d2 = [a[1] for a in session.appends if a[0] == "thinking"]
        for send in backend.sends:
            await loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_RECEIPT, tag=send["tag"], turn_id="turn-1"))
        await loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_RESULT, turn_id="turn-1", text="测试全绿。"))
        tail = [(k, d) for k, d, _t in session.appends[-2:]]
        self.assertEqual(tail, [("thinking", d1), ("commentary", d2)])
        self.assertEqual([r.state for r in loop.log.requests()], ["resolved", "resolved"])

    async def test_a_word_stirs_the_idle_close_and_dispatches_nothing(self):
        session = RecordingLiveSession()
        loop = self._loop(session, FakeBackend())
        loop.stirred.clear()
        for ev in session.map_event({"type": "session.input_transcript.delta", "delta": "我想",
                                     "start_ms": 100, "end_ms": 300}):
            await loop.handle_live_event(ev)
        self.assertTrue(loop.stirred.is_set())
        self.assertEqual(list(loop.log.requests()), [])

    async def test_progress_and_typed_lines_are_quiet_context(self):
        session = RecordingLiveSession()
        loop = self._loop(session, FakeBackend())
        await loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_PROGRESS, turn_id="t", text="running tests"))
        await loop.handle_observation(backend_base.Observation(
            kind=backend_base.OBS_TYPED, text="git status"))
        self.assertEqual([(k, d) for k, d, _t in session.appends],
                         [("thinking", None), ("thinking", None)])

    async def test_the_goodbye_is_not_ended_by_first_audio_only_by_the_stream(self):
        """Review r1 #2: without response identity nothing says the goodbye is over; audio
        (maybe the previous answer's tail) must not end it. The stream ending does."""
        session = RecordingLiveSession()
        loop = self._loop(session, FakeBackend())
        loop.live_pumping = True
        waiter = asyncio.ensure_future(loop.farewell("stopped"))
        await asyncio.sleep(0)
        self.assertEqual(session.appends[-1][:2], ("commentary", None))
        for ev in session.map_event({"type": "session.output_audio.delta", "delta": 4800}):
            await loop.handle_live_event(ev)
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        loop._live_ended.set()
        await asyncio.wait_for(waiter, 1)

    async def test_the_loop_ends_when_the_live_stream_ends(self):
        """Review r1 #7: the backend stream never ends by itself; the live one ending must
        end `run` so the daemon can tear down."""
        class Forever(FakeBackend):
            def observe(self):
                async def gen():
                    await asyncio.Event().wait()
                    yield None
                return gen()
        sock = FakeSocket([{"type": "session.started", "session": {"id": "l"}},
                           {"type": "session.closed", "reason": "expired", "usage": {}}])
        s = _session(socket=sock)
        await s.start(live_base.SessionConfig(instructions="", voice="", tools=[]))
        loop = AgentLoop(session=s, strategy=DelegationStrategy(s), backend=Forever(),
                         ledger=FakeLedger(), sink=FakeSink(), voice=GptLiveVoice(s))
        await asyncio.wait_for(loop.run(), 1.0)


class Fit(unittest.TestCase):

    def test_short_text_is_untouched_and_long_text_is_marked(self):
        self.assertEqual(fit("好的"), "好的")
        for text in ("word " * 2000, "a " * 700, "😀" * 400, "字" * 400):
            with self.subTest(text=text[:6]):
                cut = fit(text)
                self.assertTrue(cut.endswith(gpt_live.TRUNCATED_MARK))
                # bytes bound tokens for a byte-level BPE: ≤ budget bytes ⇒ ≤ budget tokens
                self.assertLessEqual(utf8_len(cut), gpt_live.APPEND_BYTE_BUDGET)
                cut.encode("utf-8")               # never a split code point


if __name__ == "__main__":
    unittest.main()
