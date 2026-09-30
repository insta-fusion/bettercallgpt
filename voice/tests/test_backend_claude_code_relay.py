"""DESIGN.md §Acceptance E4, E6 — FIFO behind one writer, and identity loss refusing a frame.

Covers the relay's ordering contract (fixture 4): two tags both send in order behind one writer,
the drain stops at the first entry that does not go, and only an unsent entry may be resumed. The
relay carries no turn/steer label (DESIGN.md §Backend split), so none is asserted.

These run against a REAL AF_UNIX socket with a scripted peer pid, because the peer-pid check is
the property under test and a mocked socket cannot fail it the way a real one can.
"""
from __future__ import annotations

import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from voice.backend.claude_code import pane as pane_mod
from voice.backend.claude_code import relay as relay_mod
from voice.backend.claude_code.relay import (
    REFUSE_RELAY_IDENTITY,
    REFUSE_RELAY_NOT_QUALIFIED,
    REFUSE_RELAY_PEER_PID,
    REFUSE_RELAY_PENDING,
    STATE_POSTED,
    STATE_POST_UNKNOWN,
    STATE_UNSENT,
    RelayActuator,
    frame,
    post_frame,
)

SESSION = "sess-1"
# The bounds the daemon would supply. Written down HERE, in the test, because the module no
# longer carries a default — which is the point: one place names a duration.
TEST_CONNECT_TIMEOUT = 3.0
# `ps` start-time granularity, as the daemon would supply it.
TEST_START_GRANULARITY = 1.0
# The `ps` subprocess bound.
TEST_PS_TIMEOUT = 5.0
OWNER = {"pid": 4242, "start": "Sat Sep  5 23:30:58 2026", "tty": "ttys023", "comm": "claude"}


class _PeerSocket(socket.socket):
    """A socket that answers LOCAL_PEERPID with whatever pid the test wants to simulate."""

    def __init__(self, peer_pid: int) -> None:
        super().__init__(socket.AF_UNIX, socket.SOCK_STREAM)
        self._peer_pid = peer_pid

    def getsockopt(self, level, optname, buflen=None):  # type: ignore[override]
        if level == relay_mod._SOL_LOCAL and optname == relay_mod._LOCAL_PEERPID:
            return struct.pack("i", self._peer_pid)
        return super().getsockopt(level, optname, buflen) if buflen is not None \
            else super().getsockopt(level, optname)


class FakeInbox:
    """A Unix socket server storing every line it receives, per connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.frames: list[list[str]] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(1.0)
                buf = b""
                try:
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                except socket.timeout:
                    pass
                self.frames.append([line for line in buf.decode("utf-8").split("\n") if line])

    def close(self) -> None:
        self._stop = True
        self._thread.join(1.0)
        self._srv.close()


class FakeLedger:
    """Records appends. A relay only needs append to exist and to be allowed to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.records: list[dict] = []
        self.fail = fail

    async def append(self, record: dict) -> int:
        if self.fail:
            raise RuntimeError("wal full")
        self.records.append(dict(record))
        return len(self.records)


class RelayTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # The scripted socket must look like darwin for `peer_pid_of` even off-mac. Set on the
        # MODULE, because that is where `post_frame` resolves the name — and restored in
        # teardown, because a patch wide enough to reach production is wide enough to need one.
        self._real_peer_pid_of = relay_mod.peer_pid_of
        if sys.platform != "darwin":
            relay_mod.peer_pid_of = lambda sock: (
                struct.unpack("i", sock.getsockopt(relay_mod._SOL_LOCAL,
                                                   relay_mod._LOCAL_PEERPID, 4))[0]
                if isinstance(sock, _PeerSocket) else None)

        self.dir = Path(tempfile.mkdtemp(prefix="voice-relay-"))
        self.sock = self.dir / "s.sock"
        self.inbox = FakeInbox(self.sock)
        self.ledger = FakeLedger()
        self.peer_pid = 4242
        # `procStart` is the SAME instant as the owner's `lstart`, rendered as UTC ctime — so
        # the registry check passes and the tests below reach the branch they are about.
        start_epoch = pane_mod.lstart_epoch(OWNER["start"])
        proc_start = time.strftime(pane_mod._CTIME, time.gmtime(start_epoch))
        self.record = {"pid": 4242, "sessionId": SESSION, "cwd": "/tmp/wt",
                       "procStart": proc_start, "messagingSocketPath": str(self.sock)}
        self.binding = {"session_id": SESSION, "socket": str(self.sock),
                        "owner": dict(OWNER),
                        "claude": {"pid": 4242, "session_id": SESSION, "cwd": "/tmp/wt",
                                   "proc_start": proc_start, "start_epoch": start_epoch}}
        self.posts: list[dict] = []
        self.identity_reason: str | None = None

        def connect(path: str, timeout: float) -> socket.socket:
            sock = _PeerSocket(self.peer_pid)
            sock.settimeout(timeout)
            sock.connect(path)
            return sock

        self._connect = connect

        def poster(path, token, text, *, expected_pid, timeout,
                   session_id="", priority=""):
            # The actuator passes its injected bound through; the fake asserts it arrives by
            # accepting it by name and handing it straight on.
            report = post_frame(path, token, text, expected_pid=expected_pid,
                                timeout=timeout,
                                session_id=session_id, priority=priority,
                                connect=self._connect)
            self.posts.append({**report, "text": text, "priority": priority})
            return report

        self.actuator = RelayActuator(
            binding=self.binding, ledger=self.ledger, token="tok-secret",
            producer={"pid": 777, "session_id": SESSION},
            connect_timeout=TEST_CONNECT_TIMEOUT, start_granularity=TEST_START_GRANULARITY,
            ps_timeout=TEST_PS_TIMEOUT,
            registry=lambda pid: dict(self.record) if pid == 4242 else None,
            fingerprint=lambda pid: dict(OWNER) if pid == 4242 else {},
            poster=poster, now=lambda: 1000.0)
        self.actuator.mark_qualified(True)
        # Identity is re-read on every frame; tests override the verdict, not the machinery.
        self._real_identity = self.actuator.identity
        self.actuator.identity = lambda: self.identity_reason

    async def asyncTearDown(self) -> None:
        relay_mod.peer_pid_of = self._real_peer_pid_of
        self.inbox.close()

    def instruction(self, text="run the tests", tag="⟨v#0a0b0c0d⟩", iid="i-1", **extra):
        return {"instruction_id": iid, "tag": tag, "text": text, **extra}

    async def send(self, **kwargs):
        instruction = self.instruction(**kwargs)
        authorization = await self.actuator.promote(instruction)
        if not authorization.get("ok"):
            return authorization
        return await self.actuator.actuate(authorization)


class E4OrderingTests(RelayTestBase):
    """E4 — FIFO behind one writer; the drain stops at the first entry that does not go."""

    async def test_E4_two_tags_both_send_in_order_behind_one_writer(self):
        first = await self.send(text="one", tag="⟨v#aaaaaaaa⟩", iid="i-1")
        second = await self.send(text="two", tag="⟨v#bbbbbbbb⟩", iid="i-2")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual([post["text"].split(" ⟨")[0] for post in self.posts], ["one", "two"])

    async def test_E4_a_later_tag_does_not_wait_on_an_earlier_tags_receipt(self):
        """The whole point of the per-tag outbox: the lock protects the socket, not the
        operator's next sentence."""
        await self.send(text="one", tag="⟨v#aaaaaaaa⟩", iid="i-1")
        self.assertEqual(len(self.actuator.outbox), 1)
        second = await self.send(text="two", tag="⟨v#bbbbbbbb⟩", iid="i-2")
        self.assertTrue(second["ok"])

    async def test_E4_the_same_tag_is_never_written_twice(self):
        await self.send(text="one", tag="⟨v#aaaaaaaa⟩", iid="i-1")
        again = await self.send(text="one", tag="⟨v#aaaaaaaa⟩", iid="i-2")
        self.assertFalse(again["ok"])
        self.assertEqual(again["refused"], REFUSE_RELAY_PENDING)
        self.assertEqual(len(self.posts), 1)

    async def test_E4_the_drain_stops_at_the_first_entry_that_does_not_go(self):
        """A later sentence delivered ahead of an earlier one is worse than a pause."""
        await self.actuator.enqueue(tag="⟨v#aaaaaaaa⟩", instruction_id="i-1", text="one")
        await self.actuator.enqueue(tag="⟨v#bbbbbbbb⟩", instruction_id="i-2", text="two")
        await self.actuator.enqueue(tag="⟨v#cccccccc⟩", instruction_id="i-3", text="three")
        attempted: list[str] = []

        async def send(entry):
            attempted.append(entry["text"])
            return {"ok": entry["text"] == "one"}

        results = await self.actuator.drain_unsent(send=send)
        self.assertEqual(attempted, ["one", "two"], "it must stop after the first failure")
        self.assertEqual(len(results), 2)

    async def test_E4_the_drain_sends_oldest_first(self):
        for index, text in enumerate(["one", "two", "three"], start=1):
            await self.actuator.enqueue(tag=f"⟨v#{index:08x}⟩", instruction_id=f"i-{index}",
                                        text=text)
        order: list[str] = []

        async def send(entry):
            order.append(entry["text"])
            return {"ok": True}

        await self.actuator.drain_unsent(send=send)
        self.assertEqual(order, ["one", "two", "three"])

    async def test_E4_only_an_unsent_entry_may_be_resumed(self):
        await self.actuator.enqueue(tag="⟨v#aaaaaaaa⟩", instruction_id="i-1", text="one")
        self.assertEqual([e["tag"] for e in self.actuator.unsent], ["⟨v#aaaaaaaa⟩"])
        await self.send(text="one", tag="⟨v#aaaaaaaa⟩", iid="i-1")
        self.assertEqual(self.actuator.unsent, (), "a written entry is never resumable")

    async def test_E4_a_started_write_is_not_resumable_after_a_crash(self):
        """Recovery adopts an unprovable state as write-started. The receiver may already have
        acted on it, and a relayed sentence is as un-retractable as a keystroke."""
        self.actuator.adopt_outbox([{"tag": "⟨v#aaaaaaaa⟩", "instruction_id": "i-1",
                                     "text": "one", "state": "posting"}])
        self.assertEqual(self.actuator.outbox[0]["state"], STATE_POST_UNKNOWN)
        self.assertEqual(self.actuator.unsent, ())

    async def test_E4_an_enqueue_survives_a_crash_and_stays_resumable(self):
        await self.actuator.enqueue(tag="⟨v#aaaaaaaa⟩", instruction_id="i-1", text="one")
        kinds = [record["kind"] for record in self.ledger.records]
        self.assertIn("enqueue", kinds)
        revived = RelayActuator(binding=self.binding, ledger=self.ledger, token="t",
                                producer={}, connect_timeout=TEST_CONNECT_TIMEOUT,
                                start_granularity=TEST_START_GRANULARITY, ps_timeout=TEST_PS_TIMEOUT,
                                poster=lambda *a, **k: {})
        revived.adopt_outbox([{"tag": "⟨v#aaaaaaaa⟩", "instruction_id": "i-1",
                               "text": "one", "state": STATE_UNSENT}])
        self.assertEqual([e["tag"] for e in revived.unsent], ["⟨v#aaaaaaaa⟩"])

    async def test_E4_a_receipt_settles_only_its_own_tag(self):
        await self.send(text="one", tag="⟨v#aaaaaaaa⟩", iid="i-1")
        await self.send(text="two", tag="⟨v#bbbbbbbb⟩", iid="i-2")
        self.assertTrue(self.actuator.resolve("⟨v#aaaaaaaa⟩", how="consumed"))
        self.assertEqual([e["tag"] for e in self.actuator.outbox], ["⟨v#bbbbbbbb⟩"])

    async def test_E4_a_withdrawal_settles_nothing(self):
        await self.send(text="one", tag="⟨v#aaaaaaaa⟩", iid="i-1")
        self.assertFalse(self.actuator.resolve("⟨v#aaaaaaaa⟩", how="withdrawn"))
        self.assertEqual(len(self.actuator.outbox), 1)


class FramingTests(RelayTestBase):
    async def test_a_post_writes_exactly_auth_then_user_frame(self):
        outcome = await self.send(text="run the tests")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["state"], STATE_POSTED)
        lines = self.inbox.frames[-1]
        self.assertEqual(len(lines), 2)
        self.assertIn('"type": "auth"', lines[0])
        self.assertIn('"type": "user"', lines[1])

    def test_the_priority_field_rides_the_frame_only_when_set(self):
        """An omitted field defaults to `next` at the receiver, which is the defect the operator
        reported as 「你回复的不是我最新的消息」."""
        self.assertNotIn(b"priority", frame("hi", "s1"))
        self.assertIn(b'"priority": "now"', frame("hi", "s1", "now"))

    async def test_a_now_send_carries_priority_now_to_the_socket(self):
        authorization = await self.actuator.promote(self.instruction(interrupt=True))
        outcome = await self.actuator.actuate(authorization)
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.posts[-1]["priority"], "now")

    async def test_an_ordinary_send_carries_no_priority(self):
        await self.send()
        self.assertEqual(self.posts[-1]["priority"], "")

    async def test_a_probe_is_never_an_interruption(self):
        """A self-test line that aborted a running turn would make connecting destructive."""
        authorization = await self.actuator.promote(self.instruction(interrupt=True), probe=True)
        self.assertFalse(authorization["interrupt"])

    def test_the_session_id_rides_the_frame(self):
        self.assertIn(b'"session_id": "s1"', frame("hi", "s1"))

    async def test_the_close_is_event_driven_and_reports_the_drain(self):
        """No settle sleep: we half-close and read to EOF, so the peer's own close is the
        signal. That is stronger than a wait — a sleep hoped the receiver had read the bytes,
        EOF proves it did."""
        await self.send()
        self.assertTrue(self.posts[-1]["drained"])

    async def test_a_peer_that_never_closes_is_still_a_successful_send(self):
        """`drained` is evidence, not a requirement: a receiver holding the connection open has
        still been written to, and treating that as a failure would resend a landed frame."""
        report = dict(self.posts[-1]) if self.posts else {}
        report.update({"frame_written": True, "drained": False, "ok": True,
                       "frame_started": True})
        self.actuator._poster = lambda *a, **k: report
        outcome = await self.send(tag="⟨v#dddddddd⟩", iid="i-9")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["state"], STATE_POSTED)


class E6RelayIdentityTests(RelayTestBase):
    """E6, second half — a binding that stopped describing reality refuses the frame."""

    async def test_E6_identity_loss_refuses_the_frame(self):
        self.identity_reason = "owner_pid_reused"
        outcome = await self.send()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["refused"], REFUSE_RELAY_IDENTITY)
        self.assertEqual(self.posts, [], "nothing may reach the socket after identity loss")

    async def test_E6_identity_loss_is_proven_never_started(self):
        self.identity_reason = "owner_pid_gone"
        outcome = await self.send()
        self.assertTrue(outcome["never_started"])

    async def test_E6_a_wrong_peer_pid_refuses_before_the_auth_line(self):
        """A stranger must never receive our token."""
        self.peer_pid = 9999
        outcome = await self.send()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["refused"], REFUSE_RELAY_PEER_PID)
        self.assertFalse(self.posts[-1]["auth_written"])

    async def test_E6_dropping_the_pid_check_would_send_to_a_stranger(self):
        """The mutation test: with the check disabled the frame lands anyway, which is what the
        check exists to prevent."""
        self.peer_pid = 9999
        relay_mod.peer_pid_of = lambda sock: None
        if sys.platform == "darwin":
            self.skipTest("on darwin a None peer pid is itself a refusal")
        outcome = await self.send()
        self.assertTrue(outcome["ok"], "proves the earlier refusal came from the pid check")

    async def test_E6_an_unreadable_owner_refuses_rather_than_assuming_death(self):
        self.actuator.identity = self._real_identity
        self.actuator._fingerprint = lambda pid: None
        outcome = await self.send()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["refused"], REFUSE_RELAY_IDENTITY)

    async def test_E6_a_gone_owner_is_named_as_gone(self):
        self.actuator.identity = self._real_identity
        self.actuator._fingerprint = lambda pid: {}
        outcome = await self.send()
        self.assertEqual(outcome["detail"], "owner_pid_gone")

    async def test_E6_a_moved_messaging_socket_refuses(self):
        """The registry field Claude Code writes is `messagingSocketPath`; a session whose
        socket moved is no longer the one we bound."""
        self.actuator.identity = self._real_identity
        self.record["messagingSocketPath"] = str(self.dir / "elsewhere.sock")
        outcome = await self.send()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["refused"], REFUSE_RELAY_IDENTITY)
        self.assertEqual(outcome["detail"], relay_mod.REFUSE_RELAY_SOCKET_MISMATCH)
        self.assertEqual(self.posts, [])

    async def test_E6_the_same_socket_under_another_spelling_still_sends(self):
        self.actuator.identity = self._real_identity
        alias = self.dir / "alias"
        alias.symlink_to(self.dir)
        self.record["messagingSocketPath"] = str(alias / "s.sock")
        outcome = await self.send()
        self.assertTrue(outcome["ok"], outcome)

    async def test_E6_an_unqualified_relay_refuses_before_any_connect(self):
        self.actuator.mark_qualified(False)
        outcome = await self.send()
        self.assertEqual(outcome["refused"], REFUSE_RELAY_NOT_QUALIFIED)
        self.assertEqual(self.posts, [])


class LedgerFailureTests(RelayTestBase):
    async def test_a_failed_wal_append_refuses_with_nothing_written(self):
        """No durable record means no effect may execute. This is the one provably-safe
        refusal."""
        self.actuator._ledger = FakeLedger(fail=True)
        outcome = await self.send()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["refused"], "ledger")
        self.assertTrue(outcome["never_started"])
        self.assertEqual(self.posts, [])

    async def test_the_intent_is_recorded_before_the_write(self):
        await self.send()
        kinds = [record["kind"] for record in self.ledger.records]
        self.assertEqual(kinds[0], "intent")
        self.assertIn("observation", kinds)

    async def test_an_enqueue_tolerates_a_failed_append(self):
        """Nothing was written to the socket, so an entry that lives only for this process is
        worse than durable but better than refusing to speak because the log is full."""
        self.actuator._ledger = FakeLedger(fail=True)
        entry = await self.actuator.enqueue(tag="⟨v#aaaaaaaa⟩", instruction_id="i-1", text="one")
        self.assertEqual(entry["state"], STATE_UNSENT)


class PromoteContractTests(RelayTestBase):
    async def test_an_instruction_without_a_wire_tag_is_a_caller_bug(self):
        with self.assertRaises(ValueError):
            await self.actuator.promote({"instruction_id": "i", "tag": "nope", "text": "x"})

    async def test_a_refusal_is_not_an_authorization(self):
        with self.assertRaises(ValueError):
            await self.actuator.actuate({"ok": False, "kind": "relay"})

    async def test_promote_reads_no_transcript_snapshot(self):
        """the backend split (DESIGN.md §Backend split): the turn/steer label and its two refusals are gone, and with
        them the constructor's snapshot parameter."""
        authorization = await self.actuator.promote(self.instruction())
        self.assertNotIn("class", authorization)
        self.assertNotIn("turn_id", authorization)


if __name__ == "__main__":
    unittest.main()
