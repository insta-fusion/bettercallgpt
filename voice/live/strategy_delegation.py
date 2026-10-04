"""SECOND PLUG: the model's delegation IS the routing — the parallel of `strategy_function.py`.

On GPT-Live the model decides by itself to hand work off (`session.delegation.created`); the
adapter (`gpt_live.py`) has already cut the operator's words for that delegation out of the
input transcript by the span rule and told the loop's log about the turn. This file turns
the delegation into the contract:

  * a non-blank span → ONE `Request` whose `transcript` AND `interpretation` are the span,
    verbatim. There is no second model writing an interpretation (an earlier design proposed
    `agent/delegate.py`; dropped 2026-09-23): the backend is an agent that reads words itself,
    and a paraphrase with no evidence beside it is exactly what the realtime join refuses.
    `priority` is always `next` — the wire carries no "interrupt" bit, and a guessed `now`
    would abort the backend's running turn on words that may only be a prefix
    (DESIGN.md §Conversation model: offset_ms is a cursor, not a completeness claim).
  * a blank span → `TranscriptFailed`, and the delegation is ANSWERED anyway: an unanswered
    delegation leaves the model repeating "我正在确认" (live, 2026-09-23).

It also owns interruption on this wire, which is local only: `KIND_INPUT_SPEECH_STARTED`
(the operator talking over queued audio) cancels the speech segments the operator talked
over. There is no
cancel and no truncate to send.

No keyword tables, no time literals, no sleeps.
"""
from __future__ import annotations

from typing import Any

from .base import (
    KIND_DELEGATION_CREATED,
    KIND_INPUT_SPEECH_STARTED,
    AudioSink,
    Decision,
    LiveEvent,
    Request,
    TranscriptFailed,
)

REFUSE_BLANK_SPAN = "blank_span"

# Spoken back on a delegation that carried no words, so the model stops waiting on it.
# Quiet context when the span is blank because the operator's own Steer already sent it.
STEERED_SPAN_NOTE = "这些话操作者已经按“立即发送”直接交给 backend 了,不用再送,也不用说明。"
BLANK_SPAN_NOTE = "这次没听到要交给 backend 的话,没有送出。需要的话直接问操作者一句。"


class DelegationStrategy:
    """`Strategy.feed` over the GPT-Live client-delegation wire.

    `session` is a `GptLiveSession` (`append` and `flush_playback` are used); `sink` is the
    `AudioSink`, held for parity with `FunctionStrategy` — the adapter owns the segment keys.
    """

    def __init__(self, session: Any, sink: AudioSink | None = None) -> None:
        self._session = session
        self._sink = sink
        self.refusals: list[dict[str, Any]] = []
        self.interruptions: list[dict[str, Any]] = []

    async def feed(self, event: LiveEvent) -> list[Decision]:
        if event.kind == KIND_DELEGATION_CREATED:
            return await self._on_delegation(event)
        if event.kind == KIND_INPUT_SPEECH_STARTED:
            rendered = self._session.flush_playback()
            self.interruptions.append({"start_ms": event.start_ms, "rendered_frames": rendered,
                                       "segment": (event.payload or {}).get("segment")})
            return []
        return []

    async def _on_delegation(self, event: LiveEvent) -> list[Decision]:
        did = event.delegation_id or ""
        span_id = event.input_item_id or ""
        text = str((event.payload or {}).get("text") or "").strip()
        if not text:
            self.refusals.append({"call_id": did, "reason": REFUSE_BLANK_SPAN,
                                  "detail": "no input words before this delegation's cursor"})
            if getattr(self._session, "steer_claimed", False):
                self._session.steer_claimed = False
                await self._session.append("thinking", did or None, STEERED_SPAN_NOTE)
            else:
                await self._session.append("commentary", did or None, BLANK_SPAN_NOTE)
            return [TranscriptFailed(input_item_id=span_id, generation=event.generation)]
        self._session.steer_claimed = False      # words of its own: nothing left to explain
        return [Request(transcript=text, interpretation=text, priority="next",
                        input_item_id=span_id, response_id=did, call_id=did,
                        generation=event.generation)]
