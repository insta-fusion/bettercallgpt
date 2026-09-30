"""The gpt-realtime wire, behind `LiveSession`.

Serves BOTH providers that speak the realtime protocol:

  * **Azure Voice Live** — host derived from `AZURE_OPENAI_ENDPOINT` (the
    `cognitiveservices` alias of the same AIServices resource), `api-key` header,
    `/voice-live/realtime?api-version=...&model=...`, and the PRE-GA event names,
    which `_RENAME` lifts to the GA ones before anything else looks at an event.
  * **OpenAI Realtime** — `wss://api.openai.com/v1/realtime?model=...`, `Bearer` auth,
    GA names already.

Everything above this file sees only `LiveEvent`s from `live/base.py`. Two rules the
rest of the design rests on are enforced HERE and nowhere else:

**Origin comes from the serialized creation queue.** `create_response(origin)` appends
to one FIFO; the next `response.created` pops it. A response the PROVIDER started (a
committed input item under server VAD, with no pending create of ours) is `user` — that
is exactly the join the strategy needs, and it is the wire's own attribution rather than
a guess. See DESIGN.md §Conversation model.

**Identity is never invented.** Every event carries the ids the wire gave it. A missing
id stays missing; a malformed `arguments` blob becomes `None`, never `{}` — an empty
object is a VALID call for a parameterless tool, so coercing "nothing arrived" into it
would authorize a dispatch the model never made (ported from `voice_listen_audio`'s
`_on_output_item`, the "flag is load-bearing" finding).

No time literals live in this module. The three connection bounds are constructor
parameters the daemon injects; there is no default in the module body.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from typing import Any, AsyncIterator, Callable

from .base import (
    KIND_CALL,
    KIND_CLOSED,
    KIND_DISCONNECTED,
    KIND_ERROR,
    KIND_INPUT_COMMITTED,
    KIND_INPUT_SPEECH_STARTED,
    KIND_INPUT_SPEECH_STOPPED,
    KIND_INPUT_TRANSCRIPT,
    KIND_OUTPUT_AUDIO,
    KIND_OUTPUT_ITEM,
    KIND_OUTPUT_TRANSCRIPT,
    KIND_RESPONSE_CREATED,
    KIND_RESPONSE_DONE,
    KIND_SESSION_STARTED,
    AudioSink,
    LiveEvent,
    Origin,
    SessionConfig,
)

# Voice Live still emits the pre-GA names; the GA ones are what this adapter maps from.
# Ported verbatim from voice_live_transport._RENAME — the four literals that differ.
RENAME = {
    "response.audio.delta": "response.output_audio.delta",
    "response.audio.done": "response.output_audio.done",
    "response.audio_transcript.delta": "response.output_audio_transcript.delta",
    "response.audio_transcript.done": "response.output_audio_transcript.done",
}

SAMPLE_RATE = 24_000
BYTES_PER_FRAME = 2          # pcm16 mono


def voice_live_host(endpoint: str, override: str = "") -> str:
    """Voice Live answers on the cognitiveservices alias of the AIServices resource."""
    if override.strip():
        return override.strip()
    host = re.sub(r"^https?://", "", endpoint or "").split("/")[0]
    return host.replace(".openai.azure.com", ".cognitiveservices.azure.com")


def voice_live_uri(endpoint: str, model: str, api_version: str, host_override: str = "") -> str:
    host = voice_live_host(endpoint, host_override)
    return f"wss://{host}/voice-live/realtime?api-version={api_version}&model={model}"


def openai_uri(model: str) -> str:
    return f"wss://api.openai.com/v1/realtime?model={model}"


async def open_socket(connect: Callable[..., Any], uri: str, headers: dict[str, str], *,
                      bound_s: float) -> Any:
    """Open a provider socket under the daemon's injected bound. Shared by every websocket
    provider (`gpt_live.py` too): DESIGN.md §Timers puts connection bounds in this file only."""
    # b3: lifecycle-ports begin  (connection open — the daemon's injected bound)
    return await asyncio.wait_for(connect(uri, headers), bound_s)
    # b3: lifecycle-ports end


async def close_socket(ws: Any, *, bound_s: float, first: str | None = None) -> None:
    """Close a provider socket under the daemon's injected bound, optionally sending `first`
    (a graceful-close frame) inside the SAME bound. If that stalls, fails or is cancelled, one
    more bounded transport close is the cleanup — shielded, so a cancellation of the caller
    still releases the socket before it propagates. Failures are abandoned, never raised."""
    async def _graceful() -> None:
        if first is not None:
            await ws.send(first)
        await ws.close()

    async def _cleanup() -> None:
        try:
            # b3: lifecycle-ports begin  (cleanup close after a stalled graceful close)
            await asyncio.shield(asyncio.wait_for(ws.close(), bound_s))
            # b3: lifecycle-ports end
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    try:
        # b3: lifecycle-ports begin  (connection close — injected bound)
        await asyncio.wait_for(_graceful(), bound_s)
        # b3: lifecycle-ports end
        return
    except asyncio.CancelledError:
        await _cleanup()
        raise
    except (asyncio.TimeoutError, Exception):
        pass
    await _cleanup()


def _frames(pcm_len: int) -> int:
    return pcm_len // BYTES_PER_FRAME


def _ms(frames: int) -> int:
    return (frames * 1000) // SAMPLE_RATE


class RealtimeSession:
    """One realtime connection. Writes are serialized; reads become `LiveEvent`s.

    `connect` is injected: `async connect(uri, headers) -> socket` where the socket has
    `send(str)` / `recv() -> str` / `close()`. Tests pass a fake; the daemon passes a
    websockets-backed one. The three bounds are REQUIRED keyword parameters — there is
    deliberately no default literal here (DESIGN.md §Timers).
    """

    def __init__(
        self,
        *,
        provider: str,                       # "voice_live" | "openai"
        model: str,
        api_key: str,
        endpoint: str = "",
        api_version: str = "",
        host_override: str = "",
        sink: AudioSink | None = None,
        connect: Callable[..., Any],
        open_bound_s: float,
        close_bound_s: float,
        reconnect_bound_s: float,
    ) -> None:
        if provider not in ("voice_live", "openai"):
            raise ValueError(f"unknown provider {provider!r}")
        if provider == "voice_live" and not api_version:
            raise ValueError("voice_live needs an api_version (no default lives here)")
        self._provider = provider
        self._model = model
        self._key = api_key
        self._endpoint = endpoint
        self._api_version = api_version
        self._host_override = host_override
        self._sink = sink
        self._connect = connect
        self.open_bound_s = open_bound_s
        self.close_bound_s = close_bound_s
        self.reconnect_bound_s = reconnect_bound_s

        self.generation = 0
        self._ws: Any = None
        self._config: SessionConfig | None = None
        self._seq = 0
        self._write_lock = asyncio.Lock()
        self._closed = False
        # nonce -> origin, for creates whose `response.created` has not arrived. Keyed by
        # the nonce the provider echoes in `response.metadata`, never by arrival order.
        self._pending_origins: dict[str, Origin] = {}
        self._create_gate = asyncio.Lock()
        # response id -> item id -> frames handed to the sink (evidence, not a promise)
        self._audio_frames: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ wire plumbing

    def uri(self) -> str:
        if self._provider == "voice_live":
            return voice_live_uri(self._endpoint, self._model, self._api_version,
                                  self._host_override)
        return openai_uri(self._model)

    def headers(self) -> dict[str, str]:
        if self._provider == "voice_live":
            return {"api-key": self._key}
        return {"Authorization": f"Bearer {self._key}", "OpenAI-Beta": "realtime=v1"}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _event(self, kind: str, **kw: Any) -> LiveEvent:
        return LiveEvent(seq=self._next_seq(), kind=kind, generation=self.generation, **kw)

    async def _send(self, obj: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("not connected")
        async with self._write_lock:
            await self._ws.send(json.dumps(obj))

    # ------------------------------------------------------------------ LiveSession

    async def start(self, config: SessionConfig) -> None:
        self._config = config
        # b3: lifecycle-ports begin  (connection open — the daemon's injected bound)
        self._ws = await asyncio.wait_for(
            self._connect(self.uri(), self.headers()), self.open_bound_s)
        # b3: lifecycle-ports end
        await self._send({"type": "session.update", "session": self._session_body(config)})

    def _session_body(self, config: SessionConfig) -> dict[str, Any]:
        """`SessionConfig` -> the provider's session shape.

        `extra` is passed through untouched and LAST: VAD settings, echo cancellation,
        reasoning effort and anything else provider-specific belong to the daemon, which
        knows which provider it opened. This adapter does not curate them.
        """
        body: dict[str, Any] = {
            "instructions": config.instructions,
            "modalities": ["text", "audio"],
            "output_audio_format": "pcm16",
            "input_audio_format": "pcm16",
        }
        if config.voice:
            # Voice Live takes the object form; OpenAI Realtime takes the bare name.
            body["voice"] = ({"name": config.voice, "type": "openai"}
                             if self._provider == "voice_live" else config.voice)
        if config.tools:
            body["tools"] = config.tools
            body["tool_choice"] = config.tool_choice
        body.update(config.extra)
        return body

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None:
            # The daemon starts the microphone before the socket, so frames arrive with
            # nowhere to go. They are dropped here: each frame is a fire-and-forget task,
            # and a raise there is only an unretrieved traceback (78 of them on the first
            # live start). A control message in the same state is a bug and still raises.
            return
        await self._send({"type": "input_audio_buffer.append",
                          "audio": base64.b64encode(pcm).decode("ascii")})

    async def create_response(self, origin: Origin, *, instructions: str | None = None,
                              tool_choice: str | None = None) -> None:
        """Ask for a response, tagged with a NONCE the provider echoes back.

        Correlation is by provider identity, never by arrival order. An earlier version
        popped the oldest pending origin off a FIFO, which silently mis-attributed the
        moment the provider interleaved one of its own: server VAD can open a user turn
        between our create and its `response.created`, and that user response would then
        wear our `challenge` origin while the challenge wore `user`. A call on the first is
        refused when it should dispatch; a call on the second dispatches when it must be
        refused. `response.metadata` is echoed on `created` and `done`, so the tag travels
        with the response it names.
        """
        async with self._create_gate:
            nonce = uuid.uuid4().hex
            body: dict[str, Any] = {"metadata": {"origin": origin, "nonce": nonce}}
            if instructions is not None:
                body["instructions"] = instructions
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
            self._pending_origins[nonce] = origin
            try:
                await self._send({"type": "response.create", "response": body})
            except BaseException:
                # The create never left, so nothing will ever echo this nonce.
                self._pending_origins.pop(nonce, None)
                raise

    async def add_item(self, item: dict[str, Any], origin: Origin) -> None:
        # `origin` belongs to the response a caller creates AFTER this item; an item by
        # itself produces no response and therefore consumes no origin slot.
        await self._send({"type": "conversation.item.create", "item": item})

    async def call_output(self, call_id: str, output: dict[str, Any]) -> None:
        await self._send({"type": "conversation.item.create",
                          "item": {"type": "function_call_output", "call_id": call_id,
                                   "output": json.dumps(output, ensure_ascii=False)}})

    async def cancel_response(self, response_id: str) -> None:
        # Voice Live's cancel is response-less (it cancels the in-progress response);
        # OpenAI Realtime accepts the id. Sending the id to Voice Live is harmless —
        # the production transport sends none, so match it per provider.
        msg: dict[str, Any] = {"type": "response.cancel"}
        if self._provider == "openai" and response_id:
            msg["response_id"] = response_id
        await self._send(msg)

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        # AS GIVEN. Clamping against what actually rendered is the strategy's job
        # (it owns the sink); this adapter must not silently move the operator's cut.
        await self._send({"type": "conversation.item.truncate", "item_id": item_id,
                          "content_index": 0, "audio_end_ms": int(audio_end_ms)})

    async def close(self) -> None:
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                # b3: lifecycle-ports begin  (connection close — injected bound)
                await asyncio.wait_for(ws.close(), self.close_bound_s)
                # b3: lifecycle-ports end
            except (asyncio.TimeoutError, Exception):
                pass

    # ------------------------------------------------------------------ event stream

    async def events(self) -> AsyncIterator[LiveEvent]:
        while True:
            if self._ws is None:
                yield self._event(KIND_CLOSED)
                return
            try:
                raw = await self._ws.recv()
            except Exception as exc:
                if self._closed:
                    yield self._event(KIND_CLOSED)
                    return
                dead = self.generation
                yield LiveEvent(seq=self._next_seq(), kind=KIND_DISCONNECTED,
                                generation=dead, payload={"error": str(exc)})
                return
            if raw is None:
                if self._closed:
                    yield self._event(KIND_CLOSED)
                    return
                yield LiveEvent(seq=self._next_seq(), kind=KIND_DISCONNECTED,
                                generation=self.generation, payload={})
                return
            for ev in self.map_event(json.loads(raw) if isinstance(raw, str) else raw):
                yield ev

    def note_reconnect(self) -> int:
        """The daemon reconnected the socket: bump the generation, forget the queue.

        Pending origins die with the socket — the responses they were promised to can
        never arrive — and a stale origin would mis-attribute the FIRST response of the
        new connection, which is the one most likely to be a user turn.
        """
        self.generation += 1
        self._pending_origins.clear()
        self._audio_frames.clear()
        return self.generation

    # ------------------------------------------------------------------ the mapper

    def map_event(self, raw: dict[str, Any]) -> list[LiveEvent]:
        """One wire event -> zero or more `LiveEvent`s. Pure: tests call it directly.

        Audio bytes go to the sink here and never ride in an event (base.py's rule);
        the event carries the frame count so the loop can see progress without bytes.
        """
        kind = RENAME.get(raw.get("type", ""), raw.get("type", ""))
        out: list[LiveEvent] = []

        if kind == "session.created" or kind == "session.updated":
            if kind == "session.created":
                out.append(self._event(KIND_SESSION_STARTED,
                                       payload={"session_id": (raw.get("session") or {}).get("id")}))
            return out

        if kind == "input_audio_buffer.speech_started":
            return [self._event(KIND_INPUT_SPEECH_STARTED,
                                input_item_id=raw.get("item_id"),
                                start_ms=raw.get("audio_start_ms"))]

        if kind == "input_audio_buffer.speech_stopped":
            return [self._event(KIND_INPUT_SPEECH_STOPPED,
                                input_item_id=raw.get("item_id"),
                                end_ms=raw.get("audio_end_ms"))]

        if kind == "input_audio_buffer.committed":
            return [self._event(KIND_INPUT_COMMITTED, input_item_id=raw.get("item_id"))]

        if kind == "conversation.item.input_audio_transcription.completed":
            text = raw.get("transcript") or ""
            # A `completed` carrying blank or whitespace-only text is NOT success. The
            # provider committed an input item and produced nothing from it, which is the
            # same fact `failed` reports; marking it complete let a strategy build a
            # dispatch out of the model's reconstruction with no transcript behind it.
            return [self._event(KIND_INPUT_TRANSCRIPT, input_item_id=raw.get("item_id"),
                                payload={"text": text, "complete": bool(text.strip()),
                                         **({} if text.strip() else {"blank": True})})]

        if kind == "conversation.item.input_audio_transcription.failed":
            err = raw.get("error") or {}
            return [self._event(KIND_INPUT_TRANSCRIPT, input_item_id=raw.get("item_id"),
                                payload={"text": "", "complete": False,
                                         "error": err.get("message") if isinstance(err, dict) else str(err)})]

        if kind == "response.created":
            resp = raw.get("response") or {}
            rid = resp.get("id") or raw.get("response_id")
            origin, unknown = self._origin_of(resp)
            payload: dict[str, Any] = {"origin": origin}
            if unknown:
                # A metadata origin naming no create of ours. Not ours to trust and not a
                # user turn either, so it is surfaced rather than silently downgraded.
                payload["unknown_origin"] = True
            return [self._event(KIND_RESPONSE_CREATED, response_id=rid, payload=payload)]

        if kind == "response.output_item.added":
            item = raw.get("item") or {}
            return [self._event(KIND_OUTPUT_ITEM, response_id=raw.get("response_id"),
                                item_id=item.get("id"),
                                payload={"type": item.get("type"), "name": item.get("name"),
                                         "call_id": item.get("call_id")})]

        if kind == "response.output_audio.delta":
            return self._audio_delta(raw)

        if kind in ("response.output_audio_transcript.delta",
                    "response.output_audio_transcript.done"):
            done = kind.endswith(".done")
            text = raw.get("transcript") if done else raw.get("delta")
            return [self._event(KIND_OUTPUT_TRANSCRIPT, response_id=raw.get("response_id"),
                                item_id=raw.get("item_id"),
                                payload={"text": text or "", "done": done})]

        if kind == "response.output_item.done":
            item = raw.get("item") or {}
            if item.get("type") != "function_call":
                return [self._event(KIND_OUTPUT_ITEM, response_id=raw.get("response_id"),
                                    item_id=item.get("id"),
                                    payload={"type": item.get("type"), "done": True})]
            return [self._event(KIND_CALL, response_id=raw.get("response_id"),
                                item_id=item.get("id"),
                                payload={"call_id": item.get("call_id") or "",
                                         "name": item.get("name") or "",
                                         "arguments": parse_arguments(item.get("arguments"))})]

        if kind == "response.done":
            resp = raw.get("response") or {}
            rid = resp.get("id") or raw.get("response_id")
            # Tell the sink no further audio is coming for this id — half of the broker's
            # third delivery evidence (DESIGN.md §Acceptance C2). The sink pairs it with its own queue
            # to decide whether the operator heard the response all the way to its end.
            # Optional by design: a sink that does not track delivery (the replay fakes)
            # simply does not offer the hook.
            if rid and self._sink is not None:
                note_done = getattr(self._sink, "note_response_done", None)
                if note_done is not None:
                    note_done(rid)
            return [self._event(KIND_RESPONSE_DONE, response_id=rid,
                                payload={"status": resp.get("status") or ""})]

        if kind == "error":
            err = raw.get("error") or {}
            return [self._event(KIND_ERROR,
                                payload={"message": err.get("message") if isinstance(err, dict)
                                         else str(err)})]

        return out

    def _origin_of(self, resp: dict[str, Any]) -> tuple[Origin, bool]:
        """(origin, unknown). A response with no metadata of ours is the PROVIDER's own —
        server VAD opening a user turn — and that is the only thing `user` may mean here."""
        metadata = resp.get("metadata")
        if not isinstance(metadata, dict):
            return "user", False
        nonce = metadata.get("nonce")
        if not nonce:
            return "user", False
        claimed = self._pending_origins.pop(nonce, None)
        if claimed is None:
            # Either a replay of a response we already matched, or metadata we never sent.
            # Neither is a user turn, and neither may carry an origin we would act on.
            return "narration", True
        return claimed, False

    def _audio_delta(self, raw: dict[str, Any]) -> list[LiveEvent]:
        rid = raw.get("response_id") or ""
        iid = raw.get("item_id") or ""
        delta = raw.get("delta")
        # The recorded fixtures carry a BYTE COUNT where the live wire carries base64:
        # audio was stripped at capture. Both are accepted so a replay exercises the same
        # accounting the live path does; only real bytes ever reach the sink.
        if isinstance(delta, int):
            pcm = b""
            nbytes = delta
        elif isinstance(delta, str) and delta:
            pcm = base64.b64decode(delta)
            nbytes = len(pcm)
        else:
            pcm = b""
            nbytes = 0
        if pcm and self._sink is not None:
            self._sink.play(rid, iid, pcm)
        frames = _frames(nbytes)
        per_item = self._audio_frames.setdefault(rid, {})
        per_item[iid] = per_item.get(iid, 0) + frames
        return [self._event(KIND_OUTPUT_AUDIO, response_id=rid, item_id=iid,
                            payload={"frames": frames, "bytes": nbytes})]


def parse_arguments(raw: Any) -> dict[str, Any] | None:
    """A function call's arguments, or None when the wire carried nothing usable.

    NEVER `{}` on failure. `{}` is a legitimate call for a parameterless tool, so
    coercing an absent or unparsable blob into it makes "nothing arrived" indistinguishable
    from "the model sent no arguments" — and a wire that carried nothing must never
    authorize anything (`voice_listen_audio._on_output_item`, malformed flag).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        args = json.loads(raw)
    except Exception:
        return None
    return args if isinstance(args, dict) else None
