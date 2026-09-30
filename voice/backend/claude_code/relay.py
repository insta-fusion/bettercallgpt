"""The relay — the one socket that hands the operator's words to Claude Code.

Ported from `voice_listen_relay.py`, bucket (a) plus its own WAL records. This is a transport:
it connects, proves who is on the other end, writes one frame, and reports exactly how far it
got. It decides nothing about what the words mean.

Four properties, each the answer to a specific way a relay can hurt someone.

**The peer is proven before the auth line.** A socket path can be replaced. On darwin the peer
pid is read off the connected socket and compared to the pid we bound; a mismatch refuses
BEFORE the token is written, so a stranger never receives our credentials.

**`priority` rides the frame.** The receiver treats an omitted field as `next` — deliver after
the current turn. That default is why every correction relayed from this process for weeks
queued behind the very turn it was correcting, and that turn commonly ended by producing the
answer being corrected. `now` makes the receiver abort the running turn. The relay carries the
field; it never decides it.

**How far the write got is the release rule.** `never_started` is true only when no byte of the
user frame was written. A write that began stays owed forever and is never replayed: the
receiver may have acted on it already, and a relayed sentence is as un-retractable as a
keystroke.

**One writer, ordered per tag.** The lock is the whole of the socket's protection. Ordering
across tags is an insertion-ordered dict, so "the order the operator said them" is a fact about
the store rather than a sort someone must remember to apply. A later tag waits for the writer,
never for an earlier tag's receipt.

What the split (the backend split (DESIGN.md §Backend split)) removed: `promote` no longer reads a transcript
snapshot to label a send `turn` or `steer`, so `CLASS_TURN`, `CLASS_STEER`, the `snapshot`
constructor parameter, and the `queue_unknown` / `turn_unknown` refusals are gone. The
conversation log already holds turn state.

Design: voice/DESIGN.md §Backend split, §Acceptance E4, E6.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import time
import uuid
from typing import Any, Callable

from .pane import owner_fingerprint, registry_matches, session_registry

RELAY_EFFECT = "relay_post"

# How far a byte-level write got. A pure transport fact; nothing here means "accepted".
STATE_NOT_SENT = "not_sent"          # proven: no byte of the user frame was written
STATE_UNSENT = "unsent"              # enqueued in the outbox; no write has begun
STATE_POSTING = "posting"            # the write has begun; its outcome is not yet known
STATE_POSTED = "posted"              # the whole frame was written; receipt pending
STATE_POST_UNKNOWN = "post_unknown"  # the write began and its outcome is unknown

REFUSE_BUSY = "busy"
REFUSE_RELAY_PLATFORM = "relay_unsupported_platform"
REFUSE_RELAY_SOCKET_MISSING = "relay_socket_missing"
REFUSE_RELAY_TOKEN_MISSING = "relay_token_missing"
REFUSE_RELAY_SOCKET_MISMATCH = "relay_socket_mismatch"
REFUSE_RELAY_PEER_PID = "relay_peer_pid_mismatch"
REFUSE_RELAY_PENDING = "relay_pending_send"
REFUSE_RELAY_IDENTITY = "relay_identity_lost"
REFUSE_RELAY_NOT_QUALIFIED = "relay_not_qualified"

SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"

# macOS: getsockopt(SOL_LOCAL, LOCAL_PEERPID) on a connected AF_UNIX socket.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 0x002

# ONE transport bound lives in this file and it has no literal here: `timeout` is a required
# parameter `app/daemon.py` supplies, so nothing in this module body names a duration. It bounds
# the socket connect, which DESIGN.md §Timers permits: it releases no semantic slot, it only stops a
# connect from hanging forever. Keeping it a REQUIRED keyword — not a defaulted constant — is
# what makes the daemon the single place a duration is written down, and what stops a semantic
# timer being smuggled in later as "just another default".
#
# There is no close-settle wait. Closing is EVENT-DRIVEN: after the frame is written we
# half-close the write side and read to EOF, so the peer's own close is the signal. That is both
# faster and a stronger guarantee than a sleep ever was — a sleep hoped the receiver had read
# the bytes, while EOF proves it did.


def relay_env(env: dict | None = None) -> tuple[str, str]:
    """(socket_path, token) from the environment. The token is read HERE and nowhere else; it
    must never reach argv, a status file, the WAL or a prompt."""
    env = env if env is not None else os.environ
    return str(env.get(SOCKET_ENV) or ""), str(env.get(TOKEN_ENV) or "")


def _refusal(reason: str, **extra: Any) -> dict:
    out = {"ok": False, "refused": reason, "state": STATE_NOT_SENT,
           "sent_bytes": False, "never_started": True}
    out.update(extra)
    return out


def peer_pid_of(sock: socket.socket) -> int | None:
    """The pid at the other end of a connected AF_UNIX socket, or None when the platform cannot
    say. Measured on macOS 24.6."""
    if sys.platform != "darwin":
        return None
    try:
        return int(struct.unpack("i", sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4))[0])
    except (OSError, struct.error):
        return None


def frame(text: str, session_id: str = "", priority: str = "") -> bytes:
    """The user frame.

    `session_id` is the FROZEN id of the session we bound. The receiver checks an optional
    top-level `session_id`, which closes the window in which a `/resume` under the same pid has
    switched conversations before its registry file is rewritten.

    `priority` is the field whose absence the operator reported for weeks as 「你回复的不是我
    最新的消息」. The receiver accepts now/next/later and an OMITTED field defaults to `next`,
    so every correction queued behind the turn it was correcting. `now` makes the receiver abort
    the running turn. Both are sent only when non-empty, so an ordinary relay keeps its exact
    previous bytes.
    """
    body: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": text}}
    if session_id:
        body["session_id"] = str(session_id)
    if priority:
        body["priority"] = str(priority)
    return (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


def _auth_line(token: str) -> bytes:
    return (json.dumps({"type": "auth", "token": token}) + "\n").encode("utf-8")


def post_frame(socket_path: str, token: str, text: str, *, expected_pid: int,
               timeout: float, session_id: str = "", priority: str = "",
               connect: Callable[[str, float], socket.socket] | None = None) -> dict:
    """Connect, prove the peer, write auth then the user frame, then wait for the peer's close.

    The report says exactly how far it got, because the caller's RELEASE decision hangs on it:
    `frame_started` False means no instruction text ever left this process.

    `drained` says the peer closed its end after our frame — positive evidence the bytes were
    read, not merely handed to the kernel. It is reported, never required: a receiver that holds
    the connection open is not a failed send, so `drained` False with `frame_written` True stays
    a success. Nothing downstream branches on it; it is here so a diagnostic can tell the two
    apart instead of guessing.
    """
    report = {"connected": False, "peer_pid": None, "auth_written": False,
              "frame_started": False, "frame_written": False, "drained": False, "error": ""}
    sock: socket.socket | None = None
    try:
        if connect is not None:
            sock = connect(socket_path, timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(socket_path)
        report["connected"] = True
        pid = peer_pid_of(sock)
        report["peer_pid"] = pid
        # BEFORE the auth line: a stranger must never receive our token.
        if pid is not None and int(pid) != int(expected_pid):
            report["error"] = REFUSE_RELAY_PEER_PID
            return report
        if pid is None and sys.platform == "darwin":
            # On the platform that CAN tell us, silence is a refusal, not a pass.
            report["error"] = REFUSE_RELAY_PEER_PID
            return report
        sock.sendall(_auth_line(token))
        report["auth_written"] = True
        report["frame_started"] = True
        sock.sendall(frame(text, session_id, priority))
        report["frame_written"] = True
        # EVENT-DRIVEN CLOSE. Half-close the write side so the receiver sees the end of our
        # frame, then read until EOF: its close is the proof it consumed us. The connect timeout
        # bounds this read too, so a peer that never closes costs one bounded wait and is
        # reported as not drained rather than hanging.
        try:
            sock.shutdown(socket.SHUT_WR)
            while True:
                if not sock.recv(65536):
                    break
            report["drained"] = True
        except OSError:
            # A peer that resets or is already gone after taking the frame is not a failed
            # send: the bytes were written. Say so and move on.
            pass
        return report
    except (OSError, ValueError) as exc:
        report["error"] = report["error"] or f"{type(exc).__name__}: {exc}"
        return report
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def live_owner(pid: int, *, ps_timeout: float) -> dict | None:
    """The live fingerprint for `pid`: {} when the process is PROVEN gone, None when the
    evidence is UNAVAILABLE. Callers treat None as "refuse this send, keep the binding" — an
    unreadable or wedged `ps` is not proof of death."""
    try:
        return owner_fingerprint(int(pid), ps_timeout=ps_timeout)
    except Exception:
        return None


def relay_binding(binding: dict, socket_path: str, token: str) -> tuple[dict | None, str | None]:
    """Bind to one session: the registry must name this pid, session, start AND socket path."""
    if sys.platform != "darwin":
        return None, REFUSE_RELAY_PLATFORM
    if not socket_path:
        return None, REFUSE_RELAY_SOCKET_MISSING
    if not token:
        return None, REFUSE_RELAY_TOKEN_MISSING
    claude = (binding or {}).get("claude") or {}
    if not claude:
        return None, REFUSE_RELAY_IDENTITY
    bound = dict(binding)
    bound["socket"] = socket_path
    return bound, None


def relay_identity_still_holds(binding: dict, registry: dict | None, *,
                               start_granularity: float) -> str | None:
    """Per-frame identity re-verification, including the socket path."""
    why = registry_matches(binding, registry, start_granularity=start_granularity)
    if why is not None:
        return why
    expected = str((binding or {}).get("socket") or "")
    actual = str((registry or {}).get("messagingSocketPath") or expected)
    # Compared as the same file, not the same spelling: macOS /tmp is /private/tmp.
    if expected and actual and os.path.realpath(expected) != os.path.realpath(actual):
        return REFUSE_RELAY_SOCKET_MISMATCH
    return None


class RelayActuator:
    """The single post slot for one relay binding: one serialized writer, one ordered outbox."""

    def __init__(self, *, binding: dict, ledger: Any, token: str, producer: dict,
                 connect_timeout: float, start_granularity: float, ps_timeout: float,
                 registry: Callable[[int], dict | None] | None = None,
                 fingerprint: Callable[[int], dict | None] | None = None,
                 poster: Callable[..., dict] | None = None,
                 now: Callable[[], float] | None = None) -> None:
        self._binding = dict(binding or {})
        self._ledger = ledger
        self._token = token
        self._producer = dict(producer)
        self._registry = registry or session_registry
        self._ps_timeout = float(ps_timeout)
        self._fingerprint = fingerprint or (
            lambda pid: live_owner(pid, ps_timeout=self._ps_timeout))
        self._poster = poster or post_frame
        # Passed through to every post, and to the per-frame identity re-check. The daemon
        # wrote these down; this class only carries them.
        self._connect_timeout = float(connect_timeout)
        self._start_granularity = float(start_granularity)
        self._now = now or time.time
        self._lock = asyncio.Lock()
        self._attempts: dict[str, dict] = {}
        # THE ORDERED PER-TAG OUTBOX. A single slot made every tag wait on the PREVIOUS tag's
        # consumption receipt, which is not what a receipt means: a tag stays un-receipted until
        # Claude reads it, which can be a whole turn later, and meanwhile the operator's next
        # two sentences were refused. The fence was protecting the SOCKET — one frame at a time
        # — and paid for it with the operator's speech. So the two are separated: `_lock` is the
        # socket's whole protection, and this insertion-ordered dict is the order.
        #
        # Entry state is one of unsent / posting / posted / post_unknown. `posting` and
        # `post_unknown` are both write-started, and a write-started entry is NEVER replayed.
        self._outbox: dict[str, dict] = {}
        self._resolved_tags: set[str] = set()
        self._qualified = False

    # -- introspection ------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def pending(self) -> dict | None:
        """The OLDEST entry still owed a receipt, or None."""
        for entry in self._outbox.values():
            return dict(entry)
        return None

    @property
    def outbox(self) -> tuple[dict, ...]:
        """Every entry still owed a receipt, oldest first."""
        return tuple(dict(entry) for entry in self._outbox.values())

    @property
    def unsent(self) -> tuple[dict, ...]:
        """The entries no byte of which was ever written — the only resumable ones."""
        return tuple(dict(e) for e in self._outbox.values() if e["state"] == STATE_UNSENT)

    @property
    def qualified(self) -> bool:
        return self._qualified

    def mark_qualified(self, ok: bool) -> None:
        self._qualified = bool(ok)

    def attempt(self, instruction_id: str) -> dict:
        record = self._attempts.get(instruction_id)
        return dict(record) if record else {"instruction_id": instruction_id,
                                            "state": STATE_NOT_SENT}

    def adopt_pending(self, pending: dict | None) -> None:
        self.adopt_outbox([pending] if pending else [])

    def adopt_outbox(self, entries) -> None:
        """Restore the outbox from recovery, in order.

        A tag already resolved in THIS process is not reinstated. A recovered entry whose state
        cannot be proven never-started is adopted as `post_unknown`: the WAL says a write BEGAN,
        and write-started is owed but never replayed. Only an entry the WAL proves nothing was
        written for comes back as `unsent`.
        """
        self._outbox = {}
        for entry in entries or ():
            if not entry:
                continue
            tag = entry.get("tag")
            if not isinstance(tag, str) or tag in self._resolved_tags:
                continue
            state = entry.get("state")
            self._outbox[tag] = {
                "tag": tag,
                "instruction_id": entry.get("instruction_id"),
                "text": entry.get("text", ""),
                "at": entry.get("at"),
                # Guessing the other way would replay a sentence Claude may already be acting on.
                "state": state if state in (STATE_UNSENT, STATE_POSTED) else STATE_POST_UNKNOWN,
            }

    async def enqueue(self, *, tag: str, instruction_id: str, text: str) -> dict:
        """Record a turn as UNSENT, durably, before anything is written.

        Separating the enqueue from the write authorization is what makes the outbox
        restart-resumable: an entry with no bytes behind it is one the next process may
        legitimately send, and it is the only kind that is.

        A ledger failure is NOT fatal here. Nothing was written to the socket, so the honest
        outcome is an entry that lives only for this process: worse than durable, better than
        refusing to speak because the log is full.
        """
        entry = {"tag": tag, "instruction_id": instruction_id, "text": text,
                 "at": self._now(), "state": STATE_UNSENT}
        try:
            await self._ledger.append({"kind": "enqueue", "effect": RELAY_EFFECT,
                                       "instruction_id": instruction_id, "tag": tag,
                                       "payload": f"{text} {tag}",
                                       "producer": dict(self._producer)})
        except Exception:
            pass
        self._outbox[tag] = entry
        return dict(entry)

    async def drain_unsent(self, *, send) -> list[dict]:
        """Send every UNSENT entry, oldest first, through the one serialized writer.

        Stops at the first entry that does not go: a later sentence delivered ahead of an earlier
        one is worse than a pause, because the operator said them in an order and Claude reads
        them in one.
        """
        results: list[dict] = []
        for entry in list(self.unsent):
            outcome = await send(entry)
            results.append({"tag": entry["tag"], "outcome": outcome})
            if not (isinstance(outcome, dict) and outcome.get("ok")):
                break
        return results

    def resolve(self, tag: str, *, how: str) -> bool:
        """A correlated CONSUMPTION (or a proven never-started send) settles that TAG.

        Per tag, not per session: a receipt for the third sentence says nothing about the first.
        Withdrawals and unresolved sightings settle nothing.
        """
        if how not in ("consumed", "never_started"):
            return False
        self._resolved_tags.add(tag)
        return self._outbox.pop(tag, None) is not None

    def identity(self) -> str | None:
        """None while the bound Claude is still the bound Claude. Re-read EVERY time: the
        registry AND the live fingerprint — a registry file that outlived its process must not
        pass."""
        claude = self._binding.get("claude") or {}
        pid = int(claude.get("pid") or 0)
        try:
            record = self._registry(pid)
        except Exception as exc:              # a registry read must never raise into a send
            return f"registry_unreadable:{type(exc).__name__}"
        why = relay_identity_still_holds(self._binding, record,
                                         start_granularity=self._start_granularity)
        if why is not None:
            return why
        try:
            live = self._fingerprint(pid)
        except Exception:
            return "owner_unreadable"
        if live is None:
            # Evidence UNAVAILABLE is not evidence of loss: refuse the send, keep the binding.
            return "owner_unreadable"
        bound = self._binding.get("owner") or {}
        if not live:
            return "owner_pid_gone"
        if str(live.get("start") or "") != str(bound.get("start") or ""):
            return "owner_pid_reused"
        return None

    # -- promotion ----------------------------------------------------------

    async def promote(self, instruction: dict, *, probe: bool = False) -> dict:
        """Side-effect-free authorization: identity, qualification, this tag's pending state.

        No transcript snapshot and no turn/steer label (the backend split (DESIGN.md §Backend split)): the conversation
        log owns turn state, and a transport does not need to name what kind of sentence this is.
        """
        iid = instruction.get("instruction_id")
        tag = instruction.get("tag")
        text = instruction.get("text")
        if not iid or not isinstance(tag, str) or not tag.startswith("⟨v#") \
                or not isinstance(text, str) or not text:
            raise ValueError("instruction needs instruction_id, a ⟨v#…⟩ tag and text")
        if self.busy:
            return _refusal(REFUSE_BUSY, instruction_id=iid)
        if not probe and not self._qualified:
            return _refusal(REFUSE_RELAY_NOT_QUALIFIED, instruction_id=iid)
        started = self._outbox.get(tag)
        if started is not None and started["state"] != STATE_UNSENT and not probe:
            # THE ONLY REFUSAL ABOUT THIS TAG: a frame for it has already begun. The receiver may
            # have acted on it, so sending it again is the one thing a relay post can never take
            # back. A DIFFERENT tag is not blocked — it waits for the writer, not this receipt.
            return _refusal(REFUSE_RELAY_PENDING, instruction_id=iid, pending=dict(started))
        why = await asyncio.to_thread(self.identity)
        if why is not None:
            return _refusal(REFUSE_RELAY_IDENTITY, instruction_id=iid, detail=why)
        return {"ok": True, "instruction_id": iid, "tag": tag, "text": text,
                "kind": "relay", "probe": bool(probe), "binding": self._binding,
                # CARRIED, NOT DECIDED. The loop owns "is this an interruption"; this layer owns
                # "how a frame says so". A PROBE is never one: it is a self-test line, and
                # aborting a turn for it would make connecting to a working session destructive.
                "interrupt": bool(instruction.get("interrupt")) and not probe}

    # -- actuation ----------------------------------------------------------

    async def actuate(self, authorization: dict) -> dict:
        """Persist intent → re-verify identity → connect, prove peer, auth, frame.

        RELEASE RULE, narrow by construction: `never_started` is true only when no byte of the
        user frame was written. A frame whose write began stays spent forever.
        """
        if not isinstance(authorization, dict) or not authorization.get("ok"):
            raise ValueError("refusals are not authorizations")
        if authorization.get("kind") != "relay":
            raise ValueError(f"unknown authorization kind {authorization.get('kind')!r}")
        if self._lock.locked():
            return _refusal(REFUSE_BUSY)
        async with self._lock:
            iid = authorization["instruction_id"]
            tag = authorization["tag"]
            text = authorization["text"]
            prior = self._attempts.get(iid)
            if prior and prior.get("state") != STATE_NOT_SENT:
                return _refusal("already_attempted", state=prior["state"], never_started=False,
                                sent_bytes=True, instruction_id=iid)
            started = self._outbox.get(tag)
            if not authorization.get("probe") and started is not None \
                    and started["state"] != STATE_UNSENT:
                # RE-CHECKED UNDER THE LOCK: two promotions of the SAME tag can pass the unlocked
                # check while one awaits its identity probe, and only the first may write.
                return _refusal(REFUSE_RELAY_PENDING, instruction_id=iid, pending=dict(started))

            payload = f"{text} {tag}"
            effect_id = uuid.uuid4().hex[:16]
            intent = {"kind": "intent", "effect": RELAY_EFFECT, "instruction_id": iid,
                      "effect_id": effect_id, "tag": tag, "payload": payload,
                      "binding": self._binding.get("socket"),
                      "producer": dict(self._producer),
                      "probe": bool(authorization.get("probe"))}
            try:
                await self._ledger.append(intent)
            except Exception as exc:
                # No durable record ⇒ no effect may execute. This is the one provably-safe
                # refusal: nothing was written.
                return _refusal("ledger", instruction_id=iid, detail=repr(exc))
            self._attempts[iid] = {"instruction_id": iid, "tag": tag, "state": STATE_NOT_SENT,
                                   "effect_id": effect_id, "at": self._now()}

            why = await asyncio.to_thread(self.identity)
            if why is not None:
                await self._observe(iid, effect_id, "not_done",
                                    {"refused": REFUSE_RELAY_IDENTITY, "detail": why})
                return _refusal(REFUSE_RELAY_IDENTITY, instruction_id=iid, detail=why)

            expected = int((self._binding.get("claude") or {}).get("pid") or 0)
            probe = bool(authorization.get("probe"))
            if not probe:
                # The entry exists BEFORE the write: a receipt arriving while the frame is in
                # flight resolves this, not nothing. From this instant it is write-started and
                # can never be replayed.
                self._resolved_tags.discard(tag)
                entry = self._outbox.setdefault(
                    tag, {"tag": tag, "instruction_id": iid, "text": text,
                          "at": self._now(), "state": STATE_UNSENT})
                entry.update({"instruction_id": iid, "text": text, "state": STATE_POSTING})

            report = await asyncio.to_thread(
                self._poster, self._binding.get("socket"), self._token, payload,
                expected_pid=expected,
                timeout=self._connect_timeout,
                session_id=str(self._binding.get("session_id") or ""),
                priority=("now" if authorization.get("interrupt") else ""))

            evidence = {k: v for k, v in report.items() if k != "error"}
            evidence["error"] = report.get("error", "")

            if not report.get("frame_started"):
                # NOTHING of the instruction left this process: connect refused, wrong peer, or
                # the auth line failed. Proven never-started ⇒ the caller may release.
                await self._observe(iid, effect_id, "not_done", evidence)
                if not probe:
                    self._outbox.pop(tag, None)
                reason = report.get("error") or "connect_failed"
                return _refusal(reason if reason == REFUSE_RELAY_PEER_PID
                                else "relay_connect_failed",
                                instruction_id=iid, detail=report.get("error", ""))

            if report.get("frame_written"):
                state = STATE_POSTED
                await self._observe(iid, effect_id, "done", evidence)
            else:
                state = STATE_POST_UNKNOWN
                await self._observe(iid, effect_id, "unknown", evidence)
            self._attempts[iid]["state"] = state
            entry = self._outbox.get(tag)
            if not probe and entry is not None:
                # Still owed. Absent means a receipt resolved it mid-write: settled, not lost.
                entry["state"] = state
            return {"ok": True, "state": state, "sent_bytes": True, "never_started": False,
                    "instruction_id": iid, "tag": tag, "peer_pid": report.get("peer_pid")}

    async def _observe(self, iid: str, effect_id: str, stage: str, evidence: dict) -> None:
        try:
            await self._ledger.append({"kind": "observation", "effect": RELAY_EFFECT,
                                       "instruction_id": iid, "effect_id": effect_id,
                                       "stage": stage, "evidence": evidence})
        except Exception:
            pass                  # the intent stands; recovery reports it as possible
