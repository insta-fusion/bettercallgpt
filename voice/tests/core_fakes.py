"""Fakes for the core tests: a ledger, a live session, a backend, an audio sink, a strategy.

Each one is the smallest thing that satisfies its contract and records what it was asked to do,
so a test asserts against calls rather than against internal state. No fake has a clock.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from ..backend import base as backend_base
from ..live import base as live_base


class FakeLedger:
    """An in-memory `LedgerPort`. The dict IS the write-ahead log."""

    def __init__(self) -> None:
        self.ops: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.outcomes: dict[str, str] = {}
        self.appended: list[dict[str, Any]] = []

    async def append(self, record):
        self.appended.append(dict(record))
        return len(self.appended)

    def record_op(self, op_id, request_id, revision, kind, payload):
        self.ops[op_id] = {"request_id": request_id, "revision": revision,
                           "kind": kind, "payload": payload}
        self.order.append(op_id)

    def set_outcome(self, op_id, outcome, reason=""):
        self.outcomes[op_id] = outcome

    def recover(self):
        return {op_id: self.outcomes.get(op_id, "uncertain") for op_id in self.order}


class FakeSession:
    """Records every write. `events()` replays whatever the test queued."""

    generation = 0

    def __init__(self, events: list[live_base.LiveEvent] | None = None) -> None:
        self._events = list(events or [])
        self.items: list[tuple[dict[str, Any], str]] = []
        self.responses: list[dict[str, Any]] = []
        self.outputs: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self.truncated: list[tuple[str, int]] = []
        self.closed = False

    async def start(self, config):
        return None

    async def send_audio(self, pcm):
        return None

    def events(self) -> AsyncIterator[live_base.LiveEvent]:
        async def gen():
            for event in self._events:
                yield event
        return gen()

    async def create_response(self, origin, *, instructions=None, tool_choice=None):
        self.responses.append({"origin": origin, "instructions": instructions,
                               "tool_choice": tool_choice})

    async def add_item(self, item, origin):
        self.items.append((item, origin))

    async def call_output(self, call_id, output):
        self.outputs.append((call_id, output))

    async def cancel_response(self, response_id):
        self.cancelled.append(response_id)

    async def truncate(self, item_id, audio_end_ms):
        self.truncated.append((item_id, audio_end_ms))

    async def close(self):
        self.closed = True

    def item_texts(self) -> list[str]:
        """Every text our code put into the session — what D1 asserts over."""
        texts = []
        for item, _origin in self.items:
            for part in item.get("content", []):
                texts.append(str(part.get("text", "")))
        return texts


class FakeBackend:
    """A scriptable backend. `receipts` pops per-call outcomes; the default is `posted`."""

    def __init__(self, *, activity="idle", turn_id=None, dialog=None) -> None:
        self._state = backend_base.BackendState(activity=activity, turn_id=turn_id, dialog=dialog)
        self.sends: list[dict[str, Any]] = []
        self.answers: list[tuple[str, str]] = []
        self.cancels: list[str] = []
        self.receipts: list[backend_base.Receipt] = []
        self.answer_receipts: list[backend_base.Receipt] = []
        self._observations: list[backend_base.Observation] = []

    def set_state(self, *, activity, turn_id=None, dialog=None, owner_lost=None):
        self._state = backend_base.BackendState(activity=activity, turn_id=turn_id,
                                                dialog=dialog, owner_lost=owner_lost)

    def observe(self) -> AsyncIterator[backend_base.Observation]:
        async def gen():
            for obs in self._observations:
                yield obs
        return gen()

    async def state(self):
        return self._state

    async def send(self, text, *, tag, priority, transcript, interpretation):
        self.sends.append({"text": text, "tag": tag, "priority": priority,
                           "transcript": transcript, "interpretation": interpretation})
        if self.receipts:
            return self.receipts.pop(0)
        return backend_base.Receipt(outcome="posted", tag=tag)

    async def answer(self, occurrence_id, choice):
        self.answers.append((occurrence_id, choice))
        if self.answer_receipts:
            return self.answer_receipts.pop(0)
        return backend_base.Receipt(outcome="applied")

    async def cancel(self, turn_id):
        self.cancels.append(turn_id)
        return backend_base.Receipt(outcome="applied")


class FakeSink:
    """An audio sink that answers by response id. `reached_end` / `epoch_unchanged` are what the
    broker's third piece of delivery evidence reads."""

    def __init__(self) -> None:
        self.ended: set[str] = set()
        self.epoch_moved: set[str] = set()
        self.played: list[tuple[str, str, int]] = []

    def play(self, response_id, item_id, pcm):
        self.played.append((response_id, item_id, len(pcm)))

    def cancel(self, response_id):
        self.epoch_moved.add(response_id)
        return 0

    def rendered_ms(self, response_id, item_id):
        return 100 if response_id in self.ended else None

    def reached_end(self, response_id) -> bool:
        return response_id in self.ended

    def epoch_unchanged(self, response_id) -> bool:
        return response_id not in self.epoch_moved


class FakeStrategy:
    """Yields the decisions a test scripted, keyed by the event seq that should produce them."""

    def __init__(self, decisions: dict[int, list[live_base.Decision]] | None = None) -> None:
        self.decisions = decisions or {}
        self.seen: list[live_base.LiveEvent] = []

    async def feed(self, event):
        self.seen.append(event)
        return list(self.decisions.get(event.seq, []))


def event(seq, kind, **kwargs) -> live_base.LiveEvent:
    return live_base.LiveEvent(seq=seq, kind=kind, **kwargs)


def run(coro):
    return asyncio.run(coro)
