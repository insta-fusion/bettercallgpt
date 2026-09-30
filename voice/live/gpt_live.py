"""The GPT-Live wire, behind `LiveSession` — the parallel of `realtime.py`.

GPT-Live (`gpt-live-1`) is a different protocol, not a different model on the realtime wire:

  * `wss://<resource>.openai.azure.com/openai/v1/live/sessions`, `api-key` header;
    the session opens with `session.start` (model, instructions, voice, delegation).
  * Caller audio goes up as `session.input_audio.append`; the model's audio comes back as
    `session.output_audio.delta` whose base64 PCM is in **`delta`** (not `audio`).
  * There are no responses, items, calls, cancel or truncate. The model decides by itself when
    to speak; when it wants work done it emits `session.delegation.created` carrying an id and
    an `offset_ms` cursor — and NO text. Words go back as `session.thinking.append` (quiet
    context) or `session.commentary.append` (for the model to speak), each bound to a
    `delegation_id` (null for general context) and at most 500 tokens.

Reference: https://learn.microsoft.com/en-us/azure/foundry/openai/gpt-live-reference
Evidence: recorded GPT-Live sessions (2026-09-23), replayed in voice/tests/test_wire_gpt_live.py.

**Identity is never invented.** The one decision identity this wire has is the delegation id,
and it is used as-is: the delegation is the model's decision, so its id is the decision's id
(`response_id` = `call_id` = delegation id). The input span it consumed is named after it
(`<delegation id>:span`) — derived, never a free-standing fabrication. Output audio has no
response id at all; the sink is keyed by a LOCAL speech-segment key (`gpt-live:<gen>:<n>`),
one per connected run of the model's voice, and no event reports that key as a response id.

**The span rule** (DESIGN.md §Conversation model, GPT-Live span rule): `offset_ms` is a cursor, not a completeness
claim. An input transcript fragment belongs to the first delegation whose `offset_ms` is ≥ the
fragment's `start_ms`; a fragment that arrives after its cursor already fired is counted as
late and never re-attributed to a later delegation.

**Interruption** is local only (DESIGN.md §Audio epoch, GPT-Live): the model stops by itself when
the operator talks (S6); what we own is the lead already queued at the speaker. The model's
voice is tracked as SEGMENTS on the server timeline — connected runs of timed output ranges,
from transcripts and audio alike, in any arrival order — and each segment has its own sink key.
An operator word that began inside a live segment is talking over it →
`KIND_INPUT_SPEECH_STARTED`; the strategy flushes, which cancels exactly the segments that began
before the operator's words ended. A later, separate segment survives with its queued audio;
anything that connects to cancelled speech joins it and is dropped. Words that began BEFORE
the model spoke (the tail of the turn it is answering) never cut it.

No time literals here: the connection bounds are injected and applied by the shared helpers in
`realtime.py`, the file DESIGN.md §Timers gives them to.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from .base import (
    KIND_CLOSED,
    KIND_DELEGATION_CREATED,
    KIND_DISCONNECTED,
    KIND_ERROR,
    KIND_INPUT_COMMITTED,
    KIND_INPUT_SPEECH_STARTED,
    KIND_INPUT_TRANSCRIPT,
    KIND_OUTPUT_AUDIO,
    KIND_OUTPUT_TRANSCRIPT,
    KIND_RESPONSE_CREATED,
    KIND_SESSION_STARTED,
    AudioSink,
    Capabilities,
    LiveEvent,
    Origin,
    SessionConfig,
)
from .realtime import BYTES_PER_FRAME, close_socket, open_socket

# The reference caps every append's `content` at 500 tokens. There is no tokenizer here, so the
# bound is one that PROVABLY holds: every token of a byte-level BPE covers at least one UTF-8
# byte, so a text of N bytes is at most N tokens. A character heuristic undercounts short words
# and emoji, and an oversized append is rejected — leaving that delegation unanswered.
APPEND_BYTE_BUDGET = 480
TRUNCATED_MARK = "…(截断)"
# A relay's recap rides as this many `thinking` appends at most, each inside the budget above.
RECAP_APPENDS = 4

# What this wire can prove (see `base.Capabilities`). No response identity → the broker is
# terminal-only and there is no runaway cut; no server echo cancellation → use headphones.
# No speech end either: the wire reports the operator's words, never that they stopped — the
# model answering or delegating proves nothing about it (div review, 2026-09-24).
CAPABILITIES = Capabilities(response_identity=False, server_echo_cancellation=False,
                            input_mute=True, function_tools=False, speech_end=False,
                            recap_bytes=RECAP_APPENDS * APPEND_BYTE_BUDGET)

APPEND_KINDS = ("thinking", "commentary", "instructions")

# The one item id the sink sees for this wire's audio: there are no output items.
AUDIO_ITEM = "gpt-live-audio"

def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def fit(text: str, budget: int = APPEND_BYTE_BUDGET) -> str:
    """`text` cut to `budget` UTF-8 bytes (so to at most `budget` tokens), marked when cut.
    Never splits a code point."""
    if utf8_len(text) <= budget:
        return text
    room = budget - utf8_len(TRUNCATED_MARK)
    cut = text.encode("utf-8")[:room].decode("utf-8", errors="ignore")
    return cut + TRUNCATED_MARK


def chunks(text: str, budget: int = APPEND_BYTE_BUDGET) -> list[str]:
    """`text` in pieces of at most `budget` UTF-8 bytes, cut on code points, never marked:
    together they are the whole text."""
    out: list[str] = []
    data = text.encode("utf-8")
    while data:
        piece = data[:budget].decode("utf-8", errors="ignore")
        if not piece:                    # a budget smaller than one code point
            break
        out.append(piece)
        data = data[len(piece.encode("utf-8")):]
    return out


def live_host(endpoint: str) -> str:
    """The resource host from an endpoint URL (any path or scheme is dropped)."""
    return re.sub(r"^(https?|wss?)://", "", (endpoint or "").strip()).split("/")[0]


def live_uri(endpoint: str) -> str:
    return f"wss://{live_host(endpoint)}/openai/v1/live/sessions"


@dataclass
class _Segment:
    """One connected run of the model's voice on the server timeline, with its sink key(s)
    (several when two runs turned out to be one)."""
    start: int
    end: int
    keys: list[str]
    dead: bool = False
    barged: bool = False
    untimed: bool = False


@dataclass(frozen=True)
class _Fragment:
    start_ms: int | None
    text: str


class GptLiveSession:
    """One GPT-Live connection. Writes are the socket's; reads become `LiveEvent`s.

    `connect` is injected exactly as for `RealtimeSession`: `async connect(uri, headers)`.
    The bounds are REQUIRED keyword parameters — no default literal lives here.
    """

    capabilities = CAPABILITIES

    def __init__(self, *, model: str, api_key: str, endpoint: str,
                 sink: AudioSink | None = None, connect: Callable[..., Any],
                 open_bound_s: float, close_bound_s: float, reconnect_bound_s: float) -> None:
        if not live_host(endpoint):
            raise ValueError("gpt_live needs an endpoint (the resource URL)")
        self._model = model
        self._key = api_key
        self._endpoint = endpoint
        self._sink = sink
        self._connect = connect
        self.open_bound_s = open_bound_s
        self.close_bound_s = close_bound_s
        self.reconnect_bound_s = reconnect_bound_s

        self.generation = 0
        self._ws: Any = None
        self._seq = 0
        self._closed = False
        self._server_closed = False
        # Input fragments not yet claimed by a delegation, in arrival order.
        self._pending: list[_Fragment] = []
        # The highest `offset_ms` a delegation has consumed; fragments at or before it that
        # arrive later are LATE, counted and never re-attributed.
        self._cursor: int | None = None
        self.late_fragments = 0
        # The model's voice as SEGMENTS on the server timeline, fed by output transcripts and
        # output audio in any arrival order. Each segment is a connected run with its own sink
        # key, so a cut cancels exactly the speech it touches and nothing else:
        #   live — may play; dead — cancelled: a range touching it joins it and is dropped
        #   (the interrupted answer arriving late), and a live segment it comes to touch dies
        #   with it, queued audio included.
        # THE PLAY FLOOR is monotonic: a cut raises it to the end of the operator's words, each
        # further word while cut raises it again, and admitting the next answer raises it to
        # that answer's start. A new segment starting below it is stale.
        # Residual (DESIGN.md §Audio epoch, GPT-Live): audio the model keeps generating past the
        # operator's words before it stops, connected to no known cancelled speech, plays —
        # that is the model's own stop latency, not something the wire lets a client prove.
        # Known limit: cancelling segments that already rendered aborts the DEVICE ONCE per
        # flush (`PlaybackSink.cancel_many`), so a later surviving segment's frames already
        # inside the device buffer at that moment are cut with it. Its queued audio survives.
        self._segments: list[_Segment] = []
        self._next_key = 0
        self._cut = False
        self._floor: int | None = None
        # Operator word starts, kept until a cut: a word in a gap of the known voice can be
        # covered later, so pending evidence is never pruned by output merely advancing.
        self._input_starts: list[int] = []
        self._input_end: int | None = None
        self.dropped_late_audio = 0
        self.usage: dict[str, Any] = {}
        # Every delegation id THIS session issued. After a relay the loop may answer one the
        # previous session issued; the new session never heard of it, so the port answers
        # it unbound instead (`GptLiveVoice._own`).
        self.delegations: set[str] = set()

    # ------------------------------------------------------------------ wire plumbing

    def uri(self) -> str:
        return live_uri(self._endpoint)

    def headers(self) -> dict[str, str]:
        return {"api-key": self._key}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _event(self, kind: str, **kw: Any) -> LiveEvent:
        return LiveEvent(seq=self._next_seq(), kind=kind, generation=self.generation, **kw)

    async def _send(self, obj: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("not connected")
        await self._ws.send(json.dumps(obj, ensure_ascii=False))

    def _new_key(self) -> str:
        """A sink key for a new speech segment — local, never a response id."""
        key = f"gpt-live:{self.generation}:{self._next_key}"
        self._next_key += 1
        return key

    def unclaimed_words(self) -> str:
        """The operator's words no delegation has claimed yet (a relay carries them over)."""
        return "".join(frag.text for frag in self._pending).strip()

    def live_keys(self) -> list[str]:
        return [key for seg in self._segments if not seg.dead for key in seg.keys]

    # ------------------------------------------------------------------ LiveSession

    async def start(self, config: SessionConfig) -> None:
        self._ws = await open_socket(self._connect, self.uri(), self.headers(),
                                     bound_s=self.open_bound_s)
        await self._send({"type": "session.start", "session": self._session_body(config)})

    def _session_body(self, config: SessionConfig) -> dict[str, Any]:
        """The session object is STRICT (unknown fields are rejected), so only documented
        fields go out. Tools do not exist on this wire; `delegation: client` is the default and
        is stated anyway, because the whole strategy depends on it."""
        body: dict[str, Any] = {"model": self._model, "instructions": config.instructions,
                                "delegation": {"type": "client"}}
        if config.voice:
            body["audio"] = {"output": {"voice": config.voice}}
        body.update(config.extra)
        return body

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None or not pcm or self._server_closed:
            # Frames that arrive before the socket (the mic opens first) are dropped, as on
            # the realtime wire; an empty append is a protocol error on this one.
            return
        await self._send({"type": "session.input_audio.append",
                          "audio": base64.b64encode(pcm).decode("ascii")})

    async def append(self, kind: str, delegation_id: str | None, content: str) -> None:
        """`session.<kind>.append`, bound to a delegation (or None: general context)."""
        if kind not in APPEND_KINDS:
            raise ValueError(f"unknown append kind {kind!r}")
        if self._server_closed or self._ws is None:
            # The session is over: nothing sent now can be spoken or heard.
            return
        await self._send({"type": f"session.{kind}.append", "delegation_id": delegation_id,
                          "content": fit(content)})

    def flush_playback(self) -> int:
        """The operator talked over the model: cancel every live segment that began before
        their words ended, with its queued audio. A later, separate segment survives. Returns
        frames of the cancelled speech already rendered (0 when none)."""
        boundary = self._input_end
        doomed = [seg for seg in self._segments
                  if not seg.dead and (boundary is None or seg.start <= boundary)]
        if not doomed:
            return 0
        self._cut = True
        self._raise_floor(boundary)
        self._input_starts = []
        return self._kill(*doomed)

    def _kill(self, *segs: "_Segment") -> int:
        """Mark segments dead and drop their queued audio in ONE sink call: the device is
        aborted at most once (when anything of them rendered — it may hold audible frames the
        queue no longer counts), so surviving audio is never cut by a second abort."""
        keys: list[str] = []
        for seg in segs:
            seg.dead = True
            keys.extend(seg.keys)
        if self._sink is None or not keys:
            return 0
        many = getattr(self._sink, "cancel_many", None)
        if many is not None:
            return many(keys) or 0
        return sum(self._sink.cancel(key) or 0 for key in keys)

    async def close(self) -> None:
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is None:
            return
        # Billing runs until the session ends: ask for the graceful end first — inside the
        # same injected bound as the transport close, with a bounded cleanup if it stalls.
        first = None if self._server_closed else json.dumps({"type": "session.close"})
        await close_socket(ws, bound_s=self.close_bound_s, first=first)

    # ------------------------------------------------------------------ event stream

    async def events(self) -> AsyncIterator[LiveEvent]:
        while True:
            if self._ws is None:
                yield self._event(KIND_CLOSED)
                return
            try:
                raw = await self._ws.recv()
            except Exception as exc:
                if self._closed or self._server_closed:
                    yield self._event(KIND_CLOSED)
                    return
                yield LiveEvent(seq=self._next_seq(), kind=KIND_DISCONNECTED,
                                generation=self.generation, payload={"error": str(exc)})
                return
            if raw is None:
                if self._closed or self._server_closed:
                    yield self._event(KIND_CLOSED)
                    return
                yield LiveEvent(seq=self._next_seq(), kind=KIND_DISCONNECTED,
                                generation=self.generation, payload={})
                return
            for ev in self.map_event(json.loads(raw) if isinstance(raw, str) else raw):
                yield ev
                if ev.kind == KIND_CLOSED:
                    return

    # ------------------------------------------------------------------ the mapper

    def map_event(self, raw: dict[str, Any]) -> list[LiveEvent]:
        """One wire event -> zero or more `LiveEvent`s. Tests call it directly.

        Audio bytes go to the sink here and never ride in an event (base.py's rule).
        """
        kind = raw.get("type", "")

        if kind == "session.started":
            session = raw.get("session") or {}
            return [self._event(KIND_SESSION_STARTED, payload={
                "session_id": session.get("id"),
                "delegation": (session.get("delegation") or {}).get("type")})]

        if kind == "session.input_transcript.delta":
            return self._input_fragment(raw)

        if kind == "session.output_transcript.delta":
            if self._track_output(raw.get("start_ms"), raw.get("end_ms")) is None:
                return []
            return self._barge() + [self._event(KIND_OUTPUT_TRANSCRIPT, start_ms=raw.get("start_ms"),
                                end_ms=raw.get("end_ms"),
                                payload={"text": raw.get("delta") or "", "done": False})]

        if kind == "session.output_audio.delta":
            return self._audio_delta(raw)

        if kind == "session.delegation.created":
            return self._delegation(raw)

        if kind in ("session.usage.updated",):
            self.usage = dict(raw.get("usage") or {})
            return []

        if kind == "session.closed":
            self._server_closed = True
            self.usage = dict(raw.get("usage") or self.usage)
            return [self._event(KIND_CLOSED, payload={"reason": raw.get("reason") or "",
                                                      "usage": self.usage})]

        if kind == "error":
            err = raw.get("error") or {}
            return [self._event(KIND_ERROR, payload={
                "message": err.get("message") if isinstance(err, dict) else str(err),
                "code": err.get("code") if isinstance(err, dict) else None})]

        # Acks (`*.appended`, `session.updated`, mute) and anything newer: nothing to act on.
        return []

    def _input_fragment(self, raw: dict[str, Any]) -> list[LiveEvent]:
        start = raw.get("start_ms")
        start = start if isinstance(start, int) else None
        if start is not None and self._cursor is not None and start <= self._cursor:
            # Its delegation already fired. Re-attributing it to the NEXT one would put the
            # operator's earlier words into a later request; its TEXT is the honest loss —
            # its timing is still evidence of talking over the model (below).
            self.late_fragments += 1
        else:
            self._pending.append(_Fragment(start_ms=start, text=raw.get("delta") or ""))
        end = raw.get("end_ms")
        end = end if isinstance(end, int) else start
        if end is not None:
            self._input_end = end if self._input_end is None else max(self._input_end, end)
            if self._cut:
                # Still talking after the cut: the interrupted answer cannot resume over them.
                self._raise_floor(end)
        if start is not None:
            self._input_starts.append(start)
        # Every word is presence: an activity event (a PARTIAL transcript with no input item,
        # which nothing joins or dispatches) so the daemon's idle close never ends a session
        # while the operator is talking and no delegation has fired yet.
        barge = self._barge()          # allocated first: event seq stays monotonic
        heard = self._event(KIND_INPUT_TRANSCRIPT, start_ms=start,
                            end_ms=end if isinstance(end, int) else None,
                            payload={"text": raw.get("delta") or "", "complete": False,
                                     "partial": True})
        return barge + [heard]

    def _audio_delta(self, raw: dict[str, Any]) -> list[LiveEvent]:
        delta = raw.get("delta")
        # Recorded fixtures carry a BYTE COUNT where the live wire carries base64 (audio was
        # stripped at capture); both are accounted the same, only real bytes reach the sink.
        if isinstance(delta, int):
            pcm, nbytes = b"", delta
        elif isinstance(delta, str) and delta:
            pcm = base64.b64decode(delta)
            nbytes = len(pcm)
        else:
            pcm, nbytes = b"", 0
        key = self._track_output(raw.get("start_ms"), raw.get("end_ms")) if nbytes else None
        if nbytes and key is None:
            self.dropped_late_audio += 1
            return []
        # An overlap this range just revealed is reported BEFORE its audio: the strategy
        # flushes on it, and this chunk dies with the speech it belongs to.
        barge = self._barge()
        if barge:
            return barge + [self._event(KIND_OUTPUT_AUDIO, start_ms=raw.get("start_ms"),
                                        end_ms=raw.get("end_ms"),
                                        payload={"frames": 0, "bytes": 0, "withheld": nbytes})]
        if pcm and key is not None and self._sink is not None:
            self._sink.play(key, AUDIO_ITEM, pcm)
        return [self._event(KIND_OUTPUT_AUDIO, start_ms=raw.get("start_ms"),
                            end_ms=raw.get("end_ms"),
                            payload={"frames": nbytes // BYTES_PER_FRAME, "bytes": nbytes,
                                     "segment": key})]

    def _raise_floor(self, value: int | None) -> None:
        if value is not None:
            self._floor = value if self._floor is None else max(self._floor, value)

    def _track_output(self, start: Any, end: Any) -> str | None:
        """Record one output range; return the sink key its audio plays under, or None when it
        is stale (drop it).

        Output transcripts are timed on every recorded wire; audio deltas carry `start_ms` per
        the reference but did not on the 2026-09-19 wire — an untimed range joins the latest
        live segment (dropped while cut).
        """
        if not isinstance(start, int):
            if self._cut:
                return None
            live = [seg for seg in self._segments if not seg.dead]
            if live:
                return live[-1].keys[0]
            seg = _Segment(start=0, end=0, keys=[self._new_key()], untimed=True)
            self._segments.append(seg)
            return seg.keys[0]
        end = end if isinstance(end, int) and end >= start else start
        touched = [seg for seg in self._segments
                   if not seg.untimed and seg.start <= end and start <= seg.end]
        if any(seg.dead for seg in touched):
            # The cancelled answer continuing. It absorbs everything it now connects to —
            # a live segment included, whose queued audio dies with it. Checked below the
            # floor too: the extension is what recognises the NEXT piece of the same answer.
            # Kill the live ones FIRST: merging mutates them into the dead segment, after which
            # nothing would know their queued audio still had to go (review r6 #1).
            self._kill(*(seg for seg in touched if not seg.dead))
            self._merge(touched, start, end).dead = True
            return None
        if not touched:
            if self._floor is not None and start < self._floor:
                return None
            seg = _Segment(start=start, end=end, keys=[self._new_key()])
            self._segments.append(seg)
        else:
            seg = self._merge(touched, start, end)
        if self._cut:
            # Live speech accepted after a cut — a new answer, or a separate one that survived
            # it: the cut is over. A NEW segment also raises the floor to its start, so nothing
            # older can follow it in.
            self._cut = False
            if not touched:
                self._raise_floor(seg.start)
        return seg.keys[0]

    def _merge(self, touched: list["_Segment"], start: int, end: int) -> "_Segment":
        """Coalesce `touched` and [start, end] into one segment, keeping every sink key."""
        keep = touched[0]
        for seg in touched[1:]:
            keep.keys.extend(k for k in seg.keys if k not in keep.keys)
            keep.start, keep.end = min(keep.start, seg.start), max(keep.end, seg.end)
            keep.dead = keep.dead or seg.dead
            self._segments.remove(seg)
        keep.start, keep.end = min(keep.start, start), max(keep.end, end)
        return keep

    def _barge(self) -> list[LiveEvent]:
        """One interruption per segment: an operator word that began INSIDE live speech.
        Words before the voice (the turn it answers) never match. Per segment, never gated
        on an earlier cut: a segment that survived one can still be talked over (r6 #2)."""
        for seg in self._segments:
            if seg.dead or seg.barged or seg.untimed:
                continue
            # STRICTLY after the voice began: an equal timestamp is the answered turn's last
            # word meeting the answer's first (recorded 2026-09-23 at 33600 ms), not a cut.
            hits = [s for s in self._input_starts if seg.start < s < seg.end]
            if hits:
                seg.barged = True
                return [self._event(KIND_INPUT_SPEECH_STARTED, start_ms=min(hits),
                                    payload={"segment": seg.keys[0]})]
        return []

    def _delegation(self, raw: dict[str, Any]) -> list[LiveEvent]:
        d = raw.get("delegation") or {}
        did = d.get("id") or ""
        offset = raw.get("offset_ms")
        if not did or d.get("target", "client") != "client":
            # A Responses-targeted delegation runs server-side and is not ours to answer.
            return [self._event(KIND_ERROR, payload={
                "message": f"delegation not for this client: {json.dumps(d)[:200]}"})]
        span: list[_Fragment] = []
        rest: list[_Fragment] = []
        for frag in self._pending:
            claimed = (not isinstance(offset, int) or frag.start_ms is None
                       or frag.start_ms <= offset)
            (span if claimed else rest).append(frag)
        self._pending = rest
        self.delegations.add(did)
        if isinstance(offset, int):
            self._cursor = offset if self._cursor is None else max(self._cursor, offset)
        text = "".join(f.text for f in span).strip()
        span_id = f"{did}:span"
        transcript = {"text": text, "complete": bool(text)}
        if not text:
            transcript["blank"] = True
        # The loop's log learns the turn exactly as it does on the realtime wire: an input item
        # committed, its transcript, and the decision that consumed it — the delegation.
        return [
            self._event(KIND_INPUT_COMMITTED, input_item_id=span_id),
            self._event(KIND_INPUT_TRANSCRIPT, input_item_id=span_id, payload=transcript),
            self._event(KIND_RESPONSE_CREATED, response_id=did,
                        payload={"origin": "user", "delegation": True}),
            self._event(KIND_DELEGATION_CREATED, response_id=did, delegation_id=did,
                        input_item_id=span_id, offset_ms=offset if isinstance(offset, int) else None,
                        payload={"text": text, "fragments": len(span)}),
        ]


class GptLiveVoice:
    """`VoicePort` over GPT-Live: quiet words are `thinking`, spoken words are `commentary`,
    and anything that answers a delegation is bound to its id."""

    capabilities = CAPABILITIES

    def __init__(self, session: GptLiveSession) -> None:
        self.session = session

    async def context(self, text: str) -> None:
        await self.session.append("thinking", None, text)

    async def announce(self, text: str, origin: Origin) -> None:
        await self.session.append("commentary", None, text)

    async def challenge(self, text: str) -> None:
        # Unreachable in practice: without response identity the broker is terminal-only and
        # never arms a spoken challenge. Kept so a misconfiguration is spoken, not dropped.
        await self.session.append("commentary", None, text)

    def _own(self, delegation_id: str | None) -> str | None:
        """The id to bind an answer to: its own when THIS session issued it; None (general
        context) for one a session before a relay issued, which this one never heard of."""
        known = getattr(self.session, "delegations", None)
        if not delegation_id or (known is not None and delegation_id not in known):
            return None
        return delegation_id

    async def receipt(self, call_id: str, output: dict[str, Any], *, speak: bool) -> None:
        note = str(output.get("note") or json.dumps(output, ensure_ascii=False))
        # Every delegation is answered: an unanswered one leaves the model saying
        # "我正在确认" forever (live test, 2026-09-23: five delegations, none answered).
        await self.session.append("commentary" if speak else "thinking", self._own(call_id),
                                  note)

    async def result(self, text: str, *, answers: list[str] | None) -> None:
        if answers is None:
            await self.session.append("thinking", None, text)
            return
        # Every delegation the turn absorbed gets its answer on its own id; the result is
        # SPOKEN once, on the last, and the earlier ones are closed quietly with the same text.
        # An id from before a relay is not this session's: the answer goes unbound, and is
        # still spoken.
        ids = [a for a in (self._own(a) for a in answers) if a] or [""]
        for did in ids[:-1]:
            await self.session.append("thinking", did, text)
        await self.session.append("commentary", ids[-1] or None, text)

    async def cut(self, response_id: str) -> None:
        # No response identity: there is nothing on the wire to cut. Never called — the
        # runaway count needs response ids this wire does not carry.
        return None

    async def seed(self, recap: str) -> None:
        # Quiet context in `thinking` appends, each inside the per-append cap; the profile's
        # `recap_bytes` already bounds how many there are.
        for piece in chunks(recap)[:RECAP_APPENDS]:
            await self.session.append("thinking", None, piece)
