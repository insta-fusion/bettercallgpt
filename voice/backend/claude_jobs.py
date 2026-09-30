"""Backend adapter over the mesh's Claude job tools.

The job tools already carry the shape a backend needs: `drive_claude_start` hands back a job id
at once, `drive_job_status` LONG-POLLS (that poll is the only wait in this file — no timer, no
sleep, no deadline of ours), `drive_job_steer` writes a correction into the same running run, and
`drive_job_stop` cancels it.

Ground truth this adapter must not round up (the agent-drivers mesh docs, docs/MESH.md §jobs):
  job states  `running` | `finished` | `failed` | `cancelled` — `finished` means the RUN ended,
              not that the task is done; the result text is what says which.
  steer states `delivered` / `received` = it reached the run → posted; `queued_next_turn` = it
              starts the next turn → posted, flagged; `unconfirmed` = the bytes reached the pipe
              and consumption is unknown → uncertain, NEVER resubmitted; `rejected` → refused.

The api is injected so the tests never import the mesh. `mesh_api()` binds the real tools.

Design: voice/DESIGN.md §Timers, §Acceptance E.
"""
from __future__ import annotations

import asyncio

from typing import AsyncIterator

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

TERMINAL_STATES = ("finished", "failed", "cancelled")

# Steer state → what the operator's send actually achieved.
STEER_OUTCOMES = {
    "delivered": ("posted", False),
    "received": ("posted", False),
    "queued_next_turn": ("posted", True),
    "unconfirmed": ("uncertain", False),
    "rejected": ("refused", False),
}


class ClaudeJobsBackend(Backend):
    """Backend over one Claude job. The long-poll is the clock."""

    def __init__(self, api):
        """`api` supplies four async callables and its own long-poll maximum.

        start(prompt, cwd) -> {job_id}
        status(job_id, wait_sec, after_seq) -> {state, seq, events, result, steers}
        steer(job_id, text) -> {state}
        stop(job_id)
        max_wait_sec: the longest poll the api itself accepts — read from the api, never a
            literal here, so this adapter waits exactly as long as the transport allows.
        """
        self._api = api
        self._job_id: str | None = None
        self._activity = "idle"
        self._turn_id: str | None = None
        self._owner_lost: str | None = None
        self._seq = 0
        self._last_state = "idle"
        # Set when a job exists. The loop starts observing BEFORE the operator's first
        # request, when there is no job yet; the observer waits for one instead of ending.
        self._started = asyncio.Event()
        # The tag of the request that STARTED the current job: the observer answers it with a
        # receipt binding tag -> job, which is how the loop knows the job's result answers it.
        self._start_tag: str | None = None
        # steer_id -> the tag of the voice request it carried. The mesh reports a steer's fate
        # in the status document's `steers` list (by steer_id, never by tag); a consumed steer
        # becomes a tagged receipt so the job's result answers that request too.
        self._steer_tags: dict[str, str] = {}
        # Steers whose `steer` call has not returned yet. A terminal status can arrive while
        # one is in flight; its tag must be registered before the result is handed on, or
        # that request is never answered.
        self._steers_in_flight = 0
        self._steers_settled = asyncio.Event()
        self._steers_settled.set()

    @property
    def _wait_sec(self):
        return getattr(self._api, "max_wait_sec", None)

    # ── actuation ────────────────────────────────────────────────────────────────

    async def send(self, text: str, *, tag: str, priority: Priority,
                   transcript: str, interpretation: str) -> Receipt:
        """Idle → start a job. Running → steer the job that is already running."""
        if self._owner_lost:
            return Receipt(outcome="refused", reason=f"owner_lost: {self._owner_lost}", tag=tag)

        if self._job_id is None:
            doc = await self._api.start(text, self._cwd())
            job_id = _text(doc.get("job_id")) if isinstance(doc, dict) else ""
            if not job_id:
                return Receipt(outcome="refused", reason=_refusal(doc, "start returned no job_id"),
                               tag=tag)
            self._job_id = job_id
            self._start_tag = tag
            self._started.set()
            self._turn_id = job_id
            self._activity = "working"
            self._last_state = "running"
            return Receipt(outcome="posted", tag=tag)

        self._steers_in_flight += 1
        self._steers_settled.clear()
        try:
            doc = await self._api.steer(self._job_id, text)
        finally:
            self._steers_in_flight -= 1
            if self._steers_in_flight == 0:
                self._steers_settled.set()
        state = _text(doc.get("state")) if isinstance(doc, dict) else ""
        outcome, queued = STEER_OUTCOMES.get(state, ("uncertain", False))
        steer_id = _text(doc.get("steer_id")) if isinstance(doc, dict) else ""
        if steer_id and outcome != "refused":
            self._steer_tags[steer_id] = tag
        reason = _text(doc.get("reason")) if isinstance(doc, dict) else ""
        if outcome == "uncertain" and state not in STEER_OUTCOMES:
            reason = reason or f"unknown steer state {state!r}"
        elif outcome == "uncertain":
            reason = reason or "write drained but consumption unconfirmed; never resubmit blind"
        elif queued:
            reason = reason or "queued as the run's next turn"
        return Receipt(outcome=outcome, reason=reason, tag=tag)

    async def answer(self, occurrence_id: str, choice: str) -> Receipt:
        return Receipt(outcome="refused", reason="a job exposes no dialog")

    async def cancel(self, turn_id: str) -> Receipt:
        if self._owner_lost:
            return Receipt(outcome="refused", reason=f"owner_lost: {self._owner_lost}")
        if self._job_id is None:
            return Receipt(outcome="refused", reason="no job running")
        doc = await self._api.stop(self._job_id)
        if isinstance(doc, dict):
            if _text(doc.get("state")) == "rejected":
                return Receipt(outcome="refused", reason=_refusal(doc, "stop rejected"))
            # `stopped: false` is not a confirmed stop — say so rather than claim it.
            if doc.get("stopped") is False:
                return Receipt(outcome="uncertain",
                               reason=_text(doc.get("cleanup_note")) or "stop not confirmed")
        return Receipt(outcome="applied")

    async def state(self) -> BackendState:
        return BackendState(activity=self._activity, turn_id=self._turn_id,
                            dialog=None, owner_lost=self._owner_lost)

    # ── observation ──────────────────────────────────────────────────────────────

    def observe(self) -> AsyncIterator[Observation]:
        return self._observe()

    async def _observe(self) -> AsyncIterator[Observation]:
        """Long-poll each job until it is terminal; emit its result; wait for the next one.

        `status(wait_sec=…)` IS the wait: it returns when progress advances past `after_seq` or
        the job goes terminal. Between jobs the observer waits for `send` to start one. A
        FINISHED job is not a lost owner: the backend goes idle and the next request starts a
        new job. Only a status that is not a document (a sentinel) loses the owner.
        """
        while True:
            if self._job_id is None:
                await self._started.wait()
            if self._owner_lost:
                return
            if self._start_tag is not None:
                # Bind the request that started this job to the job, before anything of it.
                yield Observation(kind=OBS_RECEIPT, turn_id=self._turn_id, tag=self._start_tag,
                                  payload={"disposition": "consumed"})
                self._start_tag = None
            doc = await self._api.status(self._job_id, self._wait_sec, self._seq)
            if not isinstance(doc, dict):
                async for obs in self._lose(f"status returned {type(doc).__name__}"):
                    yield obs
                return

            self._seq = _int(doc.get("seq"), self._seq)
            state = _text(doc.get("state")) or self._last_state
            self._last_state = state

            for event in doc.get("events") or ():
                obs = self._event(event)
                if obs is not None:
                    yield obs

            if state in TERMINAL_STATES and self._steers_in_flight:
                # A correction is being written into this job right now: let its `steer` call
                # return and register the tag, so the result below answers it too.
                await self._steers_settled.wait()
            for obs in self._steer_receipts(doc.get("steers"), terminal=state in TERMINAL_STATES):
                yield obs

            if state in TERMINAL_STATES:
                result = Observation(kind=OBS_RESULT, turn_id=self._turn_id,
                                     text=_text(doc.get("result")),
                                     payload={"result_id": self._job_id, "job_state": state,
                                              "failed": state != "finished",
                                              "error": _text(doc.get("error"))})
                # Idle BEFORE the result is handed on: whoever acts on the result may send
                # the next request at once, and it must start a new job, not steer this one.
                self._job_id = None
                self._steer_tags.clear()
                self._seq = 0
                self._activity = "idle"
                self._last_state = "idle"
                self._started.clear()
                yield result
                continue

            self._activity = "working"

    def _steer_receipts(self, steers, *, terminal: bool) -> list[Observation]:
        """Tagged receipts for our steers the job has taken in. `received` / `queued_next_turn`
        are consumed; when the job is over, a steer that was written (`delivered`,
        `unconfirmed`) is bound too — the job's final result is the only answer it will get.
        A rejected steer gets no receipt: its request already heard `refused`."""
        consumed = {"received", "queued_next_turn"}
        if terminal:
            consumed |= {"delivered", "unconfirmed"}
        out: list[Observation] = []
        for steer in steers or ():
            if not isinstance(steer, dict):
                continue
            sid = _text(steer.get("steer_id"))
            state = _text(steer.get("state"))
            if sid in self._steer_tags and state in consumed:
                out.append(Observation(kind=OBS_RECEIPT, turn_id=self._turn_id,
                                       tag=self._steer_tags.pop(sid),
                                       payload={"disposition": "consumed",
                                                "steer_state": state}))
        return out

    def _event(self, event) -> Observation | None:
        """One status event → one observation. Steer receipts attribute to their own text."""
        if not isinstance(event, dict):
            return None
        kind = _text(event.get("kind"))
        if kind == "steer":
            state = _text(event.get("state"))
            return Observation(kind=OBS_RECEIPT, turn_id=self._turn_id,
                               text=_text(event.get("text")),
                               tag=_text(event.get("tag")) or None,
                               payload={"disposition": state or "consumed", "steer_state": state})
        if kind in ("text", "tool", "turn"):
            return Observation(kind=OBS_PROGRESS, turn_id=self._turn_id,
                               text=_text(event.get("text")), payload={"event_kind": kind})
        return None

    async def _lose(self, reason: str):
        self._owner_lost = reason
        self._activity = "unknown"
        yield Observation(kind=OBS_OWNER_LOST, turn_id=self._turn_id, text=reason,
                          payload={"reason": reason})

    def _cwd(self) -> str:
        return getattr(self._api, "cwd", ".")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _refusal(doc, fallback: str) -> str:
    if isinstance(doc, dict):
        for key in ("reason", "error", "sentinel"):
            got = _text(doc.get(key))
            if got:
                return got
    return fallback


def mesh_api(*, max_wait_sec: float, cwd: str = ".", **start_kwargs):
    """Bind the four callables to the real mesh job tools.

    The mesh is imported INSIDE this factory so importing the backend (and its tests) never
    pulls in `agent_drivers`.

    `max_wait_sec` is REQUIRED and carries no default: it is the long-poll bound, and a bound
    written as a literal here would be this file inventing a timer. `drive_job_status` clamps
    `wait_sec` to 0..20 with a bare inline expression and exports no constant to import, so the
    daemon — which owns every lifecycle bound — injects it as the port it already is.
    """
    import json as _json

    import agent_drivers as mesh

    def _decode(raw):
        if isinstance(raw, dict):
            return raw
        text = raw if isinstance(raw, str) else ""
        try:
            doc = _json.loads(text)
        except ValueError:
            # A sentinel (`[drive_job_status ERROR] …`) is a FAILED leg, never an answer.
            return {"sentinel": text, "state": "failed"}
        return doc if isinstance(doc, dict) else {"sentinel": text, "state": "failed"}

    class _MeshApi:
        def __init__(self):
            self.cwd = cwd
            self.max_wait_sec = max_wait_sec

        async def start(self, prompt, job_cwd=None):
            return _decode(await mesh.drive_claude_start(prompt, cwd=job_cwd or self.cwd,
                                                         **start_kwargs))

        async def status(self, job_id, wait_sec, after_seq):
            doc = _decode(await mesh.drive_job_status(job_id, wait_sec=wait_sec,
                                                      after_seq=after_seq))
            # The mesh names progress `progress`/`progress_seq`; the backend reads
            # `events`/`seq`. Translate here so the adapter speaks one vocabulary.
            doc.setdefault("events", doc.get("progress") or [])
            doc.setdefault("seq", doc.get("progress_seq", 0))
            return doc

        async def steer(self, job_id, text):
            return _decode(await mesh.drive_job_steer(job_id, text))

        async def stop(self, job_id):
            return _decode(await mesh.drive_job_stop(job_id))

    return _MeshApi()
