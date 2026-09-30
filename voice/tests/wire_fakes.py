"""Fakes shared by the wire tests. No device, no socket, no clock.

Named `wire_fakes` rather than `test_wire_fakes` on purpose: the discovery pattern is
`test_wire_*.py`, so a helper module must stay outside it or unittest tries to run it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator


def fixture_events(path: Path) -> Iterator[dict[str, Any]]:
    """The INBOUND events of a recorded session, in wire order.

    `dir: "out"` lines are what the spike client sent and `dir: "mark"` lines are its own
    annotations; neither ever reaches a provider adapter, so neither is replayed.
    """
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("dir") != "in":
                continue
            yield {k: v for k, v in row.items() if k not in ("seq", "t", "dir")}


class FakeStream:
    """The slice of a device output stream `PlaybackSink` writes to."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.aborts = 0
        self.starts = 0
        self.active = True
        self.stopped = False
        self.closed = False

    def write(self, pcm: bytes) -> None:
        if not self.active:
            # Exactly what PortAudio does to a stopped stream, and exactly what killed the
            # writer thread after the first barge-in.
            raise RuntimeError("write to an inactive stream")
        self.writes.append(pcm)

    def abort(self) -> None:
        """Discard what the device still holds AND go inactive, as PortAudio does. A fake
        that stayed active would hide the bug where the next reply writes to a dead
        stream."""
        self.aborts += 1
        self.active = False

    def start(self) -> None:
        self.active = True
        self.starts += 1

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakeSink:
    """An `AudioSink` that records instead of playing, with the same epoch semantics.

    `rendered` is settable per (response, item) so a test can state exactly what the
    operator heard without pushing bytes through a thread.
    """

    def __init__(self) -> None:
        self.plays: list[tuple[str, str, bytes]] = []
        self.plays_after_cancel: list[tuple[str, str, bytes]] = []
        self.cancelled: list[str] = []
        self.rendered: dict[tuple[str, str], int] = {}     # (rid, item) -> ms
        self.ended: set[str] = set()                        # rids whose last frame rendered

    def play(self, response_id: str, item_id: str, pcm: bytes) -> None:
        if response_id in self.cancelled:
            # The real sink drops these; recording them lets a test assert the drop.
            self.plays_after_cancel.append((response_id, item_id, pcm))
            return
        self.plays.append((response_id, item_id, pcm))

    def cancel(self, response_id: str) -> int:
        self.cancelled.append(response_id)
        return sum(ms for (rid, _item), ms in self.rendered.items() if rid == response_id)

    def rendered_ms(self, response_id: str, item_id: str) -> int | None:
        return self.rendered.get((response_id, item_id))

    def reached_end(self, response_id: str) -> bool:
        return response_id in self.ended


class RecordingSession:
    """The slice of `LiveSession` a strategy calls. Records; never touches a wire."""

    def __init__(self) -> None:
        self.generation = 0
        self.call_outputs: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self.truncates: list[tuple[str, int]] = []
        self.created: list[str] = []

    async def call_output(self, call_id: str, output: dict[str, Any]) -> None:
        self.call_outputs.append((call_id, output))

    async def cancel_response(self, response_id: str) -> None:
        self.cancelled.append(response_id)

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.truncates.append((item_id, audio_end_ms))

    async def create_response(self, origin: str, **kw: Any) -> None:
        self.created.append(origin)


class FakeSocket:
    """A websocket with a scripted inbound queue. `recv()` drains it, then reports close."""

    def __init__(self, inbound: list[Any]) -> None:
        self.inbound = [json.dumps(m) if isinstance(m, dict) else m for m in inbound]
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if not self.inbound:
            raise ConnectionError("fake socket drained")
        return self.inbound.pop(0)

    async def close(self) -> None:
        self.closed = True

    @staticmethod
    def connector(inbound: list[Any], socket: "FakeSocket | None" = None) -> Callable[..., Any]:
        made = socket if socket is not None else FakeSocket(inbound)

        async def connect(uri: str, headers: dict[str, str]) -> "FakeSocket":
            made.uri = uri              # type: ignore[attr-defined]
            made.headers = headers      # type: ignore[attr-defined]
            return made

        return connect
