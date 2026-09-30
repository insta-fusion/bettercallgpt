"""The transcript — the ONLY place the listener learns what Claude is doing, and the
only place an instruction is proven RECEIVED or CONSUMED.

Bucket (a) only: observation. This module reads a JSONL transcript and reports what
STRUCTURALLY happened — a turn opened, a turn ended, an item entered the harness's queue,
a registered wire fragment came back. It never decides what a sentence MEANS.

THE ONE CUT against the module this was read from: `_reduce_assistant` used to label
mid-turn assistant text as an event kind `progress` carrying `tool_preamble=True`, and
minted the end-of-turn event as `result`. That labeling answered "is this line an answer?",
which is presentation classification and belongs to the speaking layer. Here the reducer
emits `assistant_text{turn_id, text}` for mid-turn text and `turn_result{turn_id, inputs,
text}` when the turn ends, and carries no `tool_preamble` field at all. The surviving
distinction is STRUCTURAL — did the turn end — never semantic.

WHY A DEDICATED TAILER (do not "reuse" a tail-window reader):
  * A fixed-window reader from the END of the file answers "what was said lately" for a
    voice prompt. It drops unparseable lines silently and keeps NO state. A listener that
    trusted it would read a torn tail as "Claude said nothing / Claude is idle" — the exact
    failure this module exists to prevent.
  * A headless job's stdout stream has a transport that guarantees one reply per request.
    Here the operator's own pane is the transport: the harness may enqueue, withdraw,
    reorder and absorb entries, and FIFO position proves nothing at all.

THE MEASURED MODEL (Claude Code 2.1.260, records observed live on macOS; the recorded
receipts are the transcript fixtures under tests/):

  queue-operation enqueue {content}  the harness ACCEPTED text into its queue.
                                     A tag found in `content` proves RECEIVED.
  queue-operation dequeue            carries NO content. It is FIFO bookkeeping and
                                     proves NOTHING about WHICH item was consumed.
                                     Each one is its own bookkeeping event; collapsing
                                     them would invent a receipt out of ordering.
  queue-operation remove {content}   the HARNESS withdrew the item (observed with
                                     reason `absorbed_mid_turn`). Not a consumption.
  user, message.content is a str     the entry entered the conversation: this is the
                                     receipt. A tag found here proves CONSUMED. An
                                     idle one OPENS a turn; one arriving mid-turn is a
                                     STEER that joins the active turn.
  user, message.content is a list    tool_result / interruption plumbing. NEVER a
                                     receipt, NEVER a turn opener. An interruption
                                     marker (`[Request interrupted by user...]`, which
                                     also carries `interruptedMessageId`) CLOSES the
                                     active turn without inventing a result.
  assistant stop_reason=tool_use     WORKING. `tool_use` blocks open unresolved tool
                                     ids; the matching `tool_result` blocks close them.
  assistant stop_reason=end_turn     the ONLY thing that ends a turn.

CURSOR / STATE SPLIT (contract): the Tailer owns file cursors and partial bytes and
nothing else. `reduce_transcript` is pure — (state, record) -> (state, events) — so the
same byte stream always yields the same events, and a bootstrap replay can be scored
against a live tail.

HONEST UNCERTAINTY: a truncated or rotated file whose last complete turn boundary is
not found within the scan budget leaves queue balance and pending work at the literal
"unknown". "unknown" is never rendered as "idle" — an unreadable tail must read as
"go look at the pane", never as "Claude is free".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "UNKNOWN",
    "Tailer",
    "initial_state",
    "reduce_transcript",
    "record_session_id",
]

# The literal the contract requires wherever a count cannot be honestly stated.
UNKNOWN = "unknown"


class _HistoryReplaced(Exception):
    """A4. The bytes behind the cursor are not the bytes we read. Raised inside the poll
    read so the rebuild runs outside the open file, on the same path a rotation takes."""


class _BootstrapUnproven(Exception):
    """A replay whose bytes cannot be proven to be the file we measured.

    Private and caught immediately: it exists so the two fail-closed conditions
    (descriptor identity, exact read) reach the SAME park-to-UNKNOWN block a failed read
    already reaches, instead of growing a second, drifting copy of it.
    """


# Claude Code writes these two markers as the text of a LIST-content user entry when
# the operator interrupts; both were observed in local transcripts, alongside an
# `interruptedMessageId` field. Matched as a prefix so a later suffix still counts.
_INTERRUPT_PREFIX = "[Request interrupted by user"

# A wire tag minted by the ledger: ⟨v#<uuidhex>⟩. Recognized here so a receipt can be
# attributed to the instruction that asked for it.
_TAG_RE = re.compile(r"⟨v#([0-9a-f]{8,32})⟩")


# ---------------------------------------------------------------- record helpers

def record_session_id(record: dict) -> str:
    """The session a record belongs to, or "" when the record does not say.

    Measured: every record type except `file-history-snapshot` carries `sessionId`;
    `assistant`/`user`/`system` additionally carry a `session_id` alias. A record that
    names NEITHER is un-attributable, not foreign — callers keep it, because dropping
    unlabeled records would silently discard state.
    """
    for key in ("sessionId", "session_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _message(record: dict) -> dict:
    message = record.get("message")
    return message if isinstance(message, dict) else {}


def _text_blocks(content: list) -> str:
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
    return "\n".join(out)


def _carried_text(content: list) -> str:
    """Every string a list-content record carries: text blocks AND tool_result payloads
    (a tool result quoting our line is a carrier of the tag, not a receipt — N3)."""
    parts = [_text_blocks(content)]
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                parts.append(_text_blocks(inner))
    return "\n".join(part for part in parts if part)


def _strings_in(value: Any, depth: int = 0) -> list[str]:
    """Every string inside a JSON-ish value (a tool_use input), bounded in depth."""
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _strings_in(v, depth + 1)]
    if isinstance(value, list):
        return [t for v in value for t in _strings_in(v, depth + 1)]
    return []


def _assistant_carried_text(blocks: list) -> str:
    """What an assistant record carries: its text blocks AND the strings inside its
    tool_use inputs (a Bash command quoting our line is a carrier, never a receipt — R4-1)."""
    parts = [_text_blocks(blocks)]
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            parts.extend(_strings_in(block.get("input")))
    return "\n".join(part for part in parts if part)


def _is_interruption(record: dict, content: list) -> bool:
    if record.get("interruptedMessageId"):
        return True
    return _text_blocks(content).lstrip().startswith(_INTERRUPT_PREFIX)


def tags_in(text: str) -> tuple[str, ...]:
    """Every wire tag appearing in a blob of transcript text, in order of appearance."""
    seen: list[str] = []
    for match in _TAG_RE.finditer(text or ""):
        tag = match.group(0)
        if tag not in seen:
            seen.append(tag)
    return tuple(seen)


def _bump(state: dict, **changes: Any) -> dict:
    new = dict(state)
    new.update(changes)
    new["revision"] = state["revision"] + 1
    return new


def _event(kind: str, state: dict, **payload: Any) -> dict:
    return {"kind": kind, "revision": state["revision"], "at": state["observed_at"], **payload}


# ---------------------------------------------------------------- the pure reducer

def initial_state(*, queue_balance: int | str = 0, observed_at: float = 0.0) -> dict:
    """A fresh reducer state.

    `queue_balance` is an int when the history is trustworthy, or UNKNOWN when a
    bootstrap could not find a turn boundary within its budget.
    """
    return {
        "revision": 0,
        "turn_id": None,           # uuid of the user entry that opened the active turn
        "turn_inputs": (),         # every human entry (opener + steers) in that turn
        "pending_tool_ids": frozenset(),
        "queue_balance": queue_balance,
        "queued_tags": () if queue_balance == UNKNOWN else (),
        "activity": "unknown" if queue_balance == UNKNOWN else "idle",
        "observed_at": observed_at,
        "last_record_at": None,    # transcript timestamp of the newest applied record
    }


# Record types that participate in the conversation's PARENT CHAIN. Every one of them
# carries `parentUuid`, and only the session's very first such record carries it as null
# (measured over 40 local transcripts: 40/40 whole files begin with a parentless lineage
# record, 39/40 mid-file suffixes do not). The metadata types — `queue-operation`, `mode`,
# `last-prompt`, `ai-title`, `permission-mode` — carry no `parentUuid` at all and appear
# freely mid-file, so their absence proves nothing and they are deliberately NOT in this
# set.
_LINEAGE_TYPES = ("user", "assistant", "attachment", "system")


def _sha1_prefix(fh, length: int) -> tuple[int, str]:
    """(bytes actually read, sha1) of the first `length` bytes from the current position,
    in bounded blocks. The witness used to be `sha1(fh.read(length))` -- one bytes object
    the size of the transcript, TWICE per poll. On a 123 MB transcript, woken on every
    write, that was ~250 MB of transient allocation per poll and the host killed the
    daemon for memory (2026-09-20). Same digest, same guarantee, constant memory."""
    digest = hashlib.sha1()
    remaining = length
    seen = 0
    while remaining > 0:
        block = fh.read(min(remaining, 1 << 20))
        if not block:
            break
        digest.update(block)
        seen += len(block)
        remaining -= len(block)
    return seen, digest.hexdigest()


def _is_session_origin(records: list[dict]) -> bool:
    """Does this replayed prefix BEGIN at the session's origin?

    `bootstrap()` derives its balance from the premise that byte 0 is the point where no
    turn is open and nothing is queued. For a whole file that premise is free. For a
    ROTATION it is not, and adopting it anyway is how the suffix counterexample turns a
    pending queue item into an empty queue.

    The evidence is the first lineage record: at the origin it declares no predecessor,
    and anywhere else it names the record before it. A prefix holding no lineage record
    at all (pure metadata) proves nothing either way and is refused — a rotation is
    exactly when "we cannot tell" must not read as "it's fine".

    KNOWN LOOSENESS, stated rather than discovered: a suffix that happens to begin with a
    parentless `system` record reads as an origin (1 of 40 measured). That fails toward
    ACCEPTING a rotation, so it restores the pre-repair behaviour for that one shape
    rather than introducing a new error.
    """
    for record in records:
        if record.get("type") in _LINEAGE_TYPES:
            return record.get("parentUuid") is None
    return False


def _unfence(state: dict) -> dict:
    """The narrow inverse of `_fence_unknown`, used ONLY where a NEW provable origin has
    replaced the history a fence was about.

    `_fence_unknown` is permanent by design: within one history, evidence that is gone
    does not come back. But a rotation to a file that provably starts at a session origin
    is not the same history — the fenced balance was a statement about a file that no
    longer exists, and carrying it forward latches the tailer into UNKNOWN for the rest of
    the call. Everything here is rebuilt from the new replay, so there is nothing to
    preserve: this returns the freshly derived state untouched and exists to NAME that
    decision at the one call site allowed to make it.
    """
    return state


def _fence_unknown(state: dict) -> dict:
    """Permanently fence a state whose evidence is gone (the file itself vanished).

    Fences ACTIVITY and QUEUE BALANCE — the two fields the promotion gate reads — while
    leaving turn and pending-tool bookkeeping intact. Use this only where the evidence
    is UNRECOVERABLE. A merely half-written record is fenced by the partial buffer
    instead, which un-fences itself the moment the record completes.
    """
    if state["activity"] == UNKNOWN and state["queue_balance"] == UNKNOWN:
        return state
    return _bump(state, activity=UNKNOWN, queue_balance=UNKNOWN)


def collapse(text: str) -> str:
    """Whitespace-collapsed text: the ONE normalisation the receipt equality uses."""
    return " ".join((text or "").split())


def reduce_transcript(state: dict, record: dict, *,
                      wires: dict | None = None,
                      producers: dict | None = None,
                      strict: set | frozenset | None = None) -> tuple[dict, tuple[dict, ...]]:
    """Apply ONE complete transcript record. Pure: no I/O, no clock, no mutation.

    Returns the new state and the events it produced. Unrecognized record types are a
    no-op that returns the SAME state object, so a transcript full of `mode` /
    `ai-title` / attachment chatter costs nothing and bumps no revision.

    `wires` maps a registered tag to the exact wire fragment the daemon typed
    (`"<text> <tag>"`, collapsed). For a REGISTERED tag a user string record is a
    `consumed` receipt ONLY when the whole record equals that fragment (steer plan §3,
    Astra R3 #4: `do not run the tests ⟨tag⟩` contains the fragment and must not credit
    it; merging is not measured, so there is no entry boundary to split on). Any other
    record carrying the tag — a containing string, a quotation in assistant text, a
    compaction summary — is `tag_seen_unresolved`: never credited, never re-sent.
    """
    kind = record.get("type")
    if kind == "queue-operation":
        return _reduce_queue(state, record)
    if kind == "user":
        return _reduce_user(state, record, wires=wires or {}, producers=producers or {},
                            strict=strict)
    if kind == "assistant":
        return _reduce_assistant(state, record, wires=wires or {})
    if kind == "attachment":
        return _reduce_attachment(state, record, wires=wires or {}, producers=producers or {},
                                  strict=strict)
    return state, ()


# Relay carriers — MEASURED on Claude Code 2.1.260 (tests/fixtures/relay-*.jsonl).
# A frame posted into the session's own inbox socket arrives in the transcript as:
#   idle hub    : queue-operation enqueue -> dequeue -> user (string content, origin.kind=peer,
#                 isMeta=true) whose content is `_PEER_WRAPPER + "\n" + <our exact line>`
#   busy hub    : queue-operation enqueue -> remove(reason=absorbed_mid_turn) ->
#                 attachment{type=queued_command, prompt=<our exact line>, origin.kind=peer}
# `origin.verifiedPeerPid` names the POSTING process; the daemon compares it with the
# producer it persisted (ARCH-DECISION §8 N1/N3).
_PEER_WRAPPER = "Another Claude session sent a message:"
ABSORBED_REASON = "absorbed_mid_turn"


def _peer_origin(record: dict) -> dict | None:
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "peer":
        return origin
    return None


_PEER_TRAILER = "This came from another Claude session"


def _peer_body(content: str) -> str:
    """The relayed payload inside the receiver's envelope, or the content unchanged.

    MEASURED envelope (2.1.260): `<wrapper line>\\n<payload>\\n\\n<trailer paragraph that
    starts with "This came from another Claude session …">`. ONLY the envelope is removed
    (Astra impl r1 #9): the wrapper line at the very start, and the trailer only when it
    is the LAST paragraph. Everything in between is the payload, verbatim — a payload with
    extra paragraphs simply fails the exact-equality receipt, and a payload that happens to
    mention the trailer's words is not truncated."""
    if not content.startswith(_PEER_WRAPPER):
        return content
    rest = content[len(_PEER_WRAPPER):]
    if rest[:1] not in ("\n", "\r"):
        return content                       # not the measured envelope: leave it alone
    rest = rest.lstrip("\r\n")
    paragraphs = rest.split("\n\n")
    if len(paragraphs) > 1 and paragraphs[-1].lstrip().startswith(_PEER_TRAILER):
        paragraphs = paragraphs[:-1]
    return "\n\n".join(paragraphs).rstrip()


def _peer_mismatch(tag: str, origin: dict | None, producers: dict,
                   strict: set | frozenset | None = None) -> str | None:
    """For a RELAY-registered tag (a producer pid is known), the carrier must not name a
    DIFFERENT peer pid. None when it names ours or names none at all; else the reason it
    is not credited. Decided BEFORE any `consumed` is emitted, so the tailer's
    once-per-tag dedup never spends the tag on a stranger's frame (Astra impl r1 #3).

    AN ABSENT STAMP IS NOT A WRONG STAMP. This used to read `peer is None or int(peer) !=
    int(expected)`, collapsing "the host did not stamp this frame" into "another process
    forged it". The host does not stamp every carrier: of the receipt-bearing carriers in
    the 65h journal, `queued_command` attachments came back both stamped (19) and unstamped
    (2), and the unstamped ones are indistinguishable from ours by every other measure. The
    cost of reading absence as guilt is not a missed receipt, it is a PERMANENT one: the
    relay actuator clears its single pending slot only on `consumed`/`never_started`, so
    one unstamped receipt locks the slot and every later send is refused for the rest of
    the call. Measured live: the last delivered instruction came back `absorbed` (Claude
    HAD it) and 2ms later `tag_seen_unresolved/peer_pid_mismatch` with `peer_pid: null`;
    the following eight sends all died `relay_pending_send` and the call never spoke to the
    agent again.

    What still rejects: a PRESENT stamp that disagrees (a real stranger), a carrier with no
    peer origin at all, and any text that is not the registered wire. The stamp was never
    the only proof — the tag is 122 bits of unguessable entropy, the whole line must match
    the registered wire exactly, the socket is reachable only by this account, and the
    relay's connect-time check fails CLOSED on an unknown peer.

    `strict` names tags for which absence STAYS fatal — the qualification probe, whose
    whole purpose is to prove positive identity. Crediting an unstamped probe would spend
    its once-per-tag dedup slot in `_filter`, fail the caller's `peer == os.getpid()`
    check, and leave the genuine stamped receipt with nowhere to land (Astra r-rootcause
    #5) — the same latch, moved into startup."""
    expected = producers.get(tag)
    if expected is None:
        return None
    if origin is None:
        return "peer_origin_missing"
    peer = origin.get("verifiedPeerPid")
    if peer is None:
        return "peer_pid_unstamped" if strict and tag in strict else None
    if int(peer) != int(expected):
        return "peer_pid_mismatch"
    return None


def _reduce_attachment(state: dict, record: dict, *, wires: dict | None = None,
                       producers: dict | None = None,
                       strict: set | frozenset | None = None) -> tuple[dict, tuple[dict, ...]]:
    """A `queued_command` attachment is the busy-hub receipt of a relayed frame: the
    harness absorbed the queued item into the running turn (the `remove` that precedes it
    said `absorbed_mid_turn`). Credited ONLY when the prompt equals a registered wire and
    the record carries the registered producer's peer origin; anything else is seen,
    never credited; a non-queued_command attachment is a no-op."""
    wires = wires or {}
    producers = producers or {}
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return state, ()
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str):
        return state, ()
    origin = _peer_origin(attachment) or _peer_origin(record)
    at = record.get("timestamp") or attachment.get("timestamp")
    tags = tags_in(prompt)
    if not tags:
        return state, ()
    new = _bump(state, activity="working",
                last_record_at=at or state["last_record_at"])
    events = []
    for tag in tags:
        wire = wires.get(tag)
        if wire is None:
            continue
        mismatch = _peer_mismatch(tag, origin, producers, strict)
        if origin is None or collapse(prompt) != wire or mismatch is not None:
            events.append(_event("tag_seen_unresolved", new, tag=tag,
                                 where=mismatch or "attachment",
                                 turn_id=new["turn_id"], text=prompt[:400]))
            continue
        events.append(_event("consumed", new, tag=tag, turn_id=new["turn_id"],
                             uuid=attachment.get("source_uuid"), steer=True, text=prompt,
                             attribution="steer", carrier="attachment",
                             peer_pid=origin.get("verifiedPeerPid")))
    return new, tuple(events)


def _reduce_queue(state: dict, record: dict) -> tuple[dict, tuple[dict, ...]]:
    operation = record.get("operation")
    content = record.get("content")
    content = content if isinstance(content, str) else None
    at = record.get("timestamp")

    if operation == "enqueue":
        # Tagged content is the ONLY thing that proves receipt. An untagged enqueue
        # (a task-notification, a queued human line we did not send) still moves the
        # balance — it is a real item Claude will have to eat before ours.
        tags = tags_in(content or "")
        balance = state["queue_balance"]
        balance = balance if balance == UNKNOWN else balance + 1
        new = _bump(
            state,
            queue_balance=balance,
            queued_tags=state["queued_tags"] + tags,
            last_record_at=at or state["last_record_at"],
        )
        events = [_event("queued", new, tag=tag, content=content) for tag in tags]
        if not tags:
            events = [_event("queued_foreign", new, content=content)]
        return new, tuple(events)

    if operation == "dequeue":
        # MEASURED: a dequeue carries no content. It says an item left the queue; it
        # does NOT say which. We move the balance and emit bookkeeping — one event per
        # dequeue, never collapsed — and we credit NO instruction. Consumption is
        # proven only by a tagged string-content `user` entry.
        balance = state["queue_balance"]
        if balance == UNKNOWN:
            new_balance: int | str = UNKNOWN
        else:
            new_balance = balance - 1
            if new_balance < 0:
                # More left than we ever saw arrive: our view of the queue began
                # mid-history. Refuse to invent a negative depth.
                new_balance = UNKNOWN
        new = _bump(state, queue_balance=new_balance, last_record_at=at or state["last_record_at"])
        return new, (_event("queue_bookkeeping", new, operation="dequeue"),)

    if operation == "remove":
        # The harness itself withdrew the item (observed reason: absorbed_mid_turn).
        # A withdrawn instruction of ours was never consumed and must not be reported
        # as delivered — it is surfaced so the caller can decide to re-ask.
        tags = tags_in(content or "")
        balance = state["queue_balance"]
        if balance == UNKNOWN:
            new_balance = UNKNOWN
        else:
            new_balance = balance - 1
            if new_balance < 0:
                new_balance = UNKNOWN
        remaining = tuple(t for t in state["queued_tags"] if t not in tags)
        new = _bump(
            state,
            queue_balance=new_balance,
            queued_tags=remaining,
            last_record_at=at or state["last_record_at"],
        )
        reason = record.get("reason")
        if reason == ABSORBED_REASON:
            # NOT a withdrawal: the harness pulled the item INTO the running turn. The
            # receipt that credits it is the `queued_command` attachment that follows
            # (relay carriers, above); this record only moves the balance.
            events = [_event("absorbed", new, tag=tag, content=content) for tag in tags]
        else:
            events = [
                _event("withdrawn", new, tag=tag, reason=reason, content=content)
                for tag in tags
            ]
        if not tags:
            events = [_event("queue_bookkeeping", new, operation="remove", reason=reason)]
        return new, tuple(events)

    return state, ()


def _reduce_user(state: dict, record: dict, *, wires: dict | None = None,
                 producers: dict | None = None,
                 strict: set | frozenset | None = None) -> tuple[dict, tuple[dict, ...]]:
    wires = wires or {}
    producers = producers or {}
    message = _message(record)
    content = message.get("content")
    at = record.get("timestamp")

    if isinstance(content, list):
        # NEVER a receipt and NEVER a turn opener: this is tool plumbing. A registered
        # tag inside its text blocks (a tool result quoting our line, a system note) is
        # seen-but-unresolved, never a consumption.
        carried = _carried_text(content)
        seen = tuple(t for t in tags_in(carried) if t in wires)
        interrupted = _is_interruption(record, content)
        notices = tuple(_event("tag_seen_unresolved", state, tag=t,
                               where="interruption" if interrupted else "user_list",
                               text=carried[:400]) for t in seen)
        if seen and not interrupted:
            new, more = _reduce_user_list(state, record, content, at)
            return new, notices + more
        if interrupted:
            if state["turn_id"] is None:
                return state, notices
            new = _bump(
                state,
                turn_id=None,
                turn_inputs=(),
                pending_tool_ids=frozenset(),
                activity="idle",
                last_record_at=at or state["last_record_at"],
            )
            # A closed-by-interruption turn produces NO result. Inventing one here
            # would report work that never finished.
            return new, notices + (_event("turn_interrupted", new, turn_id=state["turn_id"],
                                          inputs=state["turn_inputs"]),)
        return _reduce_user_list(state, record, content, at)

    if not isinstance(content, str):
        return state, ()

    # A STRING-content user entry is the receipt: this text entered the conversation.
    tags = tags_in(content)
    uuid = record.get("uuid")
    remaining = tuple(t for t in state["queued_tags"] if t not in tags)
    balance = state["queue_balance"]

    if state["turn_id"] is None:
        new = _bump(
            state,
            turn_id=uuid,
            turn_inputs=(uuid,),
            pending_tool_ids=frozenset(),
            queued_tags=remaining,
            queue_balance=balance,
            activity="working",
            last_record_at=at or state["last_record_at"],
        )
        opened = True
    else:
        # Mid-turn human text is a STEER: it belongs to the turn already running and
        # does not start a second one.
        new = _bump(
            state,
            turn_inputs=state["turn_inputs"] + (uuid,),
            queued_tags=remaining,
            activity="working",
            last_record_at=at or state["last_record_at"],
        )
        opened = False

    events = []
    # A relayed frame delivered to an IDLE hub is wrapped by the receiver (relay carriers,
    # above): equality is judged on the body inside the wrapper, and only for a record
    # that positively carries a peer origin.
    origin = _peer_origin(record)
    body = _peer_body(content) if origin is not None else content
    for tag in tags:
        wire = wires.get(tag)
        mismatch = _peer_mismatch(tag, origin, producers, strict) if wire is not None else None
        if wire is not None and (collapse(body) != wire or mismatch is not None):
            # The tag is ours but the record is not our line (a negation, a merge, an
            # edit, a compaction summary) — or not our producer's frame. Seen; not credited.
            events.append(_event("tag_seen_unresolved", new, tag=tag,
                                 where=mismatch or "user_string",
                                 turn_id=new["turn_id"], uuid=uuid, text=content[:400]))
            continue
        extra = ({"carrier": "peer_user", "peer_pid": origin.get("verifiedPeerPid")}
                 if origin is not None else {})
        events.append(_event("consumed", new, tag=tag, turn_id=new["turn_id"], uuid=uuid,
                             steer=not opened, text=content,
                             # "unknown" is stamped by the tailer when a fence or a
                             # rebuild sat between the enqueue and this record.
                             attribution="steer" if not opened else "turn", **extra))
    events.append(
        _event("turn_opened" if opened else "turn_steered", new,
               turn_id=new["turn_id"], uuid=uuid, tags=tags,
               # The line itself, so a line the OPERATOR typed (no tag of ours) can reach
               # the voice as context. A relayed frame carries its tag and is not repeated.
               text=content if not tags else "")
    )
    return new, tuple(events)


def _reduce_user_list(state: dict, record: dict, content: list,
                      at: Any) -> tuple[dict, tuple[dict, ...]]:
    """Tool plumbing: resolve tool ids; never a receipt, never a turn opener."""
    resolved = {
        block.get("tool_use_id")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    resolved.discard(None)
    pending = state["pending_tool_ids"] - resolved
    if pending == state["pending_tool_ids"] and not resolved:
        return state, ()
    new = _bump(state, pending_tool_ids=pending, last_record_at=at or state["last_record_at"])
    return new, ()


def _reduce_assistant(state: dict, record: dict, *,
                      wires: dict | None = None) -> tuple[dict, tuple[dict, ...]]:
    wires = wires or {}
    message = _message(record)
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    at = record.get("timestamp")
    stop_reason = message.get("stop_reason")
    # Our tag quoted back in assistant prose is NOT a receipt (Astra R3 #4 corrected the
    # round-1 "embedded = merged" reading: those were quotations and summaries).
    carried = _assistant_carried_text(blocks)
    quoted = tuple(_event("tag_seen_unresolved", state, tag=t, where="assistant",
                          turn_id=state["turn_id"], text=carried[:400])
                   for t in tags_in(carried) if t in wires)

    opened = {
        block.get("id")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    opened.discard(None)
    pending = state["pending_tool_ids"] | opened
    text = _text_blocks(blocks).strip()

    if stop_reason == "end_turn" and not text and not opened and blocks and all(
            isinstance(block, dict) and block.get("type") == "thinking" for block in blocks):
        # The host writes the turn's closing thinking block as its OWN `end_turn` record,
        # 8 ms before the record that carries the answer (measured 2026-09-21T00:58:07).
        # Closing here would make the answer an orphan: its record arrives with no open
        # turn and its text is never a result. A thinking-only record is not the end of
        # anything the operator can hear; the turn stays open for the text.
        return _bump(state, last_record_at=at or state["last_record_at"]), quoted

    if stop_reason == "end_turn":
        # The ONLY thing that closes a turn. This event says the turn ENDED and carries
        # the text that stood at the end of it; it does not claim that text is "the
        # answer" — whether any of it is worth saying is the speaking layer's call.
        new = _bump(
            state,
            turn_id=None,
            turn_inputs=(),
            pending_tool_ids=frozenset(),
            activity="idle",
            last_record_at=at or state["last_record_at"],
        )
        return new, quoted + (_event("turn_result", new, turn_id=state["turn_id"],
                                     inputs=state["turn_inputs"], text=text),)

    new = _bump(
        state,
        pending_tool_ids=pending,
        activity="working",
        last_record_at=at or state["last_record_at"],
    )
    events: list[dict] = list(quoted)
    if text:
        # Text that arrived while the turn is still open. THE TELL IS `stop_reason`, NOT
        # THIS RECORD'S OWN BLOCKS: checking whether this record also opened a tool_use is
        # the obvious reading and it is wrong on real transcripts — across a 26,103-record
        # session, ZERO assistant records carry text and a `tool_use` block together. The
        # host emits mid-turn text as a text-only record and the tool call as the next
        # record, so a same-record test separates nothing at all. Census of text-bearing
        # records in that session: 1,276 tool_use against 311 end_turn.
        #
        # The event is emitted plainly and carries no judgement about what the text is.
        # The turn did not end; that is the whole of what this module knows.
        events.append(_event("assistant_text", new, turn_id=new["turn_id"], text=text))
    return new, tuple(events)


# ---------------------------------------------------------------- the tailer

class Tailer:
    """Follows one session transcript. Owns file cursors and partial bytes ONLY.

    Everything the caller reads about Claude comes from `state`, which is produced
    exclusively by `reduce_transcript`. `poll()` decodes COMPLETE JSONL lines: a split
    UTF-8 sequence or a half-written JSON object is held in `_partial` and produces no
    event until the newline that completes it arrives.
    """

    def __init__(self, path: Path, *, session_id: str) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self._state: dict = initial_state()
        self._offset = 0
        self._partial = b""
        self._identity: tuple[int, int] | None = None   # (st_dev, st_ino)
        # HISTORY CONTINUITY (Astra queue-r2 item A). What the last replay actually read,
        # as (length, sha1). A rotation replaces the file; only a replacement that still
        # CONTAINS this exact prefix is the same history continuing, and only then may the
        # balance derived from byte 0 be believed. None = nothing replayed yet, so a first
        # bootstrap has nothing to contradict and proceeds as before.
        self._origin: tuple[int, str] | None = None
        # A4/F7. EVERY BYTE WE HAVE CONSUMED, as (length, sha1 of that prefix). `_origin`
        # proves a REPLAYED prefix continues our history; this proves the file we are about
        # to read FORWARD from is still the one those bytes came out of. Inode plus size
        # cannot: an in-place rewrite of the same length keeps both, and the cursor then
        # indexes into a different file's middle.
        #
        # THE WHOLE PREFIX, because two narrower witnesses each have a hole. A fixed tail
        # of bytes misses a replacement whose final record is longer than the window —
        # everything before those bytes is free to change and the window still matches. The
        # last complete RECORD misses one that rewrites an earlier record and leaves the
        # last alone, which is the ordinary shape of a rewritten transcript: the tail is the
        # same `end_turn`, the turn before it is not.
        self._witness: tuple[int, str] | None = None
        # THE WIRE REGISTRY lives OUTSIDE `_state` so a bootstrap/rotation rebuild cannot
        # drop it (diversity D6). tag -> collapsed wire fragment the daemon typed.
        self._wires: dict[str, str] = {}
        self._producers: dict[str, int] = {}   # relay: tag -> posting pid
        # Tags whose receipt MUST carry a positive peer stamp (the qualification probe).
        self._strict_peer: set[str] = set()
        # Tags whose attribution is no longer provable: a fence or a rebuild sat between
        # their enqueue and any later consumption. Stamped `attribution="unknown"`.
        self._uncertain: set[str] = set()
        # Emission dedupe: `consumed` and `tag_seen_unresolved` are each said ONCE per tag,
        # however many replays or repeats carry the tag.
        self._consumed_emitted: set[str] = set()
        self._unresolved_emitted: set[str] = set()

    # -------------------------------------------------------------- registration

    def register(self, tag: str, wire: str, *, producer_pid: int | None = None,
                 strict_peer: bool = False) -> None:
        """Teach the tailer what the daemon typed (or posted) under `tag` (called at
        authorization, before actuation, and by recovery for every instruction that may
        have landed — BEFORE `bootstrap()`, so replay reconciliation has something to
        reconcile). A RELAY registration names the posting pid: a carrier naming a
        DIFFERENT pid can never credit the tag. `strict_peer` additionally refuses a
        carrier that names NO pid at all — used only by the qualification probe, which
        exists to prove positive identity and must not be satisfied by an unstamped
        frame."""
        if isinstance(tag, str) and tag.startswith("⟨v#"):
            self._wires[tag] = collapse(wire)
            if producer_pid is not None:
                self._producers[tag] = int(producer_pid)
            if strict_peer:
                self._strict_peer.add(tag)

    def unregister(self, tag: str) -> None:
        """A durably released attempt never produced a keystroke; forget its wire (N2)."""
        self._wires.pop(tag, None)
        self._producers.pop(tag, None)
        self._strict_peer.discard(tag)
        self._uncertain.discard(tag)

    def registered(self) -> tuple[str, ...]:
        return tuple(self._wires)

    def _mark_uncertain(self) -> None:
        """Every registered, not-yet-consumed tag loses provable attribution."""
        for tag in self._wires:
            if tag not in self._consumed_emitted:
                self._uncertain.add(tag)

    def _filter(self, events: tuple[dict, ...] | list[dict]) -> tuple[dict, ...]:
        """Stamp attribution and enforce once-per-tag emission."""
        out: list[dict] = []
        for event in events:
            kind, tag = event.get("kind"), event.get("tag")
            if kind == "consumed" and tag in self._wires:
                if tag in self._consumed_emitted:
                    continue
                self._consumed_emitted.add(tag)
                if tag in self._uncertain:
                    event = {**event, "attribution": "unknown"}
            elif kind == "consumed" and tag in self._uncertain:
                event = {**event, "attribution": "unknown"}
            elif kind == "tag_seen_unresolved":
                if tag in self._unresolved_emitted:
                    continue
                self._unresolved_emitted.add(tag)
            out.append(event)
        return tuple(out)

    def _reconcile(self, records: list[dict]) -> tuple[dict, ...]:
        """After a replay (bootstrap or rotation): what did the replayed span say about
        registered tags? An exact-equality user string is a `consumed` with
        `attribution=unknown`; any other carrier is `tag_seen_unresolved`; each once
        (Astra R3 #5). The replay itself emits nothing else."""
        if not self._wires:
            return ()
        self._mark_uncertain()
        events: list[dict] = []
        for record in records:
            kind = record.get("type")
            if kind == "attachment":
                # The busy-hub relay carrier, replayed: same equality rule as live, but
                # attribution is unknown because turn history is incomplete (Astra R4-3).
                attachment = record.get("attachment") or {}
                prompt = attachment.get("prompt") if isinstance(attachment, dict) else None
                if attachment.get("type") != "queued_command" or not isinstance(prompt, str):
                    continue
                origin = _peer_origin(attachment) or _peer_origin(record)
                for tag in tags_in(prompt):
                    wire = self._wires.get(tag)
                    if wire is None:
                        continue
                    if origin is not None and collapse(prompt) == wire \
                            and _peer_mismatch(tag, origin, self._producers, self._strict_peer) is None:
                        events.append(_event("consumed", self._state, tag=tag, turn_id=None,
                                             uuid=attachment.get("source_uuid"), steer=True,
                                             text=prompt, attribution="unknown", replayed=True,
                                             carrier="attachment",
                                             peer_pid=origin.get("verifiedPeerPid")))
                    else:
                        events.append(_event("tag_seen_unresolved", self._state, tag=tag,
                                             where="replay", text=prompt[:400]))
                continue
            if kind not in ("user", "assistant"):
                continue
            content = _message(record).get("content")
            blocks = content if isinstance(content, list) else []
            text = content if isinstance(content, str) else (
                _assistant_carried_text(blocks) if kind == "assistant" else _carried_text(blocks))
            origin = _peer_origin(record) if kind == "user" else None
            body = _peer_body(content) if (origin is not None and isinstance(content, str)) \
                else content
            for tag in tags_in(text):
                wire = self._wires.get(tag)
                if wire is None:
                    continue
                if kind == "user" and isinstance(content, str) and collapse(body) == wire \
                        and _peer_mismatch(tag, origin, self._producers, self._strict_peer) is None:
                    extra = ({"carrier": "peer_user", "peer_pid": origin.get("verifiedPeerPid")}
                             if origin is not None else {})
                    events.append(_event("consumed", self._state, tag=tag,
                                         turn_id=None, uuid=record.get("uuid"),
                                         steer=None, text=content, attribution="unknown",
                                         replayed=True, **extra))
                else:
                    events.append(_event("tag_seen_unresolved", self._state, tag=tag,
                                         where="replay", text=text[:400]))
        return self._filter(events)

    @property
    def state(self) -> dict:
        """What Claude is doing, as a CONSUMER must read it.

        The replayed state is fenced whenever unread bytes are buffered. Deriving that
        fence here rather than storing it is what keeps `unknown` a HOLDING STATE: the
        moment the newline lands, `_partial` empties and the underlying state — which
        kept accumulating correctly the whole time — shows through again. Storing the
        fence instead would latch it, and a session that saw one half-written record
        could never promote again.
        """
        if self._partial.strip():
            return _fence_unknown(self._state)
        return self._state

    # -------------------------------------------------------------- bootstrap

    def bootstrap(self, *, require_continuity: bool = False) -> dict:
        """Seed state from the existing file and park the cursor at its end.

        Replays the COMPLETE transcript from byte 0 — the one point where the queue is
        provably empty — through the size captured by the stat. A tail window cannot
        do this job: a turn boundary proves only that no TURN was open there, never
        that the queue was empty, so any truncated replay seeded UNKNOWN and refused
        every send once a session outgrew the window (queue-window review, Astra:
        OPTION 3). The read is bounded to the captured size so a record appended between
        the stat and the read is neither replayed here nor missed: the cursor parks at
        that same size and the next poll reads it exactly once.

        `require_continuity` is set by the ROTATION path (Astra queue-r2 item A). A first
        bootstrap has no prior history to contradict, so byte 0 of the file it finds is
        the session's origin by definition. A rotation does not get that assumption for
        free: the replacement may be a SUFFIX of the history we already replayed, and
        replaying a suffix from "byte 0 = origin" derives a balance from records whose
        openers are gone. The measured counterexample is `enqueue(B), end_turn` replaced
        by the suffix `end_turn` alone — balance 1 becomes 0, and a queued instruction the
        operator is still waiting on reads as an empty queue. So a rotation must first
        prove the replacement still begins with the exact bytes we already read; when it
        cannot, the balance and activity it derives are fenced to UNKNOWN.
        """
        try:
            stat = self.path.stat()
        except OSError:
            # A session whose transcript has not appeared yet is NORMAL, not an error,
            # but it is also not evidence of idleness. Any OTHER read failure is missing
            # evidence for the same reason (Astra round 8: poll() delegates to bootstrap()
            # on rotation, so an exception raised HERE escaped both of poll's guards and
            # left a stale idle intact). `initial_state` is the ONE place that couples
            # UNKNOWN balance to non-idle activity — never restate it here, or a
            # regression in that coupling hides behind the restatement.
            self._state = initial_state(queue_balance=UNKNOWN)
            self._offset = 0
            self._partial = b""
            self._witness = None      # rewound with the cursor it describes (A4)
            self._identity = None
            # The prefix we could compare against is no longer evidence of anything: the
            # file we would compare it TO is unreadable. Clearing it keeps the next
            # bootstrap honest — it starts from UNKNOWN either way, and a stale digest
            # would only ever fence a file that may well be the same history.
            self._origin = None
            self._mark_uncertain()             # evidence about registered wires is gone
            self._pending_reconciliation = ()
            return dict(self.state)

        self._identity = (stat.st_dev, stat.st_ino)
        size = stat.st_size
        try:
            fh = open(self.path, "rb")
        except OSError:
            self._state = initial_state(queue_balance=UNKNOWN)
            self._offset = 0
            self._partial = b""
            self._witness = None      # rewound with the cursor it describes (A4)
            self._identity = None
            # The prefix we could compare against is no longer evidence of anything: the
            # file we would compare it TO is unreadable. Clearing it keeps the next
            # bootstrap honest — it starts from UNKNOWN either way, and a stale digest
            # would only ever fence a file that may well be the same history.
            self._origin = None
            self._mark_uncertain()
            self._pending_reconciliation = ()
            return dict(self.state)
        try:
            with fh:
                blob = self._read_window(fh, 0, size)
                # THE DESCRIPTOR WE READ MUST BE THE FILE WE STAT'D (Astra queue-r2 item A).
                #
                # `stat` names a path; `open` follows the path again. Between the two the
                # file can be replaced — a rotation, a `mv` — and then `size` describes one
                # inode while `blob` came from another. Measured: stat 181 bytes, truncate
                # to 95, read 95, regrow to 181, park at 181. The cursor sat past 86 bytes
                # nothing ever read, and the next poll skipped the enqueue inside them —
                # a pending queue item that silently became an empty queue.
                #
                # Both halves are checked on the OPEN descriptor: its identity must be the
                # one the stat named, and the read must have returned the whole window.
                # Either failure is missing evidence, not a smaller file.
                after = os.fstat(fh.fileno())
                identity_now = (after.st_dev, after.st_ino)
                if identity_now != self._identity or len(blob) != size:
                    raise _BootstrapUnproven(
                        f"identity={identity_now}!={self._identity}"
                        if identity_now != self._identity
                        else f"short_read {len(blob)}/{size}")
        except (OSError, _BootstrapUnproven):
            # Opened, then could not read (impl review round 2, #4/N1), or read bytes we
            # cannot prove belong to the file we measured: the same missing evidence as a
            # failed open, handled the same way.
            self._state = initial_state(queue_balance=UNKNOWN)
            self._offset = 0
            self._partial = b""
            self._witness = None      # rewound with the cursor it describes (A4)
            self._identity = None
            # The prefix we could compare against is no longer evidence of anything: the
            # file we would compare it TO is unreadable. Clearing it keeps the next
            # bootstrap honest — it starts from UNKNOWN either way, and a stale digest
            # would only ever fence a file that may well be the same history.
            self._origin = None
            self._mark_uncertain()
            self._pending_reconciliation = ()
            return dict(self.state)

        lines = blob.split(b"\n")
        # A file not ending in a newline leaves a partial final record: it is NOT
        # replayed, and it is NOT counted against the cursor.
        tail_partial = lines.pop() if lines else b""

        # STREAMED, one record in memory at a time. Measured 2026-09-20: a 123 MB /
        # 45,663-record session decoded into a list was what the host killed the daemon
        # for ("running low on memory"). Only two things need records kept: the origin
        # probe (`_is_session_origin` reads the first lineage record) and reconciliation
        # (`_reconcile` reads only records that carry a registered tag), so only those
        # are retained; everything else is reduced and dropped.
        tag_bytes = tuple(tag.encode("utf-8") for tag in self._wires)
        origin_probe: list[dict] = []
        origin_settled = False
        tagged: list[dict] = []
        undecodable = 0
        state = initial_state()
        for raw in lines:
            record = self._decode(raw)
            if record is Tailer.UNREADABLE:
                undecodable += 1
                continue
            if record is None:
                continue
            if not origin_settled:
                origin_probe.append(record)
                if record.get("type") in _LINEAGE_TYPES:
                    origin_settled = True
            if tag_bytes and any(tb in raw for tb in tag_bytes):
                tagged.append(record)
            if not undecodable:
                # Byte 0 is the session's origin: no turn open, nothing queued. Every
                # enqueue/dequeue/remove since then is inside the replayed span, so the
                # balance that comes out is derived, not manufactured (F3 stays honest).
                state, _ = reduce_transcript(state, record, wires=self._wires,
                                             producers=self._producers, strict=self._strict_peer)
        if undecodable:
            # Complete lines we could not decode: a corrupt or foreign file. Whatever
            # else the file held, we cannot claim to have read it.
            state = initial_state(queue_balance=UNKNOWN)
        replayed = origin_probe + [r for r in tagged if r not in origin_probe]

        # NOTE: a partial tail ALSO fences this state, but that fence is derived by the
        # `state` property from the live `_partial` buffer rather than stored here.
        # Storing it would latch; unknown must stay a holding state.

        # CONTINUITY, decided on the bytes rather than on a record's word for it. The
        # prefix we replayed before must still be the prefix of what we just read; a
        # replacement that merely CONTAINS those records later, or drops them entirely,
        # is a different history and the derived balance is not ours to trust.
        #
        # THERE ARE EXACTLY TWO WAYS BACK, and neither is "wait for the next record".
        # Astra queue-r2 A says preserve UNKNOWN after rotation/shrink UNLESS original-
        # history continuity is established, so a rotation merely continuous with the
        # SUFFIX must stay UNKNOWN however long it grows:
        #
        #   1. THE ORIGINAL HISTORY RETURNS. `_origin` therefore keeps pointing at the
        #      history the fence was about, never at the suffix that caused it — an
        #      `mv` undone, a file restored, and the digest matches again.
        #   2. A FILE THAT PROVABLY STARTS AT A SESSION ORIGIN replaces it. That is a
        #      different history, not a fragment of ours, and byte 0 means what
        #      `bootstrap` needs it to mean, so it BECOMES the new origin and the fence
        #      it replaces is dropped (`_unfence`).
        #
        # Anything else stays UNKNOWN. Without the second path the tailer latched: every
        # later rotation was judged against a history that no longer existed, and a call
        # never recovered a balance after one suffix rotation (quality review, Task 5).
        prior = self._origin
        if require_continuity and prior is not None and not self._continues(prior, blob):
            # An undecodable file is never promoted to origin: its state is already
            # UNKNOWN and its bytes are exactly what we could not read.
            if not undecodable and _is_session_origin(replayed):
                self._origin = (len(blob), hashlib.sha1(blob).hexdigest())
                state = _unfence(state)
            else:
                state = _fence_unknown(initial_state(queue_balance=UNKNOWN))
                self._mark_uncertain()
        else:
            self._origin = (len(blob), hashlib.sha1(blob).hexdigest())

        self._state = state
        # The cursor sits at the CAPTURED size and `_partial` carries the unterminated
        # tail up to it. Rewinding the cursor behind those bytes while ALSO holding them
        # would make the next poll read them a second time and prepend them to
        # themselves, producing a doubled, undecodable line — silently losing a record
        # that was merely mid-write. Parking it past bytes the read never saw would
        # lose an append that landed between the stat and the read.
        self._offset = size
        self._partial = tail_partial
        # Re-seeded with the cursor (A4). A witness left over from the previous file would
        # make the next poll declare THIS file replaced too, on every single poll.
        self._witness = (size, hashlib.sha1(blob[:size]).hexdigest())
        # A replay applies records and discards their events; what it may NOT discard
        # is evidence about a registered wire. Reconciled here, once per tag.
        self._pending_reconciliation = self._reconcile(replayed)
        return dict(self.state)

    def take_reconciliation(self) -> tuple[dict, ...]:
        """Events the last replay produced about registered tags (consumed once each)."""
        out = getattr(self, "_pending_reconciliation", ())
        self._pending_reconciliation = ()
        return tuple(out)

    @staticmethod
    def _continues(prior: tuple[int, str], blob: bytes) -> bool:
        """Does `blob` still begin with the exact bytes a previous replay read?

        Length and digest, nothing cleverer. A transcript is append-only within a life,
        so a replacement that continues the same history reproduces its prefix byte for
        byte; anything else — a suffix, a rewrite, a different session's file — does not.
        A shorter file cannot contain the prefix at all and is answered without hashing.
        """
        length, digest = prior
        if len(blob) < length:
            return False
        return hashlib.sha1(blob[:length]).hexdigest() == digest

    @staticmethod
    def _read_window(fh, start: int, end: int) -> bytes:
        """Bytes [start, end) as one read — never past `end`, so the replay and the
        cursor agree on what was seen. A seam so a test can fail the READ after a
        successful open, or interleave an append between the stat and the read."""
        fh.seek(start)
        return fh.read(max(0, end - start))

    # A line we could NOT PARSE, as distinct from a line that parsed fine and is simply
    # not ours (a subagent, another session, a blank). The first is missing evidence and
    # must fence; the second is normal and must not (F2, Astra round 6).
    UNREADABLE = object()

    # -------------------------------------------------------------- polling

    def poll(self) -> tuple[dict, ...]:
        """Consume whatever is newly complete and return the events it produced.

        Detects rotation/shrink by inode and size, and rebuilds from a complete turn
        boundary when it happens — a replayed prefix therefore cannot re-emit receipts
        or results the caller has already seen, because the replay starts after the
        last boundary rather than at the top of the file.
        """
        try:
            stat = self.path.stat()
        except OSError:
            # ANY failure to reach the transcript, not just FileNotFoundError (Astra round
            # 7). A PermissionError propagated out of poll(), the daemon's loop caught it
            # to keep the call alive, and the last state - possibly `idle/0` - survived
            # untouched, still permitting a keystroke. What the failure IS does not matter:
            # we cannot see the session, so we cannot claim it is idle.
            self._state = _fence_unknown(self._state)
            self._mark_uncertain()
            return ()

        identity = (stat.st_dev, stat.st_ino)
        if self._identity is None:
            self._identity = identity
        if identity != self._identity or stat.st_size < self._offset:
            # Rotated or truncated underneath us: the cursor is meaningless now. The
            # rebuild re-seeds state and emits NOTHING — except what the replayed span
            # proves about registered wires, once per tag (Astra R3 #5). It must also
            # PROVE the replacement continues the history we already replayed, or the
            # balance it derives from byte 0 is a suffix's balance (queue-r2 item A).
            self.bootstrap(require_continuity=True)
            return self.take_reconciliation()

        try:
            with open(self.path, "rb") as fh:
                # A4. CONTINUITY OF WHAT WE ALREADY READ, checked BEFORE the cursor is
                # believed and before the no-new-bytes shortcut. Inode and size agreeing
                # proves the file was not rotated or truncated; it does not prove it was
                # not REWRITTEN. `os.replace` onto a filesystem that reuses the inode
                # number, or an in-place `r+b` overwrite of the same length, keeps both —
                # and then seeking to `_offset` reads a different history from its middle,
                # silently, with the old state still standing.
                #
                # THE SIZE SHORTCUT USED TO COME FIRST, which made this check unreachable
                # for exactly the case it exists to catch: a same-length rewrite appends
                # nothing, so `st_size == _offset` returned before anything was re-read.
                # One read of the consumed prefix per poll, on a file the poll was about
                # to open anyway whenever there IS new data; when there is not, that read
                # is the entire cost of noticing a replacement.
                if self._witness is not None:
                    length, digest = self._witness
                    fh.seek(0)
                    seen, again_digest = _sha1_prefix(fh, length)
                    if seen != length or again_digest != digest:
                        raise _HistoryReplaced
                if stat.st_size == self._offset:
                    # Same file, nothing new. Checked AFTER the witness, so "no new bytes"
                    # can no longer mean "no new bytes that we looked for".
                    return ()
                fh.seek(self._offset)
                chunk = fh.read()
                # THE NEXT WITNESS, taken while the descriptor is open: the whole prefix
                # the cursor is about to cover. Re-read from the file rather than kept in
                # memory — the check above reads it back anyway, so a second copy of the
                # transcript would buy nothing.
                fh.seek(0)
                grown = (self._offset + len(chunk),
                         _sha1_prefix(fh, self._offset + len(chunk))[1])
        except _HistoryReplaced:
            # A replacement wearing our inode and our size. The cursor means nothing now,
            # exactly as after a rotation, and the same rebuild applies: replay from byte
            # zero and PROVE the result continues the history we already reported.
            self.bootstrap(require_continuity=True)
            return self.take_reconciliation()
        except OSError:
            # Same reasoning as the stat above: an unreadable transcript is missing
            # evidence, and missing evidence must never read as a proven idle.
            self._state = _fence_unknown(self._state)
            self._mark_uncertain()
            return ()

        self._offset += len(chunk)
        self._witness = grown
        buffer = self._partial + chunk
        lines = buffer.split(b"\n")
        self._partial = lines.pop()

        events: list[dict] = []
        for raw in lines:
            record = self._decode(raw)
            if record is Tailer.UNREADABLE:
                # A COMPLETE line we could not parse. F2 (Astra round 6): this used to be
                # stepped over, so a partial write that later completed into malformed
                # JSON took the state from `unknown` straight back to `idle/0` — and that
                # PASSES promotion. The line existed and we do not know what it said; it
                # could have been the enqueue that makes the queue non-empty. Fence and
                # let a complete window re-establish the state.
                self._state = _fence_unknown(self._state)
                self._mark_uncertain()
                continue
            if record is None:
                continue
            self._state, produced = reduce_transcript(self._state, record, wires=self._wires,
                                                      producers=self._producers,
                                                      strict=self._strict_peer)
            events.extend(produced)

        return self._filter(events)

    # -------------------------------------------------------------- decoding

    def _decode(self, raw: bytes) -> dict | None:
        """One COMPLETE line -> a record for this session, or None.

        `raw` is always a whole line (the caller keeps the trailing fragment), so a
        UTF-8 sequence split across reads has already been rejoined here. Strict
        decoding is deliberate: a line that does not decode is a line we do not
        understand, and guessing at it would be guessing at Claude's state.
        """
        if not raw.strip():
            return None                      # blank: nothing was ever there
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return Tailer.UNREADABLE
        try:
            record = json.loads(text)
        except ValueError:
            return Tailer.UNREADABLE
        if not isinstance(record, dict):
            return Tailer.UNREADABLE
        if record.get("isSidechain"):
            return None    # a subagent's turns are not this pane's turns
        session = record_session_id(record)
        if session and self.session_id and session != self.session_id:
            return None
        return record

    # -------------------------------------------------------------- reporting

    def snapshot(self, *, at: float) -> dict:
        """The state plus the observation time, for the conversation layer to quote."""
        snap = dict(self.state)
        snap["observed_at"] = at
        snap["pending_tool_ids"] = tuple(sorted(self.state["pending_tool_ids"]))
        snap["incomplete_tail_bytes"] = len(self._partial)
        return snap
