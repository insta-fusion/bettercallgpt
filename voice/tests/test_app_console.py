"""The call console's daemon side: a call the plugin's hooks module starts on the operator's
press (`--mod`), the Steer command, and the live fields the plugin's band draws."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice.agent.ledger import Ledger
from voice.app import daemon as daemon_mod
from voice.backend.claude_code import pane as paneg
from voice.tests.test_app_daemon import ControlChannel, _settle
from voice.tests.test_backend_claude_code_pane import OWNER, REGISTRY

NONCE = "modnonce-51ab07c3"


class ModBinding(unittest.TestCase):
    def _bind(self, *, registry=REGISTRY, owner=OWNER, nonce=NONCE, env_nonce=NONCE,
              spawned=True):
        with mock.patch.object(paneg, "owner_fingerprint", return_value=owner):
            return paneg.bind_from_mod(
                nonce=nonce, session_id=REGISTRY["sessionId"], ancestor_pid=REGISTRY["pid"],
                registry=registry, ps_timeout=1.0, start_granularity=2.0,
                now=lambda: 5.0, env_nonce=env_nonce, spawned=spawned)

    def test_a_command_claude_did_not_start_itself_is_refused(self):
        binding = self._bind(spawned=False)
        self.assertFalse(binding["bound"])
        self.assertEqual(binding["refusal"], paneg.REFUSE_NOT_SPAWNED)

    def test_started_by_claude_is_its_child_or_uvx_child_never_a_shell_under_it(self):
        from voice.app import daemon

        parents = {50: 10, 60: 40, 40: 10, 70: 41, 41: 10}
        names = {40: "uvx", 41: "zsh"}
        check = lambda me: daemon.started_by_claude(
            10, ppid=lambda: parents[me], parent_of=parents.get,
            name_of=lambda pid: names.get(pid, ""))
        self.assertTrue(check(50))       # Claude's own child: the module's spawn
        self.assertTrue(check(60))       # through uvx, itself Claude's child
        self.assertFalse(check(70))      # a shell in between: a tool call's command
        self.assertFalse(daemon.started_by_claude(0, ppid=lambda: 10))

    def test_bound_without_a_transcript_launch(self):
        binding = self._bind()
        self.assertTrue(binding["bound"], binding)
        self.assertEqual((binding["proof"], binding["dialogs"], binding["handle"]),
                         ("mod", False, ""))

    def test_every_missing_proof_refuses(self):
        cases = {
            "no nonce": dict(nonce="", env_nonce=""),
            "a nonce that is a path": dict(nonce="../x", env_nonce="../x"),
            "the environment carries another nonce": dict(env_nonce="other-nonce-1"),
            "no live owner": dict(owner={}),
            "no registry record": dict(registry=None),
            "the registry names another session": dict(
                registry={**REGISTRY, "sessionId": "someone-else"}),
        }
        for why, kw in cases.items():
            self.assertFalse(self._bind(**kw)["bound"], why)


class SteerCommand(unittest.IsolatedAsyncioTestCase):
    """`steer` is its own file, read once per id, and never disturbs `stop`."""

    FakeWatch = ControlChannel.FakeWatch

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    async def _steering(self, seen, steers):
        watch = self.FakeWatch()
        self.addCleanup(lambda: None if watch.closed else watch.close())

        async def on_command(command):
            seen.append(command)

        async def on_steer(steer_id):
            steers.append(steer_id)

        controller = daemon_mod.ControlWatcher(self.dir, on_command, on_steer=on_steer,
                                               open_watch=lambda d: watch)
        controller.start(asyncio.get_running_loop())
        self.addCleanup(controller.close)
        return watch

    async def test_a_steer_fires_once_per_id_and_a_stale_one_never(self):
        daemon_mod.atomic_write(self.dir / daemon_mod.STEER_NAME, {"id": "old", "at": 1.0})
        seen, steers = [], []
        watch = await self._steering(seen, steers)
        watch.fire()
        await _settle()
        self.assertEqual(steers, [], "a steer left by an earlier call is history")
        daemon_mod.atomic_write(self.dir / daemon_mod.STEER_NAME, {"id": "a1", "at": 2.0})
        watch.fire()
        watch.fire()                     # one write can wake the watch twice
        await _settle()
        self.assertEqual(steers, ["a1"])

    async def test_a_steer_and_a_stop_written_together_both_arrive(self):
        seen, steers = [], []
        watch = await self._steering(seen, steers)
        daemon_mod.atomic_write(self.dir / daemon_mod.STEER_NAME, {"id": "b2", "at": 3.0})
        daemon_mod.atomic_write(self.dir / daemon_mod.CONTROL_NAME,
                                {"command": "stop", "at": 3.0})
        watch.fire()
        await _settle()
        self.assertEqual((seen, steers), (["stop"], ["b2"]))

    async def test_the_cli_writes_a_fresh_id_each_time(self):
        import argparse
        import contextlib
        import io

        args = argparse.Namespace(session="sess-1")
        ids = []
        with mock.patch.object(daemon_mod, "state_dir", return_value=self.dir):
            for _ in range(2):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(daemon_mod.cmd_steer(args), 0)
                ids.append(json.loads(out.getvalue())["id"])
        self.assertNotEqual(ids[0], ids[1])
        on_disk = json.loads((self.dir / daemon_mod.STEER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["id"], ids[1])


class _Voice:
    def __init__(self):
        self.context_said = []

    async def context(self, text):
        self.context_said.append(text)


class _Session:
    def __init__(self, words=""):
        self.words = words

    def unclaimed_words(self):
        return self.words

    def claim_unsent(self):
        words, self.words = self.words, ""
        return words


class _Receipt:
    def __init__(self, outcome="posted", reason=""):
        self.outcome, self.reason = outcome, reason


class _Loop:
    def __init__(self, receipt=None, fail=False):
        self.sent, self.receipt, self.fail = [], receipt or _Receipt(), fail

    async def operator_request(self, text):
        if self.fail:
            raise RuntimeError("relay gone")
        self.sent.append(text)
        return self.receipt


class LiveView(unittest.IsolatedAsyncioTestCase):
    """What the band reads from status.json, and what a Steer press does."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.daemon = daemon_mod.VoiceDaemon(session_id="sess-1", provider="gpt_live",
                                             backend_name="process", state=self.dir)
        self.daemon.voice = _Voice()
        self.daemon.session = _Session()
        self.daemon.loop = _Loop()

    def _status(self):
        return json.loads(self.daemon.status.path.read_text(encoding="utf-8"))

    async def test_unsent_words_show_and_clear(self):
        session = _Session("帮我看一下测试")

        class Strategy:
            async def feed(self, event):
                return []

        strategy = Strategy()
        self.daemon._watch_feed(session, strategy)
        await strategy.feed(object())
        self.assertEqual(self._status()["unsent"], {"chars": 7, "preview": "帮我看一下测试"})
        session.words = ""
        await strategy.feed(object())
        self.assertEqual(self._status()["unsent"], {"chars": 0, "preview": ""})

    async def test_a_long_span_keeps_only_its_tail(self):
        session = _Session("x" * 500)
        self.daemon._publish_unsent(session)
        unsent = self._status()["unsent"]
        self.assertEqual((unsent["chars"], len(unsent["preview"])),
                         (500, daemon_mod.UNSENT_PREVIEW_MAX))

    def test_receipts_keep_the_queued_tags(self):
        receipt = {"kind": "observed", "obs": "receipt"}
        self.daemon._on_record({**receipt, "tag": "req-1", "disposition": "queued"})
        self.daemon._on_record({**receipt, "tag": "req-2", "disposition": "queued"})
        self.daemon._on_record({**receipt, "tag": "req-1", "disposition": "queued"})
        self.assertEqual(self._status()["queued"], ["req-1", "req-2"])
        self.daemon._on_record({**receipt, "tag": "req-1", "disposition": "absorbed"})
        self.daemon._on_record({**receipt, "tag": "req-2", "disposition": "consumed"})
        self.daemon._on_record({"kind": "heard", "tag": "req-9", "disposition": "queued"})
        self.assertEqual(self._status()["queued"], [])

    async def test_steer_sends_the_unsent_words_itself_once(self):
        self.daemon.session = _Session("改成先跑测试")
        self.daemon._publish_unsent(self.daemon.session)
        await self.daemon.on_steer("s1")
        self.assertEqual(self.daemon.loop.sent, ["改成先跑测试"])
        status = self._status()
        self.assertEqual(status["steer"], {"id": "s1", "result": "sent"})
        self.assertEqual(status["unsent"], {"chars": 0, "preview": ""})
        self.assertEqual(self.daemon.voice.context_said, [daemon_mod.STEER_SENT_NOTE])
        # A second press finds nothing left: nothing is sent twice.
        await self.daemon.on_steer("s2")
        self.assertEqual(self.daemon.loop.sent, ["改成先跑测试"])
        self.assertEqual(self._status()["steer"], {"id": "s2", "result": "nothing_unsent"})

    async def test_steer_with_nothing_unsent_says_so_and_sends_nothing(self):
        await self.daemon.on_steer("s1")
        self.assertEqual(self._status()["steer"], {"id": "s1", "result": "nothing_unsent"})
        self.assertEqual(self.daemon.loop.sent, [])

    async def test_a_refused_or_failed_send_is_reported(self):
        self.daemon.session = _Session("停一下")
        self.daemon.loop = _Loop(_Receipt("refused", "owner_lost"))
        await self.daemon.on_steer("s1")
        self.assertEqual(self._status()["steer"]["result"], "refused:owner_lost")
        self.assertEqual(self.daemon.voice.context_said, [])
        self.daemon.session = _Session("再说一次")
        self.daemon.loop = _Loop(fail=True)
        await self.daemon.on_steer("s2")
        self.assertEqual(self._status()["steer"]["result"], "failed:RuntimeError")


class SteerClaim(unittest.IsolatedAsyncioTestCase):
    """The words a Steer took are never handed over again by a later delegation."""

    def _session(self):
        from voice.live.gpt_live import GptLiveSession, _Fragment
        session = GptLiveSession.__new__(GptLiveSession)
        session._pending = [_Fragment(start_ms=100, text="先跑"), _Fragment(start_ms=900, text="测试")]
        session._cursor = None
        session.steer_claimed = False
        return session

    def test_claim_takes_the_words_and_moves_the_cursor(self):
        session = self._session()
        self.assertEqual(session.claim_unsent(), "先跑测试")
        self.assertEqual((session._pending, session._cursor, session.steer_claimed),
                         ([], 900, True))
        self.assertEqual(session.claim_unsent(), "")

    async def test_the_loop_sends_it_as_one_interrupting_request(self):
        from voice.agent.loop import AgentLoop

        class Backend:
            def __init__(self):
                self.sends = []

            async def send(self, text, *, tag, priority, transcript, interpretation):
                self.sends.append((text, tag, priority, transcript, interpretation))
                return _Receipt("posted")

        class Wal:
            def __init__(self):
                self.rows = []

            async def append(self, record):
                self.rows.append(record)

            def record_op(self, *args):
                self.rows.append(("op",) + args)

            def set_outcome(self, *args):
                self.rows.append(("outcome",) + args)

        loop = AgentLoop.__new__(AgentLoop)
        import itertools
        from voice.conversation.log import ConversationLog
        loop._ids = itertools.count(1)
        loop.log = ConversationLog()
        loop.ledger, loop.backend = Wal(), Backend()
        loop.changed = asyncio.Event()
        loop.stats = type("S", (), {"refused": 0, "dispatched": 0, "refusals": []})()
        receipt = await loop.operator_request("先跑测试")
        self.assertEqual(receipt.outcome, "posted")
        self.assertEqual(loop.backend.sends, [("先跑测试", "req-1", "now", "先跑测试", "先跑测试")])
        kinds = [row["kind"] if isinstance(row, dict) else row[0] for row in loop.ledger.rows]
        self.assertEqual(kinds, ["heard", "op", "outcome"], "written ahead of the send")
        # The next delegation still joins its own span: the Steer's item is no candidate.
        loop.log.commit_input("d1:span")
        loop.log.complete_transcript("d1:span", "再看一下设计")
        loop.log.response_created("d1", "user")
        self.assertEqual(loop.log.join("d1"), ("d1:span", "joined"))


class LedgerStamps(unittest.TestCase):
    def test_records_carry_at_only_when_a_clock_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.jsonl")
            ledger = Ledger(path, frozenset({"send"}))
            seen = []
            ledger.on_record = seen.append
            ledger.record_op("op-1", "req-1", "r1", "send", {})
            ledger.now = lambda: 42.5
            ledger.set_outcome("op-1", "posted")
            ledger.close()
            rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
        self.assertNotIn("at", rows[0])
        self.assertEqual(rows[1]["at"], 42.5)
        self.assertEqual([row["kind"] for row in seen], ["record_op", "outcome"])


if __name__ == "__main__":
    unittest.main()
