"""The daemon's wiring, with fakes only: no network, no audio device, no real process.

What these pin is the part that has no other test: which concrete class the env chooses,
what the daemon refuses to start without, and that the status file keeps the exact field
names the statusline segment parses. A renamed status field breaks the operator's pill
silently, which is why it is asserted against a literal list rather than a round-trip.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

from voice.app import daemon as daemon_mod
from voice.app.daemon import atomic_write
from voice.app.daemon import (
    MissingCredentials,
    Status,
    VoiceDaemon,
    build_backend,
    build_session,
    check_credentials,
    state_dir,
)
from voice.tests.wire_fakes import FakeSocket, FakeStream

# Every field status.json promises to any reader (the daemon docstring's contract).
STATUSLINE_FIELDS = ("phase", "pid", "at", "started_at", "relay", "mode",
                     "audio_ready", "asleep", "instance", "relay_count", "reconnecting")
STATUSLINE_NESTED = (("ended", "reason"), ("ended", "ended_at"),
                     ("last_receipt", "state"), ("session", "id"),
                     ("session", "name"), ("session", "cwd"))

AZURE_ENV = {"AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com",
             "AZURE_OPENAI_API_KEY": "secret-value-never-printed"}


class CredentialCheck(unittest.TestCase):

    def test_azure_needs_endpoint_and_key_by_name(self):
        check_credentials("voice_live", AZURE_ENV)      # does not raise

    def test_a_missing_name_is_reported_without_its_value(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(daemon_mod.voice_config, "cfg", return_value=""):
            with self.assertRaises(MissingCredentials) as caught:
                check_credentials("voice_live", {"AZURE_OPENAI_ENDPOINT": "https://x"})
        message = str(caught.exception)
        self.assertIn("AZURE_OPENAI_API_KEY", message)
        self.assertNotIn("https://x", message,
                         "the check must name fields, not echo their values")

    def test_a_present_but_empty_value_counts_as_missing(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg", return_value=""):
            with self.assertRaises(MissingCredentials):
                check_credentials("voice_live", {"AZURE_OPENAI_ENDPOINT": "https://x",
                                                 "AZURE_OPENAI_API_KEY": "   "})

    def test_openai_needs_only_its_own_key(self):
        check_credentials("openai", {"OPENAI_API_KEY": "sk-x"})

    def test_an_unknown_provider_is_refused_by_name(self):
        with self.assertRaises(MissingCredentials):
            check_credentials("gemini_live", {})

    def test_the_secret_value_never_appears_in_any_message(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg", return_value=""):
            with self.assertRaises(MissingCredentials) as caught:
                check_credentials("voice_live", {"AZURE_OPENAI_API_KEY": "hunter2"})
        self.assertNotIn("hunter2", str(caught.exception))


class ProviderChoice(unittest.TestCase):

    def _cfg(self, values):
        return mock.patch.object(daemon_mod.voice_config, "cfg",
                                 side_effect=lambda n, d="": values.get(n, d))

    def test_voice_live_is_the_default_and_builds_the_azure_url(self):
        with self._cfg({"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com",
                        "AZURE_OPENAI_API_KEY": "k",
                        "VOICE_LIVE_API_VERSION": "2026-07-15",
                        "VOICE_MODEL": "gpt-realtime-2.1"}):
            session = build_session("voice_live", sink=None,
                                    connect=FakeSocket.connector([]))
        self.assertEqual(
            session.uri(),
            "wss://res.cognitiveservices.azure.com/voice-live/realtime"
            "?api-version=2026-07-15&model=gpt-realtime-2.1")
        self.assertEqual(list(session.headers()), ["api-key"])

    def test_openai_provider_builds_the_openai_url_with_bearer(self):
        with self._cfg({"OPENAI_API_KEY": "sk-x", "VOICE_MODEL": "gpt-realtime"}):
            session = build_session("openai", sink=None, connect=FakeSocket.connector([]))
        self.assertEqual(session.uri(), "wss://api.openai.com/v1/realtime?model=gpt-realtime")
        self.assertTrue(session.headers()["Authorization"].startswith("Bearer "))

    def test_the_lifecycle_bounds_are_injected_from_this_module(self):
        with self._cfg({"AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com",
                        "VOICE_LIVE_API_VERSION": "2026-07-15"}):
            session = build_session("voice_live", sink=None,
                                    connect=FakeSocket.connector([]))
        self.assertEqual(session.open_bound_s, daemon_mod.CONNECT_BOUND_S)
        self.assertEqual(session.close_bound_s, daemon_mod.CLOSE_BOUND_S)
        self.assertEqual(session.reconnect_bound_s, daemon_mod.RECONNECT_BOUND_S)


class SessionCap(unittest.TestCase):
    """The operator's session length, from the environment. Off by default; when set it ends the
    whole session with its reason, and it is not a timer inside any decision."""

    def _cfg(self, values):
        return mock.patch.object(daemon_mod.voice_config, "cfg",
                                 side_effect=lambda n, d="": values.get(n, d))

    def test_no_session_cap_by_default_and_minutes_when_set(self):
        with self._cfg({}):
            self.assertEqual(daemon_mod.session_max_s(), 0.0)
        with self._cfg({"VOICE_SESSION_MAX_MINUTES": "10"}):
            self.assertEqual(daemon_mod.session_max_s(), 600.0)

    def test_the_session_cap_ends_the_session_with_its_reason(self):
        class NeverEnds:
            async def run(self):
                await asyncio.Event().wait()

        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.loop = NeverEnds()
        reasons = []

        async def shutdown(reason):
            reasons.append(reason)
        daemon.shutdown = shutdown
        with self._cfg({"VOICE_SESSION_MAX_MINUTES": "0.0005"}):
            asyncio.run(daemon._run())
        self.assertEqual(reasons, ["session limit"])


class StopEndsTheRun(unittest.IsolatedAsyncioTestCase):
    """The backend stream waits on a file watch and never ends by itself, so a `stop` that only
    closed the sockets left the process alive (measured 2026-09-21). `shutdown` now cancels the
    loop task the daemon owns, and `_run` returns."""

    async def test_a_stop_control_returns_from_run(self):
        class NeverEnds:
            async def run(self):
                await asyncio.Event().wait()

        class Recorder:
            def __init__(self):
                self.phases, self.ended = [], []

            def set(self, **kw):
                self.phases.append(kw)

            def end(self, reason):
                self.ended.append(reason)

        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.loop = NeverEnds()
        daemon.status = Recorder()
        daemon.control = daemon.capture = daemon.session = daemon.sink = daemon.ledger = None
        daemon._lock_held = False
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=lambda n, d="": d):
            run = asyncio.ensure_future(daemon._run())
            await asyncio.sleep(0)
            await daemon.on_control("stop")
            await asyncio.wait_for(run, 1.0)
        self.assertEqual(daemon.status.ended, ["stopped by control"])
        self.assertTrue(daemon._loop_task.cancelled())


class BackendChoice(unittest.TestCase):

    def test_process_backend_needs_its_argv_and_says_so(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg", return_value=""):
            with self.assertRaises(RuntimeError) as caught:
                build_backend("process")
        self.assertIn("VOICE_PROCESS_ARGV", str(caught.exception))

    def test_process_backend_is_built_from_the_argv_and_dialect(self):
        from voice.backend.process import codex_exec_parser

        values = {"VOICE_PROCESS_ARGV": "codex exec --json", "VOICE_BACKEND_CWD": "/tmp"}
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)):
            backend = build_backend("process")
        spec = backend._spec
        self.assertEqual(spec.argv, ("codex", "exec", "--json"))
        self.assertEqual(spec.cwd, "/tmp")
        self.assertIs(spec.parser, codex_exec_parser)

    def test_an_unknown_process_dialect_is_refused_by_name(self):
        values = {"VOICE_PROCESS_ARGV": "thing", "VOICE_PROCESS_DIALECT": "martian"}
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)):
            with self.assertRaises(RuntimeError) as caught:
                build_backend("process")
        self.assertIn("martian", str(caught.exception))

    def test_an_unknown_backend_names_the_valid_ones(self):
        with self.assertRaises(RuntimeError) as caught:
            build_backend("telepathy")
        self.assertIn("process", str(caught.exception))


class StatusFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "status.json"

    def test_every_field_the_statusline_reads_is_present(self):
        status = Status(self.path, session_id="sess-1", session_name="voice",
                        session_cwd="/work", now=lambda: 1000.0)
        status.publish()
        data = json.loads(self.path.read_text())
        for field in STATUSLINE_FIELDS:
            self.assertIn(field, data, f"statusline reads .{field}")
        for parent, child in STATUSLINE_NESTED:
            self.assertIn(parent, data)
        self.assertEqual(data["session"]["id"], "sess-1")
        self.assertEqual(data["session"]["cwd"], "/work")
        self.assertEqual(data["last_receipt"]["state"], "")

    def test_status_round_trips_through_the_file(self):
        status = Status(self.path, session_id="sess-1", now=lambda: 1000.0)
        status.set(phase="running", audio_ready=True, relay="live")
        read_back = Status.read(self.path)
        self.assertEqual(read_back["phase"], "running")
        self.assertTrue(read_back["audio_ready"])
        self.assertEqual(read_back["relay"], "live")
        self.assertEqual(read_back["at"], 1000.0)

    def test_end_records_the_reason_and_the_ended_at_the_segment_reads(self):
        status = Status(self.path, session_id="sess-1", now=lambda: 2000.0)
        status.end("stream ended")
        data = Status.read(self.path)
        self.assertEqual(data["phase"], "ended")
        self.assertEqual(data["ended"]["reason"], "stream ended")
        self.assertEqual(data["ended"]["ended_at"], 2000.0)

    def test_an_absent_file_reads_as_none_rather_than_raising(self):
        self.assertIsNone(Status.read(self.path / "nope"))

    def test_a_corrupt_file_reads_as_none_rather_than_raising(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(Status.read(self.path))

    def test_the_write_is_atomic_and_leaves_no_temp_file_behind(self):
        status = Status(self.path, session_id="sess-1")
        status.publish()
        status.set(phase="running")
        leftovers = [p.name for p in self.path.parent.iterdir()
                     if p.name.startswith(".status-")]
        self.assertEqual(leftovers, [])

    def test_the_state_dir_honours_the_env_override(self):
        with mock.patch.dict(os.environ, {"VOICE_LISTEN_STATE_DIR": "/tmp/voice-state"}):
            self.assertEqual(state_dir("abc"), Path("/tmp/voice-state/abc"))


class DaemonGraph(unittest.TestCase):
    """The object graph, built from env with every device and socket faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.values = {
            "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k",
            "VOICE_LIVE_API_VERSION": "2026-07-15",
            "VOICE_PROCESS_ARGV": "true",
        }

    def _daemon(self, **kw):
        return VoiceDaemon(session_id="sess-1", provider="voice_live",
                           backend_name="process", state=self.dir,
                           connect=FakeSocket.connector([]),
                           open_output=lambda: FakeStream(), **kw)

    def test_build_assembles_every_part_from_env(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": self.values.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            daemon = self._daemon()
            daemon.build()
            self.addCleanup(daemon.ledger.close)
        from voice.agent.loop import AgentLoop
        from voice.audio.io import PlaybackSink
        from voice.backend.process import ProcessBackend
        from voice.live.realtime import RealtimeSession
        from voice.live.strategy_function import FunctionStrategy

        self.assertIsInstance(daemon.sink, PlaybackSink)
        self.assertTrue(daemon.loop.broker.terminal_only, "approvals stay on the keyboard")
        self.assertIsInstance(daemon.session, RealtimeSession)
        self.assertIsInstance(daemon.strategy, FunctionStrategy)
        self.assertIsInstance(daemon.backend, ProcessBackend)
        self.assertIsInstance(daemon.loop, AgentLoop)
        # The loop got the SAME objects, not fresh ones.
        self.assertIs(daemon.loop.session, daemon.session)
        self.assertIs(daemon.loop.sink, daemon.sink)
        self.assertIs(daemon.loop.backend, daemon.backend)

    def test_build_refuses_without_the_azure_names_and_never_prints_a_value(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg", return_value=""), \
             mock.patch.dict(os.environ, {}, clear=True):
            daemon = self._daemon()
            with self.assertRaises(MissingCredentials) as caught:
                daemon.build()
        self.assertIn("AZURE_OPENAI_ENDPOINT", str(caught.exception))

    def test_build_publishes_a_status_the_statusline_can_read(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": self.values.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            daemon = self._daemon()
            daemon.build()
            self.addCleanup(daemon.ledger.close)
        data = Status.read(self.dir / "status.json")
        self.assertEqual(data["phase"], "built")
        self.assertEqual(data["session"]["id"], "sess-1")

    def test_every_injected_seam_is_satisfied_by_the_real_class(self):
        """Each component is injected and tested against fakes; the daemon wires the real
        ones. Three live launches each died at a method only a fake had (registry lookup,
        relay qualification, `Ledger.append`). This reads every `self.<seam>.<method>`
        the callers use and asserts the class the daemon injects has it."""
        import inspect
        import re

        from voice.agent import ledger as ledger_mod
        from voice.agent import loop as loop_mod
        from voice.audio import io as audio_io
        from voice.backend.claude_code import adapter, pane, relay, transcript
        from voice.live import realtime

        pairs = [
            (adapter, "_pane", pane.Pane), (adapter, "_tailer", transcript.Tailer),
            (adapter, "_relay", relay.RelayActuator), (relay, "_ledger", ledger_mod.Ledger),
            (loop_mod, "backend", adapter.ClaudeCodeBackend),
            (loop_mod, "ledger", ledger_mod.Ledger),
            (loop_mod, "session", realtime.RealtimeSession),
            (daemon_mod, "session", realtime.RealtimeSession),
            (daemon_mod, "sink", audio_io.PlaybackSink),
        ]
        missing = {}
        for mod, attr, cls in pairs:
            used = set(re.findall(rf"self\.{attr}\.([A-Za-z_]+)", inspect.getsource(mod)))
            gap = sorted(m for m in used if not hasattr(cls, m))
            if gap:
                missing[f"{mod.__name__}.{attr} -> {cls.__name__}"] = gap
        self.assertEqual(missing, {})

    def test_the_ledger_vocabulary_is_exactly_what_the_loop_records(self):
        """Consent is the broker's deterministic gate, never a ledger op the model names —
        and the vocabulary is read off the loop's own `record_op` calls, so a kind the loop
        writes can never be one the ledger refuses (measured live: a restated
        `{request, answer, cancel}` refused `send` and killed the daemon on the first
        real dispatch). Equality, not subset: a declared kind nobody writes is drift too."""
        import ast
        import inspect

        from voice.agent import loop as loop_mod

        vocabulary = daemon_mod.effect_vocabulary()
        recorded = set()
        for node in ast.walk(ast.parse(inspect.getsource(loop_mod))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "record_op" and len(node.args) >= 4
                    and isinstance(node.args[3], ast.Constant)):
                recorded.add(node.args[3].value)
        self.assertTrue(recorded, "no record_op literal found — the probe is broken")
        self.assertEqual(recorded, set(vocabulary))
        self.assertNotIn("confirm_effect", vocabulary)
        self.assertNotIn("propose_effect", vocabulary)

    def test_audio_lock_refusal_is_reported_not_raised(self):
        class Busy:
            def acquire(self, timeout=0.0):
                return False

            def release(self):
                pass

        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": self.values.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            daemon = self._daemon(audio_lock=Busy())
            daemon.build()
            self.addCleanup(daemon.ledger.close)
            self.assertFalse(daemon.acquire_audio())
        data = Status.read(self.dir / "status.json")
        self.assertFalse(data["audio_ready"])
        self.assertIn("busy", data["audio_error"])


class LifecycleBoundsAreTheOnlyNumbers(unittest.TestCase):
    """DESIGN.md §Timers: the daemon may declare connection and lock bounds, nothing else."""

    def test_no_sleep_in_the_app_modules(self):
        from voice.app import daemon

        source = Path(daemon.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sleep(", source, "voice.app.daemon must not sleep")

    def test_the_declared_bounds_are_named_lifecycle_ports(self):
        for name in ("CONNECT_BOUND_S", "CLOSE_BOUND_S", "RECONNECT_BOUND_S",
                     "AUDIO_LOCK_BOUND_S", "AUDIO_LOCK_POLL_S", "PANE_READ_BOUND_S",
                     "RELAY_CONNECT_BOUND_S", "PS_PROBE_BOUND_S",
                     "PS_START_GRANULARITY_S"):
            self.assertIsInstance(getattr(daemon_mod, name), float)


class CommandLine(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ,
                                   {"VOICE_LISTEN_STATE_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_parser_accepts_every_documented_command(self):
        parser = daemon_mod.build_parser()
        for command in ("start", "status", "stop", "preflight"):
            self.assertEqual(parser.parse_args([command]).command, command)

    def test_there_is_no_operator_mute(self):
        """On, status, off — as in Codex. A mute the operator forgets is a call that looks
        live and hears nothing; the provider's own input mute is a wire capability, not this."""
        import io
        from contextlib import redirect_stderr

        parser = daemon_mod.build_parser()
        for command in ("mute", "unmute"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                parser.parse_args([command])
        self.assertEqual(daemon_mod.CONTROL_COMMANDS, ("stop",))
        self.assertNotIn("muted", Status(Path(self.tmp.name) / "s.json", session_id="x").data)
        from voice.audio.io import Capture
        self.assertFalse(hasattr(Capture, "mute") or hasattr(Capture, "unmute"))

    def test_status_of_an_unknown_session_reports_absent_and_exits_nonzero(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = daemon_mod.main(["--session", "nobody", "status"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buffer.getvalue())["phase"], "absent")

    def test_status_prints_the_published_snapshot(self):
        import io
        from contextlib import redirect_stdout

        status = Status(state_dir("sess-9") / "status.json", session_id="sess-9")
        status.set(phase="running")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = daemon_mod.main(["--session", "sess-9", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["phase"], "running")

    def test_stop_writes_a_control_file_rather_than_touching_the_device(self):
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            code = daemon_mod.main(["--session", "sess-9", "stop"])
        self.assertEqual(code, 0)
        control = json.loads((state_dir("sess-9") / "control.json").read_text())
        self.assertEqual(control["command"], "stop")

    def _start(self, session: str, on_start=None):
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with mock.patch.object(daemon_mod, "preflight"), \
             mock.patch.object(daemon_mod, "_handshake",
                               new=mock.AsyncMock(return_value={})) as handshake, \
             mock.patch.object(daemon_mod, "VoiceDaemon") as built, redirect_stderr(err):
            built.return_value.start = mock.AsyncMock(side_effect=on_start)
            code = daemon_mod.main(["--session", session, "--backend", "claude_code",
                                    "--nonce", "abcdef12", "start"])
        return code, err.getvalue(), handshake, built

    def test_a_second_start_in_a_live_session_is_refused_and_touches_nothing(self):
        live = Status(state_dir("sess-9") / "status.json", session_id="sess-9")
        live.set(phase="running", relay="live")
        before = (state_dir("sess-9") / "status.json").read_bytes()
        held = daemon_mod.claim_session("sess-9")       # the running call's claim
        self.addCleanup(held.release)
        code, err, handshake, built = self._start("sess-9")
        self.assertEqual(code, 1)
        self.assertIn("already running (or still ending) for this", err)
        handshake.assert_not_called()                    # its nonce is not spent either
        built.assert_not_called()
        self.assertEqual((state_dir("sess-9") / "status.json").read_bytes(), before)
        self.assertFalse((state_dir("sess-9") / "control.json").exists())

    def _spied_claims(self):
        real, claims = daemon_mod.claim_session, []

        def spy(session_id):
            lock = real(session_id)
            claims.append(mock.patch.object(lock, "release", wraps=lock.release).start())
            return lock
        self.addCleanup(mock.patch.stopall)
        return mock.patch.object(daemon_mod, "claim_session", side_effect=spy), claims

    def test_the_claim_is_per_session_and_released_when_the_call_ends(self):
        other = daemon_mod.claim_session("sess-other")
        self.addCleanup(other.release)
        patch, claims = self._spied_claims()
        held_during_call = []
        with patch:
            code, _, handshake, built = self._start(
                "sess-9", on_start=lambda: held_during_call.append(not claims[0].called))
        self.assertEqual(held_during_call, [True])       # still held while the call runs
        self.assertEqual(code, 0)
        handshake.assert_awaited_once()
        built.return_value.start.assert_awaited_once()
        self.assertEqual(len(claims), 1)
        claims[0].assert_called_once_with()             # released explicitly, not by GC

    def test_a_failed_handshake_releases_the_claim(self):
        import io
        from contextlib import redirect_stderr

        patch, claims = self._spied_claims()
        with patch, mock.patch.object(daemon_mod, "preflight"), \
             mock.patch.object(daemon_mod, "_handshake",
                               new=mock.AsyncMock(side_effect=RuntimeError("refused"))), \
             redirect_stderr(io.StringIO()):
            code = daemon_mod.main(["--session", "sess-9", "--backend", "claude_code",
                                    "--nonce", "abcdef12", "start"])
        self.assertEqual(code, 1)
        claims[0].assert_called_once_with()


if __name__ == "__main__":
    unittest.main()


async def _settle() -> None:
    """Let the loop run its reader callback and the task that callback creates.

    The watcher is deliberately event-driven, so there is nothing to wait ON — a few
    yields is what "the loop got a turn" looks like from a test. No sleep: a duration here
    would be a timer in the test for a mechanism that has none.
    """
    for _ in range(6):
        await asyncio.sleep(0)


class ControlChannel(unittest.IsolatedAsyncioTestCase):
    """`stop` arrives as a directory event, never on an interval."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    class FakeWatch:
        """A watch whose event the test fires by hand."""

        def __init__(self):
            import os as _os

            self.read_fd, self.write_fd = _os.pipe()
            self.closed = False

        def fire(self):
            os.write(self.write_fd, b"x")

        def fileno(self):
            return self.read_fd

        def drain(self):
            os.read(self.read_fd, 64)

        def close(self):
            self.closed = True
            os.close(self.read_fd)
            os.close(self.write_fd)

    async def _watcher(self, seen):
        watch = self.FakeWatch()
        self.addCleanup(lambda: None if watch.closed else watch.close())

        async def on_command(command):
            seen.append(command)

        controller = daemon_mod.ControlWatcher(self.dir, on_command,
                                               open_watch=lambda d: watch)
        controller.start(asyncio.get_running_loop())
        return controller, watch

    async def test_a_command_left_on_disk_before_start_never_fires(self):
        """A `stop` from a previous run\'s shutdown must not tear the next daemon down at
        startup. `start` adopts the on-disk `at` as already-seen (measured live: a stale stop
        replayed and closed the ledger under the qualification probe, three launches running)."""
        atomic_write(self.dir / "control.json", {"command": "stop", "at": 111.0})
        seen = []
        controller, watch = await self._watcher(seen)
        self.addCleanup(controller.close)
        watch.fire()
        for _ in range(4):
            await asyncio.sleep(0)
        self.assertEqual(seen, [], "a pre-start command is history, not an instruction")
        # A NEW command (later `at`) after start still fires.
        atomic_write(self.dir / "control.json", {"command": "stop", "at": 222.0})
        watch.fire()
        for _ in range(4):
            await asyncio.sleep(0)
        self.assertEqual(seen, ["stop"])

    async def test_a_written_command_reaches_the_callback(self):
        seen = []
        controller, watch = await self._watcher(seen)
        try:
            daemon_mod.atomic_write(self.dir / daemon_mod.CONTROL_NAME,
                                    {"command": "stop", "at": 1.0})
            watch.fire()
            await _settle()
        finally:
            controller.close()
        self.assertEqual(seen, ["stop"])

    async def test_each_command_is_consumed_once_however_often_the_event_fires(self):
        """A directory event can fire more than once per write; replaying `stop` would
        shut the session down a second time."""
        seen = []
        controller, watch = await self._watcher(seen)
        try:
            daemon_mod.atomic_write(self.dir / daemon_mod.CONTROL_NAME,
                                    {"command": "stop", "at": 5.0})
            for _ in range(3):
                watch.fire()
                await _settle()
        finally:
            controller.close()
        self.assertEqual(seen, ["stop"])

    async def test_a_new_command_after_the_first_is_delivered(self):
        seen = []
        controller, watch = await self._watcher(seen)
        try:
            for at, command in ((1.0, "stop"), (2.0, "stop")):
                daemon_mod.atomic_write(self.dir / daemon_mod.CONTROL_NAME,
                                        {"command": command, "at": at})
                watch.fire()
                await _settle()
        finally:
            controller.close()
        self.assertEqual(seen, ["stop", "stop"])

    def test_an_unknown_command_is_ignored(self):
        daemon_mod.atomic_write(self.dir / daemon_mod.CONTROL_NAME,
                                {"command": "self_destruct", "at": 1.0})
        controller = daemon_mod.ControlWatcher(self.dir, None)
        self.assertIsNone(controller.read_command())

    def test_a_mute_left_by_an_older_cli_is_ignored(self):
        daemon_mod.atomic_write(self.dir / daemon_mod.CONTROL_NAME,
                                {"command": "mute", "at": 1.0})
        self.assertIsNone(daemon_mod.ControlWatcher(self.dir, None).read_command())

    def test_a_corrupt_control_file_is_ignored(self):
        (self.dir / daemon_mod.CONTROL_NAME).write_text("{not json", encoding="utf-8")
        controller = daemon_mod.ControlWatcher(self.dir, None)
        self.assertIsNone(controller.read_command())

    def test_an_absent_control_file_is_ignored(self):
        controller = daemon_mod.ControlWatcher(self.dir, None)
        self.assertIsNone(controller.read_command())

    async def test_a_platform_without_kqueue_degrades_without_raising(self):
        """Linux and Windows have no kqueue. The CLI still records; only the live
        reaction is lost, and that must not take the daemon down with it."""
        seen = []

        async def on_command(command):
            seen.append(command)

        controller = daemon_mod.ControlWatcher(self.dir, on_command,
                                               open_watch=lambda d: None)
        controller.start(asyncio.get_running_loop())
        controller.close()

    async def test_the_commands_route_to_the_same_host_callbacks_the_model_reaches(self):
        values = {"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com",
                  "AZURE_OPENAI_API_KEY": "k", "VOICE_LIVE_API_VERSION": "2026-07-15",
                  "VOICE_PROCESS_ARGV": "true"}
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            daemon = VoiceDaemon(session_id="s", provider="voice_live",
                                 backend_name="process", state=self.dir,
                                 connect=FakeSocket.connector([]),
                                 open_output=lambda: FakeStream())
            daemon.build()
            self.addCleanup(daemon.ledger.close)
            daemon.capture = mock.Mock(close=lambda: None)
            await daemon.on_control("mute")                # not a command: nothing happens
            self.assertNotEqual(Status.read(self.dir / "status.json")["phase"], "ended")
            await daemon.on_control("stop")
        self.assertEqual(Status.read(self.dir / "status.json")["ended"]["reason"],
                         "stopped by control")

    async def test_stop_ends_the_session_once(self):
        values = {"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com",
                  "AZURE_OPENAI_API_KEY": "k", "VOICE_LIVE_API_VERSION": "2026-07-15",
                  "VOICE_PROCESS_ARGV": "true"}
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            daemon = VoiceDaemon(session_id="s", provider="voice_live",
                                 backend_name="process", state=self.dir,
                                 connect=FakeSocket.connector([]),
                                 open_output=lambda: FakeStream())
            daemon.build()
            self.addCleanup(daemon.ledger.close)
            await daemon.on_control("stop")
            first = Status.read(self.dir / "status.json")["ended"]
            await daemon.on_control("stop")     # idempotent
            second = Status.read(self.dir / "status.json")["ended"]
        self.assertEqual(first["reason"], "stopped by control")
        self.assertEqual(first, second, "a second stop must not re-end the session")


class PermissionChannel(unittest.IsolatedAsyncioTestCase):
    """permission.json: what the plugin's hooks module writes when a permission prompt opens
    while the call is live. Read once per prompt, never a previous call's, never half a file."""

    SINCE = 1000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / daemon_mod.PERMISSION_NAME
        self.seen: list[dict] = []

    def _write(self, payload) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.path.write_text(text, encoding="utf-8")

    async def _watcher(self):
        """Fake watches, one per path, fired by hand (ControlChannel.FakeWatch)."""
        watches: dict[Path, ControlChannel.FakeWatch] = {}

        def open_watch(path):
            watches[Path(path)] = ControlChannel.FakeWatch()
            return watches[Path(path)]

        async def on_command(command):
            pass

        controller = daemon_mod.ControlWatcher(self.dir, on_command, open_watch=open_watch,
                                               on_permission=self.seen.append,
                                               since=self.SINCE)
        controller.start(asyncio.get_running_loop())

        def cleanup():
            controller.close()
            for watch in watches.values():
                if not watch.closed:
                    watch.close()
        self.addCleanup(cleanup)
        return controller, watches

    async def test_a_report_is_read_once_and_a_new_one_after_it(self):
        _controller, watches = await self._watcher()
        self._write({"at": 1001.5, "tool": "Bash", "summary": "touch hello.txt"})
        for _ in range(3):                         # a directory event can fire repeatedly
            watches[self.dir].fire()
            await _settle()
        self.assertEqual(self.seen, [{"at": 1001.5, "tool": "Bash",
                                      "summary": "touch hello.txt"}])
        # Rewritten in place: only the FILE's watch sees it (kqueue on a directory does not).
        self._write({"at": 1002.0, "tool": "Edit", "summary": "/repo/a.py"})
        watches[self.path].fire()
        await _settle()
        self.assertEqual([e["tool"] for e in self.seen], ["Bash", "Edit"])

    async def test_a_report_older_than_the_call_is_ignored(self):
        self._write({"at": self.SINCE - 60, "tool": "Bash", "summary": "rm -rf build/"})
        _controller, watches = await self._watcher()
        watches[self.dir].fire()
        await _settle()
        self.assertEqual(self.seen, [])
        self.assertIn(self.path, watches, "an existing file is watched from the start")

    async def test_a_half_written_report_is_read_again_when_the_write_finishes(self):
        _controller, watches = await self._watcher()
        self._write('{"at": 1003.0, "tool": "Ba')
        watches[self.dir].fire()
        await _settle()
        self.assertEqual(self.seen, [])
        self._write({"at": 1003.0, "tool": "Bash", "summary": "make"})
        watches[self.path].fire()
        await _settle()
        self.assertEqual(self.seen, [{"at": 1003.0, "tool": "Bash", "summary": "make"}])

    def test_bad_reports_are_ignored(self):
        controller = daemon_mod.ControlWatcher(self.dir, None, on_permission=self.seen.append,
                                               since=self.SINCE)
        for bad in ("", "[]", "null", '{"tool": "Bash"}', '{"at": "1001", "tool": "Bash"}',
                    '{"at": true, "tool": "Bash"}', '{"at": NaN, "tool": "Bash"}',
                    '{"at": 1001, "tool": "  "}', '{"at": 1001, "tool": 7}'):
            self._write(bad)
            self.assertIsNone(controller.read_permission(), bad)
        self.path.unlink()
        self.assertIsNone(controller.read_permission())

    def test_the_text_is_one_short_line(self):
        controller = daemon_mod.ControlWatcher(self.dir, None, since=self.SINCE)
        self._write({"at": 1004, "tool": "Bash\n", "summary": "echo a\n\techo b\x1b[2J" + "x" * 900})
        event = controller.read_permission()
        self.assertEqual(event["tool"], "Bash")
        self.assertTrue(event["summary"].startswith("echo a echo b [2J"))
        self.assertEqual(len(event["summary"]), daemon_mod.PERMISSION_TEXT_MAX)
        self.assertEqual(event["at"], 1004.0)

    def test_a_credential_across_the_cut_is_masked_whole_first(self):
        """Masking matches whole known values, so a cut made first could leave a credential's
        prefix it no longer recognises. The text is masked whole, then cut."""
        secret = "sk-test-SECRET-0123456789abcdef"
        controller = daemon_mod.ControlWatcher(self.dir, None, since=self.SINCE)
        with mock.patch.dict(os.environ, {"VOICE_TEST_API_KEY": secret}):
            for cut in (daemon_mod.PERMISSION_TEXT_MAX, 300):   # the cut now, and the old one
                summary = "x" * (cut - 10) + secret + " https://example.com"   # across the cut
                self._write({"at": 1010 + cut, "tool": "Bash", "summary": summary})
                event = controller.read_permission()
                self.assertNotIn(secret[:6], event["summary"], cut)
                self.assertLessEqual(len(event["summary"]), daemon_mod.PERMISSION_TEXT_MAX)

    def test_a_summary_past_the_limit_is_dropped_whole(self):
        controller = daemon_mod.ControlWatcher(self.dir, None, since=self.SINCE)
        self._write({"at": 1011, "tool": "Bash",
                     "summary": "y" * (daemon_mod.PERMISSION_SUMMARY_LIMIT + 1)})
        self.assertEqual(controller.read_permission(), {"at": 1011.0, "tool": "Bash", "summary": ""})

    def test_a_notice_for_another_call_instance_is_ignored(self):
        """A hook can finish writing after a new call began in the same session: the notice
        carries the status `instance` it saw, and only this call's is read."""
        controller = daemon_mod.ControlWatcher(self.dir, None, since=self.SINCE,
                                               instance="call-2")
        for other in ({"instance": "call-1"}, {}, {"instance": None}):
            self._write({"at": 1012, "tool": "Bash", "summary": "ls", **other})
            self.assertIsNone(controller.read_permission(), other)
        self._write({"at": 1012, "tool": "Bash", "summary": "ls", "instance": "call-2"})
        self.assertEqual(controller.read_permission()["summary"], "ls")

    async def test_without_a_permission_reader_the_file_is_never_watched(self):
        self._write({"at": 1005, "tool": "Bash", "summary": "x"})
        opened = []

        def open_watch(path):
            opened.append(Path(path))
            return ControlChannel.FakeWatch()

        async def on_command(command):
            pass

        controller = daemon_mod.ControlWatcher(self.dir, on_command, open_watch=open_watch)
        controller.start(asyncio.get_running_loop())
        controller.close()
        self.assertEqual(opened, [self.dir])

    async def test_a_real_watch_sees_a_rewrite_in_place(self):
        """The hooks module's `$.fs.write` truncates and writes the same inode (measured). On
        macOS that is invisible to the directory's kqueue; the file's own watch must see it."""
        from voice import platform as voice_platform

        controller = daemon_mod.ControlWatcher(
            self.dir, None, on_permission=self.seen.append, since=self.SINCE,
            open_watch=lambda p: voice_platform.watch(p, poll_s=0.01))
        controller.start(asyncio.get_running_loop())
        self.addCleanup(controller.close)

        async def until(count):
            for _ in range(200):
                if len(self.seen) >= count:
                    return
                await asyncio.sleep(0.01)

        self._write({"at": 1006.0, "tool": "Bash", "summary": "first"})
        await until(1)
        inode = self.path.stat().st_ino
        with open(self.path, "r+", encoding="utf-8") as handle:     # in place, same inode
            handle.truncate(0)
            handle.write(json.dumps({"at": 1007.0, "tool": "Bash", "summary": "second"}))
        self.assertEqual(self.path.stat().st_ino, inode)
        await until(2)
        self.assertEqual([e["summary"] for e in self.seen], ["first", "second"])

    async def test_the_daemon_hands_a_report_to_a_backend_that_can_announce_it(self):
        announced = []

        class Announcing:
            async def announce_permission(self, *, tool, summary, at):
                announced.append((tool, summary, at))
                return True

        daemon = VoiceDaemon(session_id="s", provider="voice_live", backend_name="process",
                             state=self.dir)
        daemon.backend = Announcing()
        daemon.on_permission({"at": 1008.0, "tool": "Bash", "summary": "ls"})
        await _settle()
        self.assertEqual(announced, [("Bash", "ls", 1008.0)])
        daemon.backend = object()                    # the process backend: nothing to say
        daemon.on_permission({"at": 1009.0, "tool": "Bash", "summary": "ls"})
        await _settle()
        self.assertEqual(len(announced), 1)


class StatusHeartbeat(unittest.IsolatedAsyncioTestCase):
    """While the call runs, `at` is refreshed every STATUS_HEARTBEAT_S, so a status a killed
    process left behind reads as stale (the hooks module trusts `at` for 30 s)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _daemon(self, clock):
        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.status = Status(self.dir / "status.json", session_id="s", now=lambda: clock[0])
        daemon.status.set(phase="running", relay="qualified")
        return daemon

    async def test_at_is_refreshed_every_period_on_a_fake_clock(self):
        clock = [1000.0]
        daemon = self._daemon(clock)
        waits, seen_at = [], []

        async def fake_wait_for(awaitable, timeout):
            awaitable.close()                       # the stop event's wait, never started
            waits.append(timeout)
            seen_at.append(Status.read(self.dir / "status.json")["at"])
            clock[0] += timeout
            if len(waits) == 4:
                daemon._shut = True                 # the ending starts during the 4th wait
            raise asyncio.TimeoutError

        await daemon._heartbeat(daemon_mod.STATUS_HEARTBEAT_S, wait_for=fake_wait_for)
        self.assertEqual(waits, [daemon_mod.STATUS_HEARTBEAT_S] * 4)
        self.assertEqual(seen_at, [1000.0, 1010.0, 1020.0, 1030.0])
        final = Status.read(self.dir / "status.json")
        self.assertEqual(final["at"], 1030.0, "no beat once the ending started")
        self.assertEqual((final["phase"], final["relay"]), ("running", "qualified"))

    async def test_the_ending_stops_it_at_once(self):
        daemon = self._daemon([1000.0])
        task = asyncio.ensure_future(daemon._heartbeat(3600.0))
        await asyncio.sleep(0)
        daemon._shut = True
        daemon._stopping().set()
        await asyncio.wait_for(task, 1.0)
        self.assertEqual(Status.read(self.dir / "status.json")["at"], 1000.0)

    async def test_the_run_beats_with_the_declared_cadence(self):
        daemon = _idle_daemon(_IdleLoop(), _Backend("idle"))
        beats = []

        async def heartbeat(every):
            beats.append(every)
            await asyncio.Event().wait()

        daemon._heartbeat = heartbeat
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(beats, [daemon_mod.STATUS_HEARTBEAT_S])

    async def test_its_own_writes_wake_the_watcher_into_no_work(self):
        """The heartbeat writes into the directory the control watcher watches. Each wake must
        dedupe the control and permission files it already read, and nothing may loop."""
        from voice import platform as voice_platform

        commands, reports, wakes = [], [], []

        async def on_command(command):
            commands.append(command)

        controller = daemon_mod.ControlWatcher(
            self.dir, on_command, on_permission=reports.append, since=0.0,
            open_watch=lambda p: voice_platform.watch(p, poll_s=0.01))
        real_wake = controller._wake

        def counting_wake(drained=False):
            wakes.append(1)
            real_wake(drained)

        controller._wake = counting_wake
        controller.start(asyncio.get_running_loop())
        self.addCleanup(controller.close)

        async def settle_until(predicate):
            for _ in range(200):
                if predicate():
                    return
                await asyncio.sleep(0.01)

        atomic_write(self.dir / daemon_mod.CONTROL_NAME, {"command": "stop", "at": 5.0})
        (self.dir / daemon_mod.PERMISSION_NAME).write_text(
            json.dumps({"at": 6.0, "tool": "Bash", "summary": "ls"}), encoding="utf-8")
        await settle_until(lambda: commands and reports)
        self.assertEqual((commands, len(reports)), (["stop"], 1))

        before = len(wakes)
        clock = [100.0]
        status = Status(self.dir / "status.json", session_id="s", now=lambda: clock[0])
        for _ in range(5):                                   # five beats
            clock[0] += 10
            status.set()
            await asyncio.sleep(0.03)
        await settle_until(lambda: len(wakes) > before)
        self.assertGreater(len(wakes), before, "the beats do wake the directory watch")
        settled = len(wakes)
        await asyncio.sleep(0.2)
        self.assertEqual(len(wakes), settled, "nothing loops once the beats stop")
        self.assertEqual((commands, len(reports)), (["stop"], 1), "deduped: no repeat work")


class ClaudeCodeWiring(unittest.IsolatedAsyncioTestCase):
    """The real graph, from a fake binding and a fake terminal reader.

    Nothing here shells out: the pane's `run` is injected, the relay's socket is never
    opened (only the constructor is exercised), and the transcript is a temp file.
    """

    SESSION = "11111111-2222-3333-4444-555555555555"
    OWNER = {"pid": 4242, "start": "Sat Sep  5 23:30:58 2026", "tty": "ttys023",
             "comm": "claude"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.transcript = self.dir / "transcript.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        # claude_code runs on macOS only (registry capability); these cases exercise its
        # binding checks, so they run as on macOS whatever OS runs the suite. The refusal
        # everywhere else is `test_off_macos_the_daemon_refuses_before_any_binding_check`.
        pin = mock.patch.object(daemon_mod.voice_platform, "PLATFORM", "darwin")
        pin.start()
        self.addCleanup(pin.stop)
        self.binding = {
            "bound": True, "refusal": None, "session_id": self.SESSION,
            "handle": "orca-handle-1", "owner": dict(self.OWNER),
            "claude": {"pid": 4242, "session_id": self.SESSION, "cwd": "/tmp/wt",
                       "proc_start": "Sat Sep  5 23:30:58 2026", "start_epoch": 1.0},
        }

    class FakePane:
        terminal = "orca-handle-1"

    def _relay_env(self):
        """A socket path and token, as `relay_env` would read them. Never printed."""
        return mock.patch.dict(os.environ, {"CLAUDE_MESSAGING_SOCKET": str(self.dir / "s.sock"),
                                            "CLAUDE_MESSAGING_TOKEN": "tok-secret"})

    @unittest.skipUnless(sys.platform == "darwin",
                         "the claude_code relay binds an Orca pane through kqueue: macOS only")
    def test_the_graph_builds_with_every_port_reaching_its_constructor(self):
        from voice.backend.claude_code import relay as relay_mod

        with self._relay_env(), \
             mock.patch.object(relay_mod, "RelayActuator") as actuator, \
             mock.patch.object(relay_mod, "relay_env",
                               return_value=(str(self.dir / "s.sock"), "tok-secret")):
            backend = daemon_mod.build_claude_code_backend(
                binding=self.binding, session_id=self.SESSION, ledger="LEDGER",
                pane=self.FakePane(), transcript=self.transcript,
                connect_bound_s=3.0, start_granularity_s=1.0, now=lambda: 1000.0)
        kwargs = actuator.call_args.kwargs
        # THE FOUR INJECTED PORTS reach the constructors, not module defaults.
        self.assertEqual(kwargs["connect_timeout"], 3.0)
        self.assertEqual(kwargs["start_granularity"], 1.0)
        self.assertEqual(kwargs["now"](), 1000.0)
        self.assertEqual(kwargs["ledger"], "LEDGER")
        self.assertEqual(kwargs["producer"]["session_id"], self.SESSION)
        self.assertEqual(kwargs["producer"]["pid"], os.getpid())
        self.assertIsNotNone(backend)

    @unittest.skipUnless(sys.platform == "darwin",
                         "the claude_code relay binds an Orca pane through kqueue: macOS only")
    def test_the_screen_handle_rides_under_observe_handle_never_as_handle(self):
        """The WAL binds effects to `handle or socket`; a screen handle there would
        re-attribute every posted frame to a terminal it was never sent through."""
        from voice.backend.claude_code import relay as relay_mod

        with self._relay_env(), \
             mock.patch.object(relay_mod, "RelayActuator") as actuator, \
             mock.patch.object(relay_mod, "relay_env",
                               return_value=(str(self.dir / "s.sock"), "tok-secret")):
            daemon_mod.build_claude_code_backend(
                binding=self.binding, session_id=self.SESSION, ledger=None,
                pane=self.FakePane(), transcript=self.transcript)
        bound = actuator.call_args.kwargs["binding"]
        self.assertEqual(bound["observe_handle"], "orca-handle-1")
        self.assertEqual(bound["socket"], str(self.dir / "s.sock"))

    def test_a_missing_relay_socket_refuses_with_the_reason_only(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                daemon_mod.build_claude_code_backend(
                    binding=self.binding, session_id=self.SESSION, ledger=None,
                    pane=self.FakePane(), transcript=self.transcript)
        message = str(caught.exception)
        self.assertIn("refused", message)
        self.assertNotIn("tok-secret", message, "the token must never reach a message")

    async def test_the_pane_receives_the_daemons_read_bound_and_granularity(self):
        """The other half of the port check: `Pane` declares no defaults either."""
        made = mock.Mock()
        made.bind = mock.AsyncMock(return_value={"bound": True})
        with mock.patch("voice.backend.claude_code.pane.Pane",
                        return_value=made) as pane:
            await daemon_mod.prove_ownership(
                nonce="abc123def", session_id=self.SESSION, terminal="h",
                claude_pid=4242, run=None,
                read_bound_s=daemon_mod.PANE_READ_BOUND_S,
                start_granularity_s=daemon_mod.PS_START_GRANULARITY_S,
                now=lambda: 7.0)
        kwargs = pane.call_args.kwargs
        self.assertEqual(kwargs["read_timeout"], 5.0)
        self.assertEqual(kwargs["start_granularity"], 1.0)
        self.assertEqual(kwargs["ps_timeout"], daemon_mod.PS_PROBE_BOUND_S)
        self.assertEqual(kwargs["now"](), 7.0)

    def test_one_granularity_injection_reaches_every_identity_check(self):
        """`Pane` forwards it to `registry_claims`; `RelayActuator` forwards it to
        `relay_identity_still_holds`, which forwards to `registry_matches`. Widening it
        anywhere reopens the pid-reuse window, so there is exactly one source."""
        import inspect

        from voice.backend.claude_code.pane import (Pane, registry_claims,
                                                    registry_matches)
        from voice.backend.claude_code.relay import (RelayActuator,
                                                     relay_identity_still_holds)

        for func in (Pane.__init__, RelayActuator.__init__, registry_claims,
                     registry_matches, relay_identity_still_holds):
            parameter = inspect.signature(func).parameters["start_granularity"]
            self.assertIs(parameter.default, inspect.Parameter.empty,
                          f"{func.__qualname__} must not default the granularity")

    async def test_zero_nonce_hits_refuses_with_bind_unproven(self):
        """The hard refusal: a handle pointing at another pane cannot show a command that
        pane is not running, and there is no retry and no typing the nonce in."""
        tail = ["  ⎿  $ something-else --flag", "     still the other command"]

        async def run(argv, timeout):
            # The envelope orca actually returns: the tail nests under result.terminal.
            return {"ok": True, "returncode": 0,
                    "stdout": json.dumps({"result": {"terminal": {"tail": tail}}})}

        with mock.patch("voice.backend.claude_code.pane.owner_fingerprint",
                        return_value=dict(self.OWNER)), \
             mock.patch("voice.backend.claude_code.pane.registry_claims",
                        return_value=({"pid": 4242}, None)):
            proof = await daemon_mod.prove_ownership(
                nonce="abc123def", session_id=self.SESSION, terminal="orca-handle-1",
                claude_pid=4242, run=run, read_bound_s=8.0)
        self.assertFalse(proof["binding"]["bound"])
        self.assertEqual(proof["binding"]["refusal"], daemon_mod.REFUSE_UNPROVEN)

    async def test_prove_ownership_supplies_the_registry_to_the_real_bind(self):
        """Measured 2026-09-20: `bind` defaults `registry=None` and never fetches it, so the
        one production caller omitting it refused every real handshake with
        `registry_missing` while nine tests injected the record by hand. This drives the REAL
        `Pane.bind` — only the launcher actually fetching the record can pass it."""
        tail = ["  ⎿  $ abc123def uv run python -m voice.app.daemon start"]

        async def run(argv, timeout):
            return {"ok": True, "returncode": 0,
                    "stdout": json.dumps({"result": {"terminal": {"tail": tail}}})}

        # `procStart` is the SAME instant as the ps fingerprint, rendered as UTC ctime.
        from voice.backend.claude_code import pane as paneg
        from voice.backend.claude_code.pane import lstart_epoch
        record = {"pid": 4242, "sessionId": self.SESSION, "cwd": "/w",
                  "procStart": time.strftime(
                      paneg._CTIME, time.gmtime(lstart_epoch(self.OWNER["start"])))}
        with mock.patch("voice.backend.claude_code.pane.owner_fingerprint",
                        return_value=dict(self.OWNER)), \
             mock.patch("voice.backend.claude_code.pane.session_registry",
                        return_value=record) as lookup:
            proof = await daemon_mod.prove_ownership(
                nonce="abc123def", session_id=self.SESSION, terminal="orca-handle-1",
                claude_pid=4242, run=run, read_bound_s=8.0)
        lookup.assert_called_once_with(4242)
        binding = proof["binding"]
        self.assertTrue(binding["bound"], binding.get("detail") or binding.get("refusal"))

    async def test_the_daemon_refuses_to_build_on_an_unproven_binding(self):
        daemon = VoiceDaemon(session_id=self.SESSION, provider="voice_live",
                             backend_name="claude_code", state=self.dir,
                             claude_code={"binding": {"bound": False,
                                                      "refusal": "bind_unproven"},
                                          "pane": self.FakePane(),
                                          "transcript": self.transcript})
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": {"AZURE_OPENAI_ENDPOINT": "https://x",
                                                            "AZURE_OPENAI_API_KEY": "k"}.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            with self.assertRaises(RuntimeError) as caught:
                daemon.build()
        self.assertIn("bind_unproven", str(caught.exception))

    async def test_off_macos_the_daemon_refuses_before_any_binding_check(self):
        from voice.backend.registry import Unsupported

        for platform in ("linux", "windows"):
            with self.subTest(platform=platform):
                daemon = VoiceDaemon(session_id=self.SESSION, provider="voice_live",
                                     backend_name="claude_code", state=self.dir,
                                     claude_code={"binding": self.binding,
                                                  "pane": self.FakePane(),
                                                  "transcript": str(self.transcript)})
                with mock.patch.object(daemon_mod.voice_platform, "PLATFORM", platform), \
                     mock.patch.object(daemon_mod.voice_config, "cfg",
                                       side_effect=lambda n, d="": {
                                           "AZURE_OPENAI_ENDPOINT": "https://x",
                                           "AZURE_OPENAI_API_KEY": "k"}.get(n, d)), \
                     mock.patch.dict(os.environ, AZURE_ENV):
                    with self.assertRaises(Unsupported) as caught:
                        daemon.build()
                self.assertIn(f"unsupported on {platform}", str(caught.exception))
                self.assertIsNone(daemon.ledger, "refused before the ledger opened")

    async def test_the_daemon_refuses_without_the_handshake_at_all(self):
        daemon = VoiceDaemon(session_id=self.SESSION, provider="voice_live",
                             backend_name="claude_code", state=self.dir)
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": {"AZURE_OPENAI_ENDPOINT": "https://x",
                                                            "AZURE_OPENAI_API_KEY": "k"}.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            with self.assertRaises(RuntimeError) as caught:
                daemon.build()
        self.assertIn("--nonce", str(caught.exception))

    async def test_a_missing_transcript_refuses_by_name(self):
        daemon = VoiceDaemon(session_id=self.SESSION, provider="voice_live",
                             backend_name="claude_code", state=self.dir,
                             claude_code={"binding": self.binding,
                                          "pane": self.FakePane(), "transcript": None})
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": {"AZURE_OPENAI_ENDPOINT": "https://x",
                                                            "AZURE_OPENAI_API_KEY": "k"}.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            with self.assertRaises(RuntimeError) as caught:
                daemon.build()
        self.assertIn(daemon_mod.REFUSE_NO_TRANSCRIPT, str(caught.exception))

    def test_build_backend_no_longer_builds_claude_code_argument_less(self):
        """It needs a proven binding, so the generic path must send the caller elsewhere."""
        with self.assertRaises(RuntimeError) as caught:
            build_backend("claude_code")
        self.assertIn("build_claude_code_backend", str(caught.exception))


class TranscriptResolver(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.projects = self.home / "projects" / "-Users-x-work"
        self.projects.mkdir(parents=True)

    def test_the_session_uuid_is_globbed_across_project_slugs(self):
        session = "11111111-2222-3333-4444-555555555555"
        target = self.projects / f"{session}.jsonl"
        target.write_text("", encoding="utf-8")
        self.assertEqual(daemon_mod.resolve_transcript(session, home=self.home), target)

    def test_an_absent_transcript_is_none_not_a_guess(self):
        self.assertIsNone(daemon_mod.resolve_transcript(
            "99999999-2222-3333-4444-555555555555", home=self.home))

    def test_a_value_that_is_not_a_session_id_never_globs_the_tree(self):
        for bad in ("", "../../etc", "*", "not a uuid at all"):
            self.assertIsNone(daemon_mod.resolve_transcript(bad, home=self.home))

    def test_the_newest_match_wins_when_a_slug_was_renamed(self):
        session = "11111111-2222-3333-4444-555555555555"
        older = self.projects / f"{session}.jsonl"
        older.write_text("", encoding="utf-8")
        os.utime(older, (1000, 1000))
        second = self.home / "projects" / "-Users-x-other"
        second.mkdir(parents=True)
        newer = second / f"{session}.jsonl"
        newer.write_text("", encoding="utf-8")
        os.utime(newer, (2000, 2000))
        self.assertEqual(daemon_mod.resolve_transcript(session, home=self.home), newer)


class ClaudeAncestor(unittest.TestCase):
    """Only a descendant of the operator's Claude process can resolve its pid."""

    SESSION = "11111111-2222-3333-4444-555555555555"

    def test_the_ancestor_naming_this_session_is_found(self):
        records = {900: {"sessionId": self.SESSION}}
        with mock.patch.object(daemon_mod, "_parent_of", side_effect=lambda pid: {
                100: 500, 500: 900}.get(pid, 0)):
            found = daemon_mod.find_claude_ancestor(
                self.SESSION, ppid=lambda: 100, registry=records.get)
        self.assertEqual(found, 900)

    def test_no_registry_record_means_zero_never_a_guess(self):
        with mock.patch.object(daemon_mod, "_parent_of", return_value=0):
            self.assertEqual(
                daemon_mod.find_claude_ancestor(self.SESSION, ppid=lambda: 100,
                                                registry=lambda pid: None), 0)

    def test_a_record_for_another_session_is_not_ours(self):
        records = {900: {"sessionId": "some-other-session"}}
        with mock.patch.object(daemon_mod, "_parent_of", side_effect=lambda pid: {
                100: 900}.get(pid, 0)):
            self.assertEqual(
                daemon_mod.find_claude_ancestor(self.SESSION, ppid=lambda: 100,
                                                registry=records.get), 0)


class LifecycleCues(unittest.IsolatedAsyncioTestCase):
    """The daemon's two sounds: the operator hears the relay come up and the session end
    without a word — so neither can pass for a result (measured live 2026-09-22: a daemon
    that came up in silence was taken for one that had not come up at all)."""

    def setUp(self):
        from voice.audio import cue
        self.cue = cue
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.stream = FakeStream()

    def _built(self):
        values = {"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com",
                  "AZURE_OPENAI_API_KEY": "k", "VOICE_LIVE_API_VERSION": "2026-07-15",
                  "VOICE_PROCESS_ARGV": "true"}
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            daemon = VoiceDaemon(session_id="s", provider="voice_live",
                                 backend_name="process", state=self.dir,
                                 connect=FakeSocket.connector([]),
                                 open_output=lambda: self.stream)
            daemon.build()
        self.addCleanup(daemon.ledger.close)
        # The tones are the subject here; the words at the edges are another class's.
        daemon.loop = mock.Mock(notice_open=mock.AsyncMock(), farewell=mock.AsyncMock())
        return daemon

    def test_the_two_earcons_are_distinct_click_free_pcm(self):
        start, stop = self.cue.earcon(self.cue.START), self.cue.earcon(self.cue.STOP)
        self.assertEqual(len(start), self.cue.CUE_FRAMES * 2, "pcm16 mono, CUE_FRAMES long")
        self.assertEqual(len(stop), len(start))
        self.assertNotEqual(start, stop, "up and down must be told apart by ear")
        for pcm in (start, stop):
            self.assertEqual(pcm[:2], b"\x00\x00", "fades in from silence")
            self.assertEqual(pcm[-2:], b"\x00\x00", "fades out to silence")

    async def test_the_rising_tone_sounds_when_the_relay_qualifies(self):
        daemon = self._built()
        daemon._lock_held = True
        daemon.sink.start()
        daemon._relay_qualified()
        daemon.sink.drain()
        self.assertEqual(Status.read(self.dir / "status.json")["relay"], "qualified")
        self.assertEqual(b"".join(self.stream.writes), self.cue.earcon(self.cue.START))
        daemon.sink.close()

    async def test_the_falling_tone_drains_before_the_speaker_closes(self):
        daemon = self._built()
        daemon._lock_held = True
        daemon.sink.start()
        daemon._lock = mock.Mock()
        await daemon.shutdown("stream ended")
        self.assertEqual(b"".join(self.stream.writes), self.cue.earcon(self.cue.STOP))
        self.assertTrue(self.stream.closed, "the tone went in ahead of the close sentinel")

    async def test_no_tone_when_the_audio_device_was_never_ours(self):
        """The writer IS running here, so an ungated `play` would reach the stream — the
        gate, not an idle queue, is what keeps this silent (Grok review)."""
        daemon = self._built()
        self.assertFalse(daemon._lock_held)
        daemon.sink.start()
        daemon._relay_qualified()
        daemon.sink.drain()
        await daemon.shutdown("audio busy")
        self.assertEqual(self.stream.writes, [])

    async def test_a_cue_is_never_delivery_evidence_for_a_response(self):
        """Consent and hand-off timing read `reached_end` / `rendered_ms` for the MODEL's
        response id; a tone under `cue:*` must leave every other id exactly as it was, and
        the cue id itself never reaches an end (no wire ever reports it done)."""
        daemon = self._built()
        daemon._lock_held = True
        daemon.sink.start()
        daemon._relay_qualified()
        daemon.sink.drain()
        sink = daemon.sink
        self.assertFalse(sink.reached_end("resp-1"))
        self.assertIsNone(sink.rendered_ms("resp-1", "item-1"))
        self.assertFalse(sink.reached_end(self.cue.response_id(self.cue.START)))
        self.assertIsNotNone(sink.rendered_ms(self.cue.response_id(self.cue.START),
                                              self.cue.ITEM_ID), "the tone did render")
        sink.close()

    async def test_a_second_shutdown_waits_for_the_first_to_finish(self):
        """A control `stop` and `start`'s finally-clause both call `shutdown`; the second
        must not return while the first is still closing the socket, or the process exits
        under it (reproduced by review: no stop tone, sink never closed)."""
        daemon = self._built()
        daemon._lock_held = True
        daemon.sink.start()
        daemon._lock = mock.Mock()
        gate = asyncio.Event()

        class SlowSession:
            async def close(self):
                await gate.wait()

        daemon.session = SlowSession()
        first = asyncio.ensure_future(daemon.shutdown("stopped by control"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(daemon.shutdown("stream ended"))
        await asyncio.sleep(0)
        self.assertFalse(second.done(), "the second caller must wait, not return early")
        self.assertFalse(self.stream.closed)
        gate.set()
        await asyncio.gather(first, second)
        self.assertTrue(self.stream.closed)
        self.assertEqual(b"".join(self.stream.writes), self.cue.earcon(self.cue.STOP))
        self.assertEqual(Status.read(self.dir / "status.json")["ended"]["reason"],
                         "stopped by control")


class SessionEdgeWords(unittest.IsolatedAsyncioTestCase):
    """Greeting after the relay qualifies; goodbye before the socket closes; both in the
    model's own words and never on a clock of the loop's own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.stream = FakeStream()
        self.calls: list[str] = []

    def _built(self):
        values = {"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com",
                  "AZURE_OPENAI_API_KEY": "k", "VOICE_LIVE_API_VERSION": "2026-07-15",
                  "VOICE_PROCESS_ARGV": "true"}
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": values.get(n, d)), \
             mock.patch.dict(os.environ, AZURE_ENV):
            daemon = VoiceDaemon(session_id="s", provider="voice_live",
                                 backend_name="process", state=self.dir,
                                 connect=FakeSocket.connector([]),
                                 open_output=lambda: self.stream)
            daemon.build()
        self.addCleanup(daemon.ledger.close)
        daemon._lock_held = True
        daemon._lock = mock.Mock()
        daemon.sink.start()
        calls = self.calls

        class Loop:
            async def notice_open(self):
                calls.append("greet")

            async def farewell(self, reason):
                calls.append(f"farewell:{reason}")

        class Session:
            async def close(self):
                calls.append("close")

        daemon.loop, daemon.session = Loop(), Session()
        return daemon

    async def test_qualified_greets_after_the_tone(self):
        daemon = self._built()
        daemon._relay_qualified()
        await asyncio.sleep(0)
        self.assertEqual(self.calls, ["greet"])
        daemon.sink.close()

    async def test_the_goodbye_comes_before_the_socket_closes_and_the_tone(self):
        from voice.audio import cue
        daemon = self._built()
        await daemon.shutdown("stopped by control")
        self.assertEqual(self.calls, ["farewell:stopped by control", "close"])
        self.assertEqual(b"".join(self.stream.writes), cue.earcon(cue.STOP))

    async def test_no_goodbye_when_the_stream_already_ended(self):
        daemon = self._built()
        await daemon.shutdown("stream ended")
        self.assertEqual(self.calls, ["close"])

    async def test_a_goodbye_that_never_comes_back_does_not_hold_the_ending(self):
        daemon = self._built()

        async def never(reason):
            self.calls.append("farewell")
            await asyncio.Event().wait()

        daemon.loop.farewell = never
        with mock.patch.object(daemon_mod, "FAREWELL_BOUND_S", 0.01):
            await daemon.shutdown("session limit")
        self.assertEqual(self.calls, ["farewell", "close"])
        self.assertTrue(self.stream.closed)


class SinkDrainAfterDeath(unittest.TestCase):
    """`drain` is what lets the goodbye finish before the device closes; it must return
    when the writer died, or the ending hangs on a dead speaker."""

    def test_drain_returns_after_the_writer_died_and_play_no_longer_queues(self):
        from voice.audio.io import PlaybackSink
        stream = FakeStream()
        stream.active = False                         # every write raises
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        sink.play("r1", "i", b"\x00\x00" * 480)
        sink.play("r2", "i", b"\x00\x00" * 480)      # queued behind the fatal write
        sink.drain()                                  # must not hang
        self.assertTrue(sink.dead)
        sink.play("r3", "i", b"\x00\x00" * 480)      # dead: dropped, never queued
        sink.drain()
        self.assertFalse(sink.reached_end("r3"))
        fut = sink.drained(asyncio.new_event_loop())
        self.assertTrue(fut.done(), "dead: nothing will ever be taken, so it is drained now")
        sink.close()

    def test_drained_resolves_from_the_writer_when_the_device_took_everything(self):
        from voice.audio.io import PlaybackSink
        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()

        async def scenario():
            sink.play("r1", "i", b"\x00\x00" * 480)
            sink.play("r1", "i", b"\x00\x00" * 480)
            await asyncio.wait_for(sink.drained(asyncio.get_running_loop()), 1.0)
            self.assertEqual(len(stream.writes), 2)

        asyncio.run(scenario())
        sink.close()


class TheCapStillLetsTheGoodbyeThrough(unittest.IsolatedAsyncioTestCase):
    """The session cap ends the session, and the goodbye is spoken through the loop — so the
    cap must not cancel the loop before the goodbye is awaited (it did: `wait_for` cancelled
    the task it timed out on, and the farewell then waited on a loop nobody was pumping)."""

    async def test_the_loop_is_still_running_when_the_farewell_is_awaited(self):
        seen = []

        class Loop:
            async def run(self):
                await asyncio.Event().wait()

            async def farewell(self, reason):
                seen.append(("farewell", reason, daemon._loop_task.done()))

        class Recorder:
            def __init__(self):
                self.ended = []

            def set(self, **kw):
                pass

            def end(self, reason):
                self.ended.append(reason)

        class Session:
            async def close(self):
                seen.append(("close",))

        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.loop, daemon.session, daemon.status = Loop(), Session(), Recorder()
        daemon.control = daemon.capture = daemon.sink = daemon.ledger = None
        daemon._lock_held = True
        daemon._lock = mock.Mock()
        with mock.patch.object(daemon_mod.voice_config, "cfg",
                               side_effect=lambda n, d="": "0.0005" if "MINUTES" in n else d):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(seen, [("farewell", "session limit", False), ("close",)])
        self.assertEqual(daemon.status.ended, ["session limit"])
        self.assertTrue(daemon._loop_task.cancelled())


class ABlockedSpeakerCannotHoldTheEnding(unittest.IsolatedAsyncioTestCase):
    """The goodbye's playback wait is a lifecycle bound like the wire wait: a device that
    never takes the frames lets the ending go on (review: the earlier thread-based drain
    could wait forever and strand an executor worker)."""

    async def test_farewell_returns_when_the_device_never_takes_the_goodbye(self):
        import threading
        from voice.audio.io import PlaybackSink
        release = threading.Event()

        class BlockingStream(FakeStream):
            def write(self, pcm):
                release.wait()
                super().write(pcm)

        stream = BlockingStream()
        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.sink = PlaybackSink(open_output=lambda: stream)
        daemon.sink.start()
        daemon._lock_held = True

        class Loop:
            async def farewell(self, reason):
                daemon.sink.play("bye", "i", b"\x00\x00" * 480)

        class Session:
            pass

        daemon.loop, daemon.session = Loop(), Session()
        with mock.patch.object(daemon_mod, "FAREWELL_BOUND_S", 0.05):
            await asyncio.wait_for(daemon._farewell("stopped by control"), 2.0)
        self.assertEqual(stream.writes, [], "the device is still blocked; the ending went on")
        release.set()
        daemon.sink.drain()
        daemon.sink.close()


class ABlockedSpeakerCannotHoldTheWholeEnding(unittest.IsolatedAsyncioTestCase):
    """Beyond `_farewell`: the sink's close joins its writer, and a writer stuck in a device
    write that never returns must be abandoned under the daemon's close bound, not waited
    for (review round 2: the ending sat in `thread.join()` with the audio lock unreleased)."""

    async def test_shutdown_completes_with_the_device_write_never_returning(self):
        import threading
        from voice.audio.io import PlaybackSink
        release = threading.Event()

        class BlockingStream(FakeStream):
            def write(self, pcm):
                release.wait()
                super().write(pcm)

        stream = BlockingStream()
        seen = []

        class Recorder:
            def set(self, **kw):
                pass

            def end(self, reason):
                seen.append(reason)

        class Loop:
            async def farewell(self, reason):
                daemon.sink.play("bye", "i", b"\x00\x00" * 480)

        class Session:
            async def close(self):
                seen.append("close")

        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.sink = PlaybackSink(open_output=lambda: stream)
        daemon.sink.start()
        daemon.loop, daemon.session, daemon.status = Loop(), Session(), Recorder()
        daemon.control = daemon.capture = daemon.ledger = None
        daemon._lock_held = True
        daemon._lock = mock.Mock()
        with mock.patch.object(daemon_mod, "FAREWELL_BOUND_S", 0.02), \
             mock.patch.object(daemon_mod, "CLOSE_BOUND_S", 0.02):
            await asyncio.wait_for(daemon.shutdown("stopped by control"), 2.0)
        self.assertEqual(seen, ["close", "stopped by control"])
        daemon._lock.release.assert_called_once()
        self.assertGreaterEqual(stream.aborts, 1, "the stuck writer was unblocked, not waited for")
        release.set()


class AnAbandonedWriterIsSettledOnce(unittest.TestCase):
    """Close under a bound, with the device never answering: every waiter is released,
    the count settles, no cancel can reopen the device, and a device whose own
    stop/close blocks cannot hold the closer (review round 3)."""

    def _blocked(self):
        import threading
        from voice.audio.io import PlaybackSink
        release = threading.Event()

        class WedgedStream(FakeStream):
            def write(self, pcm):
                release.wait()
                super().write(pcm)

            def stop(self):
                release.wait()            # a synchronous teardown that also never returns
                super().stop()

        stream = WedgedStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        return sink, stream, release

    def test_close_settles_waiters_and_returns_when_the_device_never_answers(self):
        sink, stream, release = self._blocked()
        loop = asyncio.new_event_loop()
        try:
            sink.play("r1", "i", b"\x00\x00" * 480)
            sink.play("r1", "i", b"\x00\x00" * 480)
            fut = sink.drained(loop)
            self.assertFalse(fut.done())
            sink.close(bound_s=0.02)            # returns although write AND stop block
            loop.run_until_complete(asyncio.sleep(0))
            self.assertTrue(fut.done(), "an abandoned queue is drained for every waiter")
            self.assertTrue(sink.dead)
            self.assertTrue(sink.drained(loop).done())
            self.assertIsNone(sink._stream)
        finally:
            release.set()
            loop.close()


class AStopDuringStartupEndsTheSessionForGood(unittest.IsolatedAsyncioTestCase):
    """The control watcher and the TERM handler are live before the loop task exists. A stop
    that lands then must not be followed by the loop coming up behind the ending (Grok
    review: `start` never read `_shut`; the ending ran, then `_run` started the loop)."""

    async def test_run_does_not_start_the_loop_after_the_ending(self):
        started = []

        class Loop:
            async def run(self):
                started.append(True)
                await asyncio.Event().wait()

        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.loop = Loop()
        daemon.status = mock.Mock()
        daemon.control = daemon.capture = daemon.session = daemon.sink = daemon.ledger = None
        daemon._lock_held = False
        await daemon.shutdown("stopped by control")       # before _run, as during qualify
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=lambda n, d="": d):
            await asyncio.wait_for(daemon._run(), 1.0)
        self.assertEqual(started, [], "the loop never came up behind the ending")
        self.assertFalse(hasattr(daemon, "_loop_task"))


class LateArrivalsAfterTheEnding(unittest.IsolatedAsyncioTestCase):
    """Two late arrivals that must change nothing once the ending began: audio offered to a
    closed sink, and a relay receipt arriving during the goodbye (independent review)."""

    def test_audio_offered_after_close_is_dropped_not_queued(self):
        from voice.audio.io import PlaybackSink
        stream = FakeStream()
        sink = PlaybackSink(open_output=lambda: stream)
        sink.start()
        sink.close()
        sink.play("late", "i", b"\x00\x00" * 480)
        sink.drain()                                   # must not hang
        self.assertTrue(sink.drained(asyncio.new_event_loop()).done())
        self.assertFalse(sink.reached_end("late"))

    async def test_a_relay_receipt_during_the_goodbye_plays_no_hello(self):
        daemon = VoiceDaemon.__new__(VoiceDaemon)
        daemon.status = mock.Mock()
        daemon.sink = mock.Mock()
        daemon.loop = mock.Mock(notice_open=mock.AsyncMock())
        daemon._lock_held = True
        daemon._shut = True
        daemon._relay_qualified()
        daemon.sink.play.assert_not_called()
        daemon.loop.notice_open.assert_not_called()
        daemon.status.set.assert_not_called()



def _idle_daemon(loop, backend=None):
    class Recorder:
        def __init__(self):
            self.ended = []

        def set(self, **kw):
            pass

        def end(self, reason):
            self.ended.append(reason)

    class Session:
        async def close(self):
            pass

    daemon = VoiceDaemon.__new__(VoiceDaemon)
    daemon.loop, daemon.session, daemon.status = loop, Session(), Recorder()
    daemon.backend = backend
    daemon.control = daemon.capture = daemon.sink = daemon.ledger = None
    daemon._lock_held = True
    daemon._lock = mock.Mock()
    return daemon


def _idle_cfg(idle):
    return lambda n, d="": idle if "IDLE" in n else ("0" if "SESSION" in n else d)


class _IdleLoop:
    def __init__(self):
        self.stirred = asyncio.Event()
        self.speaking = False
        self.farewells = []

    async def run(self):
        await asyncio.Event().wait()

    async def farewell(self, reason):
        self.farewells.append(reason)


class _Backend:
    def __init__(self, activity="idle", dialog=None, owner_lost=None):
        self.activity, self.dialog, self.owner_lost = activity, dialog, owner_lost
        self.asked = 0

    async def state(self):
        from voice.backend.base import BackendState
        self.asked += 1
        return BackendState(activity=self.activity, turn_id=None, dialog=self.dialog,
                            owner_lost=self.owner_lost)


async def _until(predicate, bound=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + bound
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


class AQuietSessionSaysGoodbyeAndEnds(unittest.IsolatedAsyncioTestCase):
    """Nothing from either side and the backend not working: the session ends itself, through
    the goodbye, so a forgotten daemon stops holding the mic and the realtime socket."""

    async def test_quiet_and_idle_backend_ends_with_idle(self):
        loop = _IdleLoop()
        daemon = _idle_daemon(loop, _Backend("idle"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])
        self.assertEqual(daemon.status.ended, ["idle"])
        self.assertTrue(daemon._loop_task.cancelled())

    async def test_default_is_on_when_the_operator_sets_nothing(self):
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=lambda n, d="": d):
            self.assertGreater(daemon_mod.idle_close_s(), 0)

    async def test_zero_turns_it_off(self):
        loop = _IdleLoop()
        daemon = _idle_daemon(loop, _Backend("idle"))
        daemon._idle_watch = mock.Mock(side_effect=AssertionError("no watch at 0"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0")):
            self.assertEqual(daemon_mod.idle_close_s(), 0)
            task = asyncio.ensure_future(daemon._run())
            await asyncio.sleep(0.05)
            daemon._idle_watch.assert_not_called()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class AWorkingBackendKeepsTheSessionOpen(unittest.IsolatedAsyncioTestCase):
    """A window that ends while the backend is working only starts the next one: its result
    is still to be heard."""

    async def test_working_windows_do_not_end_it_and_the_first_idle_one_does(self):
        loop, backend = _IdleLoop(), _Backend("working")
        daemon = _idle_daemon(loop, backend)
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            task = asyncio.ensure_future(daemon._run())
            await _until(lambda: backend.asked >= 3)
            self.assertFalse(task.done())
            self.assertEqual(loop.farewells, [])
            backend.activity = "idle"
            await asyncio.wait_for(task, 2.0)
        self.assertEqual(loop.farewells, ["idle"])

    async def test_a_turn_waiting_on_a_dialog_is_not_working(self):
        loop = _IdleLoop()
        daemon = _idle_daemon(loop, _Backend("working", dialog=object()))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])

    async def test_a_lost_owner_is_not_working(self):
        loop = _IdleLoop()
        daemon = _idle_daemon(loop, _Backend("working", owner_lost="pane gone"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])


class OneLongUtteranceIsNotSilence(unittest.IsolatedAsyncioTestCase):
    """Speech is one start event and one end event: the windows in between are presence."""

    async def test_a_window_that_ends_mid_utterance_is_not_the_end(self):
        loop, backend = _IdleLoop(), _Backend("idle")
        loop.speaking = True
        daemon = _idle_daemon(loop, backend)
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.002")):
            task = asyncio.ensure_future(daemon._run())
            await asyncio.sleep(0.15)                # past the first 0.12 s window
            self.assertFalse(task.done())
            self.assertEqual(backend.asked, 0)       # the window was spent on the utterance
            loop.speaking = False
            await asyncio.wait_for(task, 2.0)
        self.assertEqual(loop.farewells, ["idle"])


class SpeechThatNeverEndsStillEnds(unittest.IsolatedAsyncioTestCase):
    """A speech start whose end never arrives (an error) buys one window, not forever."""

    async def test_a_stuck_speaking_flag_ends_after_one_extra_window(self):
        loop = _IdleLoop()
        loop.speaking = True
        daemon = _idle_daemon(loop, _Backend("idle"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])


class AnEventDuringTheReadRenewsTheSpeechGrace(unittest.IsolatedAsyncioTestCase):
    """A new utterance that starts while the backend is read gets its own grace window: the
    next read comes two windows later, not one."""

    async def test_speech_grace_resets_when_the_read_is_interrupted_by_an_event(self):
        loop = _IdleLoop()
        reads = []

        class Stirring(_Backend):
            async def refresh(self):
                reads.append(asyncio.get_running_loop().time())
                if len(reads) == 1:
                    loop.stirred.set()            # a new utterance began during the first read
                return await self.state()

        loop.speaking = True                      # and it never reports its end
        daemon = _idle_daemon(loop, Stirring("idle"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.002")):
            await asyncio.wait_for(daemon._run(), 3.0)
        self.assertEqual(loop.farewells, ["idle"])
        self.assertEqual(len(reads), 2)
        self.assertGreater(reads[1] - reads[0], 1.5 * 0.12)   # a grace window sat between


class AFailedReadKeepsTheWatch(unittest.IsolatedAsyncioTestCase):
    """If the fresh read raises, the cached account decides; the watch must not die with it."""

    async def test_a_refresh_that_raises_falls_back_to_the_cached_state(self):
        class Broken(_Backend):
            async def refresh(self):
                raise OSError("orca not found")

        loop = _IdleLoop()
        daemon = _idle_daemon(loop, Broken("idle"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])

    async def test_both_reads_failing_still_ends_the_session(self):
        class Dead(_Backend):
            async def refresh(self):
                raise OSError("orca not found")

            async def state(self):
                raise OSError("state gone")

        loop = _IdleLoop()
        daemon = _idle_daemon(loop, Dead("idle"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])


class ANewDialogIsSpokenBeforeGoodbye(unittest.IsolatedAsyncioTestCase):
    """A dialog the read just found gets one window before it counts as waiting on the operator."""

    async def test_a_dialog_gets_one_window_then_the_session_ends(self):
        dialog = mock.Mock(occurrence_id="d1")
        loop, backend = _IdleLoop(), _Backend("working", dialog=dialog)
        daemon = _idle_daemon(loop, backend)
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])
        self.assertEqual(backend.asked, 2)           # the first window kept it open


class TheCloseReadsTheBackendFresh(unittest.IsolatedAsyncioTestCase):
    """A backend that can re-read its owner is asked to, before "working" keeps a session."""

    async def test_refresh_is_preferred_over_the_cached_state(self):
        class Refreshing(_Backend):
            async def refresh(self):
                self.owner_lost = "owner_pid_gone"
                return await self.state()

        loop = _IdleLoop()
        daemon = _idle_daemon(loop, Refreshing("working"))
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])


class SomethingDuringTheReadRestartsTheWindow(unittest.IsolatedAsyncioTestCase):
    """The fresh read waits on the pane; the operator may speak meanwhile. That is presence."""

    async def test_an_event_during_refresh_keeps_the_session(self):
        loop = _IdleLoop()

        class Stirring(_Backend):
            async def refresh(self):
                if self.asked == 0:
                    loop.stirred.set()            # the operator spoke while the pane was read
                return await self.state()

        backend = Stirring("idle")
        daemon = _idle_daemon(loop, backend)
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.0005")):
            await asyncio.wait_for(daemon._run(), 2.0)
        self.assertEqual(loop.farewells, ["idle"])
        self.assertGreaterEqual(backend.asked, 2)   # the first window did not end it


class AnyEventRestartsTheQuietWindow(unittest.IsolatedAsyncioTestCase):
    """An event from either side is presence; the window counts from the last one."""

    async def test_a_session_that_keeps_stirring_stays_open(self):
        loop, backend = _IdleLoop(), _Backend("idle")
        daemon = _idle_daemon(loop, backend)
        with mock.patch.object(daemon_mod.voice_config, "cfg", side_effect=_idle_cfg("0.002")):
            task = asyncio.ensure_future(daemon._run())
            for _ in range(20):                       # 0.2 s of stirring, window 0.12 s
                loop.stirred.set()
                await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertEqual(backend.asked, 0)
            await asyncio.wait_for(task, 2.0)         # then quiet: it ends
        self.assertEqual(loop.farewells, ["idle"])
