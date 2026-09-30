"""The conversation log — the ordered truth about what was said, asked and answered.

This file is PURE: no async, no I/O, no clock, no randomness. Every state change is a method
call whose arguments carry their own identity, and every question it answers is a function of
the events it was given. That is what makes the loop testable by replay: feed the same events,
get the same decisions. Durable storage lives behind `LedgerPort`; the log only says what the
ledger should be told.

Three ideas carry the whole model.

**The turn is the identity.** A completed user turn mints a REVISION. A request belongs to the
revision that was current when its input item completed — not to a time window, not to a
reconstructed span. Nothing here measures duration.

**The join is a proof, not a guess.** A dispatch needs input item → user-origin response →
function call. A response created by the broker (origin `challenge`) or by the loop (`narration`,
`availability`) can never carry a dispatchable call, and a call whose response has more than one
candidate input item is REFUSED rather than attributed to the likelier one. Ambiguity that cannot
be resolved by identity is refused, never resolved by heuristic.

**Nothing is shelved.** A request is pending, dispatched, resolved or refused; a result that
arrives after the operator has said something else is still a result. Whether it is stale is
the model's judgment, made from the order of the conversation it can see.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

# Which responses may carry a dispatchable function call. The provider creates a response by
# itself when it commits a user input item; everything else was asked for by our own code, and
# our own code must never be able to launder a request into existence.
USER_ORIGIN = "user"
CHALLENGE_ORIGIN = "challenge"

RequestState = Literal["pending", "dispatched", "resolved", "refused"]

# How many spoken turns the log keeps for a relay's recap (`recent_turns`). A count, not a
# window: the oldest falls off when a new one is noted.
TURNS_KEPT = 32


@dataclass(frozen=True)
class InputItem:
    """One committed thing the operator said."""
    item_id: str
    transcript: str | None = None      # None until transcription completes
    complete: bool = False
    failed: bool = False               # transcription completed empty or errored


@dataclass(frozen=True)
class ResponseRecord:
    response_id: str
    origin: str
    # Input items committed with no later response yet when this response was created. For a
    # `user` origin response this is the candidate set the join draws from.
    candidates: tuple[str, ...] = ()
    done: bool = False
    status: str = ""


@dataclass
class RequestRecord:
    """A dispatchable unit of backend work, bound to the turn that produced it."""
    request_id: str
    revision: int
    input_item_id: str
    response_id: str
    call_id: str
    transcript: str
    interpretation: str
    priority: str
    state: RequestState = "pending"
    turn_id: str | None = None         # the backend turn that is executing it
    refusal: str = ""


@dataclass(frozen=True)
class Tombstone:
    """Something that was armed or pending and is now void, with the reason it died. Kept, not
    deleted: a refusal the operator can be told about is worth more than a silent drop."""
    kind: str
    subject_id: str
    reason: str
    revision: int


class ConversationLog:
    """The ordered log. Append events as they are observed; ask it what is true."""

    def __init__(self) -> None:
        self.revision: int = 0
        self._items: dict[str, InputItem] = {}
        self._item_order: list[str] = []
        self._responses: dict[str, ResponseRecord] = {}
        self._requests: dict[str, RequestRecord] = {}
        self._request_order: list[str] = []
        self._tombstones: list[Tombstone] = []
        # Input items committed since the last response was created — the candidate pool.
        self._uncommitted_candidates: list[str] = []
        # (speaker, text) in the order said — what a relay carries into a fresh provider
        # session. Evidence of the conversation, never an input to any decision here.
        self._turns: list[tuple[str, str]] = []
        self.turns_noted = 0               # every turn ever noted: a mark for `turns_since`

    # ------------------------------------------------------------------ input side

    def commit_input(self, item_id: str) -> None:
        """The provider committed a user input item. It is not yet usable: a dispatch waits for
        its transcript."""
        if item_id in self._items:
            return
        self._items[item_id] = InputItem(item_id=item_id)
        self._item_order.append(item_id)
        self._uncommitted_candidates.append(item_id)

    def complete_transcript(self, item_id: str, text: str, *, failed: bool = False) -> int:
        """Transcription finished for a committed item. THIS is what mints a revision: the turn
        is complete, so everything unresolved from before it is now older.

        Returns the new revision.
        """
        if item_id not in self._items:
            self.commit_input(item_id)
        self._items[item_id] = InputItem(item_id=item_id, transcript=text,
                                         complete=True, failed=failed or not text.strip())
        self.revision += 1
        return self.revision

    def input_item(self, item_id: str) -> InputItem | None:
        return self._items.get(item_id)

    def last_committed_item(self) -> str | None:
        return self._item_order[-1] if self._item_order else None

    # ------------------------------------------------------------------ response side

    def response_created(self, response_id: str, origin: str) -> ResponseRecord:
        """A response began. Its candidate set is frozen here: the input items committed since
        the previous response. A later-committed item can never be attributed to this response,
        which is exactly what keeps a broker challenge out of the join."""
        candidates: tuple[str, ...] = ()
        if origin == USER_ORIGIN:
            candidates = tuple(self._uncommitted_candidates)
            self._uncommitted_candidates = []
        elif origin == CHALLENGE_ORIGIN:
            # Frozen at the challenge: the item stays out of the join on BOTH sides.
            self._uncommitted_candidates = []
        # Any other origin is words from the terminal or the daemon (a result, a greeting,
        # the goodbye) — not a turn. The pool is untouched, or the operator's first request
        # after a greeting would be refused `no_candidate` (found by review).
        record = ResponseRecord(response_id=response_id, origin=origin, candidates=candidates)
        self._responses[response_id] = record
        return record

    def response_done(self, response_id: str, status: str) -> None:
        record = self._responses.get(response_id)
        if record is None:
            return
        self._responses[response_id] = ResponseRecord(
            response_id=record.response_id, origin=record.origin,
            candidates=record.candidates, done=True, status=status)

    def response(self, response_id: str) -> ResponseRecord | None:
        return self._responses.get(response_id)

    # ------------------------------------------------------------------ the join

    def join(self, response_id: str) -> tuple[str | None, str]:
        """Resolve a function call's response id to the ONE input item that produced it.

        Returns `(input_item_id, reason)`. `input_item_id` is None when the call must be refused,
        and `reason` names which proof failed: `no_response`, `not_user_origin`, `no_candidate`,
        `ambiguous`, `transcript_pending`. Every one of those is an identity fact.
        """
        record = self._responses.get(response_id)
        if record is None:
            return None, "no_response"
        if record.origin != USER_ORIGIN:
            return None, "not_user_origin"
        if not record.candidates:
            return None, "no_candidate"
        if len(record.candidates) > 1:
            return None, "ambiguous"
        item_id = record.candidates[0]
        item = self._items.get(item_id)
        if item is None or not item.complete:
            return None, "transcript_pending"
        return item_id, "joined"

    # ------------------------------------------------------------------ requests

    def open_request(self, request_id: str, *, input_item_id: str, response_id: str,
                     call_id: str, interpretation: str, priority: str) -> RequestRecord:
        """Record a joined, transcript-complete call as a request of the CURRENT revision."""
        item = self._items.get(input_item_id)
        record = RequestRecord(
            request_id=request_id,
            revision=self.revision,
            input_item_id=input_item_id,
            response_id=response_id,
            call_id=call_id,
            transcript=(item.transcript if item and item.transcript else ""),
            interpretation=interpretation,
            priority=priority,
        )
        self._requests[request_id] = record
        self._request_order.append(request_id)
        return record

    def refuse_request(self, request_id: str, reason: str) -> None:
        record = self._requests.get(request_id)
        if record is not None:
            record.state = "refused"
            record.refusal = reason
        self._tombstones.append(Tombstone("request", request_id, reason, self.revision))

    def mark_dispatched(self, request_id: str, turn_id: str | None) -> None:
        record = self._requests.get(request_id)
        if record is None:
            return
        record.state = "dispatched"
        record.turn_id = turn_id

    def mark_resolved(self, request_id: str) -> None:
        record = self._requests.get(request_id)
        if record is not None:
            record.state = "resolved"

    def request(self, request_id: str) -> RequestRecord | None:
        return self._requests.get(request_id)

    def requests(self) -> Iterable[RequestRecord]:
        return (self._requests[rid] for rid in self._request_order)

    def unresolved(self) -> list[RequestRecord]:
        return [r for r in self.requests() if r.state in ("pending", "dispatched")]

    # ------------------------------------------------------------------ spoken turns

    def note_turn(self, speaker: str, text: str) -> None:
        """One finished thing said, by the operator, the voice or the backend."""
        if not text.strip():
            return
        self._turns.append((speaker, text.strip()))
        self.turns_noted += 1
        del self._turns[:-TURNS_KEPT]

    def turns_since(self, mark: int) -> list[tuple[str, str]]:
        """The turns noted after `turns_noted` was `mark` (as many as are still kept)."""
        return list(self._turns[-min(self.turns_noted - mark, len(self._turns)):]) \
            if self.turns_noted > mark else []

    def recent_turns(self, n: int) -> list[tuple[str, str]]:
        """The last `n` turns, oldest first."""
        return list(self._turns[-n:]) if n > 0 else []

    # ------------------------------------------------------------------ tombstones

    def tombstone(self, kind: str, subject_id: str, reason: str) -> Tombstone:
        stone = Tombstone(kind, subject_id, reason, self.revision)
        self._tombstones.append(stone)
        return stone

    def tombstones(self) -> list[Tombstone]:
        return list(self._tombstones)
