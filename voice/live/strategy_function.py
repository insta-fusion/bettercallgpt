"""PRIMARY strategy: the model's function call IS the routing.

This file owns one thing — the JOIN that gives every dispatch an identity:

    committed input item  ->  a `user`-origin response  ->  that response's function_call

A decision is yielded only when all three of these hold (DESIGN.md §Conversation model):

  (a) the call's response has origin `user`. A broker challenge or a narration response
      can sit anywhere in the stream; attributing a call to one of those would dispatch
      the operator's words against a turn they never spoke.
  (b) exactly ONE candidate input item is open for that response. Two committed items
      with no transcript to tell them apart is an ambiguous turn — refused, never guessed.
  (c) that input item's transcript has ARRIVED. The backend gets both texts labelled:
      `transcript` (raw ASR, evidence) and `interpretation` (the model's own `text`).
      A FAILED transcript still yields the Request with `transcript=""` — the loop decides
      what to do with an unevidenced dispatch; the strategy does not silently drop words.

The second thing it owns is the interruption sequence, in this order and no other:

    speech_started -> cancel_response(current) -> sink.cancel(response_id)
                   -> truncate(item, rendered_ms) IFF exactly one audio item rendered

`rendered_ms` comes from the sink — frames actually handed to the device, never bytes
queued. Zero rendered or more than one rendered item means no truncate at all: an
`audio_end_ms` we cannot prove is worse than none (Voice Live rejects a too-long cut,
and a wrong item cuts audio the operator did hear).

**Speech mode:** the session runs `tool_choice: "auto"` with ONE tool. Speaking is the
model's natural output; calling `relay` is its one decision. After each `function_call_output`
the loop sends ONE `response.create` carrying a per-response `{"tool_choice": "none"}`, because
a receipt answered under a tool-enabled response fired `request` three times for one sentence
(R11 variant A). A response may carry BOTH a `message` and a `function_call`: the audio of such
a response is the model speaking ON the user turn, and the loop then skips its own follow-up.

No keyword tables, no time literals, no sleeps. Tool names come from `TOOLS`, the closed
schema the daemon and the core tests share.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import (
    KIND_CALL,
    KIND_DISCONNECTED,
    KIND_INPUT_COMMITTED,
    KIND_INPUT_SPEECH_STARTED,
    KIND_INPUT_TRANSCRIPT,
    KIND_OUTPUT_AUDIO,
    KIND_OUTPUT_ITEM,
    KIND_RESPONSE_CREATED,
    KIND_RESPONSE_DONE,
    AudioSink,
    Decision,
    LiveEvent,
    Request,
    TranscriptFailed,
)

# ---------------------------------------------------------------- the closed schema
# ONE tool. Whether a sentence is work, chit-chat, noise, or a question the model can answer
# itself is the model's own judgment; the harness only carries the decision. NO consent tool
# exists: a model must never hold consent (G4 — the best-behaved provider approved
# `rm -rf build/` on an unrelated 「对」 in four runs of five).
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "relay",
        "description": "把操作者这句话交给 backend(会写代码、跑命令、改文件、停任务、退出语音的那个 agent)。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "他要 backend 做的事，用他自己的话说；不扩写，不补他没提的要求。"},
                "interrupt": {"type": "boolean",
                              "description": "true = 打断 backend 正在做的事（纠正、停止、改要求）；false = 排在后面。"},
            },
            "required": ["text", "interrupt"],
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)

# Why a call was refused. These are log/telemetry reasons, never routing keys.
REFUSE_UNKNOWN_TOOL = "unknown_tool"
REFUSE_MALFORMED = "malformed_arguments"
REFUSE_NOT_USER_ORIGIN = "not_user_origin"
REFUSE_AMBIGUOUS_INPUT = "ambiguous_input_item"
REFUSE_NO_INPUT = "no_input_item"
REFUSE_TRANSCRIPT = "transcription_failed"


def _usable(transcript: dict[str, Any]) -> bool:
    """Whether this transcript is EVIDENCE the operator said something.

    A `completed` event whose text is blank or whitespace is not success: the provider
    committed an input item and produced nothing from it, which is the same fact a
    `failed` event reports and must be treated the same way. Reading it as success built a
    Request out of the model's `interpretation` alone — the model's reconstruction of words
    no transcript backs, handed to a backend as though the operator had been heard.
    """
    return bool(transcript.get("complete")) and bool((transcript.get("text") or "").strip())


@dataclass
class _Response:
    origin: str
    candidates: list[str] = field(default_factory=list)   # committed input items open here
    audio_items: list[str] = field(default_factory=list)  # output audio items, in order
    done: bool = False


@dataclass
class _PendingCall:
    response_id: str
    item_id: str
    call_id: str
    name: str
    arguments: dict[str, Any]
    input_item_id: str
    generation: int


class FunctionStrategy:
    """`Strategy.feed` over the realtime function-calling wire.

    `session` is a `LiveSession` (only `call_output`, `cancel_response` and `truncate` are
    used) and `sink` is an `AudioSink`. Both are awaited/called directly — the strategy is
    the only component that knows the interruption sequence, so it performs it rather than
    describing it to someone else.
    """

    def __init__(self, session: Any, sink: AudioSink) -> None:
        self._session = session
        self._sink = sink
        self._responses: dict[str, _Response] = {}
        # Input items committed but not yet claimed by a response.
        self._unclaimed: list[str] = []
        # input item id -> {"text": str, "complete": bool}; present = the transcript landed.
        self._transcripts: dict[str, dict[str, Any]] = {}
        # Calls waiting on their input item's transcript, keyed by that input item id.
        self._waiting: dict[str, list[_PendingCall]] = {}
        self._current_response: str | None = None
        self._audible: str | None = None
        self.refusals: list[dict[str, Any]] = []      # observable, for tests and the log

    # ------------------------------------------------------------------ Strategy.feed

    async def feed(self, event: LiveEvent) -> list[Decision]:
        kind = event.kind

        if kind == KIND_INPUT_COMMITTED:
            if event.input_item_id:
                self._unclaimed.append(event.input_item_id)
            return []

        if kind == KIND_INPUT_TRANSCRIPT:
            return await self._on_transcript(event)

        if kind == KIND_RESPONSE_CREATED:
            return self._on_response_created(event)

        if kind == KIND_OUTPUT_ITEM:
            # `added` announces an item but proves nothing about audio: an item becomes a
            # truncate candidate only once a delta for it arrives (KIND_OUTPUT_AUDIO), and
            # a cut candidate only once the sink says frames rendered.
            return []

        if kind == KIND_OUTPUT_AUDIO:
            # An audio item is only a truncate candidate once it has carried audio.
            rec = self._responses.get(event.response_id or "")
            if rec is not None and event.item_id and event.item_id not in rec.audio_items:
                rec.audio_items.append(event.item_id)
            if rec is not None and event.response_id:
                # What the speakers are (or will be) playing. Outlives `_current_response`:
                # the wire finishes long before the operator has heard the last frame.
                self._audible = event.response_id
            return []

        if kind == KIND_CALL:
            return await self._on_call(event)

        if kind == KIND_RESPONSE_DONE:
            rec = self._responses.get(event.response_id or "")
            if rec is not None:
                rec.done = True
            if self._current_response == event.response_id:
                self._current_response = None
            return []

        if kind == KIND_INPUT_SPEECH_STARTED:
            await self._on_speech_started()
            return []

        if kind == KIND_DISCONNECTED:
            # Nothing joined across a dead socket: the ids on the other side are gone.
            self._responses.clear()
            self._unclaimed.clear()
            self._waiting.clear()
            self._current_response = None
            self._audible = None
            return []

        return []

    # ------------------------------------------------------------------ the join

    def _on_response_created(self, event: LiveEvent) -> list[Decision]:
        rid = event.response_id or ""
        origin = (event.payload or {}).get("origin", "user")
        rec = _Response(origin=origin)
        if origin == "user":
            # A user-origin response claims every input item committed since the last one.
            # More than one is the ambiguity case (b) — recorded, not resolved here.
            rec.candidates = list(self._unclaimed)
            self._unclaimed.clear()
        self._responses[rid] = rec
        self._current_response = rid
        return []

    async def _on_call(self, event: LiveEvent) -> list[Decision]:
        payload = event.payload or {}
        call_id = payload.get("call_id") or ""
        name = payload.get("name") or ""
        args = payload.get("arguments")
        rid = event.response_id or ""

        if name not in TOOL_NAMES:
            await self._refuse(call_id, REFUSE_UNKNOWN_TOOL,
                               f"unknown tool {name!r}", response_id=rid)
            return []
        if not isinstance(args, dict):
            # `None` from the adapter: the blob was absent or unparsable. Never coerced.
            await self._refuse(call_id, REFUSE_MALFORMED,
                               "arguments missing or unparsable", response_id=rid)
            return []

        # ---- THE JOIN: the call must sit on a user-origin response with exactly one
        # committed input item. It exists to prove the OPERATOR authorized this call.
        rec = self._responses.get(rid)
        origin = rec.origin if rec is not None else "user"
        if origin != "user":
            # Challenge / narration / availability responses are created with
            # `tool_choice: none`; a call on one is never a decision.
            await self._refuse(call_id, REFUSE_NOT_USER_ORIGIN, "not a user turn",
                               response_id=rid)
            return []
        candidates = list(rec.candidates) if rec is not None else []
        if len(candidates) == 0:
            await self._refuse(call_id, REFUSE_NO_INPUT,
                               "no committed input item for this response", response_id=rid)
            return []
        if len(candidates) > 1:
            await self._refuse(call_id, REFUSE_AMBIGUOUS_INPUT,
                               f"{len(candidates)} candidate input items", response_id=rid)
            return []

        # ---- argument validation, before anything is buffered.
        if not isinstance(args.get("text"), str) or not isinstance(args.get("interrupt"), bool):
            await self._refuse(call_id, REFUSE_MALFORMED,
                               "relay needs text (string) and interrupt (boolean)",
                               response_id=rid)
            return []

        pending = _PendingCall(response_id=rid, item_id=event.item_id or "", call_id=call_id,
                               name=name, arguments=args, input_item_id=candidates[0],
                               generation=event.generation)
        got = self._transcripts.get(pending.input_item_id)
        if got is None:
            # Not yet: buffer. `input_audio_transcription.completed` routinely lands AFTER
            # the call on this wire (R0 seq 47 call, seq 55 transcript).
            self._waiting.setdefault(pending.input_item_id, []).append(pending)
            return []
        return await self._settle(pending, got)

    async def _settle(self, pending: _PendingCall, transcript: dict[str, Any]) -> list[Decision]:
        """Turn one joined call into its decision, now that the transcript has landed."""
        if not _usable(transcript):
            # A FAILED transcription is TERMINAL for this item. It used to release a
            # `Request` with an empty transcript, which handed the backend the model's
            # interpretation of words nobody could verify — the one case where the model's
            # reconstruction has no evidence beside it at all. The loop is told the item
            # failed and consumes any armed effect with it.
            await self._session.call_output(
                pending.call_id,
                {"ok": False, "error": "the operator's words could not be transcribed"})
            self.refusals.append({"call_id": pending.call_id, "reason": REFUSE_TRANSCRIPT,
                                  "detail": "transcription failed",
                                  "response_id": pending.response_id})
            return [TranscriptFailed(input_item_id=pending.input_item_id,
                                     generation=pending.generation)]

        return [self._build_request(pending, transcript)]

    async def _on_transcript(self, event: LiveEvent) -> list[Decision]:
        iid = event.input_item_id or ""
        payload = event.payload or {}
        record = {"text": payload.get("text") or "", "complete": bool(payload.get("complete"))}
        self._transcripts[iid] = record
        released = self._waiting.pop(iid, [])
        decisions: list[Decision] = []
        for pending in released:
            decisions.extend(await self._settle(pending, record))
        if not released and not _usable(record):
            # Nothing was waiting on it, but the item still failed: the loop needs to know,
            # because an armed effect is consumed by the next committed item WHATEVER
            # happens to its transcript.
            decisions.append(TranscriptFailed(input_item_id=iid,
                                              generation=event.generation))
        return decisions

    def _build_request(self, pending: _PendingCall, transcript: dict[str, Any]) -> Request:
        """Both texts, labelled and UNEDITED — raw ASR as evidence, the model's `text` as
        interpretation. Only reached with a COMPLETE transcript."""
        return Request(
            transcript=transcript["text"],
            interpretation=pending.arguments.get("text", ""),
            priority="now" if pending.arguments["interrupt"] else "next",
            input_item_id=pending.input_item_id,
            response_id=pending.response_id,
            call_id=pending.call_id,
            generation=pending.generation,
        )

    async def _refuse(self, call_id: str, reason: str, detail: str, *,
                      response_id: str = "") -> None:
        """No decision, and the model is told why — so it can rephrase rather than repeat."""
        self.refusals.append({"call_id": call_id, "reason": reason, "detail": detail,
                              "response_id": response_id})
        if call_id:
            await self._session.call_output(call_id, {"ok": False, "error": detail})

    # ------------------------------------------------------------------ interruption

    async def _on_speech_started(self) -> None:
        """The operator started talking: cut what they HEAR, then what the wire still holds.

        Playback outlasts the wire by ~3x — `response.done` arrives while the speakers still
        hold seconds of that response. The first port keyed the cut on the wire's view and
        bailed once the response was done, so a short answer whose audio had fully arrived
        could not be interrupted at all (measured live: "sometimes it works"). The wire
        cancel is for a response the provider still considers open; the local cut and the
        truncate are for any response with audio the sink has not finished rendering. A
        response that never carried audio is not touched: nothing to cut, no epoch to spend.
        """
        wire = self._current_response
        if wire:
            rec = self._responses.get(wire)
            if rec is None or not rec.done:
                await self._session.cancel_response(wire)
        for rid in dict.fromkeys(r for r in (wire, self._audible) if r):
            rec = self._responses.get(rid)
            if rec is None or not rec.audio_items:
                continue
            if self._sink.reached_end(rid):
                continue
            self._sink.cancel(rid)
            # Truncate ONLY with an unambiguous rendered item. `rendered_ms` is frames handed
            # to the device; queued-but-undrained audio is not something the operator heard.
            rendered = [(iid, self._sink.rendered_ms(rid, iid)) for iid in rec.audio_items]
            rendered = [(iid, ms) for iid, ms in rendered if ms is not None and ms > 0]
            if len(rendered) != 1:
                continue
            item_id, ms = rendered[0]
            await self._session.truncate(item_id, ms)
