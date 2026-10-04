"""The agent loop — the ONE place events become actions.

Everything the modules before it describe; this decides and acts. It is the only async file in the core
because it is the only one that talks to anything: the live session, the backend, the audio sink,
the ledger. It holds no policy of its own — the conversation log says what is true, the broker
says whether consent was given, and the loop turns those answers into calls.

The shape is one decision, two injections, one lock:

* **One decision.** The model has one tool, `relay(text, interrupt)`. Whether a sentence is
  work for the backend, chit-chat, noise, or a question it can answer itself is the model's
  own judgment; the loop only proves the join (input item → user response → call) and posts.
* **Two injections.** Every word the backend writes enters the session as a labelled system
  message: a finished turn that answers a voice request is injected AND spoken; everything
  else the operator could read in the terminal — backend progress, a result to something they
  typed, their own typed lines — is injected silently, as context. Nothing is shelved, nothing
  is deduplicated away, nothing waits for the operator to ask (measured 2026-09-21: the
  supersede/retain machinery this replaces lost both answers of a live run).
* **One lock.** Consent to a backend permission dialog is never the model's to give: the
  broker speaks the challenge in our exact words and the operator's next utterance is judged
  by code (R4: the model approved `rm -rf` on an unrelated 「对」 four runs in five).

Four invariants it keeps:

* **Write before you act.** Every actuation is preceded by a ledger record carrying
  `(op_id, request_id, revision)`; a crash between the record and the effect recovers as
  `uncertain`.
* **Ack is not dispatch.** Only a `Request` from the strategy records a task.
* **A posted receipt never opens a response.** After a `call_output` for a refused or uncertain
  hand-off the loop asks for exactly one response with `tool_choice: none`; a posted hand-off
  gets no response at all -- the backend's result is the next thing the operator hears.
* **Owner loss is terminal.** Once the backend says the owner is gone, every further actuation
  is refused.

No timers, no sleeps, no deadlines: every release in this file is keyed to an identity — a
request id, a turn id, a response id, an occurrence id.
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

from .. import config as voice_config
from ..backend import base as backend_base
from ..conversation.log import ConversationLog
from ..audio import cue
from ..live import base as live_base
from ..live.port import SessionVoice
from .broker import Broker
from .ledger_port import LedgerPort

# The ONE list of what this loop writes to the ledger as an op `kind`. The daemon builds the
# ledger's vocabulary from it, never from a restatement.
EFFECT_KINDS = frozenset({"send", "answer"})

# Live events that are the connection looking after itself, not anyone doing anything: they
# never count as presence for the daemon's idle close (a stream of provider errors kept a
# forgotten session open).
_UPKEEP_KINDS = frozenset({live_base.KIND_ERROR, live_base.KIND_DISCONNECTED,
                           live_base.KIND_CLOSED, live_base.KIND_SESSION_STARTED})
# An utterance is over when the provider says so, or when the wire it was on is gone.
_SPEECH_ENDS = frozenset({live_base.KIND_INPUT_SPEECH_STOPPED, live_base.KIND_INPUT_COMMITTED,
                          live_base.KIND_DISCONNECTED, live_base.KIND_CLOSED})
# Evidence the operator is talking on the current wire: either one cuts audio a previous
# provider session left queued at the speaker (a relay's leftover).
_OPERATOR_VOICE = frozenset({live_base.KIND_INPUT_SPEECH_STARTED, live_base.KIND_INPUT_TRANSCRIPT})
# A relay's recap: this many recent turns, each clipped to this many UTF-8 bytes (counts and
# sizes, not durations; the provider's `Capabilities.recap_bytes` bounds the whole).
RECAP_TURNS = 8
RECAP_TURN_BYTES = 240


@dataclass
class LoopStats:
    """What happened, for tests and for the operator-facing diagnostic. Counts, never timings."""
    dispatched: int = 0
    refused: int = 0
    effects_executed: int = 0
    effects_refused: int = 0
    runaway_cuts: int = 0
    refusals: list[str] = field(default_factory=list)


class AgentLoop:
    """Wires a live session, a strategy, a backend, a broker and a ledger into one conversation."""

    def __init__(self, *, session: live_base.LiveSession, strategy: live_base.Strategy,
                 backend: backend_base.Backend, ledger: LedgerPort,
                 log: ConversationLog | None = None, broker: Broker | None = None,
                 sink: live_base.AudioSink | None = None,
                 terminal_only: bool = False,
                 voice: live_base.VoicePort | None = None) -> None:
        self.session = session
        # Every word the loop puts into the conversation goes through the port, as an intent.
        # A bare session (tests, the realtime family) gets the realtime verbs.
        if voice is None:
            if not isinstance(session, live_base.RealtimeControls):
                raise TypeError("this session has no realtime verbs: pass its provider's "
                                "VoicePort (voice/live/providers.py builds it)")
            voice = SessionVoice(session)
        self.voice = voice
        self.strategy = strategy
        self.backend = backend
        self.ledger = ledger
        self.log = log or ConversationLog()
        # A wire without response identity has no delivery evidence, so a spoken consent
        # challenge could never be proven heard: approvals stay on the terminal (DESIGN.md §Consent).
        self.broker = broker or Broker(
            terminal_only=terminal_only or not self.voice.capabilities.response_identity)
        self.sink = sink
        self.stats = LoopStats()

        self.owner_lost: str | None = None
        self._ids = itertools.count(1)
        # backend turn → the voice request it executes, so a result knows whether it answers
        # something the operator said by voice (spoken) or something else (context only).
        self._request_of_turn: dict[str, str] = {}
        # EVERY voice request a turn absorbed, in receipt order. The backend can fold a second
        # request into a running turn; each one must see the turn's result (on GPT-Live each
        # is a delegation that waits for its own answer). `_request_of_turn` keeps the last,
        # which is what labels the result.
        self._requests_of_turn: dict[str, list[str]] = {}
        # An input item that must be offered to the broker once its transcript completes.
        self._pending_consumer: str | None = None
        self._message_items: dict[str, int] = {}
        self._message_item_ids: dict[str, set[str]] = {}
        # The goodbye: the daemon waits on this before it closes the socket, so the words
        # are heard. Set when the response created under the `farewell` origin is done.
        self._farewell_rid: str | None = None
        self._farewell_done = asyncio.Event()
        # Set when the live stream ends. Without response identity there is no goodbye
        # response to see finish, so the goodbye is heard for as long as the daemon's
        # lifecycle bound allows, or until the stream ends — whichever is first.
        self._live_ended = asyncio.Event()
        # True only while the live pump is reading the socket. Off before `run` and after
        # the stream ends: then no goodbye can be spoken or heard back, and `farewell`
        # returns at once instead of waiting out the daemon's bound.
        self.live_pumping = False
        # Set by every event from either side that shows the conversation or the work moving
        # (not connection upkeep: errors, drops, reconnects). The daemon's idle close clears
        # it and waits; the loop itself never reads it, so no clock enters here.
        self.stirred = asyncio.Event()
        # True from the operator's speech start until it stops or commits: one utterance is
        # one event on the wire, so the idle close reads this instead of waiting for another.
        self.speaking = False

        # ---- the relay (a fresh provider session under the same conversation) ----
        # The daemon's hook for a provider session that ended by itself:
        # `async reconnect(reason, dead_session, barren) -> bool`. None: the conversation ends
        # with its first provider session, as it always did.
        self.reconnect: Any = None
        self.generation = getattr(session, "generation", 0)
        self.relays = 0
        # Cleared while a relay swaps sessions: backend words wait here instead of going to a
        # socket that is closing or gone, and reach the new session after its recap.
        self._voice_ready = asyncio.Event()
        self._voice_ready.set()
        # Clear while one backend observation is being handled (`settle` waits on it).
        self._obs_idle = asyncio.Event()
        self._obs_idle.set()
        # Set when a relay adopts a new session or the live side ends: a backend word that
        # died with its socket waits on it, then goes to the new session.
        self._swapped = asyncio.Event()
        # Set once the CURRENT provider session's stream has ended (a drop, a provider close,
        # or the end of the run); cleared when a relay adopts a new one. A rollover waiting
        # for a quiet moment stops waiting on it: a dead session has nothing left to protect.
        self.leg_ended = asyncio.Event()
        # Quiet-moment evidence for a scheduled rollover (`until_quiet`): decisions whose call
        # or delegation is not answered yet, and responses on the wire not done yet (only on
        # a wire that reports `done`). `changed` is set by every live event and transition.
        self._unanswered = 0
        self._open_responses: set[str] = set()
        self.changed = asyncio.Event()
        # Sink keys a previous provider session left queued: cut the moment the operator
        # talks on the new one, so interruption keeps working across a relay.
        self._leftover: set[str] = set()
        # The voice's words of the utterance in progress (partial transcripts), and whether
        # the current provider session carried anything but upkeep.
        self._voice_said: list[str] = []
        self._leg_stirred = False

    # ------------------------------------------------------------------ identity

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    # ------------------------------------------------------------------ live side

    async def handle_live_event(self, event: live_base.LiveEvent) -> None:
        """One provider event: update the log, feed the strategy, act on what it yields."""
        kind = event.kind
        self.changed.set()
        if kind not in _UPKEEP_KINDS:
            self.stirred.set()
            self._leg_stirred = True
        if kind in _OPERATOR_VOICE and self._leftover:
            self._cut_leftover()
        if kind == live_base.KIND_OUTPUT_TRANSCRIPT:
            self._note_voice(event)
        if kind == live_base.KIND_INPUT_SPEECH_STARTED:
            self.speaking = True
        elif kind == live_base.KIND_INPUT_TRANSCRIPT and event.payload.get("partial"):
            # A provider without speech-start events (GPT-Live) reports the operator's words
            # as they come: each partial one IS the operator speaking, until the adapter says
            # the turn is over (`speech_stopped`) or a delegation commits it.
            self.speaking = True
        elif kind in (live_base.KIND_DISCONNECTED, live_base.KIND_CLOSED):
            self.speaking = False         # the wire is gone: nobody is speaking on it
        elif kind in _SPEECH_ENDS and self.voice.capabilities.speech_end:
            # Only a wire that REPORTS the end of speech ends it. On one that does not
            # (GPT-Live), an input item committed by a delegation is the model's decision,
            # not proof the operator stopped.
            self.speaking = False
        if kind in (live_base.KIND_DISCONNECTED, live_base.KIND_CLOSED):
            # Nothing on a gone wire is still open: a response it started will never report
            # `done`, and waiting for one would keep a relay from ever finding quiet.
            self._open_responses.clear()

        if kind == live_base.KIND_INPUT_COMMITTED and event.input_item_id:
            self.log.commit_input(event.input_item_id)
            # The next thing the operator says consumes any delivered arm, whatever it says.
            if self.broker.armed is not None:
                self._pending_consumer = event.input_item_id

        elif kind == live_base.KIND_INPUT_TRANSCRIPT and event.input_item_id:
            if event.payload.get("complete"):
                text = str(event.payload.get("text") or "")
                failed = bool(event.payload.get("failed")) or not text.strip()
                self.log.complete_transcript(event.input_item_id, text, failed=failed)
                if not failed:
                    self._flush_voice()
                    self.log.note_turn(OPERATOR_SPEAKER, text)
                # Evidence, not decision: what the operator was heard to say.
                await self.ledger.append({"kind": "heard", "item_id": event.input_item_id,
                                          "text": text, "failed": failed})
                if self._pending_consumer == event.input_item_id:
                    self._pending_consumer = None
                    await self._consume_arm(text, failed=failed)

        elif kind == live_base.KIND_RESPONSE_CREATED and event.response_id:
            origin = str(event.payload.get("origin") or "user")
            self.log.response_created(event.response_id, origin)
            if self.voice.capabilities.response_identity:
                self._open_responses.add(event.response_id)
            if origin == "challenge":
                self.broker.speaking(event.response_id)
            elif origin == "farewell":
                self._farewell_rid = event.response_id

        elif kind == live_base.KIND_RESPONSE_DONE and event.response_id:
            status = str(event.payload.get("status") or "completed")
            self.log.response_done(event.response_id, status)
            self._open_responses.discard(event.response_id)
            self.broker.note_response_done(event.response_id, status)
            await self._check_rendered(event.response_id)
            if event.response_id == self._farewell_rid:
                self._farewell_done.set()

        elif kind == live_base.KIND_OUTPUT_TRANSCRIPT and event.response_id:
            if event.payload.get("done"):
                spoken = str(event.payload.get("text") or "")
                self.broker.note_output_transcript(event.response_id, spoken)
                # Evidence, not decision: what the voice said, per response.
                await self.ledger.append({"kind": "spoken", "response_id": event.response_id,
                                          "text": spoken})
                await self._count_message_item(event.response_id, event.item_id)
                await self._check_rendered(event.response_id)

        elif kind == live_base.KIND_OUTPUT_ITEM and event.response_id:
            if event.payload.get("type") == "message" and not event.payload.get("done"):
                await self._count_message_item(event.response_id, event.item_id)

        elif kind == live_base.KIND_DISCONNECTED or kind == live_base.KIND_SESSION_STARTED:
            await self._reset_arms(kind)

        for decision in await self.strategy.feed(event):
            await self.handle_decision(decision)

    async def _count_message_item(self, response_id: str, item_id: str | None) -> None:
        """The second message item in ANY response is a runaway -- measured live 2026-09-20/21:
        one response emitted ~50 items of "Let me rephrase that a bit more clearly.", and
        user-origin responses produced 11-20 sentence bursts without reaching their call.
        Counted per distinct item id from EITHER the item announcement or its finished
        transcript; cut at the second one. Harness-level and wordless."""
        ids = self._message_item_ids.setdefault(response_id, set())
        key = item_id or f"anon-{len(ids) + 1}"
        if key in ids:
            return
        ids.add(key)
        n = len(ids)
        self._message_items[response_id] = n
        if n > 1:
            self.stats.runaway_cuts += 1
            await self.voice.cut(response_id)
            if self.sink is not None:
                self.sink.cancel(response_id)

    async def _check_rendered(self, response_id: str) -> None:
        """Ask the sink whether the challenge response reached its last frame. Identity only.

        Only a CONCLUSIVE answer is passed on: reached the end (delivered) or the audio epoch
        moved (interrupted). "Not finished yet" is neither — the transcript's `done` arrives
        while the speaker is still playing, and judging then would tombstone every challenge.
        The question is asked again on `response.done` and when the operator answers.
        """
        effect = self.broker.armed
        if effect is None or effect.response_id != response_id or self.sink is None:
            return
        reached = bool(self.sink.reached_end(response_id))
        unchanged = bool(self.sink.epoch_unchanged(response_id))
        if not reached and unchanged:
            return
        self.broker.note_rendered(response_id, reached_end=reached, epoch_unchanged=unchanged)

    async def _reset_arms(self, reason: str) -> None:
        dead = self.broker.reset(reason)
        if dead is not None:
            self.log.tombstone("effect", dead.effect_id, reason)
            await self.voice.context(
                _system_text({"effect_cleared": {"effect_id": dead.effect_id, "reason": reason}}))

    # ------------------------------------------------------------------ decisions

    async def handle_decision(self, decision: live_base.Decision) -> None:
        # Evidence, not decision: what the model decided, on which input, in which response.
        await self.ledger.append({
            "kind": "decided", "tool": type(decision).__name__.lower(),
            "response_id": getattr(decision, "response_id", None),
            "input_item_id": getattr(decision, "input_item_id", None),
            "call_id": getattr(decision, "call_id", None)})
        if isinstance(decision, live_base.Request):
            await self._on_request(decision)
        elif isinstance(decision, live_base.TranscriptFailed):
            await self._on_transcript_failed(decision)

    async def _on_transcript_failed(self, decision: live_base.TranscriptFailed) -> None:
        """The provider committed an input item and could not transcribe it. Terminal for that
        item, and it CONSUMES any armed effect exactly as a non-matching utterance would: an
        arm must never outlive the next thing the operator said."""
        self.log.commit_input(decision.input_item_id)
        self.log.complete_transcript(decision.input_item_id, "", failed=True)
        await self.ledger.append({"kind": "heard", "item_id": decision.input_item_id,
                                  "text": "", "failed": True})
        if self._pending_consumer == decision.input_item_id or self.broker.armed is not None:
            self._pending_consumer = None
            await self._consume_arm("", failed=True)

    async def _on_request(self, decision: live_base.Request) -> None:
        """A dispatch. The strategy already proved the join; the loop proves the rest. Until
        its call is answered, no rollover may take the session it was asked on."""
        self._unanswered += 1
        try:
            await self._dispatch(decision)
        finally:
            self._unanswered -= 1
            self.changed.set()

    async def _dispatch(self, decision: live_base.Request) -> None:
        item_id, reason = self.log.join(decision.response_id)
        if item_id is None or item_id != decision.input_item_id:
            await self._refuse_call(decision.call_id, reason if item_id is None else "join_mismatch")
            return
        if self.owner_lost is not None:
            await self._refuse_call(decision.call_id, "owner_lost")
            return

        request_id = self._next_id("req")
        record = self.log.open_request(
            request_id, input_item_id=decision.input_item_id, response_id=decision.response_id,
            call_id=decision.call_id, interpretation=decision.interpretation,
            priority=decision.priority)
        op_id = self._next_id("op")
        payload = {"text": decision.interpretation, "transcript": decision.transcript,
                   "interpretation": decision.interpretation, "priority": decision.priority}
        # Write-ahead, ALWAYS before the actuation.
        self.ledger.record_op(op_id, record.request_id, record.revision, "send", payload)
        receipt = await self.backend.send(
            decision.interpretation, tag=record.request_id, priority=decision.priority,
            transcript=decision.transcript, interpretation=decision.interpretation)
        self.ledger.set_outcome(op_id, receipt.outcome, receipt.reason or "")
        if receipt.outcome == "refused":
            self.log.refuse_request(record.request_id, receipt.reason or "backend_refused")
            self.stats.refused += 1
            self.stats.refusals.append(f"{record.request_id}:{receipt.reason}")
        else:
            self.log.mark_dispatched(record.request_id, None)
            self.stats.dispatched += 1
        await self._answer_call(decision.call_id, {
            "accepted": receipt.outcome != "refused",
            "outcome": receipt.outcome,
            "request_id": record.request_id,
            "reason": receipt.reason,
            "note": _receipt_words(receipt.outcome, receipt.reason),
        }, speak=receipt.outcome != "posted")

    async def operator_request(self, text: str) -> Any:
        """Words the operator sent with Steer: no model decided this hand-off, the operator's
        own press did. Its identity is the press, so there is no response to join; everything
        else is a dispatch like any other — heard, written ahead, sent once, never resent.
        `now`: the press means "take this now", which ends the backend's running turn."""
        request_id = self._next_id("req")
        item_id = f"{request_id}:steer"
        self.log.complete_transcript(item_id, text)
        self.log.note_turn(OPERATOR_SPEAKER, text)
        await self.ledger.append({"kind": "heard", "item_id": item_id, "text": text,
                                  "failed": False, "origin": "steer"})
        record = self.log.open_request(
            request_id, input_item_id=item_id, response_id=item_id, call_id=item_id,
            interpretation=text, priority="now")
        op_id = self._next_id("op")
        payload = {"text": text, "transcript": text, "interpretation": text,
                   "priority": "now", "origin": "steer"}
        self.ledger.record_op(op_id, record.request_id, record.revision, "send", payload)
        receipt = await self.backend.send(text, tag=record.request_id, priority="now",
                                          transcript=text, interpretation=text)
        self.ledger.set_outcome(op_id, receipt.outcome, receipt.reason or "")
        if receipt.outcome == "refused":
            self.log.refuse_request(record.request_id, receipt.reason or "backend_refused")
            self.stats.refused += 1
            self.stats.refusals.append(f"{record.request_id}:{receipt.reason}")
        else:
            self.log.mark_dispatched(record.request_id, None)
            self.stats.dispatched += 1
        self.changed.set()
        return receipt

    # ------------------------------------------------------------------ call plumbing

    async def _answer_call(self, call_id: str, output: dict[str, Any], *,
                           speak: bool = True) -> None:
        """One `create_response` after a `call_output` -- or none, when the operator was
        already answered on the response that carried the call. How is the port's business
        (`live/port.py` for the realtime family: `tool_choice="none"` on that response only)."""
        await self.voice.receipt(call_id, output, speak=speak)

    async def _refuse_call(self, call_id: str, reason: str) -> None:
        self.stats.refused += 1
        self.stats.refusals.append(reason)
        await self.ledger.append({"kind": "call_refused", "call_id": call_id, "reason": reason})
        await self._answer_call(call_id, {"accepted": False, "outcome": "refused",
                                          "reason": reason,
                                          "note": _receipt_words("refused", reason)})

    # ------------------------------------------------------------------ backend side

    async def handle_observation(self, obs: backend_base.Observation) -> None:
        self.stirred.set()
        kind = obs.kind
        # Evidence, not decision: what the backend side reported, before any mapping.
        await self.ledger.append({
            "kind": "observed", "obs": kind, "turn_id": obs.turn_id, "tag": obs.tag,
            "disposition": (obs.payload or {}).get("disposition"),
            "activity": (obs.payload or {}).get("activity"),
            # Redact BEFORE clipping: a secret cut at the boundary would leave an unmaskable
            # prefix behind (the ledger's own masking only sees whole values).
            "text": voice_config.redact_text(obs.text or "")[:120]})

        if kind == backend_base.OBS_OWNER_LOST:
            self.owner_lost = obs.text or obs.payload.get("reason") or "owner_lost"
            await self._reset_arms("owner_lost")
            return

        if kind == backend_base.OBS_RECEIPT and obs.tag and obs.turn_id:
            if self.log.request(obs.tag) is None:
                # Not one of ours: the relay's qualification probe, or a tag from an earlier
                # run of this session. Its turn's result is still context (below), never a
                # spoken answer to a request nobody made.
                return
            # THE TAG IS THE REQUEST ID: this is how a dispatch learns which turn owns it.
            self._request_of_turn[obs.turn_id] = obs.tag
            absorbed = self._requests_of_turn.setdefault(obs.turn_id, [])
            if obs.tag not in absorbed:
                absorbed.append(obs.tag)
            self.log.mark_dispatched(obs.tag, obs.turn_id)
            return

        if kind == backend_base.OBS_PROGRESS and obs.text.strip():
            # Context, not a cue to speak: what the operator would see scrolling by.
            await self.voice.context(
                _backend_text(PROGRESS_PREFIX, self._label(obs.turn_id), obs.text))
            return

        if kind == backend_base.OBS_TYPED and obs.text.strip():
            # The operator's own words at the keyboard. Context, so 「我刚才打了什么」 has an
            # answer; never spoken back.
            await self.voice.context(_backend_text(TYPED_PREFIX, "", obs.text))
            return

        if kind == backend_base.OBS_RESULT and obs.turn_id:
            await self._on_result(obs)
            return

        if kind == backend_base.OBS_DIALOG and obs.dialog is not None:
            await self._on_dialog(obs)

    def _label(self, turn_id: str | None) -> str:
        request_id = self._request_of_turn.get(turn_id or "")
        return f"请求 {request_id}" if request_id else ""

    async def _on_result(self, obs: backend_base.Observation) -> None:
        """A backend turn finished. ALWAYS injected. Spoken only when the turn answers a
        request the operator made by voice; a result to something they typed, or to the
        relay's probe, is context they can already read."""
        request_ids = self._requests_of_turn.get(obs.turn_id or "", [])
        answers: list[str] | None = None
        if request_ids:
            answers = []
            for request_id in request_ids:
                self.log.mark_resolved(request_id)
                record = self.log.request(request_id)
                answers.append(record.call_id if record is not None else "")
        await self.voice.result(_backend_text(RESULT_PREFIX, self._label(obs.turn_id), obs.text),
                                answers=answers)
        # Noted once it went out: a result retried after a relay is one turn, not two.
        self.log.note_turn(BACKEND_SPEAKER, obs.text or "")

    async def _on_dialog(self, obs: backend_base.Observation) -> None:
        dialog = obs.dialog
        transition = str(obs.payload.get("transition") or "open")
        if transition == "closed":
            self.broker.dialog_closed(dialog.occurrence_id)
            return
        effect = self.broker.arm(dialog, self.log.revision)
        if effect is None:
            if self.broker.terminal_only and transition == "open":
                # No keyboard surface: the dialog is answered at the terminal, and the operator
                # must at least hear that the backend is waiting there. A request the session's
                # own hooks module reported is said as a request: another hook or the host may
                # decide it without any dialog.
                lead = PERMISSION_ASKED if obs.payload.get("source") == "hook" else DIALOG_WAITING
                await self.voice.announce(
                    _backend_text(RESULT_PREFIX, "", f"{lead}\n{dialog.prompt}"),
                    "narration")
            return
        # Said out loud in our exact words, or it can never gather its delivery evidence.
        await self.voice.challenge(self.broker.challenge_text() or "")

    # ------------------------------------------------------------------ consent

    async def _consume_arm(self, transcript: str, *, failed: bool) -> None:
        """The operator said the next thing. It consumes the arm whatever it is."""
        if self.broker.armed is not None and self.broker.armed.response_id:
            await self._check_rendered(self.broker.armed.response_id)
        state = await self.backend.state()
        current = state.dialog.occurrence_id if state.dialog else None
        verdict = self.broker.consume(transcript, current_occurrence_id=current, failed=failed)
        if verdict.outcome != "execute":
            if verdict.outcome in ("refuse", "tombstone"):
                self.stats.effects_refused += 1
                self.log.tombstone("effect", verdict.effect_id or "", verdict.reason)
            return
        if self.owner_lost is not None:
            self.stats.effects_refused += 1
            return
        op_id = self._next_id("op")
        self.ledger.record_op(op_id, verdict.effect_id or "", self.log.revision, "answer",
                              {"occurrence_id": verdict.occurrence_id, "choice": verdict.choice})
        receipt = await self.backend.answer(verdict.occurrence_id or "", verdict.choice or "")
        self.ledger.set_outcome(op_id, receipt.outcome, receipt.reason or "")
        if receipt.outcome == "refused":
            self.stats.effects_refused += 1
        else:
            self.stats.effects_executed += 1

    # ------------------------------------------------------------------ recovery

    def recover(self) -> dict[str, str]:
        """What the ledger remembers across a crash. Reported, never replayed."""
        return dict(self.ledger.recover())

    # ------------------------------------------------------------------ session edges

    async def notice_open(self) -> None:
        """The relay can carry a send: tell the model, and let it greet in its own words.
        Nothing here is a line to say — the prompt owns how the model opens."""
        await self.voice.announce(_backend_text(VOICE_PREFIX, "", OPENED), "narration")

    async def farewell(self, reason: str) -> None:
        """Tell the model the session is ending and return once its goodbye is DONE on the
        wire. The daemon then drains the speaker before it closes anything. No clock here —
        the daemon bounds the wait as a lifecycle port."""
        if not self.live_pumping:
            return
        await self.voice.announce(_backend_text(VOICE_PREFIX, "", f"{CLOSING}:{reason}"),
                                  "farewell")
        if not self.voice.capabilities.response_identity:
            # No response id, no `done`: no event on this wire says the goodbye is over. The
            # daemon's bound IS the goodbye window (it drains the speaker after it); only the
            # end of the stream releases it sooner. First audio is not completion — it may be
            # the tail of the previous answer (review r1 finding 2).
            await self._live_ended.wait()
            return
        await self._farewell_done.wait()

    # ------------------------------------------------------------------ the relay

    def _note_voice(self, event: live_base.LiveEvent) -> None:
        """The voice's words, for the recap: a finished transcript is one turn; partials
        (GPT-Live has no other kind) gather until the operator's next turn."""
        text = str(event.payload.get("text") or "")
        if event.payload.get("done"):
            self._voice_said = []
            self.log.note_turn(VOICE_SPEAKER, text)
        elif text:
            self._voice_said.append(text)

    def _flush_voice(self) -> None:
        said, self._voice_said = "".join(self._voice_said), []
        self.log.note_turn(VOICE_SPEAKER, said)

    def _flush_operator(self, session: Any) -> None:
        """Words the operator said that no turn has claimed yet, as the ADAPTER counts them
        (a GPT-Live delegation claims only up to its cursor): a relay never loses the first
        half of a question."""
        unclaimed = getattr(session, "unclaimed_words", None)
        if unclaimed is not None:
            self.log.note_turn(OPERATOR_SPEAKER, unclaimed())

    def _cut_leftover(self) -> None:
        """The operator talked on the new session: what the old one left at the speaker goes.
        `retire` never touches audio of the new session already in the device buffer."""
        keys, self._leftover = self._leftover, set()
        if self.sink is None:
            return
        retire = getattr(self.sink, "retire", None)
        if retire is not None:
            retire(keys)
        else:
            for key in keys:
                self.sink.cancel(key)

    def recap(self, budget: int | None = None) -> str:
        """What a fresh provider session needs to carry on: the requests still in flight (by
        tag) and the last few turns, newest kept first when `budget` (the receiving
        provider's `recap_bytes`) is short. Redacted per turn BEFORE any clip, so a cut can
        never leave half a secret."""
        self._flush_voice()
        if budget is None:
            budget = self.voice.capabilities.recap_bytes
        head = f"{VOICE_PREFIX} {RELAYED}"
        tail: list[str] = []
        flight = self.log.unresolved()
        self._recapped_flight = {r.request_id for r in flight}
        if flight:
            tail.append(_in_flight(flight))
        used = _utf8(head) + sum(_utf8(line) + 1 for line in tail)
        body: list[str] = []
        for speaker, text in reversed(self.log.recent_turns(RECAP_TURNS)):
            line = f"{speaker}: {_clip(voice_config.redact_text(text), RECAP_TURN_BYTES)}"
            if used + _utf8(line) + 1 > budget:
                break
            body.insert(0, line)
            used += _utf8(line) + 1
        return _clip("\n".join([head, *body, *tail]), budget)

    def hold(self) -> None:
        """A relay is swapping sessions: backend words wait until `adopt`."""
        self._voice_ready.clear()

    def release(self) -> None:
        self._voice_ready.set()

    async def settle(self) -> None:
        """`hold`, and wait for the backend observation already being handled to finish, so
        no backend word is half-way to a session a rollover is about to close."""
        self.hold()
        await self._obs_idle.wait()

    async def prepare(self, voice: Any) -> int:
        """Seed a fresh provider session with the recap BEFORE it takes over. Raises when it
        cannot take it (its socket died already): nothing has been swapped, the caller closes
        it. Returns the turn mark `catch_up` sends the rest from."""
        self._flush_voice()              # into the log BEFORE the mark: said once, not twice
        mark = self.log.turns_noted
        await voice.seed(self.recap(voice.capabilities.recap_bytes))
        return mark

    def commit(self, session: Any, strategy: Any, voice: Any) -> None:
        """The swap itself, SYNCHRONOUS: nothing can happen between the quiet moment the
        caller found and the moment the new session owns the conversation. The BACKEND is
        untouched: its turns, tags and in-flight requests carry on."""
        self._flush_voice()
        self._flush_operator(self.session)
        self.session, self.strategy, self.voice = session, strategy, voice
        self.generation = getattr(session, "generation", self.generation + 1)
        self.relays += 1
        self.speaking = False
        self._open_responses.clear()
        self._leg_stirred = False
        self.leg_ended.clear()
        sounding = getattr(self.sink, "queued_ids", None)
        self._leftover = ({key for key in sounding() if not key.startswith(cue.KEY_PREFIX)}
                          if sounding is not None else set())
        self.changed.set()
        self._swapped.set()

    async def catch_up(self, mark: int) -> None:
        """After the swap: the turns said since the recap (the conversation went on while the
        new session opened), and the void of any arm the old session held (no proof the
        operator heard its challenge on this wire). Then backend words flow again. A word the
        new session cannot take means it died already: its own pump finds the drop."""
        try:
            late = [f"{speaker}: {_clip(voice_config.redact_text(text), RECAP_TURN_BYTES)}"
                    for speaker, text in self.log.turns_since(mark)]
            # A request dispatched after the recap is still running: the new session must
            # know it by tag, or it may ask for it again.
            recapped = getattr(self, "_recapped_flight", set())
            flight = [r for r in self.log.unresolved() if r.request_id not in recapped]
            if flight:
                late.append(_in_flight(flight))
            if late:
                await self.voice.seed(_clip(f"{VOICE_PREFIX} {RELAYED_LATE}\n" + "\n".join(late),
                                            self.voice.capabilities.recap_bytes))
            await self._reset_arms("reconnect")
        except Exception as exc:
            if not _wire_died(exc):
                raise
        finally:
            self.release()

    async def adopt(self, session: Any, strategy: Any, voice: Any) -> None:
        """`prepare`, `commit` and `catch_up` in one step (a relay with no quiet to wait for)."""
        mark = await self.prepare(voice)
        self.commit(session, strategy, voice)
        await self.catch_up(mark)

    def quiet(self) -> bool:
        """Nobody is mid-sentence and nothing is owed on the wire. The speaker's queue is
        the other half, awaited by `until_quiet`. Words the provider has heard and no turn
        has claimed yet are an utterance still open, whatever else says so."""
        unclaimed = getattr(self.session, "unclaimed_words", None)
        return (not self.speaking and not self._unanswered and not self._open_responses
                and not (unclaimed is not None and unclaimed()))

    def still_quiet(self) -> bool:
        """Quiet, and no live event since `until_quiet` last saw it so. Synchronous: the
        caller swaps in the same step, with nothing able to happen in between."""
        return self.quiet() and not self.changed.is_set()

    async def until_quiet(self) -> None:
        """Return at a QUIET moment: the operator not speaking, no call or delegation waiting
        for its answer, no response on the wire, and the speaker drained — with no live
        event in between. Keyed to events and the sink's own drain, never to a clock."""
        drained = getattr(self.sink, "drained", None)
        while True:
            self.changed.clear()
            if not self.quiet():
                await self.changed.wait()
                continue
            if drained is not None:
                await drained(asyncio.get_running_loop())
            if not self.changed.is_set() and self.quiet():
                return

    # ------------------------------------------------------------------ pumps

    async def run(self) -> None:
        """Drive both streams until the LIVE one ends. The only concurrency in the core.

        The backend stream waits on the terminal and never ends by itself, so once the live
        stream is over (the provider closed the session, or the socket died) nothing more can
        be heard or said: the backend pump is cancelled and `run` returns, which is what lets
        the daemon tear down. A backend stream that ends first leaves the conversation going.
        A failure in either pump propagates at once.
        """
        live = asyncio.ensure_future(self._pump_live())
        backend = asyncio.ensure_future(self._pump_backend())
        try:
            done, _pending = await asyncio.wait({live, backend},
                                                return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if live not in done:
                await live
        finally:
            for task in (live, backend):
                if not task.done():
                    task.cancel()
            await asyncio.gather(live, backend, return_exceptions=True)

    async def _pump_live(self) -> None:
        """One provider session after another. A session swapped out by a relay winds down
        unheard; one that DROPPED (or that the provider closed) asks the daemon for a fresh
        one, and the conversation goes on in it. Our own close is not a drop."""
        self.live_pumping = True
        try:
            while True:
                session = self.session
                last: live_base.LiveEvent | None = None
                async for event in session.events():
                    if session is not self.session:
                        continue          # superseded: it no longer speaks for the conversation
                    last = event
                    try:
                        await self.handle_live_event(event)
                    except Exception as exc:
                        if self.reconnect is None or not _wire_died(exc):
                            raise
                        # The socket died under a word we were sending (a receipt): that is
                        # the drop, found by the writer before the reader.
                        last = live_base.LiveEvent(seq=event.seq, kind=live_base.KIND_DISCONNECTED,
                                                   generation=event.generation)
                        break
                if session is not self.session:
                    continue              # a relay adopted a new session: pump that one
                self.leg_ended.set()
                if self.reconnect is None or not _dropped(last):
                    return
                self.live_pumping = False
                self.hold()
                if not await self.reconnect("reconnect", session, not self._leg_stirred):
                    return
                self.live_pumping = True
        finally:
            self.live_pumping = False
            self._live_ended.set()
            self.leg_ended.set()
            self._swapped.set()

    async def _pump_backend(self) -> None:
        async for obs in self.backend.observe():
            await self._voice_ready.wait()        # a relay in progress: wait for the new session
            self._obs_idle.clear()
            try:
                session = self.session
                self._swapped.clear()
                try:
                    await self.handle_observation(obs)
                except Exception as exc:
                    if self.reconnect is None or not _wire_died(exc):
                        raise
                    # The provider socket died under this word. The live side will find the
                    # drop and relay; the word is said again on the session that replaces it.
                    while self.session is session and not self._live_ended.is_set():
                        await self._swapped.wait()
                        self._swapped.clear()
                    if self._live_ended.is_set():
                        return
                    await self._voice_ready.wait()
                    await self.handle_observation(obs)
            finally:
                self._obs_idle.set()


def _dropped(last: live_base.LiveEvent | None) -> bool:
    """The stream ended by itself: the socket died, or the PROVIDER closed the session (its
    `closed` carries a reason; ours carries none)."""
    if last is None:
        return False
    return (last.kind == live_base.KIND_DISCONNECTED
            or (last.kind == live_base.KIND_CLOSED and "reason" in (last.payload or {})))


def _wire_died(exc: BaseException) -> bool:
    """A send failed because the provider connection is gone (an OS-level connection error,
    or the websocket library's `ConnectionClosed*`) — not a bug to hide."""
    return (isinstance(exc, (ConnectionError, OSError))
            or type(exc).__name__.startswith("ConnectionClosed"))


def _in_flight(requests: Any) -> str:
    return IN_FLIGHT + "; ".join(
        f"{r.request_id}「{_clip(voice_config.redact_text(r.interpretation), RECAP_TURN_BYTES)}」"
        for r in requests)


def _utf8(text: str) -> int:
    return len(text.encode("utf-8"))


def _clip(text: str, budget: int) -> str:
    """At most `budget` UTF-8 bytes, cut on a code point, marked when cut."""
    if _utf8(text) <= budget:
        return text
    room = max(budget - _utf8("…"), 0)
    return text.encode("utf-8")[:room].decode("utf-8", errors="ignore") + "…"


def _system_text(body: dict[str, Any]) -> str:
    """A STRUCTURED body (an effect tombstone) as the text of a system message.
    Backend WORDS never come through here -- they go through `_backend_text` as prose."""
    import json
    return json.dumps(body, ensure_ascii=False)


def _receipt_words(outcome: str, reason: str | None) -> str:
    """The one sentence a receipt carries. Delivery is a fact about the wire; without words
    the model reads a JSON dict as "no result" (measured 2026-09-20). How to SAY it is the
    prompt's business; this only states what happened."""
    if outcome == "posted":
        return "已送到 backend。这不是结果;结果会以「[后台]」消息回来。"
    if outcome == "refused":
        return f"没送出去:{reason or 'backend_refused'}。"
    return "不确定送没送到。"


RESULT_PREFIX = "[后台]"
DIALOG_WAITING = "backend 在终端里等你确认,语音这边不能替你按键;请到终端回答:"
PERMISSION_ASKED = "backend 发起了权限请求,语音这边不能替你答应:"
PROGRESS_PREFIX = "[后台·进行中]"
TYPED_PREFIX = "[终端·你打的]"
# The voice's own state, as words the model reacts to in its own words.
VOICE_PREFIX = "[语音]"
OPENED = "已就绪"
CLOSING = "要关了"
# A relay's recap: the voice connection was renewed, the backend session was not.
RELAYED = "语音连接刚换了新的,后台会话没变。以下是刚才的对话,只作参考,别复述,接着聊:"
RELAYED_LATE = "换连接时又说了这些(接在上面之后):"
IN_FLIGHT = "还在后台跑的请求(结果会以「[后台]」回来): "
OPERATOR_SPEAKER = "操作者"
VOICE_SPEAKER = "语音"
BACKEND_SPEAKER = "后台"


def _backend_text(prefix: str, label: str, text: str) -> str:
    """Words from the terminal, as words: a prefix says where they came from, an optional label
    says which request they answer, and the text is verbatim. Nothing here summarises for the
    model -- the same shape as Codex's `[BACKEND]` prefix."""
    head = f"{prefix} {label}".strip()
    return f"{head}\n{text}"
