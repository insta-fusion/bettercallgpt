"""The relay, round 2: the four findings of the dual review on #212 and its minors.

Each class pins one finding; each test failed on 95dc09f (the merged relay) and passes on the
fix. The harness is `test_app_relay`'s: the real adapters, loop and daemon over a scripted
socket, a recording sink and a queue-fed backend.
"""
from __future__ import annotations

import asyncio
import unittest

from voice.agent.loop import AgentLoop
from voice.app import daemon as daemon_mod
from voice.backend import base as backend_base
from voice.live import base as live_base
from voice.live.gpt_live import GptLiveSession
from voice.tests.core_fakes import FakeLedger, FakeSession, FakeStrategy, event
from voice.tests.test_app_relay import QueueBackend, RelayHarness, _until
from voice.tests.wire_fakes import FakeStream


# ---------------------------------------------------------------- 1. drop during a rollover

class ADropDuringARolloverDoesNotDeadlock(RelayHarness):
    rollover = "0.0005"

    async def test_the_fresh_session_takes_over_when_the_old_one_drops_mid_rollover(self):
        """The rollover holds the relay lock and waits for quiet (audio still playing); the
        old session drops. It must not wait for a quiet that no longer matters, and the
        reconnect waiting on the lock must not open a third session."""
        first = self.sock
        self.connect.gate = asyncio.Event()
        await _until(lambda: self.connect.attempts == 2)           # the rollover is opening
        self.sink.pending = {"gpt-live:0:0"}                       # ...and audio plays on
        self.connect.gate.set()
        await _until(lambda: len(self.connect.sockets) == 2)
        await asyncio.sleep(0.05)
        self.assertEqual(self.daemon.session.generation, 0, "still waiting for quiet")
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1, bound=2.0)
        self.assertIs(self.daemon.session, self.daemon.loop.session)
        self.assertEqual(self.connect.attempts, 2, "no redundant third session")
        self.assertTrue(self.daemon.loop._voice_ready.is_set(), "the backend is released")
        self.assertFalse(self.daemon.status.data["reconnecting"])

class ADropDuringARolloverWithAWordInFlight(RelayHarness):
    provider = "voice_live"                       # a wire whose operator turns end
    rollover = "0.0005"

    async def test_a_backend_word_that_died_with_the_old_socket_does_not_wedge_it(self):
        """The `_obs_idle` half: the backend pump waits for a swap to say its word again,
        while the rollover waits for that word to finish. The drop breaks the tie."""
        first = self.sock
        self.ask_rt(first, 1, "跑一下测试")
        await _until(lambda: self.backend.sends)
        tag = self.backend.sends[0]["tag"]
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RECEIPT, tag=tag, turn_id="t1"))
        await _until(lambda: self.daemon.loop._request_of_turn.get("t1") == tag)
        self.connect.gate = asyncio.Event()
        await _until(lambda: self.connect.attempts == 2)           # the rollover is opening
        first.die_quietly()
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RESULT, turn_id="t1", text="测试全绿"))
        await _until(lambda: not self.daemon.loop._obs_idle.is_set())
        self.connect.gate.set()
        await _until(lambda: len(self.connect.sockets) == 2)
        await asyncio.sleep(0.05)
        first.drop()                                               # the reader finds out
        await _until(lambda: self.daemon.status.data["relay_count"] == 1, bound=2.0)
        await _until(lambda: "测试全绿" in self.said(self.sock), bound=2.0)
        self.assertEqual(self.connect.attempts, 2)


class AGoneWireLeavesNoOpenResponse(unittest.IsolatedAsyncioTestCase):
    """Realtime: a response the dead socket started never reports `done`. Left open, it
    would keep every later rollover from finding quiet."""

    async def test_a_disconnect_clears_open_responses(self):
        loop = AgentLoop(session=FakeSession(), strategy=FakeStrategy(), backend=QueueBackend(),
                         ledger=FakeLedger())
        await loop.handle_live_event(event(1, live_base.KIND_RESPONSE_CREATED,
                                           response_id="r1", payload={"origin": "user"}))
        self.assertFalse(loop.quiet())
        await loop.handle_live_event(event(2, live_base.KIND_DISCONNECTED))
        self.assertTrue(loop.quiet())


# ---------------------------------------------------------------- 2. a failed seed

class AFailedSeedIsAFailedTry(RelayHarness):

    async def test_a_reconnect_whose_recap_fails_tries_again(self):
        first = self.sock
        self.connect.poison = 1                    # the first fresh session dies at its recap
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1, bound=2.0)
        poisoned, good = self.connect.sockets[1], self.connect.sockets[2]
        self.assertTrue(poisoned.closed, "the session that failed its recap is closed")
        self.assertIs(self.daemon.session, self.daemon.loop.session)
        self.assertEqual(self.appended(good)[0][0], "thinking")   # the recap, on the good one
        self.assertFalse(self.task.done())


class AFailedSeedLeavesTheRolloverUnwedged(RelayHarness):
    rollover = "0.01"                           # 600 ms: no second try while we look

    async def test_the_old_session_carries_on_and_the_fresh_one_is_closed(self):
        first = self.sock
        self.connect.poison = 1
        await _until(lambda: len(self.connect.sockets) >= 2)
        await _until(lambda: self.connect.sockets[1].closed, bound=2.0)
        # Synchronise on the relay's own end, not on the close (review r2 MINOR).
        await _until(lambda: {"reconnecting": False} in self.daemon.status.history[-2:])
        self.assertFalse(first.closed, "the old session still carries the conversation")
        self.assertIs(self.daemon.loop.session, self.daemon.session)
        self.assertEqual(self.daemon.session.generation, 0)
        self.assertTrue(self.daemon.loop._voice_ready.is_set(), "the backend is released")
        self.assertFalse(self.daemon.status.data["reconnecting"])
        # The backend still reaches the operator through the old session.
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_PROGRESS, turn_id="t1", text="第 2 步"))
        await _until(lambda: any("第 2 步" in c for _k, _d, c in self.appended(first)))


class AStopAfterTheSwapClosesTheOldSession(RelayHarness):
    """Round-2 review MAJOR: a stop landing after the swap, before the old session's close,
    left the old paid session open (the ending closed only the new one)."""
    rollover = "0.0005"

    async def test_both_sessions_are_closed(self):
        first = self.sock
        gate = asyncio.Event()
        catch_up = self.daemon.loop.catch_up

        async def stalled(mark):
            await gate.wait()
            await catch_up(mark)

        self.daemon.loop.catch_up = stalled
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        await self.daemon.shutdown("stopped by control")
        self.assertTrue(first.closed, "the old session is closed by the ending")
        self.assertTrue(self.connect.sockets[1].closed)


class SpeechDuringTheSeedHoldsTheSwap(RelayHarness):
    """Round-2 review MAJOR: words said while the recap was being seeded were missing from
    it, and the swap then reset `speaking` and went ahead mid-utterance."""
    provider = "voice_live"
    rollover = "0.0005"

    async def test_the_swap_waits_and_the_late_words_follow_the_recap(self):
        first = self.sock
        self.connect.stall = asyncio.Event()
        await _until(lambda: len(self.connect.sockets) == 2)       # opened; its seed stalls
        stalled = self.connect.sockets[1]
        first.push({"type": "input_audio_buffer.speech_started", "item_id": "i1",
                    "audio_start_ms": 100})
        await _until(lambda: self.daemon.loop.speaking)
        stalled.send_gate.set()
        await asyncio.sleep(0.1)
        self.assertEqual(self.daemon.session.generation, 0, "no swap mid-utterance")
        for raw in ({"type": "input_audio_buffer.speech_stopped", "item_id": "i1",
                     "audio_end_ms": 900},
                    {"type": "input_audio_buffer.committed", "item_id": "i1"},
                    {"type": "conversation.item.input_audio_transcription.completed",
                     "item_id": "i1", "transcript": "等一下我还有一个问题"}):
            first.push(raw)
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        await _until(lambda: "等一下我还有一个问题" in self.said(stalled))


# ---------------------------------------------------------------- 3. GPT-Live speaking

def _live() -> GptLiveSession:
    async def connect(uri, headers):
        raise AssertionError("no socket in a mapper test")

    return GptLiveSession(model="gpt-live-1", api_key="k", endpoint="https://x.openai.azure.com",
                          connect=connect, open_bound_s=1.0, close_bound_s=1.0,
                          reconnect_bound_s=1.0)


class GptLiveNeverInfersTheOperatorIsDone(unittest.TestCase):
    """div #3: the model answering, a backchannel or a delegation is not the operator
    stopping. This wire reports no end of speech, and says so in its capabilities."""

    def test_no_speech_end_is_inferred_from_the_models_voice(self):
        from voice.live.gpt_live import CAPABILITIES

        self.assertFalse(CAPABILITIES.speech_end)
        live = _live()
        live.map_event({"type": "session.input_transcript.delta", "delta": "我想问",
                        "start_ms": 100, "end_ms": 200})
        answer = live.map_event({"type": "session.output_transcript.delta", "delta": "嗯",
                                 "start_ms": 220, "end_ms": 250})
        self.assertNotIn(live_base.KIND_INPUT_SPEECH_STOPPED, [e.kind for e in answer])


class GptLiveSpeechHoldsTheRollover(RelayHarness):
    rollover = "0.0005"

    async def test_a_backchannel_does_not_end_the_operators_turn(self):
        first = self.sock
        first.push({"type": "session.input_transcript.delta", "delta": "我想问一下明天",
                    "start_ms": 100, "end_ms": 200})
        await _until(lambda: self.daemon.loop.speaking)
        self.say(first, "嗯", at=220, until=250)                     # a backchannel
        await asyncio.sleep(0.15)
        self.assertTrue(self.daemon.loop.speaking)
        self.assertEqual(self.connect.attempts, 1, "no rollover while the operator talks")

    async def test_a_delegation_with_words_past_its_cursor_keeps_them_speaking(self):
        first = self.sock
        first.push({"type": "session.input_transcript.delta", "delta": "前半句",
                    "start_ms": 100, "end_ms": 200})
        first.push({"type": "session.input_transcript.delta", "delta": "还在说",
                    "start_ms": 300, "end_ms": 400})
        first.push({"type": "session.delegation.created", "offset_ms": 250,
                    "delegation": {"id": "d1", "target": "client"}})
        await _until(lambda: self.backend.sends)
        await asyncio.sleep(0.15)
        self.assertTrue(self.daemon.loop.speaking)
        self.assertFalse(self.daemon.loop.quiet())
        self.assertEqual(self.connect.attempts, 1, "no rollover while words are pending")


class UnclaimedWordsAreNeverQuiet(unittest.TestCase):
    """div #3: words the provider heard and no turn claimed are an utterance still open,
    whatever `speaking` says."""

    def test_pending_words_keep_the_loop_from_quiet(self):
        session = FakeSession()
        session.unclaimed_words = lambda: "还在说"
        loop = AgentLoop(session=session, strategy=FakeStrategy(), backend=QueueBackend(),
                         ledger=FakeLedger())
        loop.speaking = False
        self.assertFalse(loop.quiet())
        session.unclaimed_words = lambda: ""
        self.assertTrue(loop.quiet())


class AnUnansweredCallIsNeverQuiet(unittest.TestCase):
    """The guard the rollover leans on where nothing else would hold it (a wire with a
    speech end and no response identity): a call or delegation still waiting for its
    answer."""

    def test_an_unanswered_call_is_never_quiet(self):
        loop = AgentLoop(session=FakeSession(), strategy=FakeStrategy(), backend=QueueBackend(),
                         ledger=FakeLedger())
        loop._unanswered = 1
        self.assertFalse(loop.quiet())
        loop._unanswered = 0
        self.assertTrue(loop.quiet())


class RealtimeSpeechEndsTheTurn(RelayHarness):
    """A wire that REPORTS the end of speech ends it — the rollover proceeds after it."""
    provider = "voice_live"
    rollover = "0.0005"

    async def test_speech_stopped_lets_the_rollover_through(self):
        first = self.sock
        first.push({"type": "input_audio_buffer.speech_started", "audio_start_ms": 100,
                    "item_id": "i1"})
        await _until(lambda: self.daemon.loop.speaking)
        await asyncio.sleep(0.15)
        self.assertEqual(self.connect.attempts, 1, "no rollover mid-utterance")
        first.push({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 900,
                    "item_id": "i1"})
        await _until(lambda: self.daemon.status.data["relay_count"] >= 1)


class WordsPastTheCursorReachTheRecap(RelayHarness):
    """Round-2 review MAJOR: a delegation claims only the words up to its cursor; the rest
    were dropped from the recap (the loop wiped every partial on the complete transcript)."""

    async def test_the_second_half_after_the_cursor_is_carried(self):
        first = self.sock
        first.push({"type": "session.input_transcript.delta", "delta": "前半句",
                    "start_ms": 100, "end_ms": 200})
        first.push({"type": "session.input_transcript.delta", "delta": "后半句",
                    "start_ms": 300, "end_ms": 400})
        first.push({"type": "session.delegation.created", "offset_ms": 250,
                    "delegation": {"id": "d1", "target": "client"}})
        await _until(lambda: self.backend.sends)
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        seeded = "".join(c for _k, _d, c in self.appended(self.sock))
        self.assertIn("前半句", seeded)
        self.assertIn("后半句", seeded)


class ALateWordAfterTheAnswerStartsIsSpeechAgain(RelayHarness):
    """Round-2 opinion: the turn-over is inferred from the model's voice; a word arriving
    after it means the operator is talking again, and the rollover keeps waiting."""
    rollover = "0.0005"

    async def test_no_rollover_while_a_late_word_is_open(self):
        first = self.sock
        self.sink.pending = {"gpt-live:0:0"}                       # the answer plays
        first.push({"type": "session.input_transcript.delta", "delta": "问一下",
                    "start_ms": 100, "end_ms": 600})
        self.say(first, "好的", at=900, until=1400)                 # turn over (inferred)
        await _until(lambda: not self.daemon.loop.speaking)
        first.push({"type": "session.input_transcript.delta", "delta": "还有",
                    "start_ms": 1600, "end_ms": 1900})              # talking again
        await _until(lambda: self.daemon.loop.speaking)
        self.sink.finish()
        await asyncio.sleep(0.15)
        self.assertEqual(self.connect.attempts, 1)


class UncommittedWordsReachTheRecap(RelayHarness):

    async def test_the_first_half_of_a_question_is_in_the_recap(self):
        self.sock.push({"type": "session.input_transcript.delta", "delta": "帮我看一下那个",
                        "start_ms": 100, "end_ms": 900})
        await _until(lambda: self.daemon.loop.speaking)
        self.sock.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        seeded = "".join(c for _k, _d, c in self.appended(self.sock))
        self.assertIn("帮我看一下那个", seeded)


# ---------------------------------------------------------------- 4. device buffer

class AudioInTheDeviceBufferIsLeftover(unittest.TestCase):

    def test_the_last_written_response_still_counts_and_its_cut_aborts_the_device(self):
        from voice.audio.io import PlaybackSink

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.play("gpt-live:0:0", "a", b"\x00\x01" * 480)
            sink.drain()                           # the app queue is empty; the device is not
            self.assertIn("gpt-live:0:0", sink.queued_ids())
            sink.cancel_many(sink.queued_ids())
            self.assertEqual(stream.aborts, 1, "the device buffer is discarded")
            self.assertEqual(sink.queued_ids(), set(), "nothing sounds after the abort")
        finally:
            sink.close(bound_s=1.0)

    def test_old_audio_under_newer_audio_is_still_cut_and_the_newer_is_marked(self):
        """div #1 (BLOCKER): old A in the device buffer, new B written after it. B written
        last proves nothing about A having played: retiring A must abort, and B — cut by the
        same abort — must read as interrupted, never as heard whole."""
        from voice.audio.io import PlaybackSink

        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        try:
            sink.play("gpt-live:0:0", "a", b"\x00\x01" * 480)
            sink.drain()
            sink.play("gpt-live:1:0", "a", b"\x00\x01" * 480)
            sink.drain()
            self.assertEqual(sink.queued_ids(), {"gpt-live:0:0", "gpt-live:1:0"})
            sink.retire({"gpt-live:0:0"})
            self.assertEqual(stream.aborts, 1, "A may still be in the buffer: it is cut")
            self.assertFalse(sink.epoch_unchanged("gpt-live:1:0"), "B was cut with it")
            self.assertEqual(sink.queued_ids(), set(), "the abort emptied the buffer")
        finally:
            sink.close(bound_s=1.0)

    def test_a_cue_is_never_leftover(self):
        """A cue key cancelled would stay poisoned: the next tone of that kind never plays."""
        from voice.audio import cue

        loop = AgentLoop(session=FakeSession(), strategy=FakeStrategy(), backend=QueueBackend(),
                         ledger=FakeLedger())

        class Sink:
            def queued_ids(self):
                return {cue.response_id(cue.START), "gpt-live:0:3"}

        loop.sink = Sink()

        class Voice:
            capabilities = loop.voice.capabilities

            async def seed(self, recap):
                pass

            async def context(self, text):
                pass

        asyncio.run(loop.adopt(FakeSession(), FakeStrategy(), Voice()))
        self.assertEqual(loop._leftover, {"gpt-live:0:3"})


# ---------------------------------------------------------------- div review (9537b67)

def _mic(sock) -> list[bytes]:
    import base64

    return [base64.b64decode(m["audio"]) for m in sock.sent
            if m.get("type") == "session.input_audio.append"]


class TheMicKeepsListeningThroughAReconnect(RelayHarness):
    """div #2: the reconnect gap dropped every microphone block, so nothing the operator said
    there — a barge-in included — could reach the next session."""

    async def test_blocks_said_in_the_gap_reach_the_new_session_in_order(self):
        self.connect.gate = asyncio.Event()
        self.sock.drop()
        await _until(lambda: self.daemon.status.data["reconnecting"])
        for n in (1, 2, 3):
            await self.daemon._send_audio(bytes([n, 0]))
        self.connect.gate.set()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        await _until(lambda: len(_mic(self.sock)) >= 3)
        self.assertEqual(_mic(self.sock)[:3], [b"\x01\x00", b"\x02\x00", b"\x03\x00"])

    async def test_the_mic_is_the_new_sessions_before_the_old_close_is_done(self):
        first = self.sock
        first.close_gate = asyncio.Event()
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        new = self.sock
        await self.daemon._send_audio(b"\x07\x00")
        await _until(lambda: b"\x07\x00" in _mic(new), bound=1.0)
        self.assertFalse(first.closed, "the old close is still going on")
        first.close_gate.set()


class TheSwapIsCommittedWhereQuietWasSeen(RelayHarness):
    """div #4: the last quiet check ran in a child task and the parent yielded (cancel +
    gather) before the swap; an operator word in that window was swapped over."""
    provider = "voice_live"
    rollover = "0.0005"

    async def test_a_word_between_the_quiet_check_and_the_swap_holds_it(self):
        first = self.sock
        loop = self.daemon.loop
        real = loop.until_quiet
        spoke = []

        async def quiet_then_a_word():
            await real()
            if len(self.connect.sockets) == 2 and not spoke:   # the commit's own check
                spoke.append(True)
                first.push({"type": "input_audio_buffer.speech_started", "item_id": "i9",
                            "audio_start_ms": 100})
                await _until(lambda: loop.speaking)

        loop.until_quiet = quiet_then_a_word
        await _until(lambda: spoke)
        await asyncio.sleep(0.1)
        self.assertEqual(self.daemon.session.generation, 0, "not swapped over the word")
        self.assertFalse(first.closed)
        first.push({"type": "input_audio_buffer.speech_stopped", "item_id": "i9",
                    "audio_end_ms": 900})
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)


class ARequestSentWhileTheNewSessionWaitsIsInFlight(RelayHarness):
    """div #5: a request dispatched after the recap, before the swap, reached the new
    session only as words — not as a request still running — so it could be asked again."""
    provider = "voice_live"
    rollover = "0.0005"

    async def test_the_late_request_is_handed_over_as_in_flight(self):
        first = self.sock
        self.connect.stall = asyncio.Event()
        await _until(lambda: len(self.connect.sockets) == 2)      # opened; its recap stalls
        fresh = self.connect.sockets[1]
        self.sink.pending = {"r0"}                                 # not quiet yet
        fresh.send_gate.set()
        await _until(lambda: "语音连接刚换了新的" in self.said(fresh))
        self.ask_rt(first, 1, "跑一下全部测试")
        await _until(lambda: self.backend.sends)
        tag = self.backend.sends[0]["tag"]
        self.sink.finish()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1, bound=2.0)
        await _until(lambda: f"{tag}「跑一下全部测试」" in self.said(fresh), bound=2.0)


class TheRecapCarriesEachTurnOnce(RelayHarness):
    """div #6: the recap mark was taken before the recap flushed the voice's pending words,
    so the same words went out in the recap and again as 'said since'."""

    async def test_the_voices_words_appear_once(self):
        first = self.sock
        self.say(first, "独一无二的一句话", at=1000, until=1500)
        await _until(lambda: self.daemon.loop._voice_said)
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        said = "".join(c for _k, _d, c in self.appended(self.sock))
        self.assertEqual(said.count("独一无二的一句话"), 1)


class TheDaemonNeverNamesABackend(unittest.TestCase):
    """div #7: the daemon ran the ownership handshake only for the literal "claude_code"."""

    def test_no_backend_name_in_the_daemon(self):
        import re
        from pathlib import Path

        source = Path(daemon_mod.__file__).read_text(encoding="utf-8")
        from voice.backend import registry

        for name in registry.names():
            self.assertIsNone(re.search(rf"[\"']{name}[\"']", source), name)

    def test_a_backend_that_declares_the_handshake_gets_it(self):
        import argparse
        from unittest import mock

        from voice.backend import registry

        row = registry.BACKENDS["claude_code"]
        twin = registry.BackendProfile(name="claude_code_twin", build=row.build,
                                       capabilities=row.capabilities, prompt=row.prompt,
                                       ownership_handshake=True,
                                       dialogs_need_screen=row.dialogs_need_screen)
        args = argparse.Namespace(session="s-twin", backend="claude_code_twin", nonce="n1",
                                  terminal="", pretty=False, status=False, command="start")
        handshakes = []

        async def handshake(session_id, a):
            handshakes.append(session_id)
            raise RuntimeError("stop here")

        with mock.patch.dict(registry.BACKENDS, {"claude_code_twin": twin}), \
             mock.patch.object(daemon_mod, "_handshake", handshake), \
             mock.patch.object(daemon_mod, "preflight"), \
             mock.patch.object(daemon_mod, "claim_session", return_value=mock.Mock()):
            self.assertEqual(daemon_mod.cmd_start(args), 1)
        self.assertEqual(handshakes, ["s-twin"])


# ---------------------------------------------------------------- minors

class NoRedundantRollover(RelayHarness):

    async def test_a_rollover_of_a_session_already_replaced_opens_nothing(self):
        self.assertTrue(await self.daemon._relay("rollover", current=object()))
        self.assertEqual(self.connect.attempts, 1)


class ARetriedResultIsOneTurn(RelayHarness):

    async def test_a_result_said_again_after_a_relay_is_noted_once(self):
        first = self.sock
        self.ask(first, "d1", "跑一下测试", at=100)
        await _until(lambda: self.backend.sends)
        tag = self.backend.sends[0]["tag"]
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RECEIPT, tag=tag, turn_id="t1"))
        await _until(lambda: self.daemon.loop._request_of_turn.get("t1") == tag)
        first.die_quietly()
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RESULT, turn_id="t1", text="测试全绿"))
        await _until(lambda: not self.daemon.loop._obs_idle.is_set())
        first.drop()
        await _until(lambda: any(k == "commentary" for k, _d, c in self.appended(self.sock)))
        await _until(lambda: self.daemon.loop._obs_idle.is_set())
        turns = [t for s, t in self.daemon.loop.log.recent_turns(32) if "测试全绿" in t]
        self.assertEqual(len(turns), 1)


class ALongIdleSessionIsNotBarren(RelayHarness):

    async def test_sessions_that_lived_long_do_not_count_toward_the_empty_limit(self):
        for n in range(daemon_mod.RECONNECT_ATTEMPTS + 1):
            self.daemon._leg_opened_at = asyncio.get_running_loop().time() - 3600
            sock = self.sock
            sock.drop()
            await _until(lambda: self.daemon.status.data["relay_count"] == n + 1)
        self.assertFalse(self.task.done(), "an hour of silence is not a refusing provider")


class TheRealtimeRelay(RelayHarness):
    provider = "voice_live"

    async def test_a_drop_reconnects_and_seeds_one_system_item_without_a_response(self):
        first = self.sock
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        second = self.sock
        types = [m["type"] for m in second.sent]
        self.assertEqual(types[0], "session.update")
        items = [m for m in second.sent if m["type"] == "conversation.item.create"]
        self.assertEqual(items[0]["item"]["role"], "system")
        self.assertIn("语音连接刚换了新的", items[0]["item"]["content"][0]["text"])
        self.assertNotIn("response.create", types, "the recap is never spoken")
        self.assertEqual(self.daemon.session.generation, 1)


class TheRecapBudget(unittest.TestCase):

    def test_the_newest_turns_are_kept_inside_the_budget(self):
        loop = AgentLoop(session=FakeSession(), strategy=FakeStrategy(), backend=QueueBackend(),
                         ledger=FakeLedger())
        for n in range(8):
            loop.log.note_turn("操作者", f"第{n}句" + "字" * 40)
        recap = loop.recap(400)
        self.assertLessEqual(len(recap.encode("utf-8")), 400)
        self.assertIn("第7句", recap)
        self.assertNotIn("第0句", recap)


if __name__ == "__main__":
    unittest.main()
