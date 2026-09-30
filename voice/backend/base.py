"""The backend seam — what any agent harness must look like to the agent loop.

A backend OBSERVES one harness (state, progress, results, receipts, dialogs) and ACTUATES it
(send text, answer a dialog, cancel a turn). It decides no meaning: the words it sends are the
operator's, the dialog it reports is the harness's, the occurrence id is minted from what it saw.
Three adapters implement this: `claude_code/` (relay socket + transcript tailer + pane),
`claude_jobs.py` (the mesh's job tools), `process.py` (any stdin-in / JSONL-out CLI agent).

Design: voice/DESIGN.md §Backend split, §Acceptance E.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

Priority = Literal["now", "next"]

# Observation kinds. `result` is the only thing that mints a result; `receipt` proves a tagged
# send entered the conversation; `dialog` is the harness asking a question or a permission.
OBS_STATE = "state"          # payload.activity: idle|working|dialog|unknown, turn_id
OBS_PROGRESS = "progress"    # turn_id, text
OBS_RESULT = "result"        # turn_id, text (whole, never clipped), result_id
OBS_RECEIPT = "receipt"      # tag, turn_id, disposition: consumed|absorbed|queued|withdrawn
OBS_DIALOG = "dialog"        # dialog (below), transition: open|closed|replaced
OBS_TYPED = "typed"          # text the operator typed into the harness themselves
OBS_OWNER_LOST = "owner_lost"  # reason — every later actuation must be refused


@dataclass(frozen=True)
class Dialog:
    occurrence_id: str        # changes every time a dialog appears after being absent
    kind: Literal["permission", "question"]
    prompt: str               # verbatim
    action: str | None        # extracted action, or None when not extractable → never armed
    scope: str | None
    options: tuple[tuple[str, str], ...] = ()   # (key, label)


@dataclass(frozen=True)
class Observation:
    kind: str
    turn_id: str | None = None
    text: str = ""
    tag: str | None = None
    dialog: Dialog | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    """What an actuation returned. `posted` = written to the harness; `refused` = not written,
    with the reason; `uncertain` = written but unconfirmed (never resubmitted)."""
    outcome: Literal["posted", "applied", "refused", "uncertain"]
    reason: str = ""
    tag: str | None = None


@dataclass(frozen=True)
class BackendState:
    activity: Literal["idle", "working", "dialog", "unknown"]
    turn_id: str | None
    dialog: Dialog | None
    owner_lost: str | None = None


class Backend(Protocol):
    def observe(self) -> AsyncIterator[Observation]: ...
    async def state(self) -> BackendState: ...

    async def send(self, text: str, *, tag: str, priority: Priority,
                   transcript: str, interpretation: str) -> Receipt:
        """Hand the operator's words to the harness. `text` is what is written; `transcript`
        and `interpretation` are both carried so the harness agent sees evidence and reading.
        `now` aborts the running turn where the harness supports it (Claude Code relay
        `priority: "now"`); `next` is queued by the LOOP, not here — a backend only ever
        receives a send it should write immediately."""
        ...

    async def answer(self, occurrence_id: str, choice: str) -> Receipt:
        """Press a dialog answer. Refused if the occurrence id is not the one on screen."""
        ...

    async def cancel(self, turn_id: str) -> Receipt:
        """Stop a turn where the harness supports it; `refused` with a reason otherwise."""
        ...

    async def close(self) -> None:
        """Release what the backend holds when the session ends. No-op by default; a backend
        that owns a resource past the session overrides it."""
        return None
