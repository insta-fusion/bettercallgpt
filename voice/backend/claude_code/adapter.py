"""The Claude Code adapter — one harness, seen through the `Backend` protocol.

This is the translator. The pane says what is drawn, the tailer says what the transcript
records, the relay carries words to the socket; this file turns those three into the six
observation kinds the loop understands, and turns the loop's three actuations back into them.

It decides no meaning. The words it sends are the operator's, the dialog it reports is the
harness's, the occurrence id is minted from what was seen on screen. Every judgement about what
a sentence MEANS was removed by the separation (the backend split (DESIGN.md §Backend split)) and now lives either in the
conversation model or nowhere:

* **No channel table.** `[STATUS]` / `[COMPLETE]` / `[ATTENTION]` decided whether a line was
  progress or an answer. Gone. The adapter emits what the tailer's own structure says: mid-turn
  text is `progress`, and a turn that ENDED carries a `result`. Structural, not semantic.
* **No `Q:` extraction.** Result production used to mint a question object that rewrote the
  operator's next sentence. Gone with the whole chain. A `Q:` line is just text.
* **No merged state word.** The old code overrode the reported activity to `dialog` for routing.
  Here activity and dialog are two independent facts, reported separately, because merging them
  is what let a routing decision hide inside an observation.
* **No queue-release policy and no Escape effect.** A `next` request is the loop's durable queue,
  and a stop is a `now` send whose frame carries `priority: "now"`.

Owner loss is the one terminal fact: once the pane reports it, every later actuation is refused.
Nothing reopens it but a new binding.

Design: voice/DESIGN.md §Backend split, §Acceptance E1–E6.
"""
from __future__ import annotations

import asyncio

import hashlib
import os
import uuid
from typing import Any, AsyncIterator, Callable

from .. import base
from . import pane as pane_mod

# Tailer event kind → the receipt disposition the loop reads. Relabelling, never meaning: each
# word on the right is the same fact the tailer already proved, said in the protocol's vocabulary.
_RECEIPT_TRANSITIONS = {
    "consumed": "consumed",
    "absorbed": "absorbed",
    "queued": "queued",
    "withdrawn": "withdrawn",
}

# Which receipt dispositions release the relay's per-tag outbox slot. A transport fact: the frame
# either reached the conversation or provably never started.
_SLOT_CONSUMING = frozenset({"consumed", "absorbed"})

# The one pane class that is a dialog. Kept as a name rather than inlined so the adapter and the
# pane cannot drift on what counts.
_DIALOG_CLASS = pane_mod.CLS_DIALOG

# The wire tag shape Claude Code's transcript carries back as a receipt.
_TAG_TEMPLATE = "⟨v#{}⟩"


def render_tag(token: str) -> str:
    """The `⟨v#…⟩` wire tag. Lives here, not in the ledger: it is a Claude Code protocol detail,
    and a different harness tags differently or not at all."""
    return _TAG_TEMPLATE.format(token)


def mint_tag() -> str:
    return render_tag(uuid.uuid4().hex[:16])




class ClaudeCodeBackend(base.Backend):
    """`voice.backend.base.Backend` over one bound Claude Code session.

    The three components are injected, not constructed here, so a test drives the adapter with
    fakes and the daemon drives it with the real pane, tailer and socket.
    """

    def __init__(self, *, pane: Any, tailer: Any, relay: Any, binding: dict,
                 mint: Callable[[], str] | None = None,
                 on_qualified: Callable[[], None] | None = None,
                 wake: Callable[[], Any] | None = None) -> None:
        self._pane = pane
        self._tailer = tailer
        # Awaited after a quiet poll; resolves when the transcript changed. Event-driven,
        # no interval. Without it the stream ENDED on the first quiet poll -- measured
        # 2026-09-20: every run's backend ear died right after the startup replay, so no
        # result ever reached the voice while the answer sat in the pane.
        self._wake = wake
        self._relay = relay
        self._binding = dict(binding or {})
        self._mint = mint or mint_tag
        self._on_qualified = on_qualified
        # The self-test frame's tag, until its receipt arrives and qualifies the relay.
        self._probe_tag: str | None = None

        self._owner_lost: str | None = None
        self._activity: str = "unknown"
        # One pane read at a time: the observation stream and `refresh` both read it.
        self._pane_lock = asyncio.Lock()
        self._turn_id: str | None = None
        self._dialog: base.Dialog | None = None
        self._dialog_present = False
        # tag → the turn that consumed it, filled by a receipt. This is how a dispatch learns
        # which turn owns it, and it is the only attribution the adapter performs.
        self._tags: dict[str, str] = {}
        # wire tag (⟨v#…⟩) -> the request id the loop knows. The transcript speaks in wire
        # tags; the loop speaks in request ids; this is the only place both are known.
        self._request_of_wire: dict[str, str] = {}
        self._pending: list[base.Observation] = []
        # Set when `refresh` queued something: the stream may be parked on the transcript
        # wake, and a dialog found by the pane read must still reach the loop.
        self._nudge = asyncio.Event()

    # ------------------------------------------------------------------ state

    async def state(self) -> base.BackendState:
        """Two independent facts, never one merged word.

        Activity comes from the tailer and dialog from the pane. The old code overrode activity
        to `dialog` so a router could switch on one value; that override is exactly how a routing
        decision hid inside an observation, so the two stay separate here.
        """
        activity = self._tailer_activity() or self._activity
        return base.BackendState(
            activity=activity if activity in ("idle", "working", "unknown") else "unknown",
            turn_id=self._turn_id,
            dialog=self._dialog,
            owner_lost=self._owner_lost,
        )

    def _tailer_activity(self) -> str:
        """The reducer's own word for activity. It is rebuilt on bootstrap and on rotation,
        where no event re-announces it, so it outranks the copy the events maintain — when
        it has a word, and that word becomes the copy. `unknown` (a half-written record, a
        failed read) says nothing about the turn, so the last definite word stands."""
        activity = str((getattr(self._tailer, "state", None) or {}).get("activity") or "")
        if activity in ("idle", "working"):
            self._activity = activity
            return activity
        return ""

    async def refresh(self) -> base.BackendState:
        """`state` after one fresh pane read, for a caller about to act on "still working":
        an owner that died mid-turn writes nothing to the transcript, so only the pane can
        tell. The read is the pane's own bounded one."""
        await self._poll_pane()
        if self._pending:
            self._nudge.set()
        return await self.state()

    # ------------------------------------------------------------------ observation

    def observe(self) -> AsyncIterator[base.Observation]:
        """Everything the harness told us since the last drain, as protocol observations."""
        async def stream():
            while True:
                batch, self._pending = self._pending, []
                for obs in batch:
                    yield obs
                if not await self._advance():
                    if self._wake is None:
                        break
                    await self._wait_wake()
                    continue
                batch, self._pending = self._pending, []
                for obs in batch:
                    yield obs
        return stream()

    async def _advance(self) -> bool:
        """One poll of both sources. False when the tailer says there is nothing more."""
        events = await self._poll_tailer()
        for event in events:
            self.ingest_transcript_event(event)
        await self._poll_pane()
        return bool(events)

    async def _poll_tailer(self) -> list[dict]:
        poll = getattr(self._tailer, "poll", None)
        if poll is None:
            return []
        result = poll()
        if hasattr(result, "__await__"):
            result = await result
        return list(result or ())

    async def _wait_wake(self) -> None:
        """The transcript changed, or a fresh read queued an observation to deliver."""
        if not self._nudge.is_set():
            wake = asyncio.ensure_future(self._wake())
            nudge = asyncio.ensure_future(self._nudge.wait())
            try:
                await asyncio.wait({wake, nudge}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                wake.cancel()
                nudge.cancel()
                # Let the wake's own cleanup finish HERE, before the next poll, so it cannot
                # drain a notification that belongs to the next wait.
                woke, _ = await asyncio.gather(wake, nudge, return_exceptions=True)
            if isinstance(woke, Exception):
                raise woke                      # a failing wake still ends the stream
        self._nudge.clear()

    async def _poll_pane(self) -> None:
        observe = getattr(self._pane, "observe", None)
        if observe is None:
            return
        pending_tool_ids = frozenset(
            (getattr(self._tailer, "state", {}) or {}).get("pending_tool_ids") or ())
        async with self._pane_lock:
            result = await observe(binding=self._binding, pending_tool_ids=pending_tool_ids)
            self.ingest_pane_observation(result)

    # -- the two translators, called directly by tests ----------------------

    def ingest_transcript_event(self, event: dict) -> None:
        """One tailer event → zero or more protocol observations."""
        kind = str(event.get("kind") or "")

        if kind in _RECEIPT_TRANSITIONS:
            tag = str(event.get("tag") or "")
            turn_id = event.get("turn_id") or self._turn_id
            disposition = _RECEIPT_TRANSITIONS[kind]
            if tag and tag == self._probe_tag:
                # The self-test came back stamped with OUR pid (the tailer registered it
                # strict-peer): the socket provably leads to the bound session. Only now may
                # `promote` authorize an operator send.
                self._probe_tag = None
                mark = getattr(self._relay, "mark_qualified", None)
                if mark is not None:
                    mark(True)
                if self._on_qualified is not None:
                    self._on_qualified()
            if tag and turn_id:
                # THE TAG IS THE ATTRIBUTION. A receipt names the turn that actually consumed the
                # words, which is what lets a result arriving much later be tied back to the
                # request that caused it.
                self._tags[tag] = str(turn_id)
            if disposition in _SLOT_CONSUMING and tag:
                self._relay.resolve(tag, how="consumed")
            # The loop keys requests by ITS ids, never by wire tags: hand it the request id
            # when this wire tag was minted by `send`; a tag we did not mint (the probe, an
            # earlier run's frame) passes through unmapped and the loop ignores it. Measured
            # live 2026-09-20: with wire tags reaching the loop, no result was ever tied to a
            # request and the voice answered "还没有新的结果" while the answer sat in the pane.
            self._emit(base.Observation(kind=base.OBS_RECEIPT, turn_id=turn_id,
                                        tag=self._request_of_wire.get(tag, tag),
                                        payload={"disposition": disposition}))
            return

        if kind == "assistant_text":
            self._activity = "working"
            self._turn_id = event.get("turn_id") or self._turn_id
            # Whole text, unmodified. No prefix decides whether this is worth hearing.
            self._emit(base.Observation(kind=base.OBS_PROGRESS, turn_id=self._turn_id,
                                        text=str(event.get("text") or "")))
            return

        if kind == "turn_result":
            turn_id = event.get("turn_id") or self._turn_id
            text = str(event.get("text") or "")
            self._activity = "idle"
            self._turn_id = None
            self._emit(base.Observation(
                kind=base.OBS_RESULT, turn_id=turn_id, text=text,
                # Identity so a replay cannot mint the same result twice.
                payload={"result_id": result_id_for(turn_id, text),
                         "inputs": tuple(event.get("inputs") or ())}))
            self._emit(base.Observation(kind=base.OBS_STATE, turn_id=None,
                                        payload={"activity": "idle"}))
            return

        if kind in ("turn_opened", "turn_steered"):
            self._activity = "working"
            self._turn_id = event.get("turn_id") or self._turn_id
            self._emit(base.Observation(kind=base.OBS_STATE, turn_id=self._turn_id,
                                        payload={"activity": "working"}))
            typed = str(event.get("text") or "")
            if typed.strip():
                # A line with none of our tags: the operator typed it at the keyboard. The
                # voice sees it as context, exactly as it sees the backend's own words.
                self._emit(base.Observation(kind=base.OBS_TYPED, turn_id=self._turn_id,
                                            text=typed))
            return

        if kind == "turn_interrupted":
            self._activity = "idle"
            self._turn_id = None
            self._emit(base.Observation(kind=base.OBS_STATE, turn_id=None,
                                        payload={"activity": "idle",
                                                 "interrupted": True}))
            return

        # `tag_seen_unresolved`, `queued_foreign`, `queue_bookkeeping`: the tailer saw something
        # it will not credit. Nothing downstream may act on it, so nothing is emitted.

    def ingest_pane_observation(self, result: dict | None) -> None:
        """One pane observation → owner-loss and dialog transitions."""
        if not isinstance(result, dict):
            return

        lost = result.get("lost")
        if lost and self._owner_lost is None:
            # TERMINAL. Reported once; every later actuation refuses on it.
            self._owner_lost = str(lost)
            self._emit(base.Observation(kind=base.OBS_OWNER_LOST, text=str(lost),
                                        payload={"reason": str(lost)}))

        if not result.get("ok"):
            return

        classification = result.get("classification")
        if not isinstance(classification, dict):
            return

        record = classification.get("dialog")
        if record is None:
            if self._dialog_present and self._dialog is not None:
                closed = self._dialog
                self._dialog_present = False
                self._dialog = None
                self._emit(base.Observation(kind=base.OBS_DIALOG, dialog=closed,
                                            payload={"transition": "closed"}))
            return

        dialog = self._dialog_from(classification)
        if dialog is None:
            # The pane drew something it could not name. The adapter refuses to describe it
            # rather than describing it wrongly; the operator answers at the terminal.
            return
        previous = self._dialog
        self._dialog = dialog
        self._dialog_present = True
        if previous is None:
            transition = "open"
        elif previous.occurrence_id == dialog.occurrence_id:
            return          # same dialog still up: nothing changed, say nothing
        else:
            transition = "replaced"
        self._emit(base.Observation(kind=base.OBS_DIALOG, dialog=dialog,
                                    payload={"transition": transition}))

    def _dialog_from(self, classification: dict) -> base.Dialog | None:
        """A `Dialog` carrying only what the screen actually showed.

        `action` and `scope` are ALWAYS None. They used to be parsed out of the prompt by English
        regexes, which made this layer decide what a dialog MEANS — the one thing it must not do.
        The broker composes its challenge from the verbatim prompt and the rendered options
        instead, and the confirming phrase names an option INDEX, so nothing downstream needs a
        word understood.

        An option list we cannot enumerate structurally yields no dialog at all, and the broker
        therefore cannot arm: the operator answers at the terminal. The one exception is a wait
        Orca itself reported with nothing enumerable on screen (`agent_wait` on the record): it
        carries NO options by construction, so the operator hears that the agent is waiting and
        the broker refuses to arm it (fewer than two options).
        """
        record = classification.get("dialog") or {}
        if str(classification.get("class") or "") != _DIALOG_CLASS:
            return None
        options = tuple((str(index), str(text)) for index, text in (record.get("options") or ()))
        if record.get("agent_wait"):
            if options:
                return None
        elif len(options) < 2:
            return None
        prompt = str(record.get("question") or "")
        if not prompt:
            return None
        # OCCURRENCE IS PART OF THE ID. The same wording drawn again after the dialog went away
        # is a different question, and an approval for the earlier one must not answer it.
        occurrence_id = f"{record.get('hash')}:{record.get('occurrence')}"
        return base.Dialog(occurrence_id=occurrence_id, kind="dialog", prompt=prompt,
                           action=None, scope=None, options=options)


    def _emit(self, obs: base.Observation) -> None:
        self._pending.append(obs)

    def drain(self) -> list[base.Observation]:
        """Everything queued, for a caller driving the adapter step by step."""
        out, self._pending = self._pending, []
        return out

    # ------------------------------------------------------------------ actuation

    # ------------------------------------------------------------------ qualification

    PROBE_TEXT = ("voice relay self-test — no action needed. A queued line ending in a "
                  "⟨v#…⟩ tag is the operator speaking (see your CLAUDE.md / AGENTS.md).")

    async def qualify(self) -> bool:
        """Post the relay's one self-test frame.

        `promote` refuses every operator send until the transcript carries this tag back
        stamped with OUR pid: the receipt, not the post, proves the socket leads to the bound
        session. Ported from the old daemon's `_qualify_relay` — the first port dropped it,
        and every real send was refused `relay_not_qualified`. Returns "" when the frame was
        posted, else the relay's refusal reason — a bare failure flag left the operator with
        `degraded:relay-probe-failed` and no way to learn why (measured live). Qualification
        itself flips in `ingest_transcript_event`.
        """
        if self._owner_lost is not None:
            return f"owner_lost:{self._owner_lost}"
        tag = self._mint()
        text = self.PROBE_TEXT
        register = getattr(self._tailer, "register", None)
        if register is not None:
            # STRICT: an unstamped carrier must not credit the probe. It exists to prove
            # positive identity, and an unstamped credit would spend its slot on nothing.
            register(tag, f"{text} {tag}", producer_pid=os.getpid(), strict_peer=True)
        self._probe_tag = tag
        instruction = {"instruction_id": f"probe-{uuid.uuid4().hex[:8]}", "tag": tag,
                       "text": text, "interrupt": False}
        authorization = await self._relay.promote(instruction, probe=True)
        if not authorization.get("ok"):
            self._probe_tag = None
            return str(authorization.get("refused") or "promote_refused")
        outcome = await self._relay.actuate(authorization)
        if not outcome.get("ok"):
            self._probe_tag = None
            detail = outcome.get("detail")
            reason = str(outcome.get("refused") or "relay_refused")
            return f"{reason}:{detail}" if detail else reason
        return ""

    async def send(self, text: str, *, tag: str, priority: str,
                   transcript: str, interpretation: str) -> base.Receipt:
        """Hand the operator's words to the harness.

        `priority: "now"` puts the field on the relay frame, which is what makes the receiver
        abort its running turn. This layer carries the field; the loop decided it.
        """
        if self._owner_lost is not None:
            return base.Receipt(outcome="refused", reason=f"owner_lost:{self._owner_lost}",
                                tag=tag)
        wire_tag = tag if str(tag).startswith("⟨v#") else self._mint()
        self._request_of_wire[wire_tag] = str(tag)
        register = getattr(self._tailer, "register", None)
        if register is not None:
            # What the daemon is about to post under this tag, so the tailer can judge the
            # carrier by equality and attribute it to our pid. Only the probe was registered
            # before; operator frames went out unregistered (measured 2026-09-21).
            register(wire_tag, f"{text} {wire_tag}", producer_pid=os.getpid())
        instruction = {"instruction_id": str(tag), "tag": wire_tag, "text": text,
                       "interrupt": priority == "now"}
        authorization = await self._relay.promote(instruction)
        if not authorization.get("ok"):
            return base.Receipt(outcome="refused",
                                reason=str(authorization.get("refused") or "promote_refused"),
                                tag=wire_tag)
        outcome = await self._relay.actuate(authorization)
        if not outcome.get("ok"):
            # A refusal that is PROVEN never-started is a refusal. Anything else began a write
            # and is uncertain forever: the receiver may have acted on it, so it is never resent.
            if outcome.get("never_started"):
                return base.Receipt(outcome="refused",
                                    reason=str(outcome.get("refused") or "relay_refused"),
                                    tag=wire_tag)
            return base.Receipt(outcome="uncertain",
                                reason=str(outcome.get("refused") or "write_started"),
                                tag=wire_tag)
        if outcome.get("state") == "post_unknown":
            return base.Receipt(outcome="uncertain", reason="post_unknown", tag=wire_tag)
        # `posted` means the bytes left this process, not that Claude read them. The transcript
        # receipt is what proves consumption, and it arrives later as an observation.
        return base.Receipt(outcome="posted", tag=wire_tag)

    async def answer(self, occurrence_id: str, choice: str) -> base.Receipt:
        """Press a dialog answer, only if THAT occurrence is still the one on screen."""
        if self._owner_lost is not None:
            return base.Receipt(outcome="refused", reason=f"owner_lost:{self._owner_lost}")
        current = self._dialog
        if current is None:
            return base.Receipt(outcome="refused", reason="no_dialog")
        if current.occurrence_id != occurrence_id:
            # The dialog on screen is not the one that was armed. Pressing anyway is the stale
            # authorization bug: the operator approved a question that is no longer being asked.
            return base.Receipt(outcome="refused", reason="occurrence_changed")
        key = self._key_for(current, choice)
        if key is None:
            return base.Receipt(outcome="refused", reason="choice_not_on_screen")
        press = getattr(self._pane, "press", None)
        if press is None:
            # No keyboard surface on this binding: the dialog stays on screen and the operator
            # answers it at the terminal. Refusing is the honest outcome, not an error.
            return base.Receipt(outcome="refused", reason="terminal_only")
        result = await press(occurrence_id=occurrence_id, key=key)
        if isinstance(result, dict) and result.get("ok"):
            return base.Receipt(outcome="applied")
        reason = (result or {}).get("refused") if isinstance(result, dict) else "press_failed"
        return base.Receipt(outcome="refused", reason=str(reason or "press_failed"))

    @staticmethod
    def _key_for(dialog: base.Dialog, choice: str) -> str | None:
        """The option key to press. `choice` IS the index, as rendered on this occurrence.

        Nothing here reads a label. The previous version mapped `allow` and `deny` onto the
        lowest-numbered option whose text began "yes" or "no", which meant a reworded or
        reordered option list silently changed which key an approval landed on — and on the
        measured wording, option 2 ("Yes, and don't ask again") grants strictly more than was
        asked. The broker now names the index in the phrase the operator speaks, so the index is
        what arrives here and the only question is whether this occurrence still renders it.
        """
        for key, _label in dialog.options or ():
            if key == choice:
                return key
        return None


    async def cancel(self, turn_id: str) -> base.Receipt:
        """Refused, always, with the reason.

        There is no Escape effect here. A stop is a `request(now)`: the relay frame's
        `priority: "now"` aborts the running turn AND hands the operator's words to the agent, so
        it both interrupts and says why. A bare keystroke would do the first without the second.
        """
        return base.Receipt(outcome="refused",
                            reason="cancel_is_a_now_send", tag=None)




def result_id_for(turn_id: str | None, text: str) -> str:
    """A stable identity for one result, so a replay cannot deliver it twice."""
    digest = hashlib.sha256(f"{turn_id or ''}\x00{text}".encode("utf-8")).hexdigest()
    return digest[:16]
