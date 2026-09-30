"""The daemon: the one place where every real part is chosen and wired together.

Everything below this file is a protocol. `AgentLoop` knows a `LiveSession`, a `Strategy`,
a `Backend`, a `LedgerPort` and an `AudioSink` — never which ones. This module is where the
env picks a voice provider (by name, from `voice/live/providers.py` — this file names none),
`claude_code` or `claude_jobs` or `process`, and turns that into one object graph. Nothing
here decides conversational meaning.

CLI: `start | status | stop | preflight` — on, status, off (and a capability sheet).

**`status` is a file, not a question asked of the process.** The daemon writes a JSON
snapshot after every phase change; `status` reads it. Its field names are a CONTRACT for
any reader — `phase`, `pid`, `at`, `started_at`, `ended.reason`, `ended.ended_at`, `relay`,
`mode`, `last_receipt.state`, `audio_ready`, `asleep`, `instance`,
`session.{id,name,cwd}`, and the provider relay's `relay_count` and `reconnecting` (the phase
stays `running` through a relay: only the voice provider's session is renewed).

**Secrets are never printed.** The start-up check asserts that credential env NAMES are
present and says which are missing; it never echoes a value, and `voice.config.redact_text`
guards anything derived from a provider error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from voice import config as voice_config
from voice.audio import cue
from voice import platform as voice_platform
from voice.live import providers

# ---------------------------------------------------------------- lifecycle ports
# THE ONLY NUMBERS IN THIS FILE, and the only ones DESIGN.md §Timers permits: bounds on the
# connection and on the machine audio lock. Nothing semantic waits on them — no fragment
# grace, no confirmation window, no response deadline. They are defaults for injection, so
# a test or an operator overrides them without editing code; the modules they are handed to
# declare no defaults of their own, which is what keeps a semantic timer from growing one.
# b3: lifecycle-ports begin
CONNECT_BOUND_S = 20.0        # socket open
CLOSE_BOUND_S = 5.0           # socket close; also the speaker writer's join at the end
RECONNECT_BOUND_S = 30.0      # a reconnect attempt
AUDIO_LOCK_BOUND_S = 10.0     # bounded wait for the one-mic-per-machine lock
# A file lock has no readiness event a selector can wait on, so waiting for one means
# retrying. This is that cadence, and it lives here because `audio/io.py` declares none.
AUDIO_LOCK_POLL_S = 0.2
PANE_READ_BOUND_S = 5.0       # one `orca terminal read`
RELAY_CONNECT_BOUND_S = 3.0   # one relay socket connect
# One `ps` probe. A wedged `ps` must not hold a send hostage, and a process that does not answer
# within the bound reads as evidence UNAVAILABLE — never as proof the owner died.
PS_PROBE_BOUND_S = 5.0
# The mesh's OWN long-poll cap: `drive_job_status` clamps `wait_sec` to 0..20 inline and
# exports no constant, so the number has to be written down somewhere and this is the only
# place permitted to hold one. It is the transport's ceiling, not a bound on any decision —
# the poll IS the clock for a job, and nothing semantic is released when it returns.
JOBS_LONG_POLL_MAX_SEC = 20.0
# NOT A WAIT. `ps` reports a process start time only to the second, so this is the
# resolution of the MEASUREMENT the identity check compares against — an equality at one
# second. Widening it reopens a pid-reuse window: two processes whose starts fall inside
# the tolerance become indistinguishable, and a recycled pid would pass the check that
# exists to catch exactly that. It travels to `Pane`, `RelayActuator`, `registry_claims`,
# `registry_matches` and `relay_identity_still_holds`, which declare no default of their own.
PS_START_GRANULARITY_S = 1.0
# The operator's cap on a whole session, in minutes from the environment (0 = none). Not a
# decision timer: nothing is released when it fires; the session ends and the status says why.
SESSION_MAX_MINUTES_NAME = "VOICE_SESSION_MAX_MINUTES"
# The goodbye: one short response's round trip on the wire, and again its playback, before
# the socket closes. When either does not come, the session ends without it; nothing else
# is decided by it.
FAREWELL_BOUND_S = 8.0
# How often the PORTABLE file watch looks (Linux, Windows — `voice/platform.py`); macOS uses
# kqueue and never polls. A cadence, not a decision: only a changed file releases anything.
WATCH_POLL_S = 0.5
# The idle close: minutes with no event from either side while nothing is owed, then the
# session says goodbye and ends, so a forgotten daemon stops holding the mic and the
# realtime socket. The operator overrides it from the environment; 0 turns it off. It is
# the one operator bound with a default here, because the default is what keeps the cost
# of a forgotten session bounded.
IDLE_MINUTES_NAME = "VOICE_IDLE_MINUTES"
IDLE_MINUTES_DEFAULT = "10"
# The provider relay. A provider session that DROPS is replaced by a fresh one: up to
# RECONNECT_ATTEMPTS opens, each inside RECONNECT_BOUND_S, the first at once and each later one
# after a backoff that doubles from RECONNECT_BACKOFF_S. The same count ends a run of fresh
# sessions that each ended carrying nothing, so a provider that refuses every session cannot
# hold the daemon in a reconnect loop.
RECONNECT_ATTEMPTS = 3
RECONNECT_BACKOFF_S = 1.0
# The scheduled rollover: minutes a provider session may run before the daemon opens a fresh
# one at the next QUIET moment (ahead of the provider's own session age limit) and only then
# closes the old. The operator overrides it; 0 turns it off. A reconnect restarts the count.
ROLLOVER_MINUTES_NAME = "VOICE_SESSION_ROLLOVER_MINUTES"
# Microphone blocks held while no provider session can hear them (a reconnect's gap) and sent,
# in order, to the session that takes over — the operator's words and a barge-in survive the
# gap. A SIZE (~30 s of 20 ms blocks), not a wait: past it the oldest blocks go first.
MIC_GAP_BLOCKS = 1500
ROLLOVER_MINUTES_DEFAULT = "55"
# b3: lifecycle-ports end


def session_max_s() -> float:
    return 60.0 * float(voice_config.cfg(SESSION_MAX_MINUTES_NAME, "0") or 0)


def idle_close_s() -> float:
    return 60.0 * float(voice_config.cfg(IDLE_MINUTES_NAME, IDLE_MINUTES_DEFAULT) or 0)


def rollover_s() -> float:
    return 60.0 * float(voice_config.cfg(ROLLOVER_MINUTES_NAME, ROLLOVER_MINUTES_DEFAULT) or 0)

STATUS_NAME = "status.json"
DEFAULT_STATE_ROOT = "~/.local/state/agent-os/voice-listen"

def state_dir(session_id: str) -> Path:
    root = os.environ.get("VOICE_LISTEN_STATE_DIR") or os.path.expanduser(DEFAULT_STATE_ROOT)
    return Path(root) / session_id


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Replace the status file in one step. A statusline reading a half-written file would
    print nothing at best and garbage at worst, and it reads on someone else's schedule."""
    voice_platform.private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".status-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Status:
    """The snapshot the statusline consumes. Field names are the contract (module docstring).

    `mutate -> publish` is deliberate: every phase change writes, because a status that is
    only written at shutdown tells the operator nothing while the thing is running.
    """

    def __init__(self, path: Path, *, session_id: str, session_name: str = "",
                 session_cwd: str = "", instance: str = "", mode: str = "",
                 now=time.time) -> None:
        self.path = path
        self._now = now
        self.data: dict[str, Any] = {
            "instance": instance or uuid.uuid4().hex[:12],
            "pid": os.getpid(),
            "phase": "starting",
            "started_at": self._now(),
            "at": self._now(),
            "relay": "",
            "mode": mode,
            "audio_ready": False,
            "asleep": False,
            "last_receipt": {"state": ""},
            "relay_count": 0,
            "reconnecting": False,
            "ended": {},
            "session": {"id": session_id, "name": session_name, "cwd": session_cwd},
        }

    def set(self, **fields: Any) -> None:
        self.data.update(fields)
        self.data["at"] = self._now()
        self.publish()

    def end(self, reason: str) -> None:
        self.data["phase"] = "ended"
        self.data["ended"] = {"reason": reason, "ended_at": self._now()}
        self.data["at"] = self._now()
        self.publish()

    def publish(self) -> None:
        atomic_write(self.path, self.data)

    @staticmethod
    def read(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------- credential check

class MissingCredentials(RuntimeError):
    """Names only. This exception reaches the operator's terminal, so it must be safe
    to print even when the reason it was raised is a mistyped secret."""


def check_credentials(provider: str, env: dict[str, str] | None = None) -> None:
    source = env if env is not None else os.environ
    profile = providers.get(provider)
    if profile is None:
        raise MissingCredentials(f"unknown provider {voice_config.redact_text(provider)!r}; "
                                 f"expected one of {providers.names()}")
    names = profile.required_env
    missing = [n for n in names if not (source.get(n) or voice_config.cfg(n)).strip()]
    if missing:
        raise MissingCredentials(
            f"{provider}: missing {', '.join(missing)} — set the name(s) in the environment "
            f"or the voice env file. (Values are never read back or logged.)")


# ---------------------------------------------------------------- component choice

def build_session(provider: str, *, sink: Any, connect: Any = None,
                  connect_bound_s: float = CONNECT_BOUND_S,
                  close_bound_s: float = CLOSE_BOUND_S,
                  reconnect_bound_s: float = RECONNECT_BOUND_S) -> Any:
    """The live provider named by `provider`, built by its registry profile with this
    module's lifecycle bounds. Which providers exist is `voice/live/providers.py`'s table."""
    profile = providers.get(provider)
    if profile is None:
        raise ValueError(f"unknown provider {provider!r}; expected one of {providers.names()}")
    if connect is None:
        connect = _websocket_connect
    return profile.build_session(sink, connect, providers.Bounds(
        open_s=connect_bound_s, close_s=close_bound_s, reconnect_s=reconnect_bound_s))


async def _websocket_connect(uri: str, headers: dict[str, str]) -> Any:
    """The real socket. Imported here so nothing that merely builds a graph needs it."""
    import websockets

    return await websockets.connect(uri, additional_headers=headers, max_size=1 << 24)


def build_backend(name: str, *, drivers: dict[str, Any] | None = None,
                  jobs_long_poll_max_sec: float = JOBS_LONG_POLL_MAX_SEC,
                  claude_code: Any = None) -> Any:
    """The harness behind the voice, built by its registry profile (`voice/backend/registry.py`).

    Imports are lazy and failures are LOUD: an unknown harness or process dialect raises
    `Unsupported` naming the valid ones, at build time rather than at the first dispatch.
    `drivers` supplies `claude_jobs`' mesh entry points (host tools this process cannot
    import); `claude_code` is the daemon's builder over the proven handshake binding.
    """
    from voice.backend import registry as backends

    profile = backends.get(name)
    if profile is None:
        raise backends.Unsupported(f"unknown VOICE_BACKEND {name!r}; "
                                   f"expected one of {backends.names()}")
    return profile.build(backends.BuildContext(
        drivers=drivers or {}, jobs_long_poll_max_sec=jobs_long_poll_max_sec,
        claude_code=claude_code))


def effect_vocabulary() -> frozenset[str]:
    """What the ledger will accept as an op `kind`: exactly what the loop records.

    The loop is the ledger's only writer, so the list lives beside it (`loop.EFFECT_KINDS`)
    and is imported here, never restated. Consent stays the broker's deterministic gate and
    is never an op the model names (DESIGN.md §Consent); a stop is `request(now)`.
    """
    from voice.agent.loop import EFFECT_KINDS

    return EFFECT_KINDS


# ---------------------------------------------------------------- control channel

CONTROL_NAME = "control.json"
CONTROL_COMMANDS = ("stop",)


class ControlWatcher:
    """Wakes when the control file changes. EVENT-DRIVEN: there is no interval here.

    The DIRECTORY is watched, not the file, because the CLI writes atomically — `os.replace`
    swaps in a new inode, so a watch on the old file would go deaf after the first command.
    The watch comes from `voice/platform.py` (kqueue on macOS, a stat poll elsewhere) and is
    awaited by a task; a watch that exposes an fd is handed to `loop.add_reader` directly.
    `open_watch` is injected: a test supplies a fake that fires on demand, which is why no
    test needs a real filesystem event.
    """

    def __init__(self, directory: Path, on_command, *, open_watch=None) -> None:
        self.directory = directory
        self._on_command = on_command
        self._open_watch = open_watch or _directory_watch
        self._task: Any = None
        self._watch: Any = None
        self._loop: Any = None
        self._seen_at: float | None = None

    def start(self, loop: Any) -> None:
        voice_platform.private_dir(self.directory)
        self._loop = loop
        # A command written BEFORE the watcher started is history, not an instruction: adopt
        # its `at` as already-seen so it cannot fire. Without this, a `stop` left in the state
        # dir by a previous run's shutdown replayed on the next launch and tore the new daemon
        # down mid-startup — closing the ledger under the qualification probe (measured live:
        # relay-probe-failed with a NoneType ledger handle, three launches running).
        try:
            prior = json.loads((self.directory / CONTROL_NAME).read_text(encoding="utf-8"))
            self._seen_at = prior.get("at")
        except (OSError, ValueError):
            self._seen_at = None
        self._watch = self._open_watch(self.directory)
        if self._watch is None:
            # No watch is available on this platform. The CLI still records the command
            # and `status` still reports; what is lost is only the live reaction, and
            # saying so beats pretending the channel works.
            return
        if hasattr(self._watch, "fileno"):
            loop.add_reader(self._watch.fileno(), self._wake)
        else:
            self._task = loop.create_task(self._pump())

    async def _pump(self) -> None:
        while self._watch is not None:
            await self._watch.wait()
            self._wake(drained=True)

    def _wake(self, drained: bool = False) -> None:
        if self._watch is not None and not drained:
            self._watch.drain()
        command = self.read_command()
        if command is None:
            return
        self._loop.create_task(self._on_command(command))

    def read_command(self) -> str | None:
        """The pending command, or None. A command is consumed ONCE.

        `at` is the de-duplicator: a directory event can fire more than once for a single
        write, and replaying `stop` after the daemon already stopped would be a second,
        spurious shutdown.
        """
        try:
            data = json.loads((self.directory / CONTROL_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        command = data.get("command")
        at = data.get("at")
        if command not in CONTROL_COMMANDS:
            return None
        if at is not None and at == self._seen_at:
            return None
        self._seen_at = at
        return command

    def close(self) -> None:
        watch, self._watch = self._watch, None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        if watch is None:
            return
        if self._loop is not None and hasattr(watch, "fileno"):
            try:
                self._loop.remove_reader(watch.fileno())
            except Exception:
                pass
        watch.close()


def _transcript_wake(path: Path) -> Any:
    """A transcript wake from `voice/platform.py` (kqueue on macOS, a stat poll elsewhere), or
    None when the file cannot be watched — then the backend stream ends on a quiet poll."""
    return voice_platform.watch(path, poll_s=WATCH_POLL_S) if path.exists() else None


def _directory_watch(directory: Path) -> Any:
    """The control directory's watch — portable (`voice/platform.py`)."""
    return voice_platform.watch(directory, poll_s=WATCH_POLL_S)


# ---------------------------------------------------------------- claude_code wiring

# The ownership handshake's refusal names, re-exported so the daemon's callers can match on
# them without importing the pane. `bind_unproven` is the hard one: zero nonce hits means
# the handle points at a pane that is not running our command, and there is no retry.
REFUSE_UNPROVEN = "bind_unproven"
REFUSE_NO_TRANSCRIPT = "bind_no_transcript"
REFUSE_NO_REGISTRY = "session_registry_missing"


def resolve_transcript(session_id: str, home: Path | None = None) -> Path | None:
    """This session's transcript .jsonl, found by globbing the session UUID.

    Ported from the retired relay's `find_transcript`: an MCP tool gets the session id but never the
    transcript path, and UUIDs are unique across project slugs, so the glob is unambiguous.
    The shape is validated first — a caller that passed something that is not a session id
    would otherwise glob the whole projects tree.
    """
    import re as _re

    if not session_id or not _re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        return None
    base = Path(home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    hits = sorted((base / "projects").glob(f"*/{session_id}.jsonl"),
                  key=lambda path: path.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def find_claude_ancestor(session_id: str, *, ppid=os.getppid,
                         registry=None) -> int:
    """The Claude process this command is running inside.

    ONLY a descendant of that process can resolve this: the measured chain from a Bash tool
    call is bash → zsh → claude, three hops. A detached child's ancestors are launchd's, so
    the handshake must run in the launcher, never in the daemon child (ported from
    the retired relay's `_find_claude_ancestor` placement, not its body).
    """
    from voice.backend.claude_code.pane import session_registry

    lookup = registry or session_registry
    pid = ppid()
    seen = 0
    while pid and pid > 1 and seen < 12:
        record = lookup(pid)
        if isinstance(record, dict) and str(record.get("sessionId") or "") == session_id:
            return pid
        pid = _parent_of(pid)
        seen += 1
    return 0


def _parent_of(pid: int) -> int:
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=PS_PROBE_BOUND_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0
    try:
        return int((out.stdout or "").strip())
    except ValueError:
        return 0


async def prove_ownership(*, nonce: str, session_id: str, terminal: str,
                          claude_pid: int, run: Any, read_bound_s: float,
                          start_granularity_s: float = PS_START_GRANULARITY_S,
                          ps_bound_s: float = PS_PROBE_BOUND_S,
                          now=time.time) -> dict:
    """The `start <nonce>` handshake: prove this handle is the pane running OUR command.

    ZERO KEYSTROKES. The nonce is the first token of the command line the operator's own
    session runs inside its Bash tool; the pane is READ through the Orca handle and the
    nonce must appear inside a RUNNING tool-call block. Zero hits is a hard refusal — a
    handle pointing at another pane cannot show a command that pane is not running, and
    "helping" by typing the nonce in would forge the very proof being sought.
    """
    from voice.backend.claude_code.pane import Pane, session_registry

    pane = Pane(terminal=terminal, run=run, read_timeout=read_bound_s,
                start_granularity=start_granularity_s, ps_timeout=ps_bound_s, now=now)
    # The record for the pid `find_claude_ancestor` just proved. `bind` never reads it itself:
    # the fetch is the launcher's, so a replayed fixture can inject one and the production path
    # has exactly one place that touches the filesystem.
    binding = await pane.bind(nonce=nonce, session_id=session_id, ancestor_pid=claude_pid,
                              registry=session_registry(claude_pid))
    return {"pane": pane, "binding": binding}


def build_claude_code_backend(*, binding: dict, session_id: str, ledger: Any,
                              pane: Any, transcript: Path,
                              connect_bound_s: float = RELAY_CONNECT_BOUND_S,
                              start_granularity_s: float = PS_START_GRANULARITY_S,
                              ps_bound_s: float = PS_PROBE_BOUND_S,
                              now=time.time, env: dict | None = None,
                              on_qualified: Any = None) -> Any:
    """Assemble the adapter over a PROVEN binding. Every port comes from this module.

    The five injected ports are the daemon's whole contribution: the relay's connect bound, the
    `ps` probe bound, the `ps` start-time granularity the identity re-check compares against,
    the pane's read bound (already spent inside `prove_ownership`), and the clock. The components themselves
    are the backend worker's and are constructed, never reimplemented, here.
    """
    from voice.backend.claude_code.adapter import ClaudeCodeBackend
    from voice.backend.claude_code.relay import RelayActuator, relay_binding, relay_env
    from voice.backend.claude_code.transcript import Tailer

    socket_path, token = relay_env(env)
    bound, refusal = relay_binding(binding, socket_path, token)
    if bound is None:
        # The token is read inside `relay_env` and never leaves it; only the REASON travels.
        raise RuntimeError(f"claude_code relay binding refused: {refusal}")
    # The screen handle is carried under its own key and never as `handle`: the WAL binds
    # effects to `handle or socket`, and a screen handle there would re-attribute every
    # posted frame to a terminal it was never sent through.
    bound["observe_handle"] = str(getattr(pane, "terminal", "") or "")

    relay = RelayActuator(
        binding=bound, ledger=ledger, token=token,
        producer={"pid": os.getpid(), "session_id": session_id},
        connect_timeout=connect_bound_s, start_granularity=start_granularity_s,
        ps_timeout=ps_bound_s, now=now)
    tailer = Tailer(Path(transcript), session_id=session_id)
    # Seed from the history and park the cursor at its end. Without this the first poll
    # read the whole transcript as NEW and replayed every historical turn as live events
    # (4,079 observations at one start, measured 2026-09-20) on top of decoding it all.
    tailer.bootstrap()
    return ClaudeCodeBackend(pane=pane, tailer=tailer, relay=relay, binding=bound,
                            on_qualified=on_qualified, wake=_transcript_wake(Path(transcript)))


class VoiceDaemon:
    """The object graph, assembled once and owned for the life of the process."""

    def __init__(self, *, session_id: str, provider: str = "", backend_name: str = "",
                 state: Path | None = None, connect: Any = None,
                 open_output: Any = None, open_input: Any = None,
                 audio_lock: Any = None, drivers: dict[str, Any] | None = None,
                 open_watch: Any = None, claude_code: dict[str, Any] | None = None,
                 now=time.time) -> None:
        self.session_id = session_id
        self.provider = provider or voice_config.cfg("VOICE_LIVE_PROVIDER",
                                                     providers.DEFAULT_PROVIDER)
        self.backend_name = backend_name or voice_config.cfg("VOICE_BACKEND", _default_backend())
        self.dir = state or state_dir(session_id)
        self._connect = connect
        self._open_output = open_output
        self._open_input = open_input
        self._audio_lock = audio_lock
        # `claude_jobs` needs four host MCP callables this process cannot import. The
        # caller injects them; without them that backend refuses at BUILD, not at the
        # first dispatch, which is the difference between a clear start-up error and a
        # voice session with no hands.
        self._drivers = drivers or {}
        self._open_watch = open_watch
        # `{pane, binding, transcript}` from the launcher's handshake; only
        # VOICE_BACKEND=claude_code needs it.
        self._claude_code = claude_code or {}
        self.control: ControlWatcher | None = None
        self._now = now
        self.status = Status(self.dir / STATUS_NAME, session_id=session_id,
                             session_cwd=os.getcwd(),
                             mode=voice_config.cfg("VOICE_MODE", "relay"), now=now)
        self.sink: Any = None
        self.capture: Any = None
        self.session: Any = None
        self.strategy: Any = None
        self.backend: Any = None
        self.ledger: Any = None
        self.loop: Any = None
        self._lock_held = False

    # ------------------------------------------------------------------ assembly

    def build(self) -> None:
        """Assemble every part. Raises before anything is opened if the env is incomplete."""
        check_credentials(self.provider)
        preflight(self.provider, self.backend_name)      # refuses before anything opens
        from voice.agent.ledger import Ledger
        from voice.agent.loop import AgentLoop
        from voice.audio.io import PlaybackSink

        profile = providers.get(self.provider)
        self.profile = profile
        voice_platform.private_dir(self.dir)
        sink_kwargs = {"open_output": self._open_output} if self._open_output else {}
        self.sink = PlaybackSink(**sink_kwargs)
        self.session, self.strategy, self.voice = self._leg()
        # The ledger comes FIRST: the relay actuator writes its outbox through it, so a
        # claude_code backend cannot be built before it exists.
        self.ledger = Ledger(str(self.dir / "ledger.jsonl"), effect_vocabulary())
        try:
            self.backend = build_backend(self.backend_name, drivers=self._drivers,
                                         claude_code=self._build_claude_code)
        except BaseException:
            # A REFUSED BACKEND MUST NOT STRAND THE WAL. The ledger holds an append handle
            # from the moment it opens, and `shutdown` never runs for a build that raised —
            # so the descriptor would leak, and a restart loop would leak one per attempt.
            self._close_ledger()
            self.ledger = None
            raise
        self.tools = profile.tools()
        # Approvals stay on the keyboard (DESIGN.md §Consent): no backend here has a key-press surface,
        # so the broker never arms a spoken consent challenge; a dialog is narrated instead.
        self.loop = AgentLoop(session=self.session, strategy=self.strategy,
                              backend=self.backend, ledger=self.ledger, sink=self.sink,
                              terminal_only=True, voice=self.voice)
        self.status.set(phase="built")

    def _leg(self) -> tuple[Any, Any, Any]:
        """One provider session with its strategy and voice port, unopened. The first one at
        build; every relay builds another the same way."""
        from voice.live.port import RedactedVoice

        profile = self.profile
        session = build_session(self.provider, sink=self.sink, connect=self._connect)
        # Every word bound for the provider passes one redaction point, whatever the provider.
        return (session, profile.build_strategy(session, self.sink),
                RedactedVoice(profile.build_voice(session)))

    def _build_claude_code(self) -> Any:
        """The real Claude Code graph, over a binding this process already proved.

        The handshake runs in the LAUNCHER (`cmd_start`), because only a descendant of the
        operator's Claude process can resolve its pid; by the time a detached child runs its
        ancestors are launchd's. So the daemon RECEIVES the proven binding and pane rather
        than proving anything itself, and refuses loudly when either is missing.
        """
        proof = self._claude_code
        if not proof:
            raise RuntimeError(
                "VOICE_BACKEND=claude_code needs the ownership handshake: start it with "
                "`NONCE=<nonce> voice-daemon --nonce <nonce> [--terminal <orca handle>] start` from inside "
                "the target session's own Bash tool.")
        binding = proof.get("binding") or {}
        if not binding.get("bound"):
            raise RuntimeError("claude_code ownership refused: "
                               f"{binding.get('refusal') or REFUSE_UNPROVEN}")
        transcript = proof.get("transcript")
        if not transcript:
            raise RuntimeError(
                f"claude_code: {REFUSE_NO_TRANSCRIPT} — no transcript .jsonl for session "
                f"{self.session_id!r} under the Claude projects directory.")
        return build_claude_code_backend(
            binding=binding, session_id=self.session_id, ledger=self.ledger,
            pane=proof["pane"], transcript=Path(transcript),
            connect_bound_s=RELAY_CONNECT_BOUND_S,
            start_granularity_s=PS_START_GRANULARITY_S,
            ps_bound_s=PS_PROBE_BOUND_S, now=self._now,
            on_qualified=self._relay_qualified)

    # ------------------------------------------------------------------ cues

    def _relay_qualified(self) -> None:
        """The self-test came back: the relay can carry a send. This is the moment the
        operator can act on, so it is the moment they hear."""
        if getattr(self, "_shut", False):
            return                        # a late receipt during the goodbye: no hello now
        self.status.set(relay="qualified")
        self._cue(cue.START)
        if self.loop is not None:
            self._spawn(self.loop.notice_open(), "greeting")

    def _spawn(self, coro: Any, what: str) -> None:
        """A task the daemon does not wait for. Its failure is reported, never fatal, and
        never left as an unretrieved exception."""
        task = asyncio.ensure_future(coro)

        def _report(done: asyncio.Future) -> None:
            if done.cancelled() or done.exception() is None:
                return
            print(f"voice: {what} failed: {voice_config.redact_text(str(done.exception()))}",
                  file=sys.stderr)
        task.add_done_callback(_report)

    async def _farewell(self, reason: str) -> None:
        """Let the model say goodbye in its own words, and let the speaker finish it, before
        the socket goes. Skipped when the stream already ended (nothing to say it through)
        or the speaker was never ours. The loop must still be pumping here — it is
        cancelled after."""
        if (self.loop is None or self.session is None or not self._lock_held
                or reason == "stream ended"):
            return
        try:
            # b3: lifecycle-ports begin
            await asyncio.wait_for(self.loop.farewell(reason), FAREWELL_BOUND_S)
            # b3: lifecycle-ports end
        except asyncio.TimeoutError:
            profile = getattr(self, "profile", None)
            if profile is None or profile.capabilities.response_identity:
                print("voice: goodbye did not come back in time", file=sys.stderr)
                return
            # No response identity: no event says the goodbye is over, so the bound IS its
            # window. It ran out as designed; drain what the speaker holds below.
        except Exception as exc:
            print(f"voice: goodbye failed: {voice_config.redact_text(str(exc))}",
                  file=sys.stderr)
            return
        if self.sink is None:
            return
        # The words are done on the wire; now let the device finish playing them — but a
        # device that never takes them must not hold the ending.
        try:
            # b3: lifecycle-ports begin
            await asyncio.wait_for(self.sink.drained(asyncio.get_running_loop()),
                                   FAREWELL_BOUND_S)
            # b3: lifecycle-ports end
        except asyncio.TimeoutError:
            print("voice: the speaker did not finish the goodbye in time", file=sys.stderr)

    def _cue(self, kind: str) -> None:
        """One of the daemon's two sounds, through the sink the model speaks through. Only
        while the speaker is ours: with the audio lock refused there is no device to sound."""
        if self.sink is not None and self._lock_held:
            self.sink.play(cue.response_id(kind), cue.ITEM_ID, cue.earcon(kind))

    # ------------------------------------------------------------------ host callbacks

    async def on_control(self, command: str) -> None:
        """A command from the CLI -- or from the backend, when the operator asked it by voice
        to stop: there is no spoken control path of its own. On, status, off: nothing else."""
        if command == "stop":
            self.status.set(phase="exiting")
            await self.shutdown("stopped by control")

    # ------------------------------------------------------------------ lifecycle

    def acquire_audio(self, *, bound_s: float = AUDIO_LOCK_BOUND_S) -> bool:
        """One mic and one speaker per machine. A refusal is reported, never a crash."""
        from voice.audio.io import AudioLock

        lock = (self._audio_lock if self._audio_lock is not None
                else AudioLock(poll_s=AUDIO_LOCK_POLL_S))
        self._lock = lock
        self._lock_held = bool(lock.acquire(timeout=bound_s))
        if not self._lock_held:
            self.status.set(audio_ready=False,
                            audio_error="audio-device busy (another voice surface is active)")
        return self._lock_held

    async def start(self) -> None:
        """Open audio, open the socket, and run the loop until both streams end."""
        from voice.audio.io import Capture

        if self.loop is None:
            self.build()
        if not self.acquire_audio():
            self.status.end("audio busy")
            raise RuntimeError("audio-device busy (another voice surface is active)")
        self.sink.start()
        capture_kwargs = {"open_input": self._open_input} if self._open_input else {}
        # Through `_send_audio`, never a bound method of one session: a relay replaces it.
        self.capture = Capture(asyncio.get_running_loop(), self._send_audio,
                               **capture_kwargs)
        self.capture.start()
        self.control = ControlWatcher(self.dir, self.on_control,
                                      open_watch=self._open_watch)
        self.control.start(asyncio.get_running_loop())
        self.status.set(audio_ready=True, phase="connecting")
        session = self.session
        await session.start(self._session_config())
        if getattr(self, "_shut", False):
            # A `stop` (or TERM) landed while the socket was opening: the ending already
            # ran — against a session that had no socket yet, so the one just opened is
            # closed here — and nothing below may bring the session up behind it. The
            # ending is awaited, as `start`'s own finally-clause would have.
            try:
                await session.close()
            except Exception:
                pass
            await self.shutdown("stream ended")
            return
        self._mark_leg_opened()
        self.status.set(phase="running", relay="live")
        try:
            # A TERM ends the session the same way a control `stop` does — goodbye, tone,
            # device released — instead of killing it mid-word.
            voice_platform.install_signal(
                asyncio.get_running_loop(), signal.SIGTERM,
                lambda: self._spawn(self.shutdown("terminated"), "shutdown"))
        except (NotImplementedError, RuntimeError):
            pass
        qualify = getattr(self.backend, "qualify", None)
        if qualify is not None:
            # The relay refuses every operator send until its self-test frame is receipted.
            # Posting it is the daemon's job, once, before the loop can dispatch anything;
            # the receipt arrives through the loop's own observation of the transcript.
            why = await qualify()
            if why:
                print(f"voice: relay probe refused: {voice_config.redact_text(why)}",
                      file=sys.stderr)
            self.status.set(relay="probing" if not why
                            else f"degraded:relay-probe-failed:{why}")
        try:
            await self._run()
        finally:
            await self.shutdown(getattr(self, "_lost", "") or "stream ended")

    async def _run(self) -> None:
        """The loop as a task the daemon owns, so `shutdown` can end it: the backend stream
        waits on a file watch and never ends by itself (a `stop` left the process alive,
        measured 2026-09-21). Under the operator's session cap when one is set."""
        if getattr(self, "_shut", False):
            return                        # ended during the relay probe: do not start it
        self.loop.reconnect = self._relay
        self._loop_task = asyncio.ensure_future(self.loop.run())
        idle = idle_close_s()
        watch = asyncio.ensure_future(self._idle_watch(idle)) if idle > 0 else None
        every = rollover_s()
        rollover = asyncio.ensure_future(self._rollover_watch(every)) if every > 0 else None
        self._rollover_task = rollover
        cap = session_max_s()
        try:
            if cap <= 0:
                await self._loop_task
                return
            # b3: lifecycle-ports begin
            # Shielded: the cap must not cancel the loop itself, because the goodbye is
            # spoken THROUGH the loop — `_end` cancels it after the words are out.
            await asyncio.wait_for(asyncio.shield(self._loop_task), cap)
            # b3: lifecycle-ports end
        except asyncio.TimeoutError:
            await self.shutdown("session limit")
        except asyncio.CancelledError:
            if not getattr(self, "_shut", False):
                raise
        finally:
            for task in (watch, rollover):
                if task is not None:
                    task.cancel()

    async def _idle_watch(self, idle: float) -> None:
        """End the session after `idle` seconds in which neither side produced an event,
        unless the backend is working at that moment: then its result is still to be heard,
        and the quiet stretch only starts the next window."""
        stirred = self.loop.stirred
        spoke_through = False             # one window of grace for a single long utterance
        while not getattr(self, "_shut", False):
            stirred.clear()
            try:
                # b3: lifecycle-ports begin
                await asyncio.wait_for(stirred.wait(), idle)
                # b3: lifecycle-ports end
                spoke_through = False
                continue
            except asyncio.TimeoutError:
                pass
            if getattr(self.loop, "speaking", False) and not spoke_through:
                spoke_through = True      # mid-utterance: one more window, not an open end
                continue
            working = await self._backend_working()
            if stirred.is_set():
                spoke_through = False     # something happened during the read: a new window
                continue
            if working:
                continue
            await self.shutdown("idle")
            return

    async def _backend_working(self) -> bool:
        """The backend's own account, read fresh when a window ends. A turn waiting on a
        dialog is waiting on the operator, not working; a lost owner will never finish."""
        refresh = getattr(self.backend, "refresh", None)     # optional: a fresh owner read
        try:
            state = await (refresh or self.backend.state)()
        except Exception as exc:
            # A failed fresh read must not end the watch: that would leave the session open
            # for good. The cached account decides this window instead.
            print(f"voice: idle read failed, using the cached state: "
                  f"{voice_config.redact_text(str(exc))}", file=sys.stderr)
            try:
                state = await self.backend.state()
            except Exception:
                return False              # no account at all: the quiet window decides
        dialog = state.dialog
        if dialog is not None:
            # A dialog the read just found may not have been spoken yet: it gets one window
            # of its own before it counts as waiting on the operator.
            seen = getattr(dialog, "occurrence_id", id(dialog))
            if getattr(self, "_dialog_given", None) != seen:
                self._dialog_given = seen
                return True
        return state.activity == "working" and dialog is None and not state.owner_lost

    # ------------------------------------------------------------------ the provider relay

    def _stopping(self) -> asyncio.Event:
        event = getattr(self, "_stop_event", None)
        if event is None:
            event = self._stop_event = asyncio.Event()
        return event

    def _renewed(self) -> asyncio.Event:
        event = getattr(self, "_renew_event", None)
        if event is None:
            event = self._renew_event = asyncio.Event()
        return event

    def _mic_backlog(self) -> Any:
        found = getattr(self, "_mic_gap", None)
        if found is None:
            from collections import deque

            found = self._mic_gap = deque(maxlen=MIC_GAP_BLOCKS)
        return found

    async def _send_audio(self, pcm: bytes) -> None:
        """The microphone goes to whichever provider session is current. Between a drop and
        its replacement there is none to hear it, so the blocks are HELD, never dropped, and
        the session that takes over gets them first, in order (`_forward_mic`): what the
        operator said in the gap — a barge-in included — still arrives. The call never stops
        listening."""
        backlog = self._mic_backlog()
        ended = getattr(self.loop, "leg_ended", None)
        if (getattr(self, "_reconnecting", False) or backlog
                or (ended is not None and ended.is_set())):
            backlog.append(pcm)          # a gap, or held blocks still ahead of this one
            return
        await self.session.send_audio(pcm)

    async def _forward_mic(self) -> None:
        """The held blocks, in order, to the session that just took over; blocks that arrive
        meanwhile queue behind them. A send that fails leaves the rest held for the next."""
        backlog = self._mic_backlog()
        session = self.session
        while backlog:
            pcm = backlog[0]
            try:
                await session.send_audio(pcm)
            except Exception:
                return                   # it died already: its pump finds the drop
            if backlog and backlog[0] is pcm:
                backlog.popleft()

    def _mark_leg_opened(self) -> None:
        # b3: lifecycle-ports begin  (when the current provider session opened; read only by
        # the barren count, which may only end a reconnect loop)
        self._leg_opened_at = asyncio.get_running_loop().time()
        # b3: lifecycle-ports end

    def _leg_age(self) -> float:
        opened = getattr(self, "_leg_opened_at", None)
        if opened is None:
            return float("inf")
        # b3: lifecycle-ports begin
        return asyncio.get_running_loop().time() - opened
        # b3: lifecycle-ports end

    async def _rollover_watch(self, every: float) -> None:
        """Renew the provider session before its own age limit ends it mid-sentence: after
        `every` seconds of one session, at the next quiet moment, open a fresh one, then close
        the old. A reconnect opened a fresh session already, so it restarts the count."""
        renewed = self._renewed()
        while not getattr(self, "_shut", False):
            renewed.clear()
            try:
                # b3: lifecycle-ports begin
                await asyncio.wait_for(renewed.wait(), every)
                # b3: lifecycle-ports end
                continue
            except asyncio.TimeoutError:
                pass
            current = self.session
            await self._unless_leg_ended(self.loop.until_quiet())
            if getattr(self, "_shut", False):
                return
            if self.loop.leg_ended.is_set():
                continue                  # it died while we waited: the reconnect has it
            if not await self._relay("rollover", current=current):
                print("voice: rollover could not open a fresh provider session; the current "
                      "one carries on", file=sys.stderr)

    async def _unless_leg_ended(self, coro: Any) -> None:
        """Await `coro`, or stop waiting the moment the current provider session's stream
        ends: a dead session has no utterance to protect and no word left to deliver."""
        task = asyncio.ensure_future(coro)
        ended = asyncio.ensure_future(self.loop.leg_ended.wait())
        try:
            await asyncio.wait({task, ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (task, ended):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, ended, return_exceptions=True)
        if not task.cancelled():
            task.result()                 # its own failure still surfaces

    async def _relay(self, reason: str, dead: Any = None, barren: bool = False,
                     current: Any = None) -> bool:
        """Carry the conversation into a fresh provider session. The BACKEND session is not
        touched: its turns and in-flight requests go on, and their results reach the new one.

        `dead` is the session that ended by itself (a reconnect: retried with backoff); None
        is a scheduled rollover of `current` (one open, make-before-break: the old session
        keeps the conversation until the new one is open AND the moment is quiet, and closes
        after — or at once if it dies meanwhile). `barren`: the dead session carried nothing
        but upkeep. False means the conversation did not move to a new session.
        """
        lock = getattr(self, "_relay_lock", None)
        if lock is None:
            lock = self._relay_lock = asyncio.Lock()
        async with lock:
            if getattr(self, "_shut", False):
                return False
            if dead is not None and self.session is not dead:
                self.loop.release()
                return True                   # a rollover replaced it while it was dying
            if current is not None and self.session is not current:
                return True                   # renewed since the rollover chose its moment
            if dead is not None:
                # Barren = carried nothing AND lived about as long as an open: a session that
                # sat idle for an hour before the provider ended it is not a refusal.
                barren = barren and self._leg_age() < RECONNECT_BOUND_S
                self._barren = getattr(self, "_barren", 0) + 1 if barren else 0
                if self._barren >= RECONNECT_ATTEMPTS:
                    self._lost = (f"provider lost: {self._barren} fresh sessions in a row "
                                  "ended before anything was said")
                    return False
                self._reconnecting = True
            self.status.set(reconnecting=True)
            ok = False
            try:
                ok = await (self._reconnect_swap() if dead is not None
                            else self._rollover_swap())
            finally:
                self._reconnecting = False
                self.loop.release()
                self.status.set(reconnecting=False)
            if ok:
                print(f"voice: provider session renewed ({reason}, #{self._relays})",
                      file=sys.stderr)
            return ok

    async def _reconnect_swap(self) -> bool:
        """A fresh session after a drop: RECONNECT_ATTEMPTS tries, backing off between them.
        A try fails when the session does not open or does not take its recap. A stop during
        a backoff ends the wait at once."""
        for attempt in range(RECONNECT_ATTEMPTS):
            if attempt:
                try:
                    # b3: lifecycle-ports begin
                    await asyncio.wait_for(self._stopping().wait(),
                                           RECONNECT_BACKOFF_S * 2 ** (attempt - 1))
                    # b3: lifecycle-ports end
                    return False
                except asyncio.TimeoutError:
                    pass
            leg = await self._open_leg()
            if leg is not None and await self._commit(leg):
                return True
            if getattr(self, "_shut", False):
                return False
        self._lost = f"provider lost: reconnect gave up after {RECONNECT_ATTEMPTS} attempts"
        return False

    async def _rollover_swap(self) -> bool:
        leg = await self._open_leg()
        if leg is None:
            return False
        return await self._commit(leg, quiet=True)

    async def _commit(self, leg: Any, *, quiet: bool = False) -> bool:
        """Hand the conversation to `leg`.

        1. The recap goes in first: a leg that cannot take it is closed and the current
           session stays — nothing is left open.
        2. `quiet` (a rollover): the old session keeps the conversation until the moment is
           quiet with no backend word half-way to it — unless it dies first, then there is
           nothing left to protect.
        3. The swap is synchronous, right after that moment: speech cannot slip in between.
        4. The old session is closed in a `finally`: a stop that cancels the catch-up still
           closes it (the ending awaits this task), so a paid session is never left open.
        """
        if getattr(self, "_shut", False):
            await self._close_quietly(leg[0])
            return False
        try:
            mark = await self.loop.prepare(leg[2])
            if quiet:
                await self._unless_leg_ended(self.loop.settle())
                while True:
                    await self._unless_leg_ended(self.loop.until_quiet())
                    # The LAST look, with no await between it and the swap below: anything
                    # that happened while the wait above wound down sends it round again.
                    if self.loop.leg_ended.is_set() or self.loop.still_quiet():
                        break
        except BaseException as exc:
            await self._close_quietly(leg[0])
            if not isinstance(exc, Exception):
                raise
            print(f"voice: the fresh provider session did not take its recap: "
                  f"{voice_config.redact_text(str(exc) or type(exc).__name__)}", file=sys.stderr)
            return False
        if getattr(self, "_shut", False):
            await self._close_quietly(leg[0])
            return False
        old = self.session
        self.loop.commit(*leg)
        self.session, self.strategy, self.voice = leg
        # The microphone is the new session's from this moment, not from the old one's close.
        self._reconnecting = False
        self._mark_leg_opened()
        self._relays = getattr(self, "_relays", 0) + 1
        self.status.set(relay_count=self._relays)
        self._renewed().set()
        try:
            await self._forward_mic()
            await self.loop.catch_up(mark)
        finally:
            await self._close_quietly(old)
        return True

    async def _open_leg(self) -> Any:
        """Build and open one provider session, with a generation above every earlier one.
        None when it did not open inside the bound; the failure is reported, redacted."""
        self._generation = max(getattr(self, "_generation", 0),
                               getattr(self.session, "generation", 0)) + 1
        leg = self._leg()
        leg[0].generation = self._generation
        try:
            # b3: lifecycle-ports begin
            await asyncio.wait_for(leg[0].start(self._session_config()), RECONNECT_BOUND_S)
            # b3: lifecycle-ports end
        except asyncio.CancelledError:
            await self._close_quietly(leg[0])
            raise
        except Exception as exc:
            print(f"voice: provider session did not open: "
                  f"{voice_config.redact_text(str(exc) or type(exc).__name__)}", file=sys.stderr)
            await self._close_quietly(leg[0])
            return None
        return leg

    @staticmethod
    async def _close_quietly(session: Any) -> None:
        try:
            await session.close()
        except Exception:
            pass

    def _session_config(self) -> Any:
        from voice.live.base import SessionConfig

        return SessionConfig(
            instructions=self._instructions(), voice=voice_config.cfg("VOICE_NAME", "marin"),
            tools=self.tools,
            # One tool, the model's own choice each turn; the loop's receipt follow-up
            # carries the per-response `tool_choice: "none"` that makes it speak, not re-call.
            tool_choice="auto",
            extra=self._session_extra())

    def _instructions(self) -> str:
        from voice.prompts import compose

        try:
            # core + this provider's agency fragment + this backend's consent fragment. A
            # screen-less claude_code binding observes no dialogs: terminal consent wording.
            dialogs = (self._claude_code.get("binding") or {}).get("dialogs", True)
            return compose(self.provider, self.backend_name, dialogs=dialogs)
        except (OSError, ValueError):
            # A missing persona is a real misconfiguration, but it must not be a silent
            # one: the session still opens and the status carries the fault.
            self.status.set(relay="degraded:no-prompt")
            return ""

    def _session_extra(self) -> dict[str, Any]:
        """Provider-specific session fields — VAD, echo cancellation, transcription, effort.
        The provider's profile owns them; the adapter passes them through untouched."""
        profile = getattr(self, "profile", None) or providers.get(self.provider)
        return profile.session_extra() if profile is not None else {}

    def _close_ledger(self) -> None:
        close = getattr(self.ledger, "close", None)
        if close is not None:
            close()

    async def shutdown(self, reason: str) -> None:
        """End the session once, and let every caller wait for that one ending.

        Two callers meet here: a control `stop` (awaited inside the watcher's callback) and
        `start`'s own finally-clause, which runs as soon as the loop task is cancelled. If the
        second merely returned, `start` would return while the first was still closing the
        socket, and `asyncio.run` would cancel it mid-way — no stop tone, sink never closed,
        lock never released (reproduced in-memory by the Astra review). So the first call owns
        the work as a task and the rest await it, shielded: a caller's own cancellation must
        not cancel the ending it was waiting on."""
        ending = getattr(self, "_ending", None)
        if ending is None:
            self._shut = True
            self._stopping().set()
            ending = self._ending = asyncio.ensure_future(self._end(reason))
        await asyncio.shield(ending)

    async def _end(self, reason: str) -> None:
        # The mic first: nothing said from here on opens a new turn under the goodbye.
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        await self._farewell(reason)
        task = getattr(self, "_loop_task", None)
        if task is not None and not task.done():
            task.cancel()
        if self.control is not None:
            self.control.close()
            self.control = None
        rollover = getattr(self, "_rollover_task", None)
        if rollover is not None and not rollover.done():
            # Awaited, not just cancelled: a fresh session it opened and never adopted is
            # closed by its own cleanup, and that close must finish before the process does.
            rollover.cancel()
            await asyncio.gather(rollover, return_exceptions=True)
        if self.session is not None:
            try:
                await self.session.close()
            except Exception:
                pass
        close_backend = getattr(getattr(self, "backend", None), "close", None)
        if close_backend is not None:
            try:
                await close_backend()
            except Exception:
                pass
        if self.sink is not None:
            # The falling tone goes in ahead of the close sentinel, so `close` drains it
            # whatever ended the session — a spoken exit, the cap, or the stream.
            self._cue(cue.STOP)
            self.sink.close(bound_s=CLOSE_BOUND_S)
            self.sink = None
        if self._lock_held:
            self._lock.release()
            self._lock_held = False
        # The WAL holds an append handle for the life of the daemon; a shutdown that leaves
        # it open leaks the descriptor.
        self._close_ledger()
        self.status.end(reason)


# ---------------------------------------------------------------- preflight

def preflight(provider: str, backend: str, platform: str = "") -> dict[str, Any]:
    """provider × backend × platform: what this combination can do, or `Unsupported` naming
    why it cannot run. Called by `build` (so a refused combination never opens a socket, a
    microphone or a paid session) and by `voice-daemon --status`. Reads no secret."""
    from voice.backend import registry as backends

    platform = platform or voice_platform.PLATFORM
    live = providers.get(provider)
    if live is None:
        raise backends.Unsupported(f"unknown VOICE_LIVE_PROVIDER {provider!r}; "
                                   f"expected one of {providers.names()}")
    harness = backends.require(backend, platform)
    caps, hcaps = live.capabilities, harness.capabilities
    notes: list[str] = []
    if not caps.server_echo_cancellation:
        notes.append("no server echo cancellation: use headphones")
    if not caps.response_identity or not hcaps.dialogs:
        notes.append("permission dialogs are answered in the terminal, never by voice")
    if not voice_platform.has_kqueue():
        notes.append("control/transcript watch polls every "
                     f"{WATCH_POLL_S:g}s (no kqueue on {platform})")
    if not hcaps.verified_live:
        notes.append(f"backend {backend} is proven against fakes only")
    return {
        "platform": platform,
        "provider": {"name": provider, "required_env": list(live.required_env),
                     "response_identity": caps.response_identity,
                     "server_echo_cancellation": caps.server_echo_cancellation,
                     "function_tools": caps.function_tools, "input_mute": caps.input_mute},
        "backend": {"name": backend,
                    "platforms": sorted(hcaps.platforms) if hcaps.platforms else "any",
                    "dialogs": hcaps.dialogs, "verified_live": hcaps.verified_live},
        "interruptible": True,
        "supported": True,
        "notes": notes,
    }


def cmd_preflight(args: argparse.Namespace) -> int:
    """`voice-daemon --status`: print the capability sheet for the configured combination,
    or refuse it — before any paid session. Exit 0 supported, 3 unsupported."""
    from voice.backend import registry as backends

    provider = voice_config.cfg("VOICE_LIVE_PROVIDER", providers.DEFAULT_PROVIDER)
    backend = args.backend or voice_config.cfg("VOICE_BACKEND", _default_backend())
    try:
        report = preflight(provider, backend)
    except backends.Unsupported as exc:
        print(json.dumps(voice_config.redact_tree(
            {"supported": False, "provider": provider, "backend": backend,
             "platform": voice_platform.PLATFORM, "reason": str(exc)}), ensure_ascii=False))
        return 3
    try:
        check_credentials(provider)
        report["credentials"] = "present"
    except MissingCredentials as exc:
        report["credentials"] = f"missing: {voice_config.redact_text(str(exc))}"
    profile = backends.get(backend)
    if profile is not None and profile.dialogs_need_screen and not getattr(args, "terminal", ""):
        # Without --terminal the binding is screen-less: no dialog is ever observed.
        report["backend"]["dialogs"] = False
        report["notes"].append(f"{backend} without --terminal: ownership from the session "
                               "transcript; permission dialogs stay in the terminal")
    print(json.dumps(voice_config.redact_tree(report), ensure_ascii=False,
                     indent=2 if args.pretty else None))
    return 0


# ---------------------------------------------------------------- CLI

def resolve_session_id(explicit: str = "") -> str:
    """Which session's state directory to use.

    `VOICE_SESSION_ID` wins over the host's own id because it is the deliberate override:
    the host sets `CLAUDE_CODE_SESSION_ID` in every session, so checking it first would
    make the override unreachable exactly where someone bothered to set it.
    """
    return (explicit or os.environ.get("VOICE_SESSION_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID") or "default")


def _session_id(args: argparse.Namespace) -> str:
    return resolve_session_id(args.session)


async def _handshake(session_id: str, args: argparse.Namespace) -> dict[str, Any]:
    """The `start <nonce>` ownership proof. Runs in the LAUNCHER, before anything opens.

    Only a descendant of the operator's Claude process can resolve its pid (measured chain
    from a Bash tool call: bash → zsh → claude), so this cannot move into a detached child.
    A refusal here leaves the machine as it was found — no audio lock, no audio, no status
    file claiming a call; the session's instance claim taken just before it is released.
    """
    if not args.nonce:
        raise RuntimeError(
            "VOICE_BACKEND=claude_code needs --nonce. Run `NONCE=<n> voice-daemon --nonce <n> "
            "[--terminal <orca handle>] start` from inside the target session's own Bash tool.")
    claude_pid = find_claude_ancestor(session_id)
    if not claude_pid:
        raise RuntimeError(
            f"claude_code: {REFUSE_NO_REGISTRY} — no session registry record names "
            f"session {session_id!r} in this process's ancestry.")
    transcript = resolve_transcript(session_id)
    if transcript is None:
        raise RuntimeError(
            f"claude_code: {REFUSE_NO_TRANSCRIPT} — no transcript .jsonl for session "
            f"{session_id!r} under the Claude projects directory.")
    if args.terminal:
        # An Orca pane: the screen proof, and permission dialogs become observable.
        proof = await prove_ownership(
            nonce=args.nonce, session_id=session_id, terminal=args.terminal,
            claude_pid=claude_pid, run=_pane_runner(), read_bound_s=PANE_READ_BOUND_S,
            start_granularity_s=PS_START_GRANULARITY_S, ps_bound_s=PS_PROBE_BOUND_S)
    else:
        # Any other host (a plain terminal, the Claude desktop app): ancestry + the launch in
        # the session's own transcript. No screen, so no dialogs (DESIGN.md §Backend split).
        from voice.backend.claude_code.pane import (
            REFUSE_NONCE_UNRECORDABLE, REFUSE_NONCE_USED, ScreenlessPane, bind_without_screen,
            session_registry)
        # Single use, claimed ATOMICALLY: one O_EXCL file per nonce in the 0700 state dir. Two
        # launchers racing on one nonce both may verify; only one creates the claim.
        binding = bind_without_screen(
            nonce=args.nonce, session_id=session_id, ancestor_pid=claude_pid,
            registry=session_registry(claude_pid), transcript=transcript,
            ps_timeout=PS_PROBE_BOUND_S, start_granularity=PS_START_GRANULARITY_S,
            now=time.time, env_nonce=os.environ.get("NONCE"))
        if binding.get("bound"):
            # The nonce shape is validated by bind_without_screen: safe as a file name.
            try:
                # A FIXED root, not VOICE_LISTEN_STATE_DIR: single use must hold whichever
                # launcher (and state root) the next attempt uses.
                claims = voice_platform.private_dir(
                    Path.home() / ".local" / "state" / "voice-launch-claims" / session_id)
                os.close(os.open(claims / args.nonce,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                binding = {"bound": False, "refusal": REFUSE_NONCE_USED}
            except OSError as exc:              # cannot record single use → refuse, never bind
                binding = {"bound": False,
                           "refusal": f"{REFUSE_NONCE_UNRECORDABLE}:{type(exc).__name__}"}
        proof = {"pane": ScreenlessPane(ps_timeout=PS_PROBE_BOUND_S), "binding": binding}
    binding = proof["binding"]
    if not binding.get("bound"):
        raise RuntimeError(f"claude_code ownership refused: {binding.get('refusal')}")
    return {**proof, "transcript": transcript, "claude_pid": claude_pid}


def _pane_runner():
    """The ONE place this process may spawn a subprocess for a pane. Read-only by
    construction: it runs `orca terminal read`, and nothing built from it can press a key."""
    async def run(argv: list[str], timeout: float) -> dict:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            # b3: lifecycle-ports begin  (subprocess bound — PANE_READ_BOUND_S, injected)
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            # b3: lifecycle-ports end
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": "pane read timed out"}
        except asyncio.CancelledError:
            # A cancelled await would leave the helper running with nobody to reap it.
            proc.kill()
            await proc.wait()
            raise
        return {"ok": proc.returncode == 0, "stdout": (out or b"").decode(errors="replace"),
                "stderr": (err or b"").decode(errors="replace"),
                "returncode": proc.returncode}

    return run


INSTANCE_LOCK_NAME = "instance.lock"


def claim_session(session_id: str) -> Any:
    """Hold this session's instance lock for the life of the call, or refuse at once.

    Refusing writes nothing: the status file, the control file and the launch claims all
    still belong to the call that is running."""
    from voice.audio.io import AudioLock

    path = voice_platform.private_dir(state_dir(session_id)) / INSTANCE_LOCK_NAME
    lock = AudioLock(path)
    if not lock.acquire():                              # skip-if-busy: never waits
        raise RuntimeError(f"a voice call is already running (or still ending) for this "
                           f"session — `status` shows it, `stop` ends it; or {path} cannot "
                           f"be opened")
    return lock


def _default_backend() -> str:
    from voice.backend import registry as backends

    return backends.DEFAULT_BACKEND


def cmd_start(args: argparse.Namespace) -> int:
    session_id = _session_id(args)
    backend_name = args.backend or voice_config.cfg("VOICE_BACKEND", _default_backend())

    async def go() -> None:
        # One daemon per session, claimed before anything it could disturb: a second start
        # would otherwise rewrite the live call's status as "ended" (it loses the audio lock
        # AFTER publishing) while the first call keeps the microphone open.
        instance = claim_session(session_id)
        try:
            proof: dict[str, Any] = {}
            profile = backends.get(backend_name)
            if profile is not None and profile.ownership_handshake:
                proof = await _handshake(session_id, args)
            daemon = VoiceDaemon(session_id=session_id, backend_name=backend_name,
                                 claude_code=proof)
            await daemon.start()
        finally:
            instance.release()

    from voice.backend import registry as backends

    try:
        preflight(voice_config.cfg("VOICE_LIVE_PROVIDER", providers.DEFAULT_PROVIDER),
                  backend_name)
    except backends.Unsupported as exc:
        # Before the handshake, the lock, the microphone or a paid socket.
        print(f"voice: unsupported: {voice_config.redact_text(str(exc))}", file=sys.stderr)
        return 3
    try:
        asyncio.run(go())
    except MissingCredentials as exc:
        print(f"voice: {voice_config.redact_text(str(exc))}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"voice: {voice_config.redact_text(str(exc))}", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = state_dir(_session_id(args)) / STATUS_NAME
    data = Status.read(path)
    if data is None:
        print(json.dumps({"phase": "absent", "path": str(path)}))
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


def _signal(args: argparse.Namespace, phase: str) -> int:
    """`stop` from a second process.

    The running daemon owns the audio device, so a bare CLI cannot flip it directly. It
    writes the command; `ControlWatcher` in the daemon wakes on the directory change and
    calls the matching host callback. No interval, no poll.
    """
    directory = voice_platform.private_dir(state_dir(_session_id(args)))
    # b3: lifecycle-ports begin  (a STAMP, not a wait: `at` de-duplicates a repeated
    # directory event so one written command is acted on once)
    atomic_write(directory / "control.json", {"command": phase, "at": time.time()})
    # b3: lifecycle-ports end
    print(json.dumps({"ok": True, "command": phase, "dir": str(directory)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-daemon",
                                     description="the voice layer's process")
    parser.add_argument("--session", default="", help="session id (default: env)")
    parser.add_argument("--pretty", action="store_true", help="indent status JSON")
    parser.add_argument("--backend", default="",
                        help="claude_code | claude_jobs | process (default: VOICE_BACKEND)")
    parser.add_argument("--nonce", default="",
                        help="claude_code only: the value of NONCE= set on this command line")
    parser.add_argument("--terminal", default="",
                        help="claude_code only: the Orca terminal handle to READ")
    parser.add_argument("--status", action="store_true",
                        help="preflight: print provider x backend x platform capabilities, "
                             "refuse an unsupported combination (exit 3), start nothing")
    sub = parser.add_subparsers(dest="command")
    for name in ("start", "status", "stop", "preflight"):
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.status or args.command == "preflight":
        return cmd_preflight(args)
    if args.command is None:
        parser.error("a command is required (start | status | stop | preflight)")
    if args.command == "start":
        return cmd_start(args)
    if args.command == "status":
        return cmd_status(args)
    return _signal(args, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
