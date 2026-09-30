"""The provider relay: a fresh voice-provider session under the same conversation.

What these pin: a provider session that DROPS is replaced (bounded retries, then a clear end);
a scheduled rollover happens only at a quiet moment and opens the new session before it
closes the old; the new session is seeded with a recap through the provider's own mechanism;
the BACKEND session is never restarted, so a result still owed is spoken after the relay;
and an operator stop or the idle close never reconnects.

Everything is real except the edges: the GPT-Live adapter, its strategy and port, the agent
loop and the daemon's relay run over a scripted socket, a recording sink and a queue-fed
backend. No network, no device. Durations are shrunk through the daemon's own knobs.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice.agent.loop import AgentLoop
from voice.app import daemon as daemon_mod
from voice.app.daemon import Status, VoiceDaemon
from voice.backend import base as backend_base
from voice.live import base as live_base
from voice.live import providers
from voice.tests.core_fakes import FakeLedger, FakeSession, FakeStrategy, event

REPO = Path(__file__).resolve().parents[2]


class LiveSocket:
    """A GPT-Live socket the test scripts: `push` an event, `drop` the connection. Our own
    `close` ends `recv` too, as a real socket does."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.closed_at: float | None = None
        self.dead = False
        self.slow_close = False
        self.fail_after: int | None = None      # sends that succeed before the socket dies
        self.send_gate: asyncio.Event | None = None
        self.close_gate: asyncio.Event | None = None

    async def send(self, payload: str) -> None:
        if self.closed or self.dead:
            raise ConnectionError("closed")
        if self.send_gate is not None and len(self.sent) >= 1:
            await self.send_gate.wait()          # the session is open; its next word stalls
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            self.dead = True
            raise ConnectionError("died right after it opened")
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        item = await self.q.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        if self.close_gate is not None:
            await self.close_gate.wait()         # a close handshake that takes its time
        if self.slow_close:
            await asyncio.sleep(0.05)          # a real close takes a round trip
        if not self.closed:
            self.closed = True
            self.closed_at = self._clock()
        self.q.put_nowait(ConnectionError("closed by us"))

    def push(self, obj: dict) -> None:
        self.q.put_nowait(json.dumps(obj))

    def drop(self) -> None:
        self.dead = True
        self.q.put_nowait(ConnectionError("connection reset"))

    def die_quietly(self) -> None:
        """The connection is gone but the reader has not found out yet: sends fail first."""
        self.dead = True

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]


class Connector:
    """`connect(uri, headers)` handing out a fresh `LiveSocket` per open. `refuse` makes the
    next opens fail; `gate` holds an open until the test releases it."""

    def __init__(self) -> None:
        self.clock = lambda: asyncio.get_running_loop().time()
        self.sockets: list[LiveSocket] = []
        self.opened_at: list[float] = []
        self.refuse = False
        self.gate: asyncio.Event | None = None
        self.attempts = 0
        self.poison = 0                          # this many next sockets die after opening
        self.stall: asyncio.Event | None = None  # the next socket's words wait on this

    async def __call__(self, uri, headers):
        self.attempts += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.refuse:
            raise ConnectionError("provider refused")
        sock = LiveSocket(self.clock)
        if self.poison:
            self.poison -= 1
            sock.fail_after = 1                  # the session opens, then any word kills it
        if self.stall is not None:
            sock.send_gate, self.stall = self.stall, None
        self.sockets.append(sock)
        self.opened_at.append(self.clock())
        return sock


class RelaySink:
    """The slice of `PlaybackSink` the loop and the relay read: plays, cancels, what is still
    queued, and a drain the test controls."""

    def __init__(self) -> None:
        self.plays: list[str] = []
        self.cancelled: list[str] = []
        self.pending: set[str] = set()
        self._waiters: list[asyncio.Future] = []

    def start(self):
        pass

    def close(self, *, bound_s=None):
        pass

    def play(self, response_id, item_id, pcm):
        self.plays.append(response_id)

    def cancel(self, response_id):
        return self.cancel_many((response_id,))

    def cancel_many(self, ids):
        ids = set(ids)
        self.cancelled.extend(sorted(ids))
        self.pending -= ids
        self._wake()
        return 0

    def queued_ids(self):
        return set(self.pending)

    def drained(self, loop):
        fut = loop.create_future()
        if not self.pending:
            fut.set_result(None)
        else:
            self._waiters.append(fut)
        return fut

    def finish(self) -> None:
        """The speaker played everything it held."""
        self.pending.clear()
        self._wake()

    def _wake(self) -> None:
        if self.pending:
            return
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    def rendered_ms(self, *_a):
        return None

    def reached_end(self, _rid):
        return False

    def epoch_unchanged(self, _rid):
        return True


class QueueBackend:
    """A backend the test feeds: every `send` is posted, observations come from a queue."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.sends: list[dict] = []
        self.closed = False

    async def observe(self):
        while True:
            yield await self.q.get()

    async def state(self):
        return backend_base.BackendState(activity="idle", turn_id=None, dialog=None)

    async def send(self, text, *, tag, priority, transcript, interpretation):
        self.sends.append({"text": text, "tag": tag})
        return backend_base.Receipt(outcome="posted", tag=tag)

    async def close(self):
        self.closed = True


class RecordingStatus(Status):
    def __init__(self, path: Path) -> None:
        super().__init__(path, session_id="relay-test")
        self.history: list[dict] = []

    def set(self, **fields):
        self.history.append(dict(fields))
        super().set(**fields)


class Lock:
    def acquire(self, timeout=0.0):
        return True

    def release(self):
        pass


def _cfg(**over):
    values = {
        "VOICE_GPT_LIVE_ENDPOINT": "https://live.openai.azure.com",
        "VOICE_GPT_LIVE_API_KEY": "sk-relay-test-secret-value",
        "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "sk-relay-test-secret-value",
        "VOICE_LIVE_API_VERSION": "2026-07-15",
        daemon_mod.IDLE_MINUTES_NAME: "0",
        daemon_mod.SESSION_MAX_MINUTES_NAME: "0",
        daemon_mod.ROLLOVER_MINUTES_NAME: "0",
    }
    values.update(over)
    return mock.patch.object(daemon_mod.voice_config, "cfg",
                             side_effect=lambda n, d="": values.get(n, d))


async def _until(predicate, bound=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + bound
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.005)


class RelayHarness(unittest.IsolatedAsyncioTestCase):
    """A daemon on the gpt_live provider, assembled from its real parts, started for real."""

    rollover = "0"
    idle = "0"
    provider = "gpt_live"

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(**{daemon_mod.ROLLOVER_MINUTES_NAME: self.rollover,
                           daemon_mod.IDLE_MINUTES_NAME: self.idle})
        self.cfg.start()
        self.addCleanup(self.cfg.stop)
        # `redact_text` masks the secret VALUES it finds in the environment.
        env = mock.patch.dict(os.environ, {"VOICE_GPT_LIVE_API_KEY": "sk-relay-test-secret-value"})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("RECONNECT_BACKOFF_S", 0.001), ("FAREWELL_BOUND_S", 0.05)):
            patch = mock.patch.object(daemon_mod, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.connect = Connector()
        self.sink = RelaySink()
        self.backend = QueueBackend()
        d = VoiceDaemon(session_id="relay-test", provider=self.provider, backend_name="process",
                        state=Path(self.tmp.name), connect=self.connect,
                        open_input=lambda cb: mock.Mock(), audio_lock=Lock(),
                        open_watch=lambda directory: None)
        d.status = RecordingStatus(Path(self.tmp.name) / "status.json")
        d.profile = providers.get(self.provider)
        d.sink = self.sink
        d.tools = d.profile.tools()
        d.backend = self.backend
        d.session, d.strategy, d.voice = d._leg()
        d.loop = AgentLoop(session=d.session, strategy=d.strategy, backend=self.backend,
                           ledger=FakeLedger(), sink=self.sink, terminal_only=True,
                           voice=d.voice)
        self.daemon = d
        self.task = asyncio.ensure_future(d.start())
        await _until(lambda: self.connect.sockets and d.status.data["phase"] == "running")

    async def asyncTearDown(self):
        if not self.task.done():
            await self.daemon.shutdown("test over")
            await asyncio.wait_for(self.task, 3.0)

    # -------------------------------------------------------------- helpers

    @property
    def sock(self) -> LiveSocket:
        return self.connect.sockets[-1]

    def ask(self, sock: LiveSocket, did: str, words: str, at: int) -> None:
        """The operator says `words` and the model delegates them."""
        sock.push({"type": "session.input_transcript.delta", "delta": words,
                   "start_ms": at, "end_ms": at + 500})
        sock.push({"type": "session.delegation.created", "offset_ms": at + 600,
                   "delegation": {"id": did, "target": "client"}})

    def say(self, sock: LiveSocket, words: str, at: int, until: int) -> None:
        """The model speaks `words` over [at, until] on the server timeline."""
        sock.push({"type": "session.output_transcript.delta", "delta": words,
                   "start_ms": at, "end_ms": until})

    def appended(self, sock: LiveSocket) -> list[tuple[str, str | None, str]]:
        return [(m["type"].split(".")[1], m.get("delegation_id"), m.get("content", ""))
                for m in sock.sent if m.get("type", "").endswith(".append")
                and m["type"] != "session.input_audio.append"]

    def said(self, sock: LiveSocket) -> str:
        """Every word our code put into a session, on either wire."""
        out: list[str] = [c for _k, _d, c in self.appended(sock)]
        for m in sock.sent:
            if m.get("type") != "conversation.item.create":
                continue
            item = m["item"]
            if item.get("type") == "message":
                out += [part.get("text", "") for part in item.get("content", [])]
            elif item.get("type") == "function_call_output":
                out.append(item.get("output", ""))
        return "\n".join(out)

    def ask_rt(self, sock: LiveSocket, n: int, words: str) -> None:
        """Realtime wire: the operator says `words` (server VAD start/stop, the committed
        item and its transcript) and the model calls `relay` for them."""
        item, rid = f"i{n}", f"r{n}"
        for raw in (
                {"type": "input_audio_buffer.speech_started", "item_id": item,
                 "audio_start_ms": 100 * n},
                {"type": "input_audio_buffer.speech_stopped", "item_id": item,
                 "audio_end_ms": 100 * n + 80},
                {"type": "input_audio_buffer.committed", "item_id": item},
                {"type": "conversation.item.input_audio_transcription.completed",
                 "item_id": item, "transcript": words},
                {"type": "response.created", "response": {"id": rid}},
                {"type": "response.output_item.done", "response_id": rid,
                 "item": {"type": "function_call", "id": f"fc{n}", "call_id": f"c{n}",
                          "name": "relay",
                          "arguments": json.dumps({"text": words, "interrupt": False})}},
                {"type": "response.done", "response": {"id": rid, "status": "completed"}}):
            sock.push(raw)


class ADropReconnectsAndSeedsTheRecap(RelayHarness):

    async def test_drop_reconnects_with_a_higher_generation_and_the_recap_seeded(self):
        first = self.sock
        self.ask(first, "d1", "帮我查一下明天的天气", at=100)
        await _until(lambda: self.backend.sends)
        self.say(first, "好的,我去查一下", at=1000, until=1600)
        await _until(lambda: self.daemon.loop._voice_said)
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)

        second = self.sock
        self.assertIsNot(second, first)
        self.assertEqual(self.daemon.session.generation, 1)
        self.assertIs(self.daemon.loop.session, self.daemon.session)
        # The new session opened like the first, then heard the recap as quiet context.
        self.assertEqual(second.sent[0]["type"], "session.start")
        seeded = "".join(c for kind, did, c in self.appended(second)
                         if kind == "thinking" and did is None)
        self.assertIn("帮我查一下明天的天气", seeded)             # the operator's turn
        self.assertIn("好的,我去查一下", seeded)                   # the voice's turn
        self.assertIn("req-1", seeded)                             # the request still in flight
        self.assertNotIn("sk-relay-test-secret-value", json.dumps(second.sent))
        # The status said so while it happened, and the phase never left `running`.
        self.assertIn({"reconnecting": True}, self.daemon.status.history)
        # Synchronise on the relay's own end: the swap bumps relay_count, then still awaits the
        # mic hand-over before the outer finally clears `reconnecting` (a slow runner lands between).
        await _until(lambda: not self.daemon.status.data["reconnecting"])
        self.assertFalse(self.daemon.status.data["reconnecting"])
        self.assertEqual(self.daemon.status.data["phase"], "running")
        phases = [h["phase"] for h in self.daemon.status.history if "phase" in h]
        self.assertEqual(phases[-1], "running")

    async def test_a_recap_redacts_a_secret_the_operator_said(self):
        self.ask(self.sock, "d1", "the key is sk-relay-test-secret-value ok", at=100)
        await _until(lambda: self.backend.sends)
        self.sock.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        seeded = json.dumps(self.appended(self.sock), ensure_ascii=False)
        self.assertIn("the key is", seeded)
        self.assertNotIn("sk-relay-test-secret-value", seeded)


    async def test_a_secret_cut_by_the_clip_leaves_no_prefix_behind(self):
        """Redacted BEFORE the per-turn clip: clipping first would cut the secret at the
        boundary and leave a prefix no later redaction can recognise."""
        from voice.agent.loop import RECAP_TURN_BYTES

        words = "x" * (RECAP_TURN_BYTES - 12) + " sk-relay-test-secret-value"
        self.ask(self.sock, "d1", words, at=100)
        await _until(lambda: self.backend.sends)
        self.sock.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        seeded = "".join(c for _k, _d, c in self.appended(self.sock))
        self.assertIn("xxxx", seeded)
        self.assertNotIn("sk-relay", seeded)


class AnInFlightResultIsSpokenAfterTheRelay(RelayHarness):

    async def test_a_result_that_lands_after_the_relay_is_spoken_on_the_new_session(self):
        first = self.sock
        self.ask(first, "d1", "跑一下测试", at=100)
        await _until(lambda: self.backend.sends)
        tag = self.backend.sends[0]["tag"]
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RECEIPT, tag=tag, turn_id="t1",
            payload={"disposition": "consumed"}))
        await _until(lambda: self.daemon.loop._request_of_turn.get("t1") == tag)
        first.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RESULT, turn_id="t1", text="测试全绿,42 个通过"))
        second = self.sock
        await _until(lambda: any(k == "commentary" for k, _d, _c in self.appended(second)))
        spoken = [(d, c) for k, d, c in self.appended(second) if k == "commentary"]
        # Spoken, and unbound: the new session never issued `d1`.
        self.assertEqual(len(spoken), 1)
        self.assertIsNone(spoken[0][0])
        self.assertIn("测试全绿", spoken[0][1])
        self.assertFalse(self.backend.closed, "the backend session is never restarted")

    async def test_a_result_during_the_reconnect_waits_and_follows_the_recap(self):
        first = self.sock
        self.ask(first, "d1", "跑一下测试", at=100)
        await _until(lambda: self.backend.sends)
        tag = self.backend.sends[0]["tag"]
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RECEIPT, tag=tag, turn_id="t1"))
        await _until(lambda: self.daemon.loop._request_of_turn.get("t1") == tag)
        self.connect.gate = asyncio.Event()
        # The PROVIDER ends the session (its age limit): the adapter then drops every append
        # silently, so only the hold keeps the result from vanishing.
        first.push({"type": "session.closed", "reason": "max_duration"})
        await _until(lambda: self.daemon.status.data["reconnecting"])
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_RESULT, turn_id="t1", text="测试全绿"))
        await asyncio.sleep(0.05)
        self.assertFalse([m for m in first.sent if m.get("type") == "session.commentary.append"],
                         "nothing is said into the dead session")
        self.connect.gate.set()
        await _until(lambda: any(k == "commentary" for k, _d, _c in self.appended(self.sock)))
        kinds = [k for k, _d, _c in self.appended(self.sock)]
        self.assertEqual(kinds[0], "thinking")                    # the recap first
        self.assertEqual(kinds[-1], "commentary")                 # then the result, spoken


class ASendIntoADyingSocketIsNotFatal(RelayHarness):

    async def test_a_result_whose_send_fails_is_said_again_after_the_relay(self):
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
        await asyncio.sleep(0.02)
        first.drop()                                              # the reader finds out
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        await _until(lambda: any(k == "commentary" for k, _d, _c in self.appended(self.sock)))
        self.assertFalse(self.task.done())
        spoken = [c for k, _d, c in self.appended(self.sock) if k == "commentary"]
        self.assertIn("测试全绿", spoken[0])

    async def test_a_receipt_whose_send_fails_is_the_drop(self):
        first = self.sock
        first.die_quietly()
        self.ask(first, "d1", "跑一下测试", at=100)               # its receipt cannot go out
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        self.assertEqual(len(self.backend.sends), 1, "dispatched once, never replayed")
        self.assertFalse(self.task.done())


class InterruptionWorksAcrossARelay(RelayHarness):

    async def test_audio_the_old_session_left_queued_is_cut_when_the_operator_talks(self):
        self.sink.pending = {"gpt-live:0:0"}                      # the old answer still queued
        self.sock.drop()
        await _until(lambda: self.daemon.status.data["relay_count"] == 1)
        self.assertEqual(self.sink.cancelled, [])                 # heard out, until...
        self.sock.push({"type": "session.input_transcript.delta", "delta": "等一下",
                        "start_ms": 50, "end_ms": 400})           # ...the operator talks
        await _until(lambda: self.sink.cancelled)
        self.assertEqual(self.sink.cancelled, ["gpt-live:0:0"])


class TheRetryLimitEndsTheSession(RelayHarness):

    async def test_every_reopen_failing_ends_with_the_reason_in_the_status(self):
        self.connect.refuse = True
        self.sock.drop()
        await asyncio.wait_for(self.task, 3.0)
        ended = self.daemon.status.data["ended"]["reason"]
        self.assertIn("reconnect gave up after 3 attempts", ended)
        self.assertEqual(self.connect.attempts, 1 + daemon_mod.RECONNECT_ATTEMPTS)
        self.assertEqual(self.daemon.status.data["relay_count"], 0)

    async def test_fresh_sessions_that_each_die_empty_end_it_too(self):
        for _ in range(daemon_mod.RECONNECT_ATTEMPTS):
            sock = self.sock
            sock.drop()
            await _until(lambda: self.sock is not sock or self.task.done())
        await asyncio.wait_for(self.task, 3.0)
        self.assertIn("ended before anything was said",
                      self.daemon.status.data["ended"]["reason"])


class AStopNeverReconnects(RelayHarness):

    async def test_an_operator_stop_closes_without_a_new_session(self):
        await self.daemon.on_control("stop")
        await asyncio.wait_for(self.task, 3.0)
        self.assertEqual(self.connect.attempts, 1)
        self.assertEqual(self.daemon.status.data["ended"]["reason"], "stopped by control")
        self.assertEqual(self.daemon.status.data["relay_count"], 0)
        self.assertTrue(self.connect.sockets[0].closed)


class TheIdleCloseNeverReconnects(RelayHarness):
    idle = "0.0005"

    async def test_idle_close_ends_without_a_new_session(self):
        await asyncio.wait_for(self.task, 3.0)
        self.assertEqual(self.daemon.status.data["ended"]["reason"], "idle")
        self.assertEqual(self.connect.attempts, 1)


class ARolloverWaitsForQuiet(RelayHarness):
    rollover = "0.0005"                         # 30 ms

    async def test_rollover_waits_for_the_speaker_then_opens_before_it_closes(self):
        """Measured bound: the new session is OPEN before the old one is closed — an overlap,
        so the gap is 0 s (well under the ~1 s allowed)."""
        self.sink.pending = {"gpt-live:0:0"}    # the model's answer is still playing
        await asyncio.sleep(0.15)                # several rollover windows
        self.assertEqual(self.connect.attempts, 1, "no rollover while audio plays")
        self.sink.finish()
        await _until(lambda: self.daemon.status.data["relay_count"] >= 1)
        old, new = self.connect.sockets[0], self.connect.sockets[1]
        self.assertTrue(old.closed)
        self.assertLessEqual(self.connect.opened_at[1], old.closed_at)
        self.assertEqual(new.sent[0]["type"], "session.start")
        self.assertEqual(self.daemon.status.data["phase"], "running")



class ARolloverWaitsForTheOperator(RelayHarness):
    # Realtime: a wire that reports when the operator stops. (On GPT-Live, which does not,
    # a rollover never finds quiet once they have spoken — test_app_relay_fixes.)
    provider = "voice_live"
    rollover = "0.0005"

    def speak(self, sock: LiveSocket, item: str) -> None:
        sock.push({"type": "input_audio_buffer.speech_started", "item_id": item,
                   "audio_start_ms": 100})

    def stop(self, sock: LiveSocket, item: str) -> None:
        sock.push({"type": "input_audio_buffer.speech_stopped", "item_id": item,
                   "audio_end_ms": 900})

    async def test_the_operator_speaking_at_rollover_time_delays_it(self):
        first = self.sock
        self.speak(first, "i1")
        await _until(lambda: self.daemon.loop.speaking)
        await asyncio.sleep(0.15)
        self.assertEqual(self.connect.attempts, 1, "no rollover mid-utterance")
        self.assertFalse(first.closed)
        self.stop(first, "i1")                                     # their turn ends
        await _until(lambda: self.daemon.status.data["relay_count"] >= 1)
        self.assertTrue(first.closed)

    async def test_speech_that_starts_while_the_new_session_opens_holds_the_swap(self):
        first = self.sock
        self.connect.gate = asyncio.Event()
        await _until(lambda: self.connect.attempts == 2)          # the rollover is opening
        self.speak(first, "i1")
        await _until(lambda: self.daemon.loop.speaking)
        self.connect.gate.set()
        await asyncio.sleep(0.1)
        self.assertEqual(self.daemon.session.generation, 0, "not swapped mid-utterance")
        self.assertFalse(first.closed)
        self.stop(first, "i1")
        await _until(lambda: self.daemon.status.data["relay_count"] >= 1)
        self.assertEqual(self.daemon.session.generation, 1)
        self.assertTrue(first.closed)


class ARolloverWaitsForAnswers(RelayHarness):
    # Realtime: its wire reports the end of the operator's speech, so the only thing
    # holding the rollover here is the unanswered call.
    provider = "voice_live"
    rollover = "0.0005"

    async def test_an_unanswered_call_holds_the_rollover(self):
        gate = asyncio.Event()
        send = self.backend.send

        async def slow_send(*a, **kw):
            await gate.wait()
            return await send(*a, **kw)

        self.backend.send = slow_send
        self.ask_rt(self.sock, 1, "查一下")
        await _until(lambda: self.daemon.loop._unanswered == 1)
        await asyncio.sleep(0.15)
        self.assertEqual(self.connect.attempts, 1, "no rollover while a call waits")
        gate.set()
        await _until(lambda: self.daemon.status.data["relay_count"] >= 1)
        # The receipt went out on the session that issued the call, bound to it.
        outputs = [m["item"]["call_id"] for m in self.connect.sockets[0].sent
                   if m.get("type") == "conversation.item.create"
                   and m["item"].get("type") == "function_call_output"]
        self.assertEqual(outputs, ["c1"])

    async def test_a_backend_word_half_way_out_finishes_before_the_swap(self):
        gate = asyncio.Event()
        handle = self.daemon.loop.handle_observation

        async def slow(obs):
            await gate.wait()
            await handle(obs)

        self.daemon.loop.handle_observation = slow
        self.connect.gate = asyncio.Event()
        await _until(lambda: self.connect.attempts == 2)           # the rollover is opening
        self.backend.q.put_nowait(backend_base.Observation(
            kind=backend_base.OBS_PROGRESS, turn_id="t9", text="正在跑第 3 步"))
        await _until(lambda: not self.daemon.loop._obs_idle.is_set())
        self.connect.gate.set()
        await asyncio.sleep(0.1)
        self.assertEqual(self.daemon.session.generation, 0, "no swap under a word in flight")
        gate.set()
        await _until(lambda: self.daemon.status.data["relay_count"] >= 1)
        self.assertIn("正在跑第 3 步", self.said(self.connect.sockets[0]),
                      "it reached the old session")


class AStopDuringARolloverClosesItsFreshSession(RelayHarness):
    rollover = "0.0005"

    async def test_the_unadopted_session_is_closed_before_the_ending_finishes(self):
        self.connect.gate = asyncio.Event()
        await _until(lambda: self.connect.attempts == 2)          # the rollover is opening
        self.sink.pending = {"gpt-live:0:0"}                       # ...and audio starts playing
        self.connect.gate.set()
        await _until(lambda: len(self.connect.sockets) == 2)
        self.connect.sockets[1].slow_close = True
        await asyncio.sleep(0.05)
        self.assertEqual(self.daemon.session.generation, 0, "waiting to swap")
        await self.daemon.shutdown("stopped by control")
        self.assertTrue(self.connect.sockets[1].closed, "no paid session left open")
        self.assertEqual(self.daemon.status.data["relay_count"], 0)


class TheRelayGuards(RelayHarness):

    async def test_no_relay_once_the_session_is_ending(self):
        self.daemon._shut = True
        self.assertFalse(await self.daemon._relay("reconnect", self.daemon.session))
        self.assertEqual(self.connect.attempts, 1)
        self.daemon._shut = False

    async def test_a_drop_of_a_session_already_replaced_opens_nothing(self):
        self.daemon.loop.hold()
        self.assertTrue(await self.daemon._relay("reconnect", object()))
        self.assertEqual(self.connect.attempts, 1)
        self.assertTrue(self.daemon.loop._voice_ready.is_set(), "the backend is released")


class TheLoopReconnectsOnlyOnADrop(unittest.IsolatedAsyncioTestCase):
    """The loop asks for a new session when the stream ended BY ITSELF: a socket drop, or a
    provider `closed` (which carries a reason). Our own close carries none."""

    async def _run(self, last):
        asked = []

        async def reconnect(reason, dead, barren):
            asked.append(reason)
            return False

        loop = AgentLoop(session=FakeSession([last]), strategy=FakeStrategy(),
                         backend=QueueBackend(), ledger=FakeLedger())
        loop.reconnect = reconnect
        await asyncio.wait_for(loop.run(), 2.0)
        return asked

    async def test_a_drop_asks(self):
        self.assertEqual(await self._run(event(1, live_base.KIND_DISCONNECTED)), ["reconnect"])

    async def test_a_provider_close_asks(self):
        closed = event(1, live_base.KIND_CLOSED, payload={"reason": "max_duration"})
        self.assertEqual(await self._run(closed), ["reconnect"])

    async def test_our_own_close_does_not(self):
        self.assertEqual(await self._run(event(1, live_base.KIND_CLOSED)), [])


class TheRealtimePortSeedsAsAnItem(unittest.IsolatedAsyncioTestCase):

    async def test_the_seed_is_one_system_item_and_no_response(self):
        from voice.live.port import SessionVoice

        session = FakeSession()
        await SessionVoice(session).seed("recap")
        self.assertEqual(session.item_texts(), ["recap"])
        self.assertEqual(session.responses, [])


class TheSeedPassesTheOneRedactionPoint(unittest.IsolatedAsyncioTestCase):

    async def test_the_redacted_port_masks_a_recap(self):
        from voice.live.port import RedactedVoice

        session = FakeSession()
        from voice.live.port import SessionVoice
        with mock.patch.dict(os.environ, {"VOICE_GPT_LIVE_API_KEY": "sk-relay-test-secret-value"}):
            await RedactedVoice(SessionVoice(session)).seed("key sk-relay-test-secret-value")
        self.assertNotIn("sk-relay-test-secret-value", session.item_texts()[0])


class TheDefaults(unittest.TestCase):

    def test_idle_is_ten_minutes_and_rollover_fifty_five(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=lambda n, d="": d):
            self.assertEqual(daemon_mod.idle_close_s(), 600.0)
            self.assertEqual(daemon_mod.rollover_s(), 55 * 60.0)

    def test_zero_turns_the_rollover_off(self):
        with _cfg():
            self.assertEqual(daemon_mod.rollover_s(), 0)


SEGMENT = REPO / "host/hooks/voice-statusline-segment.sh"


# The hook ships with a separate bundle, not with `voice/` (a package that ships only
# `voice/` has its own statusline and its own test for it).
@unittest.skipUnless(shutil.which("jq") and shutil.which("bash") and SEGMENT.exists(),
                     "needs bash, jq and the bundle's statusline hook")
class TheStatuslineHoldsThroughARelay(unittest.TestCase):

    def _segment(self, **fields) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "relay-seg"
            (Path(tmp) / sid).mkdir()
            data = {"phase": "running", "relay": "qualified", "pid": os.getpid(), "ended": {}}
            data.update(fields)
            (Path(tmp) / sid / "status.json").write_text(json.dumps(data), encoding="utf-8")
            out = subprocess.run(
                ["bash", str(SEGMENT), sid],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                env={**os.environ, "VOICE_LISTEN_STATE_DIR": tmp})
            return out.stdout

    def test_reconnecting_keeps_the_segment_and_marks_it(self):
        self.assertEqual(self._segment(reconnecting=True, relay_count=1), "🎙 voice ↻")

    def test_running_shows_the_plain_segment(self):
        self.assertEqual(self._segment(reconnecting=False, relay_count=2), "🎙 voice")


if __name__ == "__main__":
    unittest.main()
