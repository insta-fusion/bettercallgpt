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
    def _bind(self, *, registry=REGISTRY, owner=OWNER, nonce=NONCE, env_nonce=NONCE):
        with mock.patch.object(paneg, "owner_fingerprint", return_value=owner):
            return paneg.bind_from_mod(
                nonce=nonce, session_id=REGISTRY["sessionId"], ancestor_pid=REGISTRY["pid"],
                registry=registry, ps_timeout=1.0, start_granularity=2.0,
                now=lambda: 5.0, env_nonce=env_nonce)

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
    def __init__(self, fail=False):
        self.said, self.fail = [], fail

    async def announce(self, text, origin):
        if self.fail:
            raise RuntimeError("socket gone")
        self.said.append((text, origin))


class _Session:
    def __init__(self, words=""):
        self.words = words

    def unclaimed_words(self):
        return self.words


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

    async def test_steer_asks_once_per_batch_of_unsent_words(self):
        self.daemon.session = _Session("改成先跑测试")
        await self.daemon.on_steer("s1")
        self.assertEqual(self._status()["steer"], {"id": "s1", "result": "asked"})
        self.assertEqual(self.daemon.voice.said,
                         [(daemon_mod.STEER_NUDGE, "narration")])
        await self.daemon.on_steer("s2")
        self.assertEqual(self._status()["steer"], {"id": "s2", "result": "already_asked"})
        self.assertEqual(len(self.daemon.voice.said), 1, "a second press adds nothing")
        # The hand-off happened: the next batch may be asked for again.
        self.daemon.session.words = ""
        self.daemon._publish_unsent(self.daemon.session)
        self.daemon.session.words = "再加一句"
        await self.daemon.on_steer("s3")
        self.assertEqual(self._status()["steer"]["result"], "asked")

    async def test_steer_with_nothing_unsent_says_so_and_speaks_nothing(self):
        await self.daemon.on_steer("s1")
        self.assertEqual(self._status()["steer"], {"id": "s1", "result": "nothing_unsent"})
        self.assertEqual(self.daemon.voice.said, [])

    async def test_a_steer_that_cannot_be_said_reports_it_and_can_be_retried(self):
        self.daemon.session = _Session("停一下")
        self.daemon.voice = _Voice(fail=True)
        await self.daemon.on_steer("s1")
        self.assertEqual(self._status()["steer"]["result"], "failed:RuntimeError")
        self.daemon.voice = _Voice()
        await self.daemon.on_steer("s2")
        self.assertEqual(self._status()["steer"]["result"], "asked")


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
