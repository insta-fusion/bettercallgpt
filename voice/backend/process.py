"""Backend adapter over a headless CLI agent child that emits JSON lines on stdout.

One child process = one turn. The child's OWN event ids are the turn identity: nothing here
mints a turn id, invents a clock, or waits on a wall-clock bound. Every wait in this file is a
wait on the child — `readline()` returns when the child writes a line or closes its stdout,
`wait()` returns when the child exits. EOF on stdout is the end of the stream: it produces
`owner_lost` and `observe()` finishes.

A `ChildSpec` carries the dialect: `parser` maps one decoded JSONL line to an `Observation` or
None, so any agent's event vocabulary plugs in. `codex_exec_parser` ships the one for
`codex exec --json`. The prompt travels on argv by default (`codex exec [PROMPT]`); a child that
reads its prompt on stdin declares `prompt_on_stdin=True`.

Design: voice/DESIGN.md §Timers, §Acceptance E7.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import signal
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from voice.backend.base import (
    Backend,
    OBS_OWNER_LOST,
    OBS_PROGRESS,
    OBS_RECEIPT,
    OBS_RESULT,
    BackendState,
    Observation,
    Priority,
    Receipt,
)

# How much of the child's stderr is retained for the owner_lost payload. A SIZE, never a time:
# the ring keeps the last lines, so a chatty child cannot grow this process without bound.
STDERR_RING_LINES = 50


@dataclass(frozen=True)
class ChildSpec:
    """How to spawn one agent child and how to read what it says.

    argv: the command; the operator's text is appended as the prompt argument unless
        `prompt_on_stdin`, in which case it is written to the child's stdin as one line.
    parser: one decoded JSON object → Observation | None. `None` means "not an event the loop
        cares about"; the line is skipped.
    """
    argv: tuple[str, ...]
    parser: Callable[[dict], Observation | None]
    cwd: str | None = None
    env_allowlist: tuple[str, ...] = ()
    prompt_on_stdin: bool = False


def codex_exec_parser(event: dict) -> Observation | None:
    """The `codex exec --json` dialect, as the CLI actually emits it (codex-cli 0.154.0).

    Recorded live — `codex exec --json -s read-only --ephemeral "Reply with only the word READY"`:

        {"type":"thread.started","thread_id":"01a0bbfc-…"}
        {"type":"turn.started"}
        {"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"READY"}}
        {"type":"turn.completed","usage":{…}}

    Two consequences drive this mapping. `turn.started` / `turn.completed` carry NO id, so the
    turn identity is the `thread_id` the child announced — its own id, held by the backend and
    stamped on every later observation. And `turn.completed` carries only usage, so the final
    agent message is the LAST `agent_message` item, which the backend accumulates; the parser
    reports each one and flags the terminal event.

    thread.started       → receipt: the child acknowledged our send, and names the turn
    turn.started         → progress opening the turn
    item.completed (agent_message) → progress carrying that message's whole text
    turn.completed       → result (the backend fills in the final agent message)
    error / turn.failed  → result with payload.failed=True carrying the error message
    """
    kind = event.get("type")

    if kind == "thread.started":
        thread_id = _as_text(event.get("thread_id"))
        return Observation(kind=OBS_RECEIPT, turn_id=thread_id or None,
                           payload={"disposition": "consumed", "thread_id": thread_id})

    if kind == "turn.started":
        return Observation(kind=OBS_PROGRESS, text="", payload={"turn_open": True})

    if kind == "item.completed":
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        # The discriminator is `type` on the item, not `item_type` (verified live).
        if item.get("type") != "agent_message":
            return None
        return Observation(kind=OBS_PROGRESS, text=_as_text(item.get("text")),
                           payload={"item_id": _as_text(item.get("id")), "agent_message": True})

    if kind == "turn.completed":
        return Observation(kind=OBS_RESULT, text="", payload={"usage": event.get("usage")})

    if kind in ("error", "turn.failed"):
        return Observation(kind=OBS_RESULT, text=_codex_error_text(event),
                           payload={"failed": True})

    return None


def _as_text(value) -> str:
    return value if isinstance(value, str) else ""


def _codex_error_text(event: dict) -> str:
    got = _as_text(event.get("message"))
    if got:
        return got
    err = event.get("error")
    if isinstance(err, dict):
        return _as_text(err.get("message"))
    return _as_text(err)


class ProcessBackend(Backend):
    """Backend over a headless JSONL child. One child per turn; no dialog, no steer."""

    def __init__(self, spec: ChildSpec):
        self._spec = spec
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_ring: collections.deque[str] = collections.deque(maxlen=STDERR_RING_LINES)
        self._stderr_task: asyncio.Task | None = None
        self._activity = "idle"
        self._turn_id: str | None = None
        self._owner_lost: str | None = None
        self._pending_tag: str | None = None
        self._last_message = ""
        self._events: asyncio.Queue[Observation] = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._generation = 0
        self._retired = -1
        self._completed = False

    # ── actuation ────────────────────────────────────────────────────────────────

    async def send(self, text: str, *, tag: str, priority: Priority,
                   transcript: str, interpretation: str) -> Receipt:
        """Hand the operator's words to a child.

        A headless child has no steer channel: `now` while a turn is running means SIGINT the
        running child and spawn a new one carrying the new words. `next` reaches a backend only
        when the loop has already decided it should be written immediately.
        """
        if self._owner_lost:
            return Receipt(outcome="refused", reason=f"owner_lost: {self._owner_lost}", tag=tag)

        if self._running():
            if priority != "now":
                return Receipt(outcome="refused",
                               reason="a turn is running; a headless child holds no queue", tag=tag)
            interrupted = await self._interrupt()
            if interrupted.outcome == "refused":
                return Receipt(outcome="refused", reason=interrupted.reason, tag=tag)
            await self._reap()

        try:
            await self._spawn(text)
        except OSError as exc:
            return Receipt(outcome="refused", reason=f"spawn failed: {exc}", tag=tag)

        self._pending_tag = tag
        self._activity = "working"
        return Receipt(outcome="posted", tag=tag)

    async def answer(self, occurrence_id: str, choice: str) -> Receipt:
        return Receipt(outcome="refused", reason="headless child has no dialog")

    async def cancel(self, turn_id: str) -> Receipt:
        if self._owner_lost:
            return Receipt(outcome="refused", reason=f"owner_lost: {self._owner_lost}")
        return await self._interrupt()

    async def state(self) -> BackendState:
        """Derived from the last event observed, never from a clock."""
        return BackendState(activity=self._activity, turn_id=self._turn_id,
                            dialog=None, owner_lost=self._owner_lost)

    async def wait_for_child(self) -> None:
        """Wait until the current child's stream is fully drained and it has exited.

        A wait on the CHILD, not on a clock. Callers that need the turn settled before acting
        use this instead of guessing an interval.
        """
        pump = self._pump
        if pump is not None:
            await pump

    # ── observation ──────────────────────────────────────────────────────────────

    def observe(self) -> AsyncIterator[Observation]:
        return self._observe()

    async def _observe(self) -> AsyncIterator[Observation]:
        """Yield what the child says until it is gone. Ends after `owner_lost`."""
        while True:
            obs = await self._events.get()
            yield obs
            if obs.kind == OBS_OWNER_LOST:
                return

    # ── child lifecycle ──────────────────────────────────────────────────────────

    def _running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _spawn(self, text: str) -> None:
        spec = self._spec
        argv = list(spec.argv)
        if not spec.prompt_on_stdin:
            argv.append(text)

        self._generation += 1
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=self._child_env(),
        )
        self._proc = proc
        self._stderr_ring.clear()
        self._last_message = ""
        self._turn_id = None
        self._completed = False
        if proc.stdin is not None:
            if spec.prompt_on_stdin:
                proc.stdin.write((text + "\n").encode())
                await proc.stdin.drain()
            # Closing stdin is load-bearing, not tidiness: `codex exec` blocks on
            # "Reading additional input from stdin..." while the pipe stays open (measured).
            proc.stdin.close()

        self._stderr_task = asyncio.create_task(self._pump_stderr(proc))
        self._pump = asyncio.create_task(self._pump_stdout(proc, self._generation))

    def _child_env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in self._spec.env_allowlist if name in os.environ}
        # PATH is how a bare command name is found at all; without it exec fails outright.
        env.setdefault("PATH", os.environ.get("PATH", ""))
        return env

    async def _pump_stdout(self, proc: asyncio.subprocess.Process, generation: int) -> None:
        """Read the child's JSONL until EOF, then reap it and announce the loss.

        The only wait is `readline()`; EOF ends the stream. A line that is not JSON, or that the
        parser does not recognize, is skipped — it is the child's business, not ours.
        """
        assert proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                obs = self._decode(raw)
                if obs is None:
                    continue
                await self._events.put(obs)
        finally:
            code = await proc.wait()
            stderr_task = self._stderr_task
            if stderr_task is not None:
                await stderr_task
            if generation > self._retired:
                await self._finish(code)

    def _decode(self, raw: bytes) -> Observation | None:
        """Turn one child line into the observation the loop sees, stamped with the child's id."""
        try:
            event = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        obs = self._spec.parser(event)
        if obs is None:
            return None

        tag = obs.tag
        text = obs.text
        payload = dict(obs.payload)

        if obs.kind == OBS_RECEIPT:
            # The child acknowledged the send we still hold a tag for; the receipt attributes to
            # THAT tag, and the id the child announced becomes this turn's identity.
            if tag is None and self._pending_tag is not None:
                tag, self._pending_tag = self._pending_tag, None
        elif obs.kind == OBS_PROGRESS and payload.get("agent_message"):
            self._last_message = text
        elif obs.kind == OBS_RESULT and not text and not payload.get("failed"):
            # `turn.completed` carries usage, not text: the result is the whole last agent
            # message, never clipped.
            text = self._last_message

        if obs.turn_id:
            self._turn_id = obs.turn_id
        if obs.kind == OBS_RESULT:
            payload.setdefault("result_id", self._turn_id)
            self._activity = "idle"
            # The turn reached its own end. A FAILED result is not a completed turn: the child
            # is expected to die after it, and that death is owner loss, not a clean handover.
            self._completed = not payload.get("failed")
        else:
            self._activity = "working"

        return Observation(kind=obs.kind, turn_id=self._turn_id, text=text, tag=tag,
                           dialog=obs.dialog, payload=payload)

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            self._stderr_ring.append(raw.decode("utf-8", "replace").rstrip("\n"))

    async def _finish(self, code: int | None) -> None:
        """A child's exit is either the expected end of ONE turn, or the loss of the owner.

        One child = one turn, so a child that delivered its result and exited cleanly has simply
        finished: the slot resets and the next `send` spawns a fresh child. Only a child that
        died WITHOUT completing its turn, or exited non-zero, is owner loss — that is the case
        where work may have been swallowed and every later actuation must be refused.
        """
        if self._completed and code == 0:
            self._proc = None
            self._pump = None
            self._activity = "idle"
            return

        self._owner_lost = f"child exited (code {code})"
        self._activity = "unknown"
        await self._events.put(Observation(
            kind=OBS_OWNER_LOST,
            turn_id=self._turn_id,
            text=self._owner_lost,
            payload={"reason": self._owner_lost, "exit_code": code,
                     "stderr": list(self._stderr_ring)},
        ))

    async def _interrupt(self) -> Receipt:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return Receipt(outcome="refused", reason="no running child")
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return Receipt(outcome="refused", reason="child already exited")
        return Receipt(outcome="applied")

    async def _reap(self) -> None:
        """Let the interrupted child finish its stream before a replacement takes its place.

        The generation each pump was started with is what keeps this child's exit from being
        announced as owner loss: retiring the current generation says the turn is being
        REPLACED, not lost. `_spawn` mints the next one.
        """
        proc, pump = self._proc, self._pump
        self._retired = self._generation
        if proc is not None:
            await proc.wait()
        if pump is not None:
            await pump
        self._proc = None
        self._pump = None


def codex_exec_spec(*, cwd: str | None = None, sandbox: str = "read-only",
                    model: str = "", env_allowlist: tuple[str, ...] = ()) -> ChildSpec:
    """A `ChildSpec` for `codex exec --json` — the prompt rides on argv."""
    argv = ["codex", "exec", "--json", "-s", sandbox, "--ephemeral"]
    if model:
        argv += ["-m", model]
    return ChildSpec(argv=tuple(argv), parser=codex_exec_parser, cwd=cwd,
                     env_allowlist=env_allowlist)


# The process dialects this build can read, by `VOICE_PROCESS_DIALECT` name. One registered:
# a new agent's JSONL vocabulary is a parser and one entry here.
DIALECTS: dict[str, Callable[[dict], Observation | None]] = {
    "codex_exec": codex_exec_parser,
}
