"""What the loop needs from the ledger — the durable side of the conversation, as a Protocol.

The conversation log is pure and in memory; everything that must survive a crash lives behind
this port: the write-ahead record placed BEFORE any actuation, the outcome written after it, and
the evidence records other writers append.

`voice/agent/ledger.py` implements it against the WAL; tests implement it in a dict. The loop
knows only these four methods, so neither implementation can leak storage detail into decisions.

Design: voice/DESIGN.md §Conversation model, §Acceptance A4, E5.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

# What a recorded op finally came to. `uncertain` is the crash verdict for an op written but
# never resolved: it is reported, never replayed.
Outcome = Literal["posted", "applied", "refused", "uncertain"]

RecoveredState = Literal["posted", "applied", "refused", "uncertain"]


@runtime_checkable
class LedgerPort(Protocol):
    """Durable memory. Every method is synchronous: the loop must be able to write the WAL
    record before actuation without an await between the decision and the record."""

    def record_op(self, op_id: str, request_id: str, revision: int,
                  kind: str, payload: dict[str, Any]) -> None:
        """Write-ahead: this op is ABOUT to be actuated. Called before the actuation, always."""
        ...

    def set_outcome(self, op_id: str, outcome: Outcome) -> None:
        """What the actuation returned. An op with no outcome recovers as `uncertain`."""
        ...

    def recover(self) -> dict[str, RecoveredState]:
        """After a restart: op id → state. Ops without an outcome are `uncertain`."""
        ...

    async def append(self, record: dict[str, Any]) -> int:
        """Carry one raw record from another writer (the relay's intents; the loop's `heard` /
        `spoken` transcripts) and return its ordinal. Evidence only: `recover()` never reads it."""
        ...
