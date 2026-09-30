"""Any provider × any harness × any OS: the registries, the platform shim, the preflight — and
the LIVE GATE: a barge-in flushes playback while the answer is still arriving.

The live gate runs on a FAKE CLOCK and a FAKE SINK, never on wall-clock sleeps: the clock is a
number the test advances, the sink renders exactly `ms` of queued audio per `advance(ms)`, and
the provider's audio chunks arrive at scripted clock times — faster than real time, so a lead
builds up exactly as it does on the live wire.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice import platform as voice_platform
from voice.app import daemon as daemon_mod
from voice.backend import registry as backends
from voice.live import base as live_base
from voice.live.gpt_live import GptLiveSession
from voice.live.realtime import RealtimeSession
from voice.live.strategy_delegation import DelegationStrategy
from voice.live.strategy_function import FunctionStrategy
from voice.tests.wire_fakes import FakeSocket, RecordingSession

RATE_MS = 24  # frames per ms at 24 kHz
CHUNK_MS = 100


class FakeClock:
    def __init__(self) -> None:
        self.now = 0


class ClockSink:
    """An AudioSink that renders `ms` of queued audio per `advance(ms)` of the fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.queue: list[list] = []          # [key, item, frames_left]
        self.rendered: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.flushed_at: int | None = None

    def play(self, key, item, pcm):
        if key in self.cancelled:
            return
        self.queue.append([key, item, len(pcm) // 2])

    def advance(self, ms: int) -> None:
        budget = ms * RATE_MS
        self.clock.now += ms
        while budget and self.queue:
            entry = self.queue[0]
            take = min(budget, entry[2])
            entry[2] -= take
            budget -= take
            self.rendered[entry[0]] = self.rendered.get(entry[0], 0) + take
            if entry[2] == 0:
                self.queue.pop(0)

    def queued_ms(self) -> int:
        return sum(e[2] for e in self.queue) // RATE_MS

    def cancel(self, key):
        self.cancelled.append(key)
        self.flushed_at = self.clock.now
        self.queue = [e for e in self.queue if e[0] != key]
        return self.rendered.get(key, 0)

    def rendered_ms(self, key, item):
        frames = self.rendered.get(key, 0)
        return frames // RATE_MS if frames else None

    def reached_end(self, key):
        return False

    def epoch_unchanged(self, key):
        return key not in self.cancelled


def _pcm(ms: int) -> str:
    return base64.b64encode(b"\x01\x00" * (ms * RATE_MS)).decode()


class LiveGate(unittest.IsolatedAsyncioTestCase):
    """Interruptible calls, on both provider families."""

    ANSWER_MS = 3000          # a 3 s answer
    ARRIVE_EVERY_MS = 40      # 100 ms chunks every 40 ms of clock: 2.5x real time
    BARGE_AT_MS = 600         # the operator talks while chunks are still coming

    async def test_gpt_live_barge_in_flushes_while_audio_is_still_arriving(self):
        clock = FakeClock()
        sink = ClockSink(clock)
        session = GptLiveSession(model="gpt-live-1", api_key="k",
                                 endpoint="https://r.openai.azure.com", sink=sink,
                                 connect=FakeSocket.connector([]), open_bound_s=1.0,
                                 close_bound_s=1.0, reconnect_bound_s=1.0)
        strategy = DelegationStrategy(session, sink)
        chunks = self.ANSWER_MS // CHUNK_MS
        arrived = 0
        barged_at_chunk = None
        for n in range(chunks):
            sink.advance(self.ARRIVE_EVERY_MS)
            if barged_at_chunk is None and clock.now >= self.BARGE_AT_MS:
                # The operator's words, timed on the server timeline inside the voice.
                for ev in session.map_event({"type": "session.input_transcript.delta",
                                             "delta": "停", "start_ms": clock.now,
                                             "end_ms": clock.now + 200}):
                    await strategy.feed(ev)
                barged_at_chunk = n
            for ev in session.map_event({"type": "session.output_audio.delta",
                                         "delta": _pcm(CHUNK_MS), "start_ms": n * CHUNK_MS,
                                         "end_ms": (n + 1) * CHUNK_MS}):
                await strategy.feed(ev)
            arrived += 1
        self.assertIsNotNone(sink.flushed_at, "the barge-in flushed playback")
        self.assertEqual(sink.flushed_at, self.BARGE_AT_MS)
        self.assertLess(barged_at_chunk, chunks - 1, "flushed while audio was still arriving")
        self.assertEqual(sink.queued_ms(), 0, "nothing of the cut answer is left to play")
        heard_ms = sum(sink.rendered.values()) // RATE_MS
        self.assertLessEqual(heard_ms, self.BARGE_AT_MS)
        self.assertEqual(session.dropped_late_audio, chunks - barged_at_chunk)

    async def test_realtime_barge_in_flushes_while_audio_is_still_arriving(self):
        clock = FakeClock()
        sink = ClockSink(clock)
        session = RealtimeSession(provider="voice_live", model="m", api_key="k",
                                  endpoint="https://r.openai.azure.com",
                                  api_version="2026-07-15", sink=sink,
                                  connect=FakeSocket.connector([]), open_bound_s=1.0,
                                  close_bound_s=1.0, reconnect_bound_s=1.0)
        recorder = RecordingSession()
        strategy = FunctionStrategy(recorder, sink)
        for ev in session.map_event({"type": "response.created",
                                     "response": {"id": "resp_1"}}):
            await strategy.feed(ev)
        chunks = self.ANSWER_MS // CHUNK_MS
        barged_at_chunk = None
        for n in range(chunks):
            sink.advance(self.ARRIVE_EVERY_MS)
            if barged_at_chunk is None and clock.now >= self.BARGE_AT_MS:
                for ev in session.map_event({"type": "input_audio_buffer.speech_started",
                                             "item_id": "in_2", "audio_start_ms": clock.now}):
                    await strategy.feed(ev)
                barged_at_chunk = n
            for ev in session.map_event({"type": "response.audio.delta",
                                         "response_id": "resp_1", "item_id": "item_1",
                                         "delta": _pcm(CHUNK_MS)}):
                await strategy.feed(ev)
        self.assertEqual(sink.flushed_at, self.BARGE_AT_MS)
        self.assertLess(barged_at_chunk, chunks - 1)
        self.assertEqual(recorder.cancelled, ["resp_1"], "the wire response is cancelled too")
        self.assertEqual(sink.queued_ms(), 0, "late deltas of the cut response never queue")


class Registries(unittest.TestCase):

    def test_every_backend_declares_its_platforms(self):
        self.assertEqual(backends.names(), ["claude_code", "process"])
        self.assertEqual(backends.get("claude_code").capabilities.platforms,
                         frozenset({"darwin"}))
        self.assertEqual(backends.get("process").capabilities.platforms,
                         frozenset({"darwin", "linux"}))

    def test_claude_code_is_refused_off_macos_by_name(self):
        for os_name in ("linux", "windows"):
            with self.subTest(os=os_name):
                with self.assertRaises(backends.Unsupported) as caught:
                    backends.require("claude_code", os_name)
                self.assertIn("LOCAL_PEERPID", str(caught.exception))
        self.assertIs(backends.require("claude_code", "darwin"), backends.get("claude_code"))

    def test_an_unknown_harness_is_unsupported(self):
        with self.assertRaises(backends.Unsupported):
            backends.require("telepathy", "darwin")

    def test_only_codex_exec_is_a_registered_dialect(self):
        from voice.backend import process

        self.assertEqual(sorted(process.DIALECTS), ["codex_exec"])

    def test_every_backend_has_a_close(self):
        from voice.backend.claude_code.adapter import ClaudeCodeBackend
        from voice.backend.process import ProcessBackend

        for cls in (ClaudeCodeBackend, ProcessBackend):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(callable(getattr(cls, "close", None)))


class Preflight(unittest.TestCase):

    def setUp(self):
        values = {"VOICE_PROCESS_ARGV": "codex exec --json"}
        patcher = mock.patch("voice.config.cfg", side_effect=lambda n, d="": values.get(n, d))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unregistered_process_dialect_is_refused_at_preflight(self):
        values = {"VOICE_PROCESS_ARGV": "claude -p", "VOICE_PROCESS_DIALECT": "claude_json"}
        with mock.patch("voice.config.cfg", side_effect=lambda n, d="": values.get(n, d)):
            with self.assertRaises(backends.Unsupported) as caught:
                daemon_mod.preflight("voice_live", "process", "linux")
        self.assertIn("claude_json", str(caught.exception))

    def test_every_provider_on_a_supported_backend_is_supported_on_macos(self):
        for provider in ("voice_live", "openai", "gpt_live"):
            for backend in ("claude_code", "process"):
                with self.subTest(provider=provider, backend=backend):
                    report = daemon_mod.preflight(provider, backend, "darwin")
                    self.assertTrue(report["supported"])
                    self.assertTrue(report["interruptible"])

    def test_gpt_live_reports_headphones_and_terminal_approvals(self):
        notes = daemon_mod.preflight("gpt_live", "claude_code", "darwin")["notes"]
        self.assertIn("no server echo cancellation: use headphones", notes)
        self.assertIn("permission dialogs are answered in the terminal, never by voice", notes)

    def test_unsupported_combinations_are_refused(self):
        with self.assertRaises(backends.Unsupported):
            daemon_mod.preflight("voice_live", "claude_code", "linux")
        with self.assertRaises(backends.Unsupported):
            daemon_mod.preflight("voice_live", "process", "windows")
        with self.assertRaises(backends.Unsupported):
            daemon_mod.preflight("gemini_live", "process", "linux")

    def test_status_flag_prints_the_sheet_and_refuses_with_exit_3(self):
        values = {"VOICE_LIVE_PROVIDER": "gpt_live", "VOICE_PROCESS_ARGV": "codex exec --json"}
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)), \
             mock.patch("sys.stdout") as out:
            rc = daemon_mod.main(["--backend", "process", "--status"])
        self.assertEqual(rc, 0)
        sheet = json.loads("".join(c.args[0] for c in out.write.call_args_list))
        self.assertEqual(sheet["provider"]["name"], "gpt_live")
        self.assertIn("credentials", sheet)
        self.assertNotIn("VOICE_GPT_LIVE_API_KEY=", json.dumps(sheet))
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)), \
             mock.patch("sys.stdout"):
            self.assertEqual(daemon_mod.main(["--backend", "telepathy", "--status"]), 3)

    def test_start_refuses_an_unsupported_combination_before_anything_opens(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": d), \
             mock.patch.object(daemon_mod.voice_platform, "PLATFORM", "linux"), \
             mock.patch.object(daemon_mod, "_handshake") as handshake, \
             mock.patch("sys.stderr"):
            rc = daemon_mod.main(["--backend", "claude_code", "start"])
        self.assertEqual(rc, 3)
        handshake.assert_not_called()


class PlatformShim(unittest.IsolatedAsyncioTestCase):

    async def test_the_portable_watch_fires_on_an_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "control.json").write_text("{}")
            watch = voice_platform._PollWatch(directory, poll_s=0.01)
            waiter = asyncio.ensure_future(watch.wait())
            await asyncio.sleep(0.03)
            self.assertFalse(waiter.done(), "no change, no wake")
            fresh = directory / ".tmp"
            fresh.write_text('{"command": "stop"}')
            os.replace(fresh, directory / "control.json")
            await asyncio.wait_for(waiter, 1)
            watch.close()

    async def test_the_portable_watch_follows_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text("a\n")
            watch = voice_platform._PollWatch(path, poll_s=0.01)
            waiter = asyncio.ensure_future(watch())
            with path.open("a") as handle:
                handle.write("b\n")
            await asyncio.wait_for(waiter, 1)

    async def test_the_control_watcher_runs_on_the_portable_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            seen = []

            async def on_command(command):
                seen.append(command)

            controller = daemon_mod.ControlWatcher(
                directory, on_command,
                open_watch=lambda d: voice_platform._PollWatch(d, poll_s=0.01))
            controller.start(asyncio.get_running_loop())
            daemon_mod.atomic_write(directory / "control.json", {"command": "stop", "at": 1.0})
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.01)
            controller.close()
            self.assertEqual(seen, ["stop"])

    async def test_signal_install_falls_back_where_the_loop_cannot(self):
        class NoSignals:
            def add_signal_handler(self, sig, cb):
                raise NotImplementedError

            def call_soon_threadsafe(self, cb):
                cb()

        previous = signal.getsignal(signal.SIGUSR1) if hasattr(signal, "SIGUSR1") else None
        if previous is None:
            self.skipTest("no SIGUSR1 on this platform")
        self.addCleanup(signal.signal, signal.SIGUSR1, previous)
        fired = []
        self.assertTrue(voice_platform.install_signal(NoSignals(), signal.SIGUSR1,
                                                      lambda: fired.append(1)))
        os.kill(os.getpid(), signal.SIGUSR1)
        self.assertEqual(fired, [1])

    def test_signal_install_reports_false_when_nothing_can_be_installed(self):
        class Nothing:
            def add_signal_handler(self, sig, cb):
                raise NotImplementedError

        with mock.patch.object(voice_platform.signal, "signal", side_effect=ValueError):
            self.assertFalse(voice_platform.install_signal(Nothing(), 15, lambda: None))

    def test_watch_uses_kqueue_only_where_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(voice_platform, "has_kqueue", return_value=False):
                w = voice_platform.watch(tmp, poll_s=0.5)
                self.assertIsInstance(w, voice_platform._PollWatch)
            if voice_platform.has_kqueue():
                w = voice_platform.watch(tmp, poll_s=0.5)
                self.assertIsInstance(w, voice_platform._KqueueWatch)
                w.close()


if __name__ == "__main__":
    unittest.main()
