"""WAL-backed operation ledger with crash recovery."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voice.config import redact_tree


@dataclass(frozen=True)
class WalRecord:
    """One record appended to the ledger."""
    kind: str
    op_id: str | None = None
    request_id: str | None = None
    revision: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class Ledger:
    """Append-only JSONL ledger with fsync, tolerant of torn last line."""

    def __init__(self, path: str, effects: frozenset[str]):
        """
        Create or open a ledger.

        Args:
            path: File path for the JSONL WAL.
            effects: Frozenset of allowed `kind` values for record_op.
        """
        self.path = Path(path)
        self.effects = effects
        self._lock = threading.Lock()
        self._foreign_seq = 0
        self._f = None
        self._ops: dict[str, str] = {}  # op_id → outcome
        self.torn = False

        self._open()
        self._recover()

    def _open(self):
        """Open or create the ledger file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 0600: the ledger records what was said and relayed; never world-readable via
        # umask, and an existing file from an older build is tightened too.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        self._f = os.fdopen(fd, "a", buffering=1)

    def _recover(self):
        """Recover state from the WAL, ignoring torn last line."""
        if not self.path.exists():
            return

        with open(self.path, "r") as f:
            lines = f.readlines()

        # Process lines, ignoring torn tail
        seen_ops = set()  # Track ops we've seen (for uncertain detection)
        for line in lines:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # Torn last line; report it and stop
                self.torn = True
                break

            kind = data.get("kind")
            op_id = data.get("op_id")
            outcome = data.get("outcome")

            # Track recorded ops for uncertain detection
            if kind == "record_op" and op_id:
                seen_ops.add(op_id)
                if op_id not in self._ops:
                    self._ops[op_id] = "uncertain"  # Default until outcome is set

            # Recover outcomes (overrides uncertain)
            if kind == "outcome" and op_id:
                self._ops[op_id] = outcome or "uncertain"


    def record_op(
        self,
        op_id: str,
        request_id: str,
        revision: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Record an operation before any actuation.

        Args:
            op_id: Unique operation identifier.
            request_id: Request identifier for result lookup.
            revision: Turn revision number.
            kind: Effect kind (must be in self.effects).
            payload: Effect-specific data.

        Raises:
            ValueError: If kind is not in effects.
        """
        if kind not in self.effects:
            raise ValueError(f"kind {kind!r} not in effects {self.effects}")

        with self._lock:
            record = {
                "kind": "record_op",
                "op_id": op_id,
                "request_id": request_id,
                "revision": revision,
                "effect_kind": kind,
                "payload": payload,
            }
            self._append(record)

    def set_outcome(self, op_id: str, outcome: str, reason: str = "") -> None:
        """
        Record the outcome of an operation.

        Args:
            op_id: The operation identifier.
            outcome: One of "posted", "applied", "refused", "uncertain".
            reason: Why, for a refusal or an uncertainty. Persisted so a refused dispatch
                can be explained after the process is gone (the first live refusal could
                not be: the reason lived only in memory).
        """
        if outcome not in ("posted", "applied", "refused", "uncertain"):
            raise ValueError(f"invalid outcome {outcome!r}")

        with self._lock:
            self._ops[op_id] = outcome
            record = {
                "kind": "outcome",
                "op_id": op_id,
                "outcome": outcome,
            }
            if reason:
                record["reason"] = str(reason)
            self._append(record)

    def recover(self) -> dict[str, str]:
        """
        Recover operation outcomes after a crash.

        Returns a dict mapping op_id to outcome. Ops with no recorded outcome
        are marked "uncertain".
        """
        with self._lock:
            return dict(self._ops)

    async def append(self, record: dict[str, Any]) -> int:
        """Append one raw record from ANOTHER writer to the same WAL and return its ordinal.

        The relay persists its intent, enqueue and observation records here before it
        actuates (write-ahead is the relay's own safety rule). `recover()` reads only the
        kinds this class writes; a foreign record is carried, never interpreted. Measured
        live: without this the relay's first write raised AttributeError and every send —
        the qualification probe included — was refused with nothing written.
        """
        with self._lock:
            self._foreign_seq += 1
            self._append({**record, "seq": self._foreign_seq})
            return self._foreign_seq

    def _append(self, record: dict[str, Any]) -> None:
        """Append a record and fsync. Known secret values are masked in every string field:
        the ledger records what the terminal and the operator said, and it persists."""
        line = json.dumps(redact_tree(record))
        self._f.write(line + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self) -> None:
        """Close the ledger file."""
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
